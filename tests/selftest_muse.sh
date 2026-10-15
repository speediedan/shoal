#!/usr/bin/env bash
# Muse selftest (shoal/muse.py, MuseBackend): the exec-per-wake harness. Everything runs against a STUB
# `muse` (a python script that records its argv and environment and prints the records `muse exec --json`
# prints), so no network, no account and no install is needed. Set SHOAL_TEST_REAL_MUSE=1 to also run one
# turn against the installed binary's built-in `echo` provider (no network either).
#
# Covered: a turn's argv (posture, session id, workspace, model, effort), the environment it gets
# (auto-update off, no login prompt, META_API_KEY stripped unless allowed), the run lock (busy from the
# instant a turn starts, a second send refused, free again after), the result record, quota detection,
# the status line and fake-session override, the viewer argv (resume when idle, read-only tail when busy),
# the read-only tail, and the runner's busy exit code.
set -uo pipefail
TESTD="$(cd "$(dirname "$0")" && pwd)"
SHOALD="$(cd "$TESTD/../shoal" && pwd)"
. "$TESTD/selftest_lib.sh"
S="${1:-${TMPDIR:-/tmp}/shoal-muse-selftest-$$}"
exec 9>"/tmp/shoal-selftest-muse.lock"; flock -w 600 9 || { echo "SELFTEST FAIL: another muse selftest holds the lock" >&2; exit 1; }
rm -rf "$S"; mkdir -p "$S"
shoal_test_setup "$S"
export SHOAL_MUSE_STATE="$S/muse-state" FAKE_MUSE_LOG="$S/muse-calls.jsonl"
H="python3 $SHOALD/cli.py"; export SHOALD PYTHONPATH="$SHOALD"
fail() { echo "SELFTEST FAIL: $*" >&2; exit 1; }

cat > "$S/muse" <<'PY'
#!/usr/bin/env python3
import json, os, sys, time
a = sys.argv[1:]
open(os.environ["FAKE_MUSE_LOG"], "a").write(json.dumps({
    "argv": a, "key": os.environ.get("META_API_KEY"), "login": os.environ.get("MUSE_LOGIN"),
    "upd": os.environ.get("MUSE_NO_AUTO_UPDATE"), "cwd": os.getcwd()}) + "\n")
if a[:1] != ["exec"]:
    sys.exit(2)
prompt = open(a[a.index("--prompt-file") + 1]).read()
sid = a[a.index("--session-id") + 1]
time.sleep(float(os.environ.get("FAKE_MUSE_SLEEP", "0")))
if os.environ.get("FAKE_MUSE_FAIL"):
    sys.stderr.write(os.environ["FAKE_MUSE_FAIL"] + "\n")
    sys.exit(1)
def rec(kind, **kw):
    print(json.dumps({"schema_version": 1, "stream": {"kind": "session", "id": sid},
                      "payload": dict(kind=kind, **kw)}), flush=True)
rec("run_started")
rec("run_terminal", terminal="completed", reason=None, text="stub: " + prompt[:40])
PY
chmod +x "$S/muse"
export SHOAL_MUSE_BIN="$S/muse"
mkdir -p "$S/ws"

echo "== 1 a turn: argv, environment, result record, the API key stripped"
python3 - "$S" <<'PY' || fail "turn lifecycle"
import json, os, sys, time
sys.path.insert(0, os.environ["SHOALD"])
S = sys.argv[1]
import muse
from backends import get
be = get("muse")
os.environ["META_API_KEY"] = "SECRET-KEY"
sid = be.create("t", S + "/ws", brief="the first brief", model="muse-spark-1.2", effort="high")
assert muse.UUID_RE.match(sid), sid
for _ in range(100):
    if muse.last_run(sid).get("ended"):
        break
    time.sleep(0.1)
last = muse.last_run(sid)
assert last["terminal"] == "completed" and last["rc"] == 0 and last["text"] == "stub: the first brief", last
call = json.loads(open(os.environ["FAKE_MUSE_LOG"]).readlines()[-1])
a = call["argv"]
assert a[0] == "exec" and "--json" in a and "--yolo" in a, a
assert a[a.index("--session-id") + 1] == sid and a[a.index("--workspace") + 1] == S + "/ws", a
assert a[a.index("--model") + 1] == "muse-spark-1.2" and a[a.index("--reasoning-effort") + 1] == "high", a
assert call["upd"] == "1" and call["login"] == "0", call
assert call["key"] is None, "META_API_KEY reached a turn: it would bill per token instead of the subscription"
os.environ["SHOAL_MUSE_ALLOW_API_KEY"] = "1"
be.send(sid, S + "/ws", "second")
for _ in range(100):
    if muse.last_run(sid).get("text") == "stub: second":
        break
    time.sleep(0.1)
assert json.loads(open(os.environ["FAKE_MUSE_LOG"]).readlines()[-1])["key"] == "SECRET-KEY", "override ignored"
del os.environ["SHOAL_MUSE_ALLOW_API_KEY"], os.environ["META_API_KEY"]
msgs = be.messages(sid, S, limit=5)
assert "stub: the first brief" in msgs and "stub: second" in msgs, msgs
open(S + "/sid1", "w").write(sid)
print("turn lifecycle ok")
PY

echo "== 2 the run lock: busy at once, a second send refused, free after, status line"
FAKE_MUSE_SLEEP=2 python3 - "$S" <<'PY' || fail "run lock"
import os, sys, time
sys.path.insert(0, os.environ["SHOALD"])
S = sys.argv[1]
import muse
from backends import get
be = get("muse")
sid = muse.uuid7()
assert not muse.busy(sid) and be.status(sid, S) == f"{sid[:8]} status:idle never-run-here"
be.send(sid, S + "/ws", "slow")
assert muse.busy(sid), "not busy right after start_run returned"
assert be.status(sid, S) == f"{sid[:8]} status:busy"
try:
    be.send(sid, S + "/ws", "again")
    raise SystemExit("second send during a turn was accepted")
except muse.BusyError:
    pass
for _ in range(100):
    if not muse.busy(sid):
        break
    time.sleep(0.1)
assert not muse.busy(sid) and "status:idle last:completed rc:0" in be.status(sid, S), be.status(sid, S)
be.send(sid, S + "/ws", "after")  # accepted again
for _ in range(100):
    if muse.last_run(sid).get("text") == "stub: after" and not muse.busy(sid):
        break
    time.sleep(0.1)
os.environ["SHOAL_FAKE_SESSIONS"] = '{"muse": {"%s": "x status:busy"}}' % sid
assert be.status(sid, S) == "x status:busy", "SHOAL_FAKE_SESSIONS must override"
print("run lock ok")
PY

echo "== 3 failures: a quota-shaped error reads as a pause with its reset, another does not"
python3 - "$S" <<'PY' || fail "failure and quota"
import os, sys, time
sys.path.insert(0, os.environ["SHOALD"])
S = sys.argv[1]
import muse
from backends import get
be = get("muse")
for text, quota in (("429 rate limit exceeded; resets 9:30pm", True), ("boom: tool crashed", False)):
    os.environ["FAKE_MUSE_FAIL"] = text
    sid = muse.uuid7()
    be.send(sid, S + "/ws", "x")
    for _ in range(100):
        if muse.last_run(sid).get("ended"):
            break
        time.sleep(0.1)
    last = muse.last_run(sid)
    assert last["rc"] == 1 and last["terminal"] == "failed" and text in last["error"], last
    hint = be.quota_paused(sid)
    assert bool(hint) == quota, (text, hint)
    if quota:
        assert "9:30pm" in hint, hint
del os.environ["FAKE_MUSE_FAIL"]
print("failure and quota ok")
PY

echo "== 4 the viewer: resume when idle, a read-only tail while a turn runs, the tail prints results"
SID1=$(cat "$S/sid1")
python3 - "$S" "$SID1" <<'PY' || fail "viewer argv"
import os, sys
sys.path.insert(0, os.environ["SHOALD"])
import muse
from backends import get
sid = sys.argv[2]
argv, env = get("muse").view_argv(sid)
assert argv[-2:] == ["resume", sid], argv
assert env.get("MUSE_NO_AUTO_UPDATE") == "1"
print("viewer argv ok")
PY
OUT=$($H muse-tail --session "$SID1" --once 2>&1); grep -q "stub: the first brief" <<<"$OUT" || fail "muse-tail shows no result :: $OUT"
FAKE_MUSE_SLEEP=2 python3 - "$S" <<'PY' || fail "viewer while busy"
import os, sys
sys.path.insert(0, os.environ["SHOALD"])
import muse
from backends import get
be = get("muse")
sid = muse.uuid7()
be.send(sid, sys.argv[1] + "/ws", "x")
argv, _ = be.view_argv(sid)
assert "muse-tail" in " ".join(argv) and "resume" not in argv, f"a running session must not be taken over: {argv}"
import time
while muse.busy(sid):
    time.sleep(0.1)
print("viewer while busy ok")
PY

echo "== 5 the runner by hand: a held lock exits 75 (busy), a free one runs the turn"
python3 - "$S" <<'PY' || fail "runner busy exit"
import fcntl, os, subprocess, sys
sys.path.insert(0, os.environ["SHOALD"])
import muse
S = sys.argv[1]
sid = muse.uuid7()
d = muse.sdir(sid, create=True)
pf = os.path.join(d, "p.txt"); open(pf, "w").write("by hand")
held = open(os.path.join(d, "lock"), "a+"); fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
cli = os.path.join(os.environ["SHOALD"], "cli.py")
r = subprocess.run([sys.executable, cli, "muse-run", "--session", sid, "--dir", S + "/ws", "--prompt-file", pf],
                   capture_output=True, text=True, env=dict(os.environ))
assert r.returncode == 75 and "busy" in r.stdout, (r.returncode, r.stdout, r.stderr)
held.close()
r = subprocess.run([sys.executable, cli, "muse-run", "--session", sid, "--dir", S + "/ws", "--prompt-file", pf],
                   capture_output=True, text=True, env=dict(os.environ))
assert r.returncode == 0 and muse.last_run(sid)["text"] == "stub: by hand", (r.returncode, r.stderr)
print("runner busy exit ok")
PY

if [ "${SHOAL_TEST_REAL_MUSE:-}" = 1 ]; then
  echo "== 6 (opt-in) one turn through the installed muse with the echo provider, then a second on the same session"
  REAL=$(command -v muse || true); [ -x "${REAL:-/nonexistent}" ] || fail "SHOAL_TEST_REAL_MUSE=1 but no muse on PATH"
  RH="$S/real-home"; mkdir -p "$RH"
  HOME="$RH" XDG_CONFIG_HOME="$RH/.config" XDG_DATA_HOME="$RH/.local/share" SHOAL_MUSE_BIN="$REAL" \
    SHOAL_MUSE_ARGS="--provider echo" python3 - "$S" <<'PY' || fail "real muse echo turn"
import os, sys, time
sys.path.insert(0, os.environ["SHOALD"])
import muse
from backends import get
S = sys.argv[1]
be = get("muse")
sid = be.create("real", S + "/ws", brief="hello real muse")
def wait():
    for _ in range(300):
        if muse.last_run(sid).get("ended") and not muse.busy(sid):
            return muse.last_run(sid)
        time.sleep(0.1)
    raise SystemExit("turn did not finish")
last = wait()
assert last["terminal"] == "completed" and last["text"] == "echo: hello real muse", last
be.send(sid, S + "/ws", "and again")
time.sleep(0.5)
last = wait()
assert last["text"] == "echo: and again", last
assert be.messages(sid, S).count("echo:") == 2
print("real muse echo ok")
PY
fi

echo "SELFTEST MUSE OK ($S)"
