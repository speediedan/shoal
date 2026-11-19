"""Lane job memory ceilings (shoal/jobs.py, shoal/pool.py): the per-job MemoryMax, the shared shoal-jobs.slice
written once per host, its shell twin for a job started over ssh, and the `result=oom-kill` verdict. systemd is
never called: systemctl and systemd-run are mocked or stubbed, and every file lands in a scratch dir."""
import argparse
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "shoal"))
import jobs  # noqa: E402
import pool  # noqa: E402


class Spec:
    def __init__(self, placement):
        self.features = {"placement": placement}


def ok(stdout=""):
    return subprocess.CompletedProcess([], 0, stdout, "")


class Sizes(unittest.TestCase):
    def test_memory_size(self):
        for v, want in ((0, ""), ("0", ""), ("", ""), (None, ""), (False, ""), ("infinity", ""), (16, "16G"),
                        (1.5, "1.5G"), ("16", "16G"), ("16g", "16G"), ("512M", "512M"), ("25%", "25%"),
                        ("0%", ""), ("lots", None), ("-1G", None), (True, None)):
            self.assertEqual(pool.memory_size(v), want, v)

    def test_feature_defaults_and_overrides(self):
        f = pool.feature(None)
        self.assertEqual((f["job_memory_max"], f["jobs_slice_memory_high"], f["jobs_slice_memory_max"]),
                         ("16G", "18G", "20G"))
        f = pool.feature(Spec({"job_memory_max": 0, "jobs_slice_memory_high": "30G", "jobs_slice_memory_max": "bad"}))
        self.assertEqual((f["job_memory_max"], f["jobs_slice_memory_high"], f["jobs_slice_memory_max"]),
                         ("", "30G", "20G"))
        self.assertEqual(f["ci_budget_min"], 90.0)

    def test_host_config_wins(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = Path(d, "host.toml")
            cfg.write_text('[jobs]\nmemory_max = "8G"\nslice_memory_max = 0\n')
            with mock.patch.dict(os.environ, {"SHOAL_CONFIG": str(cfg)}):
                lim = jobs.memory_limits(Spec({"job_memory_max": "12G", "jobs_slice_memory_high": "30G"}))
        self.assertEqual(lim, {"job_memory_max": "8G", "jobs_slice_memory_high": "30G", "jobs_slice_memory_max": ""})


class Slice(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = os.path.join(self.tmp.name, "systemd", "user")
        self.path = os.path.join(self.d, jobs.JOBS_SLICE)

    def tearDown(self):
        self.tmp.cleanup()

    def test_written_once_and_reloaded_only_when_written(self):
        with mock.patch.object(jobs, "_systemctl", return_value=ok()) as sc:
            self.assertEqual(jobs.ensure_slice("18G", "20G", self.d, reload=True), jobs.JOBS_SLICE)
            self.assertEqual(sc.call_args_list, [mock.call("daemon-reload")])
            text = Path(self.path).read_text()
            self.assertIn("MemoryHigh=18G\n", text)
            self.assertIn("MemoryMax=20G\n", text)
            self.assertTrue(text.startswith(jobs.SLICE_MARK))
            jobs.ensure_slice("18G", "20G", self.d, reload=True)        # unchanged: no write, no reload
            self.assertEqual(sc.call_count, 1)
            jobs.ensure_slice("18G", "24G", self.d, reload=True)        # its own file with new limits: rewritten
            self.assertEqual(sc.call_count, 2)
            self.assertIn("MemoryMax=24G\n", Path(self.path).read_text())
            Path(self.path).write_text("[Slice]\nMemoryMax=4G\n")      # the operator's own file: left alone
            jobs.ensure_slice("18G", "20G", self.d, reload=True)
            self.assertEqual(sc.call_count, 2)
            self.assertEqual(Path(self.path).read_text(), "[Slice]\nMemoryMax=4G\n")

    def test_off_means_no_slice(self):
        with mock.patch.object(jobs, "_systemctl", return_value=ok()) as sc:
            self.assertEqual(jobs.ensure_slice("", "", self.d, reload=True), "")
            self.assertEqual(jobs.ensure_slice_script("", ""), "")
        sc.assert_not_called()
        self.assertFalse(os.path.exists(self.path))

    def test_env_override_skips_reload(self):
        with mock.patch.dict(os.environ, {"SHOAL_SYSTEMD_USER_DIR": self.d}), \
                mock.patch.object(jobs, "_systemctl", return_value=ok()) as sc:
            jobs.ensure_slice("18G", "20G")
        sc.assert_not_called()
        self.assertTrue(os.path.exists(self.path))

    def test_shell_twin_matches(self):
        """The remote snippet writes the same file, idempotently, and leaves the operator's own file alone."""
        sh = jobs.ensure_slice_script("18G", "20G") + " && echo started"
        env = dict(os.environ, SHOAL_SYSTEMD_USER_DIR=self.d)
        for _ in range(2):
            r = subprocess.run(["bash", "-c", sh], capture_output=True, text=True, env=env)
            self.assertEqual((r.returncode, r.stdout.strip()), (0, "started"), r.stderr)
            self.assertEqual(Path(self.path).read_text(), jobs.slice_unit_text("18G", "20G"))
        Path(self.path).write_text("[Slice]\nMemoryMax=4G\n")
        subprocess.run(["bash", "-c", sh], capture_output=True, text=True, env=env)
        self.assertEqual(Path(self.path).read_text(), "[Slice]\nMemoryMax=4G\n")


def run_args(**kw):
    a = dict(sid="memtest", slug="tl", name="suite", on="", place="", cwd="", env=[], memory_high="24G",
             memory_max=None, dry_run=False, spec="", job_class="ci", cmd=["--", "pytest", "-x"])
    a.update(kw)
    return argparse.Namespace(**a)


class Run(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = self.tmp.name
        self.units = os.path.join(t, "units")
        self.patches = [mock.patch.object(jobs, "data_dir", lambda sid: os.path.join(t, "data", sid)),
                        mock.patch.object(jobs, "state_dir", lambda sid: os.path.join(t, "state", sid)),
                        mock.patch.object(jobs, "_emit", lambda *a, **k: None),
                        mock.patch.dict(os.environ, {"SHOAL_CONFIG": os.path.join(t, "none.toml"),
                                                     "SHOAL_SYSTEMD_USER_DIR": self.units})]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def start(self, a):
        calls = []

        def fake_run(argv, **kw):
            calls.append(argv)
            return ok()
        with mock.patch.object(jobs, "unit_state", return_value={"ActiveState": "inactive"}), \
                mock.patch.object(jobs.subprocess, "run", side_effect=fake_run), redirect_stdout(io.StringIO()) as out:
            rc = jobs.cmd_run(a)
        return rc, calls, out.getvalue()

    def test_default_ceilings(self):
        rc, calls, out = self.start(run_args())
        self.assertEqual(rc, 0, out)
        argv = calls[-1]
        self.assertEqual(argv[0], "systemd-run")
        self.assertEqual(argv[argv.index("--slice") + 1], jobs.JOBS_SLICE)
        self.assertIn("MemoryMax=16G", argv)
        self.assertIn("MemoryHigh=24G", argv)
        hook = next(x for x in argv if x.startswith("ExecStopPost="))
        self.assertIn("$$SERVICE_RESULT", hook)
        self.assertIn(jobs.paths("memtest", jobs.unit_name("memtest", "tl", "suite"))["result"], hook)
        self.assertIn("MemoryMax=20G", Path(self.units, jobs.JOBS_SLICE).read_text())
        meta = json.loads(Path(jobs.paths("memtest", argv[argv.index("--unit") + 1])["meta"]).read_text())
        self.assertEqual((meta["memory_max"], meta["slice"]["max"], meta["slice"]["high"]), ("16G", "20G", "18G"))

    def test_off(self):
        Path(self.tmp.name, "none.toml").write_text('[jobs]\nslice_memory_high = ""\nslice_memory_max = 0\n')
        rc, calls, out = self.start(run_args(memory_max="0"))
        self.assertEqual(rc, 0, out)
        argv = calls[-1]
        self.assertNotIn("--slice", argv)
        self.assertFalse(any(x.startswith("MemoryMax=") for x in argv))
        self.assertFalse(os.path.exists(os.path.join(self.units, jobs.JOBS_SLICE)))

    def test_explicit_and_bad_sizes(self):
        rc, calls, _ = self.start(run_args(memory_max="40G"))
        self.assertIn("MemoryMax=40G", calls[-1])
        rc, calls, out = self.start(run_args(name="bad", memory_max="huge"))
        self.assertEqual(rc, 1)
        self.assertIn("not a size", out)

    def test_remote_start_writes_slice_there(self):
        """--on: the ssh script ensures the slice on the remote host before systemd-run (run here with stubs)."""
        bindir = os.path.join(self.tmp.name, "bin")
        os.makedirs(bindir)
        log = os.path.join(self.tmp.name, "systemd-run.argv")
        Path(bindir, "systemd-run").write_text(f'#!/bin/sh\nprintf "%s\\n" "$@" > {log}\n')
        os.chmod(os.path.join(bindir, "systemd-run"), 0o755)
        rhome = os.path.join(self.tmp.name, "rhome")
        os.makedirs(os.path.join(rhome, "work"))
        env = dict(os.environ, PATH=f"{bindir}:{os.environ['PATH']}", HOME=rhome)

        def fake_ssh(host, script, timeout=60):
            return subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env)
        a = run_args(on="fakehost", cwd=os.path.join(rhome, "work"))
        with mock.patch.object(jobs, "_ssh", side_effect=fake_ssh), redirect_stdout(io.StringIO()) as out:
            rc = jobs.cmd_run(a)
        self.assertEqual(rc, 0, out.getvalue())
        argv = Path(log).read_text().splitlines()
        self.assertEqual(argv[argv.index("--slice") + 1], jobs.JOBS_SLICE)
        self.assertIn("MemoryMax=16G", argv)
        self.assertTrue(any(x.startswith("ExecStopPost=") and f"{rhome}/shoal-jobs/" in x for x in argv))
        self.assertIn("MemoryMax=20G", Path(self.units, jobs.JOBS_SLICE).read_text())


class Verdict(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = self.tmp.name
        self.p = {k: os.path.join(t, f"u.{k}") for k in ("log", "rc", "result", "meta", "granted")}
        Path(self.p["log"]).write_text("collected 900 items\n")
        self.meta = {"unit": "u", "memory_max": "16G", "slice": {"name": jobs.JOBS_SLICE, "high": "18G", "max": "20G"}}
        Path(self.p["meta"]).write_text(json.dumps(self.meta))
        self.a = argparse.Namespace(sid="memtest", slug="tl", poll=0, tail=5, timeout_min=0)

    def tearDown(self):
        self.tmp.cleanup()

    def wait_local(self, state):
        with mock.patch.object(jobs, "unit_state", return_value=state), mock.patch.object(jobs, "_emit"), \
                redirect_stdout(io.StringIO()) as out:
            rc = jobs._wait_local(self.a, "u", self.p, self.meta)
        return rc, out.getvalue()

    def test_local_oom_from_result_file(self):
        Path(self.p["result"]).write_text("oom-kill killed KILL\n")
        rc, out = self.wait_local({"LoadState": "not-found", "ActiveState": "inactive"})
        self.assertEqual(rc, 1)
        self.assertIn("JOB-VERDICT u result=oom-kill exit=-", out)
        self.assertIn("MemoryMax=16G", out)
        self.assertIn("shoal-jobs.slice", out)
        self.assertNotIn("UNIT-GONE", out)

    def test_local_oom_from_unit_result_beats_rc(self):
        Path(self.p["rc"]).write_text("137\n")
        rc, out = self.wait_local({"LoadState": "loaded", "ActiveState": "failed", "Result": "oom-kill"})
        self.assertIn("result=oom-kill exit=137", out)

    def test_local_ordinary_failure_unchanged(self):
        Path(self.p["rc"]).write_text("3\n")
        Path(self.p["result"]).write_text("exit-code exited 3\n")
        _, out = self.wait_local({"LoadState": "not-found", "ActiveState": "inactive"})
        self.assertIn("JOB-VERDICT u result=failed exit=3", out)

    def test_remote_oom(self):
        st = {"LoadState": "not-found", "ActiveState": "inactive", "rc": "", "granted": "", "rc_mtime": "",
              "boot_id": "b1", "result": "oom-kill killed KILL"}
        meta = dict(self.meta, host="fakehost", boot_id="b1", log="/r/u.log")
        with mock.patch.object(jobs, "remote_state", return_value=st), mock.patch.object(jobs, "_emit"), \
                mock.patch.object(jobs, "_ssh", return_value=ok("tail\n")), redirect_stdout(io.StringIO()) as out:
            rc = jobs._wait_remote(self.a, "u", meta)
        self.assertEqual(rc, 1)
        self.assertIn("JOB-VERDICT u result=oom-kill exit=-", out.getvalue())
        self.assertIn("fakehost journal", out.getvalue())

    def test_remote_state_reads_result(self):
        stdout = ("LoadState=not-found\nActiveState=inactive\n---\n\n---\n\n---\n\n---\nb1\n---\n"
                  "oom-kill killed KILL\n")
        with mock.patch.object(jobs, "_ssh", return_value=ok(stdout)):
            st = jobs.remote_state("fakehost", "u")
        self.assertEqual((st["boot_id"], st["result"]), ("b1", "oom-kill killed KILL"))


if __name__ == "__main__":
    unittest.main(verbosity=1)
