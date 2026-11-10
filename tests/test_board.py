#!/usr/bin/env python3
"""The Board tab's read model (`shoal ui board`) and the digest's board history, on synthetic digests. No board reads."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

PACKAGE = Path(__file__).resolve().parents[1] / "shoal"
sys.path[:0] = [str(PACKAGE), str(PACKAGE / "ui")]
import digest
import ui_board

DAY = 86400
NOW = 1_800_000_000.0
SRC = "github-project:example/1"


def item(n, **kw):
    it = {"item": f"app#{n}", "repo": "example/app", "number": n, "kind": "issue", "title": f"item {n}", "state": "OPEN",
          "status": "Todo", "labels": [], "assignees": [], "url": f"https://github.com/example/app/issues/{n}", "pos": n,
          "blocked_by": [], "parent": "", "sub_issues": 0, "source": SRC}
    it.update(kw)
    return it


DIGEST = {"generated_utc": "2027-01-15T08:00:00Z", "boards": [{"source": SRC, "title": "Example", "url": "", "done": 4}],
          "items": {f"app#{n}": it for n, it in ((1, item(1, sub_issues=2)), (2, item(2, parent="app#1")),
                                                 (3, item(3, parent="app#1")), (4, item(4, blocked_by=["app#3"])),
                                                 (5, item(5, parent="app#99")), (6, item(6, source="github-project:other/2")))}}
ROWS = [{"slug": "core", "active": "example/app#2:fix",
         "next": [{"item": "example/app#3:fix", "blocker": ""}, {"item": "example/app#4:fix", "blocker": "hold: waits on #3"}]},
        {"slug": "docs", "active": "", "next": [{"item": "app#2:review", "blocker": ""}]}]


class BoardTests(unittest.TestCase):
    def test_role_work_active_first_then_queue_place(self):
        w = ui_board.role_work(ROWS)
        self.assertEqual(w["app#2"], {"work": "active", "role": "core", "pos": 0, "blocker": ""})
        self.assertEqual(w["app#3"]["work"], "queued")
        self.assertEqual((w["app#4"]["work"], w["app#4"]["pos"]), ("queued-blocked", 2))
        self.assertEqual(ui_board.short_key("Owner/Repo#12:fix"), "repo#12")

    def test_graph_nodes_edges_and_an_off_board_parent(self):
        with tempfile.TemporaryDirectory() as d:
            b = ui_board.board_graph("sid", DIGEST, ROWS, NOW, hist_path=os.path.join(d, "none.jsonl"))[0]
        nodes = {n["key"]: n for n in b["nodes"]}
        self.assertEqual(set(nodes), {"app#1", "app#2", "app#3", "app#4", "app#5", "app#99"}, "another board's item stays out")
        self.assertTrue(nodes["app#99"]["off_board"])
        self.assertEqual((nodes["app#4"]["work"], nodes["app#4"]["blocker"]), ("queued-blocked", "hold: waits on #3"))
        self.assertEqual(nodes["app#1"]["work"], "unqueued")
        self.assertEqual(sorted((e["from"], e["to"], e["kind"]) for e in b["edges"]),
                         [("app#1", "app#2", "parent"), ("app#1", "app#3", "parent"), ("app#3", "app#4", "blocks"),
                          ("app#99", "app#5", "parent")])
        m = b["metrics"]
        self.assertEqual((m["open"], m["done"], m["total"], m["pct_done"]), (5, 4, 9, 44))
        self.assertEqual((m["active"], m["queued"], m["queued_blocked"], m["unqueued"], m["dep_blocked"]), (1, 1, 1, 2, 1))
        self.assertFalse(b["closed"])
        self.assertEqual(ui_board.board_graph("sid", None, ROWS, NOW), [])

    def test_a_board_with_nothing_open_is_closed(self):
        empty = dict(DIGEST, items={})
        b = ui_board.board_graph("sid", empty, ROWS, NOW, hist_path="/nonexistent")[0]
        self.assertTrue(b["closed"])
        self.assertEqual(b["metrics"]["pct_done"], 100)

    def test_series_closes_a_day_and_open_at_day_end(self):
        hist = [{"ts": NOW - 20 * DAY, "open": 12, "done": 1}, {"ts": NOW - 3.5 * DAY, "open": 10, "done": 3},
                {"ts": NOW - 2.5 * DAY, "open": 9, "done": 4}, {"ts": NOW - 2.2 * DAY, "open": 8, "done": 6},
                {"ts": NOW - 0.5 * DAY, "open": 8, "done": 6}]
        s = ui_board.series(hist, NOW, days=5)
        self.assertEqual(s["closed"], [0, 2, 3, 0, 0], "from the reading before the window: 1->3, then 3->6")
        self.assertEqual(s["open"], [12, 10, 8, 8, 8])
        m = ui_board.board_metrics([{"work": "unqueued", "blocked_by": [], "sub_issues": 0}] * 8, 6, hist, NOW)
        self.assertEqual((m["closed_7d"], m["eta_days"]), (5, 11))
        self.assertIsNone(ui_board.board_metrics([], 0, [], NOW)["eta_days"])

    def test_history_is_appended_at_most_every_half_hour_and_kept_ninety_days(self):
        with tempfile.TemporaryDirectory() as d, patch.object(digest, "data_dir", return_value=d):
            path = digest.history_path("sid")
            with open(path, "w") as fh:
                fh.write(json.dumps({"ts": NOW - 100 * DAY, "source": SRC, "open": 30, "done": 0}) + "\n")
            digest.note_history("sid", DIGEST, now=NOW)
            digest.note_history("sid", DIGEST, now=NOW + 60)  # too soon: nothing new
            digest.note_history("sid", DIGEST, now=NOW + digest.HISTORY_EVERY_S)
            with open(path) as fh:
                rows = [json.loads(line) for line in fh]
            self.assertEqual([r["ts"] for r in rows], [int(NOW), int(NOW + digest.HISTORY_EVERY_S)], "the 100-day-old one went")
            self.assertEqual((rows[0]["open"], rows[0]["done"]), (5, 4), "open counts the board's own items")
            got = ui_board.history("sid", SRC, NOW + digest.HISTORY_EVERY_S, path)
            self.assertEqual(len(got), 2)

    def test_write_keeps_the_digest_when_the_history_cannot_be_written(self):
        spec = type("Spec", (), {"sid": "sid"})()
        with tempfile.TemporaryDirectory() as d, patch.object(digest, "data_dir", return_value=d), \
                patch.object(digest, "note_history", side_effect=OSError("disk full")):
            path = digest.write(spec, DIGEST)
            self.assertTrue(os.path.exists(path))


if __name__ == "__main__":
    unittest.main()
