#!/usr/bin/env python3
"""The maintainer's answers to a role's asks reach the role, and name the asks they answer.

Measured 2026-11-25: (A) a Claude role opened an in-chat question for the asks it had recorded; the maintainer answered
them from the Asks dialog, and the answer, a woken message, sat undelivered behind the open question until the same
question was answered in the chat; (B) the dialog's answer named the asks by number only (`re ask 1: Grant merge.`),
and the role had replaced both asks at the same numbers three minutes after an earlier answer.

Synthetic throughout: a scratch ledger, a fake Claude session (the watcher's session checks and delivery are replaced
at the function boundary), no harness CLI, no network."""
import argparse
import contextlib
import datetime
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import types
import unittest
from unittest.mock import patch

PACKAGE = Path(__file__).resolve().parents[1] / "shoal"
sys.path[:0] = [str(PACKAGE), str(PACKAGE / "ui")]
os.environ.setdefault("SHOAL_FAKE_SESSIONS", json.dumps({"claude": [], "opencode": {}}))
os.environ["SHOAL_FAKE_REF_STATE"] = "{}"  # asks name PRs: their state is never read from GitHub here
import ask_answers as AA  # noqa: E402
import backends  # noqa: E402
import health  # noqa: E402
import ledger_cli  # noqa: E402
import ledger_watch as lw  # noqa: E402
import park  # noqa: E402
import ui_status  # noqa: E402
import worker  # noqa: E402
from ledger import Ledger, View  # noqa: E402
from roles import Role, ShoalSpec, state_dir  # noqa: E402

OLD = ["Merge PR #101 (lane-b: fix the cache key) at head 1a2b3c4d?", "Merge PR #102 (lane-b: retry the upload) at head 5e6f7a8b?"]
NEW = ["Merge PR #201 (lane-b: widen the dtype check) at head 9c0d1e2f?", "Merge PR #202 (lane-b: pin the tokenizer) at head 3a4b5c6d?"]


def prefix(n, ask):
    """How the Asks dialog names ask n (model.js answerPrefix)."""
    return AA.name({"n": n, "id": AA.ask_id(ask), "lead": AA.ask_lead(ask)}) + ":"


def answer(asks, picks):
    """The Asks dialog's message: one line per answered ask (model.js batchAnswerText)."""
    return "\n".join(f"{prefix(n, asks[n - 1])} {c}." for n, c in picks)


class Naming(unittest.TestCase):
    def test_ids_and_leads_match_the_dialogs(self):
        # the values tests/herdr_app/test_shoal_actions.mjs asserts for the dialog's side
        long = 'Merge PR #301 (lane-b: fix the  "widget" cache eviction order under load) at head 1a2b3c4d?'
        self.assertEqual(AA.ask_id("May I squash-merge PR #48?"), "afb5ba")
        self.assertEqual(AA.ask_id(long), "179294")
        self.assertEqual(AA.ask_lead(long), "Merge PR #301 (lane-b: fix the 'widget' cache")
        self.assertEqual(AA.ask_lead("x" * 70), "x" * 60)
        self.assertEqual(AA.ask_id("a  b\n"), AA.ask_id("a b"), "whitespace does not change an ask's id")
        self.assertEqual(prefix(2, long), f're ask 2 [179294] "{AA.ask_lead(long)}":')

    def test_parse(self):
        got = AA.parse('re ask 1 [abc123] "Merge PR #1": Grant merge. ok\nre ask 2: Deny.')
        self.assertEqual([(r["n"], r["id"], r["lead"], r["body"]) for r in got],
                         [(1, "abc123", "Merge PR #1", "Grant merge. ok"), (2, "", "", "Deny.")])

    def test_resolve_by_id_not_position(self):
        msg = answer(OLD, [(1, "Grant merge"), (2, "Deny")])
        self.assertEqual([(r["now"], r["how"]) for r in AA.resolve(msg, OLD)], [(1, "id"), (2, "id")])
        self.assertEqual([(r["now"], r["how"]) for r in AA.resolve(msg, list(reversed(OLD)))], [(2, "id"), (1, "id")],
                         "reordered: each line still finds its own ask")
        # the incident: the role replaced both asks at the same numbers; the answer names neither new one
        self.assertEqual([(r["now"], r["how"]) for r in AA.resolve(msg, NEW)], [(None, "gone"), (None, "gone")])
        self.assertEqual(AA.answered(msg, NEW), [])

    def test_resolve_quote_reworded_ambiguous_number(self):
        q = 're ask 1 "Merge PR #201": Grant merge.'
        self.assertEqual([(r["now"], r["how"]) for r in AA.resolve(q, ["x", NEW[0]])], [(2, "quote")])
        # same opening words, new text (a new head): pointed at, but never counted answered
        changed = NEW[0].replace("9c0d1e2f", "77777777")
        r = AA.resolve(answer(NEW, [(1, "Grant merge")]), [changed])[0]
        self.assertEqual((r["now"], r["how"]), (1, "reworded"))
        self.assertEqual(AA.answered(answer(NEW, [(1, "Grant merge")]), [changed]), [])
        r = AA.resolve('re ask 3 "Merge PR": Grant.', NEW)[0]
        self.assertEqual((r["now"], r["how"], r["hits"]), (None, "ambiguous", [1, 2]))
        self.assertEqual(AA.resolve('re ask 2 "Merge PR": Grant.', NEW)[0]["now"], 2, "the number breaks a tie")
        self.assertEqual([(r["now"], r["how"]) for r in AA.resolve("re ask 2: Deny.", NEW)], [(2, "number")])
        self.assertEqual(AA.resolve("re ask 3: Deny.", NEW)[0]["how"], "gone")

    def test_next_lines_say_what_to_do(self):
        lines = AA.next_lines(answer(OLD, [(1, "Grant merge")]) + "\n" + answer(NEW, [(2, "Deny")]), NEW, "s", "r")
        self.assertIn("NOT one of your open asks", lines[0])
        self.assertIn("do not apply it to another ask", lines[0])
        self.assertIn("-> your ask 2 now", lines[1])
        self.assertIn(f"queue --shoal s --slug r --answered {AA.ask_id(NEW[1])}`", lines[1])
        self.assertIn("by number only", AA.next_lines("re ask 1: Grant merge.", NEW)[0])
        self.assertEqual(AA.next_lines("thanks, carry on", NEW), [])


class Card(unittest.TestCase):
    """The agents list counts an ask answered only when the reply names that ask."""
    NOW = datetime.datetime(2026, 11, 25, 21, 40, tzinfo=datetime.timezone.utc)

    def msg(self, text, wake=True):
        return {"ts": "2026-11-25T21:33:22Z", "by": "user", "to": "r", "text": text, "wake": wake}

    def test_reply_answer(self):
        self.assertIsNone(ui_status.reply_answer([self.msg(answer(OLD, [(1, "Grant merge"), (2, "Grant merge")]))], NEW, self.NOW),
                          "an answer to asks the role has replaced answers nothing")
        got = ui_status.reply_answer([self.msg(answer(NEW, [(2, "Grant merge")]))], list(reversed(NEW)), self.NOW)
        self.assertEqual(got["asks"], [1], "the ask it names, wherever it now is")
        cut = [a[:20] for a in NEW]  # a status row cuts asks short and keeps their ids beside them
        got = ui_status.reply_answer([self.msg(answer(NEW, [(1, "G"), (2, "D")]))], cut, self.NOW, [AA.ask_id(a) for a in NEW])
        self.assertFalse(got["partial"])

    def test_reply_hint_and_card(self):
        sid = f"ask-answers-card-{os.getpid()}"
        self.addCleanup(shutil.rmtree, state_dir(sid), True)
        text = answer(NEW, [(1, "Grant merge"), (2, "Grant merge")])
        ui_status.note_reply(sid, "r", {"ts": "2026-11-25T21:33:22Z", "id": "e1", "wake": True, "text": text})
        reply = ui_status.read_reply(sid, "r")
        self.assertEqual(reply["asks"], [1, 2])
        self.assertEqual(reply["refs"], [[1, AA.ask_id(NEW[0]), AA.ask_lead(NEW[0])], [2, AA.ask_id(NEW[1]), AA.ask_lead(NEW[1])]])
        row = {"slug": "r", "needs_you": True, "as_of": "2026-11-25T21:30:00Z", "asks": [a[:30] for a in NEW],
               "ask_ids": [AA.ask_id(a) for a in NEW], "agent": {"group": "needs-you", "state": "asked"},
               "can_progress": {"reasons": ["waiting-on-you"]}}
        self.assertEqual(ui_status.card_view(row, reply, None)[1], "answered")
        stale = dict(row, asks=[a[:30] for a in OLD], ask_ids=[AA.ask_id(a) for a in OLD])
        self.assertEqual(ui_status.card_view(stale, reply, None)[1], "", "its asks changed: your reply answered others")
        legacy = {"ts": reply["ts"], "wake": True, "asks": [1, 2]}  # a hint written before refs
        self.assertEqual(ui_status.card_view(row, legacy, None)[1], "answered")


class Fixture(unittest.TestCase):
    """A scratch shoal with one Claude role `r` whose queue holds NEW and whose last answer is the maintainer's."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        self.sid = f"ask-answers-{os.getpid()}-{id(self) % 10000}"
        self.addCleanup(shutil.rmtree, state_dir(self.sid), True)
        env = patch.dict(os.environ, {"SHOAL_FAKE_SESSIONS": json.dumps({"claude": [], "opencode": {}})})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("CLAUDE_CODE_SESSION_ID", None)
        self.role = Role(slug="r", ecosystem="claude", title="r", doc="/x/r.md", fill="assigned", session="cafe0000-0000")
        self.spec = ShoalSpec(sid=self.sid, dir=self.root, incarnation=1, roles=[self.role],
                              ledger={"repo": "example.invalid/x.git", "dir": self.root})
        self.spec_path = os.path.join(self.root, "spec.json")
        self.spec.save(self.spec_path)
        self.led = Ledger(self.sid, root=self.root, sync=False)
        self.led.append("r", "queue", _commit=False, version=1, active="", next=[], status="wait", asks=NEW, session=self.role.session)

    def say(self, text, wake=True, by="user"):
        return self.led.append(by, "msg", _commit=False, to="r", text=text, kind="info", wake=True if wake else None)


class Next(Fixture):
    def next(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            worker.cmd_next(argparse.Namespace(sid=self.sid, slug="r", peek=True, spec=self.spec_path, no_sync=True))
        return out.getvalue()

    def test_mail_maps_each_answer_to_the_ask_it_names(self):
        self.say(answer(OLD, [(1, "Grant merge"), (2, "Grant merge")]))
        self.say(answer(list(reversed(NEW)), [(1, "Grant merge")]))  # written while NEW[1] was ask 1
        self.led.append("user", "msg", _commit=False, to="*", kind="info", text="everyone: re ask 9: whatever")
        text = self.next()
        gone = [x for x in text.splitlines() if "NOT one of your open asks" in x]
        self.assertEqual(len(gone), 2, text)
        self.assertTrue(all("[" + AA.ask_id(o) + "]" in x for o, x in zip(OLD, gone)), gone)
        hit = [x for x in text.splitlines() if "-> your ask 2 now" in x]
        self.assertEqual(len(hit), 1, text)
        self.assertIn("re ask 1 [" + AA.ask_id(NEW[1]) + "]", hit[0], "named ask 1 when written: ask 2 now")

    def test_the_maintainers_mail_is_never_cut_by_peer_mail(self):
        self.say(answer(NEW, [(1, "Grant merge")]))
        for k in range(7):
            self.led.append("peer", "msg", _commit=False, to="r", kind="info", text=f"peer note {k}")
        text = self.next()
        self.assertIn("-> your ask 1 now", text)
        self.assertIn("peer note 6", text)
        self.assertNotIn("peer note 0", text)

    def test_answered_takes_the_asks_id(self):
        for cmd in (["init", "-q"], ["config", "user.email", "t@example.invalid"], ["config", "user.name", "t"]):
            subprocess.run(["git", "-C", self.root, *cmd], check=True)
        ap = argparse.ArgumentParser()
        worker.add_parsers(ap.add_subparsers(dest="cmd"))
        a = ap.parse_args(["queue", "--shoal", self.sid, "--spec", self.spec_path, "--slug", "r", "--no-sync",
                           "--answered", AA.ask_id(NEW[1])])
        with contextlib.redirect_stdout(io.StringIO()) as out:
            rc = a.func(a)
        self.assertEqual(rc, 0, out.getvalue())
        self.assertEqual(View(Ledger(self.sid, root=self.root, sync=False), self.spec).current_queue("r")["asks"], [NEW[0]])


class Watcher(Fixture):
    """The watcher holding a woken answer for a session at an open question goes through it (stop, resume), once, and
    only when that is safe."""

    def run_ticks(self, n, held="question", attached=(), question="q1", state=None, deliver=None):
        calls = []
        ticks = [n - 1]

        def sleep(_s):
            ticks[0] -= 1
            if ticks[0] < 0:
                raise StopIteration

        def ok(sid, spec, role, session, text, through=False):
            return "stop+resume" if through else "deferred (at a question)"

        def send(*a, **k):
            calls.append(bool(k.get("through")))
            return (deliver or ok)(*a, **k)
        sc = os.path.join(self.root, "watch.state")
        ap = argparse.ArgumentParser()
        lw.add_parser(ap.add_subparsers(dest="cmd"))
        args = ap.parse_args(["ledger-watch", "--shoal", self.sid, "--spec", self.spec_path, "--slug", "r", "--wake", "direct",
                              "--session", self.role.session, "--interval", "0", "--no-sync", "--no-board", "--sidecar", sc,
                              "--ledger-dir", self.root])
        out = io.StringIO()
        with patch.object(lw, "hold_reason", lambda *a, **k: held), patch.object(lw, "claude_deliver", send), \
                patch.object(park, "claude_attach_pids", side_effect=attached if isinstance(attached, Exception) else lambda *a: list(attached)), \
                patch.object(health, "claude_open_prompt", lambda *a: question), patch.object(health, "claude_job_state", lambda r: state or {}), \
                patch.object(lw, "emit_wake", lambda *a, **k: None), patch.object(lw, "code_stamp", lambda: 0.0), \
                patch.object(lw, "_limit_until", lambda *a, **k: ""), \
                patch.object(lw, "time", types.SimpleNamespace(sleep=sleep, time=time.time)), contextlib.redirect_stdout(out):
            try:
                lw.cmd_ledger_watch(args)
            except StopIteration:
                pass
        return calls, out.getvalue(), lw.load_sidecar(sc)

    def test_a_woken_answer_goes_through_the_question_once(self):
        ev = self.say(answer(NEW, [(1, "Grant merge"), (2, "Grant merge")]))
        calls, out, sc = self.run_ticks(2)
        self.assertEqual(calls, [True], out)
        self.assertIn(ev["id"], sc.get("delivered", ""), "delivered once, not again on the next tick")
        self.assertEqual(sc.get("through"), f"{ev['id']}@q1")
        self.assertFalse(sc.get("held_reason"), sc)
        # its delivered list lost (a capped list, a reset sidecar): the same answer never closes another question
        sc["delivered"] = ""
        lw.save_sidecar(os.path.join(self.root, "watch.state"), sc)
        self.assertEqual(self.run_ticks(2, question="q2")[0], [])

    def test_a_number_only_or_stale_answer_never_stops_the_turn(self):
        self.say("re ask 1: Grant merge.")  # an older dialog, or typed: which ask it meant is not proven
        self.say(answer(OLD, [(1, "Grant merge")]))  # asks the role has replaced
        self.say(answer([NEW[0].replace("9c0d1e2f", "77777777")], [(1, "Grant merge")]))  # a grant for another head
        calls, _, sc = self.run_ticks(2)
        self.assertEqual(calls, [])
        self.assertEqual(sc.get("held_reason"), "question")

    def test_other_events_keep_the_hold(self):
        self.say("fyi: the board moved", wake=False)
        self.say(answer(NEW, [(1, "Grant merge")]), wake=False)  # an unwoken note
        self.say("take the board's next item when you can")  # woken, but answers no ask
        self.say(answer(NEW, [(1, "Grant merge")]), by="peer")
        self.led.append("user", "msg", _commit=False, to="*", kind="info", wake=True, text=answer(NEW, [(1, "Grant merge")]))
        self.led.append("user", "msg", _commit=False, to="r", kind="request", ack=True, wake=True, text=answer(NEW, [(1, "x")]))
        self.led.append("user", "offer", _commit=False, to="r", item="app#9:fix", prio=20)
        calls, _, sc = self.run_ticks(2)
        self.assertEqual(calls, [])
        self.assertEqual(sc.get("held_reason"), "question")

    def test_a_person_work_or_the_mode_keeps_the_hold(self):
        self.say(answer(NEW, [(1, "Grant merge")]))
        self.assertEqual(self.run_ticks(2, held="busy")[0], [], "mid-turn")
        self.assertEqual(self.run_ticks(2, attached=[4242])[0], [], "a person at its chat answers there")
        self.assertEqual(self.run_ticks(2, attached=OSError("no /proc"))[0], [], "unknown is not nobody")
        self.assertEqual(self.run_ticks(2, question="")[0], [], "a prompt that is no question of its own")
        self.assertEqual(self.run_ticks(2, state={"inFlight": {"tasks": 1, "kinds": ["local_agent"]}})[0], [], "background work")
        self.spec.features = {"health": {"direct_wake": "dead"}}
        self.spec.save(self.spec_path)
        self.assertEqual(self.run_ticks(2)[0], [], "direct_wake dead never stops a live worker")

    def test_answered_in_the_chat_meanwhile(self):
        # between the tick's check and the stop the session moved on: nothing is stopped, the answer waits its turn
        ev = self.say(answer(NEW, [(1, "Grant merge")]))
        calls, _, sc = self.run_ticks(1, deliver=lambda *a, **k: "deferred (mid-turn)")
        self.assertEqual(calls, [True])
        self.assertNotIn(ev["id"], sc.get("delivered", ""))
        self.assertFalse(sc.get("through"), "nothing was stopped for it")
        # the role read it at its boundary: no longer an event
        os.makedirs(state_dir(self.sid), exist_ok=True)
        with open(os.path.join(state_dir(self.sid), "mail-read-r"), "w") as fh:
            fh.write(ev["ts"] + "\n")
        self.assertEqual(self.run_ticks(2)[0], [])

    def test_a_failed_resume_is_retried(self):
        ev = self.say(answer(NEW, [(1, "Grant merge")]))
        got = iter(["FAILED after stop rc=1: daemon gone", "resume"])
        calls, out, sc = self.run_ticks(3, deliver=lambda *a, **k: next(got))
        self.assertEqual(len(calls), 2, out)
        self.assertIn("WAKE-SEND-FAILED (1/5)", out)
        self.assertIn(ev["id"], sc["delivered"])

    def test_a_cli_that_times_out_is_a_failure_not_a_dead_watcher(self):
        def slow(*a, **k):
            raise subprocess.TimeoutExpired("claude", 120)
        with patch.object(backends, "get", lambda eco, *a, **k: types.SimpleNamespace(deliver=slow)):
            self.assertTrue(lw.claude_deliver(self.sid, self.spec, self.role, "cafe", "w", through=True).startswith("FAILED"))


class Deliver(unittest.TestCase):
    """ClaudeBackend.deliver: through a question it stops and resumes, with the note first; without, it defers."""

    def go(self, through=True, prompt=True, live="idle", confirm=None, resumed_as="resume", pid=4242):
        b = backends.ClaudeBackend()
        resumed, stopped = [], []
        row = {"id": "cafe0000", "sessionId": "cafe0000-0000", "pid": pid}
        with patch.object(backends, "fake_sessions", lambda: None), \
                patch.object(b, "_row", lambda *a, **k: dict(row, pid=None) if stopped else row), \
                patch.object(b, "stop", lambda job: stopped.append(job)), \
                patch.object(b, "_resume", lambda r, s, text, *a, **k: resumed.append(text) or resumed_as), \
                patch.object(health, "claude_liveness", lambda r: live), patch.object(health, "claude_job_state", lambda r: {}), \
                patch.object(health, "claude_prompt_open", lambda r, s: prompt), \
                patch.object(health, "claude_open_prompt", lambda s: "q1" if prompt else ""), patch.object(backends, "RESUME_SETTLE_S", 0):
            spec = types.SimpleNamespace(features={}, dir="")
            ch = b.deliver(spec, types.SimpleNamespace(slug="r"), "cafe0000-0000", "WAKE", through_question=through, confirm=confirm)
        return ch, resumed, stopped

    def test_deliver(self):
        self.assertEqual(self.go(through=False), ("deferred (at a question)", [], []))
        ch, resumed, stopped = self.go()
        self.assertEqual((ch, stopped), ("stop+resume", ["cafe0000"]))
        self.assertEqual(resumed, [backends.QUESTION_CLOSED_NOTE + "WAKE"])
        self.assertEqual(self.go(prompt=False)[:2], ("stop+resume", ["WAKE"]), "no question open: no note")

    def test_the_last_check_before_the_stop(self):
        self.assertEqual(self.go(live="busy"), ("deferred (mid-turn)", [], []), "answered in the chat: its turn runs")
        self.assertEqual(self.go(confirm=lambda: "a client is attached"),
                         ("deferred (at a question: a client is attached)", [], []))
        self.assertEqual(self.go(confirm=lambda: "")[0], "stop+resume")

    def test_a_resume_that_fails_after_the_stop(self):
        self.assertEqual(self.go(resumed_as="FAILED rc=1: no daemon")[0], "FAILED after stop rc=1: no daemon",
                         "never read as delivered")
        ch, resumed, stopped = self.go(pid=None)  # the retry: no worker now, its transcript still ends at the question
        self.assertEqual((ch, stopped), ("resume", []))
        self.assertTrue(resumed[0].startswith("YOUR OPEN QUESTION WAS CLOSED"), resumed)
        self.assertTrue(self.go(pid=None, prompt=False)[1][0].startswith(backends.RESUMED_NOTE))


class Transcript(unittest.TestCase):
    def test_a_prompt_after_the_question_closes_it(self):
        tp = os.path.join(tempfile.mkdtemp(), "t.jsonl")
        self.addCleanup(shutil.rmtree, os.path.dirname(tp), True)
        ask = json.dumps({"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "AskUserQuestion", "id": "q1"}]}})
        prompt = json.dumps({"type": "user", "message": {"role": "user", "content": "LEDGER-WAKE r@s"}})
        listed = json.dumps({"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": "go"}]}})
        meta = json.dumps({"type": "user", "isMeta": True, "message": {"role": "user", "content": "caveat"}})
        with patch.object(health, "claude_transcript", lambda s: tp), patch.object(backends, "fake_sessions", lambda: None):
            for lines, want in (([ask], "q1"), ([ask, prompt], ""), ([ask, listed], ""), ([ask, meta], "q1"),
                                ([prompt, ask], "q1")):
                Path(tp).write_text("\n".join(lines) + "\n")
                self.assertEqual(health.claude_open_prompt("s"), want, lines)


class MsgNotes(Fixture):
    """`shoal msg --wake` (the Asks dialog's Submit, its dry run too) says what becomes of an answer."""

    def notes(self, text, prompt=True, stays="", to="r"):
        with patch.object(ui_status, "live_turn", lambda *a, **k: {"session": "blocked", "prompt": prompt, "t": 0}), \
                patch.object(lw, "question_stays", lambda *a: stays):
            return ledger_cli._answer_notes(self.spec, self.led, {"by": "user", "to": to, "wake": True, "text": text})

    def test_notes(self):
        mine = answer(NEW, [(1, "Grant merge")])
        self.assertEqual(len(self.notes(mine)), 1)
        self.assertIn("its watcher closes the question at its next tick", self.notes(mine)[0])
        self.assertIn("waits until the question is answered in its chat (a client is attached", self.notes(mine, stays="a client is attached: pid 7")[0])
        self.assertEqual(self.notes(mine, prompt=False), [])
        self.assertEqual(self.notes("thanks, carry on"), [])
        self.assertEqual(self.notes(mine, to="*"), [])
        old = self.notes(answer(OLD, [(1, "Grant merge")]))
        self.assertIn("names none of r's asks now", old[0])
        self.assertIn("names none of its asks by id or words", old[1])
        self.role.ecosystem = "codex"
        self.assertEqual(self.notes(mine), [], "another harness has no open question")


if __name__ == "__main__":
    unittest.main()
