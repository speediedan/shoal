"""CI pool placement: the pure planner (shoal/pool.py) and the stage/wrap builders of pool_cli.py, on
synthetic N-host fixtures. No GPU, network, ssh or systemd access; the staging test uses local git only."""
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "shoal"))
import pool  # noqa: E402

NOW = 1_800_000_000.0
GIB_MIB = 1024


def iso(epoch):
    import datetime
    return datetime.datetime.fromtimestamp(epoch, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def probe(name, gpus=(), leases=(), queue=(), envs=None, ts=NOW, lease_cmd="/opt/lease.sh", mem_gib=100,
          root="/scratch/wt", ok=True, history=None, error=""):
    """A synthetic `shoal pool probe --json`. gpus: (uuid, vendor, vram GiB)."""
    keys = [{"key": "gpu", "held": False}, {"key": "cpu-heavy", "held": False}]
    keys += [{"key": f"gpu@{u}", "held": False} for u, _v, _g in gpus]
    rows = {r["key"]: r for r in keys}
    for row in leases:
        rows[row["key"]] = dict(row, held=True)
    p = {"type": "pool_probe", "host": name, "ts": ts, "cores": 32,
         "memory": {"total_bytes": 128 * 2 ** 30, "available_bytes": mem_gib * 2 ** 30},
         "gpus": [{"uuid": u, "vendor": v, "vram_total_mib": g * GIB_MIB} for u, v, g in gpus],
         "lease": {"ok": ok, "leases": list(rows.values()), "queue": list(queue)},
         "lease_cmd": lease_cmd, "ci_envs": envs or {}, "worktree_root": root, "holder_history": history or {}}
    if error:
        p = {"host": name, "error": error}
    return p


def hosts(*probes, now=NOW):
    return [pool.host_capacity(p, now) for p in probes]


def job(jid="shoal-s-r-suite", needs="gpu:1:any", home="a", state="new", **kw):
    dem = pool.demand(needs, "any", kw.pop("cpu_heavy", False), kw.pop("memory_gib", 0), kw.pop("env", ""),
                      kw.pop("repo", "owner/repo"), kw.pop("sha", "abc1234"), kw.pop("pin", ""),
                      kw.pop("remote_block", ""))
    j = {"id": jid, "name": kw.pop("name", "suite"), "repo": "owner/repo", "demand": dem, "home": home,
         "state": state, "submit_ts": kw.pop("submit_ts", NOW - 10), "prior_s": kw.pop("prior_s", 1800.0)}
    j.update(kw)
    return j


def holder(key, name, since_s_ago, pid=11, **kw):
    return dict({"key": key, "holder": name, "pid": pid, "since": iso(NOW - since_s_ago)}, **kw)


class Demand(unittest.TestCase):
    def test_parse_needs(self):
        self.assertEqual(pool.parse_needs("cpu"), {"gpus": 0, "vendor": "", "min_vram_gib": 0.0})
        self.assertEqual(pool.parse_needs("gpu"), {"gpus": 1, "vendor": "", "min_vram_gib": 0.0})
        self.assertEqual(pool.parse_needs("gpu:2"), {"gpus": 2, "vendor": "", "min_vram_gib": 0.0})
        self.assertEqual(pool.parse_needs("gpu:1:amd:16"), {"gpus": 1, "vendor": "amd", "min_vram_gib": 16.0})
        self.assertEqual(pool.parse_needs("gpu:nvidia:24g"), {"gpus": 1, "vendor": "nvidia", "min_vram_gib": 24.0})
        for bad in ("tpu", "cpu:2", "gpu:0", "gpu:1.5", "gpu:1:intel", "gpu:1:2:3"):
            with self.assertRaises(ValueError, msg=bad):
                pool.parse_needs(bad)

    def test_lease_scope(self):
        # CPU-only work never takes the gpu key: --lease cpu-heavy only, and only when asked
        self.assertEqual(pool.lease_args(pool.demand("cpu")), ([], "none"))
        self.assertEqual(pool.lease_args(pool.demand("cpu", cpu_heavy=True)), (["--lease", "cpu-heavy"], "cpu-heavy"))
        self.assertEqual(pool.lease_args(pool.demand("gpu:1:nvidia:16"))[0], ["--gpus", "1", "--min-vram", "16"])
        self.assertEqual(pool.lease_args(pool.demand("gpu:2", cpu_heavy=True)),
                         (["--gpus", "all", "--cpu-heavy"], "gpu:all+cpu-heavy"))
        self.assertEqual(pool.device_env(pool.demand("cpu")),
                         {"CUDA_VISIBLE_DEVICES": "", "ROCR_VISIBLE_DEVICES": "", "HIP_VISIBLE_DEVICES": ""})
        self.assertEqual(pool.device_env(pool.demand("gpu:1")), {})
        self.assertEqual(pool.demand("gpu:1", vendor_default="amd")["vendor"], "amd")

    def test_names(self):
        self.assertEqual(pool.normalize_name("suite-891-ab"), pool.normalize_name("suite-902-ab"))
        self.assertEqual(pool.normalize_name("cov-2026-11-01"), "cov-#")
        self.assertEqual(pool.normalize_name("run-deadbeef1"), "run-#")
        self.assertEqual(pool.repo_identity("git@example.com:owner/name.git"), "owner/name")
        self.assertEqual(pool.repo_identity("https://example.com/owner/name"), "owner/name")
        self.assertEqual(pool.repo_identity("/srv/repos/name"), "name")

    def test_feature(self):
        class S:
            features = {}
        self.assertFalse(pool.feature(S)["enabled"])
        S.features = {"placement": True}
        self.assertTrue(pool.feature(S)["enabled"])
        S.features = {"placement": {"max_moves": 1, "margin_s": 60}}
        f = pool.feature(S)
        self.assertTrue(f["enabled"])
        self.assertEqual((f["max_moves"], f["margin_s"], f["staging_s"]), (1, 60, pool.DEFAULTS["staging_s"]))


class Estimates(unittest.TestCase):
    def test_prior_learned_blend(self):
        e = pool.Estimator()
        self.assertEqual(e.job("o/r", "suite", "a", 600)["source"], "prior")
        self.assertEqual(e.job("o/r", "suite", "a")["p50"], pool.DEFAULTS["default_est_s"])
        hist = [{"repo": "o/r", "name": f"suite-{i}", "host": "a", "run_s": 1000 + i} for i in range(10)]
        e = pool.Estimator(hist)
        learned = e.job("o/r", "suite-77", "a")
        self.assertEqual(learned["source"], "learned")
        self.assertEqual(learned["n"], 10)
        blended = e.job("o/r", "suite-77", "a", prior_s=3000)
        self.assertEqual(blended["source"], "blended")
        self.assertTrue(learned["p50"] < blended["p50"] < 3000)
        # another host class falls back to every host's runs
        self.assertEqual(e.job("o/r", "suite-1", "b")["n"], 10)

    def test_overrun_tail_grows(self):
        est = {"p50": 1000.0, "p90": 1200.0}
        self.assertEqual(pool.remaining(est, 0), 1000.0)
        self.assertEqual(pool.remaining(est, 950), 100.0)        # floor near p50
        self.assertEqual(pool.remaining(est, 1150), 100.0)       # past p50, before p90: the floor
        r1, r2 = pool.remaining(est, 2000), pool.remaining(est, 4000)
        self.assertGreater(r1, 100.0)
        self.assertGreater(r2, r1)                               # an overrun pushes its end later, not sooner
        self.assertGreater(4000 + r2, 2000 + r1)


    def test_ci_budget_soft_ceiling(self):
        est = {"p50": 7200.0, "p90": 9000.0}                    # learned longer than the 90 min budget
        self.assertEqual(pool.remaining(est, 0), 7200.0)
        self.assertEqual(pool.remaining(est, 0, 5400.0), 5400.0 + 720.0)   # within budget: budget + floor
        self.assertEqual(pool.remaining(est, 6000, 5400.0), pool.remaining(est, 6000))  # past it: ordinary tail
        # the planner caps a CI holder by its budget; a research one keeps its learned length
        hist = [{"repo": "owner/repo", "name": "long", "host": "a", "run_s": 7200}] * 5
        hs = hosts(probe("a", gpus=[("a-d", "nvidia", 24)], leases=[holder("gpu", "long-unit", 60)]))
        for cls, want in (("ci", 5400 - 60 + 720), ("research", 7200 - 60)):
            known = [{"id": "long-unit", "name": "long", "repo": "owner/repo", "class": cls,
                      "demand": pool.demand("gpu:1"), "home": "a", "state": "running"}]
            hv = pool.plan(hs, [], NOW, pool.Estimator(hist), known=known)["hosts"]["a"]
            self.assertAlmostEqual(hv["holders"][0]["remaining_s"], want, delta=1, msg=cls)


class Capacity(unittest.TestCase):
    def test_occupancy_and_stale(self):
        p = probe("a", gpus=[("d1", "nvidia", 24), ("d2", "nvidia", 8)],
                  leases=[holder("gpu", "x", 60, mode="shared"), holder("gpu@d1", "x", 60), holder("cpu-heavy", "x", 60)],
                  queue=[{"key": "gpu", "waiter": "w", "ticket": "0000000007", "want": ["d2", "d1"]},
                         {"key": "cpu-heavy", "waiter": "w", "ticket": "0000000007", "want": ["cpu-heavy"]}])
        h = pool.host_capacity(p, NOW)
        self.assertTrue(h["fresh"])
        self.assertEqual(len(h["holders"]), 1)                   # three rows, one holder; shared gate is no claim
        self.assertEqual((h["holders"][0]["devices"], h["holders"][0]["cpu"]), ({"d1"}, True))
        self.assertAlmostEqual(h["holders"][0]["elapsed_s"], 60, delta=1)
        self.assertEqual(h["tickets"], [{"ticket": "0000000007", "name": "w", "devices": ["d2", "d1"],
                                         "one": True, "cpu": True}])
        stale = pool.host_capacity(probe("b", ts=NOW - 600), NOW)
        self.assertFalse(stale["fresh"])
        self.assertIn("stale probe", stale["reasons"])
        failed = pool.host_capacity(probe("c", error="ssh: timeout"), NOW)
        self.assertFalse(failed["fresh"])
        self.assertTrue(any("probe failed" in r for r in failed["reasons"]))
        # a remote clock running ahead does not age a probe the home host just fetched
        skew = pool.host_capacity(dict(probe("d", ts=NOW + 300), fetched_ts=NOW - 2), NOW)
        self.assertTrue(skew["fresh"])

    def test_vendor_vram_env_compatibility(self):
        a, b = hosts(probe("a", gpus=[("n8", "nvidia", 8), ("n24", "nvidia", 24)]),
                     probe("b", gpus=[("m1", "amd", 32), ("m2", "amd", 32)],
                           envs={"cpu-env": {"python": "/v/bin/python", "repos": ["owner/repo"]}}))
        amd = pool.demand("gpu:1:amd", repo="owner/repo", sha="s")
        self.assertTrue(pool.compatible(a, amd, "a"))
        self.assertFalse(pool.compatible(b, amd, "a"))
        nv16 = pool.demand("gpu:1:nvidia:16")
        self.assertEqual(pool.fitting_devices(a, nv16), ["n24"])
        self.assertEqual(pool.fitting_devices(a, pool.demand("gpu:1:nvidia")), ["n8", "n24"])  # best fit first
        self.assertIn("no nvidia GPU with 32 GiB", pool.compatible(a, pool.demand("gpu:1:nvidia:32"), "a"))
        self.assertFalse(pool.compatible(a, pool.demand("gpu:1:any:16"), "a"))
        whole = pool.demand("gpu:2:any", repo="owner/repo", sha="s")
        self.assertFalse(pool.compatible(b, whole, "a"))
        self.assertIn("needs 3 fitting GPUs (the whole host)", pool.compatible(b, pool.demand("gpu:3"), "b"))
        env = pool.demand("cpu", env="cpu-env", repo="owner/repo", sha="s")
        self.assertIn("env cpu-env missing", pool.compatible(a, env, "a"))
        self.assertFalse(pool.compatible(b, env, "a"))
        self.assertIn("env cpu-env does not serve owner/other",
                      pool.compatible(b, pool.demand("cpu", env="cpu-env", repo="owner/other", sha="s"), "a"))
        # another host needs something to stage, and a lease provider for leased work
        self.assertIn("no repo@sha to stage", pool.compatible(b, pool.demand("cpu"), "a"))
        self.assertIn("dirty tree", pool.compatible(b, pool.demand("cpu", remote_block="dirty tree"), "a"))
        nolease = pool.host_capacity(probe("c", lease_cmd=""), NOW)
        self.assertIn("no lease provider", pool.compatible(nolease, pool.demand("cpu", cpu_heavy=True), "c"))
        self.assertFalse(pool.compatible(nolease, pool.demand("cpu"), "c"))
        self.assertIn("insufficient or unknown memory", pool.compatible(a, pool.demand("cpu", memory_gib=200), "a"))


class Queue(unittest.TestCase):
    def test_fifo_all_or_nothing(self):
        h = hosts(probe("a", gpus=[("d1", "nvidia", 24), ("d2", "nvidia", 24)]))[0]
        holders = [{"end": NOW + 100, "devices": {"d1"}, "cpu": False}]
        tickets = [{"id": "t1", "dur": 500, "devices": ["d1"], "one": True, "cpu": False},
                   {"id": "t2", "dur": 50, "devices": pool.ALL, "one": False, "cpu": False},
                   {"id": "t3", "dur": 50, "devices": set(), "one": False, "cpu": True}]
        res, drain = pool.simulate(h, holders, tickets, NOW)
        self.assertEqual(res["t1"][0], NOW + 100)               # its only candidate frees at +100
        self.assertEqual(res["t2"][0], NOW + 600)               # the whole host waits for t1 to finish
        self.assertEqual(res["t3"][0], NOW)                     # cpu-heavy alone does not overlap gpu wants
        self.assertEqual(drain, NOW + 650)

    def test_best_fit_device(self):
        h = hosts(probe("a", gpus=[("small", "nvidia", 8), ("big", "nvidia", 24)]))[0]
        res, _ = pool.simulate(h, [], [{"id": "t", "dur": 10, "devices": ["small", "big"], "one": True}], NOW)
        self.assertEqual(res["t"][2], ["small"])


class Planner(unittest.TestCase):
    def test_list_scheduling_spreads_over_n_hosts(self):
        hs = hosts(*(probe(n, gpus=[(f"{n}-d", "nvidia", 24)]) for n in ("a", "b", "c")))
        jobs = [job(f"j{i}", submit_ts=NOW - 100 + i) for i in range(3)]
        p = pool.plan(hs, jobs, NOW)
        self.assertEqual([p["jobs"][f"j{i}"]["host"] for i in range(3)], ["a", "b", "c"])
        self.assertEqual(p["jobs"]["j0"]["reason"], "earliest predicted finish")
        self.assertAlmostEqual(p["objective_s"], 2 * pool.DEFAULTS["staging_s"])
        # priority beats submit order
        jobs[2]["priority"] = -1
        p = pool.plan(hs, jobs, NOW)
        self.assertEqual(p["jobs"]["j2"]["host"], "a")

    def test_external_holder_and_overrun_shift_the_plan(self):
        hist = {"a": {"ci-agent": [1000] * 9 + [1200]}}
        est = pool.Estimator(holders=hist)

        def plan_at(elapsed):
            hs = hosts(probe("a", gpus=[("a-d", "nvidia", 24)], leases=[holder("gpu", "ci-agent", elapsed)]),
                       probe("b", gpus=[("b-d", "nvidia", 24)]))
            return pool.place(hs, job(), NOW, est)
        early = plan_at(950)                                      # the holder is about to finish: stay home
        self.assertEqual(early["host"], "a")
        late = plan_at(6000)                                      # the holder overran: its tail pushes home later
        self.assertEqual(late["host"], "b")
        self.assertEqual(late["reason"], "earliest predicted finish")
        hv = pool.plan(hosts(probe("a", gpus=[("a-d", "nvidia", 24)], leases=[holder("gpu", "ci-agent", 6000)])),
                       [], NOW, est)["hosts"]["a"]
        self.assertTrue(hv["holders"][0]["external"])
        self.assertEqual(hv["holders"][0]["est_source"], "learned")

    def test_external_waiters_queue_ahead(self):
        q = [{"key": "gpu", "waiter": "ci-agent", "ticket": "0000000003", "want": ["all"]}]
        hs = hosts(probe("a", gpus=[("a-d", "nvidia", 24)], queue=q), probe("b", gpus=[("b-d", "nvidia", 24)]))
        r = pool.place(hs, job(), NOW, pool.Estimator(holders={"a": {"ci-agent": [3600]}}))
        self.assertEqual(r["host"], "b")
        start_a = next(c for c in r["considered"] if c["host"] == "a")["start"]
        self.assertEqual(start_a, NOW + 3600)

    def test_home_margin_tie(self):
        hs = hosts(probe("a", gpus=[("a-d", "nvidia", 24)]), probe("b", gpus=[("b-d", "nvidia", 24)]))
        near = pool.Estimator([{"repo": "owner/repo", "name": "suite", "host": "b", "run_s": 1600},
                               {"repo": "owner/repo", "name": "suite", "host": "a", "run_s": 1800}])
        r = pool.place(hs, job(prior_s=None), NOW, near)
        self.assertEqual((r["host"], r["reason"]), ("a", "home kept within margin"))   # b ends 80 s sooner
        far = pool.Estimator([{"repo": "owner/repo", "name": "suite", "host": "b", "run_s": 600},
                              {"repo": "owner/repo", "name": "suite", "host": "a", "run_s": 1800}])
        self.assertEqual(pool.place(hs, job(prior_s=None), NOW, far)["host"], "b")

    def test_stale_and_pinned(self):
        hs = hosts(probe("a", gpus=[("a-d", "nvidia", 24)], leases=[holder("gpu", "ci-agent", 10)]),
                   probe("b", gpus=[("b-d", "nvidia", 24)], ts=NOW - 900))
        r = pool.place(hs, job(), NOW)
        self.assertEqual((r["host"], r["reason"]), ("a", "only compatible host"))
        self.assertIn("stale probe", next(c for c in r["considered"] if c["host"] == "b")["reasons"])
        r = pool.place(hs, job(pin="b"), NOW)
        self.assertIsNone(r["host"])
        self.assertEqual(r["reason"], "no compatible host")

    def test_transfer_threshold_and_max_moves(self):
        def plan_with(holder_elapsed, moves=0, margin=180):
            hs = hosts(probe("a", gpus=[("a-d", "nvidia", 24)], leases=[holder("gpu", "ci-agent", holder_elapsed)],
                             queue=[{"key": "gpu", "waiter": "me", "ticket": "0000000009", "want": ["a-d"]}]),
                       probe("b", gpus=[("b-d", "nvidia", 24)]))
            est = pool.Estimator(holders={"a": {"ci-agent": [1000] * 10}})
            return pool.plan(hs, [job("me", state="queued", current="a", moves=moves)], NOW, est,
                             {"margin_s": margin})["jobs"]["me"]
        near = plan_with(1000 - 250)      # current start +250; elsewhere +120 (staging): gain 130 < margin
        self.assertIsNone(near["transfer"])
        self.assertEqual(near["host"], "a")
        far = plan_with(0)                # current start +1000: gain 880 > margin
        self.assertEqual(far["transfer"]["to"], "b")
        self.assertEqual(far["host"], "b")
        self.assertAlmostEqual(far["transfer"]["gain_s"], 880)
        capped = plan_with(0, moves=2)
        self.assertIsNone(capped["transfer"])
        self.assertTrue(capped["reason"].startswith("move cap reached"))
        self.assertEqual(plan_with(0, margin=2000)["transfer"], None)

    def test_running_job_never_moves(self):
        hs = hosts(probe("a", gpus=[("a-d", "nvidia", 24)], leases=[holder("gpu", "me", 5000)]),
                   probe("b", gpus=[("b-d", "nvidia", 24)]))
        p = pool.plan(hs, [job("me", state="running", current="a")], NOW)
        self.assertEqual(p["jobs"], {})
        self.assertEqual(p["transfers"], [])
        # a known SHOAL job's holder estimate comes from job history, not holder history
        self.assertFalse(p["hosts"]["a"]["holders"][0]["external"])

    def test_queued_job_keeps_its_ticket_position(self):
        q = [{"key": "gpu", "waiter": "me", "ticket": "0000000002", "want": ["a-d"]},
             {"key": "gpu", "waiter": "later", "ticket": "0000000005", "want": ["a-d"]}]
        hs = hosts(probe("a", gpus=[("a-d", "nvidia", 24)], queue=q))
        r = pool.plan(hs, [job("me", state="queued", current="a")], NOW)["jobs"]["me"]
        self.assertEqual(r["start"], NOW)                        # ahead of `later`, not appended behind it

    def test_dirty_tree_stays_home(self):
        hs = hosts(probe("a", gpus=[("a-d", "nvidia", 24)], leases=[holder("gpu", "ci-agent", 0)]),
                   probe("b", gpus=[("b-d", "nvidia", 24)]))
        r = pool.place(hs, job(remote_block="dirty tree without --ref"), NOW)
        self.assertEqual(r["host"], "a")


class Reservations(unittest.TestCase):
    RESV = [{"id": "r1", "for": "s/research-role", "devices": [{"uuid": "a-big", "vendor": "nvidia"}], "cpu": True,
             "alive": True}]

    def host(self, resv=None, **kw):
        return hosts(probe("a", gpus=[("a-small", "nvidia", 8), ("a-big", "nvidia", 48)], **kw)
                     | {"reservations": self.RESV if resv is None else resv})[0]

    def test_reserved_resources_leave_the_ci_pool(self):
        h = self.host()
        self.assertEqual(h["reserved_devices"], ["a-big"])
        self.assertEqual(pool.fitting_devices(h, pool.demand("gpu:1:nvidia")), ["a-small"])
        self.assertIn("every fitting GPU is reserved for research", pool.compatible(h, pool.demand("gpu:1:nvidia:16"), "a"))
        self.assertIn("a research reservation holds part of the host (a whole-host request would wait for it)",
                      pool.compatible(h, pool.demand("gpu:2"), "a"))
        self.assertIn("cpu-heavy is reserved for research", pool.compatible(h, pool.demand("cpu", cpu_heavy=True), "a"))
        self.assertFalse(pool.compatible(h, pool.demand("gpu:1:nvidia:16", job_class="research"), "a"))
        self.assertFalse(pool.compatible(self.host(resv=[dict(self.RESV[0], alive=False)]),
                                         pool.demand("gpu:1:nvidia:16"), "a"))  # a dead hold reserves nothing

    def test_ci_never_queues_behind_a_research_hold(self):
        a = self.host(leases=[holder("gpu@a-big", "research:s/research-role", 60)])
        b = hosts(probe("b", gpus=[("b-d", "nvidia", 48)], leases=[holder("gpu", "ci-agent", 0)]))[0]
        r = pool.place([a, b], job(needs="gpu:1:nvidia:16"), NOW)
        self.assertEqual(r["host"], "b")                          # waits for b's CI holder, not a's research hold
        r = pool.place([a, b], job(needs="gpu:1:nvidia"), NOW)
        self.assertEqual((r["host"], r["considered"][0]["devices"]), ("a", ["a-small"]))

    def test_queued_job_leaves_a_host_a_reservation_took(self):
        a = self.host(queue=[{"key": "gpu", "waiter": "me", "ticket": "0000000004", "want": ["a-big"]}])
        b = hosts(probe("b", gpus=[("b-d", "nvidia", 48)], leases=[holder("gpu", "ci-agent", 0)]))[0]
        r = pool.plan([a, b], [job("me", needs="gpu:1:nvidia:16", state="queued", current="a")], NOW)["jobs"]["me"]
        self.assertEqual((r["transfer"]["to"], r["transfer"]["reason"]), ("b", "current host can no longer run it"))
        stale_b = hosts(probe("b", gpus=[("b-d", "nvidia", 48)], ts=NOW - 900))[0]
        r = pool.plan([a, stale_b], [job("me", needs="gpu:1:nvidia:16", state="queued", current="a")], NOW)["jobs"]["me"]
        self.assertIsNone(r["transfer"])

    def test_reserve_release_expire_with_a_fake_helper(self):
        import importlib
        import json as _json
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as d:
            helper = Path(d, "lease")
            helper.write_text(r"""#!/bin/sh
# a fake lease helper: --hold forks a holder, --release ends it
pf=""; args="$*"
while [ $# -gt 0 ]; do case "$1" in --pidfile) pf=$2; shift 2;; *) shift;; esac; done
case "$args" in
  *--status*) echo '{"leases":[{"key":"gpu","held":false},{"key":"cpu-heavy","held":false}],"queue":[]}' ;;
  *--hold*) setsid sleep 300 </dev/null >/dev/null 2>&1 & echo $! > "$pf"
            echo "${GPU_LEASE_DEVICES%%:*}" > "$pf.device"; echo "$args" >> "$(dirname "$pf")/calls" ;;
  *--release*) kill "$(cat "$pf")" 2>/dev/null; rm -f "$pf" "$pf.device" ;;
esac
""")
            helper.chmod(0o755)
            data = Path(d, "data")
            cfg = Path(d, "host.toml")
            cfg.write_text(f'data_root = "{data}"\nlease_cmd = "{helper}"\n')
            sdir = data / "_resources" / "testhost"
            sdir.mkdir(parents=True)
            import time as _t
            now = _t.time()
            (sdir / _t.strftime("events-%Y%m%d.jsonl", _t.gmtime(now))).write_text(_json.dumps(
                {"type": "resource_sample", "ts": now, "memory": {"available_bytes": 1 << 34},
                 "gpus": [{"uuid": "GPU-x", "vendor": "nvidia", "vram_total_mib": 24576, "arch": "sm_89"}]}) + "\n")
            env = {"SHOAL_CONFIG": str(cfg), "SHOAL_DATA_ROOT": str(data), "SHOAL_SERVER": "testhost"}
            with patch.dict(os.environ, env):
                import config
                config._CACHE.clear()
                import pool_reserve
                importlib.reload(pool_reserve)
                with self.assertRaises(ValueError):
                    pool_reserve.reserve("gpu@GPU-nope", "s/r", "job:x")
                with self.assertRaises(ValueError):
                    pool_reserve.reserve("gpus:1", "not-an-owner", "job:x")
                rec = pool_reserve.reserve("gpu@GPU-x,cpu-heavy", "s/r", "job:exp", note="probe study")
                self.assertEqual((rec["devices"], rec["cpu"]), ([{"uuid": "GPU-x", "vendor": "nvidia"}], True))
                calls = (data / "_pool" / "holds" / "calls").read_text()
                self.assertIn("--gpus 1", calls)
                self.assertIn("--lease cpu-heavy", calls)
                rep = pool_reserve.report()
                self.assertEqual((rep[0]["id"], rep[0]["alive"]), (rec["id"], True))
                import pool_cli
                self.assertEqual(pool_cli.probe()["reservations"][0]["id"], rec["id"])
                with patch("jobs.unit_state", return_value={}):
                    self.assertEqual(pool_reserve.expire(emit=False), [])          # the job never ran: kept
                    rc = Path("/tmp/s/jobs/shoal-s-r-exp.rc")
                    with patch("os.path.exists", side_effect=lambda p: p == str(rc)):
                        gone = pool_reserve.expire(emit=False)
                self.assertEqual([g["id"] for g in gone], [rec["id"]])
                self.assertIn("job:exp finished", gone[0]["released"])
                self.assertFalse(any(pool_reserve._pid_alive(h["pidfile"]) for h in rec["holds"]))
                timed = pool_reserve.reserve("gpus:1", "s/r", pool_cli.utc(now + 3600))
                self.assertEqual(pool_reserve.expire(now + 60, emit=False), [])
                self.assertEqual([g["id"] for g in pool_reserve.expire(now + 7200, emit=False)], [timed["id"]])
                third = pool_reserve.reserve("cpu-heavy", "s/r", pool_cli.utc(now + 3600))
                self.assertEqual(pool_reserve.release(third["id"])["released"], "released")
                self.assertIsNone(pool_reserve.release(third["id"]))
                self.assertEqual(pool_reserve.load(), {})
            config._CACHE.clear()


class Budget(unittest.TestCase):
    def test_overrun_pages_once_and_never_kills(self):
        import argparse
        import contextlib
        import io
        from unittest.mock import MagicMock, patch
        import jobs
        import pool_cli
        a = argparse.Namespace(sid="budgettest", slug="r1", name="suite")
        a._pool_feature = pool.feature(None)
        start = iso(NOW - 100 * 60)
        meta = {"unit": "shoal-budgettest-r1-suite", "name": "suite", "slug": "r1", "class": "ci", "started_utc": start}
        spec = MagicMock()
        spec.role.return_value = object()
        with tempfile.TemporaryDirectory() as d, \
                patch.object(pool_cli.time, "time", return_value=NOW), patch.object(jobs, "_emit") as emit, \
                patch.object(pool_cli, "save_meta"), patch("roles.ShoalSpec.load", return_value=spec), \
                patch("ledger.Ledger") as led, patch("health.page_user") as page, \
                patch.object(jobs, "_systemctl") as ctl, contextlib.redirect_stdout(io.StringIO()) as out:
            pool_cli.budget_check(a, meta["unit"], meta, "")
            pool_cli.budget_check(a, meta["unit"], meta, "")       # once per run
            self.assertIn("JOB-OVERRUN", out.getvalue())
            self.assertEqual(emit.call_count, 1)
            self.assertEqual(emit.call_args.args[2], "overrun")
            self.assertEqual(page.call_count, 1)
            led.return_value.append.assert_called_once()
            self.assertEqual(led.return_value.append.call_args.kwargs["to"], "r1")
            ctl.assert_not_called()                                 # never a kill
            # a research job has no budget; a placed CI job's budget starts at its lease grant
            for m in (dict(meta, **{"class": "research"}), dict(meta, placement={"host": "a"}),
                      dict(meta, placement={"host": "a"}, lease_granted=iso(NOW - 10 * 60))):
                pool_cli.budget_check(a, m["unit"], m, "")
            self.assertEqual(emit.call_count, 1)
            rerun = dict(meta, placement={"host": "a"}, started_utc=iso(NOW - 99 * 60))   # a later run pages again
            pool_cli.budget_check(a, meta["unit"], rerun, iso(NOW - 95 * 60))
            self.assertEqual(emit.call_count, 2)


class Builders(unittest.TestCase):
    def setUp(self):
        import pool_cli
        self.pc = pool_cli

    def test_wrap_scopes(self):
        cpu, scope = self.pc.wrap(["pytest", "-x"], pool.demand("cpu"), "/opt/lease.sh", "/j/m", "/j/c", "/j/o")
        self.assertEqual(scope, "none")
        self.assertEqual(cpu[:2], ["/bin/bash", "-c"])
        self.assertIn("export CUDA_VISIBLE_DEVICES=''", cpu[2])
        self.assertIn("export ROCR_VISIBLE_DEVICES=''", cpu[2])
        self.assertEqual(cpu[-2:], ["pytest", "-x"])
        heavy, _ = self.pc.wrap(["t"], pool.demand("cpu", cpu_heavy=True), "/opt/lease.sh --timeout 60", "m", "c", "o")
        self.assertEqual(heavy[:5], ["/opt/lease.sh", "--timeout", "60", "--lease", "cpu-heavy"])
        self.assertNotIn("--cpu-heavy", heavy)
        gpu, scope = self.pc.wrap(["t"], pool.demand("gpu:1:nvidia:16"), "/opt/lease.sh", "m", "c", "o",
                                  "e1", {"python": "/v/e1/bin/python"})
        self.assertEqual(gpu[:6], ["/opt/lease.sh", "--gpus", "1", "--min-vram", "16", "--"])
        self.assertNotIn("CUDA_VISIBLE_DEVICES", gpu[8])
        self.assertIn("VIRTUAL_ENV=/v/e1", gpu[8])
        self.assertEqual(scope, "gpu:1 min-vram 16GiB")

    def test_inner_script_claim_and_marker(self):
        with tempfile.TemporaryDirectory() as d:
            m, c, o = (os.path.join(d, x) for x in ("granted", "claim", "out"))
            inner = self.pc.inner_script(m, c, o, {})
            r = subprocess.run(["/bin/bash", "-c", inner, "x", "/bin/sh", "-c", 'echo "$SHOAL_JOB_OUT"'],
                               capture_output=True, text=True)
            self.assertEqual((r.returncode, r.stdout.strip()), (0, o))
            self.assertRegex(Path(m).read_text(), r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
            # a second start (the claim already taken by a move) never runs the command
            r = subprocess.run(["/bin/bash", "-c", inner, "x", "/bin/echo", "ran"], capture_output=True, text=True)
            self.assertEqual((r.returncode, r.stdout), (75, ""))

    def test_stage_refuses_unpushed_sha(self):
        with tempfile.TemporaryDirectory() as d:
            env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.invalid",
                       GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.invalid")
            git = lambda *a, cwd=d: subprocess.run(["git", *a], cwd=cwd, env=env, check=True, capture_output=True,
                                                   text=True).stdout.strip()
            git("init", "-q", "--bare", "origin.git")
            git("clone", "-q", "origin.git", "work")
            w = os.path.join(d, "work")
            git("commit", "-q", "--allow-empty", "-m", "one", cwd=w)
            git("push", "-q", "origin", "HEAD", cwd=w)
            pushed = git("rev-parse", "HEAD", cwd=w)
            git("commit", "-q", "--allow-empty", "-m", "two", cwd=w)
            local = git("rev-parse", "HEAD", cwd=w)
            url, root = os.path.join(d, "origin.git"), os.path.join(d, "wt")
            ok = subprocess.run(["/bin/bash", "-c", self.pc.stage_script(url, pushed, root, "owner/repo", root + "/j1")],
                                capture_output=True, text=True, env=env)
            self.assertEqual(ok.returncode, 0, ok.stderr)
            self.assertEqual(git("rev-parse", "HEAD", cwd=root + "/j1"), pushed)
            again = subprocess.run(["/bin/bash", "-c", self.pc.stage_script(url, pushed, root, "owner/repo", root + "/j1")],
                                   capture_output=True, text=True, env=env)
            self.assertEqual(again.returncode, 0, again.stderr)  # restaging the same job replaces its worktree
            bad = subprocess.run(["/bin/bash", "-c", self.pc.stage_script(url, local, root, "owner/repo", root + "/j2")],
                                 capture_output=True, text=True, env=env)
            self.assertEqual(bad.returncode, 9)
            self.assertIn("SHA-NOT-ON-REMOTE", bad.stderr)
            self.assertFalse(os.path.exists(root + "/j2"))
            src = self.pc.source_of(w, "")
            self.assertEqual(src["sha"], local)
            self.assertIn("no remote-tracking branch", src["block"])
            self.assertEqual(self.pc.source_of(w, pushed)["block"], "")
            Path(w, "f").write_text("x")
            git("add", "f", cwd=w)
            self.assertIn("dirty tree", self.pc.source_of(w, "")["block"])
            with self.assertRaises(ValueError):
                self.pc.source_of(w, "no-such-ref")


if __name__ == "__main__":
    unittest.main(verbosity=1)
