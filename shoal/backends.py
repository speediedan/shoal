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
  confirms with --consumer-launched. "Needs no prompt" after a shoal restart
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
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config  # noqa: E402

TOOL_PATH = config.tool_path()

_ENV = config.tool_env()


def fake_sessions() -> dict | None:
    """Test hook (same family as SHOAL_FAKE_PR / SHOAL_FAKE_REF_STATE): SHOAL_FAKE_SESSIONS=
    '{"claude": [<claude agents --json rows>], "opencode": {"<session id>": "<status line>"}}'
    answers every session-liveness probe without calling a harness CLI. None when unset."""
    raw = config.env("FAKE_SESSIONS")
    if not raw:
        return None
    import json as _json
    return _json.loads(raw)


class UnsupportedError(RuntimeError):
    pass


class Backend:
    ecosystem = "?"

    def create(self, title: str, repo_dir: str, dry_run: bool = False,
               brief: str = "", name: str = "", model: str = "",
               effort: str = "") -> str:
        raise NotImplementedError

    def send(self, session_id: str, repo_dir: str, prompt: str,
             dry_run: bool = False, model: str = "") -> None:
        raise NotImplementedError

    def messages(self, session_id: str, repo_dir: str, limit: int = 20,
                 role: str = "") -> str:
        raise NotImplementedError

    def status(self, session_id: str, repo_dir: str) -> str:
        raise NotImplementedError

    def wake(self, spec, role, text: str, condition: str = "", dry_run: bool = False) -> str:
        """Direct session wake: put `text` in front of `role`'s session so its next turn starts
        on it. One interface, one implementation per ecosystem; the health quorum and the
        sensors wake roles only through this. Returns the channel used, or `no-adapter (..)`."""
        return f"no-adapter ({self.ecosystem} has no session wake)"

    def view_argv(self, session_id: str) -> tuple[list[str], dict[str, str]]:
        """(argv, extra env) of the harness's own interactive client for an EXISTING session, for an
        operator to watch or answer it (`shoal view`, the UI adapters). Never creates, resumes or
        messages a session: viewing is not a wake, and a resume of a running Claude session forks it.
        Extra env may carry a credential the client needs; the caller passes it to the client process
        only and never prints it."""
        raise UnsupportedError(f"{self.ecosystem}: no viewer client")

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


def opencode_server() -> tuple[str, str]:
    """(port, pid) of the opencode server listening on loopback (OpenChamber's), or ("", "").
    SHOAL_FAKE_OPENCODE_SERVER='<port>:<pid>' answers it in tests."""
    fake = config.env("FAKE_OPENCODE_SERVER")
    if fake:
        port, _, pid = fake.partition(":")
        return port, pid
    try:
        r = subprocess.run(["ss", "-tlnpH"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return "", ""
    cands = []
    for line in r.stdout.splitlines():
        if "opencode" in line and "127.0.0.1:" in line:
            m = re.search(r"127\.0\.0\.1:(\d+)", line)
            p = re.search(r"pid=(\d+)", line)
            if m and p:
                cands.append((m.group(1), p.group(1)))
    for port, pid in cands:  # prefer the managed server: the one started with a server password
        try:
            with open(f"/proc/{pid}/environ", "rb") as fh:
                if b"OPENCODE_SERVER_PASSWORD=" in fh.read():
                    return port, pid
        except OSError:
            continue
    return cands[0] if cands else ("", "")


def _run(*argv: str, timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(list(argv), capture_output=True, text=True,
                          timeout=timeout, env=_ENV)


class OpenChamberBackend(Backend):
    ecosystem = "opencode"

    def wake(self, spec, role, text: str, condition: str = "", dry_run: bool = False) -> str:
        """opencode: a direct send to the session, whether or not its watcher is alive."""
        if dry_run:
            return "opencode-send (dry-run)"
        try:
            self.send(role.session, spec.dir, text)
            return "opencode-send"
        except Exception as exc:  # noqa: BLE001
            return f"opencode-send FAILED ({str(exc)[:80]})"

    def view_argv(self, session_id: str) -> tuple[list[str], dict[str, str]]:
        """The stock opencode TUI as a CLIENT of OpenChamber's running server (`--server URL
        --session ID`): no second server, so the one-writer store rule holds. The server's password is
        OPENCODE_SERVER_PASSWORD in that server process's own environment (same uid); the password in
        opencode's service.json is NOT accepted by the live server (measured 2026-10-01)."""
        port, pid = opencode_server()
        if not port:
            raise UnsupportedError("no opencode server listening on loopback (OpenChamber down? never start one here)")
        pw = ""
        try:
            with open(f"/proc/{pid}/environ", "rb") as fh:
                for kv in fh.read().split(b"\0"):
                    if kv.startswith(b"OPENCODE_SERVER_PASSWORD="):
                        pw = kv.split(b"=", 1)[1].decode()
        except OSError:
            pass
        if not pw:
            raise UnsupportedError(f"no server password in the environment of opencode server pid {pid}")
        b = shutil.which("opencode") or os.path.expanduser("~/.opencode/bin/opencode")
        argv = [b, "--server", f"http://127.0.0.1:{port}"] + (["--session", session_id] if session_id else [])
        return argv, {"OPENCODE_PASSWORD": pw}

    def create(self, title: str, repo_dir: str, dry_run: bool = False,
               brief: str = "", name: str = "", model: str = "",
               effort: str = "") -> str:
        # opencode create takes no model/effort flags (model rides on send);
        # effort stays briefed per the agent-neutral design.
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
        fake = fake_sessions()
        if fake is not None:
            return str((fake.get("opencode") or {}).get(session_id, ""))
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
        layout is read through opencode_store (v1 `message`, v2 `session_message`)."""
        try:
            from opencode_store import connect_ro, db_path, message_source
            db = db_path()
            if not os.path.isfile(db):
                return ""
            con = connect_ro(db)
            try:
                _, table, extra = message_source(con)
                cutoff = (datetime.datetime.now(datetime.timezone.utc).timestamp()
                          - 6 * 3600) * 1000
                rows = con.execute(
                    f"SELECT data FROM {table} WHERE session_id=? AND "
                    "time_created >= ? AND (data LIKE '%insufficient_quota%' OR "
                    "data LIKE '%insufficient credits%' OR data LIKE "
                    "'%quota exceeded%' OR data LIKE '%out of credits%' OR "
                    f"data LIKE '%payment required%'){extra} ORDER BY time_created DESC "
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


ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
REVIVED_NOTE = ("Your session was respawned: its worker process had died (the Claude background service "
                "restarted or retired it), so every background command it ran, your ledger-watch included, "
                "is gone. Run `next`, re-arm your watcher, then act on this wake: ")


def parse_bg_id(text: str) -> "re.Match[str] | None":
    """The session id from `claude --bg` output. The CLI colours its output when the
    environment forces colour (FORCE_COLOR), so escape codes are stripped first: without
    that, a live session was reported as a failed launch and never recorded in the spec."""
    m = re.search(r"backgrounded\s+[·.]\s*([0-9a-f]+)", ANSI_RE.sub("", text))
    return m if m else None


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
    reason sends are refused rather than approximated. The health quorum's
    Claude wake adapter is therefore not here: it is a wake-request file the
    role's own ledger-watch delivers, else the wake relay session's
    cross-session message (`wake` below; architecture doc §7). The
    relay is launched by create() like every lane, so it shares their
    permission mode, which cross-session delivery requires.
    """
    ecosystem = "claude"

    def wake(self, spec, role, text: str, condition: str = "", dry_run: bool = False) -> str:
        """Claude: a headless send would fork the session, so a wake goes through the role's own
        ledger-watch (a wake-request file it delivers by exiting) while that watcher is alive,
        else through the wake relay: a small Claude session whose single waiter drains a queue
        and delivers each wake as a cross-session message."""
        import json as _json
        import os as _os
        from health import health_cfg, iso, relay_alive, relay_queue_path, state_dir, wake_path, watcher_alive
        from roles import wake_relay_role
        # features.health.revive: a session whose worker process is gone (the background service
        # restarted under it, or retired it idle) is respawned before it is woken. Its background
        # commands died with it, so an orphaned ledger-watch cannot deliver: the wake goes by relay.
        revive = bool(health_cfg(spec).get("revive"))
        revived = ""
        if revive and role.session and self.session_dead(role.session):
            revived = f"; respawned {role.session[:8]} ({self.respawn(role.session, dry_run)})"
            text = REVIVED_NOTE + text
        alive, _ = watcher_alive(spec.sid, role.slug)
        if alive and not revived:
            if not dry_run:
                _os.makedirs(state_dir(spec.sid), exist_ok=True)
                with open(wake_path(spec.sid, role.slug), "a") as fh:
                    fh.write(_json.dumps({"ts": iso(), "condition": condition, "text": text}) + "\n")
            return "wake-file" + (" (dry-run)" if dry_run else "")
        if wake_relay_role(spec) is None or not (spec.features or {}).get("relay"):
            return "no-adapter (claude watcher dead, wake relay disabled)" + revived
        pending = ""
        if not relay_alive(spec.sid):
            if not revive:
                return "no-adapter (claude watcher dead, wake relay waiter dead)"
            # the quorum relaunches a dead relay (relay-dead), and relay-watch resumes from its
            # cursor, so a wake queued now is delivered once the relay is back; if it never comes
            # back, this condition's own page step still fires
            pending = " (queued; relay down, relaunch pending)"
        if not dry_run:
            with open(relay_queue_path(spec.sid), "a") as fh:
                fh.write(_json.dumps({"ts": iso(), "to": role.tag, "slug": role.slug, "session": role.session,
                                      "condition": condition, "text": text}) + "\n")
        return "wake-relay" + pending + (" (dry-run)" if dry_run else "") + revived

    def create(self, title: str, repo_dir: str, dry_run: bool = False,
               brief: str = "", name: str = "", model: str = "",
               effort: str = "") -> str:
        if dry_run:
            return "cc_DRYRUN_" + re.sub(r"\W+", "_", title)
        if not brief:
            raise UnsupportedError("claude create needs the role brief text")
        b = shutil.which("claude") or "claude"
        argv = [b, "--bg", "-n", name or title]
        if model:
            argv += ["--model", model]
        if effort:
            argv += ["--effort", effort]
        argv += [brief]
        r = subprocess.run(argv, capture_output=True, text=True, timeout=120,
                           cwd=repo_dir, env=_ENV)
        m = parse_bg_id(r.stdout + r.stderr)
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

    def _agents(self, include_stopped: bool = False) -> list:
        import json as _json
        fake = fake_sessions()
        if fake is not None:
            return list(fake.get("claude") or [])
        b = shutil.which("claude") or "claude"
        r = subprocess.run([b, "agents", "--json"] + (["--all"] if include_stopped else []),
                           capture_output=True, text=True, timeout=30, env=_ENV)
        try:
            return _json.loads(r.stdout) if r.returncode == 0 else []
        except Exception:
            return []

    def _row(self, session_id: str, include_stopped: bool = False) -> dict | None:
        for e in self._agents(include_stopped):
            sid = str(e.get("sessionId", ""))
            if sid == session_id or sid.startswith(session_id) or str(e.get("id", "")) == session_id[:8]:
                return e
        return None

    def session_dead(self, session_id: str) -> bool | None:
        """True when the session is listed but has no worker process. Plain `claude agents --json`
        omits such a session entirely, so its absence there reads as "unknown", not "dead";
        `--all` lists it with no `pid` (measured 2026-10-08: after a failed background-service
        self-respawn every worker was reaped and listed `state: stopped` with no pid; `claude stop`
        leaves `state: done` with no pid). None when the session is not listed at all."""
        e = self._row(session_id, include_stopped=True)
        return None if e is None else not e.get("pid")

    def respawn(self, session_id: str, dry_run: bool = False) -> str:
        """`claude respawn <id>`: a new worker on the same conversation (verified 2026-10-08 on a
        stopped session: pid back, conversation kept). The session idles at its prompt until
        woken, so the caller still delivers a wake."""
        if dry_run:
            return "dry-run"
        if fake_sessions() is not None:
            return "fake"
        e = self._row(session_id, include_stopped=True) or {}
        b = shutil.which("claude") or "claude"
        r = subprocess.run([b, "respawn", str(e.get("id") or session_id[:8])], capture_output=True,
                           text=True, timeout=120, env=_ENV)
        out = ANSI_RE.sub("", r.stdout + r.stderr).strip().splitlines()
        return (out[-1] if out else "respawned") if r.returncode == 0 else f"FAILED rc={r.returncode}: {out[-1] if out else ''}"

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

    def view_argv(self, session_id: str) -> tuple[list[str], dict[str, str]]:
        """`claude attach <id>`: the live background session in this terminal, ownership unchanged.
        Detaching (or closing the terminal) leaves the session running. Attach is exclusive: a second
        attach fails while another terminal holds it."""
        return [shutil.which("claude") or "claude", "attach", session_id], {}

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
    def probe_turn(model: str = "claude-haiku-5-5") -> str:
        b = shutil.which("claude") or "claude"
        r = subprocess.run(
            [b, "-p", "Reply with exactly: SHOAL-PROBE-OK (connectivity probe, "
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
               brief: str = "", name: str = "", model: str = "",
               effort: str = "") -> str:
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
