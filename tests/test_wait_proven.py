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
import pool_reserve
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


class GpuWaits(unittest.TestCase):
    """A research wait on GPUs another role reserved (measured 2026-11-26): `resv:` targets, reservations a holder
    names, and the note to queue a request of one's own."""
    RID = "r11260937-8e64"

    def rows(self, *recs, bad=()):
        return patch.object(pool_reserve, "live_records", return_value=([dict(r) for r in recs], list(bad)))

    def other(self, **kw):
        return dict({"id": self.RID, "for": "example/peer", "state": "held", "host": "hostb", "until": "job:x"}, **kw)

    def test_a_resv_target_is_pending_while_another_roles_reservation_stands(self):
        self.assertEqual(providers.normalize(f"resv:{self.RID}"), (f"resv:{self.RID}", ""))
        self.assertTrue(providers.normalize("resv:tonight")[1], "not a reservation id")
        with self.rows(self.other()):
            self.assertIs(providers.resolve(f"resv:{self.RID}", "role", "example")[0], True)
        with self.rows(self.other(state="waiting", waiting="behind an earlier request")):
            self.assertIs(providers.resolve(f"resv:{self.RID}", "role", "example")[0], True)
        with self.rows(self.other(**{"for": "example/role"})):
            self.assertEqual(providers.resolve(f"resv:{self.RID}", "role", "example"), (False, f"{self.RID} granted on hostb"))
        with self.rows():
            self.assertIs(providers.resolve(f"resv:{self.RID}", "role", "example")[0], False)
        with self.rows(bad=["hostb"]):
            self.assertIsNone(providers.resolve(f"resv:{self.RID}", "role", "example")[0], "a host unread is unknown")
        import role_time
        self.assertEqual(role_time.blocker_event(f"resv:{self.RID}"), "research-window")

    def test_a_time_wait_naming_reservations_is_unproven_once_they_all_end(self):
        st = {"state": "waiting", "sub": "research-window", "target": "time:2099-01-01T00:00:00Z", "ts": ago(100),
              "holder": f"hostb: both GPUs reserved by peer ({self.RID}, back-to-back legs)"}
        with self.rows(self.other()):
            self.assertTrue(state.wait_proven(SPEC, View(), "role", st)[0], "still reserved: the time proves it")
            self.assertEqual(state.named_ended("research-window", st["target"], st["holder"]), ([], ""))
        with self.rows():
            ok, why = state.wait_proven(SPEC, View(), "role", st)
            self.assertFalse(ok)
            self.assertIn(self.RID, why)
            self.assertEqual(state.named_ended("research-window", st["target"], st["holder"])[0], [self.RID])
        with self.rows(bad=["hostb"]):
            self.assertTrue(state.wait_proven(SPEC, View(), "role", st)[0], "unknown is never ended")
        with self.rows():
            self.assertTrue(state.wait_proven(SPEC, View(), "role", dict(st, holder="overnight GPU window"))[0])
            self.assertEqual(state.named_ended("local-ci", "job:x", st["holder"]), ([], ""), "only a research wait on a time")

    def test_a_wait_on_another_roles_reservation_is_told_to_queue_a_request(self):
        holder = f"GPUs reserved by peer ({self.RID})"
        with self.rows(self.other()):
            notes = state.gpu_wait_notes("research-window", "time:2099-01-01T00:00:00Z", holder, "role", "example")
            self.assertEqual(len(notes), 2, notes)
            self.assertIn(f"--target resv:{self.RID}", notes[0])
            self.assertIn("pool reserve --host hostb --resources gpus:<N> --for example/role", notes[1])
            notes = state.gpu_wait_notes("research-window", f"resv:{self.RID}", "peer's run", "role", "example")
            self.assertEqual(len(notes), 1, notes)
            self.assertIn("no pool request of yours is queued", notes[0])
        mine = self.other(id="r11261223-d13b", state="waiting", **{"for": "example/role"})
        with self.rows(self.other(), mine):
            self.assertEqual(len(state.gpu_wait_notes("research-proto", "time:2099-01-01T00:00:00Z", holder, "role",
                                                      "example")), 1, "a request of its own is queued")
        with self.rows(self.other(), bad=["hostc"]):
            self.assertEqual(len(state.gpu_wait_notes("research-window", "time:2099-01-01T00:00:00Z", holder, "role",
                                                      "example")), 1, "a host unread may hold its request")
        with self.rows(self.other()):
            self.assertEqual(state.gpu_wait_notes("research-window", "time:2099-01-01T00:00:00Z", "overnight window",
                                                  "role", "example"), [], "a scheduled window names no reservation")
            self.assertEqual(state.gpu_wait_notes("local-ci", "job:x", holder, "role", "example"), [])

    def test_the_pool_is_read_across_hosts_and_an_unread_host_is_reported(self):
        import pool_cli
        here = {"id": "r11260900-aaaa", "for": "example/peer", "state": "held", "alive": True}
        there = {"id": self.RID, "for": "example/peer", "state": "held", "alive": True}
        gone = {"id": "r11260800-bbbb", "alive": False}
        with patch.object(pool_reserve.config, "server", return_value="hosta"), \
                patch.object(pool_reserve, "report", return_value=[here, gone]), \
                patch.object(pool_cli, "pool_names", return_value=["hosta", "hostb", "hostc"]), \
                patch.object(pool_cli, "gather", return_value={"hostb": {"reservations": [there]},
                                                               "hostc": {"error": "unreachable"}}) as g:
            rows, bad = pool_reserve.live_records()
            self.assertEqual(([(r["id"], r["host"]) for r in rows], bad),
                             ([(here["id"], "hosta"), (self.RID, "hostb")], ["hostc"]))
            g.assert_called_once_with(only=["hostb", "hostc"])
            g.reset_mock()
            pool_reserve.live_records(lambda rs: any(r["id"] == here["id"] for r in rs))
            g.assert_not_called()
        self.assertEqual(pool_reserve.named(f"by peer (r11260730-15d1, then {self.RID}; r11260730-15d1)"),
                         ["r11260730-15d1", self.RID])


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

    def test_the_items_own_run_is_a_checkable_blocker(self):
        for b in ("job:suite12", "gh-run:owner/repo:123", "az-build:45", "time:2030-01-01T02:00:00Z"):
            self.assertEqual((blockers.kind(b), blockers.provider_target(b)), ("run", b), b)
        self.assertEqual(blockers.kind("jobber:repo#1"), "role", "only the provider kinds themselves")
        q = self.q("job:suite12", ago(800))
        for answer, want in ((True, "live"), (False, "resolved")):
            with patch.object(providers, "resolve", return_value=(answer, "x")):
                self.assertEqual(blockers.check(SPEC, View(), "role", q, "repo#1", "job:suite12", q["next_blocked_meta"]["repo#1"])[0], want)
        self.assertEqual(blockers.cleared({"repo#1": "resolved: unit exited rc=0"}, q), {"repo#1": "resolved: unit exited rc=0"},
                         "a run that ended is news for its role, as a cleared peer item is")
        import role_time
        self.assertEqual([role_time.blocker_event(b) for b in ("job:x", "gh-run:o/r:1", "time:2030-01-01T00:00:00Z", "hold:x")],
                         ["local-ci", "remote-ci", "research-window", "hold"])

    def test_a_hold_still_expires_on_its_own_age(self):
        q = self.q("hold:waiting for a decision", ago(300))
        self.assertEqual(blockers.check(SPEC, View(), "role", q, "repo#1", q["next_blocked"]["repo#1"], q["next_blocked_meta"]["repo#1"])[0], "expired")


class AgentState(unittest.TestCase):
    role = types.SimpleNamespace(fill="assigned")

    def state(self, role_status="idle", sub="", st=None, q=None, reasons=(), sess="idle", wait_done=False, stale=None):
        return ui_status.agent_state(self.role, role_status, sub, st or {}, q or {}, list(reasons), sess, wait_done, stale)

    def test_groups(self):
        self.assertEqual(self.state(q={"asks": ["Merge it?"]}, reasons=["parked"])["group"], "needs-you")
        self.assertEqual(self.state(reasons=["parked"])["state"], "parked")
        self.assertEqual(self.state(reasons=["no-work"])["state"], "no-work")
        self.assertEqual(self.state(role_status="waiting", sub="local-ci", reasons=["restart-held"])["group"], "needs-you")
        self.assertEqual(self.state(role_status="active", sess="working")["group"], "working")
        self.assertEqual(self.state(role_status="waiting", sub="research-window", st={"target": "time:x"})["group"], "waiting")
        self.assertEqual(self.state(role_status="waiting", sub="upstream")["group"], "blocked")
        self.assertEqual(self.state(role_status="waiting", sub="local-ci", reasons=["wake-held"], wait_done=True)["group"], "needs-you")
        blocked = {"next": ["a#1:fix"], "next_blocked": {"a#1": "upstream:gh-issue:o/r#2"}}
        self.assertEqual(self.state(q=blocked)["state"], "blocked: upstream")
        held = {"next": ["a#1:fix"], "next_blocked": {"a#1": "hold:the maintainer's order"}}
        self.assertEqual(self.state(q=held)["group"], "held")

    def test_an_ask_beside_a_ci_wait_says_both(self):
        s = self.state(role_status="waiting", sub="remote-ci", q={"asks": ["Refresh the key?"]})
        self.assertEqual((s["group"], s["state"]), ("needs-you", "asked"))
        self.assertTrue(s["detail"].startswith("also waiting remote-ci; Refresh"), s["detail"])

    def test_a_turn_running_reads_working_over_an_ask(self):
        s = self.state(role_status="waiting", sub="remote-ci", q={"asks": ["Merge 95?"]}, sess="working")
        self.assertEqual((s["group"], s["state"], s["detail"]), ("working", "working", "asks you: Merge 95?"))
        s = self.state(role_status="waiting", sub="remote-ci", q={"asks": ["Merge 95?"]}, sess="idle")
        self.assertEqual((s["group"], s["state"]), ("needs-you", "asked"), "back to needs you once the turn ends")

    def test_the_ledger_explains_a_stop_claude_reads_as_blocked(self):
        f = ui_status.ledger_explains_stop
        both = {"next": ["a#1:fix", "a#2:fix"], "next_blocked": {"a#1": "upstream:gh-issue:o/r#2", "a#2": "hold:x"}}
        self.assertTrue(f("waiting", {}), "a recorded wait")
        self.assertTrue(f("idle", both), "a blocker on every next item")
        self.assertFalse(f("idle", dict(both, next_blocked={"a#1": "upstream:gh-issue:o/r#2"})), "one item is free")
        self.assertFalse(f("idle", dict(both, active="a#3:fix")), "an active item it stopped on")
        self.assertFalse(f("idle", {}), "nothing recorded: Claude's reading stands")

    def test_a_restart_ready_role_in_auto_mode_needs_nobody(self):
        import context
        role = types.SimpleNamespace(slug="r")
        for mode, over, want in (("auto", False, ("auto", False)), ("auto", True, ("auto", True)),
                                 ("page", False, ("page", False))):
            with patch.object(context, "mode", return_value=mode), \
                    patch.object(context, "over", return_value=1 if over else None):
                self.assertEqual(ui_status._ctx_restart(SPEC, role), want)

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
