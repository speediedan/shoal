#!/usr/bin/env python3
"""shoal: scripted SHOAL group management (start, generation restart, watchers,
state, health, reports). Stdlib only by design (systemd-safe, no venv).
Runbook §1 has the operator sequence; the readme is the human map.

Subcommands:
  doctor        preflight (host) + live health (with --spec). Read-only.
  start         mint a new shoal (id, dirs, ledger, shoal.json), launch its first
                generation, save live ids + generation record back, verify
                loops. Roles: SLUG,ECO,TITLE,DOC[,aux][,fill=..][,model=M]
                [,effort=E] (DOC = the single plan_state.md; grants come from
                its grants-needed section, never the CLI).
  restart       staged generation restart from --spec shoal.json: handoffs,
                sync-plans, retire, restart. Every stage honors --dry-run.
                Non-opencode roles auto-launch (claude --bg) unless
                --consumer-launched SLUG:ID or --defer-consumers.
  retire        close the current generation (ended_utc), mark the spec
                retired, optionally uninstall its watcher units. Deletes no
                session (retire-by-neglect stands).
  transition    THE session-side state primitive: heartbeat + wait proof +
                telemetry in one command (state.py).
  status        one line per role: session state, heartbeat/wait posture,
                doc; plus ledger, watcher ticks and timers.
  report        per-incarnation report, md (versioned) + html (bulk).
  watch         systemd-timer watchers: lanes | pipeline (fire into the ledger).
  heartbeat     stall daemon over /tmp/<id>/hb-* + wait-* (timer */5; specs
                with features.health run the health quorum instead).
  brief         print/write one role's first prompt (restart uses this).
  install-systemd / uninstall-systemd   watcher units per shoal generation.
  spec absolutize-docs   rewrite relative role docs to absolute paths under --base.
  ledger verbs  offer | withdraw | claim | release | done | msg | show | fill |
                sync | ledger-init | ledger-lint | ledger-watch (ledger_cli.py,
                ledger_watch.py): the unified primitive, 2026-09-20.

Flattened and ledger-only (2026-09-24): `start` creates shoals/<id>/ in the
queue repo, every role gets the same brief and arms the same `ledger-watch`,
there is no coordinator role, `user` is the only writer that offers work to
other roles, and roles with `fill=self` pop the priority table (`--source
github-project:OWNER/N`) when their queue is empty.

Claude roles launch headless (`claude --bg -n <slug-gen>`); sends into a
live claude session are refused (resume would fork a copy). Every new
session carries its role's grants-needed list and asks the maintainer
first-person, all in one question, before starting work: that ask is the
only interactive step.
"""
from __future__ import annotations

import argparse
import dataclasses
import datetime
import json
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "telemetry"))
sys.path.insert(0, os.path.join(HERE, "ui"))
from ledger import SYSTEM_WRITER  # noqa: E402
import config  # noqa: E402

from backends import ClaudeBackend, UnsupportedError, get as get_backend  # noqa: E402
from briefs import brief_for  # noqa: E402
from heartbeat import age_min, cmd_heartbeat, hb_path, touch as hb_touch  # noqa: E402
import ledger_cli as _ledger_cli  # noqa: E402
import ledger_watch as _ledger_watch  # noqa: E402
import muse as _muse  # noqa: E402
import codex_harness as _codex  # noqa: E402
import digest as _digest  # noqa: E402
import jobs as _jobs  # noqa: E402
import worker as _worker  # noqa: E402
import health as _health  # noqa: E402
import kill_audit as _kill_audit  # noqa: E402
import aux as _aux  # noqa: E402
import upgrade as _upgrade  # noqa: E402
import context_cost as _context_cost  # noqa: E402
import park as _park  # noqa: E402
import startdoc as _startdoc  # noqa: E402
from roles import (SHOAL_DATA_ROOT, ShoalSpec, Role, default_spec_path, host_of, is_local,  # noqa: E402
                   is_wake_relay, home_dir, state_dir, utcnow_iso)
from systemd import cmd_install as cmd_install_systemd  # noqa: E402
from systemd import cmd_uninstall as cmd_uninstall_systemd  # noqa: E402
from watchers import cmd_lanes, cmd_pipeline, shoal_emit  # noqa: E402
import doctor as _doctor  # noqa: E402
import state as _state  # noqa: E402
import shoal_report as _report  # noqa: E402
try:  # the operator UI must never take the protocol CLI down with it (timers run this file)
    import ui_cli as _ui  # noqa: E402
    _ui_err = ""
except Exception as _exc:  # noqa: BLE001
    _ui, _ui_err = None, f"{type(_exc).__name__}: {_exc}"


def _ui_parsers(sub) -> None:
    """Register `ui` and `view`; on any failure, stubs that name the cause (never a broken CLI)."""
    global _ui_err
    if _ui is not None:
        try:
            _ui.add_parsers(sub)
            return
        except Exception as exc:  # noqa: BLE001
            _ui_err = f"{type(exc).__name__}: {exc}"
    for name in ("ui", "view"):
        if name in sub.choices:
            continue
        p = sub.add_parser(name, help="operator UI (unavailable: see `shoal ui`)")
        p.add_argument("rest", nargs="*")
        p.set_defaults(func=lambda a: (print(f"shoal ui is unavailable: {_ui_err}"), 1)[1])

try:
    from shoal_telemetry import run_pin  # noqa: E402
except Exception:  # pragma: no cover
    def run_pin():
        return {}

ALL_STAGES = ("handoffs", "sync-plans", "retire", "restart")


def want(stages: str, name: str) -> bool:
    return name in [s.strip() for s in stages.split(",") if s.strip()]


def parse_role(spec: str) -> Role:
    """SLUG,ECO,TITLE,DOC[,aux][,fill=self|assigned|none][,scope=repo|repo]
    [,scope-label=NAME][,server=H][,model=M][,effort=E][,seq=N]. DOC is the
    single plan_state.md start document. Grants come from the doc's
    grants-needed section at brief time — never from the CLI. seq sets the
    session tag ({slug}-r{seq}); default 1. fill defaults to self (5.k).
    scope= entries narrow by repo; scope-label= entries require the board label
    (labels_any) — e.g. a migration lane that only sees its own labeled items."""
    parts = spec.split(",")
    if len(parts) < 4:
        raise ValueError(f"bad --role spec (want SLUG,ECO,TITLE,DOC[,...]): {spec!r}")
    slug, eco, title, doc = parts[:4]
    model, effort, seq = "", "", 1
    fill, scope, server, aux = "self", {}, "", False
    for tok in parts[4:]:
        if tok == "aux":
            aux = True  # residual lane (architecture §4.1); always fill=self
        elif tok.startswith("fill="):
            fill = tok[len("fill="):]
        elif tok.startswith("scope="):
            scope = {"repos": [x for x in tok[len("scope="):].split("|") if x]}
        elif tok.startswith("scope-label="):
            scope.setdefault("labels_any", []).append(tok[len("scope-label="):])
        elif tok.startswith("server="):
            server = tok[len("server="):]
        elif tok.startswith("model="):
            model = tok[len("model="):]
        elif tok.startswith("effort="):
            effort = tok[len("effort="):]
        elif tok.startswith("seq="):
            try:
                seq = int(tok[len("seq="):])
            except ValueError:
                raise ValueError(f"bad --role token {tok!r} in {spec!r}")
        else:
            raise ValueError(f"bad --role token {tok!r} in {spec!r}")
    return Role(slug=slug, ecosystem=eco, title=title, doc=doc,
                model=model, effort=effort,
                seq=seq, fill="self" if aux else fill, scope=scope, server=server,
                aux=aux)


def load_spec(args: argparse.Namespace) -> ShoalSpec:
    if not getattr(args, "spec", ""):
        print("restart needs --spec <shoal.json> (legacy coord/lane flags removed 2026-09-19)")
        sys.exit(1)
    return ShoalSpec.load(args.spec)


# ---- stages ---------------------------------------------------------------

def _doc_problem(role: Role, header: bool) -> str:
    """Why a role's start doc would not brief a new session ("" when it is fine): missing,
    no plan header (`header`), or missing the grants-needed / current-state sections."""
    try:
        text = open(role.doc).read()
    except OSError:
        return f"MISSING: {role.doc} ({role.slug} has no start doc)"
    if header:
        # a titled plan or handoff, or a start doc with the sections a brief points to (measured 2026-10-12:
        # three start docs written to the four headings were refused for their titles alone)
        titled = re.search(r"^- handing-off-model:|^# Handoff|^# .*(live plan|coordination plan|live plan[ _]state|plan[ _]state)",
                           text, flags=re.M | re.I)
        sectioned = all(re.search(rf"^##\s+{s}\b", text, flags=re.M | re.I) for s in ("mission", "current state"))
        if not (titled or sectioned):
            return f"INCOMPLETE: {role.doc} (no plan header: title it as a plan_state, or give it ## Mission and ## Current state)"
        return ""
    missing = [s for s in ("grants-needed", "current state") if s not in text.lower()]
    return f"INCOMPLETE: {role.doc} (missing: {', '.join(missing)})" if missing else ""


def _check_docs(spec: ShoalSpec, dry_run: bool, header: bool, only: list[str] | None) -> int:
    """Check every start doc; with `only` (a partial restart) a problem in a role that is not
    being relaunched is printed as info and never fails the stage (its session keeps running)."""
    rc = 0
    for role in spec.roles:
        if dry_run:
            print(f"[dry-run] verify {role.doc} (mission, current state, grants-needed, next)" if header
                  else f"[dry-run] sections in {role.doc}")
            continue
        problem = _doc_problem(role, header)
        if not problem:
            print(f"ok: {role.doc}")
        elif only is not None and role.slug not in only:
            print(f"info (not relaunched, not a failure): {problem}")
        else:
            print(problem)
            rc = 5
    return rc


def stage_handoffs(spec: ShoalSpec, dry_run: bool, only: list[str] | None = None) -> int:
    print("=== 0a. start docs (one plan_state.md per role; sessions keep them current)")
    print("each role's doc verifies: presence + plan header:")
    return _check_docs(spec, dry_run, True, only)


def stage_sync_plans(spec: ShoalSpec, dry_run: bool, only: list[str] | None = None) -> int:
    print("=== 0b. start-doc sections (grants-needed + current state present)")
    return _check_docs(spec, dry_run, False, only)


def stage_retire(spec: ShoalSpec, old_sessions: list[str],
                 dry_run: bool) -> int:
    print("=== 0c. retire (verify silent; record, don't delete)")
    print("no archive API exists in either ecosystem; old sessions retire by neglect after verification.")
    be = get_backend("opencode")
    for sid in old_sessions:
        if dry_run:
            print(f"[dry-run] status {sid}")
            continue
        try:
            print(f"{sid}: {be.status(sid, spec.dir)} (verify silent in-transcript before standing down)")
        except Exception as exc:  # noqa: BLE001
            print(f"{sid}: unreachable ({exc})")
    return 0


_MISSING = object()


def merge_spec(base: dict, ours: dict, theirs: dict) -> tuple[dict, list[str]]:
    """Apply what THIS process changed (base -> ours) onto the copy on the remote (theirs): top-level
    keys whole, roles by slug and then field by field, generations by `gen`. A key both sides changed
    differently takes ours and is returned as a conflict to report. Another host's restart of its own
    role and this host's restart of another role then both survive (a whole-file save lost one)."""
    import copy
    out = copy.deepcopy(theirs)
    conflicts: list[str] = []

    def changed(o, b):
        return o is not b and o != b

    # keys the spec model never writes (the ledger-init snapshot's `snapshot_utc`, `canonical`) are not this
    # process's to change: `ours` lacks them only because `to_dict` does not produce them
    modeled = set(ShoalSpec(sid="_").to_dict()) | {"home"}
    for k in (set(base) | set(ours)) - {"roles", "generations"}:
        if k not in modeled:
            continue
        o, b, t = ours.get(k, _MISSING), base.get(k, _MISSING), theirs.get(k, _MISSING)
        if not changed(o, b):
            continue
        if changed(t, b) and changed(t, o):
            conflicts.append(k)
        if o is _MISSING:
            out.pop(k, None)
        else:
            out[k] = copy.deepcopy(o)
    # roles: keyed by slug, merged per field
    bR = {r["slug"]: r for r in base.get("roles", [])}
    oR = {r["slug"]: r for r in ours.get("roles", [])}
    tR = {r["slug"]: r for r in theirs.get("roles", [])}
    order = [r["slug"] for r in theirs.get("roles", [])] + [r["slug"] for r in ours.get("roles", [])
                                                           if r["slug"] not in tR]
    roles = []
    for slug in order:
        b, o, t = bR.get(slug), oR.get(slug), tR.get(slug)
        if not changed(o, b):
            res = t
        elif b is None:  # added here
            if t is not None and changed(t, o):
                conflicts.append(f"role {slug}")
            res = o
        elif o is None:  # removed here
            res = None
        else:  # modified here: our changed fields onto theirs
            res = copy.deepcopy(t if t is not None else b)
            for f in set(b) | set(o):
                of, bf = o.get(f, _MISSING), b.get(f, _MISSING)
                if not changed(of, bf):
                    continue
                tf = t.get(f, _MISSING) if t is not None else _MISSING
                if t is not None and changed(tf, bf) and changed(tf, of):
                    conflicts.append(f"role {slug}.{f}")
                if of is _MISSING:
                    res.pop(f, None)
                else:
                    res[f] = copy.deepcopy(of)
        if res is not None:
            roles.append(res)
    out["roles"] = roles
    # generations: keyed by gen, ours wins for an entry it changed
    if "generations" in ours or "generations" in theirs:
        key = lambda g: str(g.get("gen", ""))  # noqa: E731
        bG = {key(g): g for g in base.get("generations", []) or []}
        oG = {key(g): g for g in ours.get("generations", []) or []}
        gens = {key(g): g for g in theirs.get("generations", []) or []}
        for k, g in oG.items():
            if changed(g, bG.get(k)):
                gens[k] = copy.deepcopy(g)
        out["generations"] = sorted(gens.values(), key=lambda g: (int(g.get("incarnation", 0) or 0), str(g.get("started_utc", ""))))
    return out, conflicts


def persist_spec(spec: ShoalSpec, path: str, note: str = "", attempts: int = 3) -> None:
    """Save the spec; when the path lives under the ledger clone it is the canonical copy, so
    commit+push (remote agents read it). A spec outside the clone (tests, a pinned $SHOAL_SPEC)
    saves plain.

    Multi-host safe: under the clone's Lock it pulls, applies only THIS process's changes (the spec
    as loaded -> as now, `merge_spec`) onto the copy another host may have pushed meanwhile, commits
    and pushes; a rejected push undoes only its own commit and goes round again. A whole-file save
    without a pull lost the other host's edits, and two hosts' spec commits rebased onto each other
    conflict and wedge the clone (the spec is JSON, so it cannot merge by union like the ledger)."""
    try:
        root = spec.ledger_dir
    except Exception:
        root = ""
    in_clone = bool(root) and os.path.commonpath([os.path.abspath(path), os.path.abspath(root)]) == os.path.abspath(root)
    if not in_clone:
        spec.save(path)
        return
    from ledger import Lock, git as _git, has_remote, sync_pull_locked
    rel = os.path.relpath(path, root)
    ours = spec.to_dict()
    base = getattr(spec, "base", None)
    remote = has_remote(root)
    pushed = not remote
    merged, conflicts = ours, []
    for _ in range(max(1, attempts)):
        with Lock(root):
            if remote:
                sync_pull_locked(root)
            if base is not None and os.path.exists(path):
                with open(path) as fh:
                    merged, conflicts = merge_spec(base, ours, json.load(fh))
            ShoalSpec.from_dict(merged).save(path)
            _git(root, "add", "--", rel, check=False)
            c = _git(root, "commit", "--quiet", "-m", f"{spec.sid}: spec update{(' ' + note) if note else ''}",
                     "--", rel, check=False)
            if not remote:
                break
            branch = _git(root, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
            if _git(root, "push", "--quiet", "origin", branch, check=False, timeout=60).returncode == 0:
                pushed = True
                break
            if c.returncode == 0:
                # rejected: another host pushed first. Undo only this commit (the Lock is held, so HEAD
                # is ours) and merge again onto the fresh remote copy, never by rebase
                _git(root, "reset", "--quiet", "--soft", "HEAD~1", check=False)
                _git(root, "checkout", "--quiet", "HEAD", "--", rel, check=False)
    # the in-memory spec becomes what was saved, and the next save's base
    fresh = ShoalSpec.from_dict(merged)
    for f in dataclasses.fields(ShoalSpec):
        setattr(spec, f.name, getattr(fresh, f.name))
    spec.base = json.loads(json.dumps(merged))
    for k in conflicts:
        print(f"WARN spec merge: {k} was also changed on the remote; this host's value kept")
    if pushed:
        print(f"spec committed + pushed in {root}")
    else:
        print(f"WARN spec committed in {root} but NOT pushed after {attempts} attempt(s); the next sync pushes it")


def write_brief(spec: ShoalSpec, role: Role) -> str:
    d = os.path.join(home_dir(spec.sid), "briefs")
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, f"{role.tag}.md")
    with open(path, "w") as fh:
        fh.write(brief_for(role, spec) + "\n")
    return path


def _same_session(a: str, b: str) -> bool:
    """Two ids name one session when either is a prefix of the other (a claude short id
    against its full uuid)."""
    return bool(a and b and (a.startswith(b) or b.startswith(a)))


def sidecar_verdict(sc: dict, launched: str, old: list[str], since_utc: str,
                    mtime_utc: str = "") -> tuple[bool, str, str]:
    """Judge a ledger-watch sidecar for a role just (re)launched as `launched`.

    Returns (alive, adopt, note). A sidecar bound to a replaced session (one of `old`), or
    armed before the launch (`since_utc`; the file's mtime stands in when it records no
    `armed_utc`), belongs to the previous session and is not alive. `adopt` is the id to
    record for the role: the sidecar's id only when it is a longer form of the launched id
    or the launched id is unknown, else ""."""
    if not sc:
        return False, "", "no sidecar"
    bound = sc.get("session_id", "")
    armed = sc.get("armed_utc", "") or mtime_utc
    if bound and launched and _same_session(bound, launched):
        return True, (bound if len(bound) > len(launched) else ""), f"bound to {bound}"
    if bound and any(_same_session(bound, o) for o in old):
        return False, "", f"stale: bound to the replaced session {bound}"
    if since_utc and armed and armed < since_utc:
        return False, "", f"stale: armed {armed}, before the launch at {since_utc}"
    if not launched:
        return True, bound, f"bound to {bound or '(no id yet)'}"
    if bound:
        return True, "", f"bound to {bound} != launched id {launched} (not adopted)"
    return True, "", "armed after the launch (no id yet)"


def stale_watchers(sid: str, slug: str, old: list[str], current: str = "") -> list[tuple[int, str, str]]:
    """(pid, bound session, wake) of `ledger-watch` processes for this role still bound to a REPLACED
    session (one of `old`, never `current`). Read from /proc, so nothing is guessed from a sidecar."""
    old = [o for o in old if o and (not current or not current.startswith(o) and not o.startswith(current))]
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
        if opt.get("--shoal") != sid or opt.get("--slug") != slug:
            continue
        wake = opt.get("--wake", "")
        bound = (wake.split(":", 1)[1] if wake.startswith(("send:", "exec:")) else opt.get("--session", ""))
        if bound and any(bound.startswith(o) or o.startswith(bound) for o in old):
            out.append((int(d), bound, wake))
    return out


def reap_stale_watchers(spec: ShoalSpec, role: Role, old: list[str]) -> None:
    """After a role is relaunched, a watcher still bound to its replaced session keeps delivering:
    an opencode watcher (`--wake send:<old session>`) sends every wake to the abandoned session as
    well (measured 2026-10-08 on the test shoal: the first launch's watcher outlived the restart).
    Those are stopped: a detached opencode watcher wakes nobody by dying. A Claude watcher bound to
    an old session is only reported, because killing a background command wakes its session."""
    import signal
    for pid, bound, wake in stale_watchers(spec.sid, role.slug, old, role.session):
        if wake.startswith(("send:", "exec:")):
            try:
                os.kill(pid, signal.SIGTERM)
                print(f"  {role.slug}: stopped stale ledger-watch pid {pid} (still sending to replaced session {bound})")
            except OSError as exc:
                print(f"  {role.slug}: could not stop stale ledger-watch pid {pid}: {exc}")
        else:
            print(f"  WARN {role.slug}: ledger-watch pid {pid} is still bound to replaced session {bound}; left "
                  f"running (stopping it would wake that session). `claude stop {bound[:8]}` retires both")


def verify_consumer(spec: ShoalSpec, role: Role, cap_s: int = 600, old: list[str] | None = None,
                    since_utc: str = "", poll_s: float = 30) -> bool:
    """Effect-verification for a launched role: it writes a ledger state, its
    ledger-watch sidecar appears (armed after the launch, not bound to a replaced
    session), or its heartbeat ticks within the cap."""
    sidecar = _ledger_watch.sidecar_path(spec.sid, role.slug)
    hb = hb_path(spec.sid, role.slug)
    old = list(old or [])
    t0 = time.time()
    stale_seen = ""
    while time.time() - t0 < cap_s:
        if is_wake_relay(role):  # the relay has no ledger-watch: its effect is a live relay-watch
            from health import relay_alive
            if relay_alive(spec.sid):
                print(f"RELAY LOOP ALIVE: {role.slug} relay-watch running")
                return True
            time.sleep(poll_s)
            continue
        try:
            from ledger import Ledger, View
            led = Ledger(spec.sid, root=spec.ledger_dir)
            led.pull()
            st = View(led, spec).states.get(role.slug)
            if st and st["ts"] >= (spec.current_generation() or {}).get("started_utc", ""):
                print(f"ROLE LOOP ALIVE: {role.slug} wrote ledger state {st.get('state')} at {st['ts']}")
                return True
        except Exception:  # noqa: BLE001
            pass
        if os.path.exists(sidecar):
            try:
                mtime = datetime.datetime.fromtimestamp(os.path.getmtime(sidecar), datetime.timezone.utc)
                mtime_utc = mtime.strftime("%Y-%m-%dT%H:%M:%SZ")
            except OSError:
                mtime_utc = ""
            alive, adopt, note = sidecar_verdict(_ledger_watch.load_sidecar(sidecar), role.session, old,
                                                 since_utc, mtime_utc)
            if alive:
                print(f"CONSUMER LOOP ALIVE: {role.slug} sidecar {note}")
                if adopt and adopt != role.session:
                    print(f"  note: adopting the sidecar's full id {adopt} for launched id {role.session or '(unknown)'}")
                    role.session = adopt
                return True
            if note != stale_seen:
                print(f"  {role.slug}: ignoring sidecar ({note}); waiting for the new session's watcher")
                stale_seen = note
        a = age_min(hb)
        if a is not None and a * 60 < time.time() - t0 + 60:
            print(f"CONSUMER LOOP ALIVE: {role.slug} heartbeat ticked ({a:.0f}m ago)")
            return True
        time.sleep(poll_s)
    print(f"CONSUMER LOOP INCOMPLETE: {role.slug} showed no sidecar/heartbeat in {cap_s // 60} min "
          "— check `claude logs <id>`; grants ask may be pending in-session")
    return False


def stage_restart(spec: ShoalSpec, old_sessions: list[str], launched: dict[str, str],
                  defer_consumers: bool, probe_claude: bool,
                  dry_run: bool, verify_consumers: bool = True,
                  only: list[str] | None = None, created: list[str] | None = None) -> int:
    """Create sessions for roles in `only` (default: all roles). Untouched roles
    keep their live session ids AND seqs; restarted roles bump seq by default
    (explicit --seq wins) so {slug}-r{seq} tags never collide with the retired.
    Every slug whose session was actually created is appended to `created`, so the
    caller can persist launched ids whatever the rc."""
    created = created if created is not None else []
    targets = [r for r in spec.roles if only is None or r.slug in only]
    remote_t = [r for r in targets if not is_local(r, spec)]
    if remote_t:
        # a session is created where this runs: a remote role is recorded (start, add-role) and launched on
        # its own host, never here (multi-host)
        targets = [r for r in targets if r not in remote_t]
        by_host: dict[str, list] = {}
        for r in remote_t:
            by_host.setdefault(host_of(r, spec), []).append(r)
        for h, rs in sorted(by_host.items()):
            print(f"remote role(s) {', '.join(r.slug for r in rs)} on {h}: not launched here; launch them there: "
                  f"ssh {h} shoal restart --spec <its ledger clone>/shoals/{spec.sid}/shoal.json "
                  f"--only {','.join(r.slug for r in rs)}")
    if only is not None:
        unknown = [s for s in only if spec.role(s) is None]
        if unknown:
            print(f"unknown --only slugs: {unknown} (spec has {[r.slug for r in spec.roles]})")
            return 1
        kept = [r.slug for r in spec.roles if r.slug not in only]
        if kept:
            print(f"partial restart: relaunching {sorted(only)}; keeping {kept}")
    print("=== ledger mode, flattened: every role gets the ledger brief + ledger-watch; "
          "loop test = ledger state/sidecar by effect")
    if not dry_run:
        try:
            from ledger import Ledger, ensure_clone
            ensure_clone(spec.ledger_dir, spec.ledger.get("repo", ""), quiet=True)
            led = Ledger(spec.sid, root=spec.ledger_dir)
            if not led.exists():
                led.pull()
                led.init(spec.snapshot(), note=f"init at {'start' if spec.incarnation == 0 else 'restart'}")
        except Exception as exc:  # noqa: BLE001
            print(f"FAIL: ledger not reachable: {exc}")
            return 4
    print("=== 2. roles")
    consumers_launched: list[Role] = []
    launch_utc = utcnow_iso()
    launch_fail = 0
    for role in spec.roles:
        if role not in targets:
            continue
        be = get_backend(role.ecosystem)
        brief_path = os.path.join(home_dir(spec.sid), "briefs",
                                  f"{role.tag}.md")
        if not dry_run:
            brief_path = write_brief(spec, role)
        tag = f"{role.slug} ({role.title}-r{role.seq}, {role.ecosystem})"
        if role.ecosystem == "opencode":
            if dry_run:
                print(f"[dry-run] create + brief lane {tag}")
                role.session = f"ses_DRYRUN_{role.slug}"
            else:
                role.session = be.create(f"{role.title}-r{role.seq}", spec.dir)
                created.append(role.slug)
                print(f"lane {tag}: {role.session}")
                be.send(role.session, spec.dir, brief_for(role, spec),
                        model=role.model)
        elif role.ecosystem in ("muse", "codex"):
            # one `<harness> exec` turn per wake; shoal starts the role's persistent watcher itself, so the
            # role never needs permission to run a detached process
            hmod = _muse if role.ecosystem == "muse" else _codex
            text = brief_for(role, spec)
            print(f"{role.ecosystem} {tag}: brief -> {brief_path}")
            if role.slug in launched:
                role.session = launched[role.slug]
                print(f"  recorded live as {role.session} (--keep/--consumer-launched)")
            elif defer_consumers:
                print("  DEFERRED (--defer-consumers): launch by hand when ready.")
            elif dry_run:
                print(f"  [dry-run] would start the first {role.ecosystem} exec turn (session id "
                      f"{'minted here' if role.ecosystem == 'muse' else 'read from its first event'}) and arm "
                      f"`ledger-watch --wake exec:<session>`")
                role.session = f"{'mu' if role.ecosystem == 'muse' else 'cx'}_DRYRUN_{role.slug}"
            else:
                try:
                    role.session = be.create(f"{role.title}-r{role.seq}", spec.dir, brief=text, name=role.tag,
                                             model=role.model, effort=role.effort)
                except (UnsupportedError, RuntimeError) as exc:
                    print(f"  FAIL: {role.ecosystem} launch failed: {exc}")
                    launch_fail = 4
                    break
                created.append(role.slug)
                print(f"  launched: {role.session} (backend: {role.ecosystem}); watcher {hmod.arm_watcher(spec, role)}")
                consumers_launched.append(role)
        else:
            text = brief_for(role, spec)
            print(f"consumer {tag}: brief -> {brief_path}")
            if role.slug in launched:
                role.session = launched[role.slug]
                print(f"  recorded live as {role.session} (--keep/--consumer-launched)")
            elif defer_consumers:
                print("  DEFERRED (--defer-consumers): launch by hand when ready.")
            elif dry_run:
                print(f"  [dry-run] would auto-launch via {be.ecosystem} backend "
                      f"(claude --bg -n {role.tag}) and verify sidecar/heartbeat")
                role.session = f"cc_DRYRUN_{role.slug}"
            else:
                try:
                    role.session = be.create(
                        f"{role.title}-r{role.seq}", spec.dir,
                        brief=text, name=role.tag,
                        model=role.model, effort=role.effort)
                except (UnsupportedError, RuntimeError) as exc:
                    print(f"  FAIL: auto-launch failed: {exc}")
                    launch_fail = 4
                    break
                created.append(role.slug)
                print(f"  launched: {role.session} (backend: {be.ecosystem})")
                consumers_launched.append(role)
    if launch_fail and not created:
        return launch_fail
    if not dry_run:
        spec.record_generation(run_pin())
        shoal_emit(spec.sid, "shoal", "script", "generation",
                 event="started", gen=spec.display_gen,
                 incarnation=str(spec.incarnation),
                 note=",".join(f"{r.slug}={r.session}" for r in spec.roles if r.session))
        try:
            from ledger import Ledger
            led = Ledger(spec.sid, root=spec.ledger_dir)
            led.pull()
            led.append(SYSTEM_WRITER, "system", event="generation-started", gen=spec.display_gen,
                       note=",".join(f"{r.slug}={r.session}" for r in spec.roles if r.session), _push=False)
            led.snapshot_spec(spec.snapshot())
        except Exception as exc:  # noqa: BLE001
            print(f"WARN: ledger generation record failed: {exc}")
    if launch_fail:
        print(f"FAIL: launch stopped after {created}; their ids and the generation record are kept")
        return launch_fail
    rc = 0
    print("=== 3. ledger loop test (every launched role writes a ledger state or arms its watcher)")
    for role in spec.roles:
        if role not in targets or role.session in ("", ) or role.session.startswith(("ses_DRYRUN", "cc_DRYRUN")):
            continue
        if role.ecosystem == "opencode" and role not in consumers_launched:
            consumers_launched.append(role)
    print("=== 5. claude/codex consumers")
    print(f"  sockets (no turn): {ClaudeBackend.probe_sockets()}")
    if probe_claude and not dry_run:
        print("  probe turn:", ClaudeBackend.probe_turn()[:200])
    if consumers_launched and verify_consumers and not dry_run:
        print("  effect-verification (ledger state / watcher sidecar / heartbeat within 10 min):")
        for role in consumers_launched:
            if not verify_consumer(spec, role, old=old_sessions, since_utc=launch_utc):
                rc = rc or 3
            reap_stale_watchers(spec, role, old_sessions)
        spec.record_generation()
    print("DONE. Old generation retires by neglect; live session ids + generation record saved to the spec.")
    return rc


# ---- start ----------------------------------------------------------------

def mint_shoal_id(name: str) -> str:
    """<name>-YYYYMMDD-HHMMSS in the LOCAL timezone of creation (readme §1)."""
    return f"{name}-{datetime.datetime.now().astimezone().strftime('%Y%m%d-%H%M%S')}"


def cmd_start(args: argparse.Namespace) -> int:
    sid = mint_shoal_id(args.name)
    home = home_dir(sid)
    roles = [parse_role(r) for r in args.role]
    if getattr(args, "aux", ""):  # the residual lane (architecture §4.1)
        eco, model = _aux.parse_aux_arg(args.aux)
        roles.append(_aux.aux_role(sid, eco, model))
        if not args.dry_run:
            _aux.write_doc(sid)
    from sources import parse_source
    try:
        sources = [parse_source(x) for x in (args.source or [])]
    except ValueError as exc:
        print(exc)
        return 1
    from roles import DEFAULT_LEDGER
    ledger = {"repo": args.ledger or DEFAULT_LEDGER["repo"],
              "dir": args.ledger_dir or DEFAULT_LEDGER["dir"]}
    if not ledger["repo"] and not os.path.isdir(os.path.join(ledger["dir"], ".git")):
        print(f"start: no ledger repo and no clone at {ledger['dir']}: pass --ledger-repo URL (or set "
              "`ledger_repo` in the host config / SHOAL_LEDGER_REPO)")
        return 1
    spec = ShoalSpec(sid=sid, dir=args.dir, incarnation=0, roles=roles,
                   repos=list(args.repo or []), ledger=ledger, sources=sources,
                   home=getattr(args, "home", "") or "")
    if spec.home and spec.home != config.server():
        print(f"REFUSED: --home {spec.home} is not this host ({config.server()}): start the shoal on its home host")
        return 1
    errs = spec.validate()
    if errs:
        print("spec invalid:", *errs, sep="\n  ")
        return 1
    from layout import GROUPS_DIR, SPEC_FILE
    spec_path = os.path.join(spec.ledger_dir, GROUPS_DIR, sid, SPEC_FILE)
    if args.dry_run:
        print(f"[dry-run] new shoal {sid} (home: {home}; canonical spec: {spec_path})")
        print(f"[dry-run] spec: {spec.to_dict()}")
        print(f"[dry-run] ledger: {spec.ledger['repo']} at {spec.ledger_dir}/{GROUPS_DIR}/{sid}/ledger/ "
              f"(init + spec snapshot); sources: {spec.sources or 'none'}; "
              f"fill: {[(r.slug, r.fill) for r in roles]}")
        rc = stage_handoffs(spec, True) or stage_sync_plans(spec, True)
        rc = stage_restart(spec, [], {}, args.defer_consumers, False, True) or rc
        print("[dry-run] would save spec with live ids, then: install-systemd"
              + (" --enable" if args.enable_watchers else " (run with --enable when ready)"))
        return rc
    os.makedirs(os.path.join(home, "briefs"), exist_ok=True)
    os.makedirs(os.path.join(home, "reports"), exist_ok=True)
    os.makedirs(os.path.join(home, "telemetry"), exist_ok=True)
    os.makedirs(state_dir(sid), exist_ok=True)
    from ledger import Ledger, ensure_clone
    try:
        ensure_clone(spec.ledger_dir, spec.ledger["repo"], quiet=True)
        led = Ledger(sid, root=spec.ledger_dir)
        led.pull()
        persist_spec(spec, spec_path, note=f"start {sid}")
        led.init(None, note=f"start {sid}")
        print(f"ledger initialized: {led.ldir}")
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL: ledger init: {exc}")
        return 4
    print(f"new shoal {sid} (home: {home}; spec: {spec_path}; telemetry: {os.path.join(home, 'telemetry')})")
    args.spec = spec_path
    args.sid = sid
    args.stages = "handoffs,restart"
    args.save_spec = spec_path
    args.old_session = []
    rc = cmd_restart(args)
    if rc in (0, 3) and args.enable_watchers:
        print("=== 6. watchers (systemd user units)")
        spec = ShoalSpec.load(spec_path)
        cmd_install_systemd(spec, spec_path, dry_run=False, enable=True)
    return rc


def cmd_restart(args: argparse.Namespace) -> int:
    return run_restart(load_spec(args), args)


def run_restart(spec: ShoalSpec, args: argparse.Namespace) -> int:
    if spec.retired_utc and not getattr(args, "force", False):
        print(f"spec is retired ({spec.retired_utc}); start a fresh shoal or pass --force")
        return 1
    errs = spec.validate()
    if errs:
        print("spec invalid:", *errs, sep="\n  ")
        return 1
    stages = args.stages or ",".join(ALL_STAGES)
    dry = args.dry_run
    launched = dict(kv.split(":", 1) for kv in (getattr(args, "consumer_launched", None) or []))
    launched.update(dict(kv.split(":", 1) for kv in (getattr(args, "keep", None) or [])))
    seq_over = {}
    for kv in (getattr(args, "seq", None) or []):
        try:
            slug, num = kv.split(":", 1)
            seq_over[slug] = int(num)
        except ValueError:
            print(f"bad --seq {kv!r} (want SLUG:N)")
            return 1
    for slug, num in seq_over.items():
        r = spec.role(slug)
        if r is None:
            print(f"unknown --seq slug: {slug}")
            return 1
        r.seq = num
    for kv in (getattr(args, "set_model", None) or []):
        try:
            slug, model = kv.split(":", 1)
        except ValueError:
            print(f"bad --set-model {kv!r} (want SLUG:MODEL)")
            return 1
        r = spec.role(slug)
        if r is None:
            print(f"unknown --set-model slug: {slug}")
            return 1
        if r.model != model:
            print(f"{slug}: recorded model {r.model or '(unset)'} -> {model}")
            r.model = model
    only = None
    if getattr(args, "only", ""):
        only = [s.strip() for s in args.only.split(",") if s.strip()]
    for slug, sid in launched.items():
        r = spec.role(slug)
        if r is None:
            print(f"unknown --keep slug: {slug}")
            return 1
        r.session = sid
        if only is not None and slug not in only:
            only = only + [slug]
    if only is None and any(r.parked and not r.session for r in spec.roles):
        # a full restart relaunches every role; a parked one stays parked until it is named (unpark)
        parked = [r.slug for r in spec.roles if r.parked and not r.session]
        only = [r.slug for r in spec.roles if r.slug not in parked]
        print(f"parked role(s) {', '.join(parked)} stay parked: name them with --only (or `shoal unpark`) to relaunch")
    if only is None and any(not is_local(r, spec) for r in spec.roles):
        # a full restart would clear every other role's session id and retire them from here: on a multi-host
        # shoal it is a restart of THIS host's roles
        only = [r.slug for r in spec.roles if is_local(r, spec)]
        print(f"multi-host shoal: a full restart here restarts this host's roles only: {', '.join(only) or '(none)'}")
    if want(stages, "restart") and not getattr(args, "adding", False):
        remote = [r for r in spec.roles if (only is None or r.slug in only) and not is_local(r, spec)]
        if remote:
            # a session is launched where the restart runs: a laptop role restarted from the server
            # would run on the server (multi-host). Refuse before anything is mutated.
            here = [r.slug for r in spec.roles if (only is None or r.slug in only) and is_local(r, spec)]
            print(f"REFUSED: {', '.join(r.slug for r in remote)} run(s) on another host; restart each role on its "
                  f"own host:")
            if here:
                print(f"  here:   {config.cli()} restart --spec {args.spec} --only {','.join(here)}")
            by_host: dict[str, list] = {}
            for r in remote:
                by_host.setdefault(host_of(r, spec), []).append(r)
            for h, rs in sorted(by_host.items()):
                print(f"  {h}: ssh {h} shoal restart --spec <its ledger clone>/shoals/{spec.sid}/shoal.json "
                      f"--only {','.join(r.slug for r in rs)}")
            return 1
    old = list(args.old_session or [])
    if getattr(args, "spec", ""):
        old += old_sessions_for_roles(args.spec, only)
    if dry and want(stages, "restart"):
        import park as _pk
        for r in spec.roles:
            w = _pk.watchers_of(spec.sid, r.slug) if (only is None or r.slug in only) and is_local(r, spec) else []
            if w:
                print(f"[dry-run] {r.slug}: ledger-watch {w} running now, bound to {r.session[:8] or '(no session)'}; a "
                      f"Claude watcher left on the replaced session is only reported after the relaunch (stop that "
                      f"session to retire both)")
    if want(stages, "restart") and not dry:
        # every session-creating restart opens a new incarnation (full or
        # partial): close the open record, bump, clear replaced sessions.
        prev = ShoalSpec.load(args.spec)
        if only is None:
            prev.close_generation()
            spec.generations = prev.generations
            spec.incarnation = prev.incarnation + 1
            for r in spec.roles:
                if r.slug not in launched:
                    r.session = ""
                elif r.slug not in seq_over:
                    rr = prev.role(r.slug)
                    if rr is not None:
                        r.seq = rr.seq
        else:
            prev.close_generation()
            spec.generations = prev.generations
            spec.incarnation = prev.incarnation + 1
            for r in spec.roles:
                if r.slug in only and r.slug not in launched and r.slug not in seq_over:
                    had = prev.role(r.slug)
                    if had is not None and had.session:
                        r.seq += 1
                if r.slug in only and r.slug not in launched:
                    r.session = ""
                if r.slug in only:
                    r.parked = {}  # relaunched: no longer parked
    rc = 0
    if want(stages, "handoffs"):
        rc = stage_handoffs(spec, dry, only) or rc
    if want(stages, "sync-plans"):
        rc = stage_sync_plans(spec, dry, only) or rc
    if want(stages, "retire"):
        rc = stage_retire(spec, old, dry) or rc
    created: list[str] = []
    if want(stages, "restart"):
        rc = stage_restart(spec, old, launched,
                           args.defer_consumers, args.probe_claude, dry,
                           verify_consumers=not getattr(args, "no_verify", False),
                           only=only, created=created) or rc
    else:
        print(f"DONE (stages: {stages}).")
    save_to = getattr(args, "save_spec", "") or (args.spec if want(stages, "restart") else "")
    # launched ids must never be lost: once a session exists, the spec is saved whatever the rc
    if save_to and not dry and (rc in (0, 3) or created):
        persist_spec(spec, save_to)
        print(f"spec with live session ids + generation record saved to {save_to}")
        if rc not in (0, 3):
            print(f"FAIL rc={rc}: see above; the spec was saved anyway because sessions were launched "
                  f"({', '.join(f'{s}={spec.role(s).session}' for s in created)})")
    return rc


def old_sessions_for_roles(path: str, only: list[str] | None) -> list[str]:
    """Sessions the restart stage will replace (retire-stage verification set)."""
    try:
        old = ShoalSpec.load(path)
    except Exception:
        return []
    return [r.session for r in old.roles
            if r.session and (only is None or r.slug in only)]


def old_sessions_from_spec(path: str, gen: str) -> list[str]:
    try:
        old = ShoalSpec.load(path)
    except Exception:
        return []
    if old.display_gen == gen:
        return []
    return [r.session for r in old.roles if r.session]


def cmd_add_role(a: argparse.Namespace) -> int:
    """Extend a live shoal with new roles (no restart of existing sessions).

    New slugs are gated (handoffs + sync-plans over the whole spec, so the new
    docs must pass), then launched exactly like a partial restart with
    --only=new-slugs. Incarnation bumps; untouched roles keep sessions+seqs;
    the ledger spec snapshot refreshes inside stage_restart.
    """
    spec = ShoalSpec.load(a.spec)
    if spec.retired_utc and not getattr(a, "force", False):
        print(f"spec is retired ({spec.retired_utc}); pass --force")
        return 1
    new_roles = [parse_role(r) for r in (a.role or [])]
    if getattr(a, "aux", ""):  # the residual lane (architecture §4.1); aux-launch avoids the bump
        eco, model = _aux.parse_aux_arg(a.aux)
        new_roles.append(_aux.aux_role(spec.sid, eco, model))
        if not getattr(a, "dry_run", False):
            _aux.write_doc(spec.sid)
    if not new_roles:
        print("add-role needs at least one --role")
        return 1
    dupes = [r.slug for r in new_roles if spec.role(r.slug) is not None]
    if dupes:
        print(f"slugs already in spec (use restart --only to relaunch): {dupes}")
        return 1
    spec.roles.extend(new_roles)
    errs = spec.validate()
    if errs:
        print("spec invalid:", *errs, sep="\n  ")
        return 1
    a.only = ",".join(r.slug for r in new_roles)
    a.adding = True  # a new remote role is recorded here and launched on its host (stage_restart skips it)
    a.stages = getattr(a, "stages", "") or "handoffs,sync-plans,restart"
    a.save_spec = a.spec
    for attr, default in (("old_session", []), ("consumer_launched", []),
                          ("keep", []), ("seq", []), ("defer_consumers", False),
                          ("probe_claude", False), ("no_verify", False)):
        if not hasattr(a, attr):
            setattr(a, attr, default)
    return run_restart(spec, a)


def cmd_retire(a: argparse.Namespace) -> int:
    spec = ShoalSpec.load(a.spec)
    g = spec.close_generation()
    if a.dry_run:
        print(f"[dry-run] close generation {spec.display_gen} ({'open' if g else 'already closed/absent'}), "
              f"set retired_utc, {'uninstall units' if a.uninstall_units else 'keep units'}; "
              f"sessions untouched: {[r.session for r in spec.roles if r.session]}")
        return 0
    spec.retired_utc = utcnow_iso()
    persist_spec(spec, a.spec, note="retire")
    shoal_emit(spec.sid, "shoal", "script", "generation", event="retired", gen=spec.display_gen,
             incarnation=str(spec.incarnation), note=a.note or "")
    print(f"retired {spec.sid} gen {spec.display_gen} at {spec.retired_utc} (sessions untouched; retire-by-neglect)")
    if a.uninstall_units:
        cmd_uninstall_systemd(spec.sid)
    print("next: `shoal report --spec <spec> --gen <gen>` writes the incarnation report; "
          "review it in shoal_system_reviews.md before the next protocol change.")
    return 0


# ---- status ---------------------------------------------------------------

def cmd_watch_register(a: argparse.Namespace) -> int:
    from heartbeat import register_watch
    print(register_watch(a.sid, a.slug, a.kind, a.target, a.pid,
                         a.session, a.rescan_min))
    return 0


def cmd_sync_models(a: argparse.Namespace) -> int:
    """Reconcile recorded role models against live sessions.

    opencode roles: read from `session list --json`; drift updates the spec
    (relaunch inherits the record, so a drifted record relaunches wrong).
    claude/codex roles: model unexposed by the backend — reported, kept;
    correct with --set-model on restart/add-role or by editing the spec.
    """
    spec = ShoalSpec.load(a.spec)
    changed = False
    for r in spec.roles:
        if not r.session:
            print(f"{r.slug}: no live session, kept {r.model or '(unset)'}")
            continue
        try:
            live = get_backend(r.ecosystem).live_model(r.session, spec.dir)
        except Exception as exc:  # noqa: BLE001
            print(f"{r.slug}: query failed ({exc}), kept {r.model or '(unset)'}")
            continue
        if not live:
            print(f"{r.slug}: backend does not expose model, kept {r.model or '(unset)'}")
        elif live != (r.model or ""):
            print(f"{r.slug}: {r.model or '(unset)'} -> {live}")
            if not a.dry_run:
                r.model = live
                changed = True
        else:
            print(f"{r.slug}: {live} (matches)")
    if changed and not a.dry_run:
        persist_spec(spec, a.spec, note="sync-models")
        print(f"spec models saved to {a.spec}")
    return 0


def cmd_status(spec: ShoalSpec) -> int:
    print(f"shoal {spec.sid} incarnation={spec.incarnation} ({spec.display_gen}) dir={spec.dir} "
          "mode=flattened"
          + (f" RETIRED {spec.retired_utc}" if spec.retired_utc else ""))
    if not spec.generations:
        print("  note: no generation history — snapshot copy? canonical spec lives at "
              "the ledger clone's shoals/<id>/shoal.json")
    g = spec.current_generation()
    if g:
        print(f"  generation {g.get('gen')}: started {g.get('started_utc')} ended {g.get('ended_utc') or '(open)'}")
    for r in spec.roles:
        parts = [f"{r.slug} [{r.ecosystem}]"]
        parts.append(f"tag={r.tag} session={r.session or '(none)'}")
        if r.session and not r.session.startswith(("ses_DRYRUN", "cc_DRYRUN")):
            try:
                parts.append(f"state: {get_backend(r.ecosystem).status(r.session, spec.dir)}")
            except Exception as exc:  # noqa: BLE001
                parts.append(f"state: QUERY-FAILED ({exc})")
        age = age_min(hb_path(spec.sid, r.slug))
        parts.append(f"hb={'never' if age is None else f'{age:.0f}m ago'}")
        ws, w = _state.wait_status(spec.sid, r.slug)
        if ws != "none":
            parts.append(f"wait={ws}:{w.get('sub')} holder={w.get('holder')} rescan={w.get('rescan_utc')}")
        parts.append(f"doc={'ok' if r.doc and os.path.exists(r.doc) else 'MISSING'}")
        print("  " + " | ".join(parts))
    try:
        from ledger import Ledger, View, unpushed
        led = Ledger(spec.sid, root=spec.ledger_dir)
        v = View(led, spec)
        print(f"  ledger: {led.ldir} events={len(led.events())} contested={len(v.contested)} "
              f"violations={len(v.violations)} unpushed={unpushed(led.root)} "
              f"(`shoal show --shoal {spec.sid}` for the table)")
        for r in spec.roles:
            sc = _ledger_watch.load_sidecar(_ledger_watch.sidecar_path(spec.sid, r.slug))
            if sc:
                pid = sc.get("pid", "")
                alive = pid and os.path.exists(f"/proc/{pid}")
                print(f"  ledger-watch {r.slug}: session={sc.get('session_id', '?')[:12]} pid={pid} "
                      f"{'alive' if alive else 'DEAD'} last_tick={sc.get('last_tick', 'never')} "
                      f"last_wake={sc.get('last_wake', 'never')}")
            else:
                print(f"  ledger-watch {r.slug}: not armed (no sidecar)")
    except Exception as exc:  # noqa: BLE001
        print(f"  ledger: QUERY-FAILED ({exc})")
    ticks = []
    for job, interval in _doctor.tick_jobs(spec).items():
        a = age_min(os.path.join(state_dir(spec.sid), f"tick-{job}"))
        ticks.append(f"{job}={'never' if a is None else f'{a:.0f}m'}"
                     + ("!" if a is not None and a > 3 * interval else ""))
    print("  watcher ticks: " + " ".join(ticks) + "  (! = older than 3x interval)")
    timers = _doctor.timer_state(spec.sid)
    print(f"  systemd timers: {timers or 'none installed'}")
    return 0


# ---- CLI ------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="shoal",
                                 description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    _doctor.add_parser(sub)

    p = sub.add_parser("start", help="mint a new shoal and start its first generation")
    p.add_argument("--name", required=True)
    p.add_argument("--home", default="", help="multi-host: this host's `server` name, recorded as the shoal's home so "
                                               "every other host treats its roles (server empty) as remote")
    p.add_argument("--aux", default="", metavar="ECOSYSTEM[:MODEL]",
                   help="also start the shoal-aux residual lane (needs features.matcher)")
    p.add_argument("--dir", required=True)
    p.add_argument("--role", action="append", default=[],
                   help="SLUG,ECO,TITLE,DOC[,aux][,fill=self|assigned|none]"
                        "[,scope=repo|repo][,server=H][,model=M][,effort=E][,seq=N] (repeatable)")
    p.add_argument("--repo", action="append", default=[],
                   help="GitHub repo owner/name the shoal works (reconcile ground truth; repeatable)")
    p.add_argument("--source", action="append", default=[],
                   help="priority table for self-fill: github-project:OWNER/NUMBER[:repo|repo] (repeatable)")
    p.add_argument("--ledger-repo", "--ledger", dest="ledger", default="",
                   help="ledger repo url (default: config `ledger_repo`; required when that is empty)")
    p.add_argument("--ledger-dir", default="", help="local clone of the ledger repo (default: config `ledger_dir`)")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--consumer-launched", action="append", default=[])
    p.add_argument("--keep", action="append", default=[],
                   help="SLUG:ID to adopt an already-live session (any ecosystem; repeatable)")
    p.add_argument("--seq", action="append", default=[],
                   help="SLUG:N to pin a role's session sequence explicitly (repeatable)")
    p.add_argument("--defer-consumers", action="store_true")
    p.add_argument("--no-verify", action="store_true", help="skip consumer effect-verification poll")
    p.add_argument("--probe-claude", action="store_true")
    p.add_argument("--enable-watchers", action="store_true",
                   help="after a live start: install + enable the systemd watcher units")
    p.set_defaults(func=lambda a: cmd_start(a))

    p = sub.add_parser("restart", help="staged restart (full or --only subset)")
    p.add_argument("--spec", required=True, help="shoal.json path")
    p.add_argument("--only", default="",
                   help="comma slugs to restart (default: all). Untouched roles keep "
                        "sessions+seqs; restarted roles bump seq; incarnation bumps either way")
    p.add_argument("--keep", action="append", default=[],
                   help="SLUG:ID to adopt an already-live session (any ecosystem; repeatable). "
                        "Kept roles join --only implicitly")
    p.add_argument("--seq", action="append", default=[],
                   help="SLUG:N to pin a role's session sequence explicitly (repeatable)")
    p.add_argument("--set-model", action="append", default=[],
                   help="SLUG:MODEL to correct a role's recorded model (repeatable; "
                        "adoptions and relaunches inherit the record)")
    p.add_argument("--stages", default=",".join(ALL_STAGES))
    p.add_argument("--old-session", action="append", default=[])
    p.add_argument("--consumer-launched", action="append", default=[],
                   help="legacy alias of --keep (SLUG:ID, any ecosystem)")
    p.add_argument("--defer-consumers", action="store_true",
                   help="write consumer briefs only; launch by hand later")
    p.add_argument("--no-verify", action="store_true")
    p.add_argument("--probe-claude", action="store_true")
    p.add_argument("--save-spec", default="", help="(default: the --spec path itself)")
    p.add_argument("--force", action="store_true", help="restart a retired spec anyway")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=lambda a: cmd_restart(a))

    p = sub.add_parser("add-role", help="extend a live shoal with new roles (existing sessions untouched)")
    p.add_argument("--spec", required=True, help="shoal.json path")
    p.add_argument("--aux", default="", metavar="ECOSYSTEM[:MODEL]",
                   help="also add the shoal-aux residual lane (needs features.matcher); on a running shoal "
                        "prefer `aux-init` + `aux-launch`, which do not bump the incarnation")
    p.add_argument("--role", action="append", default=[],
                   help="SLUG,ECO,TITLE,DOC[,fill=self|assigned|none][,scope=R][,model=M][,effort=E][,seq=N] (repeatable)")
    p.add_argument("--stages", default="handoffs,sync-plans,restart")
    p.add_argument("--keep", action="append", default=[],
                   help="SLUG:ID to adopt an already-live session instead of launching")
    p.add_argument("--seq", action="append", default=[])
    p.add_argument("--set-model", action="append", default=[],
                   help="SLUG:MODEL to correct a role's recorded model (repeatable)")
    p.add_argument("--defer-consumers", action="store_true")
    p.add_argument("--no-verify", action="store_true")
    p.add_argument("--probe-claude", action="store_true")
    p.add_argument("--force", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=lambda a: cmd_add_role(a))

    p = sub.add_parser("retire", help="close the generation, mark the spec retired")
    p.add_argument("--spec", required=True)
    p.add_argument("--uninstall-units", action="store_true")
    p.add_argument("--note", default="")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_retire)

    _state.add_parser(sub)
    _report.add_parser(sub)
    _ledger_cli.add_parsers(sub)
    _ledger_watch.add_parser(sub)
    _muse.add_parsers(sub)
    _codex.add_parsers(sub)
    _digest.add_parsers(sub)
    _jobs.add_parsers(sub)
    _worker.add_parsers(sub)
    _health.add_parsers(sub)
    _kill_audit.add_parser(sub)
    _aux.add_parsers(sub)
    _upgrade.add_parser(sub)
    _context_cost.add_parser(sub)
    _park.add_parser(sub)
    _startdoc.add_parser(sub)
    _ui_parsers(sub)

    p = sub.add_parser("watch", help="systemd-timer watchers that fire into the ledger")
    config.add_id_arg(p, required=True)
    p.add_argument("mode", choices=("lanes", "pipeline"))
    p.add_argument("args", nargs="*",
                   help="lanes: ledger:<id> <dir> <lane...> [--test] | pipeline: ledger:<id> <dir> [org] [proj]")
    p.set_defaults(func=lambda a: cmd_watch(a))

    p = sub.add_parser("heartbeat", help="stall daemon over hb + wait ticks")
    config.add_id_arg(p, default="")
    p.add_argument("--spec", default="")
    p.add_argument("--threshold-min", type=float, default=60)
    p.add_argument("--refire-min", type=float, default=60)
    p.add_argument("--dir", default="")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--touch", default="",
                   help="session-side tick: touch hb file for SLUG and exit (prefer `transition`)")
    p.set_defaults(func=lambda a: cmd_heartbeat_cli(a))

    p = sub.add_parser("brief", help="print/write one role's first prompt")
    p.add_argument("--spec", required=True)
    p.add_argument("--slug", required=True)
    p.add_argument("--out", default="")
    p.set_defaults(func=lambda a: cmd_brief(a))

    p = sub.add_parser("status", help="restart preconditions + live posture at a glance")
    p.add_argument("--spec", required=True)
    p.set_defaults(func=lambda a: cmd_status(ShoalSpec.load(a.spec)))

    p = sub.add_parser("sync-models", help="reconcile recorded role models against live sessions")
    p.add_argument("--spec", required=True)
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_sync_models)

    p = sub.add_parser("watch-register", help="record one armed per-session monitor for daemon validation")
    config.add_id_arg(p, required=True)
    p.add_argument("--slug", required=True)
    p.add_argument("--kind", required=True, choices=("ci", "ledger", "lease"))
    p.add_argument("--target", required=True, help="what the watch waits on (PR#, build, queue)")
    p.add_argument("--pid", required=True)
    p.add_argument("--session", required=True)
    p.add_argument("--rescan-min", type=float, default=0)
    p.set_defaults(func=lambda a: cmd_watch_register(a))

    p = sub.add_parser("spec", help="spec maintenance (absolutize-docs)")
    ssub = p.add_subparsers(dest="spec_cmd", required=True)
    q = ssub.add_parser("absolutize-docs",
                        help="rewrite every relative role doc to an absolute path under --base")
    q.add_argument("--spec", required=True)
    q.add_argument("--base", required=True, help="directory the relative docs are relative to")
    q.add_argument("--dry-run", action="store_true")
    q.set_defaults(func=cmd_spec_absolutize_docs)

    p = sub.add_parser("install-systemd",
                       help="write (and optionally enable) systemd --user watcher units")
    p.add_argument("--spec", required=True)
    p.add_argument("--enable", action="store_true", help="daemon-reload + enable --now each timer")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_install_cli)

    p = sub.add_parser("uninstall-systemd",
                       help="disable + remove a retired shoal's watcher units")
    config.add_id_arg(p, required=True)
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=lambda a: cmd_uninstall_systemd(a.sid, dry_run=a.dry_run))

    args = ap.parse_args(argv)
    return args.func(args)


def cmd_spec_absolutize_docs(a: argparse.Namespace) -> int:
    """Rewrite every relative `role.doc` to os.path.join(BASE, doc), normalized, and persist the
    spec through the usual path (commit + push when it lives in the ledger clone)."""
    spec = ShoalSpec.load(a.spec)
    base = os.path.abspath(os.path.expanduser(a.base))
    changed = []
    for r in spec.roles:
        if r.doc and not os.path.isabs(r.doc):
            new = os.path.normpath(os.path.join(base, r.doc))
            changed.append((r.slug, r.doc, new))
            r.doc = new
    for slug, old, new in changed:
        print(f"{'[dry-run] ' if a.dry_run else ''}{slug}: {old} -> {new}"
              + ("" if os.path.exists(new) else " (MISSING at the new path)"))
    if not changed:
        print("no relative role docs: nothing to change")
        return 0
    if a.dry_run:
        print(f"[dry-run] would rewrite {len(changed)} role doc(s) in {a.spec}")
        return 0
    persist_spec(spec, a.spec, note=f"absolutize {len(changed)} role doc(s)")
    print(f"rewrote {len(changed)} role doc(s) in {a.spec}")
    return 0


def cmd_install_cli(a: argparse.Namespace) -> int:
    try:
        spec = ShoalSpec.load(a.spec)
    except OSError as exc:
        print(f"cannot read spec {a.spec}: {exc}")
        return 1
    return cmd_install_systemd(spec, os.path.abspath(a.spec),
                               dry_run=a.dry_run, enable=a.enable)


def cmd_watch(a: argparse.Namespace) -> int:
    if a.args and not a.args[0].startswith("ledger:"):
        print(f"watch target must be ledger:<id> (got {a.args[0]!r}); session-addressed fires "
              "were removed 2026-09-24 (flattened-only)")
        return 1
    if a.mode == "lanes":
        if len(a.args) < 3:
            print("usage: shoal watch --shoal ID lanes ledger:<id> <dir> <lane...> [--test]")
            return 1
        test = "--test" in a.args
        lanes = [x for x in a.args[2:] if x != "--test"]
        return cmd_lanes(a.sid, a.args[0], a.args[1], lanes, test=test)
    if len(a.args) < 2:
        print("usage: shoal watch --shoal ID pipeline ledger:<id> <dir> [org] [proj]")
        return 1
    import config
    org = a.args[2] if len(a.args) > 2 else config.azure_org()
    proj = a.args[3] if len(a.args) > 3 else config.azure_project()
    if not org or not proj:
        print("pipeline watch needs an Azure org and project: pass them, or set azure_org and "
              "azure_project in the host config (SHOAL_AZURE_ORG, SHOAL_AZURE_PROJECT)")
        return 1
    return cmd_pipeline(a.sid, a.args[0], a.args[1], org, proj)


def cmd_heartbeat_cli(a: argparse.Namespace) -> int:
    if a.touch:
        print(hb_touch(a.sid, a.touch))
        return 0
    spec = ShoalSpec.load(a.spec) if a.spec else ShoalSpec(sid=a.sid, dir=a.dir)
    return cmd_heartbeat(spec, a.threshold_min,
                         dry_run=a.dry_run, refire_min=a.refire_min)


def cmd_brief(a: argparse.Namespace) -> int:
    spec = ShoalSpec.load(a.spec)
    role = spec.role(a.slug)
    if role is None:
        print(f"no role {a.slug} in {a.spec}")
        return 1
    text = brief_for(role, spec)
    if a.out:
        with open(a.out, "w") as fh:
            fh.write(text + "\n")
        print(f"brief -> {a.out}")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
