#!/usr/bin/env python3
"""Bounded, read-only lifecycle advice at a restart-ready boundary. No model calls or hooks."""
from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

TIMEOUT_S = 15


def probe(path: str) -> dict:
    import lifecycle
    import refs
    import startdoc

    path = os.path.abspath(os.path.expanduser(path))
    lc = lifecycle.load(path)
    mode = lc["values"]["checkpoint"]["archive"]
    out = {"v": 1, "advisory": True, "mode": mode, "effective_mode": "off" if mode == "off" else "propose",
           "ok": True, "notes": list(lc["notes"]), "counts": {}, "samples": {}}
    if mode == "off":
        out["skipped"] = "checkpoint.archive = off"
        return out
    doc = startdoc.check(path)
    out["notes"] += doc["problems"] + doc["warnings"]
    known = refs.roots(False)
    out["notes"] += known["notes"]
    out["hosts"] = known["hosts"]
    out["notes"].append("roots cover this host's standalone sessions; other hosts are not read")
    if not known["specs_live"]:
        out["notes"].append("no live spec found: other roles' roots may be missing")
    # who this start doc belongs to: a role's slug or a standalone session's label, from the same roots
    whos = known["roots"].get(path) or known["roots"].get(os.path.realpath(path)) or []
    names = {w.get("slug") or w.get("label") for w in whos} - {None, ""}
    auto = mode == "auto" and bool(names & set(lc["values"]["checkpoint"]["auto_roles"]))
    if auto:
        out["effective_mode"] = "auto"
        out["roles"] = sorted(names)
    scan = refs.scan(os.path.dirname(path), docs=[path], known_roots=known["roots"], age_floor_all=auto)
    out["ok"] = doc["ok"] and scan["ok"] and not scan["candidates"] and not doc["warnings"]
    for key in ("candidates", "dangling", "untracked", "deleted"):
        out["counts"][key] = len(scan[key])
        out["samples"][key] = scan[key][:5]
    # the deep pass is the role's own periodic work: say when it is owed, from the companion log alone (cheap). Its
    # own try: a failure here is a note, never the loss of the checkpoint's counts and samples
    try:
        import deeppass
        repo = refs.Repo(os.path.dirname(path))
        rel = repo.rel(path)
        log = refs.companion_log(rel, repo.mode) if rel else None
        dp = lc["values"]["deep_pass"]
        d = deeppass.due(repo.read(log) if log else "", dp["every_days"], dp["every_restarts"])
        out["deep_pass_due"] = d["due"]
        if d["due"]:
            # first among the capped notes, and the command first within it, so the 200-character cap cuts the why
            out["notes"].insert(0, f"deep pass due: shoal lifecycle deep-pass {path} ({d['why']})")
    except Exception as exc:  # advisory: never let the deep-pass check blank the checkpoint
        out["notes"].append(f"deep-pass check unavailable: {str(exc)[:150]}")
    if auto and scan["candidates"]:
        # the role runs these itself, after moving any durable knowledge up a tier (the work loop's auto step)
        policy = lc["values"]["checkpoint"]["cross_owner"]
        out["commands"] = ["shoal archive " + " ".join(shlex.quote(c) for c in scan["candidates"])
                           + f" --cross-owner {policy} --strict --reason {shlex.quote('checkpoint auto')}"]
    elif mode == "auto" and not auto:
        out["notes"].append("archive = auto applies only to the roles in [checkpoint] auto_roles; this one proposes")
    return out


def run(path: str, timeout: float = TIMEOUT_S) -> dict:
    """The entire scan runs in a child: slow git or reference extraction cannot hold a context restart."""
    before = time.monotonic()
    try:
        p = subprocess.run([sys.executable, __file__, path], capture_output=True, text=True, timeout=timeout)
        if p.returncode:
            raise RuntimeError(p.stderr.strip() or p.stdout.strip() or f"probe exited {p.returncode}")
        out = json.loads(p.stdout)
    except Exception as exc:  # advisory checks must never reject restart_ready
        out = {"v": 1, "advisory": True, "ok": False, "counts": {}, "samples": {},
               "notes": [f"checkpoint check unavailable: {str(exc)[:500]}"]}
    notes = out["notes"]
    if len(notes) > 10:
        notes = notes[:9] + [f"({len(notes) - 9} more)"]
    out["notes"] = [note if len(note) <= 200 else note[:197] + "..." for note in notes]
    out["elapsed_ms"] = round((time.monotonic() - before) * 1000)
    return out


def report(out: dict) -> None:
    counts = ", ".join(f"{n} {k}" for k, n in out["counts"].items())
    print("lifecycle checkpoint (advisory): " + (out.get("skipped") or counts or "check unavailable"))
    for key, entries in out["samples"].items():
        for entry in entries:
            print(f"  {key}: {entry}")
    for command in out.get("commands", []):
        print(f"  auto ({', '.join(out.get('roles', []))}): {command}")
    for note in out["notes"]:
        print(f"  note: {note}")


def cmd_checkpoint(a) -> int:
    out = run(a.path)
    print(json.dumps(out, indent=1)) if a.json else report(out)
    return 0


if __name__ == "__main__":
    print(json.dumps(probe(sys.argv[1])))
