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


if __name__ == "__main__":
    unittest.main()
