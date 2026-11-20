#!/usr/bin/env python3
"""Durable action workers: the dialog may close without cancelling a confirmed handoff, offline or freeze.

`act-apply --background` takes the plan's token, saves the plan under a run id and starts `act-run` detached; the run's
output goes to its log as it comes. `act-status` reads a run by id, or the newest run for a target (a handoff's role or
seat, an offline or freeze's shoal or scope), so a dialog closed and opened again finds it."""
from __future__ import annotations

import json
import os
import secrets
import subprocess
import sys
import time


def root() -> str:
    import ui_status
    path = os.path.join(os.path.dirname(ui_status.cache_path()), "act-runs")
    os.makedirs(path, mode=0o700, exist_ok=True)
    return path


def path(run: str, suffix: str = ".json") -> str:
    import ui_actions
    if len(run) != 32 or any(c not in "0123456789abcdef" for c in run):
        raise ui_actions.ActError("invalid action run id")
    return os.path.join(root(), run + suffix)


def save(run: str, state: dict) -> None:
    tmp = path(run, f".{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(state, fh)
    os.replace(tmp, path(run))


def read(run: str) -> dict:
    import ui_actions
    try:
        with open(path(run)) as fh:
            return json.load(fh)
    except FileNotFoundError as exc:
        raise ui_actions.ActError("no saved action run") from exc


RUNNING_TAIL = 16000  # bytes of a running action's log a status call returns
FINISHED_TAIL = 200000  # and of a finished one's: its whole result (an `offline off` lists every recorded ask)


def verb_of(state: dict) -> str:
    return str((state.get("plan") or {}).get("verb") or "handoff")


def public(run: str, state: dict) -> dict:
    if state.get("running"):
        pid = state.get("pid")
        try:
            with open(f"/proc/{int(pid)}/cmdline", "rb") as cmd:
                alive = run.encode() in cmd.read().split(b"\0")
        except (OSError, TypeError, ValueError):
            alive = False
        if not alive and (pid or time.time() - state.get("created", 0) > 30):
            fresh = read(run)
            if not fresh.get("running"):
                return public(run, fresh)
            state.update(running=False, rc=1, finished=time.time())
            with open(path(run, ".log"), "a") as log:
                log.write(f"\n{verb_of(state).capitalize()} worker ended before reporting completion; "
                          "inspect this log before retrying.\n")
            save(run, state)
    progress = path(run, ".log")
    running = state.get("running", False)
    try:
        with open(progress, "rb") as fh:
            fh.seek(max(0, os.fstat(fh.fileno()).st_size - (RUNNING_TAIL if running else FINISHED_TAIL)))
            output = fh.read().decode(errors="replace")
    except OSError:
        output = f"Starting the background {verb_of(state)}…"
    plan = state.get("plan") or {}
    return {"v": 1, "kind": "shoal-act-result", "verb": verb_of(state), "run": run,
            "shoal": plan.get("shoal") or "", "scope": plan.get("scope") or "", "role": plan.get("role") or "",
            "running": running, "rc": state.get("rc", 0), "created": state.get("created", 0),
            "finished": state.get("finished", 0),
            "progress": progress, "command": state.get("command", ""), "output": output}


def start(token: str) -> dict:
    import backends
    import ui_actions
    plan = ui_actions.take_plan(token)
    if plan.get("verb") not in ui_actions.DETACHED_TIMEOUT_S:
        raise ui_actions.ActError(f"background apply is for {', '.join(ui_actions.DETACHED_TIMEOUT_S)} plans")
    run = secrets.token_hex(16)
    state = {"plan": plan, "running": True, "created": time.time(), "rc": 0}
    save(run, state)
    try:
        with open(path(run, ".log"), "a") as log:
            os.chmod(path(run, ".log"), 0o600)
            process = backends.spawn_detached(
                [sys.executable, os.path.join(ui_actions.PKG, "cli.py"), "ui", "act-run", "--run", run],
                stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                env=dict(os.environ, PYTHONUNBUFFERED="1"))
        # The worker owns state after spawn. Do not overwrite a fast completion with a running snapshot.
        if process.poll() not in (None, 0):
            state.update(running=False, rc=1)
            save(run, state)
    except OSError as exc:
        state.update(running=False, rc=1)
        save(run, state)
        raise ui_actions.ActError(f"could not start the {plan.get('verb')}: {exc}") from exc
    return public(run, read(run))


def worker(run: str) -> int:
    import fcntl
    import ui_actions
    with open(path(run, ".lock"), "a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 1
        state = read(run)
        if not state.get("running"):
            return 1
        state["pid"] = os.getpid()
        save(run, state)
        try:
            result = ui_actions.execute(state["plan"], path(run, ".log"))
            state.update(result, running=False, finished=time.time())
        except Exception as exc:  # a failed run remains visible after the dialog closes
            print(f"{verb_of(state).capitalize()} failed: {exc}", flush=True)
            state.update(running=False, rc=1, finished=time.time())
        save(run, state)
        return state["rc"]


def status(a) -> dict:
    import ui_actions
    if a.run:
        return public(a.run, read(a.run))
    found = []
    for filename in os.listdir(root()):
        if not filename.endswith(".json"):
            continue
        run = filename[:-5]
        try:
            state = read(run)
            p = state.get("plan", {})
            verb = getattr(a, "verb", "") or ""
            if verb and p.get("verb") != verb:
                match = False
            elif a.session:
                match = p.get("session") == a.session
            elif getattr(a, "scope", ""):
                match = p.get("scope") == a.scope
            elif verb and verb != "handoff":
                # one shoal's offline or freeze (a role-less verb): not a scoped run that included it
                match = bool(a.shoal) and p.get("shoal") == a.shoal and not p.get("scope")
            else:
                match = bool(a.shoal and a.role) and p.get("shoal") == a.shoal and p.get("role") == a.role
            if match:
                found.append((state.get("created", 0), run, state))
        except (OSError, ValueError):
            continue
    if not found:
        return {"v": 1, "kind": "shoal-act-result", "rc": 0, "running": False, "run": None, "output": "", "command": ""}
    _, run, state = max(found, key=lambda r: r[0])
    return public(run, state)


def cmd_status(a) -> int:
    import ui_actions
    try:
        ui_actions._emit(status(a), a.json)
        return 0
    except (OSError, ValueError) as exc:
        ui_actions._emit({"v": 1, "kind": "shoal-act-error", "error": str(exc)}, a.json)
        return 2


def add_parsers(u) -> None:
    q = u.add_parser("act-run", help="internal: execute one saved background action")
    q.add_argument("--run", required=True)
    q.set_defaults(func=lambda a: worker(a.run))
    q = u.add_parser("act-status", help="follow a saved background action (handoff, offline, freeze), "
                     "including after closing the dialog")
    q.add_argument("--run", default="")
    q.add_argument("--verb", default="", help="the newest run of this verb only (offline, freeze: by --shoal or --scope)")
    q.add_argument("--shoal", default="")
    q.add_argument("--role", default="")
    q.add_argument("--scope", default="", help="offline, freeze: a run across shoals (`all` or `host:<name>`)")
    q.add_argument("--session", default="")
    q.add_argument("--json", action="store_true")
    q.set_defaults(func=cmd_status)
