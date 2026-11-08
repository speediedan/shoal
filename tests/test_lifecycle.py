#!/usr/bin/env python3
"""Hermetic proof of a repository's lifecycle settings (shoal/lifecycle.py) and the start-doc header (startdoc.py):

- without `.shoal/lifecycle.toml` the defaults apply; with one, valid values win key by key, and an unknown table or
  key, a value of the wrong type or outside its choices is reported and the default kept;
- `[hooks]` is honoured only for a repository in this host's `[lifecycle] trusted_repos`, and reported otherwise;
- a `[layout]` template that is absolute or climbs out of the repository, and a number below its floor, are refused;
- the header is everything above the first `##` section, a `##` inside a fenced block is not one (a fence closes only
  on its own marker, at least as long); `doc-check` warns
  (never fails) on a header over the repository's cap or one holding only the title and stamp; `doc-head` prints it,
  for a path or a role, and `--all` reports every root start doc's header;
- `refs` takes its tiers and age floor from the repository's file, and reports dangling references from live
  documents only.

Synthetic paths and ids only."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile

T = tempfile.mkdtemp(prefix="shoal-lifecycle-test-")
SHOALD = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "shoal")
REPO = os.path.join(T, "repo")
D = "proj/design/lane"
LANE = os.path.join(REPO, D)
HOST = os.path.join(T, "host.toml")
ENV = dict(os.environ, SHOAL_CONFIG=HOST, SHOAL_LEDGER_DIR=os.path.join(T, "ledger"),
           XDG_CONFIG_HOME=os.path.join(T, "xdg"), SHOAL_SERVER="host-a", GIT_CONFIG_GLOBAL=os.devnull,
           GIT_CONFIG_NOSYSTEM="1")
ENV.pop("SHOAL_SPEC", None)


def write(rel: str, text: str) -> None:
    p = os.path.join(REPO, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w") as fh:
        fh.write(text)


def sh(*args: str) -> None:
    subprocess.run(args, cwd=REPO, env=ENV, check=True, capture_output=True)


def shoal(*args: str) -> tuple[int, str]:
    r = subprocess.run([sys.executable, os.path.join(SHOALD, "cli.py"), *args], env=ENV, capture_output=True, text=True)
    return r.returncode, r.stdout + r.stderr


def js(*args: str) -> dict:
    rc, out = shoal(*args, "--json")
    assert rc == 0, out
    return json.loads(out)


open(HOST, "w").write("")
os.makedirs(LANE)
sh("git", "init", "-q", "-b", "main")
sh("git", "config", "user.email", "t@example.invalid")
sh("git", "config", "user.name", "t")

# defaults, then a file with valid, unknown and wrong values
r = js("lifecycle", "show", REPO)
assert not r["found"] and not r["trusted"] and r["values"]["checkpoint"]["archive"] == "propose", r
write(".shoal/lifecycle.toml", """
[tiers]
history = ["*_log.md", "*_prompts.md"]
exclude = ["archive/*", "*/generated/*"]
header_max_lines = 5
[checkpoint]
archive = "sometimes"
min_age_days = "seven"
cross_owner = "keep"
[hooks]
on_retire = "scripts/retire.sh"
[bogus]
x = 1
[cold]
warm_dayz = 3
after_checkpoints = -1
[deep_pass]
every_days = 0
every_restarts = 2.5
[layout]
archive = "../../elsewhere/{path}"
manifest = "/abs/{dir}/MANIFEST.jsonl"
role_dir = "~/roles"
""")
r = js("lifecycle", "show", LANE)
v, notes = r["values"], " | ".join(r["notes"])
assert r["found"] and os.path.realpath(r["repo"]) == os.path.realpath(REPO), r
assert v["tiers"]["history"] == ["*_log.md", "*_prompts.md"] and v["tiers"]["header_max_lines"] == 5, v
assert v["checkpoint"] == {"archive": "propose", "min_age_days": 7, "cross_owner": "keep"}, v["checkpoint"]
for want in ("checkpoint.archive = 'sometimes' is not one of", "checkpoint.min_age_days = 'seven' is not a number",
             "unknown table [bogus]", "unknown key cold.warm_dayz", "hooks.on_retire ignored",
             "cold.after_checkpoints = -1 is below its least value 1", "deep_pass.every_days = 0 is below",
             "deep_pass.every_restarts = 2.5 is not a whole number", "layout.archive = '../../elsewhere/{path}' is absolute or climbs",
             "layout.manifest = '/abs/{dir}/MANIFEST.jsonl' is absolute", "layout.role_dir = '~/roles' is absolute"):
    assert want in notes, (want, notes)
assert v["hooks"]["on_retire"] == "", "an untrusted repository's hook is not taken"
assert v["layout"] == {"role_dir": "", "archive": "archive/{path}", "manifest": "archive/{dir}/MANIFEST.jsonl"}, \
    "a layout that leaves the repository is refused, even from a file whose other values are read: %r" % v["layout"]
assert v["deep_pass"] == {"every_days": 3, "every_restarts": 3} and v["cold"]["after_checkpoints"] == 2, v
open(HOST, "w").write(f'[lifecycle]\ntrusted_repos = ["{REPO}"]\n')
r = js("lifecycle", "show", REPO)
assert r["trusted"] and r["values"]["hooks"]["on_retire"] == "scripts/retire.sh", r
rc, out = shoal("lifecycle", "show", REPO)
assert "hooks trusted" in out and "note: unknown table [bogus]" in out, out
print("lifecycle settings ok")

# the header
sys.path.insert(0, SHOALD)
import startdoc  # noqa: E402
assert startdoc.header("# t\n\nstate\n```\n## not a section\n```\n\n## Mission\nm\n") == "# t\n\nstate\n```\n## not a section\n```\n\n"
assert startdoc.header("# only a title\nno sections\n") == "# only a title\nno sections\n"
# a fence closes only on its own marker, at least as long: a ~~~ inside ``` and a ``` inside ```` are content
assert startdoc.header("# t\n```\n~~~\n## in\n~~~\n```\n## Mission\n") == "# t\n```\n~~~\n## in\n~~~\n```\n"
assert startdoc.header("# t\n````md\n```\n## in\n```\n````\n## M\n") == "# t\n````md\n```\n## in\n```\n````\n"
body = "\n## Mission\n\nm\n\n## Current state\n\nc\n\n## Next\n\n- n\n\n## grants-needed\n\n- None pending\n"
write(f"{D}/lane_start.md", "# lane\n\nLast updated: 2026-11-08T10:00Z\n\nPosture: active on lane#1.\n" + body)
write(f"{D}/bare_start.md", "# bare\n\nLast updated: 2026-11-08T10:00Z\n" + body)
write(f"{D}/long_start.md", "# long\n\nLast updated: 2026-11-08T10:00Z\n" + "".join(f"state line {i}\n" for i in range(9)) + body)
rc, out = shoal("doc-check", os.path.join(LANE, "lane_start.md"))
assert rc == 0 and "header" not in out, out
rc, out = shoal("doc-check", os.path.join(LANE, "bare_start.md"))
assert rc == 0 and "holds only the title and stamp" in out, "a summary-less header warns, never fails: " + out
rc, out = shoal("doc-check", os.path.join(LANE, "long_start.md"))
assert rc == 0 and "over the 5 line" in out, "the repository's cap (5) applies, advisory: " + out
rc, out = shoal("doc-head", os.path.join(LANE, "lane_start.md"))
assert out == "# lane\n\nLast updated: 2026-11-08T10:00Z\n\nPosture: active on lane#1.\n\n", repr(out)
h = js("doc-head", os.path.join(LANE, "long_start.md"))
assert h["over"] and h["lines"] == 12 and h["max_lines"] == 5 and h["summary"], h
print("header and doc-check ok")

# doc-head for a role, and the report over every root start doc
spec = os.path.join(T, "ledger", "shoals", "g1", "shoal.json")
os.makedirs(os.path.dirname(spec))
json.dump({"shoal": "g1", "dir": REPO, "roles": [
    {"slug": "lane", "doc": os.path.join(LANE, "lane_start.md")},
    {"slug": "long", "doc": os.path.join(LANE, "long_start.md")},
    {"slug": "gone", "doc": os.path.join(LANE, "gone_start.md")}]}, open(spec, "w"))
rc, out = shoal("doc-head", "--shoal", "g1", "--slug", "lane")
assert rc == 0 and "Posture: active on lane#1." in out, out
rep = js("doc-head", "--all")
by = {os.path.basename(d["path"]): d for d in rep["docs"]}
assert rep["v"] == 1 and by["long_start.md"]["over"] and not by["lane_start.md"]["over"] and by["gone_start.md"]["missing"], rep
rc, out = shoal("doc-head", "--all")
assert "3 start docs: 1 over their header cap" in out and "MISSING" in out, out
print("doc-head for a role and --all ok")

# refs reads the repository's tiers: a prompt archive is history, a generated tree is never scanned, and a linked
# history document's old paths are not dangling
write(f"{D}/lane_prompts.md", "Asked for `kept_for_prompts.md`; see `proj/design/lane/never_existed.md`.\n")
write(f"{D}/kept_for_prompts.md", "only the prompt archive names this\n")
write(f"{D}/generated/out.md", "generated\n")
write(f"{D}/lane_start.md", "# lane\n\nLast updated: 2026-11-08T10:00Z\n\nPosture: active. Prompts: `lane_prompts.md`.\n" + body)
sh("git", "add", "-A")
sh("git", "commit", "-q", "-m", "lane")
r = js("refs", LANE)
cls = {os.path.basename(f["path"]): f["class"] for f in r["files"]}
assert cls["kept_for_prompts.md"] == "history-only", cls
assert "out.md" not in cls, "the repository's exclude glob keeps generated/ out"
assert r["tiers"]["history"] == ["*_log.md", "*_prompts.md"] and r["lifecycle"]["found"], r["tiers"]
assert not r["dangling"], "a history document's old paths are not dangling: %r" % r["dangling"]
print("refs with the repository's tiers ok")

subprocess.run(["rm", "-rf", T], check=True)
print("test_lifecycle OK")
