#!/usr/bin/env python3
"""Real CLI retirement on isolated repositories: trust, ordering, locks, routed workers and partial-failure retry."""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import uuid

SOURCE = Path(__file__).resolve().parents[1] / "shoal"


class Retirement(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="shoal-retire-test-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo, self.ledger = self.root / "repo", self.root / "ledger"
        self.host = self.root / "host.toml"
        self.sid = "retire-test-" + uuid.uuid4().hex[:12]
        self.specfile = self.ledger / "shoals" / self.sid / "shoal.json"
        self.registry = self.root / "xdg/shoal/ui-standalone.json"
        # remove-role prunes incarnations across every harness, and prune lists codex threads through
        # `codex app-server`: point it at a fake with no threads so no test reaches the real binary.
        codex_fake = self.root / "codex-fake"
        codex_fake.write_text("#!" + sys.executable + """
import json, sys
for line in sys.stdin:
    m = json.loads(line)
    if "id" not in m:
        continue
    res = {"data": [], "nextCursor": None} if m.get("method") == "thread/list" else {}
    print(json.dumps({"id": m["id"], "result": res}), flush=True)
""")
        codex_fake.chmod(0o755)
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("SHOAL_")}
        self.env.update(SHOAL_CONFIG=str(self.host), SHOAL_LEDGER_DIR=str(self.ledger),
                        SHOAL_DATA_ROOT=str(self.root / "data"), SHOAL_SERVER="host-a",
                        SHOAL_SPEC=str(self.specfile), SHOAL_RUNTIME_DIR=str(self.root / "runtime"),
                        XDG_CONFIG_HOME=str(self.root / "xdg"), CLAUDE_CONFIG_DIR=str(self.root / "claude"),
                        CLAUDE_JOB_DIR="", CLAUDE_CODE_SESSION_ID="", GIT_CONFIG_GLOBAL=os.devnull,
                        GIT_CONFIG_NOSYSTEM="1", GIT_AUTHOR_NAME="tester", GIT_COMMITTER_NAME="tester",
                        GIT_AUTHOR_EMAIL="tester@example.invalid", GIT_COMMITTER_EMAIL="tester@example.invalid",
                        SHOAL_FAKE_SESSIONS='{"claude": [], "opencode": {}}',
                        SHOAL_CODEX_BIN=str(codex_fake), SHOAL_CODEX_STATE=str(self.root / "codex-state"),
                        SHOAL_ROUTE_CACHE=str(self.root / "routes"),
                        SHOAL_CLI=sys.executable + " " + str(SOURCE / "cli.py"))
        for repo in (self.repo, self.ledger):
            repo.mkdir()
            self.git(repo, "init", "-q", "-b", "main")
            self.git(repo, "commit", "--allow-empty", "-qm", "fixture")
        self.write("lane/plan.md", "# Plan\n")
        self.write("lane/design.md", "Design\n")
        self.write(".shoal/lifecycle.toml", '[hooks]\non_retire = \'"./retire-hook.py" "space arg"\'\n')
        hook = '''#!PYTHON
import fcntl,json,os,subprocess,sys
from pathlib import Path
sys.path.insert(0, SOURCE)
import refs
env={k:os.environ[k] for k in ('SHOAL_ID','SHOAL_ROLE','SHOAL_DOC','SHOAL_ROLE_DIR','SHOAL_HOST')}
assert env['SHOAL_DOC'] not in refs.roots()['roots'], 'hook ran before root removal'
assert sys.argv[1:] == ['space arg']
if env['SHOAL_ID']:
    with open('/tmp/'+env['SHOAL_ID']+'/context-restart-'+env['SHOAL_ROLE']+'.lock','a') as f:
        try: fcntl.flock(f, fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError: pass
        else: raise AssertionError('retirement released its lock before the hook')
with open('.git/hook-calls','a') as f: f.write(json.dumps(env)+'\\n')
mode=Path('.git/hook-mode').read_text() if Path('.git/hook-mode').exists() else 'ok'
if mode in ('partial','archive-partial') and not Path('.git/hook-partial').exists():
    if mode == 'archive-partial':
        Path('archive').mkdir(exist_ok=True)
        subprocess.run(['git','mv','lane','archive/lane'],check=True)
        subprocess.run(['git','commit','-qm','archive fixture','--','lane','archive/lane'],check=True)
    Path('.git/hook-partial').touch()
    sys.exit(17)
Path('.git/hook-complete').touch()
'''.replace("PYTHON", sys.executable).replace("SOURCE", repr(str(SOURCE)))
        self.write("retire-hook.py", hook)
        (self.repo / "retire-hook.py").chmod(0o755)
        self.git(self.repo, "add", "--", "lane/plan.md", "lane/design.md", ".shoal/lifecycle.toml", "retire-hook.py")
        self.git(self.repo, "commit", "-qm", "documents")
        self.configure()
        self.spec = {"shoal": self.sid, "dir": str(self.repo), "home": "host-a",
                     "ledger": {"dir": str(self.ledger), "repo": str(self.ledger)},
                     "roles": [{"slug": "lane", "title": "lane", "ecosystem": "claude", "session": "",
                                "doc": str(self.repo / "lane/plan.md"), "fill": "none"}], "generations": []}
        self.save_spec()
        self.call("ledger-init", "--spec", str(self.specfile))

    def configure(self, trusted=True, extra=""):
        self.host.write_text('server = "host-a"\nledger_dir = ' + json.dumps(str(self.ledger)) +
                             '\n[lifecycle]\ntrusted_repos = ' + json.dumps([str(self.repo)] if trusted else []) +
                             '\n' + extra)

    def write(self, path, text):
        p = self.repo / path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)

    def git(self, repo, *args):
        return subprocess.run(["git", "-C", str(repo), *args], env=self.env, capture_output=True, text=True,
                              check=True).stdout

    def save_spec(self):
        self.specfile.parent.mkdir(parents=True, exist_ok=True)
        self.specfile.write_text(json.dumps(self.spec))
        self.git(self.ledger, "add", "--", str(self.specfile))
        self.git(self.ledger, "commit", "--allow-empty", "-qm", "spec", "--", str(self.specfile))

    def call(self, *args, expect=0):
        r = subprocess.run([sys.executable, str(SOURCE / "cli.py"), *args], cwd=self.repo, env=self.env,
                           capture_output=True, text=True, timeout=90)
        out = r.stdout + r.stderr
        self.assertEqual(r.returncode, expect, out)
        return out

    def events(self, standalone=False):
        p = self.ledger / "shoals" / ("_standalone" if standalone else self.sid) / "ledger/shoal.jsonl"
        return [e for line in p.read_text().splitlines() if (e := json.loads(line)).get("event") == "retire-hook"] if p.exists() else []

    def remove_role(self, *args, expect=0):
        return self.call("remove-role", "--spec", str(self.specfile), "--only", "lane", *args, expect=expect)

    def standalone(self):
        self.spec["roles"] = []
        self.save_spec()
        self.registry.parent.mkdir(parents=True, exist_ok=True)
        self.registry.write_text(json.dumps({"v": 1, "sessions": [{"label": "seat", "session": "abcdef01-0000-4000-8000-000000000000",
            "harness": "claude", "doc": str(self.repo / "lane/plan.md"), "cwd": str(self.repo)}]}))

    def test_group_dry_run_and_success(self):
        before = self.specfile.read_bytes(), self.git(self.ledger, "rev-parse", "HEAD")
        out = self.call("retire", "--spec", str(self.specfile), "--dry-run")
        self.assertIn(str(self.repo / "retire-hook.py"), out)
        self.assertIn('"SHOAL_ROLE_DIR":', out)
        self.assertIn('"space arg"', out)
        self.assertEqual(before, (self.specfile.read_bytes(), self.git(self.ledger, "rev-parse", "HEAD")))
        self.assertFalse((self.repo / ".git/hook-calls").exists())
        self.call("retire", "--spec", str(self.specfile))
        self.assertTrue(json.loads(self.specfile.read_text())["retired_utc"])
        self.assertEqual(self.events()[-1]["status"], "ok")
        self.assertTrue((self.repo / ".git/hook-complete").exists())

    def test_role_success_and_lifetime_lock(self):
        self.remove_role()
        self.assertEqual(json.loads(self.specfile.read_text())["roles"], [])
        self.assertEqual(self.events()[-1]["status"], "ok")

    def test_standalone_success_and_durable_event(self):
        self.standalone()
        self.call("remove-session", "--session", "seat")
        self.assertEqual(json.loads(self.registry.read_text())["sessions"], [])
        self.assertEqual(self.events(True)[-1]["status"], "ok")

    def test_untrusted_keeps_legacy_retire_behavior(self):
        self.configure(False)
        self.spec["roles"][0]["session"] = "abcdef01-0000"
        self.save_spec()
        self.call("retire", "--spec", str(self.specfile))
        self.assertTrue(json.loads(self.specfile.read_text())["roles"][0]["session"])
        self.assertFalse((self.repo / ".git/hook-calls").exists())

    def test_missing_or_nonexecutable_hook_precedes_removal(self):
        for command in ("missing-program", "./retire-hook.py"):
            self.write(".shoal/lifecycle.toml", '[hooks]\non_retire = ' + json.dumps(command))
            (self.repo / "retire-hook.py").chmod(0o644)
            self.assertIn("executable is unavailable", self.remove_role(expect=1))
            self.assertTrue(json.loads(self.specfile.read_text())["roles"])

    def test_dirty_directory_and_other_root_precede_removal(self):
        self.write("lane/scratch.txt", "untracked")
        self.assertIn("directory is dirty", self.remove_role(expect=1))
        (self.repo / "lane/scratch.txt").unlink()
        self.spec["roles"].append({"slug": "other", "doc": str(self.repo / "lane/design.md")})
        self.save_spec()
        self.assertIn("live root", self.remove_role(expect=1))
        self.assertEqual(len(json.loads(self.specfile.read_text())["roles"]), 2)

    def test_symlinked_other_root(self):
        (self.repo / "other").mkdir()
        (self.repo / "other/plan.md").symlink_to("../lane/design.md")
        self.git(self.repo, "add", "--", "other/plan.md")
        self.git(self.repo, "commit", "-qm", "link")
        self.spec["roles"].append({"slug": "other", "doc": str(self.repo / "other/plan.md")})
        self.save_spec()
        self.assertIn("live root", self.remove_role(expect=1))
        self.assertTrue((self.repo / "lane/plan.md").exists())

    def test_group_requires_park_and_no_archive_is_explicit(self):
        self.spec["roles"][0]["session"] = "abcdef01-0000"
        self.save_spec()
        out = self.call("retire", "--spec", str(self.specfile), expect=1)
        self.assertIn("shoal park --spec", out)
        self.assertFalse(json.loads(self.specfile.read_text()).get("retired_utc"))
        self.write(".shoal/lifecycle.toml", '[hooks]\non_retire = "missing-program"')
        self.call("retire", "--spec", str(self.specfile), "--no-archive")
        self.assertEqual(self.events()[-1]["phase"], "skipped")
        self.assertFalse((self.repo / ".git/hook-calls").exists())

    def test_no_archive_on_role_and_standalone_keeps_dirty_work(self):
        self.write("lane/plan.md", "dirty work")
        self.remove_role("--no-archive")
        self.assertEqual((self.repo / "lane/plan.md").read_text(), "dirty work")
        self.assertEqual(self.events()[-1]["phase"], "skipped")
        self.standalone()
        self.call("remove-session", "--session", "seat", "--no-archive")
        self.assertEqual(self.events(True)[-1]["phase"], "skipped")

    def test_partial_failure_is_visible_and_retry_is_idempotent(self):
        (self.repo / ".git/hook-mode").write_text("partial")
        out = self.remove_role(expect=1)
        self.assertIn("removal lane: complete", out)
        self.assertIn("retire hook lane: failed", out)
        self.assertEqual(json.loads(self.specfile.read_text())["roles"], [])
        self.assertEqual(self.events()[-1]["status"], "failed")
        self.assertIn("RETIRE HOOK lane", self.call("show", "--shoal", self.sid))
        self.call("lifecycle", "retire-hook", "--shoal", self.sid, "--role", "lane", "--dry-run")
        self.assertEqual(len((self.repo / ".git/hook-calls").read_text().splitlines()), 1)
        self.call("lifecycle", "retire-hook", "--shoal", self.sid, "--role", "lane")
        self.assertEqual(len((self.repo / ".git/hook-calls").read_text().splitlines()), 2)
        self.assertEqual(self.events()[-1]["status"], "ok")
        self.assertNotIn("RETIRE HOOK", self.call("show", "--shoal", self.sid))

    def test_retry_after_partial_archive_keeps_repository_identity(self):
        (self.repo / ".git/hook-mode").write_text("archive-partial")
        self.remove_role(expect=1)
        self.assertFalse((self.repo / "lane").exists())
        self.call("lifecycle", "retire-hook", "--shoal", self.sid, "--role", "lane")
        self.assertEqual(self.events()[-1]["status"], "ok")
        self.assertTrue((self.repo / "archive/lane/plan.md").exists())

    def test_recheck_after_removal_detects_new_work(self):
        hook = self.ledger / ".git/hooks/pre-commit"
        hook.write_text('#!' + sys.executable + '\nimport json\nfrom pathlib import Path\n' +
                        f'if not json.loads(Path({str(self.specfile)!r}).read_text())["roles"]:\n' +
                        f'    Path({str(self.repo / "lane/new-work")!r}).write_text("new work")\n')
        hook.chmod(0o755)
        self.assertIn("directory is dirty", self.remove_role(expect=1))
        self.assertEqual(json.loads(self.specfile.read_text())["roles"], [])
        self.assertFalse((self.repo / ".git/hook-calls").exists())
        self.assertEqual(self.events()[-1]["status"], "failed")

    def test_recheck_rejects_a_new_symlinked_live_root(self):
        alias = self.repo / "alias.md"
        alias.symlink_to("lane/plan.md")
        self.registry.parent.mkdir(parents=True, exist_ok=True)
        self.registry.write_text('{"v":1,"sessions":[]}')
        hook = self.ledger / ".git/hooks/pre-commit"
        entry = {"v": 1, "sessions": [{"label": "later", "session": "fedcba00", "doc": str(alias)}]}
        hook.write_text('#!' + sys.executable + '\nimport json\nfrom pathlib import Path\n' +
                        f'if not json.loads(Path({str(self.specfile)!r}).read_text())["roles"]:\n' +
                        f'    Path({str(self.registry)!r}).write_text({json.dumps(entry)!r})\n')
        hook.chmod(0o755)
        self.assertIn("live root", self.remove_role(expect=1))
        self.assertEqual(json.loads(self.specfile.read_text())["roles"], [])
        self.assertFalse((self.repo / ".git/hook-calls").exists())

    def test_retry_rechecks_trust(self):
        (self.repo / ".git/hook-mode").write_text("partial")
        self.remove_role(expect=1)
        self.configure(False)
        out = self.call("lifecycle", "retire-hook", "--shoal", self.sid, "--role", "lane", expect=1)
        self.assertIn("disabled or untrusted", out)
        self.assertEqual(len((self.repo / ".git/hook-calls").read_text().splitlines()), 1)
        self.configure()
        self.call("lifecycle", "retire-hook", "--shoal", self.sid, "--role", "lane")

    def test_directory_override_must_contain_doc(self):
        self.spec["roles"][0]["dir"] = "elsewhere"
        self.save_spec()
        self.assertIn("must contain its start doc", self.remove_role(expect=1))

    def test_lock_contention_prevents_teardown(self):
        p = Path("/tmp") / self.sid / "context-restart-lane.lock"
        p.parent.mkdir(exist_ok=True)
        with p.open("a") as guard:
            fcntl.flock(guard, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertIn("restart of lane is running", self.remove_role(expect=1))
        self.assertTrue(json.loads(self.specfile.read_text())["roles"])
        self.assertFalse((self.repo / ".git/hook-calls").exists())

    def test_standalone_lock_and_partial_retry(self):
        self.standalone()
        session = json.loads(self.registry.read_text())["sessions"][0]["session"]
        p = self.root / "runtime" / (hashlib.sha256(("standalone/" + session).encode()).hexdigest() + ".lock")
        p.parent.mkdir(exist_ok=True)
        with p.open("a") as guard:
            fcntl.flock(guard, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.call("remove-session", "--session", "seat", expect=1)
        (self.repo / ".git/hook-mode").write_text("partial")
        self.call("remove-session", "--session", "seat", expect=1)
        self.assertEqual(json.loads(self.registry.read_text())["sessions"], [])
        self.assertIn("RETIRE HOOK seat", self.call("show", "--shoal", self.sid))
        self.call("lifecycle", "retire-hook", "--session", session)
        self.assertEqual(self.events(True)[-1]["status"], "ok")

    def routed(self, reachable=True):
        self.spec["roles"][0]["server"] = "host-b"
        self.save_spec()
        host_b = self.root / "host-b.toml"
        host_b.write_text('[lifecycle]\ntrusted_repos = ' + json.dumps([str(self.repo)]) + '\n')
        stub = self.root / "ssh-stub"
        stub.write_text('#!' + sys.executable + '\n' + '''import os,shlex,subprocess,sys
if sys.argv[-1] == 'true': sys.exit(REACHABLE)
argv=shlex.split(shlex.split(sys.argv[-1])[-1])
env=dict(os.environ,SHOAL_SERVER='host-b',SHOAL_CONFIG=HOST_B,SHOAL_RUNTIME_DIR=REMOTE_RUNTIME)
sys.exit(subprocess.run(argv,env=env).returncode)
'''.replace("REACHABLE", "0" if reachable else "1").replace("HOST_B", repr(str(host_b)))
                        .replace("REMOTE_RUNTIME", repr(str(self.root / "remote-runtime"))))
        stub.chmod(0o755)
        self.env["SHOAL_SSH"] = str(stub)
        self.configure(extra='[hosts.host-b]\nssh = "user@host-b"\nresolve = "dns"\ncli = ' +
                       json.dumps(sys.executable + " " + str(SOURCE / "cli.py")) + '\n')

    def test_unreachable_document_host_precedes_spec_change(self):
        self.routed(False)
        self.assertIn("no reachable route", self.call("retire", "--spec", str(self.specfile), expect=1))
        self.assertFalse(json.loads(self.specfile.read_text()).get("retired_utc"))

    def test_routed_worker_holds_document_host_lock_through_hook(self):
        self.routed()
        self.call("retire", "--spec", str(self.specfile))
        event = self.events()[-1]
        self.assertEqual(event["status"], "ok")
        self.assertEqual(event["hook_env"]["SHOAL_HOST"], "host-b")
        self.assertTrue((self.repo / ".git/hook-complete").exists())

    def test_routed_roots_are_refreshed_after_removal(self):
        self.routed()
        self.spec["roles"][0]["server"] = ""
        self.save_spec()
        self.remove_role()
        self.assertEqual(self.events()[-1]["status"], "ok")


if __name__ == "__main__":
    unittest.main()
