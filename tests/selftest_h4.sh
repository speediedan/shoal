#!/usr/bin/env bash
# Isolated proof of the H4 components (architecture doc §4-§6): working context, board
# digest, batch triage, digest-driven ledger-watch wake, exit verdicts, job units, the work loop
# (next/queue), work-only wakes, the health quorum, user-offline mode and direct Claude wakes.
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
cleanup() { pkill -f -- '(cli\.py|shoal) ledger-watch --shoal h4selftest' 2>/dev/null || true; }
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
 "ledger":{"repo":"$S/remote/q.git","dir":"$A"}, "features":{"digest":true,"waitgate":false},
 "sources":[{"kind":"github-project","owner":"example","number":2}],
 "roles":[
  {"slug":"tl","ecosystem":"claude","title":"tl","doc":"$DOC_TL","fill":"self","session":"aaaa1111","scope":{"labels_any":["tl-4-migration"]}},
  {"slug":"up","ecosystem":"claude","title":"up","doc":"$DOC_UP","fill":"self","session":"bbbb2222"}
 ],"generations":[{"gen":"r1","incarnation":0,"started_utc":"2026-09-22T00:00:00Z","ended_utc":"","sessions":{},"run":{}}]}
JSON
$H ledger-init --spec "$SPEC" >/dev/null

python3 "$TESTD/test_resource_usage.py" || fail "resource usage telemetry and placement"
python3 "$TESTD/test_role_time.py" || fail "role time accounting"
python3 "$TESTD/test_role_time_adapters.py" || fail "role time adapters"

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
# a remote job (--on): the ssh stub runs the script locally with a scratch HOME as the "remote" home,
# so the same transient-unit machinery is exercised over the ssh boundary (and the unit name differs
# only by job name, so the stub-run unit coexists with the local ones)
mkdir -p "$S/sshstub" "$S/remote-home/work"
cat > "$S/sshstub/ssh" <<'SH'
#!/bin/sh
while [ $# -gt 1 ]; do shift; done
HOME="$SSH_STUB_HOME" exec /bin/bash -c "$1"
SH
chmod +x "$S/sshstub/ssh"
export SSH_STUB_HOME="$S/remote-home"
check "dry-run.*ssh fakehost" env PATH="$S/sshstub:$PATH" $H job run --shoal $SID --slug tl --name rmt --on fakehost --cwd "$S/remote-home/work" --dry-run -- true
check "JOB-STARTED .* on fakehost" env PATH="$S/sshstub:$PATH" $H job run --shoal $SID --slug tl --name rmt --on fakehost --cwd "$S/remote-home/work" -- bash -c 'echo remote-ok; sleep 1'
[ -f "$S/remote-home/shoal-jobs/shoal-$SID-tl-rmt.log" ] || fail "remote job log not under the remote home"
check "result=success exit=0" env PATH="$S/sshstub:$PATH" $H job wait --shoal $SID --slug tl --name rmt --poll 1
check "remote-ok" env PATH="$S/sshstub:$PATH" $H job wait --shoal $SID --slug tl --name rmt --poll 1
check "on fakehost" env PATH="$S/sshstub:$PATH" $H job status --shoal $SID
OUT=$(env PATH="$S/sshstub:$PATH" $H job run --shoal $SID --slug tl --name nobad --on fakehost --cwd "$S/remote-home/absent" -- true 2>&1 || true)
grep -q "no such remote cwd\|remote start on fakehost failed" <<<"$OUT" || fail "missing remote cwd not refused :: $OUT"
echo "remote job ok"
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
d["features"]={"digest":True,"jobs":True,"workwake":True,"waitgate":False,"health":{"validity_wake_min":90}}
json.dump(d,open(p,"w"),indent=1)
PY
# a spec written before the wake relay's retirement: features.relay, health.relay_fallback and a live
# wake-relay role. It loads, and nothing launches, watches, wakes or shows the relay
python3 - "$SPEC" <<'PY'
import json,sys; p=sys.argv[1]; d=json.load(open(p))
d["features"]["relay"]=True; d["features"]["health"]=dict(d["features"]["health"], relay_fallback=True)
d["roles"].append({"slug":"wake-relay","ecosystem":"claude","title":"wake-relay","doc":"x","fill":"none","session":"rrrr3333"})
json.dump(d,open(p,"w"),indent=1)
PY
python3 - "$SHOALD" "$SPEC" <<'PY' || fail "an old spec with the retired relay did not load cleanly"
import sys; sys.path.insert(0, sys.argv[1])
from roles import ShoalSpec
import backends
spec = ShoalSpec.load(sys.argv[2])
assert spec.role("wake-relay") is None and all("relay" not in r.slug for r in spec.roles), [r.slug for r in spec.roles]
assert backends.direct_wake_mode(spec) == "idle"  # relay_fallback is ignored; direct wake is always on
PY
OUT=$($H show --shoal $SID --spec "$SPEC" --no-sync 2>&1)
! grep -qi "relay" <<<"$OUT" || fail "show still lists the retired relay :: $OUT"
OUT=$(SHOAL_HEALTH_SHIFT_MIN=600 $H health --spec "$SPEC" --instance a --dry-run 2>&1)
! grep -qi "relay" <<<"$OUT" || fail "health still judges the retired relay :: $OUT"
for v in relay-init relay-launch relay-watch; do
  [ "$(rc_of $H $v --shoal $SID)" = 2 ] || fail "the retired verb $v still parses"
done
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

echo "== 10 Claude direct wake: a dead watcher is answered by a direct delivery, never a relay"
python3 - "$SHOALD" "$SPEC" <<'PY' || fail "a dead Claude watcher should get a direct delivery"
import json, os, sys; sys.path.insert(0, sys.argv[1])
from roles import ShoalSpec; from health import request_wake
spec = ShoalSpec.load(sys.argv[2])
got = request_wake(spec, "tl", "direct test", "watcher-dead", dry_run=True)
assert got == "deferred (session not listed)", got  # the empty fake world lists no session to resume
os.environ["SHOAL_FAKE_SESSIONS"] = json.dumps({"claude": [{"id": "aaaa1111", "sessionId": "aaaa1111-0000"}], "opencode": {}})
got = request_wake(spec, "tl", "direct test", "watcher-dead", dry_run=True)
assert got == "resume (dry-run)", got  # no worker: resumed directly
PY
[ ! -e /tmp/$SID/relay-queue ] || fail "a wake was queued for a relay"

echo "== 11 user-offline mode: banner, asks, deferred pages, back-online summary"
$H queue --shoal $SID --spec "$SPEC" --slug tl --ask "merge interpretune#9201 (PR green)" --ask "ruling on #9202 scope" --no-sync >/dev/null
export SHOAL_FAKE_REF_STATE='{"speediedan/interpretune#9201": "MERGED", "speediedan/interpretune#9202": "OPEN"}'
check "exactly one of --shoal ID or --all" $H offline on --all --shoal $SID --no-sync
check "1 shoal(s) in view" env SHOAL_UI_LEDGER_ROOTS="$ROOT" $H offline on --all --dry-run --no-sync
mkdir -p "$S/xdg-off/shoal"
printf '{"offline_defaults": {"until": "cfg-until", "note": "cfg-note"}}' > "$S/xdg-off/shoal/ui.json"
check "user-offline on" env SHOAL_UI_LEDGER_ROOTS="$ROOT" XDG_CONFIG_HOME="$S/xdg-off" $H offline on --all --no-sync
OUT=$($H next --shoal $SID --spec "$SPEC" --slug tl --peek --no-sync 2>&1)
grep -q "until cfg-until (cfg-note)" <<<"$OUT" || fail "offline config defaults did not reach the mode event :: $OUT"
check "user-offline off" $H offline off --shoal $SID --spec "$SPEC" --no-sync
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
# a wait that leaves a next item unblocked is refused at the write (waitgate: on by default, off in this suite's
# base spec so the sections that predate it keep their writes) ...
python3 - "$SPEC" <<'PY2'
import json,sys; p=sys.argv[1]; d=json.load(open(p)); d["features"].pop("waitgate",None); json.dump(d,open(p,"w"),indent=1)
PY2
check "waiting (remote-ci) with unblocked next items (interpretune#9603)" env LEASE_STUB=held $Q --slug tl \
   --active interpretune#9601:fix --blocked interpretune#9603= --wait remote-ci --holder "gate" --target lease:gpu
check "posture=waiting/local-ci" env LEASE_STUB=held $Q --slug tl --active interpretune#9601:fix --blocked interpretune#9603= \
   --wait local-ci --holder "gate" --target lease:gpu
# ... and, with the gate off, is parked work for the quorum (idle is always gated at the write)
python3 - "$SPEC" <<'PY2'
import json,sys; p=sys.argv[1]; d=json.load(open(p)); d["features"]["waitgate"]=False; json.dump(d,open(p,"w"),indent=1)
PY2
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
# page channels: string or list specs normalize, and an ntfy page POSTs to a local stub
python3 - "$SHOALD" <<'PY' || fail "ntfy page channel"
import sys, json, threading
sys.path.insert(0, sys.argv[1])
from http.server import BaseHTTPRequestHandler, HTTPServer
import health
from roles import ShoalSpec
got = {}
class Stub(BaseHTTPRequestHandler):
    def do_POST(self):
        got["path"] = self.path; got["title"] = self.headers.get("Title")
        got["body"] = self.rfile.read(int(self.headers.get("Content-Length", 0))).decode()
        self.send_response(200); self.end_headers(); self.wfile.write(b"{}")
    def log_message(self, *a): pass
srv = HTTPServer(("127.0.0.1", 0), Stub); threading.Thread(target=srv.serve_forever, daemon=True).start()
base = f"http://127.0.0.1:{srv.server_address[1]}"
mk = lambda page: ShoalSpec.from_dict({"shoal": "pgtest", "features": {"health": {"page": page}}, "roles": []})
assert health.page_channels(mk("gh-issue:o/r#1")) == ["gh-issue:o/r#1"]
assert health.page_channels(mk(["gh-issue:o/r#1", "ntfy:topic-x"])) == ["gh-issue:o/r#1", "ntfy:topic-x"]
assert health.page_channels(mk(None)) == []
assert health.ntfy_url("ntfy:topic-x") == "https://ntfy.sh/topic-x"
assert health.ntfy_url("ntfy:example.com/t") == "https://example.com/t"
assert health.ntfy_url(f"ntfy:{base}/t") == f"{base}/t"
out = health.deliver_page(mk(f"ntfy:{base}/topic-x"), "watcher dead on tl")
assert out == "+ntfy", out
assert got["path"] == "/topic-x" and "pgtest page" in got["title"] and "watcher dead" in got["body"], got
bad = health.deliver_page(mk("ntfy:http://127.0.0.1:1/nope"), "x")
assert bad.startswith(" (ntfy FAILED"), bad  # a dead channel reports, never raises
srv.shutdown()
print("ntfy page channel ok")
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
for slug in tl; do
  OUT=$($H brief --spec "$SPEC" --slug $slug 2>&1)
  grep -q "TWO separate commands" <<<"$OUT" || fail "$slug brief lacks the two-command pid proof"
  grep -q 'never use \$(...) command substitution' <<<"$OUT" || fail "$slug brief lacks the \$(...) ban"
  ! grep -qE 'ps -p \$\(|ps -p plus sidecar' <<<"$OUT" || fail "$slug brief still combines the pid proof"
done

echo "== 17 upgrade plans no relay re-arm: only ledger-watch is re-armed, and --rearm-verify-s is gone"
python3 -c "import json; d=json.load(open('$SPEC')); d.pop('tooling', None); json.dump(d, open('$SPEC','w'), indent=1)"
OUT=$($H upgrade --spec "$SPEC" --rearm now --skip-selftests --allow-dirty --dry-run 2>&1 || true)
grep -q "re-arm (ledger-watch): " <<<"$OUT" || fail "upgrade lost its ledger-watch re-arm class :: $OUT"
! grep -qi "relay" <<<"$OUT" || fail "upgrade still plans a relay re-arm :: $OUT"
[ "$(rc_of $H upgrade --spec "$SPEC" --rearm-verify-s 2 --skip-selftests --dry-run)" = 2 ] || fail "--rearm-verify-s still parses"

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

echo "== 22 direct wake: a reaped Claude worker is re-armed or resumed directly; old relay-era keys change nothing"
# the measured shape (2026-10-02): the background service reaped every worker, so `claude agents
# --json --all` lists the role with no pid, and its waiters are gone
rm -f /tmp/$SID/health.json /tmp/$SID/wake-tl /tmp/$SID/ledger-watch-tl.state
FS_REAPED='{"claude":[{"id":"aaaa1111","sessionId":"aaaa1111-0000","status":"","state":"stopped"}],"opencode":{}}'
HV="$H health --spec $SPEC --instance a --dry-run"
# watcher-dead's ladder runs from its first sighting; seed that sighting rather than run a first pass
SEED_TL='import datetime, json, sys; now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
json.dump({"ladders": {"tl|watcher-dead|pid:none": {"since": now, "step": 0}}}, open(sys.argv[1], "w"))'
# the default (direct_wake idle) and the relay-era values (`off`, revive, relay_fallback) all read the same:
# the quorum re-arms the detached watcher instead of waking the role, and the watcher resumes the session
for dw in "" off; do
  python3 - "$SPEC" "$dw" <<'PY2'
import json,sys; p=sys.argv[1]; d=json.load(open(p)); h=d["features"].get("health"); h=dict(h if isinstance(h,dict) else {})
h.pop("direct_wake", None)
if sys.argv[2]: h.update(direct_wake=sys.argv[2], revive=True, relay_fallback=True)
d["features"]["health"]=h; json.dump(d,open(p,"w"),indent=1)
PY2
  python3 -c "$SEED_TL" /tmp/$SID/health.json
  OUT=$(env SHOAL_FAKE_SESSIONS="$FS_REAPED" SHOAL_HEALTH_SHIFT_MIN=60 $HV 2>&1)
  grep -q "tl watcher-dead step1:rearm -> re-arm by the quorum (direct; no wake) (dry-run)" <<<"$OUT" \
    || fail "direct wake ($dw): a dead detached watcher should be re-armed, not woken :: $OUT"
  ! grep -qi "respawned\|relay\|tl watcher-dead step1:wake" <<<"$OUT" || fail "direct wake ($dw): re-arm must not also wake :: $OUT"
done
python3 - "$SPEC" <<'PY2'
import json,sys; p=sys.argv[1]; d=json.load(open(p)); d["features"]["health"]["rearm"]=False; json.dump(d,open(p,"w"),indent=1)
PY2
python3 -c "$SEED_TL" /tmp/$SID/health.json
OUT=$(env SHOAL_FAKE_SESSIONS="$FS_REAPED" SHOAL_HEALTH_SHIFT_MIN=60 $HV 2>&1)
grep -q "tl watcher-dead step1:wake -> resume (dry-run)" <<<"$OUT" \
  || fail "direct wake: a workerless session should be resumed directly :: $OUT"
# direct_wake dead: an in-session watcher, so the quorum wakes the role; the reaped session is resumed
python3 - "$SPEC" <<'PY2'
import json,sys; p=sys.argv[1]; d=json.load(open(p)); d["features"]["health"].pop("rearm",None)
d["features"]["health"]["direct_wake"]="dead"; json.dump(d,open(p,"w"),indent=1)
PY2
python3 -c "$SEED_TL" /tmp/$SID/health.json
OUT=$(env SHOAL_FAKE_SESSIONS="$FS_REAPED" SHOAL_HEALTH_SHIFT_MIN=60 $HV 2>&1)
grep -q "tl watcher-dead step1:wake -> resume (dry-run)" <<<"$OUT" \
  || fail "direct_wake dead: a workerless session should be resumed directly :: $OUT"
# ... and a live worker is never stopped under `dead`: the wake waits
FS_LIVE='{"claude":[{"id":"aaaa1111","sessionId":"aaaa1111-0000","status":"idle","state":"done","pid":4242}],"opencode":{}}'
python3 -c "$SEED_TL" /tmp/$SID/health.json
OUT=$(env SHOAL_FAKE_SESSIONS="$FS_LIVE" SHOAL_HEALTH_SHIFT_MIN=60 $HV 2>&1)
grep -q "tl watcher-dead step1:wake -> deferred (live worker (direct_wake dead))" <<<"$OUT" \
  || fail "direct_wake dead: a live worker should not be stopped :: $OUT"
python3 - "$SPEC" <<'PY2'
import json,sys; p=sys.argv[1]; d=json.load(open(p)); h=d["features"]["health"]
for k in ("direct_wake", "revive", "relay_fallback"): h.pop(k, None)
json.dump(d,open(p,"w"),indent=1)
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
check "wake via deferred (session not listed) (tl's ledger-watch is not running)" \
  $H msg --shoal $SID --spec "$SPEC" --to tl --wake --text "operator wake" --no-sync

echo "== 24 a claude call from a systemd unit runs in its own scope; a watcher binds its own Claude session id"
python3 - "$SHOALD" <<'PY2' || fail "detached() argv"
import os, shutil, sys; sys.path.insert(0, sys.argv[1]); import backends
os.environ.pop("INVOCATION_ID", None)
assert backends.detached(["claude", "x"]) == ["claude", "x"]
os.environ["INVOCATION_ID"] = "t"
got = backends.detached(["claude", "x"])
assert got == (["claude", "x"] if not shutil.which("systemd-run") else
               [shutil.which("systemd-run"), "--user", "--scope", "--collect", "--quiet", "claude", "x"]), got
PY2
OUT=$(env CLAUDE_CODE_SESSION_ID=cccc9999-real timeout 5 $H ledger-watch --shoal $SID --spec "$SPEC" --slug tl --wake print \
  --session bridge-id-not-mine --interval 60 --sidecar "$S/sc-bind.state" --no-sync 2>&1 || true)
grep -q "binding cccc9999-real" <<<"$OUT" || fail "watcher kept a foreign --session :: $OUT"
grep -q "^session_id=cccc9999-real" "$S/sc-bind.state" || fail "sidecar not bound to the real session :: $(cat $S/sc-bind.state)"

echo "== 25 restart stops a watcher still sending to a replaced session; warns about a Claude one"
mkdir -p "$S/fakew"; printf 'import time\ntime.sleep(120)\n' > "$S/fakew/cli.py"
python3 "$S/fakew/cli.py" ledger-watch --shoal $SID --slug tl --wake send:oldsess01 --interval 60 & P_OLD=$!
python3 "$S/fakew/cli.py" ledger-watch --shoal $SID --slug tl --wake exit --session oldclaude9 --interval 60 & P_CL=$!
python3 "$S/fakew/cli.py" ledger-watch --shoal $SID --slug tl --wake send:newsess02 --interval 60 & P_NEW=$!
sleep 0.5
OUT=$(python3 - "$SHOALD" "$SPEC" <<'PY2'
import sys; sys.path.insert(0, sys.argv[1]); import cli
from roles import ShoalSpec
spec = ShoalSpec.load(sys.argv[2]); r = spec.role("tl"); r.session = "newsess02"
cli.reap_stale_watchers(spec, r, ["oldsess01", "oldclaude9", "newsess02"])
PY2
)
sleep 0.5
grep -q "stopped stale ledger-watch pid $P_OLD" <<<"$OUT" || fail "stale send: watcher not stopped :: $OUT"
grep -q "WARN tl: ledger-watch pid $P_CL is still bound to replaced session oldclaude9" <<<"$OUT" || fail "no WARN for the Claude watcher :: $OUT"
! kill -0 $P_OLD 2>/dev/null || fail "stale opencode watcher still running"
kill -0 $P_CL 2>/dev/null || fail "Claude watcher was killed (it must only be reported)"
kill -0 $P_NEW 2>/dev/null || fail "the current session's watcher was touched"
kill $P_CL $P_NEW 2>/dev/null; wait $P_CL $P_NEW 2>/dev/null || true

echo "== 26 health_condition telemetry: a ladder that opens or clears is recorded with its duration"
python3 - /tmp/$SID/health.json <<'PY2'
import datetime, json, sys
t = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
json.dump({"ladders": {"ghost|parked-work|parked:x#1": {"since": t, "step": 0}}}, open(sys.argv[1], "w"))
PY2
env SHOAL_FAKE_SESSIONS='{"claude":[{"sessionId":"aaaa1111-0000","status":"idle","state":"done","pid":7}],"opencode":{}}' \
  $H health --spec "$SPEC" --instance a >/dev/null 2>&1 || true
EV=$(cat "$S"/data/$SID/telemetry/*.jsonl 2>/dev/null | grep '"health_condition"')
python3 -c "import json,sys; ev=[json.loads(l) for l in sys.stdin if l.strip()]; c=[e for e in ev if e['event']=='close' and e['slug']=='ghost']; assert c and 29 <= float(c[0]['duration_min']) <= 31, ev" <<<"$EV" \
  || fail "no close event with its duration :: $EV"
python3 -c "import json,sys; ev=[json.loads(l) for l in sys.stdin if l.strip()]; assert any(e['event']=='open' for e in ev), ev" <<<"$EV" \
  || fail "no open event :: $EV"
OUT=$(SHOAL_DATA_ROOT="$S/data" python3 "$FMR" --spec "$SPEC" --days 2 2>&1)
grep -q "cond-open:" <<<"$OUT" && grep -q "cond-h:parked-work" <<<"$OUT" || fail "fm_report missed health_condition :: $OUT"
rm -f /tmp/$SID/health.json

echo "== 27 an opencode (send:) watcher keeps running across wakes, defers while busy, and arms once"
FS_OC_IDLE='{"claude":[],"opencode":{"oc9":"oc9 status:idle directory:/x"}}'
FS_OC_BUSY='{"claude":[],"opencode":{"oc9":"oc9 status:busy directory:/x"}}'
python3 - "$SHOALD" <<'PY2' || fail "session_busy"
import json, os, sys; sys.path.insert(0, sys.argv[1]); import ledger_watch as lw
os.environ["SHOAL_FAKE_SESSIONS"] = json.dumps({"claude": [], "opencode": {"oc9": "oc9 status:busy directory:/x"}})
assert lw.session_busy("oc9", "/x") is True
os.environ["SHOAL_FAKE_SESSIONS"] = json.dumps({"claude": [], "opencode": {"oc9": "oc9 status:idle directory:/x"}})
assert lw.session_busy("oc9", "/x") is False
os.environ["SHOAL_FAKE_SESSIONS"] = json.dumps({"claude": [], "opencode": {}})
assert lw.session_busy("oc9", "/x") is False  # unknown never holds a wake back
PY2
# a second arm while the first watcher runs is a no-op (stand-in watcher bound to send:oc9)
mkdir -p "$S/fakew2"; printf 'import time\ntime.sleep(60)\n' > "$S/fakew2/cli.py"
python3 "$S/fakew2/cli.py" ledger-watch --shoal $SID --slug up --wake send:oc9 --interval 60 & P_W=$!
sleep 0.3
printf 'session_id=oc9\npid=%s\nwake=send:oc9\n' $P_W > "$S/sc-oc.state"
check "ALREADY ARMED: ledger-watch pid $P_W" env SHOAL_FAKE_SESSIONS="$FS_OC_IDLE" $H ledger-watch --shoal $SID --spec "$SPEC" \
  --slug up --wake send:oc9 --session oc9 --interval 60 --sidecar "$S/sc-oc.state" --no-sync
kill $P_W 2>/dev/null; wait $P_W 2>/dev/null || true

echo "== 28 a direct (Claude, detached) watcher defers mid-turn and keeps running; \`--wake relay\` is read as direct"
python3 - "$SHOALD" "$SPEC" <<'PY2' || fail "direct watcher primitives"
import json, os, sys; sys.path.insert(0, sys.argv[1])
import ledger_watch as lw
from roles import ShoalSpec
spec = ShoalSpec.load(sys.argv[2]); r = spec.role("tl")
os.environ["SHOAL_FAKE_SESSIONS"] = json.dumps({"claude": [{"id": "aaaa1111", "sessionId": "aaaa1111-0000", "pid": 7, "tempo": "active"}], "opencode": {}})
assert lw.claude_session_busy("aaaa1111-0000") is True
os.environ["SHOAL_FAKE_SESSIONS"] = json.dumps({"claude": [{"id": "aaaa1111", "sessionId": "aaaa1111-0000", "pid": 7, "tempo": "blocked", "waitingFor": "input needed"}], "opencode": {}})
assert lw.claude_session_busy("aaaa1111-0000") is True   # at a real question prompt: never deliver onto it
os.environ["SHOAL_FAKE_SESSIONS"] = json.dumps({"claude": [{"id": "aaaa1111", "sessionId": "aaaa1111-0000", "pid": 7, "tempo": "blocked"}], "opencode": {}})
assert lw.claude_session_busy("aaaa1111-0000") is False  # self-reported blocked, no prompt: deliver (it waits for this)
os.environ["SHOAL_FAKE_SESSIONS"] = json.dumps({"claude": [{"id": "aaaa1111", "sessionId": "aaaa1111-0000", "pid": 7, "tempo": "idle"}], "opencode": {}})
assert lw.claude_session_busy("aaaa1111-0000") is False
# the quorum's between-turns test agrees: blocked with no prompt is between turns (a restart-ready role read as
# mid-question got no context-restart condition, 2026-10-26); a prompt (waitingFor or status waiting) is not
import copy, health
hr = copy.copy(r); hr.ecosystem, hr.session = "claude", "aaaa1111-0000"
for row, want in (({"tempo": "blocked", "status": "idle", "state": "blocked"}, True),
                  ({"tempo": "blocked", "waitingFor": "input needed"}, False),
                  ({"tempo": "blocked", "status": "waiting"}, False),
                  ({"tempo": "active", "status": "busy"}, False),
                  ({"tempo": "idle", "status": "idle"}, True)):
    os.environ["SHOAL_FAKE_SESSIONS"] = json.dumps({"claude": [dict(row, id="aaaa1111", sessionId="aaaa1111-0000", pid=7)], "opencode": {}})
    got = health.session_between_turns(hr, health.claude_busy(), spec.dir)
    assert got is want, (row, got)
assert health.watcher_dead(False, "idle", True) and not health.watcher_dead(False, "idle", False)
# the watcher's delivery is the direct one: no worker -> resume; no spec or role -> no adapter (never a relay)
os.environ["SHOAL_FAKE_SESSIONS"] = json.dumps({"claude": [{"id": "aaaa1111", "sessionId": "aaaa1111-0000"}], "opencode": {}})
assert lw.claude_deliver(spec.sid, spec, r, "aaaa1111-0000", "LEDGER-WAKE tl: test") == "resume (fake)"
assert lw.claude_deliver(spec.sid, None, None, "aaaa1111-0000", "x").startswith("no-adapter")
import briefs
w, how = briefs.arming(r, spec)
assert w == "direct" and "setsid" in how and "NOT as a background command" in how, (w, how)
PY2
# a watcher armed before the retirement (`--wake relay`, or re-executed in place with that argv) runs as
# direct, and arming it again as direct finds it alive: no duplicate watcher, no re-arm wake
SC28="$S/sc-direct.state"; rm -f "$SC28"
setsid $H ledger-watch --shoal $SID --spec "$SPEC" --slug tl --wake relay --session aaaa1111-0000 --interval 600 \
  --sidecar "$SC28" --no-sync --no-board >"$S/w28.log" 2>&1 &
P28=$!
for i in $(seq 1 30); do grep -q "^pid=$P28$" "$SC28" 2>/dev/null && break; sleep 0.5; done
grep -q "^wake=direct$" "$SC28" || fail "a --wake relay watcher did not record wake=direct :: $(cat $SC28 2>&1)"
check "ALREADY ARMED: ledger-watch pid $P28 for tl (direct)" $H ledger-watch --shoal $SID --spec "$SPEC" --slug tl \
  --wake direct --session aaaa1111-0000 --interval 600 --sidecar "$SC28" --no-sync --no-board
kill $P28 2>/dev/null; wait $P28 2>/dev/null || true

echo "== 29 a dead Claude worker behind a live detached watcher is dormant, not stalled (FM-16, retired by direct wake)"
python3 - "$SHOALD" "$SPEC" <<'PY2' || fail "dead worker behind a live watcher"
import json, os, sys; sys.path.insert(0, sys.argv[1])
os.environ["SHOAL_FAKE_SESSIONS"] = json.dumps({"claude": [{"id": "aaaa1111", "sessionId": "aaaa1111-0000", "state": "done"}], "opencode": {}})
import health
from roles import ShoalSpec
from ledger import Ledger, View
spec = ShoalSpec.load(sys.argv[2])
view = View(Ledger(spec.sid, root=spec.ledger_dir, sync=False), spec)
health.watcher_alive = lambda sid, slug: (True, {"pid": "1"})
cbusy = {}  # the plain listing omits a session whose worker is gone
conds = [c for c in health.evaluate(spec, view, cbusy) if c["slug"] == "tl" and c["condition"] == "watcher-dead"]
assert not conds, conds  # the watcher resumes the session when work arrives: waking it only to re-arm costs a turn
os.environ["SHOAL_FAKE_SESSIONS"] = json.dumps({"claude": [{"id": "aaaa1111", "sessionId": "aaaa1111-0000", "state": "done", "pid": 9}], "opencode": {}})
assert not [c for c in health.evaluate(spec, view, {"aaaa1111-0000": "idle"}) if c["slug"] == "tl" and c["condition"] == "watcher-dead"]
PY2

echo "== 30 a user offer reopens a done item for a new round (the key drops the verb)"
$H offer --shoal $SID --spec "$SPEC" --to tl --item interpretune#9990:fix --no-sync >/dev/null
check "claimed interpretune#9990" $H claim --shoal $SID --spec "$SPEC" --slug tl --item interpretune#9990:fix --no-sync
sleep 1.1
check "done" $H done --shoal $SID --spec "$SPEC" --slug tl --item interpretune#9990:fix --no-sync
# ledger timestamps have one-second resolution and same-second events order by writer, so space the
# steps as real lanes are spaced
sleep 1.1
check "reopens interpretune#9990: done" $H offer --shoal $SID --spec "$SPEC" --to up --item interpretune#9990:review --no-sync
sleep 1.1
check "claimed interpretune#9990" $H claim --shoal $SID --spec "$SPEC" --slug up --item interpretune#9990:review --no-sync
python3 - "$SHOALD" "$SPEC" <<'PY2' || fail "reopened item"
import sys; sys.path.insert(0, sys.argv[1])
from roles import ShoalSpec
from ledger import Ledger, View
spec = ShoalSpec.load(sys.argv[2])
v = View(Ledger(spec.sid, root=spec.ledger_dir, sync=False), spec)
assert "interpretune#9990" not in v.done and v.reopened["interpretune#9990"][0]["by"] == "tl", v.reopened
assert not [x for x in v.violations if "9990" in str(x)], v.violations
PY2

echo "== 31 a Claude turn that ended while its own subagents run is still working; a background shell is not"
python3 - "$SHOALD" <<'PY2' || fail "subagents in flight"
import sys; sys.path.insert(0, sys.argv[1])
import health
row = lambda kinds, n: {"tempo": "idle", "inFlight": {"tasks": n, "kinds": kinds}}
assert health.claude_liveness(row(["local_agent"], 3)) == "busy"
assert health.claude_liveness(row(["local_bash"], 1)) == "idle"  # a background command never holds a turn open
assert health.claude_liveness(row(["local_agent"], 0)) == "idle"
assert health.claude_liveness({"tempo": "blocked", "inFlight": {"tasks": 2, "kinds": ["local_agent"]}}) == "blocked"
print("subagents in flight ok")
PY2

echo "== 32 usage limit: Claude wakes held until the reset, then one resume wake"
# the measured shape (2026-10-14): tl's turn errored on the limit (resetsAt), up stopped on the grace note
CC="$S/claude-cfg"; mkdir -p "$CC/projects/p"
python3 - "$CC" <<'PY2'
import datetime, json, sys, time
ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
p = sys.argv[1] + "/projects/p/"
open(p + "aaaa1111-0000.jsonl", "w").write(json.dumps({"type": "assistant", "timestamp": ts, "error": "rate_limit",
    "quotaLimits": {"resetsAt": int(time.time()) + 30 * 60}, "message": {"content": [{"type": "text", "text": "limit"}]}}) + "\n")
open(p + "bbbb2222-0000.jsonl", "w").write(json.dumps({"type": "user", "timestamp": ts,
    "message": {"role": "user", "content": "[Usage limit reached; a short grace allowance remains"}}) + "\n")
PY2
env CLAUDE_CONFIG_DIR="$CC" python3 - "$SHOALD" "$SPEC" <<'PY2' || fail "usage-limit marks"
import os, sys; sys.path.insert(0, sys.argv[1])
import health
from roles import ShoalSpec
spec = ShoalSpec.load(sys.argv[2]); st = {}
lim = health.update_limit(spec, st)
assert set(lim["affected"]) == {"tl", "up"} and health.limit_until(lim) == lim["reset"], lim
os.environ["SHOAL_HEALTH_SHIFT_MIN"] = "40"
assert health.limit_until(lim) == "", "the limit must end at its reset"
print("usage-limit marks ok")
PY2
FS_IDLE='{"claude":[{"id":"aaaa1111","sessionId":"aaaa1111-0000","status":"idle","state":"done","pid":4242},{"id":"bbbb2222","sessionId":"bbbb2222-0000","status":"idle","state":"done","pid":4243}],"opencode":{}}'
HV="$H health --spec $SPEC --instance a --dry-run"
rm -f /tmp/$SID/health.json /tmp/$SID/ledger-watch-tl.state /tmp/$SID/wake-tl
SEED='import datetime, json, sys; now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
json.dump({"ladders": {"tl|watcher-dead|pid:none": {"since": now, "step": 0}}}, open(sys.argv[1], "w"))'
python3 -c "$SEED" /tmp/$SID/health.json
OUT=$(env CLAUDE_CONFIG_DIR="$CC" SHOAL_FAKE_SESSIONS="$FS_IDLE" SHOAL_HEALTH_SHIFT_MIN=15 $HV 2>&1)
grep -q "tl watcher-dead step1:wake -> held (usage limit until" <<<"$OUT" || fail "a Claude wake inside the limit was not held :: $OUT"
! grep -q "usage-limit reset" <<<"$OUT" || fail "resume wake before the reset :: $OUT"
python3 -c "$SEED" /tmp/$SID/health.json
OUT=$(env CLAUDE_CONFIG_DIR="$CC" SHOAL_FAKE_SESSIONS="$FS_IDLE" SHOAL_HEALTH_SHIFT_MIN=60 $HV 2>&1)
# after the reset the step runs: a re-arm by the quorum (detached watcher, live worker) where it used to wake
grep -Eq "tl watcher-dead step1:(wake|rearm) -> " <<<"$OUT" && ! grep -q "held (usage" <<<"$OUT" || fail "the held wake did not run after the reset :: $OUT"
grep -q "tl usage-limit reset:wake" <<<"$OUT" && grep -q "up usage-limit reset:wake" <<<"$OUT" \
  || fail "no resume wake for each role the limit touched :: $OUT"
rm -f /tmp/$SID/health.json

echo "== 33 muse (exec per wake): the brief and arming, the between-turns read, the restart plan, a watcher that starts a turn"
cat > "$S/muse-stub" <<'PY2'
#!/usr/bin/env python3
import json, os, sys, time
a = sys.argv[1:]
prompt = open(a[a.index("--prompt-file") + 1]).read()
sid = a[a.index("--session-id") + 1]
time.sleep(float(os.environ.get("FAKE_MUSE_SLEEP", "0")))
print(json.dumps({"schema_version": 1, "stream": {"kind": "session", "id": sid},
                  "payload": {"kind": "run_terminal", "terminal": "completed", "text": prompt}}), flush=True)
PY2
chmod +x "$S/muse-stub"
export SHOAL_MUSE_BIN="$S/muse-stub" SHOAL_MUSE_STATE="$S/muse-state"
MU=$(python3 -c "
import sys; sys.path.insert(0, '$SHOALD'); import muse; print(muse.uuid7())")
python3 - "$SHOALD" "$SPEC" "$MU" <<'PY2' || fail "muse brief, arming and between-turns"
import fcntl, os, sys
sys.path.insert(0, sys.argv[1])
import briefs, health, muse
from roles import ShoalSpec
spec = ShoalSpec.load(sys.argv[2]); r = spec.role("up"); r.ecosystem = "muse"; r.session = sys.argv[3]
w, how = briefs.arming(r, spec)
assert w.startswith("exec:") and "run by shoal itself" in how, (w, how)
b = briefs.brief_for(r, spec)
assert "HEADLESS TURNS (Muse)" in b and "never ask one" in b and "queue --ask" in b, "muse brief lacks the headless clause"
assert "run it once with `--test` first" not in b, "a muse role must not be told to arm a watcher"
# between turns: free lock is between turns, a held one is mid-turn
assert health.session_between_turns(r, {}, "/x") is True
d = muse.sdir(r.session, create=True)
fh = open(os.path.join(d, "lock"), "a+"); fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
assert health.session_between_turns(r, {}, "/x") is False
fh.close()
print("muse brief and state ok")
PY2
python3 - "$SPEC" <<'PY2'
import json, sys; p = sys.argv[1]; d = json.load(open(p))
[r.update(ecosystem="muse") for r in d["roles"] if r["slug"] == "up"]; json.dump(d, open(p, "w"), indent=1)
PY2
check "would start the first muse exec turn" $H restart --spec "$SPEC" --only up --dry-run
# a persistent exec: watcher starts a turn on a new offer, keeps running, and is a no-op to arm twice
$H offer --shoal $SID --spec "$SPEC" --to up --item interpretune#9331:fix --no-sync >/dev/null
$H ledger-watch --shoal $SID --spec "$SPEC" --slug up --wake exec:$MU --session $MU --interval 1 --no-sync \
  --sidecar "$S/sc-mu.state" > "$S/mu-watch.log" 2>&1 &
P_MU=$!
for _ in $(seq 1 60); do [ -s "$S/muse-state/$MU/last.json" ] && grep -q '"ended"' "$S/muse-state/$MU/last.json" && break; sleep 0.5; done
grep -q "LEDGER-WAKE up@$SID" "$S/muse-state/$MU/last.json" || fail "the watcher did not start a turn with the wake text :: $(cat "$S/mu-watch.log" | tail -5) :: $(cat "$S/muse-state/$MU/last.json" 2>&1 | head -c 400)"
kill -0 $P_MU 2>/dev/null || fail "an exec: watcher must keep running across wakes (persistent) :: $(tail -5 "$S/mu-watch.log")"
check "ALREADY ARMED: ledger-watch pid $P_MU" $H ledger-watch --shoal $SID --spec "$SPEC" --slug up --wake exec:$MU --session $MU \
  --interval 1 --sidecar "$S/sc-mu.state" --no-sync
kill $P_MU 2>/dev/null; wait $P_MU 2>/dev/null || true
python3 - "$SPEC" <<'PY2'
import json, sys; p = sys.argv[1]; d = json.load(open(p))
[r.update(ecosystem="claude") for r in d["roles"] if r["slug"] == "up"]; json.dump(d, open(p, "w"), indent=1)
PY2
unset SHOAL_MUSE_BIN SHOAL_MUSE_STATE

echo "== 34 wake economy: dormant watchers, offline hold, return settle, wake gap, one wake per role, rewake cooldown, wake_off, direct delivery"
rm -f /tmp/$SID/health.json /tmp/$SID/wake-tl /tmp/$SID/wake-up
python3 - "$SHOALD" "$SPEC" <<'PY2' || fail "wake economy"
import datetime, io, json, os, sys, contextlib
sys.path.insert(0, sys.argv[1])
import health, ledger, backends, briefs
from roles import ShoalSpec
SPEC = sys.argv[2]
def spec_with(**h):
    d = json.load(open(SPEC)); d.setdefault("features", {})["health"] = h
    json.dump(d, open(SPEC, "w"), indent=1); return ShoalSpec.load(SPEC)
def ago(m):
    return (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=m)).strftime("%Y-%m-%dT%H:%M:%SZ")
MODES = {}
_init = ledger.View.__init__
def init(self, *a, **k):
    _init(self, *a, **k); self.modes.update(MODES)
ledger.View.__init__ = init
COND = []
health.evaluate = lambda spec, view, cbusy: [dict(c) for c in COND]
health.host_beats = lambda *a, **k: []
health.claude_busy = lambda: {}
def run(spec, state=None):
    p = os.path.join(health.state_dir(spec.sid), "health.json")
    if state is not None:
        json.dump(state, open(p, "w"))
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        health.run(spec, "a", dry_run=False)
    return out.getvalue(), json.load(open(p))
def pw(items, since=200):
    return {"slug": "tl", "condition": "parked-work", "key": "parked:" + items, "since": ago(since), "detail": "parked " + items}
os.environ["SHOAL_FAKE_SESSIONS"] = json.dumps({"claude": [{"id": "aaaa1111", "sessionId": "aaaa1111-0000", "pid": 7, "tempo": "idle"}], "opencode": {}})
spec = spec_with()
# offline: parked-work wakes are held (not advanced) while the maintainer is away
MODES["user-offline"] = {"mode": "user-offline", "state": "on", "ts": ago(300)}
COND[:] = [pw("x#1")]
out, st = run(spec, {"ladders": {"tl|parked-work|parked:x#1": {"since": ago(200), "step": 0}}})
assert "held (maintainer offline)" in out and st["ladders"]["tl|parked-work|parked:x#1"]["step"] == 0, out
# back online: quiet for online_settle_min, then the held wake runs once
MODES["user-offline"] = {"mode": "user-offline", "state": "off", "ts": ago(5)}
out, st = run(spec)
assert "held (settling" in out, out
MODES["user-offline"] = {"mode": "user-offline", "state": "off", "ts": ago(60)}
out, st = run(spec)
assert "tl parked-work step1:wake -> " in out and "held" not in out, out
# the same parked set re-opening (its blocker went stale) is not woken again inside rewake_min
st["ladders"] = {"tl|parked-work|parked:x#1": {"since": ago(200), "step": 0}}; st["last_wake"] = {}
out, st = run(spec, st)
assert "skipped (woken for the same at" in out, out
# a different condition for the same role inside wake_gap_min is held; outside it, it runs
COND[:] = [{"slug": "tl", "condition": "wait-expired", "key": "rescan:z", "since": ago(1), "detail": "expired"}]
out, st = run(spec, dict(st, last_wake={"tl": ago(5)}))
assert "held (woken 5 min ago" in out, out
out, st = run(spec, dict(st, last_wake={"tl": ago(45)}))
assert "tl wait-expired step1:wake -> " in out, out
# two conditions due in one run: one wake, the second merged into it
COND[:] = [pw("y#2"), {"slug": "tl", "condition": "wait-expired", "key": "rescan:w", "since": ago(1), "detail": "expired"}]
out, st = run(spec, {"ladders": {"tl|parked-work|parked:y#2": {"since": ago(200), "step": 0}}})
assert "merged into the parked-work wake" in out and out.count("wake-file") + out.count("resume") <= 1, out
# wake_off: parked-work only pages
spec = spec_with(wake_off=["parked-work"])
COND[:] = [pw("v#3")]
out, st = run(spec, {"ladders": {"tl|parked-work|parked:v#3": {"since": ago(200), "step": 0}}})
assert "tl parked-work step1:page" in out and ":wake" not in out, out
print("wake economy ladders ok")
# dormant: a dead watcher on an idle role with nothing actionable raises nothing (lazy_rearm)
import importlib; importlib.reload(health)
spec = spec_with()
v = ledger.View(ledger.Ledger(spec.sid, root=spec.ledger_dir, sync=False), spec)
r = spec.role("up")
health.watcher_dormant  # exists
assert health.watcher_dormant(spec, v, r, "active") is False, "an active role is never dormant"
# direct delivery (fake sessions): no worker -> resume; busy -> deferred; idle (the default) -> stop+resume
be = backends.get("claude")
def rows(**kw):
    os.environ["SHOAL_FAKE_SESSIONS"] = json.dumps({"claude": [dict({"id": "bbbb2222", "sessionId": "bbbb2222-0000"}, **kw)], "opencode": {}})
rows(state="done")
assert be.deliver(spec, r, "bbbb2222-0000", "w").startswith("resume"), be.deliver(spec, r, "bbbb2222-0000", "w")
rows(pid=9, tempo="active")
assert be.deliver(spec, r, "bbbb2222-0000", "w") == "deferred (mid-turn)", be.deliver(spec, r, "bbbb2222-0000", "w")
rows(pid=9, tempo="idle")
assert be.deliver(spec, r, "bbbb2222-0000", "w").startswith("stop+resume"), be.deliver(spec, r, "bbbb2222-0000", "w")
rows(pid=9, tempo="idle", inFlight={"tasks": 1, "queued": 0, "kinds": ["local_bash"]})
assert be.deliver(spec, r, "bbbb2222-0000", "w") == "deferred (background work in flight)", be.deliver(spec, r, "bbbb2222-0000", "w")
# arming: direct idle (the default) makes a Claude watcher detached; dead (never stop a live worker) does not
assert briefs.arming(r, spec)[0] == "direct"
spec = spec_with(direct_wake="dead")
assert briefs.arming(r, spec)[0] == "exit"
rows(pid=9, tempo="idle")
assert be.deliver(spec, r, "bbbb2222-0000", "w") == "deferred (live worker (direct_wake dead))"
spec_with()
print("direct delivery ok")
PY2
rm -f /tmp/$SID/health.json
# a user --wake message is skipped only once the role's mail read cursor covers it (`next` marks mail read):
# a turn-end queue write without `next` must not swallow a wake held mid-turn (measured 2026-10-26: two held
# wakes lost, never paged), while a read wake is not delivered again by a watcher armed after it (2026-10-20)
wake_hit() {
python3 - "$SHOALD" "$SPEC" up "$1" <<'PY2'
import sys; sys.path.insert(0, sys.argv[1])
import ledger, ledger_watch
from roles import ShoalSpec
spec = ShoalSpec.load(sys.argv[2]); v = ledger.View(ledger.Ledger(spec.sid, root=spec.ledger_dir, sync=False), spec)
hits = [d for _, d in ledger_watch.actionable(v, sys.argv[3], spec.role(sys.argv[3]), spec, board=False) if "wake test" in d]
assert bool(hits) == (sys.argv[4] == "yes"), hits
PY2
}
$H next --shoal $SID --spec "$SPEC" --slug up --no-sync >/dev/null 2>&1 || fail "up next"
sleep 1
$H msg --shoal $SID --spec "$SPEC" --to up --wake --text "wake test" --no-sync >/dev/null 2>&1 || fail "msg --wake"
wake_hit yes || fail "a fresh --wake msg must be actionable"
# the watcher holds it while the session is busy (nothing marks it delivered), then the turn ends with a queue
# write and no `next`: the next idle tick must still deliver it
sleep 1
$H queue --shoal $SID --spec "$SPEC" --slug up --idle --next "" --reason test --no-sync >/dev/null 2>&1 || fail "up idle"
wake_hit yes || fail "a held --wake msg was swallowed by a turn-end queue write without next"
# no cursor (never read, or /tmp wiped by a reboot): unread, so delivered rather than skipped
rm -f /tmp/$SID/mail-read-up
wake_hit yes || fail "a --wake msg was skipped with no mail read cursor"
OUT=$($H next --shoal $SID --spec "$SPEC" --slug up --no-sync 2>&1)
grep -q "wake test" <<<"$OUT" || fail "next did not show the --wake msg :: $(head -c 600 <<<"$OUT")"
wake_hit no || fail "a --wake msg read by next was delivered again"

echo "== 35 bounded context: the CONTEXT advisory in next and wakes, --restart-ready, the context-restart ladder"
Q="$H queue --shoal $SID --spec $SPEC --no-sync"
check "posture=idle" $Q --slug tl --idle --next "" --reason test
OUT=$(env SHOAL_FAKE_CONTEXT='{"tl": 412000}' $H next --shoal $SID --spec "$SPEC" --slug tl --no-sync --peek 2>&1)
grep -q "CONTEXT 412k (limit 300k)" <<<"$OUT" || fail "no CONTEXT advisory past the limit :: $(head -c 800 <<<"$OUT")"
OUT=$(env SHOAL_FAKE_CONTEXT='{"tl": 120000}' $H next --shoal $SID --spec "$SPEC" --slug tl --no-sync --peek 2>&1)
! grep -q "CONTEXT " <<<"$OUT" || fail "CONTEXT advisory under the limit"
python3 - "$SHOALD" "$SPEC" <<'PY2' || fail "context limits and wake text"
import json, os, sys
sys.path.insert(0, sys.argv[1])
import context, health
from roles import ShoalSpec
d = json.load(open(sys.argv[2])); d["features"].setdefault("health", {})["context_restart_k"] = {"*": 300, "tl": 0, "up": 800}
json.dump(d, open(sys.argv[2], "w"), indent=1)
spec = ShoalSpec.load(sys.argv[2])
assert context.limit_k(spec, "tl") == 0 and context.limit_k(spec, "up") == 800 and context.limit_k(spec, "zz") == 300
os.environ["SHOAL_FAKE_CONTEXT"] = json.dumps({"tl": 900000, "up": 500000})
assert context.over(spec, spec.role("tl")) is None, "a role with limit 0 is never advised"
assert context.over(spec, spec.role("up")) is None, "under its own 800k limit"
d["features"]["health"].pop("context_restart_k"); json.dump(d, open(sys.argv[2], "w"), indent=1)
spec = ShoalSpec.load(sys.argv[2])
assert "CONTEXT 900k" in health.wake_text(spec, "tl", "x"), "wakes carry the advisory"
print("context limits ok")
PY2
check "never with an active item" $Q --slug tl --active interpretune#9601:fix --restart-ready
# the proof of refresh (docs/start-doc.md): no `Last updated` stamp, or one older than the advisory, is refused
TLDOC=$(python3 -c "import json,sys;print([r for r in json.load(open(sys.argv[1]))['roles'] if r['slug']=='tl'][0]['doc'])" "$SPEC")
cp "$TLDOC" "$S/tl-doc.bak"
check "needs a refreshed start doc .*no \`Last updated" $Q --slug tl --idle --restart-ready
env SHOAL_FAKE_CONTEXT='{"tl": 412000}' $H next --shoal $SID --spec "$SPEC" --slug tl --no-sync >/dev/null 2>&1   # advised now
printf '\nLast updated: 2020-01-01T00:00Z\n' >> "$TLDOC"
check "is older than" $Q --slug tl --idle --restart-ready
printf '\nLast updated: %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" >> "$TLDOC"
check "posture=idle" $Q --slug tl --idle --restart-ready
# handover telemetry: advised (the non-peek next above), doc-refreshed (this restart-ready), then the quorum's restart
# and the new session's first turn, once each
python3 - "$SHOALD" "$SPEC" "$S/data/$SID/telemetry" <<'PY2' || fail "handover telemetry"
import glob, json, sys
sys.path.insert(0, sys.argv[1])
import context
from roles import ShoalSpec
def evs():
    return [json.loads(ln) for f in sorted(glob.glob(sys.argv[3] + "/events-*.jsonl")) for ln in open(f)
            if '"handover"' in ln]
ph = [e["phase"] for e in evs() if e.get("slug") == "tl"]
assert ph == ["advised", "doc-refreshed"], ph
adv = evs()[0]
assert adv["context_k"] == "412" and adv["limit_k"] == "300", adv
spec = ShoalSpec.load(sys.argv[2]); r = spec.role("tl")
context.note_restart(spec, r, "restarting on a fresh session (log x)")
context.note_first_turn(spec, r)   # the restarted session itself is no first turn
r.session = "tlnew0001"
context.note_first_turn(spec, r); context.note_first_turn(spec, r)   # once only
got = [e for e in evs() if e.get("slug") == "tl" and e["phase"] in ("restart", "first-turn")]
assert [e["phase"] for e in got] == ["restart", "first-turn"], got
assert got[0]["outcome"] == "ok" and got[1]["new_session"] == "tlnew0001", got
assert "since_restart_s" in got[1] and got[1].get("old_session", "") == got[0].get("old_session", ""), got
print("handover telemetry ok")
PY2
check "ok  .*tl_plan_state.md" $H doc-check "$TLDOC"
printf '# x\n\n## Mission\n\nm\n\n## Current state\n\nLast updated: 2026-10-22T10:00Z\n\n## Next\n\n- n\n' > "$S/nogrants.md"
check "no grants-needed section" $H doc-check "$S/nogrants.md"
[ "$(rc_of $H doc-check "$S/nogrants.md")" = 1 ] || fail "doc-check passed a doc without grants-needed"
check "is older than" $H doc-check "$S/nogrants.md" --since 2026-10-22T11:00Z
rm -f /tmp/$SID/health.json
check "tl context-restart step1:page" env SHOAL_FAKE_CONTEXT='{"tl": 412000}' SHOAL_HEALTH_BETWEEN=tl $H health --spec "$SPEC" --instance a --dry-run
OUT=$(env SHOAL_FAKE_CONTEXT='{"tl": 100000}' SHOAL_HEALTH_BETWEEN=tl $H health --spec "$SPEC" --instance a --dry-run 2>&1)
! grep -q "context-restart" <<<"$OUT" || fail "context-restart under the limit :: $OUT"
python3 - "$SPEC" <<'PY2'
import json,sys; p=sys.argv[1]; d=json.load(open(p)); d["features"].setdefault("health", {})["context_restart"]={"*": "page", "tl": "auto"}; json.dump(d,open(p,"w"),indent=1)
PY2
rm -f /tmp/$SID/health.json
check "tl context-restart step1:restart -> would restart (dry-run): .*--only tl" env SHOAL_FAKE_CONTEXT='{"tl": 412000}' SHOAL_HEALTH_BETWEEN=tl \
  $H health --spec "$SPEC" --instance a --dry-run
python3 - "$SPEC" <<'PY2'
import json,sys; p=sys.argv[1]; d=json.load(open(p)); d["features"]["health"].pop("context_restart",None); json.dump(d,open(p,"w"),indent=1)
PY2
# a later write without the flag clears it
check "posture=idle" $Q --slug tl --idle
rm -f /tmp/$SID/health.json
OUT=$(env SHOAL_FAKE_CONTEXT='{"tl": 412000}' SHOAL_HEALTH_BETWEEN=tl $H health --spec "$SPEC" --instance a --dry-run 2>&1)
! grep -q "context-restart" <<<"$OUT" || fail "restart-ready survived a later queue write :: $OUT"

echo "== 36 park and unpark: no session and no watcher, the queue kept; a full restart keeps it parked; --until unparks"
FS0='{"claude": [], "opencode": {}}'
check "posture=active" $Q --slug up --active interpretune#9701:fix --next interpretune#9702
check "REFUSED: up holds active item" env SHOAL_FAKE_SESSIONS="$FS0" $H park --spec "$SPEC" --only up
[ "$(rc_of env SHOAL_FAKE_SESSIONS="$FS0" $H park --spec "$SPEC" --only up)" = 1 ] || fail "park took a role mid-item"
check "posture=idle" $Q --slug up --idle --next interpretune#9702 --blocked interpretune#9702=hold:research
mkdir -p "$S/fakewatch"; printf 'import time\ntime.sleep(300)\n' > "$S/fakewatch/cli.py"
# a stand-in ledger-watch for up, bound to its session: the same argv shape, so the /proc scan finds it
setsid python3 "$S/fakewatch/cli.py" ledger-watch --shoal $SID --slug up --session bbbb2222 &
LWP=$!
check "would stop bbbb2222" env SHOAL_FAKE_SESSIONS="$FS0" $H park --spec "$SPEC" --only up --dry-run
check "parked up" env SHOAL_FAKE_SESSIONS="$FS0" $H park --spec "$SPEC" --only up --note "research hold" \
  --until "gh-pr:speediedan/interpretune#9703"
sleep 0.5; ! kill -0 $LWP 2>/dev/null || fail "park left up's ledger-watch running"
python3 - "$SPEC" "$SHOALD" <<'PY2' || fail "park record"
import json, sys
sys.path.insert(0, sys.argv[2])
from ledger import Ledger
from roles import ShoalSpec
r = [x for x in json.load(open(sys.argv[1]))["roles"] if x["slug"] == "up"][0]
assert r["session"] == "" and r["parked"]["note"] == "research hold" and r["parked"]["session"] == "bbbb2222", r
assert r["parked"]["until"] == "gh-pr:speediedan/interpretune#9703", r
spec = ShoalSpec.load(sys.argv[1])
evs = Ledger(spec.sid, root=spec.ledger_dir, sync=False).events(reload=True)
assert any(e.get("event") == "role-parked" and e.get("role") == "up" for e in evs), "no role-parked event"
print("park record ok")
PY2
check "parked (no session) since .*: research hold; until gh-pr:speediedan/interpretune#9703" \
  $H show --shoal $SID --spec "$SPEC" --no-sync
check "QUEUE .*interpretune#9702" $H next --shoal $SID --spec "$SPEC" --slug up --no-sync --peek
check "parked role(s) up stay parked" env SHOAL_FAKE_SESSIONS="$FS0" $H restart --spec "$SPEC" --dry-run --stages restart
check "unpark: .*restart --spec .* --only up" env SHOAL_FAKE_SESSIONS="$FS0" $H unpark --spec "$SPEC" --only up --dry-run
[ "$(rc_of $H unpark --spec "$SPEC" --only tl --dry-run)" = 1 ] || fail "unpark accepted a role with a session"
python3 - "$SHOALD" "$SPEC" <<'PY2' || fail "auto unpark"
import sys; sys.path.insert(0, sys.argv[1])
import park, providers
from roles import ShoalSpec
spec = ShoalSpec.load(sys.argv[2])
real = providers.resolve
providers.resolve = lambda t, slug="", sid="", upstream=False: (True, "PR open")
assert park.due(spec) == [], "unparked while the target is pending"
providers.resolve = lambda t, slug="", sid="", upstream=False: (None, "gh unavailable")
assert park.due(spec) == [], "unknown must never unpark"
providers.resolve = lambda t, slug="", sid="", upstream=False: (False, "PR merged")
state, acts = {}, []
park.auto_unpark(spec, sys.argv[2], state, acts, dry_run=True)
assert acts and acts[0][0] == "up" and "would restart" in acts[0][2], acts
providers.resolve = real
print("auto unpark ok")
PY2
python3 - "$SPEC" <<'PY2'
import json,sys; p=sys.argv[1]; d=json.load(open(p))
for r in d["roles"]:
    if r["slug"] == "up":
        r["session"], r["parked"] = "bbbb2222", {}
json.dump(d,open(p,"w"),indent=1)
PY2

python3 - "$SHOALD" "$SPEC" <<'PY2' || fail "operational request acknowledgment"
import sys
from unittest.mock import patch
sys.path.insert(0, sys.argv[1])
import health
from ledger import Ledger, View
from roles import ShoalSpec
spec = ShoalSpec.load(sys.argv[2])
role = spec.roles[0]
role.session = role.session or "synthetic-session"
role.server = ""
view = View(Ledger(spec.sid, root=spec.ledger_dir), spec)
request = {"id":"request-fixture", "type":"msg", "by":"source", "to":role.slug,
           "kind":"request", "ack":True, "text":"open a resource window", "ts":"2000-01-01T00:00:00Z"}
view.msgs.append(request)
with patch.object(health, "session_between_turns", return_value=True):
    assert any(c["key"] == "request:request-fixture" for c in health.evaluate(spec, view, {})), "CI posture never hides a request"
    view.msgs.append({"id":"forged", "by":"other", "to":"source", "re":request["id"]})
    assert view.unacked_to(role.slug) == [request], "a third party cannot acknowledge a unicast request"
    view.msgs.append({"id":"answer", "by":role.slug, "to":"source", "re":request["id"], "kind":"ack"})
    assert not view.unacked_to(role.slug) and not view.unacked("source")
    assert not any(c["key"] == "request:request-fixture" for c in health.evaluate(spec, view, {}))
# The writer makes requests acknowledgment-required and refuses unavailable recipients.
import ledger_cli, types, contextlib, io
recorded = []
class FakeLedger:
    def append(self, by, event_type, **fields):
        recorded.append(fields)
        return dict(fields, id="new-request", by=by)
a = types.SimpleNamespace(to=role.slug, text="resource window decision", kind="request", re="", ack=False,
                          wake=False, dry_run=False)
with patch.object(ledger_cli, "_spec", return_value=spec), \
     patch.object(ledger_cli, "_ledger", return_value=FakeLedger()), \
     patch.object(ledger_cli, "_writer", return_value="user"), contextlib.redirect_stdout(io.StringIO()):
    assert ledger_cli.cmd_msg(a) == 0 and recorded[-1]["ack"] is True
    saved = role.session
    role.session = ""
    assert ledger_cli.cmd_msg(a) == 1 and len(recorded) == 1
    role.session = saved
# A stale matching wait releases only the local monitor, never its productive unit.
import jobs
unit = jobs.find_unit("h4selftest", "owner", "suite")
args = types.SimpleNamespace(sid="h4selftest", slug="owner", name="suite", unit="", timeout_min=0, poll=1, tail=0)
proof = {"sub":"local-ci", "target":"job:suite", "rescan_utc":"2000-01-01T00:00:00Z"}
with patch("state.wait_status", return_value=("stale",proof)), \
     patch.object(jobs, "resolve", return_value=(unit, {})), \
     patch.object(jobs, "_meta_for", return_value={}), \
     patch.object(jobs, "unit_state", return_value={"ActiveState":"active"}), \
     patch.object(jobs, "_emit"), patch.object(jobs, "_systemctl") as control, \
     contextlib.redirect_stdout(io.StringIO()) as output:
    assert jobs.cmd_wait(args) == 75
    assert "RESCAN-DUE" in output.getvalue() and "not stopped" in output.getvalue()
    control.assert_not_called()
    with patch.object(jobs, "remote_state", return_value={"ActiveState":"active"}):
        assert jobs._wait_remote(args, unit, {"host":"other-host.invalid", "log":"example.log"}) == 75
    with patch("state.wait_status", return_value=("covered",proof)):
        assert not jobs.yield_for_rescan(args, unit)
    with patch("state.wait_status", return_value=("stale",dict(proof,target="job:another"))):
        assert not jobs.yield_for_rescan(args, unit)
print("operational request acknowledgment ok")
PY2

echo "== 37 prune-incarnations: every guard keeps its session, tombstone before claude rm, re-stop, per-harness archive, N-2 rule"
python3 "$TESTD/test_prune.py" || fail "prune-incarnations (tests/test_prune.py)"
check "name what to prune" $H prune-incarnations --dry-run
check "unknown harness" $H prune-incarnations --spec "$SPEC" --harness nope --dry-run
OUT=$(env XDG_CONFIG_HOME="$S/xdg-prune" CLAUDE_CONFIG_DIR="$S/cc-prune" $H prune-incarnations --spec "$SPEC" \
      --harness claude --dry-run 2>&1) || fail "prune-incarnations dry run :: $OUT"
grep -q "== claude: 0 listed, 0 shoal-created" <<<"$OUT" && grep -q "\[dry-run\] total: prune 0" <<<"$OUT" \
  || fail "prune-incarnations dry-run table :: $OUT"

echo "SELFTEST H4 OK ($S)"
rm -rf /tmp/$SID
