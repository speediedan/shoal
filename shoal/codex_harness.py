#!/usr/bin/env python3
"""Codex CLI (`codex`, OpenAI's coding agent) as a shoal harness: ONE `codex exec` PROCESS PER WAKE.

The same design as Muse (shoal/muse.py has the reasoning): sessions (Codex "threads") are durable files, so
`codex exec resume <thread-id>` continues the same session; an idle role costs no process and no tokens; shoal,
not the role, runs the persistent watcher, so a Codex role never needs permission to start a detached process.

One difference decides the shape of this module: Muse accepts a session id on its first turn
(`--session-id <uuid>`), Codex mints the thread id itself and prints it as the first event of the first turn
(`{"type":"thread.started","thread_id":...}`). So the first turn runs under a temporary state key
(`new-<hex>`); the runner reads the thread id from that first event, hard-links the turn's lock file into the
thread's own state directory (the same inode, so the thread reads busy from then on) and switches to it, and
`create_session` returns once the id is known. Later turns key on the thread id from the start.

State per thread lives in `<state root>/<thread id>/` (default /tmp/shoal-codex-<uid>), as for Muse:
  lock         flock held by the process running a turn (the CALLER takes it; the runner inherits it)
  run.jsonl    every JSONL event `codex exec --json` printed, all turns appended
  run.err      its stderr
  last.json    the latest turn: started, ended, rc, terminal (completed|failed), text, error
  prompt-*.txt the prompts, handed to codex on stdin (`-`)

Posture: unattended turns run with `--dangerously-bypass-approvals-and-sandbox` (nobody can answer an approval
prompt, and a sandboxed shell cannot push or reach the CI tooling: the same decision as for Muse, 2026-10-15,
applied to Codex 2026-10-18 pending the maintainer's confirmation). The environment of every turn drops
`OPENAI_API_KEY` and `CODEX_API_KEY` (a key outranks the ChatGPT-plan login and bills per token; override with
SHOAL_CODEX_ALLOW_API_KEY=1) and every `CLAUDE_CODE_*` variable (a Codex process is not the Claude session that
happened to start it). `codex exec` waits on stdin when it is a pipe: the prompt is piped on purpose, and a
caller with no prompt must close stdin.
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
POSTURE = ["--dangerously-bypass-approvals-and-sandbox"]
# PROVISIONAL until the first real usage-limit error is seen on this host (calibrate then, as for Muse)
QUOTA_RE = re.compile(r"(rate.?limit|usage.?limit|quota|out of credits|insufficient|too many requests|\b429\b"
                      r"|try again (in|at|after))", re.I)
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
THREAD_WAIT_S = 120


class BusyError(RuntimeError):
    """A turn is already running for the session (its lock is held) or an interactive client holds it."""


def state_root() -> str:
    return config.env("CODEX_STATE") or os.path.join(tempfile.gettempdir(), f"shoal-codex-{os.getuid()}")


def sdir(session: str, create: bool = False) -> str:
    d = os.path.join(state_root(), session)
    if create:
        os.makedirs(d, exist_ok=True)
    return d


def codex_bin() -> str:
    return config.env("CODEX_BIN") or shutil.which("codex") or os.path.expanduser("~/.nvm/versions/node/current/bin/codex")


def turn_env() -> dict:
    env = dict(os.environ)
    if not config.env("CODEX_ALLOW_API_KEY"):
        for k in ("OPENAI_API_KEY", "CODEX_API_KEY"):
            env.pop(k, None)
    for k in [k for k in env if k.startswith("CLAUDE_CODE_") or k == "CLAUDECODE"]:
        env.pop(k)
    return env


def attached_client(session: str) -> int:
    """Pid of an interactive Codex client (`codex resume <thread>`) holding the thread, else 0. Codex keeps
    a writer lock per thread, so a second owner fails or waits; the watcher must hold the wake instead
    (the same rule as for Muse, measured there 2026-10-15). The client is `node .../codex resume <id>` with a
    native `codex resume <id>` child: either matches."""
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
        for i in (0, 1, 2):
            if (len(argv) > i + 2 and os.path.basename(argv[i]) == "codex" and argv[i + 1] == "resume"
                    and session in argv[i + 2:]):
                return int(p)
    return 0


def busy(session: str) -> bool:
    """True while a turn holds the thread's lock or an interactive client is attached."""
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


ACTIVE_WINDOW_S = 120  # a thread whose rollout file changed within this long is mid-turn, not just held open


def activity_age(session: str):
    """Seconds since the thread's rollout file last changed, or None when it cannot be found. `busy()` is
    also true for a client that is merely attached, which a viewer must not draw as working."""
    import glob
    root = config.env("CODEX_HOME") or os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex")
    try:
        files = glob.glob(os.path.join(root, "sessions", "*", "*", "*", f"rollout-*-{session}.jsonl"))
        return max(0.0, time.time() - max(os.path.getmtime(f) for f in files))
    except (OSError, ValueError):
        return None


def status_line(session: str) -> str:
    """`<id8> status:busy|idle|unknown ...`, the shape the other backends' consumers match on. An attached
    client adds `attached`, then `active` or `quiet` by the rollout file's age (none: unreadable)."""
    fake = None
    try:
        from backends import fake_sessions
        fake = fake_sessions()
    except Exception:  # noqa: BLE001
        pass
    if fake is not None and "codex" in fake:  # a fake world that names no codex sessions leaves codex real
        return str((fake.get("codex") or {}).get(session, ""))
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
    """'' or a reset hint, from the last turn's failure."""
    last = last_run(session)
    if last.get("terminal") not in ("failed", "error") and last.get("rc") in (0, None):
        return ""
    blob = f"{last.get('reason') or ''} {last.get('error') or ''} {last.get('text') or ''}"
    if not QUOTA_RE.search(blob):
        return ""
    m = re.search(r"(?:resets?|try again) (?:at |in |after )?([^.\n|]{0,40})", blob, re.I)
    return (m.group(1).strip() if m else "") or "reset time unknown"


def run_argv(session: str, repo_dir: str, model: str = "", effort: str = "") -> list[str]:
    """The command for one turn; the prompt arrives on stdin (`-`). `session` is a thread id, or empty for a
    thread's first turn."""
    flags = [*POSTURE, "--json"]
    if model:
        flags += ["-m", model]
    if effort:
        flags += ["-c", f'model_reasoning_effort="{effort}"']
    extra = config.env("CODEX_ARGS")
    if extra:
        flags += shlex.split(extra)
    if session:
        return [codex_bin(), "exec", "resume", session, *flags, "-"]
    return [codex_bin(), "exec", *flags, "-C", repo_dir, "-"]


def start_run(session: str, repo_dir: str, prompt: str, model: str = "", effort: str = "",
              dry_run: bool = False) -> str:
    """Start one turn in the background and return its state key at once. `session` empty starts a new
    thread (the key is then temporary; `create_session` waits for the thread id). The lock is taken HERE and
    inherited by the runner, so the thread reads busy from the moment this returns."""
    if dry_run:
        return session
    if session and attached_client(session):
        raise BusyError(f"codex thread {session[:8]} has an interactive client attached")
    key = session or "new-" + uuid.uuid4().hex[:12]
    d = sdir(key, create=True)
    lock_fh = open(os.path.join(d, "lock"), "a+")
    try:
        fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock_fh.close()
        raise BusyError(f"codex thread {session[:8]} is already running a turn")
    pf = os.path.join(d, f"prompt-{int(time.time() * 1000)}.txt")
    with open(pf, "w") as fh:
        fh.write(prompt)
    argv = [sys.executable, CLI, "codex-run", "--key", key, "--dir", repo_dir, "--prompt-file", pf,
            "--lock-fd", str(lock_fh.fileno())]
    if session:
        argv += ["--session", session]
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
    return key


def create_session(repo_dir: str, brief: str, model: str = "", effort: str = "") -> str:
    """Start a thread's first turn and return its thread id (read from the first event)."""
    key = start_run("", repo_dir, brief, model=model, effort=effort)
    path = os.path.join(sdir(key), "thread_id")
    end = time.time() + THREAD_WAIT_S
    while time.time() < end:
        try:
            tid = open(path).read().strip()
            if tid:
                return tid
        except OSError:
            pass
        last = last_run(key)
        if last.get("ended") and not os.path.exists(path):
            raise RuntimeError("codex exec ended before a thread started: " + (last.get("error") or "no output")[-300:])
        time.sleep(0.3)
    raise RuntimeError(f"no thread id from codex exec within {THREAD_WAIT_S} s (see {sdir(key)}/run.err)")


def cmd_codex_run(a) -> int:
    """The runner: one `codex exec` turn, its events appended to run.jsonl, its result to last.json."""
    key = a.key
    d = sdir(key, create=True)
    session = a.session or ""
    held = None
    if attached_client(session) if session else False:
        print(f"busy: an interactive client is attached to thread {session[:8]}")
        return 75
    if a.lock_fd is None:  # run by hand: take the lock itself
        held = open(os.path.join(d, "lock"), "a+")
        try:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(f"busy: thread {key[:8]} is already running a turn")
            return 75
    start = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    last = {"session": session or key, "started": start, "pid": os.getpid(), "prompt": os.path.basename(a.prompt_file)}
    _write_last(d, last)
    terminal: dict = {}
    text = ""
    err_tail = ""
    rc = 1
    out = None
    cur = d  # the directory the turn's records go to: the thread's own once its id is known
    try:
        err = open(os.path.join(d, "run.err"), "a")
        err_from = err.tell()  # this turn's stderr starts here: an older turn's failure must not read as this one's
        err.write(f"--- {start} {' '.join(run_argv(session, a.dir, a.model, a.effort)[:4])} ...\n")
        err.flush()
        with open(a.prompt_file) as pin:
            p = subprocess.Popen(run_argv(session, a.dir, a.model, a.effort), stdin=pin, stdout=subprocess.PIPE,
                                 stderr=err, text=True, cwd=a.dir if os.path.isdir(a.dir) else None, env=turn_env())
            pending: list[str] = []
            out = open(os.path.join(d, "run.jsonl"), "a")
            for line in p.stdout:
                try:
                    ev = json.loads(line)
                except ValueError:
                    ev = {}
                if ev.get("type") == "thread.started" and not session:
                    session = ev.get("thread_id") or ""
                    if session:
                        cur = sdir(session, create=True)
                        tl = os.path.join(cur, "lock")
                        try:
                            os.unlink(tl)
                        except OSError:
                            pass
                        os.link(os.path.join(d, "lock"), tl)  # one inode: the thread reads busy from here on
                        out.close()
                        out = open(os.path.join(cur, "run.jsonl"), "a")
                        last["session"] = session
                        _write_last(cur, last)
                        with open(os.path.join(d, "thread_id"), "w") as fh:
                            fh.write(session + "\n")
                out.write(line)
                out.flush()
                t = ev.get("type")
                if t == "item.completed" and (ev.get("item") or {}).get("type") == "agent_message":
                    text = (ev["item"].get("text") or "")
                elif t == "turn.completed":
                    terminal = {"terminal": "completed", "text": text}
                elif t in ("turn.failed", "error"):
                    msg = ((ev.get("error") or {}).get("message") if isinstance(ev.get("error"), dict)
                           else ev.get("message")) or ""
                    terminal = {"terminal": "failed", "reason": msg[:500], "text": text}
            rc = p.wait()
        err.close()
        try:
            ep = os.path.join(d, "run.err")
            with open(ep, "rb") as fh:
                fh.seek(max(err_from, os.path.getsize(ep) - 600))
                err_tail = fh.read().decode(errors="replace")
        except OSError:
            pass
    except OSError as exc:  # codex missing or unrunnable
        err_tail = f"cannot run {codex_bin()}: {exc}"
    finally:
        if out is not None:
            out.close()
    last.update(ended=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), rc=rc,
                terminal=terminal.get("terminal") or ("failed" if rc else "completed"),
                reason=terminal.get("reason"), text=(terminal.get("text") or text or "")[:2000],
                error=err_tail.strip()[-500:] if (rc or terminal.get("terminal") == "failed") else "")
    _write_last(cur, last)
    if cur != d:
        _write_last(d, last)  # the temporary key's own record, so create_session sees the end
    if held is not None:
        held.close()
    return rc


def _write_last(d: str, last: dict) -> None:
    tmp = os.path.join(d, "last.json.tmp")
    with open(tmp, "w") as fh:
        json.dump(last, fh)
    os.replace(tmp, os.path.join(d, "last.json"))


def messages(session: str, limit: int = 20) -> str:
    """The final text of the latest turns, newest last."""
    out = []
    text = ""
    try:
        with open(os.path.join(sdir(session), "run.jsonl")) as fh:
            for line in fh:
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                t = ev.get("type")
                if t == "item.completed" and (ev.get("item") or {}).get("type") == "agent_message":
                    text = ev["item"].get("text") or ""
                elif t == "turn.completed":
                    out.append(f"[completed] {text}")
                    text = ""
                elif t == "turn.failed":
                    out.append(f"[failed] {(ev.get('error') or {}).get('message', '')}")
    except OSError:
        return ""
    return "\n".join(out[-limit:])


def interactive_view(session: str, model: str = "", effort: str = "") -> int:
    """Explicit native access, serialized with start_run so a watcher cannot win the attach race. The client is
    checked first: one opened from another observer holds this lock too, and is not a turn."""
    import native_client
    pid = attached_client(session)
    if pid:
        raise BusyError(f"{native_client.held_message(pid)}; the live view remains available")
    with open(os.path.join(sdir(session, create=True), "lock"), "a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise BusyError("a turn is running; the live view remains available")
        print("Native interactive mode pauses automatic wakes until you exit Codex (/exit).", flush=True)
        return subprocess.run([codex_bin(), "resume", session, *interactive_flags(model, effort)],
                              env=turn_env()).returncode


def observer_argv(session: str, model: str = "", effort: str = "") -> list[str]:
    """A role always opens an observer, including between turns. Native access requires an explicit key."""
    return [sys.executable, CLI, "codex-tail", "--session", session, "--allow-interactive",
            "--model", model, "--effort", effort]


def cmd_codex_tail(a) -> int:
    """A read-only view of a thread's turns that holds no lock. q/Escape/Ctrl+C returns to the card. It never starts
    or resumes anything unless a role's card allows `i`."""
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "ui"))
    from ui_view import _getkey
    import native_client
    print(f"Codex {a.session}: live read-only view. Automatic wakes remain enabled.")
    interactive = getattr(a, "allow_interactive", False)
    keys = "[q/esc/ctrl+c] back to card" + ("   [i] native interactive (only when idle; /exit releases it)" if interactive else "")
    print(keys, flush=True)
    shown, last_state = 0, ""
    path = os.path.join(sdir(a.session), "run.jsonl")
    try:
        while True:
            if os.path.exists(path):
                with open(path) as fh:
                    lines = fh.readlines()
                for line in lines[shown:]:
                    try:
                        ev = json.loads(line)
                    except ValueError:
                        continue
                    t = ev.get("type")
                    if t == "turn.started":
                        print("\n--- turn started ---")
                    elif t == "item.completed" and (ev.get("item") or {}).get("type") == "agent_message":
                        print(ev["item"].get("text") or "")
                    elif t == "turn.completed":
                        print("--- turn completed ---")
                    elif t == "turn.failed":
                        print(f"--- turn failed: {(ev.get('error') or {}).get('message', '')} ---")
                shown = len(lines)
            if a.once:
                return 0
            state = native_client.state(sys.modules[__name__], a.session)
            if state != last_state:
                print(f"\n[{state}] {keys}", flush=True)
                last_state = state
            k = _getkey(2)
            if k in ("q", "Q", "\x1b", "\x03"):
                return 0
            if interactive:
                native_client.on_key(sys.modules[__name__], k, a.session, getattr(a, "model", ""), getattr(a, "effort", ""), keys)
    except KeyboardInterrupt:
        return 0


def interactive_flags(model: str = "", effort: str = "") -> list[str]:
    """Flags for an interactive (TUI) client. The sandbox and approval posture come from the host's
    config.toml; only the seat's own model and effort are passed (its spec role, or the registry entry
    `codex-new --pick` saved), falling back to the host model. `SHOAL_CODEX_VIEW_ARGS` replaces the whole list."""
    extra = config.env("CODEX_VIEW_ARGS")
    if extra:
        return extra.split()
    flags = []
    m = model or config.env("CODEX_MODEL")
    if m:
        flags += ["-m", m]
    if effort:
        flags += ["-c", f'model_reasoning_effort="{effort}"']
    return flags


def view_argv(session: str, model: str = "", effort: str = "") -> list[str]:
    """The viewer's command: the interactive client takes the thread over, which is only safe while no turn
    runs; mid-turn the read-only tail is shown instead."""
    if busy(session):
        return [sys.executable, CLI, "codex-tail", "--session", session]
    flags = interactive_flags(model, effort)
    if UUID_RE.match(session) or os.path.isdir(sdir(session)):
        return [codex_bin(), "resume", session, *flags]
    # a standalone session by NAME: resume it, or start a fresh one the first time (name it with /rename)
    fl = "".join(" " + shlex.quote(f) for f in flags)
    return ["sh", "-c", f'"$0" resume "$1"{fl} || exec "$0"{fl}', codex_bin(), session]


def app_server(calls: list[tuple[str, dict]], timeout: float = 60) -> list[dict]:
    """Run JSON-RPC requests, in order, against a short-lived `codex app-server` (stdio): the results, one per call.
    The first error raises. The app-server is how a thread is named, listed, archived or deleted: the CLI's own
    `archive`/`delete` verbs wrap the same requests."""
    import queue
    import threading
    p = subprocess.Popen([codex_bin(), "app-server"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.DEVNULL, text=True, env=turn_env())
    q: queue.Queue = queue.Queue()
    threading.Thread(target=lambda: [q.put(ln) for ln in p.stdout], daemon=True).start()

    def send(o: dict) -> None:
        p.stdin.write(json.dumps(o) + "\n")
        p.stdin.flush()

    def answer(i: int, method: str) -> dict:
        end = time.time() + timeout
        while time.time() < end:
            try:
                j = json.loads(q.get(timeout=0.5))
            except queue.Empty:
                if p.poll() is not None:
                    break
                continue
            except ValueError:
                continue
            if j.get("id") == i:
                if "error" in j:
                    raise RuntimeError(f"{method}: {j['error'].get('message', j['error'])}")
                return j.get("result") or {}
        raise RuntimeError(f"app-server gave no answer to {method} in {timeout:g} s")

    try:
        send({"method": "initialize", "id": 0, "params": {"clientInfo": {"name": "shoal", "title": "shoal", "version": "0"}}})
        answer(0, "initialize")
        send({"method": "initialized", "params": {}})
        out = []
        for i, (method, params) in enumerate(calls, start=1):
            send({"method": method, "id": i, "params": params})
            out.append(answer(i, method))
        return out
    finally:
        p.terminate()


def thread_name_set(thread_id: str, name: str) -> None:
    """Give a persisted thread its user-facing name through a short-lived `codex app-server` (stdio JSON-RPC,
    `thread/name/set`): `codex resume <name>` and `codex queue --thread <name>` then find it. The CLI has no
    flag for this; the TUI's /rename is the interactive form."""
    app_server([("thread/name/set", {"threadId": thread_id, "name": name})])


def list_threads(limit: int = 1000) -> list[dict]:
    """Every unarchived thread of every source (interactive, `codex exec`, app-server), newest first, paged through
    `thread/list`. Role turns run as `codex exec`, which the default (interactive only) filter hides."""
    rows: list[dict] = []
    cursor = None
    while len(rows) < limit:
        params = {"limit": 100, "sortKey": "updated_at", "sourceKinds": ["cli", "vscode", "exec", "appServer"]}
        if cursor:
            params["cursor"] = cursor
        page = app_server([("thread/list", params)])[0]
        rows += [r for r in page.get("data") or [] if isinstance(r, dict)]
        cursor = page.get("nextCursor")
        if not cursor:
            break
    return rows


def prune_thread(thread_id: str, delete: bool = False) -> str:
    """Archive a thread (`thread/archive`: a flag on its one stored rollout, undone by `codex unarchive`), or with
    `delete` remove it (`thread/delete`); then drop shoal's own per-thread state. Callers check that it is idle."""
    method = "thread/delete" if delete else "thread/archive"
    app_server([(method, {"threadId": thread_id})])
    shutil.rmtree(sdir(thread_id), ignore_errors=True)
    return ("deleted" if delete else "archived") + f" thread {thread_id}"


def cmd_codex_new(a) -> int:
    """Start a named interactive Codex thread: a first turn (Codex has no empty thread) whose prompt is the
    seat's bootstrap, then its name; with --pick, a Herdr card for it (`ui pick --harness codex`)."""
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", a.name):
        print(f"codex-new: {a.name!r} is not a usable thread name (letters, digits, . _ -)")
        return 2
    cwd = os.path.abspath(os.path.expanduser(a.cwd))
    if not os.path.isdir(cwd):
        print(f"codex-new: no such directory: {cwd}")
        return 2
    prompt = open(a.prompt_file).read() if a.prompt_file else a.prompt
    if not prompt.strip():
        print("codex-new: a first prompt is required (--prompt or --prompt-file): Codex has no empty thread")
        return 2
    try:
        tid = create_session(cwd, prompt, model=a.model, effort=a.effort)
        end = time.time() + a.wait
        while busy(tid) and time.time() < end:
            time.sleep(1)
        if busy(tid):
            print(f"codex-new: the first turn is still running after {a.wait} s; name it later with /rename")
            return 1
        thread_name_set(tid, a.name)
    except (RuntimeError, OSError) as e:
        print(f"codex-new: {e}")
        return 1
    print(f"{a.name}  {tid}  {cwd}")
    pick = [sys.executable, CLI, "ui", "pick", "--harness", "codex", "--id", tid,
            "--label", a.label or a.name, "--cwd", cwd, "--save"]
    if a.model:
        pick += ["--model", a.model]
    if a.effort:
        pick += ["--effort", a.effort]
    if a.pick:
        return subprocess.call(pick)
    print(f"open it with: shoal {' '.join(pick[2:])}")
    return 0


def arm_watcher(spec, role, dry_run: bool = False) -> str:
    """Shoal runs a Codex role's persistent watcher: `ledger-watch --wake exec:<thread>` starts a turn whenever
    there is work. ALREADY ARMED makes a second start a no-op."""
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
    p = sub.add_parser("codex-run", help="(internal) run one Codex turn on a thread and record its result")
    p.add_argument("--key", required=True, help="the state key: the thread id, or new-<hex> for a first turn")
    p.add_argument("--session", default="", help="the thread id (absent on a thread's first turn)")
    p.add_argument("--dir", required=True)
    p.add_argument("--prompt-file", required=True)
    p.add_argument("--lock-fd", type=int, default=None, help="an inherited, already locked lock descriptor")
    p.add_argument("--model", default="")
    p.add_argument("--effort", default="")
    p.set_defaults(func=cmd_codex_run)
    n = sub.add_parser("codex-new", help="start a named interactive Codex thread (a first turn, then its name)")
    n.add_argument("name")
    n.add_argument("--cwd", default=".")
    n.add_argument("--prompt", default="")
    n.add_argument("--prompt-file", default="")
    n.add_argument("--model", default="")
    n.add_argument("--effort", default="")
    n.add_argument("--label", default="")
    n.add_argument("--wait", type=int, default=900, help="seconds to wait for the first turn to finish")
    n.add_argument("--pick", action="store_true", help="also save it as a standalone Herdr card (ui pick --save)")
    n.set_defaults(func=cmd_codex_new)
    q = sub.add_parser("codex-tail", help="read-only view of a Codex thread's turns")
    q.add_argument("--session", required=True)
    q.add_argument("--once", action="store_true")
    q.add_argument("--allow-interactive", action="store_true", help="offer explicit native access with i while idle")
    q.add_argument("--model", default="")
    q.add_argument("--effort", default="")
    q.set_defaults(func=cmd_codex_tail)
