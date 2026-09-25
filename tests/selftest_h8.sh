#!/usr/bin/env bash
# H8 selftest (architecture doc §4.1, §11): semantic matcher (fallback, switch record, cache),
# shadow vs live ranking, effective priority, the aux lane's minmax + unblocker ranking and
# AUX header, aux-init, `digest --compare`, `upgrade`. Isolated: scratch spec dir and SHOAL_DATA_ROOT,
# a local bare ledger remote, synthetic board items (and a board fixture behind every board
# read), a fake model behind the `command` backend, synthetic start docs and a scratch working
# dir and host config, so no network, no board read, no model call and no design-repo read.
set -uo pipefail
TESTD="$(cd "$(dirname "$0")" && pwd)"
HAGD="$(cd "$TESTD/../shoal" && pwd)"
. "$TESTD/selftest_lib.sh"
S="${1:-${TMPDIR:-/tmp}/shoal-h8-selftest-$$}"
rm -rf "$S" /tmp/h8selftest; mkdir -p "$S/root" "$S/data" "$S/remote"
hag_test_setup "$S"
ROOT="$S/root"
HAGID=h8selftest; SPEC="$ROOT/$HAGID/hag.json"; A="$S/cloneA"
export SHOAL_SPEC="$SPEC"
H="python3 $HAGD/cli.py"; export HAGD PYTHONPATH="$HAGD"
fail() { echo "SELFTEST FAIL: $*" >&2; exit 1; }
check() { local pat=$1; shift; local out; out=$("$@" 2>&1 || true); grep -q -- "$pat" <<<"$out" || fail "expected '$pat' in: $* :: $(tail -c 600 <<<"$out")"; }

git init -q --bare "$S/remote/q.git"; git clone -q "$S/remote/q.git" "$A" 2>/dev/null
( cd "$A" && git commit -q --allow-empty -m init && git push -q origin HEAD 2>/dev/null )
mkdir -p "$ROOT/$HAGID"
DOC=$(hag_doc h8role)

# fake models: `good` scores by keyword and counts its calls; `bad` is a rotated-out model
cat > "$S/good.py" <<'PY'
import json, re, sys
p = sys.stdin.read()
if "single word ok" in p:
    print("ok"); sys.exit(0)
open(sys.argv[1], "a").write("call\n")
out = []
for m in re.finditer(r"^(\S+#\d+) \| pos \d+ \| ([^|]*)\|", p, re.M):
    t = m.group(2)
    out.append({"item": m.group(1), "fits": {"tl": 3 if "TL4" in t else 0, "jl": 3 if "jlens" in t else 0,
                                             "up": 3 if "upstream" in t else 0},
                "class": "blocked" if "BLOCKED" in t else "workable", "blocker": "", "why": "fake"})
print("here you go: " + json.dumps({"items": out}))
PY
printf 'import sys\nsys.stderr.write("Upstream request failed: Model is unavailable.")\nsys.exit(1)\n' > "$S/bad.py"
GOOD="python3 $S/good.py $S/calls"; BAD="python3 $S/bad.py"

cat > "$SPEC" <<JSON
{"hag":"$HAGID","dir":"$WORK","incarnation":0,"repos":["speediedan/interpretune"],
 "ledger":{"repo":"$S/remote/q.git","dir":"$A"},
 "features":{"digest":true,"workwake":true,
   "matcher":{"backend":"command","model":"$BAD","fallbacks":[{"backend":"command","model":"$GOOD"}],
              "discover_free":false,"live":false}},
 "sources":[{"kind":"github-project","owner":"example","number":2}],
 "roles":[
  {"slug":"tl","ecosystem":"claude","title":"tl","doc":"$DOC","fill":"self","session":"aaaa1111",
   "context":{"brief":"Upgrades interpretune to TL4.","affinity":{"keywords":{"hygiene":3.0}},"never":{}}},
  {"slug":"jl","ecosystem":"opencode","title":"jl","doc":"$DOC","fill":"self","session":"ses_jl",
   "context":{"brief":"J-lens support.","never":{}}},
  {"slug":"up","ecosystem":"claude","title":"up","doc":"$DOC","fill":"self","session":"",
   "context":{"brief":"Files upstream issues.","never":{}}},
  {"slug":"hag-aux","ecosystem":"opencode","title":"hag-aux","doc":"$DOC","fill":"self","session":"ses_aux","aux":true}
 ],"generations":[{"gen":"r1","incarnation":0,"started_utc":"2026-09-22T00:00:00Z","ended_utc":"","sessions":{},"run":{}}]}
JSON
$H ledger-init --spec "$SPEC" >/dev/null
$H queue --shoal $HAGID --spec "$SPEC" --slug tl --active interpretune#1:fix --no-sync >/dev/null \
  || fail "tl queue write failed"

cat > "$S/items.py" <<'PY'
def items():
    def it(n, title, **kw):
        d = {"pos": n, "item": f"interpretune#{n}", "repo": "speediedan/interpretune", "number": n, "kind": "issue",
             "title": title, "state": "OPEN", "status": "", "labels": [], "excerpt": title, "blocked_by": []}
        d.update(kw)
        return d
    return [it(1, "TL4 bridge removal", blocked_by=["interpretune#5"]), it(2, "jlens readout"),
            it(3, "comment hygiene sweep"), it(4, "docs wide tables"), it(5, "small fix the migration lane waits on"),
            it(6, "upstream filing for a parked lane"), it(7, "BLOCKED on a release")]
PY
# the same synthetic items behind every board read the CLI makes (no GitHub read)
(cd "$S" && python3 -c "import json, items; print(json.dumps(items.items()))") | hag_board_fixture "$S/board.json"

echo "== 1 matcher: rotated-out model falls back, shadow keeps the live ranking, aux reads semantic"
python3 - "$SPEC" "$S" <<'PY' || fail "shadow build"
import json, os, sys
sys.path.insert(0, sys.argv[2])
from items import items
import digest, matcher
from roles import HagSpec
from ledger import Ledger, View
spec = HagSpec.load(sys.argv[1]); view = View(Ledger(spec.hag, root=spec.ledger_dir, sync=False), spec)
doc = digest.build(spec, view, items=items())
m = doc["matcher"]
assert doc["ranking"] == "keyword" and doc["shadow"]["ranking"] == "semantic", doc["ranking"]
assert m["calls"] == 1 and m["unscored"] == 0, m
assert any("skip command" in n and "unavailable" in n.lower() for n in m["notes"]), m["notes"]
assert "good.py" in (m["model"] or ""), m["model"]
sem = doc["shadow"]
# effective priority: #5 blocks tl's active #1 -> lifted; #7 blocked -> sunk
assert sem["items"]["interpretune#5"]["eff_pos"] == 0.5 and sem["items"]["interpretune#7"]["eff_pos"] == 14.0
# lanes rank by fit: jl sees #2 only; tl's own item is taken, and its keyword 'hygiene' weight is ignored
assert [r["item"] for r in sem["roles"]["jl"]] == ["interpretune#2"], sem["roles"]["jl"]
assert sem["roles"]["tl"] == [], sem["roles"]["tl"]
# aux: unblocker first, then minmax over ACTIVE lanes (up is parked, so #6 is aux work); #7 blocked, #2 jl's
aux = [r["item"] for r in sem["roles"]["hag-aux"]]
assert aux[0] == "interpretune#5" and "unblocks" in sem["roles"]["hag-aux"][0]["why"], aux
assert set(aux) == {"interpretune#5", "interpretune#3", "interpretune#4", "interpretune#6"}, aux
# in shadow the live doc still carries the keyword ranking for lanes, the semantic one for aux
assert doc["roles"]["hag-aux"] == sem["roles"]["hag-aux"]
assert any(r["item"] == "interpretune#3" for r in doc["roles"]["tl"]), doc["roles"]["tl"]
digest.write(spec, doc)
assert os.path.exists(digest.semantic_path(spec.hag))
PY
[ "$(wc -l < "$S/calls")" = 1 ] || fail "expected one scoring call"

echo "== 2 matcher: the cache makes a quiet board free; a stale active model is switched and recorded"
python3 - "$SPEC" "$S" <<'PY' || fail "cache/switch"
import json, os, sys
sys.path.insert(0, sys.argv[2])
from items import items
import digest, matcher
from roles import HagSpec
from ledger import Ledger, View
spec = HagSpec.load(sys.argv[1]); view = View(Ledger(spec.hag, root=spec.ledger_dir, sync=False), spec)
doc = digest.build(spec, view, items=items())
assert doc["matcher"]["calls"] == 0 and doc["matcher"]["unscored"] == 0, doc["matcher"]
its = items(); its[2]["title"] = "comment hygiene sweep, rescoped"
st = matcher.load_state(spec.hag); st["active"] = {"backend": "command", "model": spec.features["matcher"]["model"]}
matcher.save_state(spec.hag, st)
doc = digest.build(spec, view, items=its)
assert doc["matcher"]["calls"] == 1 and doc["matcher"]["scored_now"] == 1, doc["matcher"]
assert any("matcher model switched" in n for n in doc["matcher"]["notes"]), doc["matcher"]["notes"]
evs = Ledger(spec.hag, root=spec.ledger_dir, sync=False).events(reload=True)
assert any(e.get("type") == "system" and e.get("kind") == "matcher-model" for e in evs), "no switch event"
assert any(e.get("type") == "msg" and e.get("to") == "user" and "[matcher]" in e.get("text", "") for e in evs), "no page"
PY

echo "== 3 matcher: live makes the semantic ranking the one roles read; absent matcher changes nothing"
python3 - "$SPEC" "$S" <<'PY' || fail "live/absent"
import copy, json, os, sys
sys.path.insert(0, sys.argv[2])
from items import items
import digest
from roles import HagSpec
from ledger import Ledger, View
spec = HagSpec.load(sys.argv[1]); view = View(Ledger(spec.hag, root=spec.ledger_dir, sync=False), spec)
live = copy.deepcopy(spec); live.features["matcher"]["live"] = True
doc = digest.build(live, view, items=items())
assert doc["ranking"] == "semantic" and "shadow" not in doc and doc["roles"]["tl"] == [], doc["ranking"]
off = copy.deepcopy(spec); off.features.pop("matcher")
doc = digest.build(off, view, items=items())
assert doc["ranking"] == "keyword" and "matcher" not in doc and "shadow" not in doc
assert "eff_pos" not in doc["items"]["interpretune#5"], "effective priority leaked into a matcher-less HAG"
PY

echo "== 4 candidates, next AUX header, compare"
check "BOARD interpretune#5:fix" $H digest --shoal $HAGID --spec "$SPEC" --slug hag-aux --no-sync
check "aux:max-fit=0" $H digest --shoal $HAGID --spec "$SPEC" --slug hag-aux --semantic --no-sync
OUT=$($H next --shoal $HAGID --spec "$SPEC" --slug hag-aux --peek --no-sync 2>&1)
grep -q "AUX: you are the residual lane. Stay out of: tl (Upgrades interpretune to TL4); jl (J-lens support)" <<<"$OUT" \
  || fail "AUX header :: $OUT"
grep -q "Parked lanes whose work is yours meanwhile: up" <<<"$OUT" || fail "parked lane not named :: $OUT"
grep -q "BOARD interpretune#5:fix" <<<"$OUT" || fail "aux next lacks the unblocker :: $OUT"
check "SWITCH BAR:" $H digest --shoal $HAGID --spec "$SPEC" --compare --no-sync

echo "== 5 aux-init / role token / validation"
python3 - "$SPEC" "$S/spec2.json" <<'PY'
import json, sys; d = json.load(open(sys.argv[1])); d["roles"] = [r for r in d["roles"] if r["slug"] != "hag-aux"]
json.dump(d, open(sys.argv[2], "w"))
PY
check "\[dry-run\] aux-init $HAGID: doc $S/data/$HAGID/roles/hag-aux/plan_state.md; role ADD hag-aux" \
  $H aux-init --shoal $HAGID --spec "$S/spec2.json" --dry-run
check "role ADD hag-aux" $H aux-init --shoal $HAGID --spec "$S/spec2.json"
grep -q "residual lane of HAG \`$HAGID\`" "$S/data/$HAGID/roles/hag-aux/plan_state.md" || fail "aux doc not written"
python3 - "$S/spec2.json" <<'PY' || fail "aux role not added"
import json, sys; r = [x for x in json.load(open(sys.argv[1]))["roles"] if x["slug"] == "hag-aux"][0]
assert r["aux"] is True and r["fill"] == "self", r
PY
check "\[dry-run\] aux-launch $HAGID: hag-aux (hag-aux-r1) opencode" $H aux-launch --shoal $HAGID --spec "$S/spec2.json" --dry-run
(cd "$HAGD" && python3 -c "
import cli
r = cli.parse_role('x,opencode,x,/d.md,aux,fill=assigned')
assert r.aux and r.fill == 'self', r
from roles import Role
assert Role(slug='y', ecosystem='claude', title='y', doc='/d', fill='assigned', aux=True).validate()
") || fail "aux token / validation"

echo "== 6 upgrade: classes, feature edit, tooling sha, ledger record"
check "features: matcher.live=true" $H upgrade --spec "$SPEC" --enable matcher.live --skip-selftests --allow-dirty --dry-run
check "\[dry-run\] nothing written" $H upgrade --spec "$SPEC" --enable matcher.live --skip-selftests --allow-dirty --dry-run
check "upgraded $HAGID to tooling" $H upgrade --spec "$SPEC" --enable matcher.live --skip-selftests --allow-dirty
python3 - "$SPEC" "$A" <<'PY' || fail "upgrade did not record"
import json, os, sys
d = json.load(open(sys.argv[1]))
assert d["features"]["matcher"]["live"] is True and d["tooling"]["sha"], d.get("tooling")
assert d["tooling"].get("repo"), "tooling.repo not recorded"
evs = [json.loads(l) for fn in os.listdir(os.path.join(sys.argv[2], "shoals", d["hag"], "ledger"))
       for l in open(os.path.join(sys.argv[2], "shoals", d["hag"], "ledger", fn)) if l.strip()]
assert any(e.get("type") == "system" and e.get("kind") == "upgrade" for e in evs)
PY
check "tooling $(python3 -c "import json;print(json.load(open('$SPEC'))['tooling']['sha'])") ->" \
  $H upgrade --spec "$SPEC" --skip-selftests --allow-dirty --dry-run
# the re-arm time is found from inside the package (repo-root paths need `:(top)` pathspecs)
python3 - "$HAGD" "$S/rearm-repo" <<'PY' || fail "rearm_since found no re-arm change"
import os, subprocess, sys
sys.path.insert(0, sys.argv[1])
from upgrade import rearm_since
repo = sys.argv[2]; pkg = os.path.join(repo, "pkg"); os.makedirs(pkg)
def g(*a):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *a], cwd=repo, check=True,
                          capture_output=True, text=True).stdout.strip()
g("init", "-q"); open(os.path.join(pkg, "ledger.py"), "w").write("a\n"); g("add", "."); g("commit", "-qm", "one")
prev = g("rev-parse", "HEAD")
open(os.path.join(pkg, "ledger.py"), "w").write("b\n"); g("commit", "-qam", "two")
files = g("diff", "--name-only", f"{prev}..HEAD").splitlines()
assert files == ["pkg/ledger.py"], files
assert rearm_since(prev, files, cwd=pkg), "empty re-arm time from inside the package"
assert rearm_since(prev, ["pkg/other.py"], cwd=pkg) == ""
PY
# a sha recorded against another repository, or one this repository lacks, is "none recorded"
python3 -c "import json; d=json.load(open('$SPEC')); d['tooling']['repo']='git@example.invalid:other/tools.git'; json.dump(d, open('$S/spec-otherrepo.json','w'))"
python3 -c "import json; d=json.load(open('$SPEC')); d['tooling']['sha']='0123456789ab'; json.dump(d, open('$S/spec-badsha.json','w'))"
check "is from git@example.invalid:other/tools.git.*treating it as none recorded" \
  $H upgrade --spec "$S/spec-otherrepo.json" --skip-selftests --allow-dirty --dry-run
check "tooling (none recorded) ->" $H upgrade --spec "$S/spec-otherrepo.json" --skip-selftests --allow-dirty --dry-run
check "0123456789ab is not a commit in this repository; treating it as none recorded" \
  $H upgrade --spec "$S/spec-badsha.json" --skip-selftests --allow-dirty --dry-run

echo "== 7 tiers: free first, then the Go subscription, then the Zen balance; paid tiers recheck free"
python3 - "$SPEC" "$GOOD" "$BAD" <<'PY' || fail "tiers"
import sys
import matcher
from roles import HagSpec
spec = HagSpec.load(sys.argv[1]); good, bad = sys.argv[2], sys.argv[3]
c = dict(matcher.DEFAULTS, backend="command", model=bad, fallbacks=[], discover_free=False,
         go_fallbacks=[{"backend": "command", "model": good}],
         paid_fallback={"backend": "command", "model": good})
st, notes = {}, []
r = matcher.resolve(spec, c, st, {}, notes)
assert r and matcher.tier_of(r) == "go", (r, notes)          # free failed -> Go before the Zen balance
st2, notes2 = {"active": {"backend": "command", "model": bad}}, []
matcher.resolve(spec, c, st2, {}, notes2)
assert any("PAID: the Go subscription" in n for n in notes2), notes2   # a paid switch says so
# while Go is active and the recheck is not due, the free tier is not re-probed
st3 = {"active": dict(r), "free_checked": matcher.now_iso()}
c2 = dict(c, model=good)                                      # the free model is back...
r3 = matcher.resolve(spec, c2, st3, {}, [])
assert matcher.tier_of(r3) == "go", r3                        # ...but not rechecked yet
st3["free_checked"] = "2000-01-01T00:00:00Z"                  # recheck due
notes4 = []
r4 = matcher.resolve(spec, c2, st3, {}, notes4)
assert matcher.tier_of(r4) == "free" and any("free-tier recheck" in n for n in notes4), (r4, notes4)
# no Go route and no key: the Zen tier is skipped, never probed
c5 = dict(c, go_fallbacks=[], paid_fallback={"model": "m", "key_env": "SHOAL_TEST_KEY"})
assert matcher.resolve(spec, c5, {}, {}, []) is None
PY

echo "SELFTEST H8 OK ($S)"
rm -rf /tmp/$HAGID
