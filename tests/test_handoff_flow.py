#!/usr/bin/env python3
"""Synthetic handoff sequencing and durable action-worker checks. No live sessions or ledgers."""
import contextlib
import datetime
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import types
import unittest
from unittest.mock import patch

PACKAGE = Path(__file__).resolve().parents[1] / "shoal"
sys.path[:0] = [str(PACKAGE), str(PACKAGE / "ui")]
import backends
import handoff_flow as flow
import park
import ui_actions
import ui_herdr
import ui_runs
import ui_status
from standalone import save_standalone, load_standalone


class HandoffTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.env = patch.dict(os.environ, XDG_CONFIG_HOME=str(self.root / "config"), XDG_RUNTIME_DIR=str(self.root / "run"))
        self.env.start()
        self.addCleanup(self.env.stop)
        self.doc = self.root / "plan.md"
        self.write_doc("before")
        self.entry = {"harness": "muse", "session": "old-session", "label": "example-seat", "cwd": str(self.root),
                      "doc": str(self.doc), "model": "old-model", "effort": "high", "opened": True}
        save_standalone([self.entry, dict(self.entry, label="other", session="other-session")])
        self.args = types.SimpleNamespace(session="example-seat", prepare=True, force=False, spec=None, only="",
                                          expected_session="old-session", wait_seconds=2, doc="", ecosystem="", model="", effort="", dry_run=False)
        self.output = contextlib.redirect_stdout(io.StringIO())
        self.output.__enter__()
        self.addCleanup(self.output.__exit__, None, None, None)

    def write_doc(self, state):
        stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.doc.write_text(f"# Example\nLast updated: {stamp}\nCurrent task: checkpoint.\n\n## Mission\nExample.\n"
                            f"## Current state\n{state}\n## Next steps\nContinue.\n## grants-needed\nNone.\n")

    def test_standalone_checkpoint_precedes_retirement_and_cross_provider_launch(self):
        events = []
        def send(*args, **kwargs):
            events.append("checkpoint")
            self.write_doc("restart-ready")
        target = types.SimpleNamespace(create=lambda *a, **k: events.append(("create", k)) or "new-session")
        source = types.SimpleNamespace(send=send)
        with patch.object(flow, "between", return_value=True), patch.object(park, "stop_session", side_effect=lambda *a: events.append("stop") or "stopped"), \
             patch.object(backends, "get", side_effect=lambda eco: source if eco == "muse" else target):
            self.args.ecosystem = "codex"
            self.assertEqual(flow.standalone_handoff(self.args, self.entry, str(self.doc), "codex", time.monotonic() + 2), 0)
        self.assertEqual(events[:2], ["checkpoint", "stop"])
        self.assertEqual(events[2][0], "create")
        self.assertEqual(events[2][1]["model"], "", "another provider must not inherit the old model")
        rows = load_standalone()
        self.assertEqual(rows[0]["session"], "new-session")
        self.assertEqual(rows[0]["doc"], str(self.doc))
        self.assertTrue(rows[0]["opened"])
        self.assertEqual(rows[1]["session"], "other-session", "the other saved seat is retained")

    def test_unchanged_doc_times_out_without_stopping_or_launching(self):
        source = types.SimpleNamespace(send=lambda *a, **k: None)
        with patch.object(flow, "between", return_value=True), patch.object(flow, "POLL_S", 0.001), \
             patch.object(backends, "get", return_value=source), patch.object(park, "stop_session") as stop:
            with self.assertRaisesRegex(flow.HandoffError, "timed out"):
                flow.standalone_handoff(self.args, self.entry, str(self.doc), "muse", time.monotonic() + 0.02)
            stop.assert_not_called()
        self.assertEqual(load_standalone()[0]["session"], "old-session")

    def test_changed_seat_is_refused_before_delivery(self):
        save_standalone([dict(self.entry, session="someone-else")])
        with patch.object(backends, "get") as backend, self.assertRaisesRegex(flow.HandoffError, "changed sessions"):
            flow.standalone_handoff(self.args, self.entry, str(self.doc), "muse", time.monotonic() + 1)
        backend.assert_not_called()

    def test_preview_is_read_only_and_binds_the_session(self):
        before = (self.root / "config/shoal/ui-standalone.json").read_bytes()
        self.args.dry_run = True
        with patch.object(flow, "preflight"), patch.object(backends, "get") as backend:
            self.assertEqual(flow.run(self.args), 0)
            backend.assert_not_called()
        self.assertEqual((self.root / "config/shoal/ui-standalone.json").read_bytes(), before)
        self.args.expected_session = "stale-preview"
        with self.assertRaisesRegex(flow.HandoffError, "changed sessions"):
            flow.run(self.args)

    def test_dialog_role_plan_prepares_by_default_and_binds_immediate_handoffs_too(self):
        role = types.SimpleNamespace(slug="core", session="old-session")
        spec = types.SimpleNamespace(sid="example", roles=[role], role=lambda _: role)
        args = types.SimpleNamespace(shoal="example", role="core", session="", model="", effort="", ecosystem="", force=False)
        with patch.object(ui_actions, "_spec", return_value=spec), patch("roles.find_spec", return_value="/example/spec.json"):
            argv, _, _ = ui_actions.build("handoff", args)
            self.assertIn("--prepare", argv)
            self.assertIn("--expected-session=old-session", argv)
            args.no_prepare, args.force = True, True
            argv, _, _ = ui_actions.build("handoff", args)
            self.assertNotIn("--prepare", argv)
            self.assertIn("--force", argv)
            self.assertIn("--expected-session=old-session", argv)

    def test_dialog_standalone_plan_binds_the_saved_seat_and_start_doc(self):
        args = types.SimpleNamespace(session="example-seat", doc=str(self.doc), model="", effort="", ecosystem="", force=False)
        argv, _, _ = ui_actions.build("handoff", args)
        self.assertIn("--prepare", argv)
        self.assertIn("--session=example-seat", argv)
        self.assertIn("--expected-session=old-session", argv)
        self.assertIn("--doc=" + str(self.doc), argv)

    def test_failure_to_retire_prevents_replacement(self):
        source = types.SimpleNamespace(send=lambda *a, **k: self.write_doc("restart-ready"), create=unittest.mock.Mock())
        with patch.object(flow, "between", return_value=True), patch.object(backends, "get", return_value=source), \
             patch.object(park, "stop_session", return_value="STILL LIVE: old"), self.assertRaisesRegex(flow.HandoffError, "did not stop"):
            flow.standalone_handoff(self.args, self.entry, str(self.doc), "muse", time.monotonic() + 1)
        source.create.assert_not_called()

    def test_role_ready_requires_a_new_queue_and_finished_turn(self):
        role = types.SimpleNamespace(ecosystem="muse")
        spec = types.SimpleNamespace(sid="example", ledger_dir="", dir=str(self.root), role=lambda _: role)
        view = types.SimpleNamespace(current_queue=lambda _: {"id": "q1", "restart_ready": True},
                                     role_state=lambda _: {"state": "waiting"})
        with patch("ledger.Ledger"), patch("ledger.View", return_value=view), patch.object(park, "session_held", return_value=False), \
             patch("health.session_between_turns", return_value=True):
            self.assertTrue(flow.role_ready(spec, "core"))
            self.assertFalse(flow.role_ready(spec, "core", "q1"), "an old ready marker cannot acknowledge a new request")
            self.assertTrue(flow.role_ready(spec, "core", "q0"))
        with patch("ledger.Ledger"), patch("ledger.View", return_value=view), patch.object(park, "session_held", return_value=True):
            self.assertFalse(flow.role_ready(spec, "core"))

    def _role_workflow(self, already_ready):
        role = types.SimpleNamespace(slug="core", session="old-session", ecosystem="muse", doc=str(self.doc))
        spec = types.SimpleNamespace(sid="example", ledger_dir="", dir=str(self.root), role=lambda _: role)
        view = types.SimpleNamespace(current_queue=lambda _: {"id": "old-queue"})
        events, ready = [], [already_ready]
        def request(argv):
            self.assertIn("--wake", argv)
            self.assertIn("queue --restart-ready", argv[-1])
            events.append("checkpoint-request")
            self.write_doc("ready for a replacement")
            ready[0] = True
            return 0
        def handoff(*args):
            self.assertFalse(args[5], "the workflow never forces the lifecycle gate")
            events.append("handoff")
            role.session = "new-session"
            return 0
        self.args.session, self.args.spec, self.args.only = "", "/example/spec.json", "core"
        with patch("roles.ShoalSpec.load", return_value=spec), patch("roles.is_local", return_value=True), \
             patch("roles.state_dir", return_value=str(self.root)), patch.object(flow, "preflight"), \
             patch.object(flow, "role_ready", side_effect=lambda *a: ready[0]), patch("ledger.Ledger"), \
             patch("ledger.View", return_value=view), patch.object(subprocess, "call", side_effect=request), \
             patch.object(park, "handoff", side_effect=handoff):
            self.assertEqual(flow.run(self.args), 0)
        self.assertEqual(events, ["handoff"] if already_ready else ["checkpoint-request", "handoff"])

    def test_role_workflow_requests_checkpoint_before_using_the_unforced_gate(self):
        self._role_workflow(False)

    def test_ready_role_is_handed_off_without_an_extra_checkpoint_turn(self):
        self._role_workflow(True)

    def test_detached_worker_consumes_token_and_progress_can_be_reopened(self):
        script = self.root / "worker.py"
        script.write_text("import sys,time\n"
                          f"sys.path[:0] = {[str(PACKAGE), str(PACKAGE / 'ui')]!r}\n"
                          "import ui_runs,ui_actions,ui_herdr\n"
                          "def stream(args,log,**kw):\n"
                          " log.write('checkpoint requested\\n'); log.flush(); time.sleep(.15)\n"
                          " log.write('handoff complete\\n'); log.flush(); return 0,'handoff complete'\n"
                          "ui_actions._stream=stream\nui_herdr.log_action=lambda *a:None\n"
                          "raise SystemExit(ui_runs.worker(sys.argv[1]))\n")
        token = ui_actions._store({"verb": "handoff", "session": "example-seat", "argv": ["handoff", "--session=example-seat", "--prepare"]})
        children = []
        def spawn(argv, **kw):
            child = subprocess.Popen([sys.executable, str(script), argv[-1]], start_new_session=True, **kw)
            children.append(child)
            def cleanup():
                if child.poll() is None:
                    child.terminate()
                child.wait(timeout=5)
            self.addCleanup(cleanup)
            return child
        with patch.object(backends, "spawn_detached", side_effect=spawn):
            started = ui_runs.start(token)
        with self.assertRaisesRegex(ui_actions.ActError, "no such plan"):
            ui_actions.take_plan(token)
        query = types.SimpleNamespace(run="", session="example-seat", shoal="", role="")
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            status = ui_runs.status(query)
            if not status["running"]:
                break
            time.sleep(0.02)
        self.assertFalse(status["running"])
        for child in children:
            child.wait(timeout=5)
        self.assertEqual(status["rc"], 0)
        self.assertEqual(status["run"], started["run"])
        self.assertIn("checkpoint requested", status["output"])
        self.assertIn("handoff complete", status["output"])
        self.assertEqual(os.stat(ui_runs.path(started["run"])).st_mode & 0o777, 0o600)

    def test_dead_worker_is_reported_as_failure(self):
        run = "a" * 32
        ui_runs.save(run, {"running": True, "pid": 999999999, "created": time.time() - 60})
        state = ui_runs.public(run, ui_runs.read(run))
        self.assertFalse(state["running"])
        self.assertEqual(state["rc"], 1)
        self.assertIn("before reporting completion", state["output"])


if __name__ == "__main__":
    unittest.main()
