"""Azure agent rotation for research reservations (shoal/agent_rotation.py): a reservation takes this host's agent out
of rotation and its end restores exactly what was removed; a build waiting here on a reserved resource is re-queued.
A fake lease helper and an in-memory Azure; no network, no GPU."""
import contextlib
import importlib
import io
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "shoal"))
import config  # noqa: E402

TOKEN = "fake-token-never-printed"
HELPER = r"""#!/bin/sh
# a fake lease helper: --hold forks a holder, --release ends it, --status prints $dir/status.json when present
pf=""; args="$*"; here=$(dirname "$0")
while [ $# -gt 0 ]; do case "$1" in --pidfile) pf=$2; shift 2;; *) shift;; esac; done
case "$args" in
  *--status*) if [ -f "$here/status.json" ]; then cat "$here/status.json"; else
              echo '{"leases":[{"key":"gpu","held":false},{"key":"cpu-heavy","held":false}],"queue":[]}'; fi ;;
  *--hold*) if [ -f "$here/refuse" ]; then echo "busy" >&2; exit 75; fi
            setsid sleep 300 </dev/null >/dev/null 2>&1 & echo $! > "$pf"
            echo "${GPU_LEASE_DEVICES%%:*}" > "$pf.device" ;;
  *--release*) kill "$(cat "$pf")" 2>/dev/null; rm -f "$pf" "$pf.device" ;;
esac
"""


class FakeAzure:
    """The agent pool and build APIs the module calls, in memory. `refuse` makes every call a 401, `fail_put` makes a
    PUT fail in flight (`applied`: after Azure changed the agent)."""

    def __init__(self, other_caps=None):
        import agent_rotation as ar
        self.ar = ar
        self.agents = {
            9: {"id": 9, "name": "gpuhost-a", "enabled": True, "status": "online",
                "systemCapabilities": {"GPU_VENDOR": "nvidia", "GPU_COUNT": "2", "Agent.OS": "Linux",
                                       "Agent.Version": "4.259.0"},
                "userCapabilities": {"ci-any-vendor": "1", "ci-fts": "1", "ci-it": "1", "ci-release": "1",
                                     "ci-vendor-marked": "1"}},
            10: {"id": 10, "name": "gpuhost-b", "enabled": True, "status": "online",
                 "systemCapabilities": {"GPU_VENDOR": "amd", "GPU_COUNT": "2", "Agent.OS": "Linux",
                                        "Agent.Version": "4.259.0"},
                 "userCapabilities": dict(other_caps if other_caps is not None else
                                          {"ci-any-vendor": "1", "ci-fts": "1", "ci-it": "1", "ci-vendor-marked": "1"})}}
        self.timelines: dict[int, list] = {}
        self.calls: list[tuple] = []
        self.refuse = False
        self.fail_put = ""

    def writes(self, method=None):
        return [c for c in self.calls if c[0] != "GET" and (method is None or c[0] == method)]

    def call(self, method, path, query=None, body=None, project=""):
        import copy
        import re
        self.calls.append((method, path, copy.deepcopy(body), project))
        if self.refuse:
            raise self.ar.Refused(f"HTTP 401 on {method} {path}")
        if path == "distributedtask/pools":
            return {"value": [{"id": 1, "name": "Default"}]}
        if path == "distributedtask/pools/1/agents":
            name = (query or {}).get("agentName")
            return {"value": [copy.deepcopy(a) for a in self.agents.values() if not name or a["name"] == name]}
        m = re.fullmatch(r"distributedtask/pools/1/agents/(\d+)(/usercapabilities)?", path)
        if m:
            a = self.agents.get(int(m.group(1)))
            if a is None:
                raise self.ar.ApiError("HTTP 404", 404)
            if method == "PUT":
                if self.fail_put == "applied":
                    a["userCapabilities"] = dict(body)
                if self.fail_put:
                    raise self.ar.ApiError("PUT timed out")
                a["userCapabilities"] = dict(body)
            return copy.deepcopy(a)
        m = re.fullmatch(r"build/builds/(\d+)/timeline", path)
        if m:
            return {"records": copy.deepcopy(self.timelines.get(int(m.group(1)), []))}
        m = re.fullmatch(r"build/builds/(\d+)/stages/(.+)", path)
        if m and method == "PATCH":
            recs = self.timelines.get(int(m.group(1)), [])
            for r in recs:
                if body.get("state") == "cancel":
                    r["state"], r["result"] = "completed", "canceled"
                elif body.get("state") == "retry" and r["type"] == "Stage":
                    r["state"] = "pending"
            return None
        raise AssertionError(f"unexpected call {method} {path}")


def timeline(agent="gpuhost-a"):
    return [{"id": "s", "type": "Stage", "identifier": "__default", "name": "__default", "state": "inProgress"},
            {"id": "p", "type": "Phase", "parentId": "s", "name": "pytest", "state": "inProgress"},
            {"id": "j", "type": "Job", "parentId": "p", "name": "pytest", "state": "inProgress", "workerName": agent}]


def assigned(build_id, demands=("Agent.OS -equals Linux", "GPU_VENDOR", "GPU_COUNT -equals 2", "ci-any-vendor",
                                "ci-it", "Agent.Version -gtVersion 2.199.0")):
    return {"owner": {"id": build_id, "name": "20261120.43"}, "scopeId": "proj-guid", "demands": list(demands)}


class Rotation(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="shoal-rotation-")
        d = Path(self.d)
        self.helper = d / "lease"
        self.helper.write_text(HELPER)
        self.helper.chmod(0o755)
        self.data = d / "data"
        self.pat = d / "pat"
        self.pat.write_text(TOKEN + "\n")
        self.pat.chmod(0o600)
        self.cfg = d / "host.toml"
        self.write_cfg()
        sdir = self.data / "_resources" / "gpuhost-a"
        sdir.mkdir(parents=True)
        now = time.time()
        (sdir / time.strftime("events-%Y%m%d.jsonl", time.gmtime(now))).write_text(json.dumps(
            {"type": "resource_sample", "ts": now, "memory": {"available_bytes": 1 << 34},
             "gpus": [{"uuid": "GPU-big", "vendor": "nvidia", "vram_total_mib": 24576},
                      {"uuid": "GPU-small", "vendor": "nvidia", "vram_total_mib": 8192}]}) + "\n")
        self.env = patch.dict(os.environ, {"SHOAL_CONFIG": str(self.cfg), "SHOAL_DATA_ROOT": str(self.data),
                                           "SHOAL_SERVER": "gpuhost-a"})
        self.env.start()
        for k in ("SHOAL_AZURE_ORG", "SHOAL_AZURE_AGENT_PAT", "SHOAL_LEASE_CMD", "GPU_LEASE_CMD"):
            os.environ.pop(k, None)
        config._CACHE.clear()
        import agent_rotation
        import pool_reserve
        self.ar = importlib.reload(agent_rotation)
        self.pr = importlib.reload(pool_reserve)
        self.az = FakeAzure()
        self.api = patch.object(self.ar, "make_api", side_effect=self.make_api)
        self.api.start()
        self.told = []
        self.tell = patch.object(self.ar, "_tell", side_effect=lambda ev, resv, cfg: self.told.append(ev))
        self.tell.start()
        self.err = io.StringIO()
        self.stderr = contextlib.redirect_stderr(self.err)
        self.stderr.__enter__()

    def make_api(self, cfg):
        self.ar.read_pat(cfg["pat"])  # the real credential checks; the fake answers
        return self.az

    def tearDown(self):
        self.stderr.__exit__(None, None, None)
        for p in self.data.glob("_pool/holds/*.pid"):
            with contextlib.suppress(OSError, ValueError):
                os.kill(int(p.read_text().strip()), 15)
        self.tell.stop()
        self.api.stop()
        self.env.stop()
        config._CACHE.clear()
        shutil.rmtree(self.d, ignore_errors=True)

    def write_cfg(self, extra=""):
        self.cfg.write_text(f'data_root = "{self.data}"\nlease_cmd = "{self.helper}"\n'
                            f'azure_org = "https://dev.azure.example/org"\n'
                            f'[azure_agent]\npat = "{self.pat}"\n{extra}')
        config._CACHE.clear()

    def caps(self, aid=9):
        return self.az.agents[aid]["userCapabilities"]

    def reserve(self, res="gpu@GPU-big", until="job:study"):
        return self.pr.reserve(res, "s/r", until)

    # ---- out of rotation, and back ----

    def test_reserve_takes_the_agent_out_and_release_restores_exactly(self):
        self.caps()["ci-any-vendor"] = "yes"  # an unusual value comes back as it was
        rec = self.reserve()
        self.assertNotIn("ci-any-vendor", self.caps())
        self.assertEqual(self.caps()["ci-vendor-marked"], "1")  # only the configured capability goes
        self.assertIn("azure agent gpuhost-a OUT of rotation", rec["rotation"])
        st = self.ar.load_state()
        self.assertEqual((st["out"], st["removed"], st["agent"]["id"]), (True, {"ci-any-vendor": "yes"}, 9))
        self.assertEqual(self.az.agents[10]["userCapabilities"]["ci-any-vendor"], "1")  # the other agent untouched
        rel = self.pr.release(rec["id"])
        self.assertEqual(self.caps()["ci-any-vendor"], "yes")
        self.assertEqual(rel["rotation"], "")
        self.assertFalse(os.path.exists(self.ar.path()))
        puts = self.az.writes("PUT")
        self.assertEqual(len(puts), 2)
        self.assertTrue(all(p[1] == "distributedtask/pools/1/agents/9/usercapabilities" for p in puts))
        self.assertEqual(puts[1][2], {"ci-any-vendor": "yes", "ci-fts": "1", "ci-it": "1", "ci-release": "1",
                                      "ci-vendor-marked": "1"})  # the whole map, as the API replaces it

    def test_until_reached_and_holder_died_restore(self):
        now = time.time()
        timed = self.pr.reserve("gpus:1", "s/r", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now + 3600)))
        self.assertNotIn("ci-any-vendor", self.caps())
        self.assertEqual(self.pr.expire(now + 60, emit=False), [])
        self.assertNotIn("ci-any-vendor", self.caps())
        self.assertEqual([g["id"] for g in self.pr.expire(now + 7200, emit=False)], [timed["id"]])
        self.assertEqual(self.caps()["ci-any-vendor"], "1")
        dead = self.reserve("cpu-heavy")  # a cpu-heavy reservation counts too: a whole-server job waits on it
        self.assertNotIn("ci-any-vendor", self.caps())
        os.kill(int(Path(dead["holds"][0]["pidfile"]).read_text()), 9)
        time.sleep(0.2)
        gone = self.pr.expire(emit=False)
        self.assertEqual([(g["id"], g["released"]) for g in gone], [(dead["id"], "lost (holder gone)")])
        self.assertEqual(self.caps()["ci-any-vendor"], "1")

    def test_two_overlapping_reservations(self):
        a = self.reserve("gpu@GPU-big")
        b = self.reserve("gpu@GPU-small")
        self.assertEqual(len(self.az.writes("PUT")), 1)  # already out: the second reservation changes nothing
        self.assertEqual(sorted(self.ar.load_state()["reservations"]), sorted([a["id"], b["id"]]))
        self.pr.release(a["id"])
        self.assertNotIn("ci-any-vendor", self.caps())  # one still stands
        self.pr.release(b["id"])
        self.assertEqual(self.caps()["ci-any-vendor"], "1")
        self.assertEqual(len(self.az.writes("PUT")), 2)

    def test_idempotent_and_never_touches_an_unrecorded_agent(self):
        self.assertEqual(self.ar.reconcile(force=True), {})
        self.assertEqual(self.az.calls, [])  # nothing reserved, nothing recorded: no call at all
        rec = self.reserve()
        n = len(self.az.calls)
        for _ in range(3):
            self.ar.reconcile(force=True)
            self.pr.expire(emit=False)
        self.assertEqual(len(self.az.calls), n)  # out and recorded: ticks need no Azure call
        self.caps()["ci-any-vendor"] = "2"  # the maintainer set it again meanwhile: kept as found
        self.pr.release(rec["id"])
        self.assertEqual(self.caps()["ci-any-vendor"], "2")
        self.assertEqual(len(self.az.writes("PUT")), 1)
        self.assertIn("already set again, kept as found", self.err.getvalue())

    def test_an_agent_already_without_the_capability_is_left_alone(self):
        del self.caps()["ci-any-vendor"]
        rec = self.reserve()
        self.assertIn("removed nothing: it already lacked ci-any-vendor", rec["rotation"])
        self.pr.release(rec["id"])
        self.assertNotIn("ci-any-vendor", self.caps())  # shoal never adds what it did not remove
        self.assertEqual(self.az.writes(), [])

    def test_the_last_agent_with_the_capability_stays_in_rotation(self):
        del self.az.agents[10]["userCapabilities"]["ci-any-vendor"]
        rec = self.reserve()
        self.assertEqual(self.caps()["ci-any-vendor"], "1")
        self.assertIn("NOT out of rotation: kept in rotation: no other enabled agent", rec["rotation"])
        self.pr.release(rec["id"])
        self.assertEqual(self.az.writes(), [])

    def test_vendor_marked_too_when_configured(self):
        self.write_cfg('capabilities = ["ci-any-vendor", "ci-vendor-marked"]\n')
        rec = self.reserve()
        self.assertFalse({"ci-any-vendor", "ci-vendor-marked"} & set(self.caps()))
        self.pr.release(rec["id"])
        self.assertEqual((self.caps()["ci-any-vendor"], self.caps()["ci-vendor-marked"]), ("1", "1"))

    # ---- the credential ----

    def test_missing_pat_the_reservation_still_succeeds(self):
        self.pat.unlink()
        rec = self.reserve()
        self.assertEqual(self.caps()["ci-any-vendor"], "1")
        self.assertIn("NOT out of rotation: no PAT file at", rec["rotation"])
        st = self.ar.load_state()
        self.assertEqual((st["out"], "removed" in st), (False, False))
        self.assertIn("NOT taken out of rotation", self.err.getvalue())
        lines = dict((t, lvl) for lvl, t in self.ar.doctor_lines())
        self.assertTrue(any(lvl == "warn" and "will NOT take agent gpuhost-a out of rotation" in t
                            for t, lvl in lines.items()))
        self.pr.release(rec["id"])
        self.assertFalse(os.path.exists(self.ar.path()))  # nothing was changed: nothing to restore

    def test_refused_pat_backs_off_and_retries(self):
        self.az.refuse = True
        rec = self.reserve()
        st = self.ar.load_state()
        self.assertIn("HTTP 401", st["error"])
        self.assertIn("Agent Pools (Read & manage)", st["error"])
        self.assertNotIn("removed", st)
        n = len(self.az.calls)
        self.ar.tick()  # within the back-off: not asked again
        self.assertEqual(len(self.az.calls), n)
        self.az.refuse = False
        self.ar.tick(time.time() + self.ar.RETRY_S + 1)  # the scope was granted meanwhile
        self.assertNotIn("ci-any-vendor", self.caps())
        self.pr.release(rec["id"])
        self.assertEqual(self.caps()["ci-any-vendor"], "1")

    def test_a_pat_others_can_read_is_refused(self):
        self.pat.chmod(0o644)
        with self.assertRaises(self.ar.Refused):
            self.ar.read_pat(str(self.pat))

    def test_the_token_is_never_written_or_printed(self):
        self.az.refuse = True
        rec = self.reserve()
        self.az.refuse = False
        self.ar.tick(time.time() + self.ar.RETRY_S + 1)
        self.pr.release(rec["id"])
        api = self.ar.Api("https://dev.azure.example/org", TOKEN)
        blob = self.err.getvalue() + repr(api) + str(rec) + Path(self.ar.log_path()).read_text()
        self.assertNotIn(TOKEN, blob)

    # ---- a crash between the record and Azure ----

    def test_write_ahead_record_restores_after_an_in_flight_failure(self):
        self.az.fail_put = "applied"  # Azure changed the agent, the answer was lost
        rec = self.reserve()
        self.assertNotIn("ci-any-vendor", self.caps())
        st = self.ar.load_state()
        # the original value was on disk before the PUT; the reservation's next tick found it applied and kept it
        self.assertEqual((st["out"], st["removed"]), (True, {"ci-any-vendor": "1"}))
        self.assertIn("PUT timed out", self.err.getvalue())
        self.az.fail_put = ""
        importlib.reload(self.ar)  # a new process reads the record
        with patch.object(self.ar, "make_api", side_effect=self.make_api):
            self.pr.release(rec["id"])
        self.assertEqual(self.caps()["ci-any-vendor"], "1")
        self.assertFalse(os.path.exists(self.ar.path()))

    def test_unapplied_in_flight_failure_restores_nothing(self):
        self.az.fail_put = "lost"
        rec = self.reserve()
        self.assertEqual(self.caps()["ci-any-vendor"], "1")
        self.az.fail_put = ""
        puts = len(self.az.writes("PUT"))
        self.pr.release(rec["id"])
        self.assertEqual(len(self.az.writes("PUT")), puts)  # present already: no PUT
        self.assertFalse(os.path.exists(self.ar.path()))

    def test_a_failed_reservation_puts_the_agent_back(self):
        (Path(self.d) / "refuse").write_text("")
        with self.assertRaises(ValueError):
            self.reserve()
        self.assertEqual(self.caps()["ci-any-vendor"], "1")
        self.assertEqual(len(self.az.writes("PUT")), 2)  # out while it waited for its holds, back when it failed
        self.assertFalse(os.path.exists(self.ar.path()))

    # ---- builds waiting here on a reserved resource ----

    def queue(self, *rows):
        (Path(self.d) / "status.json").write_text(json.dumps(
            {"leases": [{"key": "gpu", "held": False}, {"key": "cpu-heavy", "held": False}], "queue": list(rows)}))

    def test_blocked_builds(self):
        rows = {"queue": [
            {"key": "gpu", "waiter": "azure-it-2287", "ticket": "1", "want": ["GPU-big"]},
            {"key": "gpu", "waiter": "azure-2290", "ticket": "2", "want": ["all"]},
            {"key": "gpu", "waiter": "azure-2291", "ticket": "3", "want": ["GPU-small", "GPU-big"]},
            {"key": "cpu-heavy", "waiter": "azure-2292", "ticket": "4"},
            {"key": "gpu", "waiter": "research:s/r", "ticket": "5", "want": ["GPU-big"]},
            {"key": "gpu", "waiter": "local-suite", "ticket": "6", "want": ["all"]}]}
        pat = config.AZURE_WAITER_PATTERN
        self.assertEqual(self.ar.blocked_builds(rows, {"GPU-big"}, False, pat),
                         {2287: "azure-it-2287", 2290: "azure-2290"})  # a free candidate is not blocked
        self.assertEqual(sorted(self.ar.blocked_builds(rows, {"GPU-big", "GPU-small"}, True, pat)),
                         [2287, 2290, 2291, 2292])
        self.assertEqual(self.ar.blocked_builds(rows, set(), True, pat), {2292: "azure-2292"})

    def test_requeue_decision(self):
        cfg = config.azure_agent()
        a, b = self.az.agents[9], self.az.agents[10]
        del a["userCapabilities"]["ci-any-vendor"]
        a["assignedRequest"] = assigned(2287)
        self.assertEqual(self.ar.requeue_decision(2287, a, [a, b], cfg)[0], "requeue")
        self.assertIn("is not running it (its job is build 2287)", self.ar.requeue_decision(2300, a, [a, b], cfg)[1])
        pinned = dict(a, assignedRequest=assigned(2287, ("GPU_VENDOR -equals nvidia", "ci-vendor-marked", "ci-it")))
        verdict, why = self.ar.requeue_decision(2287, pinned, [pinned, b], cfg)
        self.assertEqual(verdict, "leave")
        self.assertIn("still meets its demands", why)  # a vendor-pinned leg: it can only run here
        a2 = dict(a, userCapabilities=dict(a["userCapabilities"], **{"ci-any-vendor": "1"}))
        self.assertIn("still meets its demands", self.ar.requeue_decision(2287, a2, [a2, b], cfg)[1])
        off = dict(b, enabled=False)
        self.assertIn("no other enabled agent", self.ar.requeue_decision(2287, a, [a, off], cfg)[1])
        self.assertEqual(self.ar.requeue_decision(2287, a, [a, b], dict(cfg, requeue=False))[0], "leave")
        self.assertFalse(self.ar.satisfies({"X": "1"}, ["X -unknownOp 1"]))  # never move on a guess
        self.assertTrue(self.ar.satisfies({"Agent.Version": "4.259.0"}, ["agent.version -gtVersion 2.199.0"]))

    def test_sweep_cancels_and_retries_a_waiting_build(self):
        rec = self.reserve()
        self.az.agents[9]["assignedRequest"] = assigned(2287)
        self.az.timelines[2287] = timeline()
        self.queue({"key": "gpu", "waiter": "azure-it-2287", "ticket": "7", "want": ["GPU-big"]})
        self.ar.sweep()
        patches = self.az.writes("PATCH")
        self.assertEqual([(p[1], p[2]["state"], p[3]) for p in patches],
                         [("build/builds/2287/stages/__default", "cancel", "proj-guid"),
                          ("build/builds/2287/stages/__default", "retry", "proj-guid")])
        self.assertEqual([(e["build"], e["step"]) for e in self.told], [(2287, "retried")])
        self.assertEqual(self.ar.load_state()["builds"]["2287"]["step"], "retried")
        self.ar.sweep()
        self.assertEqual(len(self.az.writes("PATCH")), 2)  # once per out-of-rotation period
        kind, text = self.ar.message(self.told[0], [rec], config.azure_agent())
        self.assertEqual(kind, "ci-requeued")
        self.assertIn("az-build:2287", text)
        self.pr.release(rec["id"])
        self.assertFalse(os.path.exists(self.ar.path()))

    def test_sweep_waits_for_the_cancel_then_retries_on_a_later_tick(self):
        rec = self.reserve()
        self.az.agents[9]["assignedRequest"] = assigned(2287)
        self.az.timelines[2287] = timeline()
        self.queue({"key": "gpu", "waiter": "azure-it-2287", "ticket": "7", "want": ["GPU-big"]})
        real = self.az.call

        def slow(method, path, query=None, body=None, project=""):
            out = real(method, path, query, body, project)
            if method == "PATCH" and body.get("state") == "cancel":
                for r in self.az.timelines[2287]:
                    r["state"] = "inProgress"  # still cancelling
            return out
        with patch.object(self.az, "call", side_effect=slow):
            self.ar.sweep()
        self.assertEqual(self.ar.load_state()["builds"]["2287"]["step"], "cancel-sent")
        self.queue()  # the cancelled job's waiter is gone from the lease queue
        for r in self.az.timelines[2287]:
            r["state"] = "completed"
        self.ar.sweep()
        self.assertEqual(self.ar.load_state()["builds"]["2287"]["step"], "retried")
        self.pr.release(rec["id"])

    def test_a_vendor_pinned_build_is_left_and_its_owner_told(self):
        rec = self.reserve()
        self.az.agents[9]["assignedRequest"] = assigned(2295, ("GPU_VENDOR -equals nvidia", "ci-vendor-marked"))
        self.az.timelines[2295] = timeline()
        self.queue({"key": "gpu", "waiter": "azure-2295", "ticket": "8", "want": ["all"]})
        self.ar.sweep()
        self.assertEqual(self.az.writes("PATCH"), [])
        self.assertEqual([(e["build"], e["step"]) for e in self.told], [(2295, "left")])
        kind, text = self.ar.message(self.told[0], [rec], config.azure_agent())
        self.assertEqual(kind, "ci-blocked")
        self.assertIn("fails when its lease budget runs out", text)
        self.pr.release(rec["id"])

    def test_without_a_credential_a_waiting_build_is_reported_not_touched(self):
        self.pat.unlink()
        rec = self.reserve()
        self.queue({"key": "gpu", "waiter": "azure-it-2287", "ticket": "7", "want": ["GPU-big"]})
        self.ar.sweep()
        self.assertEqual(self.az.calls, [])
        self.assertEqual([(e["build"], e["step"]) for e in self.told], [(2287, "left-not-out")])
        self.assertIn("no PAT file", self.told[0]["why"])
        self.pr.release(rec["id"])

    def test_a_retry_that_fails_leaves_it_cancelled_and_says_so(self):
        rec = self.reserve()
        self.az.agents[9]["assignedRequest"] = assigned(2287)
        self.az.timelines[2287] = timeline()
        self.queue({"key": "gpu", "waiter": "azure-it-2287", "ticket": "7", "want": ["GPU-big"]})
        real = self.az.call

        def no_retry(method, path, query=None, body=None, project=""):
            if method == "PATCH" and body.get("state") == "retry":
                raise self.ar.ApiError("HTTP 400 on PATCH: retry not allowed", 400)
            return real(method, path, query, body, project)
        with patch.object(self.az, "call", side_effect=no_retry):
            self.ar.sweep()
        self.assertEqual([(e["build"], e["step"]) for e in self.told], [(2287, "cancelled")])
        kind, text = self.ar.message(self.told[0], [rec], config.azure_agent())
        self.assertEqual(kind, "ci-cancelled")
        self.assertIn("NOT queued again", text)
        self.pr.release(rec["id"])

    def test_owners_are_the_roles_waiting_on_the_build(self):
        sid = f"rotationtest{os.getpid()}"
        import roles
        sdir = Path(roles.state_dir(sid))
        sdir.mkdir(parents=True, exist_ok=True)
        try:
            (sdir / "wait-lane").write_text(json.dumps({"slug": "lane", "target": "az-build:2287"}))
            (sdir / "wait-other").write_text(json.dumps({"slug": "other", "target": "az-build:proj/12287"}))
            self.assertIn((sid, "lane"), self.ar._waiting_roles(2287))
            self.assertNotIn((sid, "other"), self.ar._waiting_roles(2287))
        finally:
            shutil.rmtree(sdir, ignore_errors=True)

    def test_off_without_azure_org(self):
        self.cfg.write_text(f'data_root = "{self.data}"\nlease_cmd = "{self.helper}"\n')
        config._CACHE.clear()
        rec = self.reserve()
        self.assertEqual(rec["rotation"], "")
        self.assertEqual(self.az.calls, [])
        self.assertIn("off (azure_org is unset)", self.ar.doctor_lines()[0][1])
        self.pr.release(rec["id"])

    def test_status_and_probe_report_it(self):
        self.pat.unlink()
        rec = self.reserve()
        import pool_cli
        p = pool_cli.probe()
        self.assertFalse(p["agent_rotation"]["out"])
        self.assertIn("NOT out of rotation", self.ar.describe(p["agent_rotation"]))
        self.pr.release(rec["id"])


if __name__ == "__main__":
    unittest.main(verbosity=1)
