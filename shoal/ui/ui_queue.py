#!/usr/bin/env python3
"""The queue manager's read side and its batch (the Herdr app's Queue dialog; docs/ui.md).

    shoal ui queue --shoal ID --role SLUG [--json]

**Read** (`state`, read-only): one role's queue as the ledger holds it (its active item, then its next items,
each with a recorded blocker), and the candidates it could take, each with where it comes from:

- `offer`: an open offer to the role or to `*` that its queue does not list yet;
- `held`: a claim it holds that its queue omits;
- `board`: an unclaimed board item in its scope (the digest's, else the board's for a fill=self role);
- `released`: an item another role gave up (blocked, lost-race, decline), not since taken or offered;
- `other`: an item another role holds (a live claim) or was offered, with that owner.

Pending transfers and the latest queue order come from `View.transfers` and `View.queue_orders` when this
shoal has them (arch-evo's verbs); the state rules live there, not here.

**Write** (`batch`): the dialog's proposed queue, as shoal verbs in one plan, each previewed with its own
`--dry-run`:

- a board or released item a role cannot take by itself is offered first (`offer`, its position as the prio);
- an item another role holds or was offered is moved by `transfer` (the holder hands it off); it joins the
  queue once that lands, so it is left out of this order;
- dropping an open offer is a `withdraw`; dropping a held item asks the role to release it (`msg --kind
  request`); a dropped board item simply leaves the order;
- Mark blocked is one request to the role, naming each item and why;
- `queue-order` sets the order last. The role applies it at once and acks.

shoal never writes a role's queue file: every step is a verb the CLI checks, and the role's own `queue`
write applies the order.
"""
from __future__ import annotations

import datetime
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.dirname(HERE)
for _p in (PKG, HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

RELEASED_DAYS = 14  # how far back a release still reads as a candidate
BOARD_LIMIT = 12
OTHER_SRCS = ("other",)
BATCH_VERBS = ("transfer", "queue-order")  # arch-evo's verbs the batch runs beside msg, offer and withdraw
REFUSED = "REFUSED "


class QueueError(ValueError):
    pass


def _key(item: str) -> str:
    from ledger import item_key
    return item_key(item or "")


def _item(e: dict) -> str:
    from ledger import fmt_item
    return fmt_item(e) if e.get("verb") and ":" not in str(e.get("item") or "") else str(e.get("item") or "")


def _since(days: float) -> str:
    t = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=days)
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def _titles(sid: str) -> dict[str, str]:
    """{item key: title} from the digest, when there is one (any age: titles do not go stale)."""
    try:
        import digest
        doc = digest.load(sid, max_age_min=1e9) or {}
    except Exception:  # noqa: BLE001 - titles are a nicety
        return {}
    return {_key(k): str(v.get("title") or "") for k, v in (doc.get("items") or {}).items() if isinstance(v, dict)}


def _board(spec, view, slug: str, exclude: set[str]) -> list[dict]:
    """The role's board candidates: the digest's, else the board's for a fill=self role. [] when unreadable."""
    taken = view.taken() | view.offered_open() | exclude
    declined = set(view.declined(slug))
    role = spec.role(slug)
    try:
        if (spec.features or {}).get("digest"):
            from digest import candidates as dc
            got = dc(spec.sid, slug, taken, declined, limit=BOARD_LIMIT)
            if got is not None:
                return [{"item": f"{c['item']}:{c['verb']}", "title": c.get("title") or "", "pos": c.get("pos"),
                         "score": c.get("score"), "weak": bool(c.get("weak"))} for c in got]
        if role is None or role.fill != "self" or not spec.sources:
            return []
        from sources import candidates
        return [{"item": f"{c['item']}:{c['verb']}", "title": c.get("title") or "", "pos": c.get("pos")}
                for c in candidates(spec.sources, role.scope, taken, declined, limit=BOARD_LIMIT)]
    except Exception:  # noqa: BLE001 - a board read never blocks the dialog
        return []


def _call(view, name: str, *args) -> list:
    """A View query this shoal may not have yet (arch-evo's transfers and queue orders): [] without it."""
    f = getattr(view, name, None)
    if not callable(f):
        return []
    try:
        got = f(*args)
    except Exception:  # noqa: BLE001
        return []
    if got is None:
        return []
    return list(got) if isinstance(got, (list, tuple)) else [got]


def state(spec, view, slug: str) -> dict:
    """The role's queue and its candidates (module doc). Read-only."""
    import ui_graph
    role = spec.role(slug)
    if role is None:
        raise QueueError(f"{slug} is not a role of {spec.sid}")
    repos = ui_graph.repo_map(spec)
    titles = _titles(spec.sid)

    def row(item: str, src: str, **kw) -> dict:
        r = {"item": item, "key": _key(item), "src": src, "title": kw.pop("title", "") or titles.get(_key(item), ""),
             "url": ui_graph.item_url(item, repos) or ""}
        r.update({k: v for k, v in kw.items() if v not in (None, "")})
        return r

    held = {_key(c["item"]): c for c in view.active(slug)}
    offered = {_key(o["item"]): o for o in view.queue(slug)}
    q = view.current_queue(slug) or {}
    blocked = {_key(k): str(v) for k, v in (q.get("next_blocked") or {}).items()}

    def how(key: str) -> str:
        return "held" if key in held else "offer" if key in offered else "board"

    if q:
        active = q.get("active") or ""
    else:  # no queue version yet: its oldest live claim is what it works
        active = _item(min(held.values(), key=lambda c: c["ts"])) if held else ""
    queue = []
    if active:
        queue.append(row(str(active), how(_key(active)), active=True))
    for it in q.get("next") or []:
        k = _key(it)
        queue.append(row(str(it), how(k), blocked=blocked.get(k, ""),
                         prio=(offered[k].get("prio") if k in offered else None)))
    listed = {r["key"] for r in queue}

    cands = []
    for k, o in offered.items():
        if k not in listed:
            cands.append(row(_item(o), "offer", by=o.get("by", ""), prio=o.get("prio"), note=o.get("note", ""),
                             to=o.get("to", ""), now=bool(o.get("now")) or None))
    for k, c in held.items():
        if k not in listed and k not in offered:
            cands.append(row(_item(c), "held", since=c.get("ts", "")))
    seen = listed | {c["key"] for c in cands}

    # items other roles hold or were offered: a transfer moves them
    others = []
    for r in spec.roles:
        if r.slug == slug:
            continue
        for c in view.active(r.slug):
            k = _key(c["item"])
            if k not in seen:
                others.append(row(_item(c), "other", owner=r.slug, how="claimed", since=c.get("ts", "")))
                seen.add(k)
        for o in view.queue(r.slug):
            k = _key(o["item"])
            if o.get("to") == r.slug and k not in seen:
                others.append(row(_item(o), "other", owner=r.slug, how="offered", prio=o.get("prio"), by=o.get("by", "")))
                seen.add(k)

    # what others gave up lately, not since taken, offered or done
    taken = view.taken() | view.offered_open()
    released, cutoff = {}, _since(RELEASED_DAYS)
    for e in view.releases:
        k = _key(e.get("item", ""))
        if (e.get("by") == slug or e.get("kind") not in ("blocked", "lost-race", "decline") or e.get("ts", "") < cutoff
                or k in taken or k in seen):
            continue
        if k not in released or e["ts"] > released[k]["ts"]:
            released[k] = e
    rel = [row(_item(e), "released", by=e.get("by", ""), kind=e.get("kind", ""), reason=e.get("reason", ""), since=e.get("ts", ""))
           for e in sorted(released.values(), key=lambda e: e["ts"], reverse=True)]
    seen |= set(released)

    board = [row(b["item"], "board", title=b.get("title", ""), pos=b.get("pos"), weak=b.get("weak") or None)
             for b in _board(spec, view, slug, seen) if _key(b["item"]) not in seen]

    transfers = [t for t in _call(view, "transfers")
                 if isinstance(t, dict) and slug in (t.get("to"), t.get("from"))]
    orders = [o for o in _call(view, "queue_orders", slug) if isinstance(o, dict)]
    from ledger import queue_next_max
    return {"role": slug, "fill": role.fill, "next_max": queue_next_max(spec), "queue": queue,
            "candidates": cands + board + rel + others,
            "transfers": [{k: t.get(k) for k in ("id", "item", "from", "to", "prio", "status", "req", "note", "ts")} for t in transfers],
            "order": ({k: orders[-1].get(k) for k in ("id", "order", "status", "ts", "note")} if orders else None),
            "verbs": has_verbs()}


def has_verbs() -> dict[str, bool]:
    """Whether this checkout's shoal has arch-evo's queue verbs yet (the dialog says so before a plan; a dry run of a
    verb shoal lacks is refused by its parser either way)."""
    import ledger_cli
    import worker
    return {v: any(callable(getattr(m, "cmd_" + v.replace("-", "_"), None)) for m in (ledger_cli, worker)) for v in BATCH_VERBS}


def text(doc: dict) -> str:
    """The state for a terminal."""
    out = [f"{doc['role']} (fill={doc['fill']}, next at most {doc['next_max']})"]
    for r in doc["queue"]:
        out.append(f"  {'ACTIVE' if r.get('active') else 'next  '} {r['item']:<32} {r['src']:<6}"
                   + (f" blocked: {r['blocked']}" if r.get("blocked") else "") + (f" :: {r['title'][:60]}" if r["title"] else ""))
    if not doc["queue"]:
        out.append("  (empty queue)")
    for c in doc["candidates"]:
        who = f" ({c['owner']}, {c['how']})" if c["src"] == "other" else f" (by {c['by']})" if c.get("by") else ""
        out.append(f"  + {c['src']:<8} {c['item']:<32}{who}" + (f" :: {c['title'][:60]}" if c["title"] else ""))
    for t in doc["transfers"]:
        out.append(f"  transfer {t.get('item')} {t.get('from')} -> {t.get('to')}: {t.get('status')}")
    if doc["order"]:
        o = doc["order"]
        out.append(f"  queue order {','.join(o.get('order') or [])}: {o.get('status')}")
    return "\n".join(out)


def _items(csv: str) -> list[str]:
    return [x.strip() for x in (csv or "").split(",") if x.strip()]


def batch(spec, view, slug: str, order: list[str], drop: list[str], blocked: list[str], note: str = "") -> dict:
    """The proposed queue as steps: {"argvs", "placed", "transfer", "summary"} (module doc). Raises QueueError."""
    st = state(spec, view, slug)
    sid = spec.sid
    rows = {r["key"]: r for r in st["queue"] + st["candidates"]}
    current = [r["key"] for r in st["queue"] if not r.get("active")]
    active = next((r for r in st["queue"] if r.get("active")), None)
    if len(order) != len({_key(x) for x in order}):
        raise QueueError("an item is in the proposed queue twice")
    if len([x for x in order if rows.get(_key(x), {}).get("src") not in OTHER_SRCS]) > st["next_max"]:
        raise QueueError(f"{slug}'s queue takes at most {st['next_max']} next items (the group's next_max)")
    offers, transfers, placed, summary, offered, moved = [], [], [], [], [], []
    for pos, it in enumerate(order):
        k = _key(it)
        r = rows.get(k)
        if r is None:
            raise QueueError(f"{it} is neither in {slug}'s queue nor one of its candidates (read the queue again)")
        if active and k == active["key"]:
            raise QueueError(f"{it} is {slug}'s active item: it stays pinned, not in the next list")
        prio = 10 + pos
        if r["src"] in OTHER_SRCS:
            transfers.append(["transfer", "--shoal", sid, f"--item={r['item']}", "--to", slug, f"--prio={prio}"]
                             + ([f"--note={note}"] if note else []))
            moved.append(r["item"])
            summary.append(f"move {r['item']} from {r['owner']} ({r['how']}) to {slug}: it joins the queue once handed off")
            continue
        if r["src"] == "released" or (r["src"] == "board" and st["fill"] != "self"):
            offers.append(["offer", "--shoal", sid, "--to", slug, f"--item={r['item']}", f"--prio={prio}"]
                          + ([f"--note={note}"] if note else []))
            offered.append(r["item"])
            summary.append(f"offer {r['item']} to {slug} (prio {prio})")
        placed.append(r["item"])
    withdraws, release_asks = [], []
    for it in drop:
        k = _key(it)
        r = rows.get(k)
        if r is None or k in {_key(x) for x in order}:
            raise QueueError(f"{it} cannot be dropped: it is not in {slug}'s queue (or is also placed)")
        if r.get("active"):
            raise QueueError(f"{it} is {slug}'s active item: hand it off or message the role instead of dropping it")
        if r["src"] == "offer":
            withdraws.append(["withdraw", "--shoal", sid, f"--item={r['item']}", "--to", slug])
            summary.append(f"withdraw the offer of {r['item']} to {slug}")
        elif r["src"] == "held":
            release_asks.append(r["item"])
            summary.append(f"ask {slug} to release {r['item']}")
        else:
            summary.append(f"leave {r['item']} out of the order")
    notes = []
    for b in blocked:
        it, _, why = b.partition("=")
        if _key(it) not in rows or not why.strip():
            raise QueueError(f"mark blocked takes item=reason for an item in the queue ({b!r})")
        notes.append(f"{it.strip()}: {why.strip()}")
        summary.append(f"tell {slug} {it.strip()} is blocked ({why.strip()})")
    changed = [_key(x) for x in placed] != current or bool(offers or withdraws or release_asks)
    orders = [["queue-order", "--shoal", sid, "--to", slug, f"--next={','.join(placed)}"] + ([f"--note={note}"] if note else [])] \
        if changed else []
    if orders:
        summary.append(f"set {slug}'s next items to {', '.join(placed) or '(none)'}")
    msgs = []
    if release_asks or notes:
        text = " ".join(
            ([f"Release from your queue: {', '.join(release_asks)} (`release --item <it> --kind decline --reason \"dropped by the maintainer\"`)."]
             if release_asks else [])
            + ([f"Blocked, record each with `queue --blocked <item>=<what it waits on>`: {'; '.join(notes)}."] if notes else []))
        msgs.append(["msg", "--shoal", sid, "--to", slug, "--kind", "request", "--ack", f"--text={text}"]
                    + ([] if orders or transfers else ["--wake"]))
    argvs = withdraws + offers + transfers + msgs + orders
    if not argvs:
        raise QueueError(f"no change to {slug}'s queue")
    return {"argvs": argvs, "placed": placed, "offered": offered, "transfer": moved, "summary": summary}


def refusals(output: str) -> dict[str, str]:
    """{item: reason} from a verb's `REFUSED <item>: <reason>` lines."""
    out = {}
    for line in (output or "").splitlines():
        line = line.strip()
        if line.startswith(REFUSED) and ": " in line[len(REFUSED):]:
            it, _, why = line[len(REFUSED):].partition(": ")  # an item has its own colon (repo#n:verb), never ": "
            out[it.strip()] = why.strip()
    return out


def cmd_queue(a) -> int:
    import json
    from ledger import Ledger, View
    from roles import ShoalSpec, find_spec
    try:
        spec = ShoalSpec.load(find_spec(a.sid))
        doc = state(spec, View(Ledger(spec.sid, root=spec.ledger_dir or None, sync=False), spec), a.role)
    except (QueueError, Exception) as exc:  # noqa: BLE001 - said plainly, exit 2
        print(json.dumps({"v": 1, "kind": "shoal-act-error", "error": str(exc)}) if a.json else f"ERROR: {exc}")
        return 2
    print(json.dumps(dict({"v": 1, "kind": "shoal-act-view", "verb": "queue", "shoal": spec.sid}, **doc), indent=1)
          if a.json else text(doc))
    return 0


def add_parser(u) -> None:
    q = u.add_parser("queue", help="one role's queue and the candidates it could take (the Queue dialog's read; read-only)")
    q.add_argument("--shoal", dest="sid", required=True)
    q.add_argument("--role", required=True)
    q.add_argument("--json", action="store_true")
    q.set_defaults(func=cmd_queue)
