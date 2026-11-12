#!/usr/bin/env python3
"""Capacious fill: a pass lapses by its class and at a fill change; a role short of candidates gets back what it passed
on as out of lane or blocked; candidates fill their limit with weaker rows after the strong ones."""
import datetime
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "shoal"))
import digest  # noqa: E402
from ledger import Ledger, View  # noqa: E402
from roles import Role, ShoalSpec  # noqa: E402

SID = "fill-test"
NOW = 1_800_000_000.0


def iso(t: float) -> str:
    return datetime.datetime.fromtimestamp(t, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def decline(item: str, cls: str, hours_ago: float, by: str = "r") -> dict:
    return {"ts": iso(NOW - hours_ago * 3600), "by": by, "type": "release", "id": f"d-{item}-{cls}",
            "item": item, "kind": "decline", "cls": cls, "reason": "x"}


class Fill(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.spec = ShoalSpec(sid=SID, dir=self.tmp.name, incarnation=1,
                              roles=[Role(slug="r", ecosystem="claude", title="r", doc="/x/r.md", fill="self",
                                          session="aaaa1111")],
                              ledger={"repo": "example.invalid/x.git", "dir": self.tmp.name})

    def view(self, events: list[dict]) -> View:
        led = Ledger(SID, root=self.tmp.name, sync=False)
        led._events = sorted(events, key=lambda e: e["ts"])
        return View(led, self.spec)

    def test_a_pass_lapses_by_its_class(self):
        v = self.view([decline("o/r#1", "blocked", 23), decline("o/r#2", "blocked", 25),
                       decline("o/r#3", "out-of-lane", 70), decline("o/r#4", "out-of-lane", 73),
                       decline("o/r#5", "needs-maintainer", 160), decline("o/r#6", "duplicate-or-stale", 170)])
        self.assertEqual(sorted(v.declined("r", now=NOW)), ["o/r#1", "o/r#3", "o/r#5"])

    def test_a_fill_change_lapses_every_earlier_pass(self):
        fill = {"ts": iso(NOW - 3600), "by": "shoal", "type": "system", "id": "f1", "event": "role-fill",
                "role": "r", "fill": "self", "was": "assigned"}
        v = self.view([decline("o/r#1", "out-of-lane", 2), fill, decline("o/r#2", "out-of-lane", 0.5)])
        self.assertEqual(sorted(v.declined("r", now=NOW)), ["o/r#2"])

    def test_a_short_role_gets_back_what_it_passed_on_last_and_weak(self):
        items = [{"item": f"o/r#{i}", "kind": "issue", "pos": i, "labels": [], "title": f"t{i}", "excerpt": "",
                  "repo": "o/r", "blocked_by": []} for i in range(1, 5)]
        v = self.view([decline("o/r#1", "out-of-lane", 1), decline("o/r#2", "needs-hardware", 1),
                       decline("o/r#3", "blocked", 1)])
        with patch("time.time", return_value=NOW):
            doc = digest.build(self.spec, view=v, items=items, no_model=True)
        rows = doc["roles"]["r"]
        self.assertEqual([x["item"] for x in rows], ["o/r#4", "o/r#1", "o/r#3"], "fresh first, then passed, never #2")
        self.assertEqual([x.get("declined") for x in rows], [None, "out-of-lane", "blocked"])
        self.assertTrue(all(x["strong"] is False for x in rows[1:]))
        with patch.object(digest, "load", return_value=doc):
            got = digest.candidates(SID, "r", taken=set(), declined=set(v.declined("r", now=NOW)), limit=5)
        self.assertEqual([c["item"] for c in got], ["o/r#4", "o/r#1", "o/r#3"])
        self.assertEqual([c["weak"] for c in got], [False, True, True])
        self.assertIn("declined-earlier:out-of-lane", digest.fmt_candidate(got[1]))

    def test_a_role_with_enough_candidates_gets_no_fallback(self):
        items = [{"item": f"o/r#{i}", "kind": "issue", "pos": i, "labels": [], "title": f"t{i}", "excerpt": "",
                  "repo": "o/r", "blocked_by": []} for i in range(1, digest.FALLBACK_BELOW + 3)]
        v = self.view([decline("o/r#1", "out-of-lane", 1)])
        with patch("time.time", return_value=NOW):
            doc = digest.build(self.spec, view=v, items=items, no_model=True)
        self.assertNotIn("o/r#1", [x["item"] for x in doc["roles"]["r"]])


if __name__ == "__main__":
    os.environ.setdefault("SHOAL_FAKE_SESSIONS", "{}")
    unittest.main()
