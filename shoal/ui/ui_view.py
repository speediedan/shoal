#!/usr/bin/env python3
"""`shoal view`: open the harness's own client on an existing session, for an operator.

  shoal view <shoal> <role> [--stub] [--dry-run]       a shoal role's live session
  shoal view --harness claude|opencode --session ID     any standalone session (not in a shoal)
             [--label NAME] [--stub] [--dry-run]

Viewing never creates, stops or messages a session, so it is never a wake and costs no tokens.
Muse roles open a lock-free observer; native interaction is a separate, explicit idle-only action.
Detaching or closing the observer leaves the session running. The
session id is resolved from the live spec on every call (ids change on restart) and checked against
the harness's own listing before anything runs.

Environment hygiene: terminal multiplexers inject their own variables (`HERDR_*`, `ZELLIJ*`). A Claude
background daemon first spawned from such a pane would carry them into every role's hooks, and a
harness integration could bind a lane to a pane. They are stripped from the client's environment.

`--stub` (what UI layouts use): a small role card that refreshes itself and attaches only on Enter,
then comes back to the card on detach. One live client per role all the time would hold every
role's exclusive Claude attach and cost a client process each; the card costs neither.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.dirname(HERE)
for _p in (PKG, HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

STRIP_PREFIXES = ("HERDR_", "ZELLIJ")
CARD_REFRESH_S = 15
# measured 2026-10-12 under a pty: Ctrl+Z ends `claude attach` (exit 0) and Ctrl+C ends the opencode
# client; the session keeps running either way, and the card comes back
DETACH_HINT = "back: ctrl+z (claude), ctrl+c (opencode), q/esc (Muse live view); native Muse: /exit"


class ViewError(RuntimeError):
    pass


def clean_env(extra: dict[str, str] | None = None, *, base: dict | None = None) -> dict[str, str]:
    env = dict(os.environ if base is None else base)
    env.update(extra or {})
    return {k: v for k, v in env.items() if not k.startswith(STRIP_PREFIXES)}


def resolve_role(sid: str, slug: str, spec_path: str = ""):
    from roles import ShoalSpec, find_spec
    spec = ShoalSpec.load(find_spec(sid, spec_path))
    role = spec.role(slug)
    if role is None:
        raise ViewError(f"{slug} is not a role of {sid} (roles: {', '.join(r.slug for r in spec.roles)})")
    if not role.session:
        raise ViewError(f"{slug} has no live session (parked or retired)")
    from roles import host_of, is_local
    if not is_local(role, spec):
        # sessions resolve through THIS host's harness (its `claude agents`, its OpenChamber server), so a
        # session on another host is attached by running its own host's `shoal view` over a route
        import routes
        if routes.route(host_of(role, spec)) is None:
            raise ViewError(f"{slug} runs on {host_of(role, spec)}: attach from that host's viewer, or add a route "
                            f"(`[hosts.{host_of(role, spec)}] ssh = \"<user>@<name>\"` in this host's config)")
    return spec, role


ATTACH_RETRY_S = 30
FAST_EXIT_S = 15


def remote_attach(server: str, sid: str, slug: str, standalone: tuple[str, str, str] | None = None) -> int:
    """Attach a role on another host through its route, from a card. A Claude attach is exclusive, so a session
    held by another viewer cannot open here: an attach that fails within FAST_EXIT_S says why (host unreachable,
    or attached elsewhere) and retries every ATTACH_RETRY_S rather than taking the session over; `q` gives up,
    `r` retries now (maintainer ruling 2026-10-20)."""
    import routes
    while True:
        if standalone:
            r = routes.route(server)
            h_, sess, lab, *rest = standalone
            job = rest[0] if rest else ""
            argv = (routes.ssh_argv(r, tty=True) + [routes.remote_cmd(r, "view", "--harness", h_, "--session", sess,
                                                                      "--label", lab, *(["--job", job] if job else []))]) if r else None
        else:
            argv = routes.view_argv(server, sid, slug)
        if argv is None:
            print(f"no route to {server}")
            return 2
        t0 = time.time()
        rc = subprocess.run(argv).returncode
        if rc == 0 or time.time() - t0 >= FAST_EXIT_S:
            return rc
        # ssh itself exits 255 for its own failures: an unreachable host, or a connection that ended under the
        # attach (measured 2026-10-21: a dropped link read as "attached elsewhere"); any other code is the remote side
        why = ("host unreachable" if routes.reachable(server, force=True) is False
               else "connection ended" if rc == 255
               else "attached elsewhere, or not attachable right now")
        sys.stdout.write(f"\n\x1b[33m{slug} on {server}: {why} (exit {rc}); retrying in {ATTACH_RETRY_S}s\x1b[0m"
                         f"   [r] retry now   [q] back to the card\n")
        sys.stdout.flush()
        k = _getkey(ATTACH_RETRY_S)
        if k in ("q", "Q"):
            return rc
        flush_input()


def verify_listed(harness: str, session: str, job: str = "") -> str:
    """'' when the harness lists the session, else why not. Never guesses. A Claude session is found by its job id
    or any conversation id it has had (backends.claude_find), so a `/clear` does not strand it. One with no worker
    (retired idle by the background service, or stopped) is still viewable: `claude attach` wakes its worker
    without a turn (measured 2026-10-22: "Waking session", pid back, no new assistant message). A session that is
    not listed as a background job is resolved with its registry entry (resolve_standalone), so a `/bg` successor
    is named rather than a resume that would start a copy."""
    if harness == "claude":
        import ui_status
        rows = ui_status.claude_rows()
        entry, entries = registry_entry(harness, session)
        d = resolve_standalone(rows, entry or {"harness": harness, "session": session, "job": job}, entries,
                               claude_jobs_dir())
        if d["action"] != "attach":
            if d["action"] == "follow":
                return (f"{d['message']}: its card follows it on the next refresh (any key), or attach it with "
                        f"`shoal view --harness claude --session {d['successors'][0]['sessionId']}`")
            return d["message"]
        e = d["row"]
        if not e.get("pid"):
            print(f"shoal view: {e.get('id')} has no worker (state {e.get('state')}, retired idle or stopped): "
                  f"attaching wakes its worker; no turn is taken")
        return ""
    return ""  # opencode: the server rejects an unknown session id itself


def _where(row: dict) -> str:
    pid, tty = row.get("pid"), ""
    try:
        tty = os.readlink(f"/proc/{pid}/fd/0")
    except (OSError, TypeError):
        pass
    return f"pid {pid}" + (f" on {tty}" if tty.startswith("/dev/") else "")


def _bg_resume(session: str, label: str = "") -> str:
    import shlex
    return f"`claude --bg --resume {session}" + (f" --name {shlex.quote(label)}`" if label else "`")


BG_FORKS = ("(`/bg` inside the session forks a new session id instead; a card follows a unique `/bg` successor "
            "on its own)")


def interactive_where(row: dict, label: str = "") -> str:
    """Why an interactive Claude session cannot be attached, and where it is. `claude attach` opens background
    sessions only, and `claude --resume` (or `--bg --resume`) of a session live in another process starts a copy,
    so the id-preserving move is: exit that window first, then resume it in the background."""
    return (f"running interactively in {_where(row)}: `claude attach` opens background sessions only, so use that "
            f"terminal. To view it here, exit that interactive window first, then run "
            f"{_bg_resume(str(row.get('sessionId')), label or str(row.get('name') or ''))} "
            f"(never while it still runs: that starts a second copy). {BG_FORKS}")


# ---- standalone Claude sessions after a `/bg` ------------------------------------------------------------------
# `/bg` in an interactive Claude session does not move it: Claude Code starts a NEW background job with a NEW
# session id (its state.json: `interactiveLineage: true`, the session's user-set name, `template: "bg"`, and no field
# naming the session it came from). A standalone card keyed to the original id would refuse, and its old advice
# (`--bg --resume <orig>`) started yet another copy while the original still ran. resolve_standalone matches the
# successor by evidence (name, cwd, lineage, start time, not already owned) and decides what the card does.

SEEN_REFRESH_S = 600


def claude_jobs_dir() -> str:
    from backends import claude_base
    return os.path.join(claude_base(), "jobs")


def _iso(t: float) -> str:
    import datetime as _dt
    return _dt.datetime.fromtimestamp(t, _dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _epoch(v) -> float | None:
    """Epoch seconds from a ms epoch number or an ISO UTC string, None when unreadable."""
    import datetime as _dt
    if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0:
        return v / 1000.0 if v > 1e11 else float(v)
    if isinstance(v, str) and v:
        try:
            return _dt.datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


def _first_transcript_ts(session: str) -> float | None:
    """The first timestamp in a Claude session's own transcript (projects/*/<session>.jsonl), None when absent."""
    import glob
    from backends import claude_base
    if not session or not re.fullmatch(r"[0-9a-fA-F-]{8,64}", session):
        return None
    for p in glob.glob(os.path.join(claude_base(), "projects", "*", f"{session}.jsonl")):
        try:
            with open(p) as fh:
                for _, ln in zip(range(200), fh):
                    try:
                        t = _epoch((json.loads(ln) or {}).get("timestamp"))
                    except (ValueError, AttributeError):
                        continue
                    if t:
                        return t
        except OSError:
            continue
    return None


def bg_since(entry: dict) -> tuple[float | None, str]:
    """(epoch seconds, source): a `/bg` successor must have started after this. The entry's `seen_utc` (refreshed by
    its card whenever the recorded session is listed); for an entry without one, its `saved_utc`, else the first
    timestamp of the original session's transcript; (None, "unknown") when none of these is available."""
    for k in ("seen_utc", "saved_utc"):
        t = _epoch(entry.get(k))
        if t:
            return t, k
    t = _first_transcript_ts(str(entry.get("session") or ""))
    return (t, "transcript") if t else (None, "unknown")


def _job_state(jobs_dir: str, job: str) -> dict | None:
    """A job's state.json under jobs_dir, None when unreadable (read-only)."""
    if not job or not re.fullmatch(r"[0-9a-f]{6,64}", job):
        return None
    try:
        with open(os.path.join(jobs_dir, job, "state.json")) as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else None
    except (OSError, ValueError):
        return None


def _bg_name_matches(name: str, label: str) -> bool:
    if not (name and label):
        return False
    m = re.fullmatch(r"(.*) \((\d+)\)", name)
    bases = {name} | ({m.group(1)} if m else set())
    # the picker keeps 40 characters of a name as the label
    return any(b == label or (len(label) == 40 and b[:40] == label) for b in bases)


# finished background jobs (`claude agents --json --all` keeps them: measured 2026-11-02, `stopped` after `claude stop`
# and `done`): an operator ended them, so they are never followed, only named
FINISHED_STATES = frozenset({"stopped", "done", "failed", "error", "killed", "cancelled", "exited"})


def bg_successors(rows: list[dict], entry: dict, entries: list[dict], jobs_dir: str,
                  since: tuple[float | None, str] | None = None) -> list[dict]:
    """The `claude agents` rows that are a `/bg` successor of this registry entry's session (bg_candidates)."""
    return bg_candidates(rows, entry, entries, jobs_dir, since)[0]


def bg_candidates(rows: list[dict], entry: dict, entries: list[dict], jobs_dir: str,
                  since: tuple[float | None, str] | None = None) -> tuple[list[dict], list[dict]]:
    """(successors, stopped copies) of this registry entry's session after a `/bg`. A successor is a background job
    named as the entry's label (or the label followed by " (N)": a second `/bg`), in the entry's cwd (when it has
    one), whose state.json says `interactiveLineage` (an unreadable state.json is accepted and noted), started after
    `since` (bg_since), not referenced by any other registry entry, and not finished (FINISHED_STATES). A job that
    matches all of that but is finished is a stopped copy: named, never followed. Read-only; never a guess."""
    from backends import _same
    label = str(entry.get("label") or "")
    if not label:
        return [], []
    t_since, src = since if since is not None else (None, "unknown")
    others = [x for x in entries if x is not entry and not (x.get("harness") == entry.get("harness")
                                                             and x.get("session") == entry.get("session")
                                                             and (x.get("label") or "") == label)]
    owned = [str(x.get(k) or "") for x in others for k in ("session", "job")]
    out, stopped = [], []
    for r in rows:
        sid, jid = str(r.get("sessionId") or ""), str(r.get("id") or "")
        if r.get("kind") != "background" or not sid or _same(sid, str(entry.get("session") or "")):
            continue
        if not _bg_name_matches(str(r.get("name") or ""), label):
            continue
        if entry.get("cwd") and os.path.normpath(str(r.get("cwd") or "")) != os.path.normpath(entry["cwd"]):
            continue
        if any(o and (_same(o, sid) or o == jid) for o in owned):
            continue
        notes = []
        st = _job_state(jobs_dir, jid)
        if st is None:
            notes.append("state.json unreadable: lineage not checked")
        elif st.get("interactiveLineage") is not True:
            continue
        started = _epoch(r.get("startedAt")) or _epoch((st or {}).get("createdAt"))
        if t_since is not None:
            if started is None or started <= t_since:
                continue
            notes.append(f"started after {src}")
        else:
            notes.append("no seen time recorded: start time not checked")
        c = {"id": jid, "sessionId": sid, "name": r.get("name"), "startedAt": started,
             "state": r.get("state") or r.get("status") or "?", "note": "; ".join(notes)}
        (stopped if str(r.get("state") or "") in FINISHED_STATES else out).append(c)
    return (sorted(out, key=lambda c: c["startedAt"] or 0), sorted(stopped, key=lambda c: c["startedAt"] or 0))


def _succ_line(i: int, c: dict) -> str:
    when = _iso(c["startedAt"]) if c.get("startedAt") else "?"
    return f"  [{i}] {c['id']}  started {when}  state {c['state']}" + (f"  ({c['note']})" if c.get("note") else "")


def resolve_standalone(rows: list[dict], entry: dict, entries: list[dict], jobs_dir: str,
                       since: tuple[float | None, str] | None = None) -> dict:
    """What a standalone Claude card does now, as a decision (pure: reads only `jobs_dir`, writes nothing):
      attach    the recorded session is listed as a background job (`row`)
      refuse    not listed as background and no successor (`message` says why and what to do)
      follow    exactly one `/bg` successor and the original is not live interactively: rewrite the entry to it
      two-live  one or more successors AND the original still runs interactively: warn, do nothing else
      pick      several successors, the original not live: list them; the operator picks one, never a guess
    `row` is the recorded session's own row (None when unlisted); `successors` from bg_successors. `since` defaults to
    bg_since(entry), read only when the recorded session is not listed as a background job."""
    import ui_status
    session, job = str(entry.get("session") or ""), str(entry.get("job") or "")
    label = str(entry.get("label") or "")
    row = ui_status.claude_session(rows, session, job)
    d = {"action": "attach", "row": row, "successors": [], "stopped": [], "message": ""}
    if row is not None and row.get("kind") != "interactive":
        return d
    live = row is not None
    succ, stopped = (bg_candidates(rows, entry, entries, jobs_dir, since if since is not None else bg_since(entry))
                     if label else ([], []))
    d["successors"], d["stopped"] = succ, stopped
    gone = (f" Stopped copies: {', '.join(c['id'] for c in stopped)} (`claude attach <id>` resumes one)."
            if stopped else "")
    if live and succ:
        d["action"] = "two-live"
        d["message"] = (f"two live copies of {label or session[:8]}: the original {session[:8]} still runs "
                        f"interactively in {_where(row)}, and `/bg` started a new background session:\n"
                        + "\n".join(_succ_line(i, c) for i, c in enumerate(succ, 1))
                        + "\nnothing is followed while both run: end one of them yourself (shoal never stops, "
                          "kills or resumes a session)")
    elif len(succ) == 1:
        d["action"] = "follow"
        d["message"] = f"followed /bg successor {succ[0]['id']} (was {session[:8]})"
    elif succ:
        d["action"] = "pick"
        d["message"] = (f"session {session[:8]} is not listed, and {len(succ)} `/bg` sessions could have "
                        f"followed it:\n" + "\n".join(_succ_line(i, c) for i, c in enumerate(succ, 1))
                        + "\npress the number of the one this card should follow (nothing is guessed)")
    else:
        d["action"] = "refuse"
        if live:
            d["message"] = interactive_where(row, label)
        elif not job:  # no job ever recorded: picked as an interactive session, which is listed only while it runs
            d["message"] = (f"session {session} is not listed by `claude agents`: not running (an interactive "
                            f"session that ended, or removed). To continue it in the background, exit any terminal "
                            f"still running it first, then run {_bg_resume(session, label)}, then [enter] attaches "
                            f"(never while it runs elsewhere: that starts a second copy). {BG_FORKS}")
        else:
            d["message"] = (f"session {session} is not listed by `claude agents` and no `/bg` successor matches "
                            "(removed? re-resolve, never guess)")
        d["message"] += gone
    return d


def registry_entry(harness: str, session: str, label: str = "") -> tuple[dict | None, list[dict]]:
    """(this seat's saved registry entry or None, every saved entry): matched by session, else by label."""
    try:
        import ui_herdr
        entries = ui_herdr.load_standalone(raw=True)
    except Exception:  # noqa: BLE001 - the registry is a convenience
        return None, []
    mine = [e for e in entries if not e.get("host") and e.get("harness") == harness]
    e = (next((e for e in mine if e.get("session") == session), None)
         or next((e for e in mine if label and (e.get("label") or e["session"][:12]) == label), None))
    return e, entries


def viewer(harness: str, session: str, label: str = "", model: str = "", effort: str = "",
           observe: bool = False) -> tuple[list[str], dict[str, str]]:
    from backends import get as get_backend
    if label and not (model and effort):
        saved_model, saved_effort = registry_view_args(harness, session, label)
        model, effort = model or saved_model, effort or saved_effort
    if harness == "muse" and observe:
        import muse
        return muse.observer_argv(session, model, effort), clean_env(base=muse.turn_env())
    argv, extra = get_backend(harness).view_argv(session, model, effort)
    if harness == "muse":
        import muse
        return argv, clean_env(base=muse.turn_env())
    return argv, clean_env(extra)


def run_viewer(harness: str, session: str, label: str, job: str = "", model: str = "", effort: str = "",
               observe: bool = False) -> int:
    why = verify_listed(harness, session, job)
    if why:
        print(f"shoal view: {label}: {why}")
        return 1
    argv, env = viewer(harness, session if not job else job, label, model, effort,
                       **({"observe": True} if observe else {}))
    print(f"shoal view: {label} -> {harness} {session} (detach to come back; the session keeps running)")
    binary = os.path.realpath(argv[0]) if harness == "claude" else ""
    started = time.time()
    r = subprocess.run(argv, env=env)
    for _ in range(REATTACH_MAX):
        # the Claude background service restarted under the client (an update reinstalled the binary: measured
        # 2026-10-22, 124 restarts in a day), which drops every attached client back to its card. Reattach the same
        # session; attaching never takes a turn.
        if harness != "claude" or not service_restarted_since(started):
            break
        print("shoal view: the Claude background service restarted (an update); reattaching")
        time.sleep(2)
        argv, env = viewer(harness, session if not job else job, label, model, effort)
        started = time.time()
        r = subprocess.run(argv, env=env)
    if r.returncode != 0 and binary and replaced(binary):
        # Claude Code updated itself while attached: the running client's own binary is gone, so its reconnect
        # (which respawns the background service from that path) fails (measured 2026-10-22: "posix_spawn
        # '...claude.exe (deleted)'"). A fresh client on the new binary reattaches the same session; no turn is taken.
        print("shoal view: Claude Code was updated while attached (the old binary is gone); reattaching on the new "
              "version")
        argv, env = viewer(harness, session if not job else job)
        r = subprocess.run(argv, env=env)
    if r.returncode != 0 and harness == "claude":
        print(f"shoal view: attach exited {r.returncode}: attached in another terminal (attach is exclusive), or the "
              f"background service is down (`claude daemon status`). Nothing was resumed or started.")
    return r.returncode


REATTACH_MAX = 3
SERVICE_START = re.compile(r"^\[(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?Z)\] \[supervisor\] .*daemon start")


def service_restarted_since(t: float) -> bool:
    """True when the Claude background service logged a start after `t` (epoch seconds): its log names each start
    (`[supervisor] ─── daemon start ───`)."""
    import datetime as _dt
    p = os.path.join(os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude"), "daemon.log")
    try:
        with open(p, "rb") as fh:
            fh.seek(max(os.path.getsize(p) - 65536, 0))
            lines = fh.read().decode(errors="replace").splitlines()
    except OSError:
        return False
    for ln in reversed(lines):
        m = SERVICE_START.match(ln)
        if m:
            ts = _dt.datetime.strptime(m.group(1)[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=_dt.timezone.utc).timestamp()
            return ts >= t - 1
    return False


def replaced(binary: str) -> bool:
    """True when the binary a client was started from no longer exists, or the name now resolves elsewhere (an update
    replaced the package while the client ran)."""
    if not os.path.exists(binary):
        return True
    try:
        from backends import tool_which
        now = tool_which("claude")
        return bool(now) and os.path.realpath(now) != binary
    except Exception:  # noqa: BLE001
        return False


def flush_input() -> None:
    """Discard pending terminal input, so nothing typed at a card reaches the harness client."""
    if sys.stdin.isatty():
        import termios
        try:
            termios.tcflush(sys.stdin.fileno(), termios.TCIFLUSH)
        except termios.error:
            pass


def _getkey(timeout: float) -> str:
    """One keypress within `timeout` seconds, '' on timeout. Raw mode only when stdin is a tty."""
    import select
    if not sys.stdin.isatty():
        time.sleep(timeout)
        return ""
    import termios
    import tty
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        # TCSAFLUSH (setcbreak's default) drops a key typed just before a refresh/prompt.
        # Cards and observers must preserve that pending Enter or detach key.
        tty.setcbreak(fd, termios.TCSANOW)
        r, _, _ = select.select([sys.stdin], [], [], timeout)
        return sys.stdin.read(1) if r else ""
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def card(sid: str, slug: str, force: bool = False) -> str:
    import ui_status
    doc = ui_status.cached_status([sid], force=force)
    s = doc["shoals"][0] if doc["shoals"] else {"error": "no such shoal"}
    if s.get("error"):
        return f"{sid}: {s['error']}"
    r = next((x for x in s["roles"] if x["slug"] == slug), None)
    if r is None:
        return f"{slug}: not a role of {sid}"
    import re as _re
    tag = _re.sub(r"-[0-9]{8}-[0-9]{6}$", "", sid)
    # Herdr draws the pane border title as plain text, so the card's own first line carries the colors:
    # the shoal tag in blue, the role bold in green (maintainer, 2026-10-12)
    head = [f"\x1b[38;2;137;180;250m{tag}\x1b[0m / \x1b[1;38;2;166;227;161m{slug}\x1b[0m"
            f"  \x1b[2m({r['tag']}, {r['harness']} {r['model'] or ''}, fill={r['fill']})\x1b[0m",
            f"shoal {sid} {s['gen']}" + ("  OFFLINE" if s["mode"]["offline"] else "")
            + ("  FREEZE" if s["mode"]["freeze"] else "")]
    rows = [["posture", f"{ui_status.posture_label(r)}  ({ui_status._age(r['posture_age_s'])})"],
            ["session", r["session"]], ["watcher", r["watcher"]],
            *([["host", f"{r['host']} via ssh, state {ui_status._age(r['remote_status'].get('age_s'))} old"
                        + ("  (STALE: host not answering)" if r["remote_status"].get("stale") else "")]]
              if r.get("remote_status") else []),
            ["last write", f"{ui_status._age(r['last_write_age_s'])} ago"], ["active", r["active"] or "-"]]
    for n in r["next"]:
        rows.append(["next", n["item"] + (f"  blocked: {n['blocker']}" if n["blocker"] else "")
                     + ("  (STALE)" if n["stale"] else "")])
    if r["wait"]:
        w = r["wait"]
        rows.append(["wait", f"{w['sub']} on {w['target'] or '-'}: {w['holder'] or ''}"])
        if str(w.get("target") or "").startswith("job:"):
            try:
                import jobs as _jobs
                m = _jobs.job_meta(sid, w["target"][4:], slug)
                st = _jobs.unit_state(m["unit"]).get("ActiveState", "?") if m else "unknown"
                last = _jobs.last_line(m.get("log", "")) if m and st == "active" else ""
                rows.append(["job", st + (f": {last}" if last else "")])
            except Exception:  # noqa: BLE001 - a card never fails on a job probe
                pass
    for i, a in enumerate(r["asks"], 1):
        rows.append([f"ask {i}", " ".join(str(a).split())])
    if r["offers_open"]:
        rows.append(["offers", f"{r['offers_open']} open"])
    reasons = [ui_status.ATTENTION.get(x, x) for x in r["can_progress"]["reasons"] if x != "parked"]
    rows.append(["attention", ", ".join(reasons) or "-"])
    paint = ({1: lambda v, row: ui_status.state_color(v) if row[0] in ("posture", "session") else ""}
             if ui_status.use_color() else None)
    table = ui_status.ascii_table(["field", "value"], rows, ui_status.term_width(), flex=(1,), paint=paint)
    return "\n".join(head + [table])


def stub(sid: str, slug: str, spec_path: str = "") -> int:
    """The role card loop: Enter attaches, `r` refreshes, `q` leaves to a shell. Read-only."""
    force = False
    import ui_term
    watch = ui_term.CodeWatch()
    import routes
    while True:
        watch.check()
        # re-read each tick: a role moved to another host (its spec `server`) is attached there at once
        remote = _remote_host(sid, slug, spec_path)
        routed = bool(remote) and routes.route(remote) is not None
        try:
            body = card(sid, slug, force)
        except Exception as exc:  # noqa: BLE001 - a card never crashes the pane
            body = f"{slug}: status unavailable ({exc})"
        force = False
        if routed:
            up = routes.reachable(remote)
            keys = (f"runs on {remote} (ssh{'' if up else ': unreachable now'})   [enter] attach   [r] refresh   [q] shell")
        elif remote:
            keys = f"runs on {remote}: attach from that host's viewer (no route here)   [r] refresh   [q] shell"
        else:
            keys = "[enter] attach   [r] refresh   [q] shell"
        sys.stdout.write(ui_term.fit_frame(body, "\x1b[2m" + keys + f"   (refresh every {CARD_REFRESH_S}s; viewing "
                                           f"never wakes the role)\n{DETACH_HINT}\x1b[0m"))
        sys.stdout.flush()
        k = _getkey(CARD_REFRESH_S)
        if k in ("q", "Q"):
            return 0
        if k in ("r", "R"):
            force = True
        if k in ("\r", "\n") and remote and not routed:
            continue  # nothing to attach here: the session lives on another host with no route from here
        if k in ("\r", "\n") and routed:
            flush_input()
            remote_attach(remote, sid, slug)
            print("\x1b[2m(back to the card in 3s)\x1b[0m")
            time.sleep(3)
            continue
        if k in ("\r", "\n"):  # Enter only: a typed word must never start an attach
            flush_input()
            # a fresh process, so the attach always runs the current code: a card lives for days, and
            # an in-process viewer kept a fixed bug alive in every open card (measured 2026-10-12)
            subprocess.run(_view_cmd(sid, slug, spec_path))
            print("\x1b[2m(back to the card in 3s)\x1b[0m")
            time.sleep(3)


def _remote_host(sid: str, slug: str, spec_path: str = "") -> str:
    """The other host a role runs on, or "" when it runs here (or cannot be resolved)."""
    try:
        from roles import ShoalSpec, find_spec, host_of, is_local
        spec = ShoalSpec.load(find_spec(sid, spec_path))
        role = spec.role(slug)
        return host_of(role, spec) if role is not None and not is_local(role, spec) else ""
    except Exception:  # noqa: BLE001 - a card never fails on this annotation
        return ""


def _view_cmd(sid: str, slug: str, spec_path: str, harness: str = "", session: str = "", label: str = "") -> list[str]:
    """`shoal view` for one attach, run as a child process of the card."""
    argv = [sys.executable, os.path.join(PKG, "cli.py"), "view"]
    if harness:
        return argv + ["--harness", harness, "--session", session, "--label", label]
    return argv + [sid, slug] + (["--spec", spec_path] if spec_path else [])


def registry_follow(harness: str, session: str, label: str, job: str = "") -> tuple[str, str]:
    """(session, job) this card should show now: its saved registry entry's, matched by label. A card's command line
    carries the ids it was opened with, and a handover moves the entry to a new job, so a card that never re-read the
    registry kept showing the retired session (measured 2026-10-24: shoal-architecture-evolution's card on 4decf3de,
    four days after it opened, while the registry held the new job)."""
    try:
        import ui_herdr
        for e in ui_herdr.local_standalone():
            if e.get("harness") == harness and (e.get("label") or e["session"][:12]) == label:
                return e["session"], e.get("job", "") or ""
    except Exception:  # noqa: BLE001 - the registry is a convenience: keep the ids the card was opened with
        pass
    return session, job


def registry_view_args(harness: str, session: str, label: str) -> tuple[str, str]:
    """(model, effort) saved on this standalone seat's registry entry, matched by session or label, so its
    card resumes the client with the model and effort the seat was created with (`muse-new --model/--effort`,
    saved by `ui pick`). ('', '') when the entry carries neither: the harness defaults then apply."""
    try:
        import ui_herdr
        for e in ui_herdr.local_standalone():
            if e.get("harness") == harness and (e["session"] == session
                                                or (e.get("label") or e["session"][:12]) == label):
                return e.get("model", "") or "", e.get("effort", "") or ""
    except Exception:  # noqa: BLE001 - the registry is a convenience: fall back to the harness defaults
        pass
    return "", ""


def follow_successor(entry: dict, succ: dict) -> str:
    """Point a saved standalone entry at its `/bg` successor (session and job), by atomic replace with a one-time
    backup of the registry file. The note the card shows, or '' when the entry moved under us (nothing written)."""
    import ui_herdr
    orig = str(entry.get("session") or "")
    ok = ui_herdr.update_standalone(entry, {"session": succ["sessionId"], "job": succ["id"], "seen_utc": _iso(time.time()),
                                            "bg_followed_from": orig}, backup=True)
    return f"followed /bg successor {succ['id']} (was {orig[:8]})" if ok else ""


def standalone_tick(harness: str, session: str, label: str, job: str = "", pick: int = 0) -> dict:
    """One refresh of a local standalone Claude card: resolve_standalone on its saved entry, then act on it. A listed
    session refreshes the entry's `seen_utc` (at most every SEEN_REFRESH_S); a unique successor is followed; with
    `pick` (a digit the operator pressed) the numbered candidate of a `pick` decision is followed. Never stops,
    kills or resumes a session. {"action": "none"} when the seat has no saved entry (the attach path decides)."""
    entry, entries = registry_entry(harness, session, label)
    if harness != "claude" or entry is None:
        return {"action": "none", "successors": [], "message": ""}
    import ui_herdr
    import ui_status
    t0 = time.time()  # before the listing: a job started after this cannot be missing from it
    d = resolve_standalone(ui_status.claude_rows(), entry, entries, claude_jobs_dir())
    if d["action"] == "attach" or (d["action"] == "refuse" and d["row"] is not None):
        if (_epoch(entry.get("seen_utc")) or 0) < t0 - SEEN_REFRESH_S:
            ui_herdr.update_standalone(entry, {"seen_utc": _iso(t0)})
    chosen = (d["successors"][0] if d["action"] == "follow" else
              d["successors"][pick - 1] if d["action"] == "pick" and 1 <= pick <= len(d["successors"]) else None)
    if chosen is not None:
        note = follow_successor(entry, chosen)
        d.update(action="followed" if note else "refuse", followed=chosen,
                 message=note or "the saved entry changed while following its /bg successor: refresh")
    return d


def standalone_stub(harness: str, session: str, label: str, host: str = "", job: str = "") -> int:
    import ui_term
    watch = ui_term.CodeWatch()
    notice, pick = "", 0
    while True:
        watch.check()
        d = {"action": "none", "message": ""}
        if not host:
            session, job = registry_follow(harness, session, label, job)
        if not host and harness == "claude":
            try:
                d = standalone_tick(harness, session, label, job, pick)
            except Exception as exc:  # noqa: BLE001 - a card never crashes the pane
                d = {"action": "none", "message": f"/bg successor check unavailable ({exc})"}
            if d["action"] == "followed":
                notice = d.pop("message")  # shown on the card from now on (cards keep no log)
                session, job = d["followed"]["sessionId"], d["followed"]["id"]
        pick = 0
        if not host and harness == "claude" and d["action"] in ("two-live", "pick", "refuse"):
            keys = ("[1-%d] follow that one   " % len(d["successors"]) if d["action"] == "pick" else
                    "" if d["action"] == "two-live" else "[enter] attach   ")
            sys.stdout.write(f"\x1b[2J\x1b[H\x1b[1m{label}\x1b[0m  (standalone {harness} session {session})\n\n"
                             + (f"\x1b[32m{notice}\x1b[0m\n" if notice else "")
                             + f"\x1b[33m{d['message']}\x1b[0m\n\n"
                             f"\x1b[2m{keys}[r] refresh   [q] shell\n{DETACH_HINT}\x1b[0m\n")
            sys.stdout.flush()
            k = _getkey(60)
            if k in ("q", "Q"):
                return 0
            if d["action"] == "pick" and k.isdigit():
                pick = int(k)
            elif d["action"] == "refuse" and k in ("\r", "\n"):
                flush_input()
                subprocess.run(_view_cmd("", "", "", harness=harness, session=session, label=label)
                               + (["--job", job] if job else []))
                time.sleep(2)
            continue
        if host:
            import routes
            up = routes.reachable(host)
            sys.stdout.write(f"\x1b[2J\x1b[H\x1b[1m{label}\x1b[0m  (standalone {harness} session {session} on {host})\n\n"
                             f"\x1b[2m[enter] attach over ssh{'' if up else ' (unreachable now)'}   [q] shell\n"
                             f"{DETACH_HINT}\x1b[0m\n")
            sys.stdout.flush()
            k = _getkey(60)
            if k in ("q", "Q"):
                return 0
            if k in ("\r", "\n"):
                flush_input()
                remote_attach(host, "", "", standalone=(harness, session, label.rsplit("@", 1)[0], job))
                time.sleep(2)
            continue
        hint = (DETACH_HINT if harness not in ("muse", "codex") else
                f"this card hosts the {harness.capitalize()} client itself: quit {harness} to come back here; the "
                "session is saved on disk and [enter] resumes it (a name that does not exist yet starts a new session)")
        sys.stdout.write(f"\x1b[2J\x1b[H\x1b[1m{label}\x1b[0m  (standalone {harness} session {session})\n\n"
                         + (f"\x1b[32m{notice}\x1b[0m\n" if notice else "")
                         + (f"\x1b[33m{d['message']}\x1b[0m\n" if d.get("message") else "")
                         + f"\x1b[2m[enter] attach   [q] shell\n{hint}\x1b[0m\n")
        sys.stdout.flush()
        k = _getkey(300)
        if k in ("q", "Q"):
            return 0
        if k in ("\r", "\n"):
            flush_input()
            subprocess.run(_view_cmd("", "", "", harness=harness, session=session, label=label)
                           + (["--job", job] if job else []))
            time.sleep(2)


def cmd_view(a) -> int:
    try:
        if a.session:
            if a.harness not in ("claude", "opencode", "muse", "codex"):
                print("shoal view: --session needs --harness claude|opencode|muse|codex")
                return 2
            label = a.label or f"{a.harness}:{a.session[:12]}"
            if a.dry_run:
                argv, env = viewer(a.harness, a.session, label)
                print(json.dumps({"argv": argv, "env_extra": sorted(k for k in env if k not in os.environ)}))
                return 0
            job = getattr(a, "job", "") or ""
            if getattr(a, "host", ""):
                return (standalone_stub(a.harness, a.session, label, a.host, job) if a.stub else
                        remote_attach(a.host, "", "", standalone=(a.harness, a.session, label.rsplit("@", 1)[0], job)))
            job = getattr(a, "job", "") or ""
            return (standalone_stub(a.harness, a.session, label, job=job) if a.stub
                    else run_viewer(a.harness, a.session, label, job))
        if not (a.target_shoal and a.role):
            print("usage: shoal view <shoal> <role> | shoal view --harness H --session ID")
            return 2
        if a.stub and not a.dry_run:
            return stub(a.target_shoal, a.role, a.spec)
        spec, role = resolve_role(a.target_shoal, a.role, a.spec)
        from roles import host_of, is_local
        if not is_local(role, spec):
            import routes
            if a.dry_run:
                print(json.dumps({"argv": routes.view_argv(host_of(role, spec), spec.sid, role.slug), "env_extra": []}))
                return 0
            return remote_attach(host_of(role, spec), spec.sid, role.slug)
        if a.dry_run:
            argv, env = viewer(role.ecosystem, role.session, model=role.model, effort=role.effort,
                               observe=role.ecosystem == "muse")
            print(json.dumps({"argv": argv, "env_extra": sorted(k for k in env if k not in os.environ)}))
            return 0
        return run_viewer(role.ecosystem, role.session, f"{spec.sid}/{role.slug}",
                          model=role.model, effort=role.effort, observe=role.ecosystem == "muse")
    except Exception as exc:  # noqa: BLE001
        print(f"shoal view: {exc}")
        return 1


def add_parser(sub) -> None:
    p = sub.add_parser("view", help="open the harness client on an existing session (never a wake, never a resume)")
    p.add_argument("target_shoal", nargs="?", default="", metavar="SHOAL")
    p.add_argument("role", nargs="?", default="", metavar="ROLE")
    p.add_argument("--spec", default="")
    p.add_argument("--harness", default="", choices=("", "claude", "opencode", "muse", "codex"))
    p.add_argument("--session", default="", help="a standalone session id (not a shoal role); for muse and codex also a session name")
    p.add_argument("--label", default="")
    p.add_argument("--stub", action="store_true", help="role card that attaches on Enter (UI layouts)")
    p.add_argument("--host", default="", help="with --session: the standalone session lives on this routed host")
    p.add_argument("--job", default="", help="with --harness claude: the background job id (stable across /clear)")
    p.add_argument("--dry-run", action="store_true", help="print the client argv (and env NAMES) only")
    p.set_defaults(func=cmd_view)
