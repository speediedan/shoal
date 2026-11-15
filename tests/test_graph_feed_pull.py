#!/usr/bin/env python3
"""`ledger_pull_min`: the graph feed's pull of a ledger clone behind its remote moves it once, then reports it current
(a host that runs no role has nothing else that pulls). Local bare repository only."""
import subprocess
import sys
import tempfile

T = tempfile.mkdtemp()
import os  # noqa: E402
W = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "shoal")
sys.path[:0] = [W, W + "/ui"]


def g(*a, cwd=None):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *a], cwd=cwd, check=True,
                          capture_output=True, text=True)


g("init", "-q", "--bare", "-b", "main", T + "/r.git")
g("clone", "-q", T + "/r.git", T + "/a")
g("commit", "-q", "--allow-empty", "-m", "one", cwd=T + "/a")
g("push", "-q", "origin", "HEAD:main", cwd=T + "/a")
g("clone", "-q", T + "/r.git", T + "/b")
g("commit", "-q", "--allow-empty", "-m", "two", cwd=T + "/a")
g("push", "-q", "origin", "HEAD:main", cwd=T + "/a")
import ui_graph  # noqa: E402
assert ui_graph.pull_ledger(T + "/b") is True, "a clone behind its remote moves"
assert ui_graph.pull_ledger(T + "/b") is False, "a current clone does not"
print("graph feed pull ok")
