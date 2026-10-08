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
# one run at a time per host: the shoal id (h4selftest), its /tmp state dir and the watcher cleanup below
# are fixed, so two concurrent runs (another checkout or worktree, or `upgrade`) clobber each other
exec 9>"/tmp/shoal-selftest-h4selftest.lock"; flock -w 1800 9 || { echo "SELFTEST FAIL: another h4selftest run holds the lock" >&2; exit 1; }
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

echo "== 16 parked roles are quiet; timers judged by their own ActiveState; briefs prove pids without \$(...)"
# a parked role (no session) with a leftover sidecar whose pid is dead: no condition, no FAIL, no idle-deaf
sleep 0 & DEADPID=$!; wait $DEADPID || true
DOC_P=$(shoal_doc parked)
python3 - "$SPEC" "$S/parked.json" "$DOC_P" <<'PY'
import json,sys; d=json.load(open(sys.argv[1]))
d["roles"].append({"slug":"parked","ecosystem":"claude","title":"parked","doc":sys.argv[3],"fill":"self","session":""})
json.dump(d,open(sys.argv[2],"w"),indent=1)
PY
printf 'pid=%s\nsession_id=oldparked\narmed_utc=2026-01-01T00:00:00Z\nlast_tick=2026-01-01T00:00:00Z\n' "$DEADPID" > /tmp/$SID/ledger-watch-parked.state
OUT=$($H doctor --spec "$S/parked.json" 2>&1 || true)
grep -q "^INFO ledger-watch parked: parked (no session)" <<<"$OUT" || fail "parked ledger-watch not INFO :: $(grep parked <<<"$OUT")"
grep -q "^INFO heartbeat parked: parked (no session)" <<<"$OUT" || fail "parked heartbeat not INFO :: $(grep parked <<<"$OUT")"
! grep -E "^(FAIL|WARN) .*parked" <<<"$OUT" || fail "parked role raised a FAIL/WARN"
SHOAL_HEALTH_BETWEEN=parked python3 - "$SHOALD" "$S/parked.json" <<'PY' || fail "health or heartbeat judged a parked role"
import sys; sys.path.insert(0, sys.argv[1])
from roles import ShoalSpec
from ledger import Ledger, View
import health, heartbeat
spec = ShoalSpec.load(sys.argv[2])
view = View(Ledger(spec.sid, root=spec.ledger_dir, sync=False), spec)
conds = [c for c in health.evaluate(spec, view, {}) if c["slug"] == "parked"]
assert not conds, conds
rows = [r for r in heartbeat.classify(spec, 60) if r["slug"] == "parked"]
assert rows and rows[0]["klass"] == "parked" and not rows[0]["suspect"], rows
PY
rm -f /tmp/$SID/ledger-watch-parked.state
# timers: the .timer unit's own state decides; a running service never reads inactive
python3 - "$SHOALD" <<'PY' || fail "timer verdicts"
import sys; sys.path.insert(0, sys.argv[1])
import doctor
show = """Id=shoal-x-lanes.timer
ActiveState=active
SubState=running
UnitFileState=enabled

Id=shoal-x-health-a.timer
ActiveState=active
SubState=waiting
UnitFileState=enabled
"""
t = {k.split("-", 2)[2][:-6]: v for k, v in doctor.parse_timer_show(show).items()}
assert set(t) == {"lanes", "health-a"}, t
assert doctor.timer_verdict(t)[0] == "ok", doctor.timer_verdict(t)
t["pipeline"] = {"ActiveState": "inactive", "SubState": "dead", "UnitFileState": "disabled"}
v = doctor.timer_verdict(t); assert v[0] == "fail" and "pipeline (inactive/dead, disabled)" in v[1], v
t["pipeline"] = {"ActiveState": "failed", "SubState": "failed", "UnitFileState": "enabled"}
assert doctor.timer_verdict(t)[0] == "fail"
t["pipeline"] = {"ActiveState": "active", "SubState": "waiting", "UnitFileState": "disabled"}
assert doctor.timer_verdict(t)[0] == "warn"
PY
if ! have_systemd; then
  echo "SKIP 16 live timer probe: $(skip_reason)"
else
  systemd-run --user --quiet --unit "shoal-$SID-busy" --on-active=1s --on-unit-active=1h sleep 20
  sleep 3   # the service is now running: list-timers reads NEXT n/a, the timer itself is active
  python3 - "$SHOALD" "$SID" <<'PY' || fail "a timer whose service is running read inactive"
import sys; sys.path.insert(0, sys.argv[1])
import doctor
t = doctor.timer_state(sys.argv[2])
assert t.get("busy", {}).get("ActiveState") == "active", t
assert doctor.timer_verdict(t)[0] == "ok", doctor.timer_verdict(t)
PY
  systemctl --user stop "shoal-$SID-busy.timer" "shoal-$SID-busy.service" 2>/dev/null || true
fi
# pid proof: two separate commands, never command substitution
for slug in tl wake-relay; do
  OUT=$($H brief --spec "$SPEC" --slug $slug 2>&1)
  grep -q "TWO separate commands" <<<"$OUT" || fail "$slug brief lacks the two-command pid proof"
  grep -q 'never use \$(...) command substitution' <<<"$OUT" || fail "$slug brief lacks the \$(...) ban"
  ! grep -qE 'ps -p \$\(|ps -p plus sidecar' <<<"$OUT" || fail "$slug brief still combines the pid proof"
done
grep -qi 'never use \$(...) command substitution' "$S/data/$SID/roles/wake-relay/plan_state.md" \
  || fail "relay doc lacks the \$(...) ban"

echo "== 17 upgrade --rearm now: a stale relay-watch is killed and its re-arm verified (or WARNed)"
RPID=/tmp/$SID/relay-watch.pid
setsid $H relay-watch --shoal $SID --interval 1 >"$S/relay-u1.log" 2>&1 &
RW1=$!
for i in $(seq 1 30); do [ "$(cat $RPID 2>/dev/null)" = "$RW1" ] && break; sleep 0.5; done
[ "$(cat $RPID 2>/dev/null)" = "$RW1" ] || fail "relay-watch never wrote its pid"
touch -d "2001-01-01 00:00:00 UTC" $RPID   # armed long before any package change
python3 -c "import json; d=json.load(open('$SPEC')); d.pop('tooling', None); json.dump(d, open('$SPEC','w'), indent=1)"
check "relay-watch armed before .*pid $RW1 .*killed now" \
  $H upgrade --spec "$SPEC" --rearm now --skip-selftests --allow-dirty --dry-run
kill -0 $RW1 2>/dev/null || fail "--dry-run killed the relay-watch"
# stand-in for the relay session: re-arm once the old waiter dies
( while kill -0 $RW1 2>/dev/null; do sleep 0.2; done
  setsid $H relay-watch --shoal $SID --interval 1 >"$S/relay-u2.log" 2>&1 & echo $! > "$S/rw2.pid" ) &
OUT=$(SHOAL_REARM_VERIFY_S=30 $H upgrade --spec "$SPEC" --rearm now --skip-selftests --allow-dirty 2>&1 || true)
grep -q "relay-watch: killed pid $RW1" <<<"$OUT" || fail "stale relay-watch not killed :: $OUT"
RW2=$(cat "$S/rw2.pid" 2>/dev/null || true)
grep -q "relay-watch re-armed: pid $RW2" <<<"$OUT" || fail "re-arm not verified (want pid $RW2) :: $OUT"
# no re-arm within the window: a WARN with the exact re-arm line
touch -d "2001-01-01 00:00:00 UTC" $RPID
python3 -c "import json; d=json.load(open('$SPEC')); d.pop('tooling', None); json.dump(d, open('$SPEC','w'), indent=1)"
OUT=$(SHOAL_REARM_VERIFY_S=2 $H upgrade --spec "$SPEC" --rearm now --skip-selftests --allow-dirty 2>&1 || true)
grep -q "relay-watch: killed pid $RW2" <<<"$OUT" || fail "second stale relay-watch not killed :: $OUT"
grep -q "WARN relay-watch NOT re-armed within 2s .*relay-watch --shoal $SID" <<<"$OUT" || fail "no re-arm WARN :: $OUT"

echo "== 18 liveness from turn state; candidates shown to a role idle behind blocked next items"
# Claude: `status` reads busy while any background task (the role's own ledge-watch) runs, so only
# `state` says whether a turn is in progress (done = between turns)
Q="$H queue --shoal $SID --spec $SPEC --no-sync"
check "posture=active" $Q --slug tl --active interpretune#9801:fix --next "" --reason test
rm -f /tmp/$SID/health.json
FS_DONE='{"claude":[{"sessionId":"aaaa1111-0000","status":"busy","state":"done"}],"opencode":{}}'
FS_WORK='{"claude":[{"sessionId":"aaaa1111-0000","status":"busy","state":"working"}],"opencode":{}}'
FS_OLD='{"claude":[{"sessionId":"aaaa1111-0000","status":"idle"}],"opencode":{}}'
env SHOAL_FAKE_SESSIONS="$FS_DONE" $H health --spec "$SPEC" --instance a >/dev/null 2>&1   # first sighting recorded
check "tl turn-ended-active step1:wake" env SHOAL_FAKE_SESSIONS="$FS_DONE" SHOAL_HEALTH_SHIFT_MIN=6 $H health --spec "$SPEC" --instance b --dry-run
OUT=$(env SHOAL_FAKE_SESSIONS="$FS_WORK" SHOAL_HEALTH_SHIFT_MIN=6 $H health --spec "$SPEC" --instance b --dry-run 2>&1)
! grep -q "tl turn-ended-active" <<<"$OUT" || fail "continuation fired for a session whose state is working :: $OUT"
check "tl turn-ended-active step1:wake" env SHOAL_FAKE_SESSIONS="$FS_OLD" SHOAL_HEALTH_SHIFT_MIN=6 $H health --spec "$SPEC" --instance b --dry-run
python3 - "$SHOALD" <<'PY2' || fail "claude_liveness mapping"
import sys; sys.path.insert(0, sys.argv[1]); from health import claude_liveness as L
assert L({"status": "busy", "state": "done"}) == "idle"
assert L({"status": "busy", "state": "working"}) == "busy"
assert L({"status": "idle", "state": "blocked"}) == "blocked"
assert L({"status": "busy", "state": "mystery"}) == "busy"   # unknown state: the raw status decides
assert L({"status": "idle"}) == "idle"
# tempo wins over state: an idle session with a live background watcher reads state=working
assert L({"status": "busy", "state": "working", "tempo": "idle"}) == "idle"
assert L({"status": "busy", "state": "working", "tempo": "active"}) == "busy"
assert L({"status": "busy", "state": "working", "tempo": "blocked"}) == "blocked"
assert L({"status": "busy", "state": "working", "tempo": "mystery"}) == "busy"   # unknown tempo: state decides
PY2
# hidden candidates: two blocked next items and no active item used to leave no slot for a candidate
check "posture=idle" $Q --slug tl --idle --next interpretune#9802,interpretune#9803 \
  --blocked "interpretune#9802=hold:x" --blocked "interpretune#9803=up:interpretune#9301"
OUT=$($H next --shoal $SID --spec "$SPEC" --slug tl --no-sync --peek 2>&1)
grep -q "^BOARD interpretune#80" <<<"$OUT" || fail "no candidates for a role idle behind blocked next items :: $OUT"
grep -q "short queue: [1-9][0-9]* eligible candidate" <<<"$(tail -1 $A/shoals/$SID/ledger/tl.jsonl)" \
  || fail "default reason claims no eligible items beside candidates :: $(tail -1 $A/shoals/$SID/ledger/tl.jsonl)"
# three unblocked next items and an active one: the queue is full, no candidates
check "posture=active" $Q --slug tl --active interpretune#9801:fix \
  --next interpretune#9802,interpretune#9803,interpretune#9804 --blocked "interpretune#9802=" --blocked "interpretune#9803="
OUT=$($H next --shoal $SID --spec "$SPEC" --slug tl --no-sync --peek 2>&1)
! grep -q "^BOARD " <<<"$OUT" || fail "candidates shown for a full queue :: $OUT"
# --peek never claims to have marked mail read
$H msg --shoal $SID --spec "$SPEC" --by up --to tl --text "peek probe" --no-sync >/dev/null
check "left unread for the role" $H next --shoal $SID --spec "$SPEC" --slug tl --no-sync --peek
check "posture=idle" $Q --slug tl --idle --next ""

echo "== 19 verified blockers (features.blockers): stale holds and cleared blockers stop parking work"
Q="$H queue --shoal $SID --spec $SPEC --no-sync"
NX="$H next --shoal $SID --spec $SPEC --no-sync --peek"
export LEASE_STUB=free
# before the feature: up idles behind a hold with board candidates, and the watcher stays silent
check "posture=idle" $Q --slug up --idle --next interpretune#9901 --blocked "interpretune#9901=hold:before"
check "posture=active" $Q --slug up --active interpretune#9301:fix --next ""
check "done=\['interpretune#9301:fix'\]" $Q --slug up --done interpretune#9301:fix --url https://example.invalid/pr/2 \
  --idle --next interpretune#9901 --blocked "interpretune#9901=hold:before"
WATCH_BOARD='import sys; sys.path.insert(0, sys.argv[1]); from roles import ShoalSpec; from ledger import Ledger, View
from ledger_watch import actionable
spec = ShoalSpec.load(sys.argv[2]); view = View(Ledger(spec.sid, root=spec.ledger_dir, sync=False), spec)
print(sum(1 for i, _ in actionable(view, "up", spec.role("up"), spec, board=True) if i.startswith("board:")))'
[ "$(python3 -c "$WATCH_BOARD" "$SHOALD" "$SPEC")" = 0 ] || fail "board wake for a role with next items, feature off"
python3 - "$SPEC" <<'PY2'
import json,sys; p=sys.argv[1]; d=json.load(open(p))
d["features"]["blockers"]={"hold_ttl_min":60,"peer_ttl_min":120,"hold_page_min":180}; json.dump(d,open(p,"w"),indent=1)
PY2
[ "$(python3 -c "$WATCH_BOARD" "$SHOALD" "$SPEC")" -gt 0 ] || fail "no board wake for an idle role whose next items are all blocked"
# idle is refused while strong candidates exist, until each is passed with a reason
OUT=$($Q --slug tl --idle --next interpretune#9902,interpretune#9903 --blocked "interpretune#9902=hold:design" \
  --blocked "interpretune#9903=up:interpretune#9301" 2>&1 || true)
grep -q "already cleared (interpretune#9301 done by up" <<<"$OUT" || fail "a peer blocker on a done item was accepted :: $OUT"
check "a maintainer blocker needs its ask recorded" $Q --slug tl --idle --next interpretune#9902 --blocked "interpretune#9902=ask:scope"
OUT=$($Q --slug tl --idle --next interpretune#9902 --blocked "interpretune#9902=hold:design" 2>&1 || true)
grep -q "idle while the board offers eligible items" <<<"$OUT" || fail "idle accepted beside strong candidates :: $OUT"
for i in 1 2 3 4; do
  C=$(grep -o "eligible items ([^)]*)" <<<"$OUT" | sed 's/eligible items (//; s/)//; s/, / /g')
  [ -n "$C" ] || break
  P=""; for c in $C; do P="$P --pass ${c%%:*}:out-of-lane:note=selftest"; done
  OUT=$($Q --slug tl --idle --next interpretune#9902 --blocked "interpretune#9902=hold:design" $P 2>&1 || true)
done
grep -q "posture=idle" <<<"$OUT" || fail "idle still refused after passing every candidate :: $OUT"
# a hold expires after hold_ttl_min: next names it, idle is refused, parked-work wakes
OUT=$(env SHOAL_HEALTH_SHIFT_MIN=61 $NX --slug tl 2>&1)
grep -q "BLOCKER STALE interpretune#9902 <- hold:design (expired" <<<"$OUT" || fail "expired hold not named :: $OUT"
OUT=$(env SHOAL_HEALTH_SHIFT_MIN=61 $Q --slug tl --idle 2>&1 || true)
grep -q "interpretune#9902 <- hold:design is stale (expired" <<<"$OUT" || fail "idle accepted behind an expired hold :: $OUT"
rm -f /tmp/$SID/health.json
env SHOAL_HEALTH_SHIFT_MIN=61 $H health --spec "$SPEC" --instance a >/dev/null 2>&1   # first sighting recorded
check "tl parked-work step1:wake" env SHOAL_HEALTH_SHIFT_MIN=92 $H health --spec "$SPEC" --instance a --dry-run
# re-recording is the re-verification; a hold kept past hold_page_min pages the maintainer once
check "posture=idle" env SHOAL_HEALTH_SHIFT_MIN=61 $Q --slug tl --idle --blocked "interpretune#9902=hold:design still"
python3 - "$A/shoals/$SID/ledger/tl.jsonl" <<'PY2' || fail "blocker metadata not carried"
import json,sys; q=[json.loads(l) for l in open(sys.argv[1]) if '"type":"queue"' in l][-1]
m=q["next_blocked_meta"]["interpretune#9902"]; assert m["n"] == 1 and m["first"] <= m["since"], m
PY2
OUT=$(env SHOAL_HEALTH_SHIFT_MIN=62 $H health --spec "$SPEC" --instance a --dry-run 2>&1)
! grep -q "tl hold-long" <<<"$OUT" || fail "hold-long paged before hold_page_min :: $OUT"
# hold-long wakes the role first, and pages only if the hold still stands 240 min after that wake
rm -f /tmp/$SID/health.json
check "tl hold-long step1:wake" env SHOAL_HEALTH_SHIFT_MIN=181 $H health --spec "$SPEC" --instance a
OUT=$(env SHOAL_HEALTH_SHIFT_MIN=300 $H health --spec "$SPEC" --instance a --dry-run 2>&1)
! grep -q "tl hold-long step2:page" <<<"$OUT" || fail "hold-long paged before its wake had 240 min :: $OUT"
check "tl hold-long step2:page" env SHOAL_HEALTH_SHIFT_MIN=422 $H health --spec "$SPEC" --instance a --dry-run
rm -f /tmp/$SID/health.json /tmp/$SID/wake-tl
# checkable blockers: a free lease has cleared (refused when recorded); a held one parks the item
check "already cleared (lease gpu free" $Q --slug tl --idle --blocked "interpretune#9902=lease:gpu"
check "posture=idle" env LEASE_STUB=held $Q --slug tl --idle --blocked "interpretune#9902=lease:gpu"
OUT=$($NX --slug tl 2>&1)
grep -q "BLOCKER STALE interpretune#9902 <- lease:gpu (resolved: lease gpu free" <<<"$OUT" || fail "cleared lease blocker not named :: $OUT"
python3 - "$SPEC" <<'PY2'
import json,sys; p=sys.argv[1]; d=json.load(open(p)); d["features"].pop("blockers",None); json.dump(d,open(p,"w"),indent=1)
PY2
check "posture=idle" $Q --slug tl --idle --next ""
check "posture=idle" $Q --slug up --idle --next ""

echo "== 20 waits keep their targets; CI waits need one (waittargets); finished items leave next"
Q="$H queue --shoal $SID --spec $SPEC --no-sync"
export LEASE_STUB=held
check "posture=waiting/local-ci" $Q --slug tl --active interpretune#9951:fix --next "" --wait local-ci \
  --holder "job j1" --target lease:gpu
# a transition re-declaring the same wait without --target keeps it (it used to blank it)
check "target lease:gpu carried forward" $H transition --shoal $SID --spec "$SPEC" --slug tl --to waiting \
  --sub local-ci --holder "job j1" --monitor "waiter 1" --no-ledger
python3 -c "import json; w=json.load(open('/tmp/$SID/wait-tl')); assert w['target']=='lease:gpu', w" \
  || fail "transition blanked the wait target"
# before waittargets a target-less CI wait is accepted; with it, refused
check "posture=waiting/local-ci" $Q --slug tl --wait local-ci --holder "job j2"
python3 - "$SPEC" <<'PY2'
import json,sys; p=sys.argv[1]; d=json.load(open(p)); d["features"]["waittargets"]=True; json.dump(d,open(p,"w"),indent=1)
PY2
check "a local-ci wait needs a checkable target" $Q --slug tl --wait local-ci --holder "job j3"
check "posture=waiting/local-ci" $Q --slug tl --wait local-ci --holder "job j3" --target lease:gpu
# the watcher wakes the role once its local target resolves, on the second tick that sees it
WATCH_WAIT='import os, sys; sys.path.insert(0, sys.argv[1]); from roles import ShoalSpec; from ledger import Ledger, View
import ledger_watch as lw
spec = ShoalSpec.load(sys.argv[2]); view = View(Ledger(spec.sid, root=spec.ledger_dir, sync=False), spec)
for tick in (1, 2):
    got = [d for i, d in lw.actionable(view, "tl", spec.role("tl"), spec, board=False) if i.startswith("waitdone:")]
    print(tick, len(got), got[0][:40] if got else "")'
OUT=$(LEASE_STUB=held python3 -c "$WATCH_WAIT" "$SHOALD" "$SPEC")
grep -q "^2 0" <<<"$OUT" || fail "watcher woke on a pending wait target :: $OUT"
OUT=$(LEASE_STUB=free python3 -c "$WATCH_WAIT" "$SHOALD" "$SPEC")
grep -q "^1 0" <<<"$OUT" && grep -q "^2 1 WAIT RESOLVED lease:gpu" <<<"$OUT" || fail "no grace-then-wake on a resolved wait :: $OUT"
# a finished item cannot sit in next (the role recorded it done, or a peer did)
check "lists finished items: interpretune#9301" $Q --slug tl --active interpretune#9951:fix --next interpretune#9301
python3 - "$SPEC" <<'PY2'
import json,sys; p=sys.argv[1]; d=json.load(open(p)); d["features"].pop("waittargets",None); json.dump(d,open(p,"w"),indent=1)
PY2
export LEASE_STUB=free
check "posture=idle" $Q --slug tl --idle --next ""

echo "== 21 health-review fm_report: runs on a group's own streams, no host values"
FMR="$TESTD/../plugins/shoal-skills/skills/shoal-health-review/scripts/fm_report.py"
OUT=$(SHOAL_DATA_ROOT="$S/data" python3 "$FMR" --spec "$SPEC" --days 2 2>&1) || fail "fm_report failed :: $OUT"
grep -q "^metric .*trend" <<<"$OUT" || fail "fm_report printed no metric table :: $OUT"
grep -q "FM9:hold_renewals" <<<"$OUT" || fail "fm_report missed the hold renewals this suite wrote :: $OUT"
SHOAL_DATA_ROOT="$S/data" python3 "$FMR" --spec "$SPEC" --json | python3 -c "import json,sys; d=json.load(sys.stdin); assert 'counts' in d and 'hours' in d" \
  || fail "fm_report --json"
OUT=$(env -u SHOAL_DATA_ROOT python3 "$FMR" --spec "$SPEC" 2>&1 || true)
grep -q "no data root" <<<"$OUT" || fail "fm_report ran without a data root :: $OUT"

echo "== 22 revive (features.health.revive): dead Claude workers are respawned and a dead relay relaunched"
# the measured shape (2026-10-02): the background service reaped every worker, so `claude agents
# --json --all` lists the role and the relay with no pid, and both waiters are gone
rm -f /tmp/$SID/health.json /tmp/$SID/relay-watch.pid /tmp/$SID/wake-tl /tmp/$SID/ledger-watch-tl.state
FS_REAPED='{"claude":[{"id":"aaaa1111","sessionId":"aaaa1111-0000","status":"","state":"stopped"},{"id":"rrrr3333","sessionId":"rrrr3333-0000","status":"","state":"stopped"}],"opencode":{}}'
HV="$H health --spec $SPEC --instance a --dry-run"
# watcher-dead's ladder runs from its first sighting; seed that sighting rather than run a real
# first pass (which, with revive on, would launch a real relay session)
SEED_TL='import datetime, json, sys; now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
json.dump({"ladders": {"tl|watcher-dead|pid:none": {"since": now, "step": 0}}}, open(sys.argv[1], "w"))'
python3 -c "$SEED_TL" /tmp/$SID/health.json
# negative control, feature off: the relay only pages and the role's wake has no adapter
OUT=$(env SHOAL_FAKE_SESSIONS="$FS_REAPED" SHOAL_HEALTH_SHIFT_MIN=60 $HV 2>&1)
grep -q "wake-relay relay-dead step1:page" <<<"$OUT" || fail "relay-dead did not page with revive off :: $OUT"
grep -q "tl watcher-dead step1:wake -> no-adapter (claude watcher dead, wake relay waiter dead)" <<<"$OUT" \
  || fail "control: a reaped role should have no wake path with revive off :: $OUT"
! grep -q "respawned\|relaunch" <<<"$OUT" || fail "revive acted while off :: $OUT"
python3 - "$SPEC" <<'PY2'
import json,sys; p=sys.argv[1]; d=json.load(open(p)); h=d["features"].get("health")
d["features"]["health"]=dict(h if isinstance(h,dict) else {}, revive=True); json.dump(d,open(p,"w"),indent=1)
PY2
python3 -c "$SEED_TL" /tmp/$SID/health.json
OUT=$(env SHOAL_FAKE_SESSIONS="$FS_REAPED" SHOAL_HEALTH_SHIFT_MIN=60 $HV 2>&1)
grep -q "wake-relay relay-dead step1:relaunch -> relay-launch rc=0: \[dry-run\] relay-launch $SID" <<<"$OUT" \
  || fail "dead relay not relaunched first :: $OUT"
grep -q "tl watcher-dead step1:wake -> wake-relay (queued; relay down, relaunch pending) (dry-run); respawned aaaa1111 (dry-run)" <<<"$OUT" \
  || fail "reaped role not respawned and queued for the relaunching relay :: $OUT"
[ "$(grep -n 'relay-dead' <<<"$OUT" | head -1 | cut -d: -f1)" -lt "$(grep -n 'tl watcher-dead' <<<"$OUT" | head -1 | cut -d: -f1)" ] \
  || fail "relay relaunch must precede the role wakes it will carry :: $OUT"
# a live worker (pid present) is never respawned
FS_LIVE='{"claude":[{"id":"aaaa1111","sessionId":"aaaa1111-0000","status":"idle","state":"done","pid":4242}],"opencode":{}}'
python3 -c "$SEED_TL" /tmp/$SID/health.json
OUT=$(env SHOAL_FAKE_SESSIONS="$FS_LIVE" SHOAL_HEALTH_SHIFT_MIN=60 $HV 2>&1)
! grep -q "respawned aaaa1111" <<<"$OUT" || fail "respawned a session whose worker is alive :: $OUT"
python3 - "$SPEC" <<'PY2'
import json,sys; p=sys.argv[1]; d=json.load(open(p)); d["features"]["health"].pop("revive",None); json.dump(d,open(p,"w"),indent=1)
PY2
rm -f /tmp/$SID/health.json

echo "== 23 a peer blocker must name a role that can take the item (FM-4); peer mail says it is mail"
Q="$H queue --shoal $SID --spec $SPEC --no-sync"
python3 - "$SPEC" <<'PY2'
import json,sys; p=sys.argv[1]; d=json.load(open(p)); d["features"]["blockers"]=True
[r.update(fill="assigned") for r in d["roles"] if r["slug"]=="up"]; json.dump(d,open(p,"w"),indent=1)
PY2
# measured shape: work delegated to an assigned role by mail, then blocked on it with no offer
check "note: this is mail. up reads it" $H msg --shoal $SID --spec "$SPEC" --by tl --to up --no-sync \
  --text "please review and merge interpretune#9971"
check "up is fill=assigned: it works only items offered or handed to it" $Q --slug tl --active interpretune#9970:fix \
  --next interpretune#9971 --blocked "interpretune#9971=up:interpretune#9971"
OUT=$($H msg --shoal $SID --spec "$SPEC" --by tl --to up --no-sync --text "no item named here" 2>&1)
! grep -q "note: this is mail" <<<"$OUT" || fail "mail note printed for a msg naming no item :: $OUT"
# once the maintainer offers it, the same blocker is accepted
check "offered interpretune#9971:review to up" $H offer --shoal $SID --spec "$SPEC" --to up --item interpretune#9971:review --no-sync
check "posture=active" $Q --slug tl --active interpretune#9970:fix --next interpretune#9971 --blocked "interpretune#9971=up:interpretune#9971"
# carried forward after the offer is withdrawn, the same blocker is stale: it parks nothing, and the
# named role is not woken about it (the waiter is)
check "withdrew" $H withdraw --shoal $SID --spec "$SPEC" --to up --item interpretune#9971 --reason selftest --no-sync
check "BLOCKER STALE interpretune#9971 <- up:interpretune#9971 (expired: untakeable: up is fill=assigned" \
  $H next --shoal $SID --spec "$SPEC" --slug tl --peek --no-sync
# a fill=self peer can claim board items itself: not checked
python3 - "$SPEC" <<'PY2'
import json,sys; p=sys.argv[1]; d=json.load(open(p)); [r.update(fill="self") for r in d["roles"] if r["slug"]=="up"]
json.dump(d,open(p,"w"),indent=1)
PY2
check "posture=active" $Q --slug tl --active interpretune#9970:fix --next interpretune#9972 --blocked "interpretune#9972=up:interpretune#9972"
python3 - "$SPEC" <<'PY2'
import json,sys; p=sys.argv[1]; d=json.load(open(p)); d["features"].pop("blockers",None); json.dump(d,open(p,"w"),indent=1)
PY2
check "posture=idle" $Q --slug tl --idle --next ""
# a user --wake msg to a role whose watcher is dead goes through the wake adapter (no watcher runs here)
check "wake via no-adapter (claude watcher dead, wake relay waiter dead) (tl's ledger-watch is not running)" \
  $H msg --shoal $SID --spec "$SPEC" --to tl --wake --text "operator wake" --no-sync

echo "SELFTEST H4 OK ($S)"
rm -rf /tmp/$SID
