#!/usr/bin/env python3
"""`offline` from the actions dialog: a detached apply that a status call follows (Herdr's exec ends a call at 30 s),
and `offline off` itself, its wakes and GitHub reads a few at a time with the record still first. Synthetic: no live
ledger, session or GitHub."""
import contextlib
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest.mock import patch

PACKAGE = Path(__file__).resolve().parents[1] / "shoal"
sys.path[:0] = [str(PACKAGE), str(PACKAGE / "ui")]
import backends  # noqa: E402
import health  # noqa: E402
import ui_actions  # noqa: E402
import ui_runs  # noqa: E402
import worker  # noqa: E402


class DetachedOfflineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        env = patch.dict(os.environ, XDG_CONFIG_HOME=str(self.root / "config"), XDG_RUNTIME_DIR=str(self.root / "run"))
        env.start()
        self.addCleanup(env.stop)

    def spawn_with(self, body: str):
        """spawn_detached as a worker whose shoal verb is `body` (a stub of ui_actions._stream)."""
        script = self.root / "worker.py"
        script.write_text("import sys,time\n"
                          f"sys.path[:0] = {[str(PACKAGE), str(PACKAGE / 'ui')]!r}\n"
                          "import ui_runs,ui_actions,ui_herdr\n"
                          "def stream(args,log,timeout=120):\n" + body +
                          "ui_actions._stream=stream\nui_herdr.log_action=lambda *a:None\n"
                          "raise SystemExit(ui_runs.worker(sys.argv[1]))\n")

        def spawn(argv, **kw):
            child = subprocess.Popen([sys.executable, str(script), argv[-1]], start_new_session=True, **kw)

            def cleanup():
                if child.poll() is None:
                    child.terminate()
                child.wait(timeout=5)
            self.addCleanup(cleanup)
            return child
        return spawn

    def wait_done(self, query, seconds=8):
        deadline = time.monotonic() + seconds
        seen_running = False
        while time.monotonic() < deadline:
            st = ui_runs.status(query)
            seen_running |= st["running"]
            if not st["running"]:
                return st, seen_running
            time.sleep(0.02)
        self.fail(f"the run did not end: {st}")

    def test_scoped_offline_runs_detached_and_reopens_by_scope_with_its_whole_result(self):
        # each shoal's `offline off` writes as it goes; a run longer than a dialog's call is followed, not waited on
        body = (" sid = args[args.index('--shoal') + 1]\n"
                " log.write(f'waking r1, r2 ({sid})\\n'); log.flush(); time.sleep(.3)\n"
                " out = f'user-offline off [ev-{sid}]; woke [r1, r2]\\n' + ''.join(f'  ASK r1 [{i}]: ask {i}\\n' for i in range(1, 400))\n"
                " log.write(out); log.flush(); return 0, out\n")
        argvs = [["offline", "off", "--shoal", "alpha-20261101-000000"], ["offline", "off", "--shoal", "beta-20261101-000000"]]
        token = ui_actions._store({"verb": "offline", "shoal": "", "scope": "all", "role": "", "argvs": argvs})
        with patch.object(backends, "spawn_detached", side_effect=self.spawn_with(body)):
            started = ui_runs.start(token)
        self.assertTrue(started["run"] and started["verb"] == "offline" and started["scope"] == "all", started)
        with self.assertRaisesRegex(ui_actions.ActError, "no such plan"):
            ui_actions.take_plan(token)  # one use: the worker took it
        by_scope = types.SimpleNamespace(run="", verb="offline", scope="all", shoal="", role="", session="")
        st, seen_running = self.wait_done(by_scope)
        self.assertTrue(seen_running, "the first status calls see it running (the dialog follows it)")
        self.assertEqual((st["run"], st["rc"], st["verb"]), (started["run"], 0, "offline"))
        self.assertGreater(st["finished"], 0)
        # the whole result, not the running tail: every shoal named, every ask listed
        self.assertIn("== alpha-20261101-000000", st["output"])
        self.assertIn("== beta-20261101-000000", st["output"])
        self.assertEqual(st["output"].count("ASK r1 [1]: ask 1\n"), 2)
        self.assertIn("ASK r1 [399]: ask 399", st["output"])
        self.assertGreater(len(st["output"].encode()), ui_runs.RUNNING_TAIL)
        saved = ui_runs.read(st["run"])
        self.assertIn("== alpha-20261101-000000\nuser-offline off [ev-alpha-20261101-000000]", saved["output"])
        # a status for one shoal's offline, or for a handoff, does not pick up the scoped run
        one = types.SimpleNamespace(run="", verb="offline", scope="", shoal="alpha-20261101-000000", role="", session="")
        self.assertIsNone(ui_runs.status(one)["run"])
        ho = types.SimpleNamespace(run="", verb="", scope="", shoal="alpha-20261101-000000", role="r1", session="")
        self.assertIsNone(ui_runs.status(ho)["run"])

    def test_one_shoal_freeze_reopens_by_shoal_and_a_failure_keeps_its_exit(self):
        body = " log.write('freeze on\\n'); log.flush(); return 3, 'freeze on\\nERROR: push failed'\n"
        argv = ["freeze", "on", "--shoal", "alpha-20261101-000000"]
        token = ui_actions._store({"verb": "freeze", "shoal": "alpha-20261101-000000", "role": "", "argv": argv})
        with patch.object(backends, "spawn_detached", side_effect=self.spawn_with(body)):
            started = ui_runs.start(token)
        q = types.SimpleNamespace(run="", verb="freeze", scope="", shoal="alpha-20261101-000000", role="", session="")
        st, _ = self.wait_done(q)
        self.assertEqual((st["run"], st["rc"], st["running"]), (started["run"], 3, False))
        wrong = types.SimpleNamespace(run="", verb="offline", scope="", shoal="alpha-20261101-000000", role="", session="")
        self.assertIsNone(ui_runs.status(wrong)["run"], "a freeze run is not an offline run")

    def test_background_apply_is_refused_for_a_quick_verb(self):
        token = ui_actions._store({"verb": "msg", "shoal": "a", "role": "r", "argv": ["msg", "--shoal", "a"]})
        with self.assertRaisesRegex(ui_actions.ActError, "background apply is for handoff, offline, freeze plans"):
            ui_runs.start(token)

    def test_a_dead_worker_names_its_verb(self):
        run = "b" * 32
        ui_runs.save(run, {"running": True, "pid": 999999999, "created": time.time() - 60,
                           "plan": {"verb": "offline", "scope": "all"}})
        st = ui_runs.public(run, ui_runs.read(run))
        self.assertEqual((st["running"], st["rc"], st["verb"]), (False, 1, "offline"))
        self.assertIn("Offline worker ended before reporting completion", st["output"])


class FakeView:
    def __init__(self, asks, offline=True):
        self.asks, self.offline, self.done = asks, offline, {}

    def user_offline(self):
        return {"ts": "2026-11-20T00:00:00Z"} if self.offline else None

    def current_queue(self, slug):
        return {"asks": self.asks.get(slug, [])}

    def role_state(self, slug):
        return {}


class OfflineOffTests(unittest.TestCase):
    """`offline off` on a synthetic shoal: the record first, the wakes a few at a time, the listing bounded."""

    def run_off(self, asks, wake, states, budget=5.0, dry_run=False):
        events, lock = [], threading.Lock()
        spec = types.SimpleNamespace(sid="alpha-20261101-000000", ledger_dir="", effective_repos=["example/app"],
                                     roles=[types.SimpleNamespace(slug=s) for s in ("r1", "r2", "r3", "r4", "idle")])
        led = types.SimpleNamespace(append=lambda *a, **k: events.append(("record", k.get("state"))) or {"id": "ev1"})

        def ref_state(ref, live=True):
            if ref in states and live:
                delay, st = states[ref]
                time.sleep(delay)
                with lock:
                    events.append(("read", ref))
                    cache[ref] = st
            return cache.get(ref, "UNKNOWN")
        cache = {}

        def request_wake(spec_, slug, reason, condition="", dry_run=False, banner=None):
            with lock:
                events.append(("wake", slug))
            return wake(slug)
        a = types.SimpleNamespace(sid=spec.sid, spec="", state="off", until="", note="", force=False, dry_run=dry_run,
                                  no_sync=True, ledger_dir="")
        out = io.StringIO()
        with patch.object(worker, "_load", return_value=(spec, led, FakeView(asks))), \
             patch.object(worker, "View", return_value=FakeView({}, offline=False)), \
             patch.object(worker, "known_repos", return_value={}), patch.object(worker, "ref_state", side_effect=ref_state), \
             patch.object(worker, "HINT_BUDGET_S", budget), patch.object(health, "request_wake", side_effect=request_wake), \
             contextlib.redirect_stdout(out):
            t = time.monotonic()
            rc = worker._offline_one(a)
            took = time.monotonic() - t
        return rc, out.getvalue(), events, took

    def test_record_before_any_wake_wakes_at_once_and_hints_from_concurrent_reads(self):
        asks = {"r1": ["merge #1", "ruling on #2"], "r2": ["merge #3"], "r3": ["design call"], "r4": ["merge #1 again"]}
        states = {f"example/app#{n}": (0.3, st) for n, st in ((1, "MERGED"), (2, "OPEN"), (3, "CLOSED"))}

        def wake(slug):
            time.sleep(0.4)
            if slug == "r3":
                raise RuntimeError("adapter broke")
            return "wake-file"
        rc, out, events, took = self.run_off(asks, wake, states)
        self.assertEqual(rc, 0, out)
        kinds = [k for k, _ in events]
        self.assertEqual(kinds.count("record"), 1)
        self.assertLess(kinds.index("record"), kinds.index("wake"), "the ledger record comes before any wake")
        self.assertEqual(sorted(s for k, s in events if k == "wake"), ["r1", "r2", "r3", "r4"], "only roles holding asks")
        self.assertEqual(sorted(r for k, r in events if k == "read"), ["example/app#1", "example/app#2", "example/app#3"],
                         "each referenced item read once")
        # four wakes of 0.4 s and three reads of 0.3 s, one after another, would take 2.5 s
        self.assertLess(took, 1.6, f"wakes and reads ran one at a time ({took:.2f}s)")
        self.assertIn("  woke r1: wake-file", out)
        self.assertIn("  woke r3: FAILED (adapter broke)", out)
        self.assertIn("user-offline off [ev1]; woke ['r1', 'r2', 'r3', 'r4']", out)
        self.assertIn("  ASK r1 [1]: merge #1 (LIKELY STALE: app#1 merged)\n", out)
        self.assertIn("  ASK r1 [2]: ruling on #2\n", out)
        self.assertIn("  ASK r2 [1]: merge #3 (LIKELY STALE: app#3 closed)\n", out)
        self.assertIn("5 recorded ask(s), 3 likely stale. Each woken role", out)

    def test_a_slow_github_read_is_left_out_of_the_listing_not_waited_on(self):
        asks = {"r1": ["merge #1"], "r2": ["merge #9"]}
        states = {"example/app#1": (0.05, "MERGED"), "example/app#9": (2.0, "MERGED")}
        rc, out, _events, took = self.run_off(asks, lambda s: "wake-file", states, budget=0.4)
        self.assertEqual(rc, 0, out)
        self.assertLess(took, 1.5, f"the listing waited for a slow read ({took:.2f}s)")
        self.assertIn("  ASK r1 [1]: merge #1 (LIKELY STALE: app#1 merged)\n", out)
        self.assertIn("  ASK r2 [1]: merge #9 (not checked: GitHub was slow)\n", out)
        self.assertIn("2 recorded ask(s), 1 likely stale, 1 not checked against GitHub within 0.4 s.", out)

    def test_dry_run_records_nothing_and_wakes_nobody(self):
        rc, out, events, _ = self.run_off({"r1": ["merge #1"]}, lambda s: "wake-file", {"example/app#1": (0, "MERGED")},
                                          dry_run=True)
        self.assertEqual(rc, 0)
        self.assertEqual(events, [])
        self.assertIn("[dry-run] user-offline off; would wake ['r1']; would list 1 recorded ask(s)", out)


if __name__ == "__main__":
    unittest.main()
