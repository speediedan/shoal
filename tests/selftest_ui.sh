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
# a spec written before the wake relay's retirement (features.relay, health.relay_fallback, a wake-relay role, local
# and on another host whose rows still carry relay_alive): it loads, and the UI shows no relay row, condition,
# summary field, tab-line segment or Herdr token
python3 - "$SPEC" "$S/relay-spec.json" <<'PY2' || fail "old relay spec"
import json, sys
d = json.load(open(sys.argv[1]))
d.setdefault("features", {})["relay"] = True
h = d["features"].get("health"); d["features"]["health"] = dict(h if isinstance(h, dict) else {}, relay_fallback=True)
d["roles"].append({"slug": "wake-relay", "ecosystem": "claude", "title": "wake-relay", "doc": d["roles"][0]["doc"],
                   "fill": "none", "session": "relay000", "server": "other-host.invalid"})
json.dump(d, open(sys.argv[2], "w"))
PY2
cat > "$S/relay-rows.json" <<JSON
{"v": 1, "shoals": [{"id": "$SID", "roles": [{"slug": "wake-relay", "session_id": "relay000", "session": "idle", "watcher": "none",
 "can_progress": {"ok": true, "reasons": []}, "needs_you": false, "context_k": null, "heartbeat_age_s": 5, "relay_alive": false}]}]}
JSON
rm -rf "$SHOAL_ROUTE_CACHE"
STUB_SSH_RC=0 STUB_SSH_OUT="$S/relay-rows.json" SHOAL_SSH="$S/ssh-stub" python3 - "$SHOALD" "$S/relay-spec.json" <<'PY2' || fail "old relay spec shows a relay"
import json, sys; sys.path.insert(0, sys.argv[1]); sys.path.insert(0, sys.argv[1] + "/ui")
import ui_status, ui_herdr
from roles import ShoalSpec
s = ShoalSpec.load(sys.argv[2])
assert s.role("wake-relay") is None and not any(r.slug == "wake-relay" for r in ui_herdr.viewable_roles(s))
d = ui_status.shoal_doc(sys.argv[2], [], with_conditions=True, refresh_conditions=True)
assert "relay" not in d and all(r["slug"] != "wake-relay" for r in d["roles"]), d
assert all("relay" not in r and "relay_alive" not in r for r in d["roles"]), d["roles"]
assert not [c for c in (d.get("conditions") or {}).get("open", []) if "relay" in c["condition"] + c["slug"]], d["conditions"]
assert "shoal_relay" not in ui_herdr.shoal_tokens(d) and not [b for b in ui_status._shoal_bits(d) if "relay" in b]
doc = {"shoals": [d], "usage": {}}
assert "relay" not in ui_status.render_text(doc, 160) and "relay" not in ui_status.render_line(doc, 200)
print("old relay spec ok")
PY2
! $H ui herdr-config --client | grep -q 'shoal_relay' || fail "the Herdr client block still draws \$shoal_relay"
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
# a dashboard or card frame taller than its pane is fitted to it: no tick scrolls, so the scrollback never
# collects a copy of the usage table per refresh (a small VT model replays each frame)
python3 - "$SHOALD" <<'PY2' || fail "frame fit"
import os, re, sys, time
sys.path[:0] = [sys.argv[1], sys.argv[1] + "/ui"]
os.environ["SHOAL_UI_COLOR"] = "1"  # SGR in the body: widths must be counted without it
import ui_status, ui_term

class Screen:
    """A small VT model: cursor home and CUP, ED 0/2/3, SGR ignored, deferred autowrap at `cols`, and a
    line feed on the last row that scrolls the top row into the scrollback (a tty maps \\n to CR LF)."""
    def __init__(self, cols, rows):
        self.cols, self.rows, self.x, self.y, self.wrap, self.scrolled = cols, rows, 0, 0, False, 0
        self.grid, self.scrollback = [[" "] * cols for _ in range(rows)], []
    def lf(self):
        self.x, self.wrap = 0, False
        if self.y == self.rows - 1:
            self.scrollback.append("".join(self.grid.pop(0)).rstrip()); self.grid.append([" "] * self.cols)
            self.scrolled += 1
        else:
            self.y += 1
    def write(self, s):
        for m in re.finditer(r"\x1b\[([0-9;?]*)([@-~])|(\n)|(\r)|([^\x1b\n\r])", s):
            if m.group(2):
                p, f = m.group(1), m.group(2)
                if f == "H":
                    r, _, c = p.partition(";")
                    self.y, self.x, self.wrap = int(r or 1) - 1, int(c or 1) - 1, False
                elif f == "J":
                    if p == "3":
                        self.scrollback = []
                    elif p == "2":
                        self.grid = [[" "] * self.cols for _ in range(self.rows)]
                    else:
                        raise AssertionError(f"ED {p!r} not modelled")
                elif f != "m":
                    raise AssertionError(f"CSI {p}{f} not modelled")
            elif m.group(3):
                self.lf()
            elif m.group(4):
                self.x, self.wrap = 0, False
            else:
                if self.wrap:
                    self.lf()
                self.grid[self.y][self.x] = m.group(5)
                if self.x == self.cols - 1:
                    self.wrap = True
                else:
                    self.x += 1
    def lines(self):
        return ["".join(r).rstrip() for r in self.grid]

now = time.time()
win = lambda pct: [{"name": n, "used_percent": pct, "resets_at": now + 3600 * (i + 1)} for i, n in enumerate(("5h", "week", "month"))]
usage = {"accounts": [{"provider": p, "stale": False, "age_s": 60, "rows": [{"bucket": p, "windows": win(10 * (i + 1))}]}
                      for i, p in enumerate(("codex", "claude", "muse", "opencode-go"))]}
roles = [{"slug": f"role-{i}", "harness": "claude", "host": "", "posture": "idle", "sub": "", "session": "idle",
          "active": f"example/repo#{i}:fix", "next": [{"item": f"example/repo#{i + 10}:fix", "blocker": "", "stale": False}],
          "last_write_age_s": 60 * i, "asks": [], "offers_open": 0,
          "can_progress": {"ok": i % 2 == 0, "reasons": [] if i % 2 == 0 else ["parked-work"]}} for i in range(8)]
conds = [{"slug": f"role-{i % 8}", "condition": "watcher-dead", "detail": f"synthetic condition {i} " + "x" * 40} for i in range(16)]
doc = {"usage": usage, "shoals": [{"id": "example-shoal", "gen": "r1", "mode": {"offline": False, "freeze": False},
       "tooling": {"current": True}, "needs_you": 0, "stalled": 0, "asks_open": 0, "pages_24h": 0,
       "roles": roles, "conditions": {"open": conds}}]}
footer = "\x1b[2m12:00:00  [r] refresh  [m] menu  [q] shell\x1b[0m"
plain = lambda s: re.sub(r"\x1b\[[0-9;?]*[ -/]*[@-~]", "", s)
for cols, rows in ((160, 42), (100, 24), (60, 30)):
    body = ui_status.render_text(doc, width=cols)
    need = sum(max(1, -(-len(plain(ln)) // cols)) for ln in body.split("\n"))
    clipped = need > rows - 2  # the body, a blank row, the one-row footer
    assert clipped or cols == 160, (cols, rows, need)
    t = Screen(cols, rows)
    for _ in range(3):  # three refresh ticks
        t.write(ui_term.fit_frame(body, footer, cols, rows))
    shown = t.lines()
    assert t.scrolled == 0 and t.scrollback == [], (cols, rows, t.scrolled, t.scrollback[:3])
    assert sum(ln.startswith("Subscription usage") for ln in shown) == 1, (cols, rows, shown)
    assert shown[0].startswith("Subscription usage"), shown[0]
    assert shown[-1] == plain(footer), (cols, rows, shown[-1])
    assert any("more lines: enlarge the pane" in ln for ln in shown) == clipped, (cols, rows, need, shown)
# the model catches the bug: the old redraw (ED 2 and home, a frame taller than the pane) scrolls, and
# every tick leaves one more copy of the usage header in the scrollback
cols, rows = 100, 24
body = ui_status.render_text(doc, width=cols)
t = Screen(cols, rows)
for _ in range(3):
    t.write("\x1b[2J\x1b[H" + body + "\n\n" + footer + "\n")
assert sum(ln.startswith("Subscription usage") for ln in t.scrollback) > 1, t.scrollback
print("frame fit ok")
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
# the ledger strip takes ledger_share percent
python3 - "$SHOALD" "$SPEC" <<'PY2' || fail "ledger share"
import sys; sys.path.insert(0, sys.argv[1]); sys.path.insert(0, sys.argv[1] + "/ui")
import ui_herdr, ui_status
from roles import ShoalSpec
s = ShoalSpec.load(sys.argv[2])
o = ui_herdr.overview_root(s)
assert o["direction"] == "down" and o["ratio"] == 0.88, o
q = {"can_progress": {"reasons": ["stopped-active"]}, "posture": "active", "sub": "", "next": []}
assert ui_status.posture_label(q) == "ACTIVE?"
PY2
check "ui set: one of" $H ui set show_relay false  # the retired setting is refused
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
unset SHOAL_HERDR_SESSION
# an empty scratch config from here on: the picker offers every saved standalone entry, so an unset
# XDG_CONFIG_HOME would read (and a --save would write) the operator's own registry
export XDG_CONFIG_HOME="$S/xdg-empty"

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
# a scratch registry: the operator's own saved seats must never reach the candidate list under test
XDG_CONFIG_HOME="$S/xdg-picker" python3 - "$SHOALD" "$S" <<'PY2' || fail "standalone picker"
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
            # omitted, sourceKinds lists interactive threads only: a `codex-new` seat (codex exec) never shows
            assert 'exec' in request['params'].get('sourceKinds', []), request['params']
            result = {'data': [{'id': 'cd-seat', 'name': 'codex-seat', 'cwd': '/tmp/example'},
                               {'id': 'role-id', 'name': 'worker-r2', 'cwd': '/tmp/example'},
                               {'id': 'headless', 'source': 'exec', 'cwd': '/tmp/example'},
                               {'id': 'named-exec', 'name': 'exec-seat', 'source': 'exec', 'cwd': '/tmp/example'}]}
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
# a named headless thread is a seat; an unnamed one is a role turn or a matcher run
assert [e['session'] for e in codex] == ['cd-seat', 'role-id', 'named-exec'], codex
assert codex[-1]['label'] == 'exec-seat', codex[-1]
assert [e['session'] for e in muse] == ['mu-seat'] and muse[0]['model'] == 'example-model'
assert set(log.read_text().splitlines()) == {'initialize', 'initialized', 'thread/list', 'session/list'}
# A failed provider cannot hide another provider's sessions.
claude = {'sessionId':'cl-seat', 'pid':123, 'id':'job-123', 'name':'claude-seat', 'state':'working'}
with patch.object(backends.ClaudeBackend, '_agents', return_value=[claude]), \
     patch.object(ui_herdr, 'role_slugs', return_value={'worker'}), \
     patch.object(ui_herdr.subprocess, 'run', return_value=types.SimpleNamespace(stdout='{}', returncode=0)), \
     patch.object(ui_sessions, 'stored_sessions', side_effect=lambda h: codex if h == 'codex' else muse):
    cands = ui_herdr.candidates_standalone({'role-id'})
assert [(e['harness'], e['session']) for e in cands] == [('claude', 'cl-seat'), ('codex', 'cd-seat'), ('codex', 'named-exec'),
                                                     ('muse', 'mu-seat')], cands
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
    with patch.object(ui_herdr, '_choose', return_value=3):
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
# saved entries stay pickable when no harness lists them (a headless thread, an ended session) or they were
# dismissed; a re-pick keeps what was saved on the seat, and model/effort pass on only within one harness
XDG_CONFIG_HOME="$S/xdg-registry" python3 - "$SHOALD" <<'PY2' || fail "standalone registry picks"
import contextlib, io, json, os, sys, types
from unittest.mock import patch
sys.path[:0] = [sys.argv[1], sys.argv[1] + "/ui"]
import backends, ui_herdr, ui_sessions
saved = [{'harness': 'codex', 'session': 'cx-unlisted', 'label': 'cx-seat', 'model': 'example-codex-model',
          'effort': 'high', 'doc': '/docs/cx.md', 'opened': True},
         {'harness': 'claude', 'session': 'cl-dismissed-1111', 'label': 'cl-seat', 'doc': '/docs/cl.md',
          'handover': 'pending', 'opened': True, 'dismissed': True},
         {'harness': 'claude', 'session': 'role-held-2222', 'label': 'held-seat', 'opened': True}]
ui_herdr.save_standalone(saved)
nothing = [patch.object(backends.ClaudeBackend, '_agents', return_value=[]),
           patch.object(ui_herdr, 'role_slugs', return_value=set()),
           patch.object(ui_herdr.subprocess, 'run', return_value=types.SimpleNamespace(stdout='{}', returncode=0)),
           patch.object(ui_sessions, 'stored_sessions', return_value=[])]
with contextlib.ExitStack() as st:
    for p in nothing:
        st.enter_context(p)
    cands = ui_herdr.candidates_standalone({'role-held'})
assert [(c['session'], c['state']) for c in cands] == [('cx-unlisted', 'registered'), ('cl-dismissed-1111', 'dismissed')], cands

def pick(cand):
    ns = types.SimpleNamespace(harness='', session_id='', label='', save=True, session='', pause=False)
    opened = []
    with patch.object(ui_herdr, 'candidates_standalone', return_value=[cand]), \
         patch.object(ui_herdr, 'role_sessions', return_value=set()), \
         patch.object(ui_herdr, '_choose', return_value=0), patch.object(ui_herdr, 'Herdr'), \
         patch.object(ui_herdr, 'open_standalone', side_effect=lambda h, e, **kw: opened.append(e) or 'opened'), \
         contextlib.redirect_stdout(io.StringIO()):
        assert ui_herdr._pick(ns) == 0
    return opened[0], {e['session']: e for e in ui_herdr.load_standalone(raw=True)}

# re-picking the dismissed entry reopens it with its doc and handover state; the dismissal is cleared
e, reg = pick({'harness': 'claude', 'session': 'cl-dismissed-1111', 'label': 'cl-seat', 'state': 'dismissed'})
got = reg['cl-dismissed-1111']
assert 'dismissed' not in got and got['doc'] == '/docs/cl.md' and got['handover'] == 'pending', got
assert e['doc'] == '/docs/cl.md', e
# a Claude session picked into the Codex seat's label keeps the seat's doc, never its Codex model or effort
e, reg = pick({'harness': 'claude', 'session': 'cl-new-3333', 'job': 'cl-new', 'label': 'cx-seat', 'state': 'idle'})
got = reg['cl-new-3333']
assert 'cx-unlisted' not in reg, 'one label, one card'
assert 'model' not in got and 'effort' not in got, got
assert got['doc'] == '/docs/cx.md', got
# within one harness the seat's model and effort carry over
ui_herdr.save_standalone(ui_herdr.load_standalone(raw=True) + [dict(saved[0], session='cx-old', label='cx2')])
e, reg = pick({'harness': 'codex', 'session': 'cx-new', 'label': 'cx2', 'state': 'saved'})
got = reg['cx-new']
assert got['model'] == 'example-codex-model' and got['effort'] == 'high' and got['doc'] == '/docs/cx.md', got
print('standalone registry picks ok')
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
python3 "$TESTD/test_muse_view.py" || fail "Muse role viewing"

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
# an interactive Claude session (a terminal seat, not --bg) cannot be attached: its card says where it runs,
# and no attach id is guessed for it
python3 - "$SHOALD" <<'PY2' || fail "interactive claude seat"
import json, os, sys; sys.path.insert(0, sys.argv[1]); sys.path.insert(0, sys.argv[1] + "/ui")
os.environ["SHOAL_FAKE_SESSIONS"] = json.dumps({"claude": [
    {"sessionId": "1111aaaa-2222", "pid": os.getpid(), "kind": "interactive", "name": "seat"}], "opencode": {}})
import backends, ui_view
why = ui_view.verify_listed("claude", "1111aaaa-2222")
assert f"running interactively in pid {os.getpid()}" in why and "--bg --resume 1111aaaa-2222" in why, why
try:
    argv, _env = ui_view.viewer("claude", "1111aaaa-2222")
except backends.UnsupportedError as exc:
    assert "interactively" in str(exc), exc
else:
    raise AssertionError(f"an interactive session got a `claude attach` argv: {argv[1:]}")
assert [(s["session"], s["state"]) for s in backends.get("claude").sessions()] == [("1111aaaa-2222", "interactive")]
# an unknown id never recorded with a job: an ended interactive session, resumable in the background
why = ui_view.verify_listed("claude", "2222bbbb-3333")
assert "not listed" in why and "--bg --resume 2222bbbb-3333" in why, why
# one recorded with a job is a background session that is gone: never a guess
why = ui_view.verify_listed("claude", "2222bbbb-3333", "2222bbbb")
assert "re-resolve, never guess" in why and "--bg --resume" not in why, why
print("interactive claude seat ok")
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

echo "== 10 graph feed: nodes, edges and the event tail from a synthetic ledger; cursor round trip; writes nothing"
# Two shoals in one ledger repository: alpha (core, dist-ci, tune, a parked research) and beta (research, analysis).
# Every line is synthetic and timed relative to now, so the window counts are exact.
GR="$S/graph-root"; GL="$S/graph-ledger"; GA=uigraph-alpha; GB=uigraph-beta
rm -rf "$GR" "$GL" "/tmp/$GA" "/tmp/$GB"; mkdir -p "$GR/shoals/$GA" "$GR/shoals/$GB" "/tmp/$GA"
git init -q "$GL"
python3 - "$GR" "$GL" "$DOC" "$WORK" <<'PY2' || fail "graph fixture"
import datetime as dt, json, os, sys, time
GR, GL, DOC, WORK = sys.argv[1:5]
now = dt.datetime.now(dt.timezone.utc)
ago = lambda s: (now - dt.timedelta(seconds=s)).strftime("%Y-%m-%dT%H:%M:%SZ")
def spec(sid, roles, home=""):
    d = {"shoal": sid, "home": home, "dir": WORK, "incarnation": 0, "repos": ["example-org/repo-one"], "ledger": {"dir": GL},
         "roles": [dict(ecosystem="claude", title=r[0], doc=DOC, slug=r[0], fill=r[1], session=r[2]) for r in roles],
         "generations": [{"gen": "r1", "incarnation": 0, "started_utc": ago(86400 * 3), "ended_utc": "", "sessions": {}, "run": {}}]}
    json.dump(d, open(os.path.join(GR, "shoals", sid, "shoal.json"), "w"))
spec("uigraph-alpha", [("core", "assigned", "c0re0000"), ("dist-ci", "assigned", "d15c0000"),
                       ("tune", "assigned", "t0ne0000"), ("research", "assigned", "")])
spec("uigraph-beta", [("research", "assigned", "r5ch0000"), ("analysis", "assigned", "an4l0000")],
     home="graph-host-b.invalid")   # beta runs on another host: its edges to alpha cross hosts
n = [0]
def ev(sid, writer, age, typ, **kw):
    n[0] += 1
    e = dict(ts=ago(age), by=kw.pop("by", writer), type=typ, id=f"g{n[0]:05d}", **kw)
    d = os.path.join(GL, "shoals", sid, "ledger"); os.makedirs(d, exist_ok=True)
    open(os.path.join(d, f"{writer}.jsonl"), "a").write(json.dumps(e, separators=(",", ":")) + "\n")
A, B = "uigraph-alpha", "uigraph-beta"
ev(A, "shoal", 86400 * 2, "system", event="ledger-init")
ev(A, "user", 26 * 3600, "offer", to="core", item="repo-one#101", verb="fix", src="user")      # assigned roles claim
ev(A, "user", 26 * 3600, "offer", to="dist-ci", item="repo-one#103", verb="fix", src="user")   # only what is offered
ev(A, "user", 3 * 3600, "offer", to="tune", item="repo-one#871", verb="fix", src="user")
ev(A, "user", 5 * 3600, "grant", state="granted", role="core", scope="merge selftest PRs", kind="merge")
ev(A, "user", 4 * 3600, "grant", state="pending", role="tune", scope="push an image", kind="image")
ev(A, "core", 30 * 3600, "msg", to="dist-ci", text="old news", kind="info")         # outside the day
ev(A, "core", 3 * 3600, "msg", to="dist-ci", text="hourly", kind="info")
ev(A, "core", 1800, "msg", to="dist-ci", text="half an hour", kind="info")
ev(A, "core", 120, "msg", to="dist-ci", text="two minutes", kind="info")
ev(A, "core", 60, "msg", to="dist-ci", text="PR 103 rebased; please rerun the GPU leg", kind="info")
ev(A, "core", 600, "msg", to="*", text="queue v12: active repo-one#101:fix", kind="info")
ev(A, "core", 1200, "msg", to="ext:evolution", text="reply to the outside seat", kind="info")
ev(A, "core", 3 * 3600, "claim", item="repo-one#101", verb="fix")
ev(A, "core", 2 * 3600, "done", item="repo-one#99", verb="fix", kind="fix", url="https://github.com/example-org/repo-one/pull/120")
ev(A, "core", 7000, "queue", version=1, active="repo-one#101:fix", next=["repo-one#104:review"],
   next_blocked={"repo-one#104": "dist-ci:repo-one#103"}, posture="active")
ev(A, "dist-ci", 3 * 3600, "claim", item="repo-one#103", verb="fix")
ev(A, "dist-ci", 900, "state", state="waiting", sub="local-ci", holder="job ci-1", target="job:ci-1")
ev(A, "dist-ci", 2400, "msg", to="ext:research", text="cross-shoal note", kind="info")
ev(A, "tune", 1500, "queue", version=1, active="repo-one#871:fix", asks=["approve image push scope?"], posture="active")
ev(A, "tune", 1500, "state", state="waiting", sub="user-auth", holder="image push scope")
ev(A, "tune", 1500, "msg", to="user", text="approve image push scope?", kind="request")
ev(A, "shoal", 3000, "msg", to="user", text="health: tune waits on you", kind="health")
ev(A, "ext-evolution", 900, "msg", by="ext:evolution", to="core", text="a note from outside", kind="info")
ev(A, "wake-relay", 86400 * 2, "state", state="waiting", sub="queue-empty")
ev(B, "shoal", 86400, "system", event="ledger-init")
ev(B, "analysis", 7200, "msg", to="research", text="findings attached", kind="info")
# role-time native cache: core is mid-turn, dist-ci between turns (its CI wait comes from the ledger); tune has none
t = time.time()
nat = lambda st: {"native": {"state": st, "observed_at": t, "budget_s": 90.0, "wait_event": "", "raw": ""}}
json.dump({"v": 1, "shoal": A, "host": "h", "written_at": t, "roles": {"core": nat("turn"), "dist-ci": nat("between")},
           "errors": {}}, open(f"/tmp/{A}/role-time-native.json", "w"))
# analysis's native observation is past its budget: role-time reads unknown:stale-source, the graph its posture
os.makedirs(f"/tmp/{B}", exist_ok=True)
json.dump({"v": 1, "shoal": B, "host": "h", "written_at": t - 1000, "roles": {"analysis": {"native": {
           "state": "between", "observed_at": t - 1000, "budget_s": 90.0, "wait_event": "", "raw": ""}}},
           "errors": {}}, open(f"/tmp/{B}/role-time-native.json", "w"))
# dist-ci's CI job: placed on a pool host and granted its lease (the job meta `job wait` keeps)
os.makedirs(f"/tmp/{A}/jobs", exist_ok=True)
json.dump({"unit": "shoal-uigraph-ci-1", "shoal": A, "slug": "dist-ci", "name": "ci-1", "cmd": ["true"],
           "started_utc": ago(800), "rc_file": f"/tmp/{A}/jobs/none.rc", "lease_granted": True,
           "placement": {"host": "graph-pool-c.invalid", "reason": "selftest"}},
          open(f"/tmp/{A}/jobs/shoal-uigraph-ci-1.json", "w"))
PY2
git -C "$GL" add -A && git -C "$GL" -c user.email=t@e -c user.name=t commit -qm fixture || fail "graph fixture commit"
touch "$GL/.git/FETCH_HEAD"   # the clone's last fetch: the ledger half of a role-time snapshot is fresh
snap() { (cd "$GL" && git rev-parse HEAD && git status --porcelain && find shoals -type f -printf '%p %s %T@\n' | sort); }
SNAP0=$(snap)
export SHOAL_FAKE_SESSIONS='{"claude": [], "opencode": {}}'
G0=$($H ui graph --ledger-root "$GR" --json) || fail "ui graph rc :: $(tail -c 600 <<<"$G0")"
[ "$(snap)" = "$SNAP0" ] || fail "ui graph wrote into the ledger clone"
python3 - "$G0" <<'PY2' || fail "graph contract"
import json, sys
d = json.loads(sys.argv[1]); A, B = "uigraph-alpha", "uigraph-beta"
assert d["v"] == 1 and d["kind"] == "shoal-graph" and d["cursor"], d.keys()
assert d["windows"] == {"rate_s": 300, "recent_s": 3600, "day_s": 86400}, d["windows"]
nodes = {n["id"]: n for n in d["nodes"]}
kinds = {i: n["kind"] for i, n in nodes.items()}
for i, k in {"user": "user", f"{A}:shoal": "system", f"{A}:core": "role", f"{A}:dist-ci": "role",
             f"{A}:tune": "role", f"{A}:research": "role", f"{B}:research": "role", f"{B}:analysis": "role",
             "ext:evolution": "external", "ext:research": "external"}.items():
    assert kinds.get(i) == k, (i, kinds.get(i))
# the retired wake relay's writer folds into the system node: no relay node, no writer node for it
assert not any(k in ("relay", "writer") for k in kinds.values()) and f"{A}:wake-relay" not in nodes, kinds
# outside seats: aliased to the role of the same slug in another shoal, or not at all
assert nodes["ext:research"]["alias_of"] == f"{B}:research" and nodes["ext:evolution"]["alias_of"] is None
# role state: role-time over the cached native status where there is one, the posture where there is none
st = lambda i: nodes[i]["state"]
assert (st(f"{A}:core")["class"], st(f"{A}:core")["event"], st(f"{A}:core")["source"]) == ("work", "turn", "role-time"), st(f"{A}:core")
assert (st(f"{A}:dist-ci")["class"], st(f"{A}:dist-ci")["event"], st(f"{A}:dist-ci")["group"]) == ("wait", "local-ci", "system"), st(f"{A}:dist-ci")
assert (st(f"{A}:tune")["event"], st(f"{A}:tune")["group"], st(f"{A}:tune")["source"]) == ("user-auth", "human", "posture"), st(f"{A}:tune")
assert st(f"{A}:tune")["needs_you"] and "waiting-on-you" in st(f"{A}:tune")["reasons"], st(f"{A}:tune")
assert (st(f"{A}:research")["class"], st(f"{A}:research")["event"]) == ("excluded", "parked"), st(f"{A}:research")
assert st(f"{B}:analysis")["source"] == "posture" and "role-time:stale-source" in st(f"{B}:analysis")["quality"], st(f"{B}:analysis")
assert st(f"{B}:analysis")["class"] != "unknown", st(f"{B}:analysis")
# work: item URLs from the spec's repos, done with the event's own url
w = nodes[f"{A}:core"]["work"]
assert w["active"] == {"item": "repo-one#101:fix", "url": "https://github.com/example-org/repo-one/issues/101"}, w
assert w["next"][0]["blocker"] == "dist-ci:repo-one#103" and w["next"][0]["url"].endswith("/repo-one/issues/104"), w
assert w["done_recent"] == [{"item": "repo-one#99", "url": "https://github.com/example-org/repo-one/pull/120",
                             "ts": w["done_recent"][0]["ts"]}], w
assert nodes[f"{A}:tune"]["work"]["asks"] == ["approve image push scope?"]
assert nodes["user"]["work"] == {"asks_open": 1, "grants_pending": 0}, nodes["user"]   # no row lists any
assert "grants_pending" not in nodes[f"{A}:core"]["work"] and "grants_pending_n" not in d["shoals"][0]["metrics"]
# a CI wait names its job: where it runs (a pool host here), placed, granted, started
j = st(f"{A}:dist-ci")["job"]
assert j["unit"] == "shoal-uigraph-ci-1" and j["host"] == "graph-pool-c.invalid" and j["placed"] and j["granted"], j
assert 790 <= j["started_age_s"] < 900 and st(f"{A}:dist-ci")["job_host"] == "graph-pool-c.invalid", j
assert "job" not in st(f"{A}:core") and "job_host" not in st(f"{A}:tune")
# hosts: this host, beta's home and the job's pool host, with per-host role aggregates
H = {h["name"]: h for h in d["hosts"]}
here = H[d["host"]]
assert here["home"] and (here["roles"], here["working"], here["waiting"], here["needs_you"]) == (4, 1, 2, 1), here
assert here["msgs_1h"] == 7 and here["id"] == "host:" + d["host"], here   # core 5, dist-ci 1, tune 1 in the hour
assert (H["graph-host-b.invalid"]["roles"], H["graph-host-b.invalid"]["home"]) == (2, False), H
assert H["graph-pool-c.invalid"]["roles"] == 0, H
assert nodes[f"{B}:research"]["host"] == "graph-host-b.invalid" and nodes["ext:research"]["host"] == "graph-host-b.invalid"
# edges
E = {(e["src"], e["dst"], e["class"]): e for e in d["edges"]}
m = E[(f"{A}:core", f"{A}:dist-ci", "msg")]
assert (m["n_rate"], m["n_1h"], m["n_24h"]) == (2, 3, 4) and m["last_age_s"] < 120 and m["shoal"] == A, m
assert m["id"] == f"{A}:core|{A}:dist-ci|msg" and m["rate_per_h"] > 0
assert E[(f"{A}:core", f"{A}:*", "broadcast")]["n_24h"] == 1
bo = E[(f"{A}:core", f"{A}:dist-ci", "blocked-on")]
assert bo["structural"] and bo["last_ts"] is None and bo["stale"] is False, bo
ask = E[(f"{A}:tune", "user", "ask")]
assert ask["n_24h"] == 1 and ask["open"] == 1, ask
assert E[(f"{A}:shoal", "user", "health")]["n_1h"] == 1
assert E[("user", f"{A}:tune", "offer")]["n_1h"] == 0 and E[("user", f"{A}:tune", "offer")]["n_24h"] == 1
assert E[("user", f"{A}:core", "grant")]["n_24h"] == 1
assert E[("ext:evolution", f"{A}:core", "msg")]["n_rate"] == 0 and E[("ext:evolution", f"{A}:core", "msg")]["n_1h"] == 1
assert E[(f"{A}:dist-ci", "ext:research", "msg")]["n_24h"] == 1
al = E[("ext:research", f"{B}:research", "alias")]
assert al["shoal"] is None and al["structural"] and al["cross_host"] is False, al
# cross_host: dist-ci (this host) writing to the outside seat that is beta's research (the other host)
assert E[(f"{A}:dist-ci", "ext:research", "msg")]["cross_host"] is True
assert E[(f"{A}:core", f"{A}:dist-ci", "msg")]["cross_host"] is False and E[(f"{A}:tune", "user", "ask")]["cross_host"] is False
assert E[(f"{B}:analysis", f"{B}:research", "msg")]["shoal"] == B
# shoal metrics
sm = {s["id"]: s for s in d["shoals"]}[A]["metrics"]
want = dict(roles=4, working=1, needs_you=1, asks_open=1, pages_24h=1, done_24h=1, done_7d=1, claims_24h=2,
            offers_24h=1, msgs_1h=9, msgs_24h=10, work_ratio_24h=None)
assert {k: sm[k] for k in want} == want, sm
# events: ts order, seq increasing, broadcast dst, the done carries its url
ev = d["events"]
assert [e["seq"] for e in ev] == sorted(e["seq"] for e in ev) and len({e["seq"] for e in ev}) == len(ev), ev
assert [e["ts"] for e in ev] == sorted(e["ts"] for e in ev)
b = next(e for e in ev if e["class"] == "broadcast")
assert b["dst"] == f"{A}:*" and b["type"] == "msg" and b["summary"].startswith("queue v12"), b
dn = next(e for e in ev if e["type"] == "done")
assert dn["url"].endswith("/pull/120") and dn["dst"] is None, dn
assert all(len(e["summary"]) <= 120 for e in ev)
assert not any(e["type"] in ("state", "queue") for e in ev)
print("graph contract ok")
PY2
CUR=$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["cursor"])' "$G0")
SEQ0=$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["events"][-1]["seq"])' "$G0")
G1=$($H ui graph --ledger-root "$GR" --json --since "$CUR") || fail "ui graph --since rc"
python3 - "$G1" <<'PY2' || fail "graph since: nothing new"
import json, sys; d = json.loads(sys.argv[1])
assert d["events"] == [] and len(d["nodes"]) > 5 and d["edges"] and d["shoals"], d["events"]
PY2
NOW=$(date -u +%Y-%m-%dT%H:%M:%SZ)
printf '{"ts":"%s","by":"core","type":"msg","id":"gnew0001","to":"dist-ci","text":"one more","kind":"info"}\n' "$NOW" \
  >> "$GL/shoals/$GA/ledger/core.jsonl"
G2=$($H ui graph --ledger-root "$GR" --json --since "$CUR") || fail "ui graph --since rc after append"
python3 - "$G2" "$SEQ0" "$CUR" <<'PY2' || fail "graph cursor round trip"
import json, sys; d = json.loads(sys.argv[1]); seq0 = int(sys.argv[2])
ev = d["events"]
assert len(ev) == 1 and ev[0]["summary"] == "one more" and ev[0]["seq"] == seq0 + 1, ev
assert d["cursor"] != sys.argv[3]
e = {(x["src"], x["dst"], x["class"]): x for x in d["edges"]}[("uigraph-alpha:core", "uigraph-alpha:dist-ci", "msg")]
assert e["n_rate"] == 3 and e["n_24h"] == 5, e
print("cursor round trip ok")
PY2
CUR2=$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["cursor"])' "$G2")
G3=$($H ui graph --ledger-root "$GR" --json --since "$CUR2") || fail "ui graph --since newest rc"
python3 -c 'import json,sys; d=json.loads(sys.argv[1]); assert d["events"] == [], d["events"]' "$G3" || fail "graph since the newest cursor"
# the feed: an in-memory tail reads only appended whole lines, and the document lands beside the status cache
python3 - "$SHOALD" "$GR" "$GL" <<'PY2' || fail "graph builder incremental"
import os, sys
sys.path[:0] = [sys.argv[1], sys.argv[1] + "/ui"]
import ui_graph
b = ui_graph.GraphBuilder(root=sys.argv[2])
d1 = b.build()
p = os.path.join(sys.argv[3], "shoals", "uigraph-alpha", "ledger", "core.jsonl")
key = "uigraph-alpha/core.jsonl"
assert b.tail.files[key]["off"] == os.path.getsize(p)
with open(p, "a") as fh:  # a line still being written: no newline yet
    fh.write('{"ts":"2099-01-01T00:00:00Z","by":"core","type":"msg","id":"gnew0002","to":"*","text":"partial')
d2 = b.build()
assert d2["events"][-1]["seq"] == d1["events"][-1]["seq"] and b.tail.files[key]["off"] < os.path.getsize(p)
with open(p, "a") as fh:
    fh.write('","kind":"info"}\n')
d3 = b.build()
assert d3["events"][-1]["seq"] == d1["events"][-1]["seq"] + 1 and d3["events"][-1]["summary"] == "partial", d3["events"][-1]
assert b.tail.files[key]["off"] == os.path.getsize(p)
# grants the status rows list pass through to the role, the shoal and the maintainer node
sid = "uigraph-alpha"
rows = [dict(r, grants_pending=[{"item": "push an image", "kind": "image"}] if r["slug"] == "tune" else [])
        for r in b._rows[sid][1]]
b.use_cache = True
b._cached_shoals = lambda: {sid: {"id": sid, "roles": rows, "grants_pending_n": 1}}
d4 = b.build()
n = {x["id"]: x for x in d4["nodes"]}
assert n[sid + ":tune"]["work"]["grants_pending"] == [{"item": "push an image", "kind": "image"}], n[sid + ":tune"]["work"]
assert n["user"]["work"]["grants_pending"] == 1
assert {s["id"]: s for s in d4["shoals"]}[sid]["metrics"]["grants_pending_n"] == 1
print("incremental tail ok")
PY2
$H ui graph-feed --ledger-root "$GR" --once >/dev/null || fail "graph-feed --once rc"
GJ="$XDG_RUNTIME_DIR/shoal-ui-$(id -u)/graph.json"
python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); assert d["v"] == 1 and d["kind"] == "shoal-graph"' "$GJ" \
  || fail "graph-feed did not write $GJ"
check "needs you: tune" $H ui graph --ledger-root "$GR"
check "top edges" $H ui graph --ledger-root "$GR"
# only the test's own appends changed the clone: no file was added, committed or rewritten by a graph run
[ "$(cd "$GL" && git status --porcelain)" = " M shoals/$GA/ledger/core.jsonl" ] || fail "graph run changed the ledger clone"
rm -rf "/tmp/$GA" "/tmp/$GB"
echo "graph feed ok"

echo "== 11 standalone card after a Claude /bg: a new background job with a new id, followed only on evidence"
python3 - "$SHOALD" "$S" <<'PY2' || fail "bg successor"
import datetime as dt, glob, json, os, sys; sys.path.insert(0, sys.argv[1]); sys.path.insert(0, sys.argv[1] + "/ui")
S = sys.argv[2]
os.environ["XDG_CONFIG_HOME"] = os.path.join(S, "xdg-bg")      # a scratch registry, never the operator's
cc = os.path.join(S, "cc-bg"); os.environ["CLAUDE_CONFIG_DIR"] = cc  # scratch jobs/ and projects/
JOBS = os.path.join(cc, "jobs")
import ui_herdr, ui_view
ORIG = "11111111-1111-4111-8111-111111111111"
CWD = "/work/seat"
SEEN = "2026-11-01T00:00:00Z"
T = dt.datetime(2026, 11, 1, tzinfo=dt.timezone.utc).timestamp()
def job(jid, lineage=True):
    os.makedirs(os.path.join(JOBS, jid), exist_ok=True)
    json.dump({"sessionId": jid + "-2222-4222-8222-222222222222", "resumeSessionId": jid + "-2222-4222-8222-222222222222",
               "interactiveLineage": lineage, "name": "seat", "nameSource": "user", "template": "bg", "cwd": CWD},
              open(os.path.join(JOBS, jid, "state.json"), "w"))
def bg(jid, name="seat", cwd=CWD, after=60, state="idle"):
    return {"id": jid, "sessionId": jid + "-2222-4222-8222-222222222222", "kind": "background", "name": name, "cwd": cwd,
            "startedAt": int((T + after) * 1000), "state": state, "status": "idle", "pid": 4100}
ITV = {"kind": "interactive", "sessionId": ORIG, "pid": os.getpid(), "name": "seat"}
for j in ("aaaa0001", "bbbb0001", "cccc0001", "dddd0001", "eeee0001", "44444444"):
    job(j)
job("ffff0001", lineage=False)
def setrows(rows):
    os.environ["SHOAL_FAKE_SESSIONS"] = json.dumps({"claude": rows, "opencode": {}})
def reset():
    ui_herdr.save_standalone([{"harness": "claude", "session": ORIG, "label": "seat", "cwd": CWD, "seen_utc": SEEN},
                              {"harness": "claude", "session": "44444444-2222-4222-8222-222222222222", "job": "44444444",
                               "label": "other"}])
def saved():
    return next(e for e in ui_herdr.load_standalone(raw=True) if e["label"] == "seat")
def backups():
    return glob.glob(ui_herdr.standalone_path() + ".bak-bgfollow-*")
tick = lambda pick=0: ui_view.standalone_tick("claude", ORIG, "seat", "", pick)

# 0 successors, the original not listed: refused, with the exit-first background resume and the /bg note
reset(); setrows([])
d = tick()
assert d["action"] == "refuse" and not d["successors"], d
m = d["message"]
assert "not listed" in m and "exit any terminal" in m and f"--bg --resume {ORIG} --name seat" in m and "/bg" in m, m
assert ui_view.verify_listed("claude", ORIG) == m
# 0 successors, the original live interactively: where it runs, exit first, then resume; its seen time is refreshed
setrows([ITV])
d = tick()
assert d["action"] == "refuse" and d["row"]["kind"] == "interactive", d
m = d["message"]
assert f"running interactively in pid {os.getpid()}" in m and "exit that interactive window first" in m, m
assert f"--bg --resume {ORIG} --name seat" in m and "forks a new session id" in m, m
assert saved()["seen_utc"] > SEEN and saved()["session"] == ORIG, saved()
assert "exit that interactive window first" in ui_view.interactive_where(ITV)

# exactly 1 successor, the original gone: followed, by atomic replace, with one backup
reset(); setrows([bg("aaaa0001")])
d = tick()
assert d["action"] == "followed" and d["message"] == "followed /bg successor aaaa0001 (was 11111111)", d
e = saved()
assert (e["session"], e["job"], e["bg_followed_from"]) == ("aaaa0001-2222-4222-8222-222222222222", "aaaa0001", ORIG), e
assert len(backups()) == 1 and not os.path.exists(ui_herdr.standalone_path() + ".tmp"), backups()
assert ORIG in open(backups()[0]).read(), "the backup holds the registry from before the follow"
assert ui_view.registry_follow("claude", ORIG, "seat", "") == ("aaaa0001-2222-4222-8222-222222222222", "aaaa0001")
assert ui_view.standalone_tick("claude", e["session"], "seat", "aaaa0001")["action"] == "attach"
# a second follow writes no second backup
reset(); d = tick()
assert d["action"] == "followed" and len(backups()) == 1, backups()
# a successor that has answered its first prompt reads `done` (idle, with a pid): live, and followed
reset(); setrows([bg("aaaa0001", state="done")])
d = tick()
assert d["action"] == "followed" and saved()["job"] == "aaaa0001" and not d.get("stopped"), (d, saved())

# 1 successor while the original still runs interactively: two live copies, a warning, nothing rewritten
reset(); setrows([ITV, bg("aaaa0001")])
d = tick()
assert d["action"] == "two-live" and saved()["session"] == ORIG and saved()["seen_utc"] == SEEN, (d, saved())
m = d["message"]
assert "two live copies" in m and f"pid {os.getpid()}" in m and "aaaa0001" in m and "2026-11-01T00:01:00Z" in m, m
assert "--bg --resume" not in m, m
assert "two live copies" in ui_view.verify_listed("claude", ORIG)

# 2 successors (a second /bg named "seat (2)"): listed, nothing guessed; a digit picks one
reset(); setrows([bg("aaaa0001"), bg("bbbb0001", name="seat (2)", after=120)])
d = tick()
assert d["action"] == "pick" and [c["id"] for c in d["successors"]] == ["aaaa0001", "bbbb0001"], d
assert "[1] aaaa0001" in d["message"] and "[2] bbbb0001" in d["message"] and saved()["session"] == ORIG, d
assert tick(pick=7)["action"] == "pick" and saved()["session"] == ORIG, "an out-of-range digit follows nothing"
d = tick(pick=2)
assert d["action"] == "followed" and saved()["job"] == "bbbb0001", (d, saved())

# excluded: another cwd, one another entry already holds, one started before seen_utc, no interactive lineage,
# and a stopped copy (named only, never followed)
reset(); setrows([bg("cccc0001", cwd="/work/elsewhere"), bg("44444444"), bg("dddd0001", after=-3600),
                  bg("ffff0001"), bg("eeee0001", state="stopped"), dict(bg("bbbb0001", name="seats"))])
d = tick()
assert d["action"] == "refuse" and not d["successors"] and [c["id"] for c in d["stopped"]] == ["eeee0001"], d
assert "Stopped copies: eeee0001 (`claude attach <id>` resumes one)" in d["message"], d["message"]
assert saved()["session"] == ORIG and len(backups()) == 1, saved()
# an unreadable state.json is accepted, and noted
e = saved(); ents = ui_herdr.load_standalone(raw=True)
c = ui_view.bg_successors([bg("abab0001")], e, ents, JOBS, (T, "seen_utc"))
assert [x["id"] for x in c] == ["abab0001"] and "unreadable" in c[0]["note"], c

# an entry with no seen_utc: a successor must start after the saved time, else the original transcript's first line
e = {"harness": "claude", "session": ORIG, "label": "seat", "cwd": CWD}
os.makedirs(os.path.join(cc, "projects", "p"), exist_ok=True)
open(os.path.join(cc, "projects", "p", ORIG + ".jsonl"), "w").write(json.dumps({"type": "user", "timestamp": SEEN}) + "\n")
assert ui_view.bg_since(e) == (T, "transcript"), ui_view.bg_since(e)
assert ui_view.bg_since(dict(e, saved_utc="2026-11-01T00:00:30Z")) == (T + 30, "saved_utc")
rows = [bg("dddd0001", after=-10), bg("aaaa0001", after=10)]
assert [x["id"] for x in ui_view.bg_successors(rows, e, [e], JOBS, ui_view.bg_since(e))] == ["aaaa0001"]
print("bg successor ok")
PY2

echo "== 12 Herdr app adapter: plan, reconcile over a fake herdr-app and hc, card status reports, the host list"
# one stub plays both herdr-app and hc (by its name), keeping the app's spaces in a JSON file and logging every call
cat > "$S/app-stub" <<'STUB'
#!/usr/bin/env python3
import json, os, sys
st_path = os.environ["STUB_APP_STATE"]
def load():
    try:
        return json.load(open(st_path))
    except (OSError, ValueError):
        return {"spaces": [], "terms": {}, "n": 0, "machines": []}
st = load()
me, args = os.path.basename(sys.argv[0]), sys.argv[1:]
open(os.environ["STUB_APP_LOG"], "a").write(json.dumps([me] + args) + "\n")
def save():
    json.dump(st, open(st_path, "w"))
def term(argv, space, tab):
    st["n"] += 1
    t = f"t{st['n']}"
    st["terms"][t] = {"labels": {}, "argv": argv}
    tab["terminals"].append({"terminal": t, "pane": "p" + t})
    return t
def find_tab(pane):
    for s in st["spaces"]:
        for tb in s["tabs"]:
            if any(x["pane"] == pane for x in tb["terminals"]):
                return s, tb
    return None, None
if args[:1] == ["--version"]:
    print(("herdr-app " if me == "herdr-app" else "hc ") + os.environ.get("STUB_APP_VERSION", "0.1.12" if me == "herdr-app" else "0.1.17"))
elif me == "herdr-app" and args[:1] == ["status"]:
    sys.exit(0 if os.environ.get("STUB_APP_UP", "1") == "1" else 1)
elif me == "herdr-app" and args[:1] == ["ls"]:
    print(json.dumps({"spaces": [{"id": s["id"], "name": s["name"], "machine": s["machine"],
                                  "tabs": [{"id": t["id"], "name": t["name"], "terminals": t["terminals"]} for t in s["tabs"]]}
                                 for s in st["spaces"]]}))
elif me == "herdr-app" and args[:1] == ["do"]:
    cmd, a = args[1], json.loads(args[2] if len(args) > 2 else "{}")
    out = {}
    if cmd == "space.new" and a.get("machine") and st.get("connecting", 0) < int(os.environ.get("STUB_CONNECTING", "0")):
        st["connecting"] = st.get("connecting", 0) + 1; save()  # a machine added a moment ago is still connecting
        print(json.dumps({"error": f"{a['machine']} is connecting"})); sys.exit(1)
    if cmd == "space.new":
        s = {"id": f"sp{len(st['spaces'])}", "name": a.get("name"), "machine": a.get("machine") or None, "tabs": []}
        tb = {"id": f"tb{s['id']}-0", "name": a.get("tab") or "1", "terminals": []}
        s["tabs"].append(tb); st["spaces"].append(s)
        out = {"space": s["id"], "tab": tb["id"], "terminal": term(a.get("argv"), s, tb)}
    elif cmd == "tab.new":
        s = next(x for x in st["spaces"] if x["id"] == a["space"])
        tb = {"id": f"tb{s['id']}-{len(s['tabs'])}", "name": a.get("name"), "terminals": [], "mod": a.get("mod")}
        s["tabs"].append(tb)
        out = {"space": s["id"], "tab": tb["id"]}
        if not a.get("mod"):
            out["terminal"] = term(a.get("argv"), s, tb)
    elif cmd == "pane.split":
        s, tb = find_tab(a["pane"])
        t = term(a.get("argv"), s, tb)
        out = {"pane": "p" + t, "terminal": t}
    elif cmd == "pane.close":
        s, tb = find_tab(a["pane"])
        tb["terminals"] = [x for x in tb["terminals"] if x["pane"] != a["pane"]]
    save(); print(json.dumps(out))
elif me == "hc" and args[:1] == ["get"]:
    t = st["terms"].get(args[1]) or {}
    fg = os.environ.get("STUB_FG_" + args[1])
    argv = fg.split() if fg else ["/usr/bin/python3", "/x/shoal/cli.py", "view"]
    print(json.dumps({"address": args[1], "labels": t.get("labels", {}),
                      "foreground": {"name": os.path.basename(argv[0]), "argv": argv}}))
elif me == "hc" and args[:1] == ["label"]:
    for kv in args[2:]:
        if "=" in kv:
            k, v = kv.split("=", 1); st["terms"].setdefault(args[1], {"labels": {}})["labels"][k] = v
    save(); print("{}")
elif me == "hc" and args[:2] == ["machine", "ls"]:
    print(json.dumps({"machines": [{"name": m} for m in st["machines"]]}))
elif me == "hc" and args[:2] == ["machine", "add"]:
    st["machines"].append(args[args.index("--name") + 1]); save(); print("{}")
else:
    print(json.dumps({"error": "stub: unknown " + " ".join(args)})); sys.exit(1)
STUB
chmod +x "$S/app-stub"; mkdir -p "$S/appbin"; ln -sf "$S/app-stub" "$S/appbin/herdr-app"; ln -sf "$S/app-stub" "$S/appbin/hc"
mkdir -p "$S/apphome/mods/lattice-view"
AENV="SHOAL_HERDR_APP_BIN=$S/appbin/herdr-app SHOAL_HC_BIN=$S/appbin/hc HERDR_APP_HOME=$S/apphome STUB_APP_STATE=$S/app.json STUB_APP_LOG=$S/app.log XDG_STATE_HOME=$S/state-app"
# the plan: a space per shoal (overview, roles, lattice) and the cards' argv; read-only
OUT=$(env $AENV $H ui app-plan --json --no-standalone --ledger-root "$ROOT") || fail "app-plan :: $OUT"
python3 - "$OUT" "$SID" <<'PY3' || fail "app plan shape"
import json, sys
d = json.loads(sys.argv[1]); sid = sys.argv[2]
assert d["kind"] == "shoal-app-plan" and d["v"] == 1, d
s = next(x for x in d["spaces"] if x.get("shoal") == sid)
assert s["name"] == sid and [t["name"] for t in s["tabs"]] == ["overview", "roles", "lattice"], s
cards = [p for p in s["tabs"][1]["panes"]]
assert any(p["label"] == f"{sid}/cl" and p["argv"][-4:] == ["view", sid, "cl", "--stub"] for p in cards), cards
lat = s["tabs"][2]
assert lat["mod"] == "lattice-view" and lat["props"]["shoal"] == sid and lat["props"]["source"]["kind"] in ("file", "exec"), lat
print("app plan ok")
PY3
# no app running: app-open says so and changes nothing
OUT=$(env $AENV STUB_APP_UP=0 $H ui app-open --no-standalone --ledger-root "$ROOT" 2>&1); RC=$?
[ "$RC" = 1 ] && grep -q "not running here" <<<"$OUT" || fail "app-open without an app: rc $RC :: $OUT"
# a dry run lists what it would add; nothing structural is called
: > "$S/app.log"
OUT=$(env $AENV $H ui app-open --dry-run --no-standalone --ledger-root "$ROOT") || fail "app-open dry-run :: $OUT"
grep -q "\[dry-run\] space $SID" <<<"$OUT" && grep -q "\[dry-run\] tab lattice (lattice-view)" <<<"$OUT" || fail "dry-run plan :: $OUT"
! grep -q '"do"' "$S/app.log" || fail "a dry run called herdr-app do :: $(cat "$S/app.log")"
# a real open: the space, its tabs, a split per further pane, every terminal labelled; then focus
OUT=$(env $AENV $H ui app-open --no-standalone --ledger-root "$ROOT") || fail "app-open :: $OUT"
python3 - "$S/app.json" "$S/app.log" "$SID" <<'PY4' || fail "app-open reconcile"
import json, sys
st = json.load(open(sys.argv[1])); log = [json.loads(l) for l in open(sys.argv[2])]; sid = sys.argv[3]
s = next(x for x in st["spaces"] if x["name"] == sid)
assert [t["name"] for t in s["tabs"]] == ["overview", "roles", "lattice"], s["tabs"]
assert len(s["tabs"][0]["terminals"]) == 2 and s["tabs"][2].get("mod") == "lattice-view", s["tabs"]
labels = sorted(t["labels"].get("shoal.pane", "") for t in st["terms"].values())
assert f"{sid}/overview" in labels and f"{sid}/ledger" in labels and f"{sid}/cl" in labels, labels
cmds = [l[2] for l in log if l[:2] == ["herdr-app", "do"]]
assert cmds.count("space.new") == 1 and "space.select" in cmds and "tab.select" in cmds, cmds
assert not any(c in ("pane.close", "space.close", "tab.close", "terminal.kill") for c in cmds), cmds
print("app open ok")
PY4
# idempotent: a second open adds nothing
: > "$S/app.log"
OUT=$(env $AENV $H ui app-open --no-standalone --no-focus --ledger-root "$ROOT") || fail "app-open again :: $OUT"
grep -q "every planned space is open" <<<"$OUT" || fail "second open :: $OUT"
! grep -qE '"(space.new|tab.new|pane.split)"' "$S/app.log" || fail "second open changed the app :: $(cat "$S/app.log")"
# a card whose terminal came back as a bare shell is replaced; a pane running anything else is left alone
CL=$(python3 -c "import json,sys;st=json.load(open('$S/app.json'));print(next(t for t,v in st['terms'].items() if v['labels'].get('shoal.pane')=='$SID/cl'))")
OV=$(python3 -c "import json,sys;st=json.load(open('$S/app.json'));print(next(t for t,v in st['terms'].items() if v['labels'].get('shoal.pane')=='$SID/overview'))")
: > "$S/app.log"
OUT=$(env $AENV "STUB_FG_$CL=-bash" "STUB_FG_$OV=claude attach c1a2b3c4" $H ui app-open --no-standalone --no-focus --ledger-root "$ROOT") || fail "app-open repair :: $OUT"
grep -q "restart $SID/cl (its pane is a bare shell)" <<<"$OUT" || fail "bare shell card not restarted :: $OUT"
python3 - "$S/app.log" "p$CL" "p$OV" <<'PY8' || fail "bare shell repair touched the wrong panes :: $(cat "$S/app.log")"
import json, sys
do = [(l[2], json.loads(l[3])) for l in map(json.loads, open(sys.argv[1])) if l[:2] == ["herdr-app", "do"]]
assert ("pane.close", {"pane": sys.argv[2]}) in do, do
assert not any(a.get("pane") == sys.argv[3] for _, a in do), do
PY8
# a remote host: its route becomes an app machine and its plan comes over ssh
cp "$SHOAL_CONFIG" "$S/host-app.toml"; printf '\n[hosts.lane-b]\nssh = "u@lane-b.invalid"\nresolve = "dns"\n' >> "$S/host-app.toml"
env $AENV $H ui app-plan --json --no-standalone --ledger-root "$ROOT" > "$S/plan-b.json" || fail "app-plan for the remote stub"
printf '#!/bin/sh\ncat "%s"\n' "$S/plan-b.json" > "$S/ssh-plan"; chmod +x "$S/ssh-plan"
OUT=$(env $AENV STUB_CONNECTING=2 SHOAL_CONFIG="$S/host-app.toml" SHOAL_SSH="$S/ssh-plan" $H ui app-open --machine lane-b --no-standalone --no-focus) \
  || fail "app-open remote :: $OUT"
python3 - "$S/app.json" "$SID" <<'PY5' || fail "remote machine space"
import json, sys
st = json.load(open(sys.argv[1]))
assert st["machines"] == ["lane-b"], st["machines"]
assert any(s["name"] == sys.argv[2] and s["machine"] == "lane-b" for s in st["spaces"]), st["spaces"]
print("app remote ok")
PY5
# the card's own status report (OSC 7501), the mapping, and the host list
python3 - "$SHOALD" <<'PY6' || fail "card report"
import base64, os, sys; sys.path.insert(0, sys.argv[1]); sys.path.insert(0, sys.argv[1] + "/ui")
import ui_app
o = ui_app.osc7501("blocked", "auth", "lane/r", "wait user-auth")
assert o.startswith("\x1b]7501;state=blocked:kind=auth:app=shoal:title=") and o.endswith("\x1b\\"), repr(o)
assert base64.b64encode(b"wait user-auth").decode() in o
def row(**kw):
    r = {"slug": "r", "posture": "idle", "sub": None, "wait": None, "active": None, "session": "idle", "needs_you": False,
         "asks": [], "can_progress": {"ok": True, "reasons": []}}
    r.update(kw); return r
assert ui_app.card_report(row(), "lane")["state"] == "idle"
w = row(posture="wait", sub="user-auth", needs_you=True, wait={"sub": "user-auth", "target": "gh"},
        can_progress={"ok": True, "reasons": ["waiting-on-you"]})
assert (ui_app.card_report(w, "lane")["state"], ui_app.card_report(w, "lane")["kind"]) == ("blocked", "auth")
q = row(session="working", needs_you=True, can_progress={"ok": False, "reasons": ["blocked-on-question"]})
assert (ui_app.card_report(q, "lane")["state"], ui_app.card_report(q, "lane")["kind"]) == ("blocked", "question")
# an open prompt is the thing to answer, even while the ledger wait is user-auth
qa = row(posture="wait", sub="user-auth", needs_you=True, wait={"sub": "user-auth", "target": "gh"},
         can_progress={"ok": False, "reasons": ["blocked-on-question", "waiting-on-you"]})
assert ui_app.card_report(qa, "lane")["kind"] == "question", ui_app.card_report(qa, "lane")
j = row(posture="wait", sub="local-ci", wait={"sub": "local-ci", "target": "job:build"})
rep = ui_app.card_report(j, "lane")
assert rep["state"] == "working" and "local-ci" in rep["msg"] and rep["title"] == "lane/r", rep
assert ui_app.foreground_kind({"foreground": {"name": "bash", "argv": ["-bash"]}}) == "shell"
assert ui_app.foreground_kind({"foreground": {"name": "bash", "argv": ["bash", "-c", "x"]}}) == "other"
assert ui_app.foreground_kind({"foreground": {"name": "claude", "argv": ["claude", "attach", "x"]}}) == "other"
assert not ui_app.in_core_terminal({}) and ui_app.in_core_terminal({"HC_TERMINAL_ID": "t1"})
# a shoal's space lives on its home host only (every host reads every shoal through the shared ledger)
import config
Spec = lambda home: type("Spec", (), {"home": home})()
me = config.server() or os.uname().nodename.split(".")[0]
assert ui_app.home_here(Spec("")) and ui_app.home_here(Spec(me)) and not ui_app.home_here(Spec("elsewhere.invalid"))
print("card report ok")
PY6
OUT=$(env $AENV SHOAL_UI_LEDGER_ROOTS="$ROOT" HC_TERMINAL_ID=t1 timeout 4 $H view $SID cl --stub </dev/null 2>/dev/null); true
grep -q $'\x1b\]7501;state=' <<<"$OUT" || fail "a card in a core terminal reports no OSC 7501 :: $(tail -c 300 <<<"$OUT" | od -c | tail -5)"
OUT=$(env $AENV SHOAL_UI_LEDGER_ROOTS="$ROOT" timeout 4 $H view $SID cl --stub </dev/null 2>/dev/null); true
! grep -q $'\x1b\]7501' <<<"$OUT" || fail "a card outside the app wrote an OSC 7501 report"
OUT=$(env $AENV SHOAL_CONFIG="$S/host-app.toml" SHOAL_SSH="$S/ssh-stub" STUB_SSH_RC=255 $H ui launch --list --json) || fail "launch --list :: $OUT"
python3 - "$OUT" <<'PY7' || fail "launch list shape"
import json, sys
d = json.loads(sys.argv[1])
assert d["kind"] == "shoal-launch-list" and d["hosts"][0]["name"] == "local" and d["hosts"][0]["local"], d
b = next(h for h in d["hosts"] if h["name"] == "lane-b")
assert b["reachable"] is False and "shoal doctor" in b["hint"] and b["route"] == "u@lane-b.invalid", b
print("launch list ok")
PY7
OUT=$(env $AENV $H ui app-install --dry-run) || fail "app-install dry-run :: $OUT"
grep -q "shoal-graph-feed.service" <<<"$OUT" || fail "app-install plan :: $OUT"
# mods go into the app home: a link, or a marked copy for one with pictures (the app loads no PNG through a link);
# a stale copy is refreshed, a folder shoal did not make is left alone
python3 - "$SHOALD" "$S" <<'PY9' || fail "mod install"
import os, sys; sys.path.insert(0, sys.argv[1]); sys.path.insert(0, sys.argv[1] + "/ui")
S = sys.argv[2]
os.environ["HERDR_APP_HOME"] = os.path.join(S, "apphome-mods")
import ui_app
src = os.path.join(S, "modsrc"); ui_app.MOD_DIR = src
for d in ("plain", "pics/assets"):
    os.makedirs(os.path.join(src, d), exist_ok=True)
open(os.path.join(src, "plain", "main.js"), "w").write("x")
open(os.path.join(src, "pics", "main.js"), "w").write("y")
open(os.path.join(src, "pics", "assets", "a.png"), "wb").write(b"png")
assert ui_app.install_mod("plain").startswith("link ") and ui_app.link_state("plain")[0]
assert ui_app.install_mod("pics").startswith("copy ") and ui_app.link_state("pics")[0]
dest = os.path.join(S, "apphome-mods", "mods", "pics")
assert not os.path.islink(dest) and os.path.exists(os.path.join(dest, ui_app.COPY_MARK))
open(os.path.join(dest, "herdr.d.ts"), "w").write("app-written")   # files the app writes never make it stale
assert ui_app.install_mod("pics") == "" and ui_app.install_mod("plain") == ""
open(os.path.join(src, "pics", "main.js"), "w").write("y2")
assert not ui_app.link_state("pics")[0] and "stale" in ui_app.link_state("pics")[1]
assert ui_app.install_mod("pics").startswith("copy ") and open(os.path.join(dest, "main.js")).read() == "y2"
mine = os.path.join(S, "apphome-mods", "mods", "plain")
os.remove(mine); os.makedirs(mine); open(os.path.join(mine, "main.js"), "w").write("operator's own")
assert "left as it is" in ui_app.install_mod("plain") and open(os.path.join(mine, "main.js")).read() == "operator's own"
print("mod install ok")
# the 0.9.3 paste keys: the app's defaults are swapped, other keys kept, a binding the person chose is left
import json
kp = os.path.join(S, "apphome-mods", "keys.json")
json.dump({"global": {"alt-1": "space.select:1"},
           "terminal": {"ctrl-v": "terminal.paste_image", "ctrl-shift-v": "terminal.paste", "ctrl-shift-c": "terminal.copy"}},
          open(kp, "w"))
lines = ui_app.install_keys()
k = json.load(open(kp))
assert k["terminal"]["ctrl-v"] == "terminal.paste" and k["terminal"]["ctrl-shift-v"] == "terminal.paste_image", k
assert k["terminal"]["ctrl-shift-c"] == "terminal.copy" and k["global"] == {"alt-1": "space.select:1"}, k
assert len(lines) == 2 and ui_app.keys_state()[0] and ui_app.install_keys() == [], lines
bak = json.load(open(kp + ".bak-shoal-paste"))  # a one-time backup of the file as it was
assert bak["terminal"]["ctrl-v"] == "terminal.paste_image", bak
open(kp + ".odd", "w").write(json.dumps({"terminal": ["not", "an", "object"]}))
os.replace(kp, kp + ".keep"); os.replace(kp + ".odd", kp)
assert "left as it is" in ui_app.install_keys()[0] and json.load(open(kp))["terminal"] == ["not", "an", "object"]
os.replace(kp + ".keep", kp)
k["terminal"]["ctrl-v"] = "send:x"; json.dump(k, open(kp, "w"))
assert "by you" in ui_app.install_keys()[0] and json.load(open(kp))["terminal"]["ctrl-v"] == "send:x"
print("paste keys ok")
# the shoal ocean theme: its file is shoal's own; settings.json's theme is set only when the person set none
sp = os.path.join(S, "apphome-mods", "settings.json")
json.dump({"zoom": 1.0}, open(sp, "w"))
lines = ui_app.install_theme()
assert json.load(open(sp)) == {"zoom": 1.0, "theme": "shoal-ocean"} and ui_app.theme_state()[0], lines
assert json.load(open(sp + ".bak-shoal-theme")) == {"zoom": 1.0}
th = json.load(open(os.path.join(S, "apphome-mods", "themes", "shoal-ocean.json")))
assert th["background"] == "#000000" and th["foreground"].startswith("#f") and len(th["ansi"]) == 16, th
assert ui_app.install_theme() == []
json.dump({"theme": "paper"}, open(sp, "w"))
assert "left as it is" in ui_app.install_theme()[0] and json.load(open(sp))["theme"] == "paper"
print("theme ok")
PY9
# the plugin wrapper (every 0.9.3 pane, app card and unit runs it) finds the Python of the `shoal` on PATH: from a
# python shebang, or from the exec line of a shell wrapper `shoal` (running cli.py under sh broke a host's units)
mkdir -p "$S/wrap-sh" "$S/wrap-py" "$S/wrap-none"
printf '#!/bin/sh\nexec %s /x/cli.py "$@"\n' "$(command -v python3)" > "$S/wrap-sh/shoal"
printf '#!%s\nimport sys\n' "$(command -v python3)" > "$S/wrap-py/shoal"
printf '#!/bin/sh\necho none\n' > "$S/wrap-none/shoal"
chmod +x "$S"/wrap-*/shoal
WRAP="$TESTD/../plugins/shoal-herdr/bin/shoal-ui"
for d in sh py none; do
  OUT=$(env -u SHOAL_PYTHON PATH="$S/wrap-$d:/usr/bin:/bin" sh -x "$WRAP" --help 2>&1 | grep '^+ exec' || true)
  want=$( [ "$d" = none ] && echo "python3" || command -v python3 )
  grep -q "^+ exec $want .*/shoal/cli.py --help" <<<"$OUT" || fail "shoal-ui interpreter for a $d shoal :: $OUT"
done
echo "plugin wrapper interpreter ok"

echo "== 13 actions without a terminal: act-plan previews a write verb under a one-time token, act-apply runs it once"
AE="SHOAL_UI_LEDGER_ROOTS=$ROOT"
OUT=$(env $AE $H ui act-plan msg --shoal $SID --role cl --text "from the dialog" --json) || fail "act-plan msg :: $OUT"
TOK=$(python3 - "$OUT" <<'PY10'
import json, os, stat, sys
d = json.loads(sys.argv[1])
assert d["kind"] == "shoal-act-plan" and d["confirm"] is True and d["cost"] == "", d
assert d["command"].startswith("shoal msg --shoal") and "--to cl" in d["command"], d
assert "dry-run" in d["preview"].lower(), d["preview"]
print(d["token"])
PY10
) || fail "plan shape"
P="$XDG_RUNTIME_DIR/shoal-ui-$(id -u)/act-plans/$TOK.json"
[ "$(stat -c %a "$P")" = 600 ] || fail "a plan is stored private (mode $(stat -c %a "$P"))"
OUT=$(env $AE $H ui act-apply --token "$TOK" --json) || fail "act-apply :: $OUT"
grep -q '"kind": "shoal-act-result"' <<<"$OUT" && grep -q '"rc": 0' <<<"$OUT" || fail "apply result :: $OUT"
grep -q '"from the dialog"\|from the dialog' "$S/data/ui-actions.jsonl" || fail "apply is logged like the palette"
[ ! -e "$P" ] || fail "a plan is forgotten once applied"
OUT=$(env $AE $H ui act-apply --token "$TOK" --json 2>&1); RC=$?
[ "$RC" = 2 ] && grep -q "already applied, or expired" <<<"$OUT" || fail "a token applies once: rc $RC :: $OUT"
OUT=$(env $AE $H ui act-apply --token "../../etc/passwd" --json 2>&1); RC=$?
[ "$RC" = 2 ] && grep -q "not a plan token" <<<"$OUT" || fail "a token is only ever a token :: $OUT"
# an expired plan does not run
OUT=$(env $AE $H ui act-plan withdraw --shoal $SID --item example/repo#7 --json) || fail "act-plan withdraw :: $OUT"
TOK=$(python3 -c 'import json,sys;print(json.loads(sys.argv[1])["token"])' "$OUT")
P="$XDG_RUNTIME_DIR/shoal-ui-$(id -u)/act-plans/$TOK.json"
python3 -c 'import json,sys;p=sys.argv[1];d=json.load(open(p));d["created"]-=3600;json.dump(d,open(p,"w"))' "$P"
OUT=$(env $AE $H ui act-apply --token "$TOK" --json 2>&1); RC=$?
[ "$RC" = 2 ] && grep -q "expired" <<<"$OUT" || fail "an expired plan is refused :: $OUT"
# the palette's rules: costs named, offline without a confirm, freeze --no-wake without a cost, roles checked
python3 - "$SHOALD" "$SID" "$SPEC" <<'PY11' || fail "act build rules"
import os, sys; sys.path.insert(0, sys.argv[1]); sys.path.insert(0, sys.argv[1] + "/ui")
os.environ["SHOAL_SPEC"] = sys.argv[3]
import ui_actions
class A:
    def __init__(self, **kw):
        self.shoal = sys.argv[2]; self.role = ""; self.text = ""; self.item = ""; self.note = ""
        self.wake = False; self.state = ""; self.until = ""; self.no_wake = False
        self.__dict__.update(kw)
args, cost, ok = ui_actions.build("msg", A(role="*", text="all", wake=True))
assert "--wake" in args and "EVERY role" in cost and ok
args, cost, ok = ui_actions.build("offer", A(role="oc", item="example/repo#9:fix"))
assert args[:5] == ["offer", "--shoal", sys.argv[2], "--to", "oc"] and "wakes an idle role" in cost
args, cost, ok = ui_actions.build("offline", A(state="on", until="2026-11-04T09:00-08:00", note="out"))
assert args[:2] == ["offline", "on"] and "--until" in args and ok is False and cost == ""
assert "every role" in ui_actions.build("freeze", A(state="on"))[1]
assert ui_actions.build("freeze", A(state="on", no_wake=True)) == (["freeze", "on", "--shoal", sys.argv[2], "--no-wake"], "", True)
for verb, kw in (("msg", dict(role="nope", text="x")), ("msg", dict(role="cl")), ("offer", dict(role="cl")),
                 ("freeze", dict(state="maybe")), ("withdraw", {})):
    try:
        ui_actions.build(verb, A(**kw)); raise SystemExit(f"{verb} {kw} accepted")
    except ui_actions.ActError:
        pass
print("act rules ok")
PY11
OUT=$(env $AE $H ui act-plan next --shoal $SID --role cl --json) || fail "act-plan next :: $OUT"
grep -q '"kind": "shoal-act-view"' <<<"$OUT" && grep -q 'next for cl' <<<"$OUT" || fail "next view :: $(head -c 400 <<<"$OUT")"
OUT=$(env $AE $H ui act-plan asks --shoal $SID --json) || fail "act-plan asks :: $OUT"
python3 -c 'import json,sys;d=json.loads(sys.argv[1]);assert d["kind"]=="shoal-act-view" and isinstance(d["asks"],list),d' "$OUT" || fail "asks view"
OUT=$(env $AE $H ui act-plan roles --shoal $SID --json) || fail "act-plan roles :: $OUT"
python3 -c 'import json,sys;d=json.loads(sys.argv[1]);s=d["shoals"][0];assert d["kind"]=="shoal-act-roles" and "cl" in [r["slug"] for r in s["roles"]] and "parked" not in [r["slug"] for r in s["roles"]],d' "$OUT" \
  || fail "roles view (roles with a session only) :: $OUT"
echo "act plan and apply ok"

# the lattice-view Herdr app mod's pure modules on its fixture (Node only; skipped without node)
if command -v node >/dev/null 2>&1; then
  node "$TESTD/herdr_app/test_lattice_view.mjs" >"$S/lattice-view.out" 2>&1 || fail "herdr-app lattice-view: $(tail -c 800 "$S/lattice-view.out")"
  echo "herdr-app lattice-view ok"
  node "$TESTD/herdr_app/test_shoal_home.mjs" >"$S/shoal-home.out" 2>&1 || fail "herdr-app shoal-home: $(tail -c 800 "$S/shoal-home.out")"
  echo "herdr-app shoal-home ok"
else
  echo "herdr-app lattice-view, shoal-home: skipped (node not on PATH)"
fi

echo "SELFTEST PASS ($S)"
