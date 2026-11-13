#!/usr/bin/env python3
"""An umbrella licenses no work on its rows: `next` names each row's holder, a row claimed under another lane's active
umbrella mails that lane, open PRs that close a queued item are named, and `shoal claimed` answers who holds what."""
import datetime
import os
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "shoal"))
import umbrella  # noqa: E402
from ledger import Ledger, View  # noqa: E402
from roles import Role, ShoalSpec  # noqa: E402

SID = "umbrella-test"
NOW = 1_800_000_000.0


def iso(t: float) -> str:
    return datetime.datetime.fromtimestamp(t, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def ev(by: str, typ: str, mins_ago: float, n: int, **kw) -> dict:
    return {"ts": iso(NOW - mins_ago * 60), "by": by, "type": typ, "id": f"e{n:04d}", "_writer_file": by, **kw}


DOC = {"items": {
    "app#1": {"item": "app#1", "parent": "", "sub_issues": 3, "rows": ["app#2", "app#3", "app#4"], "prs": []},
    "app#9": {"item": "app#9", "parent": "", "sub_issues": 0, "rows": [],
              "prs": [{"number": 99, "state": "OPEN", "branch": "feature/9_fix"}, {"number": 98, "state": "CLOSED"}]},
}}


class Umbrella(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        roles = [Role(slug=s, ecosystem="claude", title=s, doc="/x/r.md", fill="self", session=f"{s}0000") for s in ("core", "integ")]
        self.spec = ShoalSpec(sid=SID, dir=self.tmp.name, incarnation=1, roles=roles,
                              ledger={"repo": "example.invalid/x.git", "dir": self.tmp.name})

    def view(self, events):
        led = Ledger(SID, root=self.tmp.name, sync=False)
        led._events = sorted(events, key=lambda e: e["ts"])
        return View(led, self.spec)

    def events(self):
        return [ev("core", "claim", 60, 1, item="app#1", verb="fix", src="queue", session="core0000"),
                ev("core", "queue", 59, 2, version=1, active="app#1:fix", next=["app#9:fix"], status="active",
                   session="core0000"),
                ev("integ", "claim", 30, 3, item="app#2", verb="fix", src="queue", session="integ0000"),
                ev("integ", "queue", 29, 4, version=1, active="app#2:fix", next=[], status="active", session="integ0000")]

    def test_next_names_the_rows_and_their_holders(self):
        v = self.view(self.events())
        lines = umbrella.next_lines(self.spec, v, "core", "app#1:fix", ["app#9:fix"], doc=DOC)
        umb = next(x for x in lines if x.startswith("UMBRELLA app#1"))
        self.assertIn("app#2 (integ)", umb)
        self.assertIn("app#3 (free)", umb)
        self.assertIn("licenses no work on its rows", umb)
        pr = [x for x in lines if x.startswith("PR OPEN app#9")]
        self.assertEqual(len(pr), 1, "only the open PR")
        self.assertIn("#99", pr[0])
        self.assertIn("feature/9_fix", pr[0])
        # the row's holder is told whose umbrella it is under
        rl = umbrella.next_lines(self.spec, v, "integ", "app#2:fix", [], doc=DOC)
        self.assertTrue(any(x.startswith("UNDER UMBRELLA app#2") and "core" in x for x in rl), rl)

    def test_a_row_claimed_under_another_lanes_umbrella_mails_it(self):
        v = self.view(self.events()[:2])
        mail = umbrella.claim_mail(self.spec, v, "integ", ["app#3:fix"], doc=DOC)
        self.assertEqual([to for to, _ in mail], ["core"])
        self.assertIn("app#3", mail[0][1])
        self.assertEqual(umbrella.claim_mail(self.spec, v, "core", ["app#3:fix"], doc=DOC), [], "your own umbrella")
        self.assertEqual(umbrella.claim_mail(self.spec, v, "integ", ["app#9:fix"], doc=DOC), [], "not a row")

    def test_claimed_answers_and_exits_3_when_another_lane_holds_it(self):
        v = self.view(self.events())
        rc, lines = umbrella.report(self.spec, v, "app#2", slug="core", doc=DOC)
        self.assertEqual(rc, 3)
        self.assertTrue(lines[0].startswith("CLAIMED-BY integ"))
        self.assertTrue(any(x.startswith("UMBRELLA app#1: the active item of core") for x in lines), lines)
        rc, lines = umbrella.report(self.spec, v, "app#2", slug="integ", doc=DOC)
        self.assertEqual(rc, 0, "your own claim")
        rc, lines = umbrella.report(self.spec, v, "app#3", slug="integ", doc=DOC)
        self.assertEqual(rc, 0)
        self.assertTrue(lines[0].startswith("FREE app#3"))
        self.assertTrue(any("core may be working it under app#1" in x for x in lines), lines)
        rc, lines = umbrella.report(self.spec, v, "app#1", slug="integ", doc=DOC)
        self.assertTrue(any(x.startswith("ROWS of app#1") and "app#2 (integ)" in x for x in lines), lines)

    def test_no_digest_says_nothing(self):
        v = self.view(self.events())
        self.assertEqual(umbrella.next_lines(self.spec, v, "core", "app#1:fix", [], doc={}), [])


if __name__ == "__main__":
    os.environ.setdefault("SHOAL_FAKE_SESSIONS", "{}")
    unittest.main()
