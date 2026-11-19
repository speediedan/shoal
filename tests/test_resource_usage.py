"""Synthetic host/lease observations: no GPU, network or systemd access."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "shoal"))
import resource_usage as r


class Resources(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.gpus = [{"uuid": "GPU-a", "vendor": "amd", "utilization_pct": 0, "vram_total_mib": 32000, "vram_used_mib": 100},
                     {"uuid": "GPU-b", "vendor": "amd", "utilization_pct": 0, "vram_total_mib": 32000, "vram_used_mib": 100}]

    def sample(self, ts, held=True, busy=0, ok=True, mode="exclusive"):
        provider = {"ok": ok, "leases": [{"key": "gpu", "held": held, "mode": mode, "pid": 123,
                    "holder": "test-job", "since": r.utc(0)}, {"key": "cpu-heavy", "held": False}], "queue": []}
        gpus = copy.deepcopy(self.gpus)
        gpus[0]["utilization_pct"] = busy
        return {"type": "resource_sample", "host": "host-a", "boot_id": "boot-a", "ts": ts, "ts_utc": r.utc(ts),
                "gpus": gpus, "leases": r.normalized_leases(provider, gpus, "host-a", "boot-a"),
                "lease_provider": {"ok": ok, "known_keys": ["gpu", "cpu-heavy"]}, "queue": [],
                "memory": {"total_bytes": 128*r.GIB, "available_bytes": 100*r.GIB},
                "capabilities": {"session_ready": ["example-harness"]}}

    def test_exclusive_and_shared_scope(self):
        exclusive = self.sample(0)
        self.assertEqual(exclusive["leases"][0]["devices"], ["GPU-a", "GPU-b"])
        self.assertEqual(exclusive["leases"][0]["scope"], "host_gpu")
        provider = {"leases": [{"key": "gpu", "held": True, "mode": "shared"},
                              {"key": "gpu@GPU-a", "held": True, "pid": 1},
                              {"key": "cpu-heavy", "held": True, "pid": 1}]}
        leases = r.normalized_leases(provider, self.gpus, "host-a", "boot-a")
        self.assertEqual([x["scope"] for x in leases], ["device_gpu", "host_cpu_memory"])
        self.assertEqual(leases[0]["devices"], ["GPU-a"])
        self.assertFalse(leases[1]["gpu_reserved"])

    def test_lifecycle_and_time_weighted_coverage(self):
        events = []
        for ts, held, busy in ((0, True, 0), (10, True, 20), (20, True, None), (30, False, 0)):
            events += r.append_sample(self.root, self.sample(ts, held, busy))
        self.assertEqual(sum(x["type"] == "lease_observed_acquire" for x in events), 1)
        release = next(x for x in events if x["type"] == "lease_observed_release")
        self.assertEqual((release["release_after"], release["release_by"]), (20, 30))
        lease = r.efficiency(events)[0]
        self.assertEqual(lease["reserved_s"], 30)
        self.assertEqual(lease["duration_kind"], "observed_upper_bound")
        gpu0, gpu1 = lease["devices_report"]
        self.assertEqual(gpu0["covered_s"], 20)
        self.assertAlmostEqual(gpu0["coverage_pct"], 200/3)
        self.assertEqual(gpu0["mean_utilization_pct"], 10)
        self.assertEqual(gpu0["active_fraction_pct"], 50)
        self.assertEqual(gpu1["active_fraction_pct"], 0)
        self.assertAlmostEqual(lease["mean_host_memory_used_pct"], 100*28/128)

    def test_closed_days_compress_and_retention_drops_old_days(self):
        day = 86400
        host = self.root / "host-a"
        r.append_sample(self.root, self.sample(0))                    # day 1970-01-01
        r.append_sample(self.root, self.sample(day + 10))             # first sample of day 2
        self.assertTrue((host / "events-19700101.jsonl.gz").exists(), "a closed day is compressed")
        self.assertFalse((host / "events-19700101.jsonl").exists())
        self.assertTrue((host / "events-19700102.jsonl").exists(), "the open day stays plain")
        # a late append to a compressed day becomes one more gzip member, read back in full
        (host / "events-19700101.jsonl").write_text(json.dumps({"ts": 5, "type": "late"}) + "\n")
        r.append_sample(self.root, self.sample(2 * day + 10))         # day 3 compacts again
        events = r.load_events([self.root])
        self.assertEqual(sorted(e["type"] for e in events if e["ts"] < day),
                         ["late", "lease_observed_acquire", "resource_sample"])
        self.assertEqual(len([e for e in events if e.get("type") == "resource_sample"]), 3)
        # retention: compressed days older than the window are removed, younger ones kept
        r.append_sample(self.root, self.sample(5 * day + 10), retain_days=3)
        self.assertFalse((host / "events-19700101.jsonl.gz").exists())
        self.assertTrue((host / "events-19700103.jsonl.gz").exists())

    def test_failure_gap_and_new_episode(self):
        events = r.append_sample(self.root, self.sample(0))
        failed = self.sample(10, False, ok=False)
        self.assertEqual(len(r.append_sample(self.root, failed)), 1, "unknown provider never releases a lease")
        events += r.append_sample(self.root, self.sample(100))
        self.assertIsNone(r.efficiency(events)[0]["devices_report"][0]["active_fraction_pct"])
        events += r.append_sample(self.root, self.sample(110, False))
        next_events = r.append_sample(self.root, self.sample(120))
        self.assertNotEqual(events[0]["leases"][0]["lease_id"], next_events[0]["leases"][0]["lease_id"],
                            "reappearing unattributed claims get separate observed episodes")

    def test_idle_control_and_admission(self):
        sample = self.sample(100, held=False, busy=7)
        answer = r.placement([sample], gpu_count=1, gpu_vendor="amd", vram_mib=24000, now=100)
        self.assertEqual(answer["recommended_host"], "host-a")
        self.assertTrue(answer["advisory_only"])
        self.assertIsNone(r.placement([sample], gpu_count=1, now=100)["recommended_host"],
                          "unknown workload compatibility cannot become a cross-vendor recommendation")
        self.assertIsNone(r.placement([sample], gpu_count=1, gpu_vendor="nvidia", now=100)["recommended_host"])

        self.assertIsNone(r.placement([self.sample(100)], gpu_count=1, gpu_vendor="amd", now=100)["recommended_host"])
        self.assertIsNone(r.placement([sample], gpu_count=1, gpu_vendor="amd", now=1000)["recommended_host"])
        self.assertIsNone(r.placement([sample], harness="missing-harness", now=100)["recommended_host"])
        self.assertIsNone(r.placement([sample], memory_gib=101, now=100)["recommended_host"])
        sample["gpus"][0]["utilization_pct"] = None
        self.assertIsNone(r.placement([sample], gpu_count=2, gpu_vendor="amd", now=100)["recommended_host"])
        sample["queue"] = [{"key": "gpu"}]
        self.assertIsNone(r.placement([sample], gpu_count=1, gpu_vendor="amd", now=100)["recommended_host"])

    def test_unknown_provider(self):
        self.assertFalse(r.lease_observation("")["ok"])
        with patch.object(r, "run", return_value=type("Result", (), {"returncode":0, "stdout":'{"leases": [], "queue": []}'})()):
            self.assertFalse(r.lease_observation("synthetic-provider")["ok"])

    def test_amd_sysfs_identity_and_missing_sensor(self):
        device = self.root / "card7/device"
        device.mkdir(parents=True)
        for key, value in {"vendor":"0x1002", "unique_id":"0xabcd", "gpu_busy_percent":"7",
                           "mem_info_vram_total":str(32*r.GIB), "mem_info_vram_used":str(r.GIB)}.items():
            (device / key).write_text(value)
        integrated = self.root / "card8/device"
        integrated.mkdir(parents=True)
        (integrated / "vendor").write_text("0x1002")
        with patch.object(r.shutil, "which", return_value=None):
            rows, errors = r.gpu_observations(self.root)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["uuid"], "GPU-000000000000abcd")
            self.assertEqual(rows[0]["vram_total_mib"], 32768)
            (device / "gpu_busy_percent").unlink()
            self.assertIsNone(r.gpu_observations(self.root)[0][0]["utilization_pct"])
            (device / "power").mkdir()
            (device / "power/runtime_status").write_text("suspended")
            suspended = r.gpu_observations(self.root)[0][0]
            self.assertEqual(suspended["runtime_status"], "suspended")
            self.assertIsNone(suspended["utilization_pct"], "suspend is recorded separately, never as a fabricated zero")
            sample = self.sample(100, held=False)
            sample["gpus"] = [suspended]
            self.assertEqual(r.placement([sample], gpu_count=1, gpu_vendor="amd", now=100)["recommended_host"], "host-a")

    def test_install_enable_restarts_a_running_service(self):
        """--enable must restart an already-active unit: `enable --now` leaves the old process (and its
        old ExecStart) running after the unit file is rewritten."""
        import argparse
        a = argparse.Namespace(resource_cmd="install", directory=str(self.root / "out"), host="host-a",
                               lease_command="", interval=15, session_ready="", prom_out="", role_time_spec=[],
                               dry_run=False, enable=True)
        with patch.object(r.Path, "home", return_value=self.root), patch.object(r.subprocess, "run") as run:
            self.assertEqual(r.execute(a), 0)
        calls = [c.args[0] for c in run.call_args_list]
        self.assertEqual(calls, [["systemctl", "--user", "daemon-reload"],
                                 ["systemctl", "--user", "enable", "shoal-resources.service"],
                                 ["systemctl", "--user", "restart", "shoal-resources.service"]])
        unit = (self.root / ".config/systemd/user/shoal-resources.service").read_text()
        self.assertIn("ExecStart=", unit)
        self.assertNotIn("--no-role-time-live", unit, "the caches follow the live specs unless opted out")
        a.no_role_time_live, a.enable = True, False
        with patch.object(r.Path, "home", return_value=self.root), patch.object(r.subprocess, "run"):
            r.execute(a)
        self.assertIn('"--no-role-time-live"', (self.root / ".config/systemd/user/shoal-resources.service").read_text())
        with patch.object(r.Path, "home", return_value=self.root), patch.object(r.subprocess, "run") as run:
            r.execute(a)
        self.assertEqual([c.args[0] for c in run.call_args_list], [["systemctl", "--user", "daemon-reload"]])


MIB = 1024 ** 2


def plant_cgroup(root, rel, cur_mib, peak_mib=None, anon_mib=0, file_mib=0, oom=0):
    d = Path(root, rel)
    d.mkdir(parents=True, exist_ok=True)
    (d / "memory.current").write_text(f"{cur_mib * MIB}\n")
    if peak_mib is not None:
        (d / "memory.peak").write_text(f"{peak_mib * MIB}\n")
    (d / "memory.stat").write_text(f"anon {anon_mib * MIB}\nfile {file_mib * MIB}\nkernel 4096\n")
    (d / "memory.events").write_text(f"low 0\nhigh 3\nmax 1\noom 1\noom_kill {oom}\noom_group_kill 0\n")
    return d


def plant_proc(proc, pid, rss_mib, comm, cg, uid_ok=True):
    d = Path(proc, str(pid))
    d.mkdir(parents=True)
    pages = rss_mib * MIB // 4096
    (d / "statm").write_text(f"{pages * 2} {pages} 10 1 0 5 0\n")
    (d / "comm").write_text(comm + "\n")
    (d / "cgroup").write_text(f"0::/{cg}\n")


class FastPath(unittest.TestCase):
    """The 2 s fast path on a planted cgroup tree and /proc: files only, no subprocess."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        t = Path(self.tmp.name)
        self.cg, self.proc, self.out = t / "cgroup", t / "proc", t / "out"
        (self.proc / "pressure").mkdir(parents=True)
        (self.proc / "pressure" / "memory").write_text(
            "some avg10=12.50 avg60=4.00 avg300=1.00 total=123\nfull avg10=6.25 avg60=2.00 avg300=0.50 total=99\n")
        (self.proc / "meminfo").write_text("MemTotal: 65536000 kB\nMemAvailable: 2097152 kB\nSwapTotal: 0 kB\n"
                                           "SwapFree: 1024 kB\nDirty: 51200 kB\n")
        u = "user.slice/user-1000.slice/user@1000.service"
        plant_cgroup(self.cg, "system.slice", 900, peak_mib=1200)
        plant_cgroup(self.cg, "user.slice/user-1000.slice", 30000)
        plant_cgroup(self.cg, f"{u}/app.slice/shoal-g-r-suite.service", 14700, peak_mib=15000, anon_mib=14000,
                     file_mib=600)
        plant_cgroup(self.cg, f"{u}/app.slice/tiny.service", 5)
        plant_cgroup(self.cg, f"{u}/app.slice/small.service", 20)
        plant_cgroup(self.cg, "user.slice/user-1001.slice", 26000)
        self.docker = "user.slice/user-1001.slice/user@1001.service/user.slice/docker-abc123.scope"
        plant_cgroup(self.cg, self.docker, 25000, anon_mib=24000)
        plant_cgroup(self.cg, self.docker + "/inner", 100)  # a container's own cgroups are not walked
        plant_proc(self.proc, 4242, 14700, "pytest", f"{u}/app.slice/shoal-g-r-suite.service")
        plant_proc(self.proc, 5151, 9000, "python", self.docker)
        for i in range(6):
            plant_proc(self.proc, 100 + i, 10 + i, f"small{i}", f"{u}/app.slice/tiny.service")
        (self.proc / "self").mkdir()  # not a pid
        self.f = r.FastSampler("host-a", self.out, proc=str(self.proc), cgroup_root=str(self.cg), min_mib=64)

    def test_record_reads_the_planted_tree(self):
        with patch.object(r.subprocess, "run", side_effect=AssertionError("the fast path runs no subprocess")), \
                patch.object(r.subprocess, "Popen", side_effect=AssertionError("the fast path runs no subprocess")):
            rec = self.f.record(now=1_800_000_000.0)
        self.assertEqual(rec["type"], "fast_sample")
        self.assertEqual(rec["psi"], {"some10": 12.5, "some60": 4.0, "full10": 6.25, "full60": 2.0})
        self.assertEqual(rec["mem"], {"avail_mib": 2048, "swap_free_mib": 1, "dirty_mib": 50})
        cg = rec["cg"]
        self.assertEqual(cg["user@1000/app.slice/shoal-g-r-suite.service"],
                         {"cur": 14700, "peak": 15000, "anon": 14000, "file": 600, "oom": 0})
        self.assertEqual(cg["system.slice"]["peak"], 1200)
        self.assertIn("user.slice/user-1000.slice", cg)
        self.assertIn("user.slice/user-1001.slice", cg)
        self.assertEqual(cg["user@1001/user.slice/docker-abc123.scope"]["cur"], 25000)
        self.assertFalse(any(k.endswith("/inner") for k in cg))
        self.assertNotIn("user@1000/app.slice/tiny.service", cg)
        self.assertEqual(rec["rest"], {"n": 2, "cur": 25})
        self.assertEqual([p["pid"] for p in rec["top"]], [4242, 5151, 105, 104, 103])
        self.assertEqual(rec["top"][0]["comm"], "pytest")
        self.assertEqual(rec["top"][0]["rss_mib"], 14700)
        self.assertEqual(rec["top"][1]["cg"], "user@1001/user.slice/docker-abc123.scope")
        self.assertNotIn("errors", rec)

    def test_written_per_day_rotated_and_bounded(self):
        import gzip
        day = 86400.0
        t0 = 1_800_000_000.0
        for d in range(10):
            self.f.write(self.f.record(now=t0 + d * day))
        files = sorted(p.name for p in (self.out / "host-a").iterdir())
        plain = [f for f in files if f.endswith(".jsonl")]
        gz = [f for f in files if f.endswith(".jsonl.gz")]
        self.assertEqual(len(plain), 1, files)
        self.assertTrue(all(f.startswith("fast-") for f in files), files)
        self.assertLessEqual(len(gz), r.FAST_RETAIN_DAYS + 1, files)
        with gzip.open(self.out / "host-a" / gz[-1], "rt") as fh:
            self.assertEqual(json.loads(fh.readline())["type"], "fast_sample")
        self.assertEqual(r.load_events([self.out]), [], "fast records are not resource events")

    def test_oom_kill_logged_at_once_and_summarised(self):
        import io
        from contextlib import redirect_stdout
        unit = Path(self.cg, "user.slice/user-1000.slice/user@1000.service/app.slice/shoal-g-r-suite.service")
        with redirect_stdout(io.StringIO()) as out:
            self.f.tick(now=1_800_000_000.0)
            (unit / "memory.events").write_text("oom 1\noom_kill 2\n")
            self.f.tick(now=1_800_000_002.0)
        lines = [json.loads(x) for x in out.getvalue().splitlines()]
        self.assertEqual(len(lines), 1, lines)
        self.assertEqual(lines[0]["oom_kill"]["cgroups"], {"user@1000/app.slice/shoal-g-r-suite.service": 2})
        s = self.f.summary(15)["resources_summary"]
        self.assertEqual(s["samples"], 2)
        self.assertEqual(s["oom_kills"], {"user@1000/app.slice/shoal-g-r-suite.service": 2})
        self.assertEqual((s["avail_min_mib"], s["some10_max"], s["full10_max"]), (2048, 12.5, 6.25))
        self.assertEqual(s["max_cg"]["path"], "user@1001/user.slice/docker-abc123.scope")
        self.assertEqual(self.f.summary(15)["resources_summary"]["samples"], 0, "a summary opens a new window")

    def test_missing_files_are_errors_logged_once(self):
        import io
        from contextlib import redirect_stdout
        f = r.FastSampler("host-a", self.out, proc=str(self.proc / "absent"), cgroup_root=str(self.cg / "absent"))
        with redirect_stdout(io.StringIO()) as out:
            rec = f.tick(now=1_800_000_000.0)
            f.tick(now=1_800_000_002.0)
        self.assertEqual(rec["psi"], {})
        self.assertEqual(rec["mem"], {})
        self.assertEqual(len(rec["errors"]), 3)
        self.assertEqual(len(out.getvalue().splitlines()), 3, "each distinct error is logged once")

    def test_watch_does_not_echo_samples(self):
        """watch logs a start line, then only summaries: neither the full sample nor a fast record reaches stdout."""
        import argparse
        import io
        from contextlib import redirect_stdout
        a = argparse.Namespace(resource_cmd="watch", directory=str(self.out), host="host-a", lease_command="",
                               interval=0.05, session_ready="", prom_out="", role_time_spec=[], no_role_time_live=True,
                               retain_days=90, fast_interval=0.01, fast_retain_days=7, summary_min=0.002,
                               fast_min_mib=64, proc_root=str(self.proc), cgroup_root=str(self.cg))
        fake = {"v": 1, "type": "resource_sample", "ts": 1_800_000_000.0, "ts_utc": r.utc(1_800_000_000.0),
                "host": "host-a", "boot_id": "b", "gpus": [], "gpu_errors": [], "memory": {},
                "lease_provider": {"ok": False, "error": "none", "known_keys": []}, "leases": [], "queue": []}
        ticks = []
        orig = r.FastSampler.tick

        def counted(self_, now=None):
            ticks.append(1)
            return orig(self_, now)
        with patch.object(r, "snapshot", return_value=fake), patch.object(r.FastSampler, "tick", counted), \
                redirect_stdout(io.StringIO()) as out:
            r.watch(a, stop=lambda: len(ticks) >= 25)
        lines = [json.loads(x) for x in out.getvalue().splitlines()]
        kinds = {next(iter(x)) for x in lines}
        self.assertIn("resources_watch_start", kinds)
        self.assertIn("resources_summary", kinds)
        self.assertLessEqual(kinds, {"resources_watch_start", "resources_summary"}, lines)
        fast = list((self.out / "host-a").glob("fast-*.jsonl"))
        self.assertEqual(len(fast), 1)
        self.assertGreaterEqual(len(fast[0].read_text().splitlines()), 25)
        self.assertTrue(list((self.out / "host-a").glob("events-*.jsonl")), "full samples still run in their thread")

    def test_watch_exits_when_a_full_sample_fails(self):
        import argparse
        import io
        from contextlib import redirect_stdout
        a = argparse.Namespace(resource_cmd="watch", directory=str(self.out), host="host-a", lease_command="",
                               interval=0.05, session_ready="", prom_out="", role_time_spec=[], no_role_time_live=True,
                               retain_days=90, fast_interval=0.01, summary_min=0, proc_root=str(self.proc),
                               cgroup_root=str(self.cg))
        with patch.object(r, "snapshot", side_effect=RuntimeError("probe broke")), redirect_stdout(io.StringIO()):
            with self.assertRaises(RuntimeError):
                r.watch(a)


if __name__ == "__main__":
    unittest.main()
