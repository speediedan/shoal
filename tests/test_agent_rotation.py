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
                                           "SHOAL_SERVER": "gpuhost-a", "SHOAL_HOLD_SCOPE": "0",
                                           "SHOAL_POOL_POLL_S": "0.1"})
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
        self.told, self.told_resv = [], []
        self.tell = patch.object(self.ar, "_tell", side_effect=lambda ev, resv, cfg: (self.told.append(ev),
                                                                                      self.told_resv.append(resv)))
        self.tell.start()
        self.err = io.StringIO()
        self.stderr = contextlib.redirect_stderr(self.err)
        self.stderr.__enter__()

    def make_api(self, cfg):
        self.ar.read_pat(cfg["pat"])  # the real credential checks; the fake answers
        return self.az

    def tearDown(self):
        self.stderr.__exit__(None, None, None)
        for p in list(self.data.glob("_pool/holds/*.pid")) + list(Path(self.d).glob("data-*/_pool/holds/*.pid")):
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
        (Path(self.d) / "refuse").write_text("")  # busy (75): it waits, holding nothing, until its timeout
        with self.assertRaises(ValueError):
            self.pr.reserve("gpu@GPU-big", "s/r", "job:study", timeout_min=0.01)
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

    def cancel_pending(self, build_id=2347):
        """Reserve, and leave a build's stage cancel sent but not yet completed (Azure still cancelling)."""
        rec = self.reserve()
        self.az.agents[9]["assignedRequest"] = assigned(build_id)
        self.az.timelines[build_id] = timeline()
        self.queue({"key": "gpu", "waiter": f"azure-it-{build_id}", "ticket": "7", "want": ["GPU-big"]})
        real = self.az.call

        def slow(method, path, query=None, body=None, project=""):
            out = real(method, path, query, body, project)
            if method == "PATCH" and body.get("state") == "cancel":
                for r in self.az.timelines[build_id]:
                    r["state"] = "inProgress"  # still cancelling
            return out
        with patch.object(self.az, "call", side_effect=slow):
            self.ar.sweep()
        self.assertEqual(self.ar.load_state()["builds"][str(build_id)]["step"], "cancel-sent")
        return rec

    def test_a_cancel_sent_build_is_retried_after_the_reservation_ends(self):
        # measured 2026-11-21: out 23:35:42, stage cancel sent 23:35:43, reservation ended 23:35:58, never retried
        rec = self.cancel_pending()
        self.queue()  # the cancelled job's waiter left the lease queue
        self.pr.release(rec["id"])
        self.assertEqual(self.caps()["ci-any-vendor"], "1")  # back in rotation
        st = self.ar.load_state()
        self.assertEqual(st["builds"]["2347"]["step"], "cancel-sent")  # the release kept the build to follow up
        self.assertEqual(st["builds"]["2347"]["resv"][0]["id"], rec["id"])
        self.assertEqual(self.told, [])
        for r in self.az.timelines[2347]:
            r["state"] = "completed"
        self.ar.tick()  # no reservation stands now: the follow-up still runs
        retries = [p for p in self.az.writes("PATCH") if p[2]["state"] == "retry"]
        self.assertEqual([(p[1], p[3]) for p in retries], [("build/builds/2347/stages/__default", "proj-guid")])
        self.assertEqual([(e["build"], e["step"]) for e in self.told], [(2347, "retried")])
        self.assertEqual([r["id"] for r in self.told_resv[0]], [rec["id"]])  # the owner is still known
        kind, text = self.ar.message(self.told[0], self.told_resv[0], config.azure_agent())
        self.assertEqual(kind, "ci-requeued")
        self.assertIn(rec["id"], text)
        self.ar.tick()
        self.assertFalse(os.path.exists(self.ar.path()))  # done: nothing left to record
        self.assertEqual(len([p for p in self.az.writes("PATCH") if p[2]["state"] == "retry"]), 1)

    def test_a_cancel_that_never_settles_is_reported_without_reservations(self):
        rec = self.cancel_pending()
        self.queue()
        self.pr.release(rec["id"])
        self.write_cfg_without_lease()  # the follow-up needs neither a reservation nor the lease helper
        now = time.time()
        self.ar.tick(now + 60)
        self.assertEqual(self.ar.load_state()["builds"]["2347"]["step"], "cancel-sent")  # still cancelling
        self.ar.tick(now + self.ar.CANCEL_SETTLE_S + 60)
        self.assertEqual([(e["build"], e["step"]) for e in self.told], [(2347, "cancelled")])
        self.assertIn("did not complete within 15 min", self.told[0]["why"])
        kind, text = self.ar.message(self.told[0], self.told_resv[0], config.azure_agent())
        self.assertEqual(kind, "ci-cancelled")
        self.assertIn("Rerun its failed jobs", text)
        self.assertEqual([p for p in self.az.writes("PATCH") if p[2]["state"] == "retry"], [])
        # an empty reservation list still makes a message (only roles waiting on the build hear it then)
        self.assertIn("a research reservation held", self.ar.message(self.told[0], [], config.azure_agent())[1])

    def test_an_unreadable_stage_is_retried_later_then_reported(self):
        rec = self.cancel_pending()
        self.queue()
        self.pr.release(rec["id"])
        self.az.refuse = True
        now = time.time()
        self.ar.tick(now + 60)
        self.assertEqual(self.ar.load_state()["builds"]["2347"]["step"], "cancel-sent")
        self.assertIn("not retried yet", self.err.getvalue())
        self.ar.tick(now + self.ar.CANCEL_SETTLE_S + 60)
        self.assertEqual([(e["build"], e["step"]) for e in self.told], [(2347, "cancelled")])
        self.assertIn("could not be read to retry it", self.told[0]["why"])

    def write_cfg_without_lease(self):
        self.cfg.write_text(f'data_root = "{self.data}"\nazure_org = "https://dev.azure.example/org"\n'
                            f'[azure_agent]\npat = "{self.pat}"\n')
        config._CACHE.clear()

    # ---- the swap: both hosts reserved ----

    def test_keep_choice(self):
        def v(host, free, inn, reserved=True, cpu_free=True, fresh=True):
            return self.ar.host_view(host, host, [{"uuid": f"{host}-{i}", "vram_gib": g} for i, g in enumerate(free)],
                                     [], not cpu_free, reserved, inn, 16.0, fresh=fresh)
        # the incident: a kept with only its 8 GB card free, b out with a 32 GB card free
        self.assertEqual(self.ar.keep_choice([v("a", [8], True), v("b", [32], False)])[0], "b")
        # once both are in, b stays and a leaves; and b stays when a is out
        self.assertEqual(self.ar.keep_choice([v("a", [8], True), v("b", [32], True)])[0], "b")
        self.assertEqual(self.ar.keep_choice([v("a", [8], False), v("b", [32], True)])[0], "b")
        # a VRAM difference alone is no swap: both have a large card free, the one in rotation stays
        self.assertEqual(self.ar.keep_choice([v("a", [24], True), v("b", [32], False)])[0], "a")
        self.assertEqual(self.ar.keep_choice([v("a", [24], False), v("b", [32], True)])[0], "b")
        # nobody in (a race): the larger free card
        self.assertEqual(self.ar.keep_choice([v("a", [24], False), v("b", [32], False)])[0], "b")
        # cpu-heavy reserved ranks below cpu-heavy free, above fewer large cards
        self.assertEqual(self.ar.keep_choice([v("a", [24], True, cpu_free=False), v("b", [24], False)])[0], "b")
        self.assertEqual(self.ar.keep_choice([v("a", [24, 24], True, cpu_free=False), v("b", [24], False)])[0], "a")
        # the rule does not apply: a host without a reservation, a stale view, one host
        self.assertEqual(self.ar.keep_choice([v("a", [8], True), v("b", [32, 32], False, reserved=False)])[0], "")
        self.assertEqual(self.ar.keep_choice([v("a", [8], True), v("b", [32], False, fresh=False)])[0], "")
        self.assertEqual(self.ar.keep_choice([v("a", [8], True)])[0], "")

    @contextlib.contextmanager
    def as_host(self, name):
        """Run as another pool host: its own config, data root and state file, the same Azure."""
        if name == "gpuhost-a":
            env = {"SHOAL_CONFIG": str(self.cfg), "SHOAL_DATA_ROOT": str(self.data), "SHOAL_SERVER": name}
        else:
            env = {"SHOAL_CONFIG": str(Path(self.d) / f"host-{name}.toml"),
                   "SHOAL_DATA_ROOT": str(Path(self.d) / f"data-{name}"), "SHOAL_SERVER": name}
        saved = {k: os.environ.get(k) for k in env}
        os.environ.update(env)
        config._CACHE.clear()
        try:
            yield
        finally:
            for k, val in saved.items():
                if val is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = val
            config._CACHE.clear()

    def add_host_b(self):
        data = Path(self.d) / "data-gpuhost-b"
        sdir = data / "_resources" / "gpuhost-b"
        sdir.mkdir(parents=True)
        now = time.time()
        (sdir / time.strftime("events-%Y%m%d.jsonl", time.gmtime(now))).write_text(json.dumps(
            {"type": "resource_sample", "ts": now, "memory": {"available_bytes": 1 << 34},
             "gpus": [{"uuid": "GPU-b1", "vendor": "amd", "vram_total_mib": 32768},
                      {"uuid": "GPU-b2", "vendor": "amd", "vram_total_mib": 32768}]}) + "\n")
        (Path(self.d) / "host-gpuhost-b.toml").write_text(
            f'data_root = "{data}"\nlease_cmd = "{self.helper}"\nazure_org = "https://dev.azure.example/org"\n'
            f'[azure_agent]\npat = "{self.pat}"\n')

    def probe_of(self, host, now):
        """What `pool probe` on `host` reports, as far as the swap rule reads it."""
        import pool_cli
        with self.as_host(host):
            return {"type": "pool_probe", "host": host, "ts": now, "fetched_ts": now,
                    "gpus": (pool_cli._latest_sample(host) or {}).get("gpus") or [], "lease": {},
                    "reservations": self.pr.report(), "agent_rotation": self.ar.summary()}

    def tick_as(self, host, now):
        import pool_cli
        other = "gpuhost-b" if host == "gpuhost-a" else "gpuhost-a"
        with self.as_host(host), \
                patch.object(pool_cli, "pool_names", return_value=["gpuhost-a", "gpuhost-b"]), \
                patch.object(self.ar, "_peer_probes", side_effect=lambda t, names: {other: self.probe_of(other, t)}):
            return self.ar.tick(now, peers=True)

    def test_both_hosts_reserved_the_one_with_the_larger_free_gpu_stays_in(self):
        # measured 2026-11-21: b reserved one of its two 32 GB cards and left rotation; then a reserved its 24 GB card,
        # leaving only 8 GB free, and the last-agent guard kept a in, so a build needing one large GPU waited there
        self.add_host_b()
        with self.as_host("gpuhost-b"):
            rb = self.pr.reserve("gpu@GPU-b1", "s/r", "job:study")
        self.assertNotIn("ci-any-vendor", self.caps(10))
        ra = self.reserve("gpu@GPU-big")
        self.assertIn("kept in rotation: no other enabled agent", ra["rotation"])
        self.assertEqual(self.caps(9)["ci-any-vendor"], "1")
        self.az.agents[9]["assignedRequest"] = assigned(2347)
        self.az.timelines[2347] = timeline()
        self.queue({"key": "gpu", "waiter": "azure-it-2347", "ticket": "9", "want": ["GPU-big"]})
        t = time.time()
        self.tick_as("gpuhost-a", t + 1)  # a is not chosen, and still the last agent: it waits for b
        self.assertEqual(self.caps(9)["ci-any-vendor"], "1")
        self.tick_as("gpuhost-b", t + 2)  # b is chosen: it comes back in although its reservation stands
        self.assertEqual(self.caps(10)["ci-any-vendor"], "1")
        with self.as_host("gpuhost-b"):
            s = self.ar.summary()
            self.assertEqual((s["out"], s["kept"]), (False, "best"))
            self.assertIn("kept IN rotation while reserved (swap)", self.ar.describe(s))
            self.assertIn("gpuhost-b has the most usable free CI capacity", self.ar.describe(s))
        self.tick_as("gpuhost-a", t + self.ar.RETRY_S + 3)  # a's next try: b has the capability now, so a leaves
        self.assertNotIn("ci-any-vendor", self.caps(9))
        # the build waiting on a's reserved card moves to b
        self.assertEqual([(p[1], p[2]["state"]) for p in self.az.writes("PATCH")],
                         [("build/builds/2347/stages/__default", "cancel"),
                          ("build/builds/2347/stages/__default", "retry")])
        self.assertEqual([(e["build"], e["step"]) for e in self.told], [(2347, "left-not-out"), (2347, "retried")])
        puts = len(self.az.writes("PUT"))
        self.assertEqual(puts, 3)  # b out, b back in, a out
        for k in range(1, 6):  # no flapping across repeated ticks of both hosts
            when = t + k * (self.ar.RETRY_S + 1) + 10
            self.tick_as("gpuhost-a", when)
            self.tick_as("gpuhost-b", when + 1)
            self.assertEqual(len(self.az.writes("PUT")), puts)
            self.assertNotIn("ci-any-vendor", self.caps(9))
            self.assertEqual(self.caps(10)["ci-any-vendor"], "1")
        # a's reservation ends: a comes back in and b, reserved, leaves as before the swap
        self.pr.release(ra["id"])
        self.assertEqual(self.caps(9)["ci-any-vendor"], "1")
        self.tick_as("gpuhost-b", t + 10 * (self.ar.RETRY_S + 1))
        self.assertNotIn("ci-any-vendor", self.caps(10))
        with self.as_host("gpuhost-b"):
            self.pr.release(rb["id"])
        self.assertEqual((self.caps(9)["ci-any-vendor"], self.caps(10)["ci-any-vendor"]), ("1", "1"))

    def test_no_swap_on_a_vram_difference_or_an_unknown_view(self):
        self.add_host_b()
        with self.as_host("gpuhost-b"):
            rb = self.pr.reserve("gpu@GPU-b1", "s/r", "job:study")
        ra = self.reserve("gpu@GPU-small")  # a keeps its 24 GB card free: as usable as b's 32 GB one
        self.assertEqual(self.caps(9)["ci-any-vendor"], "1")
        t = time.time()
        for k in range(3):
            self.tick_as("gpuhost-b", t + k * (self.ar.RETRY_S + 1) + 1)
            self.tick_as("gpuhost-a", t + k * (self.ar.RETRY_S + 1) + 2)
        self.assertEqual((self.caps(9).get("ci-any-vendor"), self.caps(10).get("ci-any-vendor")), ("1", None))
        self.assertEqual(self.ar.load_state()["kept"], "best")  # a is the choice now, not only the last agent
        # a view of a that cannot be read moves nothing on b
        import pool_cli
        self.pr.release(ra["id"])
        ra = self.reserve("gpu@GPU-big")  # now b would be better, but b cannot see a
        with self.as_host("gpuhost-b"), \
                patch.object(pool_cli, "pool_names", return_value=["gpuhost-a", "gpuhost-b"]), \
                patch.object(self.ar, "_peer_probes",
                             side_effect=lambda t, names: {"gpuhost-a": {"host": "gpuhost-a", "error": "rc=255"}}):
            self.ar.tick(t + 5 * self.ar.RETRY_S, peers=True)
        self.assertNotIn("ci-any-vendor", self.caps(10))
        self.pr.release(ra["id"])
        with self.as_host("gpuhost-b"):
            self.pr.release(rb["id"])

    def test_swap_off(self):
        self.add_host_b()
        for p in (self.cfg, Path(self.d) / "host-gpuhost-b.toml"):
            p.write_text(p.read_text() + "swap = false\n")
        config._CACHE.clear()
        with self.as_host("gpuhost-b"):
            rb = self.pr.reserve("gpu@GPU-b1", "s/r", "job:study")
        ra = self.reserve("gpu@GPU-big")
        t = time.time()
        self.tick_as("gpuhost-b", t + 1)
        self.tick_as("gpuhost-a", t + self.ar.RETRY_S + 2)
        self.assertEqual((self.caps(9).get("ci-any-vendor"), self.caps(10).get("ci-any-vendor")), ("1", None))
        self.pr.release(ra["id"])
        with self.as_host("gpuhost-b"):
            self.pr.release(rb["id"])

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
