"""Where a group lives inside the ledger repository.

A group's directory is `shoals/<id>/` in the ledger clone, holding its canonical spec
`shoal.json` and its `ledger/<writer>.jsonl` files. Groups created before the rename live
at `hags/<id>/` with `hag.json`; both are read, and a group keeps whichever layout it has
until it is migrated (the ledger-repository cutover moves `hags/` to `shoals/` and renames
each `hag.json`). New groups are always created in the current layout.
"""
from __future__ import annotations

import os

GROUPS_DIR = "shoals"
SPEC_FILE = "shoal.json"
LEGACY_GROUPS_DIR = "hags"
LEGACY_SPEC_FILE = "hag.json"


def group_dir(root: str, sid: str) -> str:
    """`<root>/shoals/<sid>`, or the legacy `<root>/hags/<sid>` when only that exists."""
    cur = os.path.join(root, GROUPS_DIR, sid)
    old = os.path.join(root, LEGACY_GROUPS_DIR, sid)
    return old if not os.path.isdir(cur) and os.path.isdir(old) else cur


def spec_file(gdir: str) -> str:
    """The spec inside a group directory: `shoal.json`, or a legacy `hag.json` when only that exists."""
    cur = os.path.join(gdir, SPEC_FILE)
    old = os.path.join(gdir, LEGACY_SPEC_FILE)
    return old if not os.path.exists(cur) and os.path.exists(old) else cur


def spec_path(root: str, sid: str) -> str:
    return spec_file(group_dir(root, sid))


def is_legacy(root: str, sid: str) -> bool:
    gdir = group_dir(root, sid)
    return (os.path.basename(os.path.dirname(gdir)) == LEGACY_GROUPS_DIR
            or os.path.basename(spec_file(gdir)) == LEGACY_SPEC_FILE)
