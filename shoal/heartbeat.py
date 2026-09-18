#!/usr/bin/env python3
"""Heartbeat stall daemon (proposal §5, v0).

Contract (sessions are briefed on it): every live session touches
`/tmp/<hag>/hb-<slug>` on every REPORT/STATUS update.

The daemon fires STALL-SUSPECT for any heartbeat older than the threshold
that is not covered by a verified wait:
- consumer roles: the queue STATUS section carries a `waiting-on:` holder
  line (or state `waiting`) → covered, quiet.
- opencode lanes: no STATUS file exists, so coverage cannot be proven here —
  the daemon attaches the lane's live session status line and leaves the
  dismissal to the coordinator. (Lane-side wait coverage is future work.)

Delivery: coordinator role present → session send to the coordinator;
flattened (no coordinator) → append to `<hagdir>/channel.md` shared channel
+ telemetry watch_event either way. Cron: */5.
"""
from __future__ import annotations

import datetime
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from backends import get as get_backend  # noqa: E402
from watchers import hag_emit  # noqa: E402

HB_PREFIX = "hb-"


def hb_path(hag: str, slug: str) -> str:
    return os.path.join("/tmp", hag, f"{HB_PREFIX}{slug}")


def touch(hag: str, slug: str) -> str:
    """Session-side tick. Returns the path touched."""
    p = hb_path(hag, slug)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "a"):
        os.utime(p, None)
    return p


def age_min(path: str) -> float | None:
    try:
        return (datetime.datetime.now().timestamp()
                - os.path.getmtime(path)) / 60
    except OSError:
        return None


def status_wait_cover(queue_file: str) -> str | None:
    """Return the covering wait reason if STATUS shows a verified wait."""
    try:
        text = open(queue_file).read()
        status = text[text.index("## STATUS"):]
    except Exception:
        return None
    state = re.search(r"^- state:\s*(\S+)", status, flags=re.M)
    holder = re.search(r"^- waiting-on:\s*(.+)", status, flags=re.M)
    if holder and holder.group(1).strip().lower() not in ("", "none", "-"):
        return holder.group(1).strip()
    if state and state.group(1).strip() == "waiting" and holder:
        return holder.group(1).strip() or "waiting (no holder named)"
    return None


def cmd_heartbeat(spec, threshold_min: float, notify: str = "",
                  dry_run: bool = False) -> int:
    from roles import HagSpec
    assert isinstance(spec, HagSpec)
    suspects: list[str] = []
    for role in spec.roles:
        p = hb_path(spec.hag, role.slug)
        age = age_min(p)
        if age is None:
            suspects.append(f"{role.slug}: NO HEARTBEAT FILE (never ticked)")
            continue
        if age <= threshold_min:
            print(f"{role.slug}: ok ({age:.0f}m)")
            continue
        cover = status_wait_cover(role.queue) if role.queue else None
        if cover:
            print(f"{role.slug}: stale {age:.0f}m but covered ({cover}) — quiet")
            continue
        extra = ""
        if role.ecosystem == "opencode" and role.session:
            try:
                extra = get_backend("opencode").status(role.session, spec.dir)
            except Exception as exc:  # noqa: BLE001
                extra = f"status query failed: {exc}"
        suspects.append(f"{role.slug}: STALE {age:.0f}m > {threshold_min:.0f}m"
                        + (f" [{extra}]" if extra else ""))
    if not suspects:
        print("heartbeat: all covered/fresh")
        return 0
    body = "STALL-SUSPECT: " + " ; ".join(suspects)
    print(body)
    hag_emit(spec.hag, notify or "heartbeat", "script", "watch_event",
             kind="stall-suspect", note="; ".join(suspects),
             channel="within-ecosystem" if spec.coordinator else "shared-channel")
    if dry_run:
        return 0
    coord = spec.coordinator
    if coord and coord.session and notify:
        get_backend("opencode").send(notify, spec.dir, body)
    elif coord and coord.session:
        get_backend("opencode").send(coord.session, spec.dir, body)
    else:
        from roles import HAG_ROOT
        chan = os.path.join(HAG_ROOT, spec.hag, "channel.md")
        os.makedirs(os.path.dirname(chan), exist_ok=True)
        with open(chan, "a") as fh:
            fh.write(f"\n## {datetime.datetime.now(datetime.timezone.utc):%FT%TZ} {body}\n")
        print(f"flattened: appended to {chan}")
    return 0
