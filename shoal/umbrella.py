#!/usr/bin/env python3
"""Umbrellas and their rows: an issue with open sub-issues (an umbrella) held as a lane's item licenses no work on its
rows. A claim protects one item id; working a row under an umbrella claim never checked who held the row, so two lanes
implemented the same row for two hours and found out only when one push was rejected (measured 2026-11-12: fts-core,
active on an umbrella, built a row it-fts-integration had claimed and was building).

The board digest carries each issue's open rows (`rows`) and the open pull requests that close it (`prs`), so nothing
here reads GitHub: `next` names an umbrella's rows and who holds each, and the open PRs on what a lane holds or
intends; `queue` mails the holder of an umbrella when a row under it is claimed; `shoal claimed <item>` answers who
holds an item, its umbrella and its rows, and exits 3 when another lane holds it.
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from ledger import item_key  # noqa: E402

# relations change slowly, and a stale digest's rows are far better than none: any age is read
ANY_AGE_MIN = 1e9


def doc_for(sid: str) -> dict:
    import digest
    return digest.load(sid, max_age_min=ANY_AGE_MIN) or {}


def rows_of(doc: dict) -> dict[str, list[str]]:
    """Umbrella key -> its open rows' keys."""
    return {item_key(k): [item_key(r) for r in it.get("rows") or []]
            for k, it in (doc.get("items") or {}).items() if it.get("rows")}


def parents_of(doc: dict) -> dict[str, str]:
    """Row key -> its umbrella's key, from the rows lists and each item's own parent."""
    out = {}
    for k, it in (doc.get("items") or {}).items():
        if it.get("parent"):
            out[item_key(k)] = item_key(it["parent"])
    for u, rows in rows_of(doc).items():
        for r in rows:
            out.setdefault(r, u)
    return out


def actives(spec, view) -> dict[str, str]:
    """Item key -> the role whose active item it is."""
    out = {}
    for r in spec.roles:
        a = (view.current_queue(r.slug) or {}).get("active") or ""
        if a:
            out[item_key(a)] = r.slug
    return out


def holder(view, key: str) -> str:
    w = view.winner(key)
    return w["by"] if w is not None else ""


def _prs(doc: dict, key: str) -> list[dict]:
    it = (doc.get("items") or {}).get(key) or {}
    return [p for p in it.get("prs") or [] if p.get("state") == "OPEN"]


def next_lines(spec, view, slug: str, active: str, nxt: list[str], doc: dict | None = None) -> list[str]:
    """What `next` says about umbrellas and open PRs for this lane's active and next items."""
    doc = doc_for(spec.sid) if doc is None else doc
    rows, parents, act = rows_of(doc), parents_of(doc), actives(spec, view)
    out = []
    mine = [x for x in [active] + list(nxt) if x]
    for it in mine:
        key = item_key(it)
        if key in rows:
            listed = ", ".join(f"{r} ({'yours' if holder(view, r) == slug else holder(view, r) or 'free'})"
                               for r in rows[key])
            out.append(f"UMBRELLA {key}: an umbrella licenses no work on its rows. Claim the row you work before you "
                       f"start it (`queue --claim <row>`, then `--active <row>`); a row another lane holds is theirs. "
                       f"Rows: {listed}")
        u = parents.get(key)
        if u and act.get(u) and act[u] != slug and holder(view, key) == slug:
            out.append(f"UNDER UMBRELLA {key}: a row of {u}, which is {act[u]}'s active item; it was mailed when you "
                       f"claimed this row, so keep to the row")
        for p in _prs(doc, key):
            out.append(f"PR OPEN {key}: #{p['number']} closes it" + (f" (branch {p['branch']})" if p.get("branch") else "")
                       + f"; if it is not yours, run `shoal claimed --shoal {spec.sid} {key}` before you write code")
    return out


def claim_mail(spec, view, slug: str, claims: list[str], doc: dict | None = None) -> list[tuple[str, str]]:
    """(to, text) for each umbrella another lane holds as its active item, a row of which was just claimed."""
    doc = doc_for(spec.sid) if doc is None else doc
    parents, act = parents_of(doc), actives(spec, view)
    out = []
    for c in claims:
        key = item_key(c)
        u = parents.get(key)
        if u and act.get(u) and act[u] != slug:
            out.append((act[u], f"{slug} claimed {key}, a row of your active umbrella {u}: that row is {slug}'s now, "
                                 f"so do not implement it under {u} (`shoal claimed --shoal {spec.sid} {key}`)"))
    return out


def report(spec, view, item: str, slug: str = "", doc: dict | None = None) -> tuple[int, list[str]]:
    """(rc, lines) for `shoal claimed`: 0 when the item is free or yours, 3 when another lane holds it or it is done."""
    doc = doc_for(spec.sid) if doc is None else doc
    key = item_key(item)
    rows, parents, act = rows_of(doc), parents_of(doc), actives(spec, view)
    lines, rc = [], 0
    if key in view.done:
        d = view.done[key]
        lines.append(f"DONE {key} by {d['by']} {d['ts']}")
        rc = 3
    w = view.winner(key)
    if w is not None:
        lines.append(f"CLAIMED-BY {w['by']} since {w['ts']}" + (" (you)" if w["by"] == slug else ""))
        if w["by"] != slug:
            rc = 3
    elif key not in view.done:
        lines.append(f"FREE {key}: no live claim")
    for o in view.offers.values():
        if o["status"] == "open" and item_key(o["item"]) == key:
            lines.append(f"OFFERED to {o['to']} by {o['by']}")
    u = parents.get(key)
    if u:
        lines.append(f"UMBRELLA {u}" + (f": the active item of {act[u]}" if act.get(u) else ": nobody's active item"))
        if act.get(u) and act[u] != slug and (w is None or w["by"] == act[u]):
            lines.append(f"  {act[u]} may be working it under {u}: ask them before you start")
    if key in rows:
        lines.append(f"ROWS of {key}: " + ", ".join(f"{r} ({holder(view, r) or 'free'})" for r in rows[key]))
    for p in _prs(doc, key):
        lines.append(f"PR OPEN #{p['number']} closes it" + (f" (branch {p['branch']})" if p.get("branch") else ""))
    return rc, lines


def cmd_claimed(a) -> int:
    from ledger import LedgerError
    from worker import _load
    try:
        spec, _led, view = _load(a)
    except (LedgerError, OSError) as exc:
        print(f"ERROR: {exc}")
        return 1
    rc, lines = report(spec, view, a.item, a.slug or "")
    print("\n".join(lines))
    return rc


def add_parsers(sub) -> None:
    p = sub.add_parser("claimed", help="who holds an item, its umbrella and its rows, and open PRs that close it "
                                       "(exit 3 when another lane holds it): run it before a first push or PR")
    import config
    config.add_id_arg(p, required=True)
    p.add_argument("item", help="repo#n")
    p.add_argument("--slug", default="", help="you: your own claim exits 0")
    p.add_argument("--spec", default="")
    p.add_argument("--no-sync", action="store_true")
    p.set_defaults(func=cmd_claimed)
