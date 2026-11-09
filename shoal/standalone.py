#!/usr/bin/env python3
"""The standalone-session registry: sessions outside any shoal that the viewer, the handover and `remove-session` know
about, saved as `$XDG_CONFIG_HOME/shoal/ui-standalone.json` (`{"v": 1, "sessions": [...]}`), one per host.

Core modules (`refs`, `park`) read it here; the viewer (`ui/ui_herdr.py`) re-exports these names, so there is one source
of truth and no core module imports a UI module for it.
"""
from __future__ import annotations

import json
import os


def standalone_path() -> str:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, "shoal", "ui-standalone.json")


def unique_by_label(entries: list[dict]) -> tuple[list[dict], list[dict]]:
    """(kept, dropped): two saved sessions with one label share a pane label, so their feeder reports
    collided (equal sequence numbers: the first, an old idle seat, won and a working seat read idle,
    2026-10-19). The later entry wins, as the newer registration is the live one."""
    last = {}
    for i, e in enumerate(entries):
        last[e.get("label") or e["session"][:12]] = i
    kept = [e for i, e in enumerate(entries) if last[e.get("label") or e["session"][:12]] == i]
    return kept, [e for e in entries if e not in kept]


def load_standalone(raw: bool = False) -> list[dict]:
    try:
        with open(standalone_path()) as fh:
            entries = list(json.load(fh).get("sessions", []))
    except (OSError, ValueError):
        return []
    return entries if raw else unique_by_label(entries)[0]


def save_standalone(entries: list[dict]) -> None:
    p = standalone_path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w") as fh:
        json.dump({"v": 1, "sessions": entries}, fh, indent=1)
    os.replace(tmp, p)
