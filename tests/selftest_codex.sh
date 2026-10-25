#!/usr/bin/env bash
# Codex selftest (shoal/codex_harness.py, CodexBackend): the exec-per-wake harness. Everything runs against a STUB
# `codex` (a python script that records its argv, environment and stdin and prints the JSONL events `codex exec
# --json` prints), so no network, no account and no install is needed.
#
# Covered: a thread's first turn (the thread id read from its first event, posture and model flags, the prompt on
# stdin, the environment: no API keys, no Claude session identity), the lock hard-linked into the thread's own
# state directory (busy from the first moment), a resume turn, the run lock (a second send refused, free after),
# the result record, quota detection, the status line, an attached interactive client holding a thread, the viewer
# argv, and the runner's busy exit code.
set -uo pipefail
TESTD="$(cd "$(dirname "$0")" && pwd)"
SHOALD="$(cd "$TESTD/../shoal" && pwd)"
. "$TESTD/selftest_lib.sh"
S="${1:-${TMPDIR:-/tmp}/shoal-codex-selftest-$$}"
exec 9>"/tmp/shoal-selftest-codex.lock"; flock -w 600 9 || { echo "SELFTEST FAIL: another codex selftest holds the lock" >&2; exit 1; }
rm -rf "$S"; mkdir -p "$S"
shoal_test_setup "$S"
export SHOAL_CODEX_STATE="$S/codex-state" FAKE_CODEX_LOG="$S/codex-calls.jsonl"
H="python3 $SHOALD/cli.py"; export SHOALD PYTHONPATH="$SHOALD"
fail() { echo "SELFTEST FAIL: $*" >&2; exit 1; }

cat > "$S/codex" <<'PY'
#!/usr/bin/env python3
import json, os, sys, time, uuid
a = sys.argv[1:]
prompt = sys.stdin.read() if a and a[-1] == "-" else ""
open(os.environ["FAKE_CODEX_LOG"], "a").write(json.dumps({
    "argv": a, "stdin": prompt, "openai": os.environ.get("OPENAI_API_KEY"), "codex_key": os.environ.get("CODEX_API_KEY"),
    "claude": [k for k in os.environ if k.startswith("CLAUDE_CODE_") or k == "CLAUDECODE"], "cwd": os.getcwd()}) + "\n")
if a[:1] == ["app-server"]:
    # a minimal app-server: initialize, thread/name/set (logged)
    for line in sys.stdin:
        m = json.loads(line)
        if "id" in m:
            if m["method"] == "thread/name/set":
                open(os.environ["FAKE_CODEX_LOG"], "a").write(json.dumps({"name_set": m["params"]}) + "\n")
            print(json.dumps({"id": m["id"], "result": {}}), flush=True)
    sys.exit(0)
if a[:1] != ["exec"]:
    sys.exit(2)
resume = a[1:2] == ["resume"]
tid = a[2] if resume else os.environ.get("FAKE_CODEX_TID") or str(uuid.UUID(int=(uuid.uuid4().int & ~(0xF << 76)) | (7 << 76)))
def ev(o): print(json.dumps(o), flush=True)
ev({"type": "thread.started", "thread_id": tid})
time.sleep(float(os.environ.get("FAKE_CODEX_SLEEP", "0")))
ev({"type": "turn.started"})
if os.environ.get("FAKE_CODEX_FAIL"):
    ev({"type": "turn.failed", "error": {"message": os.environ["FAKE_CODEX_FAIL"]}})
    sys.stderr.write(os.environ["FAKE_CODEX_FAIL"] + "\n")
    sys.exit(1)
ev({"type": "item.completed", "item": {"id": "item_0", "type": "agent_message", "text": "stub: " + prompt[:40]}})
ev({"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}})
PY
chmod +x "$S/codex"
export SHOAL_CODEX_BIN="$S/codex"
mkdir -p "$S/ws"

echo "== 1 a first turn: the thread id from its first event, posture, flags, stdin, environment"
OPENAI_API_KEY=SECRET CODEX_API_KEY=SECRET2 CLAUDE_CODE_SESSION_ID=abc12345 CLAUDECODE=1 FAKE_CODEX_SLEEP=1 \
python3 - "$S" <<'PY' || fail "first turn"
import json, os, sys, time
sys.path.insert(0, os.environ["SHOALD"])
S = sys.argv[1]
import codex_harness as cx
from backends import get
be = get("codex")
tid = be.create("t", S + "/ws", brief="the first brief", model="gpt-test", effort="high")
assert cx.UUID_RE.match(tid), tid
open(S + "/tid1", "w").write(tid)
assert cx.busy(tid), "the thread must read busy while its first turn still runs"
for _ in range(100):
    if not cx.busy(tid):
        break
    time.sleep(0.1)
assert not cx.busy(tid), "still busy"
last = cx.last_run(tid)
assert last.get("terminal") == "completed" and last.get("rc") == 0, last
assert "stub: the first brief" in last["text"], last
call = [json.loads(l) for l in open(S + "/codex-calls.jsonl")][0]
a = call["argv"]
assert a[:2] == ["exec", "--dangerously-bypass-approvals-and-sandbox"] or "--dangerously-bypass-approvals-and-sandbox" in a, a
assert "--json" in a and a[a.index("-m") + 1] == "gpt-test", a
assert 'model_reasoning_effort="high"' in a, a
assert a[a.index("-C") + 1] == S + "/ws", a
assert a[-1] == "-" and call["stdin"] == "the first brief", call
assert call["openai"] is None and call["codex_key"] is None, "an API key leaked into the turn"
assert call["claude"] == [], "Claude session identity leaked into the turn"
print("first turn ok")
PY

echo "== 2 a resume turn, the run lock, a second send refused"
FAKE_CODEX_SLEEP=2 python3 - "$S" "$(cat "$S/tid1")" <<'PY' || fail "resume and lock"
import json, os, sys, time
sys.path.insert(0, os.environ["SHOALD"])
import codex_harness as cx
from backends import get
be = get("codex")
S, tid = sys.argv[1], sys.argv[2]
be.send(tid, S + "/ws", "second prompt", model="gpt-test")
assert cx.busy(tid), "busy from the instant send returns"
try:
    be.send(tid, S + "/ws", "third")
    raise SystemExit("a second send must be refused while a turn runs")
except cx.BusyError:
    pass
assert "status:busy" in be.status(tid, S + "/ws"), be.status(tid, S + "/ws")
for _ in range(100):
    if not cx.busy(tid):
        break
    time.sleep(0.1)
assert not cx.busy(tid)
calls = [json.loads(l) for l in open(S + "/codex-calls.jsonl")]
a = calls[-1]["argv"]
assert a[:3] == ["exec", "resume", tid] and a[-1] == "-", a
assert calls[-1]["stdin"] == "second prompt"
assert "status:idle" in be.status(tid, S + "/ws") and "last:completed" in be.status(tid, S + "/ws")
assert "stub: second prompt" in be.messages(tid, S, 5), be.messages(tid, S, 5)
print("resume and lock ok")
PY

echo "== 3 failures: a quota-shaped error reads as a pause with its reset, another does not"
python3 - "$S" "$(cat "$S/tid1")" <<'PY' || fail "failure and quota"
import os, sys, time
sys.path.insert(0, os.environ["SHOALD"])
import codex_harness as cx
from backends import get
be = get("codex")
S, tid = sys.argv[1], sys.argv[2]
def run(msg):
    os.environ["FAKE_CODEX_FAIL"] = msg
    be.send(tid, S + "/ws", "x")
    for _ in range(100):
        if not cx.busy(tid):
            break
        time.sleep(0.1)
    os.environ.pop("FAKE_CODEX_FAIL")
run("You've hit your usage limit. Try again in 3 hours 20 minutes.")
assert cx.last_run(tid)["terminal"] == "failed"
assert "3 hours" in be.quota_paused(tid), be.quota_paused(tid)
run("model not found")
assert be.quota_paused(tid) == "", be.quota_paused(tid)
print("failure and quota ok")
PY

echo "== 4 an attached interactive client holds the thread: busy, no turn starts, the runner exits 75"
cat > "$S/codex-client" <<'PY'
#!/usr/bin/env python3
import time
time.sleep(60)
PY
mkdir -p "$S/bin"; cp "$S/codex-client" "$S/bin/codex"; chmod +x "$S/bin/codex"
SIDA=01a00000-0000-7000-8000-00000000aaaa
"$S/bin/codex" resume "$SIDA" & CLI_PID=$!
sleep 0.5
python3 - "$S" "$SIDA" <<'PY' || { kill $CLI_PID 2>/dev/null; fail "attached client"; }
import os, sys
sys.path.insert(0, os.environ["SHOALD"])
import codex_harness as cx
sid = sys.argv[2]
assert cx.attached_client(sid), "client not seen"
assert cx.busy(sid) and "attached" in cx.status_line(sid), cx.status_line(sid)
try:
    cx.start_run(sid, sys.argv[1] + "/ws", "x")
    raise SystemExit("a turn started under an attached client")
except cx.BusyError:
    pass
assert not cx.attached_client("01a00000-0000-7000-8000-00000000bbbb")
print("attached client ok")
PY
$H codex-run --key "$SIDA" --session "$SIDA" --dir "$S/ws" --prompt-file "$S/codex-calls.jsonl" >/dev/null 2>&1; [ $? = 75 ] || { kill $CLI_PID; fail "runner must exit 75 under an attached client"; }
kill $CLI_PID 2>/dev/null; wait $CLI_PID 2>/dev/null

echo "== 5 the viewer: resume when idle (a name resumes or starts fresh), a read-only tail while a turn runs"
python3 - "$S" "$(cat "$S/tid1")" <<'PY' || fail "viewer argv"
import os, sys
sys.path.insert(0, os.environ["SHOALD"])
import codex_harness as cx
from backends import get
sid = sys.argv[2]
argv, env = get("codex").view_argv(sid)
assert argv[1:3] == ["resume", sid], argv
# the seat's own pair (spec role or saved registry entry) rides every resume; nothing is forced without one
assert "-m" not in argv and not any("model_reasoning_effort" in x for x in argv), argv
argv = get("codex").view_argv(sid, model="gpt-test-9", effort="xhigh")[0]
assert argv[argv.index("-m") + 1] == "gpt-test-9" and 'model_reasoning_effort="xhigh"' in argv, argv
argv2 = cx.view_argv("infra-laptop")
assert argv2[0] == "sh" and "resume" in argv2[2] and "exec" in argv2[2], argv2
print("viewer argv ok")
PY
OUT=$($H codex-tail --session "$(cat "$S/tid1")" --once 2>&1); grep -q "stub: the first brief" <<<"$OUT" || fail "codex-tail shows no result :: $OUT"
FAKE_CODEX_SLEEP=2 python3 - "$S" <<'PY' || fail "viewer while busy"
import os, sys, time
sys.path.insert(0, os.environ["SHOALD"])
import codex_harness as cx
from backends import get
be = get("codex")
tid = be.create("v", sys.argv[1] + "/ws", brief="x")
argv, _ = be.view_argv(tid)
assert "codex-tail" in " ".join(argv) and "resume" not in argv, f"a running thread must not be taken over: {argv}"
while cx.busy(tid):
    time.sleep(0.1)
print("viewer while busy ok")
PY

echo "== 6 a first turn that fails before a thread starts raises, it does not hang"
FAKE_CODEX_TID= python3 - "$S" <<'PY' || fail "no thread"
import os, sys
sys.path.insert(0, os.environ["SHOALD"])
import codex_harness as cx
os.environ["SHOAL_CODEX_BIN"] = "/nonexistent/codex"
try:
    cx.create_session(sys.argv[1] + "/ws", "x")
    raise SystemExit("expected a failure")
except RuntimeError as e:
    assert "thread" in str(e) or "cannot run" in str(e), e
print("no-thread failure ok")
PY
echo "== 7 codex-new: a named thread (first turn, then thread/name/set through app-server)"
OUT=$($H codex-new My-Seat.1 --cwd "$S/ws" --prompt "bootstrap prompt" --wait 30 2>&1) || fail "codex-new :: $OUT"
TID7=$(awk '{print $2}' <<<"$OUT" | head -1)
python3 - "$S" "$TID7" <<'PY' || fail "codex-new name"
import json, sys
S, tid = sys.argv[1], sys.argv[2]
rows = [json.loads(l) for l in open(S + "/codex-calls.jsonl")]
ns = [r["name_set"] for r in rows if "name_set" in r]
assert ns and ns[-1] == {"threadId": tid, "name": "My-Seat.1"}, (ns, tid)
print("codex-new ok")
PY
$H codex-new "bad name" --cwd "$S/ws" --prompt x >/dev/null 2>&1 && fail "an invalid name must be refused"
$H codex-new ok-name --cwd "$S/ws" >/dev/null 2>&1 && fail "a missing first prompt must be refused"
echo "SELFTEST CODEX OK ($S)"
