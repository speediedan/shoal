#!/usr/bin/env python3
"""Hermetic proof of `prune-incarnations` (shoal/prune.py): every guard keeps its session, an eligible Claude job is
removed only after its tombstone is written (transcript untouched) and the jobs sharing its conversation are stopped
again, Codex/opencode/Muse prunes reach the right fake API or move, a dry run changes nothing, the N-2 rule of
features.prune, and pruned incarnations keep their names in `context-report`.

Every harness is a fake: `claude` and `openchamber` are scripts first on PATH, `codex app-server` is a script named by
SHOAL_CODEX_BIN, OpenChamber's HTTP API is SHOAL_FAKE_OPENCHAMBER_API (a request log), and `claude agents` answers
from SHOAL_FAKE_SESSIONS. Synthetic ids only."""
from __future__ import annotations

import fcntl
import glob
import json
import os
import subprocess
import sys
import tempfile
import time

T = tempfile.mkdtemp(prefix="shoal-prune-test-")
SHOALD = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "shoal")
OLD = "2026-01-01T00:00:00Z"
NOW = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

CC = os.path.join(T, "claude")
BIN = os.path.join(T, "bin")
LEDGER = os.path.join(T, "ledger")
for d in (CC, BIN, LEDGER, os.path.join(T, "xdg", "shoal"), os.path.join(T, "data")):
    os.makedirs(d, exist_ok=True)


def fail(msg: str) -> None:
    print(f"test_prune FAIL: {msg}", file=sys.stderr)
    sys.exit(1)


def check(cond, msg: str) -> None:
    if not cond:
        fail(msg)


# ---- the Claude world: jobs on disk, rows in the fake listing ---------------------------------------------------
def job(jid: str, name: str, conv: str, updated: str = OLD, worktree: str = "", tmpfile: bool = True) -> None:
    d = os.path.join(CC, "jobs", jid)
    os.makedirs(os.path.join(d, "tmp"), exist_ok=True)
    st = {"sessionId": conv, "name": name, "state": "stopped", "updatedAt": updated, "cwd": "/w",
          "respawnFlags": ["-n", name]}
    if worktree:
        st["worktreePath"] = worktree
    json.dump(st, open(os.path.join(d, "state.json"), "w"))
    open(os.path.join(d, "timeline.jsonl"), "w").write('{"t": 1}\n')
    if tmpfile:
        open(os.path.join(d, "tmp", "scratch.bin"), "wb").write(b"x" * 4096)


def conv(jid: str) -> str:
    return f"{jid}-0000-4000-8000-000000000000"


ROWS = []


def row(jid: str, name: str, state: str = "stopped", pid: int = 0, c: str = "") -> None:
    r = {"id": jid, "sessionId": c or conv(jid), "name": name, "state": state, "kind": "background",
         "startedAt": 1}
    if pid:
        r["pid"] = pid
    ROWS.append(r)


# eligible: tl-r1 and a copy of its conversation (a1b2c3d4)
job("a1a1a1a1", "tl-r1", conv("a1a1a1a1")); row("a1a1a1a1", "tl-r1")
job("a1b2c3d4", "tl-r1", conv("a1a1a1a1")); row("a1b2c3d4", "tl-r1", c=conv("a1a1a1a1"))
os.makedirs(os.path.join(CC, "projects", "p"), exist_ok=True)
TRANSCRIPT = os.path.join(CC, "projects", "p", conv("a1a1a1a1") + ".jsonl")
open(TRANSCRIPT, "w").write('{"type": "user"}\n')
# (d) a live worker, and a working state with no pid
job("a2a2a2a2", "tl-r2", conv("a2a2a2a2")); row("a2a2a2a2", "tl-r2", state="working", pid=4242)
job("a2b2b2b2", "tl-r2", conv("a2b2b2b2")); row("a2b2b2b2", "tl-r2", state="blocked")
# (e) a stopped job whose conversation a live job (not a shoal one) holds
job("a3a3a3a3", "tl-r3", conv("a3a3a3a3")); row("a3a3a3a3", "tl-r3")
row("a3cccccc", "some-chat", state="working", pid=4343, c=conv("a3a3a3a3"))
# (f) its worktree exists; a git worktree under its tmp
WT = os.path.join(T, "wt-a4")
os.makedirs(WT)
job("a4a4a4a4", "tl-r4", conv("a4a4a4a4"), worktree=WT); row("a4a4a4a4", "tl-r4")
job("a5a5a5a5", "tl-r13", conv("a5a5a5a5")); row("a5a5a5a5", "tl-r13")
os.makedirs(os.path.join(CC, "jobs", "a5a5a5a5", "tmp", "repo-wt"))
open(os.path.join(CC, "jobs", "a5a5a5a5", "tmp", "repo-wt", ".git"), "w").write("gitdir: /elsewhere\n")
# (a) the current tl session, (b) up's parked session, (c) a registry entry (dismissed)
job("c5c5c5c5", "tl-r5", conv("c5c5c5c5")); row("c5c5c5c5", "tl-r5")
job("b0b0b0b0", "up-r1", conv("b0b0b0b0")); row("b0b0b0b0", "up-r1")
job("d0d0d0d0", "tl-r9", conv("d0d0d0d0")); row("d0d0d0d0", "tl-r9")
# (a) the current session of a role in a RETIRED spec
job("d1d1d1d1", "tl-r8", conv("d1d1d1d1")); row("d1d1d1d1", "tl-r8")
# (g) the session this runs inside
job("e0e0e0e0", "tl-r10", conv("e0e0e0e0")); row("e0e0e0e0", "tl-r10")
# (h) a ledger-watch still bound to it
job("f0f0f0f0", "tl-r11", conv("f0f0f0f0")); row("f0f0f0f0", "tl-r11")
# younger than --min-age
job("0a0a0a0a", "tl-r12", conv("0a0a0a0a"), updated=NOW); row("0a0a0a0a", "tl-r12")
# not shoal's: never considered
job("9c9c9c9c", "random-chat", conv("9c9c9c9c")); row("9c9c9c9c", "random-chat")
# standalone: seatA's current job (registered) and its predecessor of the same name
job("5f5f5f5f", "seatA", conv("5f5f5f5f")); row("5f5f5f5f", "seatA")
job("5e5e5e5e", "seatA", conv("5e5e5e5e")); row("5e5e5e5e", "seatA")
# a stopped `/bg` fork of seatA: the seat's name, ids of its own; the registry records 5f5f5f5f, so it is not the seat
job("5c5c5c5c", "seatA", conv("5c5c5c5c")); row("5c5c5c5c", "seatA")
# a registry entry that records no id at all is matched by its label
job("5b5b5b5b", "seatB", conv("5b5b5b5b")); row("5b5b5b5b", "seatB")
# seatC records a session and an EMPTY job string (an interactive seat never backgrounded): "" is no id, so its
# stopped same-name fork is not the seat
job("5d5d5d5d", "seatC", conv("5d5d5d5d")); row("5d5d5d5d", "seatC")

# ---- specs: one live shoal (pt) and one retired one (old) -------------------------------------------------------
MU_CUR, MU_OLD, MU_LOCK = ("01a00000-0000-7000-8000-00000000000%d" % i for i in (1, 2, 3))
spec = {
    "shoal": "pt-20260101-000000", "dir": T, "incarnation": 6, "features": {},
    "ledger": {"repo": "", "dir": LEDGER},
    "roles": [
        {"slug": "tl", "ecosystem": "claude", "title": "tl", "doc": "/d", "fill": "assigned", "seq": 5,
         "session": "c5c5c5c5"},
        {"slug": "up", "ecosystem": "claude", "title": "up", "doc": "/d", "fill": "assigned", "seq": 1, "session": "",
         "parked": {"at": OLD, "session": "b0b0b0b0"}},
        {"slug": "cx", "ecosystem": "codex", "title": "cx", "doc": "/d", "fill": "assigned", "seq": 3,
         "session": "t-current"},
        {"slug": "oc", "ecosystem": "opencode", "title": "octitle", "doc": "/d", "fill": "assigned", "seq": 3,
         "session": "ses_cur"},
        {"slug": "mu", "ecosystem": "muse", "title": "mu", "doc": "/d", "fill": "assigned", "seq": 3,
         "session": MU_CUR}],
    "generations": [
        {"gen": f"r{i}", "incarnation": i - 1, "started_utc": OLD, "ended_utc": OLD,
         "sessions": s, "seqs": q} for i, (s, q) in enumerate([
            ({"tl": "a1a1a1a1", "cx": "t-old1", "oc": "ses_old", "mu": MU_OLD}, {"tl": 1, "cx": 1, "oc": 1, "mu": 1}),
            ({"tl": "a2a2a2a2", "cx": "t-busy", "oc": "ses_busy", "mu": MU_LOCK}, {"tl": 2, "cx": 2, "oc": 2, "mu": 2}),
            ({"tl": "a3a3a3a3"}, {"tl": 3}),
            ({"tl": "a4a4a4a4"}, {"tl": 4})], start=1)]}
os.makedirs(os.path.join(LEDGER, "shoals", spec["shoal"]))
SPEC = os.path.join(LEDGER, "shoals", spec["shoal"], "shoal.json")
json.dump(spec, open(SPEC, "w"), indent=1)
retired = {"shoal": "older-20250101-000000", "dir": T, "retired_utc": OLD, "ledger": {"repo": "", "dir": LEDGER},
           "roles": [{"slug": "zz", "ecosystem": "claude", "title": "zz", "doc": "/d", "session": "d1d1d1d1"}]}
os.makedirs(os.path.join(LEDGER, "shoals", retired["shoal"]))
json.dump(retired, open(os.path.join(LEDGER, "shoals", retired["shoal"], "shoal.json"), "w"))
json.dump({"v": 1, "sessions": [
    {"harness": "claude", "session": conv("d0d0d0d0"), "job": "d0d0d0d0", "label": "old-seat", "dismissed": True},
    {"harness": "claude", "session": conv("5f5f5f5f"), "job": "5f5f5f5f", "label": "seatA"},
    {"harness": "claude", "label": "seatB"},
    {"harness": "claude", "session": conv("5a5a5a5a"), "job": "", "label": "seatC"}]},
    open(os.path.join(T, "xdg", "shoal", "ui-standalone.json"), "w"))

# ---- fakes ---------------------------------------------------------------------------------------------------------
CLOG = os.path.join(T, "claude.log")
open(os.path.join(BIN, "claude"), "w").write(f"""#!/bin/sh
echo "$*" >> "{CLOG}"
if [ "$1" = rm ]; then
  if [ -f "{CC}/archived_sessions/jobs/$2/ARCHIVE_INFO.txt" ] && [ -d "{CC}/jobs/$2" ]; then
    echo "tombstone-before-rm $2" >> "{CLOG}"
  fi
  rm -rf "{CC}/jobs/$2"
fi
exit 0
""")
# OpenChamber's HTTP API (fake): the session list (one archived session it must skip) and the active map
OC = {"/api/session": {"data": [
    {"id": "ses_cur", "title": "octitle-r3", "time": {"updated": 1}},
    {"id": "ses_old", "title": "octitle-r1", "time": {"updated": 1}, "location": {"directory": "/w"}},
    {"id": "ses_busy", "title": "octitle-r2", "time": {"updated": 1}},
    {"id": "ses_m1", "title": "shoal-matcher-1700000000000", "time": {"updated": 1}},
    {"id": "ses_m0", "title": "shoal-matcher-1600000000000", "time": {"updated": 1, "archived": 2}},
    {"id": "ses_user", "title": "a chat", "time": {"updated": 1}}], "cursor": {"next": None}},
    "/api/session/active": {"data": {"ses_busy": {}}}}
open(os.path.join(T, "ocapi.log.get.json"), "w").write(json.dumps(OC))
open(os.path.join(BIN, "openchamber"), "w").write(f"""#!/bin/sh
echo "unexpected openchamber $*" >> "{T}/openchamber.log"; exit 2
""")
XLOG = os.path.join(T, "codex.log")
open(os.path.join(BIN, "codex-fake"), "w").write(f"""#!/usr/bin/env python3
import json, sys
threads = [{{"id": "t-current", "updatedAt": 1, "status": {{"type": "notLoaded"}}}},
           {{"id": "t-old1", "updatedAt": 1, "status": {{"type": "notLoaded"}}}},
           {{"id": "t-busy", "updatedAt": 1, "status": {{"type": "active", "activeFlags": []}}}},
           {{"id": "t-user", "updatedAt": 1, "status": {{"type": "notLoaded"}}}}]
for line in sys.stdin:
    m = json.loads(line)
    if "id" not in m:
        continue
    meth = m.get("method")
    if meth not in ("initialize", "thread/list"):
        open("{XLOG}", "a").write(json.dumps([meth, m.get("params")]) + "\\n")
    res = {{"data": threads, "nextCursor": None}} if meth == "thread/list" else {{}}
    print(json.dumps({{"id": m["id"], "result": res}}), flush=True)
""")
for f in ("claude", "openchamber", "codex-fake"):
    os.chmod(os.path.join(BIN, f), 0o755)
# Muse store: three sessions; MU_LOCK's .session.lock is held while the test runs
MROOT = os.path.join(T, "muse", "sessions")
for s in (MU_CUR, MU_OLD, MU_LOCK):
    d = os.path.join(MROOT, "2026", "10", "01", s)
    os.makedirs(d)
    open(os.path.join(d, "session.jsonl"), "w").write("{}\n")
    open(os.path.join(d, ".session.lock"), "w").close()
    os.utime(os.path.join(d, "session.jsonl"), (1, 1))
MSTATE = os.path.join(T, "muse-state")
os.makedirs(os.path.join(MSTATE, MU_OLD))
lockfh = open(os.path.join(MROOT, "2026", "10", "01", MU_LOCK, ".session.lock"))
fcntl.flock(lockfh, fcntl.LOCK_EX)
# a ledger-watch bound to f0f0f0f0 (a process whose argv reads like one)
os.makedirs(os.path.join(T, "w"))
open(os.path.join(T, "w", "cli.py"), "w").write("import time\ntime.sleep(120)\n")
watch = subprocess.Popen([sys.executable, os.path.join(T, "w", "cli.py"), "ledger-watch", "--shoal", spec["shoal"],
                          "--slug", "tl", "--wake", "direct", "--session", conv("f0f0f0f0")])

os.environ.update({
    "PATH": BIN + ":" + os.environ["PATH"], "CLAUDE_CONFIG_DIR": CC, "XDG_CONFIG_HOME": os.path.join(T, "xdg"),
    "SHOAL_LEDGER_DIR": LEDGER, "SHOAL_DATA_ROOT": os.path.join(T, "data"),
    "SHOAL_FAKE_SESSIONS": json.dumps({"claude": ROWS, "opencode": {}}),
    "SHOAL_CODEX_BIN": os.path.join(BIN, "codex-fake"), "SHOAL_CODEX_STATE": os.path.join(T, "codex-state"),
    "SHOAL_MUSE_DATA": MROOT, "SHOAL_MUSE_STATE": MSTATE, "SHOAL_FAKE_OPENCHAMBER_API": os.path.join(T, "ocapi.log"),
    "SHOAL_PRUNE_VERIFY_S": "0", "CLAUDE_CODE_SESSION_ID": conv("e0e0e0e0"), "OPENCHAMBER_DATA_DIR": os.path.join(T, "noc")})
os.environ.pop("SHOAL_SPEC", None)
os.environ.pop("CLAUDE_PID", None)
sys.path.insert(0, SHOALD)
import prune  # noqa: E402


def snapshot() -> list[str]:
    return sorted(glob.glob(os.path.join(T, "**"), recursive=True))


def verdicts(res) -> dict:
    return {(c.harness, c.job or c.session): (c.verdict, c.reason) for v in res.values() for c in v["cands"]}


try:
    time.sleep(0.3)  # the watcher's argv is readable
    quiet = []
    # ---- dry run: the table, every guard, nothing changed -------------------------------------------------------
    before = snapshot()
    res = prune.run(all_specs=True, standalone=True, matcher=True, dry_run=True, log=quiet.append)
    check(snapshot() == before, "a dry run changed files")
    check(not os.path.exists(CLOG) and not os.path.exists(XLOG) and not os.path.exists(os.path.join(T, "ocapi.log"))
          and not os.path.exists(os.path.join(T, "openchamber.log")), "a dry run reached a harness")
    v = verdicts(res)
    want = {
        ("claude", "a1a1a1a1"): ("prune", ""), ("claude", "a1b2c3d4"): ("prune", ""),
        ("claude", "a2a2a2a2"): ("keep", "(d) live worker"), ("claude", "a2b2b2b2"): ("keep", "(d) state blocked"),
        ("claude", "a3a3a3a3"): ("keep", "(e) conversation shared with a3cccccc"),
        ("claude", "a3cccccc"): ("keep", "(d) live worker"),  # a copy holding a role's conversation is shoal's
        ("claude", "a4a4a4a4"): ("keep", "(f) its worktree exists"),
        ("claude", "a5a5a5a5"): ("keep", "(f) git worktree under its tmp"),
        ("claude", "c5c5c5c5"): ("keep", "(a-c) current session of tl@"),
        ("claude", "b0b0b0b0"): ("keep", "(a-c) parked session of up@"),
        ("claude", "d0d0d0d0"): ("keep", "(a-c) standalone seat old-seat (dismissed)"),
        ("claude", "d1d1d1d1"): ("keep", "(a-c) current session of zz@older"),
        ("claude", "e0e0e0e0"): ("keep", "(g) the job this runs inside"),
        ("claude", "f0f0f0f0"): ("keep", "(h) ledger-watch pid"),
        ("claude", "0a0a0a0a"): ("keep", "younger than --min-age"),
        ("claude", "5f5f5f5f"): ("keep", "(a-c) standalone seat seatA"),
        ("claude", "5e5e5e5e"): ("prune", ""), ("claude", "5c5c5c5c"): ("prune", ""),
        ("claude", "5d5d5d5d"): ("prune", ""),
        ("claude", "5b5b5b5b"): ("keep", "(a-c) standalone seat seatB (no recorded id: matched by label)"),
        ("codex", "t-current"): ("keep", "(a-c) current session of cx@"), ("codex", "t-old1"): ("prune", ""),
        ("codex", "t-busy"): ("keep", "(d) thread active"),
        ("opencode", "ses_cur"): ("keep", "(a-c) current session of oc@"), ("opencode", "ses_old"): ("prune", ""),
        ("opencode", "ses_busy"): ("keep", "(d) session active"), ("opencode", "ses_m1"): ("prune", ""),
        ("muse", MU_CUR): ("keep", "(a-c) current session of mu@"), ("muse", MU_OLD): ("prune", ""),
        ("muse", MU_LOCK): ("keep", "(h) lock held")}
    for k, (verdict, reason) in want.items():
        got = v.get(k)
        check(got is not None, f"{k} not considered :: {sorted(v)}")
        check(got[0] == verdict and reason in got[1], f"{k}: want {verdict} '{reason}', got {got}")
    for k in v:
        check(k in want, f"{k} considered but should not be (not shoal's) :: {v[k]}")
    check(any("== claude:" in ln and "prune 5" in ln for ln in quiet), f"claude table header :: {quiet[:3]}")
    # without --standalone / --matcher those are not considered
    v2 = verdicts(prune.run(all_specs=True, dry_run=True, log=quiet.append))
    check(("claude", "5e5e5e5e") not in v2 and ("opencode", "ses_m1") not in v2, "standalone/matcher without the flag")

    # ---- the N-2 rule (features.prune) ---------------------------------------------------------------------------
    from roles import ShoalSpec
    sp = ShoalSpec.load(SPEC)
    tl = sp.role("tl")
    check(prune.auto(sp, tl, "restart", dry_run=True) == "", "auto prune ran with features.prune off")
    sp.features["prune"] = True
    out = prune.auto(sp, tl, "first-turn", dry_run=True)
    check("--max-seq 3" in out and "--role tl" in out and "--harness claude" in out, f"auto first-turn :: {out}")
    res = prune.run(specs=[SPEC], dry_run=True, only_slug="tl", max_seq=tl.seq - 2, min_age=0, log=quiet.append)
    seqs = {c.seq for v_ in res.values() for c in v_["cands"]}
    check(seqs and max(seqs) <= 3 and 1 in seqs, f"N-2 rule considered seqs {seqs}")
    check(prune.auto(sp, sp.role("up"), "restart", dry_run=True) == "", "auto prune for a parked role")
    out = prune.auto(sp, tl, "restart", detach=False, dry_run=True, log=quiet.append)
    check(out.startswith("would prune 2 tl incarnation(s) <= r3"), f"in-process auto :: {out}")

    # ---- apply: Claude purge with tombstone, re-stop, transcript untouched ---------------------------------------
    res = prune.run(all_specs=True, harnesses=("claude",), standalone=True, log=quiet.append)
    log = open(CLOG).read().splitlines()
    check("rm a1a1a1a1" in log and "rm a1b2c3d4" in log and "rm 5e5e5e5e" in log and "rm 5c5c5c5c" in log and "rm 5d5d5d5d" in log, f"claude rm calls :: {log}")
    check(sum(1 for ln in log if ln.startswith("rm ")) == 5, f"only the eligible jobs are removed :: {log}")
    check("tombstone-before-rm a1a1a1a1" in log and "tombstone-before-rm 5e5e5e5e" in log, f"tombstone order :: {log}")
    i = log.index("rm a1a1a1a1")
    check("stop a1b2c3d4" in log[i:], f"no re-stop of the job sharing the conversation :: {log}")
    stone = os.path.join(CC, "archived_sessions", "jobs", "a1a1a1a1")
    check(sorted(os.listdir(stone)) == ["ARCHIVE_INFO.txt", "state.json", "timeline.jsonl"], os.listdir(stone))
    info = open(os.path.join(stone, "ARCHIVE_INFO.txt")).read()
    check("name: tl-r1" in info and f"transcript: {TRANSCRIPT}" in info and "claude --resume" in info, info)
    check(os.path.isfile(TRANSCRIPT), "the transcript was removed")
    check(not os.path.exists(os.path.join(CC, "jobs", "a1a1a1a1")), "job dir still there")
    for keep in ("a2a2a2a2", "a3a3a3a3", "a4a4a4a4", "a5a5a5a5", "c5c5c5c5", "b0b0b0b0", "d0d0d0d0", "e0e0e0e0",
                 "f0f0f0f0", "0a0a0a0a", "9c9c9c9c", "5f5f5f5f"):
        check(os.path.isdir(os.path.join(CC, "jobs", keep)), f"kept job {keep} removed")
    ev = [json.loads(ln) for f in glob.glob(os.path.join(T, "data", spec["shoal"], "telemetry", "events-*.jsonl"))
          for ln in open(f)]
    pr = [e for e in ev if e.get("type") == "pruned"]
    check(len(pr) == 2 and {e["job"] for e in pr} == {"a1a1a1a1", "a1b2c3d4"} and pr[0]["mode"] == "purge"
          and pr[0]["harness"] == "claude", f"pruned telemetry :: {pr}")
    hev = [json.loads(ln) for f in glob.glob(os.path.join(T, "data", "_host", "telemetry", "events-*.jsonl"))
           for ln in open(f)]
    check(any(e.get("job") == "5e5e5e5e" and e.get("kind") == "standalone" for e in hev), f"host telemetry :: {hev}")

    # ---- apply: Codex archive, opencode archive (OpenChamber), Muse move -------------------------------------------
    os.makedirs(os.path.join(T, "codex-state", "t-old1"))
    prune.run(all_specs=True, harnesses=("codex", "opencode", "muse"), matcher=True, log=quiet.append)
    xl = [json.loads(ln) for ln in open(XLOG)]
    check(xl == [["thread/archive", {"threadId": "t-old1"}]], f"codex calls :: {xl}")
    check(not os.path.exists(os.path.join(T, "codex-state", "t-old1")), "codex shoal state kept")
    api = [json.loads(ln) for ln in open(os.path.join(T, "ocapi.log"))]
    check([(a["method"], a["path"], a["body"]) for a in api] ==
          [("POST", "/api/openchamber/sessions/archive", {"ids": ["ses_old"]}),
           ("POST", "/api/openchamber/sessions/archive", {"ids": ["ses_m1"]})], f"opencode calls :: {api}")
    moved = os.path.join(T, "muse", "archived-sessions", "2026", "10", "01", MU_OLD)
    check(os.path.isfile(os.path.join(moved, "session.jsonl")), "muse session not moved")
    check(not os.path.exists(os.path.join(MROOT, "2026", "10", "01", MU_OLD)), "muse session left in the store")
    check(os.path.isdir(os.path.join(MROOT, "2026", "10", "01", MU_LOCK)), "a locked muse session was moved")
    check(not os.path.exists(os.path.join(MSTATE, MU_OLD)), "muse shoal state kept")
    # --delete: codex thread/delete
    open(XLOG, "w").close()
    xl_threads = prune.run(all_specs=True, harnesses=("codex",), delete=True, log=quiet.append)
    check([json.loads(ln) for ln in open(XLOG)] == [["thread/delete", {"threadId": "t-old1"}]], "codex --delete")
    check(xl_threads["codex"]["cands"], "codex candidates")

    # ---- context-report names: pruned incarnations keep theirs -------------------------------------------------------
    import context_cost
    nm = context_cost.names()
    check(nm.get(conv("a1a1a1a1"), {}).get("name") == "tl-r1", f"tombstone name :: {nm.get(conv('a1a1a1a1'))}")
    check(nm.get("a4a4a4a4", {}).get("name", "").startswith("tl-r4") or
          nm.get(conv("a4a4a4a4"), {}).get("name", "").startswith("tl-r4"), "history name")
    print("prune ok")
finally:
    watch.kill()
    lockfh.close()
