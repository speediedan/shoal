"""A Claude session whose directory is gone: shoal restores the directory as a detached worktree of the shoal's dir and
resumes there, or fails naming the missing directory (2026-11-21: fts-core deleted the worktree its session started in,
and every wake failed with "Workspace not trusted ... could not be resolved on disk")."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

PACKAGE = Path(__file__).resolve().parents[1] / "shoal"
sys.path[:0] = [str(PACKAGE)]
import backends  # noqa: E402


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True).stdout.strip()


class Case(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="resume-cwd-"))
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-q", "-b", "main")
        git(self.repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "c0")
        self.trees = self.tmp / "worktrees"
        self.trees.mkdir()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


class RestoreSessionDir(Case):
    def test_a_deleted_worktree_comes_back_detached_at_the_anchor_head(self):
        path = self.trees / "role-140"
        git(self.repo, "worktree", "add", "-q", "-b", "feature/140", str(path))
        shutil.rmtree(path)  # removed without `git worktree remove`: its entry still holds the path
        sha, why = backends.restore_session_dir(str(path), str(self.repo))
        self.assertEqual(why, "")
        self.assertEqual(sha, git(self.repo, "rev-parse", "HEAD")[:8])
        self.assertTrue(path.is_dir())
        self.assertEqual(git(path, "rev-parse", "--abbrev-ref", "HEAD"), "HEAD")  # detached: no branch checked out
        self.assertIn("feature/140", git(self.repo, "branch", "--list", "feature/140"))  # the branch is untouched

    def test_an_existing_directory_is_left_alone(self):
        path = self.trees / "here"
        path.mkdir()
        self.assertEqual(backends.restore_session_dir(str(path), str(self.repo)), ("", "nothing to restore"))

    def test_reasons_when_it_cannot_be_restored(self):
        gone = self.trees / "gone"
        self.assertIn("no shoal dir", backends.restore_session_dir(str(gone), "")[1])
        self.assertIn("parent", backends.restore_session_dir(str(self.tmp / "nope" / "deeper"), str(self.repo))[1])
        plain = self.tmp / "plain"
        plain.mkdir()
        self.assertIn("not a git repository", backends.restore_session_dir(str(gone), str(plain))[1])
        self.assertFalse(gone.exists())


class ResumeFromAMissingDirectory(Case):
    def setUp(self):
        super().setUp()
        self.log = self.tmp / "claude.log"
        stub = self.tmp / "claude"
        stub.write_text(f'#!/bin/bash\necho "$PWD" > {self.log}\nprintf "%s\\n" "$@" >> {self.log}\n')
        stub.chmod(0o755)
        self.patches = [patch.object(backends, "tool_which", lambda name: str(stub)),
                        patch.object(backends, "detached", lambda argv: argv),
                        patch.object(backends, "claude_env", lambda account="": dict(os.environ)),
                        patch.object(backends, "fake_sessions", lambda: None)]
        for p in self.patches:
            p.start()
        self.claude = backends.ClaudeBackend.__new__(backends.ClaudeBackend)

    def tearDown(self):
        for p in self.patches:
            p.stop()
        super().tearDown()

    def test_the_directory_is_restored_and_the_resume_runs_there_with_a_note(self):
        path = self.trees / "fts-core-140"
        out = self.claude._resume({"cwd": str(path)}, "sess-1", "wake text", anchor=str(self.repo))
        self.assertTrue(out.startswith("resume (restored missing directory"), out)
        ran_in, *argv = self.log.read_text().splitlines()
        self.assertEqual(os.path.realpath(ran_in), os.path.realpath(path))
        self.assertEqual(argv[:3], ["--bg", "--resume", "sess-1"])
        self.assertIn("placeholder", argv[3])
        self.assertTrue(argv[3].endswith("wake text"))

    def test_without_an_anchor_the_resume_fails_naming_the_directory(self):
        path = self.trees / "fts-core-140"
        out = self.claude._resume({"cwd": str(path)}, "sess-1", "wake text", anchor="")
        self.assertTrue(out.startswith("FAILED (session directory"), out)
        self.assertIn(str(path), out)
        self.assertFalse(self.log.exists())  # nothing was run

    def test_a_present_directory_resumes_as_before(self):
        path = self.trees / "live"
        path.mkdir()
        self.assertEqual(self.claude._resume({"cwd": str(path)}, "sess-1", "t", anchor=str(self.repo)), "resume")
        self.assertEqual(self.log.read_text().splitlines()[-1], "t")


if __name__ == "__main__":
    unittest.main()
