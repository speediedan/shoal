#!/usr/bin/env python3
"""kill-audit: every harness-reaped background command, with the evidence that explains it.

Scans Claude Code transcripts for GENUINE kills (a harness-originated task-notification with
status killed and the memory-pressure summary, unique per task id; agents quoting the phrase
do not count; failure modes §19/§21) and joins each one to:
  - the host PSI events `psi_watch` logged in the 30 s before it (count, MemAvailable),
  - the harness version that ran the session,
  - the cgroup memory.events oom_kill counters at audit time (kernel ruled in or out).
New kills are appended once to `$HAG_DATA_ROOT/_instruments/kills.jsonl`.

    hag.py kill-audit [--since 2026-09-16] [--no-record]
"""
from __future__ import annotations

import datetime
import glob
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from roles import HAG_DATA_ROOT  # noqa: E402

PHRASE = "stopped because the system is running low on memory"
INSTR = os.path.join(HAG_DATA_ROOT, "_instruments")
PSI_LOG = os.path.join(INSTR, "psi_events.jsonl")
KILLS = os.path.join(INSTR, "kills.jsonl")
SCARCE = "MemAvailable under 10% and under 4 GiB"


def parse(ts: str) -> datetime.datetime:
    return datetime.datetime.fromisoformat(ts.replace("Z", "+00:00"))


def corpus(since: str) -> list[dict]:
    out, seen = [], set()
    for f in glob.glob(os.path.expanduser("~/.claude/projects/*/*.jsonl")):
        try:
            fh = open(f, errors="replace")
        except OSError:
            continue
        with fh:
            for line in fh:
                if PHRASE not in line:
                    continue
                try:
                    o = json.loads(line)
                except ValueError:
                    continue
                if o.get("type") != "user" or (o.get("timestamp") or "") < since:
                    continue
                origin = o.get("origin") or {}
                if isinstance(origin, dict) and origin.get("kind") not in (None, "task-notification"):
                    continue
                c = o.get("message", {}).get("content")
                if not isinstance(c, str):
                    continue  # tool results quoting a notification are not notifications
                for m in re.finditer(r"<task-notification>(.*?)</task-notification>", c, re.S):
                    b = m.group(1)
                    if PHRASE not in b or "<status>killed</status>" not in b:
                        continue
                    tid = re.search(r"<task-id>(.*?)</task-id>", b)
                    sm = re.search(r"<summary>(.*?)</summary>", b, re.S)
                    tid = tid.group(1) if tid else "?"
                    if tid in seen:
                        continue
                    seen.add(tid)
                    out.append({"ts": o.get("timestamp"), "task_id": tid, "session": o.get("sessionId", "")[:8],
                                "version": o.get("version", ""), "cwd": o.get("cwd", ""),
                                "summary": re.sub(r"\s+", " ", sm.group(1) if sm else "")[:160]})
    return sorted(out, key=lambda r: r["ts"])


def psi_events() -> list[dict]:
    ev = []
    try:
        with open(PSI_LOG) as fh:
            for line in fh:
                r = json.loads(line)
                if r.get("kind") == "event":
                    ev.append(r)
    except (OSError, ValueError):
        pass
    return ev


def oom_counters() -> dict[str, int]:
    out = {}
    for cg in ("user.slice/user-1000.slice", "system.slice"):
        try:
            with open(f"/sys/fs/cgroup/{cg}/memory.events") as fh:
                out[cg] = int(re.search(r"^oom_kill (\d+)", fh.read(), re.M).group(1))
        except (OSError, AttributeError):
            out[cg] = -1
    return out


def cmd_kill_audit(a) -> int:
    kills = corpus(a.since)
    psi = psi_events()
    psi_first = psi[0]["ts"] if psi else None
    oom = oom_counters()
    known = set()
    try:
        with open(KILLS) as fh:
            known = {json.loads(l)["task_id"] for l in fh if l.strip()}
    except (OSError, ValueError, KeyError):
        pass
    new = 0
    for k in kills:
        t = parse(k["ts"])
        near = [e for e in psi if 0 <= (t - parse(e["ts"])).total_seconds() <= 30]
        if near:
            avail = min(e["meminfo_kb"]["MemAvailable"] for e in near) / 2**20
            k["psi"] = f"{len(near)} PSI fire(s) in the 30 s before; min MemAvailable {avail:.1f} GiB"
        elif psi_first and k["ts"] >= psi_first:
            k["psi"] = "NO PSI fire logged in the 30 s before (not the host-PSI reaper: investigate)"
        else:
            k["psi"] = "before psi_watch coverage"
        k["oom_kill_at_audit"] = oom
        mark = "" if k["task_id"] in known else " NEW"
        print(f"{k['ts']} {k['session']} v{k['version']} {k['task_id']}{mark} :: {k['psi']} :: {k['summary'][:90]}")
        if k["task_id"] not in known and not a.no_record:
            os.makedirs(INSTR, exist_ok=True)
            with open(KILLS, "a") as fh:
                fh.write(json.dumps(k) + "\n")
            new += 1
    by_v: dict[str, int] = {}
    for k in kills:
        by_v[k["version"]] = by_v.get(k["version"], 0) + 1
    print(f"{len(kills)} kills since {a.since} by harness version {by_v}; {new} newly recorded in {KILLS}; "
          f"oom_kill counters {oom}; 2.1.280+ reaps only when {SCARCE}")
    return 0


def add_parser(sub) -> None:
    p = sub.add_parser("kill-audit", help="genuine harness reaps joined to host PSI evidence (failure modes §21)")
    p.add_argument("--since", default="2026-09-16")
    p.add_argument("--no-record", action="store_true", help="print only; do not append to kills.jsonl")
    p.set_defaults(func=cmd_kill_audit)
