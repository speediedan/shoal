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
  (a rebase rewrote that commit) still restores by blob; a hook template's other braces are left as written;
- `--migrate` moves a legacy nested archive folder (or a file in one) to the mirror of each file's original path with
  `migrate` events naming the legacy path in `from`, refusing live references unless rewritten, a path with no legacy
  segment, a path already under the archive root, two legacy files with one original in a run, an original that is
  live again, and a mirror that already exists; `refs --resolve` finds it by `from`;
- a directory the run empties is a target (listed, dry-run, rewritten keeping a trailing slash, prose words left alone),
  refused under rewrite when its files land in different places; a refusing hook's cause is shown and its whole output
  kept under the git directory; a .csv field and an extension listed in `[refs] text_ext` are referrers.

Synthetic paths and ids only."""
from __future__ import annotations

import json
import os
import site
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
# the cause printed first and a page of accepted notes after it, on the other stream: the refusal still shows the cause
write("hook.sh", 'echo "check....Failed"; echo "the-real-cause: x.md is still named"; '
      'i=0; while [ $i -lt 40 ]; do echo "note: an accepted reference, number $i" >&2; i=$((i+1)); done; exit 1\n')
git("commit", "-q", "-am", "a hook whose cause comes first")
before = state()
rc, out = archive("proj/design/lane/tmp.md")
assert rc == 1 and "the-real-cause" in out["refused"] and "full output: " in out["refused"], out
kept = out["refused"].split("full output: ", 1)[1].split(")")[0]
assert kept.startswith(os.path.join(REPO, ".git")) and "number 39" in open(kept).read(), kept
assert state() == before, "the kept output lives under the git directory, not in the tree"
# only the newest KEEP_OUTPUTS logs stay: plant older ones and refuse once more
kd = os.path.dirname(kept)
for i in range(25):
    open(os.path.join(kd, f"archive-hook-2000-01-01T00{i:02d}00Z.log"), "w").write("old\n")
rc, out = archive("proj/design/lane/tmp.md")
logs = sorted(f for f in os.listdir(kd) if f.startswith("archive-hook-"))
assert rc == 1 and len(logs) == 20 and not any(f.startswith("archive-hook-2000-01-01T0000") for f in logs), logs
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

# --cross-owner rewrite: another owner's live documents and symlinks are pointed at the archive paths in the same
# commit, each reference in the form it was written in; history keeps its references and is counted
RW = "proj/design/rw"
# a `~/` reference needs the repository under HOME; keep the user site (tomli) where it was
ENV["PYTHONUSERBASE"] = site.getuserbase()
ENV["HOME"] = T
for name in "abcdefg":
    write(f"{RW}/{name}.md", f"{name}\n")
OTHER = (f"# other\n\nSee [a](../design/rw/a.md#sec), and `{RW}/b.md`.\n"
         f"Abs: {REPO}/{RW}/c.md; twice: ../design/rw/a.md ../design/rw/a.md.\n"
         "Suffix: rw/d.md is here. Home: ~/repo/proj/design/rw/e.md\nUnrelated: proj/README.md\n")
write("proj/other/other_start.md", OTHER)
write("proj/other/other_log.md", "C1: read ../design/rw/a.md\n")
os.symlink("../design/rw/f.md", os.path.join(REPO, "proj/other/f_link.md"))
os.makedirs(os.path.join(REPO, "proj/third"))
os.symlink("../design/rw/g.md", os.path.join(REPO, "proj/third/third_start.md"))
git("add", "-A")
git("commit", "-q", "-m", "rewrite base")
json.dump({"shoal": "g1", "dir": REPO, "roles": [{"slug": "lane", "doc": os.path.join(LANE, "lane_start.md")},
                                                 {"slug": "other", "doc": os.path.join(REPO, "proj/other/other_start.md")},
                                                 {"slug": "third", "doc": os.path.join(REPO, "proj/third/third_start.md")}]},
          open(spec, "w"))
TARGETS = [f"{RW}/{x}.md" for x in "abcdef"]
before = state()
rc, out = archive(*TARGETS)
assert rc == 1 and "named by live documents" in out["refused"] and "--cross-owner rewrite" in out["refused"], out
rc, out = archive(*TARGETS, "--cross-owner", "keep")
assert rc == 1 and "not defined yet" in out["refused"], out
rc, out = archive("--cold", f"{RW}/a.md", "--cross-owner", "rewrite")
assert rc == 1 and "archiving only" in out["refused"], out
rc, out = archive(f"{RW}/g.md", "--cross-owner", "rewrite")
assert rc == 1 and "proj/third/third_start.md is a start doc" in out["refused"] and "move that role first" in out["refused"], out
rc, out = archive(*TARGETS, "--cross-owner", "rewrite", "--dry-run")
assert rc == 0 and out["steps"][0] == "rewrite 8 reference(s) in 2 file(s), listed below", out["steps"][:2]
assert rc == 0 and f"rewrite proj/other/other_start.md:3 `../design/rw/a.md` -> `../../archive/{RW}/a.md`" in out["steps"], out
assert f"repoint symlink proj/other/f_link.md: ../design/rw/f.md -> ../../archive/{RW}/f.md" in out["steps"], out
write("proj/other/other_start.md", OTHER + "an edit in progress\n")
rc, out = archive(*TARGETS, "--cross-owner", "rewrite")
assert rc == 1 and "proj/other/other_start.md has uncommitted changes" in out["refused"], out
write("proj/other/other_start.md", OTHER)
assert state() == before, "a refused or dry rewrite changed something"
print("rewrite refusals and dry run ok")

# a refusing hook puts every referrer back byte for byte; an edit made after the rewrite is left in place and named
write("hook.sh", "exit 1\n")
git("commit", "-q", "-am", "hook refuses")
open(HOST, "w").write(f'[lifecycle]\ntrusted_repos = ["{REPO}"]\n')
before = state()
rc, out = archive(*TARGETS, "--cross-owner", "rewrite")
assert rc == 1 and "hook exited 1" in out["refused"], out
assert state() == before and open(os.path.join(REPO, "proj/other/other_start.md")).read() == OTHER
assert os.readlink(os.path.join(REPO, "proj/other/f_link.md")) == "../design/rw/f.md"
write("hook.sh", "echo concurrent >> proj/other/other_start.md\nexit 1\n")
git("commit", "-q", "-am", "hook edits a referrer, then refuses")
rc, out = archive(*TARGETS, "--cross-owner", "rewrite")
assert rc == 1 and "left proj/other/other_start.md as found" in out["refused"], out
assert open(os.path.join(REPO, "proj/other/other_start.md")).read().endswith("concurrent\n"), "the later edit stays"
assert not os.path.exists(os.path.join(REPO, "archive", RW, "a.md")), "the moves are undone"
git("reset", "-q", "HEAD", "--", "proj/other/other_start.md")
git("checkout", "--", "proj/other/other_start.md")
open(HOST, "w").write("")
assert state()[1] == "", state()
print("rewrite undo ok")

# the verifier catches a writer that is not a pure path substitution, before anything is committed
os.environ.update(ENV)
sys.path.insert(0, SHOALD)
import archive as archive_mod  # noqa: E402
os.chdir(REPO)
plan = archive_mod.plan_archive([os.path.join(REPO, p) for p in TARGETS], "", "t", cross_owner="rewrite")
pure = archive_mod._substitute
archive_mod._substitute = lambda text, subs: pure(text, subs) + "extra\n"
before = state()
try:
    archive_mod.apply(plan, "should not land")
    raise AssertionError("an impure rewrite was committed")
except archive_mod.Refused as exc:
    assert "changed text after its last substitution" in str(exc), exc
archive_mod._substitute = pure
assert state() == before, "a caught rewrite leaves nothing behind"
print("rewrite verifier ok")

# the rewrite itself: one commit with the moves, the referrers and the manifest; forms, anchors and punctuation kept
rc, out = archive(*TARGETS, "--cross-owner", "rewrite", "--reason", "concluded")
assert rc == 0 and out["ok"], out
A = f"archive/{RW}"
assert open(os.path.join(REPO, "proj/other/other_start.md")).read() == (
    f"# other\n\nSee [a](../../{A}/a.md#sec), and `{A}/b.md`.\n"
    f"Abs: {REPO}/{A}/c.md; twice: ../../{A}/a.md ../../{A}/a.md.\n"
    f"Suffix: {A}/d.md is here. Home: ~/repo/{A}/e.md\nUnrelated: proj/README.md\n"), "every form keeps its form"
assert open(os.path.join(REPO, "proj/other/other_log.md")).read() == "C1: read ../design/rw/a.md\n", "history untouched"
link = os.path.join(REPO, "proj/other/f_link.md")
assert os.readlink(link) == f"../../{A}/f.md" and open(link).read() == "f\n"
files = git("show", "--no-renames", "--name-only", "--format=", "HEAD").split()
assert sorted(files) == sorted(TARGETS + [f"{A}/{x}.md" for x in "abcdef"]
                               + [f"{A}/MANIFEST.jsonl", "proj/other/other_start.md", "proj/other/f_link.md"]), files
assert "rewrote 8 reference(s) in 2 file(s)" in git("log", "-1", "--format=%s"), git("log", "-1", "--format=%s")
ev = {e["path"]: e for e in (json.loads(x) for x in open(os.path.join(REPO, A, "MANIFEST.jsonl")))}
assert ev[f"{RW}/a.md"]["refs"] == {"history": 1, "rewritten": [
    {"file": "proj/other/other_start.md", "line": n, "from": "../design/rw/a.md", "to": f"../../{A}/a.md"}
    for n in (3, 4, 4)]}, ev[f"{RW}/a.md"]
assert ev[f"{RW}/f.md"]["refs"]["rewritten"] == [{"file": "proj/other/f_link.md", "line": 0, "from": "../design/rw/f.md",
                                                  "to": f"../../{A}/f.md"}], ev[f"{RW}/f.md"]
assert git("status", "--porcelain") == ""
print("rewrite ok")

# --migrate: a legacy nested archive folder moves to the mirror of each file's original path, with migrate events
LG = "proj/design/lane2/handoffs/archive"
write(f"{LG}/h1.md", "h1\n")
write(f"{LG}/deep/h2.md", "h2\n")
write("proj/design/lane2/handoffs/live_h3.md", "h3\n")
write("proj/other/cites_start.md", f"Old handoff: [h1](../design/lane2/handoffs/archive/h1.md).\n")
git("add", "-A")
git("commit", "-q", "-m", "a legacy archive folder")
before = state()
rc, out = archive("--migrate", "proj/design/lane2/handoffs/archive")
assert rc == 1 and "is still named by live documents" in out["refused"] and state() == before, out
rc, out = archive("--migrate", "proj/design/lane2/handoffs/live_h3.md")
assert rc == 1 and "not in a legacy archive folder" in out["refused"], out
write("proj/design/lane2/handoffs/archived/h1.md", "twin\n")
write("archive/proj/archive/x.md", "x\n")
git("add", "-A")
git("commit", "-q", "-m", "a twin legacy copy and a file under the archive root")
before = state()
rc, out = archive("--migrate", f"{LG}/h1.md", "proj/design/lane2/handoffs/archived/h1.md", "--cross-owner", "rewrite")
assert rc == 1 and "both had the original path proj/design/lane2/handoffs/h1.md" in out["refused"], out
rc, out = archive("--migrate", "archive/proj/archive/x.md")
assert rc == 1 and "already under the archive root archive/" in out["refused"] and state() == before, out
git("rm", "-q", "-r", "proj/design/lane2/handoffs/archived", "archive/proj/archive")
git("commit", "-q", "-m", "drop the twin and the archive-root file")
rc, out = archive("--migrate", "--restore", f"{LG}/h1.md")
assert rc == 1 and "not with --restore" in out["refused"], out
rc, out = archive("--migrate", "proj/design/lane2/handoffs/archive", "--cross-owner", "rewrite", "--reason", "legacy")
assert rc == 0 and out["ok"], out
M = "archive/proj/design/lane2/handoffs"
assert open(os.path.join(REPO, M, "h1.md")).read() == "h1\n"
assert open(os.path.join(REPO, M, "deep/h2.md")).read() == "h2\n"
assert not os.path.exists(os.path.join(REPO, LG)), "the emptied legacy folder is gone"
ev = [json.loads(x) for x in open(os.path.join(REPO, M, "MANIFEST.jsonl"))]
assert ev == [dict(ev[0], event="migrate", path="proj/design/lane2/handoffs/h1.md", **{"from": f"{LG}/h1.md"},
                   archive_path=f"{M}/h1.md", reason="legacy")], ev
deep = [json.loads(x) for x in open(os.path.join(REPO, M, "deep", "MANIFEST.jsonl"))]
assert deep[0]["path"] == "proj/design/lane2/handoffs/deep/h2.md" and deep[0]["from"] == f"{LG}/deep/h2.md", deep
assert f"../../{M}/h1.md" in open(os.path.join(REPO, "proj/other/cites_start.md")).read(), "the live link follows"
assert git("status", "--porcelain") == "" and "migrate: " in git("log", "-1", "--format=%s")
r = subprocess.run([sys.executable, os.path.join(SHOALD, "cli.py"), "refs", "--resolve", f"{LG}/h1.md", "--json"],
                   env=ENV, capture_output=True, text=True, cwd=REPO)
res = json.loads(r.stdout)
assert res["via"] == "from" and res["state"] == "warm" and res["archive_path"] == f"{M}/h1.md", res
write("proj/design/lane2/handoffs/archive/h1.md", "again\n")
write("proj/design/lane2/handoffs/h4.md", "live\n")
write("proj/design/lane2/handoffs/archive/h4.md", "legacy h4\n")
git("add", "-A")
git("commit", "-q", "-m", "a legacy copy whose original is live again")
rc, out = archive("--migrate", "proj/design/lane2/handoffs/archive/h4.md")
assert rc == 1 and "is live again" in out["refused"], out
rc, out = archive("--migrate", "proj/design/lane2/handoffs/archive/h1.md")
assert rc == 1 and "already exists in the archive" in out["refused"], out
print("migrate ok")

# a directory the run empties is a target: named by a live document, it is listed under notify, shown by --dry-run and
# rewritten under rewrite (trailing slash kept, each form kept; a bare word such as prose's "archive" is not a
# reference to it); history keeps it and is counted; a directory whose
# files land in different places is refused under rewrite
L3 = "proj/design/lane3"
write(f"{L3}/archive/top.md", "top\n")
write(f"{L3}/archive/runs/r1/notes.txt", "raw\n")
write(f"{L3}/README.md", "Runs: `archive/runs/r1/` stay raw.\nIndex: proj/design/lane3/archive holds the rest.\n"
      "The raw archive is kept.\n")
write(f"{L3}/lane3_log.md", "C1: kept archive/runs/r1/ as written.\n")
git("add", "-A")
git("commit", "-q", "-m", "a legacy folder named as a directory")
before = state()
rc, out = archive("--migrate", f"{L3}/archive")
assert rc == 1 and f"{L3}/archive/runs/r1 is still named" in out["refused"] and f"{L3}/README.md:1" in out["refused"], out
assert f"{L3}/archive is still named" in out["refused"] and state() == before, out
rc, out = archive("--migrate", f"{L3}/archive", "--cross-owner", "rewrite", "--dry-run")
steps = "\n".join(out["steps"])
assert rc == 0 and f"`archive/runs/r1/` -> `../../../archive/{L3}/runs/r1/`" in steps, steps
assert f"`{L3}/archive` -> `archive/{L3}`" in steps and state() == before, steps
rc, out = archive("--migrate", f"{L3}/archive", "--cross-owner", "rewrite", "--reason", "legacy")
assert rc == 0 and out["ok"], out
assert open(os.path.join(REPO, L3, "README.md")).read() == (
    f"Runs: `../../../archive/{L3}/runs/r1/` stay raw.\nIndex: archive/{L3} holds the rest.\nThe raw archive is kept.\n")
assert open(os.path.join(REPO, L3, "lane3_log.md")).read() == "C1: kept archive/runs/r1/ as written.\n"
assert not os.path.exists(os.path.join(REPO, L3, "archive")) and git("status", "--porcelain") == ""
ev = [json.loads(x) for x in open(os.path.join(REPO, "archive", L3, "runs/r1/MANIFEST.jsonl"))]
dirs = [x for x in ev[0]["refs"]["rewritten"] if x.get("dir") == f"{L3}/archive/runs/r1"]
assert [x["from"] for x in dirs] == ["archive/runs/r1/"] and ev[0]["refs"]["history"] == 1, ev
# each emptied directory's references go with the first file (sorted) moved out of it: here runs/r1/notes.txt for both
assert [x["from"] for x in ev[0]["refs"]["rewritten"] if x.get("dir") == f"{L3}/archive"] == [f"{L3}/archive"], ev
L4 = "proj/design/lane4"
write(f"{L4}/archive/a.md", "a\n")
write(f"{L4}/archive/archived/b.md", "b\n")
write(f"{L4}/index.md", f"Old: {L4}/archive/ is legacy.\n")
git("add", "-A")
git("commit", "-q", "-m", "a legacy folder whose files land in two places")
before = state()
rc, out = archive("--migrate", f"{L4}/archive", "--cross-owner", "rewrite")
assert rc == 1 and "moves to different places" in out["refused"] and f"{L4}/index.md:1" in out["refused"], out
assert state() == before, out
print("directories ok")

# data tables are referrers: a .csv field naming a moved file or an emptied directory is listed and rewritten, the rest of
# the row byte for byte; another extension is read once the repository lists it in [refs] text_ext
L5 = "proj/design/lane5"
write(f"{L5}/archive/runs/r1/raw.txt", "raw\n")
write(f"{L5}/tables/runs.csv", "run_id,data_dir,label\nr1,../archive/runs/r1,\"a, b\"\nr2,../live,x\n")
write(f"{L5}/tables/notes.rst", "See ../archive/runs/r1/raw.txt for the raw run.\n")
git("add", "-A")
git("commit", "-q", "-m", "a run table and an rst note")
before = state()
rc, out = archive("--migrate", f"{L5}/archive")
assert rc == 1 and f"{L5}/tables/runs.csv:2" in out["refused"] and "notes.rst" not in out["refused"], out
assert state() == before, out
lc = open(os.path.join(REPO, ".shoal/lifecycle.toml")).read()
write(".shoal/lifecycle.toml", lc + "\n[refs]\ntext_ext = [\"rst\"]\n")
git("commit", "-q", "-am", "read .rst for references")
rc, out = archive("--migrate", f"{L5}/archive", "--cross-owner", "rewrite")
assert rc == 0 and out["ok"], out
assert open(os.path.join(REPO, L5, "tables/runs.csv")).read() == (
    f"run_id,data_dir,label\nr1,../../../../archive/{L5}/runs/r1,\"a, b\"\nr2,../live,x\n")
assert open(os.path.join(REPO, L5, "tables/notes.rst")).read() == (
    f"See ../../../../archive/{L5}/runs/r1/raw.txt for the raw run.\n")
# a data table over [refs] data_max_bytes is skipped, and the run says so
write(f"{L5}/tables/big.csv", "x," * 400 + f"\n../../../../archive/{L5}/runs/r1\n")
write("proj/design/lane6/z.md", "z\n")
lc = open(os.path.join(REPO, ".shoal/lifecycle.toml")).read()
write(".shoal/lifecycle.toml", lc.replace("[refs]\n", "[refs]\ndata_max_bytes = 512\n"))
git("add", "-A")
git("commit", "-q", "-m", "a table over the cap")
rc, out = archive("proj/design/lane6/z.md", "--dry-run")
assert any(f"{L5}/tables/big.csv not read for references" in n for n in out["notes"]), out
print("data tables ok")

subprocess.run(["rm", "-rf", T], check=True)
print("test_archive OK")
