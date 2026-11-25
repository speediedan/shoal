"""CI efficiency snapshot on synthetic fixtures: no GitHub, network or clock access."""
import gzip
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
import urllib.parse
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "shoal"))
import ci_wait as cw  # noqa: E402
import efficiency as ef  # noqa: E402
import role_time as rt  # noqa: E402

REPO = "example-owner/example-repo"
OTHER = "example-owner/other-repo"
DAY = "2026-10-27"
T0 = ef.day_bounds(DAY)[0]
H = 3600.0
CFG = {"repo": REPO, "ignore": ["^Auto Labeler$"]}


def iso(t):
    import datetime as dt
    return dt.datetime.fromtimestamp(t, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class FakeGitHub:
    """`gh api` answers for the search and files endpoints, with call counts and scripted failures."""

    def __init__(self, prs=None, files=None, fail_search=0):
        self.prs = prs or []          # [{"number", "title", "body", "merged_at": epoch}]
        self.files = files or {}      # {number: [{"filename", "additions", "deletions"}]}
        self.fail_search = fail_search
        self.calls = []

    def __call__(self, argv, login=False, timeout=60):
        self.calls.append(tuple(argv))
        path = argv[2]
        if path.startswith("search/issues?"):
            if self.fail_search:
                self.fail_search -= 1
                return 1, ""
            if not path.endswith("page=1"):
                return 0, json.dumps({"items": []})
            return 0, json.dumps({"items": [
                {"number": p["number"], "title": p.get("title", ""), "body": p.get("body", ""),
                 "pull_request": {"merged_at": iso(p["merged_at"])}} for p in self.prs]})
        if "/pulls/" in path and "/files?" in path:
            n = int(path.split("/pulls/")[1].split("/")[0])
            if n not in self.files:
                return 1, ""
            page = int(path.rsplit("page=", 1)[1])
            return 0, json.dumps(self.files[n] if page == 1 else [])
        return 1, ""

    def count(self, kind):
        return sum(1 for c in self.calls if kind in c[2])


def reader(fake):
    return ef.GitHubReader(run=fake, pause=0, sleep=lambda s: None)


def head(sha, pr, done, mins, rerun=0.0, p2c=1800.0, gate_wait=0.0):
    return {"type": "pr_head", "key": f"head:{sha}", "repo": REPO, "head_sha": sha, "pr": pr,
            "first_seen": done - p2c, "ci_complete": done, "push_to_complete_s": p2c, "job_minutes": mins,
            "rerun_minutes": rerun, "gate_wait_s": gate_wait}


def store_records():
    """One day of a repository's ci-wait records: two PR heads, GitHub jobs (one ignored), a failed
    gate build, local CI jobs (one with the unleased-CPU mark, one older without the field)."""
    return [
        head("a1", 1, T0 + 2 * H, 40.0, rerun=10.0, p2c=3600.0),
        head("b1", 2, T0 + 5 * H, 20.0, p2c=1800.0),
        head("z9", 1, T0 - 20 * H, 30.0, p2c=600.0),   # PR 1's earlier head, the day before
        {"type": "gh_job", "key": "gh:1", "repo": REPO, "workflow": "Test", "job": "cpu", "completed": T0 + H,
         "queue_s": 60.0, "run_s": 1200.0},
        {"type": "gh_job", "key": "gh:2", "repo": REPO, "workflow": "Auto Labeler", "job": "label",
         "completed": T0 + H, "queue_s": 5.0, "run_s": 9000.0},
        {"type": "az_build", "key": "az:1", "repo": REPO, "pipeline": "gpu-gate", "finished": T0 + 3 * H,
         "run_s": 600.0, "gate_wait_s": 900.0, "result": "failed", "approval_result": "succeeded"},
        {"type": "local_job", "key": "job:s:u1", "repo": REPO, "kind": "ci", "finished": T0 + 4 * H,
         "lease_wait_s": 300.0, "run_s": 600.0, "unleased_cpu": True},
        {"type": "local_job", "key": "job:s:u2", "repo": REPO, "kind": "plain", "finished": T0 + 4 * H,
         "lease_wait_s": None, "run_s": 120.0},
        {"type": "local_job", "key": "job:s:u3", "repo": REPO, "kind": "research", "finished": T0 + 4 * H,
         "run_s": 9999.0},
        {"type": "local_lease", "key": "lease:h:L1", "repo": REPO, "kind": "direct", "released": T0 + 6 * H,
         "held_s": 180.0},
        {"type": "local_lease", "key": "lease:h:L2", "repo": REPO, "kind": "gate", "released": T0 + 6 * H,
         "held_s": 5000.0},
    ]


MERGED = [{"number": 1, "title": "Add the widget", "merged_at": T0 + 2.5 * H},
          {"number": 2, "title": "Fix a typo", "merged_at": T0 + 6 * H},
          {"number": 3, "title": 'Revert "Add the gadget"', "body": f"Reverts {REPO}#77", "merged_at": T0 + 7 * H},
          {"number": 4, "title": "Late", "merged_at": T0 + 25 * H},     # the next UTC day
          {"number": 5, "title": "Early", "merged_at": T0 - 60}]       # the day before
FILES = {1: [{"filename": "src/widget.py", "additions": 400, "deletions": 50},
             {"filename": "uv.lock", "additions": 3000, "deletions": 2000},
             {"filename": "docs/vendored/x.md", "additions": 100, "deletions": 0}],
         2: [{"filename": "README.md", "additions": 3, "deletions": 3}],
         3: [{"filename": "src/gadget.py", "additions": 0, "deletions": 120}],
         4: [{"filename": "a.py", "additions": 1, "deletions": 0}],
         5: [{"filename": "a.py", "additions": 1, "deletions": 0}]}


class Basics(unittest.TestCase):
    def test_exclusions_buckets_and_reverts(self):
        ex = ef.DEFAULT_EXCLUDE
        for p in ("uv.lock", "sub/poetry.lock", "package-lock.json", "requirements/ci/py311.txt", "nb/x.ipynb",
                  "pkg/api_pb2.py", "web/app.min.js", "third/vendored/lib.py", "vendor/x.go", ".agents/skills/s/SKILL.md",
                  "pkg/generated/models.py"):
            self.assertTrue(ef.excluded(p, ex), p)
        for p in ("src/lock.py", "src/vendoring.py", "docs/generation.md"):
            self.assertFalse(ef.excluded(p, ex), p)
        self.assertEqual(ef.bucket_names([50, 500]), ["lt50", "50to500", "ge500"])
        self.assertEqual([ef.bucket(n, [50, 500]) for n in (0, 49, 50, 499, 500)],
                         ["lt50", "lt50", "50to500", "50to500", "ge500"])
        self.assertTrue(ef.is_revert('Revert "x"'))
        self.assertTrue(ef.is_revert("reverts the cache change"))
        self.assertFalse(ef.is_revert("Revert-proof the cache"))
        self.assertFalse(ef.is_revert("Reverting is hard"))
        self.assertEqual(ef.revert_targets({"title": 'Revert "x"', "body": f"Reverts {REPO}#12 and reverts #13"}),
                         [12, 13])
        self.assertEqual(ef.revert_targets({"title": "Mentions reverts #12", "body": ""}), [],
                         "only a revert PR names a reverted PR")

    def test_settings_defaults_flags_and_cache_beside_out(self):
        import config
        with tempfile.TemporaryDirectory() as td, \
                mock.patch.object(config, "file_values", lambda: {"efficiency": {"buckets": [100, 10], "min_coverage": 0.8}}), \
                mock.patch.object(config, "data_root", lambda: td):
            s = ef.settings()
            self.assertEqual(s["out"], os.path.join(td, "_efficiency", "efficiency.jsonl"))
            self.assertEqual(s["cache_dir"], os.path.join(td, "_efficiency"))
            self.assertEqual((s["buckets"], s["min_coverage"]), ([10.0, 100.0], 0.8))
            s = ef.settings(out=os.path.join(td, "x", "eff.jsonl"))
            self.assertEqual(s["cache_dir"], os.path.join(td, "x"), "the file cache follows --out")
            s = ef.settings(out=os.path.join(td, "x", "eff.jsonl"), cache_dir=os.path.join(td, "c"))
            self.assertEqual(s["cache_dir"], os.path.join(td, "c"))


class GitHub(unittest.TestCase):
    def test_one_search_per_week_of_days_keeps_utc_days(self):
        fake = FakeGitHub(MERGED, FILES)
        with tempfile.TemporaryDirectory() as td:
            days = ef.last_days(9, ef.shift(DAY, 1))
            got = ef.merged_by_day(reader(fake), REPO, days, td)
            self.assertEqual(fake.count("search/issues"), 2, "nine days: two searches, not nine")
            q = next(c[2] for c in fake.calls if "search/issues" in c[2])
            self.assertIn("merged%3A", q)
            self.assertEqual([p["number"] for p in got[DAY]], [1, 2, 3])
            self.assertEqual([p["number"] for p in got[ef.shift(DAY, 1)]], [4])
            self.assertEqual([p["number"] for p in got[ef.shift(DAY, -1)]], [5])
            self.assertEqual(got[DAY][0]["files"][0], ["src/widget.py", 400, 50])
            n = fake.count("/files?")
            ef.merged_by_day(reader(fake), REPO, [DAY], td)
            self.assertEqual(fake.count("/files?"), n, "a merged PR's file list is read once, then cached")

    def test_failures_retry_then_none_and_pacing(self):
        slept = []
        fake = FakeGitHub(MERGED, FILES, fail_search=1)
        gh = ef.GitHubReader(run=fake, pause=2.5, sleep=slept.append)
        self.assertEqual(len(gh.merged_prs(REPO, DAY)), 3, "a failed page is retried")
        self.assertIn(30.0, slept, "after a backoff")
        self.assertTrue(any(0 < s <= 2.5 for s in slept), "calls are spaced for the search rate limit")
        fake = FakeGitHub(MERGED, FILES, fail_search=99)
        self.assertIsNone(reader(fake).merged_prs(REPO, DAY))
        self.assertEqual(fake.count("search/issues"), 1 + ef.GH_SEARCH_RETRIES)
        self.assertIsNone(reader(FakeGitHub()).pr_files(REPO, 42), "an unreadable file list is None, not []")

    def test_moved_repository_searched_by_its_current_name(self):
        moved = FakeGitHub(MERGED, FILES)
        inner = moved.__call__

        def run(argv, login=False):
            if argv[2] == f"repos/{REPO}":  # the REST redirect answers the new name; search would not follow it
                return 0, json.dumps({"full_name": "neworg/" + REPO.split("/", 1)[1]})
            if argv[2].startswith("search/issues?") and urllib.parse.quote(f"repo:{REPO} ") in argv[2]:
                return 1, ""
            return inner(argv, login=login)
        gh = reader(run)
        self.assertEqual(len(gh.merged_prs(REPO, DAY)), 3, "the moved repository's merges are found")
        self.assertEqual(len(gh.merged_prs(REPO, ef.shift(DAY, 1))), 1)
        lookups = [c for c in moved.calls if c[2] == f"repos/{REPO}"]
        self.assertEqual(lookups, [], "the lookup is answered before the fake")
        self.assertEqual(gh.current_name(REPO), "neworg/" + REPO.split("/", 1)[1])
        self.assertEqual(reader(FakeGitHub()).current_name(REPO), REPO, "an unreadable lookup keeps the name")
        with tempfile.TemporaryDirectory() as td:
            got = ef.merged_by_day(reader(FakeGitHub(MERGED, {1: FILES[1]})), REPO, [DAY], td)
            self.assertEqual([p["files"] is None for p in got[DAY]], [False, True, True])
            self.assertEqual(sorted(ef._load_json(ef._cache_path(td, REPO), {})), ["1"], "only a full read is cached")


class RepoDay(unittest.TestCase):
    def row(self, prs=None, ok=True):
        prs = [dict(p, files=[[f["filename"], f["additions"], f["deletions"]] for f in FILES[p["number"]]])
               for p in MERGED[:3]] if prs is None else prs
        return ef.repo_day(REPO, DAY, store_records(), prs, CFG, ef.DEFAULT_EXCLUDE, ef.DEFAULT_BUCKETS, gh_ok=ok, now=0)

    def test_lines_cost_ratios_and_guardrails(self):
        r = self.row()
        m, ci, g = r["merged"], r["ci"], r["guardrails"]
        self.assertEqual((m["prs"], m["lines"], m["lines_excluded"]), (3, 450 + 6 + 120, 5100))
        self.assertEqual(m["buckets"], {"lt50": 1, "50to500": 2, "ge500": 0})
        self.assertEqual(ci["pr_heads"], 2, "the head that completed the day before is not this day's")
        self.assertEqual(ci["github_jobs"], 1, "an ignored workflow is not CI")
        # GitHub 1200 s, gate 600 s, local CI and plain jobs 720 s (research is not CI), direct lease 180 s
        self.assertEqual(ci["job_minutes"], round((1200 + 600 + 720 + 180) / 60, 2))
        self.assertEqual(ci["ci_wait_s"], 3600 + 1800 + 300 + 720)
        self.assertEqual(r["ratios"]["lines_per_job_minute"], round(576 / 45.0, 3))
        self.assertEqual(r["ratios"]["lines_per_ci_wait_hour"], round(576 / (6420 / 3600), 3))
        self.assertEqual(g["rerun_share"], round(10 / 60, 3))
        self.assertEqual((g["failed_gates"], g["reverts_merged"], g["reverted_targets"]), (1, 1, [77]))
        self.assertEqual((g["unleased_cpu_jobs"], g["unleased_cpu_observed"]), (1, 1),
                         "a job record from before the mark existed says nothing either way")
        p1 = next(p for p in r["prs"] if p["n"] == 1)
        self.assertEqual((p1["heads"], p1["job_minutes"], p1["ci_wait_s"]), (2, 70.0, 4200.0),
                         "a PR's own CI is every head the store saw for it, on any day")
        b = r["by_bucket"]["50to500"]
        self.assertEqual((b["prs"], b["median_job_minutes"]), (2, 70.0), "a PR with no heads has no CI median")

    def test_unreadable_github_counts_no_merges(self):
        r = self.row(prs=[], ok=False)
        self.assertIsNone(r["merged"])
        self.assertEqual(r["sources"]["github"], "error")
        self.assertIsNone(r["ratios"]["lines_per_job_minute"])
        self.assertGreater(r["ci"]["job_minutes"], 0, "the day's CI cost is still recorded")
        self.assertIn("GitHub unreadable", ef.line(r))


def journal_lines(t0, roles, extra=()):
    """A journal segment: header, membership, snapshots. `roles` is {role: [(t, kind)]} where kind
    is "work" (a turn) or a ledger wait sub such as "remote-ci" or "lease" target."""
    out = [{"type": "segment", "policy": rt.POLICY_VERSION, "opened": t0}]
    for r in roles:
        out.append({"type": "membership", "role": r, "spans": [[T0 - 30 * 86400, None]]})
    out += list(extra)
    for r, obs in roles.items():
        for t, kind in obs:
            nat = {"state": "turn" if kind == "work" else "between", "observed_at": t, "budget_s": 1e9}
            led = {"state": "waiting", "sub": kind, "observed_at": t, "budget_s": 1e9}
            if kind == "lease":
                led = dict(led, sub="local-ci", target="lease:gpu")
            out.append({"type": "snapshot", "role": r, "t": t, "native": nat, "ledger": led})
    return out


def write_journal(d, sid):
    """Closed segments for the day before and DAY, then a live file: role `a` works 6 h and waits on
    remote CI the rest of DAY; role `b` waits on a lease from 12:00; role `gone` was dropped from the
    spec (listed in an old segment with an open span, absent from every later header)."""
    base = Path(d) / sid
    base.mkdir(parents=True)
    old = journal_lines(T0 - 2 * 86400, {"gone": [(T0 - 2 * 86400 + 10, "work")]})
    prev = journal_lines(T0 - 86400, {"a": [(T0 - 3600, "work")], "b": [(T0 - 3600, "work")]})
    day = journal_lines(T0, {"a": [(T0 + 6 * H, "remote-ci")], "b": [(T0 + 12 * H, "lease")]})
    for name, lines in ((f"role-time-observations-{ef.shift(DAY, -2).replace('-', '')}.jsonl.gz", old),
                        (f"role-time-observations-{ef.shift(DAY, -1).replace('-', '')}.jsonl.gz", prev)):
        with gzip.open(base / name, "wt") as fh:
            fh.write("".join(json.dumps(x) + "\n" for x in lines))
    (base / f"role-time-observations-{DAY.replace('-', '')}.jsonl").write_text(
        "".join(json.dumps(x) + "\n" for x in day))
    (base / "role-time-observations.jsonl").write_text(
        "".join(json.dumps(x) + "\n" for x in journal_lines(T0 + 86400, {"a": [], "b": []})))
    return str(base / "role-time-observations.jsonl")


class RoleTime(unittest.TestCase):
    def test_a_day_from_the_journal(self):
        with tempfile.TemporaryDirectory() as td:
            j = write_journal(td, "s1")
            obs, mem = ef.load_days(j, [DAY])
            self.assertNotIn("gone", {r for r, sp in mem.items() if any(e is None or e > T0 for _, e in sp)},
                             "a role a later segment header leaves out stopped being a member then")
            d = ef.role_time_day(obs[DAY], mem, DAY)
            self.assertEqual(d["work_s"], 6 * H + 12 * H, "a works until 06:00, b until 12:00 (carried in)")
            self.assertEqual(d["waits"], {"local-ci": 0.0, "remote-ci": 18 * H, "lease": 12 * H})
            x = ef.shoal_ratios(d, 3000)
            self.assertEqual((x["AAR"], x["coverage"]), (2.0, 1.0))
            self.assertEqual(x["ci_lease_share"], round(30 / 48, 4))
            self.assertEqual(x["lines_per_role_ci_wait_hour"], 100.0)
            # the role-time report reads the same journal the same way, but for the dropped role
            o2, m2 = rt.load_observations(j)
            per_role, _ = rt.account(o2, {r: s for r, s in m2.items() if r != "gone"}, T0, T0 + 86400)
            self.assertEqual(rt.summarize(per_role, 86400)["T"]["work"], d["work_s"])

    def test_low_coverage_gives_no_ratios(self):
        d = {"delta_s": 86400.0, "T_e": 86400.0, "work_s": 100.0, "wait_s": 100.0, "unknown_s": 86000.0,
             "waits": {"remote-ci": 100.0}}
        x = ef.shoal_ratios(d, 5000)
        self.assertIsNone(x["lines_per_role_ci_wait_hour"])
        self.assertIsNone(x["AAR"])
        self.assertEqual(x["coverage"], round(400 / 86400, 4))
        self.assertIsNone(ef.shoal_ratios(None, 5)["coverage"])


class Snapshot(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        td = self.td.name
        self.store = os.path.join(td, "store")
        os.makedirs(self.store)
        cw.save(self.store, REPO, store_records(), {})
        self.s = {"out": os.path.join(td, "out", "eff.jsonl"), "cache_dir": os.path.join(td, "cache"),
                  "store": self.store, "exclude": list(ef.DEFAULT_EXCLUDE), "buckets": [50.0, 500.0],
                  "min_coverage": 0.5}
        self.journal = write_journal(os.path.join(td, "rt"), "s1")

    def tearDown(self):
        self.td.cleanup()

    def run_snap(self, fake, days=(DAY,), shoals=None):
        shoals = [("s1", self.journal, {REPO, OTHER})] if shoals is None else shoals
        return ef.snapshot(list(days), [CFG], self.s, reader(fake), shoals, now=1)

    def test_rows_rerun_and_github_down(self):
        rows, new, failed = self.run_snap(FakeGitHub(MERGED, FILES))
        self.assertEqual(failed, [])
        self.assertEqual([(r["kind"], r.get("repo") or r.get("shoal")) for r in new], [("repo", REPO), ("shoal", "s1")])
        sh = new[1]
        self.assertEqual((sh["merged_lines"], sh["merged_prs"], sh["repos_uncounted"]), (576, 3, [OTHER]))
        self.assertEqual(sh["ratios"]["AAR"], 2.0)
        self.assertIn("not counted: " + OTHER, ef.line(sh))
        self.assertEqual(len(ef.load_rows(self.s["out"])), 2)
        # a re-run replaces the day's rows, nothing is appended twice
        self.run_snap(FakeGitHub(MERGED, FILES))
        self.assertEqual(len(ef.load_rows(self.s["out"])), 2)
        # GitHub down: the stored row with its merges is kept, and the failure is returned
        rows, new, failed = self.run_snap(FakeGitHub(fail_search=99))
        self.assertEqual(failed, [(REPO, DAY)])
        self.assertEqual([r["kind"] for r in new], ["shoal"])
        kept = next(r for r in rows if r["kind"] == "repo")
        self.assertEqual(kept["merged"]["lines"], 576)
        self.assertEqual(new[0]["merged_lines"], 576, "the shoal row uses the kept repository row")
        # with nothing stored, the day gets a row that says it could not count merges
        os.remove(self.s["out"])
        rows, new, failed = self.run_snap(FakeGitHub(fail_search=99))
        self.assertIsNone(new[0]["merged"])
        self.assertIsNone(new[1]["merged_lines"])

    def test_rollup_and_prometheus(self):
        days = [ef.shift(DAY, -1), DAY]
        rows, _, _ = self.run_snap(FakeGitHub(MERGED, FILES), days=days)
        up = ef.rollup(rows, days, rows)
        a = up["repos"][REPO]
        self.assertEqual((a["days"], a["github_days"], a["merged"]["prs"], a["merged"]["lines"]), (2, 2, 4, 577))
        self.assertEqual(a["ci"]["pr_heads"], 3)
        self.assertEqual(a["ratios"]["job_minutes_per_head"], 30.0)
        self.assertEqual(a["guard"]["reverted_prs"], 1)
        s = up["shoals"]["s1"]
        self.assertEqual((s["days"], s["low_coverage_days"]), (2, 1),
                         "the day before has no snapshot until 23:00: too little known time for ratios")
        # a day GitHub could not read adds cost but not to the ratios
        broken = [dict(r, merged=None, prs=[]) if r["kind"] == "repo" and r["day"] == DAY else r for r in rows]
        b = ef.rollup(broken, days, broken)["repos"][REPO]
        self.assertEqual(b["merged"]["lines"], 1)
        self.assertGreater(b["ci"]["job_minutes"], 0)
        prev = next(r for r in rows if r["kind"] == "repo" and r["day"] == days[0])
        self.assertEqual(b["ratios"]["lines_per_job_minute"], ef._ratio(1, prev["ci"]["job_minutes"]))
        text = ef.render_prometheus(rows, now=5)
        self.assertIn(f'shoal_efficiency_merged_lines{{repo="{REPO}",window="1d"}} 576', text)
        self.assertIn(f'shoal_efficiency_merged_lines{{repo="{REPO}",window="7d"}} 577', text)
        self.assertIn(f'shoal_efficiency_merged_prs_by_size{{repo="{REPO}",window="7d",bucket="50to500"}} 2', text)
        self.assertIn('shoal_efficiency_aar{shoal="s1",window="1d"} 2.0', text)
        self.assertIn('shoal_efficiency_role_time_coverage{shoal="s1",window="1d"} 1.0', text)
        self.assertNotIn("Add the widget", text, "no title, PR number or SHA reaches a label")
        self.assertNotIn("a1", text)
        self.assertIn("shoal_efficiency_last_success_timestamp_seconds 5.000", ef.render_prometheus([], now=5))

    def test_command_line(self):
        import argparse
        p = argparse.ArgumentParser()
        sub = p.add_subparsers(dest="cmd")
        ef.add_parser(sub)
        a = p.parse_args(["efficiency", "snapshot", "--spec", "s.json", "--day", DAY, "--backfill", "3"])
        self.assertEqual(ef._days(a), [ef.shift(DAY, -2), ef.shift(DAY, -1), DAY])
        a.day = "27/10/2026"
        with self.assertRaises(SystemExit):
            ef._days(a)


if __name__ == "__main__":
    unittest.main()
