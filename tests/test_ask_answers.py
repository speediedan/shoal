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
import sys
import tempfile
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
import ui_status  # noqa: E402
import worker  # noqa: E402
from ledger import Ledger, View  # noqa: E402
from roles import Role, ShoalSpec, state_dir  # noqa: E402

OLD = ["Merge PR #101 (lane-b: fix the cache key) at head 1a2b3c4d?", "Merge PR #102 (lane-b: retry the upload) at head 5e6f7a8b?"]
NEW = ["Merge PR #201 (lane-b: widen the dtype check) at head 9c0d1e2f?", "Merge PR #202 (lane-b: pin the tokenizer) at head 3a4b5c6d?"]


def answer(asks, picks):
    """The Asks dialog's message: one line per answered ask (model.js batchAnswerText)."""
    return "\n".join(f"{AA.answer_prefix(n, asks[n - 1])} {c}." for n, c in picks)


class Naming(unittest.TestCase):
    def test_ids_and_leads_match_the_dialogs(self):
        # the values tests/herdr_app/test_shoal_actions.mjs asserts for the dialog's side
        long = 'Merge PR #301 (lane-b: fix the  "widget" cache eviction order under load) at head 1a2b3c4d?'
        self.assertEqual(AA.ask_id("May I squash-merge PR #48?"), "afb5ba")
        self.assertEqual(AA.ask_id(long), "179294")
        self.assertEqual(AA.ask_lead(long), "Merge PR #301 (lane-b: fix the 'widget' cache")
        self.assertEqual(AA.ask_lead("x" * 70), "x" * 60)
        self.assertEqual(AA.ask_id("a  b\n"), AA.ask_id("a b"), "whitespace does not change an ask's id")
        self.assertEqual(AA.answer_prefix(2, long), f're ask 2 [179294] "{AA.ask_lead(long)}":')

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
        self.assertIn("queue --shoal s --slug r --answered 2", lines[1])
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
    def test_mail_maps_each_answer_to_the_ask_it_names(self):
        self.say(answer(OLD, [(1, "Grant merge"), (2, "Grant merge")]))
        self.say(answer(list(reversed(NEW)), [(1, "Grant merge")]))  # written while NEW[1] was ask 1
        out = io.StringIO()
        a = argparse.Namespace(sid=self.sid, slug="r", peek=True, spec=self.spec_path, no_sync=True)
        with contextlib.redirect_stdout(out):
            worker.cmd_next(a)
        text = out.getvalue()
        gone = [x for x in text.splitlines() if "NOT one of your open asks" in x]
        self.assertEqual(len(gone), 2, text)
        self.assertTrue(all("[" + AA.ask_id(o) + "]" in x for o, x in zip(OLD, gone)), gone)
        hit = [x for x in text.splitlines() if "-> your ask 2 now" in x]
        self.assertEqual(len(hit), 1, text)
        self.assertIn("re ask 1 [" + AA.ask_id(NEW[1]) + "]", hit[0], "named ask 1 when written: ask 2 now")


class Watcher(Fixture):
    """The watcher holding a woken answer for a session at an open question goes through it (stop, resume)."""

    def run_ticks(self, n, held="question", attached=(), mode="idle"):
        if mode != "idle":
            self.spec.features = {"health": {"direct_wake": mode}}
            self.spec.save(self.spec_path)
        calls = []
        ticks = [n - 1]

        def sleep(_s):
            ticks[0] -= 1
            if ticks[0] < 0:
                raise StopIteration

        def deliver(sid, spec, role, session, text, through_question=False):
            calls.append((text, through_question))
            return "stop+resume" if through_question else "deferred (at a question)"
        import time as _time
        sc = os.path.join(self.root, "watch.state")
        ap = argparse.ArgumentParser()
        lw.add_parser(ap.add_subparsers(dest="cmd"))
        args = ap.parse_args(["ledger-watch", "--shoal", self.sid, "--spec", self.spec_path, "--slug", "r", "--wake", "direct",
                              "--session", self.role.session, "--interval", "0", "--no-sync", "--no-board", "--sidecar", sc,
                              "--ledger-dir", self.root])
        out = io.StringIO()
        with patch.object(lw, "hold_reason", lambda *a, **k: held), patch.object(lw, "claude_deliver", deliver), \
                patch.object(lw, "claude_attached", lambda role: list(attached)), patch.object(lw, "emit_wake", lambda *a, **k: None), \
                patch.object(lw, "code_stamp", lambda: 0.0), patch.object(lw, "_limit_until", lambda *a, **k: ""), \
                patch.object(lw, "time", types.SimpleNamespace(sleep=sleep, time=_time.time)), contextlib.redirect_stdout(out):
            try:
                lw.cmd_ledger_watch(args)
            except StopIteration:
                pass
        return calls, out.getvalue(), lw.load_sidecar(sc)

    def test_a_woken_answer_goes_through_the_question(self):
        ev = self.say(answer(NEW, [(1, "Grant merge"), (2, "Grant merge")]))
        calls, out, sc = self.run_ticks(2)
        self.assertEqual(len(calls), 1, (calls, out))
        text, through = calls[0]
        self.assertTrue(through, "delivered through the open question")
        self.assertIn(ev["id"], out)
        self.assertIn("closed an open question", out)
        self.assertIn(ev["id"], sc.get("delivered", ""), "delivered once, not again on the next tick")
        self.assertFalse(sc.get("held_reason"), sc)

    def test_the_resumed_turn_is_told_why(self):
        # the note goes on in ClaudeBackend.deliver, only when a question was open
        lw_deliver = lw.claude_deliver
        seen = {}
        fake = types.SimpleNamespace(deliver=lambda *a, **k: seen.update(k) or "stop+resume")
        with patch.object(backends, "get", lambda eco, *a, **k: fake):
            lw_deliver(self.sid, self.spec, self.role, self.role.session, "LEDGER-WAKE", through_question=True)
        self.assertTrue(seen["through_question"])
        self.assertTrue(seen["question_note"].startswith("YOUR OPEN QUESTION WAS CLOSED"))

    def test_other_events_and_people_keep_the_hold(self):
        self.say("fyi: the board moved", wake=False)
        self.say("take the board's next item when you can")  # woken, but answers no ask
        self.led.append("user", "msg", _commit=False, to="*", kind="info", wake=True, text=answer(NEW, [(1, "Grant merge")]))
        self.led.append("user", "msg", _commit=False, to="r", kind="request", ack=True, wake=True, text="QUEUE ORDER: re ask 1: x")
        self.led.append("user", "offer", _commit=False, to="r", item="app#9:fix", prio=20)
        calls, _, sc = self.run_ticks(2)
        self.assertEqual(calls, [], "no answer to its asks (a note, a broadcast, a request, an offer): nothing stops the turn")
        self.assertEqual(sc.get("held_reason"), "question")
        self.say(answer(NEW, [(1, "Grant merge")]))
        calls, out, sc = self.run_ticks(2, attached=[4242])
        self.assertEqual(calls, [], "a person attached at its chat answers there")
        self.assertIn("claude attach", sc.get("held_client", ""), sc)
        calls, _, _ = self.run_ticks(2, mode="dead")
        self.assertEqual(calls, [], "direct_wake dead never stops a live worker")
        self.assertEqual(lw.through_question(View(self.led, self.spec), "r", [], self.spec, self.role), (False, ""))

    def test_a_question_only_the_listing_saw(self):
        # the transcript had not shown the question yet, the listing had: the deferred delivery is retried through it
        self.say(answer(NEW, [(1, "Grant merge")]))
        calls, out, sc = self.run_ticks(1, held="")
        self.assertEqual([t for _, t in calls], [False, True], calls)
        self.assertFalse(sc.get("held_reason"), sc)


class Deliver(unittest.TestCase):
    """ClaudeBackend.deliver: through a question it stops and resumes, with the note first; without, it defers."""

    def go(self, through, prompt=True, note="NOTE: "):
        b = backends.ClaudeBackend()
        resumed, stopped = [], []
        row = {"id": "cafe0000", "sessionId": "cafe0000-0000", "pid": 4242}
        with patch.object(backends, "fake_sessions", lambda: None), \
                patch.object(b, "_row", lambda *a, **k: dict(row, pid=None) if stopped else row), \
                patch.object(b, "stop", lambda job: stopped.append(job)), patch.object(b, "_resume", lambda r, s, text, *a, **k: resumed.append(text) or "resume"), \
                patch.object(health, "claude_liveness", lambda r: "idle"), patch.object(health, "claude_job_state", lambda r: {}), \
                patch.object(health, "claude_prompt_open", lambda r, s: prompt), patch.object(backends, "RESUME_SETTLE_S", 0):
            spec = types.SimpleNamespace(features={}, dir="")
            ch = b.deliver(spec, types.SimpleNamespace(slug="r"), "cafe0000-0000", "WAKE", through_question=through, question_note=note)
        return ch, resumed

    def test_deliver(self):
        self.assertEqual(self.go(False), ("deferred (at a question)", []))
        self.assertEqual(self.go(True), ("stop+resume", ["NOTE: WAKE"]))
        self.assertEqual(self.go(True, prompt=False), ("stop+resume", ["WAKE"]), "no question open: no note")


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


class MsgNote(Fixture):
    """`shoal msg --wake` (the Asks dialog's Submit) says what becomes of an answer to a role at an open question."""

    def test_note(self):
        live = {"session": "blocked", "prompt": True, "t": 0}
        ev = self.say(answer(NEW, [(1, "Grant merge")]))
        note = lambda e=ev, role=self.role: ledger_cli._question_note(self.spec, role, e)
        with patch.object(ui_status, "live_turn", lambda *a, **k: live), patch.object(lw, "claude_attached", lambda role: []):
            self.assertIn("its watcher closes that question and resumes the turn", note())
            self.assertIn("names none of its asks", note(self.say("go on")), "a plain note waits for the question")
        with patch.object(ui_status, "live_turn", lambda *a, **k: live), patch.object(lw, "claude_attached", lambda role: [77]):
            self.assertIn("a client is attached to it (pid 77)", note())
        with patch.object(ui_status, "live_turn", lambda *a, **k: dict(live, prompt=False)):
            self.assertEqual(note(), "")
        self.assertEqual(note(role=types.SimpleNamespace(ecosystem="codex", slug="c")), "")


if __name__ == "__main__":
    unittest.main()
