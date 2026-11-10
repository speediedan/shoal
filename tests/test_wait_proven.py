#!/usr/bin/env python3
"""Waits and blockers shoal proves itself, research windows, and the agents list's derived state. Synthetic only."""
import datetime
import os
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch

PACKAGE = Path(__file__).resolve().parents[1] / "shoal"
sys.path[:0] = [str(PACKAGE), str(PACKAGE / "ui")]
import blockers
import providers
import state
import ui_app
import ui_status


def ago(minutes: float) -> str:
    t = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=minutes)
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


SPEC = types.SimpleNamespace(sid="example", features={"waitdiscipline": True, "blockers": True})


class View:
    def __init__(self, msgs=(), done=()):
        self.msgs = list(msgs)
        self.done = {k: {"by": "peer", "ts": ago(1)} for k in done}

    def msgs_to(self, slug, since_ts=""):
        return [m for m in self.msgs if m.get("to") in (slug, "*") and m["ts"] > since_ts and m.get("by") != slug]

    def current_queue(self, slug):
        return {}

    def queue(self, slug):
        return []


class WaitProven(unittest.TestCase):
    def test_a_pending_target_proves_the_wait_and_resolved_or_unknown_do_not(self):
        st = {"state": "waiting", "sub": "local-ci", "target": "job:example", "ts": ago(200)}
        for answer, proven in ((True, True), (False, False), (None, False)):
            with patch.object(providers, "resolve", return_value=(answer, "x")):
                self.assertEqual(state.wait_proven(SPEC, View(), "role", st)[0], proven, answer)

    def test_a_user_wait_is_proven_while_its_ask_is_open_and_unanswered(self):
        st = {"state": "waiting", "sub": "user-auth", "ts": ago(300)}
        self.assertTrue(state.wait_proven(SPEC, View(), "role", st, {"asks": ["Merge it?"]})[0])
        self.assertFalse(state.wait_proven(SPEC, View(), "role", st, {"asks": []})[0])
        reply = View([{"by": "user", "to": "role", "ts": ago(10)}])
        self.assertFalse(state.wait_proven(SPEC, reply, "role", st, {"asks": ["Merge it?"]})[0])

    def test_nothing_is_proven_past_a_day_or_without_the_feature(self):
        st = {"state": "waiting", "sub": "local-ci", "target": "job:example", "ts": ago(1500)}
        with patch.object(providers, "resolve", return_value=(True, "running")):
            self.assertFalse(state.wait_proven(SPEC, View(), "role", st)[0])
            off = types.SimpleNamespace(sid="example", features={})
            self.assertFalse(state.wait_proven(off, View(), "role", dict(st, ts=ago(5)))[0])


class ResearchWindow(unittest.TestCase):
    def test_a_time_target_is_pending_until_then(self):
        self.assertEqual(providers.normalize("time:2026-11-11 02:00")[0], "time:2026-11-11T02:00:00Z")
        self.assertTrue(providers.normalize("time:tonight")[1])
        future = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.assertIs(providers.resolve(f"time:{future}", "role")[0], True)
        self.assertIs(providers.resolve("time:2020-01-01T00:00:00Z", "role")[0], False)

    def test_research_window_needs_a_target_and_a_time_target_needs_research_window(self):
        self.assertIn("research-window wait needs", state.check_target("research-window", "", "role")[2])
        self.assertIn("wait on it as research-window", state.check_target("local-ci", "time:2030-01-01T00:00:00Z", "role")[2])
        self.assertEqual(state.check_target("research-window", "time:2030-01-01T00:00:00Z", "role")[2], "")


class ProvenBlockers(unittest.TestCase):
    def q(self, blocker, since):
        return {"next": ["repo#1:fix"], "next_blocked": {"repo#1": blocker}, "next_blocked_meta": {"repo#1": {"since": since}}}

    def test_an_upstream_blocker_its_provider_shows_pending_lasts_a_day(self):
        q = self.q("upstream:gh-issue:owner/repo#9", ago(800))
        with patch.object(providers, "resolve", return_value=(True, "open")):
            self.assertEqual(blockers.check(SPEC, View(), "role", q, "repo#1", q["next_blocked"]["repo#1"], q["next_blocked_meta"]["repo#1"])[0], "live")
            q = self.q("upstream:gh-issue:owner/repo#9", ago(1500))
            self.assertEqual(blockers.check(SPEC, View(), "role", q, "repo#1", q["next_blocked"]["repo#1"], q["next_blocked_meta"]["repo#1"])[0], "expired")
        q = self.q("upstream:gh-issue:owner/repo#9", ago(800))
        self.assertEqual(blockers.check(SPEC, View(), "role", q, "repo#1", q["next_blocked"]["repo#1"], q["next_blocked_meta"]["repo#1"], live=False)[0], "unknown")

    def test_a_hold_still_expires_on_its_own_age(self):
        q = self.q("hold:waiting for a decision", ago(300))
        self.assertEqual(blockers.check(SPEC, View(), "role", q, "repo#1", q["next_blocked"]["repo#1"], q["next_blocked_meta"]["repo#1"])[0], "expired")


class AgentState(unittest.TestCase):
    role = types.SimpleNamespace(fill="assigned")

    def state(self, posture="idle", sub="", st=None, q=None, reasons=(), sess="idle", wait_done=False, stale=None):
        return ui_status.agent_state(self.role, posture, sub, st or {}, q or {}, list(reasons), sess, wait_done, stale)

    def test_groups(self):
        self.assertEqual(self.state(q={"asks": ["Merge it?"]}, reasons=["parked"])["group"], "needs-you")
        self.assertEqual(self.state(reasons=["parked"])["state"], "parked")
        self.assertEqual(self.state(reasons=["no-work"])["state"], "no-work")
        self.assertEqual(self.state(posture="active", sess="working")["group"], "working")
        self.assertEqual(self.state(posture="waiting", sub="research-window", st={"target": "time:x"})["group"], "waiting")
        self.assertEqual(self.state(posture="waiting", sub="upstream")["group"], "blocked")
        self.assertEqual(self.state(posture="waiting", sub="local-ci", reasons=["wake-held"], wait_done=True)["group"], "needs-you")
        blocked = {"next": ["a#1:fix"], "next_blocked": {"a#1": "upstream:gh-issue:o/r#2"}}
        self.assertEqual(self.state(q=blocked)["state"], "blocked: upstream")
        held = {"next": ["a#1:fix"], "next_blocked": {"a#1": "hold:the maintainer's order"}}
        self.assertEqual(self.state(q=held)["group"], "held")

    def test_a_blocker_past_its_daily_look_stays_blocked(self):
        q = {"next": ["a#1:fix"], "next_blocked": {"a#1": "peer:o/r#3"}}
        s = self.state(q=q, reasons=["parked-work"], stale={"a#1": "expired: recorded 1500m ago"})
        self.assertEqual((s["group"], s["state"]), ("blocked", "blocked: role"))
        self.assertIn("daily re-check due", s["detail"])
        s = self.state(q=q, reasons=["parked-work"], stale={"a#1": "resolved: done"})
        self.assertEqual(s["state"], "work-due")


class RoutedCard(unittest.TestCase):
    def test_seat_ids(self):
        argv = ["/x/shoal-ui", "view", "--harness", "codex", "--session", "old", "--label", "seat", "--stub"]
        self.assertEqual(ui_app.seat_ids(argv), ("codex", "old"))
        self.assertIsNone(ui_app.seat_ids(["codex", "resume", "old"]))


if __name__ == "__main__":
    unittest.main()
