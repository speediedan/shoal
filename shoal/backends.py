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
- MuseBackend (Muse Code sessions): one `muse exec --session-id` process per wake, durable sessions,
  no daemon (shoal/muse.py has the design). Shoal itself runs the role's persistent watcher.
- CodexBackend (Codex CLI threads): the same shape as Muse, one `codex exec` (resume) process per wake
  (shoal/codex_harness.py has the design; the thread id is minted by Codex on the first turn).
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


def tool_which(name: str) -> str | None:
    """A harness binary on this process's PATH, else on the host's tool path (`tool_path`): a command run
    over ssh gets a login shell's PATH, which on a host with an nvm install lacks `claude` (measured
    2026-10-20: a remote `shoal view` failed with "No such file or directory: 'claude'")."""
    return shutil.which(name) or shutil.which(name, path=_ENV.get("PATH", ""))


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

    def sessions(self) -> list[dict]:
        """Discover pickable sessions without a turn or attach. Rows carry session, label,
        cwd and state, plus optional job/model/effort. Unsupported discovery returns no rows.
        A future ACP backend can implement this hook without changing the picker menu."""
        return []

    def create(self, title: str, repo_dir: str, dry_run: bool = False,
               brief: str = "", name: str = "", model: str = "",
               effort: str = "") -> str:
        raise NotImplementedError

    def reset(self, session: str, prompt: str, label: str = "", cwd: str = "", dry_run: bool = False) -> dict:
        """Start a NEW conversation in the same seat with `prompt` as its first message (a handover from a start doc;
        docs/handover.md), retire the old one, and return {"session", "job", "did"} for the caller to record. The
        concept is ecosystem neutral; each harness implements it its own way, and ACP's `session/new` plus
        `session/prompt` is the target form. A harness without one raises UnsupportedError: the handover then stops
        after its advise step and a reset by hand stays possible."""
        raise UnsupportedError(f"{self.ecosystem}: no reset yet (a new session from the start doc, by hand)")

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

    def view_argv(self, session_id: str, model: str = "", effort: str = "") -> tuple[list[str], dict[str, str]]:
        """(argv, extra env) of the harness's own interactive client for an EXISTING session, for an
        operator to watch or answer it (`shoal view`, the UI adapters). Never creates, resumes or
        messages a session: viewing is not a wake, and a resume of a running Claude session forks it.
        Extra env may carry a credential the client needs; the caller passes it to the client process
        only and never prints it. `model` and `effort` are the seat's own (spec role or registry entry)
        for clients that take them per run; harnesses whose client ignores them may too."""
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

    def sessions(self) -> list[dict]:
        import json
        r = _run("openchamber", "session", "list", "--limit", "15", "--with-status", "--json", timeout=30)
        if r.returncode:
            raise RuntimeError(f"session list exited {r.returncode}")
        return [{"session": s["id"], "label": str(s.get("title") or s["id"][:12])[:40],
                 "state": (s.get("status") or {}).get("type"),
                 "cwd": (s.get("location") or {}).get("directory")}
                for s in json.loads(r.stdout or "{}").get("sessions", []) if s.get("id")]

    def wake(self, spec, role, text: str, condition: str = "", dry_run: bool = False) -> str:
        """opencode: a direct send to the session, whether or not its watcher is alive."""
        if dry_run:
            return "opencode-send (dry-run)"
        try:
            self.send(role.session, spec.dir, text)
            return "opencode-send"
        except Exception as exc:  # noqa: BLE001
            return f"opencode-send FAILED ({str(exc)[:80]})"

    def view_argv(self, session_id: str, model: str = "", effort: str = "") -> tuple[list[str], dict[str, str]]:
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
        b = tool_which("opencode") or os.path.expanduser("~/.opencode/bin/opencode")
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
RESUMED_NOTE = ("Your session was resumed for this wake: its worker had been retired (idle) or had died, so "
                "background commands it ran inside the session are gone. A detached ledger-watch survives that; "
                "`next` says whether yours is armed. Act on this wake: ")
# `claude --bg --resume` right after `claude stop` still finds the session running and forks a copy;
# measured 2026-10-20: forked with the pid gone for 1 s, continued in place 4 s later
RESUME_SETTLE_S = 6.0
# how long a reset watches the old jobs for a respawned worker (2026-10-24: one came back within 2 s of a removal)
RETIRE_VERIFY_S = 20.0
COPY_RE = re.compile(r"started a copy as ([0-9a-f]{8})")


# ---- Claude identity -----------------------------------------------------------------------------------------
# A Claude background session has a stable JOB id (8 hex, what `claude attach`, `stop` and `respawn` take) and a
# CONVERSATION id that changes on `/clear` (measured 2026-10-22: job aaeab104 went from conversation aaeab104-f4c9..
# to 31873623-.. and its viewer card refused it as "not listed"). `claude agents` reports the current conversation;
# the job's state.json keeps the original one. Shoal stores whichever id it saw (a spec keeps the launch id, a
# viewer registry the id current when it was saved), so every lookup resolves a stored id against all three.

def claude_original(job: str) -> str:
    """The conversation id a Claude job was started with (its state.json), '' when unknown."""
    if not job or not re.fullmatch(r"[0-9a-f]{6,64}", job) or fake_sessions() is not None:
        return ""
    base = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")
    try:
        import json as _json
        with open(os.path.join(base, "jobs", job, "state.json")) as fh:
            return str((_json.load(fh) or {}).get("sessionId") or "")
    except (OSError, ValueError):
        return ""


def _same(a: str, b: str) -> bool:
    return bool(a and b) and (a == b or a.startswith(b) or b.startswith(a))


def claude_find(rows: list, session: str, job: str = "") -> dict | None:
    """The `claude agents` row a stored id names: by an explicit job id first, then the current conversation id, the
    job id (a stored id's first 8 characters) and the job's original conversation id (rows may carry it as
    `originalSessionId`, tests). None when nothing matches: never a guess."""
    if job:
        e = next((e for e in rows if str(e.get("id", "")) == job), None)
        if e is not None:
            return e
    if not session:
        return None
    for e in rows:
        if _same(str(e.get("sessionId", "")), session):
            return e
    for e in rows:
        jid = str(e.get("id", ""))
        if jid and (jid == session or jid == session[:8]):
            return e
    for e in rows:
        orig = str(e.get("originalSessionId") or claude_original(str(e.get("id", ""))))
        if _same(orig, session):
            return e
    return None


def claude_holders(rows: list, session: str, job: str = "") -> list[dict]:
    """Every row holding the conversation a stored id names, a live worker first, then the most recently started. A
    stop+resume of a job whose id is not its conversation's prefix starts a second job on the same conversation, and acting
    on the first match only reaches the stopped one (measured 2026-10-24: the advice resumed b193724a's conversation as job
    4decf3de, the reset stopped b193724a again, and the copy stayed live beside the new session)."""
    e = claude_find(rows, session, job)
    if e is None:
        return []
    conv = str(e.get("sessionId") or session)
    out = [r for r in rows if _same(str(r.get("sessionId", "")), conv)] or [e]
    return sorted(out, key=lambda r: (bool(r.get("pid")), r.get("startedAt") or 0), reverse=True)


def claude_current(session: str, job: str = "") -> str:
    """The conversation id a stored id names now (after any `/clear`), else the stored id itself."""
    try:
        e = claude_find(get("claude")._agents(include_stopped=True), session, job)
    except Exception:  # noqa: BLE001
        e = None
    return str((e or {}).get("sessionId") or session)


def direct_wake_mode(spec) -> str:
    """features.health.direct_wake: 'off', 'dead' (resume a session with no worker: no relay turn) or
    'idle' (also stop and resume a live session idle at its prompt with nothing in flight). Default
    'dead' when health.revive is on, else 'off'."""
    h = (getattr(spec, "features", {}) or {}).get("health")
    h = h if isinstance(h, dict) else {}
    m = str(h.get("direct_wake", "dead" if h.get("revive") else "off")).lower()
    return m if m in ("off", "dead", "idle") else "off"


def relay_fallback(spec) -> bool:
    """features.health.relay_fallback: whether a wake that cannot be delivered directly goes to the wake
    relay (two turns) or waits for a direct chance (the session wakes itself when its in-flight work
    ends). Default: on, except in direct_wake 'idle' mode, where the relay is left for nothing."""
    h = (getattr(spec, "features", {}) or {}).get("health")
    h = h if isinstance(h, dict) else {}
    return bool(h.get("relay_fallback", direct_wake_mode(spec) != "idle"))


def detached(argv: list[str]) -> list[str]:
    """argv run so that a Claude background daemon it may start outlives the caller's systemd unit.
    Under a systemd service (INVOCATION_ID set, e.g. a health timer), `claude` starts its daemon on
    demand inside the unit's cgroup, and KillMode=control-group kills it when the oneshot ends, with
    every session it spawned (measured 2026-10-12 after a reboot: a relay relaunched by the quorum died
    4 s later). `systemd-run --user --scope` gives the call its own scope, which keeps the caller's
    environment, cwd and pipes."""
    sr = shutil.which("systemd-run")
    if os.environ.get("INVOCATION_ID") and sr:
        return [sr, "--user", "--scope", "--collect", "--quiet", *argv]
    return argv


def spawn_detached(argv: list[str], **kw) -> subprocess.Popen:
    """Popen a long-lived process that must outlive the caller's systemd unit (a watcher the quorum
    re-arms): detached() gives it its own scope, and this waits, up to 5 s, until the child has left the
    unit's cgroup. Without the wait the oneshot can end first and take the child with it (measured
    2026-10-20 in a probe unit: the scoped child died when the unit exited at once, lived when it did not)."""
    import time as _time
    wrapped = detached(argv)
    p = subprocess.Popen(wrapped, start_new_session=True, **kw)
    if wrapped is argv:
        return p
    try:
        mine = open("/proc/self/cgroup").read().strip()
    except OSError:
        return p
    end = _time.time() + 5
    while _time.time() < end and p.poll() is None:
        try:
            if open(f"/proc/{p.pid}/cgroup").read().strip() != mine:
                break
        except OSError:
            break
        _time.sleep(0.05)
    return p


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

    def sessions(self) -> list[dict]:
        return [{"session": str(e["sessionId"]), "job": str(e.get("id") or ""),
                 "label": str(e.get("name") or e["sessionId"][:8])[:40],
                 "state": e.get("state"), "cwd": e.get("cwd")}
                for e in self._agents() if e.get("sessionId") and e.get("pid")]

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
        alive, _ = watcher_alive(spec.sid, role.slug)
        if direct_wake_mode(spec) != "off":
            # a live detached watcher delivers the wake file itself (directly, by resume), so a retired
            # worker needs nothing here; without one the quorum delivers it now
            if alive:
                if not dry_run:
                    _os.makedirs(state_dir(spec.sid), exist_ok=True)
                    with open(wake_path(spec.sid, role.slug), "a") as fh:
                        fh.write(_json.dumps({"ts": iso(), "condition": condition, "text": text}) + "\n")
                return "wake-file" + (" (dry-run)" if dry_run else "")
            return self.deliver(spec, role, role.session, text, dry_run)
        if revive and role.session and self.session_dead(role.session):
            revived = f"; respawned {role.session[:8]} ({self.respawn(role.session, dry_run)})"
            text = REVIVED_NOTE + text
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

    def deliver(self, spec, role, session: str, text: str, dry_run: bool = False) -> str:
        """Deliver one wake to a Claude session with no turn but the target's own, where it allows that
        (features.health.direct_wake; architecture doc §7). Returns the channel:
        - `resume`: the session has no worker (retired idle at its prompt, reaped, stopped), so
          `claude --bg --resume <session> <text>` continues it under the same id with the text as its
          prompt (verified 2026-10-20: same id, saved options, prompt cache mostly kept);
        - `stop+resume` (mode `idle`): a live worker idle at its prompt with nothing in flight is
          stopped and resumed the same way;
        - `deferred (...)`: busy, at a question, or work in flight; the caller retries later (a session
          with work in flight wakes itself when that work ends). With relay_fallback the wake relay
          takes these instead (`wake-relay`);
        - `FAILED ...`: the resume did not take; the caller retries."""
        import time as _time
        from health import claude_job_state, claude_liveness, claude_prompt_open
        mode = direct_wake_mode(spec)
        row = self._row(session, include_stopped=True) if session else None
        full = str((row or {}).get("sessionId") or session)
        why = ""
        if mode == "off" or row is None:
            why = "direct wake off" if mode == "off" else "session not listed"
        elif not row.get("pid"):
            return self._resume(row, full, RESUMED_NOTE + text, dry_run)
        else:
            live = claude_liveness(row)
            fl = claude_job_state(row).get("inFlight") or {}
            busy_fl = isinstance(fl, dict) and (int(fl.get("tasks") or 0) or int(fl.get("queued") or 0) or fl.get("kinds"))
            if live == "busy":
                why = "mid-turn"
            elif claude_prompt_open(row, full):
                why = "at a question"
            elif busy_fl:
                why = "background work in flight"
            elif mode != "idle":
                why = "live worker (direct_wake dead)"
            else:
                if dry_run or fake_sessions() is not None:
                    return "stop+resume" + (" (dry-run)" if dry_run else " (fake)")
                self.stop(str(row.get("id") or full[:8]))
                for _ in range(40):
                    r2 = self._row(full, include_stopped=True) or {}
                    if not r2.get("pid"):
                        break
                    _time.sleep(0.5)
                _time.sleep(RESUME_SETTLE_S)
                return "stop+" + self._resume(row, full, text, dry_run)
        if relay_fallback(spec):
            return self._relay(spec, role, session, text, dry_run) + f" ({why})"
        return f"deferred ({why})"

    def _resume(self, row: dict, session: str, text: str, dry_run: bool = False) -> str:
        if dry_run or fake_sessions() is not None:
            return "resume" + (" (dry-run)" if dry_run else " (fake)")
        b = tool_which("claude") or "claude"
        cwd = row.get("cwd") or None
        if cwd and not os.path.isdir(cwd):
            cwd = None
        r = subprocess.run(detached([b, "--bg", "--resume", session, text]), capture_output=True, text=True,
                           timeout=120, cwd=cwd, env=_ENV)
        out = ANSI_RE.sub("", r.stdout + r.stderr)
        m = COPY_RE.search(out)
        if m:
            # the session was still running: a copy would act as the role beside it, so it goes at once
            self.stop(m.group(1))
            subprocess.run([b, "rm", m.group(1)], capture_output=True, text=True, timeout=30, env=_ENV)
            return f"FAILED (session still running; copy {m.group(1)} stopped and removed)"
        if r.returncode != 0:
            return f"FAILED rc={r.returncode}: {out.strip().splitlines()[-1][:120] if out.strip() else ''}"
        return "resume"

    def _relay(self, spec, role, session: str, text: str, dry_run: bool = False) -> str:
        import json as _json
        from health import iso, relay_alive, relay_queue_path
        from roles import wake_relay_role
        if wake_relay_role(spec) is None or not (spec.features or {}).get("relay"):
            return "no-adapter (wake relay disabled)"
        pending = "" if relay_alive(spec.sid) else " (queued; relay down, relaunch pending)"
        if not dry_run:
            with open(relay_queue_path(spec.sid), "a") as fh:
                fh.write(_json.dumps({"ts": iso(), "to": role.tag, "slug": role.slug, "session": session,
                                      "condition": "wake", "text": text}) + "\n")
        return "wake-relay" + pending + (" (dry-run)" if dry_run else "")

    def reset(self, session: str, prompt: str, label: str = "", cwd: str = "", dry_run: bool = False) -> dict:
        """Claude: a new job on the prompt, with the old job's saved options (name, model, effort from its state.json),
        in its directory; then `claude stop` on the old job. Not `/clear`: a background job cleared and later stopped
        splits into its original conversation and a separate job for the cleared one, and the two block each other
        (measured 2026-10-22)."""
        import json as _json
        rows = self._agents(include_stopped=True)
        holders = claude_holders(rows, session)
        e = holders[0] if holders else {}
        jobs = [str(h.get("id")) for h in holders if h.get("id")]
        flags = []
        base = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")
        for j in jobs:  # the saved options of whichever copy has them (a long-lived job may carry none)
            try:
                flags = list((_json.load(open(os.path.join(base, "jobs", j, "state.json"))) or {}).get("respawnFlags") or [])
            except (OSError, ValueError):
                flags = []
            if flags:
                break
        opt = {flags[i]: flags[i + 1] for i in range(len(flags) - 1) if str(flags[i]).startswith("-")}
        name = label or opt.get("-n") or str(e.get("name") or "") or "session"
        where = cwd or str(e.get("cwd") or "") or os.getcwd()
        old = ", ".join(jobs) or session[:8]
        if dry_run:
            return {"session": "", "job": "", "did": f"would start a new job {name!r} in {where} "
                                                      f"(model {opt.get('--model', 'default')}) and stop {old}"}
        if fake_sessions() is not None:  # tests: no real launch
            return {"session": "fe5e7000-0000", "job": "fe5e7000", "did": f"fake new job fe5e7000 ({name}); stopped {old or '-'}"}
        new = self.create(name, where, brief=prompt, name=name, model=opt.get("--model", ""), effort=opt.get("--effort", ""))
        live = self.retire(jobs)
        did = f"new job {new[:8]} ({name}); stopped {old or '-'}"
        return {"session": new, "job": new[:8], "did": did + (f"; STILL LIVE: {', '.join(live)}" if live else ""),
                "still_live": live}

    def retire(self, jobs: list[str], verify_s: float = RETIRE_VERIFY_S) -> list[str]:
        """Stop every job and verify that none comes back: the background service can respawn a stopped job's worker
        when another job on its conversation is removed (measured 2026-10-24: `claude rm` of the copy above restarted
        b193724a, and a later stop did not hold). A job seen live again is stopped once more; returns those still live.
        Never `claude rm`: a stopped job is retired, and removing one is what woke the other."""
        import time as _time
        for j in jobs:
            self.stop(j)
        restopped: set[str] = set()
        deadline = _time.time() + verify_s
        while jobs and _time.time() < deadline:
            _time.sleep(2)
            rows = {str(r.get("id")): r for r in self._agents(include_stopped=True)}
            back = [j for j in jobs if (rows.get(j) or {}).get("pid")]
            for j in back:
                if j not in restopped:
                    self.stop(j)
                    restopped.add(j)
        rows = {str(r.get("id")): r for r in self._agents(include_stopped=True)}
        return [j for j in jobs if (rows.get(j) or {}).get("pid")]

    def create(self, title: str, repo_dir: str, dry_run: bool = False,
               brief: str = "", name: str = "", model: str = "",
               effort: str = "") -> str:
        if dry_run:
            return "cc_DRYRUN_" + re.sub(r"\W+", "_", title)
        if not brief:
            raise UnsupportedError("claude create needs the role brief text")
        b = tool_which("claude") or "claude"
        argv = [b, "--bg", "-n", name or title]
        if model:
            argv += ["--model", model]
        if effort:
            argv += ["--effort", effort]
        argv += [brief]
        r = subprocess.run(detached(argv), capture_output=True, text=True, timeout=120,
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
        b = tool_which("claude") or "claude"
        r = subprocess.run([b, "agents", "--json"] + (["--all"] if include_stopped else []),
                           capture_output=True, text=True, timeout=30, env=_ENV)
        try:
            return _json.loads(r.stdout) if r.returncode == 0 else []
        except Exception:
            return []

    def _row(self, session_id: str, include_stopped: bool = False) -> dict | None:
        return claude_find(self._agents(include_stopped), session_id)

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
        b = tool_which("claude") or "claude"
        r = subprocess.run(detached([b, "respawn", str(e.get("id") or session_id[:8])]), capture_output=True,
                           text=True, timeout=120, env=_ENV)
        out = ANSI_RE.sub("", r.stdout + r.stderr).strip().splitlines()
        return (out[-1] if out else "respawned") if r.returncode == 0 else f"FAILED rc={r.returncode}: {out[-1] if out else ''}"

    def messages(self, session_id: str, repo_dir: str, limit: int = 20,
                 role: str = "") -> str:
        b = tool_which("claude") or "claude"
        r = subprocess.run([b, "logs", session_id], capture_output=True,
                           text=True, timeout=30, env=_ENV)
        out = re.sub(r"\x1b\[[0-9;?]*[a-zA-Z]", "", r.stdout)
        lines = [ln for ln in out.splitlines() if ln.strip()]
        return "\n".join(lines[-max(limit, 1) * 4:])

    def status(self, session_id: str, repo_dir: str) -> str:
        e = claude_find(self._agents(), session_id)
        if e is not None:
            return f"{e.get('name', '?')} state:{e.get('state', '?')}"
        return "not-listed (retired or unknown id)"

    def view_argv(self, session_id: str, model: str = "", effort: str = "") -> tuple[list[str], dict[str, str]]:
        """`claude attach <id>`: the live background session in this terminal, ownership unchanged.
        Detaching (or closing the terminal) leaves the session running. Attach is exclusive: a second
        attach fails while another terminal holds it. Recaps are switched off for the client: Claude
        Code may write an `away_summary` (a model call) when a client returns to a session with
        unseen activity (measured once on a testbed lane, 2026-10-08; not reproducible on demand),
        and a viewer should cost nothing."""
        # `claude attach` matches the job id (8 hex), not the session UUID a spec may record (restart
        # adopts the sidecar's full id): measured 2026-10-12, "No job matching '<uuid>'"
        e = self._row(session_id, include_stopped=True) or {}
        job = str(e.get("id") or session_id.split("-", 1)[0][:8])
        return [tool_which("claude") or "claude", "attach", job], {"CLAUDE_CODE_ENABLE_AWAY_SUMMARY": "0"}

    @staticmethod
    def launch_command(brief_file: str) -> str:
        b = tool_which("claude") or "claude"
        return f'{b} "$(cat {brief_file})"'

    @staticmethod
    def stop(session_id: str) -> str:
        import shutil as _sh
        b = tool_which("claude") or "claude"
        r = subprocess.run([b, "stop", session_id], capture_output=True,
                           text=True, timeout=30, env=_ENV)
        return (r.stdout + r.stderr).strip()[-200:]

    def quota_paused(self, session_id: str, repo_dir: str = "") -> str:
        """Terminal-log limit signature (zero model cost). Verified 2026-09-21:
        'reached your Fable limit' present while quota-dead, absent after the
        opus switch on the same session."""
        b = tool_which("claude")
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
        b = tool_which("claude") or "claude"
        r = subprocess.run(
            [b, "-p", "Reply with exactly: SHOAL-PROBE-OK (connectivity probe, "
                      "no action)", "--model", model, "--output-format", "text"],
            capture_output=True, text=True, timeout=300, env=_ENV)
        return (r.stdout + r.stderr).strip()[-200:]


class CodexBackend(Backend):
    """Codex CLI: a session is a thread id and each turn is one `codex exec resume <id>` process
    (shoal/codex_harness.py). Like Muse there is no harness-wide listing, no attach and no respawn: a turn
    that died is simply followed by the next wake, so revive is the ordinary wake path."""
    ecosystem = "codex"

    def sessions(self) -> list[dict]:
        import ui_sessions
        return ui_sessions.stored_sessions(self.ecosystem)

    def create(self, title: str, repo_dir: str, dry_run: bool = False,
               brief: str = "", name: str = "", model: str = "",
               effort: str = "") -> str:
        import codex_harness as codex
        if dry_run:
            return "cx_DRYRUN_" + re.sub(r"\W+", "_", title)
        if not brief:
            raise UnsupportedError("codex create needs the role brief text")
        return codex.create_session(repo_dir, brief, model=model, effort=effort)

    def send(self, session_id: str, repo_dir: str, prompt: str,
             dry_run: bool = False, model: str = "", effort: str = "") -> None:
        import codex_harness as codex
        codex.start_run(session_id, repo_dir, prompt, model=model, effort=effort, dry_run=dry_run)

    def messages(self, session_id: str, repo_dir: str, limit: int = 20, role: str = "") -> str:
        import codex_harness as codex
        return codex.messages(session_id, limit)

    def status(self, session_id: str, repo_dir: str) -> str:
        import codex_harness as codex
        return codex.status_line(session_id)

    def wake(self, spec, role, text: str, condition: str = "", dry_run: bool = False) -> str:
        """As for Muse: a wake goes to the role's own watcher while it runs; a dead watcher is started again
        and the wake is sent directly."""
        import json as _json
        import codex_harness as codex
        from health import iso, state_dir, wake_path, watcher_alive
        alive, _ = watcher_alive(spec.sid, role.slug)
        if alive:
            if not dry_run:
                os.makedirs(state_dir(spec.sid), exist_ok=True)
                with open(wake_path(spec.sid, role.slug), "a") as fh:
                    fh.write(_json.dumps({"ts": iso(), "condition": condition, "text": text}) + "\n")
            return "wake-file" + (" (dry-run)" if dry_run else "")
        rearmed = codex.arm_watcher(spec, role, dry_run) if role.session else "no session"
        try:
            self.send(role.session, spec.dir, text, dry_run=dry_run, model=role.model, effort=role.effort)
        except codex.BusyError:
            return f"codex-busy (the running turn reads the queue at its end; watcher {rearmed})"
        except Exception as exc:  # noqa: BLE001
            return f"codex-send FAILED ({str(exc)[:80]}); watcher {rearmed}"
        return f"codex-send; watcher {rearmed}" + (" (dry-run)" if dry_run else "")

    def view_argv(self, session_id: str, model: str = "", effort: str = "") -> tuple[list[str], dict[str, str]]:
        import codex_harness as codex
        return codex.view_argv(session_id, model, effort), {}

    def quota_paused(self, session_id: str, repo_dir: str = "") -> str:
        import codex_harness as codex
        return codex.quota_hint(session_id)


class MuseBackend(Backend):
    """Muse Code: a session is a durable id and each turn is one `muse exec --session-id <id>` process
    (shoal/muse.py). There is no harness-wide listing, no attach and no respawn: a turn that died is
    simply followed by the next wake, so revive is the ordinary wake path."""
    ecosystem = "muse"

    def sessions(self) -> list[dict]:
        import ui_sessions
        return ui_sessions.stored_sessions(self.ecosystem)

    def create(self, title: str, repo_dir: str, dry_run: bool = False,
               brief: str = "", name: str = "", model: str = "",
               effort: str = "") -> str:
        import muse
        if dry_run:
            return "mu_DRYRUN_" + re.sub(r"\W+", "_", title)
        if not brief:
            raise UnsupportedError("muse create needs the role brief text")
        session = muse.uuid7()
        muse.start_run(session, repo_dir, brief, model=model, effort=effort)
        return session

    def send(self, session_id: str, repo_dir: str, prompt: str,
             dry_run: bool = False, model: str = "", effort: str = "") -> None:
        import muse
        muse.start_run(session_id, repo_dir, prompt, model=model, effort=effort, dry_run=dry_run)

    def messages(self, session_id: str, repo_dir: str, limit: int = 20, role: str = "") -> str:
        import muse
        return muse.messages(session_id, limit)

    def status(self, session_id: str, repo_dir: str) -> str:
        import muse
        return muse.status_line(session_id)

    def wake(self, spec, role, text: str, condition: str = "", dry_run: bool = False) -> str:
        """A wake goes to the role's own watcher (a wake-request file it delivers at its next idle tick)
        while that watcher runs; a dead watcher is started again and the wake is sent directly."""
        import json as _json
        import muse
        from health import iso, state_dir, wake_path, watcher_alive
        alive, _ = watcher_alive(spec.sid, role.slug)
        if alive:
            if not dry_run:
                os.makedirs(state_dir(spec.sid), exist_ok=True)
                with open(wake_path(spec.sid, role.slug), "a") as fh:
                    fh.write(_json.dumps({"ts": iso(), "condition": condition, "text": text}) + "\n")
            return "wake-file" + (" (dry-run)" if dry_run else "")
        rearmed = muse.arm_watcher(spec, role, dry_run) if role.session else "no session"
        try:
            self.send(role.session, spec.dir, text, dry_run=dry_run, model=role.model, effort=role.effort)
        except muse.BusyError:
            return f"muse-busy (the running turn reads the queue at its end; watcher {rearmed})"
        except Exception as exc:  # noqa: BLE001
            return f"muse-send FAILED ({str(exc)[:80]}); watcher {rearmed}"
        return f"muse-send; watcher {rearmed}" + (" (dry-run)" if dry_run else "")

    def view_argv(self, session_id: str, model: str = "", effort: str = "") -> tuple[list[str], dict[str, str]]:
        import muse
        return muse.view_argv(session_id, model, effort), {"MUSE_NO_AUTO_UPDATE": "1"}

    def quota_paused(self, session_id: str, repo_dir: str = "") -> str:
        import muse
        return muse.quota_hint(session_id)


BACKENDS = {"claude": ClaudeBackend, "opencode": OpenChamberBackend, "codex": CodexBackend, "muse": MuseBackend}


def ecosystems() -> tuple[str, ...]:
    return tuple(BACKENDS)


def get(ecosystem: str) -> Backend:
    return BACKENDS[ecosystem]()
