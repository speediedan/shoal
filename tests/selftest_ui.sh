#!/usr/bin/env bash
# selftest_ui.sh: the UI read model (`ui status`/`ui line`), the viewer (`view`) and the Herdr adapter
# (docs/ui.md). Isolated like the other suites: scratch host config and data root, a local bare
# ledger remote, synthetic start docs, SHOAL_FAKE_SESSIONS for liveness, and a FAKE herdr socket
# server (a Python stub that records every method), so no herdr, claude, opencode or network is
# touched. Proves: the read model writes nothing (ledger HEAD and files unchanged), can_progress
# reasons from harness state, env hygiene of the viewer, idempotent `ui open`, a feed tick that
# reports state and tokens, and that the adapter never calls an input or close method.
#   usage: selftest_ui.sh [scratch-dir]
set -uo pipefail
TESTD="$(cd "$(dirname "$0")" && pwd)"
SHOALD="$(cd "$TESTD/../shoal" && pwd)"
. "$TESTD/selftest_lib.sh"
S="${1:-${TMPDIR:-/tmp}/shoal-ui-selftest-$$}"
exec 9>"/tmp/shoal-selftest-uiselftest.lock"; flock -w 1800 9 || { echo "SELFTEST FAIL: another uiselftest run holds the lock" >&2; exit 1; }
rm -rf "$S" /tmp/uiselftest; mkdir -p "$S/root" "$S/data" "$S/remote"
shoal_test_setup "$S"
export XDG_RUNTIME_DIR="$S/run"  # the UI status cache lives here; never the real one
ROOT="$S/root"; SID=uiselftest; SPEC="$ROOT/shoals/$SID/shoal.json"; A="$S/cloneA"
export SHOAL_SPEC="$SPEC"
H="python3 $SHOALD/cli.py"
fail() { echo "SELFTEST FAIL: $*" >&2; exit 1; }
check() { local pat=$1; shift; local out; out=$("$@" 2>&1 || true); grep -q -- "$pat" <<<"$out" || fail "expected '$pat' in: $* :: $(tail -c 800 <<<"$out")"; }

git init -q --bare "$S/remote/q.git"; git clone -q "$S/remote/q.git" "$A" 2>/dev/null
( cd "$A" && git commit -q --allow-empty -m init && git push -q origin HEAD 2>/dev/null )
mkdir -p "$ROOT/shoals/$SID"
DOC=$(shoal_doc uirole)
cat > "$SPEC" <<JSON
{"shoal":"$SID","dir":"$WORK","incarnation":2,"repos":["example/repo"],
 "ledger":{"repo":"$S/remote/q.git","dir":"$A"},
 "features":{"workwake":true},
 "roles":[
  {"slug":"cl","ecosystem":"claude","title":"cl","doc":"$DOC","fill":"self","session":"c1a2b3c4-0000"},
  {"slug":"oc","ecosystem":"opencode","title":"oc","doc":"$DOC","fill":"assigned","session":"ses_oc1"},
  {"slug":"dead","ecosystem":"claude","title":"dead","doc":"$DOC","fill":"self","session":"deadbeef-0000"},
  {"slug":"parked","ecosystem":"claude","title":"parked","doc":"$DOC","fill":"self","session":""}
 ],"generations":[{"gen":"r3","incarnation":2,"started_utc":"2026-10-01T00:00:00Z","ended_utc":"","sessions":{},"run":{}}]}
JSON
$H ledger-init --spec "$SPEC" >/dev/null || fail "ledger-init"
$H offer --shoal $SID --spec "$SPEC" --to oc --item example/repo#7:fix --no-sync >/dev/null || fail "offer"
$H queue --shoal $SID --spec "$SPEC" --slug cl --active example/repo#3:fix --next example/repo#4:fix --no-sync >/dev/null 2>&1 || true
export SHOAL_FAKE_SESSIONS='{"claude": [{"sessionId": "c1a2b3c4-0000-1111", "pid": 4242, "state": "blocked", "name": "cl"},
  {"sessionId": "deadbeef-0000-2222", "state": "stopped", "name": "dead"},
  {"sessionId": "f00dface-9999", "pid": 777, "state": "idle", "name": "standalone-x"}],
  "opencode": {"ses_oc1": "ses_oc1 status:idle directory:/x"}}'

echo "== 1 read model: contract v1, reasons from harness state, and it writes nothing"
HEAD0=$(git -C "$A" rev-parse HEAD); SUM0=$(cat "$A"/shoals/$SID/ledger/*.jsonl | sha256sum)
OUT=$($H ui status --shoal $SID --ledger-root "$ROOT" --json) || fail "ui status rc"
python3 - "$OUT" <<'PY' || fail "status contract"
import json, sys
d = json.loads(sys.argv[1])
assert d["v"] == 1 and len(d["shoals"]) == 1, d
s = d["shoals"][0]
assert s["id"] == "uiselftest" and s["gen"] == "r3", s
r = {x["slug"]: x for x in s["roles"]}
assert r["cl"]["session"] == "blocked" and "blocked-on-question" in r["cl"]["can_progress"]["reasons"], r["cl"]
assert r["cl"]["needs_you"] is True
assert r["dead"]["session"] == "dead" and "dead-worker" in r["dead"]["can_progress"]["reasons"], r["dead"]
assert r["parked"]["posture"] == "parked" and r["parked"]["can_progress"]["ok"], r["parked"]
assert r["oc"]["session"] == "idle" and r["oc"]["offers_open"] == 1, r["oc"]
assert r["cl"]["view"][-2:] == ["uiselftest", "cl"], r["cl"]["view"]
assert s["needs_you"] >= 2, s
print("contract ok")
PY
[ "$(git -C "$A" rev-parse HEAD)" = "$HEAD0" ] || fail "ui status committed to the ledger"
[ "$(cat "$A"/shoals/$SID/ledger/*.jsonl | sha256sum)" = "$SUM0" ] || fail "ui status wrote a ledger line"
check "uiselftest: 2!" $H ui line --shoal $SID --ledger-root "$ROOT"
check "QUESTION" $H ui status --shoal $SID --ledger-root "$ROOT"

echo "== 2 viewer: argv per harness, never a resume; multiplexer env stripped; dead sessions refused"
check '"attach", "c1a2b3c4-0000"' $H view $SID cl --spec "$SPEC" --dry-run
sleep 300 & PWPID=$!
# the opencode viewer reads the server password from the server process's own environment
env OPENCODE_SERVER_PASSWORD=pw-selftest sleep 300 & SRVPID=$!
OCV=$(SHOAL_FAKE_OPENCODE_SERVER="4096:$SRVPID" $H view $SID oc --spec "$SPEC" --dry-run)
kill $PWPID $SRVPID 2>/dev/null
grep -q -- '"--server", "http://127.0.0.1:4096", "--session", "ses_oc1"' <<<"$OCV" || fail "opencode viewer argv: $OCV"
grep -q OPENCODE_PASSWORD <<<"$OCV" || fail "opencode viewer env: $OCV"
grep -q pw-selftest <<<"$OCV" && fail "viewer printed a credential value"
grep -q -- '--resume\|"resume"' <<<"$OCV$($H view $SID cl --spec "$SPEC" --dry-run)" && fail "a viewer resumes"
check "has no worker" $H view $SID dead --spec "$SPEC"
check "no live session" $H view $SID parked --spec "$SPEC"
HERDR_PANE_ID=w1:p1 HERDR_SOCKET_PATH=/x ZELLIJ_SESSION_NAME=z python3 - "$SHOALD" <<'PY' || fail "env hygiene"
import sys; sys.path.insert(0, sys.argv[1]); sys.path.insert(0, sys.argv[1] + "/ui")
import ui_view
e = ui_view.clean_env({"OPENCODE_PASSWORD": "x"})
assert not [k for k in e if k.startswith(("HERDR_", "ZELLIJ"))], sorted(e)
assert e["OPENCODE_PASSWORD"] == "x"
print("env ok")
PY

echo "== 3 herdr adapter against a recording fake socket: idempotent open, feed, allowed methods only"
export XDG_CONFIG_HOME="$S/xdg"; export SHOAL_HERDR_SESSION=uitest
SOCK="$XDG_CONFIG_HOME/herdr/sessions/uitest/herdr.sock"; mkdir -p "$(dirname "$SOCK")"
python3 - "$SOCK" "$S/herdr-calls.jsonl" <<'PY' &
import json, os, socket, sys
path, log = sys.argv[1], sys.argv[2]
ws, panes, n = [], [], [0]
def walk(node, wid, tid, out):
    if node["type"] == "pane":
        n[0] += 1
        out.append({"pane_id": f"{wid}:p{n[0]}", "label": node.get("label"), "tab_id": tid, "workspace_id": wid})
    else:
        walk(node["first"], wid, tid, out); walk(node["second"], wid, tid, out)
def handle(req):
    m, p = req["method"], req.get("params") or {}
    open(log, "a").write(json.dumps({"method": m, "params": p}) + "\n")
    if m == "workspace.list":
        return {"workspaces": ws}
    if m == "workspace.create":
        w = {"workspace_id": f"w{len(ws) + 1}", "label": p.get("label"), "active_tab_id": f"w{len(ws) + 1}:t1"}
        ws.append(w); return {"workspace": w}
    if m == "layout.apply":
        if "tab_id" in p and "workspace_id" in p:
            return {"__error": "invalid_target"}
        wid = p.get("workspace_id") or p["tab_id"].split(":")[0]
        tid = p.get("tab_id") or f"{wid}:t{len(panes) + 9}"
        panes[:] = [q for q in panes if q["tab_id"] != tid]
        walk(p["root"], wid, tid, panes); return {"layout": {}}
    if m == "pane.list":
        return {"panes": [q for q in panes if not p.get("workspace_id") or q["workspace_id"] == p["workspace_id"]]}
    if m == "pane.process_info":
        return {"process_info": {"foreground_processes": [{"name": "python3"}]}}
    return {"type": "ok"}
s = socket.socket(socket.AF_UNIX); s.bind(path); s.listen(8)
while True:
    c, _ = s.accept()
    buf = b""
    while not buf.endswith(b"\n"):
        chunk = c.recv(65536)
        if not chunk: break
        buf += chunk
    if not buf: c.close(); continue
    req = json.loads(buf); res = handle(req)
    if "__error" in res:
        out = {"id": req["id"], "error": {"code": res["__error"], "message": "fake"}}
    else:
        out = {"id": req["id"], "result": res}
    c.sendall((json.dumps(out) + "\n").encode()); c.close()
PY
FAKE=$!
for i in 1 2 3 4 5 6 7 8 9 10; do [ -S "$SOCK" ] && break; sleep 0.3; done
[ -S "$SOCK" ] || fail "fake herdr socket did not start"
check "create workspace shoal:$SID: overview + 3 role cards" $H ui open --ledger-root "$ROOT" --shoal $SID --no-standalone
check "up to date (3 role cards)" $H ui open --ledger-root "$ROOT" --shoal $SID --no-standalone
[ "$(grep -c '"layout.apply"' "$S/herdr-calls.jsonl")" = 2 ] || fail "open is not idempotent: $(grep -c layout.apply "$S/herdr-calls.jsonl") applies"
SHOAL_LEDGER_DIR="$ROOT" $H ui feed --once >/dev/null || fail "feed --once"
python3 - "$S/herdr-calls.jsonl" <<'PY' || fail "feed reports / allowed methods"
import json, sys
calls = [json.loads(l) for l in open(sys.argv[1])]
ra = [c["params"] for c in calls if c["method"] == "pane.report_agent"]
assert {r["agent"] for r in ra} == {"cl", "oc", "dead"}, ra
st = {r["agent"]: r["state"] for r in ra}
assert st["cl"] == "blocked" and st["dead"] == "blocked", st
md = [c["params"] for c in calls if c["method"] == "pane.report_metadata"]
assert all(m["source"] == "shoal" and m["ttl_ms"] > 0 for m in md) and md, md
assert any(m["tokens"].get("posture") == "QUESTION" for m in md), md
assert any(c["method"] == "workspace.report_metadata" for c in calls)
allowed = {"workspace.list", "workspace.create", "layout.apply", "pane.list", "pane.process_info",
           "pane.report_agent", "pane.report_metadata", "workspace.report_metadata", "pane.get"}
bad = sorted({c["method"] for c in calls} - allowed)
assert not bad, bad
print("adapter ok")
PY
kill $FAKE 2>/dev/null
# static guard: the adapter source never calls an input or close method on a pane
grep -nE 'call\("(pane\.send_|pane\.close|agent\.prompt|agent\.send_keys|pane\.run)' "$SHOALD/ui/"*.py \
  && fail "the UI adapter calls a pane input/close method"
unset XDG_CONFIG_HOME SHOAL_HERDR_SESSION

echo "== 4 herdr config: managed block merges idempotently and refuses duplicate tables"
CFG="$S/herdr-config.toml"
printf '[theme]\nname = "nord"\n' > "$CFG"
$H ui herdr-config --client --write --path "$CFG" >/dev/null || fail "client block write"
$H ui herdr-config --client --write --path "$CFG" >/dev/null || fail "client block rewrite"
[ "$(grep -c 'shoal-ui managed block (shoal' "$CFG")" = 1 ] || fail "managed block duplicated"
grep -q 'name = "nord"' "$CFG" || fail "user config lost"
$H ui herdr-config --server --write --path "$S/server.toml" >/dev/null || fail "server block"
# both files are valid TOML (when this interpreter has a TOML reader; Python 3.10 may not)
python3 - "$CFG" "$S/server.toml" <<'PY2' || fail "managed config is not valid TOML"
import sys
try:
    import tomllib as t
except ImportError:
    try:
        import tomli as t
    except ImportError:
        print("toml check SKIP (no reader)"); sys.exit(0)
for f in sys.argv[1:]:
    d = t.loads(open(f).read())
assert d["session"]["resume_agents_on_restore"] is False
print("toml ok")
PY2
printf '[ui]\nfoo = 1\n' > "$S/conflict.toml"
check "already defines \[ui\]" $H ui herdr-config --client --write --path "$S/conflict.toml"
grep -q "resume_agents_on_restore = false" "$S/server.toml" || fail "server block lacks restore off"
head -1 "$S/server.toml" | grep -q "managed block" || fail "server block (top-level keys) must come first"

rm -rf /tmp/uiselftest
echo "SELFTEST PASS ($S)"
