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
 "features":{"workwake":true,"waitgate":false},
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
# A native prompt remains actionable when the harness tempo reads idle; an answer clears it.
python3 - "$SHOALD" "$ROOT" "$SID" <<'PY2' || fail "idle prompt classification"
import sys, os, json
sys.path[:0] = [sys.argv[1], sys.argv[1] + "/ui"]
import ui_status
fake = json.loads(os.environ["SHOAL_FAKE_SESSIONS"])
row = fake["claude"][0]
row["tempo"] = "idle"
for pending in (True, False):
    row["waitingFor"] = "input needed" if pending else None
    os.environ["SHOAL_FAKE_SESSIONS"] = json.dumps(fake)
    doc = ui_status.status([sys.argv[3]], root=sys.argv[2], opencode_probe=False)
    role = next(r for r in doc["shoals"][0]["roles"] if r["slug"] == "cl")
    assert role["session"] == "idle"
    assert ("blocked-on-question" in role["can_progress"]["reasons"]) == pending, role
print("idle prompt classification ok")
PY2
# the overview table carries each role's ecosystem (claude, opencode, codex, muse) as its own column
TXT=$($H ui status --shoal $SID --ledger-root "$ROOT" --width 200) || fail "ui status text rc"
grep -Eq '\| role +\| host +\| ecosystem +\| posture' <<<"$TXT" || fail "overview table has no host and ecosystem columns: $TXT"
# a role on another host is never attached from here (sessions resolve through this host's harness), and gets no card
check "far runs on other-host.invalid: attach from that host's viewer" $H view $SID far --spec "$SPEC" --dry-run
python3 - "$SHOALD" "$SPEC" <<'PY2' || fail "remote roles get no card"
import sys; sys.path.insert(0, sys.argv[1]); sys.path.insert(0, sys.argv[1] + "/ui")
import ui_herdr, ui_view
from roles import ShoalSpec
spec = ShoalSpec.load(sys.argv[2])
assert "far" not in {r.slug for r in ui_herdr.viewable_roles(spec)}
assert ui_view._remote_host(spec.sid, "far", sys.argv[2]) == "other-host.invalid"
assert ui_view._remote_host(spec.sid, "cl", sys.argv[2]) == ""
print("remote cards ok")
PY2
# ... unless this host has an ssh route to that host: then the card attaches by running that host's own
# `shoal view` over ssh (a stub ssh here records its argv and exits with $STUB_SSH_RC)
cat > "$S/ssh-stub" <<'SH'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "${STUB_SSH_LOG:-/dev/null}"
case "$*" in *--rows-only*) [ -n "${STUB_SSH_OUT:-}" ] && cat "$STUB_SSH_OUT" ;; *"ui standalone"*) [ -n "${STUB_SSH_SA:-}" ] && cat "$STUB_SSH_SA" ;; esac
exit "${STUB_SSH_RC:-0}"
SH
chmod +x "$S/ssh-stub"
cp "$SHOAL_CONFIG" "$S/host-noroute.toml"
printf '\n[hosts."other-host.invalid"]\nssh = "u@other-host.invalid"\nresolve = "dns"\n' >> "$SHOAL_CONFIG"
OUT=$(SHOAL_SSH="$S/ssh-stub" $H view $SID far --spec "$SPEC" --dry-run) || fail "view far with a route rc :: $OUT"
python3 - "$OUT" "$S/ssh-stub" <<'PY2' || fail "view over a route"
import json, sys
a = json.loads(sys.argv[1])["argv"]
assert a[0] == sys.argv[2] and "-tt" in a and "BatchMode=yes" in a and "u@other-host.invalid" in a, a
assert a[-1] == "bash -lc 'shoal view uiselftest far'", a[-1]
print("view over a route ok")
PY2
STUB_SSH_LOG="$S/ssh.log" STUB_SSH_RC=0 SHOAL_SSH="$S/ssh-stub" python3 - "$SHOALD" "$SPEC" <<'PY2' || fail "routed cards"
import os, sys; sys.path.insert(0, sys.argv[1]); sys.path.insert(0, sys.argv[1] + "/ui")
import routes, ui_herdr, ui_view
from roles import ShoalSpec
spec = ShoalSpec.load(sys.argv[2])
assert "far" in {r.slug for r in ui_herdr.viewable_roles(spec)}, "a routed remote role gets a card"
assert routes.reachable("other-host.invalid", force=True) is True
assert routes.reachable("nowhere.invalid") is None, "no route: None"
os.environ["STUB_SSH_RC"] = "255"
assert routes.reachable("other-host.invalid", force=True) is False
# an attach that fails fast says why and retries every ATTACH_RETRY_S; q gives up (no session is taken over)
ui_view.ATTACH_RETRY_S = 0
keys = iter(["r", "q"])
ui_view._getkey = lambda t: next(keys)
import io, contextlib
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    rc = ui_view.remote_attach("other-host.invalid", spec.sid, "far")
assert rc == 255 and buf.getvalue().count("host unreachable") == 2, buf.getvalue()
os.environ["STUB_SSH_RC"] = "1"
routes._store("reach-other-host.invalid", {"ok": True})
ui_view.routes = routes
orig = routes.reachable
routes.reachable = lambda server, force=False: True
keys = iter(["q"])
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    rc = ui_view.remote_attach("other-host.invalid", spec.sid, "far")
assert rc == 1 and "attached elsewhere" in buf.getvalue(), buf.getvalue()
# reachable, but ssh's own exit 255: the connection ended, not a refused attach
os.environ["STUB_SSH_RC"] = "255"
keys = iter(["q"])
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    rc = ui_view.remote_attach("other-host.invalid", spec.sid, "far")
assert rc == 255 and "connection ended" in buf.getvalue() and "attached elsewhere" not in buf.getvalue(), buf.getvalue()
os.environ["STUB_SSH_RC"] = "1"
routes.reachable = orig
attempts = [l for l in open(os.environ["STUB_SSH_LOG"]) if "shoal view uiselftest far" in l]
assert len(attempts) == 4, attempts
print("routed cards ok")
PY2
# live state of a routed remote role comes from its own host's rows (`ui status --rows-only`), cached 30 s;
# a failed pull keeps the last good rows, marked stale
OUT=$($H ui status --shoal $SID --ledger-root "$ROOT" --rows-only --json) || fail "rows-only rc"
python3 - "$OUT" <<'PY2' || fail "rows-only is this host's rows only"
import json, sys
d = json.loads(sys.argv[1]); r = {x["slug"] for x in d["shoals"][0]["roles"]}
assert "far" not in r and "cl" in r and "usage" not in d, (r, list(d))
print("rows-only ok")
PY2
cat > "$S/far-rows.json" <<JSON
{"v": 1, "shoals": [{"id": "$SID", "roles": [{"slug": "far", "session_id": "farfar00", "session": "idle", "watcher": "alive",
 "can_progress": {"ok": true, "reasons": []}, "needs_you": false, "context_k": 42, "heartbeat_age_s": 30}]}]}
JSON
rm -rf "$SHOAL_ROUTE_CACHE"
STUB_SSH_OUT="$S/far-rows.json" SHOAL_SSH="$S/ssh-stub" python3 - "$SHOALD" "$SPEC" "$ROOT" <<'PY2' || fail "remote status merge"
import os, sys; sys.path.insert(0, sys.argv[1]); sys.path.insert(0, sys.argv[1] + "/ui")
import routes, ui_status
def far():
    doc = ui_status.status(["uiselftest"], root=sys.argv[3])
    return next(x for x in doc["shoals"][0]["roles"] if x["slug"] == "far")
f = far()
assert f["session"] == "idle" and f["watcher"] == "alive" and f["context_k"] == 42, f
assert f["remote_status"]["ok"] and not f["remote_status"]["stale"], f["remote_status"]
# the host stops answering: the last good rows stay, marked stale once the cache has aged
os.environ["STUB_SSH_RC"] = "255"; os.environ.pop("STUB_SSH_OUT")
import json, time
p = os.path.join(routes._cache_dir(), "status-other-host.invalid-uiselftest.json")
d = json.load(open(p)); d["t"] -= 60; d["pulled"] -= 200; json.dump(d, open(p, "w"))
f = far()
assert f["session"] == "idle" and f["remote_status"]["stale"] and not f["remote_status"]["ok"], f
print("remote status merge ok")
PY2
# the wake relay's watch process runs where the relay role does: a relay on another host is read from that
# host's rows (relay_alive), unknown when nothing can say, and never DEAD because this host has no watch process
python3 - "$SPEC" "$S/relay-spec.json" <<'PY2' || fail "relay spec"
import json, sys
d = json.load(open(sys.argv[1]))
d["roles"].append({"slug": "wake-relay", "ecosystem": "claude", "title": "wake-relay", "doc": d["roles"][0]["doc"],
                   "fill": "none", "session": "relay000", "server": "other-host.invalid"})
json.dump(d, open(sys.argv[2], "w"))
PY2
cat > "$S/relay-rows.json" <<JSON
{"v": 1, "shoals": [{"id": "$SID", "roles": [{"slug": "wake-relay", "session_id": "relay000", "session": "idle", "watcher": "none",
 "can_progress": {"ok": true, "reasons": []}, "needs_you": false, "context_k": null, "heartbeat_age_s": 5, "relay_alive": true}]}]}
JSON
rm -rf "$SHOAL_ROUTE_CACHE"
STUB_SSH_RC=0 STUB_SSH_OUT="$S/relay-rows.json" SHOAL_SSH="$S/ssh-stub" python3 - "$SHOALD" "$S/relay-spec.json" <<'PY2' || fail "relay read where it runs"
import os, sys; sys.path.insert(0, sys.argv[1]); sys.path.insert(0, sys.argv[1] + "/ui")
import routes, ui_status, ui_herdr
def doc():
    return ui_status.shoal_doc(sys.argv[2], [])
d = doc()
assert d["relay"] == {"alive": True}, d["relay"]
assert ui_herdr.shoal_tokens(d)["shoal_relay"] == "ok"
# the relay's host says it is down
rows = open(os.environ["STUB_SSH_OUT"]).read().replace('"relay_alive": true', '"relay_alive": false')
open(os.environ["STUB_SSH_OUT"], "w").write(rows); [os.remove(os.path.join(routes._cache_dir(), f)) for f in os.listdir(routes._cache_dir())]
d = doc()
assert d["relay"] == {"alive": False}, d["relay"]
assert ui_herdr.shoal_tokens(d)["shoal_relay"] == "DEAD" and "relay DEAD" in ui_status._shoal_bits(d)
# nothing can say (the host does not answer, nothing cached): unknown, not DEAD
os.environ["STUB_SSH_RC"] = "255"; os.environ.pop("STUB_SSH_OUT")
[os.remove(os.path.join(routes._cache_dir(), f)) for f in os.listdir(routes._cache_dir())]
d = doc()
assert d["relay"] == {"alive": None}, d["relay"]
assert ui_herdr.shoal_tokens(d)["shoal_relay"] == "?", ui_herdr.shoal_tokens(d)
assert "relay DEAD" not in ui_status._shoal_bits(d)
print("relay read where it runs ok")
PY2
# standalone sessions saved on a routed host are mirrored here (label @host) and attach over ssh; a reachable
# host's list is the truth, an unreachable host's mirrors stay as they were
printf '{"v": 1, "host": "other-host.invalid", "sessions": [{"harness": "claude", "session": "abcd1234-5555", "label": "lapseat", "turn": "idle", "state": "idle"}]}' > "$S/sa.json"
rm -rf "$SHOAL_ROUTE_CACHE"
STUB_SSH_SA="$S/sa.json" SHOAL_SSH="$S/ssh-stub" XDG_CONFIG_HOME="$S/xdg-sa" python3 - "$SHOALD" <<'PY2' || fail "remote standalone"
import io, json, os, sys, contextlib; sys.path.insert(0, sys.argv[1]); sys.path.insert(0, sys.argv[1] + "/ui")
import routes, ui_herdr
local = {"harness": "opencode", "session": "ses_local", "label": "mine"}
entries = [dict(local)]
changed, notes = ui_herdr.reconcile_remote_standalone(entries)
m = [e for e in entries if e.get("host")]
assert changed and len(m) == 1 and m[0]["label"] == "lapseat@other-host.invalid", entries
assert ui_herdr.standalone_argv(m[0])[-2:] == ["--host", "other-host.invalid"], ui_herdr.standalone_argv(m[0])
assert ui_herdr.remote_standalone_state(m[0]) == ("idle", "idle")
# the mirror never re-exports: this host's own list has only its local, open sessions
ui_herdr.save_standalone(entries + [{"harness": "claude", "session": "gone0000", "label": "closed", "dismissed": True}])
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    ui_herdr.cmd_standalone(type("A", (), {"json": True})())
mine = json.loads(buf.getvalue())["sessions"]
assert [x["session"] for x in mine] == ["ses_local"], mine
# unreachable now: the mirror stays; reachable with an empty list: it goes
os.environ["STUB_SSH_RC"] = "255"; rm = routes._cache_dir(); [os.remove(os.path.join(rm, f)) for f in os.listdir(rm)]
changed, notes = ui_herdr.reconcile_remote_standalone(entries)
assert not changed and any(e.get("host") for e in entries) and "not reachable" in " ".join(notes), (entries, notes)
os.environ["STUB_SSH_RC"] = "0"; open(os.environ["STUB_SSH_SA"], "w").write('{"v": 1, "sessions": []}')
[os.remove(os.path.join(rm, f)) for f in os.listdir(rm)]
changed, notes = ui_herdr.reconcile_remote_standalone(entries)
assert changed and not any(e.get("host") for e in entries), entries
print("remote standalone ok")
PY2
cp "$S/host-noroute.toml" "$SHOAL_CONFIG"
# a single-host shoal reports no host beats even when the ledger holds an old one (a leftover read as a fault)
python3 - "$SHOALD" "$SPEC" "$S" <<'PY2' || fail "single-host beats"
import json, os, sys; sys.path.insert(0, sys.argv[1]); sys.path.insert(0, sys.argv[1] + "/ui")
import ui_status
from ledger import Ledger
from roles import ShoalSpec
spec = ShoalSpec.load(sys.argv[2])
Ledger(spec.sid, root=spec.ledger_dir, sync=False).append("shoal", "system", kind="host-beat", host="other-host.invalid",
                                                         instance="feedfacecafe")
d = json.load(open(sys.argv[2])); d["roles"] = [r for r in d["roles"] if r["slug"] != "far"]
one = os.path.join(sys.argv[3], "single-host-spec.json"); json.dump(d, open(one, "w"))
rows = ui_status.claude_rows()
multi = ui_status.shoal_doc(sys.argv[2], rows)
single = ui_status.shoal_doc(one, rows)
assert multi["multi_host"] and "other-host.invalid" in multi["hosts"], multi["hosts"]
assert not single["multi_host"] and single["hosts"] == {}, single["hosts"]
print("single-host beats ok")
PY2
# the fixture is multi-host (role far lives elsewhere): the host column names it, the header names home
grep -Eq '\| far +\| other-host.invalid +\| claude' <<<"$TXT" || fail "the remote role's host is not shown: $TXT"
grep -Eq '\| cl +\| [^|]+ +\| claude +\|' <<<"$TXT" || fail "cl row does not read ecosystem claude: $TXT"
grep -Eq '\| oc +\| [^|]+ +\| opencode +\|' <<<"$TXT" || fail "oc row does not read ecosystem opencode: $TXT"
grep -Eq '\| parked +\| [^|]+ +\| claude +\| parked +\| parked' <<<"$TXT" || fail "parked role missing from overview table: $TXT"
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
check '--disable-sandbox || exec' env SHOAL_MUSE_BIN=/x/muse $H view --harness muse --session infra-aux --dry-run
check '"resume", "01a14091-3870-7923-b66b-d3bfd335dd4e"' env SHOAL_CODEX_BIN=/x/codex $H view --harness codex --session 01a14091-3870-7923-b66b-d3bfd335dd4e --dry-run
check 'resume \\"$1\\" || exec' env SHOAL_CODEX_BIN=/x/codex $H view --harness codex --session infra-laptop --dry-run
# a standalone seat's saved model and effort (ui pick --model/--effort, from muse-new --pick) ride every resume
mkdir -p "$S/xdg-view/shoal"
python3 - "$S" <<'PY2'
import json, sys
json.dump({"v": 1, "sessions": [{"harness": "muse", "session": "01a14091-3870-7923-b66b-d3bfd335dd4e",
           "label": "saved-seat", "model": "muse-test-9", "effort": "xhigh"}]},
          open(sys.argv[1] + "/xdg-view/shoal/ui-standalone.json", "w"))
PY2
check '"--model", "muse-test-9", "--reasoning-effort", "xhigh"' env XDG_CONFIG_HOME="$S/xdg-view" SHOAL_MUSE_BIN=/x/muse $H view --harness muse --session 01a14091-3870-7923-b66b-d3bfd335dd4e --label saved-seat --dry-run
check '"--reasoning-effort", "high"' env XDG_CONFIG_HOME="$S/xdg-view-none" SHOAL_MUSE_BIN=/x/muse $H view --harness muse --session 01a14091-3870-7923-b66b-d3bfd335dd4e --dry-run
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
# Standalone cards use the same confirmed question detector as role cards, including idle
# tempo and active subagents. Answering clears QUESTION; raw blocked alone does not imply it.
entry = {"harness": "claude", "session": "fixture-session", "job": "fixture-job"}
base = {"sessionId": "fixture-session", "id": "fixture-job", "pid": 123, "state": "blocked"}
for tempo in ("blocked", "active", "idle"):
    row = dict(base, tempo=tempo, waitingFor="input needed")
    assert ui_herdr.standalone_state(entry, [row]) == ("QUESTION", "blocked")
assert ui_herdr.standalone_state(entry, [dict(base, state="done", tempo="idle")]) == ("idle", "idle")
assert ui_herdr.standalone_state(entry, [base]) == ("blocked", "blocked")
assert ui_herdr.standalone_state(entry, [dict(base, pid=None, waitingFor="input needed")])[0] == "dead"
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
# a role whose worker is gone is still viewable: `claude attach` wakes it without a turn (section 7 checks the note);
# --dry-run here, so no real attach runs
check '"attach", "deadbeef"' $H view $SID dead --spec "$SPEC" --dry-run
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
  check "create workspace $SID: overview + 3 role cards" $H ui open --ledger-root "$ROOT" --shoal $SID --no-standalone
check "up to date (3 role cards)" $H ui open --ledger-root "$ROOT" --shoal $SID --no-standalone
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
# a standalone session closed in the viewer stays closed across `ui open` (dismissed), and --show brings it back
python3 - "$SHOALD" <<'PY2' || fail "close the standalone workspace"
import sys; sys.path.insert(0, sys.argv[1]); sys.path.insert(0, sys.argv[1] + "/ui")
import ui_herdr
h = ui_herdr.Herdr()
w = next(w for w in h.workspaces() if w.get("label") == "my chat")
h.call("workspace.close", {"workspace_id": w["workspace_id"]})
PY2
check "standalone my chat: closed in the viewer, left closed" $H ui open --ledger-root "$ROOT" --shoal $SID
check "standalone my chat: closed in the viewer, left closed" $H ui open --ledger-root "$ROOT" --shoal $SID
python3 - "$SHOALD" "$XDG_CONFIG_HOME" <<'PY2' || fail "dismissed standalone stays closed"
import json, sys; sys.path.insert(0, sys.argv[1]); sys.path.insert(0, sys.argv[1] + "/ui")
import ui_herdr
assert not any(p.get("label") == "standalone/my chat" for p in ui_herdr.Herdr().panes()), "re-created"
e = json.load(open(sys.argv[2] + "/shoal/ui-standalone.json"))["sessions"][0]
assert e["opened"] and e["dismissed"], e
# a fresh server (nothing labelled to compare with) never dismisses: a restore that lost every pane reopens them
class Fresh:
    def panes(self, *a): return []
    def call(self, m, p=None, timeout=0): raise AssertionError(m)
e2 = {"harness": "claude", "session": "feedface-1", "label": "x", "opened": True}
ui_herdr.open_standalone = lambda h, e, dry, reapply=False: "open " + e["label"]
assert ui_herdr.sync_standalone(Fresh(), [e2], set(), False, True, False) == ["open x"] and "dismissed" not in e2
assert "left closed" in ui_herdr.sync_standalone(Fresh(), [e2], set(), True, True, False)[0] and e2["dismissed"]
assert ui_herdr.sync_standalone(Fresh(), [e2], {"x"}, True, True, False) == ["open x"] and "dismissed" not in e2
print("dismissal ok")
PY2
check "open standalone standalone/my chat" $H ui open --ledger-root "$ROOT" --shoal $SID --show "my chat"
python3 -c "import json,sys; e=json.load(open(sys.argv[1]+'/shoal/ui-standalone.json'))['sessions'][0]; assert e['opened'] and 'dismissed' not in e, e" "$XDG_CONFIG_HOME" || fail "--show did not clear the dismissal"
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
check "up to date (3 role cards in 2 tabs)" $H ui open --ledger-root "$ROOT" --shoal $SID --no-standalone
# auto_rebalance: `ui set` lays an open shoal's roles out at once (the feeder does within a minute)
check "uiselftest: re-apply roles tab: lay the roles out" env SHOAL_LEDGER_DIR="$ROOT" $H ui set roles_per_tab ""
check "up to date (3 role cards)" $H ui open --ledger-root "$ROOT" --shoal $SID --no-standalone
# the free card of a role no longer viewable is relaid away
python3 - "$SHOALD" "$SPEC" "$SID" <<'PY2' || fail "a removed role's card is relaid away"
import sys; sys.path.insert(0, sys.argv[1]); sys.path.insert(0, sys.argv[1] + "/ui")
import ui_herdr
from roles import ShoalSpec
spec = ShoalSpec.load(sys.argv[2]); h = ui_herdr.Herdr(); sid = sys.argv[3]
cards = [p for p in h.panes() if str(p.get("label", "")).startswith(sid + "/") and not p["label"].endswith(("/overview", "/ledger"))]
nodes = [ui_herdr.role_node(spec, r) for r in ui_herdr.viewable_roles(spec)]
nodes.append(ui_herdr._node(sid + "/ghost", spec.dir, ui_herdr.shoal_argv("view", sid, "ghost", "--stub"), {}))
h.call("layout.apply", {"tab_id": cards[0]["tab_id"], "tab_label": "roles", "root": ui_herdr._grid(nodes)})
out = ui_herdr.ensure_shoal(h, spec)
assert any(f"remove {sid}/ghost" in ln for ln in out), out
assert not any(p.get("label") == sid + "/ghost" for p in h.panes()), "the ghost card was kept"
assert "up to date" in ui_herdr.ensure_shoal(h, spec)[0]
print("removed role ok")
PY2
check "up to date (3 role cards)" $H ui open --ledger-root "$ROOT" --shoal $SID --no-standalone
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
assert {r["agent"] for r in ra} >= {"cl", "oc", "dead"}, ra
assert "far" not in {r["agent"] for r in ra}, ra  # a role on another host has no card here, so nothing to report
st = {r["agent"]: r["state"] for r in ra}
assert st["cl"] == "blocked" and st["dead"] == "blocked", st
md = [c["params"] for c in calls if c["method"] == "pane.report_metadata"]
assert all(m["source"] == "shoal" and m["ttl_ms"] > 0 for m in md) and md, md
assert any(m["tokens"].get("posture") == "QUESTION" for m in md), md
assert all("mode" in m["tokens"] for m in md if m["tokens"].get("role")), md  # why a role reads working
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
# a role pane whose foreground is a bare shell is not reported as an agent (Herdr clears such a report
# within a blink, so the role flashed into the agents list every tick); its tokens still go, and the card
# is reported again once something other than a shell runs in it
CLP=$(python3 - "$SHOALD" "$SID" <<'PY2'
import sys; sys.path.insert(0, sys.argv[1]); sys.path.insert(0, sys.argv[1] + "/ui")
import ui_herdr
h = ui_herdr.Herdr()
print(next(p["pane_id"] for p in h.panes() if str(p.get("label", "")).endswith("/cl")))
PY2
)
setfg() { python3 - "$SHOALD" "$CLP" "$1" <<'PY2'
import sys; sys.path.insert(0, sys.argv[1]); sys.path.insert(0, sys.argv[1] + "/ui")
import ui_herdr
ui_herdr.Herdr().call("test.set_fg", {"pane_id": sys.argv[2], "procs": [{"name": sys.argv[3], "argv": [sys.argv[3]]}]})
PY2
}
reports() { grep '"pane.report_agent"' "$S/herdr-calls.jsonl" | grep -c "\"agent\": \"cl\""; }
meta() { grep '"pane.report_metadata"' "$S/herdr-calls.jsonl" | grep -c '"role": "cl"'; }
setfg bash
R0=$(reports); M0=$(meta)
SHOAL_LEDGER_DIR="$ROOT" $H ui feed --once >/dev/null || fail "feed --once on a bare shell card"
[ "$(reports)" = "$R0" ] || fail "a bare shell card was reported as an agent"
[ "$(meta)" -gt "$M0" ] || fail "a bare shell card lost its tokens"
setfg python
SHOAL_LEDGER_DIR="$ROOT" $H ui feed --once >/dev/null || fail "feed --once on a live card"
[ "$(reports)" -gt "$R0" ] || fail "a live card was not reported as an agent"
echo "bare shell cards ok"
# state clarity: why a role reads working (mode glyph), attached-but-quiet Muse/Codex seats read idle, one card per label,
# and the legend says what the icons mean
python3 - "$SHOALD" <<'PY2' || fail "state clarity"
import json, os, sys, tempfile, time, types
sys.path.insert(0, sys.argv[1]); sys.path.insert(0, sys.argv[1] + "/ui")
import ui_herdr, muse, codex_harness, backends
role = lambda **k: dict({"session": "idle", "posture": "idle", "sub": "", "needs_you": False, "can_progress": {"reasons": []}}, **k)
assert ui_herdr.role_mode(role(session="working")) == ui_herdr.MODE_TURN
assert ui_herdr.role_mode(role(posture="wait", sub="remote-ci")) == ui_herdr.MODE_JOB
assert ui_herdr.herdr_state(role(posture="wait", sub="remote-ci")) == "working"
q = role(session="working", needs_you=True, can_progress={"reasons": ["blocked-on-question"]})
assert ui_herdr.role_mode(q) == "" and ui_herdr.herdr_state(q) == "blocked"
assert ui_herdr.role_mode(role()) == "" and ui_herdr.herdr_state(role()) == "idle"
# activity: an attached client with a journal that has not moved is quiet, a moving one active, an unreadable one unlabelled
d = tempfile.mkdtemp()
os.environ["SHOAL_MUSE_DATA"] = d
sid = "01a1aaaa-0000-7000-8000-000000000000"
os.makedirs(f"{d}/.msp-view-v1/{sid}")
j = f"{d}/.msp-view-v1/{sid}/journal-00000000.bin"
open(j, "w").write("x")
muse.busy = lambda s: True
muse.attached_client = lambda s: 4242
assert muse.status_line(sid).endswith("status:busy attached active"), muse.status_line(sid)
os.utime(j, (time.time() - 3600, time.time() - 3600))
assert muse.status_line(sid).endswith("status:busy attached quiet"), muse.status_line(sid)
os.remove(j)
assert muse.status_line(sid).endswith("status:busy attached"), muse.status_line(sid)
muse.attached_client = lambda s: 0
assert muse.status_line(sid).endswith("status:busy"), muse.status_line(sid)
# codex: the rollout file's age
cd = tempfile.mkdtemp()
os.environ["SHOAL_CODEX_HOME"] = cd
tid = "01a1bbbb-0000-7000-8000-000000000000"
os.makedirs(f"{cd}/sessions/2026/10/19")
rf = f"{cd}/sessions/2026/10/19/rollout-2026-10-19T00-00-00-{tid}.jsonl"
open(rf, "w").write("{}")
codex_harness.busy = lambda s: True
codex_harness.attached_client = lambda s: 99
assert codex_harness.status_line(tid).endswith("attached active")
os.utime(rf, (time.time() - 3600, time.time() - 3600))
assert codex_harness.status_line(tid).endswith("attached quiet")
# the standalone state follows those words
for line, want in (("x status:busy attached quiet", ("attached", "idle")), ("x status:busy attached active", ("working", "working")),
                   ("x status:busy", ("working", "working")), ("x status:idle", ("idle", "idle"))):
    backends.get = lambda harness, _l=line: types.SimpleNamespace(status=lambda *a, **k: _l)
    assert ui_herdr.standalone_state({"harness": "muse", "session": "s"}, []) == want, (line, want)
# one card per label: the later registration wins; pick --save replaces and says so
kept, dropped = ui_herdr.unique_by_label([{"harness": "codex", "session": "aaaa", "label": "seat"},
                                           {"harness": "claude", "session": "bbbb", "label": "seat"},
                                           {"harness": "muse", "session": "cccc", "label": "other"}])
assert [e["session"] for e in kept] == ["bbbb", "cccc"] and [e["session"] for e in dropped] == ["aaaa"]
before = ui_herdr.load_standalone(raw=True)
ns = lambda sess: types.SimpleNamespace(harness="claude", session_id=sess, label="dup-seat", cwd="", save=True, session="")
ui_herdr.cmd_pick(ns("dddd-1"))
ui_herdr.cmd_pick(ns("eeee-2"))
now = [e for e in ui_herdr.load_standalone(raw=True) if e.get("label") == "dup-seat"]
assert [e["session"] for e in now] == ["eeee-2"], now
ui_herdr.save_standalone(before)
# the legend and the sidebar row carry the mapping
lg = ui_herdr.legend_text(False)
assert "half-moon" in lg and ui_herdr.MODE_TURN in lg and ui_herdr.MODE_JOB in lg and "attached" in lg
assert "$mode" in ui_herdr.sidebar_block()
assert any(k == "legend" for k, _ in ui_herdr.MENU)
assert any(k == "grants" for k, _ in ui_herdr.MENU), "the palette manages standing grants"
print("state clarity ok")
PY2
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
# standalone naming: every harness gets a state from its own probe, so the feeder reports the
# session's label as its agent name (an unreported muse pane reads as the bare binary name)
os.environ["SHOAL_FAKE_SESSIONS"] = json.dumps({
    "claude": [{"sessionId": "c1a2b3c4-0000-1111", "pid": 4242, "state": "blocked", "waitingFor": "input"}],
    "muse": {"mu-busy": "mu-busy status:busy attached", "mu-idle": "mu-idle status:idle last:ok rc:0"},
    "opencode": {"ses_oc9": "ses_oc9 status:busy directory:/x"}})
rows = ui_status.claude_rows()
assert ui_herdr.standalone_state({"harness": "claude", "session": "c1a2b3c4-0000"}, rows) == ("QUESTION", "blocked")
assert ui_herdr.standalone_state({"harness": "muse", "session": "mu-busy"}, rows) == ("working", "working")
assert ui_herdr.standalone_state({"harness": "muse", "session": "mu-idle"}, rows) == ("idle", "idle")
assert ui_herdr.standalone_state({"harness": "opencode", "session": "ses_oc9"}, rows) == ("working", "working")
assert ui_herdr.standalone_state({"harness": "muse", "session": "mu-absent"}, rows) == ("unknown", "idle")
print("standalone state ok")
PY2

echo "== 3b standalone picker: local Codex/Muse lists, metadata, errors and popup lifetime"
python3 - "$SHOALD" "$S" <<'PY2' || fail "standalone picker"
import contextlib, io, json, os, pathlib, subprocess, sys, time, types
from unittest.mock import patch
sys.path[:0] = [sys.argv[1], sys.argv[1] + "/ui"]
import backends, ui_herdr, ui_sessions, ui_status, ui_term, config
scratch = pathlib.Path(sys.argv[2])
server = scratch / "session-list-server"
log = scratch / "session-list-methods"
server.write_text("#!" + sys.executable + "\n" + '''import json, os, sys
for line in sys.stdin:
    request = json.loads(line)
    method = request['method']
    with open(os.environ['PICKER_TEST_LOG'], 'a') as f: f.write(method + '\\n')
    assert method in ('initialize', 'initialized', 'thread/list', 'session/list'), method
    if 'id' not in request: continue
    if method == 'initialize':
        assert request['params']['clientInfo']['name'] == 'shoal'
        result = {}
    else:
        if method == 'thread/list':
            assert not os.environ.get('OPENAI_API_KEY')
            result = {'data': [{'id': 'cd-seat', 'name': 'codex-seat', 'cwd': '/tmp/example'},
                               {'id': 'role-id', 'name': 'worker-r2', 'cwd': '/tmp/example'}]}
        else:
            assert not os.environ.get('META_API_KEY')
            result = {'sessions': [{'sessionId': 'mu-seat', 'name': 'muse-seat', 'kind': 'root',
                                   'workspaceRoot': '/tmp/example', 'modelId': 'example-model'},
                                  {'sessionId': 'child', 'name': 'child', 'kind': 'subagent'}]}
    print(json.dumps({'method': 'notice', 'params': {}}), flush=True)
    print(json.dumps({'id': request['id'], 'result': result}), flush=True)
''')
server.chmod(0o755)
env = {'SHOAL_CODEX_BIN': str(server), 'SHOAL_MUSE_BIN': str(server), 'PICKER_TEST_LOG': str(log),
       'OPENAI_API_KEY': 'synthetic-token', 'META_API_KEY': 'synthetic-token'}
# Each harness inherits its normal subscription environment with its own inference key removed.
with patch.dict(os.environ, env):
    codex = ui_sessions.stored_sessions('codex')
    muse = ui_sessions.stored_sessions('muse')
assert codex[0]['session'] == 'cd-seat' and codex[0]['label'] == 'codex-seat'
assert [e['session'] for e in muse] == ['mu-seat'] and muse[0]['model'] == 'example-model'
assert set(log.read_text().splitlines()) == {'initialize', 'initialized', 'thread/list', 'session/list'}
# A failed provider cannot hide another provider's sessions.
claude = {'sessionId':'cl-seat', 'pid':123, 'id':'job-123', 'name':'claude-seat', 'state':'working'}
with patch.object(backends.ClaudeBackend, '_agents', return_value=[claude]), \
     patch.object(ui_herdr, 'role_slugs', return_value={'worker'}), \
     patch.object(ui_herdr.subprocess, 'run', return_value=types.SimpleNamespace(stdout='{}', returncode=0)), \
     patch.object(ui_sessions, 'stored_sessions', side_effect=lambda h: codex if h == 'codex' else muse):
    cands = ui_herdr.candidates_standalone({'role-id'})
assert [e['harness'] for e in cands] == ['claude', 'codex', 'muse'], cands
# A registered backend participates without a menu branch or a particular transport.
class ExampleACP(backends.Backend):
    ecosystem = 'example-acp'
    def sessions(self):
        return [{'session':'acp-seat', 'label':'ACP seat', 'state':'saved', 'cwd':'/tmp/example'}]
with patch.dict(backends.BACKENDS, {'example-acp':ExampleACP}), \
     patch.object(backends.ClaudeBackend, '_agents', return_value=[]), \
     patch.object(ui_herdr, 'role_slugs', return_value=set()), \
     patch.object(ui_herdr.subprocess, 'run', return_value=types.SimpleNamespace(stdout='{}', returncode=0)), \
     patch.object(ui_sessions, 'stored_sessions', return_value=[]):
    assert ui_herdr.candidates_standalone(set())[0]['harness'] == 'example-acp'
assert backends.Backend().sessions() == [], 'unsupported discovery does not break other backends'

with patch.object(backends.ClaudeBackend, '_agents', return_value=[]), \
     patch.object(ui_herdr, 'role_slugs', return_value=set()), \
     patch.object(ui_herdr.subprocess, 'run', return_value=types.SimpleNamespace(stdout='{}', returncode=0)), \
     patch.object(ui_sessions, 'stored_sessions', side_effect=[codex, RuntimeError('listing failed')]):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf): available = ui_herdr.candidates_standalone({'role-id'})
    assert available[0]['harness'] == 'codex' and 'muse sessions unavailable' in buf.getvalue()

ns = types.SimpleNamespace(harness='', session_id='', label='', save=True, session='', pause=True)
opened = []
with patch.object(ui_herdr, 'candidates_standalone', return_value=cands), \
     patch.object(ui_herdr, '_choose', return_value=0), \
     patch.object(ui_herdr, 'load_standalone', return_value=[]), \
     patch.object(ui_herdr, 'save_standalone'), patch.object(ui_herdr, 'Herdr'), \
     patch.object(ui_herdr, 'open_standalone', side_effect=lambda h,e,**kw: opened.append(e) or 'opened'), \
     patch.object(ui_term, 'pause') as pause:
    assert ui_herdr.cmd_pick(ns) == 0
    assert opened[0]['job'] == 'job-123', 'interactive picks preserve the stable Claude job id'
    pause.assert_not_called()
    with patch.object(ui_herdr, '_choose', return_value=2):
        assert ui_herdr.cmd_pick(ns) == 0 and opened[-1]['model'] == 'example-model'
    with patch.object(ui_herdr, '_choose', return_value=None):
        assert ui_herdr.cmd_pick(ns) == 0
    pause.assert_not_called()
    with patch.object(ui_herdr, 'open_standalone', side_effect=ui_herdr.HerdrError('server unavailable')):
        assert ui_herdr.cmd_pick(ns) == 1
    pause.assert_called_once()
with patch.object(ui_herdr, 'candidates_standalone', return_value=[]), patch.object(ui_term, 'pause') as pause:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf): assert ui_herdr.cmd_pick(ns) == 1
    assert 'Codex, Muse' in buf.getvalue() and '--harness HARNESS --id ID' in buf.getvalue()
    pause.assert_called_once()
with patch.object(ui_herdr, 'focused_role', return_value=('', '')), \
     patch.object(ui_herdr, '_choose', return_value=next(i for i,x in enumerate(ui_herdr.MENU) if x[0]=='pick')), \
     patch.object(ui_herdr.subprocess, 'run', return_value=types.SimpleNamespace(returncode=0)) as run:
    assert ui_herdr.cmd_menu(types.SimpleNamespace()) == 0
    assert run.call_args.args[0][-3:] == ['ui', 'pick', '--pause'], 'option 12 keeps failures visible'
start = time.monotonic()
try:
    ui_sessions._list([sys.executable, '-c', 'import time; time.sleep(60)'], config.tool_env(), 'thread/list', {}, timeout=0.05)
except TimeoutError:
    pass
else:
    raise AssertionError('listing timeout must be bounded')
assert time.monotonic()-start < 4, 'timed-out listing server is reaped promptly'
print('standalone picker ok')
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

# doctor reads the config file this Herdr reads (HERDR_CONFIG_PATH), and an integration is a Claude HOOK:
# a permission rule that merely names a herdr script is not one
mkdir -p "$S/dochome/.claude"
printf '{"permissions": {"allow": ["Bash(/x/shoal-herdr-tmux.sh:*)"]}, "enabledPlugins": {"herdr-helper@x": true}}' > "$S/dochome/.claude/settings.json"
HOME="$S/dochome" HERDR_CONFIG_PATH="$S/server.toml" python3 - "$SHOALD" "$S/server.toml" <<'PY2' || fail "doctor config path and integrations"
import os, sys; sys.path.insert(0, sys.argv[1]); sys.path.insert(0, sys.argv[1] + "/ui")
import ui_herdr
def checks():
    return {n: (ok, d) for n, ok, d in ui_herdr.doctor_checks("doctorsess")}
c = checks()
assert c["agent resume on restore is off"] == (True, sys.argv[2]), c["agent resume on restore is off"]
assert c["no herdr agent integrations on this lane host"][0], c["no herdr agent integrations on this lane host"]
open(os.path.expanduser("~/.claude/settings.json"), "w").write('{"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "herdr agent report"}]}]}}')
c = checks()
assert not c["no herdr agent integrations on this lane host"][0], c
os.environ["HERDR_CONFIG_PATH"] = os.path.join(os.path.dirname(sys.argv[2]), "absent.toml")
assert not checks()["agent resume on restore is off"][0]
print("doctor config path and integrations ok")
PY2

rm -rf /tmp/uiselftest
echo "== 5 local view provisioning (herdr-config --local, herdr-install --local) and the Herdr pin (herdr-get)"
# the local file: the user's own settings plus ONE merged block (server and client), idempotent, valid TOML
mkdir -p "$S/xdg-local/herdr"
printf '[theme]\nname = "nord"\n' > "$S/xdg-local/herdr/config.toml"
LC="$S/xdg-local/herdr/shoal-local.toml"
XDG_CONFIG_HOME="$S/xdg-local" SHOAL_LOCAL_HERDR_CONF="$LC" $H ui herdr-config --local --write >/dev/null || fail "local write"
cp "$LC" "$S/local-1.toml"
check "is current" env XDG_CONFIG_HOME="$S/xdg-local" SHOAL_LOCAL_HERDR_CONF="$LC" $H ui herdr-config --local --write
cmp -s "$LC" "$S/local-1.toml" || fail "local config not idempotent"
[ "$(grep -c 'shoal-ui managed block (shoal' "$LC")" = 1 ] || fail "local block duplicated"
grep -q 'name = "nord"' "$LC" || fail "user settings lost in the local file"
python3 - "$LC" <<'PY2' || fail "local config"
import sys
try:
    import tomllib as t
except ImportError:
    try:
        import tomli as t
    except ImportError:
        print("toml check SKIP (no reader)"); sys.exit(0)
d = t.loads(open(sys.argv[1]).read())
assert d["session"]["resume_agents_on_restore"] is False, "the server half (restore off) is in the local file"
assert "tab_bar_right" in d["ui"] and "sidebar" in d["ui"], "[ui] carries the server's status line and the client's sidebar"
print("local config ok")
PY2
# units carry HERDR_CONFIG_PATH; --dry-run reports and touches nothing
python3 - "$SHOALD" "$LC" <<'PY2' || fail "local units"
import sys; sys.path.insert(0, sys.argv[1]); sys.path.insert(0, sys.argv[1] + "/ui")
import ui_herdr
for k in ("server", "feed"):
    t = ui_herdr.unit_text(k, sys.argv[2])
    assert f"Environment=HERDR_CONFIG_PATH={sys.argv[2]}" in t, t
assert "HERDR_CONFIG_PATH" not in ui_herdr.unit_text("server"), "a lane host's unit reads config.toml"
path = next(l for l in ui_herdr.unit_text("server").splitlines() if l.startswith("Environment=PATH="))[len("Environment=PATH="):]
assert len(path.split(":")) == len(set(path.split(":"))), f"duplicate PATH entries: {path}"
# doctor names where the plugin is really linked, and says when that is another checkout
listed = f"1 plugin installed:\n- shoal.herdr (shoal) enabled [local:{ui_herdr.PLUGIN_DIR}]\n"
assert ui_herdr.plugin_link_state(listed) == (True, ui_herdr.PLUGIN_DIR), ui_herdr.plugin_link_state(listed)
ok, why = ui_herdr.plugin_link_state("1 plugin installed:\n- shoal.herdr (shoal) enabled [local:/elsewhere/plugins/shoal-herdr]\n")
assert ok and "/elsewhere/plugins/shoal-herdr" in why and "this code is" in why, why
assert ui_herdr.plugin_link_state("0 plugins installed")[0] is False
print("local units ok")
PY2
printf '#!/bin/sh\necho "herdr ${FAKE_HERDR_VERSION:-0.9.3}"\n' > "$S/fake-herdr"; chmod +x "$S/fake-herdr"
UNITS0=$(ls ~/.config/systemd/user 2>/dev/null | md5sum)
OUT=$(XDG_CONFIG_HOME="$S/xdg-local" SHOAL_LOCAL_HERDR_CONF="$S/xdg-local/herdr/never.toml" SHOAL_HERDR_BIN="$S/fake-herdr" \
      $H ui herdr-install --local --dry-run) || fail "install --local --dry-run rc :: $OUT"
grep -q "\[dry-run\] local config .*never.toml: write" <<<"$OUT" || fail "dry-run plan :: $OUT"
[ ! -e "$S/xdg-local/herdr/never.toml" ] || fail "--dry-run wrote the local config"
[ "$(ls ~/.config/systemd/user 2>/dev/null | md5sum)" = "$UNITS0" ] || fail "--dry-run touched the user units"
# the pin: --check against a fake install (no network)
mkdir -p "$S/hprefix/bin"; cp "$S/fake-herdr" "$S/hprefix/bin/herdr"
check "installed=0.9.3 pin=0.9.3" env HERDR_PREFIX="$S/hprefix" $H ui herdr-get --check
if env HERDR_PREFIX="$S/hprefix" FAKE_HERDR_VERSION=0.9.2 $H ui herdr-get --check >/dev/null 2>&1; then fail "an off-pin herdr passed --check"; fi
python3 - "$SHOALD" <<'PY2' || fail "pin matches the plugin"
import re, sys; sys.path.insert(0, sys.argv[1] + "/ui")
import herdr_pin
m = re.search(r'min_herdr_version = "([^"]+)"', open(sys.argv[1] + "/../plugins/shoal-herdr/herdr-plugin.toml").read())
assert m and m.group(1) == herdr_pin.PIN, (m and m.group(1), herdr_pin.PIN)
print("pin ok")
PY2

echo "== 6 ui launch: the chooser from routes, both modes, fail fast on an unreachable host, never inside Herdr"
printf '#!/bin/sh\necho "herdr $*" >> "${STUB_HERDR_LOG:-/dev/null}"\n' > "$S/herdr-stub"; chmod +x "$S/herdr-stub"
cp "$SHOAL_CONFIG" "$S/host-launch.toml"
printf '\n[hosts."other-host.invalid"]\nssh = "u@other-host.invalid"\nresolve = "dns"\n' >> "$S/host-launch.toml"
LENV="SHOAL_CONFIG=$S/host-launch.toml SHOAL_SSH=$S/ssh-stub SHOAL_HERDR_BIN=$S/herdr-stub XDG_STATE_HOME=$S/state XDG_CONFIG_HOME=$S/xdg-launch"
check "not inside the Herdr viewer" env $LENV HERDR_SESSION=shoal $H ui launch --server local --dry-run
OUT=$(env -u HERDR_SOCKET_PATH -u HERDR_SESSION $LENV $H ui launch --server other-host.invalid --dry-run) || fail "launch remote dry-run :: $OUT"
grep -q "u@other-host.invalid 'bash -lc" <<<"$OUT" || fail "remote preparation over the route :: $OUT"
grep -q "exec $S/herdr-stub --remote u@other-host.invalid --session shoal --remote-keybindings server" <<<"$OUT" || fail "remote client argv :: $OUT"
OUT=$(env -u HERDR_SOCKET_PATH -u HERDR_SESSION $LENV $H ui launch --server local --dry-run) || fail "launch local dry-run :: $OUT"
grep -q "exec $S/herdr-stub --session shoal   (config $S/xdg-launch/herdr/shoal-local.toml)" <<<"$OUT" \
  || fail "a host with no shoal-herdr unit is provisioned for the local view :: $OUT"
[ ! -e "$S/state/shoal/launch.last" ] || fail "a dry run remembered a choice"
# a lane host (its unit reads config.toml): the server is left as it is; its own client reads the merged local file
mkdir -p "$S/xdg-lane/systemd/user" && printf '[Service]\nEnvironment=HERDR_SESSION=shoal\n' > "$S/xdg-lane/systemd/user/shoal-herdr.service"
OUT=$(env -u HERDR_SOCKET_PATH -u HERDR_SESSION $LENV XDG_CONFIG_HOME="$S/xdg-lane" $H ui launch --server local --dry-run) || fail "lane local dry-run :: $OUT"
grep -q "exec $S/herdr-stub --session shoal   (config $S/xdg-lane/herdr/shoal-local.toml)" <<<"$OUT" || fail "lane host client config :: $OUT"
[ ! -e "$S/xdg-lane/herdr/shoal-local.toml" ] || fail "a dry run wrote the lane client's config"
# unreachable: fails within the ssh timeout, says so with the doctor hint, never runs the client
rm -rf "$SHOAL_ROUTE_CACHE"
OUT=$(env -u HERDR_SOCKET_PATH -u HERDR_SESSION $LENV STUB_SSH_RC=255 STUB_HERDR_LOG="$S/herdr.log" \
      $H ui launch --server other-host.invalid 2>&1); RC=$?
[ "$RC" = 2 ] && grep -q "unreachable now .*shoal doctor" <<<"$OUT" || fail "unreachable host: rc $RC :: $OUT"
[ ! -s "$S/herdr.log" ] || fail "a client was attached to an unreachable host :: $(cat "$S/herdr.log")"
[ ! -e "$S/state/shoal/launch.last" ] || fail "a host that just failed became the remembered choice"
# attach-only probes too: no Herdr ssh error, the doctor hint instead
OUT=$(env -u HERDR_SOCKET_PATH -u HERDR_SESSION $LENV STUB_SSH_RC=255 STUB_HERDR_LOG="$S/herdr.log" \
      $H ui launch --server other-host.invalid --no-open 2>&1); RC=$?
[ "$RC" = 2 ] && grep -q "unreachable now .*shoal doctor" <<<"$OUT" && [ ! -s "$S/herdr.log" ] || fail "--no-open, unreachable: rc $RC :: $OUT"
# reachable: the client runs (the stub records it) and the choice is remembered
env -u HERDR_SOCKET_PATH -u HERDR_SESSION $LENV STUB_SSH_RC=0 STUB_HERDR_LOG="$S/herdr.log" \
      $H ui launch --server other-host.invalid --no-open >/dev/null 2>&1 || fail "reachable attach-only launch"
grep -q -- "--remote u@other-host.invalid --session shoal" "$S/herdr.log" || fail "the client did not run :: $(cat "$S/herdr.log")"
grep -qx "other-host.invalid" "$S/state/shoal/launch.last" || fail "a choice that worked was not remembered"
env $LENV python3 - "$SHOALD" "$S" <<'PY2' || fail "chooser"
import io, os, sys, contextlib; sys.path.insert(0, sys.argv[1]); sys.path.insert(0, sys.argv[1] + "/ui")
import routes, ui_launch
assert ui_launch.candidates() == ["local", "other-host.invalid"], ui_launch.candidates()
routes._store("reach-other-host.invalid", {"ok": False})
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    got = ui_launch.choose(read=lambda p: "")
import re
assert got == "other-host.invalid" and re.search(r"> remote +other-host\.invalid  \(unreachable now\) +2", buf.getvalue()), buf.getvalue()
# Original 36x13 ASCII outlines fit as a whole composition, or are omitted.
rows = [("local", "here, this host", False), ("remote", "other-host.invalid", True)]
assert len({len(r) for r in ui_launch._HEADER}) == 1
plain = ui_launch.dashboard(rows, 2, cols=200, lines=50, color=False, uni=True)
assert "███████╗" in plain and "\x1b[" not in plain and "▸ remote  other-host.invalid  (unreachable now)" in plain, plain
assert all(len(row) <= 200 for row in plain.split("\n"))
for cols, lines in ((72, 23), (90, 30), (160, 50)):
    picture = ui_launch.dashboard(rows, 1, cols=cols, lines=lines, color=False, uni=True).split("\n")
    assert len(picture) == lines and all(len(row) <= cols for row in picture)
    first = next(i for i, row in enumerate(picture) if row.strip())
    last = max(i for i, row in enumerate(picture) if row.strip())
    assert abs(first - (lines - 1 - last)) <= 1
    if cols < ui_launch._ASCII_COLS or lines < 40:
        assert not any(".---." in row for row in picture), "no tiny or partial fish"
for cols, lines in ((100, 30), (80, 24), (60, 20)):
    page = ui_launch.dashboard(rows, 1, cols=cols, lines=lines, color=False, uni=True)
    for text in ("which host serves the view?", "here, this host", "other-host.invalid", "stigmergic"):
        assert text in page, (cols, lines, text)
colored_full = ui_launch.dashboard(rows, 1, cols=160, lines=50, color=True, uni=True)
assert set(ui_launch._FISH_COLORS) <= set(re.findall(r"\x1b\[([\d;]+)m", colored_full))
full = ui_launch.dashboard(rows, 1, cols=120, lines=40, color=False, uni=True)
mw, mh = ui_launch.min_size(2)
assert (mw, mh) == (72, 23), "compact minimum, optional art never forces a giant window"
from unittest.mock import patch
with patch.object(ui_launch.os, "get_terminal_size", return_value=os.terminal_size((90, 30))), \
     patch.dict(os.environ, {"COLUMNS": "200", "LINES": "50"}):
    assert ui_launch.terminal_size() == (90, 30), "tty dimensions override stale inherited environment"
assert "\n" not in ui_launch._frame(full), "raw tty rendering never relies on newline conversion"
assert "\x1b[40;1H\x1b[2K" in ui_launch._frame(full), "each row is positioned and cleared explicitly"
feed = iter(["", "", "\x1b", "[", "B", "\r"])
assert ui_launch._choose_keys(["local", "other"], rows, 1, lambda: next(feed), lambda s: None, lambda: "") == "other", "poll timeouts redraw, never cancel"
drawn = []
feed = iter("\x1b[B\r")
assert ui_launch._choose_keys(["local", "other"], rows, 1, lambda: next(feed), drawn.append, lambda: "") == "other"
assert drawn == [1, 2], drawn
feed = iter("jkq")
assert ui_launch._choose_keys(["local", "other"], rows, 1, lambda: next(feed), lambda s: None, lambda: "") is None
feed = iter("t")
assert ui_launch._choose_keys(["local", "other"], rows, 1, lambda: next(feed), lambda s: None, lambda: "me@typed.example") == "me@typed.example"
feed = iter("2")
assert ui_launch._choose_keys(["local", "other"], rows, 1, lambda: next(feed), lambda s: None, lambda: "") == "other"
colored = ui_launch.dashboard(rows, 1, cols=100, color=True, uni=True)
assert len(set(re.findall(r"\x1b\[([\d;]+)m", colored))) >= 8, "each fish its own color, the header shaded"
narrow = ui_launch.dashboard(rows, 1, cols=40, color=False, uni=True)
assert "S H O A L" in narrow and "███" not in narrow and "_..-" not in narrow, narrow
assert all(len(l) <= 40 for l in narrow.splitlines()), [len(l) for l in narrow.splitlines()]
plain_ascii = ui_launch.dashboard(rows, 1, cols=100, color=False, uni=False)
assert plain_ascii.isascii() and "> local" in plain_ascii, "a stream that cannot encode box drawing gets ASCII"
with contextlib.redirect_stdout(io.StringIO()):
    assert ui_launch.choose(read=lambda p: "1") == "local"
    assert ui_launch.choose(read=lambda p: "me@typed.example") == "me@typed.example"
    assert ui_launch.choose(read=lambda p: "q") is None
assert ui_launch.is_local("local") and not ui_launch.is_local("other-host.invalid")
# a percent sidebar width becomes this window's columns
f = os.path.join(sys.argv[2], "sidebar.toml")
open(f, "w").write("[ui]\n# shoal-sidebar-max: 25%\nsidebar_max_width = 40\n")
ui_launch.resolve_sidebar(f, cols=200)
assert "sidebar_max_width = 50" in open(f).read(), open(f).read()
ui_launch.resolve_sidebar(f, cols=40)
assert "sidebar_max_width = 18" in open(f).read(), "floor of 18 columns"
# A real PTY reproduces inherited dimensions and disabled output newline conversion.
# Resize while selected, shrink below minimum, recover, then cancel an empty typed target.
import fcntl, pty, select, struct, subprocess, termios, time
master, slave = pty.openpty()
attrs = termios.tcgetattr(slave)
attrs[1] &= ~termios.OPOST
termios.tcsetattr(slave, termios.TCSANOW, attrs)
def resize(cols, lines):
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", lines, cols, 0, 0))
resize(80, 24)
child = """import sys
sys.path.insert(0, sys.argv[1])
import ui_launch
rows = [('local', 'this host', False), ('remote', 'other-host.invalid', False)]
print('RESULT', ui_launch._choose_interactive(['local', 'other'], rows, 1), flush=True)
"""
env = dict(os.environ, COLUMNS="200", LINES="60")
env.pop("NO_COLOR", None)
p = subprocess.Popen([sys.executable, "-c", child, os.path.dirname(ui_launch.__file__)],
                     stdin=slave, stdout=slave, stderr=slave, env=env)
def read_until(marker):
    output = b""
    deadline = time.monotonic() + 8
    while marker not in output and time.monotonic() < deadline:
        if select.select([master], [], [], 0.1)[0]:
            output += os.read(master, 65536)
    assert marker in output, (marker, output[-2000:])
    return output
try:
    initial = read_until(b"\x1b[J")
    assert b"which host serves" in initial and b"Enlarge" not in initial
    assert b"\x1b[24;1H\x1b[2K" in initial and b"\n" not in initial
    os.write(master, b"j")
    selected_frame = read_until(b"\x1b[J")
    assert b"\x1b[1;7mremote" in selected_frame, "selection changes in the PTY"
    resize(32, 8)
    small = read_until(b"\x1b[J")
    assert b"Enlarge window to 72x23" in small and b"which host serves" not in small
    assert small.count(b"\x1b[8;23;72t") == 1, "one resize request, no loop"
    resize(110, 36)
    recovered = read_until(b"\x1b[J")
    assert b"\x1b[36;1H\x1b[2K" in recovered and b"\x1b[1;7mremote" in recovered
    os.write(master, b"t")
    read_until(b"ssh target: ")
    os.write(master, b"\n")
    assert b"which host serves" in read_until(b"\x1b[J"), "empty target repaints the overwritten frame"
    os.write(master, b"q")
    final = read_until(b"RESULT None")
    assert b"\x1b[?7h" in final and b"\x1b[?1049l" in final
    assert p.wait(timeout=5) == 0
    restored = termios.tcgetattr(slave)
    assert restored == attrs, "chooser restores the tty settings it inherited"
finally:
    if p.poll() is None:
        p.terminate()
        p.wait(timeout=5)
    os.close(master)
    os.close(slave)
print("launch chooser ok")
PY2

python3 "$TESTD/test_splash_graphics.py" || fail "splash graphics"

echo "== 6b repo skills: the .agents/skills vendored copies match the plugin masters"
REPO_ROOT=$(cd "$SHOALD/.." && pwd)
for s in shoal-protocol shoal-operator shoal-health-review; do
  diff -rq "$REPO_ROOT/plugins/shoal-skills/skills/$s" "$REPO_ROOT/.agents/skills/$s" >/dev/null \
    || fail ".agents/skills/$s drifted from plugins/ (edit under plugins/, then copy over)"
done
echo "repo skills ok"

echo "== 7 Claude identity: a /clear changes the conversation id, never the job; a retired worker is still viewable"
python3 - "$SHOALD" "$S" <<'PY2' || fail "claude identity"
import io, json, os, sys, contextlib; sys.path.insert(0, sys.argv[1]); sys.path.insert(0, sys.argv[1] + "/ui")
# job abcd1234 was started as abcd1234-0000 and has been /clear'ed to ffff0000-1111; job eeee5555 has no worker
os.environ["SHOAL_FAKE_SESSIONS"] = json.dumps({"claude": [
    {"id": "abcd1234", "sessionId": "ffff0000-1111", "originalSessionId": "abcd1234-0000", "pid": 9, "status": "idle"},
    {"id": "eeee5555", "sessionId": "eeee5555-2222", "state": "done"}], "opencode": {}})
import backends, health, ui_herdr, ui_status, ui_view
rows = ui_status.claude_rows()
for stored in ("abcd1234-0000", "ffff0000-1111", "abcd1234"):
    assert (ui_status.claude_session(rows, stored) or {}).get("id") == "abcd1234", stored
assert (ui_status.claude_session(rows, "zzzz9999-0000", "abcd1234") or {}).get("id") == "abcd1234", "an explicit job wins"
assert ui_status.claude_session(rows, "zzzz9999-0000") is None, "never a guess"
busy = health.claude_busy()
assert {"ffff0000-1111", "abcd1234", "abcd1234-0000"} <= set(busy), sorted(busy)
assert backends.get("claude")._row("abcd1234-0000")["id"] == "abcd1234"
# a session with no worker is viewable (attach wakes it, no turn); one not listed is refused
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    assert ui_view.verify_listed("claude", "eeee5555-2222") == ""
assert "attaching wakes its worker; no turn is taken" in buf.getvalue(), buf.getvalue()
assert "not listed" in ui_view.verify_listed("claude", "zzzz9999-0000")
assert ui_view.viewer("claude", "abcd1234")[0][-2:] == ["attach", "abcd1234"]
# the registry learns each Claude entry's job id while its stored id still resolves
entries = [{"harness": "claude", "session": "ffff0000-1111", "label": "seat"}, {"harness": "opencode", "session": "ses_x"}]
assert ui_herdr.record_claude_jobs(entries) and entries[0]["job"] == "abcd1234" and "job" not in entries[1], entries
assert ui_herdr.standalone_argv(entries[0])[-2:] == ["--job", "abcd1234"], ui_herdr.standalone_argv(entries[0])
print("claude identity ok")
PY2
# Claude Code updated while attached: the client's binary is gone, its reconnect fails; one fresh reattach follows
python3 - "$SHOALD" "$S" <<'PY2' || fail "reattach after update"
import io, os, sys, contextlib; sys.path.insert(0, sys.argv[1]); sys.path.insert(0, sys.argv[1] + "/ui")
import ui_view
d = sys.argv[2]; old = os.path.join(d, "claude-old"); new = os.path.join(d, "claude-new"); log = os.path.join(d, "attach.log")
for p, rc in ((old, 1), (new, 0)):
    open(p, "w").write(f"#!/bin/sh\necho $0 >> {log}\nexit {rc}\n"); os.chmod(p, 0o755)
calls = iter([old, new])
ui_view.verify_listed = lambda *a, **k: ""
def viewer(h, s, label="", model="", effort=""):
    return [next(calls), "attach", s], dict(os.environ)
ui_view.viewer = viewer
orig_run = ui_view.subprocess.run
def run(argv, env=None):
    r = orig_run(argv, env=env)
    if argv[0] == old:
        os.remove(old)  # simulate the update landing during the attach
    return r
ui_view.subprocess.run = run
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    rc = ui_view.run_viewer("claude", "abcd1234", "seat")
assert rc == 0 and "updated while attached" in buf.getvalue(), buf.getvalue()
assert open(log).read().split() == [old, new], open(log).read()
print("reattach after update ok")
PY2

echo "== 8 handover for standalone sessions, stale-worker upkeep, reattach after a service restart"
python3 - "$SHOALD" "$S" <<'PY2' || fail "handover"
import datetime, io, json, os, sys, contextlib; sys.path.insert(0, sys.argv[1]); sys.path.insert(0, sys.argv[1] + "/ui")
S = sys.argv[2]
os.environ["XDG_CONFIG_HOME"] = os.path.join(S, "xdg-handover")
os.environ["SHOAL_FAKE_SESSIONS"] = json.dumps({"claude": [
    {"id": "seat0001", "sessionId": "seat0001-aaaa", "pid": 7, "tempo": "idle", "inFlight": {}, "name": "seat"}], "opencode": {}})
os.environ["SHOAL_FAKE_CONTEXT"] = json.dumps({"seat": 620000})
import handover, startdoc, ui_herdr
doc = os.path.join(S, "seat_plan_state.md")
open(doc, "w").write("# seat\n\n## Mission\n\nm\n\n## Current state\n\nLast updated: 2020-01-01T00:00Z\n\n## Next\n\n- n\n\n## grants-needed\n\n- None pending\n")
ui_herdr.save_standalone([{"harness": "claude", "session": "seat0001-aaaa", "job": "seat0001", "label": "seat"}])
# opting in above the limit advises at once (one direct delivery)
A = type("A", (), {"target": "seat", "doc": doc, "mode": "auto", "k": 0, "list": False, "dry_run": False})()
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    assert handover.cmd_handover(A) == 0
assert "advised (620k past 500k)" in buf.getvalue(), buf.getvalue()
e = ui_herdr.load_standalone(raw=True)[0]
assert e["handover_state"]["step"] == "advised" and e["doc"] == doc, e
# a doc not refreshed since the advice: no reset
assert handover.tick() == [] and ui_herdr.load_standalone(raw=True)[0]["handover_state"]["step"] == "advised"
# refreshed: reset onto it; the registry follows the new job
open(doc, "a").write("\nLast updated: %s\n" % datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
notes = handover.tick()
assert any("handed over: fake new job fe5e7000" in n for n in notes), notes
e = ui_herdr.load_standalone(raw=True)[0]
assert e["job"] == "fe5e7000" and e["handover_state"]["step"] == "reset" and e["handover_state"]["from"] == "seat0001", e
# the new session under its limit: complete
os.environ["SHOAL_FAKE_CONTEXT"] = json.dumps({"seat": 40000})
assert any("handover complete" in n for n in handover.tick())
assert "handover_state" not in ui_herdr.load_standalone(raw=True)[0]
# mode off clears it; advise mode never resets
A.mode = "off"
with contextlib.redirect_stdout(io.StringIO()):
    handover.cmd_handover(A)
assert ui_herdr.load_standalone(raw=True)[0]["handover"] == "off"
# a stop+resume of a job whose id is not its conversation's prefix leaves two jobs on one conversation: the delivery
# follows the live copy, and the reset stops both (2026-10-24: b193724a stopped, 4decf3de live beside the new session)
os.environ["SHOAL_FAKE_SESSIONS"] = json.dumps({"claude": [
    {"id": "old00001", "sessionId": "conv0001-aaaa", "name": "seat2", "startedAt": 1},
    {"id": "conv0001", "sessionId": "conv0001-aaaa", "pid": 9, "tempo": "idle", "inFlight": {}, "name": "seat2-state",
     "startedAt": 2}], "opencode": {}})
import backends
rows = backends.get("claude")._agents(include_stopped=True)
assert [r["id"] for r in backends.claude_holders(rows, "conv0001-aaaa", "old00001")] == ["conv0001", "old00001"]
e2 = {"harness": "claude", "session": "conv0001-aaaa", "job": "old00001", "label": "seat2"}
assert handover.follow(e2) and e2["job"] == "conv0001", e2
out = backends.get("claude").reset("conv0001-aaaa", "resume", label="seat2")
assert "stopped conv0001, old00001" in out["did"], out
# retire: a worker that comes back after its stop is stopped once more; one that stays up is reported
cb = backends.get("claude"); seen = {"stops": []}
live = {"old00001": [None, 5, None], "conv0001": [None, 6, 6, 6]}   # pid per _agents call after the stops
def fake_agents(include_stopped=False):
    return [{"id": j, "pid": (v.pop(0) if len(v) > 1 else v[0])} for j, v in live.items()]
cb.stop, cb._agents = (lambda j: seen["stops"].append(j) or ""), fake_agents
assert cb.retire(["old00001", "conv0001"], verify_s=5) == ["conv0001"]
assert seen["stops"].count("old00001") == 2 and seen["stops"].count("conv0001") == 2, seen
del cb.stop, cb._agents
# a standalone card shows its registry entry's ids, not the ones on its command line
import ui_view
ui_herdr.save_standalone([{"harness": "claude", "session": "fe5e7000-0000", "job": "fe5e7000", "label": "seat"}])
assert ui_view.registry_follow("claude", "seat0001-aaaa", "seat", "seat0001") == ("fe5e7000-0000", "fe5e7000")
assert ui_view.registry_follow("claude", "zzz", "nobody", "z") == ("zzz", "z")
print("handover ok")
PY2
python3 - "$SHOALD" "$SPEC" <<'PY2' || fail "stale upkeep"
import json, os, sys; sys.path.insert(0, sys.argv[1]); sys.path.insert(0, sys.argv[1] + "/ui")
os.environ["SHOAL_FAKE_SESSIONS"] = json.dumps({"claude": [
    {"id": "c1a2b3c4", "sessionId": "c1a2b3c4-0000-1111", "pid": 4242, "tempo": "idle", "inFlight": {}},
    {"id": "farfar00", "sessionId": "farfar00", "pid": 9, "tempo": "active", "inFlight": {}}], "opencode": {}})
os.environ["SHOAL_FAKE_ROSTER"] = json.dumps({"c1a2b3c4": {"cliVersion": "2.1.292"}})
os.environ["SHOAL_FAKE_CLAUDE_VERSION"] = "2.1.308"
import health
from roles import ShoalSpec
spec = ShoalSpec.load(sys.argv[2]); acts, state = [], {}
health.stale_worker_upkeep(spec, acts, False, state)
assert any(a[0] == "cl" and "runs Claude 2.1.292, installed 2.1.308" in a[2] and "no turn" in a[2] for a in acts), acts
os.environ["SHOAL_FAKE_ROSTER"] = json.dumps({"c1a2b3c4": {"cliVersion": "2.1.308"}})
acts = []; health.stale_worker_upkeep(spec, acts, False, state)
assert not acts, "a current worker is left alone"
print("stale upkeep ok")
PY2
python3 - "$SHOALD" "$S" <<'PY2' || fail "reattach after a service restart"
import io, os, sys, contextlib; sys.path.insert(0, sys.argv[1]); sys.path.insert(0, sys.argv[1] + "/ui")
d = os.path.join(sys.argv[2], "cc-restart"); os.makedirs(d, exist_ok=True); os.environ["CLAUDE_CONFIG_DIR"] = d
log = os.path.join(d, "daemon.log"); open(log, "w").write("")
first, second, calls = os.path.join(d, "a1"), os.path.join(d, "a2"), os.path.join(d, "calls")
open(first, "w").write("#!/bin/sh\necho a1 >> %s\nsleep 1\necho \"[$(date -u +%%Y-%%m-%%dT%%H:%%M:%%S.000Z)] [supervisor] ─── daemon start ─── version=x\" >> %s\nexit 0\n" % (calls, log))
open(second, "w").write("#!/bin/sh\necho a2 >> %s\nexit 0\n" % calls)
for p in (first, second): os.chmod(p, 0o755)
import ui_view
seq = iter([first, second])
ui_view.verify_listed = lambda *a, **k: ""
ui_view.viewer = lambda h, s, label="", model="", effort="": ([next(seq), "attach", s], dict(os.environ))
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    rc = ui_view.run_viewer("claude", "abcd1234", "seat")
assert rc == 0 and "background service restarted" in buf.getvalue(), buf.getvalue()
assert open(calls).read().split() == ["a1", "a2"], open(calls).read()
print("reattach after a service restart ok")
PY2

echo "== 9 context-report: cost per session from transcripts, warm vs cold, payback, one snapshot per host and day"
python3 - "$SHOALD" "$S" <<'PY2' || fail "context-report"
import datetime as dt, json, os, sys; sys.path.insert(0, sys.argv[1])
S = sys.argv[2]; cc = os.path.join(S, "cc-cost"); os.environ["CLAUDE_CONFIG_DIR"] = cc
os.environ["SHOAL_DATA_ROOT"] = os.path.join(S, "data-cost")
os.makedirs(os.path.join(cc, "projects", "p"), exist_ok=True); os.makedirs(os.path.join(cc, "jobs", "abcd1234"), exist_ok=True)
json.dump({"sessionId": "abcd1234-0000", "respawnFlags": ["-n", "busy-seat"]}, open(os.path.join(cc, "jobs", "abcd1234", "state.json"), "w"))
now = dt.datetime.now(dt.timezone.utc)
def call(t, read, w1h, out=100):
    return json.dumps({"type": "assistant", "timestamp": t.strftime("%Y-%m-%dT%H:%M:%S.000Z"), "message": {"model": "claude-opus-5-5",
        "usage": {"input_tokens": 2, "cache_read_input_tokens": read, "cache_creation_input_tokens": w1h, "output_tokens": out,
                  "cache_creation": {"ephemeral_1h_input_tokens": w1h, "ephemeral_5m_input_tokens": 0}}}})
lines = [call(now - dt.timedelta(hours=5), 0, 400000)]                       # cold: the whole context written
lines += [call(now - dt.timedelta(hours=3, minutes=i), 400000, 1000) for i in range(9)]   # warm, after a 2 h gap
open(os.path.join(cc, "projects", "p", "abcd1234-0000.jsonl"), "w").write("\n".join(lines) + "\n")
os.environ["SHOAL_FAKE_SESSIONS"] = json.dumps({"claude": [], "opencode": {}})
import context_cost
doc = context_cost.report(1.0)
r = next(x for x in doc["sessions"] if x["id"] == "abcd1234-0000")
assert r["name"] == "busy-seat" and r["calls"] == 10 and r["cold_calls"] == 1 and r["cold_wakes"] == 1, r
assert r["context_now_k"] == 401 and r["share"]["cold_writes"] > 0.4, r
assert r["payback"]["warm_calls"] > 0 and r["payback"]["cold_wakes"] < 1, r["payback"]
# one snapshot per host and day; a second call writes nothing
p = context_cost.write_snapshot(now.date())
assert p and os.path.exists(p) and context_cost.write_snapshot(now.date()) == "", p
assert json.load(open(p))["v"] == 1
assert "busy-seat" in context_cost.render(doc)
# turns: a prompt with an origin starts one; tool results, meta records and a subagent's prompts do not
def rec(t, **kw):
    return json.dumps({"type": "user", "timestamp": t.strftime("%Y-%m-%dT%H:%M:%S.000Z"), **kw})
t0 = now - dt.timedelta(hours=12)
tl = [rec(t0 - dt.timedelta(minutes=1), origin={"kind": "human"}, message={"content": "go"}),
      rec(t0, message={"content": [{"type": "tool_result", "content": "x"}]}),
      rec(t0, isMeta=True, message={"content": [{"type": "text", "text": "skill body"}]}),
      rec(t0, isSidechain=True, origin={"kind": "human"}, message={"content": "subagent"})]
for i in range(12):   # turn i at t0 + i h, one call after it, the context 100k + 60k per turn
    t = t0 + dt.timedelta(hours=i)
    if i:
        tl.append(rec(t, origin={"kind": "task-notification" if i % 2 else "peer"}, message={"content": "wake"}))
    tl.append(call(t + dt.timedelta(minutes=1), 100000 + 60000 * i - 1000, 1000))
open(os.path.join(cc, "projects", "p", "eeee0000-0000.jsonl"), "w").write("\n".join(tl) + "\n")
calls, turns = context_cost.read_session(os.path.join(cc, "projects", "p", "eeee0000-0000.jsonl"), now - dt.timedelta(days=1), now)
assert len(turns) == 12 and len(calls) == 12, (len(turns), len(calls))
assert round(context_cost.growth_per_turn(calls, turns)) == 60000
h = context_cost.replay(calls, turns, 300)
# crosses 300k at turn 5 (from 100k), then a fresh 75k crosses again after 4 turns, 4 h later; 225k / 60k is 4 turns
assert (h["handovers"], h["first_after_turns"], h["turns_between"], h["hours_between"], h["turns_from_fresh"]) == \
    (2, 5, [4], [4.0], 4), h
r = next(x for x in context_cost.report(1.0, limits=(300, 500))["sessions"] if x["id"] == "eeee0000-0000")
assert r["turns"] == 12 and r["calls_per_turn"] == 1.0 and set(r["handover"]) == {"300", "500"}, r
assert "handover at a limit" in context_cost.render(context_cost.report(1.0))
# --handovers: the handovers that happened, from `handover` telemetry and the context-restart condition, costed by the
# same model (refresh turn at half the context, fresh first write at 2.0x)
def ev(mins, typ, sess, **kw):
    t = now - dt.timedelta(minutes=mins)
    e = dict(ts_utc=t.strftime("%Y-%m-%dT%H:%M:%SZ"), shoal="t1", session=sess, type=typ, **kw)
    d = os.path.join(S, "data-cost", "t1", "telemetry"); os.makedirs(d, exist_ok=True)
    open(os.path.join(d, f"events-{t.strftime('%Y%m%d')}.jsonl"), "a").write(json.dumps(e) + "\n")
ev(30, "handover", "aaaa", slug="rl", phase="advised", context_k="520", limit_k="500")
ev(20, "handover", "aaaa", slug="rl", phase="doc-refreshed", context_k="534", since_advised_s="600")
ev(20, "health_condition", "health", slug="rl", event="open", condition="context-restart", key="session:aaaa")
ev(19, "handover", "aaaa", slug="rl", phase="restart", outcome="ok", old_session="aaaa", since_ready_s="2")
ev(17, "health_condition", "health", slug="rl", event="close", condition="context-restart", key="session:aaaa",
   duration_min="2.5")
ev(18, "handover", "bbbb", slug="rl", phase="first-turn", old_session="aaaa", new_session="bbbb", since_restart_s="45",
   fresh_context_k="80")
ev(10, "handover", "cccc", slug="r2", phase="advised", context_k="410", limit_k="400")
ev(5, "handover", "dddd", slug="r3", phase="restart", outcome="failed", old_session="dddd")
ev(60 * 30, "handover", "eeee", slug="r4", phase="advised", context_k="600")   # outside a one-day window
doc = context_cost.handovers(1.0)
hs = {h["slug"]: h for h in doc["handovers"]}
assert set(hs) == {"rl", "r2", "r3"}, hs
rl = hs["rl"]
assert rl["status"] == "complete" and rl["new_session"] == "bbbb" and rl["fresh_measured"], rl
assert (rl["context_k"], rl["fresh_k"], rl["advised_to_ready_s"], rl["ready_to_restart_s"], rl["restart_to_first_turn_s"],
        rl["condition_min"]) == (534, 80, 600, 2, 45, 2.5), rl
assert (rl["cost_m"], rl["payback_warm_calls"], rl["payback_cold_wakes"]) == (0.43, 9.4, 0.47), rl
assert hs["r2"]["status"] == "advised" and not hs["r2"]["fresh_measured"] and hs["r2"]["cost_m"] == 0.35, hs["r2"]
assert hs["r3"]["status"] == "failed" and "cost_m" not in hs["r3"], hs["r3"]
assert {(r["slug"], r["handovers"], r["complete"], r["failed"]) for r in doc["roles"]} == \
    {("rl", 1, 1, 0), ("r2", 1, 0, 0), ("r3", 1, 0, 1)}, doc["roles"]
out = context_cost.render_handovers(doc)
assert "complete" in out and "~75k" in out and "total t1 rl" in out, out
assert "no handover events" in context_cost.render_handovers(context_cost.handovers(1.0, now - dt.timedelta(days=5)))
print("context-report ok")
PY2

echo "SELFTEST PASS ($S)"
