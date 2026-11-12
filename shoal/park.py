#!/usr/bin/env python3
"""Park a role: no session, so no wakes, conditions or pages, while its queue, offers, claims and blockers are kept.

    shoal park   --spec S --only ROLE[,ROLE] [--note TEXT] [--until <kind>:<ref>] [--force] [--dry-run]
    shoal unpark --spec S --only ROLE[,ROLE] [--dry-run]
    shoal handoff --spec S --only ROLE [--model M] [--effort X] [--ecosystem E] [--force] [--dry-run]
    shoal remove-role --spec S --only ROLE [--force] [--dry-run]
    shoal remove-session --session LABEL [--force] [--dry-run]

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

Auto-park (`features.autopark`, off unless set): a role that holds nothing and has been quiet for a grace period is parked
`--until offer:<role>` by the quorum, and the next offer to it relaunches it. An empty queue is time the role waits on
someone assigning it work, and that wait has no end by itself; parked time is excluded from role-time, so each empty-queue
episode costs at most the grace. `parkable` lists everything that keeps a role out, and each reason errs towards leaving
it alone: a wrongly parked role costs one restart, but a role parked with something open loses its next wake.

Why a verb: measured 2026-10-24, a role had to be parked by hand (a spec edit, `claude stop`, then a `kill -9` of a
watcher still bound to the stopped session, which an incoming offer would have used to resume it), and a role left
waiting on a `hold:` instead would have been paged as stale after `hold_ttl_min`.
"""
from __future__ import annotations

import argparse
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
AUTOPARK_AFTER_MIN = 60.0


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


def autopark_cfg(spec) -> dict | None:
    """features.autopark: absent or false (off), true (on), or {"after_min": 60, "exempt": ["<role>"]}."""
    v = (getattr(spec, "features", {}) or {}).get("autopark")
    if not v:
        return None
    return dict(v) if isinstance(v, dict) else {}


def _running_jobs(sid: str, slug: str) -> list[str]:
    """This role's running job units (their prefix may also match a longer slug, which only keeps a role from parking)."""
    import jobs
    try:
        out = subprocess.run(["systemctl", "--user", "list-units", "--state=running", "--no-legend", "--plain",
                              jobs.unit_name(sid, slug, "*")], capture_output=True, text=True, timeout=20).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    return [ln.split()[0] for ln in out.splitlines() if ln.strip()]


def _claude_client(role) -> str:
    """Why a person may be in this Claude session ('' when nothing says so): it runs interactively, or a `claude attach`
    holds it. `session_held` is about what cannot be stopped, so it says False here, but a stop would cut this person off."""
    from backends import get as get_backend
    row = get_backend("claude")._row(role.session, include_stopped=True) or {}
    if row.get("kind") == "interactive":
        return f"it runs interactively in pid {row.get('pid')}"
    job = str(row.get("id") or role.session.split("-", 1)[0][:8])
    try:
        pids = [p for p in os.listdir("/proc") if p.isdigit()]
    except OSError:
        return ""
    for p in pids:
        try:
            with open(f"/proc/{p}/cmdline", "rb") as fh:
                argv = [x.decode(errors="replace") for x in fh.read().split(b"\0") if x]
        except OSError:
            continue
        for i in (0, 1, 2):
            if (len(argv) > i + 2 and os.path.basename(argv[i]) == "claude" and argv[i + 1] == "attach"
                    and job in argv[i + 2:]):
                return f"a client (pid {p}) is attached"
    return ""


def _session_active_min(role) -> float | None:
    """Minutes since the session's own record changed, or None when it cannot be read. Direct chat in a session writes
    nothing to the ledger, so this is the only sign of a person who has been talking to the role."""
    import health
    if role.ecosystem == "claude":
        path = health.claude_transcript(role.session)
        try:
            return (time.time() - os.path.getmtime(path)) / 60 if path else None
        except OSError:
            return None
    if role.ecosystem == "muse":
        import muse as harness
    elif role.ecosystem == "codex":
        import codex_harness as harness
    else:
        return None
    age = harness.activity_age(role.session)
    return age / 60 if age is not None else None


def parkable(spec, view, role, cbusy: dict, after_min: float) -> str:
    """Why `role` stays out of auto-park, '' when nothing does."""
    import health
    import startdoc
    from roles import is_local
    slug = role.slug
    if not role.session or getattr(role, "parked", None):
        return "no session"
    if not is_local(role, spec):
        return "another host's quorum decides"
    if role.fill != "assigned" or getattr(role, "aux", False):
        return f"fill={role.fill}: it does not wait on an assignment"
    q = view.current_queue(slug) or {}
    if q.get("active") or q.get("next") or q.get("asks"):
        return "its queue holds something"
    if view.queue(slug) or view.active(slug):
        return "it has an open offer or a live claim"
    st = view.role_state(slug) or {}
    role_status = st.get("state") or ("idle" if view.is_idle(slug) else "active")
    if role_status != "idle" and not (role_status == "waiting" and st.get("sub") == "queue-empty"):
        return f"its status is {role_status}"
    # the grace runs from the role's last write or the last message addressed to it: a message that has not been answered
    # yet may be about to wake it, and a park would drop that wake
    stamps = [view.last_by.get(slug, "")] + [m["ts"] for m in view.msgs if m.get("to") == slug]
    last = max(stamps)
    age = health.age_min(last) if last else None
    if age is None or age < after_min:
        return f"quiet for {age:.0f} of {after_min:g} min" if age is not None else "it has not written yet"
    active = _session_active_min(role)
    if active is not None and active < after_min:
        return f"its session was active {active:.0f} min ago, inside the {after_min:g} min grace"
    # blocked with no prompt open counts as between turns, but it is also how a role that asked for something in its session
    # looks: the request is in the job's `needs`, not the ledger, and a stop would lose it
    if role.ecosystem == "claude" and next((v for k, v in cbusy.items() if k.startswith(role.session)), None) == "blocked":
        return "its session is blocked, and may be waiting on an answer"
    if health.session_between_turns(role, cbusy, spec.dir) is not True:
        return "not known to be between turns"
    if session_held(role, spec.dir):
        return "a turn or an attached client holds its session"
    if role.ecosystem == "claude":
        client = _claude_client(role)
        if client:
            return f"a person may be in its session: {client}"
    held = _running_jobs(spec.sid, slug)
    if held:
        return f"a job of its is running ({held[0]})"
    if role.doc:  # the offer relaunches it from this doc, and a restart refuses one that is incomplete
        doc = startdoc.check(role.doc)
        if not doc["ok"]:
            return "its start doc would not carry a new session: " + "; ".join(doc["problems"])
    return ""


def auto_park(spec, spec_path: str, view, conds: list, state: dict, acts: list, dry_run: bool = False) -> None:
    """Health quorum: park at most one role a run (a park stops a session and can take seconds), one no condition is
    open on, and try a role again only after a full grace if its park failed."""
    cfg = autopark_cfg(spec)
    if cfg is None or not spec_path:
        return
    from health import age_min, claude_busy, iso
    from ledger import SYSTEM_WRITER
    cbusy = claude_busy()
    after = float(cfg.get("after_min", AUTOPARK_AFTER_MIN))
    exempt = set(cfg.get("exempt") or [])
    raised = {c.get("slug") for c in conds}
    tried = state.setdefault("parks", {})
    for r in spec.roles:
        if r.slug in exempt or r.slug in raised:
            continue
        if tried.get(r.slug) and (age_min(tried[r.slug]) or 0.0) < after:
            continue
        if parkable(spec, view, r, cbusy, after):
            continue
        if dry_run:
            acts.append((r.slug, "auto-park", f"would park until an offer lands (dry-run; quiet past {after:g} min)"))
            return
        tried[r.slug] = iso()
        rc = park(spec, spec_path, [r.slug], note=f"auto-park: nothing queued for {after:g} min; the next offer relaunches it",
                  until=f"offer:{r.slug}", by=SYSTEM_WRITER)
        acts.append((r.slug, "auto-park", "parked until an offer lands" if rc == 0 else f"park refused (rc {rc})"))
        return


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
            dry_run: bool = False, effort: str = "", note: str = "") -> int:
    """Hand a role to a fresh session in place, from its start doc rather than a compaction, optionally on another model
    or provider: the quorum's `context_restart` run by hand, under the quorum's gate. Everything that can be proved is
    proved before the old session stops: the role is at a boundary (restart-ready, status idle or waiting, between
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
    client = _claude_client(role) if role.ecosystem == "claude" else ""
    if client:
        # an attached `claude attach` keeps the worker alive, so the stop reports it still live and nothing restarts
        # (measured 2026-11-10: it-tune's card was attached 8 s before the quorum's restart, which failed STILL LIVE)
        print(f"REFUSED: a person may be in {slug}'s session: {client}. Detach the client (its card or terminal: "
              "Ctrl+\\, or quit it), then try again; --force does not change that")
        return 1
    tool = ECOSYSTEM_TOOLS.get(new_eco or role.ecosystem, "")
    if tool and not tool_which(tool):
        print(f"REFUSED: {tool} is not on this host's tool path, so a new {new_eco or role.ecosystem} session cannot start")
        return 1
    view = View(Ledger(spec.sid, root=spec.ledger_dir or None, sync=False), spec)
    q = view.current_queue(slug) or {}
    st = view.role_state(slug) or {}
    role_status = st.get("state") or ("idle" if view.is_idle(slug) else "active")
    between = health.session_between_turns(role, health.claude_busy(), spec.dir)
    if not (q.get("restart_ready") and role_status in ("idle", "waiting") and between) and not force:
        why = [] if q.get("restart_ready") else ["it has not said it is restart-ready (`queue --restart-ready` refreshes its doc first)"]
        if role_status not in ("idle", "waiting"):
            why.append(f"its status is {role_status}")
        if not between:
            why.append("it is not known to be between turns")
        print(f"REFUSED: {slug}: {'; '.join(why)}. --force hands off anyway and can cut off work in progress")
        return 1
    if role.doc:
        doc = startdoc.check(role.doc)
        if not doc["ok"]:
            print(f"REFUSED: {slug}'s start doc would not carry the new session: " + "; ".join(doc["problems"]))
            return 1
        held = startdoc.split_job_folders(role.doc)[0]
        if held:  # a forced handoff, or a role that went restart-ready earlier: the prune guard keeps those jobs until the doc stops naming them
            print(f"note: its start doc still names job folder(s) {', '.join(held)}: kept until the doc stops naming them")
    if role_status == "waiting":  # what the operator is deciding about: a job or lease goes on without this session
        print(f"{slug} waits on {st.get('sub') or '?'}" + (f" ({st['target']})" if st.get("target") else "")
              + ": the wait stays with the role, and the new session reads it from the ledger")
    extra: list[str] = []
    if new_eco:
        extra.append(f"--set-ecosystem={slug}:{new_eco}")
    if model or new_eco:  # another provider never inherits the old model name: empty is the provider's default
        extra.append(f"--set-model={slug}:{model}")
    if effort:
        extra.append(f"--set-effort={slug}:{effort}")
    if note.strip():  # the operator's restart note, in the new session's first prompt
        extra.append(f"--note={slug}:{note.strip()}")
    to = " ".join(x for x in (new_eco or role.ecosystem, model or ("" if new_eco else role.model), effort) if x)
    print(f"{slug}: {role.ecosystem} session {role.session[:8]} hands off to a new {to} session from "
          f"{role.doc or 'its brief (no start doc on record)'}" + ("" if q.get("restart_ready") else " (not restart-ready: forced)"))
    if new_eco:
        print(f"not checked: {tool}'s login on this host, which the new session needs")
    out = health.context_restart(spec, spec_path, role, dry_run, tuple(extra))
    print(out)
    return 1 if out.startswith("FAILED") else 0


def remove_role(spec, spec_path: str, slug: str, force: bool = False, dry_run: bool = False,
                no_archive: bool = False) -> int:
    import contextlib
    import retire_hooks as rh
    role = spec.role(slug)
    from roles import is_local
    if role is None or not is_local(role, spec):
        return _remove_role(spec, spec_path, slug, force, dry_run)
    try:
        with contextlib.ExitStack() as stack:
            op = rh.Operation(rh.role_target(spec, role), rh.ledger_for(spec), stack, no_archive)
            op.preview()
            rc = _remove_role(spec, spec_path, slug, force, dry_run, op)
            if rc or dry_run:
                return rc
            print(f"removal {slug}: complete")
            return op.finish()
    except (rh.Refused, OSError, RuntimeError) as exc:
        print(f"REFUSED: {exc}")
        return 1


def _remove_role(spec, spec_path: str, slug: str, force: bool = False, dry_run: bool = False, hook=None) -> int:
    """Take a role out for good: stop its session and watchers, prune its incarnations from every harness's listing, remove
    it from the spec and write `role-removed`. The ledger keeps its history. In that order because a prune protects any
    session the spec still names, and a role only stops being recognised once the spec drops it. Its open offers are
    withdrawn first, because nothing else takes an offer back from a role that is gone and its item stays out of every fill.
    Its live claims are released too: replay already ignores a claim by a writer not in the spec, but it would come back to
    life if the slug were ever added again. A session
    held by a turn or client nothing here can stop refuses, even forced, as does one that will not stop; an active item
    refuses unless forced. It takes the role's restart lock, so a restart in flight cannot bring the role back."""
    import prune
    from cli import persist_spec
    from ledger import SYSTEM_WRITER, Ledger, View, item_key
    from roles import host_of, is_local
    role = spec.role(slug)
    if role is None:
        print(f"remove-role: no role {slug} (spec has {[x.slug for x in spec.roles]})")
        return 2
    if not is_local(role, spec):
        print(f"REFUSED: {slug} runs on {host_of(role, spec)}: remove it on its own host")
        return 1
    if role.session and session_held(role, spec.dir):
        print(f"REFUSED: {slug}'s {role.ecosystem} session is held by a running turn or an attached client, and nothing here "
              f"can stop it; --force does not change that. Detach it or let the turn end, then try again")
        return 1
    led = Ledger(spec.sid, root=spec.ledger_dir)
    led.pull()
    view = View(led, spec)
    q = view.current_queue(slug) or {}
    if q.get("active") and not force:
        print(f"REFUSED: {slug} holds active item {q['active']}; let it finish or release it, or pass --force")
        return 1
    st = view.role_state(slug) or {}
    if st.get("state") == "waiting":  # a job unit keeps its lease after the role is gone: the operator is deciding about it
        print(f"{slug} waits on {st.get('sub') or '?'}" + (f" ({st['target']})" if st.get("target") else "")
              + ": a job or lease behind that wait stays until it ends or is stopped")
    claims = [str(c.get("item")) for c in view.active(slug) if c.get("item")]
    offers = sorted({key for key, os_ in view.by_item.items() for o in os_ if o.get("to") == slug and o.get("status") in ("open", "claimed")})
    for item in q.get("next") or []:
        print(f"{slug}: next item {item} goes back to no one: offer it elsewhere if it still matters")
    if hook and not dry_run:
        hook.prepare()
    if role.session:
        did = stop_session(role, dry_run)
        print(f"{slug}: {did}")
        if "STILL LIVE" in did:
            print(f"REFUSED: {slug}'s session did not stop, so it could go on writing as the role: nothing was changed")
            return 1
    if dry_run:
        print(f"{slug}: would end ledger-watch {watchers_of(spec.sid, slug) or 'none running'}, release its claims {claims or 'none'}, "
              f"withdraw its offers {offers or 'none'}, prune these incarnations, remove the role from the spec and write role-removed:")
        prune.run(specs=[spec_path], min_age=0, dry_run=True, only_slug=slug, max_seq=10 ** 6, trigger="remove", sizes=False)
        if role.session:
            print(f"(its current session {role.session[:12]} shows as kept above; the real run clears it first, and removes it)")
        return 0
    killed, left = end_watchers(spec.sid, slug)
    print(f"{slug}: ledger-watch ended: {killed or 'none running'}" + (f"; STILL RUNNING: {left}" if left else ""))
    old, role.session, role.parked = role.session, "", {}
    persist_spec(spec, spec_path, note=f"remove {slug}: session cleared")
    prune.run(specs=[spec_path], delete=prune.feature(spec)["delete"], min_age=0, only_slug=slug, max_seq=10 ** 6, trigger="remove", sizes=False)
    for item in claims:  # as the role itself: a release counts only from the claim's own writer, and its session is stopped
        led.append(slug, "release", item=item_key(item), kind="decline", reason="role removed")
    for item in offers:
        led.append("user", "withdraw", item=item, to=slug, reason="role removed")
    spec.roles = [r for r in spec.roles if r.slug != slug]
    persist_spec(spec, spec_path, note=f"remove {slug}")
    led.append(SYSTEM_WRITER, "system", event="role-removed", role=slug, session=old)
    print(f"removed {slug} from {spec.sid}: its ledger history stays")
    return 0


def remove_session(key: str, force: bool = False, dry_run: bool = False, no_archive: bool = False) -> int:
    import contextlib
    import retire_hooks as rh
    import standalone
    entries = standalone.load_standalone(raw=True)
    entry = next((e for e in entries if not e.get("host") and key in (e.get("label"), e.get("session"), e.get("job"))), None)
    if entry is None:
        return _remove_session(key, force, dry_run)
    try:
        with contextlib.ExitStack() as stack:
            op = rh.Operation(rh.session_target(entry), rh.ledger_for(), stack, no_archive)
            op.preview()
            rc = _remove_session(key, force, dry_run, op)
            if rc or dry_run:
                return rc
            print(f"removal {op.target.role}: complete")
            return op.finish()
    except (rh.Refused, OSError, RuntimeError) as exc:
        print(f"REFUSED: {exc}")
        return 1


def _remove_session(key: str, force: bool = False, dry_run: bool = False, hook=None) -> int:
    """Take a saved standalone session out for good: stop it (a live one only when forced), remove it from its harness's
    listing and forget it. Refuses the session this runs inside, and one a worktree still hangs off unless forced."""
    import types
    import prune
    import ui_herdr
    from backends import claude_base, get as get_backend
    entries = ui_herdr.load_standalone(raw=True)
    e = next((x for x in entries if not x.get("host") and key in (x.get("label"), x.get("session"), x.get("job"))), None)
    if e is None:
        print(f"REFUSED: no saved standalone session {key!r} on this host (a writer with no saved session can only be hidden)")
        return 1
    if hook and e["session"] != hook.target.session:
        print("REFUSED: the saved seat changed sessions during retirement preflight; preview it again")
        return 1
    label = e.get("label") or e["session"][:12]
    harness = e.get("harness") or "claude"
    job = str(e.get("job") or "")
    seat = types.SimpleNamespace(ecosystem=harness, session=e["session"])
    here = os.path.basename(os.environ.get("CLAUDE_JOB_DIR", "").rstrip("/"))
    if here and here in (job, e["session"][:8]):
        print(f"REFUSED: {label} is the session running this")
        return 1
    if session_held(seat):
        print(f"REFUSED: {label}'s {harness} session is held by a running turn or an attached client, and nothing here can stop "
              f"it; --force does not change that")
        return 1
    live = harness == "claude" and claude_live(seat)
    if live and not force:
        print(f"REFUSED: {label} has a live session: --force stops it first and can cut off work in progress")
        return 1
    if harness == "claude" and job and not force:
        wt = prune.git_worktree_under(os.path.join(claude_base(), "jobs", job, "tmp"))
        if wt:
            print(f"REFUSED: a git worktree is under its job directory ({wt}): --force removes the session anyway")
            return 1
    if dry_run:
        print(f"{label}: would {'stop it, ' if live else ''}remove it from the {harness} listing and forget it")
        return 0
    if hook:
        hook.prepare()
    if live:
        print(f"{label}: {stop_session(seat)}")
    try:
        print(f"{label}: {get_backend(harness).prune(e['session'], job)}")
    except Exception as exc:  # noqa: BLE001 - the harness refused: the entry stays, so this can be tried again
        print(f"REFUSED: {harness} did not remove {label} ({str(exc)[:200]}): it is still saved")
        return 1
    ui_herdr.save_standalone([x for x in entries if x is not e])
    print(f"removed {label}")
    return 0


def set_fill(spec, spec_path: str, slug: str, fill: str, note: str = "", wake: bool = True, dry_run: bool = False) -> int:
    """Change how a role gets its work (`fill`: self, assigned or none) without restarting it: persist the spec, record a
    `role-fill` event, and tell the role by a woken message, since its brief keeps the old fill until its next restart.
    Its watcher reloads the changed spec at its next tick, so a self-filling role's board wakes follow."""
    import briefs
    from cli import persist_spec
    from ledger import SYSTEM_WRITER, Ledger
    from roles import FILL_MODES
    role = spec.role(slug)
    if role is None:
        print(f"set-fill: unknown role {slug!r} (spec has {[r.slug for r in spec.roles]})")
        return 1
    if fill not in FILL_MODES:
        print(f"set-fill: fill is one of {', '.join(FILL_MODES)}")
        return 1
    if getattr(role, "aux", False) and fill != "self":
        print(f"set-fill: REFUSED: {slug} is the residual lane (aux), which fills itself by design")
        return 1
    was = role.fill
    if was == fill:
        print(f"{slug}: fill is already {fill}; nothing to do")
        return 0
    why = f": {note}" if note else ""
    text = (f"FILL CHANGED for {slug}: {was} -> {fill} (the maintainer{why}). {briefs.FILL_SENTENCE[fill]} Your brief still "
            f"says fill={was} until your next restart; this message wins over it. Run `next` and take work this way from now.")
    tell = wake and bool(role.session)
    if dry_run:
        print(f"[dry-run] {slug}: fill {was} -> {fill}; save the spec, record role-fill"
              + (f", wake it with: {text}" if tell else " (no session: nothing to wake)" if wake else " (no wake)"))
        return 0
    role.fill = fill
    persist_spec(spec, spec_path, note=f"set-fill {slug} {was}->{fill}")
    led = Ledger(spec.sid, root=spec.ledger_dir)
    led.append(SYSTEM_WRITER, "system", event="role-fill", role=slug, fill=fill, was=was, note=note or None)
    if tell:
        led.append("user", "msg", to=slug, kind="info", wake=True, text=text)
    print(f"{slug}: fill {was} -> {fill}" + (" (woken to take work this way)" if tell else ""))
    return 0


def cmd_set_fill(a) -> int:
    from roles import ShoalSpec
    return set_fill(ShoalSpec.load(a.spec), a.spec, a.only, a.fill, a.note, not a.no_wake, a.dry_run)


def cmd_remove_role(a) -> int:
    from roles import ShoalSpec
    return remove_role(ShoalSpec.load(a.spec), a.spec, a.only, a.force, a.dry_run, getattr(a, "no_archive", False))


def cmd_remove_session(a) -> int:
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "ui"))
    return remove_session(a.session, a.force, a.dry_run, getattr(a, "no_archive", False))


def cmd_handoff(a) -> int:
    if getattr(a, "prepare", False) or getattr(a, "session", ""):
        import handoff_flow
        try:
            return handoff_flow.run(a)
        except (handoff_flow.HandoffError, OSError, RuntimeError) as exc:
            print(f"REFUSED: {exc}", flush=True)
            return 1
    from roles import ShoalSpec
    if not a.spec or not a.only:
        print("handoff: a role needs --spec and --only (a standalone seat takes --session and --prepare)")
        return 2
    spec = ShoalSpec.load(a.spec)
    expected = getattr(a, "expected_session", "")
    if expected and (spec.role(a.only) is None or spec.role(a.only).session != expected):
        print("REFUSED: this role changed sessions since the preview: plan the handoff again")
        return 1
    return handoff(spec, a.spec, a.only, a.model, a.ecosystem, a.force, a.dry_run, a.effort, getattr(a, "note", ""))


def add_parser(sub) -> None:
    p = sub.add_parser("remove-role", help="take a role out for good: stop it, prune its incarnations, drop it from the spec; the ledger keeps its history")
    p.add_argument("--no-archive", action="store_true", help="skip the retirement hook and record the opt-out")
    p.add_argument("--spec", required=True)
    p.add_argument("--only", required=True, help="one slug")
    p.add_argument("--force", action="store_true", help="even with an active item")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_remove_role)
    p = sub.add_parser("set-fill", help="change how a role gets its work (self, assigned or none) without a restart; "
                                        "it is told by a woken message")
    p.add_argument("--spec", required=True)
    p.add_argument("--only", required=True, help="one slug")
    p.add_argument("--fill", required=True, choices=["self", "assigned", "none"])
    p.add_argument("--note", default="", help="why, for the role and the ledger")
    p.add_argument("--no-wake", action="store_true", help="record it without waking the role")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_set_fill)
    p = sub.add_parser("remove-session", help="take a saved standalone session out for good: stop it, remove it from its harness, forget it")
    p.add_argument("--no-archive", action="store_true", help="skip the retirement hook and record the opt-out")
    p.add_argument("--session", required=True, help="its label, session id or job")
    p.add_argument("--force", action="store_true", help="even when it is live or a worktree hangs off it")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_remove_session)
    p = sub.add_parser("handoff", help="hand a role or saved standalone seat to a fresh session from its start doc")
    target = p.add_mutually_exclusive_group(required=True)
    target.add_argument("--spec")
    target.add_argument("--session", default="", help="a saved standalone seat on this host (checkpoint first by default)")
    p.add_argument("--only", default="", help="one role slug, with --spec")
    checkpoint = p.add_mutually_exclusive_group()
    checkpoint.add_argument("--prepare", action="store_true", help="request a checkpoint if needed, wait until restart-ready, then hand off")
    checkpoint.add_argument("--no-prepare", action="store_true", help="standalone: use its existing valid checkpoint, only while idle")
    p.add_argument("--wait-seconds", type=int, default=3600, help="checkpoint deadline with --prepare (default 3600)")
    p.add_argument("--expected-session", default="", help="refuse if the seat changed sessions since the preview")
    p.add_argument("--through-question", action="store_true",
                   help="standalone Claude: a seat stopped at an open question is stopped and resumed with the checkpoint request (the question is dropped)")
    p.add_argument("--await-idle", action="store_true", help=argparse.SUPPRESS)  # the detached self-handover finisher
    p.add_argument("--doc", default="", help="standalone: its start doc (default: the saved one)")
    p.add_argument("--note", default="", help="a note for the new session, added to its first prompt after the start doc")
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
