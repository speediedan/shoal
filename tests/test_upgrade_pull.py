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

print("upgrade --pull: current, behind, dirty, detached and not a clone ok")
