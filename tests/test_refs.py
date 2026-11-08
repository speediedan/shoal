#!/usr/bin/env python3
"""Hermetic proof of `shoal refs` (shoal/refs.py) on a scratch repository, ledger and standalone registry:

- `--roots` (JSON `v` 1) names every role doc of a live spec, parked roles included, and every standalone entry's doc,
  never a retired spec's, and says which host it read; with `--all-hosts` each routed host answers for itself over
  its route, and one that cannot (an old shoal, another contract) is named with the reason; a relative standalone
  doc resolves against the entry's cwd or is reported, never resolved against this process's;
- a role directory's files are classed root (the start doc, a symlinked spec doc and its target), live (transitively,
  and from a live document outside the directory), history-only, superseded and unreferenced (on both sides of the age
  floor), with `keep:` honoured and the candidates listed;
- an untracked file, an uncommitted deletion and a dangling link make `--check` exit 1, and the directory is clean
  once they are gone;
- without the known roots, a spec's start doc reads as unreferenced: what the roots exist to prevent.

Synthetic paths and ids only."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile

T = tempfile.mkdtemp(prefix="shoal-refs-test-")
SHOALD = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "shoal")
OLD = "2026-01-01T00:00:00Z"
REPO = os.path.join(T, "repo")
D = "proj/design/lane"
LANE = os.path.join(REPO, D)
ENV = dict(os.environ, SHOAL_LEDGER_DIR=os.path.join(T, "ledger"), XDG_CONFIG_HOME=os.path.join(T, "xdg"),
           SHOAL_SERVER="host-a", GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
ENV.pop("SHOAL_SPEC", None)


def sh(*args: str, env: dict | None = None) -> str:
    return subprocess.run(args, cwd=REPO, env=env or ENV, check=True, capture_output=True, text=True).stdout


def refs(*args: str) -> tuple[int, dict | str]:
    r = subprocess.run([sys.executable, os.path.join(SHOALD, "cli.py"), "refs", *args], env=ENV, capture_output=True,
                       text=True)
    try:
        return r.returncode, json.loads(r.stdout)
    except ValueError:
        return r.returncode, r.stdout + r.stderr


def write(rel: str, text: str) -> None:
    p = os.path.join(REPO, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w") as fh:
        fh.write(text)


os.makedirs(LANE)
sh("git", "init", "-q", "-b", "main")
sh("git", "config", "user.email", "t@example.invalid")
sh("git", "config", "user.name", "t")
write(f"{D}/lane_start.md", "# lane\n\nSee [aux](aux.md); the log is `lane_log.md`. Gone: `proj/design/lane/missing.md`.\n")
write(f"{D}/lane_log.md", "C1: wrote old.md.\n")
write(f"{D}/aux.md", "details in deep.md\n")
write(f"{D}/deep.md", "deep\n")
write(f"{D}/old.md", "old\n")
write(f"{D}/orphan.md", "nobody names this\n")
write(f"{D}/sup.md", "---\nstatus: superseded\n---\nreplaced\n")
write(f"{D}/kept.md", "keep: evidence for an open question\n\nkept\n")
write(f"{D}/other_start.md", "# a parked role's start doc\n")
write(f"{D}/other2.md", "# the target of a symlinked start doc\n")
write(f"{D}/ext.md", "named from outside\n")
write("proj/README.md", "The lane's external note: `proj/design/lane/ext.md`.\n")
os.symlink("other2.md", os.path.join(LANE, "link_start.md"))
sh("git", "add", "-A")
sh("git", "commit", "-q", "-m", "old", env=dict(ENV, GIT_AUTHOR_DATE=OLD, GIT_COMMITTER_DATE=OLD))
write(f"{D}/fresh.md", "new and not linked yet\n")
sh("git", "add", "-A")
sh("git", "commit", "-q", "-m", "fresh")
write(f"{D}/untracked.md", "never added\n")


def spec(sid: str, roles: list[dict], retired: str = "") -> None:
    p = os.path.join(T, "ledger", "shoals", sid, "shoal.json")
    os.makedirs(os.path.dirname(p), exist_ok=True)
    json.dump({"shoal": sid, "dir": REPO, "retired_utc": retired, "roles": roles}, open(p, "w"))


spec("g1", [{"slug": "a", "doc": os.path.join(LANE, "other_start.md"), "parked": {"at": OLD, "by": "user"}},
            {"slug": "b", "doc": os.path.join(LANE, "link_start.md")}])
spec("g0", [{"slug": "z", "doc": os.path.join(LANE, "orphan.md")}], retired=OLD)
os.makedirs(os.path.join(T, "xdg", "shoal"))
json.dump({"v": 1, "sessions": [{"harness": "claude", "session": "s0000001", "label": "seat",
                                 "doc": os.path.join(LANE, "lane_start.md")}]},
          open(os.path.join(T, "xdg", "shoal", "ui-standalone.json"), "w"))

# --roots
rc, r = refs("--roots", "--json")
assert rc == 0 and isinstance(r, dict), r
assert r["v"] == 1 and r["hosts"] == ["host-a"] and r["specs_live"] == 1, r
real = os.path.realpath(LANE)
got = {os.path.basename(p): w for p, w in r["roots"].items()}
assert set(got) == {"other_start.md", "link_start.md", "lane_start.md"}, got
assert got["other_start.md"][0] == {"kind": "role", "shoal": "g1", "slug": "a", "parked": True}, got
assert got["lane_start.md"][0]["kind"] == "standalone" and got["lane_start.md"][0]["host"] == "host-a", got
assert "orphan.md" not in got, "a retired spec's doc is not a root"
print("refs --roots ok")

# the directory
rc, r = refs(LANE, "--json", "--min-age-days", "7")
assert rc == 0 and isinstance(r, dict), r
cls = {os.path.basename(f["path"]): f["class"] for f in r["files"]}
want = {"lane_start.md": "root", "other_start.md": "root", "link_start.md": "root", "other2.md": "root",
        "aux.md": "live", "deep.md": "live", "lane_log.md": "live", "ext.md": "live",
        "old.md": "history-only", "sup.md": "superseded", "orphan.md": "unreferenced", "fresh.md": "unreferenced",
        "kept.md": "unreferenced"}
assert cls == want, {k: (cls.get(k), v) for k, v in want.items() if cls.get(k) != v}
cand = {os.path.basename(p) for p in r["candidates"]}
assert cand == {"old.md", "sup.md", "orphan.md"}, cand  # fresh.md is under the age floor; kept.md says why it stays
assert [os.path.basename(x) for x in r["untracked"]] == ["untracked.md"], r["untracked"]
assert [x["target"] for x in r["dangling"]] == ["proj/design/lane/missing.md"], r["dangling"]
assert not r["ok"] and r["v"] == 1, r
rc, _ = refs(LANE, "--check")
assert rc == 1, "untracked and dangling must fail --check"
print("refs <dir> ok")

# without the known roots, a spec's start doc looks unreferenced
rc, r = refs(LANE, "--json", "--no-known-roots")
assert {os.path.basename(f["path"]): f["class"] for f in r["files"]}["other_start.md"] == "unreferenced", r
rc, out = refs(LANE, "--no-known-roots")
assert "WARNING: --no-known-roots" in out, out
print("refs --no-known-roots ok")

# clean: the untracked file gone, the link fixed; then an uncommitted deletion fails again
os.remove(os.path.join(LANE, "untracked.md"))
write(f"{D}/lane_start.md", "# lane\n\nSee [aux](aux.md); the log is `lane_log.md`.\n")
sh("git", "commit", "-q", "-am", "fix")
rc, _ = refs(LANE, "--check")
assert rc == 0, "a clean directory passes --check"
os.remove(os.path.join(LANE, "deep.md"))
rc, r = refs(LANE, "--json")
assert [os.path.basename(x) for x in r["deleted"]] == ["deep.md"] and not r["ok"], r
print("refs --check ok")

# the registry moved to core; the viewer re-exports the same functions
sys.path[:0] = [SHOALD, os.path.join(SHOALD, "ui")]
import standalone  # noqa: E402
import ui_herdr  # noqa: E402
assert ui_herdr.load_standalone is standalone.load_standalone and ui_herdr.standalone_path is standalone.standalone_path
print("standalone registry in core ok")

# --all-hosts: each routed host answers `refs --roots --json` for itself over its route (a stand-in ssh here); a host
# whose answer is missing or of another contract is named in `unreachable` with the reason, never silently dropped
STUB = os.path.join(T, "ssh-stub")
FAR = os.path.join(T, "far.json")
with open(STUB, "w") as fh:
    fh.write('#!/bin/sh\ncase "$*" in *c.invalid*) echo "refs: no such command" >&2; exit 2;; '
             '*d.invalid*) echo \'{"v": 2, "roots": {}}\'; exit 0;; esac\ncat "$STUB_FAR"\n')
os.chmod(STUB, 0o755)
json.dump({"v": 1, "hosts": ["host-b"], "roots": {"/elsewhere/far_start.md": [
    {"kind": "standalone", "label": "far", "host": "host-b"}]}}, open(FAR, "w"))
with open(os.path.join(T, "host.toml"), "w") as fh:
    fh.write('[hosts.host-b]\nssh = "u@b.invalid"\n[hosts.host-c]\nssh = "u@c.invalid"\n[hosts.host-d]\nssh = "u@d.invalid"\n')
ENV.update(SHOAL_CONFIG=os.path.join(T, "host.toml"), SHOAL_SSH=STUB, STUB_FAR=FAR,
           SHOAL_ROUTE_CACHE=os.path.join(T, "route-cache"))
rc, r = refs("--roots", "--all-hosts", "--json")
assert rc == 0 and r["hosts"] == ["host-a", "host-b"], r
assert r["roots"]["/elsewhere/far_start.md"] == [{"kind": "standalone", "label": "far", "host": "host-b"}], r["roots"]
why = {u["host"]: u["reason"] for u in r["unreachable"]}
assert set(why) == {"host-c", "host-d"} and "too old" in why["host-c"] and "contract v2" in why["host-d"], why
rc, r = refs("--roots", "--json")
assert r["hosts"] == ["host-a"] and "/elsewhere/far_start.md" not in r["roots"], "without --all-hosts only this host"
rc, out = refs("--roots")
assert "--all-hosts" in out, out  # the text form says what it did not read
print("refs --roots --all-hosts ok")

# a relative standalone doc resolves against the entry's cwd, or is reported and left out, never resolved here
json.dump({"v": 1, "sessions": [{"harness": "claude", "session": "s0000003", "label": "rel", "cwd": LANE, "doc": "kept.md"},
                                {"harness": "claude", "session": "s0000004", "label": "nocwd", "doc": "orphan.md"}]},
          open(os.path.join(T, "xdg", "shoal", "ui-standalone.json"), "w"))
rc, r = refs("--roots", "--json")
names = {os.path.basename(p) for p in r["roots"]}
assert "kept.md" in names and "orphan.md" not in names, names
assert any("'orphan.md'" in n and "nocwd" in n for n in r["notes"]), r["notes"]
print("relative standalone docs ok")

subprocess.run(["rm", "-rf", T], check=True)
print("test_refs OK")
