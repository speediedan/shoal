"""Where a group lives inside the ledger repository.

A group's directory is `shoals/<id>/` in the ledger clone, holding its canonical spec
`shoal.json` and its `ledger/<writer>.jsonl` files.
"""
from __future__ import annotations

import os

GROUPS_DIR = "shoals"
SPEC_FILE = "shoal.json"


def group_dir(root: str, sid: str) -> str:
    """`<root>/shoals/<sid>`."""
    return os.path.join(root, GROUPS_DIR, sid)


def spec_file(gdir: str) -> str:
    """The spec inside a group directory: `shoal.json`."""
    return os.path.join(gdir, SPEC_FILE)


def spec_path(root: str, sid: str) -> str:
    return spec_file(group_dir(root, sid))
