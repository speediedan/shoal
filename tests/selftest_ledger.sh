#!/usr/bin/env bash
# selftest_ledger.sh — isolated end-to-end proof of the ledger primitive
# (readme 5.j). Builds a local bare "remote" + two clones (two hosts), a
# 3-role spec under a scratch spec dir and HAG_DATA_ROOT, and drives every verb:
# init, offers + permissions (user-only offers; a role-written offer is a
# violation), the claim race across clones (contested -> lost-race),
# withdraw, decline, msg + ack, board fill
# (reads the REAL priority table read-only), exit-wake watcher, transition,
# heartbeat, systemd plan, status, lint, report. Exit 1 on the first failed
# assertion. Nothing outside $SCRATCH is written (no real remote touched).
#   usage: selftest_ledger.sh [scratch-dir]
set -eu
HAGD="$(cd "$(dirname "$0")" && pwd)"
S="${1:-${TMPDIR:-/tmp}/hag-selftest-$$}"
rm -rf "$S"; mkdir -p "$S/root" "$S/data" "$S/remote"
export HAG_DATA_ROOT="$S/data"; ROOT="$S/root"
SPEC="$ROOT/t1hag/hag.json"; A="$S/cloneA"; B="$S/cloneB"
export HAG_SPEC="$SPEC"
H="python3 $HAGD/hag.py"
fail() { echo "SELFTEST FAIL: $*" >&2; exit 1; }
expect() { local rc=$1 got=0; shift; "$@" && got=0 || got=$?; [ "$got" = "$rc" ] || fail "rc=$got (want $rc): $*"; }
check() { local pat=$1; shift; local out; out=$("$@" 2>&1 || true); grep -q -- "$pat" <<<"$out" || fail "expected '$pat' in output of: $* :: $out"; }

git init -q --bare "$S/remote/hag-queues.git"
git clone -q "$S/remote/hag-queues.git" "$A" 2>/dev/null
( cd "$A" && git commit -q --allow-empty -m init && git push -q origin HEAD 2>/dev/null )
git clone -q "$S/remote/hag-queues.git" "$B" 2>/dev/null
mkdir -p "$ROOT/t1hag"
DOCS=/home/speediedan/repos/distributed-insight/project_admin/interpretune/design
cat > "$SPEC" <<JSON
{"hag":"t1hag","dir":"/home/speediedan/repos/interpretune","incarnation":0,"repos":["speediedan/interpretune"],
 "ledger":{"repo":"$S/remote/hag-queues.git","dir":"$A"},
 "sources":[{"kind":"github-project","owner":"speediedan","number":2}],
 "roles":[
  {"slug":"alpha","ecosystem":"claude","title":"alpha","doc":"$DOCS/ie_integration_hub/handoffs/it-architect.md","fill":"self","session":"aaaa1111","scope":{"repos":["speediedan/interpretune"]}},
  {"slug":"beta","ecosystem":"opencode","title":"beta","doc":"$DOCS/ie_integration_hub/ie_integration_hub_plan_state.md","fill":"self","session":"ses_bbbb"},
  {"slug":"observer","ecosystem":"opencode","title":"observer","doc":"$DOCS/jacobian_lens_subspace_explore/jlens_subspace_support_plan_state.md","fill":"none","can_fill_others":true,"coordinator":false,"queue":"","session":"ses_ffff"}
 ],"generations":[{"gen":"r1","incarnation":0,"started_utc":"2026-09-20T00:00:00Z","ended_utc":"","sessions":{},"run":{}}]}
JSON
echo "== 1 ledger-init (dry-run, live); a pre-flattening spec (can_fill_others/coordinator/queue keys) loads"
expect 0 $H ledger-init --spec "$SPEC" --dry-run >/dev/null
expect 0 $H ledger-init --spec "$SPEC" >/dev/null
[ -d "$A/hags/t1hag/ledger" ] || fail "ledger dir missing after init"
expect 0 $H offer --hag t1hag --to alpha --item interpretune#372:fix --base interpretune@3ac6fdf9 --note "phase 1 only" >/dev/null
check "interpretune#372:fix" $H show --hag t1hag --slug alpha
echo "== 2 offers + permissions"
expect 0 $H offer --hag t1hag --to alpha --item interpretune#600:fix --base interpretune@3ac6fdf9 --prio 10 >/dev/null
expect 1 $H offer --hag t1hag --to alpha --item interpretune#427:fix --by beta >/dev/null      # a role never offers
expect 1 $H offer --hag t1hag --to beta --item interpretune#427:fix --by beta >/dev/null       # not even to itself
expect 1 $H offer --hag t1hag --to alpha --item interpretune#427:fix --by observer >/dev/null  # legacy can_fill_others is ignored
check "only user offers work" $H offer --hag t1hag --to alpha --item interpretune#427:fix --by observer
expect 0 $H offer --hag t1hag --to alpha --item interpretune#427:fix --base interpretune@3ac6fdf9 >/dev/null
expect 0 $H offer --hag t1hag --to '*' --item interpretune#501:scope --base interpretune@3ac6fdf9 >/dev/null
check "already offered" $H offer --hag t1hag --to alpha --item interpretune#600:fix
check "pool offers (to=\*): interpretune#501:scope" $H show --hag t1hag
echo "== 3 claim race across clones on the pool item #501 (B stale, A lands first)"
expect 4 $H claim --hag t1hag --slug beta --item interpretune#600:fix --ledger-dir "$B" >/dev/null   # offered to alpha only: refused
expect 0 $H sync --hag t1hag --ledger-dir "$B" >/dev/null      # B fetches once, then goes stale
expect 0 $H claim --hag t1hag --slug beta --item interpretune#501:scope --ledger-dir "$B" --no-sync >/dev/null
expect 0 $H claim --hag t1hag --slug alpha --item interpretune#501:scope --base interpretune@3ac6fdf9 >/dev/null
expect 0 $H sync --hag t1hag --ledger-dir "$B" >/dev/null
expect 1 $H ledger-lint --hag t1hag >/dev/null            # E2 contested
check "E2 contested claim on interpretune#501" $H ledger-lint --hag t1hag
check "CLAIM-LOST interpretune#501: alpha landed first" timeout 20 $H ledger-watch --hag t1hag --slug beta --wake exit --interval 60 --ledger-dir "$B" --no-board
expect 0 $H release --hag t1hag --slug beta --item interpretune#501 --kind lost-race --reason "alpha landed first" --ledger-dir "$B" >/dev/null
expect 0 $H ledger-lint --hag t1hag >/dev/null
expect 4 $H claim --hag t1hag --slug beta --item interpretune#501:scope --ledger-dir "$B" >/dev/null   # held by alpha
echo "== 4 done, withdraw, decline, msg + ack, ask"
expect 0 $H claim --hag t1hag --slug alpha --item interpretune#600:fix >/dev/null
expect 0 $H done --hag t1hag --slug alpha --item interpretune#600:fix --kind merge --url https://github.com/speediedan/interpretune/pull/601 >/dev/null
expect 0 $H done --hag t1hag --slug alpha --item interpretune#501:scope >/dev/null
expect 4 $H claim --hag t1hag --slug beta --item interpretune#501:scope --ledger-dir "$B" >/dev/null   # done
expect 1 $H withdraw --hag t1hag --item interpretune#427 --to alpha --reason reassign --by beta >/dev/null   # roles never withdraw
expect 0 $H withdraw --hag t1hag --item interpretune#427 --to alpha --reason reassign >/dev/null
expect 0 $H offer --hag t1hag --to '*' --item interpretune#554:scope --base interpretune@3ac6fdf9 >/dev/null
expect 0 $H claim --hag t1hag --slug beta --item interpretune#554:scope --ledger-dir "$B" >/dev/null
expect 0 $H release --hag t1hag --slug beta --item interpretune#554 --kind decline --reason "out of lane" --retry "after #600" --ledger-dir "$B" >/dev/null
check "interpretune#554:scope" $H show --hag t1hag --slug alpha     # re-opened for others
check "declined: interpretune#554" $H show --hag t1hag --slug beta  # never re-offered to beta
expect 0 $H msg --hag t1hag --by user --to alpha --text "review adapter#39 next" --kind instruct --ack >/dev/null
MID=$(python3 -c "import json;[print(json.loads(l)['id']) for l in open('$A/hags/t1hag/ledger/user.jsonl') if '\"msg\"' in l]" | tail -1)
expect 0 $H msg --hag t1hag --by alpha --to user --text ack --kind ack --re "$MID" >/dev/null
expect 0 $H msg --hag t1hag --by beta --to user --text "need merge grant #602" --kind ask --ledger-dir "$B" >/dev/null
check "msgs to user" $H show --hag t1hag
echo "== 5 board fill (real priority table, read-only)"
check "still has" $H fill --hag t1hag --slug alpha            # #372 offered + #554 pool open
expect 0 $H claim --hag t1hag --slug alpha --item interpretune#372:fix >/dev/null
expect 0 $H done --hag t1hag --slug alpha --item interpretune#372:fix >/dev/null
expect 0 $H withdraw --hag t1hag --item interpretune#554 >/dev/null
check "interpretune#" $H fill --hag t1hag --slug alpha --limit 3
if $H fill --hag t1hag --slug alpha --limit 5 | grep -q "interpretune#600\|interpretune#372\|interpretune#501"; then fail "taken/withdrawn items surfaced as candidates"; fi
echo "== 6 exit-wake watcher: arms, wakes on a new offer, exits 0, sidecar bound"
( set +e; timeout 60 $H ledger-watch --hag t1hag --slug beta --wake exit --interval 2 --ledger-dir "$B" --no-board --session ses_bbbb > "$S/watch.out" 2>&1; echo "rc=$?" >> "$S/watch.out" ) &
sleep 4
expect 0 $H offer --hag t1hag --to beta --item interpretune#450:fix --base interpretune@3ac6fdf9 >/dev/null
wait
grep -q "LEDGER-WAKE beta@t1hag" "$S/watch.out" || fail "watcher did not wake: $(cat "$S/watch.out")"
grep -q "rc=0" "$S/watch.out" || fail "watcher exit code: $(tail -1 "$S/watch.out")"
grep -q "session_id=ses_bbbb" /tmp/t1hag/ledger-watch-beta.state || fail "sidecar not bound"
check "LEDGER-WAKE-TEST" timeout 20 $H ledger-watch --hag t1hag --slug beta --wake exit --interval 60 --ledger-dir "$B" --no-board --test
echo "== 7 transition -> ledger state; heartbeat covers via ledger; systemd plan; status; doctor; report"
check "ledger" $H transition --hag t1hag --slug alpha --to waiting --sub remote-ci --holder "gate 1350" --rescan-min 30
check "ledger" $H transition --hag t1hag --slug beta --to active --ledger-dir "$B"
rm -f /tmp/t1hag/hb-beta   # beta's heartbeat is on "another host": ledger state must cover it
check "beta: fresh" $H heartbeat --spec "$SPEC" --threshold-min 60 --dry-run
check "lanes ledger:t1hag" $H install-systemd --spec "$SPEC" --dry-run
if $H install-systemd --spec "$SPEC" --dry-run | grep -q "queue-"; then fail "legacy queue units in ledger mode"; fi
check "ledger: .*events=" $H status --spec "$SPEC"
check "ledger lint" $H doctor --spec "$SPEC"
check "## Ledger economics" $H report --spec "$SPEC" --gen r1 --dry-run --no-pull
check "## Wake efficiency" $H report --spec "$SPEC" --gen r1 --dry-run --no-pull
check "§13 wasted wakes" $H report --spec "$SPEC" --gen r1 --dry-run --no-pull
check "ledger-watch --hag t1hag --slug alpha --wake exit" $H brief --spec "$SPEC" --slug alpha
check "wake send:" $H brief --spec "$SPEC" --slug beta
echo "== 8 flattened only: a role-written offer is a projection violation; removed transports are gone"
expect 0 $H ledger-lint --hag t1hag >/dev/null
python3 -c "import sys; sys.path.insert(0, '$HAGD'); from ledger import Ledger; Ledger('t1hag', root='$A').append('beta', 'offer', to='alpha', item='interpretune#777:fix', base='interpretune@3ac6fdf9', prio=1)" >/dev/null
expect 1 $H ledger-lint --hag t1hag >/dev/null
check "E1 .* beta offer interpretune#777: beta may not offer to alpha" $H ledger-lint --hag t1hag
if $H show --hag t1hag --slug alpha | grep -q "interpretune#777"; then fail "role-written offer honoured by the projection"; fi
check "permission violations ignored: 1" $H show --hag t1hag
expect 1 $H watch --hag t1hag pipeline ses_ffff /tmp >/dev/null   # session-addressed fires removed
expect 2 $H queue-watch --sidecar /dev/null --interval 1 2>/dev/null
expect 2 $H lint-queues 2>/dev/null
expect 2 $H start --name t --dir /tmp --role a,claude,a,/dev/null,coord --legacy-queues --dry-run 2>/dev/null
rm -rf /tmp/t1hag
echo "SELFTEST PASS ($S)"
