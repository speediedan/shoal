#!/usr/bin/env python3
"""The start-doc contract (docs/start-doc.md): what a session's plan_state must hold for a fresh session to continue from
it alone, and `shoal doc-check`, which says whether a doc meets it.

    shoal doc-check <path> [--since UTC] [--json]

A start doc is a present-tense live plan. Required:
  - a `Last updated: <UTC timestamp>` line (the handover's proof of refresh);
  - a mission, a current state (or a "resume here" section), the next steps, and a grants-needed section.
Recommended: decisions in force, running monitors, and an append-only companion log that keeps history out of the doc.
Sections are found by keyword in the headings, case-insensitive, so existing docs ("Mission and deliverables", "Current
state (2026-10-22)", "Resume here", "Grants-needed") qualify without a rewrite. Read-only; no model calls.
"""
from __future__ import annotations

import datetime
import json
import os
import re
import sys

REQUIRED = {
    "mission": ("mission",),
    "current state": ("current state", "resume here", "state of play", "where things are"),
    "next steps": ("next", "resume here"),
    "grants-needed": ("grants-needed", "grants needed"),
}
RECOMMENDED = {
    "decisions": ("decision",),
    "running monitors": ("running monitors", "monitors"),
}
STAMP = re.compile(r"^\W*last updated\W*:?\s*\**\s*([0-9]{4}-[0-9]{2}-[0-9]{2}[T ][0-9]{2}:[0-9]{2}(?::[0-9]{2})?\s*(?:Z|UTC|[+-][0-9]{2}:?[0-9]{2}))",
                   re.I | re.M)
HEADING = re.compile(r"^#{1,4}\s+(.+?)\s*$", re.M)
FUTURE_SLACK_MIN = 10


def parse_ts(text: str) -> datetime.datetime | None:
    t = text.strip().replace(" UTC", "Z").replace("UTC", "Z").replace(" ", "T", 1)
    t = re.sub(r"T(\d{2}:\d{2})Z$", r"T\1:00Z", t)
    t = t.replace("Z", "+00:00")
    t = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", t)
    try:
        d = datetime.datetime.fromisoformat(t)
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=datetime.timezone.utc)


def last_updated(text: str) -> datetime.datetime | None:
    """The newest `Last updated:` stamp in the doc (a doc may carry one per section; the newest counts)."""
    stamps = [parse_ts(m.group(1)) for m in STAMP.finditer(text)]
    stamps = [s for s in stamps if s is not None]
    return max(stamps) if stamps else None


def check(path: str, since: datetime.datetime | None = None, now: datetime.datetime | None = None) -> dict:
    """{ok, problems, warnings, last_updated, path}. `since`: the stamp must be at or after it (the refresh proof)."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    out = {"path": path, "ok": False, "problems": [], "warnings": [], "last_updated": None}
    try:
        text = open(os.path.expanduser(path)).read()
    except OSError as exc:
        out["problems"].append(f"cannot read the start doc: {exc}")
        return out
    heads = [h.lower() for h in HEADING.findall(text)]
    for name, keys in REQUIRED.items():
        if not any(k in h for h in heads for k in keys):
            out["problems"].append(f"no {name} section (a heading naming one of: {', '.join(keys)})")
    for name, keys in RECOMMENDED.items():
        if not any(k in h for h in heads for k in keys):
            out["warnings"].append(f"no {name} section (recommended)")
    lu = last_updated(text)
    if lu is None:
        out["problems"].append("no `Last updated: <UTC timestamp>` line (for example `Last updated: 2026-10-22T19:05Z`)")
    else:
        out["last_updated"] = lu.strftime("%Y-%m-%dT%H:%M:%SZ")
        if lu > now + datetime.timedelta(minutes=FUTURE_SLACK_MIN):
            out["problems"].append(f"`Last updated` {out['last_updated']} is in the future (use UTC)")
        if since is not None and lu < since:
            out["problems"].append(f"`Last updated` {out['last_updated']} is older than {since.strftime('%Y-%m-%dT%H:%M:%SZ')}: "
                                   f"refresh the doc (current state, decisions, next steps) and its stamp")
    out["ok"] = not out["problems"]
    return out


def cmd_doc_check(a) -> int:
    since = parse_ts(a.since) if a.since else None
    if a.since and since is None:
        print(f"doc-check: --since {a.since!r} is not a timestamp (use UTC, for example 2026-10-22T19:05Z)")
        return 2
    r = check(a.path, since)
    if a.json:
        print(json.dumps(r))
    else:
        print(f"{'ok' if r['ok'] else 'FAIL'}  {r['path']}  (last updated {r['last_updated'] or 'none'})")
        for p in r["problems"]:
            print(f"  problem: {p}")
        for w in r["warnings"]:
            print(f"  note:    {w}")
    return 0 if r["ok"] else 1


def add_parser(sub) -> None:
    p = sub.add_parser("doc-check", help="does a start doc (plan_state) meet the start-doc contract? (read-only)")
    p.add_argument("path")
    p.add_argument("--since", default="", help="the Last updated stamp must be at or after this UTC time")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_doc_check)


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    add_parser(ap.add_subparsers(dest="cmd", required=True))
    a = ap.parse_args()
    sys.exit(a.func(a))
