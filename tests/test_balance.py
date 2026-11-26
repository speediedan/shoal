#!/usr/bin/env python3
"""Hermetic proof of quota balancing with hysteresis (accounts.balance_pick, role_spend, the health quorum's holds).

Measured 2026-11-23: two pickers undid each other (the context restart's rebalance took the best-scored account, the
share move the account under its share), the shares swung with every reading, and a quiet role was moved as readily as
a busy one: 88 moves in a week, 55 of them back within 6 h. Here, with synthetic readings, transcripts and specs only:

- one rule for both moves, so a restart never lands a role where the next quota run moves it back;
- a share move only for a clear margin of the pool's spend, held for a while, cutting the imbalance by the margin; a
  move back to the account a role left needs twice the margin;
- spend, not role count: a quiet role is never moved for share, but leaves an account near its limit like any other;
- the replayed swing of 2026-11-23 (shares alternating each hour) makes no move after the first and no bounce;
- role spend read incrementally from this host's transcripts (a call counted once, a half-written line next time),
  published on the host-beat and read back for another host's roles.
"""
from __future__ import annotations

import datetime
import json
import os
import sys
import tempfile
import time

T = tempfile.mkdtemp(prefix="shoal-balance-test-")
SHOALD = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "shoal")
HOME = os.path.join(T, "home")
D1 = os.path.join(HOME, ".claude")
os.makedirs(os.path.join(D1, "projects", "-work-proj"), exist_ok=True)
CFG = os.path.join(T, "host.toml")
open(CFG, "w").write('server = "host-a"\n\n[accounts]\nselect = "balance"\nquota_handoff = true\nrebalance = true\n\n'
                     '[accounts.claude-2]\nprovider = "claude"\nenv = { CLAUDE_CONFIG_DIR = "~/.claude-2" }\n')
os.environ.update(HOME=HOME, XDG_CONFIG_HOME=os.path.join(T, "xdg"), XDG_RUNTIME_DIR=os.path.join(T, "run"),
                  SHOAL_CONFIG=CFG, SHOAL_LEDGER_DIR=os.path.join(T, "ledgers"))
for k in ("CLAUDE_CONFIG_DIR", "SHOAL_FAKE_SESSIONS", "SHOAL_SERVER"):
    os.environ.pop(k, None)
sys.path[:0] = [SHOALD, os.path.join(SHOALD, "ui")]

import accounts  # noqa: E402
import health  # noqa: E402
import role_spend  # noqa: E402
import subscription_usage  # noqa: E402
from roles import Role, ShoalSpec  # noqa: E402

NOW = 1_800_000_000.0
WEEK = 10080


def fail(msg: str) -> None:
    print(f"test_balance FAIL: {msg}", file=sys.stderr)
    sys.exit(1)


def check(cond, msg: str) -> None:
    if not cond:
        fail(msg)


def week(used: float, left_h: float, acct: str) -> dict:
    return {"provider": acct, "stale": False, "error": "", "rows": [{"bucket": "b", "windows": [
        {"name": "week", "used_percent": used, "resets_at": NOW + left_h * 3600, "minutes": WEEK}]}]}


# two Max accounts with equal room: each takes half the pool's spend
EVEN = [week(40, 96, "claude"), week(40, 96, "claude-2")]
A, B = ("claude", ""), ("claude", "claude-2")


def pick(acct: str, load: dict, w: float, snap=EVEN, **kw):
    return accounts.balance_pick("claude", acct, load, False, ["claude"], NOW, snap, unit="M/day", weight=w, **kw)


# ---- the rule -------------------------------------------------------------------------------------------------------
check(accounts.share_margin_pct() == 10 and accounts.share_hold_min() == 30 and accounts.quota_cooldown_min() == 360,
      "defaults: a 10% margin held 30 min, and a 6 h cooldown for a role moved for share")
# 200M/day on the pool, 130 on claude against a 100 share: over by 30, more than the 20 margin
load = {A: 130.0, B: 70.0}
check(accounts.over_accounts(load, ["claude"], NOW, EVEN) == {A}, "claude is over its share by the margin")
got = pick("", load, 30.0)
check(got and got[:2] == ("claude", "claude-2") and "cuts the imbalance by 60.0 (margin 20.0)" in got[2],
      f"a busy role (30M/day) moves off the account over its share: {got}")
check(pick("", load, 3.0) is None, "a quiet role (3M/day) is never moved for share: the cut (6) is under the margin")
check(pick("", load, 75.0) is None,
      "nor one whose move would overshoot (75M/day: claude-2 would end 45 over its share, worse than before)")
check(pick("", {A: 115.0, B: 85.0}, 30.0) is None, "15 over a 100 share is within the margin: nothing moves")
check(pick("claude-2", load, 30.0) is None, "a role on the account under its share stays")
check(pick("", load, 30.0, over_for_min=10) is None and pick("", load, 30.0, over_for_min=30),
      "the account must have been over by the margin for share_hold_min (30) first")
check(pick("", load, 30.0, left=B) is None, "a move back to the account the role left needs twice the margin")
check(pick("", {A: 150.0, B: 50.0}, 45.0, left=B), "and makes it with twice the margin (50 over, a cut of 90)")
# near a limit: off, whatever the role spends, and whatever its account's share
hot = [week(98.5, 30, "claude"), week(40, 96, "claude-2")]
got = pick("", {A: 60.0, B: 140.0}, 0.5, snap=hot, over_for_min=0, left=B)
check(got and got[1] == "claude-2" and "is near its limit (week 98.5% used)" in got[2],
      f"a quiet role leaves an account near its limit like any other: {got}")
# counts: with no spend known every role weighs 1 and the margin is in roles
got = accounts.balance_pick("claude", "", {A: 7, B: 4}, False, ["claude"], NOW, EVEN)
check(got and "7 roles against a 5.5-role share" in got[2], f"count mode: 1.5 roles over, a cut of 2 roles: {got}")
check(accounts.balance_pick("claude", "", {A: 6, B: 5}, False, ["claude"], NOW, EVEN) is None,
      "count mode: half a role over is no margin")

# ---- one rule for both moves: the 2026-11-23 swing, replayed ----------------------------------------------------------
# the old rebalance sent each restarting role to the best-scored account and the share move sent it back. Here every
# hour flips which account scores better (and so holds the larger share), each of four roles restarts every 3 h and
# sits at a quiet boundary every run in between, and the quorum keeps its holds (persistence, cooldown, settle)
ROLES = {"busy-a": 40.0, "busy-b": 35.0, "mid": 15.0, "quiet": 2.0}
place = {"busy-a": "", "busy-b": "", "mid": "claude-2", "quiet": "claude-2"}
moves, moved_at, left, over_since, settle = [], {}, {}, {}, -1e9
for step in range(24 * 12):  # a day of 5 min runs
    t = NOW + step * 300
    hour = step // 12
    snap = [week(40 if hour % 2 else 55, 96, "claude"), week(55 if hour % 2 else 40, 96, "claude-2")]
    load = {A: 0.0, B: 0.0}
    for r, w in ROLES.items():
        load[("claude", place[r])] += w
    ov = accounts.over_accounts(load, ["claude"], t, snap)
    over_since = {k: over_since.get(k, t) for k in ov}
    done = False
    for i, (r, w) in enumerate(sorted(ROLES.items())):
        restart = step % 36 == i * 9
        if done and not restart:
            continue
        k = ("claude", place[r])
        lf = left.get(r)
        got = accounts.balance_pick("claude", place[r], load, restart, ["claude"], t, snap, unit="M/day", weight=w,
                                    left=("claude", lf[0]) if lf and t - lf[1] < 86400 else None,
                                    over_for_min=(t - over_since[k]) / 60 if k in over_since else 0.0)
        if got is None or (not restart and (t - moved_at.get(r, -1e9) < 360 * 60 or t - settle < 15 * 60)):
            continue
        moves.append((step, r, place[r], got[1]))
        load[k] -= w
        load[("claude", got[1])] += w
        left[r], moved_at[r], settle, place[r] = (place[r], t), t, t, got[1]
        done = done or not restart
bounces = sum(1 for a in moves for b in moves if a[1] == b[1] and b[0] > a[0] and (b[2], b[3]) == (a[3], a[2])
              and b[0] - a[0] <= 72)
# a swing of 14% of the pool is over the 10% margin, so the first hours settle the roles (two moves); after that
# neither side is over by a margin a move could cut, and the role that moved may not go back for twice the margin
check(len(moves) <= 2 and bounces == 0 and not any(m[1] == "quiet" for m in moves) and max(m[0] for m in moves) < 36,
      f"shares swinging hourly between 43% and 57%: two moves early, then none, no bounce, the quiet role never: {moves}")
# the old pair, for contrast: the best-scored account at each restart and the count share at each boundary
old = 0
place = {"busy-a": "", "busy-b": "", "mid": "claude-2", "quiet": "claude-2"}
for step in range(24 * 12):
    hour = step // 12
    best = "" if hour % 2 else "claude-2"
    for i, r in enumerate(sorted(ROLES)):
        if step % 36 == i * 9 and place[r] != best:
            place[r], old = best, old + 1
check(old >= 6, f"(the old restart pick alone moved roles {old} times in the same day)")

# ---- role spend: this host's transcripts, read incrementally ----------------------------------------------------------
SESS, OLD, SUB = "aaaa0001-0000-4000-8000-000000000001", "bbbb0002-0000-4000-8000-000000000002", "agent-x"
P = os.path.join(D1, "projects", "-work-proj")


def iso(t: float) -> str:
    return datetime.datetime.fromtimestamp(t, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def call(mid: str, t: float, read: int = 0, w1h: int = 0, out: int = 0, model: str = "m-test") -> str:
    return json.dumps({"type": "assistant", "timestamp": iso(t), "message": {
        "id": mid, "model": model, "usage": {"input_tokens": 0, "cache_read_input_tokens": read,
                                             "cache_creation_input_tokens": w1h, "output_tokens": out,
                                             "cache_creation": {"ephemeral_1h_input_tokens": w1h}}}}) + "\n"


now = time.time()
with open(os.path.join(P, SESS + ".jsonl"), "w") as fh:
    fh.write(call("m1", now - 3600, read=10_000_000))  # 1.0M cost-eq
    fh.write(call("m1", now - 3600, read=10_000_000))  # the same call's second content block: not counted again
    fh.write(call("m2", now - 1800, w1h=1_000_000))  # 2.0M
    fh.write(call("m3", now - 1700, read=50_000_000, model="<synthetic>"))  # not a model call
with open(os.path.join(P, OLD + ".jsonl"), "w") as fh:  # the role's previous incarnation, ended an hour ago
    fh.write(call("o1", now - 7200, out=200_000))  # 1.0M
    fh.write(call("o0", now - 30 * 3600, out=200_000))  # outside the 24 h window
os.makedirs(os.path.join(P, SESS, "subagents"), exist_ok=True)
with open(os.path.join(P, SESS, "subagents", SUB + ".jsonl"), "w") as fh:
    fh.write(call("s1", now - 600, read=5_000_000))  # 0.5M
SPEC = ShoalSpec(sid="bal-test", dir=T, roles=[Role(slug="lead", ecosystem="claude", session=SESS),
                                              Role(slug="away", ecosystem="claude", session="cccc0003", server="host-b"),
                                              Role(slug="cx", ecosystem="codex", session="dddd0004")],
                 generations=[{"gen": "r1", "ended_utc": iso(now - 3600)[:19] + "Z", "sessions": {"lead": OLD[:8]}}])
got = role_spend.measure([SPEC], now)
check(got == {("bal-test", "lead"): 4.5}, f"4.5M in the last 24 h, across incarnations and subagents: {got}")
with open(os.path.join(P, SESS + ".jsonl"), "a") as fh:
    fh.write(call("m4", now - 60, read=20_000_000))  # 2.0M
    fh.write(call("m5", now - 30, read=40_000_000)[:-20])  # still being written
role_spend._MEMO.clear()
got = role_spend.measure([SPEC], now)
cache = json.load(open(role_spend.cache_path()))["files"]
check(got == {("bal-test", "lead"): 6.5} and cache[os.path.join(P, SESS + ".jsonl")]["off"] <
      os.path.getsize(os.path.join(P, SESS + ".jsonl")), f"only the appended, complete line is added: {got}")
# the host-beat carries it; another host reads a remote role's spend from the latest beat
LED = os.path.join(T, "ledgers", "shoals", "bal-test", "ledger")
os.makedirs(LED, exist_ok=True)
SPEC.ledger = {"dir": os.path.join(T, "ledgers")}
with open(os.path.join(LED, "shoal.jsonl"), "w") as fh:
    for ts, v in ((now - 7200, 9.0), (now - 600, 12.5)):
        fh.write(json.dumps({"ts": iso(ts)[:19] + "Z", "by": "shoal", "type": "system", "kind": "host-beat",
                             "host": "host-b", "spend": {"away": v}}) + "\n")
check(role_spend.beat(SPEC) == {"lead": 6.5}, f"this host's beat: {role_spend.beat(SPEC)}")
check(role_spend.beats(SPEC, now) == {"host-b": {"away": 12.5}}, "another host's latest beat")
w = role_spend.weights([SPEC], now)
check(w == {("bal-test", "lead"): 6.5, ("bal-test", "away"): 12.5, ("bal-test", "cx"): None},
      f"weights: measured here, beaten there, unknown for another provider: {w}")
load, counts, per, unit = accounts.balance_load([SPEC], now)
check(unit == "M/day" and per[("bal-test", "cx")] == 9.5 and load == {A: 19.0, ("codex", ""): 9.5}
      and counts == {A: 2, ("codex", ""): 1}, f"an unknown role weighs as the mean known one: {load} {per}")
role_spend._MEMO.clear()
load, counts, per, unit = accounts.balance_load([ShoalSpec(sid="none", dir=T, roles=[
    Role(slug="c1", ecosystem="codex", session="e1"), Role(slug="c2", ecosystem="codex", session="e2")])], now)
check(unit == "roles" and load == {("codex", ""): 2.0}, f"nothing known at all: role counts, as before: {load}")

# ---- the quorum: holds, the once-per-run observation and the beat ----------------------------------------------------
st = {}
check(health.balance_hold("limit", "lead", st, False) == "", "a move off an account near its limit is never held")
check(health.balance_hold("limit", "lead", st, True) == "another role moved this run", "but one quota move per run")
health.balance_moved(st, "lead", "share", "claude/", "bal-test/lead")
check(st["quota_left"]["lead"]["from"] == "claude/" and health.balance_hold("share", "lead", st, False).startswith(
    "moved 0 min ago (cooldown 360)"), "a role moved for share waits out its cooldown")
check(health.balance_hold("share", "other", st, False).startswith("a role on this host moved for share 0 min ago"),
      "and every other share move on this host waits out the settle")
check(health.balance_hold("limit", "other", st, False) == "", "the settle never holds a move off a limit")
os.remove(accounts.settle_path())
health.accounts.balance_load = lambda specs=None, now=None: ({A: 130.0, B: 70.0}, {A: 4, B: 3},
                                                             {("bal-test", "lead"): 30.0}, "M/day")
subscription_usage.snapshot = lambda keys, now=None: {"accounts": [  # the quorum reads the clock
    dict(week(40, 96, a), rows=[{"bucket": "b", "windows": [
        {"name": "week", "used_percent": 40, "resets_at": time.time() + 96 * 3600, "minutes": WEEK}]}])
    for a in ("claude", "claude-2")]}
accounts.pool = lambda providers=None: ["claude"]
st = {}
health.balance_observe(st)
check(list(st["balance_over"]) == ["claude/"], f"the run records since when claude is over: {st}")
first = st["balance_over"]["claude/"]
health.balance_observe(st)
check(st["balance_over"]["claude/"] == first, "and keeps that time while it stays over")
lead = SPEC.role("lead")
check(health.balance_pick(SPEC, lead, False) is None, "a share move waits for the account to stay over 30 min")
st["balance_over"]["claude/"] = "2000-01-01T00:00:00Z"
got = health.balance_pick(SPEC, lead, False)
check(got and got[1] == "claude-2" and got[3] == "share", f"then moves: {got}")
st["quota_left"] = {"lead": {"from": "claude/claude-2", "at": health.iso()}}
check(health.balance_pick(SPEC, lead, False) is None, "unless that is the account it left today (twice the margin)")
check(health.beat_spend(SPEC) == {"spend": {"lead": 6.5}}, f"the beat carries the spend: {health.beat_spend(SPEC)}")
health._BALANCE.clear()
health._BALANCE["off"] = True
check(health.balance_pick(SPEC, lead, False) is None, "a run whose balancing inputs failed moves no role")
health._BALANCE.clear()
print("test_balance: ok")
