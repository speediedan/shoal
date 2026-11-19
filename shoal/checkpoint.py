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
    setting = lc["values"]["checkpoint"]["archive"]
    out = {"v": 1, "advisory": True, "mode": setting, "effective_mode": "propose",
           "ok": True, "notes": list(lc["notes"]), "counts": {}, "samples": {}}
    if setting == "off":
        out["effective_mode"], out["skipped"] = "off", "checkpoint.archive = off"
        return out
    known = refs.roots(False)
    # who this start doc belongs to: a role's slug or a standalone session's label, from the roots
    whos = known["roots"].get(path) or known["roots"].get(os.path.realpath(path)) or []
    names = {w.get("slug") or w.get("label") for w in whos} - {None, ""}
    mode = lifecycle.archive_mode(setting, names)
    if mode == "off":
        out["effective_mode"], out["skipped"] = "off", f"checkpoint.archive is off for {', '.join(sorted(names)) or 'this doc'}"
        return out
    doc = startdoc.check(path)
    out["notes"] += doc["problems"] + doc["warnings"]
    out["notes"] += known["notes"]
    out["hosts"] = known["hosts"]
    out["notes"].append("roots cover this host's standalone sessions; other hosts are not read")
    if not known["specs_live"]:
        out["notes"].append("no live spec found: other roles' roots may be missing")
    # a start doc named through a symlink is read, and scoped, where its target lives
    doc_path = os.path.realpath(path)
    scope = os.path.dirname(doc_path)
    auto = mode == "auto"
    if auto:
        others = shared_with(known["roots"], scope, names)
        if others:
            # a directory that holds another role's start doc is not this role's to archive from (one writer per
            # directory): propose, and say what would make auto apply
            auto = False
            out["notes"].insert(0, f"auto not applied: this start doc's directory also holds the start doc of "
                                   f"{'; '.join(others)}; split the role into its own directory first")
        else:
            out["effective_mode"] = "auto"
            out["roles"] = sorted(names)
    scan = refs.scan(scope, docs=[doc_path], known_roots=known["roots"], age_floor_all=auto)
    out["ok"] = doc["ok"] and scan["ok"] and not scan["candidates"] and not doc["warnings"]
    for key in ("candidates", "dangling", "untracked", "deleted"):
        out["counts"][key] = len(scan[key])
        out["samples"][key] = scan[key][:5]
    # the deep pass is the role's own periodic work: say when it is owed, from the companion log alone (cheap). Its
    # own try: a failure here is a note, never the loss of the checkpoint's counts and samples
    try:
        import deeppass
        repo = refs.Repo(scope)
        rel = repo.rel(doc_path)
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
        # absolute paths, so the command archives the same files from any working directory
        out["commands"] = ["shoal archive " + " ".join(shlex.quote(os.path.join(scan["repo"], c)) for c in scan["candidates"])
                           + f" --cross-owner {policy} --strict --reason {shlex.quote('checkpoint auto')}"]
    return out


def shared_with(roots: dict, scope: str, names: set[str]) -> list[str]:
    """Who else has a start doc in directory `scope`, named there directly or through a symlink there, or a symlink
    elsewhere whose target is there: every role (slug) or standalone session (label) not in `names`."""
    import refs
    others: list[str] = []
    for p, whos in roots.items():
        here = {os.path.dirname(os.path.abspath(p)), os.path.dirname(os.path.realpath(p))}
        if os.path.realpath(scope) not in {os.path.realpath(h) for h in here}:
            continue
        for w in whos:
            if (w.get("slug") or w.get("label")) not in names:
                text = refs.who_text(w)
                if text not in others:
                    others.append(text)
    return others


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
