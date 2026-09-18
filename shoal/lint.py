#!/usr/bin/env python3
"""Queue-file linter + commit ownership gate (readme (d), proposal §7).

  hag.py lint-queues --spec hag.json            # every consumer queue in the spec
  hag.py lint-queues --file <queue.md> ...      # explicit files
  hag.py lint-queues --staged [--file ...]      # pre-commit: refuse a commit that
                                                # edits BOTH sections of one file

Checks (each a one-line finding, exit 1 on any ERROR, 0 on WARN-only):
  E1 QUEUE section unreadable/empty (never-hash-emptiness applies to queues too)
  E2 QUEUE numbering not sequential from 1 (renumber on refill)
  E3 STATUS missing a required key: state, active-issue, waiting-on, updated-utc, last-done
  E4 STATUS state not canonical (active|waiting|idle|paused|offline)
  E5 STATUS waiting with no holder in waiting-on
  E6 last-done holds more than 3 entries (cap: 3 most recent)
  E7 [--staged] one commit touches both QUEUE and STATUS lines of one file
  W1 updated-utc older than --stale-hours (default 24) while state is active
  W2 declined: line without a reason
  W3 QUEUE item without a verb (:fix / :review / :scope ...)
"""
from __future__ import annotations

import argparse
import datetime
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from queue_section import QueueReadError, read_queue  # noqa: E402

REQUIRED_STATUS = ("state", "active-issue", "waiting-on", "updated-utc", "last-done")
VALID_STATES = {"active", "waiting", "idle", "paused", "offline"}
VERB_RE = re.compile(r":(fix|review|scope|design|verify|triage|merge|doc|test|release)\b")


def split_sections(text: str) -> tuple[str, str]:
    qi, si = text.find("## QUEUE"), text.find("## STATUS")
    if qi < 0 or si < 0 or si < qi:
        return "", ""
    return text[qi:si], text[si:]


def lint_file(path: str, stale_hours: float) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    try:
        text = open(path).read()
    except OSError as exc:
        return [("E1", f"unreadable: {exc}")]
    try:
        _h, ids = read_queue(path)
    except QueueReadError as exc:
        out.append(("E1", f"QUEUE: {exc}"))
        ids = []
    queue, status = split_sections(text)
    nums = [int(m) for m in re.findall(r"^(\d+)\.\s+\*\*", queue, flags=re.M)]
    if nums and nums != list(range(1, len(nums) + 1)):
        out.append(("E2", f"QUEUE numbering {nums} is not 1..{len(nums)} (renumber on refill)"))
    for item in ids:
        if not VERB_RE.search(item):
            out.append(("W3", f"QUEUE item without verb: {item[:60]!r}"))
    keys = {}
    for k in REQUIRED_STATUS:
        m = re.search(rf"^- {re.escape(k)}:\s*(.*)$", status, flags=re.M)
        if not m:
            out.append(("E3", f"STATUS missing key {k}"))
        else:
            keys[k] = m.group(1).strip()
    st = keys.get("state", "")
    if st and st.split()[0] not in VALID_STATES:
        out.append(("E4", f"STATUS state {st!r} not canonical {sorted(VALID_STATES)}"))
    if st.startswith("waiting") and keys.get("waiting-on", "").lower() in ("", "none", "-"):
        out.append(("E5", "STATUS waiting with no holder in waiting-on"))
    ld = keys.get("last-done", "")
    if ld and ld.lower() not in ("none", "none (fresh hag)"):
        n = len(re.findall(r"(?:^|;\s*)\S+#\d+:\w+", ld))
        n = n or len([x for x in ld.split(";") if x.strip()])
        if n > 3:
            out.append(("E6", f"last-done holds {n} entries (cap 3; older history -> telemetry/log)"))
    up = keys.get("updated-utc", "")
    if up and st.startswith("active"):
        try:
            t = datetime.datetime.strptime(up[:16], "%Y-%m-%dT%H:%M").replace(tzinfo=datetime.timezone.utc)
            age_h = (datetime.datetime.now(datetime.timezone.utc) - t).total_seconds() / 3600
            if age_h > stale_hours:
                out.append(("W1", f"active but updated-utc {age_h:.0f}h old (> {stale_hours:g}h)"))
        except Exception:
            out.append(("W1", f"updated-utc {up!r} not parseable as YYYY-MM-DDTHH:MM"))
    for m in re.finditer(r"^\s*-\s*declined:\s*(.*)$|^\s*-\s*(\S+#\d+:\w+):\s*(.*)$", status, flags=re.M):
        line = m.group(0)
        if "declined" in line and not re.search(r":\s*\S.{4,}", m.group(1) or ""):
            out.append(("W2", f"declined line without a reason: {line.strip()[:60]!r}"))
    return out


def staged_sections_touched(path: str, repo: str) -> set[str]:
    """Which sections a STAGED diff of `path` touches: {'QUEUE','STATUS'}."""
    r = subprocess.run(["git", "-C", repo, "diff", "--cached", "-U0", "--", path],
                       capture_output=True, text=True, timeout=30)
    if r.returncode != 0 or not r.stdout.strip():
        return set()
    try:
        new = subprocess.run(["git", "-C", repo, "show", f":{os.path.relpath(path, repo)}"],
                             capture_output=True, text=True, timeout=30).stdout
    except Exception:
        new = open(path).read()
    lines = new.splitlines()
    status_at = next((i for i, l in enumerate(lines) if l.startswith("## STATUS")), len(lines))
    touched = set()
    for m in re.finditer(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", r.stdout, flags=re.M):
        start = int(m.group(1))
        count = int(m.group(2) or "1")
        lo, hi = start - 1, start - 1 + max(count, 1)
        if lo < status_at:
            touched.add("QUEUE")
        if hi > status_at:
            touched.add("STATUS")
    return touched


def cmd_lint(a: argparse.Namespace) -> int:
    files = list(a.file or [])
    if a.spec:
        from roles import HagSpec
        spec = HagSpec.load(a.spec)
        files += [r.queue for r in spec.roles if r.queue]
    if not files:
        print("nothing to lint (give --spec or --file)")
        return 0
    repo = a.repo or subprocess.run(["git", "rev-parse", "--show-toplevel"],
                                    capture_output=True, text=True,
                                    cwd=os.path.dirname(os.path.abspath(files[0]))).stdout.strip()
    rc = 0
    for f in files:
        findings = lint_file(f, a.stale_hours)
        if a.staged:
            touched = staged_sections_touched(f, repo)
            if touched == {"QUEUE", "STATUS"}:
                findings.append(("E7", "staged diff edits BOTH QUEUE and STATUS: split into two commits "
                                       "(coordinator owns QUEUE, consumer owns STATUS)"))
        if not findings:
            print(f"ok: {f}")
            continue
        for code, msg in findings:
            print(f"{code} {f}: {msg}")
            if code.startswith("E"):
                rc = 1
    return rc


def add_parser(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("lint-queues", help="queue-file linter + commit ownership gate")
    p.add_argument("--spec", default="")
    p.add_argument("--file", action="append", default=[])
    p.add_argument("--staged", action="store_true", help="pre-commit ownership gate on staged diffs")
    p.add_argument("--stale-hours", type=float, default=24)
    p.add_argument("--repo", default="")
    p.set_defaults(func=cmd_lint)
