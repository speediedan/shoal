#!/usr/bin/env python3
"""Release work first and lane work off the board (measured 2026-11-17: two fill=self lanes of fts-it-refactor idled
beside free work). The release in progress (a repository's lowest open version milestone) ranks first and is listed with
who has each item; an umbrella ranks below claimable rows; a `github-label` source brings in lane-labelled issues the
board lacks, after the board, marked unboarded; `claimed` reads an offer its addressee declined, and a holder's handoff,
as `next` does; a done on an issue the digest shows open says what it blocks; mail from inside a role's session must
name its writer. (`queue --pass` taking an item as `next` names it is in selftest_h4.)"""
import argparse
import datetime
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "shoal"))
import digest  # noqa: E402
import ledger_cli  # noqa: E402
import sources  # noqa: E402
import umbrella  # noqa: E402
from ledger import Ledger, LedgerError, View  # noqa: E402
from roles import Role, ShoalSpec  # noqa: E402

SID = "release-fill-test"
NOW = 1_800_000_000.0


def iso(t: float) -> str:
    return datetime.datetime.fromtimestamp(t, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def item(n: int, **kw) -> dict:
    return sources.fixture_item(dict({"repo": "o/r", "number": n, "title": f"t{n}"}, **kw), n)


def ev(typ: str, by: str, n: int, minutes: int, **kw) -> dict:
    return dict({"ts": iso(NOW - 3600 + minutes * 60), "by": by, "type": typ, "id": f"{typ}-{by}-{n}-{minutes}",
                 "item": f"r#{n}"}, **kw)


class ReleaseFill(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.spec = ShoalSpec(sid=SID, dir=self.tmp.name, incarnation=1,
                              roles=[Role(slug="r", ecosystem="claude", title="r", doc="/x/r.md", fill="self",
                                          session="aaaa1111-0000-4000-8000-000000000000"),
                                     Role(slug="q", ecosystem="claude", title="q", doc="/x/q.md", fill="self",
                                          session="bbbb2222-0000-4000-8000-000000000000")],
                              ledger={"repo": "example.invalid/x.git", "dir": self.tmp.name})

    def view(self, events: list[dict]) -> View:
        led = Ledger(SID, root=self.tmp.name, sync=False)
        led._events = sorted(events, key=lambda e: e["ts"])
        return View(led, self.spec)

    def test_the_release_in_progress_ranks_first_and_umbrellas_last(self):
        items = [item(1, rows=["r#9"], sub_issues=1), item(2), item(3, milestone="v2.17.0"),
                 item(4, milestone="v2.15.0"), item(5, milestone="v2.15.0"), item(6, milestone="backlog"),
                 item(7, sub_issues=2)]
        self.assertEqual(digest.releases(items), {"o/r": "v2.15.0"}, "the lowest version; a name is no release")
        v = self.view([ev("claim", "q", 5, 1)])
        with patch("time.time", return_value=NOW):
            doc = digest.build(self.spec, view=v, items=items, no_model=True)
        rows = doc["roles"]["r"]
        self.assertEqual([x["item"] for x in rows], ["r#4", "r#2", "r#3", "r#6", "r#1", "r#7"])
        self.assertIn("release:v2.15.0+3", rows[0]["why"])
        self.assertIn("umbrella:1-open-rows-2", rows[4]["why"])
        self.assertIn("umbrella:0-open-rows-2", rows[5]["why"], "its rows all closed: still an umbrella")
        self.assertEqual(doc["release"], {"r v2.15.0": [{"item": "r#4", "has": ""},
                                                        {"item": "r#5", "has": "claimed by q"}]})
        with patch.object(digest, "load", return_value=doc):
            self.assertEqual(digest.release_lines(SID, "r"), ["RELEASE r v2.15.0: r#4 (free), r#5 (claimed by q)"])
            got = digest.candidates(SID, "r", taken=set(), declined=set(), limit=5)
        self.assertIn("umbrella-rows=r#9", digest.fmt_candidate(got[4]))

    def test_a_label_source_brings_lane_issues_the_board_lacks(self):
        board, labels = Path(self.tmp.name, "board.json"), Path(self.tmp.name, "labels.json")
        board.write_text(json.dumps([{"repo": "o/r", "number": 1}, {"repo": "o/r", "number": 2}]))
        labels.write_text(json.dumps({"lane: x": [{"repo": "o/r", "number": 2}, {"repo": "o/s", "number": 7}]}))
        self.spec.sources = [{"kind": "github-project", "owner": "o", "number": 3},
                             {"kind": "github-label", "label": "lane: x", "repos": ["o/r", "o/s"]}]
        self.assertEqual(self.spec.validate(), [])
        env = {"SHOAL_BOARD_FIXTURE": str(board), "SHOAL_LABEL_FIXTURE": str(labels)}
        with patch.dict(os.environ, env), patch("time.time", return_value=NOW):
            doc = digest.build(self.spec, view=self.view([]), no_model=True)
        self.assertEqual(doc["sources"], ["github-project:o/3", "github-label:lane: x"])
        self.assertEqual([x["item"] for x in doc["roles"]["r"]], ["r#1", "r#2", "s#7"], "the board first, once each")
        self.assertEqual(doc["unboarded"], ["s#7"])
        self.assertEqual(doc["items"]["s#7"]["pos"], 3)
        self.assertIn("unboarded (github-label:lane: x)", digest.fmt_candidate(dict(doc["items"]["s#7"], verb="fix",
                                                                                    score=0, why=[])))
        self.assertEqual(digest.label_query(self.spec.sources[1]), 'is:issue is:open label:"lane: x" repo:o/r repo:o/s')
        self.assertEqual(sources.parse_source("github-label:o/r|o/s:lane: x"),
                         {"kind": "github-label", "label": "lane: x", "repos": ["o/r", "o/s"]})
        self.spec.sources = [{"kind": "github-label", "label": "lane: x"}]
        self.assertTrue(any("github-label" in e for e in self.spec.validate()), "a label needs its repositories")

    def test_claimed_reads_offers_as_next_does(self):
        v = self.view([ev("offer", "user", 7, 1, to="r"), ev("claim", "r", 7, 2),
                       ev("release", "r", 7, 3, kind="decline", cls="blocked"),
                       ev("offer", "user", 8, 1, to="q"), ev("claim", "q", 8, 2),
                       ev("release", "q", 8, 3, kind="handoff", to="r")])
        _, lines = umbrella.report(self.spec, v, "r#7", doc={})
        self.assertIn("OFFERED to r by user (declined by r: not on their queue)", lines)
        _, lines = umbrella.report(self.spec, v, "r#8", doc={})
        self.assertIn("HANDED to r by q (its holder's handoff)", lines)

    def test_mail_from_a_role_session_names_its_writer(self):
        a = argparse.Namespace(by="")
        with patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": self.spec.roles[0].session}):
            with self.assertRaises(LedgerError) as cm:
                ledger_cli._writer(a, self.spec, allow_ext=True)
            self.assertIn("--by r", str(cm.exception))
            self.assertEqual(ledger_cli._writer(argparse.Namespace(by="user"), self.spec), "user", "named: allowed")
        with patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "cccc3333", "CLAUDE_JOB_DIR": "", "SHOAL_SELF_SESSION": ""}):
            self.assertEqual(ledger_cli._writer(a, self.spec), "user", "outside a role's session: user, as before")

    def test_a_done_on_an_open_issue_says_what_it_blocks(self):
        doc = {"generated_utc": "2026-11-17T22:00:00Z", "items": {"r#9": dict(item(9), state="OPEN")}}
        with patch.object(digest, "load", return_value=doc):
            self.assertEqual(ledger_cli.open_in_digest(self.spec, "r#9"), "as of 2026-11-17T22:00:00Z")
            self.assertEqual(ledger_cli.open_in_digest(self.spec, "r#10"), "")


if __name__ == "__main__":
    os.environ.setdefault("SHOAL_FAKE_SESSIONS", "{}")
    unittest.main()
