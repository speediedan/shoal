#!/usr/bin/env python3
"""A handoff from a session that stopped on its provider's usage limit, and so cannot write its own checkpoint.

Such a session answers no checkpoint request: every turn it is woken for ends at once on the limit (measured
2026-11-14: a Codex role claimed an issue, edited five files in a worktree, and hit the limit before its next turn
could record any of it in its start doc). Waiting for its restart-ready mark would run to the deadline, and forcing
the handoff would lose what it did after its doc's last stamp. So the handoff detects the stop (`limit_stop`), goes
ahead without the mark, and hands the new session a reconciliation brief (`brief`): the start doc's stamp, the
predecessor's transcript and how much it did after that stamp, to be read and written into the start doc before new
work. The operator sees the same facts in the handoff's output (the dialog's result).
"""
from __future__ import annotations

import datetime
import glob
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

TAIL_BYTES = 1048576
TOOL_MARKS = {"codex": ('"type":"function_call"', '"type":"custom_tool_call"'), "claude": ('"type":"tool_use"',),
              "muse": ('"tool_call"', '"tool_use"')}


def transcript(ecosystem: str, session: str) -> str:
    """The session's own record on this host, or ""."""
    if not session:
        return ""
    if ecosystem == "claude":
        import health
        return health.claude_transcript(session)
    if ecosystem == "codex":
        import config
        root = config.env("CODEX_HOME") or os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex")
        hits = glob.glob(os.path.join(root, "sessions", "*", "*", "*", f"rollout-*-{session}.jsonl"))
        return max(hits, key=os.path.getmtime) if hits else ""
    if ecosystem == "muse":
        import muse
        d = muse.native_session_dir(session)
        return os.path.join(d, "session.jsonl") if d and os.path.isfile(os.path.join(d, "session.jsonl")) else ""
    return ""


def _tail(path: str) -> list[dict]:
    try:
        with open(path, "rb") as fh:
            fh.seek(max(0, os.path.getsize(path) - TAIL_BYTES))
            lines = fh.read().decode(errors="replace").splitlines()
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if isinstance(d, dict):
            out.append(d)
    return out


def _codex_stop(path: str) -> dict | None:
    """The last turn's end, when it failed on the usage limit: Codex records it on `task_complete`."""
    for d in reversed(_tail(path)):
        p = d.get("payload") if isinstance(d.get("payload"), dict) else {}
        if p.get("type") != "task_complete":
            continue
        err = p.get("error") if isinstance(p.get("error"), dict) else {}
        if "usage_limit" in str(err.get("codex_error_info") or ""):
            return {"at": str(d.get("timestamp") or "")[:19] + "Z", "why": str(err.get("message") or "usage limit")}
        return None
    return None


def _claude_stop(path: str) -> dict | None:
    """The newest reply, when it is a usage-limit error or grace note rather than a model's answer."""
    import health
    for d in reversed(_tail(path)):
        if d.get("error") == "rate_limit":
            return {"at": str(d.get("timestamp") or "")[:19] + "Z", "why": "the Claude usage limit"}
        if d.get("type") == "user":
            content = (d.get("message") or {}).get("content")
            text = content if isinstance(content, str) else " ".join(
                str(c.get("text", "")) for c in content or [] if isinstance(c, dict))
            if text.lstrip().startswith(health.LIMIT_GRACE_TEXT):
                return {"at": str(d.get("timestamp") or "")[:19] + "Z", "why": "the Claude usage limit"}
        if d.get("type") == "assistant":
            return None
    return None


def limit_stop(ecosystem: str, session: str) -> dict | None:
    """{at, why} when the session's last turn ended on its provider's usage limit, else None."""
    path = transcript(ecosystem, session)
    if ecosystem == "codex":
        hit = _codex_stop(path) if path else None
        if hit is None:  # the harness's own record of the last headless turn
            import codex_harness
            hint = codex_harness.quota_hint(session)
            last = codex_harness.last_run(session)
            hit = {"at": str(last.get("ended") or ""), "why": f"usage limit (resets {hint})"} if hint else None
        return hit
    if ecosystem == "claude":
        return _claude_stop(path) if path else None
    if ecosystem == "muse":
        import muse
        hint = muse.quota_hint(session)
        return {"at": "", "why": f"usage limit (resets {hint})"} if hint else None
    return None


def tool_calls_since(ecosystem: str, path: str, since: datetime.datetime | None) -> int | None:
    """How many tool calls the transcript records after `since` (None: unknown)."""
    if not path or since is None:
        return None
    marks = TOOL_MARKS.get(ecosystem, ())
    floor = since.strftime("%Y-%m-%dT%H:%M:%S")
    n = 0
    for d in _tail(path):
        if str(d.get("timestamp") or "")[:19] >= floor:
            line = json.dumps(d, separators=(",", ":"))
            n += sum(line.count(m) for m in marks)
    return n


def brief(ecosystem: str, session: str, doc: str, stop: dict) -> tuple[list[str], str]:
    """(lines for the operator, the note for the new session's first prompt)."""
    import startdoc
    path = transcript(ecosystem, session)
    try:
        stamp = startdoc.last_updated(open(os.path.expanduser(doc), encoding="utf-8").read()) if doc else None
    except OSError:
        stamp = None
    stamp_s = stamp.strftime("%Y-%m-%dT%H:%M:%SZ") if stamp else "no stamp"
    calls = tool_calls_since(ecosystem, path, stamp)
    at = f" at {stop['at']}" if stop.get("at") else ""
    lines = [f"{ecosystem} session {session[:8]} stopped on its {stop['why']}{at}: it cannot write a checkpoint, so this "
             "hands off without one and asks the new session to reconcile",
             f"reconcile: start doc stamped {stamp_s}"
             + (f"; {calls} tool call(s) in the predecessor's transcript since" if calls is not None else "")
             + (f"; transcript {path}" if path else "; no transcript on this host")]
    if calls == 0:
        lines.append("reconcile: nothing recorded after the stamp, so the start doc should be current")
    note = (f"Your predecessor ({ecosystem} session {session}) stopped on its provider's usage limit{at} and could not "
            f"write a restart-ready checkpoint. Your start doc was last stamped {stamp_s}. Before new work, reconcile: "
            + (f"read its transcript {path} from that stamp on" if path else "its transcript is not on this host, so "
               "check its worktrees and branches")
            + " for claims, worktrees and uncommitted changes, jobs, mail sent and wakes left unanswered; check them "
            "against the ledger (`shoal show`) and the repositories; record what you find in a 'Handoff "
            "reconciliation' section of your start doc with a fresh `Last updated` stamp; then continue.")
    return lines, note
