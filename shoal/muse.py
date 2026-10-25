#!/usr/bin/env python3
"""Muse Code (`muse`, Meta's coding-agent CLI) as a shoal harness: ONE `muse exec` PROCESS PER WAKE.

Why exec per wake and not a long-lived `muse serve` host (design: distributed-insight
project_admin/shoal/design/muse-code-support, validated 2026-10-15): sessions are durable files, so a
second `muse exec --session-id <uuid>` continues the same session; an idle role costs no process and no
tokens; `muse serve` serves exactly one client over stdio, so several shoal CLIs could not share a host;
and shoal, not the role, runs the persistent watcher, so a Muse role never needs permission to start a
detached process.

State per session lives in `<state root>/<session uuid>/` (default /tmp/shoal-muse-<uid>):
  lock       flock held by the process that runs a turn: held means busy. The CALLER takes it and the
             runner inherits the descriptor, so a session is busy from the instant `start_run` returns
  run.jsonl  every JSONL record `muse exec --json` printed, all turns appended
  run.err    its stderr
  last.json  the latest turn: started, ended, rc, terminal (completed|failed|...), reason, text, error
  prompt-*.txt  the prompts handed to `--prompt-file`

Posture (maintainer decision 2026-10-15): the sandbox is off and approval prompts are off for unattended
turns, `--yolo` (it also trusts the workspace so its rules and skills load, and withholds
`request_user_input`: a headless turn has nobody to answer a question, so asks go through `queue --ask`).
`META_API_KEY` is stripped from the environment of every turn: it outranks the account login and bills
per token, and the maintainer chose the subscription (override with SHOAL_MUSE_ALLOW_API_KEY=1).
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config  # noqa: E402

CLI = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cli.py")
POSTURE = ["--yolo"]
QUOTA_RE = re.compile(r"(rate.?limit|usage.?limit|quota|out of credits|insufficient|too many requests|\b429\b)", re.I)
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


class BusyError(RuntimeError):
    """A turn is already running for the session (its lock is held)."""


def state_root() -> str:
    return config.env("MUSE_STATE") or os.path.join(tempfile.gettempdir(), f"shoal-muse-{os.getuid()}")


def sdir(session: str, create: bool = False) -> str:
    d = os.path.join(state_root(), session)
    if create:
        os.makedirs(d, exist_ok=True)
    return d


def muse_bin() -> str:
    return (config.env("MUSE_BIN") or shutil.which("muse")
            or os.path.expanduser("~/.local/bin/muse"))


def uuid7() -> str:
    """A time-ordered UUID (version 7), the form Muse mints for its own session ids."""
    b = bytearray(int(time.time() * 1000).to_bytes(6, "big") + os.urandom(10))
    b[6] = (b[6] & 0x0F) | 0x70
    b[8] = (b[8] & 0x3F) | 0x80
    return str(uuid.UUID(bytes=bytes(b)))


def turn_env() -> dict:
    env = dict(os.environ)
    env["MUSE_NO_AUTO_UPDATE"] = "1"   # a pinned binary: an hourly self-update must not change a running shoal
    env["MUSE_LOGIN"] = "0"            # never open a browser login from an unattended turn: fail fast
    if not config.env("MUSE_ALLOW_API_KEY"):
        env.pop("META_API_KEY", None)  # it outranks the subscription login and bills per token
    # a Muse process is not the Claude session that happened to start it: an inherited id would make the
    # role's watcher bind itself to the operator's session (measured 2026-10-15, fts-core)
    for k in [k for k in env if k.startswith("CLAUDE_CODE_") or k == "CLAUDECODE"]:
        env.pop(k)
    return env


def attached_client(session: str) -> int:
    """Pid of an interactive Muse client (`muse resume <session>`) holding the session, else 0. Muse
    refuses a second owner ("session ... is already in use"), so an exec turn started under an attached
    client fails and its wake is lost (measured 2026-10-15, fts-core): the watcher must hold the wake."""
    me = os.getpid()
    try:
        pids = [p for p in os.listdir("/proc") if p.isdigit() and int(p) != me]
    except OSError:
        return 0
    for p in pids:
        try:
            with open(f"/proc/{p}/cmdline", "rb") as fh:
                argv = [x.decode(errors="replace") for x in fh.read().split(b"\0") if x]
        except OSError:
            continue
        # the native binary is argv[0]; a wrapper or interpreter (a script named muse...) puts it second
        for i in (0, 1):
            if (len(argv) > i + 2 and os.path.basename(argv[i]).startswith("muse") and argv[i + 1] == "resume"
                    and session in argv[i + 2:]):
                return int(p)
    return 0


def busy(session: str) -> bool:
    """True while a turn holds the session's lock or an interactive client is attached. A session never
    run here, or one whose directory is gone, is not busy."""
    if attached_client(session):
        return True
    lock = os.path.join(sdir(session), "lock")
    if not os.path.exists(lock):
        return False
    try:
        with open(lock, "a+") as fh:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            fcntl.flock(fh, fcntl.LOCK_UN)
    except OSError:
        return False
    return False


def last_run(session: str) -> dict:
    try:
        with open(os.path.join(sdir(session), "last.json")) as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


ACTIVE_WINDOW_S = 120  # a session whose journal changed within this long is mid-turn, not just held open


def activity_age(session: str):
    """Seconds since the session's journal last changed (Muse appends to it as a turn runs), or None when it
    cannot be read. `busy()` is also true for a client that is merely attached and waiting for the maintainer,
    which a viewer must not draw as working."""
    root = config.env("MUSE_DATA") or os.path.expanduser("~/.local/share/muse/sessions")
    try:
        files = [os.path.join(d, f) for d in (os.path.join(root, ".msp-view-v1", session),)
                 for f in os.listdir(d) if f.startswith("journal")]
        return max(0.0, time.time() - max(os.path.getmtime(f) for f in files))
    except (OSError, ValueError):
        return None


def status_line(session: str) -> str:
    """`<id8> status:busy|idle|unknown ...`, the shape the opencode consumers already match on. An attached
    client adds `attached`, then `active` (the journal moved within ACTIVE_WINDOW_S) or `quiet` (it did not);
    no word means the journal could not be read."""
    fake = None
    try:
        from backends import fake_sessions
        fake = fake_sessions()
    except Exception:  # noqa: BLE001
        pass
    if fake is not None and "muse" in fake:  # a fake world that names no muse sessions leaves muse real
        return str((fake.get("muse") or {}).get(session, ""))
    if busy(session):
        if not attached_client(session):
            return f"{session[:8]} status:busy"
        age = activity_age(session)
        return f"{session[:8]} status:busy attached" + ("" if age is None else " active" if age <= ACTIVE_WINDOW_S else " quiet")
    last = last_run(session)
    if not last:
        return f"{session[:8]} status:idle never-run-here"
    tail = f" last:{last.get('terminal') or '?'} rc:{last.get('rc')}"
    return f"{session[:8]} status:idle{tail}"


def quota_hint(session: str) -> str:
    """'' or a reset hint, from the last turn's failure. PROVISIONAL: no Muse usage-limit error has been
    seen on this host yet, so the pattern is broad; calibrate after the first real limit (plan, Phase 4)."""
    last = last_run(session)
    if last.get("terminal") not in ("failed", "error") and last.get("rc") in (0, None):
        return ""
    blob = f"{last.get('reason') or ''} {last.get('error') or ''} {last.get('text') or ''}"
    if not QUOTA_RE.search(blob):
        return ""
    m = re.search(r"resets? (?:at |in )?([^.\n|]{0,40})", blob, re.I)
    return (m.group(1).strip() if m else "") or "reset time unknown"


def run_argv(session: str, repo_dir: str, prompt_file: str, model: str = "", effort: str = "") -> list[str]:
    argv = [muse_bin(), "exec", "--json", "--session-id", session, "--prompt-file", prompt_file,
            "--workspace", repo_dir] + POSTURE
    if model:
        argv += ["--model", model]
    if effort:
        argv += ["--reasoning-effort", effort]
    extra = config.env("MUSE_ARGS")  # tests: `--provider echo`
    return argv + (extra.split() if extra else [])


def start_run(session: str, repo_dir: str, prompt: str, model: str = "", effort: str = "",
              dry_run: bool = False) -> None:
    """Start one turn on `session` in the background and return at once. The lock is taken HERE and
    inherited by the runner, so the session reads busy from the moment this returns."""
    if dry_run:
        return
    if attached_client(session):
        raise BusyError(f"muse session {session[:8]} has an interactive client attached")
    d = sdir(session, create=True)
    lock_fh = open(os.path.join(d, "lock"), "a+")
    try:
        fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock_fh.close()
        raise BusyError(f"muse session {session[:8]} is already running a turn")
    pf = os.path.join(d, f"prompt-{int(time.time() * 1000)}.txt")
    with open(pf, "w") as fh:
        fh.write(prompt)
    argv = [sys.executable, CLI, "muse-run", "--session", session, "--dir", repo_dir, "--prompt-file", pf,
            "--lock-fd", str(lock_fh.fileno())]
    if model:
        argv += ["--model", model]
    if effort:
        argv += ["--effort", effort]
    from backends import detached
    log = open(os.path.join(d, "runner.log"), "a")
    try:
        subprocess.Popen(detached(argv), stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                         cwd=repo_dir if os.path.isdir(repo_dir) else None, env=turn_env(),
                         start_new_session=True, pass_fds=[lock_fh.fileno()])
    finally:
        log.close()
        lock_fh.close()  # the child keeps the inherited, still-locked descriptor


def cmd_muse_run(a) -> int:
    """The runner: one `muse exec` turn, its records appended to run.jsonl, its result to last.json."""
    d = sdir(a.session, create=True)
    held = None
    if attached_client(a.session):
        print(f"busy: an interactive client is attached to session {a.session[:8]}")
        return 75
    if a.lock_fd is None:  # run by hand: take the lock itself
        held = open(os.path.join(d, "lock"), "a+")
        try:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(f"busy: session {a.session[:8]} is already running a turn")
            return 75
    start = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    last = {"session": a.session, "started": start, "pid": os.getpid(), "prompt": os.path.basename(a.prompt_file)}
    _write_last(d, last)
    terminal = {}
    err_tail = ""
    rc = 1
    try:
        with open(os.path.join(d, "run.jsonl"), "a") as out, open(os.path.join(d, "run.err"), "a") as err:
            err_from = err.tell()  # this turn's stderr starts here: an older failure must not read as this one's
            err.write(f"--- {start} {' '.join(run_argv(a.session, a.dir, a.prompt_file, a.model, a.effort)[:4])} ...\n")
            p = subprocess.Popen(run_argv(a.session, a.dir, a.prompt_file, a.model, a.effort),
                                 stdout=subprocess.PIPE, stderr=err, text=True, cwd=a.dir if os.path.isdir(a.dir) else None,
                                 env=turn_env())
            for line in p.stdout:
                out.write(line)
                out.flush()
                try:
                    pl = (json.loads(line).get("payload") or {})
                except ValueError:
                    continue
                if pl.get("kind") == "run_terminal":
                    terminal = pl
            rc = p.wait()
        try:
            with open(os.path.join(d, "run.err"), "rb") as fh:
                fh.seek(max(err_from, os.path.getsize(os.path.join(d, "run.err")) - 600))
                err_tail = fh.read().decode(errors="replace")
        except OSError:
            pass
    except OSError as exc:  # muse missing or unrunnable
        err_tail = f"cannot run {muse_bin()}: {exc}"
    last.update(ended=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), rc=rc,
                terminal=terminal.get("terminal") or ("failed" if rc else "completed"),
                reason=terminal.get("reason"), text=(terminal.get("text") or "")[:2000],
                error=err_tail.strip()[-500:] if rc else "")
    _write_last(d, last)
    if held is not None:
        held.close()
    return rc


def _write_last(d: str, last: dict) -> None:
    tmp = os.path.join(d, f"last.json.{os.getpid()}.tmp")
    with open(tmp, "w") as fh:
        json.dump(last, fh)
    os.replace(tmp, os.path.join(d, "last.json"))


def messages(session: str, limit: int = 20) -> str:
    """The final text of the latest turns (the run_terminal records), newest last."""
    out = []
    try:
        with open(os.path.join(sdir(session), "run.jsonl")) as fh:
            for line in fh:
                try:
                    pl = json.loads(line).get("payload") or {}
                except ValueError:
                    continue
                if pl.get("kind") == "run_terminal":
                    out.append(f"[{pl.get('terminal')}] {pl.get('text') or pl.get('reason') or ''}")
    except OSError:
        return ""
    return "\n".join(out[-limit:])


def cmd_muse_tail(a) -> int:
    """A read-only view of a session's turns: past results, then new ones as they arrive. Never starts or
    resumes anything."""
    d = sdir(a.session)
    print(f"muse session {a.session} (read-only view; `muse resume {a.session}` takes it over when no turn runs)")
    shown = 0
    path = os.path.join(d, "run.jsonl")
    try:
        while True:
            if os.path.exists(path):
                with open(path) as fh:
                    lines = fh.readlines()
                for line in lines[shown:]:
                    try:
                        pl = json.loads(line).get("payload") or {}
                    except ValueError:
                        continue
                    if pl.get("kind") == "run_terminal":
                        print(f"\n--- turn {pl.get('terminal')} ---\n{pl.get('text') or pl.get('reason') or ''}")
                    elif pl.get("kind") == "run_started":
                        print("\n--- turn started ---")
                shown = len(lines)
            if a.once:
                return 0
            print(f"\r[{'running' if busy(a.session) else 'idle'}]", end="", flush=True)
            time.sleep(2)
    except KeyboardInterrupt:
        return 0


DEFAULT_MODEL = "muse-wonder-full-1.0"


def interactive_flags(model: str = "", effort: str = "") -> list[str]:
    """Flags for an interactive (TUI) client: the model and effort are per run, not stored in the session,
    and the sandbox is off for the same reason it is off for exec turns (a sandboxed shell cannot push or
    reach the CI tooling). Approvals stay on: a person is at the keyboard, unlike an exec turn. A seat's own
    model and effort (its spec role, or the registry entry `muse-new --pick` saved) win over the host
    defaults, so a card resumes the seat as it was created. `SHOAL_MUSE_VIEW_ARGS` replaces the whole list."""
    extra = config.env("MUSE_VIEW_ARGS")
    if extra:
        return extra.split()
    return ["--model", model or config.env("MUSE_MODEL") or DEFAULT_MODEL,
            "--reasoning-effort", effort or "high", "--trust-workspace", "--disable-sandbox"]


def view_argv(session: str, model: str = "", effort: str = "") -> list[str]:
    """The viewer's command: the interactive client takes the session over, which is only safe while no
    turn runs; mid-turn the read-only tail is shown instead."""
    if busy(session):
        return [sys.executable, CLI, "muse-tail", "--session", session]
    if UUID_RE.match(session) or os.path.isdir(sdir(session)):
        return [muse_bin(), "resume", session, *interactive_flags(model, effort)]
    # a standalone session by NAME (Muse names are unique per user): resume it, or start a fresh one the
    # first time (`muse resume` of an unknown name fails); the user then names it with /name
    flags = " ".join(shlex.quote(f) for f in interactive_flags(model, effort))
    return ["sh", "-c", f'"$0" resume "$1" {flags} || exec "$0" {flags}', muse_bin(), session]


def msp_session(name: str, cwd: str, model: str = "", effort: str = "") -> str:
    """Create a durable, named, empty Muse session through a short-lived `muse serve` and return its id.

    An interactive session is the same durable object an exec turn uses; it just lacks the first turn.
    Creating it here (rather than typing `/name` in a fresh TUI) gives the viewer a stable id and name
    that peer sessions can address, and puts it in the workspace the role belongs to.
    """
    import queue
    import threading
    p = subprocess.Popen([muse_bin(), "serve"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.DEVNULL, env=turn_env(), cwd=cwd)
    q: queue.Queue = queue.Queue()
    threading.Thread(target=lambda: [q.put(json.loads(ln)) for ln in iter(p.stdout.readline, b"")],
                     daemon=True).start()
    n = [0]

    def send(o: dict) -> None:
        p.stdin.write((json.dumps(o) + "\n").encode())
        p.stdin.flush()

    def call(method: str, params: dict) -> dict:
        n[0] += 1
        send({"jsonrpc": "2.0", "id": n[0], "method": method, "params": params})
        end = time.time() + 60
        while time.time() < end:
            try:
                j = q.get(timeout=0.5)
            except queue.Empty:
                continue
            if j.get("id") == n[0]:
                if "error" in j:
                    raise RuntimeError(f"{method}: {j['error'].get('message', j['error'])}")
                return j.get("result", {})
        raise RuntimeError(f"{method}: no answer in 60 s")

    try:
        call("initialize", {"clientInfo": {"name": "shoal", "version": "0"}})
        send({"jsonrpc": "2.0", "method": "initialized", "params": {}})
        params = {"commandId": uuid7(), "workspaceRoot": cwd}
        if model:
            params["modelId"] = model
        sid = call("session/start", params)["session"]["sessionId"]
        call("session/rename", {"commandId": uuid7(), "sessionId": sid, "name": name})
        return sid
    finally:
        p.terminate()


def cmd_muse_new(a) -> int:
    """Create a named interactive Muse session and (with --pick) give it a Herdr card."""
    name = a.name.lower()
    if name in ("all", "self", "current") or not re.fullmatch(r"[a-z](?:[a-z0-9]|-(?=[a-z0-9])){2,31}", name):
        print(f"muse-new: {a.name!r} is not a valid Muse session name "
              "(3-32 chars: lowercase letters, digits, single hyphens; starts with a letter)")
        return 2
    cwd = os.path.abspath(os.path.expanduser(a.cwd))
    if not os.path.isdir(cwd):
        print(f"muse-new: no such directory: {cwd}")
        return 2
    try:
        sid = msp_session(name, cwd, a.model, a.effort)
    except (RuntimeError, OSError) as e:
        print(f"muse-new: {e}")
        return 1
    print(f"{name}  {sid}  {cwd}")
    pick = [sys.executable, CLI, "ui", "pick", "--harness", "muse", "--id", sid,
            "--label", a.label or name, "--cwd", cwd, "--save"]
    if a.model:
        pick += ["--model", a.model]
    if a.effort:
        pick += ["--effort", a.effort]
    if a.pick:
        return subprocess.call(pick)
    print(f"open it with: shoal {' '.join(pick[2:])}")
    return 0


def arm_watcher(spec, role, dry_run: bool = False) -> str:
    """Shoal runs a Muse role's persistent watcher (the role never arms one): `ledger-watch --wake
    exec:<session>` starts a turn whenever there is work. ALREADY ARMED makes a second start a no-op."""
    if dry_run:
        return "arm (dry-run)"
    from roles import state_dir
    sd = state_dir(spec.sid)
    os.makedirs(sd, exist_ok=True)
    argv = [sys.executable, CLI, "ledger-watch", "--shoal", spec.sid, "--slug", role.slug,
            "--wake", f"exec:{role.session}", "--session", role.session, "--interval", "120"]
    from backends import detached
    log = open(os.path.join(sd, f"ledger-watch-{role.slug}.log"), "a")
    try:
        subprocess.Popen(detached(argv), stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                         start_new_session=True, env=turn_env())
    finally:
        log.close()
    return "armed"


def add_parsers(sub) -> None:
    p = sub.add_parser("muse-run", help="(internal) run one Muse turn on a session and record its result")
    p.add_argument("--session", required=True)
    p.add_argument("--dir", required=True)
    p.add_argument("--prompt-file", required=True)
    p.add_argument("--lock-fd", type=int, default=None, help="an inherited, already locked lock descriptor")
    p.add_argument("--model", default="")
    p.add_argument("--effort", default="")
    p.set_defaults(func=cmd_muse_run)
    n = sub.add_parser("muse-new", help="create a named interactive Muse session (a standalone or a role's seat)")
    n.add_argument("name", help="3-32 chars: lowercase letters, digits, single hyphens")
    n.add_argument("--cwd", default=".", help="the workspace the session runs in")
    n.add_argument("--model", default="")
    n.add_argument("--effort", default="")
    n.add_argument("--label", default="")
    n.add_argument("--pick", action="store_true", help="also save it as a standalone Herdr card (ui pick --save)")
    n.set_defaults(func=cmd_muse_new)
    q = sub.add_parser("muse-tail", help="read-only view of a Muse session's turns")
    q.add_argument("--session", required=True)
    q.add_argument("--once", action="store_true")
    q.set_defaults(func=cmd_muse_tail)
