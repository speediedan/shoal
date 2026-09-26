"""The user-scope copies of this package's agent skills (plugins/shoal-skills/skills/*).

Role sessions load `shoal-protocol` from each harness's skill home, not from this checkout, so
a merged protocol change reaches a session only once those copies are refreshed (measured
2026-09-26: the copies installed at the cutover were three merges behind, so sessions ran the
old wait rules and next-list length). `shoal upgrade` syncs them; `doctor` reports stale ones.
"""
from __future__ import annotations

import filecmp
import os
import shutil

import config

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "plugins", "shoal-skills", "skills")


def names() -> list[str]:
    return sorted(d for d in os.listdir(SRC) if os.path.isdir(os.path.join(SRC, d))) if os.path.isdir(SRC) else []


def _differs(a: str, b: str) -> bool:
    if not os.path.isdir(b):
        return True
    cmp = filecmp.dircmp(a, b)
    if cmp.left_only or cmp.right_only or cmp.diff_files or cmp.funny_files:
        return True
    return any(_differs(os.path.join(a, d), os.path.join(b, d)) for d in cmp.common_dirs)


def stale() -> list[str]:
    """`<home>/<skill>` copies that are missing or differ from this checkout."""
    return [os.path.join(h, n) for h in config.skill_homes() for n in names() if _differs(os.path.join(SRC, n), os.path.join(h, n))]


def sync(dry_run: bool = False) -> list[str]:
    """Replace every stale copy with this checkout's version; returns what changed."""
    out = []
    for dst in stale():
        out.append(dst)
        if not dry_run:
            if os.path.isdir(dst):
                shutil.rmtree(dst)
            shutil.copytree(os.path.join(SRC, os.path.basename(dst)), dst)
    return out
