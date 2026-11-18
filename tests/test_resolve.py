#!/usr/bin/env python3
"""Hermetic proof of `shoal refs --resolve` (archive.resolve) on a scratch repository taken through every state by the
real `shoal archive` commands:

- `live` (tracked), `warm` (its archive path and the restore command), `restored`; a file recreated by hand after
  it was archived reads `live` with a note saying the manifest's last event is not a restore;
- `cold`: the recovery command reads the content back by blob, and by `restore_from:archive_path` once the event has no
  usable blob;
- `gone`: deleted outside the archive protocol, exit 0 with `"protocol": "bypassed"` and a `git show <commit>^:<path>`
  that reads the content back;
- a legacy path (a `migrate` event's `from`) and an archive path that went cold are found through the manifests;
- `--from` reads a relative link as written in that document; a unique partial path resolves as `refs` reads one, but
  never ahead of the path itself deleted outside the protocol;
- exit 1, never a pointer at a missing file: an event the tree contradicts, and a path nothing knows; exit 2 outside a
  repository, and for `--from` without `--resolve`.

Synthetic paths and ids only."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile

T = tempfile.mkdtemp(prefix="shoal-resolve-test-")
SHOALD = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "shoal")
REPO = os.path.join(T, "repo")
D = "proj/design/lane"
A = f"archive/{D}"
HOST = os.path.join(T, "host.toml")
ENV = dict(os.environ, SHOAL_CONFIG=HOST, SHOAL_LEDGER_DIR=os.path.join(T, "ledger"), XDG_CONFIG_HOME=os.path.join(T, "xdg"),
           SHOAL_SERVER="host-a", GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1",
           SHOAL_ROUTE_CACHE=os.path.join(T, "route-cache"), SHOAL_SSH=os.path.join(T, "ssh-stub"))
for k in ("SHOAL_SPEC", "SHOAL_SLUG"):
    ENV.pop(k, None)


def write(rel: str, text: str) -> None:
    p = os.path.join(REPO, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w") as fh:
        fh.write(text)


def git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=REPO, env=ENV, check=True, capture_output=True, text=True).stdout


def shoal(*args: str, cwd: str = REPO) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, os.path.join(SHOALD, "cli.py"), *args], env=ENV, capture_output=True,
                          text=True, cwd=cwd)


def archive(*args: str) -> None:
    r = shoal("archive", *args, "--json")
    assert r.returncode == 0, r.stdout + r.stderr


def resolve(*args: str, cwd: str = REPO) -> tuple[int, dict]:
    r = shoal("refs", "--resolve", *args, "--json", cwd=cwd)
    try:
        return r.returncode, json.loads(r.stdout)
    except ValueError:
        raise AssertionError(r.stdout + r.stderr)


def manifest() -> list[dict]:
    return [json.loads(x) for x in open(os.path.join(REPO, A, "MANIFEST.jsonl"))]


def set_manifest(events: list[dict], message: str) -> None:
    write(f"{A}/MANIFEST.jsonl", "".join(json.dumps(e) + "\n" for e in events))
    git("commit", "-q", "-am", message)


def sh(cmd: str) -> str:
    return subprocess.run(cmd, shell=True, cwd=REPO, env=ENV, check=True, capture_output=True, text=True).stdout


open(HOST, "w").write("")
os.makedirs(os.path.join(REPO, D))
git("init", "-q", "-b", "main")
git("config", "user.email", "t@example.invalid")
git("config", "user.name", "t")
write(f"{D}/lane_start.md", "# lane\n\nLast updated: 2026-11-16T10:00Z\n\nSee [live](live.md).\n")
write(f"{D}/lane_log.md", "C1: wrote warm.md, cold.md, cold2.md, back.md, gone.md and leg.md.\n")
for name in ("live", "warm", "cold", "cold2", "back", "gone"):
    write(f"{D}/{name}.md", f"{name} text\n")
write(f"{D}/old_archive/leg.md", "legacy text\n")
write("proj/other/notes_log.md", "Earlier: [w](../design/lane/warm.md).\n")
git("add", "-A")
git("commit", "-q", "-m", "base")
spec = os.path.join(T, "ledger", "shoals", "g1", "shoal.json")
os.makedirs(os.path.dirname(spec))
json.dump({"shoal": "g1", "dir": REPO, "roles": [{"slug": "lane", "doc": os.path.join(REPO, D, "lane_start.md")}]},
          open(spec, "w"))

archive(*(f"{D}/{x}.md" for x in ("warm", "cold", "cold2", "back")), "--reason", "concluded")
archive("--cold", f"{D}/cold.md", f"{D}/cold2.md", "--force")
archive("--restore", f"{D}/back.md")
git("rm", "-q", f"{D}/gone.md")
git("commit", "-q", "-m", "removed outside the protocol")
# a legacy nested archive folder migrated by hand, with its event (what P3's migration writes)
git("mv", f"{D}/old_archive/leg.md", f"{A}/leg.md")
set_manifest(manifest() + [{"ts": "2026-11-16T10:05:00Z", "by": "t", "event": "migrate", "path": f"{D}/leg.md",
                            "from": f"{D}/old_archive/leg.md", "archive_path": f"{A}/leg.md"}], "migrate leg.md")

rc, r = resolve(f"{D}/live.md")
assert rc == 0 and r["state"] == "live" and r["ok"], r
rc, r = resolve(f"{D}/warm.md")
assert rc == 0 and r["state"] == "warm" and r["archive_path"] == f"{A}/warm.md", r
assert r["restore"] == f"shoal archive --restore {D}/warm.md" and r["event"]["reason"] == "concluded", r
rc, r = resolve(f"{D}/back.md")
assert rc == 0 and r["state"] == "restored" and r["event"]["event"] == "restore", r
out = shoal("refs", "--resolve", f"{D}/warm.md").stdout
assert out.startswith(f"{D}/warm.md: warm\n  at {A}/warm.md (archive ") and "restore: shoal archive --restore" in out, out
print("live, warm and restored ok")

rc, r = resolve(f"{D}/cold.md")
assert rc == 0 and r["state"] == "cold" and r["recover"].startswith("git show ") and ":" not in r["recover"], r
assert sh(r["recover"]) == "cold text\n", "the blob reads the content back"
events = manifest()
for e in events:
    if e["path"] == f"{D}/cold2.md" and e["event"] == "cold":
        e["blob"] = "0" * 40
set_manifest(events, "an unusable blob")
rc, r = resolve(f"{D}/cold2.md")
assert rc == 0 and r["state"] == "cold" and r["recover"].endswith(f":{A}/cold2.md"), r
assert sh(r["recover"]) == "cold2 text\n", "restore_from:archive_path reads the content back"
print("cold ok")

rc, r = resolve(f"{D}/gone.md")
assert rc == 0 and r["state"] == "gone" and r["protocol"] == "bypassed" and r["ok"], r
assert r["recover"] == f"git show {r['deleted_in']}^:{D}/gone.md" and sh(r["recover"]) == "gone text\n", r
out = shoal("refs", "--resolve", f"{D}/gone.md").stdout
assert "deleted outside the archive protocol in" in out, out
print("gone ok")

rc, r = resolve(f"{D}/old_archive/leg.md")
assert rc == 0 and r["via"] == "from" and r["path"] == f"{D}/leg.md" and r["state"] == "warm", r
rc, r = resolve(f"{A}/cold.md")
assert rc == 0 and r["via"] == "archive_path" and r["path"] == f"{D}/cold.md" and r["state"] == "cold", r
print("legacy and archive paths ok")

rc, r = resolve("../design/lane/warm.md", "--from", os.path.join(REPO, "proj/other/notes_log.md"))
assert rc == 0 and r["path"] == f"{D}/warm.md" and r["state"] == "warm", r
rc, r = resolve("../design/lane/warm.md", cwd=os.path.join(REPO, "proj/other"))
assert rc == 0 and r["path"] == f"{D}/warm.md", "relative to the current directory without --from"
rc, r = resolve("lane/warm.md")
assert rc == 0 and r["via"] == "suffix" and r["state"] == "warm", r
write("x/n.md", "root n\n")
write("proj/x/n.md", "nested n\n")
git("add", "-A")
git("commit", "-q", "-m", "two n.md")
git("rm", "-q", "x/n.md")
git("commit", "-q", "-m", "removed outside the protocol")
rc, r = resolve("x/n.md")
assert rc == 0 and r["state"] == "gone" and r["path"] == "x/n.md", "the path itself, deleted, beats a partial-path match"
print("--from, relative and suffix ok")

write(f"{D}/cold.md", "recreated by hand\n")
git("add", f"{D}/cold.md")
git("commit", "-q", "-m", "recreated outside the protocol")
rc, r = resolve(f"{D}/cold.md")
assert rc == 0 and r["state"] == "live" and "tracked again without a restore event" in r["note"] and "cold" in r["note"], r
assert "note: tracked again" in shoal("refs", "--resolve", f"{D}/cold.md").stdout
assert f"(archive path {A}/cold.md)" in r["note"], r["note"]
rc, r = resolve(f"{D}/live.md")
assert "note" not in r, "a file never archived carries no note"
print("tracked again ok")

set_manifest(manifest() + [{"ts": "2026-11-16T10:06:00Z", "by": "t", "event": "archive", "path": f"{D}/phantom.md",
                            "archive_path": f"{A}/phantom.md"}], "an event the tree contradicts")
rc, r = resolve(f"{D}/phantom.md")
assert rc == 1 and r["state"] == "inconsistent" and "not tracked" in r["problem"] and not r["ok"], r
rc, r = resolve(f"{D}/never.md")
assert rc == 1 and r["state"] == "unknown", r
assert shoal("refs", "--resolve", "x.md", cwd=T).returncode == 2, "outside a repository"
assert shoal("refs", D, "--from", "x.md").returncode == 2, "--from without --resolve"
print("refusals ok")

subprocess.run(["rm", "-rf", T], check=True)
print("test_resolve OK")
