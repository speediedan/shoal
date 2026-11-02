"""CI wait by repository on recorded fixtures: no GitHub, Azure, network or clock access."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "shoal"))
import ci_wait as cw  # noqa: E402

REPO = "example-owner/example-repo"
T0 = 1793000000.0  # a fixed instant (2026-10-27)


def iso(t):
    import datetime as dt
    return dt.datetime.fromtimestamp(t, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


CFG = {"repos": [{"repo": REPO, "ignore": ["^Auto Labeler$"],
                  "azure": [{"org": "https://dev.azure.example/org", "project": "proj"}],
                  "local": {"paths": ["/example-repo/"], "holders": ["^azure-ex-", "^example-repo$"]}}],
       "backfill_days": 1}


class FakeRunner:
    """Answers `gh api` and `az` calls from fixtures and counts them."""

    def __init__(self):
        self.calls = []
        self.updated = iso(T0 + 1000)

    def __call__(self, argv, login=False, timeout=60):
        self.calls.append((tuple(argv), login))
        if argv[:2] == ["gh", "api"]:
            path = argv[2]
            if "/actions/runs?created=" in path:
                if not path.endswith("page=1"):
                    return 0, json.dumps({"workflow_runs": []})
                runs = [{"id": 11, "name": "Test full", "event": "pull_request", "status": "completed",
                         "run_attempt": 2, "head_sha": "abc", "pull_requests": [], "updated_at": self.updated},
                        {"id": 12, "name": "Auto Labeler", "event": "pull_request", "status": "completed",
                         "run_attempt": 1, "head_sha": "abc", "pull_requests": [], "updated_at": self.updated},
                        {"id": 13, "name": "Test full", "event": "pull_request", "status": "in_progress",
                         "run_attempt": 1, "head_sha": "def", "pull_requests": [], "updated_at": self.updated}]
                return 0, json.dumps({"workflow_runs": runs if f"created={cw._day(T0)}" in path else []})
            if path.endswith("/runs/11/jobs?filter=all&per_page=100"):
                return 0, json.dumps({"jobs": [
                    {"id": 1, "name": "cpu", "run_attempt": 1, "head_sha": "abc", "created_at": iso(T0 + 10),
                     "started_at": iso(T0 + 70), "completed_at": iso(T0 + 670), "conclusion": "failure"},
                    {"id": 2, "name": "cpu", "run_attempt": 2, "head_sha": "abc", "created_at": iso(T0 + 700),
                     "started_at": iso(T0 + 760), "completed_at": iso(T0 + 1360), "conclusion": "success"}]})
            if path.endswith("/runs/12/jobs?filter=all&per_page=100"):
                return 0, json.dumps({"jobs": [
                    {"id": 3, "name": "label", "run_attempt": 1, "head_sha": "abc", "created_at": iso(T0),
                     "started_at": iso(T0 + 5), "completed_at": iso(T0 + 9000), "conclusion": "success"}]})
            if path.endswith("/commits/abc/pulls"):
                return 0, json.dumps([{"number": 7, "head": {"sha": "abc"}}])
            return 1, ""
        if argv[:3] == ["az", "pipelines", "runs"]:
            return 0, json.dumps([
                {"id": 900, "status": "completed", "result": "succeeded", "reason": "pullRequest",
                 "queueTime": iso(T0 + 20), "finishTime": iso(T0 + 4000), "sourceVersion": "merge-sha",
                 "triggerInfo": {"pr.number": "7", "pr.sourceSha": "abc"},
                 "definition": {"name": "gpu-gate"}, "repository": {"id": REPO, "type": "GitHub"}},
                {"id": 901, "status": "completed", "repository": {"id": "someone/else"}}])
        if argv[:3] == ["az", "devops", "invoke"]:
            return 0, json.dumps({"records": [
                {"type": "Checkpoint.Approval", "startTime": iso(T0 + 30), "finishTime": iso(T0 + 2430),
                 "result": "succeeded"},
                {"type": "Job", "name": "gpu", "startTime": iso(T0 + 2436), "finishTime": iso(T0 + 4000)}]})
        return 1, ""


class Collection(unittest.TestCase):
    def test_github_azure_and_pr_heads(self):
        with tempfile.TemporaryDirectory() as td:
            run = FakeRunner()
            cw.collect(CFG, td, run=run, now=T0 + 5000)
            recs = cw.load(td, REPO)
            gh = sorted((r for r in recs.values() if r["type"] == "gh_job"), key=lambda r: r["key"])
            self.assertEqual([(r["attempt"], r["queue_s"], r["run_s"]) for r in gh if r["workflow"] == "Test full"],
                             [(1, 60.0, 600.0), (2, 60.0, 600.0)])
            self.assertEqual({r["pr"] for r in gh}, {7}, "the head resolves through commits/{sha}/pulls")
            az = [r for r in recs.values() if r["type"] == "az_build"]
            self.assertEqual(len(az), 1, "a build of another repository is not this repository's")
            self.assertEqual((az[0]["gate_wait_s"], az[0]["run_s"], az[0]["head_sha"]), (2400.0, 1564.0, "abc"))
            # every az call ran under a login shell, so its credential never passes through here
            self.assertTrue(all(login for argv, login in run.calls if argv[0] == "az"))
            head = next(r for r in recs.values() if r["type"] == "pr_head")
            # the ignored bot workflow (which ended last) does not set CI complete; the gate does
            self.assertEqual((head["pr"], head["first_seen"], head["ci_complete"]), (7, T0 + 10, T0 + 4000))
            self.assertEqual(head["checks_counted"], 3)
            self.assertAlmostEqual(head["job_minutes"], (600 + 600 + 1564) / 60, places=1)
            self.assertAlmostEqual(head["rerun_minutes"], 10.0)

    def test_a_second_pass_fetches_no_jobs_for_unchanged_runs(self):
        with tempfile.TemporaryDirectory() as td:
            run = FakeRunner()
            cw.collect(CFG, td, run=run, now=T0 + 5000)
            n_jobs = sum(1 for argv, _ in run.calls if "/jobs?" in " ".join(argv))
            self.assertEqual(n_jobs, 2, "the in-progress run is not fetched")
            run.calls.clear()
            got = cw.collect(CFG, td, run=run, now=T0 + 5100)
            self.assertEqual(sum(1 for argv, _ in run.calls if "/jobs?" in " ".join(argv)), 0)
            self.assertEqual(sum(1 for argv, _ in run.calls if argv[:3] == ("az", "devops", "invoke")), 0)
            self.assertEqual(got[REPO], 0, "nothing new to store")
            run.updated = iso(T0 + 2000)  # a rerun changes the run: its jobs are read again
            run.calls.clear()
            cw.collect(CFG, td, run=run, now=T0 + 5200)
            self.assertEqual(sum(1 for argv, _ in run.calls if "/jobs?" in " ".join(argv)), 2)

    def test_required_list_overrides_ignore(self):
        cfg = dict(CFG["repos"][0], required=["^gpu-gate$"])
        self.assertTrue(cw.counted(cfg, "", "", "gpu-gate"))
        self.assertFalse(cw.counted(cfg, "Test full", "cpu"))
        self.assertFalse(cw.counted(CFG["repos"][0], "Auto Labeler", "label"))
        self.assertTrue(cw.counted(CFG["repos"][0], "Test full", "cpu"))


class Local(unittest.TestCase):
    def test_jobs_and_direct_leases(self):
        with tempfile.TemporaryDirectory() as td:
            jobs = Path(td) / "jobs"
            jobs.mkdir()
            rc = jobs / "u1.rc"
            rc.write_text("0\n")
            os.utime(rc, (T0 + 900, T0 + 900))
            (jobs / "u1.json").write_text(json.dumps({
                "unit": "u1", "shoal": "s", "slug": "lane", "name": "ci", "class": "ci", "rc_file": str(rc),
                "started_utc": iso(T0), "lease_granted": iso(T0 + 300),
                "placement": {"repo": REPO, "submit_ts": T0}}))
            (jobs / "u2.json").write_text(json.dumps({
                "unit": "u2", "shoal": "s", "slug": "lane", "name": "tests", "cwd": "/home/u/repos/example-repo",
                "started_utc": iso(T0), "rc_file": str(jobs / "missing.rc")}))
            (jobs / "u3.json").write_text(json.dumps({
                "unit": "u3", "shoal": "s", "name": "x", "cwd": "/elsewhere", "started_utc": iso(T0)}))
            got = {r["key"]: r for r in cw.local_jobs([str(jobs)], CFG["repos"])}
            self.assertEqual((got["job:s:u1"]["lease_wait_s"], got["job:s:u1"]["run_s"]), (300.0, 600.0))
            self.assertEqual(got["job:s:u2"]["repo"], REPO, "attributed by its working directory")
            self.assertIsNone(got["job:s:u2"]["finished"], "still running: no exit-code file")
            self.assertIsNone(got["job:s:u3"]["repo"], "unattributed, never guessed")
            ev = Path(td) / "events"
            ev.mkdir()
            lines = []
            for lid, holder in (("L1", "example-repo"), ("L2", "azure-ex-55"), ("L3", "unknown-thing")):
                lease = {"lease_id": lid, "holder": holder, "key": "gpu", "since": iso(T0)}
                lines += [{"type": "lease_observed_acquire", "host": "h", "ts": T0 + 5, "lease": lease},
                          {"type": "lease_observed_release", "host": "h", "ts": T0 + 125, "lease": lease,
                           "release_after": T0 + 110, "release_by": T0 + 125}]
            (ev / "events-20261027.jsonl").write_text("".join(json.dumps(x) + "\n" for x in lines))
            ls = {r["holder"]: r for r in cw.local_leases([str(ev)], CFG["repos"])}
            self.assertEqual((ls["example-repo"]["repo"], ls["example-repo"]["kind"], ls["example-repo"]["held_s"]),
                             (REPO, "direct", 125.0))
            self.assertEqual(ls["azure-ex-55"]["kind"], "gate", "the gate's own run is never counted twice")
            self.assertIsNone(ls["unknown-thing"]["repo"])


class Report(unittest.TestCase):
    def test_summary_and_ranking(self):
        with tempfile.TemporaryDirectory() as td:
            cw.collect(CFG, td, run=FakeRunner(), now=T0 + 5000)
            s = cw.summarize(list(cw.load(td, REPO).values()), T0, T0 + 86400)
            self.assertEqual(s["pr_heads"], 1)
            self.assertEqual(s["push_to_complete_s"]["p50"], 3990.0)
            self.assertEqual(s["gate"]["wait_s"]["p50"], 2400.0)
            self.assertEqual(s["most_wait_per_head_s"][0], ["gate: gpu-gate", 2400.0])
            self.assertEqual(s["github"]["rerun_jobs"], 1)
            ranked = cw.summarize(list(cw.load(td, REPO).values()), T0, T0 + 86400, CFG["repos"][0])
            self.assertNotIn("Auto Labeler", " ".join(k for k, _ in ranked["most_wait_per_head_s"]),
                             "an ignored check is not ranked either")


if __name__ == "__main__":
    unittest.main()
