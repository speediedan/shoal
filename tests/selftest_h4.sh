#!/usr/bin/env bash
# Isolated proof of the H4 components (architecture doc §4-§6): working context, board
# digest, batch triage, digest-driven ledger-watch wake, exit verdicts, job units, the work loop
# (next/queue), work-only wakes, the health quorum, user-offline mode and the Claude relay.
# Touches nothing live: a bare local remote, HAG_ROOT/HAG_DATA_ROOT under a scratch dir,
# hag id h4selftest (state in /tmp/h4selftest). Reads the real GitHub board (read-only).
#   selftest_h4.sh [SCRATCH_DIR]
set -eu
HAGD="$(cd "$(dirname "$0")" && pwd)"
S="${1:-${TMPDIR:-/tmp}/hag-h4-selftest-$$}"
# background watchers this test starts must never outlive it (a stray one shares the
# sidecar and silently swallows the next run's wakes)
cleanup() { pkill -f -- 'hag.py (ledger-watch|relay-watch) --hag h4selftest' 2>/dev/null || true; }
trap cleanup EXIT
cleanup
rm -rf "$S" /tmp/h4selftest; mkdir -p "$S/root" "$S/data" "$S/remote"
export HAG_ROOT="$S/root" HAG_DATA_ROOT="$S/data"
HAGID=h4selftest; SPEC="$HAG_ROOT/$HAGID/hag.json"; A="$S/cloneA"
H="python3 $HAGD/hag.py"
fail() { echo "SELFTEST FAIL: $*" >&2; exit 1; }
check() { local pat=$1; shift; local out; out=$("$@" 2>&1 || true); printf "%s\n" "$out" > "$S/last_check.out"; grep -q -- "$pat" <<<"$out" || fail "expected '$pat' in: $* :: $(tail -c 600 <<<"$out")"; }
rc_of() { "$@" >/dev/null 2>&1 && echo 0 || echo $?; }

git init -q --bare "$S/remote/q.git"; git clone -q "$S/remote/q.git" "$A" 2>/dev/null
( cd "$A" && git commit -q --allow-empty -m init && git push -q origin HEAD 2>/dev/null )
mkdir -p "$HAG_ROOT/$HAGID"
DOCS=/home/speediedan/repos/distributed-insight/project_admin/interpretune/design
cat > "$SPEC" <<JSON
{"hag":"$HAGID","dir":"/home/speediedan/repos/interpretune","incarnation":0,"repos":["speediedan/interpretune"],
 "ledger":{"repo":"$S/remote/q.git","dir":"$A"}, "features":{"digest":true},
 "sources":[{"kind":"github-project","owner":"speediedan","number":2}],
 "roles":[
  {"slug":"tl","ecosystem":"claude","title":"tl","doc":"$DOCS/ie_integration_hub/handoffs/it-architect.md","fill":"self","session":"aaaa1111","scope":{"labels_any":["tl-4-migration"]}},
  {"slug":"up","ecosystem":"claude","title":"up","doc":"$DOCS/ie_integration_hub/ie_integration_hub_plan_state.md","fill":"self","session":"bbbb2222"}
 ],"generations":[{"gen":"r1","incarnation":0,"started_utc":"2026-09-22T00:00:00Z","ended_utc":"","sessions":{},"run":{}}]}
JSON
$H ledger-init --spec "$SPEC" >/dev/null

echo "== 1 working context: scope derives, migrate converts and removes scope"
check '"tl-4-migration": 3.0' $H context show --hag $HAGID --spec "$SPEC" --slug tl
check '\[dry-run\] would update context' $H context migrate --hag $HAGID --spec "$SPEC" --dry-run
$H context migrate --hag $HAGID --spec "$SPEC" >/dev/null
python3 - "$SPEC" <<'PY' || fail "migrate did not move scope into context"
import json,sys; r={x["slug"]:x for x in json.load(open(sys.argv[1]))["roles"]}
assert r["tl"]["scope"]=={} and r["tl"]["context"]["affinity"]["labels"]["tl-4-migration"]==3.0
PY
cat > "$S/up_ctx.json" <<'JSON'
{"brief":"Drafts upstream issue and PR bodies for repos we do not own; never edits in-tree code.",
 "affinity":{"repos":{"speediedan/interpretune":-1.0},"keywords":{"upstream":2.0,"TransformerLens":1.0,"SAELens":1.5}},
 "never":{"labels":["maintainer-only"]}}
JSON
$H context set --hag $HAGID --spec "$SPEC" --slug up --file "$S/up_ctx.json" >/dev/null
check '"upstream": 2.0' $H context show --hag $HAGID --spec "$SPEC" --slug up
# contexts are per-HAG orchestration state in the data dir: export, edit there, load back
check "wrote $S/data/$HAGID/contexts/tl.json" $H context export --hag $HAGID --spec "$SPEC"
python3 - "$S/data/$HAGID/contexts/tl.json" <<'PY'
import json,sys; p=sys.argv[1]; d=json.load(open(p)); d["affinity"].setdefault("keywords",{})["HookedTransformer"]=2.5; json.dump(d,open(p,"w"))
PY
check "context load: \['tl'\]" $H context load --hag $HAGID --spec "$SPEC"
check '"HookedTransformer": 2.5' $H context show --hag $HAGID --spec "$SPEC" --slug tl
check "nothing to change" $H context load --hag $HAGID --spec "$SPEC"

echo "== 2 digest: one board read, ranked per role, written to the data root"
check '\[dry-run\] digest h4selftest' $H digest --hag $HAGID --spec "$SPEC" --refresh --dry-run --no-sync
[ ! -f "$S/data/$HAGID/board_digest.json" ] || fail "dry-run wrote a digest"
check 'open items' $H digest --hag $HAGID --spec "$SPEC" --refresh --no-sync
[ -f "$S/data/$HAGID/board_digest.json" ] || fail "digest not written"
check 'BOARD ' $H digest --hag $HAGID --spec "$SPEC" --slug tl --limit 3
TOP=$(python3 -c "import json;d=json.load(open('$S/data/$HAGID/board_digest.json'));print(d['roles']['tl'][0]['item'])")
SECOND=$(python3 -c "import json;d=json.load(open('$S/data/$HAGID/board_digest.json'));print(d['roles']['tl'][1]['item'])")
THIRD=$(python3 -c "import json;d=json.load(open('$S/data/$HAGID/board_digest.json'));print(d['roles']['tl'][2]['item'])")
echo "   tl top3: $TOP $SECOND $THIRD"

echo "== 3 triage: claim first-landed-wins, passes as ONE commit, suggest validated"
[ "$(rc_of $H triage --hag $HAGID --spec "$SPEC" --slug up --pass "$TOP:bogus-class")" = 1 ] || fail "bad class accepted"
[ "$(rc_of $H triage --hag $HAGID --spec "$SPEC" --slug up --pass "$TOP:out-of-lane:suggest=nobody")" = 1 ] || fail "bad suggest accepted"
check '\[dry-run\] pass' $H triage --hag $HAGID --spec "$SPEC" --slug up --pass "$TOP:out-of-lane:suggest=tl" --dry-run
N0=$(git -C "$A" rev-list --count HEAD)
check 'passed 2 item(s) in one commit' $H triage --hag $HAGID --spec "$SPEC" --slug up \
   --pass "$TOP:out-of-lane:suggest=tl:note=in-tree TL work" --pass "$SECOND:out-of-lane"
N1=$(git -C "$A" rev-list --count HEAD)
[ $((N1 - N0)) = 1 ] || fail "triage passes made $((N1 - N0)) commits (want 1)"
check "claimed" $H triage --hag $HAGID --spec "$SPEC" --slug tl --claim "$THIRD:fix"
check "CLAIM-REFUSED" $H triage --hag $HAGID --spec "$SPEC" --slug up --claim "$THIRD:fix"

echo "== 4 routing: a suggest= decline boosts the suggested role on the next refresh"
$H digest --hag $HAGID --spec "$SPEC" --refresh --no-sync >/dev/null
python3 - "$S/data/$HAGID/board_digest.json" "$TOP" <<'PY' || fail "routing bonus not applied"
import json,sys; d=json.load(open(sys.argv[1])); top=sys.argv[2]
row=[r for r in d["roles"]["tl"] if r["item"]==top]
assert row and any(w.startswith("routed") for w in row[0]["why"]), row
assert all(r["item"]!=top for r in d["roles"]["up"]), "decliner still sees its declined item"
PY

echo "== 5 ledger-watch reads the digest (no board call) and records its exit verdict"
OUT=$($H ledger-watch --hag $HAGID --spec "$SPEC" --slug tl --wake exit --interval 1 --no-sync 2>&1 || true)
grep -q "score=" <<<"$OUT" || fail "watch wake did not carry digest candidates :: $OUT"
grep -q "VERDICT WAKE" <<<"$OUT" || fail "no exit verdict line :: $OUT"
grep -q "^exit=.* WAKE " /tmp/$HAGID/ledger-watch-tl.state || fail "sidecar lacks exit verdict"

echo "== 6 job units: success, failure exit code, external kill, idempotent waiter"
check "\[dry-run\] systemd-run --user --unit hag-$HAGID-tl-ok" $H job run --hag $HAGID --slug tl --name ok --dry-run -- true
check "JOB-STARTED" $H job run --hag $HAGID --slug tl --name ok --memory-high 1G -- bash -c 'echo job-ok-output; sleep 2'
check "JOB-ALREADY-RUNNING" $H job run --hag $HAGID --slug tl --name ok -- true
check "result=success exit=0" $H job wait --hag $HAGID --slug tl --name ok --poll 1
check "job-ok-output" $H job wait --hag $HAGID --slug tl --name ok --poll 1   # re-armed waiter: same verdict
$H job run --hag $HAGID --slug tl --name bad --memory-high 1G -- bash -c 'exit 3' >/dev/null
check "result=failed exit=3" $H job wait --hag $HAGID --slug tl --name bad --poll 1
$H job run --hag $HAGID --slug tl --name kill --memory-high 1G -- sleep 60 >/dev/null; sleep 1
systemctl --user kill --signal=KILL hag-$HAGID-tl-kill.service
check "UNIT-GONE-NO-STATUS" $H job wait --hag $HAGID --slug tl --name kill --poll 1
check "NOT-FOUND" $H job wait --hag $HAGID --slug tl --name never --poll 1
check "hag-$HAGID-tl-ok" $H job status --hag $HAGID

echo "== 7 work loop: next reads, queue writes ONE commit (claim + posture + version)"
python3 - "$SPEC" <<'PY'
import json,sys; p=sys.argv[1]; d=json.load(open(p))
d["features"]={"digest":True,"jobs":True,"workwake":True,"health":{"validity_wake_min":90}}
json.dump(d,open(p,"w"),indent=1)
PY
check "\[dry-run\] relay-init $HAGID: doc $S/data/$HAGID/roles/hag-relay/plan_state.md; role ADD" $H relay-init --hag $HAGID --spec "$SPEC" --dry-run
check "role ADD" $H relay-init --hag $HAGID --spec "$SPEC"
grep -qi "current state" "$S/data/$HAGID/roles/hag-relay/plan_state.md" || fail "relay doc lacks the current-state section restart verifies"
python3 - "$SPEC" <<'PY' || fail "relay-init did not add the role / feature"
import json,sys; d=json.load(open(sys.argv[1])); r=[x for x in d["roles"] if x["slug"]=="hag-relay"]
assert r and r[0]["fill"]=="none" and d["features"].get("relay") is True, (r, d["features"])
r[0]["session"]="rrrr3333"; json.dump(d,open(sys.argv[1],"w"),indent=1)
PY
check "role present" $H relay-init --hag $HAGID --spec "$SPEC"
git -C "$A" add -A >/dev/null 2>&1; git -C "$A" commit -qm spec >/dev/null 2>&1 || true
check "QUEUE v0" $H next --hag $HAGID --spec "$SPEC" --slug up --no-sync
I1=interpretune#9101; I2=interpretune#9102; I3=interpretune#9103; I4=interpretune#9104
check "\[dry-run\] up queue v1" $H queue --hag $HAGID --spec "$SPEC" --slug up --active $I1:fix --next $I2,$I3 --dry-run --no-sync
N0=$(git -C "$A" rev-list --count HEAD)
check "up queue v1: active=$I1:fix" $H queue --hag $HAGID --spec "$SPEC" --slug up --active $I1:fix --next $I2,$I3 --reason "two known" --no-sync
N1=$(git -C "$A" rev-list --count HEAD); [ $((N1 - N0)) = 1 ] || fail "queue made $((N1 - N0)) commits (want 1)"
check "CLAIM-REFUSED" $H queue --hag $HAGID --spec "$SPEC" --slug tl --active $I1:fix --no-sync
check "QUEUE v1 .*active=$I1" $H next --hag $HAGID --spec "$SPEC" --slug up --no-sync
check "up queue v2: active=$I2 next=$I3 posture=active.*done=\['$I1:fix'\]" $H queue --hag $HAGID --spec "$SPEC" --slug up --done $I1:fix --url https://example.invalid/pr/1 --active $I2 --next $I3 --no-sync
check "posture=waiting/remote-ci" $H queue --hag $HAGID --spec "$SPEC" --slug up --wait remote-ci --holder "gate 1" --target job:hag-$HAGID-none --no-sync
grep -q '"target": "job:hag-h4selftest-none"' /tmp/$HAGID/wait-up || fail "wait proof lacks target"
[ "$(rc_of $H queue --hag $HAGID --spec "$SPEC" --slug up --next a,b,c,d --no-sync)" = 1 ] || fail "4 next items accepted"

echo "== 8 wakes are work-only: busy roles are not woken by offers or peer mail"
$H queue --hag $HAGID --spec "$SPEC" --slug tl --active $I4:fix --next "" --no-sync >/dev/null
$H offer --hag $HAGID --spec "$SPEC" --to tl --item interpretune#9201:fix --no-sync >/dev/null
$H msg --hag $HAGID --spec "$SPEC" --by up --to tl --text "peer chatter" --no-sync >/dev/null
python3 - "$HAGD" "$SPEC" "$A" <<'PY' || fail "busy-role gating wrong"
import sys; sys.path.insert(0,sys.argv[1])
from roles import HagSpec; from ledger import Ledger, View; from ledger_watch import actionable
s=HagSpec.load(sys.argv[2]); v=View(Ledger(s.hag,root=sys.argv[3],sync=False),s)
acts=[d for _,d in actionable(v,"tl",s.role("tl"),s,board=True)]
assert not any(d.startswith(("OFFER","MSG","BOARD")) for d in acts), acts
PY
$H offer --hag $HAGID --spec "$SPEC" --to tl --item interpretune#9202:fix --now --no-sync >/dev/null
$H msg --hag $HAGID --spec "$SPEC" --by user --to tl --text "maintainer ping" --wake --no-sync >/dev/null
python3 - "$HAGD" "$SPEC" "$A" <<'PY' || fail "--now offer / user --wake msg did not wake the busy role"
import sys; sys.path.insert(0,sys.argv[1])
from roles import HagSpec; from ledger import Ledger, View; from ledger_watch import actionable
s=HagSpec.load(sys.argv[2]); v=View(Ledger(s.hag,root=sys.argv[3],sync=False),s)
acts=[d for _,d in actionable(v,"tl",s.role("tl"),s,board=True)]
assert any("9202" in d and "NOW" in d for d in acts) and any("maintainer ping" in d for d in acts), acts
assert not any("9201" in d or "peer chatter" in d for d in acts), acts
PY
$H queue --hag $HAGID --spec "$SPEC" --slug tl --idle --next "" --no-sync >/dev/null
python3 - "$HAGD" "$SPEC" "$A" <<'PY' || fail "idle role not woken by a plain offer"
import sys; sys.path.insert(0,sys.argv[1])
from roles import HagSpec; from ledger import Ledger, View; from ledger_watch import actionable
s=HagSpec.load(sys.argv[2]); v=View(Ledger(s.hag,root=sys.argv[3],sync=False),s)
acts=[d for _,d in actionable(v,"tl",s.role("tl"),s,board=True)]
assert any("9201" in d for d in acts) and not any("peer chatter" in d for d in acts), acts
PY
check "MAIL" $H next --hag $HAGID --spec "$SPEC" --slug tl --no-sync

echo "== 9 health quorum: leeway, ladder, adapters, peer check, validity, resolved waits"
check "health-a: ok" env HAG_HEALTH_SHIFT_MIN=0 $H health --spec "$SPEC" --instance a --dry-run
$H offer --hag $HAGID --spec "$SPEC" --to tl --item interpretune#9203:fix --no-sync >/dev/null   # arrives after tl's last queue version
setsid $H ledger-watch --hag $HAGID --spec "$SPEC" --slug tl --wake print --interval 2 --no-sync --no-board >"$S/tl-watch.log" 2>&1 &
TLW=$!
for i in $(seq 1 30); do grep -q "^pid=$TLW$" /tmp/$HAGID/ledger-watch-tl.state 2>/dev/null && break; sleep 0.5; done
grep -q "^pid=$TLW$" /tmp/$HAGID/ledger-watch-tl.state || fail "tl watcher never bound its sidecar"
check "tl unacked-work step1:wake -> wake-file" env HAG_HEALTH_SHIFT_MIN=11 $H health --spec "$SPEC" --instance a
[ -f /tmp/$HAGID/wake-tl ] || fail "wake request file not written"
sleep 5; grep -q "HEALTH unacked-work" "$S/tl-watch.log" || fail "live ledger-watch did not deliver the wake request :: $(tail -3 $S/tl-watch.log)"
check "tl unacked-work step2:wake" env HAG_HEALTH_SHIFT_MIN=31 $H health --spec "$SPEC" --instance a --dry-run
$H queue --hag $HAGID --spec "$SPEC" --slug tl --active interpretune#9201:fix --next "" --no-sync >/dev/null
[ ! -f /tmp/$HAGID/wake-tl ] || fail "queue write did not acknowledge the wake request"
kill $TLW 2>/dev/null || true
check "up wait-resolved step1:wake" env HAG_HEALTH_SHIFT_MIN=27 $H health --spec "$SPEC" --instance a
check "up wait-validity step1:wake" env HAG_HEALTH_SHIFT_MIN=91 $H health --spec "$SPEC" --instance b --dry-run
echo "2026-01-01T00:00:00Z" > /tmp/$HAGID/tick-health-b
check "user peer-dead" $H health --spec "$SPEC" --instance a --dry-run

echo "== 10 Claude relay: dead watcher -> relay queue -> relay-watch exits with the SendMessage line"
setsid $H relay-watch --hag $HAGID --interval 1 >"$S/relay.log" 2>&1 &
RW=$!
for i in $(seq 1 30); do [ "$(cat /tmp/$HAGID/relay-watch.pid 2>/dev/null)" = "$RW" ] && break; sleep 0.5; done
python3 - "$HAGD" "$SPEC" <<'PY' | grep -q "^relay$" || fail "request_wake did not choose the relay"
import sys; sys.path.insert(0,sys.argv[1])
from roles import HagSpec; from health import request_wake
print(request_wake(HagSpec.load(sys.argv[2]), "tl", "relay test", "watcher-dead"))
PY
sleep 3; grep -q "RELAY-WAKE to=tl-r1" "$S/relay.log" || fail "relay-watch did not deliver :: $(cat $S/relay.log)"
grep -q "VERDICT RELAY-DELIVER 1" "$S/relay.log" || fail "relay-watch verdict missing"
kill $RW 2>/dev/null || true

echo "== 11 user-offline mode: banner, asks, deferred pages, back-online summary"
$H queue --hag $HAGID --spec "$SPEC" --slug tl --ask "merge interpretune#9201 (PR green)" --no-sync >/dev/null
check "user-offline on" $H offline on --hag $HAGID --spec "$SPEC" --until tomorrow --note "travel" --no-sync
check "USER OFFLINE since" $H next --hag $HAGID --spec "$SPEC" --slug tl --no-sync
DEF=""; for i in 1 2 3 4; do DEF+=$(env HAG_HEALTH_SHIFT_MIN=200 $H health --spec "$SPEC" --instance a 2>&1); done
grep -q "deferred (user offline)" <<<"$DEF" || fail "no page was deferred while offline :: $(tail -c 400 <<<"$DEF")"
check "ASK tl: merge interpretune#9201" $H offline off --hag $HAGID --spec "$SPEC" --no-sync
check "user deferred pages -> page" $H health --spec "$SPEC" --instance a --dry-run

echo "== 12 wait discipline: asks, blockers, rescan caps, expiry and blocking wakes, labels, peek"
python3 - "$SPEC" <<'PY'
import json,sys; p=sys.argv[1]; d=json.load(open(p)); d["features"]["waitdiscipline"]=True; json.dump(d,open(p,"w"),indent=1)
PY
check "tl (tl-r1)" $H show --hag $HAGID --spec "$SPEC" --no-sync
check "IDENTITY: your Lane row label is \`tl (tl-r1)\`" $H next --hag $HAGID --spec "$SPEC" --slug tl --no-sync --peek
[ "$(rc_of $H queue --hag $HAGID --spec "$SPEC" --slug up --asks-clear --wait user-design --holder maintainer --no-sync)" = 3 ] || fail "user wait accepted without an ask"
check "a user-design wait needs the prompted ask" $H queue --hag $HAGID --spec "$SPEC" --slug up --asks-clear --wait user-design --holder maintainer --no-sync
check "clamped to the user-design cap 240m" $H queue --hag $HAGID --spec "$SPEC" --slug up --wait user-design --holder maintainer \
   --ask "Who owns #9301: A, B, or hold?" --rescan-min 720 --no-sync
grep -q '"rescan_utc"' /tmp/$HAGID/wait-up || fail "no wait proof"
python3 - /tmp/$HAGID/wait-up <<'PY' || fail "rescan not clamped in the wait proof"
import json,sys,datetime as d; w=json.load(open(sys.argv[1]))
f=lambda s: d.datetime.strptime(s,"%Y-%m-%dT%H:%M:%SZ"); assert (f(w["rescan_utc"])-f(w["since_utc"])).total_seconds()<=240*60+5, w
PY
[ "$(rc_of $H transition --hag $HAGID --spec "$SPEC" --slug tl --to waiting --sub session-blocked --holder "up")" = 3 ] || fail "session-blocked accepted without --blocked-on"
check "posture=waiting/session-blocked" $H queue --hag $HAGID --spec "$SPEC" --slug tl --wait session-blocked --holder "up #9301" \
   --blocked-on up:interpretune#9301 --no-sync
check "BLOCKING tl since .* on interpretune#9301 -- NOT in your queue" $H next --hag $HAGID --spec "$SPEC" --slug up --no-sync --peek
check "BLOCKING tl on interpretune#9301" $H show --hag $HAGID --spec "$SPEC" --slug up --no-sync
check "up blocking-unscheduled step1:wake" env HAG_HEALTH_SHIFT_MIN=31 $H health --spec "$SPEC" --instance a --dry-run
check "wait-expired step1:wake" env HAG_HEALTH_SHIFT_MIN=245 $H health --spec "$SPEC" --instance b --dry-run
M0=$(cat /tmp/$HAGID/mail-read-up 2>/dev/null || echo none)
$H next --hag $HAGID --spec "$SPEC" --slug up --no-sync --peek >/dev/null
[ "$(cat /tmp/$HAGID/mail-read-up 2>/dev/null || echo none)" = "$M0" ] || fail "--peek consumed the role's mail"
$H claim --hag $HAGID --spec "$SPEC" --slug up --item interpretune#9401:fix --no-sync >/dev/null
check "HELD-UNSCHEDULED interpretune#9401" $H queue --hag $HAGID --spec "$SPEC" --slug up --active interpretune#9301:fix --next "" --reason test --no-sync
# a holder hands its claim to a named role: an implicit offer the recipient can claim
[ "$(rc_of $H release --hag $HAGID --spec "$SPEC" --slug up --item interpretune#9401 --kind decline --to tl --no-sync)" = 1 ] || fail "--to accepted on a decline"
check "handed interpretune#9401 to tl" $H release --hag $HAGID --spec "$SPEC" --slug up --item interpretune#9401 --kind handoff --to tl \
   --reason "maintainer ruling: tl owns it" --no-sync
check "OFFER interpretune#9401.* by=up" $H next --hag $HAGID --spec "$SPEC" --slug tl --no-sync --peek
check "claimed interpretune#9401" $H claim --hag $HAGID --spec "$SPEC" --slug tl --item interpretune#9401:fix --no-sync

echo "SELFTEST H4 OK ($S)"
rm -rf /tmp/$HAGID
