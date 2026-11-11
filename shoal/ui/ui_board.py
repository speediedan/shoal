#!/usr/bin/env python3
"""`shoal ui board`: each shoal's priority boards as dependency graphs, for the Herdr app's Board tab (shoal-board).

One read model per board, from this host's last digest (the open items, with each issue's parent and open blockers) and
the status rows (each role's active item and queue). A node is an open item. Its `work` says where it stands with the
roles: `active` (a role's active item), `queued` (in a role's queue, at `pos`), `queued-blocked` (queued with a recorded
blocker) or `unqueued`. Edges are `parent` (an issue to its sub-issue) and `blocks` (an open blocker to the item it
blocks). A parent that is not open on the board is kept as an `off-board` node, so the tree stays whole. The board's
history (open and done counts over time) is what `digest.write` appended to `board_history.jsonl`.

Read-only: nothing here writes to a ledger, a board or a session.
"""
from __future__ import annotations

import datetime
import json
import os
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


def role_work(rows: list[dict]) -> dict[str, dict]:
    """{item key: {work, role, pos, blocker}} from the status rows: a role's active item, else its place in a queue."""
    out: dict[str, dict] = {}
    for r in rows:
        if r.get("active"):
            out[short_key(r["active"])] = {"work": "active", "role": r["slug"], "pos": 0, "blocker": ""}
    for r in rows:
        for i, n in enumerate(r.get("next") or [], 1):
            k = short_key(n.get("item") or "")
            if not k or k in out:
                continue
            blocker = str(n.get("blocker") or "")
            out[k] = {"work": "queued-blocked" if blocker else "queued", "role": r["slug"], "pos": i, "blocker": blocker}
    return out


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
    count = lambda w: sum(1 for n in on if n["work"] == w)  # noqa: E731
    s = series(hist, now)
    closed_7d = sum(s["closed"][-7:])
    known = [o for o in s["open"] if o is not None]
    return {
        "open": len(on), "done": done, "total": len(on) + done,
        "pct_done": round(100 * done / (len(on) + done)) if on or done else 100,
        "active": count("active"), "queued": count("queued"), "queued_blocked": count("queued-blocked"),
        "unqueued": count("unqueued"), "dep_blocked": sum(1 for n in on if n["blocked_by"]),
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
            w = work.get(k, {"work": "unqueued", "role": "", "pos": 0, "blocker": ""})
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
                              "work": work.get(e["from"], {}).get("work", "unqueued"), "role": work.get(e["from"], {}).get("role", ""),
                              "pos": work.get(e["from"], {}).get("pos", 0), "blocker": "", "parent": "", "blocked_by": [],
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


def render_text(d: dict) -> str:
    out = []
    for s in d["shoals"]:
        for b in s["boards"]:
            m = b["metrics"]
            out.append(f"{s['tag']} / {b['title'] or b['id']}: {m['open']} open, {m['done']} done ({m['pct_done']}%), "
                       f"{m['active']} active, {m['queued']} queued, {m['queued_blocked']} queued and blocked, "
                       f"{m['unqueued']} unqueued; {m['closed_7d']} closed in 7 days")
            for n in b["nodes"]:
                if n.get("off_board"):
                    continue
                where = (f"{n['role']} #{n['pos']}" if n["pos"] else n["role"]) if n["role"] else ""
                out.append(f"  {n['work']:<15} {n['key']:<28} {where:<22} {n['title'][:60]}")
        if not s["boards"]:
            out.append(f"{s['tag']}: no board digest on this host")
    return "\n".join(out) or "no shoals"


def cmd_board(a) -> int:
    d = doc([a.shoal] if getattr(a, "shoal", "") else None)
    print(json.dumps(d, default=str) if a.json else render_text(d))
    return 0
