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
# synthetic board, board order = priority: open interpretune items, one out of alpha's scope,
# one closed, one in a skipped status
shoal_board_fixture "$S/board.json" <<'JSON'
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
DOC_A=$(shoal_doc alpha); DOC_B=$(shoal_doc beta); DOC_O=$(shoal_doc observer)
cat > "$SPEC" <<JSON
{"shoal":"t1shoal","dir":"$WORK","incarnation":0,"repos":["speediedan/interpretune"],
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
expect 0 $H offer --shoal t1shoal --to alpha --item interpretune#372:fix --base interpretune@3ac6fdf9 --note "phase 1 only" >/dev/null
check "interpretune#372:fix" $H show --shoal t1shoal --slug alpha
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
expect 0 $H offer --shoal t1shoal --to alpha --item interpretune#600:fix --base interpretune@3ac6fdf9 --prio 10 >/dev/null
expect 1 $H offer --shoal t1shoal --to alpha --item interpretune#427:fix --by beta >/dev/null      # a role never offers
expect 1 $H offer --shoal t1shoal --to beta --item interpretune#427:fix --by beta >/dev/null       # not even to itself
expect 1 $H offer --shoal t1shoal --to alpha --item interpretune#427:fix --by observer >/dev/null  # legacy can_fill_others is ignored
check "only user offers work" $H offer --shoal t1shoal --to alpha --item interpretune#427:fix --by observer
expect 0 $H offer --shoal t1shoal --to alpha --item interpretune#427:fix --base interpretune@3ac6fdf9 >/dev/null
expect 0 $H offer --shoal t1shoal --to '*' --item interpretune#501:scope --base interpretune@3ac6fdf9 >/dev/null
check "already offered" $H offer --shoal t1shoal --to alpha --item interpretune#600:fix
check "pool offers (to=\*): interpretune#501:scope" $H show --shoal t1shoal
echo "== 3 claim race across clones on the pool item #501 (B stale, A lands first)"
expect 4 $H claim --shoal t1shoal --slug beta --item interpretune#600:fix --ledger-dir "$B" >/dev/null   # offered to alpha only: refused
expect 0 $H sync --shoal t1shoal --ledger-dir "$B" >/dev/null      # B fetches once, then goes stale
expect 0 $H claim --shoal t1shoal --slug beta --item interpretune#501:scope --ledger-dir "$B" --no-sync >/dev/null
expect 0 $H claim --shoal t1shoal --slug alpha --item interpretune#501:scope --base interpretune@3ac6fdf9 >/dev/null
expect 0 $H sync --shoal t1shoal --ledger-dir "$B" >/dev/null
expect 1 $H ledger-lint --shoal t1shoal >/dev/null            # E2 contested
check "E2 contested claim on interpretune#501" $H ledger-lint --shoal t1shoal
check "CLAIM-LOST interpretune#501: alpha landed first" timeout 20 $H ledger-watch --shoal t1shoal --slug beta --wake exit --interval 60 --ledger-dir "$B" --no-board
expect 0 $H release --shoal t1shoal --slug beta --item interpretune#501 --kind lost-race --reason "alpha landed first" --ledger-dir "$B" >/dev/null
expect 0 $H ledger-lint --shoal t1shoal >/dev/null
expect 4 $H claim --shoal t1shoal --slug beta --item interpretune#501:scope --ledger-dir "$B" >/dev/null   # held by alpha
echo "== 4 done, withdraw, decline, msg + ack, ask"
expect 0 $H claim --shoal t1shoal --slug alpha --item interpretune#600:fix >/dev/null
expect 0 $H done --shoal t1shoal --slug alpha --item interpretune#600:fix --kind merge --url https://github.com/speediedan/interpretune/pull/601 >/dev/null
expect 0 $H done --shoal t1shoal --slug alpha --item interpretune#501:scope >/dev/null
expect 4 $H claim --shoal t1shoal --slug beta --item interpretune#501:scope --ledger-dir "$B" >/dev/null   # done
expect 1 $H withdraw --shoal t1shoal --item interpretune#427 --to alpha --reason reassign --by beta >/dev/null   # roles never withdraw
expect 0 $H withdraw --shoal t1shoal --item interpretune#427 --to alpha --reason reassign >/dev/null
expect 0 $H offer --shoal t1shoal --to '*' --item interpretune#554:scope --base interpretune@3ac6fdf9 >/dev/null
expect 0 $H claim --shoal t1shoal --slug beta --item interpretune#554:scope --ledger-dir "$B" >/dev/null
expect 0 $H release --shoal t1shoal --slug beta --item interpretune#554 --kind decline --reason "out of lane" --retry "after #600" --ledger-dir "$B" >/dev/null
check "interpretune#554:scope" $H show --shoal t1shoal --slug alpha     # re-opened for others
check "declined: interpretune#554" $H show --shoal t1shoal --slug beta  # never re-offered to beta
expect 0 $H msg --shoal t1shoal --by user --to alpha --text "review adapter#39 next" --kind instruct --ack >/dev/null
MID=$(python3 -c "import json;[print(json.loads(l)['id']) for l in open('$A/shoals/t1shoal/ledger/user.jsonl') if '\"msg\"' in l]" | tail -1)
expect 0 $H msg --shoal t1shoal --by alpha --to user --text ack --kind ack --re "$MID" >/dev/null
expect 0 $H msg --shoal t1shoal --by beta --to user --text "need merge grant #602" --kind ask --ledger-dir "$B" >/dev/null
check "msgs to user" $H show --shoal t1shoal
# an outside seat (a standalone session) may send plain mail to one role: nothing else
expect 0 $H msg --shoal t1shoal --by ext:infra-expansion --to alpha --text "fsdp2 handoff: see the doc" >/dev/null
expect 1 $H msg --shoal t1shoal --by ext:infra-expansion --to '*' --text "x" >/dev/null
expect 1 $H msg --shoal t1shoal --by ext:infra-expansion --to alpha --text "x" --wake >/dev/null
expect 1 $H msg --shoal t1shoal --by ext:infra-expansion --to alpha --text "x" --kind instruct >/dev/null
expect 1 $H msg --shoal t1shoal --by ext:Bad_Name --to alpha --text "x" >/dev/null
expect 1 $H offer --shoal t1shoal --by ext:infra-expansion --to alpha --item interpretune#555:fix --base interpretune@3ac6fdf9 >/dev/null
check "ext:infra-expansion" $H show --shoal t1shoal --slug alpha
# the reply path: a role mails the outside seat, which reads it with show --slug ext:<name>
expect 0 $H msg --shoal t1shoal --by alpha --to ext:infra-expansion --text "reply: defaults are fine" >/dev/null
check "reply: defaults are fine" $H show --shoal t1shoal --slug ext:infra-expansion
expect 1 $H msg --shoal t1shoal --by alpha --to ext:infra-expansion --text "x" --kind instruct >/dev/null
expect 1 $H msg --shoal t1shoal --by user --to ext:infra-expansion --text "x" --wake >/dev/null
echo "== 5 board fill (synthetic board fixture, no GitHub read)"
check "still has" $H fill --shoal t1shoal --slug alpha            # #372 offered + #554 pool open
expect 0 $H claim --shoal t1shoal --slug alpha --item interpretune#372:fix >/dev/null
expect 0 $H done --shoal t1shoal --slug alpha --item interpretune#372:fix >/dev/null
expect 0 $H withdraw --shoal t1shoal --item interpretune#554 >/dev/null
check "interpretune#9001" $H fill --shoal t1shoal --slug alpha --limit 3
FILL=$($H fill --shoal t1shoal --slug alpha --limit 5)
if grep -q "interpretune#600\|interpretune#372\|interpretune#501" <<<"$FILL"; then fail "taken/withdrawn items surfaced as candidates"; fi
if grep -q "other#39\|interpretune#9002\|interpretune#9003" <<<"$FILL"; then fail "out-of-scope, closed or Done items surfaced :: $FILL"; fi
grep -q "interpretune#9004" <<<"$FILL" && grep -q "interpretune#9005" <<<"$FILL" || fail "board order candidates missing :: $FILL"
echo "== 6 exit-wake watcher: arms, wakes on a new offer, exits 0, sidecar bound"
( set +e; timeout 60 $H ledger-watch --shoal t1shoal --slug beta --wake exit --interval 2 --ledger-dir "$B" --no-board --session ses_bbbb > "$S/watch.out" 2>&1; echo "rc=$?" >> "$S/watch.out" ) &
sleep 4
expect 0 $H offer --shoal t1shoal --to beta --item interpretune#450:fix --base interpretune@3ac6fdf9 >/dev/null
wait
grep -q "LEDGER-WAKE beta@t1shoal" "$S/watch.out" || fail "watcher did not wake: $(cat "$S/watch.out")"
grep -q "rc=0" "$S/watch.out" || fail "watcher exit code: $(tail -1 "$S/watch.out")"
grep -q "session_id=ses_bbbb" /tmp/t1shoal/ledger-watch-beta.state || fail "sidecar not bound"
check "LEDGER-WAKE-TEST" timeout 20 $H ledger-watch --shoal t1shoal --slug beta --wake exit --interval 60 --ledger-dir "$B" --no-board --test
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
check "ledger-watch --shoal t1shoal --slug alpha --wake exit" $H brief --spec "$SPEC" --slug alpha
check "wake send:" $H brief --spec "$SPEC" --slug beta
check "prove the pid with TWO separate commands: first .cat /tmp/t1shoal/ledger-watch-alpha.state." $H brief --spec "$SPEC" --slug alpha
if $H brief --spec "$SPEC" --slug alpha | grep -qE 'ps -p \$\(|ps -p plus sidecar'; then fail "brief combines the pid proof"; fi
echo "== 8 flattened only: a role-written offer is a projection violation; removed transports are gone"
expect 0 $H ledger-lint --shoal t1shoal >/dev/null
python3 -c "import sys; sys.path.insert(0, '$SHOALD'); from ledger import Ledger; Ledger('t1shoal', root='$A').append('beta', 'offer', to='alpha', item='interpretune#777:fix', base='interpretune@3ac6fdf9', prio=1)" >/dev/null
expect 1 $H ledger-lint --shoal t1shoal >/dev/null
check "E1 .* beta offer interpretune#777: beta may not offer to alpha" $H ledger-lint --shoal t1shoal
if $H show --shoal t1shoal --slug alpha | grep -q "interpretune#777"; then fail "role-written offer honoured by the projection"; fi
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
led.append(SYSTEM_WRITER, "offer", to="alpha", item="interpretune#779:fix", base="interpretune@3ac6fdf9", prio=1)
v = View(Ledger("t1shoal", root=sys.argv[3]), ShoalSpec.load(sys.argv[2]))
items = {o["item"].split(":")[0] for o in v.offers.values() if o["status"] == "open"}
assert "interpretune#779" in items, items
bad = [x for x in v.violations if "#779" in str(x)]
assert not bad, bad
PY2
[ "$(SHOAL_DATA_ROOT="$S/new-root" python3 -c "import sys; sys.path.insert(0, '$SHOALD'); import config; print(config.data_root())")" = "$S/new-root" ] \
  || fail "SHOAL_DATA_ROOT not honoured"
echo "== layout: shoals/<id>/shoal.json; next_max; wake relay slug"
python3 - "$SHOALD" "$S/layout" <<'PY3' || fail "layout / next_max / wake relay checks"
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
from roles import Role, is_wake_relay
assert is_wake_relay(Role(slug="wake-relay", ecosystem="claude", title="", doc="", fill="none"))
assert not is_wake_relay(Role(slug="wake-relay", ecosystem="claude", title="", doc="", fill="self"))
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
SHOAL_SERVER=hostA check "REFUSED: beta run(s) on another host" $H restart --spec "$MH/shoals/t1shoal/shoal.json" --only beta --dry-run
SHOAL_SERVER=hostA check "ssh hostB shoal restart" $H restart --spec "$MH/shoals/t1shoal/shoal.json" --only alpha,beta --dry-run
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
spec.features = {"relay": True, "health": {"host_silent_min": 0}}
spec.roles.append(Role(slug="wake-relay", ecosystem="claude", title="wake-relay", doc="x", fill="none", session="rrrr"))
spec.roles.append(Role(slug="gamma", ecosystem="claude", title="gamma", doc="x", fill="assigned", session="gggg", server="hostB"))
assert briefs.arming(spec.role("alpha"), spec)[0] == "relay"  # same host as the relay (home)
assert briefs.arming(spec.role("gamma"), spec)[0] == "exit"   # the relay cannot reach another host's session
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
# a single-host shoal neither beats nor judges
single = ShoalSpec.load(sys.argv[2]); single.home = ""
for r in single.roles: r.server = ""
assert health.host_beats(single, led, {}) == []
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
PATH="$S/ocstub:$PATH" python3 - "$SHOALD" "$S/mrun" <<'PY3' || fail "matcher openchamber transport"
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
rm -rf /tmp/t1shoal
echo "SELFTEST PASS ($S)"
