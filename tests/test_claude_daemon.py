"""The durable Claude daemon service (shoal/claude_daemon.py): 2026-11-25, two Claude daemons started by clients in Herdr
terminals carried the terminals' HC_SCOPE, and closing them ended every session on their accounts. No real Claude and no
user systemd here: a stub `claude`, stand-in processes for daemons, a scratch account directory."""
import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

PACKAGE = Path(__file__).resolve().parents[1] / "shoal"
sys.path[:0] = [str(PACKAGE), str(PACKAGE / "ui")]
import accounts  # noqa: E402
import backends  # noqa: E402
import claude_daemon as cd  # noqa: E402
import config  # noqa: E402

UNIT = "shoal-claude-daemon@claude.service"
DONE = subprocess.CompletedProcess([], 0, "", "")


class Case(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="claude-daemon-"))
        self.home = self.tmp / "account"
        (self.home / "daemon").mkdir(parents=True)
        self.units = self.tmp / "units"
        self.procs = []
        self.ctl = []
        self.slept = []
        for p in (patch.object(accounts, "home", lambda provider, account="", e=None: str(self.home)),
                  patch.object(accounts, "homes", lambda provider: [("", str(self.home))]),
                  patch.object(backends, "fake_sessions", lambda: None),
                  patch.object(cd, "_systemctl", side_effect=lambda *a: (self.ctl.append(a), DONE)[1]),
                  patch.object(cd, "_sleep", self.slept.append),
                  patch.dict(os.environ, {"SHOAL_SYSTEMD_USER_DIR": str(self.units), "HC_SCOPE": "1-caller",
                                          "INVOCATION_ID": "abc"})):
            p.start()
            self.addCleanup(p.stop)
        usr1 = signal.getsignal(signal.SIGUSR1)
        self.addCleanup(lambda: signal.signal(signal.SIGUSR1, usr1))

    def tearDown(self):
        for p in self.procs:
            p.kill()
            p.wait()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def process(self, scope=""):
        env = {k: v for k, v in os.environ.items() if k != "HC_SCOPE"}
        p = subprocess.Popen(["sleep", "60"], env={**env, **({"HC_SCOPE": scope} if scope else {})})
        self.procs.append(p)
        return p

    def lock(self, pid, origin="transient", start=None):
        (self.home / "daemon.lock").write_text(json.dumps({
            "pid": pid, "origin": origin, "version": "9.9.9", "procStart": cd._start_time(pid) if start is None else start}))

    def install(self):
        self.units.mkdir(exist_ok=True)
        (self.units / cd.TEMPLATE).write_text(cd.MARK + "\n")

    def in_service(self):
        p = patch.object(cd, "cgroup", lambda pid: f"/user.slice/shoal.slice/shoal-claude.slice/{UNIT}")
        p.start()
        self.addCleanup(p.stop)


class WhatRuns(Case):
    def test_no_lock_a_dead_holder_and_a_recycled_pid_are_no_daemon(self):
        self.assertIsNone(cd.holder())
        p = self.process()
        self.lock(p.pid, start="1")
        self.assertIsNone(cd.holder())
        self.lock(p.pid)
        self.assertEqual((cd.holder()["pid"], cd.holder()["origin"], cd.holder()["service"]), (p.pid, "transient", False))
        p.kill()
        p.wait()
        self.assertIsNone(cd.holder())  # a killed daemon leaves its lock behind

    def test_report(self):
        self.assertEqual(cd.report()[0], "info")
        self.install()
        self.assertIn("is installed", cd.report()[1])
        self.lock(self.process().pid)
        with patch.object(cd.os.path, "isdir", lambda p: True):
            self.assertEqual(cd.report()[0], "warn")
            self.assertIn("claude-daemon takeover", cd.report()[1])
        self.in_service()
        self.assertEqual(cd.report()[0], "ok")

    def test_a_daemon_or_a_session_that_carries_a_terminals_scope_is_reported(self):
        clean, tagged = self.process(), self.process("2-card")
        self.lock(clean.pid)
        self.assertEqual((cd.exposure(), cd.conditions([""])), ("", []))
        self.lock(tagged.pid)
        self.assertIn("its daemon carry", cd.exposure())
        self.lock(clean.pid)
        (self.home / "daemon" / "roster.json").write_text(json.dumps({"workers": {
            "aaaa1111": {"pid": clean.pid, "replPid": tagged.pid}, "bbbb2222": {"pid": clean.pid}}}))
        c = cd.conditions(["", ""])
        self.assertEqual([x["key"] for x in c], [f"claude:{clean.pid}"])
        self.assertIn("aaaa1111", c[0]["detail"])
        self.assertNotIn("bbbb2222", c[0]["detail"])

    def test_health_pages_about_the_accounts_its_local_roles_use(self):
        import health
        from types import SimpleNamespace as NS
        self.lock(self.process("2-card").pid)
        role = NS(ecosystem="claude", session="s", account="")
        with patch.object(health, "is_local", lambda r, s: True):
            got = health.claude_daemon_checks(NS(roles=[role]))
            self.assertEqual([c["condition"] for c in got], ["claude-daemon"])
            self.assertEqual(health.claude_daemon_checks(NS(roles=[NS(**{**vars(role), "ecosystem": "codex"})])), [])
        self.assertIn("claude-daemon", health.HOLD_EXEMPT)
        self.assertEqual(health.LADDERS["claude-daemon"], [(10, "page")])


class NoScopeReachesClaude(Case):
    def test_tool_calls_and_viewers_drop_the_scope(self):
        import ui_view
        self.assertNotIn("HC_SCOPE", config.tool_env())
        self.assertNotIn("HC_SCOPE", accounts.env("claude"))
        self.assertNotIn("HC_SCOPE", ui_view.clean_env())
        with patch.dict(os.environ, {"HC_TERMINAL_ID": "t1"}):
            self.assertEqual(config.tool_env().get("HC_TERMINAL_ID"), "t1")  # its own address only


class Ensure(Case):
    def test_without_the_service_nothing_happens(self):
        self.assertEqual((cd.ensure(), self.ctl, self.slept), ("", [], []))

    def test_the_service_is_started_and_waited_for(self):
        self.install()
        p = self.process()

        def sleep(s):
            self.slept.append(s)
            if len(self.slept) == 3:
                self.lock(p.pid, "foreground")
        self.in_service()
        with patch.object(cd, "_sleep", sleep):
            self.assertEqual(cd.ensure(), "service")
        self.assertEqual((self.ctl, len(self.slept)), ([("start", "--no-block", UNIT)], 3))
        self.ctl.clear()
        self.assertEqual((cd.ensure(), self.ctl), ("service", []))  # a held account costs one file read

    def test_an_on_demand_daemon_is_used_as_it_is(self):
        self.install()
        self.lock(self.process().pid)
        self.assertEqual((cd.ensure(), self.slept), ("transient", []))

    def test_a_service_that_does_not_start_never_stops_a_launch(self):
        self.install()
        self.assertEqual(cd.ensure(wait_s=1), "")
        self.assertLess(sum(self.slept), 1.3)
        self.slept.clear()
        with patch.object(cd, "_systemctl", lambda *a: subprocess.CompletedProcess([], 1, "", "no bus")):
            self.assertEqual((cd.ensure(), self.slept), ("", []))
        with patch.object(cd, "_systemctl", side_effect=OSError("gone")):
            self.assertEqual(cd.ensure(), "")

    def test_every_launch_path_ensures_first(self):
        calls = []
        ok = subprocess.CompletedProcess([], 0, "backgrounded · abcd1234", "")

        def harness(name, args, **kw):
            calls.append(("run", args[0], args[1]))
            return ok
        be = backends.ClaudeBackend("two")
        with patch.object(cd, "ensure", lambda a="", **kw: calls.append(("ensure", a))), \
             patch.object(backends, "run_harness", harness), patch.object(backends, "claude_env", lambda a="": {}), \
             patch.object(backends, "row_account", lambda row, job="": "rows"), \
             patch.object(backends.ClaudeBackend, "_row", lambda self, s, include_stopped=False: {"id": "abcd1234"}):
            be.create("t", str(self.tmp), brief="b")
            be._resume({"cwd": str(self.tmp)}, "sess", "text")
            be.respawn("abcd1234")
        self.assertEqual(calls, [("ensure", "two"), ("run", "--bg", "-n"), ("ensure", "rows"), ("run", "--bg", "--resume"),
                                 ("ensure", "rows"), ("run", "respawn", "abcd1234")])

    def test_an_attach_ensures_first(self):
        import ui_view
        calls = []
        with patch.object(cd, "ensure", lambda a="", **kw: calls.append("ensure")), \
             patch.object(ui_view, "verify_listed", lambda *a: ""), \
             patch.object(ui_view, "viewer", lambda *a, **kw: (["claude", "attach", "x"], {})), \
             patch.object(ui_view, "service_restarted_since", lambda t: False), \
             patch.object(ui_view.subprocess, "run", lambda *a, **kw: (calls.append("run"), DONE)[1]), \
             contextlib.redirect_stdout(io.StringIO()):
            ui_view.run_viewer("claude", "s", "l")
            ui_view.run_viewer("codex", "s", "l")
        self.assertEqual(calls, ["ensure", "run", "run"])


class Service(Case):
    def setUp(self):
        super().setUp()
        self.seen = self.tmp / "seen"
        stub = self.tmp / "claude"
        stub.write_text(f'#!/bin/bash\necho "$* scope=${{HC_SCOPE-none}} inv=${{INVOCATION_ID-none}}" >> {self.seen}\n')
        stub.chmod(0o755)
        p = patch.object(backends, "harness_exe", lambda name, wait_s=None: str(stub))
        p.start()
        self.addCleanup(p.stop)

    def launches(self):
        return self.seen.read_text().splitlines() if self.seen.exists() else []

    def run_(self, rounds):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cd.run("", rounds=rounds)
        return out.getvalue()

    def test_it_starts_the_daemon_with_an_environment_of_its_own(self):
        self.run_(1)
        self.assertEqual(self.launches(), ["daemon run --origin foreground scope=none inv=none"])

    def test_an_on_demand_daemon_is_left_alone_until_a_takeover(self):
        self.lock(self.process().pid)
        out = self.run_(5)
        self.assertEqual((self.launches(), out.count("left alone")), ([], 1))

        def sleep(s):
            self.slept.append(s)
            if len(self.slept) == 7:
                os.kill(os.getpid(), signal.SIGUSR1)
        with patch.object(cd, "_sleep", sleep):
            self.run_(3)
        self.assertEqual(len(self.launches()), 1)

    def test_a_daemon_the_service_started_is_watched_not_doubled(self):
        self.lock(self.process().pid, "foreground")  # its successor after an update, or one an earlier `run` started
        self.run_(4)
        self.assertEqual(self.launches(), [])

    def test_an_account_held_once_is_taken_back_from_an_on_demand_daemon(self):
        p = self.process()
        self.lock(p.pid, "foreground")

        def sleep(s):
            self.slept.append(s)
            self.lock(p.pid)  # a client won the restart at an update
        with patch.object(cd, "_sleep", sleep):
            self.run_(2)
        self.assertEqual(len(self.launches()), 1)

    def test_a_restart_for_an_update_is_not_an_exit(self):
        p = self.process()
        self.lock(p.pid)
        gone = [None, cd.holder(), cd.holder()]
        with patch.object(cd, "holder", lambda a="": gone.pop(0) if gone else None):
            gone.insert(0, {"pid": p.pid, "origin": "transient", "cgroup": "/x", "service": False})
            self.run_(4)
            self.assertEqual((self.launches(), self.slept), ([], [cd.POLL_S, cd.GONE_S, cd.POLL_S, cd.POLL_S]))
            self.run_(1)  # nobody when `run` starts: it starts one at once
        self.assertEqual(len(self.launches()), 1)

    def test_a_daemon_that_exited_is_replaced(self):
        p = self.process()
        self.lock(p.pid)

        def sleep(s):
            self.slept.append(s)
            (self.home / "daemon.lock").unlink(missing_ok=True)
        with patch.object(cd, "_sleep", sleep):
            self.run_(3)
        self.assertEqual((self.slept[:2], len(self.launches())), ([cd.POLL_S, cd.GONE_S], 1))

    def test_a_daemon_that_will_not_stay_up_backs_off(self):
        self.run_(9)
        self.assertEqual(self.slept[:5], [2, 4, 8, 16, 32])
        self.assertEqual(max(self.slept), cd.BACKOFF_MAX_S)

    def test_takeover_signals_the_units_main_process_only(self):
        self.lock(self.process().pid)
        main = self.process()
        with patch.object(cd, "_systemctl", lambda *a: subprocess.CompletedProcess([], 0, f"{main.pid}\n", "")), \
             patch.object(cd.os, "kill") as kill, contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(cd.takeover(), 1)  # that pid is not in the unit's cgroup
            self.assertFalse(kill.called)
            with patch.object(cd, "cgroup", lambda pid: f"/x/{UNIT}" if pid == main.pid else "/x/run-1.scope"):
                self.assertEqual(cd.takeover(), 1)
            kill.assert_called_once_with(main.pid, signal.SIGUSR1)
        self.assertIn("not taken over", out.getvalue())


class Install(Case):
    def test_the_unit_never_signals_a_daemon_or_a_session(self):
        t = cd.unit_text()
        for line in ("KillMode=process", "Restart=always", "OOMPolicy=continue", "Slice=shoal-claude.slice",
                     "claude-daemon run --account %i"):
            self.assertIn(line, t)

    def test_install_writes_once_and_leaves_an_operators_file_alone(self):
        path = self.units / cd.TEMPLATE
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cd.install(dry_run=True), 0)
            self.assertFalse(path.exists())
            self.assertEqual(cd.install(), 0)
            self.assertEqual((path.read_text(), self.ctl), (cd.unit_text(), []))  # a redirected dir is not the user manager's
            path.write_text("[Service]\n")
            self.assertEqual((cd.install(), path.read_text()), (1, "[Service]\n"))
            self.assertEqual((cd.uninstall(), path.exists()), (0, True))

    def test_accounts_are_instances(self):
        self.assertEqual((cd.unit(""), cd.unit("claude-2")), (UNIT, "shoal-claude-daemon@claude-2.service"))
        self.assertEqual(cd.unit("a b"), "")


if __name__ == "__main__":
    unittest.main()
