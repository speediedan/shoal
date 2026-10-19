"""Hermetic regression controls for account meters, not model token counters."""
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "shoal"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "shoal/ui"))
import subscription_usage as u

FUTURE = 2000000000


def codex(percent=32):
    return {"rateLimits": {"planType": "plus", "primary": {
        "usedPercent": percent, "windowDurationMins": 300, "resetsAt": FUTURE}},
        "api_key": "secret-must-not-land", "accountId": "private-account"}


class UsageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="shoal-usage-test-")
        self.env = patch.dict(os.environ, {"XDG_RUNTIME_DIR": self.tmp.name})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_normalizes_all_providers_and_retains_real_zero(self):
        c = u.normalize("codex", codex())[0]
        self.assertEqual(c["windows"][0]["name"], "5h")
        self.assertEqual(c["windows"][0]["used_percent"], 32)
        cl = u.normalize("claude", {"five_hour": {"utilization": 0, "resets_at": None},
                                   "seven_day": {"utilization": 85, "resets_at": "2030-01-01T00:00:00Z"}})[0]
        self.assertEqual(cl["windows"][0]["used_percent"], 0)
        self.assertEqual(cl["windows"][1]["name"], "week")
        m = u.normalize("muse", {"is_subs_active": True, "subs_tier_name": "Power",
            "subs_usage": {"window": {"used_percent": 101, "resets_at": FUTURE, "window_duration_mins": 300},
                           "weekly": {"used_percent": 55, "resets_at": FUTURE}}})[0]
        self.assertEqual(m["windows"][0]["used_percent"], 101)
        go = u.normalize("opencode-go", {"usage": {
            "rolling": {"usagePercent": .5, "resetAt": FUTURE},
            "weekly": {"used": 3, "limit": 10, "resetAt": FUTURE},
            "monthly": {"usedPercent": 40, "resetsAt": FUTURE}}})[0]
        self.assertEqual([w["used_percent"] for w in go["windows"]], [.5, 30, 40])

    def test_codex_buckets_and_server_denial(self):
        d = codex()
        d["rateLimitsByLimitId"] = {"shared": d["rateLimits"], "other": {
            "primary": {"usedPercent": 5, "windowDurationMins": 60}}}
        d["ordinaryUsageAllowed"] = False
        rows = u.normalize("codex", d)
        self.assertEqual([r["bucket"] for r in rows], ["shared", "other"])
        self.assertTrue(all(r["blocked"] for r in rows))
        self.assertEqual(u.severity({"stale": False}, rows[1]), "critical")

    def test_partial_payload_never_invents_a_window(self):
        rows = u.normalize("claude", {"five_hour": {"utilization": 10}, "seven_day": None})
        self.assertEqual(len(rows[0]["windows"]), 1)
        for provider, data in [("codex", {}), ("claude", {}), ("opencode-go", {}),
                               ("muse", {"is_subs_active": True, "subs_usage": None})]:
            with self.subTest(provider=provider), self.assertRaises(u.UsageError):
                u.normalize(provider, data)

    def test_status_line_retains_denial_without_a_meter_or_in_another_bucket(self):
        rows = u.normalize("codex", {"ordinaryUsageAllowed": False})
        self.assertEqual("codex: LIMIT", u.summary({"accounts": [{"provider": "codex", "rows": rows}]}))
        d = codex()
        d["rateLimitsByLimitId"] = {"normal": d["rateLimits"], "denied": {
            "spendControlReached": True, "primary": {"usedPercent": 5}}}
        rows = u.normalize("codex", d)
        line = u.summary({"accounts": [{"provider": "codex", "rows": rows}]})
        self.assertTrue(line.startswith("codex: 32%/") and line.endswith(" LIMIT"), line)

    def test_bad_percent_is_not_a_green_meter(self):
        for value in [None, "0", True, -1, float("nan"), float("inf")]:
            with self.subTest(value=value), self.assertRaises(u.UsageError):
                u.normalize("codex", codex(value))

    def test_unsupported_extra_spend_and_model_weekly_are_distinct(self):
        rows = u.normalize("claude", {"five_hour": {"utilization": 7},
            "seven_day_opus": {"utilization": 91},
            "extra_usage": {"is_enabled": False, "utilization": 0}})
        self.assertEqual(rows[1]["bucket"], "opus")
        self.assertEqual(len(rows[0]["windows"]), 1)
        rows = u.normalize("claude", {"extra_usage": {"is_enabled": True, "utilization": 25}})
        self.assertEqual(rows[0]["windows"][0]["name"], "extra month")

    def test_shared_cache_deduplicates_and_never_persists_raw_response(self):
        calls = []
        def fetch(p):
            calls.append(p)
            return codex()
        u.collect(["codex", "codex"], fetcher=fetch, now=1000)
        u.collect(["codex"], fetcher=fetch, now=1100)
        self.assertEqual(calls, ["codex"])
        u.collect(["codex"], fetcher=fetch, now=1300)
        self.assertEqual(calls, ["codex", "codex"])
        raw = u.cache_path().read_text()
        self.assertNotIn("secret-must-not-land", raw)
        self.assertNotIn("private-account", raw)
        self.assertEqual(u.cache_path().stat().st_mode & 0o777, 0o600)

    def test_failure_keeps_last_measurement_and_backs_off(self):
        u.collect(["codex"], fetcher=lambda _: codex(), now=1000)
        def fail(_):
            raise RuntimeError("secret-in-exception")
        d = u.collect(["codex"], fetcher=fail, now=1300)
        a = d["accounts"]["codex"]
        self.assertEqual(a["observed_at"], 1000)
        self.assertEqual(a["rows"][0]["windows"][0]["used_percent"], 32)
        self.assertNotIn("secret-in-exception", u.cache_path().read_text())
        self.assertTrue(u.snapshot(["codex"], now=1301)["accounts"][0]["stale"])
        d = u.collect(["codex"], fetcher=fail, now=1600)
        self.assertEqual(d["accounts"]["codex"]["next_poll_at"], 2200)

    def test_host_lock_makes_second_collector_a_reader(self):
        path = u.cache_path()
        path.parent.mkdir(parents=True)
        with open(str(path) + ".lock", "w") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            def forbidden(_):
                self.fail("second collector reached the network")
            d = u.collect(["codex"], fetcher=forbidden, force=True)
            self.assertEqual(d["accounts"], {})

    def test_fresh_cache_and_expired_reset_are_never_inferred_to_zero(self):
        u.collect(["codex"], fetcher=lambda _: codex(100), now=time.time())
        doc = u.snapshot(["codex"])
        w = doc["accounts"][0]["rows"][0]["windows"][0]
        w["resets_at"] = time.time() - 1
        rendered = u.render(doc)
        self.assertIn("~100%", rendered)
        self.assertIn("refresh due", rendered)
        self.assertRegex(u.summary(doc), r"^codex~: 100%/")  # past its reset: marked, never read as fresh

    def test_ui_readers_are_cache_only_and_unknown_is_visible(self):
        import ui_status
        with patch.object(u, "fetch", side_effect=AssertionError("reader did network")):
            doc = u.snapshot(["muse", "opencode-go"])
            rendered = ui_status.render_text({"shoals": [], "usage": doc})
            line = ui_status.render_line({"shoals": [], "usage": doc})
        self.assertIn("? unavailable", rendered)
        self.assertIn("muse: not collected yet", rendered)
        self.assertIn("opencode: ?", line)
        self.assertNotIn("0%", rendered)

    def test_colours_and_local_zone(self):
        a = {"provider": "codex", "stale": False, "rows": u.normalize("codex", codex(70))}
        with patch.object(u.config, "timezone", return_value="America/Los_Angeles"):
            rendered = u.render({"accounts": [a]}, color=True)
        self.assertIn("\x1b[33m", rendered)
        self.assertIn("America/Los_Angeles", rendered)
        a["rows"][0]["windows"][0]["used_percent"] = 90
        self.assertIn("\x1b[31m", u.render({"accounts": [a]}, color=True))
        self.assertRegex(rendered, r"week\s+\| month")  # the missing windows read as --, never as 0
        self.assertIn("| --   | --    |", rendered)

    def test_status_line_is_percent_numbers_only(self):
        now = time.time()
        w = lambda n, p: {"name": n, "used_percent": p, "resets_at": now + 3600}  # noqa: E731
        doc = {"accounts": [
            {"provider": "codex", "stale": False, "rows": [{"bucket": "codex", "windows": [w("5h", 4.2), w("week", 22.6)]}]},
            {"provider": "claude", "stale": False, "rows": [{"bucket": "claude", "windows": [w("5h", 10), w("week", 87)]}]},
            {"provider": "opencode-go", "stale": False, "rows": [{"bucket": "opencode-go", "windows": [w("5h", 4), w("week", 10), w("month", 13)]}]},
            {"provider": "muse", "stale": True, "rows": [{"bucket": "muse", "windows": [w("5h", 7)]}]}]}
        self.assertEqual(u.summary(doc), "codex: 4%/23%/- | claude: 10%/87%/- | opencode: 4%/10%/13% | muse~: 7%/-/-")
        self.assertNotIn("@", u.summary(doc))  # no reset times in the status bar

    def test_table_puts_all_windows_of_a_provider_on_one_line(self):
        now = time.time()
        w = lambda n, p, off: {"name": n, "used_percent": p, "resets_at": now + off}  # noqa: E731
        doc = {"accounts": [
            {"provider": "codex", "stale": False, "age_s": 240, "rows": [{"bucket": "codex", "windows": [w("5h", 10, 3600), w("week", 23, 90000)]}]},
            {"provider": "opencode-go", "stale": False, "age_s": 7500,
             "rows": [{"bucket": "opencode-go", "windows": [w("5h", 0, 3600), w("week", 0, 90000), w("month", 6, 900000)]}]}]}
        out = u.render(doc, width=200, color=False)
        rows = {ln.split("|")[1].strip(): ln for ln in out.splitlines() if ln.startswith("| ") and "provider" not in ln}
        self.assertEqual(set(rows), {"codex", "opencode-go"})
        self.assertEqual(rows["codex"].count("%"), 2)
        self.assertEqual(rows["opencode-go"].count("%"), 3)  # 5h, week and month in one row
        self.assertIn("10%", rows["codex"])
        self.assertIn("2h ago", rows["opencode-go"])
        self.assertEqual(len({len(ln) for ln in out.splitlines() if ln.startswith(("|", "+"))}), 1)  # aligned box

    def test_colour_does_not_change_column_alignment(self):
        import re
        now = time.time()
        doc = {"accounts": [{"provider": "claude", "stale": False, "age_s": 60, "rows": [
            {"bucket": "claude", "windows": [{"name": "5h", "used_percent": 95, "resets_at": now + 600},
                                              {"name": "week", "used_percent": 75, "resets_at": now + 90000}]}]}]}
        plain = u.render(doc, width=200, color=False)
        coloured = re.sub(r"\x1b\[[0-9;]*m", "", u.render(doc, width=200, color=True))
        self.assertEqual(plain, coloured)

    def test_bars_never_read_as_empty_or_full_by_rounding(self):
        glyphs = ("#", "-")
        self.assertEqual(u.bar(0, 8, glyphs), ("", "-" * 8))
        self.assertEqual(u.bar(3, 8, glyphs), ("#", "-" * 7))  # a nonzero reading shows
        self.assertEqual(u.bar(99.6, 8, glyphs), ("#" * 7, "-"))  # not yet at the limit
        self.assertEqual(u.bar(100, 8, glyphs), ("#" * 8, ""))
        self.assertEqual(u.bar(150, 4, glyphs), ("#" * 4, ""))

    def test_table_shrinks_its_bars_then_falls_back_to_lines(self):
        now = time.time()
        doc = {"accounts": [{"provider": "muse", "stale": False, "age_s": 60, "rows": [
            {"bucket": "muse", "windows": [{"name": s, "used_percent": 50, "resets_at": now + 3600} for s in ("5h", "week", "month")]}]}]}
        widths = {}
        for width in (200, 116, 104):
            out = u.render(doc, width=width, color=False)
            self.assertTrue(out.startswith("Subscription usage"))
            widths[width] = max(len(ln) for ln in out.splitlines()[1:])
            self.assertLessEqual(widths[width], width)
        self.assertGreater(widths[200], widths[104])
        narrow = u.render(doc, width=60, color=False)
        self.assertNotIn("+--", narrow)  # too narrow for a table: one line per provider
        self.assertIn("muse: 5h 50%", narrow)

    def test_async_refresh_does_not_block_ui(self):
        gate = threading.Event()
        entered = threading.Event()
        def slow(_, **kwargs):
            entered.set()
            gate.wait(2)
        with patch.object(u, "collect", side_effect=slow):
            started = time.monotonic()
            u.refresh_async(["codex"])
            self.assertLess(time.monotonic() - started, .2)
            self.assertTrue(entered.wait(1))
            u.refresh_async(["codex"])
            gate.set()
            for _ in range(100):
                if not u._RUNNING:
                    break
                time.sleep(.01)
        self.assertFalse(u._RUNNING)

    def test_one_bridge_read_covers_multiple_accounts_and_hides_raw_errors(self):
        command = [sys.executable, "-c", "import json; print(json.dumps({'muse': {'error': 'secret-error'}, "
                   "'opencode-go': {'usage': {'monthly': {'usedPercent': 25, 'resetAt': 2000000000}}}}))"]
        bridge = {"command": command, "providers": ["muse", "opencode-go"]}
        def unavailable(_):
            raise u.UsageError("native quota unavailable")
        with patch.object(u, "read_bridge", wraps=u.read_bridge) as reader:
            d = u.collect(["muse", "opencode-go"], bridge=bridge, fetcher=unavailable, now=1000)
        self.assertEqual(reader.call_count, 1)
        self.assertIn("fallback quota reader unavailable", d["accounts"]["muse"]["error"])
        self.assertNotIn("secret-error", u.cache_path().read_text())
        self.assertEqual(d["accounts"]["opencode-go"]["source"], "fallback reader")
        with patch.object(u, "read_bridge", side_effect=AssertionError("cached bridge was polled")):
            u.collect(["muse", "opencode-go"], bridge=bridge, fetcher=unavailable, now=1010)

    def test_bridge_failure_is_visible_and_never_runs_a_shell(self):
        bridge = {"command": [sys.executable, "-c", "raise SystemExit(2)"], "providers": ["muse"]}
        def unavailable(_):
            raise u.UsageError("native quota unavailable")
        d = u.collect(["muse"], bridge=bridge, fetcher=unavailable, now=1000)
        self.assertIn("fallback quota reader unavailable", d["accounts"]["muse"]["error"])
        self.assertNotIn("rows", d["accounts"]["muse"])

    def test_direct_success_never_reads_optional_fallback(self):
        bridge = {"command": ["unused"], "providers": ["codex", "muse"]}
        with patch.object(u, "read_bridge", side_effect=AssertionError("fallback was called")):
            d = u.collect(["codex"], bridge=bridge, fetcher=lambda _: codex(), now=1000)
        self.assertEqual(d["accounts"]["codex"]["source"], "account API")

    def test_fallback_keeps_its_original_observation_time(self):
        bridge = {"command": ["unused"], "providers": ["codex"]}
        def unavailable(_):
            raise u.UsageError("native quota unavailable")
        for observed in (1000, 5001, "invalid"):
            with patch.object(u, "read_bridge", return_value={"codex": dict(codex(), observed_at=observed)}):
                d = u.collect(["codex"], bridge=bridge, fetcher=unavailable, force=True, now=4000)
            if observed == 1000:
                self.assertEqual(d["accounts"]["codex"]["observed_at"], 1000)
                self.assertTrue(u.snapshot(["codex"], now=4001)["accounts"][0]["stale"])
            else:
                self.assertIn("fallback quota reader unavailable", d["accounts"]["codex"]["error"])

    def test_fallback_gets_only_failed_due_providers_on_stdin(self):
        command = [sys.executable, "-c", "import sys,json; d=json.load(sys.stdin); "
                   "assert d['providers']==['muse']; print('{}')"]
        bridge = {"command": command, "providers": ["codex", "muse"]}
        def native(p):
            if p == "muse":
                raise u.UsageError("quota omitted")
            return codex()
        with patch.object(u, "read_bridge", wraps=u.read_bridge) as reader:
            u.collect(["codex", "muse"], bridge=bridge, fetcher=native, now=1000)
        self.assertEqual(reader.call_args.args[1], ["muse"])

    def test_credentials_are_private_data_and_never_shell_code(self):
        path = Path(self.tmp.name) / "usage.env"
        name = "SHOAL_USAGE_OPENCODE_GO_API_KEY"
        path.write_text("# comment\n" + name + "='$(touch never-run)`literal`'\n")
        path.chmod(0o600)
        with patch.dict(os.environ, {"SHOAL_USAGE_ENV_FILE": str(path)}):
            self.assertEqual(u.credential("opencode-go"), "$(touch never-run)`literal`")
            with patch.dict(os.environ, {name: "environment-override"}):
                self.assertEqual(u.credential("opencode-go"), "environment-override")
            path.chmod(0o644)
            with self.assertRaises(u.UsageError):
                u.credential("opencode-go")
            path.chmod(0o600)
            path.write_text("UNRECOGNIZED_KEY=secret\n")
            with self.assertRaises(u.UsageError):
                u.credential("opencode-go")
            path.unlink()
            path.symlink_to(Path(self.tmp.name) / "absent")
            with self.assertRaises(u.UsageError):
                u.credential("opencode-go")

    def test_api_key_override_bypasses_native_store_and_never_enters_cache(self):
        name = "SHOAL_USAGE_OPENCODE_GO_API_KEY"
        with patch.dict(os.environ, {name: "private-test-key"}), \
                patch.object(u, "go_key", side_effect=AssertionError("native key was read")), \
                patch.object(u, "http_json", return_value={"usage": {"weekly": {"usedPercent": 10}}}) as call:
            u.collect(["opencode-go"], now=1000)
        self.assertEqual(call.call_args.args, ("https://opencode.ai/zen/go/v1/usage", "private-test-key"))
        self.assertNotIn("private-test-key", u.cache_path().read_text())

    def test_reset_display_rounds_up_instead_of_inviting_an_early_restart(self):
        with patch.object(u.config, "timezone", return_value="UTC"):
            self.assertEqual(u.reset_text(1792447199.9), "Oct 19 22:00")

    def test_go_current_endpoint_percent_shape(self):
        rows = u.normalize("opencode-go", {"usage": {
            "rolling": {"status": "ok", "percent": 0, "resetsAt": None},
            "weekly": {"status": "ok", "percent": 12, "resetsAt": "2030-01-01T00:00:00Z"},
            "monthly": {"status": "exceeded", "percent": 101, "resetsAt": "2030-02-01T00:00:00Z"}}})
        self.assertEqual([w["used_percent"] for w in rows[0]["windows"]], [0, 12, 101])
        self.assertEqual(u.severity({"stale": False}, rows[0]), "critical")

    def test_codex_transport_never_starts_a_thread_or_turn(self):
        fake = Path(self.tmp.name) / "codex-fixture"
        methods = Path(self.tmp.name) / "methods"
        fake.write_text("#!/usr/bin/env python3\n" + '''import json,sys,os
for line in sys.stdin:
 d=json.loads(line)
 with open(os.environ['USAGE_METHODS'],'a') as f:f.write(d['method']+'\\n')
 if 'id' not in d:continue
 if d['method']=='initialize':r={}
 elif d['method']=='account/rateLimits/read':r={'rateLimits':{'primary':{'usedPercent':12}}}
 else:sys.exit(9)
 print(json.dumps({'id':d['id'],'result':r}),flush=True)
''')
        fake.chmod(0o700)
        with patch.dict(os.environ, {"SHOAL_CODEX_BIN": str(fake), "USAGE_METHODS": str(methods)}):
            d = u.codex_read()
        self.assertEqual(d["rateLimits"]["primary"]["usedPercent"], 12)
        self.assertEqual(methods.read_text().splitlines(), ["initialize", "initialized", "account/rateLimits/read"])


if __name__ == "__main__":
    unittest.main()
