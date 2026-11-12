#!/usr/bin/env python3
"""A repository's role-document lifecycle settings (docs/lifecycle.md): `<repository>/.shoal/lifecycle.toml`.

    shoal lifecycle show [<path>] [--json]

One file at the root of the repository that holds role directories says how that repository wants them kept: the
layout (where a role directory goes, where an archived file goes), the tiers (which documents are history, which trees
are never scanned, the start-doc header cap), the checkpoint and deep-pass cadences, warm-to-cold, and the commands
shoal may run for it (`[hooks]`). The file travels with the repository, so every shoal and standalone session writing
there reads the same values, whatever its harness. Without the file, the defaults below apply.

Hooks are code execution from repository content, so they are honoured only for a repository this host trusts: its
path in the host config's `[lifecycle] trusted_repos`. Everything else (layout, tiers, cadences) runs nothing and is read
from any repository. Unknown keys and values of the wrong type are reported, never fatal: a bad file falls back to the
defaults key by key. Read-only; no model calls.
"""
from __future__ import annotations

import copy
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import config  # noqa: E402

FILE = os.path.join(".shoal", "lifecycle.toml")
DEFAULTS: dict[str, dict] = {
    # role_dir "": a role's directory is its start doc's; archive and manifest are templates over the original path
    "layout": {"role_dir": "", "archive": "archive/{path}", "manifest": "archive/{dir}/MANIFEST.jsonl"},
    "tiers": {"history": ["*_log.md", "*_log_*.md"], "exclude": ["archive/*"], "header_max_lines": 40,
              "header_max_bytes": 3072},
    "checkpoint": {"archive": "propose", "min_age_days": 7.0, "cross_owner": "notify"},
    "deep_pass": {"every_days": 3, "every_restarts": 3},
    "cold": {"warm_days": 90, "after_checkpoints": 2},
    "instructions": {"every_days": 14},
    "hooks": {"commit": "", "on_retire": ""},
}
CHOICES = {("checkpoint", "archive"): ("auto", "propose", "off"),
           ("checkpoint", "cross_owner"): ("notify", "rewrite", "keep")}
KIND = {int: "a whole number", float: "a number", str: "a string", list: "a list of strings", bool: "true or false"}
# the least value each number may take: a cap or cadence of 0 makes every doc over its cap or every pass due at once, and
# a negative age floor makes every file a candidate. min_age_days 0 means "now" and is a number of days, so a float.
FLOORS = {("tiers", "header_max_lines"): 1, ("tiers", "header_max_bytes"): 1, ("checkpoint", "min_age_days"): 0,
          ("deep_pass", "every_days"): 1, ("deep_pass", "every_restarts"): 1, ("cold", "warm_days"): 1,
          ("cold", "after_checkpoints"): 1, ("instructions", "every_days"): 1}


def _toml():
    try:
        import tomllib as t  # type: ignore[import-not-found]
    except ImportError:
        try:
            import tomli as t  # type: ignore[import-not-found,no-redef]
        except ImportError:
            return None
    return t


def repo_root(path: str) -> str | None:
    """The git top level holding `path` (a file or directory), or None."""
    p = os.path.abspath(os.path.expanduser(path))
    d = p if os.path.isdir(p) else os.path.dirname(p)
    try:
        r = subprocess.run(["git", "-C", d, "rev-parse", "--show-toplevel"], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return os.path.realpath(r.stdout.strip()) if r.returncode == 0 and r.stdout.strip() else None


def trusted_repos() -> list[str]:
    v = (config.file_values().get("lifecycle") or {}).get("trusted_repos") or []
    return [os.path.realpath(os.path.expanduser(str(x))) for x in v if isinstance(x, str) and x.strip()]


def _same_type(default, value) -> bool:
    if isinstance(default, bool):
        return isinstance(value, bool)
    if isinstance(default, int):
        return isinstance(value, int) and not isinstance(value, bool)
    if isinstance(default, float):
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if isinstance(default, list):
        return isinstance(value, list) and all(isinstance(x, str) for x in value)
    return isinstance(value, type(default))


def _escapes(template: str) -> bool:
    """True when a layout path could leave the repository: absolute, home-relative, or with a `..` segment."""
    t = template.strip()
    return os.path.isabs(t) or t.startswith("~") or ".." in t.replace("\\", "/").split("/")


def load(path: str) -> dict:
    """{repo, file, found, trusted, values, notes} for the repository holding `path`. `values` has every table and key
    of DEFAULTS, from the file where it is valid, else the default."""
    repo = repo_root(path)
    out = {"repo": repo, "file": os.path.join(repo, FILE) if repo else None, "found": False, "trusted": False,
           "values": copy.deepcopy(DEFAULTS), "notes": []}
    if repo is None:
        out["notes"].append(f"{path} is not in a git repository: defaults apply")
        return out
    out["trusted"] = repo in trusted_repos()
    if not os.path.isfile(out["file"]):
        return out
    t = _toml()
    if t is None:
        out["notes"].append(f"{out['file']} skipped: neither tomllib nor tomli is importable")
        return out
    try:
        with open(out["file"], "rb") as fh:
            got = t.load(fh)
    except Exception as exc:  # noqa: BLE001 - a bad file falls back to the defaults, never takes a command down
        out["notes"].append(f"{out['file']} skipped: {exc}")
        return out
    out["found"] = True
    for table, keys in got.items():
        if table not in DEFAULTS or not isinstance(keys, dict):
            out["notes"].append(f"unknown table [{table}] ignored")
            continue
        for key, value in keys.items():
            if key not in DEFAULTS[table]:
                out["notes"].append(f"unknown key {table}.{key} ignored")
            elif not _same_type(DEFAULTS[table][key], value):
                out["notes"].append(f"{table}.{key} = {value!r} is not {KIND[type(DEFAULTS[table][key])]}: default kept")
            elif (table, key) in CHOICES and value not in CHOICES[(table, key)]:
                out["notes"].append(f"{table}.{key} = {value!r} is not one of {CHOICES[(table, key)]}: default kept")
            elif (table, key) in FLOORS and value < FLOORS[(table, key)]:
                out["notes"].append(f"{table}.{key} = {value!r} is below its least value {FLOORS[(table, key)]}: default kept")
            elif table == "layout" and value and _escapes(value):
                # layout is read from any repository, trusted or not, and later steps move files by it: a template
                # that leaves the repository would let an untrusted file decide where files are written
                out["notes"].append(f"layout.{key} = {value!r} is absolute or climbs out of the repository: default kept")
            elif table == "hooks" and value and not out["trusted"]:
                out["notes"].append(f"hooks.{key} ignored: {repo} is not in this host's [lifecycle] trusted_repos")
            else:
                out["values"][table][key] = value
    return out


def cmd_lifecycle_show(a) -> int:
    r = load(a.path or os.getcwd())
    if a.json:
        print(json.dumps(r, indent=1))
        return 0
    src = r["file"] if r["found"] else f"defaults (no {FILE})"
    print(f"{r['repo'] or a.path}: {src}; hooks {'trusted' if r['trusted'] else 'not trusted on this host'}")
    for table, keys in r["values"].items():
        print(f"  [{table}]")
        for k, v in keys.items():
            print(f"    {k} = {json.dumps(v)}")
    for n in r["notes"]:
        print(f"  note: {n}")
    return 0


def add_parser(sub) -> None:
    p = sub.add_parser("lifecycle", help="a repository's role-document lifecycle settings (.shoal/lifecycle.toml)")
    s = p.add_subparsers(dest="lifecycle_cmd", required=True)
    q = s.add_parser("show", help="the effective settings for the repository holding PATH, with where each came from")
    q.add_argument("path", nargs="?", default="")
    q.add_argument("--json", action="store_true")
    q.set_defaults(func=cmd_lifecycle_show)
    import checkpoint
    q = s.add_parser("checkpoint", help="read-only, bounded checkpoint advice for a start doc (never archives)")
    q.add_argument("path")
    q.add_argument("--json", action="store_true")
    q.set_defaults(func=checkpoint.cmd_checkpoint)


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    add_parser(ap.add_subparsers(dest="cmd", required=True))
    a = ap.parse_args()
    sys.exit(a.func(a))
