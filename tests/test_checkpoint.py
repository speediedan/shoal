#!/usr/bin/env python3
"""Checkpoint advice is read-only and bounded, with a real repo and queue integration in selftest_h4."""
from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "shoal"))
import checkpoint
import refs


with tempfile.TemporaryDirectory(prefix="shoal-checkpoint-") as tmp:
    root = Path(tmp)
    env = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")

    def git(*args):
        return subprocess.run(["git", "-C", tmp, *args], env=env, capture_output=True, text=True, check=True).stdout

    git("init", "-q")
    git("config", "user.name", "tester")
    git("config", "user.email", "tester@example.invalid")
    lane = root / "lane"
    lane.mkdir()
    doc = lane / "plan_state.md"
    doc.write_text("# Lane\nLast updated: 2020-01-01T00:00Z\nCurrent summary.\n\n"
                   "## Mission\nWork.\n## Current state\n[missing](missing.md)\n"
                   "## Next steps\nContinue.\n## grants-needed\nNone.\n")
    (lane / "notes_log.md").write_text("Old [result](result.md).\n[kept](kept.md)\n")
    (lane / "result.md").write_text("Concluded.\n")
    (lane / "kept.md").write_text("---\nkeep: comparison baseline\n---\nResult.\n")
    (root / ".shoal").mkdir()
    config = root / ".shoal/lifecycle.toml"
    config.write_text('[checkpoint]\narchive = "propose"\n[tiers]\nheader_max_lines = 2\n')
    git("add", "--", "lane", ".shoal/lifecycle.toml")
    git("commit", "-qm", "fixture")
    (lane / "scratch.py").write_text("pass\n")
    before = git("status", "--porcelain"), git("rev-parse", "HEAD")
    known = {"notes": [], "hosts": ["host-a"], "specs_live": 1, "roots": {str(doc): []}}
    # Real graph extraction and configuration, synthetic roots only: no live host state or network.
    known["roots"][str(doc)] = [{"kind": "standalone", "label": "lane", "host": "host-a"}]
    with mock.patch.object(refs, "roots", return_value=known):
        r = checkpoint.probe(str(doc))
    assert not r["ok"] and r["advisory"] and r["effective_mode"] == "propose", r
    assert r["counts"]["candidates"] == 1 and r["samples"]["candidates"] == ["lane/result.md"], r
    assert r["counts"]["dangling"] and r["counts"]["untracked"] == 1, r
    assert any("cap" in n for n in r["notes"]), r
    assert before == (git("status", "--porcelain"), git("rev-parse", "HEAD"))
    config.write_text('[checkpoint]\narchive = "auto"\n')
    with mock.patch.object(refs, "roots", return_value=known):
        r = checkpoint.probe(str(doc))
    assert r["effective_mode"] == "propose" and any("auto" in n for n in r["notes"]), r
    config.write_text('[checkpoint]\narchive = "off"\n')
    with mock.patch.object(refs, "scan", side_effect=AssertionError("off must not scan")):
        assert checkpoint.probe(str(doc))["skipped"]
    # A bounded child for the actual command, and failure paths that cannot reject restart-ready.
    assert checkpoint.run(str(doc))["skipped"]
    for failure in (subprocess.TimeoutExpired("probe", 0.01), OSError("cannot spawn")):
        with mock.patch.object(checkpoint.subprocess, "run", side_effect=failure):
            r = checkpoint.run(str(doc), timeout=0.01)
        assert r["advisory"] and not r["ok"] and "unavailable" in r["notes"][0], r
        checkpoint.report(r)
    with mock.patch.object(checkpoint.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "bad JSON")):
        assert not checkpoint.run(str(doc))["ok"]
    with contextlib.redirect_stdout(io.StringIO()) as stream:
        from argparse import Namespace
        assert checkpoint.cmd_checkpoint(Namespace(path=str(doc), json=True)) == 0
    assert json.loads(stream.getvalue())["skipped"]

print("checkpoint read-only, proposals, keep reasons, off, timeout and failures ok")
