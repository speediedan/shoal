#!/usr/bin/env bash
# selftest_ledger.sh — isolated end-to-end proof of the ledger primitive
# (readme 5.j). Builds a local bare "remote" + two clones (two hosts), a
# 3-role spec under a scratch spec dir and SHOAL_DATA_ROOT, and drives every verb:
# init, offers + permissions (user-only offers; a role-written offer is a
# violation), the claim race across clones (contested -> lost-race),
# withdraw, decline, msg + ack, board fill (a synthetic board fixture, no
# GitHub read), exit-wake watcher, transition, heartbeat, systemd plan,
# status, lint, report, spec absolutize-docs. Exit 1 on the first failed
# assertion. Nothing outside $SCRATCH is written (no real remote touched);
# hermetic: scratch host config, synthetic start docs, scratch working dir.
#   usage: selftest_ledger.sh [scratch-dir]
set -eu
TESTD="$(cd "$(dirname "$0")" && pwd)"
HAGD="$(cd "$TESTD/../shoal" && pwd)"
. "$TESTD/selftest_lib.sh"
S="${1:-${TMPDIR:-/tmp}/shoal-selftest-$$}"
rm -rf "$S"; mkdir -p "$S/root" "$S/data" "$S/remote"
hag_test_setup "$S"
ROOT="$S/root"
SPEC="$ROOT/t1hag/hag.json"; A="$S/cloneA"; B="$S/cloneB"
export SHOAL_SPEC="$SPEC"
H="python3 $HAGD/cli.py"
fail() { echo "SELFTEST FAIL: $*" >&2; exit 1; }
expect() { local rc=$1 got=0; shift; "$@" && got=0 || got=$?; [ "$got" = "$rc" ] || fail "rc=$got (want $rc): $*"; }
check() { local pat=$1; shift; local out; out=$("$@" 2>&1 || true); grep -q -- "$pat" <<<"$out" || fail "expected '$pat' in output of: $* :: $out"; }

git init -q --bare "$S/remote/ledgers.git"
git clone -q "$S/remote/ledgers.git" "$A" 2>/dev/null
( cd "$A" && git commit -q --allow-empty -m init && git push -q origin HEAD 2>/dev/null )
git clone -q "$S/remote/ledgers.git" "$B" 2>/dev/null
mkdir -p "$ROOT/t1hag"
# synthetic board, board order = priority: open interpretune items, one out of alpha's scope,
# one closed, one in a skipped status
hag_board_fixture "$S/board.json" <<'JSON'
[{"repo":"speediedan/interpretune","number":600,"title":"fixture: offered then done"},
 {"repo":"speediedan/interpretune","number":372,"title":"fixture: offered then done"},
 {"repo":"speediedan/interpretune","number":501,"title":"fixture: pool item, contested"},
 {"repo":"speediedan/interpretune","number":9001,"title":"fixture: free candidate one"},
 {"repo":"example/other","number":39,"title":"fixture: outside alpha's scope"},
 {"repo":"speediedan/interpretune","number":9002,"title":"fixture: closed","state":"CLOSED"},
 {"repo":"speediedan/interpretune","number":9003,"title":"fixture: done status","status":"Done"},
 {"repo":"speediedan/interpretune","number":9004,"title":"fixture: free candidate two","kind":"pr"},
 {"repo":"speediedan/interpretune","number":9005,"title":"fixture: free candidate three"}]
JSON
DOC_A=$(hag_doc alpha); DOC_B=$(hag_doc beta); DOC_O=$(hag_doc observer)
cat > "$SPEC" <<JSON
{"hag":"t1hag","dir":"$WORK","incarnation":0,"repos":["speediedan/interpretune"],
 "ledger":{"repo":"$S/remote/ledgers.git","dir":"$A"},
 "sources":[{"kind":"github-project","owner":"example","number":2}],
 "roles":[
  {"slug":"alpha","ecosystem":"claude","title":"alpha","doc":"$DOC_A","fill":"self","session":"aaaa1111","scope":{"repos":["speediedan/interpretune"]}},
  {"slug":"beta","ecosystem":"opencode","title":"beta","doc":"$DOC_B","fill":"self","session":"ses_bbbb"},
  {"slug":"observer","ecosystem":"opencode","title":"observer","doc":"$DOC_O","fill":"none","can_fill_others":true,"coordinator":false,"queue":"","session":"ses_ffff"}
 ],"generations":[{"gen":"r1","incarnation":0,"started_utc":"2026-09-20T00:00:00Z","ended_utc":"","sessions":{},"run":{}}]}
JSON
echo "== 1 ledger-init (dry-run, live); a pre-flattening spec (can_fill_others/coordinator/queue keys) loads"
expect 0 $H ledger-init --spec "$SPEC" --dry-run >/dev/null
expect 0 $H ledger-init --spec "$SPEC" >/dev/null
[ -d "$A/shoals/t1hag/ledger" ] || fail "ledger dir missing after init"
expect 0 $H offer --shoal t1hag --to alpha --item interpretune#372:fix --base interpretune@3ac6fdf9 --note "phase 1 only" >/dev/null
check "interpretune#372:fix" $H show --shoal t1hag --slug alpha
echo "== 2 offers + permissions"
expect 0 $H offer --shoal t1hag --to alpha --item interpretune#600:fix --base interpretune@3ac6fdf9 --prio 10 >/dev/null
expect 1 $H offer --shoal t1hag --to alpha --item interpretune#427:fix --by beta >/dev/null      # a role never offers
expect 1 $H offer --shoal t1hag --to beta --item interpretune#427:fix --by beta >/dev/null       # not even to itself
expect 1 $H offer --shoal t1hag --to alpha --item interpretune#427:fix --by observer >/dev/null  # legacy can_fill_others is ignored
check "only user offers work" $H offer --shoal t1hag --to alpha --item interpretune#427:fix --by observer
expect 0 $H offer --shoal t1hag --to alpha --item interpretune#427:fix --base interpretune@3ac6fdf9 >/dev/null
expect 0 $H offer --shoal t1hag --to '*' --item interpretune#501:scope --base interpretune@3ac6fdf9 >/dev/null
check "already offered" $H offer --shoal t1hag --to alpha --item interpretune#600:fix
check "pool offers (to=\*): interpretune#501:scope" $H show --shoal t1hag
echo "== 3 claim race across clones on the pool item #501 (B stale, A lands first)"
expect 4 $H claim --shoal t1hag --slug beta --item interpretune#600:fix --ledger-dir "$B" >/dev/null   # offered to alpha only: refused
expect 0 $H sync --shoal t1hag --ledger-dir "$B" >/dev/null      # B fetches once, then goes stale
expect 0 $H claim --shoal t1hag --slug beta --item interpretune#501:scope --ledger-dir "$B" --no-sync >/dev/null
expect 0 $H claim --shoal t1hag --slug alpha --item interpretune#501:scope --base interpretune@3ac6fdf9 >/dev/null
expect 0 $H sync --shoal t1hag --ledger-dir "$B" >/dev/null
expect 1 $H ledger-lint --shoal t1hag >/dev/null            # E2 contested
check "E2 contested claim on interpretune#501" $H ledger-lint --shoal t1hag
check "CLAIM-LOST interpretune#501: alpha landed first" timeout 20 $H ledger-watch --shoal t1hag --slug beta --wake exit --interval 60 --ledger-dir "$B" --no-board
expect 0 $H release --shoal t1hag --slug beta --item interpretune#501 --kind lost-race --reason "alpha landed first" --ledger-dir "$B" >/dev/null
expect 0 $H ledger-lint --shoal t1hag >/dev/null
expect 4 $H claim --shoal t1hag --slug beta --item interpretune#501:scope --ledger-dir "$B" >/dev/null   # held by alpha
echo "== 4 done, withdraw, decline, msg + ack, ask"
expect 0 $H claim --shoal t1hag --slug alpha --item interpretune#600:fix >/dev/null
expect 0 $H done --shoal t1hag --slug alpha --item interpretune#600:fix --kind merge --url https://github.com/speediedan/interpretune/pull/601 >/dev/null
expect 0 $H done --shoal t1hag --slug alpha --item interpretune#501:scope >/dev/null
expect 4 $H claim --shoal t1hag --slug beta --item interpretune#501:scope --ledger-dir "$B" >/dev/null   # done
expect 1 $H withdraw --shoal t1hag --item interpretune#427 --to alpha --reason reassign --by beta >/dev/null   # roles never withdraw
expect 0 $H withdraw --shoal t1hag --item interpretune#427 --to alpha --reason reassign >/dev/null
expect 0 $H offer --shoal t1hag --to '*' --item interpretune#554:scope --base interpretune@3ac6fdf9 >/dev/null
expect 0 $H claim --shoal t1hag --slug beta --item interpretune#554:scope --ledger-dir "$B" >/dev/null
expect 0 $H release --shoal t1hag --slug beta --item interpretune#554 --kind decline --reason "out of lane" --retry "after #600" --ledger-dir "$B" >/dev/null
check "interpretune#554:scope" $H show --shoal t1hag --slug alpha     # re-opened for others
check "declined: interpretune#554" $H show --shoal t1hag --slug beta  # never re-offered to beta
expect 0 $H msg --shoal t1hag --by user --to alpha --text "review adapter#39 next" --kind instruct --ack >/dev/null
MID=$(python3 -c "import json;[print(json.loads(l)['id']) for l in open('$A/shoals/t1hag/ledger/user.jsonl') if '\"msg\"' in l]" | tail -1)
expect 0 $H msg --shoal t1hag --by alpha --to user --text ack --kind ack --re "$MID" >/dev/null
expect 0 $H msg --shoal t1hag --by beta --to user --text "need merge grant #602" --kind ask --ledger-dir "$B" >/dev/null
check "msgs to user" $H show --shoal t1hag
echo "== 5 board fill (synthetic board fixture, no GitHub read)"
check "still has" $H fill --shoal t1hag --slug alpha            # #372 offered + #554 pool open
expect 0 $H claim --shoal t1hag --slug alpha --item interpretune#372:fix >/dev/null
expect 0 $H done --shoal t1hag --slug alpha --item interpretune#372:fix >/dev/null
expect 0 $H withdraw --shoal t1hag --item interpretune#554 >/dev/null
check "interpretune#9001" $H fill --shoal t1hag --slug alpha --limit 3
FILL=$($H fill --shoal t1hag --slug alpha --limit 5)
if grep -q "interpretune#600\|interpretune#372\|interpretune#501" <<<"$FILL"; then fail "taken/withdrawn items surfaced as candidates"; fi
if grep -q "other#39\|interpretune#9002\|interpretune#9003" <<<"$FILL"; then fail "out-of-scope, closed or Done items surfaced :: $FILL"; fi
grep -q "interpretune#9004" <<<"$FILL" && grep -q "interpretune#9005" <<<"$FILL" || fail "board order candidates missing :: $FILL"
echo "== 6 exit-wake watcher: arms, wakes on a new offer, exits 0, sidecar bound"
( set +e; timeout 60 $H ledger-watch --shoal t1hag --slug beta --wake exit --interval 2 --ledger-dir "$B" --no-board --session ses_bbbb > "$S/watch.out" 2>&1; echo "rc=$?" >> "$S/watch.out" ) &
sleep 4
expect 0 $H offer --shoal t1hag --to beta --item interpretune#450:fix --base interpretune@3ac6fdf9 >/dev/null
wait
grep -q "LEDGER-WAKE beta@t1hag" "$S/watch.out" || fail "watcher did not wake: $(cat "$S/watch.out")"
grep -q "rc=0" "$S/watch.out" || fail "watcher exit code: $(tail -1 "$S/watch.out")"
grep -q "session_id=ses_bbbb" /tmp/t1hag/ledger-watch-beta.state || fail "sidecar not bound"
check "LEDGER-WAKE-TEST" timeout 20 $H ledger-watch --shoal t1hag --slug beta --wake exit --interval 60 --ledger-dir "$B" --no-board --test
echo "== 7 transition -> ledger state; heartbeat covers via ledger; systemd plan; status; doctor; report"
check "ledger" $H transition --shoal t1hag --slug alpha --to waiting --sub remote-ci --holder "gate 1350" --rescan-min 30
check "ledger" $H transition --shoal t1hag --slug beta --to active --ledger-dir "$B"
rm -f /tmp/t1hag/hb-beta   # beta's heartbeat is on "another host": ledger state must cover it
check "beta: fresh" $H heartbeat --spec "$SPEC" --threshold-min 60 --dry-run
check "lanes ledger:t1hag" $H install-systemd --spec "$SPEC" --dry-run
if $H install-systemd --spec "$SPEC" --dry-run | grep -q "queue-"; then fail "legacy queue units in ledger mode"; fi
check "ledger: .*events=" $H status --spec "$SPEC"
check "ledger lint" $H doctor --spec "$SPEC"
check "## Ledger economics" $H report --spec "$SPEC" --gen r1 --dry-run --no-pull
check "## Wake efficiency" $H report --spec "$SPEC" --gen r1 --dry-run --no-pull
check "§13 wasted wakes" $H report --spec "$SPEC" --gen r1 --dry-run --no-pull
check "ledger-watch --shoal t1hag --slug alpha --wake exit" $H brief --spec "$SPEC" --slug alpha
check "wake send:" $H brief --spec "$SPEC" --slug beta
echo "== 8 flattened only: a role-written offer is a projection violation; removed transports are gone"
expect 0 $H ledger-lint --shoal t1hag >/dev/null
python3 -c "import sys; sys.path.insert(0, '$HAGD'); from ledger import Ledger; Ledger('t1hag', root='$A').append('beta', 'offer', to='alpha', item='interpretune#777:fix', base='interpretune@3ac6fdf9', prio=1)" >/dev/null
expect 1 $H ledger-lint --shoal t1hag >/dev/null
check "E1 .* beta offer interpretune#777: beta may not offer to alpha" $H ledger-lint --shoal t1hag
if $H show --shoal t1hag --slug alpha | grep -q "interpretune#777"; then fail "role-written offer honoured by the projection"; fi
check "permission violations ignored: 1" $H show --shoal t1hag
expect 1 $H watch --shoal t1hag pipeline ses_ffff /tmp >/dev/null   # session-addressed fires removed
expect 2 $H queue-watch --sidecar /dev/null --interval 1 2>/dev/null
expect 2 $H lint-queues 2>/dev/null
expect 2 $H start --name t --dir /tmp --role a,claude,a,/dev/null,coord --legacy-queues --dry-run 2>/dev/null
echo "== 9 relocation: no ledger repo configured -> explicit --ledger-repo; relative role docs"
check "pass --ledger-repo" $H start --name t --dir "$WORK" --role "a,claude,a,$DOC_A" --dry-run
expect 1 $H start --name t --dir "$WORK" --role "a,claude,a,$DOC_A" --dry-run >/dev/null
python3 -c "import json,sys; d=json.load(open('$SPEC')); d.pop('ledger'); d['hag']='t2hag'; json.dump(d, open('$S/noledger.json','w'))"
check "pass --ledger-repo" $H ledger-init --spec "$S/noledger.json" --dry-run
check "\[dry-run\] ledger for t2hag: clone $S/remote/ledgers.git" $H ledger-init --spec "$S/noledger.json" --ledger-repo "$S/remote/ledgers.git" --dry-run
mkdir -p "$S/rel/t1hag"
python3 -c "
import json; d=json.load(open('$SPEC'))
d['roles'][0]['doc']='alpha_plan_state.md'; d['roles'][1]['doc']='./sub/../beta_plan_state.md'
for r in d['roles']: r['session']=''   # no live sessions: hag_checks probes no harness
json.dump(d, open('$S/rel/t1hag/hag.json','w'))"
python3 - "$HAGD" "$S/rel/t1hag/hag.json" > "$S/doctor-rel.out" 2>&1 <<'PY' || true
import sys; sys.path.insert(0, sys.argv[1])
import doctor
from roles import HagSpec
d = doctor.Doc()
doctor.hag_checks(d, HagSpec.load(sys.argv[2]), sys.argv[2], 60)
PY
grep -q "WARN doc path alpha: relative role doc 'alpha_plan_state.md'" "$S/doctor-rel.out" || fail "doctor did not warn on a relative doc :: $(cat "$S/doctor-rel.out")"
check "\[dry-run\] alpha: alpha_plan_state.md -> $DOCS/alpha_plan_state.md" $H spec absolutize-docs --spec "$S/rel/t1hag/hag.json" --base "$DOCS" --dry-run
grep -q '"doc": "alpha_plan_state.md"' "$S/rel/t1hag/hag.json" || fail "dry-run rewrote the spec"
check "rewrote 2 role doc(s)" $H spec absolutize-docs --spec "$S/rel/t1hag/hag.json" --base "$DOCS"
python3 -c "
import json; r=json.load(open('$S/rel/t1hag/hag.json'))['roles']
assert r[0]['doc']=='$DOCS/alpha_plan_state.md' and r[1]['doc']=='$DOCS/beta_plan_state.md' and r[2]['doc']=='$DOC_O', r" \
  || fail "absolutize-docs wrote the wrong paths"
check "nothing to change" $H spec absolutize-docs --spec "$S/rel/t1hag/hag.json" --base "$DOCS"
echo "== 10 rename compatibility: --hag alias, legacy hag.py system writer, HAG_* env fallback"
diff <($H ledger-lint --shoal t1hag 2>&1) <($H ledger-lint --hag t1hag 2>&1) >/dev/null \
  || fail "--hag alias does not behave like --shoal"
if $H ledger-lint --help | grep -q -- "--hag"; then fail "--hag alias shows in help"; fi
python3 - "$HAGD" "$SPEC" "$A" <<'PY2' || fail "legacy or current system-writer offer not honoured"
import sys; sys.path.insert(0, sys.argv[1])
from ledger import Ledger, View, SYSTEM_WRITER
from roles import HagSpec
assert SYSTEM_WRITER == "shoal", SYSTEM_WRITER
led = Ledger("t1hag", root=sys.argv[3])
led.append("hag.py", "offer", to="alpha", item="interpretune#778:fix", base="interpretune@3ac6fdf9", prio=1)
led.append(SYSTEM_WRITER, "offer", to="alpha", item="interpretune#779:fix", base="interpretune@3ac6fdf9", prio=1)
v = View(Ledger("t1hag", root=sys.argv[3]), HagSpec.load(sys.argv[2]))
items = {o["item"].split(":")[0] for o in v.offers.values() if o["status"] == "open"}
assert {"interpretune#778", "interpretune#779"} <= items, items
bad = [x for x in v.violations if "#778" in str(x) or "#779" in str(x)]
assert not bad, bad
PY2
[ "$(env -u SHOAL_DATA_ROOT HAG_DATA_ROOT="$S/legacy-root" python3 -c "import sys; sys.path.insert(0, '$HAGD'); import config; print(config.data_root())")" = "$S/legacy-root" ] \
  || fail "HAG_DATA_ROOT fallback not honoured"
[ "$(SHOAL_DATA_ROOT="$S/new-root" HAG_DATA_ROOT="$S/legacy-root" python3 -c "import sys; sys.path.insert(0, '$HAGD'); import config; print(config.data_root())")" = "$S/new-root" ] \
  || fail "SHOAL_DATA_ROOT does not win over HAG_DATA_ROOT"
echo "== layout: shoals/ for new groups, legacy hags/ + hag.json still read; next_max; wake relay slugs"
python3 - "$HAGD" "$S/layout" <<'PY3' || fail "layout / next_max / wake relay checks"
import os, sys
sys.path.insert(0, sys.argv[1])
from layout import group_dir, spec_path, is_legacy
root = sys.argv[2]
os.makedirs(os.path.join(root, "hags", "old1"))
open(os.path.join(root, "hags", "old1", "hag.json"), "w").write("{}")
assert group_dir(root, "old1").endswith("hags/old1") and spec_path(root, "old1").endswith("hags/old1/hag.json")
assert is_legacy(root, "old1")
assert group_dir(root, "new1").endswith("shoals/new1") and spec_path(root, "new1").endswith("shoals/new1/shoal.json")
assert not is_legacy(root, "new1")
from ledger import queue_next_max, QUEUE_NEXT_MAX
class S:  # a stand-in spec
    def __init__(self, f): self.features = f
assert queue_next_max(S({"workwake": True})) == QUEUE_NEXT_MAX == 3
assert queue_next_max(S({"workwake": {"next_max": 5}})) == 5
assert queue_next_max(S({"workwake": {"next_max": "x"}})) == 3
from roles import Role, is_wake_relay
assert is_wake_relay(Role(slug="wake-relay", ecosystem="claude", title="", doc="", fill="none"))
assert is_wake_relay(Role(slug="hag-relay", ecosystem="claude", title="", doc="", fill="none"))
assert not is_wake_relay(Role(slug="wake-relay", ecosystem="claude", title="", doc="", fill="self"))
from digest import has_affinity
assert not has_affinity({"affinity": {}}) and has_affinity({"affinity": {"repos": {"a/b": 1}}})
print("layout ok")
PY3
echo "== opencode store: v1 and v2 layouts (provider-error scan, quota check)"
python3 - "$HAGD" "$S/ocstore" <<'PY3' || fail "opencode store layout handling"
import os, sqlite3, sys, time
sys.path.insert(0, sys.argv[1])
root = sys.argv[2]; os.makedirs(root, exist_ok=True)
now = int(time.time() * 1000)
err = '{"role":"assistant","error":{"message":"Upstream request failed: reasoning encrypted_content was not issued to this caller"}}'
quota = '{"role":"assistant","error":{"message":"insufficient_quota: resets tomorrow 09:00."}}'
def v1(p):
    c = sqlite3.connect(p); c.execute("CREATE TABLE message (id TEXT, session_id TEXT, time_created INTEGER, time_updated INTEGER, data TEXT)")
    c.execute("INSERT INTO message VALUES ('m1','ses_a',?,?,?)", (now, now, err)); c.commit(); c.close()
def v2(p):
    v1(p)  # the 1.x tables stay after migration and stop growing
    c = sqlite3.connect(p)
    c.execute("CREATE TABLE session_v2 (id TEXT)")
    c.execute("CREATE TABLE session_message (id TEXT, session_id TEXT, type TEXT, seq INTEGER, time_created INTEGER, time_updated INTEGER, data TEXT)")
    rows = [("n1", "ses_a", "assistant", err), ("n2", "ses_a", "assistant", err), ("n3", "ses_a", "user", err),
            ("n4", "ses_b", "assistant", quota)]
    for i, sid, typ, d in rows:
        c.execute("INSERT INTO session_message VALUES (?,?,?,1,?,?,?)", (i, sid, typ, now, now, d))
    c.commit(); c.close()
import doctor, opencode_store
from backends import OpenChamberBackend
for name, build, want_store, want_total in (("v1.db", v1, "v1", 1), ("v2.db", v2, "v2", 2)):
    p = os.path.join(root, name); build(p); os.environ["OPENCODE_DB"] = p
    out = doctor.provider_failures(["ses_a"], hours=1)
    assert out["store"] == want_store and out["total"] == want_total, (name, out)
os.environ["OPENCODE_DB"] = os.path.join(root, "v2.db")
assert "tomorrow" in OpenChamberBackend().quota_paused("ses_b"), "v2 quota row not found"
assert OpenChamberBackend().quota_paused("ses_a") == ""
print("opencode store ok")
PY3
python3 - "$HAGD" <<'PY3' || fail "claude --bg id not parsed through colour codes"
import sys
sys.path.insert(0, sys.argv[1])
from backends import parse_bg_id
assert parse_bg_id("backgrounded · \x1b[36mcdb29a7f\x1b[39m · role-r1").group(1) == "cdb29a7f"
assert parse_bg_id("backgrounded · abc123 · role-r1").group(1) == "abc123"
assert parse_bg_id("error: no session") is None
PY3
echo "== wait targets: normalization and validation"
SHOAL_DEFAULT_OWNER=acme python3 - "$HAGD" <<'PY3' || fail "wait target normalization"
import sys
sys.path.insert(0, sys.argv[1])
from providers import normalize
assert normalize("gh-pr:interpretune:637") == ("gh-pr:acme/interpretune#637", ""), normalize("gh-pr:interpretune:637")
assert normalize("gh-pr:acme/interpretune#637")[0] == "gh-pr:acme/interpretune#637"
assert normalize("gh-issue:decoderesearch/interp-engine#28")[0] == "gh-issue:decoderesearch/interp-engine#28"
assert normalize("gh-pr:no-number-here")[1], "unparseable gh-pr accepted"
assert normalize("gh-run:acme/x:12345") == ("gh-run:acme/x:12345", "")
assert normalize("gh-run:12345")[1], "gh-run without a repo accepted"
assert "no provider" in normalize("bogus:1")[1]
assert normalize("lease:gpu") == ("lease:gpu", "")
print("targets ok")
PY3
echo "== skills: stale user-scope copies are reported, then synced"
python3 - "$HAGD" <<'PY3' || fail "skill copy sync"
import os, sys
sys.path.insert(0, sys.argv[1])
import config, skills
homes = config.skill_homes()
assert homes and all("skillhomes" in h for h in homes), homes  # isolated from the real homes
assert len(skills.stale()) == len(homes) * len(skills.names())
assert len(skills.sync()) == len(homes) * len(skills.names()) and skills.stale() == []
p = os.path.join(homes[0], "shoal-protocol", "SKILL.md"); open(p, "a").write("\nlocal edit\n")
assert skills.stale() == [os.path.join(homes[0], "shoal-protocol")]
print("skills ok")
PY3
rm -rf /tmp/t1hag
echo "SELFTEST PASS ($S)"
