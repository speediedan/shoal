"""Hermetic controls for wait-target providers and the validity wake: no tool is ever run."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "shoal"))
import config  # noqa: E402
import health  # noqa: E402
import providers  # noqa: E402


def done(stdout="", stderr="", rc=0):
    return subprocess.CompletedProcess([], rc, stdout, stderr)


class HostConfig(unittest.TestCase):
    """Each test writes its own host config, so no developer's config/host.toml leaks in."""
    toml = ""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="shoal-providers-test-")
        p = os.path.join(self.tmp.name, "host.toml")
        with open(p, "w") as fh:
            fh.write(self.toml)
        env = {k: v for k, v in os.environ.items() if not k.startswith("SHOAL_AZURE")}
        env["SHOAL_CONFIG"] = p
        self.env = patch.dict(os.environ, env, clear=True)
        self.env.start()
        config._CACHE.clear()
        self.which = patch("providers.shutil.which", return_value="/usr/bin/az")
        self.which.start()

    def tearDown(self):
        self.which.stop()
        self.env.stop()
        config._CACHE.clear()
        self.tmp.cleanup()


class AzBuild(HostConfig):
    toml = 'azure_org = "https://dev.azure.com/o"\nazure_projects = ["first", "second"]\n'

    def test_bare_id_tries_each_project(self):
        calls = []

        def run(argv, timeout=30):
            calls.append(argv)
            if argv[argv.index("--project") + 1] == "first":
                return done(stderr="ERROR: The requested build 7 could not be found.", rc=1)
            return done("inProgress\n")
        with patch("providers._run", side_effect=run):
            self.assertEqual(providers.resolve("az-build:7", "r"), (True, "build inProgress (second)"))
        self.assertEqual([c[c.index("--project") + 1] for c in calls], ["first", "second"])
        self.assertIn("https://dev.azure.com/o", calls[0])

    def test_named_project_is_the_only_lookup(self):
        with patch("providers._run", return_value=done("completed\n")) as run:
            self.assertEqual(providers.resolve("az-build:second/7", "r"), (False, "build completed"))
        argv = run.call_args.args[0]
        self.assertEqual(argv[argv.index("--project") + 1], "second")
        self.assertEqual(run.call_count, 1)

    def test_not_found_anywhere_is_unknown(self):
        with patch("providers._run", return_value=done(stderr="build 7 could not be found", rc=1)):
            pending, why = providers.resolve("az-build:7", "r")
        self.assertIsNone(pending)
        self.assertIn("not found in first, second", why)

    def test_timeout_is_unknown_and_names_the_credential(self):
        with patch("providers._run", side_effect=subprocess.TimeoutExpired(["az"], 60)):
            pending, why = providers.resolve("az-build:7", "r")
        self.assertIsNone(pending)
        self.assertIn("no Azure DevOps credential", why)

    def test_other_error_is_unknown_not_a_lookup_miss(self):
        with patch("providers._run", return_value=done(stderr="ERROR: TF400813: not authorized", rc=1)) as run:
            pending, why = providers.resolve("az-build:7", "r")
        self.assertIsNone(pending)
        self.assertIn("TF400813", why)
        self.assertEqual(run.call_count, 1)

    def test_malformed_ref_is_refused(self):
        for bad in ("az-build:", "az-build:abc", "az-build:p/q/7", "az-build:7;rm"):
            self.assertTrue(providers.normalize(bad)[1], bad)
        self.assertEqual(providers.normalize("az-build:finetuning-scheduler/1660"), ("az-build:finetuning-scheduler/1660", ""))


class AzProjectDefault(HostConfig):
    toml = 'azure_project = "only"\n'

    def test_single_project_falls_back_to_azure_project(self):
        self.assertEqual(config.azure_projects(), ["only"])


class LoginShell(HostConfig):
    toml = 'login_shell_tools = ["az"]\n'

    def test_listed_tool_runs_under_a_login_shell(self):
        with patch("providers.subprocess.run", return_value=done()) as run:
            providers._run(["az", "pipelines", "build", "show", "--id", "7"])
            providers._run(["gh", "run", "view", "1"])
        first, second = (c.args[0] for c in run.call_args_list)
        self.assertEqual(first, ["bash", "-lc", "az pipelines build show --id 7"])
        self.assertEqual(second, ["gh", "run", "view", "1"])
        self.assertIs(run.call_args.kwargs["stdin"], subprocess.DEVNULL)


class Validity(unittest.TestCase):
    def test_confirmed_pending_under_discipline_gets_no_validity_wake(self):
        self.assertFalse(health.validity_applies("remote-ci", "az-build:7", True, True))

    def test_unknown_or_undisciplined_still_does(self):
        self.assertTrue(health.validity_applies("remote-ci", "az-build:7", None, True))
        self.assertTrue(health.validity_applies("remote-ci", "az-build:7", True, False))
        self.assertTrue(health.validity_applies("local-ci", "", None, True))
        self.assertTrue(health.validity_applies("queue-empty", "job:x", None, True))

    def test_upstream_never_does(self):
        self.assertFalse(health.validity_applies("upstream", "gh-pr:o/r#1", None, True))


if __name__ == "__main__":
    unittest.main()
