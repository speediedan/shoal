#!/usr/bin/env python3
"""Park a role: no session, so no wakes, conditions or pages, while its queue, offers, claims and blockers are kept.

    shoal park   --spec S --only ROLE[,ROLE] [--note TEXT] [--until <kind>:<ref>] [--force] [--dry-run]
    shoal unpark --spec S --only ROLE[,ROLE] [--dry-run]
    shoal handoff --spec S --only ROLE [--model M] [--effort X] [--ecosystem E] [--force] [--dry-run]

Park, in an order that wakes nothing:
  1. refuse a remote role (park it on its own host) and a role with an active item unless --force (its
     work would stop mid-item);
  2. stop its session through its backend (Claude: every job on its conversation, watched so a respawned worker is
     stopped again);
  3. end every ledger-watch still running for it with SIGKILL, so no exit path can deliver a wake to the stopped
     session, and verify none remains;
  4. clear its session in the spec (the same merge path a restart uses) and record `parked` {at, by, note, until};
  5. append a `role-parked` system event to the ledger, so the digest and the history show who parked it and why.

Unpark is `restart --only ROLE`, which clears `parked`; a full restart keeps parked roles parked. With `--until`, the
health quorum unparks the role itself once that wait target resolves (any provider kind: job:, lease:, gh-run:, gh-pr:,
gh-issue:, az-build:), so a hold that ends on a known event needs nobody to remember it.

Why a verb: measured 2026-10-24, fts-core had to be parked by hand (a spec edit, `claude stop`, then a `kill -9` of a
watcher still bound to the stopped session, which an incoming offer would have used to resume it), and a role left
waiting on a `hold:` instead would have been paged as stale after `hold_ttl_min`.
"""
from __future__ import annotations

import datetime
import os
import shlex
import signal
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config  # noqa: E402

UNPARK_RETRY_MIN = 30  # a quorum run that started an unpark does not start another for this long


def utcnow() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def watchers_of(sid: str, slug: str) -> list[int]:
    """Pids of every `ledger-watch` running for this shoal and role, read from /proc (never from a sidecar)."""
    out = []
    for d in os.listdir("/proc"):
        if not d.isdigit() or int(d) == os.getpid():
            continue
        try:
            with open(f"/proc/{d}/cmdline", "rb") as fh:
                argv = [x.decode(errors="replace") for x in fh.read().split(b"\0") if x]
        except OSError:
            continue
        if "ledger-watch" not in argv or not any(a.endswith(("shoal", "cli.py")) for a in argv[:3]):
            continue
        opt = {argv[i]: argv[i + 1] for i in range(len(argv) - 1) if argv[i].startswith("--")}
        if opt.get("--shoal") == sid and opt.get("--slug") == slug:
            out.append(int(d))
    return out


def end_watchers(sid: str, slug: str, wait_s: float = 5.0) -> tuple[list[int], list[int]]:
    """(killed, still running) after SIGKILL to every watcher of the role."""
    pids = watchers_of(sid, slug)
    for p in pids:
        try:
            os.kill(p, signal.SIGKILL)
        except OSError:
            pass
    deadline = time.time() + wait_s
    left = watchers_of(sid, slug)
    while left and time.time() < deadline:
        time.sleep(0.2)
        left = watchers_of(sid, slug)
    return pids, left


def stop_session(role, dry_run: bool = False) -> str:
    """Stop the role's session through its backend; what was done."""
    from backends import claude_holders, fake_sessions, get as get_backend
    be = get_backend(role.ecosystem)
    if role.ecosystem == "claude":
        rows = be._agents(include_stopped=True)
        jobs = [str(h.get("id")) for h in claude_holders(rows, role.session) if h.get("id")] or [role.session[:8]]
        if dry_run:
            return f"would stop {', '.join(jobs)}"
        if fake_sessions() is not None:
            return f"stopped {', '.join(jobs)} (fake)"
        live = be.retire(jobs)
        return f"stopped {', '.join(jobs)}" + (f"; STILL LIVE: {', '.join(live)}" if live else "")
    if hasattr(be, "stop"):
        if dry_run:
            return f"would stop {role.session}"
        be.stop(role.session)
        return f"stopped {role.session}"
    return f"no {role.ecosystem} stop: the session is idle between wakes, and clearing it ends them"


def park(spec, spec_path: str, slugs: list[str], note: str = "", until: str = "", by: str = "user",
         force: bool = False, dry_run: bool = False) -> int:
    from ledger import SYSTEM_WRITER, Ledger, View
    from roles import is_local
    import providers
    if until:
        until, err = providers.normalize(until)
        if err:
            print(f"park: --until {err}")
            return 1
    led = Ledger(spec.sid, root=spec.ledger_dir)
    led.pull()
    view = View(led, spec)
    roles = []
    for s in slugs:
        r = spec.role(s)
        if r is None:
            print(f"park: unknown role {s!r} (spec has {[x.slug for x in spec.roles]})")
            return 1
        if not is_local(r, spec):
            print(f"park: {s} runs on another host: park it there (shoal park --spec <its clone's spec> --only {s})")
            return 1
        q = view.current_queue(s) or {}
        if q.get("active") and not force:
            print(f"park: REFUSED: {s} holds active item {q['active']}; let it finish (or release it), or pass --force")
            return 1
        roles.append(r)
    for r in roles:
        if not r.session:
            print(f"{r.slug}: already parked (no session); recording the note and condition")
        else:
            print(f"{r.slug}: {stop_session(r, dry_run)}")
        if dry_run:
            print(f"{r.slug}: would end ledger-watch {watchers_of(spec.sid, r.slug) or 'none running'}, clear the "
                  f"session, record parked{' until ' + until if until else ''}")
            continue
        killed, left = end_watchers(spec.sid, r.slug)
        print(f"{r.slug}: ledger-watch ended: {killed or 'none running'}"
              + (f"; STILL RUNNING: {left} (end them by hand: kill -9)" if left else ""))
        old = r.session
        r.session = ""
        r.parked = {"at": utcnow(), "by": by, "note": note, "until": until, "session": old}
    if dry_run:
        return 0
    from cli import persist_spec
    persist_spec(spec, spec_path, note=f"park {','.join(slugs)}")
    for r in roles:
        led.append(SYSTEM_WRITER, "system", event="role-parked", role=r.slug, parked_by=by, note=note, until=until,
                   session=r.parked.get("session", ""))
    print(f"parked {', '.join(r.slug for r in roles)}: `shoal show` reads `parked (no session)`; "
          f"unpark with `shoal unpark --spec {spec_path} --only {','.join(slugs)}`"
          + (f", or the quorum does once {until} resolves" if until else ""))
    return 0


def unpark_argv(spec_path: str, slugs: list[str]) -> list[str]:
    return shlex.split(config.cli()) + ["restart", "--spec", spec_path, "--only", ",".join(slugs)]


def unpark(spec, spec_path: str, slugs: list[str], dry_run: bool = False) -> int:
    not_parked = [s for s in slugs if spec.role(s) is None or spec.role(s).session]
    if not_parked:
        print(f"unpark: {', '.join(not_parked)} not parked (unknown, or it has a session): use restart for those")
        return 1
    argv = unpark_argv(spec_path, slugs) + (["--dry-run"] if dry_run else [])
    print("unpark: " + " ".join(argv[-5 if not dry_run else -6:]))
    return subprocess.run(argv).returncode


def due(spec) -> list:
    """Parked local roles whose --until target has resolved (False from its provider; unknown never unparks)."""
    import providers
    from roles import is_local
    out = []
    for r in spec.roles:
        u = (getattr(r, "parked", None) or {}).get("until", "")
        if r.session or not u or not is_local(r, spec):
            continue
        pending, why = providers.resolve(u, r.slug, spec.sid)
        if pending is False:
            out.append((r, why))
    return out


def auto_unpark(spec, spec_path: str, state: dict, acts: list, dry_run: bool = False) -> None:
    """Health quorum: restart each parked role whose --until target resolved, at most once per UNPARK_RETRY_MIN, never
    during a freeze. The restart is detached from the quorum run (it verifies the new session for minutes)."""
    if not spec_path:
        return
    from health import age_min, iso
    seen = state.setdefault("unparks", {})
    for r, why in due(spec):
        last = seen.get(r.slug)
        if last and (age_min(last) or 0.0) < UNPARK_RETRY_MIN:
            continue
        argv = unpark_argv(spec_path, [r.slug])
        if dry_run:
            acts.append((r.slug, "unpark", f"would restart ({r.parked.get('until')}: {why}) (dry-run)"))
            continue
        from backends import spawn_detached
        from roles import state_dir
        log = os.path.join(state_dir(spec.sid), f"unpark-{r.slug}.log")
        os.makedirs(state_dir(spec.sid), exist_ok=True)
        spawn_detached(argv, stdin=subprocess.DEVNULL, stdout=open(log, "a"), stderr=subprocess.STDOUT,
                       cwd=spec.dir or None)
        seen[r.slug] = iso()
        acts.append((r.slug, "unpark", f"{r.parked.get('until')} {why}: restart started (log {log})"))


def cmd_park(a) -> int:
    from roles import ShoalSpec
    spec = ShoalSpec.load(a.spec)
    slugs = [s.strip() for s in a.only.split(",") if s.strip()]
    if not slugs:
        print("park: --only ROLE[,ROLE] is required")
        return 2
    return park(spec, a.spec, slugs, note=a.note, until=a.until, by=a.by, force=a.force, dry_run=a.dry_run)


def cmd_unpark(a) -> int:
    from roles import ShoalSpec
    spec = ShoalSpec.load(a.spec)
    slugs = [s.strip() for s in a.only.split(",") if s.strip()]
    if not slugs:
        print("unpark: --only ROLE[,ROLE] is required")
        return 2
    return unpark(spec, a.spec, slugs, dry_run=a.dry_run)


def session_held(role, repo_dir: str = "") -> bool:
    """True when a non-Claude role's session is held by a running turn or an attached client. Nothing can stop one from
    here (`context_restart` stops only Claude), so its old turn would keep writing as the role beside a new session."""
    import ledger_watch
    if role.ecosystem in ("muse", "codex"):
        return ledger_watch.muse_session_busy(role.session, role.ecosystem)
    if role.ecosystem == "opencode":
        return ledger_watch.session_busy(role.session, repo_dir)
    return False


def claude_live(role) -> bool:
    """True when a Claude job on the role's conversation has a live worker."""
    from backends import claude_holders, get as get_backend
    return any(h.get("pid") for h in claude_holders(get_backend("claude")._agents(include_stopped=True), role.session))


def handoff(spec, spec_path: str, slug: str, model: str = "", ecosystem: str = "", force: bool = False,
            dry_run: bool = False, effort: str = "") -> int:
    """Hand a role to a fresh session in place, from its start doc rather than a compaction, optionally on another model
    or provider: the quorum's `context_restart` run by hand, under the quorum's gate. Everything that can be proved is
    proved before the old session stops: the role is at a boundary (restart-ready, posture idle or waiting, between
    turns; only that gate yields to `force`, which can cut off a Claude turn), a session nothing here can stop is not
    held (`force` never overrides that), and the new provider's tool and the start doc are in order. A login on the new
    provider is not checked (no harness has a uniform probe), and the restart itself runs detached: its log tells."""
    import health
    import startdoc
    from backends import tool_which
    from doctor import ECOSYSTEM_TOOLS
    from ledger import Ledger, View
    from roles import ECOSYSTEMS, host_of, is_local
    role = spec.role(slug)
    if role is None:
        print(f"handoff: no role {slug}")
        return 2
    if not is_local(role, spec):
        print(f"REFUSED: {slug} runs on {host_of(role, spec)}: hand it off on its own host")
        return 1
    if not role.session:
        print(f"REFUSED: {slug} has no session (`unpark` relaunches a parked role)")
        return 1
    if ecosystem and ecosystem not in ECOSYSTEMS:
        print(f"handoff: unknown provider {ecosystem} ({', '.join(ECOSYSTEMS)})")
        return 2
    new_eco = ecosystem if ecosystem != role.ecosystem else ""
    if session_held(role, spec.dir):
        print(f"REFUSED: {slug}'s {role.ecosystem} session is held by a running turn or an attached client, and nothing "
              f"here can stop it; --force does not change that. Detach it or let the turn end, then try again")
        return 1
    tool = ECOSYSTEM_TOOLS.get(new_eco or role.ecosystem, "")
    if tool and not tool_which(tool):
        print(f"REFUSED: {tool} is not on this host's tool path, so a new {new_eco or role.ecosystem} session cannot start")
        return 1
    view = View(Ledger(spec.sid, root=spec.ledger_dir or None, sync=False), spec)
    q = view.current_queue(slug) or {}
    st = view.role_state(slug) or {}
    posture = st.get("state") or ("idle" if view.is_idle(slug) else "active")
    between = health.session_between_turns(role, health.claude_busy(), spec.dir)
    if not (q.get("restart_ready") and posture in ("idle", "waiting") and between) and not force:
        why = [] if q.get("restart_ready") else ["it has not said it is restart-ready (`queue --restart-ready` refreshes its doc first)"]
        if posture not in ("idle", "waiting"):
            why.append(f"its posture is {posture}")
        if not between:
            why.append("it is not known to be between turns")
        print(f"REFUSED: {slug}: {'; '.join(why)}. --force hands off anyway and can cut off work in progress")
        return 1
    if role.doc:
        doc = startdoc.check(role.doc)
        if not doc["ok"]:
            print(f"REFUSED: {slug}'s start doc would not carry the new session: " + "; ".join(doc["problems"]))
            return 1
    if posture == "waiting":  # what the operator is deciding about: a job or lease goes on without this session
        print(f"{slug} waits on {st.get('sub') or '?'}" + (f" ({st['target']})" if st.get("target") else "")
              + ": the wait stays with the role, and the new session reads it from the ledger")
    extra: list[str] = []
    if new_eco:
        extra.append(f"--set-ecosystem={slug}:{new_eco}")
    if model or new_eco:  # another provider never inherits the old model name: empty is the provider's default
        extra.append(f"--set-model={slug}:{model}")
    if effort:
        extra.append(f"--set-effort={slug}:{effort}")
    to = " ".join(x for x in (new_eco or role.ecosystem, model or ("" if new_eco else role.model), effort) if x)
    print(f"{slug}: {role.ecosystem} session {role.session[:8]} hands off to a new {to} session from "
          f"{role.doc or 'its brief (no start doc on record)'}" + ("" if q.get("restart_ready") else " (not restart-ready: forced)"))
    if new_eco:
        print(f"not checked: {tool}'s login on this host, which the new session needs")
    out = health.context_restart(spec, spec_path, role, dry_run, tuple(extra))
    print(out)
    return 1 if out.startswith("FAILED") else 0


def cmd_handoff(a) -> int:
    from roles import ShoalSpec
    return handoff(ShoalSpec.load(a.spec), a.spec, a.only, a.model, a.ecosystem, a.force, a.dry_run, a.effort)


def add_parser(sub) -> None:
    p = sub.add_parser("handoff", help="hand a role to a fresh session from its start doc, optionally on another model or provider")
    p.add_argument("--spec", required=True)
    p.add_argument("--only", required=True, help="one slug")
    p.add_argument("--model", default="", help="the new session's model (default: the recorded one)")
    p.add_argument("--effort", default="", help="the new session's effort (default: the recorded one; on another provider, its default)")
    p.add_argument("--ecosystem", default="", help="the new session's provider (default: the current one)")
    p.add_argument("--force", action="store_true", help="even though the role has not said it is restart-ready")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_handoff)
    p = sub.add_parser("park", help="stop a role and keep its queue: no session, no wakes, no pages (until unparked)")
    p.add_argument("--spec", required=True)
    p.add_argument("--only", required=True, help="comma slugs")
    p.add_argument("--note", default="", help="why (shown in the ledger and in show)")
    p.add_argument("--until", default="", help="a wait target (<kind>:<ref>); the quorum unparks once it resolves")
    p.add_argument("--by", default="user")
    p.add_argument("--force", action="store_true", help="park a role that holds an active item")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_park)
    p = sub.add_parser("unpark", help="relaunch a parked role (restart --only)")
    p.add_argument("--spec", required=True)
    p.add_argument("--only", required=True, help="comma slugs")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_unpark)
