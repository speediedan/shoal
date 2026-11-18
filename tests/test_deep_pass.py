#!/usr/bin/env python3
"""Hermetic proof of `shoal lifecycle deep-pass` (shoal/deeppass.py) on a scratch repository, ledger and host config:

- due: with no `Deep pass` marker in the companion log; by days since the last marker; by the `After ...` snapshots
  below it (restarts); not due inside both cadences; a marker inside the log's text, not a heading, still counts;
- keep reasons are listed with the days since the file last changed;
- consolidation candidates are the live auxiliary files, oldest change first, never the start doc or its log;
- cold candidates are warm manifest entries at least `[cold] warm_days` old that no live document names, with the
  `shoal archive --cold` command; a younger entry and one a live document still names are left out;
- the trend marker line counts what it reports, and the previous marker's numbers come back;
- a marker inside a fenced block, or one whose time does not parse, is skipped;
- `lifecycle checkpoint` carries `deep_pass_due`, and its first note starts with the whole command; a failure in the
  deep-pass check is a note that costs the checkpoint none of its counts; nothing is written.

Synthetic paths and ids only."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time

T = tempfile.mkdtemp(prefix="shoal-deep-pass-test-")
SHOALD = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "shoal")
REPO = os.path.join(T, "repo")
D = "proj/design/lane"
A = f"archive/{D}"
HOST = os.path.join(T, "host.toml")
DAY = 86400
ENV = dict(os.environ, SHOAL_CONFIG=HOST, SHOAL_LEDGER_DIR=os.path.join(T, "ledger"), XDG_CONFIG_HOME=os.path.join(T, "xdg"),
           SHOAL_SERVER="host-a", GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1",
           SHOAL_ROUTE_CACHE=os.path.join(T, "route-cache"), SHOAL_SSH=os.path.join(T, "ssh-stub"))
for k in ("SHOAL_SPEC", "SHOAL_SLUG"):
    ENV.pop(k, None)
sys.path.insert(0, SHOALD)
import deeppass  # noqa: E402


def utc(t: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


def write(rel: str, text: str) -> None:
    p = os.path.join(REPO, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w") as fh:
        fh.write(text)


def git(*args: str, when: float | None = None) -> str:
    env = dict(ENV, **({"GIT_COMMITTER_DATE": f"@{int(when)} +0000", "GIT_AUTHOR_DATE": f"@{int(when)} +0000"}
                       if when else {}))
    return subprocess.run(["git", *args], cwd=REPO, env=env, check=True, capture_output=True, text=True).stdout


def shoal(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, os.path.join(SHOALD, "cli.py"), *args], env=ENV, capture_output=True,
                          text=True, cwd=REPO)


NOW = time.time()

# due, as a pure function of the log text
LOG = "# log\n\n## Part 1\n\n### Session C1\n\nDeep pass {ts}: 3 live auxiliary, 10 KB, 1 kept, 0 warm, 0 cold candidates\n\n"
d = deeppass.due("# log\n\n### After C1\n", 3, 3, NOW)
assert d["due"] and d["last"] is None and "no deep pass" in d["why"], d
d = deeppass.due(LOG.format(ts=utc(NOW - DAY)) + "## Part 2\n\n### After C1\n\n- x\n", 3, 3, NOW)
assert not d["due"] and d["restarts_since"] == 1 and d["days_since"] == 1.0, d
assert d["previous_trend"] == "3 live auxiliary, 10 KB, 1 kept, 0 warm, 0 cold candidates", d
d = deeppass.due(LOG.format(ts=utc(NOW - 4 * DAY)), 3, 3, NOW)
assert d["due"] and "4.0 days since" in d["why"] and d["restarts_since"] == 0, d
snaps = "".join(f"### After C{n}\n\n- x\n\n" for n in (1, 2, 3))
d = deeppass.due("### After C0\n\n" + LOG.format(ts=utc(NOW - DAY)) + snaps, 3, 3, NOW)
assert d["due"] and d["restarts_since"] == 3 and "3 restarts since" in d["why"], "a snapshot above the marker is not counted"
d = deeppass.due(LOG.format(ts=utc(NOW - 5 * DAY)) + LOG.format(ts=utc(NOW - DAY)), 3, 3, NOW)
assert not d["due"] and d["days_since"] == 1.0, "the last marker is the last pass"
bad = "Deep pass 2026-11-17T25:61Z: 1 live auxiliary\n"
d = deeppass.due(LOG.format(ts=utc(NOW - DAY)) + bad, 3, 3, NOW)
assert not d["due"] and d["days_since"] == 1.0, "a marker whose time does not parse is skipped, not raised"
d = deeppass.due("# log\n\n" + bad, 3, 3, NOW)
assert d["due"] and d["last"] is None, d
fenced = "```\nDeep pass {ts}: 0 live auxiliary\n```\n".format(ts=utc(NOW - DAY))
d = deeppass.due(LOG.format(ts=utc(NOW - 9 * DAY)) + fenced, 3, 3, NOW)
assert d["due"] and d["days_since"] == 9.0, "a marker inside a fenced block is content"
print("due ok")

# a role directory: start doc linking two auxiliary files (one old, one new) and an archive path still in use
open(HOST, "w").write("")
os.makedirs(os.path.join(REPO, D))
git("init", "-q", "-b", "main")
git("config", "user.email", "t@example.invalid")
git("config", "user.name", "t")
write(f"{D}/aux_old.md", "old notes\n")
git("add", "-A")
git("commit", "-q", "-m", "old aux", when=NOW - 30 * DAY)
write(f"{D}/lane_start.md", f"# lane\n\nLast updated: {utc(NOW)}\n\nSee [old](aux_old.md), [new](aux_new.md) and "
                            f"`{A}/ref.md`.\n")
write(f"{D}/lane_log.md", LOG.format(ts=utc(NOW - 5 * DAY)) + "## Part 2\n\n### After C1\n\n- x\n")
write(f"{D}/aux_new.md", "new notes\n")
write(f"{D}/kept.md", "keep: the layout reference other docs cite\n\nbody\n")
for name in ("old", "recent", "ref"):
    write(f"{A}/{name}.md", f"{name}\n")
ev = [{"ts": utc(NOW - 100 * DAY), "by": "t", "event": "archive", "path": f"{D}/old.md", "archive_path": f"{A}/old.md"},
      {"ts": utc(NOW - 10 * DAY), "by": "t", "event": "archive", "path": f"{D}/recent.md", "archive_path": f"{A}/recent.md"},
      {"ts": utc(NOW - 100 * DAY), "by": "t", "event": "migrate", "path": f"{D}/ref.md", "from": f"{D}/old/ref.md",
       "archive_path": f"{A}/ref.md"}]
write(f"{A}/MANIFEST.jsonl", "".join(json.dumps(e) + "\n" for e in ev))
git("add", "-A")
git("commit", "-q", "-m", "lane")
spec = os.path.join(T, "ledger", "shoals", "g1", "shoal.json")
os.makedirs(os.path.dirname(spec))
json.dump({"shoal": "g1", "dir": REPO, "roles": [{"slug": "lane", "doc": os.path.join(REPO, D, "lane_start.md")}]},
          open(spec, "w"))
DOC = os.path.join(REPO, D, "lane_start.md")
before = git("status", "--porcelain") + git("rev-parse", "HEAD")

r = shoal("lifecycle", "deep-pass", DOC, "--json")
assert r.returncode == 0, r.stdout + r.stderr
out = json.loads(r.stdout)
assert out["due"]["due"] and out["due"]["days_since"] == 5.0 and out["log"] == f"{D}/lane_log.md", out["due"]
assert [k["path"] for k in out["keep"]] == [f"{D}/kept.md"], out["keep"]
assert out["keep"][0]["keep"] == "the layout reference other docs cite" and out["keep"][0]["days_since_change"] == 0.0
assert [c["path"] for c in out["consolidate"]] == [f"{D}/aux_old.md", f"{D}/aux_new.md"], out["consolidate"]
assert out["consolidate"][0]["days_since_change"] == 30.0 and out["consolidate"][0]["bytes"] == 10, out["consolidate"]
print("keep and consolidation ok")

assert [c["path"] for c in out["cold"]] == [f"{D}/old.md"], "younger and still-named entries are not cold candidates"
assert out["cold_command"] == f"shoal archive --cold {D}/old.md", out
print("cold candidates ok")

t = out["trend"]
assert t == {"live_auxiliary": 2, "kb": t["kb"], "kept": 1, "warm": 3, "cold_candidates": 1}, t
assert out["marker"].startswith("Deep pass ") and out["marker"].endswith(
    f": 2 live auxiliary, {t['kb']} KB, 1 kept, 3 warm, 1 cold candidate"), out["marker"]
assert out["due"]["previous_trend"].startswith("3 live auxiliary"), out["due"]
text = shoal("lifecycle", "deep-pass", DOC).stdout
assert "DUE (5.0 days since (every 3))" in text and "record in the log snapshot: Deep pass " in text, text
assert "go cold: shoal archive --cold" in text, text
print("trend ok")

r = shoal("lifecycle", "checkpoint", DOC, "--json")
cp = json.loads(r.stdout)
assert cp["deep_pass_due"] is True and cp["notes"][0] == (
    f"deep pass due: shoal lifecycle deep-pass {DOC} (5.0 days since (every 3))"), "the command comes first, whole"
assert git("status", "--porcelain") + git("rev-parse", "HEAD") == before, "the deep pass and checkpoint write nothing"

# a failure in the deep-pass check is a note; the checkpoint keeps its counts and samples
os.environ.update(ENV)
import checkpoint  # noqa: E402


def broken(*a, **k):
    raise RuntimeError("simulated deep-pass failure")


deeppass.due = broken
cp = checkpoint.probe(DOC)
assert "deep_pass_due" not in cp and "candidates" in cp["counts"], cp
assert any(n.startswith("deep-pass check unavailable: simulated") for n in cp["notes"]), cp["notes"]
print("checkpoint ok")

subprocess.run(["rm", "-rf", T], check=True)
print("test_deep_pass OK")
