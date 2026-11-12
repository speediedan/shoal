#!/usr/bin/env python3
"""`shoal upgrade --pull` fast-forwards the checkout shoal runs from: current, behind, dirty, diverged, no upstream."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "shoal"))
import upgrade  # noqa: E402

ENV = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1", GIT_AUTHOR_NAME="t",
           GIT_AUTHOR_EMAIL="t@example.invalid", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.invalid")


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, env=ENV, capture_output=True, text=True, check=True).stdout.strip()


with tempfile.TemporaryDirectory(prefix="shoal-pull-") as tmp, patch.dict(os.environ, ENV):
    up, a, b = Path(tmp, "up.git"), Path(tmp, "a"), Path(tmp, "b")
    git(tmp, "init", "-q", "--bare", "-b", "main", str(up))
    git(tmp, "clone", "-q", str(up), str(a))
    (a / "shoal").mkdir()
    (a / "shoal" / "x.py").write_text("x = 1\n")
    git(a, "add", ".")
    git(a, "commit", "-qm", "one")
    git(a, "push", "-q", "origin", "HEAD:main")
    git(tmp, "clone", "-q", str(up), str(b))
    with patch.object(upgrade, "HERE", str(b / "shoal")):
        rc, what, moved = upgrade.pull_here()
        assert rc == 0 and not moved and "already at" in what, what
        (a / "shoal" / "x.py").write_text("x = 2\n")
        git(a, "commit", "-qam", "two")
        git(a, "push", "-q", "origin", "HEAD:main")
        rc, what, moved = upgrade.pull_here(dry_run=True)
        assert rc == 0 and not moved and "1 commit(s) behind" in what, what
        rc, what, moved = upgrade.pull_here()
        assert rc == 0 and moved and "->" in what and (b / "shoal" / "x.py").read_text() == "x = 2\n", what
        (b / "shoal" / "x.py").write_text("x = 3\n")
        rc, what, _ = upgrade.pull_here()
        assert rc == 1 and "uncommitted" in what, what
        git(b, "checkout", "-q", "--", ".")
        git(b, "checkout", "-q", "--detach")
        rc, what, _ = upgrade.pull_here()
        assert rc == 1 and "no upstream" in what, what
    with patch.object(upgrade, "HERE", tmp):
        rc, what, _ = upgrade.pull_here()
        assert rc == 2 and "not a git checkout" in what, what

# both UI feeds are restart candidates (the graph feed was missed: it ran a day-old ui_graph through three upgrades),
# only the active ones restart, and `--pull-only` restarts them on a host whose checkout moved
import types  # noqa: E402

assert upgrade.UI_FEED_UNITS == ("shoal-ui-feed.service", "shoal-graph-feed.service")
assert upgrade.classify(["shoal/ui/ui_graph.py"])[upgrade.UI_KEY] == ["ui_graph.py"]
calls = []


def fake_run(argv, **_kw):
    calls.append(argv)
    active = argv[2] == "is-active" and argv[3] == "shoal-graph-feed.service"
    return types.SimpleNamespace(returncode=0, stdout="active\n" if active else "inactive\n", stderr="")


with patch.object(upgrade.subprocess, "run", fake_run):
    assert upgrade.ui_feeds_active() == ["shoal-graph-feed.service"]
    calls.clear()
    with patch.object(upgrade, "pull_here", return_value=(0, "pulled", True)):
        assert upgrade.cmd_upgrade(types.SimpleNamespace(pull_only=True, dry_run=False)) == 0
    assert ["systemctl", "--user", "restart", "shoal-graph-feed.service"] in calls, calls
    assert not any(c[2] == "restart" and c[3] == "shoal-ui-feed.service" for c in calls), "an inactive feed stays"
    calls.clear()
    with patch.object(upgrade, "pull_here", return_value=(0, "already at", False)):
        upgrade.cmd_upgrade(types.SimpleNamespace(pull_only=True, dry_run=False))
    assert not any(c[2] == "restart" for c in calls), "nothing moved: nothing restarts"

print("upgrade --pull: current, behind, dirty, detached and not a clone ok; UI feeds restart where code moved")
