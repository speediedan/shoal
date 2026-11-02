"""Cross-host imbalance over compatible demand, with the pool planner's functions faked: no hosts,
probes or network."""
from pathlib import Path
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "shoal"))
import pool  # noqa: E402
import pool_cli  # noqa: E402
import pool_metrics as pm  # noqa: E402


def meta(unit, host, dem, granted=None):
    return {"unit": unit, "lease_granted": granted, "placement": {"host": host, "home": "h1", "demand": dem}}


HOSTS = [{"name": "h1", "vendor": "nvidia"}, {"name": "h2", "vendor": "amd"}]


def compatible(host, dem, home):
    """A fake planner rule: a pin ties a job to its host; a vendor must match the host's GPUs."""
    if dem.get("pin") and dem["pin"] != host["name"]:
        return ["pinned"]
    v = dem.get("vendor") or "any"
    return [] if v == "any" or v == host["vendor"] else ["vendor"]


class Imbalance(unittest.TestCase):
    def setUp(self):
        pm._last.update(t=None, text="")

    def run_with(self, metas, states):
        with mock.patch.object(pool_cli, "placed_metas", lambda sid="": metas), \
                mock.patch.object(pool_cli, "job_state", lambda m: states[m["unit"]]), \
                mock.patch.object(pool_cli, "gather", lambda cache_s=20: {}), \
                mock.patch.object(pool_cli, "capacities", lambda probes, now, max_age_s=0: HOSTS), \
                mock.patch.object(pool, "compatible", compatible), \
                mock.patch("config.server", lambda: "h1"):
            return pm.waiting_compatible(1000.0), pm.render(1000.0, "h1")

    def test_only_compatible_queued_demand_counts(self):
        metas = [meta("any-gpu", "h1", {"gpus": 1, "vendor": "any"}),        # either host: counts for h2
                 meta("cuda", "h1", {"gpus": 1, "vendor": "nvidia"}),        # h2 is AMD: never counts
                 meta("pinned", "h1", {"gpus": 1, "vendor": "any", "pin": "h1"}),
                 meta("cpu", "h1", {"gpus": 0, "cpu_heavy": True}),          # cpu-heavy key
                 meta("plain", "h1", {"gpus": 0}),                           # takes no lease
                 meta("running", "h1", {"gpus": 1, "vendor": "any"}),
                 meta("granted", "h1", {"gpus": 1, "vendor": "any"}, granted="2026-11-02T00:00:00Z")]
        states = {m["unit"]: "queued" for m in metas}
        states["running"] = "running"
        (queued, counts), text = self.run_with(metas, states)
        self.assertEqual(queued, {"h1": 5})
        self.assertEqual(counts, {("h1", "h2", "gpu"): 1, ("h1", "h2", "cpu-heavy"): 1})
        self.assertIn('shoal_pool_waiting_compatible{host="h1",target="h2",key="gpu"} 1', text)
        self.assertIn('shoal_pool_placed_queued{host="h1"} 5', text)

    def test_nothing_queued_is_a_measured_zero_and_no_probe(self):
        with mock.patch.object(pool_cli, "gather", side_effect=AssertionError("no probe when nothing waits")):
            (_q, counts), text = self.run_with([], {})
        self.assertEqual(counts, {})
        self.assertIn('shoal_pool_placed_queued{host="h1"} 0', text)

    def test_recomputes_at_most_once_a_minute(self):
        self.run_with([], {})
        with mock.patch.object(pm, "waiting_compatible", side_effect=AssertionError("cached")):
            self.assertIn("shoal_pool_placed_queued", pm.render(1030.0, "h1"))


if __name__ == "__main__":
    unittest.main()
