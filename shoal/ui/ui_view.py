#!/usr/bin/env python3
"""`shoal view`: open the harness's own client on an existing session, for an operator.

  shoal view <shoal> <role> [--stub] [--dry-run]       a shoal role's live session
  shoal view --harness claude|opencode --session ID     any standalone session (not in a shoal)
             [--label NAME] [--stub] [--dry-run]

The viewer never creates, resumes, stops or messages a session (`Backend.view_argv`), so viewing is
never a wake and costs no tokens; detaching or closing the terminal leaves the session running. The
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
DETACH_HINT = "detach once attached: ctrl+z (claude) or ctrl+c (opencode); the session keeps running"


class ViewError(RuntimeError):
    pass


def clean_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith(STRIP_PREFIXES)}
    env.update(extra or {})
    return env


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


def remote_attach(server: str, sid: str, slug: str) -> int:
    """Attach a role on another host through its route, from a card. A Claude attach is exclusive, so a session
    held by another viewer cannot open here: an attach that fails within FAST_EXIT_S says why (host unreachable,
    or attached elsewhere) and retries every ATTACH_RETRY_S rather than taking the session over; `q` gives up,
    `r` retries now (maintainer ruling 2026-10-20)."""
    import routes
    while True:
        argv = routes.view_argv(server, sid, slug)
        if argv is None:
            print(f"no route to {server}")
            return 2
        t0 = time.time()
        rc = subprocess.run(argv).returncode
        if rc == 0 or time.time() - t0 >= FAST_EXIT_S:
            return rc
        why = ("host unreachable" if routes.reachable(server, force=True) is False
               else "attached elsewhere, or not attachable right now")
        sys.stdout.write(f"\n\x1b[33m{slug} on {server}: {why} (exit {rc}); retrying in {ATTACH_RETRY_S}s\x1b[0m"
                         f"   [r] retry now   [q] back to the card\n")
        sys.stdout.flush()
        k = _getkey(ATTACH_RETRY_S)
        if k in ("q", "Q"):
            return rc
        flush_input()


def verify_listed(harness: str, session: str) -> str:
    """'' when the harness lists the session with a live worker, else why not. Never guesses."""
    if harness == "claude":
        import ui_status
        e = ui_status.claude_session(ui_status.claude_rows(), session)
        if e is None:
            return f"session {session} is not listed by `claude agents` (relaunched? re-resolve, never guess)"
        if not e.get("pid"):
            return f"session {session} has no worker (state {e.get('state')}): it is dead, not viewable"
        return ""
    return ""  # opencode: the server rejects an unknown session id itself


def viewer(harness: str, session: str) -> tuple[list[str], dict[str, str]]:
    from backends import get as get_backend
    argv, extra = get_backend(harness).view_argv(session)
    return argv, clean_env(extra)


def run_viewer(harness: str, session: str, label: str) -> int:
    why = verify_listed(harness, session)
    if why:
        print(f"shoal view: {label}: {why}")
        return 1
    argv, env = viewer(harness, session)
    print(f"shoal view: {label} -> {harness} {session} (detach to come back; the session keeps running)")
    r = subprocess.run(argv, env=env)
    if r.returncode != 0 and harness == "claude":
        print(f"shoal view: attach exited {r.returncode}: is the session attached in another terminal? "
              "Detach it there first (attach is exclusive). Nothing was resumed or started.")
    return r.returncode


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
        tty.setcbreak(fd)
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
    remote = _remote_host(sid, slug, spec_path)
    import routes
    while True:
        watch.check()
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
        sys.stdout.write("\x1b[2J\x1b[H" + body + "\n\n\x1b[2m" + keys
                         + f"   (refresh every {CARD_REFRESH_S}s; viewing never wakes the role)\n{DETACH_HINT}\x1b[0m\n")
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


def standalone_stub(harness: str, session: str, label: str) -> int:
    import ui_term
    watch = ui_term.CodeWatch()
    while True:
        watch.check()
        hint = (DETACH_HINT if harness not in ("muse", "codex") else
                f"this card hosts the {harness.capitalize()} client itself: quit {harness} to come back here; the "
                "session is saved on disk and [enter] resumes it (a name that does not exist yet starts a new session)")
        sys.stdout.write(f"\x1b[2J\x1b[H\x1b[1m{label}\x1b[0m  (standalone {harness} session {session})\n\n"
                         f"\x1b[2m[enter] attach   [q] shell\n{hint}\x1b[0m\n")
        sys.stdout.flush()
        k = _getkey(300)
        if k in ("q", "Q"):
            return 0
        if k in ("\r", "\n"):
            flush_input()
            subprocess.run(_view_cmd("", "", "", harness=harness, session=session, label=label))
            time.sleep(2)


def cmd_view(a) -> int:
    try:
        if a.session:
            if a.harness not in ("claude", "opencode", "muse", "codex"):
                print("shoal view: --session needs --harness claude|opencode|muse|codex")
                return 2
            label = a.label or f"{a.harness}:{a.session[:12]}"
            if a.dry_run:
                argv, env = viewer(a.harness, a.session)
                print(json.dumps({"argv": argv, "env_extra": sorted(k for k in env if k not in os.environ)}))
                return 0
            return standalone_stub(a.harness, a.session, label) if a.stub else run_viewer(a.harness, a.session, label)
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
            argv, env = viewer(role.ecosystem, role.session)
            print(json.dumps({"argv": argv, "env_extra": sorted(k for k in env if k not in os.environ)}))
            return 0
        return run_viewer(role.ecosystem, role.session, f"{spec.sid}/{role.slug}")
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
    p.add_argument("--dry-run", action="store_true", help="print the client argv (and env NAMES) only")
    p.set_defaults(func=cmd_view)
