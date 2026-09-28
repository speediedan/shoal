#!/usr/bin/env bash
# Isolated proof of the H4 components (architecture doc §4-§6): working context, board
# digest, batch triage, digest-driven ledger-watch wake, exit verdicts, job units, the work loop
# (next/queue), work-only wakes, the health quorum, user-offline mode and the Claude relay.
# Touches nothing live: a bare local remote, the spec and SHOAL_DATA_ROOT under a scratch dir,
# shoal id h4selftest (state in /tmp/h4selftest). Hermetic: a synthetic board fixture (no
# GitHub read), synthetic start docs, a scratch working dir, a scratch host config, stub
# lease providers. Job-unit sections need systemd --user and print SKIP without it.
#   selftest_h4.sh [SCRATCH_DIR]
set -eu
TESTD="$(cd "$(dirname "$0")" && pwd)"
SHOALD="$(cd "$TESTD/../shoal" && pwd)"
. "$TESTD/selftest_lib.sh"
S="${1:-${TMPDIR:-/tmp}/shoal-h4-selftest-$$}"
# background watchers this test starts must never outlive it (a stray one shares the
# sidecar and silently swallows the next run's wakes)
cleanup() { pkill -f -- '(cli\.py|shoal) (ledger-watch|relay-watch) --shoal h4selftest' 2>/dev/null || true; }
trap cleanup EXIT
cleanup
rm -rf "$S" /tmp/h4selftest; mkdir -p "$S/root" "$S/data" "$S/remote"
shoal_test_setup "$S"
ROOT="$S/root"
SID=h4selftest; SPEC="$ROOT/shoals/$SID/shoal.json"; A="$S/cloneA"
export SHOAL_SPEC="$SPEC"
H="python3 $SHOALD/cli.py"
fail() { echo "SELFTEST FAIL: $*" >&2; exit 1; }
check() { local pat=$1; shift; local out; out=$("$@" 2>&1 || true); printf "%s\n" "$out" > "$S/last_check.out"; grep -q -- "$pat" <<<"$out" || fail "expected '$pat' in: $* :: $(tail -c 600 <<<"$out")"; }
rc_of() { "$@" >/dev/null 2>&1 && echo 0 || echo $?; }

git init -q --bare "$S/remote/q.git"; git clone -q "$S/remote/q.git" "$A" 2>/dev/null
( cd "$A" && git commit -q --allow-empty -m init && git push -q origin HEAD 2>/dev/null )
mkdir -p "$ROOT/shoals/$SID"
# synthetic board: TL4 migration items (tl's label), upstream items (up's keywords), one item
# `up` must never see, general work
shoal_board_fixture "$S/board.json" <<'JSON'
[{"repo":"speediedan/interpretune","number":8001,"title":"Remove the TL3 bridge shims","labels":["tl-4-migration"]},
 {"repo":"speediedan/interpretune","number":8002,"title":"Port HookedTransformer hooks to TL4","labels":["tl-4-migration"],"excerpt":"HookedTransformer hook names changed"},
 {"repo":"speediedan/interpretune","number":8003,"title":"TL4 config migration","labels":["tl-4-migration"]},
 {"repo":"speediedan/interpretune","number":8004,"title":"TL4 docs refresh","labels":["tl-4-migration"]},
 {"repo":"speediedan/interpretune","number":8005,"title":"File the upstream SAELens report","excerpt":"upstream SAELens issue draft"},
 {"repo":"speediedan/interpretune","number":8006,"title":"Maintainer release checklist","labels":["maintainer-only"]},
 {"repo":"speediedan/interpretune","number":8007,"title":"General cleanup"},
 {"repo":"speediedan/interpretune","number":8008,"title":"Upstream TransformerLens padding report","excerpt":"upstream TransformerLens"}]
JSON
DOC_TL=$(shoal_doc tl); DOC_UP=$(shoal_doc up)
cat > "$SPEC" <<JSON
{"shoal":"$SID","dir":"$WORK","incarnation":0,"repos":["speediedan/interpretune"],
 "ledger":{"repo":"$S/remote/q.git","dir":"$A"}, "features":{"digest":true},
 "sources":[{"kind":"github-project","owner":"example","number":2}],
 "roles":[
  {"slug":"tl","ecosystem":"claude","title":"tl","doc":"$DOC_TL","fill":"self","session":"aaaa1111","scope":{"labels_any":["tl-4-migration"]}},
  {"slug":"up","ecosystem":"claude","title":"up","doc":"$DOC_UP","fill":"self","session":"bbbb2222"}
 ],"generations":[{"gen":"r1","incarnation":0,"started_utc":"2026-09-22T00:00:00Z","ended_utc":"","sessions":{},"run":{}}]}
JSON
$H ledger-init --spec "$SPEC" >/dev/null

echo "== 1 working context: scope derives, migrate converts and removes scope"
check '"tl-4-migration": 3.0' $H context show --shoal $SID --spec "$SPEC" --slug tl
check '\[dry-run\] would update context' $H context migrate --shoal $SID --spec "$SPEC" --dry-run
$H context migrate --shoal $SID --spec "$SPEC" >/dev/null
python3 - "$SPEC" <<'PY' || fail "migrate did not move scope into context"
import json,sys; r={x["slug"]:x for x in json.load(open(sys.argv[1]))["roles"]}
assert r["tl"]["scope"]=={} and r["tl"]["context"]["affinity"]["labels"]["tl-4-migration"]==3.0
PY
cat > "$S/up_ctx.json" <<'JSON'
{"brief":"Drafts upstream issue and PR bodies for repos we do not own; never edits in-tree code.",
 "affinity":{"repos":{"speediedan/interpretune":-1.0},"keywords":{"upstream":2.0,"TransformerLens":1.0,"SAELens":1.5}},
 "never":{"labels":["maintainer-only"]}}
JSON
$H context set --shoal $SID --spec "$SPEC" --slug up --file "$S/up_ctx.json" >/dev/null
check '"upstream": 2.0' $H context show --shoal $SID --spec "$SPEC" --slug up
# contexts are per-shoal orchestration state in the data dir: export, edit there, load back
check "wrote $S/data/$SID/contexts/tl.json" $H context export --shoal $SID --spec "$SPEC"
python3 - "$S/data/$SID/contexts/tl.json" <<'PY'
import json,sys; p=sys.argv[1]; d=json.load(open(p)); d["affinity"].setdefault("keywords",{})["HookedTransformer"]=2.5; json.dump(d,open(p,"w"))
PY
check "context load: \['tl'\]" $H context load --shoal $SID --spec "$SPEC"
check '"HookedTransformer": 2.5' $H context show --shoal $SID --spec "$SPEC" --slug tl
check "nothing to change" $H context load --shoal $SID --spec "$SPEC"

echo "== 2 digest: one board read, ranked per role, written to the data root"
check '\[dry-run\] digest h4selftest' $H digest --shoal $SID --spec "$SPEC" --refresh --dry-run --no-sync
[ ! -f "$S/data/$SID/board_digest.json" ] || fail "dry-run wrote a digest"
check 'open items' $H digest --shoal $SID --spec "$SPEC" --refresh --no-sync
[ -f "$S/data/$SID/board_digest.json" ] || fail "digest not written"
check 'BOARD ' $H digest --shoal $SID --spec "$SPEC" --slug tl --limit 3
TOP=$(python3 -c "import json;d=json.load(open('$S/data/$SID/board_digest.json'));print(d['roles']['tl'][0]['item'])")
SECOND=$(python3 -c "import json;d=json.load(open('$S/data/$SID/board_digest.json'));print(d['roles']['tl'][1]['item'])")
THIRD=$(python3 -c "import json;d=json.load(open('$S/data/$SID/board_digest.json'));print(d['roles']['tl'][2]['item'])")
echo "   tl top3: $TOP $SECOND $THIRD"

echo "== 3 triage: claim first-landed-wins, passes as ONE commit, suggest validated"
[ "$(rc_of $H triage --shoal $SID --spec "$SPEC" --slug up --pass "$TOP:bogus-class")" = 1 ] || fail "bad class accepted"
[ "$(rc_of $H triage --shoal $SID --spec "$SPEC" --slug up --pass "$TOP:out-of-lane:suggest=nobody")" = 1 ] || fail "bad suggest accepted"
check '\[dry-run\] pass' $H triage --shoal $SID --spec "$SPEC" --slug up --pass "$TOP:out-of-lane:suggest=tl" --dry-run
N0=$(git -C "$A" rev-list --count HEAD)
check 'passed 2 item(s) in one commit' $H triage --shoal $SID --spec "$SPEC" --slug up \
   --pass "$TOP:out-of-lane:suggest=tl:note=in-tree TL work" --pass "$SECOND:out-of-lane"
N1=$(git -C "$A" rev-list --count HEAD)
[ $((N1 - N0)) = 1 ] || fail "triage passes made $((N1 - N0)) commits (want 1)"
check "claimed" $H triage --shoal $SID --spec "$SPEC" --slug tl --claim "$THIRD:fix"
check "CLAIM-REFUSED" $H triage --shoal $SID --spec "$SPEC" --slug up --claim "$THIRD:fix"

echo "== 4 routing: a suggest= decline boosts the suggested role on the next refresh"
$H digest --shoal $SID --spec "$SPEC" --refresh --no-sync >/dev/null
python3 - "$S/data/$SID/board_digest.json" "$TOP" <<'PY' || fail "routing bonus not applied"
import json,sys; d=json.load(open(sys.argv[1])); top=sys.argv[2]
row=[r for r in d["roles"]["tl"] if r["item"]==top]
assert row and any(w.startswith("routed") for w in row[0]["why"]), row
assert all(r["item"]!=top for r in d["roles"]["up"]), "decliner still sees its declined item"
PY

echo "== 5 ledger-watch reads the digest (no board call) and records its exit verdict"
OUT=$($H ledger-watch --shoal $SID --spec "$SPEC" --slug tl --wake exit --interval 1 --no-sync 2>&1 || true)
grep -q "score=" <<<"$OUT" || fail "watch wake did not carry digest candidates :: $OUT"
grep -q "VERDICT WAKE" <<<"$OUT" || fail "no exit verdict line :: $OUT"
grep -q "^exit=.* WAKE " /tmp/$SID/ledger-watch-tl.state || fail "sidecar lacks exit verdict"

echo "== 6 job units: success, failure exit code, external kill, idempotent waiter"
check "\[dry-run\] systemd-run --user --unit shoal-$SID-tl-ok" $H job run --shoal $SID --slug tl --name ok --dry-run -- true
if ! have_systemd; then
  echo "SKIP 6 job units (live): $(skip_reason)"
else
check "JOB-STARTED" $H job run --shoal $SID --slug tl --name ok --memory-high 1G -- bash -c 'echo job-ok-output; sleep 2'
check "JOB-ALREADY-RUNNING" $H job run --shoal $SID --slug tl --name ok -- true
check "result=success exit=0" $H job wait --shoal $SID --slug tl --name ok --poll 1
check "job-ok-output" $H job wait --shoal $SID --slug tl --name ok --poll 1   # re-armed waiter: same verdict
$H job run --shoal $SID --slug tl --name bad --memory-high 1G -- bash -c 'exit 3' >/dev/null
check "result=failed exit=3" $H job wait --shoal $SID --slug tl --name bad --poll 1
$H job run --shoal $SID --slug tl --name kill --memory-high 1G -- sleep 60 >/dev/null; sleep 1
systemctl --user kill --signal=KILL shoal-$SID-tl-kill.service
check "UNIT-GONE-NO-STATUS" $H job wait --shoal $SID --slug tl --name kill --poll 1
check "NOT-FOUND" $H job wait --shoal $SID --slug tl --name never --poll 1
check "shoal-$SID-tl-ok" $H job status --shoal $SID
# a health wait target names the job, not the unit: a live job whose name extends a finished
# one must read pending, the finished one resolved, an unknown one unknown (never resolved)
$H job run --shoal $SID --slug tl --name ok-long --memory-high 1G -- sleep 4 >/dev/null
TP=$(cd "$SHOALD" && python3 -c "import health
for t in ('job:ok-long','job:ok','job:never'): print(t, health.target_pending(t,'tl','$SID')[0])")
grep -q "job:ok-long True" <<<"$TP" && grep -q "job:ok False" <<<"$TP" && grep -q "job:never None" <<<"$TP" \
  || fail "job wait target misread: $TP"
check "result=success exit=0" $H job wait --shoal $SID --slug tl --name ok-long --poll 1
fi
# a job name resolves to its `shoal-` unit
python3 - "$SHOALD" "$SID" <<'PY' || fail "job unit not resolved by name"
import sys; sys.path.insert(0, sys.argv[1])
import jobs
h = sys.argv[2]
assert jobs.find_unit(h, "tl", "ok") == f"shoal-{h}-tl-ok"
PY

echo "== 6b wait-target providers: lease contract (held/queued pending, free resolved, failures unknown)"
cat > "$S/lease_stub.sh" <<'SH'
#!/bin/sh
# a lease provider meeting docs/providers.md; LEASE_STUB selects the answer
[ "$1 $2" = "--status --json" ] || { echo "unexpected argv: $*" >&2; exit 2; }
case "${LEASE_STUB:-free}" in
  held)   echo '{"leases":[{"key":"gpu","held":true,"holder":"ci-1","pid":7,"since":"2026-09-24T00:00:00Z","cmd":"x","container":false}],"queue":[]}' ;;
  queued) echo '{"leases":[{"key":"gpu","held":false}],"queue":[{"key":"gpu","waiter":"tl","since":"2026-09-24T00:00:00Z"}]}' ;;
  free)   echo '{"leases":[{"key":"gpu","held":false},{"key":"cpu-heavy","held":true,"holder":"other"}],"queue":[]}' ;;
  badjson) echo 'lease table: gpu held' ;;
  fail)   echo '{"leases":[]}'; exit 4 ;;
esac
SH
chmod +x "$S/lease_stub.sh"
PV=$(cd "$SHOALD" && env SHOAL_LEASE_CMD="$S/lease_stub.sh" python3 -c "
import os, providers
def r(t, mode=None, **env):
    if mode: os.environ['LEASE_STUB'] = mode
    return providers.resolve(t, 'tl', '$SID')[0]
print('held', r('lease:gpu', 'held'))
print('queued', r('lease:gpu', 'queued'))
print('free', r('lease:gpu', 'free'))
print('other-key-held', r('lease:cpu-heavy', 'free'))
print('unknown-key', r('lease:tpu', 'free'))
print('badjson', r('lease:gpu', 'badjson'))
print('fail', r('lease:gpu', 'fail'))
print('nokind', providers.resolve('carrier-pigeon:x', 'tl', '$SID'))
print('empty', providers.resolve('', 'tl', '$SID')[0])
")
PV_NOCMD=$(cd "$SHOALD" && env -u SHOAL_LEASE_CMD -u GPU_LEASE_CMD python3 -c "
import providers; print(providers.resolve('lease:gpu', 'tl', 'x'))")
for want in "held True" "queued True" "free False" "other-key-held True" "unknown-key None" "badjson None" \
            "fail None" "nokind (None, 'no provider for carrier-pigeon')" "empty None"; do
  grep -qxF "$want" <<<"$PV" || fail "provider answer '$want' missing :: $PV"
done
grep -q "(None, 'no lease provider configured" <<<"$PV_NOCMD" || fail "no lease_cmd must read unknown :: $PV_NOCMD"
PV_GPU=$(cd "$SHOALD" && env -u SHOAL_LEASE_CMD GPU_LEASE_CMD="$S/lease_stub.sh" LEASE_STUB=held python3 -c "
import providers; print(providers.resolve('lease:gpu', 'tl', 'x')[0])")
[ "$PV_GPU" = True ] || fail "GPU_LEASE_CMD fallback not honoured :: $PV_GPU"

echo "== 7 work loop: next reads, queue writes ONE commit (claim + posture + version)"
python3 - "$SPEC" <<'PY'
import json,sys; p=sys.argv[1]; d=json.load(open(p))
d["features"]={"digest":True,"jobs":True,"workwake":True,"health":{"validity_wake_min":90}}
json.dump(d,open(p,"w"),indent=1)
PY
check "\[dry-run\] relay-init $SID: doc $S/data/$SID/roles/wake-relay/plan_state.md; role ADD" $H relay-init --shoal $SID --spec "$SPEC" --dry-run
check "role ADD" $H relay-init --shoal $SID --spec "$SPEC"
grep -qi "current state" "$S/data/$SID/roles/wake-relay/plan_state.md" || fail "relay doc lacks the current-state section restart verifies"
python3 - "$SPEC" <<'PY' || fail "relay-init did not add the role / feature"
import json,sys; d=json.load(open(sys.argv[1])); r=[x for x in d["roles"] if x["slug"]=="wake-relay"]
assert r and r[0]["fill"]=="none" and d["features"].get("relay") is True, (r, d["features"])
r[0]["session"]="rrrr3333"; json.dump(d,open(sys.argv[1],"w"),indent=1)
PY
check "role present" $H relay-init --shoal $SID --spec "$SPEC"
git -C "$A" add -A >/dev/null 2>&1; git -C "$A" commit -qm spec >/dev/null 2>&1 || true
check "QUEUE v0" $H next --shoal $SID --spec "$SPEC" --slug up --no-sync
I1=interpretune#9101; I2=interpretune#9102; I3=interpretune#9103; I4=interpretune#9104
check "\[dry-run\] up queue v1" $H queue --shoal $SID --spec "$SPEC" --slug up --active $I1:fix --next $I2,$I3 --dry-run --no-sync
N0=$(git -C "$A" rev-list --count HEAD)
check "up queue v1: active=$I1:fix" $H queue --shoal $SID --spec "$SPEC" --slug up --active $I1:fix --next $I2,$I3 --reason "two known" --no-sync
N1=$(git -C "$A" rev-list --count HEAD); [ $((N1 - N0)) = 1 ] || fail "queue made $((N1 - N0)) commits (want 1)"
check "CLAIM-REFUSED" $H queue --shoal $SID --spec "$SPEC" --slug tl --active $I1:fix --no-sync
check "QUEUE v1 .*active=$I1" $H next --shoal $SID --spec "$SPEC" --slug up --no-sync
check "up queue v2: active=$I2 next=$I3 posture=active.*done=\['$I1:fix'\]" $H queue --shoal $SID --spec "$SPEC" --slug up --done $I1:fix --url https://example.invalid/pr/1 --active $I2 --next $I3 --no-sync
# the wait is written while its target is pending, then the target resolves (a resolved target
# is refused at write time: there is nothing left to wait on)
export SHOAL_LEASE_CMD="$S/lease_stub.sh" LEASE_STUB=free
[ "$(rc_of $H queue --shoal $SID --spec "$SPEC" --slug up --wait remote-ci --holder x --target lease:gpu --no-sync)" = 3 ] \
  || fail "a wait on an already-resolved target was accepted"
check "already resolved" $H queue --shoal $SID --spec "$SPEC" --slug up --wait remote-ci --holder x --target lease:gpu --no-sync
check "wait target refused: gh-pr target" $H queue --shoal $SID --spec "$SPEC" --slug up --wait remote-ci --holder x --target gh-pr:no-number-here --no-sync
if have_systemd; then
  $H job run --shoal $SID --slug up --name gate1 -- sleep 6 >/dev/null   # running now: pending
  WT_TARGET=job:gate1
else
  echo "SKIP 7 job-unit wait target: $(skip_reason); using lease:gpu on the stub provider"
  export LEASE_STUB=held
  WT_TARGET=lease:gpu
fi
check "posture=waiting/remote-ci" $H queue --shoal $SID --spec "$SPEC" --slug up --wait remote-ci --holder "gate 1" --target $WT_TARGET --no-sync
grep -q "\"target\": \"$WT_TARGET\"" /tmp/$SID/wait-up || fail "wait proof lacks target"
check "NOTE: this wait covers the active item only" $H next --shoal $SID --spec "$SPEC" --slug up --peek --no-sync
if have_systemd; then
  check "result=success exit=0" $H job wait --shoal $SID --slug up --name gate1 --poll 1   # now resolved
else
  export LEASE_STUB=free
fi
check "WAIT TARGET RESOLVED" $H next --shoal $SID --spec "$SPEC" --slug up --peek --no-sync
[ "$(rc_of $H queue --shoal $SID --spec "$SPEC" --slug up --next a,b,c,d --no-sync)" = 1 ] || fail "4 next items accepted"

echo "== 8 wakes are work-only: busy roles are not woken by offers or peer mail"
$H queue --shoal $SID --spec "$SPEC" --slug tl --active $I4:fix --next "" --no-sync >/dev/null
$H offer --shoal $SID --spec "$SPEC" --to tl --item interpretune#9201:fix --no-sync >/dev/null
$H msg --shoal $SID --spec "$SPEC" --by up --to tl --text "peer chatter" --no-sync >/dev/null
python3 - "$SHOALD" "$SPEC" "$A" <<'PY' || fail "busy-role gating wrong"
import sys; sys.path.insert(0,sys.argv[1])
from roles import ShoalSpec; from ledger import Ledger, View; from ledger_watch import actionable
s=ShoalSpec.load(sys.argv[2]); v=View(Ledger(s.sid,root=sys.argv[3],sync=False),s)
acts=[d for _,d in actionable(v,"tl",s.role("tl"),s,board=True)]
assert not any(d.startswith(("OFFER","MSG","BOARD")) for d in acts), acts
PY
$H offer --shoal $SID --spec "$SPEC" --to tl --item interpretune#9202:fix --now --no-sync >/dev/null
$H msg --shoal $SID --spec "$SPEC" --by user --to tl --text "maintainer ping" --wake --no-sync >/dev/null
python3 - "$SHOALD" "$SPEC" "$A" <<'PY' || fail "--now offer / user --wake msg did not wake the busy role"
import sys; sys.path.insert(0,sys.argv[1])
from roles import ShoalSpec; from ledger import Ledger, View; from ledger_watch import actionable
s=ShoalSpec.load(sys.argv[2]); v=View(Ledger(s.sid,root=sys.argv[3],sync=False),s)
acts=[d for _,d in actionable(v,"tl",s.role("tl"),s,board=True)]
assert any("9202" in d and "NOW" in d for d in acts) and any("maintainer ping" in d for d in acts), acts
assert not any("9201" in d or "peer chatter" in d for d in acts), acts
PY
$H queue --shoal $SID --spec "$SPEC" --slug tl --idle --next "" --no-sync >/dev/null
python3 - "$SHOALD" "$SPEC" "$A" <<'PY' || fail "idle role not woken by a plain offer"
import sys; sys.path.insert(0,sys.argv[1])
from roles import ShoalSpec; from ledger import Ledger, View; from ledger_watch import actionable
s=ShoalSpec.load(sys.argv[2]); v=View(Ledger(s.sid,root=sys.argv[3],sync=False),s)
acts=[d for _,d in actionable(v,"tl",s.role("tl"),s,board=True)]
assert any("9201" in d for d in acts) and not any("peer chatter" in d for d in acts), acts
PY
check "MAIL" $H next --shoal $SID --spec "$SPEC" --slug tl --no-sync

echo "== 9 health quorum: leeway, ladder, adapters, peer check, validity, resolved waits"
check "health-a: ok" env SHOAL_HEALTH_SHIFT_MIN=0 $H health --spec "$SPEC" --instance a --dry-run
$H offer --shoal $SID --spec "$SPEC" --to tl --item interpretune#9203:fix --no-sync >/dev/null   # arrives after tl's last queue version
setsid $H ledger-watch --shoal $SID --spec "$SPEC" --slug tl --wake print --interval 2 --no-sync --no-board >"$S/tl-watch.log" 2>&1 &
TLW=$!
for i in $(seq 1 30); do grep -q "^pid=$TLW$" /tmp/$SID/ledger-watch-tl.state 2>/dev/null && break; sleep 0.5; done
grep -q "^pid=$TLW$" /tmp/$SID/ledger-watch-tl.state || fail "tl watcher never bound its sidecar"
check "tl unacked-work step1:wake -> wake-file" env SHOAL_HEALTH_SHIFT_MIN=11 $H health --spec "$SPEC" --instance a
[ -f /tmp/$SID/wake-tl ] || fail "wake request file not written"
sleep 5; grep -q "HEALTH unacked-work" "$S/tl-watch.log" || fail "live ledger-watch did not deliver the wake request :: $(tail -3 $S/tl-watch.log)"
check "tl unacked-work step2:wake" env SHOAL_HEALTH_SHIFT_MIN=31 $H health --spec "$SPEC" --instance a --dry-run
$H queue --shoal $SID --spec "$SPEC" --slug tl --active interpretune#9201:fix --next "" --no-sync >/dev/null
[ ! -f /tmp/$SID/wake-tl ] || fail "queue write did not acknowledge the wake request"
kill $TLW 2>/dev/null || true
check "up wait-resolved step1:wake" env SHOAL_HEALTH_SHIFT_MIN=27 $H health --spec "$SPEC" --instance a
check "up wait-validity step1:wake" env SHOAL_HEALTH_SHIFT_MIN=91 $H health --spec "$SPEC" --instance b --dry-run
echo "2026-01-01T00:00:00Z" > /tmp/$SID/tick-health-b
check "user peer-dead" $H health --spec "$SPEC" --instance a --dry-run

echo "== 10 Claude relay: dead watcher -> relay queue -> relay-watch exits with the SendMessage line"
setsid $H relay-watch --shoal $SID --interval 1 >"$S/relay.log" 2>&1 &
RW=$!
for i in $(seq 1 30); do [ "$(cat /tmp/$SID/relay-watch.pid 2>/dev/null)" = "$RW" ] && break; sleep 0.5; done
python3 - "$SHOALD" "$SPEC" <<'PY' | grep -q "^wake-relay$" || fail "request_wake did not choose the wake relay"
import sys; sys.path.insert(0,sys.argv[1])
from roles import ShoalSpec; from health import request_wake
print(request_wake(ShoalSpec.load(sys.argv[2]), "tl", "relay test", "watcher-dead"))
PY
sleep 3; grep -q "RELAY-WAKE to=tl-r1" "$S/relay.log" || fail "relay-watch did not deliver :: $(cat $S/relay.log)"
grep -q "VERDICT RELAY-DELIVER 1" "$S/relay.log" || fail "relay-watch verdict missing"
kill $RW 2>/dev/null || true
sleep 1
check "wake-relay relay-dead step1:page" env SHOAL_HEALTH_SHIFT_MIN=11 $H health --spec "$SPEC" --instance a --dry-run
check "VERDICT RELAY-MAX-HOURS" timeout 30 $H relay-watch --shoal $SID --interval 1 --max-hours 0.0005

echo "== 11 user-offline mode: banner, asks, deferred pages, back-online summary"
$H queue --shoal $SID --spec "$SPEC" --slug tl --ask "merge interpretune#9201 (PR green)" --ask "ruling on #9202 scope" --no-sync >/dev/null
export SHOAL_FAKE_REF_STATE='{"speediedan/interpretune#9201": "MERGED", "speediedan/interpretune#9202": "OPEN"}'
check "user-offline on" $H offline on --shoal $SID --spec "$SPEC" --until tomorrow --note "travel" --no-sync
LONGMSG="RULES UPDATE selftest: $(printf 'word%.0s ' $(seq 1 60))END-OF-MAINTAINER-TEXT"
$H msg --shoal $SID --spec "$SPEC" --by user --to tl --text "$LONGMSG" --no-sync >/dev/null
OUT=$($H next --shoal $SID --spec "$SPEC" --slug tl --no-sync 2>&1)
grep -q "USER OFFLINE since" <<<"$OUT" || fail "no offline banner :: $OUT"
grep -q "END-OF-MAINTAINER-TEXT" <<<"$OUT" || fail "maintainer mail was shortened :: $OUT"
ML=$(grep -n "^MAIL " <<<"$OUT" | cut -d: -f1); QL=$(grep -n "^QUEUE v" <<<"$OUT" | cut -d: -f1)
[ -n "$ML" ] && [ "$ML" -lt "$QL" ] || fail "MAIL must print before QUEUE (a piped next consumed mail unseen) :: $OUT"
(cd "$SHOALD" && python3 -c "
import health; from roles import ShoalSpec
t = health.wake_text(ShoalSpec.load('$SPEC'), 'tl', 'probe', 'x')
assert t.startswith('USER OFFLINE since') and 'raise NO prompted question' in t, t
") || fail "a wake sent while offline must carry the offline banner"
[ "$(rc_of $H asks --shoal $SID --spec "$SPEC" --slug tl --no-sync)" = 3 ] || fail "asks printed question sets while offline"
DEF=""; for i in 1 2 3 4; do DEF+=$(env SHOAL_HEALTH_SHIFT_MIN=200 $H health --spec "$SPEC" --instance a 2>&1); done
grep -q "deferred (user offline)" <<<"$DEF" || fail "no page was deferred while offline :: $(tail -c 400 <<<"$DEF")"
OUT=$($H offline off --shoal $SID --spec "$SPEC" --no-sync 2>&1)
grep -q "ASK tl \[1\]: merge interpretune#9201 (PR green) (LIKELY STALE: interpretune#9201 merged)" <<<"$OUT" \
  || fail "return summary lacks the stale hint :: $OUT"
grep -q "ASK tl \[2\]: ruling on #9202 scope$" <<<"$OUT" || fail "open ask mislabelled :: $OUT"
grep -q "2 recorded ask(s), 1 likely stale" <<<"$OUT" || fail "no return count :: $OUT"
OUT=$($H next --shoal $SID --spec "$SPEC" --slug tl --peek --no-sync 2>&1)
grep -q "ASKS TO RAISE: the maintainer is online" <<<"$OUT" && grep -q "at most 4 questions each" <<<"$OUT" \
  || fail "next lacks the return protocol :: $OUT"
[ "$(rc_of $H queue --shoal $SID --spec "$SPEC" --slug tl --drop-ask 1 --no-sync)" = 1 ] || fail "drop without a reason accepted"
check "asks=1" $H queue --shoal $SID --spec "$SPEC" --slug tl --drop-ask 1 --drop-reason "merged upstream" --no-sync
OUT=$($H next --shoal $SID --spec "$SPEC" --slug tl --peek --no-sync 2>&1)
grep -q "ASKS (open): \[1\] ruling on #9202 scope$" <<<"$OUT" || fail "drop-ask kept the wrong ask :: $OUT"
grep -q "DROPPED ASKS (not answered.*merge interpretune#9201 (PR green) \[dropped .*: merged upstream\]" <<<"$OUT" \
  || fail "an unanswered drop is not disclosed :: $OUT"
[ "$(rc_of $H queue --shoal $SID --spec "$SPEC" --slug tl --drop-ask 7 --drop-reason x --no-sync)" = 1 ] || fail "bad --drop-ask accepted"
# six asks -> two question sets (4 + 2), labelled from live PR data
$H queue --shoal $SID --spec "$SPEC" --slug tl --ask "merge #9203 now" --ask "q4" --ask "q5" --ask "q6" --ask "q7" --no-sync >/dev/null
export SHOAL_FAKE_PR='{"speediedan/interpretune#9203": {"title": "Live title 9203", "state": "OPEN", "headRefOid": "abcdef123456", "mergeable": "MERGEABLE", "checks": {"pass": 3}}}'
QJ=$($H asks --shoal $SID --spec "$SPEC" --slug tl --question-json --no-sync)
python3 -c '
import json, sys
d = json.loads(sys.argv[1])
assert [len(s) for s in d["sets"]] == [4, 2], [len(s) for s in d["sets"]]
q = d["sets"][0][1]
assert "Live title 9203" in q["question"] and "head abcdef12" in q["question"], q
assert [o["label"] for o in q["options"]][0] == "Grant merge", q["options"]
assert d["dropped_unanswered"] and d["dropped_unanswered"][0]["reason"] == "merged upstream"
' "$QJ" || fail "question sets wrong :: $QJ"
unset SHOAL_FAKE_PR
[ "$(rc_of $H queue --shoal $SID --spec "$SPEC" --slug tl --asks-clear --no-sync)" = 1 ] || fail "blind --asks-clear accepted"
check "asks=4" $H queue --shoal $SID --spec "$SPEC" --slug tl --answered 1 --answered 2 --no-sync
check "asks=0" $H queue --shoal $SID --spec "$SPEC" --slug tl --asks-clear --drop-reason "selftest cleanup" --no-sync
# later sections still read ref states (asks naming #9301): answer them offline, never from GitHub
export SHOAL_FAKE_REF_STATE='{"speediedan/interpretune#9301": "OPEN"}'
check "user deferred pages -> page" $H health --spec "$SPEC" --instance a --dry-run

echo "== 12 wait discipline: asks, blockers, rescan caps, expiry and blocking wakes, labels, peek"
python3 - "$SPEC" <<'PY'
import json,sys; p=sys.argv[1]; d=json.load(open(p)); d["features"]["waitdiscipline"]=True; json.dump(d,open(p,"w"),indent=1)
PY
check "tl (tl-r1)" $H show --shoal $SID --spec "$SPEC" --no-sync
check "IDENTITY: your Lane row label is \`tl (tl-r1)\`" $H next --shoal $SID --spec "$SPEC" --slug tl --no-sync --peek
[ "$(rc_of $H queue --shoal $SID --spec "$SPEC" --slug up --asks-clear --wait user-design --holder maintainer --no-sync)" = 3 ] || fail "user wait accepted without an ask"
check "a user-design wait needs the prompted ask" $H queue --shoal $SID --spec "$SPEC" --slug up --asks-clear --wait user-design --holder maintainer --no-sync
check "clamped to the user-design cap 240m" $H queue --shoal $SID --spec "$SPEC" --slug up --wait user-design --holder maintainer \
   --ask "Who owns #9301: A, B, or hold?" --rescan-min 720 --no-sync
grep -q '"rescan_utc"' /tmp/$SID/wait-up || fail "no wait proof"
python3 - /tmp/$SID/wait-up <<'PY' || fail "rescan not clamped in the wait proof"
import json,sys,datetime as d; w=json.load(open(sys.argv[1]))
f=lambda s: d.datetime.strptime(s,"%Y-%m-%dT%H:%M:%SZ"); assert (f(w["rescan_utc"])-f(w["since_utc"])).total_seconds()<=240*60+5, w
PY
[ "$(rc_of $H transition --shoal $SID --spec "$SPEC" --slug tl --to waiting --sub session-blocked --holder "up")" = 3 ] || fail "session-blocked accepted without --blocked-on"
[ "$(rc_of $H queue --shoal $SID --spec "$SPEC" --slug tl --wait session-blocked --holder self --blocked-on tl:x#1 --no-sync)" = 3 ] \
  || fail "a session-blocked wait naming the lane itself was accepted"
[ "$(rc_of $H queue --shoal $SID --spec "$SPEC" --slug tl --wait upstream --holder "engine release" --no-sync)" = 3 ] \
  || fail "an upstream wait without a target was accepted"
check "posture=waiting/session-blocked" $H queue --shoal $SID --spec "$SPEC" --slug tl --wait session-blocked --holder "up #9301" \
   --blocked-on up:interpretune#9301 --no-sync
check "BLOCKING tl since .* on interpretune#9301 -- NOT in your queue" $H next --shoal $SID --spec "$SPEC" --slug up --no-sync --peek
check "BLOCKING tl on interpretune#9301" $H show --shoal $SID --spec "$SPEC" --slug up --no-sync
check "up blocking-unscheduled step1:wake" env SHOAL_HEALTH_SHIFT_MIN=31 $H health --spec "$SPEC" --instance a --dry-run
check "wait-expired step1:wake" env SHOAL_HEALTH_SHIFT_MIN=245 $H health --spec "$SPEC" --instance b --dry-run
M0=$(cat /tmp/$SID/mail-read-up 2>/dev/null || echo none)
$H next --shoal $SID --spec "$SPEC" --slug up --no-sync --peek >/dev/null
[ "$(cat /tmp/$SID/mail-read-up 2>/dev/null || echo none)" = "$M0" ] || fail "--peek consumed the role's mail"
$H claim --shoal $SID --spec "$SPEC" --slug up --item interpretune#9401:fix --no-sync >/dev/null
check "HELD-UNSCHEDULED interpretune#9401" $H queue --shoal $SID --spec "$SPEC" --slug up --active interpretune#9301:fix --next "" --reason test --no-sync
# a holder hands its claim to a named role: an implicit offer the recipient can claim
[ "$(rc_of $H release --shoal $SID --spec "$SPEC" --slug up --item interpretune#9401 --kind decline --to tl --no-sync)" = 1 ] || fail "--to accepted on a decline"
check "handed interpretune#9401 to tl" $H release --shoal $SID --spec "$SPEC" --slug up --item interpretune#9401 --kind handoff --to tl \
   --reason "maintainer ruling: tl owns it" --no-sync
check "OFFER interpretune#9401.* by=up" $H next --shoal $SID --spec "$SPEC" --slug tl --no-sync --peek
check "claimed interpretune#9401" $H claim --shoal $SID --spec "$SPEC" --slug tl --item interpretune#9401:fix --no-sync

echo "== 13 continuation: a turn that ends while active is woken to continue (not after 90 min)"
python3 - "$SPEC" <<'PY'
import json,sys; p=sys.argv[1]; d=json.load(open(p)); d["features"]["continuation"]={"after_min":5}; json.dump(d,open(p,"w"),indent=1)
PY
check "note: posture ACTIVE means you keep working in this turn" $H queue --shoal $SID --spec "$SPEC" --slug tl --active interpretune#9501:fix --next "" --reason test --no-sync
check "END EVERY TURN IN A SLEEP POSTURE" $H next --shoal $SID --spec "$SPEC" --slug tl --no-sync --peek
rm -f /tmp/$SID/health.json
env SHOAL_HEALTH_BETWEEN=tl $H health --spec "$SPEC" --instance a >/dev/null 2>&1   # first sighting recorded
check "tl turn-ended-active step1:wake" env SHOAL_HEALTH_BETWEEN=tl SHOAL_HEALTH_SHIFT_MIN=6 $H health --spec "$SPEC" --instance b --dry-run
OUT=$(env SHOAL_HEALTH_BETWEEN=tl SHOAL_HEALTH_SHIFT_MIN=6 $H health --spec "$SPEC" --instance b --dry-run 2>&1)
! grep -q "tl stale-active" <<<"$OUT" || fail "stale-active fired alongside continuation :: $OUT"
OUT=$(env SHOAL_HEALTH_SHIFT_MIN=6 $H health --spec "$SPEC" --instance b --dry-run 2>&1)
! grep -q "tl turn-ended-active" <<<"$OUT" || fail "continuation fired for a session not proven between turns :: $OUT"
check "posture=idle" $H queue --shoal $SID --spec "$SPEC" --slug tl --idle --next "" --no-sync
OUT=$(env SHOAL_HEALTH_BETWEEN=tl SHOAL_HEALTH_SHIFT_MIN=12 $H health --spec "$SPEC" --instance b --dry-run 2>&1)
! grep -q "tl turn-ended-active" <<<"$OUT" || fail "continuation fired after the role wrote a sleep posture :: $OUT"

echo "== 14 parked work: idle needs every next item blocked; blockers carry forward; freeze mode"
Q="$H queue --shoal $SID --spec $SPEC --no-sync"
check "posture=active" $Q --slug tl --active interpretune#9601:fix --next interpretune#9602,interpretune#9603 --reason test
[ "$(rc_of $Q --slug tl --idle)" = 3 ] || fail "idle accepted with unblocked next items"
check "idle with unblocked next items (interpretune#9602, interpretune#9603)" $Q --slug tl --idle
check "BLOCKER must be another role" $Q --slug tl --idle --blocked interpretune#9602=tl:interpretune#1
check "ITEM=BLOCKER" $Q --slug tl --idle --blocked interpretune#9602
check "idle with unblocked next items (interpretune#9603)" $Q --slug tl --idle --blocked interpretune#9602=up:interpretune#9301
check "posture=idle" $Q --slug tl --idle --blocked interpretune#9602=up:interpretune#9301 --blocked "interpretune#9603=hold:design pending"
check "BLOCKING tl .* on interpretune#9301" $H next --shoal $SID --spec "$SPEC" --slug up --no-sync --peek
check "NEXT BLOCKED interpretune#9603 <- hold:design pending" $H next --shoal $SID --spec "$SPEC" --slug tl --no-sync --peek
# blockers carry forward to the next write; clearing one re-opens the gate
check "posture=idle" $Q --slug tl --idle
[ "$(rc_of $Q --slug tl --idle --blocked interpretune#9603=)" = 3 ] || fail "idle accepted after clearing a blocker"
# a wait that leaves a next item unblocked is parked work for the quorum (idle is gated at the write)
check "posture=waiting/remote-ci" env LEASE_STUB=held $Q --slug tl --active interpretune#9601:fix --blocked interpretune#9603= \
   --wait remote-ci --holder "gate" --target lease:gpu
rm -f /tmp/$SID/health.json
env LEASE_STUB=held $H health --spec "$SPEC" --instance a >/dev/null 2>&1   # first sighting recorded
check "tl parked-work step1:wake" env LEASE_STUB=held SHOAL_HEALTH_SHIFT_MIN=31 $H health --spec "$SPEC" --instance a --dry-run
OUT=$(env LEASE_STUB=held SHOAL_HEALTH_SHIFT_MIN=31 $H health --spec "$SPEC" --instance a --dry-run 2>&1)
! grep -q "parked-work.*interpretune#9602" <<<"$OUT" || fail "a blocked next item counted as parked :: $OUT"
# freeze: banner, idle allowed, parked-work suppressed, job run refused; lifted banner after
check "freeze on (every role woken)" $H freeze on --shoal $SID --spec "$SPEC" --note "restart window" --no-sync
check "freeze already on" $H freeze on --shoal $SID --spec "$SPEC" --no-sync
check "FREEZE ACTIVE since .* (restart window)" $H next --shoal $SID --spec "$SPEC" --slug up --no-sync --peek
OUT=$(env LEASE_STUB=held SHOAL_HEALTH_SHIFT_MIN=31 $H health --spec "$SPEC" --instance a --dry-run 2>&1)
! grep -q "tl parked-work" <<<"$OUT" || fail "parked-work raised during a freeze :: $OUT"
[ "$(rc_of $H job run --shoal $SID --spec "$SPEC" --slug tl --name frozen --dry-run -- true)" = 5 ] || fail "job run allowed during a freeze"
check "posture=idle" $Q --slug tl --idle --blocked interpretune#9603=
check "freeze off (every role woken)" $H freeze off --shoal $SID --spec "$SPEC" --no-sync
OUT=$($H next --shoal $SID --spec "$SPEC" --slug tl --no-sync --peek 2>&1)
grep -q "FREEZE LIFTED at" <<<"$OUT" || fail "no lifted banner after freeze off :: $OUT"
! grep -q "FREEZE ACTIVE" <<<"$OUT" || fail "active banner after freeze off :: $OUT"
check "\[dry-run\] systemd-run" $H job run --shoal $SID --spec "$SPEC" --slug tl --name thawed --dry-run -- true
check "posture=idle" $Q --slug tl --idle --next ""

echo "== 15 restart: --only scopes the doc checks; launched ids always persist; stale sidecars never adopted"
mkdir -p "$S/restart/shoals/$SID"
RSPEC="$S/restart/shoals/$SID/shoal.json"
printf '# up live plan_state (selftest)\n\n## Mission\n\nno required sections\n' > "$S/up_bad_plan_state.md"
python3 - "$SHOALD" "$SPEC" "$RSPEC" "$S/up_bad_plan_state.md" > "$S/restart.out" 2>&1 <<'PY' || fail "restart persistence / sidecar verdict :: $(tail -30 "$S/restart.out")"
import argparse, json, os, sys
sys.path.insert(0, sys.argv[1])
import cli
import ledger_watch
from roles import Role, ShoalSpec
spec_src, rspec, bad_doc = sys.argv[2:5]
d = json.load(open(spec_src))
for r in d["roles"]:
    if r["slug"] == "up":
        r["doc"] = bad_doc
json.dump(d, open(rspec, "w"))

class FakeBackend:  # stands in for the harness: every create returns a fresh id
    ecosystem = "claude"
    n = 0
    def create(self, title, repo_dir, brief="", name="", model="", effort="", **kw):
        FakeBackend.n += 1
        return f"fake{FakeBackend.n}-{title}"
cli.get_backend = lambda eco: FakeBackend()

def restart(only):
    ns = argparse.Namespace(spec=rspec, stages="handoffs,sync-plans,restart", dry_run=False, only=only,
                            consumer_launched=[], keep=[], seq=[], set_model=[], old_session=[],
                            defer_consumers=False, probe_claude=False, no_verify=True, save_spec="",
                            force=False)
    return cli.run_restart(ShoalSpec.load(rspec), ns)

inc0 = ShoalSpec.load(rspec).incarnation
# an unrelated, kept role's incomplete doc does not fail a partial restart
rc = restart("tl")
s = ShoalSpec.load(rspec)
assert rc == 0, rc
assert s.role("tl").session.startswith("fake") and s.role("up").session == "bbbb2222", s.to_dict()["roles"]
assert s.incarnation == inc0 + 1, s.incarnation
# a failing doc check on the relaunched role still persists the session that launched
rc = restart("up")
s = ShoalSpec.load(rspec)
assert rc == 5, rc
assert s.role("up").session.startswith("fake"), s.role("up").session
assert s.incarnation == inc0 + 2, s.incarnation

V = cli.sidecar_verdict
since, before, after = "2026-09-28T12:00:00Z", "2026-09-28T11:00:00Z", "2026-09-28T13:00:00Z"
assert V({}, "new1", ["old1"], since)[:2] == (False, "")
assert V({"session_id": "old1-uuid", "armed_utc": after}, "new1", ["old1"], since)[:2] == (False, "")
assert V({"session_id": "other", "armed_utc": before}, "new1", ["old1"], since)[:2] == (False, "")
assert V({"session_id": "new1-uuid", "armed_utc": after}, "new1", ["old1"], since)[:2] == (True, "new1-uuid")
assert V({"session_id": "new1", "armed_utc": after}, "new1-uuid", ["old1"], since)[:2] == (True, "")
assert V({"session_id": "x", "armed_utc": after}, "", ["old1"], since)[:2] == (True, "x")
assert V({"session_id": "x", "armed_utc": after}, "new1", ["old1"], since)[:2] == (True, "")
assert V({"pid": "1"}, "new1", [], since, mtime_utc=before)[0] is False

# verify_consumer end to end: a replaced session's sidecar is ignored, the new one's is adopted
spec = ShoalSpec.load(rspec)
path = ledger_watch.sidecar_path(spec.sid, "ghost")
os.makedirs(os.path.dirname(path), exist_ok=True)
open(path, "w").write("session_id=oldghost\narmed_utc=2026-01-01T00:00:00Z\n")
role = Role(slug="ghost", ecosystem="claude", title="ghost", doc="/dev/null", fill="self", session="newghost")
assert not cli.verify_consumer(spec, role, cap_s=0.3, old=["oldghost"], since_utc=cli.utcnow_iso(), poll_s=0.1)
assert role.session == "newghost", role.session
open(path, "w").write(f"session_id=newghost-full-uuid\narmed_utc={cli.utcnow_iso()}\n")
assert cli.verify_consumer(spec, role, cap_s=5, old=["oldghost"], since_utc="2026-01-02T00:00:00Z", poll_s=0.1)
assert role.session == "newghost-full-uuid", role.session
os.remove(path)
print("restart ok")
PY
grep -q "info (not relaunched, not a failure): INCOMPLETE: $S/up_bad_plan_state.md" "$S/restart.out" || fail "kept role's doc problem not reported as info"
grep -q "saved anyway because sessions were launched" "$S/restart.out" || fail "no persisted-anyway message"
grep -q "ignoring sidecar (stale: bound to the replaced session oldghost)" "$S/restart.out" || fail "stale sidecar not reported"

echo "SELFTEST H4 OK ($S)"
rm -rf /tmp/$SID
