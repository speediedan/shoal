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
BGPIDS=()
trap 'kill "${BGPIDS[@]}" 2>/dev/null' EXIT
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
  {"slug":"parked","ecosystem":"claude","title":"parked","doc":"$DOC","fill":"self","session":""},
  {"slug":"far","ecosystem":"claude","title":"far","doc":"$DOC","fill":"self","session":"farfar00","server":"other-host.invalid"}
 ],"generations":[{"gen":"r3","incarnation":2,"started_utc":"2026-10-01T00:00:00Z","ended_utc":"","sessions":{},"run":{}}]}
JSON
$H ledger-init --spec "$SPEC" >/dev/null || fail "ledger-init"
$H offer --shoal $SID --spec "$SPEC" --to oc --item example/repo#7:fix --no-sync >/dev/null || fail "offer"
$H queue --shoal $SID --spec "$SPEC" --slug cl --active example/repo#3:fix --next example/repo#4:fix --no-sync >/dev/null 2>&1 || true
export SHOAL_FAKE_SESSIONS='{"claude": [{"sessionId": "c1a2b3c4-0000-1111", "id": "c1a2b3c4", "pid": 4242, "state": "blocked", "waitingFor": "input needed", "name": "cl"},
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
# a role on another host has no local sidecar or harness row: never a local stall
assert r["far"]["session"] == "remote" and r["far"]["watcher"] == "remote", r["far"]
assert r["far"]["can_progress"]["ok"], r["far"]
print("contract ok")
PY
# the read model and the health quorum share their predicates: parked-work and watcher-dead agree
$H queue --shoal $SID --spec "$SPEC" --slug oc --wait user-feedback --holder maintainer --next example/repo#8:fix --no-sync >/dev/null 2>&1 || fail "park oc"
python3 - "$SHOALD" "$SPEC" <<'PY2' || fail "ui reasons drift from health.evaluate"
import sys
sys.path.insert(0, sys.argv[1]); sys.path.insert(0, sys.argv[1] + "/ui")
import health, ui_status
from ledger import Ledger, View
from roles import ShoalSpec
spec = ShoalSpec.load(sys.argv[2])
view = View(Ledger(spec.sid, root=spec.ledger_dir, sync=False), spec)
rows = ui_status.claude_rows()
ev = {(c["slug"], c["condition"]) for c in health.evaluate(spec, view, health.claude_busy())
      if c["condition"] in ("parked-work", "watcher-dead")}
ui = set()
for r in spec.roles:
    if not r.session or ui_status.is_remote(r):
        continue
    row = ui_status.role_row(spec, view, r, rows, ui_status.utcnow())
    if row["session"] == "dead":
        continue  # the UI reports dead-worker; the quorum's dead-worker path is revive, not these two
    ui |= {(r.slug, c) for c in ("parked-work", "watcher-dead") if c in row["can_progress"]["reasons"]}
ev = {x for x in ev if x[0] not in ("dead", "far")}
assert ui == ev, (sorted(ui), sorted(ev))
assert ("oc", "parked-work") in ui, sorted(ui)
print("predicates agree:", sorted(ui))
PY2
HEAD0=$(git -C "$A" rev-parse HEAD); SUM0=$(cat "$A"/shoals/$SID/ledger/*.jsonl | sha256sum)
$H ui status --shoal $SID --ledger-root "$ROOT" --json >/dev/null || fail "ui status rc (2)"
[ "$(git -C "$A" rev-parse HEAD)" = "$HEAD0" ] || fail "ui status committed to the ledger"
[ "$(cat "$A"/shoals/$SID/ledger/*.jsonl | sha256sum)" = "$SUM0" ] || fail "ui status wrote a ledger line"
check "uiselftest: 3!" $H ui line --shoal $SID --ledger-root "$ROOT"
check "QUESTION" $H ui status --shoal $SID --ledger-root "$ROOT"
# a long-running pane reloads itself once shoal's code changes and the change has held for two checks
python3 - "$SHOALD" "$S" <<'PY2' || fail "CodeWatch"
import os, sys, time
sys.path.insert(0, sys.argv[1] + "/ui")
import ui_term
w = ui_term.CodeWatch(); w.pkg = sys.argv[2] + "/cw"; os.makedirs(w.pkg, exist_ok=True)
open(w.pkg + "/a.py", "w").write("x = 1\n"); w.stamp = w._stamp()
calls = []
os.execv = lambda *a: calls.append(a)
w.check(); assert not calls
os.utime(w.pkg + "/a.py", (time.time() + 5, time.time() + 5))
w.check(); assert not calls, "re-exec on the first sighting"
w.check(); assert calls, "no re-exec after the change held"
PY2
# colors follow the maintainer's scheme; idle with every next item blocked reads blocked:<kind>
python3 - "$SHOALD" <<'PY2' || fail "state colors / blocked label"
import sys; sys.path.insert(0, sys.argv[1]); sys.path.insert(0, sys.argv[1] + "/ui")
import ui_status as u
assert u.state_color("BLOCKED") == u.RED and u.state_color("blocked:upstream") == u.RED and u.state_color("DEAD") == u.RED
assert u.state_color("wait:remote-ci") == u.ORANGE and u.state_color("idle") == u.ORANGE
assert u.state_color("active") == u.GREEN and u.state_color("working") == u.GREEN
r = {"can_progress": {"reasons": []}, "posture": "idle", "sub": "",
     "next": [{"item": "a#1", "blocker": "upstream:gh-pr:o/r#1", "stale": None},
              {"item": "a#2", "blocker": "upstream:gh-pr:o/r#2", "stale": None}]}
assert u.posture_label(r) == "blocked:upstream", u.posture_label(r)
r["next"][1]["stale"] = True
assert u.posture_label(r) == "idle"  # a stale blocker frees the item: not blocked
# a role waiting on its own job keeps the wait as its posture even with a question out
q = {"can_progress": {"reasons": ["blocked-on-question", "waiting-on-you"]}, "posture": "wait", "sub": "local-ci",
     "next": [], "session": "blocked", "needs_you": True}
assert u.posture_label(q) == "wait:local-ci", u.posture_label(q)
import ui_herdr
assert ui_herdr.herdr_state(q) == "working"
q["sub"] = "user-auth"
assert u.posture_label(q) == "QUESTION" and ui_herdr.herdr_state(q) == "blocked"
t = u.ascii_table(["k", "v"], [["posture", "idle"]], 40, paint={1: lambda v, row: u.state_color(v)})
assert u.ORANGE in t and all(len(l.replace(u.ORANGE, "").replace(u.RESET, "")) == len(t.splitlines()[0]) for l in t.splitlines())
PY2
# the prompt line editor: pre-filled default, cursor moves, insert, delete, paste, Escape cancels
OUT=$(python3 "$TESTD/ui_line_editor_check.py" "$SHOALD" 2>&1)
[ "$OUT" = "$(printf "%s\n" "'Y2026-10-13T09:00X-07:0pasted'" None "'keep'" "'abc'")" ] || fail "line editor :: $OUT"
# a role card is a field/value table
OUT=$(COLUMNS=90 python3 -c "import sys; sys.path.insert(0, '$SHOALD'); sys.path.insert(0, '$SHOALD/ui'); import ui_status, ui_view
ui_status.cached_status = lambda ids=None, force=False: ui_status.status(ids, '$ROOT', opencode_probe=False)
print(ui_view.card('$SID', 'cl'))" 2>&1)
grep -q '| field ' <<<"$OUT" && grep -q '| attention ' <<<"$OUT" || fail "card is not a table :: $OUT"
# the text view is an ASCII table fitted to the width it is given (wide: one table; narrow: two)
OUT=$($H ui status --shoal $SID --ledger-root "$ROOT" --width 160 2>&1)
grep -q '^| role ' <<<"$OUT" && grep -q '| attention ' <<<"$OUT" || fail "no status table :: $OUT"
awk -v w=160 'length($0) > w {exit 1}' <<<"$OUT" || fail "status table wider than 160"
OUT=$($H ui status --shoal $SID --ledger-root "$ROOT" --width 80 2>&1)
awk -v w=80 'length($0) > w && /^[|+]/ {exit 1}' <<<"$OUT" || fail "narrow status table wider than 80 :: $OUT"
grep -q '| last write ' <<<"$OUT" || fail "narrow layout missing its status table :: $OUT"
python3 - "$SHOALD" <<'PY2' || fail "ascii_table clipping"
import sys; sys.path.insert(0, sys.argv[1]); sys.path.insert(0, sys.argv[1] + "/ui")
import ui_status
t = ui_status.ascii_table(["a", "b"], [["x" * 50, "keep"]], 30, flex=(0,))
assert all(len(l) <= 30 for l in t.splitlines()), t
assert "..." in t and "keep" in t, t
PY2
# a popup that fits closes on Enter (not only q), and the empty asks popup closes on one key
OUT=$(printf '\n' | python3 -c "import sys; sys.path.insert(0, '$SHOALD/ui'); import ui_term; ui_term.page('a\nb'); print('CLOSED')" 2>&1)
grep -q CLOSED <<<"$OUT" || fail "page() did not close on Enter :: $OUT"
grep -q 'act", "asks"]' "$TESTD/../plugins/shoal-herdr/herdr-plugin.toml" || fail "asks popup still double-paused"

# quiet-check classifies what a Claude transcript gained: turns, turn inputs, recaps
python3 - "$SHOALD" "$S/tx.jsonl" <<'PY2' || fail "transcript classification"
import json, sys
sys.path.insert(0, sys.argv[1]); sys.path.insert(0, sys.argv[1] + "/ui")
import ui_status
p = sys.argv[2]
open(p, "w").write(json.dumps({"type": "system", "subtype": "turn_duration"}) + "\n")
off = len(open(p, "rb").read())
with open(p, "a") as fh:
    for e in ({"type": "system", "subtype": "away_summary", "content": "recap"},
              {"type": "user", "message": {"content": [{"type": "text", "text": "wake"}]}},
              {"type": "user", "message": {"content": [{"type": "tool_result", "content": "x"}]}},
              {"type": "assistant", "message": {"content": []}},
              {"type": "system", "subtype": "turn_duration"}):
        fh.write(json.dumps(e) + "\n")
t = ui_status.transcript_since(p, off)
assert t == {"turns": 1, "turn_inputs": 1, "recaps": 1, "other": 2}, t
print("transcript classes ok")
PY2

echo "== 2 viewer: argv per harness, never a resume; multiplexer env stripped; dead sessions refused"
check '"attach", "c1a2b3c4"' $H view $SID cl --spec "$SPEC" --dry-run
# a standalone Muse session: a UUID resumes; a name resumes or, the first time, starts a new session
check '"resume", "01a14091-3870-7923-b66b-d3bfd335dd4e"' env SHOAL_MUSE_BIN=/x/muse $H view --harness muse --session 01a14091-3870-7923-b66b-d3bfd335dd4e --dry-run
check 'resume \\"$1\\" || exec' env SHOAL_MUSE_BIN=/x/muse $H view --harness muse --session infra-aux --dry-run
# a blocked session with no prompt (waitingFor empty) is not a QUESTION, and keeps its ledger posture
python3 - "$SHOALD" <<'PY2' || fail "blocked without a prompt"
import sys; sys.path.insert(0, sys.argv[1]); sys.path.insert(0, sys.argv[1] + "/ui")
import ui_status
assert ui_status.pending_question({"waitingFor": "input needed"}, "x") is True
assert ui_status.pending_question({"state": "blocked"}, "x") is False
says = {"posture": "idle", "sub": None, "next": [], "can_progress": {"reasons": ["blocked-says-waiting"]}}
assert ui_status.posture_label(says) == "idle", ui_status.posture_label(says)
assert ui_status.posture_label({**says, "posture": "wait", "sub": "queue-empty"}) == "wait:queue-empty"
import ui_herdr
mk = lambda s, n, r: {"session": s, "needs_you": n, "can_progress": {"reasons": r}}
assert ui_herdr.herdr_state(mk("working", True, ["waiting-on-you"])) == "working"  # busy wins over needs-you
assert ui_herdr.herdr_state(mk("blocked", True, ["blocked-on-question"])) == "blocked"
# the feeder never waits on the provider probes behind conditions: a missing or stale cache returns at once
import json as _j, os as _o, tempfile, time as _t, types
cd = tempfile.mkdtemp(); st = types.SimpleNamespace(sid="condtest", ledger_dir="/nonexistent")
ui_status.BACKGROUND_CONDITIONS = True
t0 = _t.time(); r = ui_status.conditions(st, None, cd)
assert _t.time() - t0 < 1 and r.get("pending") and r["open"] == [], r
_j.dump({"ts": "2000-01-01T00:00:00Z", "open": [{"slug": "x"}]}, open(_o.path.join(cd, "ui-conditions.json"), "w"))
t0 = _t.time(); r = ui_status.conditions(st, None, cd)
assert _t.time() - t0 < 1 and r["open"] == [{"slug": "x"}], r  # the stale cache is served meanwhile
ui_status.BACKGROUND_CONDITIONS = False
# an open prompt on a session its subagents keep working is blocked; a job or CI wait still reads working
q = mk("working", True, ["blocked-on-question"])
assert ui_herdr.herdr_state(q) == "blocked", ui_herdr.herdr_state(q)
assert ui_herdr.herdr_state(dict(q, posture="wait", sub="local-ci")) == "working"
assert ui_status.posture_label(dict(q, posture="wait", sub="user-auth", next=[], active="x")) == "QUESTION"
PY2
check 'CLAUDE_CODE_ENABLE_AWAY_SUMMARY' $H view $SID cl --spec "$SPEC" --dry-run
# the opencode viewer reads the server password from the server process's own environment
env OPENCODE_SERVER_PASSWORD=pw-selftest sleep 300 & SRVPID=$!; BGPIDS+=("$SRVPID")
OCV=$(SHOAL_FAKE_OPENCODE_SERVER="4096:$SRVPID" $H view $SID oc --spec "$SPEC" --dry-run)
kill $SRVPID 2>/dev/null
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
import config  # every harness probe's env (backends, claude_rows) drops pane identity too
assert not [k for k in config.tool_env() if k.startswith(("HERDR_", "ZELLIJ"))]
assert e["OPENCODE_PASSWORD"] == "x"
print("env ok")
PY

echo "== 3 herdr adapter against a recording fake socket: idempotent open, feed, allowed methods only"
export XDG_CONFIG_HOME="$S/xdg"; export SHOAL_HERDR_SESSION=uitest
SOCK="$XDG_CONFIG_HOME/herdr/sessions/uitest/herdr.sock"; mkdir -p "$(dirname "$SOCK")"
python3 - "$SOCK" "$S/herdr-calls.jsonl" "$SHOALD" <<'PY' &
import json, os, socket, sys
path, log, shoald = sys.argv[1], sys.argv[2], sys.argv[3]
ws, panes, n, fg = [], [], [0], {}
def walk(node, wid, tid, out):
    if node["type"] == "pane":
        n[0] += 1
        pid = f"{wid}:p{n[0]}"
        cmd = node.get("command") or ["bash"]
        # the real card process: the wrapper execs python on this checkout's cli.py
        fg[pid] = ([{"name": "python3", "argv": ["python3", os.path.join(shoald, "cli.py")] + cmd[1:]}]
                   if cmd[0].endswith("shoal-ui") else [{"name": os.path.basename(cmd[0]), "argv": cmd}])
        out.append({"pane_id": pid, "label": node.get("label"), "tab_id": tid, "workspace_id": wid})
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
    if m == "workspace.rename":
        for w in ws:
            if w["workspace_id"] == p["workspace_id"]:
                w["label"] = p["label"]
        return {"type": "ok"}
    if m == "workspace.close":
        ws[:] = [w for w in ws if w["workspace_id"] != p["workspace_id"]]
        panes[:] = [q for q in panes if q["workspace_id"] != p["workspace_id"]]
        return {"type": "ok"}
    if m == "tab.close":
        panes[:] = [q for q in panes if q["tab_id"] != p["tab_id"]]
        return {"type": "ok"}
    if m == "pane.move":
        for q in panes:
            if q["pane_id"] == p["pane_id"]:
                q["tab_id"] = p["destination"]["tab_id"]
        return {"type": "ok"}
    if m == "tab.rename":
        return {"type": "ok"}
    if m == "test.close_pane":  # test hook: a card missing, as for a role added after the tab was built
        panes[:] = [q for q in panes if q["pane_id"] != p["pane_id"]]
        return {"type": "ok"}
    if m == "pane.get":
        return {"pane": next((q for q in panes if q["pane_id"] == p["pane_id"]), {})}
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
        return {"process_info": {"foreground_processes": fg.get(p["pane_id"], [{"name": "bash"}])}}
    if m == "test.set_fg":  # test hook: what an operator started in a pane
        fg[p["pane_id"]] = p["procs"]; return {"type": "ok"}
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
FAKE=$!; BGPIDS+=("$FAKE")
for i in 1 2 3 4 5 6 7 8 9 10; do [ -S "$SOCK" ] && break; sleep 0.3; done
[ -S "$SOCK" ] || fail "fake herdr socket did not start"
# an explicit session wins over a socket inherited from the pane the suite runs in
HERDR_SOCKET_PATH="$S/not-the-live-herdr.sock" \
  check "create workspace $SID: overview + 4 role cards" $H ui open --ledger-root "$ROOT" --shoal $SID --no-standalone
check "up to date (4 role cards)" $H ui open --ledger-root "$ROOT" --shoal $SID --no-standalone
# --reapply restarts idle cards on the current code, and refuses while a card is in use
check "re-apply roles tab: restart the cards on the current code" $H ui open --ledger-root "$ROOT" --shoal $SID --no-standalone --reapply
python3 - "$SHOALD" <<'PY2' || fail "reapply must refuse an attached card"
import sys
sys.path.insert(0, sys.argv[1]); sys.path.insert(0, sys.argv[1] + "/ui")
import ui_herdr
h = ui_herdr.Herdr()
pid = next(p["pane_id"] for p in h.panes() if str(p.get("label", "")).endswith("/cl"))
h.call("test.set_fg", {"pane_id": pid, "procs": [{"name": "claude", "argv": ["claude", "attach", "x"]}]})
PY2
check "is in use" $H ui open --ledger-root "$ROOT" --shoal $SID --no-standalone --reapply
# a role added while a card is attached opens in a tab of its own; the attached card is kept
python3 - "$SHOALD" "$S" <<'PY2' || fail "drop a card to stand in for a new role"
import sys
sys.path.insert(0, sys.argv[1]); sys.path.insert(0, sys.argv[1] + "/ui")
import ui_herdr
h = ui_herdr.Herdr()
gone = next(p for p in h.panes() if "/" in str(p.get("label", "")) and not str(p["label"]).endswith(("/cl", "/overview", "/ledger")))
h.call("test.close_pane", {"pane_id": gone["pane_id"]})
open(sys.argv[2] + "/gone", "w").write(gone["label"])
PY2
check "in a new tab" $H ui open --ledger-root "$ROOT" --shoal $SID --no-standalone
python3 - "$SHOALD" "$S" <<'PY2' || fail "new card in its own tab, attached card untouched"
import sys
sys.path.insert(0, sys.argv[1]); sys.path.insert(0, sys.argv[1] + "/ui")
import ui_herdr
h = ui_herdr.Herdr()
gone = open(sys.argv[2] + "/gone").read()
by = {p.get("label"): p for p in h.panes()}
cl = next(p for l, p in by.items() if str(l).endswith("/cl"))
assert gone in by and by[gone]["tab_id"] != cl["tab_id"], (gone, by.get(gone), cl)
print("attached add ok")
PY2
# the roles are now spread over two tabs though one holds them all: with the card still attached the
# stray card MOVES into the first tab (pane.move: nothing is replaced), instead of the refusal
check "move 1 card" $H ui open --ledger-root "$ROOT" --shoal $SID --no-standalone
python3 - "$SHOALD" <<'PY2' || fail "consolidated into one tab, attached card untouched"
import sys
sys.path.insert(0, sys.argv[1]); sys.path.insert(0, sys.argv[1] + "/ui")
import ui_herdr
h = ui_herdr.Herdr()
cards = [p for p in h.panes() if "/" in str(p.get("label", "")) and not str(p["label"]).endswith(("/overview", "/ledger"))]
assert len({p["tab_id"] for p in cards}) == 1, [(p["label"], p["tab_id"]) for p in cards]
assert any(str(p["label"]).endswith("/cl") for p in cards)
print("consolidate ok")
PY2
python3 - "$SHOALD" <<'PY2' || fail "reset the attached card"
import sys
sys.path.insert(0, sys.argv[1]); sys.path.insert(0, sys.argv[1] + "/ui")
import ui_herdr
h = ui_herdr.Herdr()
pid = next(p["pane_id"] for p in h.panes() if str(p.get("label", "")).endswith("/cl"))
h.call("test.set_fg", {"pane_id": pid, "procs": [{"name": "bash", "argv": ["bash"]}]})
PY2
# focused card: an action hands the focused pane to its popup; the popup resolves its role from it
python3 - "$SHOALD" <<'PY2' || fail "focused card"
import os, sys
sys.path.insert(0, sys.argv[1]); sys.path.insert(0, sys.argv[1] + "/ui")
import ui_herdr
h = ui_herdr.Herdr()
pid = next(p["pane_id"] for p in h.panes() if str(p.get("label", "")).endswith("/cl"))
os.environ["SHOAL_UI_FOCUS_PANE"] = pid
assert ui_herdr.focused_role() == ("uiselftest", "cl"), ui_herdr.focused_role()
print("focused card ok")
# a workspace under the legacy `shoal:<id>` label is renamed to the short tag, not duplicated
h.call("workspace.create", {"label": "shoal:zz-20260101-000000"})
ws = ui_herdr.find_shoal_ws(h, "zz-20260101-000000")
assert ws and ws["label"] == "zz", ws
assert [w["label"] for w in h.workspaces()].count("zz") == 1
# a workspace labelled with the full id (an earlier stem collision) is renamed to the tag too
h.call("workspace.create", {"label": "yy-20260101-000000"})
ws = ui_herdr.find_shoal_ws(h, "yy-20260101-000000")
assert ws and ws["label"] == "yy" and not ui_herdr.find_ws(h, "yy-20260101-000000"), ws
print("legacy rename ok")
# the old shared `standalone` workspace closes only when every pane is ours (card, stub, idle shell)
w = h.call("workspace.create", {"label": "standalone"})["workspace"]
h.call("layout.apply", {"tab_id": w["active_tab_id"], "tab_label": "x", "root": {"type": "pane",
       "label": "standalone/old", "command": ui_herdr.shoal_argv("view", "--harness", "claude", "--session", "s", "--stub")}})
old = next(p for p in h.panes() if p.get("label") == "standalone/old")
assert ui_herdr.disposable(h, old["pane_id"])
h.call("test.set_fg", {"pane_id": old["pane_id"], "procs": [{"name": "vim", "argv": ["vim", "notes.md"]}]})
assert not ui_herdr.disposable(h, old["pane_id"])
msg = ui_herdr.migrate_standalone(h)
assert "in use" in msg[0] and ui_herdr.find_ws(h, "standalone"), msg
h.call("test.set_fg", {"pane_id": old["pane_id"], "procs": [{"name": "bash", "argv": ["bash"]}]})
assert "retire" in ui_herdr.migrate_standalone(h)[0] and not ui_herdr.find_ws(h, "standalone")
print("standalone migration allowlist ok")
# a retired shoal's workspace is released: closed when every pane is disposable, else relabelled
# with its full id, so a successor with the same stem never inherits it
import ui_status
real = ui_herdr.retired_ids
ui_herdr.retired_ids = lambda: ["old-20250101-000000"]
w1 = h.call("workspace.create", {"label": "old"})["workspace"]
h.call("layout.apply", {"tab_id": w1["active_tab_id"], "tab_label": "roles", "root": {"type": "pane",
       "label": "old-20250101-000000/r1", "command": ui_herdr.shoal_argv("view", "old-20250101-000000", "r1", "--stub")}})
busy = next(p for p in h.panes() if p.get("label") == "old-20250101-000000/r1")
h.call("test.set_fg", {"pane_id": busy["pane_id"], "procs": [{"name": "claude", "argv": ["claude", "attach", "x"]}]})
assert "relabel" in ui_herdr.release_retired(h, close=True)[0]
assert ui_herdr.find_ws(h, "old-20250101-000000") and not ui_herdr.find_ws(h, "old")
h.call("test.set_fg", {"pane_id": busy["pane_id"], "procs": [{"name": "bash", "argv": ["bash"]}]})
assert "close" in ui_herdr.release_retired(h, close=True)[0]
assert not ui_herdr.find_ws(h, "old-20250101-000000")
ui_herdr.retired_ids = real
print("retired release ok")
PY2
# standalone sessions get one workspace each, named after the session; the old shared one retires
mkdir -p "$XDG_CONFIG_HOME/shoal"
printf '{"v":1,"sessions":[{"harness":"claude","session":"f00dface-9999","label":"my chat"}]}' > "$XDG_CONFIG_HOME/shoal/ui-standalone.json"
check "workspace 'my chat'" $H ui open --ledger-root "$ROOT" --shoal $SID
check "standalone standalone/my chat: open" $H ui open --ledger-root "$ROOT" --shoal $SID
[ "$(grep -c '"layout.apply"' "$S/herdr-calls.jsonl")" = 9 ] || fail "open is not idempotent (2 shoal tabs + 1 standalone + 2 test-made tabs + roles and overview on --reapply + overview on the refused --reapply + the attached-add tab; the consolidation moves the stray card, no apply): $(grep -c layout.apply "$S/herdr-calls.jsonl") applies"
# --reapply restarts an idle standalone stub on the current code too
check "restart on the current code standalone standalone/my chat" $H ui open --ledger-root "$ROOT" --shoal $SID --reapply
# roles lay out in tabs of roles_per_tab cards; changing it re-lays them and closes a leftover tab
check "roles_per_tab = 2" $H ui set roles_per_tab 2
check "re-apply roles tabs: lay the roles out in tabs of the configured size" $H ui open --ledger-root "$ROOT" --shoal $SID --no-standalone
python3 - "$SHOALD" "$SID" <<'PY2' || fail "two role tabs"
import sys; sys.path.insert(0, sys.argv[1]); sys.path.insert(0, sys.argv[1] + "/ui")
import ui_herdr
h = ui_herdr.Herdr()
tabs = {p["tab_id"] for p in h.panes() if str(p.get("label", "")).startswith(sys.argv[2] + "/") and not p["label"].endswith(("/overview", "/ledger"))}
assert len(tabs) == 2, tabs
PY2
check "up to date (4 role cards in 2 tabs)" $H ui open --ledger-root "$ROOT" --shoal $SID --no-standalone
check "roles_per_tab = (default)" $H ui set roles_per_tab ""
check "re-apply roles tab: lay the roles out" $H ui open --ledger-root "$ROOT" --shoal $SID --no-standalone
check "up to date (4 role cards)" $H ui open --ledger-root "$ROOT" --shoal $SID --no-standalone
# role_order puts the named roles first; a card header shows the shoal tag and the role
check "role_order.$SID = cl" $H ui set role_order.$SID cl
python3 - "$SHOALD" "$SPEC" <<'PY2' || fail "role order"
import sys; sys.path.insert(0, sys.argv[1]); sys.path.insert(0, sys.argv[1] + "/ui")
import ui_herdr
from roles import ShoalSpec
assert ui_herdr.role_tabs(ShoalSpec.load(sys.argv[2]))[0][1][0].slug == "cl"
PY2
$H ui set role_order.$SID "" >/dev/null
# the sidebar width setting reaches the client block, as columns or as a share the launcher resolves
check "sidebar_max_width = 50%" $H ui set sidebar_max_width 50%
check "# shoal-sidebar-max: 50%" $H ui herdr-config --client
check "sidebar_max_width = 48" $H ui set sidebar_max_width 48
check "^sidebar_max_width = 48" $H ui herdr-config --client
$H ui set sidebar_max_width "" >/dev/null
# the wake relay has no card unless show_relay is true; the ledger strip takes ledger_share percent
python3 - "$SHOALD" "$SPEC" <<'PY2' || fail "relay hidden by default / ledger share"
import sys; sys.path.insert(0, sys.argv[1]); sys.path.insert(0, sys.argv[1] + "/ui")
import ui_herdr, ui_status
from roles import ShoalSpec, is_wake_relay
s = ShoalSpec.load(sys.argv[2])
assert not any(is_wake_relay(r) for r in ui_herdr.viewable_roles(s))
o = ui_herdr.overview_root(s)
assert o["direction"] == "down" and o["ratio"] == 0.88, o
q = {"can_progress": {"reasons": ["stopped-active"]}, "posture": "active", "sub": "", "next": []}
assert ui_status.posture_label(q) == "ACTIVE?"
PY2
check "show_relay = false" $H ui set show_relay false
$H ui set show_relay "" >/dev/null
check '^agent_panel_sort = "priority"' $H ui herdr-config --client
check "agent_sort = spaces" $H ui set agent_sort spaces
check '^agent_panel_sort = "spaces"' $H ui herdr-config --client
$H ui set agent_sort "" >/dev/null
check "pane_borders = off" $H ui set pane_borders off
check '^pane_borders = "off"' $H ui herdr-config --client
$H ui set pane_borders "" >/dev/null
# a hidden shoal leaves every view (unless named) and its idle workspace closes; show brings it back
check "hidden shoals: $SID" $H ui hide $SID
OUT=$($H ui status --ledger-root "$ROOT" 2>&1); ! grep -q "$SID" <<<"$OUT" || fail "hidden shoal still in ui status :: $OUT"
check "$SID" $H ui status --ledger-root "$ROOT" --shoal $SID
check "close workspace 'uiselftest' of hidden shoal $SID" $H ui open --ledger-root "$ROOT" --no-standalone
check "hidden shoals: (none)" $H ui show $SID
check "create workspace" $H ui open --ledger-root "$ROOT" --no-standalone
SHOAL_LEDGER_DIR="$ROOT" $H ui feed --once >/dev/null || fail "feed --once"
python3 - "$S/herdr-calls.jsonl" <<'PY' || fail "feed reports / allowed methods"
import json, sys
calls = [json.loads(l) for l in open(sys.argv[1])]
ra = [c["params"] for c in calls if c["method"] == "pane.report_agent"]
assert {r["agent"] for r in ra} >= {"cl", "oc", "dead", "far"}, ra
st = {r["agent"]: r["state"] for r in ra}
assert st["cl"] == "blocked" and st["dead"] == "blocked", st
md = [c["params"] for c in calls if c["method"] == "pane.report_metadata"]
assert all(m["source"] == "shoal" and m["ttl_ms"] > 0 for m in md) and md, md
assert any(m["tokens"].get("posture") == "QUESTION" for m in md), md
assert any(c["method"] == "workspace.report_metadata" for c in calls)
allowed = {"workspace.list", "workspace.create", "layout.apply", "pane.list", "pane.process_info",
           "pane.report_agent", "pane.report_metadata", "workspace.report_metadata", "pane.get",
           "workspace.rename", "workspace.close", "tab.close", "tab.rename", "pane.move"}
md_shoal = {m["tokens"].get("shoal") for m in md if m["tokens"].get("role")}
assert md_shoal == {"uiselftest"}, md_shoal
bad = sorted({c["method"] for c in calls if not c["method"].startswith("test.")} - allowed)
assert not bad, bad
print("adapter ok")
PY
kill $FAKE 2>/dev/null
# static guard: the adapter source never calls an input or close method on a pane
grep -nE 'call\("(pane\.send_|pane\.close|agent\.prompt|agent\.send_keys|pane\.run)' "$SHOALD/ui/"*.py \
  && fail "the UI adapter calls a pane input/close method"
unset XDG_CONFIG_HOME SHOAL_HERDR_SESSION

python3 - "$SHOALD" <<'PY2' || fail "picker filter / shoal tag"
import sys
sys.path.insert(0, sys.argv[1]); sys.path.insert(0, sys.argv[1] + "/ui")
import ui_herdr
slugs = {"cl", "oc", "tb-opencode"}
assert ui_herdr.internal_session("shoal-matcher-20261008T1200", slugs)
assert ui_herdr.internal_session("tb-opencode-r1", slugs) and ui_herdr.internal_session("cl-r12", slugs)
assert not ui_herdr.internal_session("Agent-ecosystem-neutrality Phase C", slugs)
assert not ui_herdr.internal_session("shoal-ui-explore", slugs)
ui_herdr._TAGS.clear(); ui_herdr._TAGS.update({"it-mvp-20260921-170946": "it-mvp"})
assert ui_herdr.shoal_tag("it-mvp-20260921-170946") == "it-mvp"
assert ui_herdr.shoal_tag("other-20261008-161310") == "other"
# a retired shoal with the same stem does not force the full id (the next incarnation retires it)
import json, os, tempfile, ui_status
root = tempfile.mkdtemp()
for sid, retired in (("grp-20260101-000000", "2026-01-02T00:00:00Z"), ("grp-20260201-000000", "")):
    os.makedirs(os.path.join(root, "shoals", sid))
    json.dump({"shoal": sid, "retired_utc": retired, "roles": []}, open(os.path.join(root, "shoals", sid, "shoal.json"), "w"))
os.environ["SHOAL_LEDGER_DIR"] = root
ui_herdr._TAGS.clear()
assert ui_herdr.shoal_tag("grp-20260201-000000") == "grp", ui_herdr._TAGS
assert "tab_bar_right" in ui_herdr.server_block() and "tab_bar_right" not in ui_herdr.client_block()
assert '"workspace"' not in ui_herdr.client_block().split("[ui.sidebar.spaces]")[0]
assert '$shoal"' in ui_herdr.client_block()
print("picker filter, tags and config placement ok")
PY2

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
