#!/usr/bin/env python3
"""Checkpoint-first operator handoff, for a role or a saved standalone seat.

The caller owns the durable progress log. Roles checkpoint through their own ledger queue;
standalone seats refresh their start doc and finish their turn, as in context handover.
No readiness gate is forced by this workflow.

A seat that hands itself off cannot wait for its own turn to end: the handoff is relaunched as a detached
user unit (`detach_self`) that waits until the seat is idle on two checks, then continues without it.
A Claude seat stopped at an open question is never between turns, so the checkpoint wait would run to its
deadline: that is refused at once unless the operator chooses `--through-question`.
"""
from __future__ import annotations

import fcntl
import hashlib
import os
import subprocess
import sys
import time
import types

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

WAIT_S = 3600
POLL_S = 5
LINEAGE_MAX = 10
# what a detached finisher needs from the caller's environment: a transient user unit starts from the manager's,
# which lacks the caller's tool path (measured 2026-11-09: the first self-handover unit could not find its harness)
FINISHER_ENV = ("PATH", "HOME", "USER", "LANG", "TMPDIR", "XDG_CONFIG_HOME", "XDG_RUNTIME_DIR", "CLAUDE_CONFIG_DIR",
                "OPENCODE_DB", "VIRTUAL_ENV", "PYTHONPATH")


class HandoffError(ValueError):
    pass


def note(text: str) -> None:
    print(text, flush=True)


def fingerprint(path: str) -> str:
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def standalone_entry(key: str) -> dict:
    from standalone import load_standalone
    rows = [e for e in load_standalone() if not e.get("host") and key in
            (e.get("label"), e.get("session"), e.get("job"))]
    if len(rows) != 1:
        raise HandoffError("pick one saved standalone session on this host")
    return rows[0]


def between(entry: dict) -> bool:
    import health
    import park
    role = types.SimpleNamespace(ecosystem=entry["harness"], session=entry["session"], slug=entry.get("label", "seat"))
    return not park.session_held(role, entry.get("cwd", "")) and health.session_between_turns(
        role, health.claude_busy() if role.ecosystem == "claude" else {}, entry.get("cwd", "")) is True


def question_open(harness: str, session: str) -> bool:
    """A live Claude session stopped at an open question prompt. Other harnesses report no such state."""
    if harness != "claude" or not session:
        return False
    import backends
    import health
    row = backends.get("claude")._row(session, include_stopped=True)
    return bool(row and row.get("pid")) and health.claude_prompt_open(row, str(row.get("sessionId") or session))


def is_self(entry: dict) -> bool:
    """The caller runs inside this seat's own session: a Claude background job (its job folder or conversation),
    or `SHOAL_SELF_SESSION` for a harness that names neither. A detached finisher clears all three."""
    if os.environ.get("SHOAL_HANDOFF_DETACHED"):
        return False
    job = os.path.basename(os.environ.get("CLAUDE_JOB_DIR", "").rstrip("/"))
    mine = {os.environ.get("SHOAL_SELF_SESSION", ""), os.environ.get("CLAUDE_CODE_SESSION_ID", "")} - {""}
    if entry.get("harness") == "claude" and job and job in (entry.get("job"), entry["session"][:8]):
        return True
    return entry["session"] in mine


def settled(entry: dict) -> bool:
    """Idle on two checks POLL_S apart: a seat that has just run the command is between tool calls, not done."""
    if not between(entry):
        return False
    time.sleep(POLL_S)
    return between(entry)


def detach_self(a, entry: dict, doc: str) -> int:
    """Relaunch this handoff as a transient user unit that outlives the seat's turn, then return at once so the seat
    can end that turn. The seat's checkpoint must already be written: it is validated here and again by the unit."""
    import shlex
    import startdoc
    from config import env
    label = entry.get("label") or entry["session"][:12]
    if not startdoc.check(doc)["ok"] or startdoc.split_job_folders(doc)[0]:
        raise HandoffError("a seat handing itself off must first write a valid start doc with no live job-folder "
                           "references (it cannot wait on its own turn)")
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    unit = "shoal-handoff-" + "".join(c if c.isalnum() or c in "-_." else "-" for c in label) + "-" + stamp
    root = env("RUNTIME_DIR") or os.path.join(os.environ.get("XDG_RUNTIME_DIR") or f"/tmp/shoal-{os.getuid()}", "shoal-handoff")
    os.makedirs(root, mode=0o700, exist_ok=True)
    log = os.path.join(root, unit + ".log")
    cmd = [sys.executable, os.path.join(HERE, "cli.py"), "handoff", "--session", label, "--no-prepare", "--await-idle",
           "--expected-session", entry["session"], "--wait-seconds", str(a.wait_seconds), "--doc", doc]
    for flag, val in (("--ecosystem", a.ecosystem), ("--model", a.model), ("--effort", a.effort),
                      ("--note", getattr(a, "note", ""))):
        if val:
            cmd += [flag, val]
    argv = ["systemd-run", "--user", "--unit", unit, "--collect", "--quiet",
            "-p", f"WorkingDirectory={entry.get('cwd') or os.getcwd()}",
            "-p", f"StandardOutput=append:{log}", "-p", f"StandardError=append:{log}", "-E", "SHOAL_HANDOFF_DETACHED=1"]
    argv += [f"-E{k}={os.environ[k]}" for k in FINISHER_ENV if os.environ.get(k)]
    # host settings, never credentials: a unit's environment is on its command line and in the journal
    argv += [f"-E{k}={v}" for k, v in sorted(os.environ.items()) if k.startswith("SHOAL_") and k != "SHOAL_SELF_SESSION"
             and not any(w in k for w in ("TOKEN", "SECRET", "KEY", "PASS"))]
    if a.dry_run:
        note(f"{label} is this session: would detach {unit} (log {log}) to hand off once it is idle: "
             + " ".join(shlex.quote(x) for x in argv + cmd))
        return 0
    r = subprocess.run(argv + cmd, capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise HandoffError(f"could not start the detached finisher: {(r.stdout + r.stderr).strip()[-300:]}")
    note(f"{label} is this session: handoff detached as {unit}, log {log}. End this turn now; the finisher hands "
         f"off once the seat is idle on two checks, within {a.wait_seconds}s")
    return 0


def resume_brief(doc: str, note: str = "") -> str:
    """A standalone seat's first prompt: resume from its start doc, then the operator's note for this handoff, if any."""
    sys.path.insert(0, os.path.join(HERE, "ui"))
    import handover
    text = handover.RESUME.format(doc=doc)
    return text + (f"\n\nA note from the operator for this handoff: {note.strip()}" if note.strip() else "")


def preflight(doc: str, ecosystem: str) -> None:
    import backends
    from doctor import ECOSYSTEM_TOOLS
    if ecosystem not in backends.ecosystems():
        raise HandoffError(f"unknown provider {ecosystem}")
    tool = ECOSYSTEM_TOOLS.get(ecosystem)
    if tool and not backends.tool_which(tool):
        raise HandoffError(f"{tool} is not on this host's tool path")
    if not doc or not os.path.isfile(os.path.expanduser(doc)):
        raise HandoffError("a readable start doc is required: save its path on the standalone seat or supply --doc")
    if not os.path.isdir(os.path.dirname(os.path.abspath(os.path.expanduser(doc)))):
        raise HandoffError("the start doc's folder is missing")


def role_ready(spec, slug: str, previous_queue: str = "") -> bool:
    import health
    import park
    from ledger import Ledger, View
    role = spec.role(slug)
    view = View(Ledger(spec.sid, root=spec.ledger_dir or None, sync=False), spec)
    q, st = view.current_queue(slug) or {}, view.role_state(slug) or {}
    posture = st.get("state") or ("idle" if view.is_idle(slug) else "active")
    return bool(q.get("restart_ready") and (not previous_queue or q.get("id") != previous_queue) and posture in ("idle", "waiting") and
                not park.session_held(role, spec.dir) and
                health.session_between_turns(role, health.claude_busy() if role.ecosystem == "claude" else {}, spec.dir))


def wait_for(check, message: str, deadline: float) -> None:
    last = -1
    while not check():
        left = deadline - time.monotonic()
        if left <= 0:
            raise HandoffError("timed out waiting for the next handoff stage; read the progress log before retrying")
        minute = int(left / 60)
        if minute != last:
            note(f"{message} ({int(left)}s remaining)")
            last = minute
        time.sleep(min(POLL_S, left))


def run(a) -> int:
    import park
    import startdoc
    from roles import ShoalSpec, is_local
    session_key = getattr(a, "session", "")
    expected = getattr(a, "expected_session", "")
    prepare = bool(getattr(a, "prepare", False))
    if session_key:
        prepare = not getattr(a, "no_prepare", False)
        if a.force:
            raise HandoffError("--force is for roles; a standalone handoff requires an idle session")
    if getattr(a, "through_question", False) and not session_key:
        raise HandoffError("--through-question is for standalone seats; a role's open question is answered first")
    if a.force and prepare:
        raise HandoffError("checkpoint-first handoff and --force cannot be combined")
    if not 1 <= a.wait_seconds <= 86400:
        raise HandoffError("--wait-seconds must be between 1 and 86400")
    if session_key:
        entry = standalone_entry(session_key)
        old = entry["session"]
        doc = getattr(a, "doc", "") or entry.get("doc", "")
        eco = a.ecosystem or entry["harness"]
        label = entry.get("label") or old[:12]
    else:
        if not a.spec or not a.only:
            raise HandoffError("a role handoff needs --spec and --only")
        spec = ShoalSpec.load(a.spec)
        role = spec.role(a.only)
        if role is None or not role.session or not is_local(role, spec):
            raise HandoffError("pick a live role on this host")
        old, doc, eco, label = role.session, role.doc, a.ecosystem or role.ecosystem, role.slug
    if expected and expected != old:
        raise HandoffError("this seat has changed sessions since the preview: plan the handoff again")
    doc = os.path.abspath(os.path.expanduser(doc)) if doc else ""
    preflight(doc, eco)
    if session_key and is_self(entry):
        # a self-handover has no later turn to take a checkpoint in: it must be written first, then opted into
        if prepare:
            raise HandoffError(f"{label} is this session: refresh your start doc, then pass --no-prepare")
        return detach_self(a, entry, doc)
    await_idle = bool(getattr(a, "await_idle", False))
    if session_key and not prepare:
        if not (await_idle or between(entry)) or not startdoc.check(doc)["ok"] or startdoc.split_job_folders(doc)[0]:
            raise HandoffError("using an existing checkpoint requires a valid start doc, no live job-folder references, and an idle source")
    if session_key and prepare and question_open(entry["harness"], old) and not getattr(a, "through_question", False):
        raise HandoffError(f"{label} is stopped at an open question, so it cannot take a checkpoint request: answer it, "
                           "or pass --through-question to stop that turn and resume it with the request (the question "
                           "is dropped)")
    if a.dry_run:
        step = f"request a restart-ready checkpoint if needed, wait up to {a.wait_seconds}s" if prepare else "validate its existing checkpoint and idle session"
        note(f"{label}: would {step}, "
             f"then hand off to {eco} from {doc}. Closing the dialog does not cancel it")
        return 0
    # One workflow per seat, including callers outside the dialog.
    from config import env
    root = env("RUNTIME_DIR") or os.path.join(os.environ.get("XDG_RUNTIME_DIR") or f"/tmp/shoal-{os.getuid()}", "shoal-handoff")
    os.makedirs(root, mode=0o700, exist_ok=True)
    key = hashlib.sha256(("standalone/" + old if session_key else spec.sid + "/" + label).encode()).hexdigest()
    with open(os.path.join(root, key + ".lock"), "a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise HandoffError("a handoff for this seat is already running; reopen its progress log") from exc
        deadline = time.monotonic() + a.wait_seconds
        if session_key:
            return standalone_handoff(a, entry, doc, eco, deadline, prepare=prepare)
        previous_queue, requested_at = "", None
        def ready():
            fresh = ShoalSpec.load(a.spec)
            if fresh.role(label) is None or fresh.role(label).session != old:
                raise HandoffError("the role changed sessions while waiting; this handoff was stopped")
            return role_ready(fresh, label, previous_queue) and startdoc.check(doc, requested_at)["ok"]
        if not ready():
            import datetime
            from ledger import Ledger, View
            if question_open(role.ecosystem, old):
                raise HandoffError(f"{label} is stopped at an open question, so its checkpoint request would wait to "
                                   "the deadline: answer the question first")
            view = View(Ledger(spec.sid, root=spec.ledger_dir or None, sync=False), spec)
            previous_queue = (view.current_queue(label) or {}).get("id", "")
            requested_at = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)
            note(f"1/3 Asking {label} to refresh its start doc and become restart-ready")
            text = ("Operator-requested handoff: refresh your start doc and companion log so a fresh session can "
                    "continue from them alone. Preserve your work, queue, blockers, grants-needed and monitors. "
                    f"Set `Last updated: <UTC>` using seconds, at or after {requested_at.strftime('%Y-%m-%dT%H:%M:%SZ')}. "
                    "Acknowledge this request with a linked `msg --kind ack --to user --re <request-id>`. "
                    "End at an idle or waiting boundary with `queue --restart-ready`, then stop this turn. "
                    "The operator will perform the handoff after that checkpoint; do not restart yourself.")
            rc = subprocess.call([sys.executable, os.path.join(HERE, "cli.py"), "msg", "--shoal", spec.sid, "--spec", a.spec,
                                  "--to", label, "--by", "user", "--kind", "request", "--ack", "--wake", "--text", text])
            if rc:
                raise HandoffError("the checkpoint request could not be delivered; no handoff performed")
            wait_for(ready, f"Waiting for {label}'s checkpoint and current turn to finish", deadline)
        note(f"2/3 {label} is restart-ready; handing off through the existing lifecycle gates")
        # Re-read before using the ordinary, unforced handoff gate.
        from roles import state_dir
        restart_log = os.path.join(state_dir(spec.sid), f"context-restart-{label}.log")
        offset = os.path.getsize(restart_log) if os.path.exists(restart_log) else 0
        rc = park.handoff(ShoalSpec.load(a.spec), a.spec, label, a.model, a.ecosystem, False, False, a.effort,
                          getattr(a, "note", ""))
        if rc:
            return rc
        note("3/3 Restart launched; following its log until the replacement is recorded")
        def replaced():
            nonlocal offset
            if os.path.exists(restart_log):
                with open(restart_log) as log:
                    log.seek(offset)
                    chunk = log.read()
                    offset = log.tell()
                if chunk:
                    print(chunk, end="", flush=True)
            fresh = ShoalSpec.load(a.spec).role(label)
            return fresh is not None and bool(fresh.session) and fresh.session != old
        wait_for(replaced, f"Waiting for {label}'s replacement; restart log: {restart_log}", time.monotonic() + 600)
        note(f"Handoff complete: {label}'s replacement is recorded in the spec")
        return 0


def standalone_handoff(a, entry: dict, doc: str, eco: str, deadline: float, prepare: bool = True) -> int:
    import backends
    import park
    import startdoc
    sys.path.insert(0, os.path.join(HERE, "ui"))
    import handover
    import ui_herdr
    old, label = entry["session"], entry.get("label") or entry["session"][:12]
    original = dict(entry)
    def unchanged():
        current = standalone_entry(label)
        if (current["session"], current["harness"]) != (old, entry["harness"]):
            raise HandoffError("the standalone seat changed sessions while waiting; no handoff performed")
        return current
    unchanged()
    through = bool(getattr(a, "through_question", False)) and question_open(entry["harness"], old)
    if prepare:
        stamp = handover.now_iso()
        before = fingerprint(doc)
        if through:
            note(f"1/3 {label} is stopped at an open question: stopping that turn to deliver the checkpoint request")
        else:
            note(f"1/3 Waiting for {label}'s current turn to finish before requesting its checkpoint")
            wait_for(lambda: bool(unchanged()) and between(entry), "Waiting for the session or attached client", deadline)
        text = (f"Operator-requested handoff: refresh your start doc {doc} and its companion log so a new session can "
                "continue from them alone. Include current work, decisions, grants-needed and monitors. Update its "
                "job-folder references: move required artifacts to durable paths and remove obsolete references. Set its "
                f"`Last updated: <UTC>` stamp using seconds, at or after {stamp}, record that you are restart-ready, and finish this turn with nothing "
                "mid-flight. The operator will perform the handoff; do not restart yourself.")
        backend = backends.get(entry["harness"])
        note(f"1/3 Asking {label} to write a fresh restart-ready checkpoint")
        if entry["harness"] == "claude":
            spec, role = handover._pseudo(entry)
            channel = backend.deliver(spec, role, old, text, through_question=through)
            if channel.startswith(("deferred", "FAILED", "no-adapter", "unsupported")):
                raise HandoffError(f"checkpoint request was not delivered: {channel}")
        else:
            backend.send(old, entry.get("cwd") or os.getcwd(), text, model=entry.get("model", ""))
        def ready():
            unchanged()
            return startdoc.check(doc, startdoc.parse_ts(stamp))["ok"] and fingerprint(doc) != before and between(entry)
        wait_for(ready, f"Waiting for {label}'s refreshed doc and completed checkpoint turn", deadline)
    else:
        if getattr(a, "await_idle", False):
            note(f"1/3 Waiting for {label} to finish the turn that started this handoff")
            wait_for(lambda: bool(unchanged()) and settled(entry), "Waiting for the seat to be idle on two checks", deadline)
        if not between(entry) or not startdoc.check(doc)["ok"]:
            raise HandoffError("using an existing checkpoint requires a valid start doc and an already idle session")
        note(f"1/3 Using {label}'s existing validated checkpoint; the source session is idle")
    if startdoc.split_job_folders(doc)[0]:
        raise HandoffError("the checkpoint still depends on a live job folder; move those artifacts and retry")
    # Re-read just before retirement; retain the old registry entry on any launch failure.
    unchanged()
    if not between(entry):
        raise HandoffError("the session started another turn after its checkpoint; no replacement started")
    seat = types.SimpleNamespace(ecosystem=entry["harness"], session=old)
    note(f"2/3 Checkpoint verified; retiring the old {entry['harness']} session")
    stopped = park.stop_session(seat)
    note(stopped)
    if "STILL LIVE" in stopped:
        raise HandoffError("the old session did not stop; no replacement started")
    model = a.model or (entry.get("model", "") if eco == entry["harness"] else "")
    effort = a.effort or (entry.get("effort", "") if eco == entry["harness"] else "")
    note(f"3/3 Starting the new {eco} session from {doc}")
    new = backends.get(eco).create(label, entry.get("cwd") or os.getcwd(),
                                   brief=resume_brief(doc, getattr(a, "note", "")), name=label, model=model, effort=effort)
    at = handover.now_iso()
    # the sessions this seat has had, newest last: which one a stale card, job or name-addressed message belongs to
    lineage = (list(original.get("lineage") or []) + [{"harness": original["harness"], "session": old,
               "job": original.get("job", ""), "until": at}])[-LINEAGE_MAX:]
    updates = {"harness": eco, "session": new, "job": new[:8] if eco == "claude" else "", "doc": doc,
               "model": model, "effort": effort, "handover_state": {"step": "reset", "at": at, "from": old},
               "lineage": lineage}
    if not ui_herdr.update_standalone(original, updates):
        raise HandoffError(f"replacement {new} started, but the seat registry changed: reconcile it before retrying")
    note(f"Handoff complete: {label} now follows {new}; its space and saved settings are retained")
    return 0
