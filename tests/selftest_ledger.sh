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
SHOALD="$(cd "$TESTD/../shoal" && pwd)"
. "$TESTD/selftest_lib.sh"
# /tmp/t1shoal is fixed and removed at the end: two runs at once fail each other, an upgrade's among them (2026-11-05)
exec 9>"/tmp/shoal-selftest-t1shoal.lock"; flock -w 1800 9 || { echo "SELFTEST FAIL: another t1shoal run holds the lock" >&2; exit 1; }
S="${1:-${TMPDIR:-/tmp}/shoal-selftest-$$}"
rm -rf "$S"; mkdir -p "$S/root" "$S/data" "$S/remote"
shoal_test_setup "$S"
ROOT="$S/root"
SPEC="$ROOT/shoals/t1shoal/shoal.json"; A="$S/cloneA"; B="$S/cloneB"
export SHOAL_SPEC="$SPEC"
H="python3 $SHOALD/cli.py"
fail() { echo "SELFTEST FAIL: $*" >&2; exit 1; }
expect() { local rc=$1 got=0; shift; "$@" && got=0 || got=$?; [ "$got" = "$rc" ] || fail "rc=$got (want $rc): $*"; }
check() { local pat=$1; shift; local out; out=$("$@" 2>&1 || true); grep -q -- "$pat" <<<"$out" || fail "expected '$pat' in output of: $* :: $out"; }

git init -q --bare "$S/remote/ledgers.git"
git clone -q "$S/remote/ledgers.git" "$A" 2>/dev/null
( cd "$A" && git commit -q --allow-empty -m init && git push -q origin HEAD 2>/dev/null )
git clone -q "$S/remote/ledgers.git" "$B" 2>/dev/null
mkdir -p "$ROOT/shoals/t1shoal"
# synthetic board, board order = priority: open app items, one out of alpha's scope,
# one closed, one in a skipped status
shoal_board_fixture "$S/board.json" <<'JSON'
[{"repo":"acme/app","number":600,"title":"fixture: offered then done"},
 {"repo":"acme/app","number":372,"title":"fixture: offered then done"},
 {"repo":"acme/app","number":501,"title":"fixture: pool item, contested"},
 {"repo":"acme/app","number":9001,"title":"fixture: free candidate one"},
 {"repo":"example/other","number":39,"title":"fixture: outside alpha's scope"},
 {"repo":"acme/app","number":9002,"title":"fixture: closed","state":"CLOSED"},
 {"repo":"acme/app","number":9003,"title":"fixture: done status","status":"Done"},
 {"repo":"acme/app","number":9004,"title":"fixture: free candidate two","kind":"pr"},
 {"repo":"acme/app","number":9005,"title":"fixture: free candidate three"}]
JSON
DOC_A=$(shoal_doc alpha); DOC_B=$(shoal_doc beta); DOC_O=$(shoal_doc observer)
cat > "$SPEC" <<JSON
{"shoal":"t1shoal","dir":"$WORK","incarnation":0,"repos":["acme/app"],
 "ledger":{"repo":"$S/remote/ledgers.git","dir":"$A"},
 "sources":[{"kind":"github-project","owner":"example","number":2}],
 "roles":[
  {"slug":"alpha","ecosystem":"claude","title":"alpha","doc":"$DOC_A","fill":"self","session":"aaaa1111","scope":{"repos":["acme/app"]}},
  {"slug":"beta","ecosystem":"opencode","title":"beta","doc":"$DOC_B","fill":"self","session":"ses_bbbb"},
  {"slug":"observer","ecosystem":"opencode","title":"observer","doc":"$DOC_O","fill":"none","can_fill_others":true,"coordinator":false,"queue":"","session":"ses_ffff"}
 ],"generations":[{"gen":"r1","incarnation":0,"started_utc":"2026-09-20T00:00:00Z","ended_utc":"","sessions":{},"run":{}}]}
JSON
echo "== 1 ledger-init (dry-run, live); a pre-flattening spec (can_fill_others/coordinator/queue keys) loads"
expect 0 $H ledger-init --spec "$SPEC" --dry-run >/dev/null
expect 0 $H ledger-init --spec "$SPEC" >/dev/null
[ -d "$A/shoals/t1shoal/ledger" ] || fail "ledger dir missing after init"
# the ledger repo ignores its lock and temp files, so an untracked lock never blocks a rebase
[ "$(grep -cxF .shoal-lock "$A/.gitignore")" = 1 ] && [ "$(grep -cxF '*.tmp' "$A/.gitignore")" = 1 ] \
  || fail "ledger .gitignore lacks .shoal-lock / *.tmp :: $(cat "$A/.gitignore" 2>&1)"
[ -f "$A/.shoal-lock" ] || fail "no lock file after init (the check below would prove nothing)"
[ -z "$(git -C "$A" status --porcelain)" ] || fail "ledger clone not clean after init :: $(git -C "$A" status --porcelain)"
# an existing ledger whose hand-made .gitignore names only an old lock: extended once, lines kept
printf '.old-lock\n' > "$A/.gitignore"; git -C "$A" commit -qam "hand-made ignore" >/dev/null
check "added .shoal-lock, \*.tmp" $H ledger-init --spec "$SPEC"
expect 0 $H ledger-init --spec "$SPEC" >/dev/null
[ "$(cat "$A/.gitignore")" = "$(printf '.old-lock\n.shoal-lock\n*.tmp')" ] || fail "gitignore not extended idempotently :: $(cat "$A/.gitignore")"
[ -z "$(git -C "$A" status --porcelain)" ] || fail "ledger clone not clean after re-init :: $(git -C "$A" status --porcelain)"
expect 0 $H offer --shoal t1shoal --to alpha --item app#372:fix --base app@3ac6fdf9 --note "phase 1 only" >/dev/null
check "app#372:fix" $H show --shoal t1shoal --slug alpha
# a pull that lands on another writer's uncommitted line (a multi-line write between its appends and
# its one commit) rebases around it and keeps it; a plain rebase refused and killed the watcher (FM-17)
git -C "$B" pull -q 2>/dev/null; echo remote > "$B/fm17-remote"; git -C "$B" add fm17-remote
git -C "$B" commit -qm "fm17 remote commit"; git -C "$B" push -q 2>/dev/null || fail "fm17: push from B"
echo '{"ts":"2026-09-20T00:00:09Z","type":"state","state":"idle"}' >> "$A/shoals/t1shoal/ledger/user.jsonl"
python3 - "$SHOALD" "$A" <<'PY' || fail "pull onto an uncommitted writer line"
import os, sys
sys.path.insert(0, sys.argv[1])
from ledger import sync_pull
a = sys.argv[2]
assert sync_pull(a) is True
assert os.path.exists(os.path.join(a, "fm17-remote")), "remote commit not pulled"
assert open(os.path.join(a, "shoals/t1shoal/ledger/user.jsonl")).read().rstrip().endswith('"state":"idle"}'), "line lost"
print("fm17 autostash ok")
# a fetch that hangs past its timeout is a failed fetch (False), never an exception that ends a watcher
import subprocess, ledger
real = subprocess.run
def hung(argv, *a, **k):
    if "fetch" in argv:
        raise subprocess.TimeoutExpired(argv, k.get("timeout", 60))
    return real(argv, *a, **k)
ledger.subprocess.run = hung
assert sync_pull(a) is False
ledger.subprocess.run = real
print("fm17 hung fetch ok")
PY
git -C "$A" checkout -q -- shoals/t1shoal/ledger/user.jsonl
echo "== 2 offers + permissions"
expect 0 $H offer --shoal t1shoal --to alpha --item app#600:fix --base app@3ac6fdf9 --prio 10 >/dev/null
expect 1 $H offer --shoal t1shoal --to alpha --item app#427:fix --by beta >/dev/null      # a role never offers
expect 1 $H offer --shoal t1shoal --to beta --item app#427:fix --by beta >/dev/null       # not even to itself
expect 1 $H offer --shoal t1shoal --to alpha --item app#427:fix --by observer >/dev/null  # legacy can_fill_others is ignored
check "only user offers work" $H offer --shoal t1shoal --to alpha --item app#427:fix --by observer
expect 0 $H offer --shoal t1shoal --to alpha --item app#427:fix --base app@3ac6fdf9 >/dev/null
expect 0 $H offer --shoal t1shoal --to '*' --item app#501:scope --base app@3ac6fdf9 >/dev/null
check "already offered" $H offer --shoal t1shoal --to alpha --item app#600:fix
check "pool offers (to=\*): app#501:scope" $H show --shoal t1shoal
echo "== 3 claim race across clones on the pool item #501 (B stale, A lands first)"
expect 4 $H claim --shoal t1shoal --slug beta --item app#600:fix --ledger-dir "$B" >/dev/null   # offered to alpha only: refused
expect 0 $H sync --shoal t1shoal --ledger-dir "$B" >/dev/null      # B fetches once, then goes stale
expect 0 $H claim --shoal t1shoal --slug beta --item app#501:scope --ledger-dir "$B" --no-sync >/dev/null
expect 0 $H claim --shoal t1shoal --slug alpha --item app#501:scope --base app@3ac6fdf9 >/dev/null
expect 0 $H sync --shoal t1shoal --ledger-dir "$B" >/dev/null
expect 1 $H ledger-lint --shoal t1shoal >/dev/null            # E2 contested
check "E2 contested claim on app#501" $H ledger-lint --shoal t1shoal
check "CLAIM-LOST app#501: alpha landed first" timeout 20 $H ledger-watch --shoal t1shoal --slug beta --wake exit --interval 60 --ledger-dir "$B" --no-board
expect 0 $H release --shoal t1shoal --slug beta --item app#501 --kind lost-race --reason "alpha landed first" --ledger-dir "$B" >/dev/null
expect 0 $H ledger-lint --shoal t1shoal >/dev/null
expect 4 $H claim --shoal t1shoal --slug beta --item app#501:scope --ledger-dir "$B" >/dev/null   # held by alpha
echo "== 4 done, withdraw, decline, msg + ack, ask"
expect 0 $H claim --shoal t1shoal --slug alpha --item app#600:fix >/dev/null
expect 0 $H done --shoal t1shoal --slug alpha --item app#600:fix --kind merge --url https://github.com/acme/app/pull/601 >/dev/null
expect 0 $H done --shoal t1shoal --slug alpha --item app#501:scope >/dev/null
expect 4 $H claim --shoal t1shoal --slug beta --item app#501:scope --ledger-dir "$B" >/dev/null   # done
expect 1 $H withdraw --shoal t1shoal --item app#427 --to alpha --reason reassign --by beta >/dev/null   # roles never withdraw
expect 0 $H withdraw --shoal t1shoal --item app#427 --to alpha --reason reassign >/dev/null
expect 0 $H offer --shoal t1shoal --to '*' --item app#554:scope --base app@3ac6fdf9 >/dev/null
expect 0 $H claim --shoal t1shoal --slug beta --item app#554:scope --ledger-dir "$B" >/dev/null
expect 0 $H release --shoal t1shoal --slug beta --item app#554 --kind decline --reason "out of lane" --retry "after #600" --ledger-dir "$B" >/dev/null
check "app#554:scope" $H show --shoal t1shoal --slug alpha     # re-opened for others
check "declined: app#554" $H show --shoal t1shoal --slug beta  # never re-offered to beta
expect 0 $H msg --shoal t1shoal --by user --to alpha --text "review adapter#39 next" --kind instruct --ack >/dev/null
MID=$(python3 -c "import json;[print(json.loads(l)['id']) for l in open('$A/shoals/t1shoal/ledger/user.jsonl') if '\"msg\"' in l]" | tail -1)
expect 0 $H msg --shoal t1shoal --by alpha --to user --text ack --kind ack --re "$MID" >/dev/null
expect 0 $H msg --shoal t1shoal --by beta --to user --text "need merge grant #602" --kind ask --ledger-dir "$B" >/dev/null
check "msgs to user" $H show --shoal t1shoal
# an outside seat (a standalone session) may send plain mail to one role: nothing else
expect 0 $H msg --shoal t1shoal --by ext:outside-seat --to alpha --text "fsdp2 handoff: see the doc" >/dev/null
expect 1 $H msg --shoal t1shoal --by ext:outside-seat --to '*' --text "x" >/dev/null
expect 1 $H msg --shoal t1shoal --by ext:outside-seat --to alpha --text "x" --wake >/dev/null
expect 1 $H msg --shoal t1shoal --by ext:outside-seat --to alpha --text "x" --kind instruct >/dev/null
expect 1 $H msg --shoal t1shoal --by ext:Bad_Name --to alpha --text "x" >/dev/null
expect 1 $H offer --shoal t1shoal --by ext:outside-seat --to alpha --item app#555:fix --base app@3ac6fdf9 >/dev/null
check "ext:outside-seat" $H show --shoal t1shoal --slug alpha
# the reply path: a role mails the outside seat, which reads it with show --slug ext:<name>
expect 0 $H msg --shoal t1shoal --by alpha --to ext:outside-seat --text "reply: defaults are fine" >/dev/null
check "reply: defaults are fine" $H show --shoal t1shoal --slug ext:outside-seat
expect 1 $H msg --shoal t1shoal --by alpha --to ext:outside-seat --text "x" --kind instruct >/dev/null
expect 1 $H msg --shoal t1shoal --by user --to ext:outside-seat --text "x" --wake >/dev/null
echo "== 5 board fill (synthetic board fixture, no GitHub read)"
check "still has" $H fill --shoal t1shoal --slug alpha            # #372 offered + #554 pool open
expect 0 $H claim --shoal t1shoal --slug alpha --item app#372:fix >/dev/null
expect 0 $H done --shoal t1shoal --slug alpha --item app#372:fix >/dev/null
expect 0 $H withdraw --shoal t1shoal --item app#554 >/dev/null
check "app#9001" $H fill --shoal t1shoal --slug alpha --limit 3
FILL=$($H fill --shoal t1shoal --slug alpha --limit 5)
if grep -q "app#600\|app#372\|app#501" <<<"$FILL"; then fail "taken/withdrawn items surfaced as candidates"; fi
if grep -q "other#39\|app#9002\|app#9003" <<<"$FILL"; then fail "out-of-scope, closed or Done items surfaced :: $FILL"; fi
grep -q "app#9004" <<<"$FILL" && grep -q "app#9005" <<<"$FILL" || fail "board order candidates missing :: $FILL"
echo "== 6 exit-wake watcher: arms, wakes on a new offer, exits 0, sidecar bound"
( set +e; timeout 60 $H ledger-watch --shoal t1shoal --slug beta --wake exit --interval 2 --ledger-dir "$B" --no-board --session ses_bbbb > "$S/watch.out" 2>&1; echo "rc=$?" >> "$S/watch.out" ) &
sleep 4
expect 0 $H offer --shoal t1shoal --to beta --item app#450:fix --base app@3ac6fdf9 >/dev/null
wait
grep -q "LEDGER-WAKE beta@t1shoal" "$S/watch.out" || fail "watcher did not wake: $(cat "$S/watch.out")"
grep -q "rc=0" "$S/watch.out" || fail "watcher exit code: $(tail -1 "$S/watch.out")"
grep -q "session_id=ses_bbbb" /tmp/t1shoal/ledger-watch-beta.state || fail "sidecar not bound"
check "LEDGER-WAKE-TEST" timeout 20 $H ledger-watch --shoal t1shoal --slug beta --wake exit --interval 60 --ledger-dir "$B" --no-board --test
echo "== 6b a re-armed watcher counts fetch failures afresh: a dead watcher's count must not end it on its first miss"
git clone -q "$B" "$S/unreachable"; git -C "$S/unreachable" remote set-url origin "$S/no-such-remote.git"
sed -i 's/^fetch_fail=.*/fetch_fail=7/' /tmp/t1shoal/ledger-watch-beta.state
grep -q "^fetch_fail=7$" /tmp/t1shoal/ledger-watch-beta.state || fail "sidecar not seeded with a dead watcher's count"
OUT=$(timeout 60 $H ledger-watch --shoal t1shoal --slug beta --wake exit --interval 1 --ledger-dir "$S/unreachable" --no-board --session ses_bbbb 2>&1 || true)
grep -q "FETCH-FAILED (1/5)" <<<"$OUT" && grep -q "FETCH-FAILED (5/5)" <<<"$OUT" && grep -q "INSTRUMENT-FAILED" <<<"$OUT" \
  || fail "re-armed watcher did not count afresh :: $OUT"
echo "== 7 transition -> ledger state; heartbeat covers via ledger; systemd plan; status; doctor; report"
check "ledger" $H transition --shoal t1shoal --slug alpha --to waiting --sub remote-ci --holder "gate 1350" --rescan-min 30
check "ledger" $H transition --shoal t1shoal --slug beta --to active --ledger-dir "$B"
rm -f /tmp/t1shoal/hb-beta   # beta's heartbeat is on "another host": ledger state must cover it
check "beta: fresh" $H heartbeat --spec "$SPEC" --threshold-min 60 --dry-run
check "lanes ledger:t1shoal" $H install-systemd --spec "$SPEC" --dry-run
if $H install-systemd --spec "$SPEC" --dry-run | grep -q "queue-"; then fail "legacy queue units in ledger mode"; fi
check "ledger: .*events=" $H status --spec "$SPEC"
check "ledger lint" $H doctor --spec "$SPEC"
check "## Ledger economics" $H report --spec "$SPEC" --gen r1 --dry-run --no-pull
check "## Wake efficiency" $H report --spec "$SPEC" --gen r1 --dry-run --no-pull
check "§13 wasted wakes" $H report --spec "$SPEC" --gen r1 --dry-run --no-pull
check "ledger-watch --shoal t1shoal --slug alpha --wake direct" $H brief --spec "$SPEC" --slug alpha  # Claude: direct wake by default
check "wake send:" $H brief --spec "$SPEC" --slug beta
check "prove the pid with TWO separate commands: first .cat /tmp/t1shoal/ledger-watch-alpha.state." $H brief --spec "$SPEC" --slug alpha
if $H brief --spec "$SPEC" --slug alpha | grep -qE 'ps -p \$\(|ps -p plus sidecar'; then fail "brief combines the pid proof"; fi
echo "== 8 flattened only: a role-written offer is a projection violation; removed transports are gone"
expect 0 $H ledger-lint --shoal t1shoal >/dev/null
python3 -c "import sys; sys.path.insert(0, '$SHOALD'); from ledger import Ledger; Ledger('t1shoal', root='$A').append('beta', 'offer', to='alpha', item='app#777:fix', base='app@3ac6fdf9', prio=1)" >/dev/null
expect 1 $H ledger-lint --shoal t1shoal >/dev/null
check "E1 .* beta offer app#777: beta may not offer to alpha" $H ledger-lint --shoal t1shoal
if $H show --shoal t1shoal --slug alpha | grep -q "app#777"; then fail "role-written offer honoured by the projection"; fi
check "permission violations ignored: 1" $H show --shoal t1shoal
expect 1 $H watch --shoal t1shoal pipeline ses_ffff /tmp >/dev/null   # session-addressed fires removed
expect 2 $H queue-watch --sidecar /dev/null --interval 1 2>/dev/null
expect 2 $H lint-queues 2>/dev/null
expect 2 $H start --name t --dir /tmp --role a,claude,a,/dev/null,coord --legacy-queues --dry-run 2>/dev/null
echo "== 9 relocation: no ledger repo configured -> explicit --ledger-repo; relative role docs"
check "pass --ledger-repo" $H start --name t --dir "$WORK" --role "a,claude,a,$DOC_A" --dry-run
expect 1 $H start --name t --dir "$WORK" --role "a,claude,a,$DOC_A" --dry-run >/dev/null
python3 -c "import json,sys; d=json.load(open('$SPEC')); d.pop('ledger'); d['shoal']='t2shoal'; json.dump(d, open('$S/noledger.json','w'))"
check "pass --ledger-repo" $H ledger-init --spec "$S/noledger.json" --dry-run
check "\[dry-run\] ledger for t2shoal: clone $S/remote/ledgers.git" $H ledger-init --spec "$S/noledger.json" --ledger-repo "$S/remote/ledgers.git" --dry-run
mkdir -p "$S/rel/shoals/t1shoal"
python3 -c "
import json; d=json.load(open('$SPEC'))
d['roles'][0]['doc']='alpha_plan_state.md'; d['roles'][1]['doc']='./sub/../beta_plan_state.md'
for r in d['roles']: r['session']=''   # no live sessions: group_checks probes no harness
json.dump(d, open('$S/rel/shoals/t1shoal/shoal.json','w'))"
python3 - "$SHOALD" "$S/rel/shoals/t1shoal/shoal.json" > "$S/doctor-rel.out" 2>&1 <<'PY' || true
import sys; sys.path.insert(0, sys.argv[1])
import doctor
from roles import ShoalSpec
d = doctor.Doc()
doctor.group_checks(d, ShoalSpec.load(sys.argv[2]), sys.argv[2], 60)
PY
grep -q "WARN doc path alpha: relative role doc 'alpha_plan_state.md'" "$S/doctor-rel.out" || fail "doctor did not warn on a relative doc :: $(cat "$S/doctor-rel.out")"
check "\[dry-run\] alpha: alpha_plan_state.md -> $DOCS/alpha_plan_state.md" $H spec absolutize-docs --spec "$S/rel/shoals/t1shoal/shoal.json" --base "$DOCS" --dry-run
grep -q '"doc": "alpha_plan_state.md"' "$S/rel/shoals/t1shoal/shoal.json" || fail "dry-run rewrote the spec"
check "rewrote 2 role doc(s)" $H spec absolutize-docs --spec "$S/rel/shoals/t1shoal/shoal.json" --base "$DOCS"
python3 -c "
import json; r=json.load(open('$S/rel/shoals/t1shoal/shoal.json'))['roles']
assert r[0]['doc']=='$DOCS/alpha_plan_state.md' and r[1]['doc']=='$DOCS/beta_plan_state.md' and r[2]['doc']=='$DOC_O', r" \
  || fail "absolutize-docs wrote the wrong paths"
check "nothing to change" $H spec absolutize-docs --spec "$S/rel/shoals/t1shoal/shoal.json" --base "$DOCS"
echo "== 10 system writer: an offer by the system writer is honoured; SHOAL_* env"
python3 - "$SHOALD" "$SPEC" "$A" <<'PY2' || fail "system-writer offer not honoured"
import sys; sys.path.insert(0, sys.argv[1])
from ledger import Ledger, View, SYSTEM_WRITER
from roles import ShoalSpec
assert SYSTEM_WRITER == "shoal", SYSTEM_WRITER
led = Ledger("t1shoal", root=sys.argv[3])
led.append(SYSTEM_WRITER, "offer", to="alpha", item="app#779:fix", base="app@3ac6fdf9", prio=1)
v = View(Ledger("t1shoal", root=sys.argv[3]), ShoalSpec.load(sys.argv[2]))
items = {o["item"].split(":")[0] for o in v.offers.values() if o["status"] == "open"}
assert "app#779" in items, items
bad = [x for x in v.violations if "#779" in str(x)]
assert not bad, bad
PY2
[ "$(SHOAL_DATA_ROOT="$S/new-root" python3 -c "import sys; sys.path.insert(0, '$SHOALD'); import config; print(config.data_root())")" = "$S/new-root" ] \
  || fail "SHOAL_DATA_ROOT not honoured"
echo "== layout: shoals/<id>/shoal.json; next_max; retired wake relay ignored, relay wake alias"
python3 - "$SHOALD" "$S/layout" <<'PY3' || fail "layout / next_max / retired relay checks"
import os, sys
sys.path.insert(0, sys.argv[1])
from layout import group_dir, spec_path
root = sys.argv[2]
assert group_dir(root, "new1").endswith("shoals/new1") and spec_path(root, "new1").endswith("shoals/new1/shoal.json")
from ledger import queue_next_max, QUEUE_NEXT_MAX
class S:  # a stand-in spec
    def __init__(self, f): self.features = f
assert queue_next_max(S({"workwake": True})) == QUEUE_NEXT_MAX == 3
assert queue_next_max(S({"workwake": {"next_max": 5}})) == 5
assert queue_next_max(S({"workwake": {"next_max": "x"}})) == 3
from roles import ShoalSpec, canon_wake, is_detached_wake
# the retired relay role (fill=none) is dropped at load; a worker that merely shares the slug is kept
d = {"shoal": "x", "dir": "/w", "roles": [{"slug": "a", "ecosystem": "claude"},
                                          {"slug": "wake-relay", "ecosystem": "claude", "fill": "none", "session": "r"}]}
assert [r.slug for r in ShoalSpec.from_dict(d).roles] == ["a"]
d["roles"][1]["fill"] = "self"
assert [r.slug for r in ShoalSpec.from_dict(d).roles] == ["a", "wake-relay"]
# `relay` (the retired relay's watcher mode) is read as `direct`
assert canon_wake("relay") == "direct" and canon_wake("send:x") == "send:x" and canon_wake("exit") == "exit"
assert is_detached_wake("relay") and is_detached_wake("direct") and is_detached_wake("exec:s") and not is_detached_wake("exit")
from digest import has_affinity
assert not has_affinity({"affinity": {}}) and has_affinity({"affinity": {"repos": {"a/b": 1}}})
print("layout ok")
PY3
echo "== opencode store: v1 and v2 layouts (provider-error scan, quota check)"
python3 - "$SHOALD" "$S/ocstore" <<'PY3' || fail "opencode store layout handling"
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
python3 - "$SHOALD" <<'PY3' || fail "claude --bg id not parsed through colour codes"
import sys
sys.path.insert(0, sys.argv[1])
from backends import parse_bg_id
assert parse_bg_id("backgrounded · \x1b[36mcdb29a7f\x1b[39m · role-r1").group(1) == "cdb29a7f"
assert parse_bg_id("backgrounded · abc123 · role-r1").group(1) == "abc123"
assert parse_bg_id("error: no session") is None
PY3
echo "== wait targets: normalization and validation"
SHOAL_DEFAULT_OWNER=acme python3 - "$SHOALD" <<'PY3' || fail "wait target normalization"
import sys
sys.path.insert(0, sys.argv[1])
from providers import normalize
assert normalize("gh-pr:app:637") == ("gh-pr:acme/app#637", ""), normalize("gh-pr:app:637")
assert normalize("gh-pr:acme/app#637")[0] == "gh-pr:acme/app#637"
assert normalize("gh-issue:decoderesearch/interp-engine#28")[0] == "gh-issue:decoderesearch/interp-engine#28"
assert normalize("gh-pr:no-number-here")[1], "unparseable gh-pr accepted"
assert normalize("gh-run:acme/x:12345") == ("gh-run:acme/x:12345", "")
assert normalize("gh-run:12345")[1], "gh-run without a repo accepted"
assert "no provider" in normalize("bogus:1")[1]
assert normalize("lease:gpu") == ("lease:gpu", "")
print("targets ok")
PY3
echo "== upstream semantics: an open PR is pending whatever its checks say"
SHOAL_DEFAULT_OWNER=acme python3 - "$SHOALD" "$S" <<'PY3' || fail "upstream gh-pr semantics"
import subprocess, sys
sys.path.insert(0, sys.argv[1])
import providers, state
calls = []
def fake(state_, buckets):
    def run(argv, timeout=30):
        calls.append(argv)
        out = '{"state": "%s"}' % state_ if "view" in argv else str([{"bucket": b} for b in buckets]).replace("'", '"')
        return subprocess.CompletedProcess(argv, 0, out, "")
    return run
# the measured shape: an upstream PR awaiting review, checks finished (one failing)
providers._run = fake("OPEN", ["fail", "pass", "skipping"])
assert providers.resolve("gh-pr:acme/x#7", "r")[0] is False              # a CI wait: checks done
assert providers.resolve("gh-pr:acme/x#7", "r", upstream=True) == (True, "PR open")
assert not any("checks" in a for a in calls[-1:]), "upstream semantics read checks"
canon, notes, refusal = state.check_target("upstream", "gh-pr:acme/x#7", "r")
assert canon == "gh-pr:acme/x#7" and not refusal, refusal                 # was refused as resolved
assert state.check_target("remote-ci", "gh-pr:acme/x#7", "r")[2], "a finished CI wait accepted"
providers._run = fake("MERGED", [])
assert providers.resolve("gh-pr:acme/x#7", "r", upstream=True) == (False, "PR merged")
assert "already resolved" in state.check_target("upstream", "gh-pr:acme/x#7", "r")[2]
import blockers
providers._run = fake("OPEN", ["pass"])
class V: done = {}
spec = type("Spec", (), {"features": {"blockers": True}, "sid": "t"})()
assert blockers.check(spec, V(), "r", {}, "x#1", "upstream:gh-pr:acme/x#7", None)[0] == "live"
providers._run = fake("CLOSED", [])
assert blockers.check(spec, V(), "r", {}, "x#1", "upstream:gh-pr:acme/x#7", None) == ("resolved", "PR closed")
print("upstream semantics ok")
PY3
echo "== skills: stale user-scope copies are reported, then synced"
python3 - "$SHOALD" <<'PY3' || fail "skill copy sync"
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
echo "== multi-host: two clones write ONE shared file (user) before a sync; union merge, no wedge"
# measured 2026-10-19 on two real hosts: without the union rule the second host's rebase conflicted and
# every later pull on it failed (its writes stayed local)
expect 0 $H msg --shoal t1shoal --ledger-dir "$A" --no-sync --by user --to alpha --text "MH1 from host A"
expect 0 $H msg --shoal t1shoal --ledger-dir "$B" --by user --to beta --text "MH1 from host B"
expect 0 $H sync --shoal t1shoal --ledger-dir "$A"
BR=$(git -C "$A" rev-parse --abbrev-ref HEAD)
[ "$(git -C "$A" rev-list --count "origin/$BR..HEAD")" = 0 ] || fail "host A has unpushed commits after sync"
expect 0 $H sync --shoal t1shoal --ledger-dir "$B"
for c in "$A" "$B"; do
  [ "$(grep -c 'MH1 from host' "$c/shoals/t1shoal/ledger/user.jsonl")" = 2 ] || fail "$c lacks a host's user line :: $(cat "$c/shoals/t1shoal/ledger/user.jsonl")"
  grep -qxF 'shoals/*/ledger/*.jsonl merge=union' "$c/.git/info/attributes" || fail "$c has no clone-local union rule"
  [ -z "$(git -C "$c" status --porcelain)" ] || fail "$c not clean after the shared-file race :: $(git -C "$c" status --porcelain)"
done
git -C "$A" show "origin/$BR:.gitattributes" | grep -qxF 'shoals/*/ledger/*.jsonl merge=union' || fail "the union rule was not committed to the ledger repo"
echo "== multi-host: two hosts save the spec concurrently; each keeps the other's change (no lost update, no wedge)"
python3 - "$SHOALD" "$A" "$B" <<'PY3' || fail "concurrent spec saves"
import io, contextlib, json, sys
sys.path.insert(0, sys.argv[1])
import cli
from roles import ShoalSpec
A, B = sys.argv[2], sys.argv[3]
pa, pb = f"{A}/shoals/t1shoal/shoal.json", f"{B}/shoals/t1shoal/shoal.json"
sa = ShoalSpec.load(pa)  # host A loads first (a restart holds its spec for minutes)
sb = ShoalSpec.load(pb)
for sp, root in ((sb, B),):  # each host's clone is its own (the spec names one path; real hosts share the layout)
    sp.ledger = dict(sp.ledger, dir=root)
    sp.base["ledger"] = dict(sp.base["ledger"], dir=root)
sb.role("beta").model = "model-from-B"
with contextlib.redirect_stdout(io.StringIO()):
    cli.persist_spec(sb, pb, note="B edits beta")
sa.role("alpha").model = "model-from-A"
out = io.StringIO()
with contextlib.redirect_stdout(out):
    cli.persist_spec(sa, pa, note="A edits alpha")
assert "pushed" in out.getvalue(), out.getvalue()
import subprocess
br = subprocess.run(["git", "-C", A, "rev-parse", "--abbrev-ref", "HEAD"], capture_output=True, text=True).stdout.strip()
remote = json.loads(subprocess.run(["git", "-C", A, "show", f"origin/{br}:shoals/t1shoal/shoal.json"],
                                   capture_output=True, text=True, check=True).stdout)
models = {r["slug"]: r.get("model") for r in remote["roles"]}
assert models["alpha"] == "model-from-A" and models["beta"] == "model-from-B", models
assert remote["ledger"]["dir"] == A, remote["ledger"]  # B's local clone path never leaked into the shared spec
assert sa.role("beta").model == "model-from-B", "A's in-memory spec did not take the merged copy"
print("spec merge ok")
PY3
echo "== a restart's own ledger snapshot is the merge base: no session WARN, generation history kept"
python3 - "$SHOALD" "$A" <<'PY3' || fail "restart snapshot merge"
import io, contextlib, json, sys
sys.path.insert(0, sys.argv[1])
import cli
from layout import spec_file
from ledger import Ledger
from roles import ShoalSpec
pa = f"{sys.argv[2]}/shoals/t1shoal/shoal.json"
def quiet_persist(s, note):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        cli.persist_spec(s, pa, note=note)
    return out.getvalue()
s = ShoalSpec.load(pa)
d = s.to_dict()
d["generations"] = [{"gen": f"r{i}", "incarnation": i, "started_utc": f"2026-10-0{i}T00:00:00Z", "ended_utc": "",
                     "sessions": {}, "seqs": {}} for i in (1, 2, 3)]
s2 = ShoalSpec.from_dict(d); s2.base = s.base
quiet_persist(s2, "history")
# the restart (stage_restart): launch with a short id, record the generation, snapshot over the spec file, then adopt
# the full id from the new watcher's sidecar and persist (measured 2026-10-26: a WARN each time, history cut to two)
for genuine in (False, True):
    s = ShoalSpec.load(pa)
    gens0 = [g["gen"] for g in s.to_dict()["generations"]]
    s.role("alpha").session = "abcd1234"
    d = s.to_dict(); d["generations"].append({"gen": f"r{len(gens0) + 1}", "incarnation": len(gens0) + 1,
                                               "started_utc": "2026-10-09T00:00:00Z", "ended_utc": "", "sessions": {}, "seqs": {}})
    base = s.base; s = ShoalSpec.from_dict(d); s.base = base
    led = Ledger(s.sid, root=sys.argv[2], sync=False)
    snap = s.snapshot()
    led.snapshot_spec(snap, push=False)
    s.snapshot_written = (spec_file(led.dir), json.loads(json.dumps(snap)))
    if genuine:  # another writer changes the same field after the snapshot: still a conflict
        other = json.loads(json.dumps(snap)); [r for r in other["roles"] if r["slug"] == "alpha"][0]["session"] = "zzzz9999"
        led.snapshot_spec(other, push=False)
    s.role("alpha").session = "abcd1234-0000-full"
    out = quiet_persist(s, "restart")
    saved = json.load(open(pa))
    gens = [g["gen"] for g in saved["generations"]]
    assert set(gens0) < set(gens) and len(gens) == len(gens0) + 1, (gens0, gens)
    assert [r for r in saved["roles"] if r["slug"] == "alpha"][0]["session"] == "abcd1234-0000-full", saved["roles"]
    assert ("WARN spec merge: role alpha.session" in out) == genuine, out
print("restart snapshot merge ok")
PY3
expect 0 $H sync --shoal t1shoal --ledger-dir "$B"
echo "== multi-host: locality (a remote role is not watched, woken or launched from this host)"
MH="$S/mh"; mkdir -p "$MH/shoals/t1shoal"
python3 - "$SPEC" "$MH/shoals/t1shoal/shoal.json" <<'PY3'
import json, sys
d = json.load(open(sys.argv[1]))
d["home"] = "hostA"
for r in d["roles"]:
    r["server"] = "hostB" if r["slug"] == "beta" else ""
json.dump(d, open(sys.argv[2], "w"), indent=2)
PY3
SHOAL_SERVER=hostA python3 - "$SHOALD" "$MH/shoals/t1shoal/shoal.json" "$SPEC" "$A" <<'PY3' || fail "locality"
import sys
sys.path.insert(0, sys.argv[1])
import health
from ledger import Ledger, View
from roles import ShoalSpec, is_home, is_local
mh, single = ShoalSpec.load(sys.argv[2]), ShoalSpec.load(sys.argv[3])
mh.ledger = dict(mh.ledger, dir=sys.argv[4])
assert is_home(mh) and is_local(mh.role("alpha"), mh) and not is_local(mh.role("beta"), mh)
view = View(Ledger("t1shoal", root=sys.argv[4], sync=False), mh)
# control first: the same role in the single-host spec raises conditions here (no sidecar on this host)
ctl = [c for c in health.evaluate(single, View(Ledger("t1shoal", root=sys.argv[4], sync=False), single), {}) if c["slug"] == "beta"]
assert ctl, "control: beta raised no condition even as a local role, so the check below would prove nothing"
got = [c for c in health.evaluate(mh, view, {}) if c["slug"] == "beta"]
assert not got, got
w = health.request_wake(mh, "beta", "test", "test", dry_run=True)
assert w.startswith("remote (skipped"), w
print("locality ok")
PY3
# a peek from the home host names the remote role's host instead of reading this host's sidecar (control: local alpha)
SHOAL_SERVER=hostA check "WATCH ledger-watch runs on hostB: check it there" $H next --shoal t1shoal --spec "$MH/shoals/t1shoal/shoal.json" --slug beta --peek --no-sync
SHOAL_SERVER=hostA check "WATCH ledger-watch pid=" $H next --shoal t1shoal --spec "$MH/shoals/t1shoal/shoal.json" --slug alpha --peek --no-sync
SHOAL_SERVER=hostA check "REFUSED: beta run(s) on another host" $H restart --spec "$MH/shoals/t1shoal/shoal.json" --only beta --dry-run
SHOAL_SERVER=hostA check "ssh hostB shoal restart" $H restart --spec "$MH/shoals/t1shoal/shoal.json" --only alpha,beta --dry-run
# lifecycle: mint on home, launch each role on its own host
SHOAL_SERVER=hostA check "full restart here restarts this host's roles only: alpha, observer" $H restart --spec "$MH/shoals/t1shoal/shoal.json" --dry-run
SHOAL_SERVER=hostA check "remote role(s) gamma on hostB: not launched here" $H add-role --spec "$MH/shoals/t1shoal/shoal.json" --role "gamma,opencode,gamma,$DOC_B,fill=assigned,server=hostB" --dry-run
OUT=$(SHOAL_SERVER=hostA $H start --name mhs --home hostA --dir "$WORK" --role "ra,opencode,ra,$DOC_A,fill=assigned" --role "rb,claude,rb,$DOC_B,fill=assigned,server=hostB" --ledger-repo "$S/remote/ledgers.git" --ledger-dir "$A" --dry-run 2>&1 || true)
grep -q "remote role(s) rb on hostB: not launched here" <<<"$OUT" || fail "start launched or did not report the remote role :: $OUT"
grep -q "create + brief lane ra (ra-r1" <<<"$OUT" || fail "start did not launch the home role :: $OUT"
! grep -q "rb (rb-r1" <<<"$OUT" || fail "start would launch the remote role here :: $OUT"
SHOAL_SERVER=hostB check "REFUSED: --home hostA is not this host" $H start --name mhs --home hostA --dir "$WORK" --role "ra,opencode,ra,$DOC_A,fill=assigned" --ledger-repo "$S/remote/ledgers.git" --ledger-dir "$A" --dry-run
SHOAL_SERVER=hostA check "beta runs on hostB: its own watcher delivers this --wake" $H msg --shoal t1shoal --spec "$MH/shoals/t1shoal/shoal.json" --ledger-dir "$A" --no-sync --to beta --wake --text "remote wake"
SHOAL_SERVER=hostA python3 - "$SHOALD" "$MH/shoals/t1shoal/shoal.json" "$A" <<'PY3' || fail "host overview and name clash"
import sys
sys.path.insert(0, sys.argv[1]); sys.path.insert(0, sys.argv[1] + "/ui")
import json, health, ui_status, ui_herdr
from ledger import Ledger
p = sys.argv[2]
d = json.load(open(p)); d["ledger"]["dir"] = sys.argv[3]; json.dump(d, open(p, "w"))
doc = ui_status.shoal_doc(p, [], opencode_probe=False)
assert doc["multi_host"] and doc["home"] == "hostA", doc["home"]
assert {r["slug"]: r["host"] for r in doc["roles"]}["beta"] == "hostB"
txt = ui_status.render_text({"shoals": [doc]}, width=200)
assert "home hostA" in txt and "| host " in txt and "hostB" in txt, txt[:600]
narrow = ui_status.render_text({"shoals": [doc]}, width=70)
assert "| host" in narrow, narrow[:400]
assert ui_herdr.role_tokens_for(doc, doc["roles"][1])["host"] == "hostB"
# server names are labels: another machine beating under THIS host's name is a clash (detection, no registry)
led = Ledger("t1shoal", root=sys.argv[3], sync=False)
assert health.name_clash(health.ShoalSpec.load(p), led, 60) == []
led.append("shoal", "system", kind="host-beat", host="hostA", instance="feedfacecafe")
c = health.name_clash(health.ShoalSpec.load(p), Ledger("t1shoal", root=sys.argv[3], sync=False), 60)
assert [x["condition"] for x in c] == ["host-name-clash"] and "feedfacecafe" in c[0]["detail"], c
print("host overview and clash ok")
PY3
SHOAL_SERVER=hostB SHOAL_LEDGER_DIR="$MH" python3 - "$SHOALD" <<'PY3' || fail "doctor host profile"
import sys
sys.path.insert(0, sys.argv[1])
import doctor
p = doctor.host_profile()
assert p["ecosystems"] == {"opencode"} and p["home_of"] == [], p  # hostB runs only beta (opencode)
print("profile ok")
PY3
echo "== multi-host: per-host timer sets, Claude wake mode by host, host heartbeats"
for H_ in hostA hostB hostC; do
SHOAL_SERVER=$H_ python3 - "$SHOALD" "$MH/shoals/t1shoal/shoal.json" "$H_" <<'PY3' || fail "per-host jobs on $H_"
import sys
sys.path.insert(0, sys.argv[1])
import systemd
from roles import ShoalSpec
spec = ShoalSpec.load(sys.argv[2])
spec.features = {"health": True}
jobs = {j[0] for j in systemd.jobs_for(spec, sys.argv[2])}
want = {"hostA": {"health-a", "health-b", "lanes", "pipeline", "reconcile", "weekly-report"},  # home: alpha, observer + group duties
        "hostB": {"health-a", "health-b", "lanes"},  # beta (opencode) only: no group duty, no duplicated board reads
        "hostC": set()}[sys.argv[3]]
assert jobs == want, (sys.argv[3], jobs)
print(sys.argv[3], "jobs ok")
PY3
done
SHOAL_SERVER=hostA python3 - "$SHOALD" "$MH/shoals/t1shoal/shoal.json" "$A" <<'PY3' || fail "wake mode and host beats"
import sys
sys.path.insert(0, sys.argv[1])
import briefs, health
from ledger import Ledger
from roles import Role, ShoalSpec
spec = ShoalSpec.load(sys.argv[2])
spec.ledger = dict(spec.ledger, dir=sys.argv[3])
# relay-era keys (features.relay, health.relay_fallback) are ignored: direct wake is the one Claude path
spec.features = {"relay": True, "health": {"host_silent_min": 0, "relay_fallback": True}}
spec.roles.append(Role(slug="gamma", ecosystem="claude", title="gamma", doc="x", fill="assigned", session="gggg", server="hostB"))
assert briefs.arming(spec.role("alpha"), spec)[0] == "direct"  # home
assert briefs.arming(spec.role("gamma"), spec)[0] == "direct"  # another host: its own watcher resumes it there
spec.features["health"]["direct_wake"] = "dead"  # never stop a live worker: an in-session watcher wakes it
assert briefs.arming(spec.role("alpha"), spec)[0] == "exit"
spec.features["health"]["direct_wake"] = "off"  # the relay-era opt-out reads as the default
assert briefs.arming(spec.role("alpha"), spec)[0] == "direct"
spec.features["health"].pop("direct_wake")
led = Ledger("t1shoal", root=sys.argv[3], sync=False)
import config, os
os.environ["SHOAL_SERVER"] = "hostB"  # hostB's quorum beats; home is not hostB, so it judges nobody
assert health.host_beats(spec, led, {}, dry_run=False) == []
os.environ["SHOAL_SERVER"] = "hostA"
led = Ledger("t1shoal", root=sys.argv[3], sync=False)
got = health.host_beats(spec, led, {"host_beat": health.iso()}, dry_run=False)
assert [c["slug"] for c in got] == ["host:hostB"] and got[0]["condition"] == "host-silent", got
# control: a host that never beat is not judged (no false page when the feature arrives)
spec.roles.append(Role(slug="delta", ecosystem="opencode", title="delta", doc="x", fill="assigned", session="ses_d", server="hostC"))
assert [c["slug"] for c in health.host_beats(spec, Ledger("t1shoal", root=sys.argv[3], sync=False), {"host_beat": health.iso()})] == ["host:hostB"]
# a single-host shoal neither beats nor judges, with or without a recorded home
single = ShoalSpec.load(sys.argv[2]); single.home = ""
for r in single.roles: r.server = ""
assert health.host_beats(single, led, {}) == []
# a shared checkout that cannot take its upstream is a page; behind alone, on an idle host, is not
import subprocess, tempfile
with tempfile.TemporaryDirectory() as td:
    def run(cwd, *a):
        subprocess.run(["git", "-C", cwd, "-c", "user.email=t@example.invalid", "-c", "user.name=t", *a], check=True, capture_output=True)
    def put(cwd, name, text, msg=""):
        with open(os.path.join(cwd, name), "w") as fh:
            fh.write(text)
        if msg:
            run(cwd, "add", name); run(cwd, "commit", "-qm", msg)
    bare, a, b = (os.path.join(td, n) for n in ("r.git", "a", "b"))
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", bare], check=True)
    for c in (a, b):
        subprocess.run(["git", "clone", "-q", bare, c], check=True, capture_output=True)
    run(a, "checkout", "-qb", "main"); put(a, "f", "1", "one"); run(a, "push", "-q", "-u", "origin", "main")
    run(b, "pull", "-q", "origin", "main"); run(b, "branch", "-q", "--set-upstream-to=origin/main", "main")
    assert health.stuck_checkout(b) == ("", ""), "in step"
    put(a, "up", "upstream", "two"); run(a, "push", "-q")
    assert health.stuck_checkout(b) == ("", ""), "behind alone is not stuck"
    put(b, "up", "upstream")
    assert health.stuck_checkout(b) == ("", ""), "a local copy identical to upstream does not block"
    put(b, "up", "someone's work")
    state, detail = health.stuck_checkout(b)
    assert state == "blocked" and " up" in detail, (state, detail)
    os.unlink(os.path.join(b, "up"))
    put(b, "mine", "x", "local")
    state, detail = health.stuck_checkout(b)
    assert state == "diverged" and "1 ahead and 1 behind" in detail, (state, detail)
    gd = os.path.join(b, ".git", "rebase-merge"); os.mkdir(gd)
    assert health.stuck_checkout(b)[0] == "in-progress"
    os.rmdir(gd)
    assert health.stuck_checkout(os.path.join(td, "no-such-path")) == ("", ""), "not a checkout on this host"
    wc = ShoalSpec.load(sys.argv[2])
    assert health.watched_checkouts(wc) == [], "off when unset"
    wc.features = {"health": {"watch_checkouts": [b, os.path.join(td, "no-such-path")]}}
    got = health.watched_checkouts(wc, dry_run=True)
    assert [(c["condition"], c["key"]) for c in got] == [("checkout-stuck", "diverged:" + b)] and got[0]["slug"] == "host:hostA", got
    assert health.LADDERS["checkout-stuck"] == [(60, "page")]
single.home = "hostA"
n0 = len(led.events(reload=True))
assert health.host_beats(single, led, {}, dry_run=False) == [] and len(led.events(reload=True)) == n0, "beat written"
print("wake mode and host beats ok")
PY3
echo "== matcher transport: model calls go through openchamber, never the opencode CLI"
mkdir -p "$S/ocstub"
cat > "$S/ocstub/openchamber" <<'SH'
#!/bin/sh
case "$1 $2" in
  "session create") echo '{"status":"ok","sessionId":"ses_stub1"}' ;;
  "session send")   echo '{"status":"ok","sessionId":"ses_stub1","lastAssistantMessage":{"text":"{\"ok\": true}"}}' ;;
  *) echo "unexpected: $*" >&2; exit 2 ;;
esac
SH
printf '#!/bin/sh\necho CALLED >> "%s/opencode-called"; exit 9\n' "$S" > "$S/ocstub/opencode"
chmod +x "$S/ocstub/openchamber" "$S/ocstub/opencode"
PATH="$S/ocstub:$PATH" SHOAL_FAKE_OPENCHAMBER_API="$S/ocapi.log" python3 - "$SHOALD" "$S/mrun" <<'PY3' || fail "matcher openchamber transport"
import sys
sys.path.insert(0, sys.argv[1])
import matcher
assert matcher.openchamber_bin().endswith("ocstub/openchamber"), matcher.openchamber_bin()
assert matcher.call_opencode("opencode/x-free", "{}", sys.argv[2], 30) == '{"ok": true}'
big = "x" * (matcher.PROMPT_INLINE_MAX + 1)
assert matcher.call_opencode("opencode/x-free", big, sys.argv[2], 30) == '{"ok": true}'
assert open(sys.argv[2] + "/prompt.md").read() == big, "long prompt not written to the session dir"
print("transport ok")
PY3
[ ! -f "$S/opencode-called" ] || fail "the matcher invoked the opencode CLI"
# each run's session is deleted afterwards, through OpenChamber's server (never left in the store)
[ "$(grep -c '"method": "DELETE", "path": "/api/session/ses_stub1"' "$S/ocapi.log")" = 2 ] \
  || fail "matcher sessions not deleted after each run :: $(cat "$S/ocapi.log" 2>/dev/null)"

echo "== queue-order and transfer: the maintainer reorders a role's next items and moves items between roles"
VIEWPY='import sys; sys.path.insert(0, sys.argv[1])
from roles import ShoalSpec
from ledger import Ledger, View
spec = ShoalSpec.load(sys.argv[2]); v = View(Ledger(spec.sid, root=spec.ledger_dir, sync=False), spec)'
# a withdraw reaches a claimed offer (it read as claimed, and the fold booked it as a violation); the claim stays
expect 0 $H offer --shoal t1shoal --to alpha --item app#9703:fix >/dev/null
expect 0 $H claim --shoal t1shoal --slug alpha --item app#9703:fix >/dev/null
expect 0 $H withdraw --shoal t1shoal --item app#9703 --to alpha --reason "changed plan" >/dev/null
python3 - "$SHOALD" "$SPEC" <<PY2 || fail "withdraw of a claimed offer"
$VIEWPY
o = [o for o in v.by_item["app#9703"]][-1]
assert o["status"] == "withdrawn", o
assert any(c["item"].startswith("app#9703") for c in v.active("alpha")), "a withdraw never ends a claim"
assert not [w for e, w in v.violations if "9703" in str(e.get("item"))], v.violations
PY2
# transfer: refusals print one line per item, and nothing is written
check "REFUSED app#9709: nobody holds it or is offered it; offer it instead" \
  $H transfer --shoal t1shoal --item app#9709 --to beta --dry-run
check "REFUSED app#9703: observer has fill=none and never claims" \
  $H transfer --shoal t1shoal --item app#9703 --to observer
check "REFUSED app#9703: already held by alpha" $H transfer --shoal t1shoal --item app#9703 --to alpha
expect 1 $H transfer --shoal t1shoal --item app#9709 --to beta >/dev/null
# an item only offered moves at once: withdrawn from alpha, offered to beta, recorded done
expect 0 $H offer --shoal t1shoal --to alpha --item app#9702:review >/dev/null
check "OK app#9702:review: withdrawn from alpha, offered to beta (prio 30)" \
  $H transfer --shoal t1shoal --item app#9702:review --to beta --prio 30 --dry-run
expect 0 $H transfer --shoal t1shoal --item app#9702:review --to beta --prio 30 >/dev/null
# a held item moves through its holder: a request to alpha, pending until its handoff lands
expect 0 $H claim --shoal t1shoal --slug alpha --item app#9701:fix >/dev/null
check "pending the holder's handoff" $H transfer --shoal t1shoal --item app#9701 --to beta --prio 15 --note "beta owns this area"
check "TRANSFER app#9701 to beta" $H next --shoal t1shoal --slug alpha --peek
expect 0 $H claim --shoal t1shoal --slug alpha --item app#9704:fix >/dev/null
expect 0 $H transfer --shoal t1shoal --item app#9704 --to beta >/dev/null
python3 - "$SHOALD" "$SPEC" <<PY2 || fail "transfer records"
$VIEWPY
t = {x["item"]: x for x in v.transfers()}
assert t["app#9702"]["status"] == "done" and t["app#9702"]["from"] == "alpha", t
assert t["app#9701"]["status"] == "pending" and t["app#9701"]["req"], t
assert [o["item"] for o in v.queue("alpha") if "9702" in o["item"]] == [], "withdrawn from alpha"
assert [o["prio"] for o in v.queue("beta") if "9702" in o["item"]] == [30], v.queue("beta")
assert any(m["id"] == t["app#9701"]["req"] for m in v.unacked_to("alpha")), "the holder's request is chased"
assert {x["item"] for x in v.transfers("gamma")} == set()
PY2
expect 0 $H release --shoal t1shoal --slug alpha --item app#9701 --kind handoff --to beta --prio 15 >/dev/null
expect 0 $H release --shoal t1shoal --slug alpha --item app#9704 --kind blocked --reason "needs a design call" >/dev/null
python3 - "$SHOALD" "$SPEC" <<PY2 || fail "transfer outcomes"
$VIEWPY
t = {x["item"]: x for x in v.transfers("beta")}
assert t["app#9701"]["status"] == "done", t
assert t["app#9704"]["status"] == "failed" and t["app#9704"]["release"]["kind"] == "blocked", t
assert [o["prio"] for o in v.queue("beta") if "9701" in o["item"]] == [15], "the handoff carries the transfer's prio"
assert not {t[k]["req"] for k in t} & {m["id"] for m in v.unacked_to("alpha")}, "a settled transfer is not chased"
PY2
# queue-order: each item checked, one line per refusal, nothing written when any is refused
expect 0 $H claim --shoal t1shoal --slug alpha --item app#9705:fix >/dev/null
expect 0 $H claim --shoal t1shoal --slug alpha --item app#9706:fix >/dev/null
expect 0 $H offer --shoal t1shoal --to alpha --item app#9707:fix >/dev/null
expect 0 $H claim --shoal t1shoal --slug beta --item app#9708:fix >/dev/null
OUT=$($H queue-order --shoal t1shoal --to alpha --next "app#9706:fix,app#9708:fix,app#9702,app#9706" --dry-run 2>&1 || true)
grep -q "^OK app#9706:fix: held by alpha, position 1" <<<"$OUT" || fail "queue-order OK row :: $OUT"
grep -q "^REFUSED app#9708:fix: held by beta; transfer it first" <<<"$OUT" || fail "queue-order held row :: $OUT"
grep -q "^REFUSED app#9702: offered to beta; transfer it first" <<<"$OUT" || fail "queue-order offered row :: $OUT"
grep -q "^REFUSED app#9706: listed twice" <<<"$OUT" || fail "queue-order duplicate row :: $OUT"
expect 1 $H queue-order --shoal t1shoal --to alpha --next "app#9708" >/dev/null
check "next=(none)" $H queue-order --shoal t1shoal --to alpha --next= --dry-run
check "queue-order to alpha: next=app#9706:fix,app#9705:fix" \
  $H queue-order --shoal t1shoal --to alpha --next "app#9706:fix,app#9705:fix" --note "first try"
check "queue-order to alpha" $H queue-order --shoal t1shoal --to alpha --next "app#9707:fix,app#9706:fix"
NOUT=$($H next --shoal t1shoal --slug alpha --peek 2>&1)
grep -q "^QUEUE-ORDER .* next=app#9707:fix,app#9706:fix" <<<"$NOUT" || fail "next shows the pending order :: $NOUT"
[ "$(grep -c '^QUEUE-ORDER' <<<"$NOUT")" = 1 ] || fail "a replaced order still shows :: $NOUT"
OID=$(python3 - "$SHOALD" "$SPEC" <<PY2
$VIEWPY
o = v.queue_orders("alpha")
assert [x["status"] for x in o] == ["superseded", "pending"], o
assert o[0]["id"] not in {m["id"] for m in v.unacked_to("alpha")}, "a replaced order leaves the unacked-work ladder"
print(o[-1]["id"])
PY2
) || fail "queue orders"
expect 0 $H msg --shoal t1shoal --by alpha --to user --kind ack --re "$OID" --text applied >/dev/null
python3 - "$SHOALD" "$SPEC" <<PY2 || fail "an acked order is applied"
$VIEWPY
assert v.queue_orders("alpha")[-1]["status"] == "applied", v.queue_orders("alpha")
PY2
$H next --shoal t1shoal --slug alpha --peek 2>&1 | grep -q "^QUEUE-ORDER" && fail "an applied order still shows in next"
# one second's events fold by writer, so a holder's release in the transfer's own second folds first
python3 - "$SHOALD" "$S/sametick" <<'PY2' || fail "a release in the transfer's own second settles it"
import os, sys
sys.path.insert(0, sys.argv[1])
import ledger
from ledger import Ledger, View
ledger.iso = lambda dt=None: "2026-11-08T00:00:00Z"
led = Ledger("sametick", root=sys.argv[2], sync=False)
os.makedirs(led.ldir, exist_ok=True)
led.append("alpha", "claim", item="app#9790", _commit=False)
led.append("user", "system", event="transfer", item="app#9790", to="beta", prio=20, req="rq1",
           _commit=False, **{"from": "alpha"})
led.append("alpha", "release", item="app#9790", kind="handoff", to="beta", _commit=False)
t = View(led, None).transfers()
assert [x["status"] for x in t] == ["done"], t
PY2
echo "queue-order and transfer ok"

echo "== signed grants: sign, verify at read, forged excluded, revoke and expiry"
if command -v ssh-keygen >/dev/null; then
  ssh-keygen -q -t ed25519 -N "" -f "$S/grantkey"
  printf 'shoal-grant %s\n' "$(cat "$S/grantkey.pub")" > "$S/allowed_signers"
  export SHOAL_GRANT_KEY="$S/grantkey" SHOAL_GRANT_ALLOWED_SIGNERS="$S/allowed_signers"
  python3 -c "
import json
p = '$SPEC'; d = json.load(open(p)); d.setdefault('features', {})['grants'] = True
open(p, 'w').write(json.dumps(d, indent=2) + '\n')"
  check "would sign and append" $H grant --shoal t1shoal --spec "$SPEC" --role alpha --scope "fixture reviews" --kind review-comment --dry-run --no-sync
  GOUT=$($H grant --shoal t1shoal --spec "$SPEC" --role alpha --scope "fixture reviews" --kind review-comment --no-sync 2>&1) || fail "grant :: $GOUT"
  grep -q "(signed, verified)" <<<"$GOUT" || fail "grant not verified :: $GOUT"
  GID=$(grep -oE '^grant [0-9a-f]+' <<<"$GOUT" | cut -d' ' -f2)
  [ -n "$GID" ] || fail "no grant id :: $GOUT"
  check "active" $H grants --shoal t1shoal --spec "$SPEC" --no-sync
  check "| grants |" $H show --shoal t1shoal --spec "$SPEC" --no-sync
  check "STANDING GRANTS" $H next --shoal t1shoal --spec "$SPEC" --slug alpha --peek --no-sync
  check "fixture reviews" $H next --shoal t1shoal --spec "$SPEC" --slug alpha --peek --no-sync
  printf -- '- Fixture review comments [grant: review-comment]\n' >> "$DOC_A"
  python3 - "$SHOALD" "$SPEC" <<'PY2' || fail "brief grants block"
import sys; sys.path.insert(0, sys.argv[1])
from roles import ShoalSpec
import briefs
spec = ShoalSpec.load(sys.argv[2])
b = briefs.brief_for(spec.role("alpha"), spec)
assert "STANDING GRANTS" in b and "fixture reviews" in b, b
assert "Fixture review comments [grant: review-comment]" not in b, "covered item still asked"
print("brief ok")
PY2
  python3 - "$SHOALD" "$SPEC" <<'PY2' || fail "forged grant setup"
import sys; sys.path.insert(0, sys.argv[1])
from roles import ShoalSpec
from ledger import Ledger
spec = ShoalSpec.load(sys.argv[2])
led = Ledger(spec.sid, root=spec.ledger_dir, sync=False)
led.append("user", "grant", state="granted", role="alpha", scope="FORGED SCOPE", _commit=False)
led.append("alpha", "grant", state="granted", role="alpha", scope="ROLE FORGED", _commit=False)
led.commit_own("user", "fixture forged grant"); led.commit_own("alpha", "fixture role grant")
print("forged written")
PY2
  check "unverified" $H grants --shoal t1shoal --spec "$SPEC" --no-sync
  $H next --shoal t1shoal --spec "$SPEC" --slug alpha --peek --no-sync 2>&1 | grep -q "FORGED SCOPE" \
    && fail "an unverified grant reached next"
  check "does not verify" $H ledger-lint --shoal t1shoal --spec "$SPEC" --no-sync
  check "only the maintainer grants" $H ledger-lint --shoal t1shoal --spec "$SPEC" --no-sync
  OUT=$(SHOAL_GRANT_ALLOWED_SIGNERS="$S/none" $H grants --shoal t1shoal --spec "$SPEC" --no-sync 2>&1 || true)
  grep -q "unverified" <<<"$OUT" && ! grep -q " active " <<<"$OUT" || fail "missing signers not fail-closed :: $OUT"
  EOUT=$($H grant --shoal t1shoal --spec "$SPEC" --role beta --scope "already over" --until 2020-01-01T00:00:00Z --no-sync 2>&1) || fail "expired grant write :: $EOUT"
  check "expired" $H grants --shoal t1shoal --spec "$SPEC" --no-sync
  check "revoked $GID" $H revoke --shoal t1shoal --spec "$SPEC" --id "$GID" --no-sync
  check "revoked" $H grants --shoal t1shoal --spec "$SPEC" --no-sync
  $H next --shoal t1shoal --spec "$SPEC" --slug alpha --peek --no-sync 2>&1 | grep -q "STANDING GRANTS" \
    && fail "a revoked grant still shows in next"
  echo "grants ok"
else
  echo "SKIP grants (no ssh-keygen)"
fi
rm -rf /tmp/t1shoal
echo "SELFTEST PASS ($S)"
