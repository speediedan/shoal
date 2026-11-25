"""Research reservations taken all or nothing (shoal/pool_reserve.py), and a job's memory pressure (shoal/jobs.py,
shoal/health.py). A fake lease helper with real flock semantics stands in for the host's (one lock file per device,
a background holder per hold, `--timeout 0` tries once); devices, lease files and cgroup files live under a temp
root. No GPU, systemd, network or ssh."""
import argparse
import contextlib
import importlib
import io
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

SHOAL = Path(__file__).resolve().parents[1] / "shoal"
sys.path.insert(0, str(SHOAL))
import config  # noqa: E402

# A lease helper with the contract shoal uses: --status --json, --hold --pidfile P (a background holder that keeps a
# flock on each key it holds; it exits 75 when the key is not free within --timeout), --release --pidfile P. A named
# device is the only candidate when GPU_LEASE_DEVICES names one; else any device in FAKE_DEVICES, smallest first.
HELPER = r'''#!/usr/bin/env python3
import fcntl, json, os, signal, sys, time
D = os.environ["FAKE_LEASE_DIR"]
DEVS = [d for d in os.environ.get("FAKE_DEVICES", "").split(",") if d]
args = sys.argv[1:]
def opt(name, default=None):
    return args[args.index(name) + 1] if name in args else default
def lockpath(key):
    return os.path.join(D, key + ".lock")
def held(key):
    with open(lockpath(key), "a") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return False
        except OSError:
            return True
def holder(key):
    try:
        return open(os.path.join(D, key + ".holder")).read().strip()
    except OSError:
        return None
if "--status" in args:
    keys = ["gpu@" + d for d in DEVS] + ["cpu-heavy"]
    rows = [{"key": "gpu", "held": any(held(k) for k in keys[:-1]), "mode": "shared"}]
    rows += [dict({"key": k, "held": held(k)}, **({"holder": holder(k)} if held(k) else {})) for k in keys]
    print(json.dumps({"leases": rows, "queue": []}))
    sys.exit(0)
if "--release" in args:
    pf = opt("--pidfile")
    try:
        os.killpg(int(open(pf).read().strip()), signal.SIGTERM)
    except (OSError, ValueError):
        pass
    for x in (pf, pf + ".device"):
        try:
            os.remove(x)
        except OSError:
            pass
    sys.exit(0)
if "--hold" in args:
    pf = opt("--pidfile")
    timeout = float(opt("--timeout", "3600"))
    with open(os.path.join(D, "calls"), "a") as fh:
        fh.write(" ".join(args) + " DEVICES=" + os.environ.get("GPU_LEASE_DEVICES", "") + "\n")
    if opt("--lease") == "cpu-heavy":
        cands = ["cpu-heavy"]
    else:
        named = [x.split(":")[0] for x in os.environ.get("GPU_LEASE_DEVICES", "").split(",") if x]
        cands = ["gpu@" + d for d in (named or DEVS)]
    r, w = os.pipe()
    if os.fork() == 0:
        os.close(r)
        os.setsid()
        nul = os.open(os.devnull, os.O_RDWR)
        for fd in (0, 1, 2):
            os.dup2(nul, fd)
        end = time.time() + timeout
        while True:
            for k in cands:
                fh = open(lockpath(k), "a")
                try:
                    fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError:
                    fh.close()
                    continue
                open(os.path.join(D, k + ".holder"), "w").write(os.environ.get("GPU_LEASE_PROJECT", "?"))
                open(pf + ".device", "w").write(k[4:] if k.startswith("gpu@") else "all")
                open(pf, "w").write(str(os.getpid()))
                os.write(w, b"ok")
                os.close(w)
                while True:
                    signal.pause()
            if time.time() >= end:
                os.write(w, b"busy")
                os._exit(75)
            time.sleep(0.05)
    os.close(w)
    got = os.read(r, 8)
    if got == b"ok":
        sys.exit(0)
    print("gpu_lease: timed out waiting", file=sys.stderr)
    sys.exit(75)
sys.exit(2)
'''

DEVS = {"GPU-small": 8192, "GPU-big": 24576}


class Host:
    """A temp pool host: config, data root, resource sample, fake helper and lease directory."""

    def __init__(self, devs=DEVS):
        self.d = Path(tempfile.mkdtemp(prefix="shoal-resv-"))
        self.data = self.d / "data"
        self.leases = self.d / "leases"
        self.leases.mkdir()
        self.helper = self.d / "lease"
        self.helper.write_text(HELPER)
        self.helper.chmod(0o755)
        self.cfg = self.d / "host.toml"
        self.cfg.write_text(f'data_root = "{self.data}"\nlease_cmd = "{self.helper}"\n')
        sdir = self.data / "_resources" / "testhost"
        sdir.mkdir(parents=True)
        now = time.time()
        (sdir / time.strftime("events-%Y%m%d.jsonl", time.gmtime(now))).write_text(json.dumps(
            {"type": "resource_sample", "ts": now, "memory": {"available_bytes": 1 << 34},
             "gpus": [{"uuid": u, "vendor": "nvidia", "vram_total_mib": m, "arch": "sm_89"}
                      for u, m in devs.items()]}) + "\n")
        self.env = {"SHOAL_CONFIG": str(self.cfg), "SHOAL_DATA_ROOT": str(self.data), "SHOAL_SERVER": "testhost",
                    "SHOAL_HOLD_SCOPE": "0", "SHOAL_POOL_POLL_S": "0.1", "FAKE_LEASE_DIR": str(self.leases),
                    "FAKE_DEVICES": ",".join(devs)}
        for k in ("SHOAL_AZURE_ORG", "SHOAL_LEASE_CMD", "GPU_LEASE_CMD"):
            os.environ.pop(k, None)
        self.patch = patch.dict(os.environ, self.env)
        self.patch.start()
        config._CACHE.clear()
        import pool_reserve
        self.pr = importlib.reload(pool_reserve)
        self.procs = []

    def close(self):
        for p in self.procs:
            if p.poll() is None:
                p.kill()
        for pf in list(self.d.rglob("*.pid")):
            with contextlib.suppress(OSError, ValueError):
                os.killpg(int(pf.read_text().strip()), signal.SIGKILL)
        self.patch.stop()
        config._CACHE.clear()
        shutil.rmtree(self.d, ignore_errors=True)

    def outside_hold(self, dev, name):
        """A hold someone else (a CI job) takes straight from the helper."""
        pf = str(self.d / f"outside-{name}.pid")
        r = subprocess.run([str(self.helper), "--gpus", "1", "--timeout", "0", "--hold", "--pidfile", pf],
                           env=dict(os.environ, GPU_LEASE_DEVICES=f"{dev}:0", GPU_LEASE_PROJECT=f"ci-{name}"))
        assert r.returncode == 0, r
        return pf

    def drop(self, pf):
        subprocess.run([str(self.helper), "--release", "--pidfile", pf], env=os.environ)

    def reserve_cli(self, resources, until="job:study", timeout_min="1"):
        p = subprocess.Popen([sys.executable, str(SHOAL / "cli.py"), "pool", "reserve", "--resources", resources,
                              "--for", "resvtest/r", "--until", until, "--timeout-min", timeout_min, "--json", "--no-event"],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=os.environ)
        self.procs.append(p)
        return p

    def held_by(self):
        """{device: holder project} as the helper sees it."""
        out = json.loads(subprocess.run([str(self.helper), "--status", "--json"], capture_output=True, text=True,
                                        env=os.environ).stdout)
        return {r["key"][4:]: r.get("holder") for r in out["leases"] if r["key"].startswith("gpu@") and r["held"]}

    def records(self):
        return self.pr.load()


def wait_for(pred, timeout=30.0, step=0.05):
    end = time.time() + timeout
    while time.time() < end:
        v = pred()
        if v:
            return v
        time.sleep(step)
    raise AssertionError("condition not reached")


class AllOrNothing(unittest.TestCase):
    def setUp(self):
        self.h = Host()
        self.addCleanup(self.h.close)

    def test_two_two_device_requests_do_not_deadlock(self):
        # defect 1: both devices busy with outside holders, two gpus:2 requests queued, devices freed one at a time.
        # The old code took one device per request as each freed and both then waited forever.
        h = self.h
        small, big = h.outside_hold("GPU-small", "a"), h.outside_hold("GPU-big", "b")
        p1 = h.reserve_cli("gpus:2")
        p2 = h.reserve_cli("gpus:2")
        wait_for(lambda: len(h.records()) == 2 and all(r.get("state") == "waiting" for r in h.records().values()))
        h.drop(small)  # one device frees: neither request may take it alone
        time.sleep(1.0)
        self.assertEqual(h.held_by(), {"GPU-big": "ci-b"})
        self.assertTrue(all(r["state"] == "waiting" and not r["holds"] for r in h.records().values()))
        h.drop(big)  # both free: exactly one request takes both
        first = wait_for(lambda: [r for r in h.records().values() if r.get("state") == "held"])
        self.assertEqual(len(first), 1)
        winner = first[0]
        self.assertEqual(sorted(d["uuid"] for d in winner["devices"]), ["GPU-big", "GPU-small"])
        time.sleep(0.5)
        loser = [r for r in h.records().values() if r["id"] != winner["id"]][0]
        self.assertEqual((loser["state"], loser["holds"], loser["devices"]), ("waiting", [], []))
        self.assertIn("by reservation " + winner["id"], loser["waiting"])
        self.assertEqual(set(h.held_by().values()), {"research:resvtest/r"})
        self.assertEqual(len(list((h.data / "_pool" / "holds").glob(f"{loser['id']}-*.pid"))), 0)  # it holds nothing
        # the first ends: the second takes both
        self.assertEqual(self.h.pr.release(winner["id"])["released"], "released")
        second = wait_for(lambda: (h.records().get(loser["id"]) or {}).get("state") == "held" and h.records()[loser["id"]])
        self.assertEqual(sorted(d["uuid"] for d in second["devices"]), ["GPU-big", "GPU-small"])
        for p in (p1, p2):
            out, err = p.communicate(timeout=30)
            self.assertEqual(p.returncode, 0, out + err)
            self.assertIn("RESERVE-WAITING", err)  # each said it waits, holding nothing, and how to cancel it
        self.h.pr.release(loser["id"])

    def test_waiting_request_holds_nothing_and_release_cancels_it(self):
        # defect 3 of the report: `pool release` of a request never granted cancels it and its waiting `pool reserve`
        h = self.h
        busy = h.outside_hold("GPU-big", "a")
        p = h.reserve_cli("gpu@GPU-big,gpu@GPU-small")
        rec = wait_for(lambda: [r for r in h.records().values() if r.get("waiting", "starting") != "starting"])[0]
        self.assertIn("gpu@GPU-big is held by ci-a", rec["waiting"])
        self.assertEqual(h.held_by(), {"GPU-big": "ci-a"})  # the free GPU-small was not taken while it waits
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = h.pr.cmd_release(argparse.Namespace(id=rec["id"], host="", json=False, no_event=True))
        self.assertEqual(rc, 0)
        self.assertIn(f"CANCELLED {rec['id']}", out.getvalue())
        so, se = p.communicate(timeout=30)
        self.assertEqual(p.returncode, 3, so + se)
        self.assertTrue(json.loads(so)["cancelled"])
        self.assertIn(f"RESERVE-WAITING {rec['id']}", se)
        self.assertIn(f"pool release {rec['id']} --host testhost", se)
        self.assertEqual(h.records(), {})
        self.assertEqual(h.held_by(), {"GPU-big": "ci-a"})
        h.drop(busy)

    def test_a_record_an_older_expire_dropped_is_written_back(self):
        # at upgrade time a `resources watch` still running the old code drops a record with no holds as "holder
        # gone": the waiting request writes it back and keeps waiting (only `pool release` cancels it)
        h = self.h
        busy = h.outside_hold("GPU-big", "a")
        p = h.reserve_cli("gpu@GPU-big")
        rid = wait_for(lambda: list(h.records()))[0]
        h.pr._drop(rid)
        wait_for(lambda: rid in h.records())
        self.assertIsNone(p.poll())
        h.drop(busy)
        so, se = p.communicate(timeout=30)
        self.assertEqual(p.returncode, 0, so + se)
        self.assertEqual(json.loads(so)["id"], rid)
        h.pr.release(rid)

    def test_an_older_request_with_no_record_is_cancelled_too(self):
        # a request an older copy of this code was taking at upgrade time: a `.pending` marker and pidfiles, no record
        h = self.h
        hdir = h.data / "_pool" / "holds"
        hdir.mkdir(parents=True, exist_ok=True)
        waiter = subprocess.Popen(["sleep", "60"])
        h.procs.append(waiter)
        (hdir / "r0101-old1.pending").write_text(str(waiter.pid))
        took = str(hdir / "r0101-old1-0.pid")
        subprocess.run([str(h.helper), "--gpus", "1", "--timeout", "0", "--hold", "--pidfile", took],
                       env=dict(os.environ, GPU_LEASE_DEVICES="GPU-small:0", GPU_LEASE_PROJECT="research:resvtest/r"))
        self.assertEqual(h.held_by(), {"GPU-small": "research:resvtest/r"})
        rec = h.pr.release("r0101-old1")
        self.assertTrue(rec["released"].startswith("cancelled"))
        waiter.wait(timeout=10)
        wait_for(lambda: h.held_by() == {})
        self.assertEqual(list(hdir.glob("r0101-old1*")), [])
        self.assertIsNone(h.pr.release("r0101-nothing"))

    def test_a_held_device_is_never_granted_twice(self):
        # defect 2: a second request for a device a live reservation holds waits (here: times out), same role or not
        h = self.h
        a = h.pr.reserve("gpu@GPU-small", "resvtest/r", "job:one")
        with self.assertRaises(ValueError) as cm:
            h.pr.reserve("gpu@GPU-small", "resvtest/r", "job:two", timeout_min=0.01)
        self.assertIn(f"gpu@GPU-small is held by reservation {a['id']}", str(cm.exception))
        self.assertEqual(list(h.records()), [a["id"]])
        b = h.pr.reserve("gpus:1", "resvtest/r", "job:two")  # gpus:N picks around it
        self.assertEqual([d["uuid"] for d in b["devices"]], ["GPU-big"])
        # its holder dies (a launcher unit that ended took it along) while its job still runs: the reservation is kept
        # and its hold taken again; the device stays out of every other request
        os.killpg(int(Path(a["holds"][0]["pidfile"]).read_text()), signal.SIGKILL)
        wait_for(lambda: "GPU-small" not in h.held_by())
        import jobs
        active = {"ActiveState": "active"}
        with patch.object(jobs, "unit_state", side_effect=lambda u: active if u.endswith("-one") else {}):
            self.assertEqual(h.pr.expire(emit=False), [])
            kept = h.records()[a["id"]]
            self.assertEqual(kept["kept"]["why"], "lost (holder gone)")
            self.assertEqual(kept["kept"]["rehold"], "held again")
            self.assertEqual(h.held_by().get("GPU-small"), "research:resvtest/r")
            self.assertTrue(h.pr.alive(kept))
            with self.assertRaises(ValueError):
                h.pr.reserve("gpu@GPU-small", "resvtest/other", "job:three", timeout_min=0.01)
        # held again, it stands as any other until its job ends; then it goes
        self.assertEqual(h.pr.expire(emit=False), [])
        self.assertNotIn("kept", h.records()[a["id"]])
        rc = Path(jobs.paths("resvtest", jobs.unit_name("resvtest", "r", "one"))["rc"])
        rc.parent.mkdir(parents=True, exist_ok=True)
        rc.write_text("0\n")
        self.addCleanup(shutil.rmtree, rc.parent.parent, True)
        gone = h.pr.expire(emit=False)
        self.assertEqual([g["id"] for g in gone], [a["id"]])
        self.assertNotIn("GPU-small", h.held_by())

    def test_pick_is_keyed_by_device_across_records_queue_and_helper(self):
        pr = self.h.pr
        gpus = {"GPU-small": {"vram_total_mib": 8192}, "GPU-big": {"vram_total_mib": 24576}}
        free = {"ok": True, "leases": [{"key": "gpu", "held": False}], "queue": []}
        two = pr.parse_resources("gpus:2")
        self.assertEqual([p["uuid"] for p in pr.pick(two, gpus, free, {})[0]], ["GPU-small", "GPU-big"])
        one = pr.parse_resources("gpus:1")
        self.assertEqual(pr.pick(one, gpus, free, {})[0], [{"kind": "device", "uuid": "GPU-small"}])  # best fit
        # a live reservation lists a device the helper reports free (its hold was lost under a running job)
        rec = {"id": "r1", "for": "resvtest/a", "state": "held", "devices": [{"uuid": "GPU-small"}], "holds": [],
               "holder_lost": "not held again"}
        picks, why = pr.pick(pr.parse_resources("gpu@GPU-small"), gpus, free, {"r1": rec})
        self.assertIsNone(picks)
        self.assertIn("held by reservation r1", why)
        self.assertEqual(pr.pick(one, gpus, free, {"r1": rec})[0], [{"kind": "device", "uuid": "GPU-big"}])
        self.assertIsNone(pr.pick(two, gpus, free, {"r1": rec})[0])
        # a waiting request lists nothing: it blocks no one
        waiting = {"id": "r2", "state": "waiting", "pid": os.getpid(), "devices": [], "holds": []}
        self.assertIsNotNone(pr.pick(two, gpus, free, {"r2": waiting})[0])
        # a ticket already queued at the helper goes first; a whole-server holder blocks every GPU, not cpu-heavy
        q = dict(free, queue=[{"key": "gpu", "waiter": "ci-x", "ticket": "7", "want": ["GPU-big"]}])
        self.assertIn("queued ci-x", pr.pick(two, gpus, q, {})[1])
        whole = {"ok": True, "leases": [{"key": "gpu", "held": True, "mode": "exclusive", "holder": "ci-y"}],
                 "queue": []}
        self.assertIn("whole-server gpu lease is held by ci-y", pr.pick(one, gpus, whole, {})[1])
        self.assertEqual(pr.pick(pr.parse_resources("cpu-heavy"), gpus, whole, {})[0], [{"kind": "cpu"}])
        with self.assertRaises(ValueError):
            pr.parse_resources("gpu@GPU-big,gpu@GPU-big")
        # fair order: an earlier waiting request that overlaps goes first; a disjoint one does not hold this one back
        me = os.getpid()
        early = {"id": "r0", "for": "resvtest/a", "state": "waiting", "pid": me, "queued_ts": 1.0, "want": "gpus:2"}
        late = {"id": "r9", "state": "waiting", "pid": me, "queued_ts": 2.0, "want": "gpu@GPU-small"}
        self.assertIn("behind the earlier request r0", pr.ahead_of(late, {"r0": early, "r9": late}))
        self.assertEqual(pr.ahead_of(early, {"r0": early, "r9": late}), "")
        cpu = dict(late, want="cpu-heavy")
        self.assertEqual(pr.ahead_of(cpu, {"r0": early, "r9": cpu}), "")
        self.assertEqual(pr.ahead_of(late, {"r0": dict(early, pid=0), "r9": late}), "")  # its process is gone

    def test_a_refused_hold_fails_at_once_and_a_busy_one_times_out(self):
        h = self.h
        bad = h.d / "bad"
        bad.write_text("#!/bin/sh\ncase \"$*\" in *--status*) echo '{\"leases\":[{\"key\":\"gpu\",\"held\":false}],"
                       "\"queue\":[]}';; *--hold*) echo 'cannot create lease dir' >&2; exit 1;; esac\n")
        bad.chmod(0o755)
        h.cfg.write_text(f'data_root = "{h.data}"\nlease_cmd = "{bad}"\n')
        config._CACHE.clear()
        t0 = time.time()
        with self.assertRaises(ValueError) as cm:
            h.pr.reserve("gpus:1", "resvtest/r", "job:x", timeout_min=5)
        self.assertLess(time.time() - t0, 30)
        self.assertIn("refused the hold", str(cm.exception))
        self.assertEqual(h.records(), {})

    def test_job_until_ignores_an_exit_file_from_an_earlier_run(self):
        h = self.h
        import jobs
        unit = jobs.unit_name("resvtest", "r", "again")
        rc = Path(jobs.paths("resvtest", unit)["rc"])
        rc.parent.mkdir(parents=True, exist_ok=True)
        rc.write_text("0\n")
        os.utime(rc, (time.time() - 600, time.time() - 600))
        rec = h.pr.reserve("gpu@GPU-big", "resvtest/r", "job:again")
        with patch.object(jobs, "unit_state", return_value={}):
            self.assertEqual(h.pr.expire(emit=False), [])  # the old exit file is not this run ending
            rc.write_text("0\n")  # the job ran and exited
            self.assertEqual([g["id"] for g in h.pr.expire(emit=False)], [rec["id"]])
        shutil.rmtree(rc.parent.parent, ignore_errors=True)

    def test_hold_scope(self):
        pr = self.h.pr
        self.assertEqual(pr.scope_argv("r1", 0), [])  # SHOAL_HOLD_SCOPE=0
        with patch.dict(os.environ, {"SHOAL_HOLD_SCOPE": "auto"}), patch.dict(pr._SCOPE, {"ok": True}):
            argv, _env = pr.hold_argv("/opt/lease", {"kind": "cpu"}, "/p.pid", "resvtest/r", {}, 0, pr.scope_argv("r1", 0))
        self.assertEqual(argv[:5], ["systemd-run", "--user", "--scope", "--quiet", "--collect"])
        self.assertTrue(argv[5].startswith("--unit=shoal-hold-r1-0-"))
        self.assertEqual(argv[-7:], ["--lease", "cpu-heavy", "--timeout", "0", "--hold", "--pidfile", "/p.pid"])


PSI_BAD = "some avg10=82.00 avg60=74.50 avg300=40.00 total=1\nfull avg10=71.00 avg60=66.20 avg300=30.00 total=1\n"
PSI_OK = "some avg10=1.00 avg60=0.50 avg300=0.10 total=1\nfull avg10=0.00 avg60=0.00 avg300=0.00 total=1\n"


class MemoryPressure(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp(prefix="shoal-psi-"))
        self.addCleanup(shutil.rmtree, self.d, True)
        self.cg = "/user.slice/user@1.service/shoal.slice/shoal-jobs.slice/shoal-s-r-train.service"
        (self.d / self.cg.lstrip("/")).mkdir(parents=True)
        self.env = patch.dict(os.environ, {"SHOAL_CGROUP_ROOT": str(self.d), "SHOAL_DATA_ROOT": str(self.d / "data"),
                                           "SHOAL_SERVER": "testhost"})
        self.env.start()
        self.addCleanup(self.env.stop)
        config._CACHE.clear()
        import jobs
        self.jobs = importlib.reload(jobs)

    def write(self, unit_psi, slice_psi):
        (self.d / self.cg.lstrip("/") / "memory.pressure").write_text(unit_psi)
        (self.d / self.cg.lstrip("/")).parent.joinpath("memory.pressure").write_text(slice_psi)

    def test_pressure_and_throttled(self):
        j = self.jobs
        self.write(PSI_BAD, PSI_BAD)
        pr = j.pressure_of(self.cg)
        self.assertTrue(pr["throttled"])
        self.assertEqual(pr["parent_name"], "shoal-jobs.slice")
        self.assertEqual(pr["unit"]["full"]["avg60"], 66.2)
        text = j.pressure_text(pr)
        self.assertIn("job some 82/74% full 71/66%", text)
        self.assertIn("THROTTLED (full avg60 66% >= 50%", text)
        self.assertIn("shoal-jobs.slice is at its memory ceiling", text)
        self.write(PSI_OK, PSI_BAD)  # the slice under pressure, this job not stalled: not throttled
        pr = j.pressure_of(self.cg)
        self.assertFalse(pr["throttled"])
        self.assertNotIn("THROTTLED", j.pressure_text(pr))
        self.assertEqual(j.pressure_of(""), {})
        self.assertEqual(j.pressure_of("/no/such.service"), {})

    def test_status_wait_and_health(self):
        j = self.jobs
        self.write(PSI_BAD, PSI_BAD)
        p = j.paths("psitest", "shoal-s-r-train")
        os.makedirs(p["sdir"], exist_ok=True)
        self.addCleanup(shutil.rmtree, os.path.dirname(p["sdir"]), True)
        meta = {"unit": "shoal-s-r-train", "shoal": "s", "slug": "r", "name": "train", "cmd": ["python", "train.py"],
                "started_utc": "2026-11-25T00:00:00Z", "log": p["log"], "rc_file": p["rc"]}
        Path(p["meta"]).write_text(json.dumps(meta))
        st = {"ActiveState": "active", "SubState": "running", "ControlGroup": self.cg}
        out = io.StringIO()
        with patch.object(j, "unit_state", return_value=st), contextlib.redirect_stdout(out):
            j.cmd_status(argparse.Namespace(sid="psitest", slug=""))
        self.assertIn("shoal-s-r-train: active/running THROTTLED", out.getvalue())
        self.assertIn("memory pressure (avg10/avg60): job some 82/74% full 71/66%; shoal-jobs.slice", out.getvalue())
        # job wait: one JOB-THROTTLED line (and event) however many polls see it, a note beside the verdict
        a = argparse.Namespace(sid="s", slug="r")
        w = j.ThrottleWatch(a, "shoal-s-r-train")
        out = io.StringIO()
        with patch.object(j, "_emit") as emit, contextlib.redirect_stdout(out):
            for _ in range(3):
                w.see(j.pressure_of(self.cg))
        self.assertEqual(out.getvalue().count("JOB-THROTTLED shoal-s-r-train"), 1)
        self.assertEqual([c.args[2] for c in emit.call_args_list], ["job-throttled"])
        self.assertIn("full avg60 peak 66%", w.verdict_note())
        # health: a WARN at first sight, a page only if it lasts 30 min
        with patch.object(j, "unit_state", return_value=st):
            found = j.throttled_jobs("psitest")
        self.assertEqual([m["unit"] for m, _ in found], ["shoal-s-r-train"])
        import health
        conds = health.throttled_jobs(type("S", (), {"sid": "psitest"})(), found)
        self.assertEqual((conds[0]["slug"], conds[0]["condition"], conds[0]["first_seen"]), ("r", "job-throttled", True))
        self.assertIn("THROTTLED", conds[0]["detail"])
        self.assertEqual(health.LADDERS["job-throttled"], [(30, "page")])
        self.assertIn("job-throttled", health.HOLD_EXEMPT)
        # a job that is not running, or not stalled, is not raised
        with patch.object(j, "unit_state", return_value=dict(st, ActiveState="inactive")):
            self.assertEqual(j.throttled_jobs("psitest"), [])
        self.write(PSI_OK, PSI_BAD)
        with patch.object(j, "unit_state", return_value=st):
            self.assertEqual(j.throttled_jobs("psitest"), [])

    def test_remote_state_reads_pressure(self):
        j = self.jobs
        stdout = ("LoadState=loaded\nActiveState=active\n---\n---\n---\n---\nboot\n---\n---\n" + self.cg + "\n---\n"
                  + PSI_BAD + "---\n" + PSI_OK)
        with patch.object(j, "_ssh", return_value=subprocess.CompletedProcess([], 0, stdout, "")) as ssh:
            st = j.remote_state("farhost", "shoal-s-r-train")
        self.assertIn("memory.pressure", ssh.call_args.args[1])
        self.assertEqual(st["ControlGroup"], self.cg)
        self.assertTrue(st["pressure"]["throttled"])
        self.assertFalse(j.throttled(st["pressure"]["parent"]))


if __name__ == "__main__":
    unittest.main()
