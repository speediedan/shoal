#!/usr/bin/env python3
"""`shoal ui board`: each shoal's priority boards as dependency graphs, for the Herdr app's Board tab (shoal-board).

One read model per board, from this host's last digest (the open items, with each issue's parent and open blockers) and
the status rows (each role's active item, wait and queue). A node is an open item. Its `work` says where it stands with
the roles: `active` (a role's active item), `queued` (in a role's queue, at `pos`) or `unqueued`. Its `state` says where
the issue stands, from what the role recorded (`progress`):

- `in-progress`: a role is on it. The active item of a role that works or waits on its own run (CI, a job, research,
  a window, a lease), and a queued item whose recorded blocker is its own run (`job:`, `gh-run:`, `az-build:`, `time:`,
  `lease:`). `why` says which: working, ci, job, research, window, lease.
- `blocked`: it waits on something outside its own work. `why` is `issue` (another issue, `on` names it: a peer's
  `<role>:<repo#n>`, or a session-blocked wait), `upstream` (`upstream:<target>`, or an upstream wait), `maintainer`
  (an `ask`/`user` blocker, or a user wait) or `hold` (a free-text hold that names nothing).
- `queued`: in a queue with nothing recorded against it (or a blocker that has since cleared); `not-queued`: no role
  holds it.

A free-text hold that plainly describes the item's own run (a job, hosted CI, a gate) reads in progress, with
`inferred` set: most holds measured on 2026-11-19 did, from before the structured forms existed.

Edges are `parent` (an issue to its sub-issue) and `blocks` (an open blocker to the item it blocks). A parent that is
not open on the board is kept as an `off-board` node, so the tree stays whole. The board's history (open and done
counts over time) is what `digest.write` appended to `board_history.jsonl`.

Read-only: nothing here writes to a ledger, a board or a session.
"""
from __future__ import annotations

import datetime
import json
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [p for p in (HERE, os.path.dirname(HERE)) if p not in sys.path]

V = 1
DAY_S = 86400
SERIES_DAYS = 14  # the metrics panel's trend windows


def short_key(item: str) -> str:
    """`repo#n`, lower-case, for matching a queue's item with a board's: owner and verb dropped."""
    key = str(item or "").split(":", 1)[0].strip().lower()
    return key.split("/", 1)[1] if "/" in key and "#" in key else key


def _epoch(ts: str) -> float | None:
    try:
        return datetime.datetime.strptime(str(ts), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc).timestamp()
    except ValueError:
        return None


# a role's wait on its active item, as the issue's progress: its own run is progress; a peer, upstream or the maintainer
# is a block
WAIT_PROGRESS = {"local-ci": ("in-progress", "ci"), "remote-ci": ("in-progress", "ci"),
                 "research-proto": ("in-progress", "research"), "research-window": ("in-progress", "window"),
                 "queue-empty": ("in-progress", "working"), "session-blocked": ("blocked", "issue"),
                 "upstream": ("blocked", "upstream"), "user-auth": ("blocked", "maintainer"),
                 "user-design": ("blocked", "maintainer"), "user-feedback": ("blocked", "maintainer")}
RUN_WHY = {"job": "job", "gh-run": "ci", "az-build": "ci", "time": "window"}
# a free-text hold that describes the item's own run (the words roles used for it before `job:`/`gh-run:` blockers),
# read by the first such word: CI, or another run. Words a dependency is written in ("needs", "after", "waits on #n")
# are not here, so such a hold stays blocked
HOLD_WORDS = re.compile(r"\b(?:(?P<ci>ci|hosted|gates?|az-build|azure|builds?\s+\d+|matrix|legs?|checks|re-?gate"
                        r"|re-?run|suite)|(?P<lease>lease)|(?P<window>research window)"
                        r"|(?P<job>jobs?|waiter|running|runs?|queued|render))\b", re.I)


def ref_key(ref: str) -> str:
    """The item a blocker or wait target names, as `repo#n` ('' when it names none): from `gh-issue:o/r#n`,
    `role:o/r#n:verb` and the like."""
    ref = str(ref or "")
    if ref.split(":", 1)[0] in ("gh-issue", "gh-pr"):
        ref = ref.split(":", 1)[1]
    m = re.search(r"([\w.-]+/)?([\w.-]+)#(\d+)", ref)
    return f"{m.group(2).lower()}#{m.group(3)}" if m else ""


def progress(blocker: str = "", stale_kind: str = "", wait: dict | None = None, status: str = "") -> dict:
    """{state, why, on, inferred}: where an issue stands, from its role's record. A next item from its recorded
    `blocker` (a cleared one frees it); an active item (`status` given) from its role's status and wait."""
    def out(state: str, why: str, on: str = "", inferred: bool = False) -> dict:
        return {"state": state, "why": why, "on": on, "inferred": inferred}

    if status:
        if status == "parked":
            return out("in-progress", "parked")
        if status != "wait":
            return out("in-progress", "working")
        w = wait or {}
        target = str(w.get("target") or "")
        if target.startswith("lease:") and WAIT_PROGRESS.get(str(w.get("sub") or ""), ("",))[0] == "in-progress":
            return out("in-progress", "lease", target.split(":", 1)[1])
        state, why = WAIT_PROGRESS.get(str(w.get("sub") or ""), ("in-progress", "working"))
        on = ""
        if why == "issue":
            peer = str(w.get("blocked_on") or "")
            on = ref_key(peer) or peer.split(":", 1)[0]
        elif why == "upstream":
            on = ref_key(target) or target
        return out(state, why, on)
    import blockers
    b = str(blocker or "")
    if not b or stale_kind == "resolved":
        return out("queued", "")
    k = blockers.kind(b)
    if k == "role":
        return out("blocked", "issue", ref_key(b.split(":", 1)[1]) or b.split(":", 1)[0])
    if k == "upstream":
        rest = b.split(":", 1)[1] if ":" in b else ""
        return out("blocked", "upstream", ref_key(rest) or rest)
    if k == "ask":
        return out("blocked", "maintainer")
    if k == "lease":
        return out("in-progress", "lease", b.split(":", 1)[1])
    if k == "run":
        return out("in-progress", RUN_WHY.get(b.split(":", 1)[0], "job"), b)
    m = HOLD_WORDS.search(b.split(":", 1)[1] if b.startswith("hold:") else b)
    if m:
        return out("in-progress", m.lastgroup, inferred=True)
    return out("blocked", "hold")


def role_work(rows: list[dict]) -> dict[str, dict]:
    """{item key: {work, role, pos, blocker, state, why, on, inferred}} from the status rows: a role's active item, else
    its place in a queue, with the issue's `progress`."""
    out: dict[str, dict] = {}
    for r in rows:
        if r.get("active"):
            out[short_key(r["active"])] = {"work": "active", "role": r["slug"], "pos": 0, "blocker": "",
                                           **progress(wait=r.get("wait"), status=r.get("status") or "active")}
    for r in rows:
        for i, n in enumerate(r.get("next") or [], 1):
            k = short_key(n.get("item") or "")
            if not k or k in out:
                continue
            blocker = str(n.get("blocker") or "")
            out[k] = {"work": "queued", "role": r["slug"], "pos": i, "blocker": blocker,
                      **progress(blocker, str(n.get("stale_kind") or ""))}
    return out


UNQUEUED = {"work": "unqueued", "role": "", "pos": 0, "blocker": "", "state": "not-queued", "why": "", "on": "",
            "inferred": False}


def history(sid: str, source: str, now: float, path: str = "") -> list[dict]:
    """The board's recorded counts, oldest first: [{ts, open, done}] from `board_history.jsonl`."""
    import digest
    out = []
    try:
        with open(path or digest.history_path(sid)) as fh:
            for line in fh:
                try:
                    h = json.loads(line)
                except ValueError:
                    continue
                if h.get("source") == source and isinstance(h.get("ts"), (int, float)) and h["ts"] <= now:
                    out.append({"ts": h["ts"], "open": int(h.get("open") or 0), "done": int(h.get("done") or 0)})
    except OSError:
        pass
    return out


def series(hist: list[dict], now: float, days: int = SERIES_DAYS) -> dict:
    """Per day for the last `days` days, oldest first: the open count at the day's end (the last reading by then, null
    before any) and how many items closed that day (the rise in the done count; a board's done count only grows unless
    items leave the board, so a fall reads as zero)."""
    start = now - days * DAY_S
    opened, closed = [], []
    prev_done = None
    for h in hist:  # the last reading before the window is the base the first day's closes are counted from
        if h["ts"] < start:
            prev_done = h["done"]
    for d in range(days):
        end = start + (d + 1) * DAY_S
        day = [h for h in hist if start + d * DAY_S <= h["ts"] < end]
        last_by = next((h for h in reversed(hist) if h["ts"] < end), None)
        opened.append(last_by["open"] if last_by else None)
        if day:
            top = max(h["done"] for h in day)
            closed.append(max(0, top - prev_done) if prev_done is not None else 0)
            prev_done = day[-1]["done"]
        else:
            closed.append(0)
    return {"days": days, "open": opened, "closed": closed}


def board_metrics(nodes: list[dict], done: int, hist: list[dict], now: float) -> dict:
    """The panel above the graph: the rail card's counts and the trends."""
    on = [n for n in nodes if not n.get("off_board")]
    count = lambda st: sum(1 for n in on if n.get("state") == st)  # noqa: E731
    s = series(hist, now)
    closed_7d = sum(s["closed"][-7:])
    known = [o for o in s["open"] if o is not None]
    return {
        "open": len(on), "done": done, "total": len(on) + done,
        "pct_done": round(100 * done / (len(on) + done)) if on or done else 100,
        "in_progress": count("in-progress"), "queued": count("queued"), "blocked": count("blocked"),
        # blocked on a free-text hold: nothing named, so nothing clears it but the role
        "blocked_hold": sum(1 for n in on if n.get("state") == "blocked" and n.get("why") == "hold"),
        "unqueued": count("not-queued"), "dep_blocked": sum(1 for n in on if n["blocked_by"]),
        "epics": sum(1 for n in on if n["sub_issues"]),
        "closed_7d": closed_7d, "close_rate_7d": round(closed_7d / 7, 2),
        "open_change_14d": (known[-1] - known[0]) if len(known) > 1 else None,
        # at the last week's pace, the days until the open count reaches zero (null without a pace)
        "eta_days": round(len(on) / (closed_7d / 7)) if closed_7d else None,
        "series": s,
    }


def board_graph(spec_id: str, digest_doc: dict | None, rows: list[dict], now: float, hist_path: str = "") -> list[dict]:
    """The shoal's boards as graphs, in the digest's board order."""
    if not digest_doc:
        return []
    work = role_work(rows)
    items = digest_doc.get("items") or {}
    boards = []
    for b in digest_doc.get("boards") or []:
        src = b.get("source") or ""
        mine = sorted((it for it in items.values() if it.get("source") == src), key=lambda it: it.get("pos") or 0)
        keys = {short_key(it.get("item") or ""): it for it in mine}
        nodes, edges = [], []
        for k, it in keys.items():
            w = work.get(k, UNQUEUED)
            blocked_by = [short_key(x) for x in it.get("blocked_by") or []]
            parent = short_key(it.get("parent") or "")
            nodes.append({
                "key": k, "item": it.get("item") or k, "repo": it.get("repo") or "", "number": it.get("number"),
                "kind": it.get("kind") or "issue", "title": it.get("title") or "", "url": it.get("url") or "",
                "status": it.get("status") or "", "labels": list(it.get("labels") or []),
                "assignees": list(it.get("assignees") or []), "milestone": it.get("milestone") or "",
                "updated": it.get("updated") or "", "excerpt": it.get("excerpt") or "", "pos_board": it.get("pos") or 0,
                "parent": parent, "blocked_by": blocked_by, "sub_issues": int(it.get("sub_issues") or 0),
                "linked_prs": list(it.get("linked_prs") or []), **w,
            })
            if parent:
                edges.append({"from": parent, "to": k, "kind": "parent"})
            edges.extend({"from": x, "to": k, "kind": "blocks"} for x in blocked_by)
        # a parent or blocker not open on this board: kept as a node of its own, so no edge hangs loose
        known = {n["key"] for n in nodes}
        for e in edges:
            if e["from"] not in known:
                known.add(e["from"])
                nodes.append({"key": e["from"], "item": e["from"], "title": "", "url": "", "off_board": True,
                              **work.get(e["from"], UNQUEUED), "parent": "", "blocked_by": [],
                              "sub_issues": 0, "labels": [], "assignees": [], "status": "", "kind": "issue"})
        done = int(b.get("done") or 0)
        boards.append({
            "id": src.split(":", 1)[-1], "source": src, "title": b.get("title") or "", "url": b.get("url") or "",
            "done": done, "open": sum(1 for n in nodes if not n.get("off_board")),
            "closed": not any(not n.get("off_board") for n in nodes),
            "nodes": nodes, "edges": edges,
            "metrics": board_metrics(nodes, done, history(spec_id, src, now, hist_path), now),
        })
    return boards


def doc(ids: list[str] | None = None, now: float | None = None) -> dict:
    """Every shoal this host keeps a digest for (or those named), with its roles and boards."""
    import digest
    import ui_herdr
    import ui_status
    now = time.time() if now is None else now
    status = ui_status.cached_status(ids)
    hidden = set() if ids else ui_status.hidden_shoals()
    shoals = []
    for s in status.get("shoals", []):
        if s.get("error") or s.get("retired") or s["id"] in hidden:
            continue
        try:
            with open(digest.digest_path(s["id"])) as fh:
                dg = json.load(fh)
        except (OSError, ValueError):
            dg = None
        rows = s.get("roles") or []
        gen = _epoch(str((dg or {}).get("generated_utc") or ""))
        shoals.append({
            "id": s["id"], "tag": ui_herdr.shoal_tag(s["id"]),
            "roles": [{"slug": r["slug"], "fill": r.get("fill") or "", "status": ui_status.status_label(r),
                       "host": r.get("host") or ""} for r in rows if r.get("session_id")],
            "digest_age_s": int(max(0.0, now - gen)) if gen is not None else None,
            "boards": board_graph(s["id"], dg, rows, now),
        })
    return {"v": V, "kind": "shoal-board", "ts": int(now), "shoals": shoals}


def state_label(n: dict) -> str:
    """`in progress: ci`, `blocked on repo#n`, `blocked: maintainer`, `queued`: a node's state in words (`?` when read
    from a hold's text)."""
    st, why, on = n.get("state") or "", n.get("why") or "", n.get("on") or ""
    if st == "in-progress":
        return f"in progress: {why}" + ("?" if n.get("inferred") else "")
    if st == "blocked":
        if why == "issue" and on:
            return f"blocked on {on}"
        return f"blocked: {why}" + (f" {on}" if why == "upstream" and on else "")
    return "not queued" if st == "not-queued" else st


def render_text(d: dict) -> str:
    out = []
    for s in d["shoals"]:
        for b in s["boards"]:
            m = b["metrics"]
            out.append(f"{s['tag']} / {b['title'] or b['id']}: {m['open']} open, {m['done']} done ({m['pct_done']}%), "
                       f"{m['in_progress']} in progress, {m['queued']} queued, {m['blocked']} blocked "
                       f"({m['blocked_hold']} on a free-text hold), {m['unqueued']} not queued; "
                       f"{m['closed_7d']} closed in 7 days")
            for n in b["nodes"]:
                if n.get("off_board"):
                    continue
                where = (f"{n['role']} #{n['pos']}" if n["pos"] else n["role"]) if n["role"] else ""
                out.append(f"  {state_label(n):<26} {n['key']:<28} {where:<22} {n['title'][:60]}")
        if not s["boards"]:
            out.append(f"{s['tag']}: no board digest on this host")
    return "\n".join(out) or "no shoals"


def cmd_board(a) -> int:
    d = doc([a.shoal] if getattr(a, "shoal", "") else None)
    print(json.dumps(d, default=str) if a.json else render_text(d))
    return 0
