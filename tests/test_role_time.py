"""Role time accounting on synthetic snapshots: no ledger, harness, network or clock access."""
import json
import os
from pathlib import Path
import random
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "shoal"))
import role_time as rt


def snap(t, native="between", posture="idle", sub="", seq=0, n_obs=None, l_obs=None, n_budget=1e9,
         l_budget=1e9, **ledger):
    s = {"t": t, "seq": seq, "lifecycle": ledger.pop("lifecycle", {}),
         "native": None if native is None else {"state": native, "observed_at": t if n_obs is None else n_obs,
                                                "budget_s": n_budget, "wait_event": ledger.pop("wait_event", "")},
         "ledger": {"observed_at": t if l_obs is None else l_obs, "budget_s": l_budget, "state": posture,
                    "sub": sub, **ledger}}
    return s


def run(obs, t1, t0=0.0, mem=None):
    mem = mem or {r: [(t0, None)] for r in obs}
    per, segs = rt.account(obs, mem, t0, t1, timeline=True)
    return per, segs


class Classification(unittest.TestCase):
    def cls(self, s, now=None):
        return rt.classify(s, s["t"] if now is None else now)[:2]

    def test_native_turn_is_work_whatever_the_sleep_posture(self):
        self.assertEqual(self.cls(snap(0, "turn", "waiting", "remote-ci")), ("work", "turn"))
        self.assertEqual(self.cls(snap(0, "turn", "paused")), ("work", "turn"))

    def test_identified_wait_phase_inside_a_turn(self):
        self.assertEqual(self.cls(snap(0, "turn", wait_event="local-ci")), ("wait", "local-ci"))
        self.assertEqual(self.cls(snap(0, "turn", wait_event="nonsense")), ("unknown", "unmapped-state"))

    def test_between_turn_postures(self):
        self.assertEqual(self.cls(snap(0, posture="waiting", sub="remote-ci")), ("wait", "remote-ci"))
        self.assertEqual(self.cls(snap(0, posture="waiting", sub="local-ci", target="lease:gpu")), ("wait", "lease"))
        self.assertEqual(self.cls(snap(0, posture="waiting", sub="queue-empty")), ("idle", "no-work"))
        self.assertEqual(self.cls(snap(0, posture="waiting", sub="queue-empty", refill_requested=True)),
                         ("wait", "refill"))
        self.assertEqual(self.cls(snap(0, posture="waiting", sub="mystery")), ("unknown", "unmapped-state"))
        self.assertEqual(self.cls(snap(0, posture="paused")), ("excluded", "paused"))
        self.assertEqual(self.cls(snap(0, posture="offline")), ("excluded", "offline"))
        self.assertEqual(self.cls(snap(0, "prompt", "waiting", "remote-ci")), ("wait", "user-prompt"))

    def test_stale_active_posture_never_accrues_work(self):
        c, e, q, _ = rt.classify(snap(0, posture="active", active="repo#1"), 0)
        self.assertEqual((c, e), ("idle", "ready-work"))
        self.assertIn("stale-active", q)
        self.assertEqual(self.cls(snap(0, posture="active")), ("idle", "no-work"))

    def test_lifecycle(self):
        parked = {"parked": True}
        self.assertEqual(self.cls(snap(0, None, lifecycle=parked)), ("excluded", "parked"))
        self.assertEqual(self.cls(snap(0, "absent", lifecycle=parked)), ("excluded", "parked"))
        self.assertEqual(self.cls(snap(0, "turn", lifecycle=parked)), ("unknown", "park-conflict"))
        self.assertEqual(self.cls(snap(0, "absent", lifecycle={"handover": True})), ("wait", "handover"))
        self.assertEqual(self.cls(snap(0, "absent")), ("unknown", "session-missing"))
        self.assertEqual(self.cls(snap(0, "turn", lifecycle={"conflict": "two live workers"})),
                         ("unknown", "conflicting-session"))

    def test_sources(self):
        self.assertEqual(self.cls(snap(0, None)), ("unknown", "missing-source"))
        self.assertEqual(self.cls(snap(100, "turn", n_obs=50, n_budget=15)), ("unknown", "stale-source"))
        # a fresh native turn does not need the ledger; between turns it does
        self.assertEqual(self.cls(snap(100, "turn", l_obs=0, l_budget=180)), ("work", "turn"))
        self.assertEqual(self.cls(snap(500, "between", l_obs=0, l_budget=180)), ("unknown", "stale-source"))
        s = snap(0, "between")
        s["ledger"] = None
        self.assertEqual(self.cls(s), ("unknown", "missing-source"))


class Blocked(unittest.TestCase):
    def q(self, blockers, nxt=("a", "b"), now=0, **kw):
        s = snap(0, next=list(nxt), blockers=blockers, **kw)
        return rt.classify(s, now)

    def test_one_live_blocker_beside_a_runnable_item_is_ready_work(self):
        c, e, q, _ = self.q({"a": {"blocker": "upstream:o/r#2", "status": "live"},
                             "b": {"blocker": "hold:x", "status": "expired"}})
        self.assertEqual((c, e), ("idle", "ready-work"))
        self.assertIn("stale-blocked", q)
        # an unblocked next item is runnable too
        self.assertEqual(self.q({"a": {"blocker": "lease:gpu", "status": "live"}})[:2], ("idle", "ready-work"))

    def test_all_non_runnable(self):
        self.assertEqual(self.q({"a": {"blocker": "upstream:o/r#2", "status": "live"},
                                 "b": {"blocker": "upstream:o/r#3", "status": "live"}})[:2], ("wait", "upstream"))
        self.assertEqual(self.q({"a": {"blocker": "peer-role:r#4", "status": "live"},
                                 "b": {"blocker": "ask:which", "status": "live"}})[:2], ("wait", "mixed"))
        self.assertEqual(self.q({"a": {"blocker": "peer-role:r#4", "status": "live"},
                                 "b": {"blocker": "ask:which", "status": "unknown"}})[:2], ("wait", "unverified"))
        self.assertEqual(self.q({"a": {"blocker": "hold:release", "status": "live"}}, nxt=("a",))[:2],
                         ("wait", "hold"))
        self.assertEqual(self.q({"a": {"blocker": "ask:x", "status": "live"}}, nxt=("a",))[:2], ("wait", "user-ask"))

    def test_all_stale_is_ready_work_and_age_only_is_labelled(self):
        c, e, q, _ = self.q({"a": {"blocker": "hold:x", "status": "resolved"},
                             "b": {"blocker": "hold:y", "status": "expired"}})
        self.assertEqual((c, e), ("idle", "ready-work"))
        _c, _e, q2, _ = self.q({"a": {"blocker": "lease:gpu", "status": "live", "evidence": "age-only"}}, nxt=("a",))
        self.assertIn("age-only-blockers", q2)

    def test_active_item_and_empty_queue(self):
        self.assertEqual(self.q({"a": {"blocker": "lease:gpu", "status": "live"}}, nxt=("a",), active="c")[:2],
                         ("idle", "ready-work"))
        self.assertEqual(self.q({}, nxt=())[:2], ("idle", "no-work"))
        self.assertEqual(self.q({}, nxt=(), refill_requested=True)[:2], ("wait", "refill"))

    def test_blocker_expiry_splits_the_interval(self):
        s = snap(0, next=["a"], blockers={"a": {"blocker": "hold:x", "status": "live", "expires_at": 40}})
        per, segs = run({"r": [s]}, 100)
        self.assertEqual(per["r"][("wait", "hold")], 40)
        self.assertEqual(per["r"][("idle", "ready-work")], 60)


class Accounting(unittest.TestCase):
    def test_v4_control_two_roles_half_work_half_wait(self):
        obs = {r: [snap(0, "turn"), snap(30, "between", "waiting", "remote-ci")] for r in ("r1", "r2")}
        per, _ = run(obs, 60)
        s = rt.summarize(per, 60)
        self.assertAlmostEqual(s["AAR"], 2.0)
        self.assertAlmostEqual(s["A_work"], 1.0)
        self.assertAlmostEqual(s["RWF"], 0.5)
        self.assertAlmostEqual(s["coverage"], 1.0)

    def test_v4_control_waiting_on_own_gpu_job(self):
        per, _ = run({"r": [snap(0, "between", "waiting", "local-ci", target="job:train")]}, 60)
        s = rt.summarize(per, 60)
        self.assertAlmostEqual(s["AAR"], 1.0)
        self.assertAlmostEqual(s["A_work"], 0.0)
        self.assertAlmostEqual(s["RWF"], 0.0)

    def test_turn_inside_an_unchanged_sleep_posture(self):
        w = dict(posture="waiting", sub="remote-ci")
        obs = {"r": [snap(0, "between", **w), snap(1000, "turn", **w), snap(1300, "between", **w)]}
        per, _ = run(obs, 3600)
        self.assertEqual(per["r"][("work", "turn")], 300)
        self.assertEqual(per["r"][("wait", "remote-ci")], 3300)

    def test_population_comes_from_membership_not_writers(self):
        obs = {"r": [snap(0, "between", "waiting", "remote-ci")],
               "nonmember": [snap(0, "turn", "active")]}
        per, _ = rt.account(obs, {"r": [(0, None)]}, 0, 60)
        self.assertNotIn("nonmember", per)
        self.assertAlmostEqual(rt.summarize(per, 60)["AAR"], 1.0)

    def test_gaps_bounds_and_coverage(self):
        # native read at 0 believed for 15 s, no further observation: 15 s work, 45 s unknown
        per, _ = run({"r": [snap(0, "turn", n_budget=15)]}, 60)
        self.assertEqual(per["r"][("work", "turn")], 15)
        self.assertEqual(per["r"][("unknown", "stale-source")], 45)
        s = rt.summarize(per, 60)
        self.assertAlmostEqual(s["AAR"], 0.25)
        self.assertAlmostEqual(s["AAR_upper"], 1.0)
        self.assertAlmostEqual(s["RWF_upper"], 1.0)
        self.assertAlmostEqual(s["coverage"], 0.25)
        # before the first observation in a membership span
        per, _ = run({"r": [snap(20, "turn")]}, 60)
        self.assertEqual(per["r"][("unknown", "no-observation")], 20)

    def test_excluded_time_leaves_the_eligible_denominator(self):
        obs = {"r": [snap(0, "turn"), snap(30, "between", "paused")]}
        s = rt.summarize(run(obs, 60)[0], 60)
        self.assertAlmostEqual(s["RWF"], 1.0)
        self.assertAlmostEqual(s["eligible_population"], 0.5)
        s = rt.summarize(run({"r": [snap(0, "between", "offline")]}, 60)[0], 60)
        self.assertIsNone(s["RWF"])
        self.assertIsNone(s["coverage"])

    def test_same_time_records_follow_append_order(self):
        obs = {"r": [snap(10, "between", "waiting", "remote-ci", seq=2), snap(10, "turn", seq=1), snap(0, "turn")]}
        per, _ = run(obs, 20)
        self.assertEqual(per["r"][("work", "turn")], 10)
        self.assertEqual(per["r"][("wait", "remote-ci")], 10)

    def test_conservation_over_random_fixtures_and_subwindows(self):
        rng = random.Random(7)
        natives = ["turn", "between", "prompt", "absent", "weird", None]
        postures = [("waiting", "remote-ci"), ("waiting", "queue-empty"), ("idle", ""), ("paused", ""),
                    ("active", ""), ("waiting", "x")]
        obs, mem = {}, {}
        for r in range(6):
            ts = sorted(rng.uniform(0, 1000) for _ in range(rng.randint(0, 30)))
            obs[f"r{r}"] = [snap(t, rng.choice(natives), *rng.choice(postures), n_budget=rng.choice([5, 50, 1e9]),
                                 next=["a"], blockers={"a": {"blocker": "hold:x", "status": "live",
                                                             "expires_at": t + rng.uniform(0, 100)}})
                            for t in ts]
            a = rng.uniform(0, 300)
            mem[f"r{r}"] = [(a, a + rng.uniform(100, 400)), (a + 500, None)]
        whole, _ = rt.account(obs, mem, 0, 1000)
        for r, spans in mem.items():
            span_s = sum(min(1000, e if e is not None else 1000) - max(0, s) for s, e in spans)
            self.assertAlmostEqual(sum(whole[r].values()), span_s, places=6)
        cut = 437.5
        left, _ = rt.account(obs, mem, 0, cut)
        right, _ = rt.account(obs, mem, cut, 1000)
        for r in mem:
            for k in set(whole[r]) | set(left[r]) | set(right[r]):
                self.assertAlmostEqual(whole[r][k], left[r][k] + right[r][k], places=6)


class Live(unittest.TestCase):
    def test_zero_initialized_series_and_no_host_labels(self):
        acc = rt.Accumulator("g", ["r"], now=0)
        text = acc.render_prometheus()
        self.assertIn('shoal_role_time_seconds_total{shoal="g",role="r",class="work",event="turn"} 0.000', text)
        self.assertEqual(text.count("shoal_role_time_seconds_total{"), len(rt.pairs()))
        self.assertNotIn("host=", text)
        self.assertNotIn("session=", text)

    def test_live_matches_reconciled_when_records_arrive_in_order(self):
        snaps = [snap(0, "turn"), snap(30, "between", "waiting", "remote-ci"), snap(45, "prompt")]
        acc = rt.Accumulator("g", ["r"], now=0)
        for s in snaps:
            acc.observe("r", s)
        acc.advance_all(60)
        per, _ = run({"r": snaps}, 60)
        for k, v in per["r"].items():
            self.assertAlmostEqual(acc.counters["r"][k], v)
        self.assertIn('class="wait",event="user-prompt"} 1', acc.render_prometheus())

    def test_late_record_is_not_backdated(self):
        acc = rt.Accumulator("g", ["r"], now=0)
        acc.observe("r", snap(0, "turn"))
        acc.advance_all(50)
        acc.observe("r", snap(20, "between", "waiting", "remote-ci"))  # arrives late
        acc.advance_all(60)
        self.assertEqual(acc.counters["r"][("work", "turn")], 50)
        self.assertEqual(acc.counters["r"][("wait", "remote-ci")], 10)
        self.assertEqual(acc.late["r"], 1)
        # the reconciled view uses event time
        per, _ = run({"r": [snap(0, "turn"), snap(20, "between", "waiting", "remote-ci")]}, 60)
        self.assertEqual(per["r"][("work", "turn")], 20)

    def test_checkpoint_restart_gap_and_lost_state(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "ckpt.json")
            acc = rt.Accumulator("g", ["r"], now=0, epoch=5)
            acc.observe("r", snap(0, "turn"))
            acc.advance_all(100)
            acc.save(path)
            back = rt.Accumulator.load(path, "g", ["r"], now=700)
            self.assertEqual(back.epoch, 5)
            self.assertEqual(back.counters["r"][("work", "turn")], 100)
            self.assertEqual(back.counters["r"][("unknown", "gap")], 600)
            Path(path).write_text("{torn")
            fresh = rt.Accumulator.load(path, "g", ["r"], now=800)
            self.assertEqual(fresh.epoch, 800)
            self.assertEqual(sum(fresh.counters["r"].values()), 0)

    def test_membership_change_stops_accrual(self):
        acc = rt.Accumulator("g", ["r"], now=0)
        acc.observe("r", snap(0, "turn"))
        acc.set_member("r", False, 30)
        acc.advance_all(60)
        self.assertEqual(sum(acc.counters["r"].values()), 30)
        self.assertIn('shoal_role_expected{shoal="g",role="r"} 0', acc.render_prometheus())


class Cli(unittest.TestCase):
    def test_report_json(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "obs.jsonl")
            lines = [{"type": "membership", "role": "r", "spans": [["1970-01-01T00:00:00Z", None]]},
                     dict(snap(0, "turn"), type="snapshot", role="r"),
                     dict(snap(30, "between", "waiting", "user-auth"), type="snapshot", role="r")]
            Path(p).write_text("\n".join(json.dumps(x) for x in lines) + "\n{torn")
            cli = Path(__file__).resolve().parents[1] / "shoal" / "cli.py"
            r = subprocess.run([sys.executable, str(cli), "role-time", "report", "--input", p, "--since", "0",
                                "--until", "60", "--json"], capture_output=True, text=True, timeout=60)
            self.assertEqual(r.returncode, 0, r.stderr)
            doc = json.loads(r.stdout)
            self.assertAlmostEqual(doc["aggregate"]["AAR"], 1.0)
            self.assertAlmostEqual(doc["aggregate"]["RWF"], 0.5)
            self.assertAlmostEqual(doc["aggregate"]["A_wait"]["user-auth"], 0.5)


if __name__ == "__main__":
    unittest.main()
