"""CI efficiency review on synthetic snapshot rows: no GitHub, network, model or clock access."""
import argparse
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "shoal"))
import efficiency as ef  # noqa: E402
import efficiency_review as er  # noqa: E402

REPO = "example-owner/example-repo"
CFG = {"repo": REPO}
END = "2026-10-31"
DAYS = ef.last_days(28, END)          # 2026-10-04 .. 2026-10-31
CHANGE = "2026-10-24"                 # the slow leg leaves the per-PR path; the after window starts 10-25
H = 3600.0


def records(slow_until=CHANGE, reruns_from=None, gate_wait_s=1800.0):
    """Four PR heads a day. Each runs a 100-minute fast leg and, up to `slow_until`, a 60-minute
    slow leg; one gate build a day waits `gate_wait_s`."""
    out = []
    for i, day in enumerate(DAYS):
        t0 = ef.day_bounds(day)[0]
        slow = day <= slow_until
        for k in range(4):
            pr, done = 1000 + 4 * i + k, t0 + (k + 1) * H
            mins = 100.0 + (60.0 if slow else 0.0)
            rerun = 40.0 if reruns_from and day >= reruns_from else 0.0
            out.append({"type": "pr_head", "key": f"head:{pr}", "repo": REPO, "head_sha": str(pr), "pr": pr,
                        "first_seen": done - 1800, "ci_complete": done, "push_to_complete_s": 1800.0,
                        "job_minutes": mins, "rerun_minutes": rerun, "gate_wait_s": 0.0})
            out.append({"type": "gh_job", "key": f"gh:{pr}:fast", "repo": REPO, "workflow": "Test", "job": "fast (linux)",
                        "completed": done, "queue_s": 30.0, "run_s": 6000.0, "attempt": 1})
            if slow:
                out.append({"type": "gh_job", "key": f"gh:{pr}:slow", "repo": REPO, "workflow": "Test",
                            "job": "slow (macos)", "completed": done, "queue_s": 600.0, "run_s": 3600.0, "attempt": 1})
        out.append({"type": "az_build", "key": f"az:{i}", "repo": REPO, "pipeline": "gpu-gate", "finished": t0 + 6 * H,
                    "run_s": 600.0, "gate_wait_s": gate_wait_s, "result": "succeeded"})
    return out


def rows_for(recs):
    rows = []
    for i, day in enumerate(DAYS):
        t0 = ef.day_bounds(day)[0]
        prs = [{"number": 1000 + 4 * i + k, "title": f"change {i}.{k}", "merged_at": t0 + (k + 2) * H,
                "files": [["src/m.py", 80, 20]]} for k in range(2)]
        rows.append(ef.repo_day(REPO, day, recs, prs, CFG, ef.DEFAULT_EXCLUDE, ef.DEFAULT_BUCKETS, now=0))
    for i, day in enumerate(DAYS):
        lease = 4 * H if day <= CHANGE else 2 * H
        rt = {"delta_s": 86400.0, "T_e": 2 * 86400.0, "work_s": 20 * H, "wait_s": 20 * H, "unknown_s": 0.0,
              "waits": {"local-ci": 0.0, "remote-ci": 4 * H, "lease": lease}}
        rows.append(ef.shoal_day("s1", day, rt, {REPO}, [r for r in rows if r["kind"] == "repo" and r["day"] == day], now=0))
    return rows


def exp(**kw):
    e = {"type": "experiment", "id": "slow-leg-off", "date": CHANGE, "scope": REPO, "metric": "job_minutes_per_head",
         "predicted": {"change": -0.3}, "change": "the slow leg runs nightly, not per PR"}
    e.update(kw)
    return e


class Metrics(unittest.TestCase):
    def test_metric_values_and_waste_ranking(self):
        rows = rows_for(records())
        doc = er.review(rows, END, ["2026-10-04", "2026-10-10"])
        t = doc["tables"][REPO]
        self.assertEqual(t["job_minutes_per_head"], {"current": 100.0, "previous": 160.0, "baseline": 160.0})
        self.assertEqual(t["merged_lines_per_day"]["current"], 200.0)
        self.assertEqual(doc["tables"]["shoal:s1"]["lease_share"]["baseline"], round(4 / 48, 4))
        cost = doc["waste"]["cost"]
        self.assertEqual((cost[0]["source"], cost[0]["hours"]), ("github run: Test / fast (linux)", round(7 * 4 * 6000 / H, 1)))
        self.assertEqual(cost[0]["per_head_min"]["current"], 100.0)
        waits = {x["source"]: x for x in doc["waste"]["wait"]}
        self.assertEqual(waits["gate wait: gpu-gate"]["hours"], 3.5)
        self.assertNotIn("github queue: Test / slow (macos)", waits, "the slow leg is gone from the current window")
        base = er.review(rows, "2026-10-20", ["2026-10-04", "2026-10-10"])
        top = {x["source"]: x for x in base["waste"]["wait"]}
        self.assertEqual(top["github queue: Test / slow (macos)"]["per_head_min"]["current"], 10.0)
        self.assertEqual(er.metric_value(REPO, "source_minutes_per_head", base["ups"]["current"], "github run: *macos*"),
                         60.0, "a glob over sources")
        old = [{k: v for k, v in r.items() if k != "by_source"} for r in rows]
        self.assertIsNone(er.metric_value(REPO, "source_minutes_per_head", ef.rollup(old, DAYS, old), "*"),
                          "rows from before sources were kept say nothing, not zero")

    def test_report_text(self):
        rows = rows_for(records())
        text = er.render(er.review(rows, END, ["2026-10-04", "2026-10-10"], {"slow-leg-off": exp()}, {}))
        self.assertIn(f"# CI efficiency review, week ending {END}", text)
        self.assertIn("## Largest sources of waste: CI time (current window)", text)
        self.assertIn("| job_minutes_per_head | down | 100 | 160 | -38% | 160 | -38% |", text)
        self.assertIn("| slow-leg-off | job_minutes_per_head | 160 (2026-10-17 to 2026-10-23) | 100 (2026-10-25 to 2026-10-31, 7 d)"
                      " | -38% | -30% | keep |", text)
        self.assertNotIn("\u2014", text, "no em dash in the report")


class Experiments(unittest.TestCase):
    def test_keep_revert_inconclusive_waiting_pending(self):
        rows = rows_for(records())
        v = er.evaluate(exp(), rows, END)
        self.assertEqual((v["verdict"], v["final"], v["before"]["value"], v["after"]["value"]), ("keep", True, 160.0, 100.0))
        self.assertEqual(v["measured_change"], -0.375)
        v = er.evaluate(exp(predicted={"to": 150, "from": 160}), rows, END)
        self.assertEqual(v["verdict"], "keep", "a predicted value is checked as a relative change")
        v = er.evaluate(exp(metric="lines_per_job_minute", predicted={"change": -0.2}), rows, END)
        self.assertEqual(v["verdict"], "revert", "the metric moved the other way")
        v = er.evaluate(exp(predicted={"change": -0.9}), rows, END)
        self.assertEqual(v["verdict"], "inconclusive", "less than half the predicted effect")
        v = er.evaluate(exp(), rows, "2026-10-29")
        self.assertEqual((v["verdict"], v["final"], v["after"]["n"]), ("keep", False, 5), "provisional before a week")
        self.assertEqual(er.evaluate(exp(), rows, "2026-10-26")["state"], "waiting")
        self.assertEqual(er.evaluate(exp(date=None, status="pending"), rows, END)["state"], "pending")
        self.assertEqual(er.evaluate(exp(metric="nope"), rows, END)["state"], "invalid")
        v = er.evaluate(exp(scope="shoal:s1", metric="lease_share", predicted={"change": -0.4}), rows, END)
        self.assertEqual(v["verdict"], "keep")
        self.assertAlmostEqual(v["measured_change"], -0.5, places=2)

    def test_a_worse_guardrail_withholds_keep(self):
        rows = rows_for(records(reruns_from="2026-10-25"))
        v = er.evaluate(exp(), rows, END)
        self.assertEqual(v["verdict"], "inconclusive")
        self.assertIn("guardrail worse: rerun_share 0 to 0.4", v["notes"])

    def test_log_round_trip_and_close(self):
        rows = rows_for(records())
        with tempfile.TemporaryDirectory() as td:
            log = os.path.join(td, "experiments.jsonl")
            with open(log, "w") as fh:
                fh.write(json.dumps(exp(date=None, status="pending")) + "\n")
                fh.write(json.dumps(exp()) + "\n")  # the change went in: the later line replaces the pending one
                fh.write(json.dumps(exp(id="other", date="2026-10-24")) + "\n")
                fh.write(json.dumps(exp(id="same-change", ref="o/r#1", metric="rerun_share")) + "\n")
            exps, verdicts, lines = er.load_log(log)
            self.assertEqual(exps["slow-leg-off"]["date"], CHANGE)
            doc = er.review(rows, "2026-10-29", None, exps, verdicts)
            st = {x["id"]: x for x in doc["experiments"]}
            self.assertIn("overlaps other, same-change", st["slow-leg-off"]["notes"])
            exps["slow-leg-off"]["ref"] = "o/r#1"
            self.assertEqual(er.overlaps(exps)["slow-leg-off"], ["other"], "two lines for one change do not confound")
            exps["slow-leg-off"].pop("ref")
            er.save_verdicts(log, lines, doc["verdicts_new"])
            exps, verdicts, lines = er.load_log(log)
            er.save_verdicts(log, lines, er.review(rows, "2026-10-29", None, exps, verdicts)["verdicts_new"])
            exps, verdicts, lines = er.load_log(log)
            self.assertEqual(len(verdicts["slow-leg-off"]), 1, "a re-run review replaces its own verdict")
            doc = er.review(rows, END, None, exps, verdicts)
            er.save_verdicts(log, lines, doc["verdicts_new"])
            exps, verdicts, _ = er.load_log(log)
            self.assertTrue(verdicts["slow-leg-off"][-1]["final"])
            later = er.review(rows, END, None, exps, verdicts)
            st = {x["id"]: x for x in later["experiments"]}
            self.assertEqual((st["slow-leg-off"]["state"], st["other"]["state"]), ("closed", "closed"))
            self.assertNotIn("slow-leg-off", [v["id"] for v in later["verdicts_new"]], "a closed experiment is not judged again")
            self.assertEqual((st["same-change"]["verdict"], st["same-change"]["final"]), ("inconclusive", False),
                             "a metric at zero before has no relative change; it stays open until it ages out")


class Commands(unittest.TestCase):
    def ns(self, td, **kw):
        a = argparse.Namespace(rows=os.path.join(td, "eff.jsonl"), day="", baseline="2026-10-04:2026-10-10",
                               experiments="", report_dir="", wake_seat="", dry_run=False, json=False)
        for k, v in kw.items():
            setattr(a, k, v)
        return a

    def test_review_writes_report_and_verdicts_and_wakes_only_when_asked(self):
        import config
        with tempfile.TemporaryDirectory() as td, mock.patch.object(config, "file_values", lambda: {}), \
                mock.patch.object(config, "data_root", lambda: td), mock.patch.object(er, "wake_seat") as wake, \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            ef.save_rows(os.path.join(td, "eff.jsonl"), [], rows_for(records()))
            self.assertEqual(er.cmd_experiment(argparse.Namespace(
                id="slow-leg-off", scope=REPO, metric="job_minutes_per_head", change="slow leg nightly", date=CHANGE,
                ref="", source="", predict_change=-0.3, predict_to=None, predict_from=None, before="", after="",
                rows=os.path.join(td, "eff.jsonl"), experiments="", dry_run=False)), 0)
            self.assertEqual(er.cmd_experiment(argparse.Namespace(
                id="x", scope=REPO, metric="speed", change="?", date="", ref="", source="", predict_change=-0.3,
                predict_to=None, predict_from=None, before="", after="", rows=os.path.join(td, "eff.jsonl"),
                experiments="", dry_run=False)), 2, "an unknown metric is refused")
            self.assertEqual(er.cmd_review(self.ns(td)), 0)
            report = os.path.join(td, "reports", f"efficiency-review-{END}.md")
            self.assertTrue(os.path.exists(report))
            _, verdicts, _ = er.load_log(os.path.join(td, "experiments.jsonl"))
            self.assertEqual(verdicts["slow-leg-off"][-1]["verdict"], "keep")
            wake.assert_not_called()
            er.cmd_review(self.ns(td, wake_seat="reviewer"))
            self.assertEqual(wake.call_args.args[0], "reviewer")
            self.assertIn(report, wake.call_args.args[1])
            with self.assertRaises(SystemExit):
                er.cmd_review(self.ns(td, baseline="2026-10-10:2026-10-04"))

    def test_parsers(self):
        p = argparse.ArgumentParser()
        sub = p.add_subparsers(dest="cmd")
        ef.add_parser(sub)
        a = p.parse_args(["efficiency", "review", "--baseline", "2026-10-04:2026-10-10", "--dry-run"])
        self.assertIs(a.func, er.cmd_review)
        a = p.parse_args(["efficiency", "experiment", "--id", "x", "--scope", REPO, "--metric", "rerun_share",
                          "--change", "c", "--predict-change", "-0.5"])
        self.assertIs(a.func, er.cmd_experiment)


if __name__ == "__main__":
    unittest.main()
