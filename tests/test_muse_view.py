"""Role observation must neither hold a session nor hide progress or trap the terminal."""
import contextlib
import fcntl
import io
import json
import os
from pathlib import Path
import pty
import select
import subprocess
import sys
import tempfile
import termios
import time
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "shoal"), str(ROOT / "shoal/ui")]
import muse
import ui_view
import ui_status

SID = "01a10000-0000-7000-8000-000000000001"


class MuseViewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = patch.dict(os.environ, {
            "SHOAL_MUSE_STATE": self.tmp.name + "/state",
            "SHOAL_MUSE_DATA": self.tmp.name + "/data",
            "SHOAL_FAKE_SESSIONS": "{}",
        })
        self.env.start()
        self.addCleanup(self.env.stop)

    def native(self):
        d = Path(os.environ["SHOAL_MUSE_DATA"]) / "2026/01/02" / SID
        d.mkdir(parents=True)
        return d

    def test_exit_that_cancels_a_turn_is_seen_and_c_continues_only_after_one(self):
        d = self.native()
        t0 = time.time()
        us = lambda dt: int((t0 + dt) * 1e6)  # noqa: E731
        rows = [
            {"recorded_at": us(-60), "payload_type": "command.invoked", "payload": {"record": {"command": "/exit"}}},
            {"recorded_at": us(1), "payload_type": "runtime.user_intent.accepted", "payload": {}},
            {"recorded_at": us(8), "payload_type": "command.invoked", "payload": {"record": {"command": "/exit"}}},
            {"recorded_at": us(8), "payload_type": "runtime.session",
             "payload": {"event": {"kind": "terminal", "terminal": "cancelled"}, "kind": "run"}},
        ]
        (d / "session.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")
        self.assertTrue(muse.exit_cancelled_turn(SID, t0))
        self.assertFalse(muse.exit_cancelled_turn(SID, t0 + 10), "nothing after the window")
        # an /exit between turns ends nothing: no cancelled terminal follows it
        (d / "session.jsonl").write_text(json.dumps(rows[0]) + "\n" + json.dumps(rows[2]) + "\n")
        self.assertFalse(muse.exit_cancelled_turn(SID, t0))
        started = []
        with patch.object(muse, "start_run", side_effect=lambda *a, **k: started.append(a)):
            self.assertIn("nothing to continue", muse.continue_cancelled(SID))
            Path(muse.sdir(SID, create=True), "cancelled-by-exit").write_text(f"{t0}\n")
            self.assertIn("continuing", muse.continue_cancelled(SID, repo_dir=self.tmp.name))
            self.assertIn("nothing to continue", muse.continue_cancelled(SID), "the marker is spent")
        self.assertEqual(len(started), 1)
        self.assertEqual(started[0][2], muse.CONTINUE_PROMPT)

    def test_role_observer_in_idle_and_busy_states_preserves_standalone(self):
        for busy in (False, True):
            with patch.object(muse, "busy", return_value=busy):
                argv, _ = ui_view.viewer("muse", SID, model="test-model", effort="xhigh", observe=True)
                self.assertIn("muse-tail", argv)
                self.assertNotIn("resume", argv)
                self.assertIn("--allow-interactive", argv)
                self.assertIn("test-model", argv)
                self.assertIn("xhigh", argv)
        with patch.object(muse, "busy", return_value=False):
            self.assertIn("resume", ui_view.viewer("muse", SID)[0])

    def test_cmd_view_roles_take_observer_path_including_dry_run(self):
        role = types.SimpleNamespace(ecosystem="muse", session=SID, model="m", effort="high", slug="builder", host="")
        spec = types.SimpleNamespace(sid="demo", home="", host="", dir=self.tmp.name)
        args = types.SimpleNamespace(session="", harness="", target_shoal="demo", role="builder",
                                     stub=False, dry_run=False, spec="")
        with patch.object(ui_view, "resolve_role", return_value=(spec, role)), \
                patch.object(ui_view, "run_viewer", return_value=0) as run, \
                patch("roles.is_local", return_value=True):
            self.assertEqual(ui_view.cmd_view(args), 0)
            self.assertTrue(run.call_args.kwargs["observe"])
            args.dry_run = True
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(ui_view.cmd_view(args), 0)
            self.assertIn("muse-tail", json.loads(out.getvalue())["argv"])

    def test_view_environment_preserves_muse_removals(self):
        with patch.dict(os.environ, {"META_API_KEY": "synthetic-key", "HERDR_PANE_ID": "fixture",
                                     "CLAUDE_CODE_SESSION_ID": "fixture", "CLAUDECODE": "1",
                                     "SHOAL_MUSE_ALLOW_API_KEY": ""}), patch.object(muse, "busy", return_value=False):
            for observe in (False, True):
                _, env = ui_view.viewer("muse", SID, observe=observe)
                for key in ("META_API_KEY", "HERDR_PANE_ID", "CLAUDE_CODE_SESSION_ID", "CLAUDECODE"):
                    self.assertNotIn(key, env)
                self.assertEqual(env["MUSE_NO_AUTO_UPDATE"], "1")
        self.assertNotIn("HERDR_PANE_ID", ui_view.clean_env({"HERDR_PANE_ID": "fixture"}))

    def test_modern_journal_freshness_and_legacy_fallback(self):
        d = self.native()
        journal = d / "session.jsonl"
        journal.write_text("{}\n")
        self.assertLess(muse.activity_age(SID), 3)

        ago = time.time() - 3600
        os.utime(journal, (ago, ago))
        self.assertGreater(muse.activity_age(SID), 3500)
        journal.unlink()
        self.assertIsNone(muse.activity_age(SID))
        old = Path(os.environ["SHOAL_MUSE_DATA"]) / ".msp-view-v1" / SID
        old.mkdir(parents=True)
        (old / "journal-000.bin").write_text("old")
        self.assertLess(muse.activity_age(SID), 3)

    def test_idle_native_viewer_is_not_role_work(self):
        role = types.SimpleNamespace(ecosystem="muse", session=SID, host="")
        backend = types.SimpleNamespace(status=lambda *a: "status:busy attached quiet")
        with patch.object(ui_status, "is_remote", return_value=False), patch("backends.get", return_value=backend):
            self.assertEqual(ui_status.session_state(role, [], False), "idle")
            backend.status = lambda *a: "status:busy attached"
            self.assertEqual(ui_status.session_state(role, [], False), "unknown")
            backend.status = lambda *a: "status:busy attached active"
            self.assertEqual(ui_status.session_state(role, [], False), "working")

    def test_partial_records_are_retried_and_private_records_not_rendered(self):
        path = Path(muse.sdir(SID, create=True)) / "run.jsonl"
        def rec(**kw):
            return json.dumps({"payload": kw}) + "\n"
        data = rec(kind="run_started", prompt="PRIVATE_PROMPT")
        data += rec(kind="task_lifecycle", event={"operation": "tool:read_file"})
        data += rec(kind="tool_result", text="PRIVATE_TOOL_RESULT")
        data += rec(kind="reasoning", text="PRIVATE_REASONING")
        delta = rec(kind="run_output_delta", text="Visible progress\n")
        path.write_text(data + delta[:17])
        calls = []
        def key(timeout):
            calls.append(timeout)
            if len(calls) == 1:
                with path.open("a") as fh:
                    fh.write(delta[17:])
                    fh.write(rec(kind="run_terminal", terminal="completed", text="Visible progress\n"))
                return ""
            return "q"
        out = io.StringIO()
        with patch.object(ui_view, "_getkey", side_effect=key), contextlib.redirect_stdout(out):
            self.assertEqual(muse.cmd_muse_tail(types.SimpleNamespace(session=SID, once=False)), 0)
        text = out.getvalue()
        self.assertEqual(text.count("Visible progress"), 1)
        self.assertIn("tool:read_file", text)
        self.assertNotIn("PRIVATE", text)
        self.assertIn("completed", text)
        self.assertFalse(muse.busy(SID), "observing must not retain a lock")

    def test_explicit_native_access_serializes_with_watcher_and_releases(self):
        def native(argv, env):
            self.assertIn("resume", argv)
            self.assertIn("my-model", argv)
            self.assertIn("xhigh", argv)
            with self.assertRaises(muse.BusyError):
                muse.start_run(SID, self.tmp.name, "queued wake")
            return types.SimpleNamespace(returncode=0)
        with patch.object(muse, "attached_client", return_value=0), \
                patch.object(muse.subprocess, "run", side_effect=native):
            self.assertEqual(muse.interactive_view(SID, "my-model", "xhigh"), 0)
        self.assertFalse(muse.busy(SID))
        with open(Path(muse.sdir(SID)) / "lock", "a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(muse.BusyError), patch.object(muse.subprocess, "run") as run:
                muse.interactive_view(SID)
            run.assert_not_called()

    def test_native_name_resume_detected_but_stale_lock_pid_is_not(self):
        d = self.native()
        script = Path(self.tmp.name) / "muse-owner"
        script.write_text("import fcntl,os,sys,time\np=open(sys.argv[1],'w')\np.write(f'pid={os.getpid()}\\nhost=test\\n');p.flush()\nfcntl.flock(p,fcntl.LOCK_EX)\nprint('ready',flush=True)\ntime.sleep(30)\n")
        for mode, expected in ((["resume", "friendly-name"], True),
                               (["exec", "--session-id", SID], False),
                               (["resume", SID, "--no-session-log"], False)):
            proc = subprocess.Popen([sys.executable, str(script), str(d / ".session.lock"), *mode],
                                    stdout=subprocess.PIPE, text=True)
            try:
                self.assertTrue(select.select([proc.stdout], [], [], 5)[0])
                self.assertEqual(proc.stdout.readline().strip(), "ready")
                self.assertEqual(muse.attached_client(SID), proc.pid if expected else 0)
            finally:
                proc.terminate()
                proc.wait(timeout=5)
                proc.stdout.close()
            self.assertEqual(muse.attached_client(SID), 0)

    def test_tty_detach_restores_terminal_and_observation_does_not_block_turn(self):
        # A PTY exercises the actual key reader. The held writer lock stays independent of the observer.
        for key in (b"q", b"\x1b", b"\x03"):
            with self.subTest(key=key):
                d = Path(muse.sdir(SID, create=True))
                master, slave = pty.openpty()
                attrs = termios.tcgetattr(slave)
                def controlling_tty():
                    os.setsid()
                    fcntl.ioctl(0, termios.TIOCSCTTY, 0)
                proc = subprocess.Popen([sys.executable, str(ROOT / "shoal/cli.py"), "muse-tail", "--session", SID],
                                        stdin=slave, stdout=slave, stderr=slave, close_fds=True,
                                        preexec_fn=controlling_tty)
                try:
                    data = b""
                    until = time.monotonic() + 5
                    while b"[idle]" not in data and time.monotonic() < until:
                        if select.select([master], [], [], .1)[0]:
                            data += os.read(master, 65536)
                    self.assertIn(b"[idle]", data)
                    with open(d / "lock", "a+") as lock:
                        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    os.write(master, key)
                    self.assertEqual(proc.wait(timeout=5), 0)
                    self.assertEqual(termios.tcgetattr(slave), attrs)
                finally:
                    if proc.poll() is None:
                        proc.terminate()
                        proc.wait(timeout=5)
                    os.close(master)
                    os.close(slave)


if __name__ == "__main__":
    unittest.main()
