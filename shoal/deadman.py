#!/usr/bin/env python3
"""Deadman check, recovery tier T1 (architecture doc §6).

A Claude role's ledger-watch is a harness background command, so it can die without
its session ever acting on the notification (reaped while a turn crashed, a
notification consumed without a re-arm). The deadman is a harness SCHEDULED PROMPT
(not a shell, so the background-shell reaper cannot touch it) that runs:

    hag.py watch-check --hag H --slug S

which ticks `/tmp/<hag>/deadman-<slug>`, verifies the ledger-watch pid (alive AND its
command line is this role's ledger-watch, so a recycled pid cannot pass), and prints
one line: `DEADMAN OK ...` (end the turn) or `DEADMAN DEAD ...` with the exact re-arm
command and whether the watcher exited by design (its sidecar records an exit verdict
for that pid) or was killed externally. opencode roles do not need this: the heartbeat
wakes them directly.

`--print-schedule` prints the scheduled-prompt text and cron line a Claude role
registers at start (the brief carries the same).
"""
from __future__ import annotations

import datetime
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "telemetry"))

from roles import state_dir  # noqa: E402

DEADMAN_CRON = "13 * * * *"  # hourly, off the :00/:30 marks
DEADMAN_INTERVAL_MIN = 60


def now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def tick_path(hag: str, slug: str) -> str:
    return os.path.join(state_dir(hag), f"deadman-{slug}")


def pid_is_watch(pid: str, slug: str) -> bool:
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as fh:
            argv = fh.read().split(b"\0")
    except OSError:
        return False
    joined = b" ".join(argv)
    return b"ledger-watch" in joined and slug.encode() in joined


def rearm_command(hag: str, slug: str, sc: dict) -> str:
    wake = sc.get("wake") or "exit"
    cmd = f"python3 {os.path.join(HERE, 'hag.py')} ledger-watch --hag {hag} --slug {slug} --wake {wake}"
    if sc.get("session_id"):
        cmd += f" --session {sc['session_id']}"
    if sc.get("interval"):
        cmd += f" --interval {sc['interval']}"
    return cmd


def check(hag: str, slug: str) -> tuple[bool, str]:
    from ledger_watch import load_sidecar, sidecar_path
    sc = load_sidecar(sidecar_path(hag, slug))
    pid = sc.get("pid", "")
    if pid and pid_is_watch(pid, slug):
        return True, (f"DEADMAN OK {slug}@{hag}: ledger-watch pid {pid} alive "
                      f"(armed {sc.get('armed_utc', '?')}, last tick {sc.get('last_tick', '?')})")
    if not sc:
        how = "no sidecar: never armed on this host"
    else:
        ex = sc.get("exit", "")
        if ex.split(" ", 1)[0] == pid and pid:
            how = f"exited by design ({ex}); the wake it delivered was not followed by a re-arm"
        else:
            how = (f"pid {pid or '?'} gone with no exit verdict: killed externally "
                   f"(check the harness notification status and `hag.py kill-audit`)")
    return False, (f"DEADMAN DEAD {slug}@{hag}: {how}. Re-arm NOW as a background command, then prove "
                   f"the new pid (ps -p <pid> and sidecar pid match): {rearm_command(hag, slug, sc)}")


def cmd_watch_check(a) -> int:
    if a.print_schedule:
        print(f"cron: {DEADMAN_CRON}  (recurring; harness scheduled prompt, session-only, 7-day expiry)")
        print(f"prompt: HAG deadman: run `python3 {os.path.join(HERE, 'hag.py')} watch-check --hag {a.hag} "
              f"--slug {a.slug}`. If it prints DEADMAN OK, end the turn with no other action. If it prints "
              f"DEADMAN DEAD, re-arm exactly as it says, prove the pid, `hag.py transition` to your posture.")
        return 0
    os.makedirs(state_dir(a.hag), exist_ok=True)
    with open(tick_path(a.hag, a.slug), "w") as fh:
        fh.write(now_iso() + "\n")
    ok, line = check(a.hag, a.slug)
    print(line)
    try:
        from hag_telemetry import append_event, build_event, resolve_dir
        append_event(resolve_dir(None, a.hag), build_event(
            a.hag, a.slug, "script", "watch_event",
            [f"kind=deadman", f"slug={a.slug}", "tier=T1", f"note={'ok' if ok else 'dead'}"]))
    except Exception as exc:  # noqa: BLE001
        print(f"(telemetry emit failed: {exc})", file=sys.stderr)
    return 0 if ok else 3


def add_parser(sub) -> None:
    p = sub.add_parser("watch-check", help="deadman (tier T1): verify this role's ledger-watch, print OK or re-arm line")
    p.add_argument("--hag", required=True)
    p.add_argument("--slug", required=True)
    p.add_argument("--print-schedule", action="store_true", help="print the scheduled-prompt cron + text")
    p.set_defaults(func=cmd_watch_check)
