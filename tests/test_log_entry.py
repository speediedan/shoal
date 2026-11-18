#!/usr/bin/env python3
"""Hermetic proof of `shoal log-entry` (shoal/logentry.py) on a scratch log:

- a key prints that session's entries in log order, checkpoints and addenda included, and nothing else; an entry ends
  at the next heading of the same or a higher level, so its own subheadings come with it;
- a bare number matches every prefix (`12` gives `C12` and `Session 12`), a prefixed key only itself, in either case;
- `--snapshot` prints the `After ...` snapshots instead, `--any` every heading with the key; a section number
  (`Part 1`, `Phase 2`, `1. Where things are`) is never a key;
- `--last N` gives every entry of the newest N keys in the chosen mode (`--snapshot --last 2`: the two newest snapshots);
- a heading inside a fenced block is content, never an entry;
- `--list` prints the outline with line numbers and no bodies; `--json` carries line, level, key and kind;
- no match exits 1 and names the nearest keys; no key, `--last` or `--list` exits 2, as does a log that cannot be read.

Synthetic paths and ids only."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile

T = tempfile.mkdtemp(prefix="shoal-log-entry-test-")
SHOALD = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "shoal")
LOG = os.path.join(T, "lane_log.md")


def run(*args: str) -> tuple[int, str]:
    r = subprocess.run([sys.executable, os.path.join(SHOALD, "cli.py"), "log-entry", *args], capture_output=True,
                       text=True)
    return r.returncode, r.stdout


def headings(out: str) -> list[str]:
    """The printed headings, skipping fenced lines (the scratch log fences one heading-shaped line)."""
    out_, fenced = [], False
    for line in out.splitlines():
        if line.startswith("```"):
            fenced = not fenced
        elif line.startswith("#") and not fenced:
            out_.append(line)
    return out_


open(LOG, "w").write("""# lane: log

## Part 1: session entries

### Session C1 (2026-11-01) - first

C1 body.

### Session C1 addendum (2026-11-02) - a correction

C1 addendum body.

### Session C2 (2026-11-03): second

C2 body.

#### Detail of C2

C2 detail.

```
### Session C9 (inside a fence) - not an entry
```

### Session 12 (2026-11-04) - an unprefixed key

Twelve.

### Session C12 checkpoint 1 (2026-11-05) - prefixed twelve

C12 body.

### Session r3 (2026-11-06) - an r-numbered key

r3 body.

## Part 2: end-of-session snapshots

### After C1 (2026-11-02) - snapshot one

- one

### After C2 (2026-11-03) - snapshot two

- two

### After Session 12 (2026-11-04) - snapshot twelve

- twelve

## Part 3: archived

### 1. Where things are

### Phase 2 notes
""")

rc, out = run(LOG, "C1")
assert rc == 0 and headings(out) == ["### Session C1 (2026-11-01) - first",
                                     "### Session C1 addendum (2026-11-02) - a correction"], out
assert "C1 addendum body." in out and "C2" not in out and "snapshot" not in out, out
rc, out = run(LOG, "C2")
assert rc == 0 and headings(out) == ["### Session C2 (2026-11-03): second", "#### Detail of C2"], out
assert "C2 detail." in out and "inside a fence" in out and "Session 12" not in out, "a fenced heading is content"
print("session entries ok")

rc, out = run(LOG, "12")
assert headings(out) == ["### Session 12 (2026-11-04) - an unprefixed key",
                         "### Session C12 checkpoint 1 (2026-11-05) - prefixed twelve"], out
rc, out = run(LOG, "c12")
assert headings(out) == ["### Session C12 checkpoint 1 (2026-11-05) - prefixed twelve"], out
rc, out = run(LOG, "r3", "C1")
assert len(headings(out)) == 3 and headings(out)[-1].startswith("### Session r3"), "several keys, in log order"
rc, out = run(LOG, "C9")
assert rc == 1 and "no session entry for C9" in out and "nearest: C12, C2" in out, out
print("keys ok")

rc, out = run(LOG, "C2", "--snapshot")
assert rc == 0 and headings(out) == ["### After C2 (2026-11-03) - snapshot two"] and "- two" in out, out
rc, out = run(LOG, "12", "--snapshot")
assert headings(out) == ["### After Session 12 (2026-11-04) - snapshot twelve"], out
rc, out = run(LOG, "C1", "--any")
assert len(headings(out)) == 3 and headings(out)[-1] == "### After C1 (2026-11-02) - snapshot one", out
rc, out = run(LOG, "1", "--any")
assert headings(out) == ["### Session C1 (2026-11-01) - first", "### Session C1 addendum (2026-11-02) - a correction",
                         "### After C1 (2026-11-02) - snapshot one"], "section numbers (Part 1, 1. Where) are not keys"
rc, out = run(LOG, "2", "--any")
assert not any("Phase" in h or "Part" in h for h in headings(out)), out
print("modes ok")

rc, out = run(LOG, "--last", "1")
assert headings(out) == ["### Session r3 (2026-11-06) - an r-numbered key"], out
rc, out = run(LOG, "--last", "2")
assert headings(out) == ["### Session C12 checkpoint 1 (2026-11-05) - prefixed twelve",
                         "### Session r3 (2026-11-06) - an r-numbered key"], out
rc, out = run(LOG, "--snapshot", "--last", "2")
assert headings(out) == ["### After C2 (2026-11-03) - snapshot two",
                         "### After Session 12 (2026-11-04) - snapshot twelve"], "--last follows the mode"
print("--last ok")

rc, out = run(LOG, "--list")
lines = out.splitlines()
assert rc == 0 and not any("body" in x for x in lines) and not any("inside a fence" in x for x in lines), out
assert lines[0].split() == ["3", "##", "Part", "1:", "session", "entries"], lines[0]
rc, out = run(LOG, "--list", "--snapshot", "--json")
h = json.loads(out)["headings"]
assert [x["key"] for x in h] == ["C1", "C2", "12"] and {x["kind"] for x in h} == {"snapshot"}, h
rc, out = run(LOG, "C2", "--json")
e = json.loads(out)["entries"]
assert [(x["line"], x["level"], x["key"], x["kind"]) for x in e] == [(13, 3, "C2", "session")], e
assert "#### Detail of C2" in e[0]["text"], "a subheading is part of its entry"
print("--list and --json ok")

rc, out = run(LOG)
assert rc == 2 and "name a key" in out, out
rc, out = run(os.path.join(T, "missing_log.md"), "C1")
assert rc == 2 and "cannot read" in out, out
print("usage ok")

subprocess.run(["rm", "-rf", T], check=True)
print("test_log_entry OK")
