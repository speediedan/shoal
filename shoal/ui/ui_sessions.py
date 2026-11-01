"""Stored interactive sessions for the picker, through read-only local harness APIs.

Listing never starts or resumes a session, takes its lock, or requests a model turn.
"""
from __future__ import annotations

import json
import os
import queue
import shutil
import signal
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (os.path.dirname(HERE), HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import config  # noqa: E402


def _list(argv: list[str], env: dict, method: str, params: dict, timeout: float = 10) -> dict:
    """Initialize a short-lived stdio server, issue one list request, and reap its process group."""
    p = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                         env=env, start_new_session=True)
    replies: queue.Queue = queue.Queue()

    def reader():
        try:
            for line in p.stdout:
                replies.put(json.loads(line))
        except (OSError, ValueError) as exc:
            replies.put(exc)
        finally:
            replies.put(EOFError("session listing server closed its output"))

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    deadline = time.monotonic() + timeout

    def send(value):
        p.stdin.write((json.dumps(value) + "\n").encode())
        p.stdin.flush()

    def call(n, method, params):
        send({"jsonrpc": "2.0", "id": n, "method": method, "params": params})
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                raise TimeoutError("session listing timed out")
            try:
                msg = replies.get(timeout=left)
            except queue.Empty:
                raise TimeoutError("session listing timed out") from None
            if isinstance(msg, Exception):
                raise RuntimeError(str(msg))
            if not isinstance(msg, dict):
                raise ValueError("session listing returned a non-object reply")
            if msg.get("id") != n:
                continue
            if "error" in msg:
                raise RuntimeError(str(msg["error"].get("message", "session listing failed")))
            result = msg.get("result", {})
            if not isinstance(result, dict):
                raise ValueError("session listing returned a non-object result")
            return result

    try:
        call(1, "initialize", {"clientInfo": {"name": "shoal", "version": "1"}})
        send({"jsonrpc": "2.0", "method": "initialized", "params": {}})
        return call(2, method, params)
    finally:
        # The CLI may be a wrapper with a native child. Reap only the group we created.
        try:
            os.killpg(p.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            p.wait(timeout=2)
        except subprocess.TimeoutExpired:
            os.killpg(p.pid, signal.SIGKILL)
            p.wait(timeout=2)
        p.stdin.close()
        thread.join(timeout=1)
        p.stdout.close()


def stored_sessions(harness: str) -> list[dict]:
    """The 100 most recently updated root sessions; for Codex, interactive threads and named headless ones."""
    if harness == "codex":
        import codex_harness as backend
        name, args, method = "codex", ["app-server", "--stdio"], "thread/list"
        binary = config.env("CODEX_BIN") or name
        # omitted, sourceKinds means interactive sources only, which hides a seat `codex-new` made with `codex exec`
        params = {"limit": 100, "sortKey": "updated_at", "sourceKinds": ["cli", "vscode", "exec", "appServer"]}
    else:
        import muse as backend
        name, args, method = "muse", ["serve"], "session/list"
        binary = config.env("MUSE_BIN") or name
        params = {"limit": 100}
    env = config.tool_env(backend.turn_env())
    executable = shutil.which(binary, path=env["PATH"])
    if not executable:
        return []  # a harness that is not installed is not a failed provider
    if harness == "codex" and config.env("CODEX_HOME"):
        env["CODEX_HOME"] = config.env("CODEX_HOME")
    result = _list([executable, *args], env, method, params)
    out = []
    rows = result.get("data" if harness == "codex" else "sessions", [])
    if not isinstance(rows, list):
        raise ValueError("session listing returned a non-list page")
    for row in rows:
        if not isinstance(row, dict):
            continue
        if row.get("kind") == "subagent":
            continue
        if harness == "codex" and row.get("source") in ("exec", "appServer") and not row.get("name"):
            continue  # an unnamed headless run (a role turn, a matcher); `codex-new` names its seat
        sid = row.get("id" if harness == "codex" else "sessionId")
        if not sid:
            continue
        e = {"harness": harness, "session": sid, "label": str(row.get("name") or sid[:12])[:40],
             "cwd": row.get("cwd" if harness == "codex" else "workspaceRoot"), "state": "saved"}
        if row.get("modelId"):
            e["model"] = row["modelId"]
        out.append(e)
    return out
