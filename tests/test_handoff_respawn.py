#!/usr/bin/env python3
"""A Codex move that failed (2026-11-18): a role's own background CI waiters re-invoked its Claude session as soon as the
handoff stopped it, the restart ended FAILED with the session live and its watcher gone, a later handoff used the
provider default as it was hours earlier, and wake turns on Codex threads kept a model the spec no longer named.

- the host config is re-read when its file changes, in a process that read it before;
- a Claude role with background tasks in flight is not ready for a handoff (`health.claude_background`);
- a wake turn on a Codex thread runs on the role's model and effort (`ledger_watch.fire`);
- the handoff request asks the role to end its in-session background tasks.
The re-stop rounds of `retire` are in selftest_ui."""
import os
from pathlib import Path
import sys
import tempfile
import time
import types
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "shoal"))
import config  # noqa: E402
import health  # noqa: E402
import ledger_watch  # noqa: E402


class HandoffRespawn(unittest.TestCase):
    def test_the_host_config_is_reread_when_it_changes(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = Path(d, "host.toml")
            cfg.write_text('[provider_defaults.codex]\nmodel = "m-new"\n')
            with patch.dict(os.environ, {"SHOAL_CONFIG": str(cfg)}):
                self.assertEqual(config.provider_default("codex")[0], "m-new")
                cfg.write_text('[provider_defaults.codex]\nmodel = "m-old"\n')
                t = time.time() + 5
                os.utime(cfg, (t, t))
                self.assertEqual(config.provider_default("codex")[0], "m-old", "a long-lived process sees the edit")

    def test_background_tasks_in_flight_hold_a_handoff(self):
        role = types.SimpleNamespace(slug="r", ecosystem="claude", session="conv0001-aaaa")
        rows = [{"id": "conv0001", "sessionId": "conv0001-aaaa", "pid": 9, "tempo": "idle",
                 "inFlight": {"tasks": 2, "kinds": ["shell"]}}]
        with patch.object(health, "_CLAUDE_ROWS", rows):
            self.assertEqual(health.claude_background(role), 2)
        rows[0]["inFlight"] = {}
        with patch.object(health, "_CLAUDE_ROWS", rows):
            self.assertEqual(health.claude_background(role), 0)
        self.assertEqual(health.claude_background(types.SimpleNamespace(slug="x", ecosystem="claude", session="")), 0)

    def test_a_codex_wake_turn_runs_on_the_role_model(self):
        sent = []

        class FakeBackend:
            def send(self, sid, repo_dir, text, **kw):
                sent.append((sid, kw))

        with patch("backends.get", return_value=FakeBackend()):
            self.assertTrue(ledger_watch.fire("exec:t1", "wake", "/d", "codex", "gpt-6.1-sol", "high"))
            self.assertTrue(ledger_watch.fire("exec:t2", "wake", "/d", "muse", "m", "e"))
        self.assertEqual(sent, [("t1", {"model": "gpt-6.1-sol", "effort": "high"}), ("t2", {})])

    def test_the_request_asks_for_background_tasks_to_end(self):
        src = Path(__file__).resolve().parents[1].joinpath("shoal", "handoff_flow.py").read_text()
        self.assertIn("Stop every in-session background task or monitor you hold", src)


if __name__ == "__main__":
    unittest.main()
