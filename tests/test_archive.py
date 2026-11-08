#!/usr/bin/env python3
"""Hermetic proof of `shoal archive` (shoal/archive.py) on a scratch repository, ledger and host config:

- a history-only file moves to the layout's archive path in one commit of exactly its paths, with an `archive` event
  in its directory's manifest; a staged change elsewhere is left staged, not swept in;
- refused, with nothing changed: a file a live document names, a start-doc root, an untracked file, a file with
  uncommitted changes; `--dry-run` changes nothing;
- a moved relative symlink is re-aimed from its new place; a symlink and its target archived together stay paired;
- a trusted commit hook that refuses leaves the tree, index and manifest exactly as they were; one that commits and
  then fails (a rejected push) is a landed run with a note, not undone;
- `--restore` from warm; `--cold` refused before warm_days and when a live document names the archive path, done with
  `--force`, its `restore_from` holding the file; `--restore` from cold brings the content back;
- a root's companion log (`<stem>_log.md`) is live in `refs` even when nothing links it;
- another host's start-doc roots are honoured, and a host that cannot answer refuses unless `--force`;
- going cold records the blob and mode: a binary and an executable come back exactly, and an orphaned `restore_from`
  (a rebase rewrote that commit) still restores by blob; a hook template's other braces are left as written.

Synthetic paths and ids only."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile

T = tempfile.mkdtemp(prefix="shoal-archive-test-")
SHOALD = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "shoal")
REPO = os.path.join(T, "repo")
D = "proj/design/lane"
LANE = os.path.join(REPO, D)
HOST = os.path.join(T, "host.toml")
ENV = dict(os.environ, SHOAL_CONFIG=HOST, SHOAL_LEDGER_DIR=os.path.join(T, "ledger"), XDG_CONFIG_HOME=os.path.join(T, "xdg"),
           SHOAL_SERVER="host-a", GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1",
           SHOAL_ROUTE_CACHE=os.path.join(T, "route-cache"), SHOAL_SSH=os.path.join(T, "ssh-stub"))
for k in ("SHOAL_SPEC", "SHOAL_SLUG"):
    ENV.pop(k, None)


BINARY = bytes(range(256)) + b"\xff\xfe\x00"


def write(rel: str, text: str) -> None:
    p = os.path.join(REPO, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w") as fh:
        fh.write(text)


def git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=REPO, env=ENV, check=True, capture_output=True, text=True).stdout


def archive(*args: str) -> tuple[int, dict]:
    r = subprocess.run([sys.executable, os.path.join(SHOALD, "cli.py"), "archive", *args, "--json"], env=ENV,
                       capture_output=True, text=True, cwd=REPO)
    try:
        return r.returncode, json.loads(r.stdout)
    except ValueError:
        raise AssertionError(r.stdout + r.stderr)


def state() -> tuple[str, str, str]:
    """HEAD, the porcelain status and the file list: what a refused run must leave exactly as it was."""
    files = subprocess.run(["find", ".", "-path", "./.git", "-prune", "-o", "-print"], cwd=REPO, capture_output=True,
                           text=True).stdout
    return git("rev-parse", "HEAD"), git("status", "--porcelain"), "\n".join(sorted(files.split()))


def manifest() -> list[dict]:
    p = os.path.join(REPO, "archive", D, "MANIFEST.jsonl")
    return [json.loads(x) for x in open(p)] if os.path.exists(p) else []


open(HOST, "w").write("")
os.makedirs(LANE)
git("init", "-q", "-b", "main")
git("config", "user.email", "t@example.invalid")
git("config", "user.name", "t")
write(f"{D}/lane_start.md", "# lane\n\nLast updated: 2026-11-08T10:00Z\n\nSee [aux](aux.md).\n")
write(f"{D}/lane_log.md", "C1: wrote old.md, tmp.md and gone.md.\n")
for name in ("aux", "old", "tmp", "gone", "pair", "far_start", "lone"):
    write(f"{D}/{name}.md", f"{name}\n")
with open(os.path.join(LANE, "blob.bin"), "wb") as fh:
    fh.write(BINARY)
write(f"{D}/tool.sh", "#!/bin/sh\necho tool\n")
os.chmod(os.path.join(LANE, "tool.sh"), 0o755)
write("proj/shared/x.md", "shared\n")
write("proj/README.md", "unrelated\n")
os.symlink("../../shared/x.md", os.path.join(LANE, "alias.md"))
os.symlink("pair.md", os.path.join(LANE, "pair_link.md"))
git("add", "-A")
git("commit", "-q", "-m", "base")
spec = os.path.join(T, "ledger", "shoals", "g1", "shoal.json")
os.makedirs(os.path.dirname(spec))
json.dump({"shoal": "g1", "dir": REPO, "roles": [{"slug": "lane", "doc": os.path.join(LANE, "lane_start.md")}]},
          open(spec, "w"))

# the companion log is live by contract, linked or not
r = json.loads(subprocess.run([sys.executable, os.path.join(SHOALD, "cli.py"), "refs", LANE, "--json"], env=ENV,
                              capture_output=True, text=True).stdout)
assert {os.path.basename(f["path"]): f["class"] for f in r["files"]}["lane_log.md"] == "live", r["files"]
print("companion log ok")

# another host's start-doc roots: a doc a standalone seat there uses is refused here; a host that cannot answer refuses
# unless --force, which says so
FAR = os.path.join(T, "far.json")
json.dump({"v": 1, "hosts": ["host-b"], "roots": {os.path.join(LANE, "far_start.md"): [
    {"kind": "standalone", "label": "far-seat", "host": "host-b"}]}}, open(FAR, "w"))
with open(ENV["SHOAL_SSH"], "w") as fh:
    fh.write(f'#!/bin/sh\ncase "$*" in *c.invalid*) exit 255;; esac\ncat "{FAR}"\n')
os.chmod(ENV["SHOAL_SSH"], 0o755)
open(HOST, "w").write('[hosts.host-b]\nssh = "u@b.invalid"\n')
rc, out = archive("proj/design/lane/far_start.md")
assert rc == 1 and "is a start doc (standalone session far-seat on host-b)" in out["refused"], out
open(HOST, "w").write('[hosts.host-b]\nssh = "u@b.invalid"\n[hosts.host-c]\nssh = "u@c.invalid"\n')
rc, out = archive("proj/design/lane/lone.md")
assert rc == 1 and "cannot read the start-doc roots of host-c" in out["refused"], out
rc, out = archive("proj/design/lane/lone.md", "--force")
assert rc == 0 and any("without the roots of host-c" in n for n in out["notes"]), out
open(HOST, "w").write("")
print("other hosts' roots ok")

# refusals change nothing
before = state()
for args, why in ((["proj/design/lane/aux.md"], "named by live documents"), (["proj/design/lane/lane_start.md"], "start doc"),
                  (["proj/design/lane/new.md"], "not tracked")):
    if "new.md" in args[0]:
        write(f"{D}/new.md", "new\n")
    rc, out = archive(*args)
    assert rc == 1 and why in out["refused"], (args, out)
os.remove(os.path.join(LANE, "new.md"))
write(f"{D}/old.md", "edited\n")
rc, out = archive("proj/design/lane/old.md")
assert rc == 1 and "uncommitted changes" in out["refused"], out
git("checkout", "--", f"{D}/old.md")
rc, out = archive("proj/design/lane/old.md", "--dry-run")
assert rc == 0 and out["dry_run"] and any("move proj/design/lane/old.md -> archive/proj/design/lane/old.md" in s for s in out["steps"])
assert state() == before, "a refusal or dry run changed something"
print("refusals and dry run ok")

# archive a history-only file, with an unrelated staged change that must stay staged and out of the commit
write("proj/README.md", "staged elsewhere\n")
git("add", "proj/README.md")
rc, out = archive("proj/design/lane/old.md", "--reason", "concluded", "--by", "lane")
assert rc == 0 and out["ok"], out
assert os.path.exists(os.path.join(REPO, "archive", D, "old.md")) and not os.path.exists(os.path.join(LANE, "old.md"))
files = git("show", "--no-renames", "--name-only", "--format=", "HEAD").split()
assert sorted(files) == sorted([f"{D}/old.md", f"archive/{D}/old.md", f"archive/{D}/MANIFEST.jsonl"]), files
assert "M  proj/README.md" in git("status", "--porcelain"), "the unrelated staged change stays staged"
e = manifest()[-1]
assert (e["event"], e["path"], e["archive_path"], e["reason"], e["by"]) == ("archive", f"{D}/old.md", f"archive/{D}/old.md", "concluded", "lane"), e
git("reset", "-q", "HEAD", "--", "proj/README.md")
git("checkout", "--", "proj/README.md")
print("archive ok")

# symlinks: a relative link is re-aimed from its new place; a link archived with its target stays paired
rc, out = archive("proj/design/lane/alias.md")
assert rc == 0, out
link = os.path.join(REPO, "archive", D, "alias.md")
assert os.readlink(link) == "../../../../proj/shared/x.md" and open(link).read() == "shared\n", os.readlink(link)
rc, out = archive("proj/design/lane/pair.md")
assert rc == 1 and "named by live documents" in out["refused"], "a symlink to it is a live reference"
rc, out = archive("proj/design/lane/pair.md", "proj/design/lane/pair_link.md")
assert rc == 0 and open(os.path.join(REPO, "archive", D, "pair_link.md")).read() == "pair\n", out
print("symlinks ok")

# a trusted hook that refuses: nothing left behind; one that commits then fails: landed, with a note
# a template with other braces (shell ${VAR}, awk-style {x}) is left as written; only {message} and {paths} expand
write(".shoal/lifecycle.toml", "[hooks]\ncommit = 'sh hook.sh {message} {paths} # ${HOME} {print}'\n")
write("hook.sh", 'exit 1\n')
git("add", "-A")
git("commit", "-q", "-m", "hooks")
open(HOST, "w").write(f'[lifecycle]\ntrusted_repos = ["{REPO}"]\n')
before, mf = state(), manifest()
rc, out = archive("proj/design/lane/tmp.md")
assert rc == 1 and "hook exited 1" in out["refused"], out
assert state() == before and manifest() == mf, "a refused hook must leave the tree, index and manifest as they were"
write("hook.sh", 'msg=$1; shift; git commit -q -m "$msg" -- "$@" && exit 5\n')
git("commit", "-q", "-am", "hook commits then fails")
rc, out = archive("proj/design/lane/tmp.md")
assert rc == 0 and any("hook exited 5" in n for n in out["notes"]), out
assert os.path.exists(os.path.join(REPO, "archive", D, "tmp.md")), "a landed commit is not undone"
open(HOST, "w").write("")
print("hooks ok")

# restore from warm
rc, out = archive("--restore", "proj/design/lane/old.md")
assert rc == 0 and open(os.path.join(LANE, "old.md")).read() == "old\n" and manifest()[-1]["event"] == "restore", out
print("restore from warm ok")

# cold: refused too soon, refused while a live document names the archive path, then done; restore from cold
rc, out = archive("proj/design/lane/gone.md")
assert rc == 0, out
rc, out = archive("--cold", "proj/design/lane/gone.md")
assert rc == 1 and "fewer than [cold] warm_days" in out["refused"], out
write("proj/notes.md", "Evidence: `archive/proj/design/lane/gone.md`.\n")
git("add", "proj/notes.md")
git("commit", "-q", "-m", "a live note names the archived file")
rc, out = archive("--cold", "proj/design/lane/gone.md", "--force")
assert rc == 1 and "still named by live documents" in out["refused"], out
git("rm", "-q", "proj/notes.md")
git("commit", "-q", "-m", "drop the note")
holder = git("rev-parse", "HEAD").strip()
rc, out = archive("--cold", "proj/design/lane/gone.md", "--force")
assert rc == 0 and not os.path.exists(os.path.join(REPO, "archive", D, "gone.md")), out
e = manifest()[-1]
assert e["event"] == "cold" and e["restore_from"] == holder, e
assert git("cat-file", "blob", f"{holder}:archive/{D}/gone.md") == "gone\n"
rc, out = archive("--restore", "proj/design/lane/gone.md")
assert rc == 0 and open(os.path.join(LANE, "gone.md")).read() == "gone\n" and manifest()[-1]["event"] == "restore", out
print("cold and restore from cold ok")

# cold keeps content and mode: a binary and an executable come back byte for byte, exec bit set
rc, out = archive("proj/design/lane/blob.bin", "proj/design/lane/tool.sh")
assert rc == 0, out
rc, out = archive("--cold", "proj/design/lane/blob.bin", "proj/design/lane/tool.sh", "--force")
assert rc == 0, out
cold = {e["path"]: e for e in manifest() if e["event"] == "cold"}
assert cold[f"{D}/tool.sh"]["mode"] == "100755" and cold[f"{D}/blob.bin"]["blob"], cold
for f in ("blob.bin", "tool.sh"):
    rc, out = archive("--restore", f"proj/design/lane/{f}")
    assert rc == 0, out
assert open(os.path.join(LANE, "blob.bin"), "rb").read() == BINARY, "a binary is restored byte for byte"
assert os.access(os.path.join(LANE, "tool.sh"), os.X_OK) and open(os.path.join(LANE, "tool.sh")).read() == "#!/bin/sh\necho tool\n"
print("binary and mode ok")

# an orphaned restore_from (a rebase rewrote the commit it names): the blob id still restores the file
rc, out = archive("proj/design/lane/blob.bin")
assert rc == 0, out
rc, out = archive("--cold", "proj/design/lane/blob.bin", "--force")
assert rc == 0, out
mpath = os.path.join(REPO, "archive", D, "MANIFEST.jsonl")
lines = open(mpath).read().splitlines()
last = json.loads(lines[-1])
last["restore_from"] = "0" * 40
open(mpath, "w").write("\n".join(lines[:-1] + [json.dumps(last, sort_keys=True)]) + "\n")
git("commit", "-q", "-am", "simulate an orphaned restore_from")
rc, out = archive("--restore", "proj/design/lane/blob.bin")
assert rc == 0 and open(os.path.join(LANE, "blob.bin"), "rb").read() == BINARY, out
print("restore by blob ok")

subprocess.run(["rm", "-rf", T], check=True)
print("test_archive OK")
