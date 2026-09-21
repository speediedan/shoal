#!/usr/bin/env python3
"""Session backends: one interface per agent ecosystem.

- OpenChamberBackend (opencode sessions): fully scripted create/send/status.
- ClaudeBackend (claude sessions): brief generation + explicit operator launch.
  A Claude session CANNOT be created headless — it needs an interactive
  terminal on the native `claude` binary, and subscription models must never
  be routed through opencode. So restart never silently skips a consumer:
  it writes the brief file (built from the lane plan_state + queue paths)
  and either launches `claude "<brief>"` when a TTY is present or prints the
  exact launch command and records the consumer as PENDING until the operator
  confirms with --consumer-launched. "Needs no prompt" after a HAG restart
  is therefore a spec violation by construction, not a judgment call.
- CodexBackend: stub. Accepts the role shape, raises NotSupported on every
  operation, so adding the real transport later touches this file only.
"""
from __future__ import annotations

import datetime
import os
import re
import shutil
import subprocess

TOOL_PATH = ("/home/speediedan/.nvm/versions/node/v24.13.0/bin:"
             "/home/speediedan/.bun/bin:/usr/bin:/bin")

_ENV = dict(os.environ, PATH=TOOL_PATH + ":" + os.environ.get("PATH", ""))


class UnsupportedError(RuntimeError):
    pass


class Backend:
    ecosystem = "?"

    def create(self, title: str, repo_dir: str, dry_run: bool = False,
               brief: str = "", name: str = "", model: str = "") -> str:
        raise NotImplementedError

    def send(self, session_id: str, repo_dir: str, prompt: str,
             dry_run: bool = False, model: str = "") -> None:
        raise NotImplementedError

    def messages(self, session_id: str, repo_dir: str, limit: int = 20,
                 role: str = "") -> str:
        raise NotImplementedError

    def status(self, session_id: str, repo_dir: str) -> str:
        raise NotImplementedError

    def live_model(self, session_id: str, repo_dir: str) -> str:
        return ""

    def quota_paused(self, session_id: str, repo_dir: str = "") -> str:
        """'' or the quota-reset hint (agent-neutral contract; see heartbeat
        quota-paused klass). Each backend implements its own signal."""
        return ""

    def live_model(self, session_id: str, repo_dir: str) -> str:
        """Provider/model the session actually runs ('' when unexposed).

        Sessions can be switched mid-life at user direction, so the spec's
        recorded model drifts; sync-models reconciles it. Matters at relaunch:
        a relaunched session inherits the RECORDED model, so a drifted record
        relaunches the wrong model."""
        return ""


def _run(*argv: str, timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(list(argv), capture_output=True, text=True,
                          timeout=timeout, env=_ENV)


class OpenChamberBackend(Backend):
    ecosystem = "opencode"

    def create(self, title: str, repo_dir: str, dry_run: bool = False,
               brief: str = "", name: str = "", model: str = "") -> str:
        if dry_run:
            return "ses_DRYRUN_" + re.sub(r"\W+", "_", title)
        r = _run("openchamber", "session", "create", "--dir", repo_dir,
                 "--title", title)
        m = re.search(r"ses_[A-Za-z0-9]+", r.stdout + r.stderr)
        if not m:
            raise RuntimeError(f"session create failed for {title!r}: "
                               f"{(r.stdout + r.stderr).strip()[-500:]}")
        return m.group(0)

    def send(self, session_id: str, repo_dir: str, prompt: str,
             dry_run: bool = False, model: str = "") -> None:
        if dry_run:
            return
        argv = ["openchamber", "session", "send", "--session", session_id,
                "--dir", repo_dir, "--prompt", prompt]
        if model:
            argv += ["--model", model]
        r = subprocess.run(argv, capture_output=True, text=True, timeout=60,
                           env=_ENV)
        if r.returncode != 0:
            raise RuntimeError(f"session send to {session_id} failed: "
                               f"{(r.stdout + r.stderr).strip()[-500:]}")

    def messages(self, session_id: str, repo_dir: str, limit: int = 20,
                 role: str = "") -> str:
        argv = ["openchamber", "session", "messages", "--session", session_id,
                "--dir", repo_dir, "--limit", str(limit)]
        if role:
            argv += ["--role", role]
        return _run(*argv).stdout

    def status(self, session_id: str, repo_dir: str) -> str:
        r = _run("openchamber", "session", "status", "--session", session_id,
                 "--dir", repo_dir)
        return (r.stdout + r.stderr).strip().splitlines()[0] \
            if (r.stdout + r.stderr).strip() else ""

    def live_model(self, session_id: str, repo_dir: str) -> str:
        import json as _json
        r = _run("openchamber", "session", "list", "--dir", repo_dir,
                 "--all", "--json")
        try:
            d = _json.loads(r.stdout)
        except Exception:
            return ""
        sessions = d.get("sessions") if isinstance(d, dict) else d
        for s in sessions or []:
            if s.get("id") == session_id:
                m = s.get("model") or {}
                pid, mid = m.get("providerID", ""), m.get("id", "")
                return f"{pid}/{mid}" if pid and mid else (mid or "")
        return ""

    def quota_paused(self, session_id: str, repo_dir: str = "") -> str:
        """'' or the quota-reset hint. opencode refuses sends at quota: those
        failures land as error message rows, so scan recent rows for
        hard-exhaustion signatures (PROVISIONAL patterns — no live opencode
        quota event has been seen on this host to calibrate against; transient
        429 rate-limits deliberately excluded, they self-resolve). Storage
        layout is v1-bound; re-validate on opencode v2."""
        import sqlite3 as _sqlite3
        try:
            db = os.path.join(os.path.expanduser("~"), ".local", "share",
                              "opencode", "opencode.db")
            if not os.path.isfile(db):
                return ""
            con = _sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=20)
            try:
                cutoff = (datetime.datetime.now(datetime.timezone.utc).timestamp()
                          - 6 * 3600) * 1000
                rows = con.execute(
                    "SELECT data FROM message WHERE session_id=? AND "
                    "time_created >= ? AND (data LIKE '%insufficient_quota%' OR "
                    "data LIKE '%insufficient credits%' OR data LIKE "
                    "'%quota exceeded%' OR data LIKE '%out of credits%' OR "
                    "data LIKE '%payment required%') ORDER BY time_created DESC "
                    "LIMIT 3", (session_id, cutoff)).fetchall()
            finally:
                con.close()
        except Exception:
            return ""
        if not rows:
            return ""
        m = re.search(r"resets? ([^.\n|]{0,40})", rows[0][0], flags=re.I)
        reset = m.group(1).split("\x1b")[0].strip() if m else "reset time unknown"
        return reset or "reset time unknown"


class ClaudeBackend(Backend):
    """Native `claude` binary. Launched headless via `claude --bg`.

    create() runs `claude --bg -n <name> [--model m] '<brief>'` with
    cwd=repo_dir and returns the background id (`claude agents` /
    `attach` / `logs` / `stop` / `rm` take it). No operator terminal needed:
    start/restart launch every ecosystem by default. status() reads
    `claude agents --json`; messages() tails `claude logs` (best-effort).
    send() stays unsupported on purpose: --resume against a RUNNING bg
    session forks a copy instead of injecting (verified 2026-09-19), so
    headless sends would silently fork consumers. Coordinator→consumer
    traffic travels ONLY in the queue file — that is the channel, and the
    reason sends are refused rather than approximated.
    """
    ecosystem = "claude"

    def create(self, title: str, repo_dir: str, dry_run: bool = False,
               brief: str = "", name: str = "", model: str = "") -> str:
        if dry_run:
            return "cc_DRYRUN_" + re.sub(r"\W+", "_", title)
        if not brief:
            raise UnsupportedError("claude create needs the role brief text")
        b = shutil.which("claude") or "claude"
        argv = [b, "--bg", "-n", name or title]
        if model:
            argv += ["--model", model]
        argv += [brief]
        r = subprocess.run(argv, capture_output=True, text=True, timeout=120,
                           cwd=repo_dir, env=_ENV)
        m = re.search(r"backgrounded\s+[·.]\s*([0-9a-f]+)", r.stdout + r.stderr)
        if not m:
            raise RuntimeError(f"claude --bg failed for {title!r}: "
                               f"{(r.stdout + r.stderr).strip()[-500:]}")
        return m.group(1)

    def send(self, session_id: str, repo_dir: str, prompt: str,
             dry_run: bool = False) -> None:
        raise UnsupportedError(
            "no headless send into a live claude session: --resume forks a "
            "copy when the session is running, so sends would fork consumers. "
            "Coordinator-to-consumer traffic goes in the queue file ONLY.")

    def _agents(self) -> list:
        import json as _json
        b = shutil.which("claude") or "claude"
        r = subprocess.run([b, "agents", "--json"], capture_output=True,
                           text=True, timeout=30, env=_ENV)
        try:
            return _json.loads(r.stdout) if r.returncode == 0 else []
        except Exception:
            return []

    def messages(self, session_id: str, repo_dir: str, limit: int = 20,
                 role: str = "") -> str:
        b = shutil.which("claude") or "claude"
        r = subprocess.run([b, "logs", session_id], capture_output=True,
                           text=True, timeout=30, env=_ENV)
        out = re.sub(r"\x1b\[[0-9;?]*[a-zA-Z]", "", r.stdout)
        lines = [ln for ln in out.splitlines() if ln.strip()]
        return "\n".join(lines[-max(limit, 1) * 4:])

    def status(self, session_id: str, repo_dir: str) -> str:
        for e in self._agents():
            sid = str(e.get("sessionId", ""))
            if sid == session_id or sid.startswith(session_id) \
                    or str(e.get("id", "")) == session_id:
                return f"{e.get('name', '?')} state:{e.get('state', '?')}"
        return "not-listed (retired or unknown id)"

    @staticmethod
    def launch_command(brief_file: str) -> str:
        b = shutil.which("claude") or "claude"
        return f'{b} "$(cat {brief_file})"'

    @staticmethod
    def stop(session_id: str) -> str:
        import shutil as _sh
        b = _sh.which("claude") or "claude"
        r = subprocess.run([b, "stop", session_id], capture_output=True,
                           text=True, timeout=30, env=_ENV)
        return (r.stdout + r.stderr).strip()[-200:]

    def quota_paused(self, session_id: str, repo_dir: str = "") -> str:
        """Terminal-log limit signature (zero model cost). Verified 2026-09-21:
        'reached your Fable limit' present while quota-dead, absent after the
        opus switch on the same session."""
        b = shutil.which("claude")
        if not b:
            return ""
        try:
            r = subprocess.run([b, "logs", session_id], capture_output=True,
                               text=True, timeout=60, env=_ENV)
        except Exception:
            return ""
        tail = r.stdout[-6000:]
        if not re.search(r"reached your .{0,30} limit|used \d+% of your .{0,30} limit",
                         tail, flags=re.I):
            return ""
        m = re.search(r"resets? ([^.\n|]{0,40})", r.stdout, flags=re.I)
        reset = m.group(1).strip() if m else "reset time unknown"
        return reset.split("\x1b")[0].strip() or "reset time unknown"

    @staticmethod
    def probe_sockets() -> str:
        import glob
        uid = os.getuid()
        socks = glob.glob(f"/run/user/{uid}/cc-socks/*")
        return f"{len(socks)} socket(s) in /run/user/{uid}/cc-socks/"

    @staticmethod
    def probe_turn(model: str = "claude-haiku-4-5-20251001") -> str:
        b = shutil.which("claude") or "claude"
        r = subprocess.run(
            [b, "-p", "Reply with exactly: HAG-PROBE-OK (connectivity probe, "
                      "no action)", "--model", model, "--output-format", "text"],
            capture_output=True, text=True, timeout=300, env=_ENV)
        return (r.stdout + r.stderr).strip()[-200:]


class CodexBackend(Backend):
    """Placeholder for a future `codex` CLI transport."""
    ecosystem = "codex"

    def _no(self, op: str) -> UnsupportedError:
        return UnsupportedError(
            f"codex backend not implemented (op={op}): implement the transport "
            "in backends.py CodexBackend; roles/specs need no changes.")

    def create(self, title: str, repo_dir: str, dry_run: bool = False,
               brief: str = "", name: str = "", model: str = "") -> str:
        raise self._no("create")

    def send(self, session_id: str, repo_dir: str, prompt: str,
             dry_run: bool = False) -> None:
        raise self._no("send")

    def messages(self, session_id: str, repo_dir: str, limit: int = 20,
                 role: str = "") -> str:
        raise self._no("messages")

    def status(self, session_id: str, repo_dir: str) -> str:
        raise self._no("status")


def get(ecosystem: str) -> Backend:
    return {"opencode": OpenChamberBackend(), "claude": ClaudeBackend(),
            "codex": CodexBackend()}[ecosystem]
