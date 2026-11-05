#!/usr/bin/env python3
"""What a role's observer says and does about a native client: the shared half of `muse-tail` and `codex-tail`.

A Muse or Codex session has one owner at a time. A native client (`muse resume`, `codex resume`) holds it, and holds
the role's wakes, until it exits, however long it sits idle. So the observer names a turn and a client apart ("running"
for both read as a turn nobody could find), says where the client is, and offers to end one that has gone quiet:
a card left open elsewhere otherwise locks out every other viewer, and the role, with no way to reach it from here.

`h` below is the harness module (muse or codex_harness), which supplies `attached_client`, `activity_age`, `busy`
and `interactive_view`.
"""
from __future__ import annotations

import os
import signal
import subprocess
import time

QUIET_S = 120  # a session written to within this long is mid-turn: never ended from a viewer
EXIT_WAIT_S = 8.0
RELEASE_WAIT_S = 5.0


def where(pid: int) -> str:
    """`pid 123 on pts/2 since Tue Nov  3 11:10:30 2026`, as much of it as can be read."""
    out = f"pid {pid}"
    try:
        tty = os.readlink(f"/proc/{pid}/fd/0")
        if tty.startswith(("/dev/pts/", "/dev/tty")):
            out += f" on {tty[5:]}"
    except OSError:
        pass
    try:
        since = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True, text=True, timeout=5).stdout.strip()
        if since:
            out += f" since {since}"
    except (OSError, subprocess.SubprocessError):
        pass
    return out


def held_message(pid: int) -> str:
    return f"a native client ({where(pid)}) holds this session and its wakes"


def state(h, session: str) -> str:
    """The observer's status word: a client is checked first, because a client opened from an observer also holds
    the lock a turn holds."""
    pid = h.attached_client(session)
    if pid:
        return f"held: {held_message(pid)}"
    return "running" if h.busy(session) else "idle"


def take_over(h, session: str) -> str:
    """End a quiet native client. Returns '' once it has gone, else why it was left alone."""
    pid = h.attached_client(session)
    if not pid:
        return ""
    age = h.activity_age(session)
    if age is not None and age < QUIET_S:
        return f"not ended: the session was written to {age:.0f} s ago, so {where(pid)} may be mid-turn"
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError as exc:
        return f"not ended: {exc}"
    deadline = time.time() + EXIT_WAIT_S
    while time.time() < deadline:
        if h.attached_client(session) != pid:
            return ""
        time.sleep(0.2)
    return f"not ended: {where(pid)} did not exit"


def on_key(h, key: str, session: str, model: str, effort: str, keys: str) -> None:
    """`i` asks for native access; `T` (a capital, so never by accident) ends the client that refused it first."""
    if key == "T":
        why = take_over(h, session)
        if why:
            print(f"\n{why}", flush=True)
            return
    elif key not in ("i", "I"):
        return
    deadline = time.time() + (RELEASE_WAIT_S if key == "T" else 0)
    while True:
        try:
            h.interactive_view(session, model, effort)
            break
        except h.BusyError as exc:
            # the observer that opened the client ended by `T` lets go of the lock a moment after the client exits
            if time.time() < deadline and not h.attached_client(session):
                time.sleep(0.2)
                continue
            print(f"\n{exc}", flush=True)
            if h.attached_client(session):
                print("[T] end that client and attach here", flush=True)
            break
    print("\nBack to live read-only view. " + keys, flush=True)
