#!/usr/bin/env python3
"""One session's entries from an append-only log, without reading the log (docs/lifecycle.md, "Reading history").

    shoal log-entry <log> [<key> ...] [--snapshot | --any] [--last N] [--list] [--json]

A log keeps a role's history out of its start doc: session entries (`### Session C12 (2026-11-16) - ...`) and
end-of-session snapshots (`### After C12 ...`). A restart reads the start doc, not the log; when it does need the log, it
needs one entry, as a peer needs only a start doc's header (`shoal doc-head`).

An entry is a heading at levels 2 to 4 and its body, up to the next heading at the same or a higher level. Headings
inside fenced blocks are content (`startdoc.prose`). A heading's key is its first session token, after at most two
words: `C12`, `r22`, or a bare `12` when a session word comes before it (`Session 12`, `After Session 12`), so section
numbers (`Part 1`, `Phase 2`, `1. Where things are`) are never keys. A bare number matches any prefix (`12` matches
`C12`, `r12` and `12`); a prefixed key matches only itself, in either case.

By default only session entries print: headings whose words before the key are `Session`, so `Session C4 checkpoint 2`
and `Session C1 addendum` come with `C4` and `C1`. `--snapshot` prints the `After ...` snapshots instead, and `--any`
every heading with that key. `--last N` prints every entry of the newest N keys in the chosen mode. `--list` prints the
heading outline with line numbers and no bodies. No match exits 1 and names the nearest keys; a log that cannot be read
exits 2. Read-only; no model calls.
"""
from __future__ import annotations

import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import startdoc  # noqa: E402

HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
KEYED = re.compile(r"^((?:[A-Za-z]+\s+){0,2}?)([A-Za-z]?)(\d+)\b")
MODES = ("session", "snapshot", "any")
SESSION_WORDS = {"session", "after", "handoff", "earlier"}  # a bare number after one of these is a session key


def entries(text: str) -> list[dict]:
    """Every heading at levels 2 to 4: {line (1-based), level, heading, lead, key, kind, text}. `key` is "" for a heading
    with no session token; `kind` is session, snapshot or other."""
    lines = text.splitlines(keepends=True)
    heads = []
    for i, (line, ok) in enumerate(zip(lines, startdoc.prose(lines))):
        m = HEADING.match(line.rstrip("\n")) if ok else None
        if m:
            heads.append((i, len(m.group(1)), m.group(2)))
    out = []
    for n, (i, level, title) in enumerate(heads):
        if not 2 <= level <= 4:
            continue
        end = next((j for j, lv, _ in heads[n + 1:] if lv <= level), len(lines))
        m = KEYED.match(title)
        lead = m.group(1).split() if m else []
        words = [w.lower() for w in lead]
        if m and not m.group(2) and not SESSION_WORDS & set(words):
            m, lead, words = None, [], []  # a section number (Part 1, 1. Where things are), not a session
        kind = ("session" if m and words == ["session"] else
                "snapshot" if m and words[:1] == ["after"] else "other")
        out.append({"line": i + 1, "level": level, "heading": title, "lead": " ".join(lead),
                    "key": (m.group(2) + m.group(3)) if m else "", "kind": kind,
                    "text": "".join(lines[i:end]).rstrip() + "\n"})
    return out


def matches(key: str, want: str) -> bool:
    """Does a heading's key answer a requested one? A bare number matches any prefix; a prefixed key, only itself."""
    km, wm = re.fullmatch(r"([A-Za-z]?)(\d+)", key), re.fullmatch(r"([A-Za-z]?)(\d+)", want)
    if not km or not wm or int(km.group(2)) != int(wm.group(2)):
        return False
    return not wm.group(1) or km.group(1).lower() == wm.group(1).lower()


def select(es: list[dict], keys: list[str], mode: str, last: int = 0) -> list[dict]:
    pool = [e for e in es if e["key"] and (mode == "any" or e["kind"] == mode)]
    if keys:
        pool = [e for e in pool if any(matches(e["key"], k) for k in keys)]
    if last:
        newest = []
        for e in reversed(pool):
            if e["key"].lower() not in newest:
                newest.append(e["key"].lower())
        pool = [e for e in pool if e["key"].lower() in newest[:last]]
    return pool


def nearest(es: list[dict], keys: list[str], mode: str, n: int = 3) -> list[str]:
    present = []
    for e in es:
        if e["key"] and (mode == "any" or e["kind"] == mode) and e["key"] not in present:
            present.append(e["key"])
    want = [re.fullmatch(r"([A-Za-z]?)(\d+)", k) for k in keys]
    want = [(m.group(1).lower(), int(m.group(2))) for m in want if m]

    def distance(k: str) -> tuple[int, int]:
        prefix, num = re.fullmatch(r"([A-Za-z]?)(\d+)", k).groups()
        # the same prefix first (C9 is nearer C12 than 12 or r3), then the numeric distance
        return min((int(bool(p) and p != prefix.lower()), abs(int(num) - w)) for p, w in want)
    return sorted(present, key=distance)[:n] if want else []


def cmd_log_entry(a) -> int:
    mode = "snapshot" if a.snapshot else "any" if a.any else "session"
    try:
        with open(os.path.expanduser(a.log), errors="replace") as fh:
            text = fh.read()
    except OSError as exc:
        print(f"log-entry: cannot read {a.log}: {exc}")
        return 2
    es = entries(text)
    if a.list:
        shown = select(es, a.keys, mode, a.last) if (a.keys or a.last or a.snapshot or a.any) else es
        if a.json:
            print(json.dumps({"log": a.log, "mode": mode, "headings": [{k: v for k, v in e.items() if k != "text"}
                                                                       for e in shown]}, indent=1))
        else:
            for e in shown:
                print(f"{e['line']:>6}  {'#' * e['level']} {e['heading']}")
        return 0
    if not a.keys and not a.last:
        print("log-entry: name a key (C12, r22, 12), or --last N, or --list")
        return 2
    picked = select(es, a.keys, mode, a.last)
    if not picked:
        near = nearest(es, a.keys, mode)
        what = ", ".join(a.keys) if a.keys else f"the last {a.last}"
        kind = {"session": "session ", "snapshot": "snapshot ", "any": ""}[mode]
        print(f"log-entry: no {kind}entry for {what} in {a.log}" + (f"; nearest: {', '.join(near)}" if near else ""))
        return 1
    if a.json:
        print(json.dumps({"log": a.log, "mode": mode, "entries": picked}, indent=1))
    else:
        print("\n".join(e["text"] for e in picked), end="")
    return 0


def add_parser(sub) -> None:
    p = sub.add_parser("log-entry", help="print one session's entries from an append-only log, by key (C12, r22, 12) "
                                         "(read-only)")
    p.add_argument("log")
    p.add_argument("keys", nargs="*", help="session keys: C12, r22, or a bare number for any prefix")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--snapshot", action="store_true", help="the end-of-session snapshots (After ...) instead")
    g.add_argument("--any", action="store_true", help="every heading with the key")
    p.add_argument("--last", type=int, default=0, metavar="N", help="every entry of the newest N keys, in the mode")
    p.add_argument("--list", action="store_true", help="the heading outline with line numbers, no bodies")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_log_entry)


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    add_parser(ap.add_subparsers(dest="cmd", required=True))
    a = ap.parse_args()
    sys.exit(a.func(a))
