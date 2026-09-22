#!/usr/bin/env bash
# Isolated proof of the H4 components (architecture doc §4-§6): working context, board
# digest, batch triage, digest-driven ledger-watch wake, exit verdicts, deadman, job units.
# Touches nothing live: a bare local remote, HAG_ROOT/HAG_DATA_ROOT under a scratch dir,
# hag id h4selftest (state in /tmp/h4selftest). Reads the real GitHub board (read-only).
#   selftest_h4.sh [SCRATCH_DIR]
set -eu
HAGD="$(cd "$(dirname "$0")" && pwd)"
S="${1:-${TMPDIR:-/tmp}/hag-h4-selftest-$$}"
rm -rf "$S" /tmp/h4selftest; mkdir -p "$S/root" "$S/data" "$S/remote"
export HAG_ROOT="$S/root" HAG_DATA_ROOT="$S/data"
HAGID=h4selftest; SPEC="$HAG_ROOT/$HAGID/hag.json"; A="$S/cloneA"
H="python3 $HAGD/hag.py"
fail() { echo "SELFTEST FAIL: $*" >&2; exit 1; }
check() { local pat=$1; shift; local out; out=$("$@" 2>&1 || true); grep -q -- "$pat" <<<"$out" || fail "expected '$pat' in: $* :: ${out: -600}"; }
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

echo "== 6 deadman: by-design exit, alive, killed externally, never armed"
check "exited by design" $H watch-check --hag $HAGID --slug tl
[ "$(rc_of $H watch-check --hag $HAGID --slug tl)" = 3 ] || fail "dead watch not rc 3"
check "never armed" $H watch-check --hag $HAGID --slug up
setsid $H ledger-watch --hag $HAGID --spec "$SPEC" --slug up --wake print --interval 30 --no-sync --no-board >/dev/null 2>&1 &
WP=$!; sleep 3
check "DEADMAN OK up@$HAGID: ledger-watch pid" $H watch-check --hag $HAGID --slug up
kill -9 $WP; sleep 1
check "killed externally" $H watch-check --hag $HAGID --slug up
[ -f /tmp/$HAGID/deadman-up ] || fail "deadman tick not written"
check "cron: 13 \* \* \* \*" $H watch-check --hag $HAGID --slug up --print-schedule

echo "== 7 job units: success, failure exit code, external kill, idempotent waiter"
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

echo "SELFTEST H4 OK ($S)"
rm -rf /tmp/$HAGID
