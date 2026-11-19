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
        self.assertEqual(w["app#2"], {"work": "active", "role": "core", "pos": 0, "blocker": "", "state": "in-progress",
                                      "why": "working", "on": "", "inferred": False})
        self.assertEqual((w["app#3"]["work"], w["app#3"]["state"]), ("queued", "queued"))
        self.assertEqual((w["app#4"]["work"], w["app#4"]["pos"], w["app#4"]["state"], w["app#4"]["why"]),
                         ("queued", 2, "blocked", "hold"))
        self.assertEqual(ui_board.short_key("Owner/Repo#12:fix"), "repo#12")

    def test_progress_an_active_item_by_its_roles_wait(self):
        p = lambda sub, target="", **kw: ui_board.progress(wait={"sub": sub, "target": target, **kw}, status="wait")  # noqa: E731
        self.assertEqual(ui_board.progress(status="active")["state"], "in-progress")
        for sub, why in (("local-ci", "ci"), ("remote-ci", "ci"), ("research-proto", "research"), ("research-window", "window")):
            self.assertEqual((p(sub, "job:x")["state"], p(sub, "job:x")["why"]), ("in-progress", why), sub)
        self.assertEqual(p("local-ci", "lease:gpu")["why"], "lease", "its run waits for a lease: still its own work")
        self.assertEqual((p("user-auth")["state"], p("user-auth")["why"]), ("blocked", "maintainer"))
        self.assertEqual(p("upstream", "gh-issue:example/lib#7"), {"state": "blocked", "why": "upstream", "on": "lib#7",
                                                                    "inferred": False})
        self.assertEqual(p("session-blocked", blocked_on="peer:example/app#9")["on"], "app#9")
        self.assertEqual(p("session-blocked", blocked_on="peer")["on"], "peer", "a peer named without an item")

    def test_progress_a_queued_item_by_its_recorded_blocker(self):
        def got(b, stale=""):
            r = ui_board.progress(b, stale)
            return r["state"], r["why"], r["on"], r["inferred"]
        self.assertEqual(got(""), ("queued", "", "", False))
        self.assertEqual(got("peer:example/app#9"), ("blocked", "issue", "app#9", False))
        self.assertEqual(got("peer:example/app#9", "resolved"), ("queued", "", "", False), "a cleared blocker frees it")
        self.assertEqual(got("peer:example/app#9", "expired")[0], "blocked", "past its daily look it is still a blocker")
        self.assertEqual(got("upstream:gh-pr:example/lib#3"), ("blocked", "upstream", "lib#3", False))
        self.assertEqual(got("ask:merge order"), ("blocked", "maintainer", "", False))
        self.assertEqual(got("lease:gpu"), ("in-progress", "lease", "gpu", False))
        self.assertEqual(got("job:suite12"), ("in-progress", "job", "job:suite12", False))
        self.assertEqual(got("gh-run:example/app:123"), ("in-progress", "ci", "gh-run:example/app:123", False))
        self.assertEqual(got("time:2030-01-01T02:00:00Z")[:2], ("in-progress", "window"))
        # free text: a hold that describes the item's own run reads in progress (inferred); any other names nothing
        own = {"hold:PR 12 head 1a2b re-merged main: hosted 123456789 running, then approve its gate": "ci",
               "hold:job x12 on the GPU host runs slices 1-3, waiter w12": "job",
               "hold:run12-main queued on cpu-light (yields to the gate)": "job",
               "hold:selection jobs s12a/s12b, watched by local job w12": "job",
               "hold:suite job s12 waits for the cpu-heavy lease": "ci"}
        for b, why in own.items():
            self.assertEqual(got(b)[:2] + (got(b)[3],), ("in-progress", why, True), b)
        for b in ("hold:step 2 needs another repository to publish its model specs first",
                  "hold:the service key is still untrusted (re-checked at 18:15Z)",
                  "hold:design follows the stack merge; re-check whether the override settles it"):
            self.assertEqual(got(b), ("blocked", "hold", "", False), b)

    def test_state_labels_for_the_text_view(self):
        lab = lambda **kw: ui_board.state_label(dict({"state": "", "why": "", "on": "", "inferred": False}, **kw))  # noqa: E731
        self.assertEqual(lab(state="in-progress", why="ci"), "in progress: ci")
        self.assertEqual(lab(state="in-progress", why="job", inferred=True), "in progress: job?")
        self.assertEqual(lab(state="blocked", why="issue", on="app#9"), "blocked on app#9")
        self.assertEqual(lab(state="blocked", why="upstream", on="lib#3"), "blocked: upstream lib#3")
        self.assertEqual(lab(state="blocked", why="hold"), "blocked: hold")
        self.assertEqual(lab(state="not-queued"), "not queued")

    def test_graph_nodes_edges_and_an_off_board_parent(self):
        with tempfile.TemporaryDirectory() as d:
            b = ui_board.board_graph("sid", DIGEST, ROWS, NOW, hist_path=os.path.join(d, "none.jsonl"))[0]
        nodes = {n["key"]: n for n in b["nodes"]}
        self.assertEqual(set(nodes), {"app#1", "app#2", "app#3", "app#4", "app#5", "app#99"}, "another board's item stays out")
        self.assertTrue(nodes["app#99"]["off_board"])
        self.assertEqual((nodes["app#4"]["state"], nodes["app#4"]["blocker"]), ("blocked", "hold: waits on #3"))
        self.assertEqual((nodes["app#1"]["work"], nodes["app#1"]["state"]), ("unqueued", "not-queued"))
        self.assertEqual(nodes["app#99"]["state"], "not-queued", "an off-board parent no role holds")
        self.assertEqual(sorted((e["from"], e["to"], e["kind"]) for e in b["edges"]),
                         [("app#1", "app#2", "parent"), ("app#1", "app#3", "parent"), ("app#3", "app#4", "blocks"),
                          ("app#99", "app#5", "parent")])
        m = b["metrics"]
        self.assertEqual((m["open"], m["done"], m["total"], m["pct_done"]), (5, 4, 9, 44))
        self.assertEqual((m["in_progress"], m["queued"], m["blocked"], m["blocked_hold"], m["unqueued"], m["dep_blocked"]),
                         (1, 1, 1, 1, 2, 1))
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
        m = ui_board.board_metrics([{"work": "unqueued", "state": "not-queued", "blocked_by": [], "sub_issues": 0}] * 8, 6, hist, NOW)
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

    def test_a_bad_line_is_skipped_and_the_rest_of_the_history_kept(self):
        with tempfile.TemporaryDirectory() as d, patch.object(digest, "data_dir", return_value=d):
            path = digest.history_path("sid")
            good = {"ts": NOW - DAY, "source": SRC, "open": 7, "done": 2}
            with open(path, "w") as fh:
                fh.write(json.dumps(good) + "\n" + '{"ts": 1, "source": "torn' + "\n" + "[1, 2]\n"
                         + json.dumps({"ts": "soon", "source": SRC}) + "\n")
            digest.note_history("sid", DIGEST, now=NOW)
            with open(path) as fh:
                rows = [json.loads(line) for line in fh]
            self.assertEqual([r["ts"] for r in rows], [int(NOW - DAY), int(NOW)], "the good row stays; the bad ones go")
            self.assertFalse([f for f in os.listdir(d) if f.endswith(".tmp")], "no temporary file left behind")

    def test_write_keeps_the_digest_when_the_history_cannot_be_written(self):
        spec = type("Spec", (), {"sid": "sid"})()
        with tempfile.TemporaryDirectory() as d, patch.object(digest, "data_dir", return_value=d), \
                patch.object(digest, "note_history", side_effect=OSError("disk full")):
            path = digest.write(spec, DIGEST)
            self.assertTrue(os.path.exists(path))
        with tempfile.TemporaryDirectory() as d, patch.object(digest, "data_dir", return_value=d), \
                patch.object(digest, "note_history", side_effect=TypeError("a malformed board")):
            self.assertTrue(os.path.exists(digest.write(spec, DIGEST)), "nor for any other error")


if __name__ == "__main__":
    unittest.main()
