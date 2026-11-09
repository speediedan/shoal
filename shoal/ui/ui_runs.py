#!/usr/bin/env python3
"""Durable action workers: the dialog may close without cancelling a confirmed handoff."""
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
                log.write("\nHandoff worker ended before reporting completion; inspect this log before retrying.\n")
            save(run, state)
    progress = path(run, ".log")
    try:
        with open(progress, "rb") as fh:
            fh.seek(max(0, os.fstat(fh.fileno()).st_size - 16000))
            output = fh.read().decode(errors="replace")
    except OSError:
        output = "Starting the background handoff…"
    return {"v": 1, "kind": "shoal-act-result", "verb": "handoff", "run": run,
            "running": state.get("running", False), "rc": state.get("rc", 0),
            "progress": progress, "command": state.get("command", ""), "output": output}


def start(token: str) -> dict:
    import backends
    import ui_actions
    plan = ui_actions.take_plan(token)
    if plan.get("verb") != "handoff":
        raise ui_actions.ActError("background apply is for handoff plans")
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
        raise ui_actions.ActError(f"could not start the handoff: {exc}") from exc
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
        except Exception as exc:  # a failed handoff remains visible after the dialog closes
            print(f"Handoff failed: {exc}", flush=True)
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
            if a.session:
                match = p.get("session") == a.session
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
    q = u.add_parser("act-status", help="follow a saved background handoff, including after closing the dialog")
    q.add_argument("--run", default="")
    q.add_argument("--shoal", default="")
    q.add_argument("--role", default="")
    q.add_argument("--session", default="")
    q.add_argument("--json", action="store_true")
    q.set_defaults(func=cmd_status)
