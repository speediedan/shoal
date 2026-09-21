#!/usr/bin/env python3
"""hag.py — scripted HAG management (start, generation restart, watchers,
state, health, reports). Stdlib only by design (systemd-safe, no venv).
Runbook §1 has the operator sequence; the readme is the human map.

Subcommands:
  doctor        preflight (host) + live health (with --spec). Read-only.
  start         mint a new HAG (id, dirs, hag.json, queue files), launch its
                first generation, save live ids + generation record back,
                verify loops. Roles: SLUG,ECO,TITLE,DOC[,QUEUE][,coord]
                [,model=M][,effort=E] (DOC = the single plan_state.md;
                grants come from its grants-needed section, never the CLI).
  restart       staged generation restart from --spec hag.json: handoffs,
                sync-plans, retire, restart. Every stage honors --dry-run.
                Non-opencode roles auto-launch (claude --bg) unless
                --consumer-launched SLUG:ID or --defer-consumers.
  retire        close the current generation (ended_utc), mark the spec
                retired, optionally uninstall its watcher units. Deletes no
                session (retire-by-neglect stands).
  transition    THE session-side state primitive: heartbeat + wait proof +
                telemetry in one command (state.py).
  status        one line per role: session state, heartbeat/wait posture,
                doc, queue; plus watcher ticks and timers.
  lint-queues   queue-file linter + commit ownership gate (pre-commit).
  report        per-incarnation report, md (versioned) + html (bulk).
  watch         coordinator-side watchers: queue | lanes | pipeline (timers).
  queue-watch   consumer QUEUE self-watch (background, exits on unseen items).
  heartbeat     stall daemon over /tmp/<hag>/hb-* + wait-* (timer */5).
  brief         print/write one role's first prompt (restart uses this).
  install-systemd / uninstall-systemd   watcher units per HAG generation.

Flattening (proposal 5.k): pass --no-coordinator (or a spec with no
coordinator role) — coordinator creation, registry delivery, and the
coordinator-executed loop test are skipped; lanes get the shared-channel
brief; STALL traffic goes to channel.md. Same machinery, one spec edit.

Claude consumers launch headless (`claude --bg -n <slug-gen>`); sends into
a live claude session are refused (resume would fork a copy) — the queue
file is the only coordinator-to-consumer channel. Every new session carries
its role's grants-needed list and asks the maintainer first-person, all in
one question, before starting work: that ask is the only interactive step.
"""
from __future__ import annotations

import argparse
import datetime
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "telemetry"))

from backends import ClaudeBackend, UnsupportedError, get as get_backend  # noqa: E402
from briefs import brief_for  # noqa: E402
from heartbeat import age_min, cmd_heartbeat, hb_path, touch as hb_touch  # noqa: E402
from queue_section import QueueReadError, read_queue  # noqa: E402
from queue_watch import cmd_queue_watch  # noqa: E402
from roles import (HAG_DATA_ROOT, HAG_ROOT, HagSpec, Role, default_spec_path,  # noqa: E402
                   state_dir, utcnow_iso)
from systemd import cmd_install as cmd_install_systemd  # noqa: E402
from systemd import cmd_uninstall as cmd_uninstall_systemd  # noqa: E402
from watchers import cmd_lanes, cmd_pipeline, cmd_queue, hag_emit  # noqa: E402
import doctor as _doctor  # noqa: E402
import lint as _lint  # noqa: E402
import state as _state  # noqa: E402
import hag_report as _report  # noqa: E402

try:
    from hag_telemetry import run_pin  # noqa: E402
except Exception:  # pragma: no cover
    def run_pin():
        return {}

ALL_STAGES = ("handoffs", "sync-plans", "retire", "restart")


def want(stages: str, name: str) -> bool:
    return name in [s.strip() for s in stages.split(",") if s.strip()]


def parse_role(spec: str) -> Role:
    """SLUG,ECO,TITLE,DOC[,QUEUE][,coord][,model=M][,effort=E][,seq=N]. DOC is the
    single plan_state.md start document. Grants come from the doc's
    grants-needed section at brief time — never from the CLI. seq sets the
    session tag ({slug}-r{seq}); default 1."""
    parts = spec.split(",")
    if len(parts) < 4:
        raise ValueError(f"bad --role spec (want SLUG,ECO,TITLE,DOC[,...]): {spec!r}")
    slug, eco, title, doc = parts[:4]
    queue, coord, model, effort, seq = "", False, "", "", 1
    for tok in parts[4:]:
        if tok == "coord":
            coord = True
        elif tok.startswith("model="):
            model = tok[len("model="):]
        elif tok.startswith("effort="):
            effort = tok[len("effort="):]
        elif tok.startswith("seq="):
            try:
                seq = int(tok[len("seq="):])
            except ValueError:
                raise ValueError(f"bad --role token {tok!r} in {spec!r}")
        elif not queue:
            queue = tok
        else:
            raise ValueError(f"bad --role token {tok!r} in {spec!r}")
    return Role(slug=slug, ecosystem=eco, title=title, doc=doc,
                queue=queue, coordinator=coord, model=model, effort=effort,
                seq=seq)


def load_spec(args: argparse.Namespace) -> HagSpec:
    if not getattr(args, "spec", ""):
        print("restart needs --spec <hag.json> (legacy coord/lane flags removed 2026-09-19)")
        sys.exit(1)
    spec = HagSpec.load(args.spec)
    if getattr(args, "no_coordinator", False):
        for r in spec.roles:
            r.coordinator = False
    return spec


# ---- stages ---------------------------------------------------------------

def stage_handoffs(spec: HagSpec, dry_run: bool) -> int:
    print("=== 0a. start docs (one plan_state.md per role; sessions keep them current)")
    print("each role's doc verifies: presence + plan header:")
    rc = 0
    for role in spec.roles:
        if dry_run:
            print(f"[dry-run] verify {role.doc} (mission, current state, grants-needed, next)")
            continue
        try:
            text = open(role.doc).read()
        except OSError:
            print(f"MISSING: {role.doc} ({role.slug} has no start doc)")
            rc = 5
            continue
        if re.search(r"^- handing-off-model:|^# Handoff|^# .*(live plan|coordination plan|live plan state|plan state)",
                     text, flags=re.M | re.I):
            print(f"ok: {role.doc}")
        else:
            print(f"INCOMPLETE: {role.doc} (no plan header)")
            rc = 5
    return rc


def stage_sync_plans(spec: HagSpec, dry_run: bool) -> int:
    print("=== 0b. start-doc sections (grants-needed + current state present)")
    rc = 0
    for role in spec.roles:
        if dry_run:
            print(f"[dry-run] sections in {role.doc}")
            continue
        try:
            text = open(role.doc).read().lower()
        except OSError:
            print(f"MISSING: {role.doc}")
            rc = 5
            continue
        missing = [s for s in ("grants-needed", "current state")
                   if s not in text]
        if missing:
            print(f"INCOMPLETE: {role.doc} (missing: {', '.join(missing)})")
            rc = 5
        else:
            print(f"ok: {role.doc}")
    return rc


def stage_retire(spec: HagSpec, old_sessions: list[str],
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


def write_brief(spec: HagSpec, role: Role, coord_id: str = "") -> str:
    d = os.path.join(HAG_ROOT, spec.hag, "briefs")
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, f"{role.tag}.md")
    with open(path, "w") as fh:
        fh.write(brief_for(role, spec, coord_id) + "\n")
    return path


def verify_consumer(spec: HagSpec, role: Role, cap_s: int = 600) -> bool:
    """Effect-verification for a launched non-opencode consumer: its sidecar
    appears (bound to a session id) or its heartbeat ticks within the cap."""
    sidecar = os.path.join(state_dir(spec.hag), f"hag-watch-{spec.hag}-{role.slug}.state")
    hb = hb_path(spec.hag, role.slug)
    t0 = time.time()
    while time.time() - t0 < cap_s:
        if os.path.exists(sidecar):
            sid = ""
            for line in open(sidecar):
                if line.startswith("session_id="):
                    sid = line.split("=", 1)[1].strip()
            print(f"CONSUMER LOOP ALIVE: {role.slug} sidecar bound to {sid or '(no id yet)'}")
            if sid and role.session and not (sid.startswith(role.session) or role.session.startswith(sid)):
                print(f"  note: sidecar session {sid} != launched id {role.session} (adopting sidecar id)")
                role.session = sid
            return True
        a = age_min(hb)
        if a is not None and a * 60 < time.time() - t0 + 60:
            print(f"CONSUMER LOOP ALIVE: {role.slug} heartbeat ticked ({a:.0f}m ago)")
            return True
        time.sleep(30)
    print(f"CONSUMER LOOP INCOMPLETE: {role.slug} showed no sidecar/heartbeat in {cap_s // 60} min "
          "— check `claude logs <id>`; grants ask may be pending in-session")
    return False


def stage_restart(spec: HagSpec, old_sessions: list[str], launched: dict[str, str],
                  defer_consumers: bool, probe_claude: bool,
                  dry_run: bool, verify_consumers: bool = True,
                  only: list[str] | None = None) -> int:
    """Create sessions for roles in `only` (default: all roles). Untouched roles
    keep their live session ids AND seqs; restarted roles bump seq by default
    (explicit --seq wins) so {slug}-r{seq} tags never collide with the retired."""
    targets = [r for r in spec.roles if only is None or r.slug in only]
    if only is not None:
        unknown = [s for s in only if spec.role(s) is None]
        if unknown:
            print(f"unknown --only slugs: {unknown} (spec has {[r.slug for r in spec.roles]})")
            return 1
        kept = [r.slug for r in spec.roles if r.slug not in only]
        if kept:
            print(f"partial restart: relaunching {sorted(only)}; keeping {kept}")
    coord = None if spec.flattened else spec.coordinator
    if spec.flattened:
        print("=== flattened mode: no coordinator (registry/loop-test skipped, shared-channel briefs)")
    if coord:
        print(f"=== 1. coordinator: {coord.title}-{spec.display_gen}")
    be_oc = get_backend("opencode")
    coord_id = ""
    if coord:
        if coord not in targets:
            coord_id = coord.session
            if not coord_id:
                print(f"coordinator {coord.slug} not restarted and has no live session: "
                      f"add it to --only or pass --keep {coord.slug}:<id>")
                return 1
            print(f"coordinator kept live: {coord_id}")
        elif dry_run:
            print("[dry-run] create + brief coordinator")
            coord_id = "ses_DRYRUNCOORD"
        else:
            coord_id = be_oc.create(f"{coord.title}-r{coord.seq}", spec.dir)
            coord.session = coord_id
            print(f"coordinator: {coord_id}")
            be_oc.send(coord_id, spec.dir, brief_for(coord, spec), model=coord.model)
    print("=== 2. lanes/roles")
    consumers_launched: list[Role] = []
    for role in spec.roles:
        if role.coordinator or role not in targets:
            continue
        be = get_backend(role.ecosystem)
        brief_path = os.path.join(HAG_ROOT, spec.hag, "briefs",
                                  f"{role.tag}.md")
        if not dry_run:
            brief_path = write_brief(spec, role, coord_id)
        tag = f"{role.slug} ({role.title}-r{role.seq}, {role.ecosystem})"
        if role.ecosystem == "opencode":
            if dry_run:
                print(f"[dry-run] create + brief lane {tag} with coord {coord_id or '(none)'}")
                role.session = f"ses_DRYRUN_{role.slug}"
            else:
                role.session = be.create(f"{role.title}-r{role.seq}", spec.dir)
                print(f"lane {tag}: {role.session}")
                be.send(role.session, spec.dir, brief_for(role, spec, coord_id),
                        model=role.model)
        else:
            text = brief_for(role, spec, coord_id)
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
                        model=role.model)
                except (UnsupportedError, RuntimeError) as exc:
                    print(f"  FAIL: auto-launch failed: {exc}")
                    return 4
                print(f"  launched: {role.session} (backend: {be.ecosystem})")
                consumers_launched.append(role)
    if not dry_run:
        spec.record_generation(run_pin())
        hag_emit(spec.hag, coord_id or "hag.py", "script", "generation",
                 event="started", gen=spec.display_gen,
                 incarnation=str(spec.incarnation),
                 note=",".join(f"{r.slug}={r.session}" for r in spec.roles if r.session))
    rc = 0
    if coord and not spec.flattened:
        print("=== 3. registry + loop test (coordinator-executed)")
        reg = (f"REGISTRY ({spec.display_gen}, direct-relay): "
               + " ".join(f"{r.slug}={r.session};" for r in spec.roles if not r.coordinator))
        prompt = (reg + " LOOP TEST NOW: send LANE-INSTRUCT 1 yourself to each lane "
                        "(status ping), collect REPORTs in-transcript.")
        if dry_run:
            print(f"[dry-run] send registry + loop test to {coord_id}")
        else:
            be_oc.send(coord_id, spec.dir, prompt)
        print("=== 4. verify (10 min cap)")
        nlanes = sum(1 for r in spec.roles
                     if not r.coordinator and r.ecosystem == "opencode")
        if dry_run:
            print(f"[dry-run] poll {coord_id} for {nlanes} ack-1s")
        else:
            ok = False
            for _ in range(20):
                msgs = be_oc.messages(coord_id, spec.dir, limit=20, role="user")
                n = len(re.findall(r"ack-1", msgs))
                if n >= nlanes:
                    print(f"LOOP TEST PASS: {n} ack-1s in coordinator transcript")
                    ok = True
                    break
                time.sleep(30)
            if not ok:
                msgs = be_oc.messages(coord_id, spec.dir, limit=20, role="user")
                n = len(re.findall(r"ack-1", msgs))
                print(f"LOOP TEST INCOMPLETE: {n}/{nlanes} ack-1s — investigate before steady-state")
                rc = 3
    print("=== 5. claude/codex consumers")
    print(f"  sockets (no turn): {ClaudeBackend.probe_sockets()}")
    if probe_claude and not dry_run:
        print("  probe turn:", ClaudeBackend.probe_turn()[:200])
    if consumers_launched and verify_consumers and not dry_run:
        print("  effect-verification (sidecar or heartbeat within 10 min):")
        for role in consumers_launched:
            if not verify_consumer(spec, role):
                rc = rc or 3
        spec.record_generation()
    print("DONE. Old generation retires by neglect; live session ids + generation record saved to the spec.")
    return rc


# ---- start ----------------------------------------------------------------

QUEUE_TEMPLATE = """# QUEUE + STATUS: {slug} (HAG {hag})

Ownership: QUEUE coordinator-only, STATUS consumer-only; one section per commit.
Completion is verified against GitHub, never against STATUS claims.
Lint: `hag.py lint-queues --file <this file>`.

## QUEUE (coordinator-owned; consumer: read-only)

(empty — coordinator seeds top-down with bases; items as `N. **repo#n:verb** (base repo@sha): ...`)

## STATUS (consumer-owned; coordinator: read-only)

- state: idle
- active-issue: none
- waiting-on: queue-empty
- updated-utc: {ts}
- last-done:{last_done}
"""


def adopt_status(old_queue: str, ts: str) -> str:
    """Carry last-done (max 3) + base lines from a retired queue file's STATUS."""
    try:
        text = open(old_queue).read()
        status = text[text.index("## STATUS"):]
    except Exception:
        return ""
    last_done, base = "", ""
    m = re.search(r"^- last-done:(.*)", status, flags=re.M)
    if m:
        last_done = " " + m.group(1).strip()[:600]
    m = re.search(r"^- base:(.*)", status, flags=re.M)
    if m:
        base = "\n- base:" + m.group(1).rstrip()
    return (last_done or " none (fresh HAG)") + base + f"\n- predecessor: {old_queue}"


def mint_hag_id(name: str) -> str:
    """<name>-YYYYMMDD-HHMMSS in the LOCAL timezone of creation (readme §1)."""
    return f"{name}-{datetime.datetime.now().astimezone().strftime('%Y%m%d-%H%M%S')}"


def cmd_start(args: argparse.Namespace) -> int:
    hag = mint_hag_id(args.name)
    hagdir = os.path.join(HAG_ROOT, hag)
    roles = [parse_role(r) for r in args.role]
    if not any(r.coordinator for r in roles) and not args.no_coordinator:
        # default: first opencode role coordinates unless told otherwise
        for r in roles:
            if r.ecosystem == "opencode":
                r.coordinator = True
                break
    if args.no_coordinator:
        for r in roles:
            r.coordinator = False
    spec = HagSpec(hag=hag, dir=args.dir, incarnation=0, roles=roles,
                   repos=list(args.repo or []))
    errs = spec.validate()
    if errs:
        print("spec invalid:", *errs, sep="\n  ")
        return 1
    for r in roles:
        if r.queue or r.coordinator or r.ecosystem == "opencode":
            continue
        r.queue = os.path.join(hagdir, "queues", f"{r.slug}.md")
    if args.dry_run:
        print(f"[dry-run] new HAG {hag} at {hagdir} (data: {os.path.join(HAG_DATA_ROOT, hag)})")
        print(f"[dry-run] spec: {spec.to_dict()}")
        if args.adopt_queue:
            print(f"[dry-run] adopt last-done + base from {args.adopt_queue}")
        rc = stage_handoffs(spec, True) or stage_sync_plans(spec, True)
        rc = stage_restart(spec, [], {}, args.defer_consumers, False, True) or rc
        print("[dry-run] would save spec with live ids, then: install-systemd"
              + (" --enable" if args.enable_watchers else " (run with --enable when ready)"))
        return rc
    os.makedirs(os.path.join(hagdir, "queues"), exist_ok=True)
    os.makedirs(os.path.join(hagdir, "briefs"), exist_ok=True)
    os.makedirs(os.path.join(hagdir, "reports"), exist_ok=True)
    os.makedirs(os.path.join(HAG_DATA_ROOT, hag, "telemetry"), exist_ok=True)
    os.makedirs(os.path.join(HAG_DATA_ROOT, hag, "reports"), exist_ok=True)
    os.makedirs(state_dir(hag), exist_ok=True)
    spec_path = os.path.join(hagdir, "hag.json")
    for r in roles:
        if r.coordinator or not r.queue or os.path.exists(r.queue):
            continue
        ts = utcnow_iso()
        last_done = adopt_status(args.adopt_queue, ts) if args.adopt_queue else " none (fresh HAG)"
        with open(r.queue, "w") as fh:
            fh.write(QUEUE_TEMPLATE.format(slug=r.slug, hag=hag, ts=ts,
                                           last_done=last_done))
    spec.save(spec_path)
    print(f"new HAG {hag} at {hagdir} (spec: {spec_path}; telemetry: {os.path.join(HAG_DATA_ROOT, hag, 'telemetry')})")
    args.spec = spec_path
    args.hag = hag
    args.stages = "handoffs,restart"
    args.save_spec = spec_path
    args.old_session = []
    rc = cmd_restart(args)
    if rc in (0, 3) and args.enable_watchers:
        print("=== 6. watchers (systemd user units)")
        spec = HagSpec.load(spec_path)
        coord_id = spec.coordinator.session if spec.coordinator else ""
        cmd_install_systemd(spec, spec_path, coord_id, dry_run=False, enable=True)
    return rc


def cmd_restart(args: argparse.Namespace) -> int:
    spec = load_spec(args)
    if spec.retired_utc and not getattr(args, "force", False):
        print(f"spec is retired ({spec.retired_utc}); start a fresh HAG or pass --force")
        return 1
    if getattr(args, "no_coordinator", False):
        for r in spec.roles:
            r.coordinator = False
    errs = spec.validate()
    if errs:
        print("spec invalid:", *errs, sep="\n  ")
        return 1
    stages = args.stages or ",".join(ALL_STAGES)
    dry = args.dry_run
    launched = dict(kv.split(":", 1) for kv in (args.consumer_launched or []))
    launched.update(dict(kv.split(":", 1) for kv in (args.keep or [])))
    seq_over = {}
    for kv in (args.seq or []):
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
    old = list(args.old_session or [])
    if getattr(args, "spec", ""):
        old += old_sessions_for_roles(args.spec, only)
    if want(stages, "restart") and not dry:
        # every session-creating restart opens a new incarnation (full or
        # partial): close the open record, bump, clear replaced sessions.
        prev = HagSpec.load(args.spec)
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
                    r.seq += 1
                if r.slug in only and r.slug not in launched:
                    r.session = ""
    rc = 0
    if want(stages, "handoffs"):
        rc = stage_handoffs(spec, dry) or rc
    if want(stages, "sync-plans"):
        rc = stage_sync_plans(spec, dry) or rc
    if want(stages, "retire"):
        rc = stage_retire(spec, old, dry) or rc
    if want(stages, "restart"):
        rc = stage_restart(spec, old, launched,
                           args.defer_consumers, args.probe_claude, dry,
                           verify_consumers=not getattr(args, "no_verify", False),
                           only=only) or rc
    else:
        print(f"DONE (stages: {stages}).")
    save_to = getattr(args, "save_spec", "") or (args.spec if want(stages, "restart") else "")
    if save_to and not dry and rc in (0, 3):
        spec.save(save_to)
        print(f"spec with live session ids + generation record saved to {save_to}")
    return rc


def old_sessions_for_roles(path: str, only: list[str] | None) -> list[str]:
    """Sessions the restart stage will replace (retire-stage verification set)."""
    try:
        old = HagSpec.load(path)
    except Exception:
        return []
    return [r.session for r in old.roles
            if r.session and (only is None or r.slug in only)]


def old_sessions_from_spec(path: str, gen: str) -> list[str]:
    try:
        old = HagSpec.load(path)
    except Exception:
        return []
    if old.display_gen == gen:
        return []
    return [r.session for r in old.roles if r.session]


def cmd_retire(a: argparse.Namespace) -> int:
    spec = HagSpec.load(a.spec)
    g = spec.close_generation()
    if a.dry_run:
        print(f"[dry-run] close generation {spec.display_gen} ({'open' if g else 'already closed/absent'}), "
              f"set retired_utc, {'uninstall units' if a.uninstall_units else 'keep units'}; "
              f"sessions untouched: {[r.session for r in spec.roles if r.session]}")
        return 0
    spec.retired_utc = utcnow_iso()
    spec.save(a.spec)
    hag_emit(spec.hag, "hag.py", "script", "generation", event="retired", gen=spec.display_gen,
             incarnation=str(spec.incarnation), note=a.note or "")
    print(f"retired {spec.hag} gen {spec.display_gen} at {spec.retired_utc} (sessions untouched; retire-by-neglect)")
    if a.uninstall_units:
        cmd_uninstall_systemd(spec.hag)
    print("next: `hag.py report --spec <spec> --gen <gen>` writes the incarnation report; "
          "review it in hag_system_reviews.md before the next protocol change.")
    return 0


# ---- status ---------------------------------------------------------------

def cmd_status(spec: HagSpec) -> int:
    print(f"HAG {spec.hag} incarnation={spec.incarnation} ({spec.display_gen}) dir={spec.dir} "
          f"mode={'flattened' if spec.flattened else 'coordinated'}"
          + (f" RETIRED {spec.retired_utc}" if spec.retired_utc else ""))
    g = spec.current_generation()
    if g:
        print(f"  generation {g.get('gen')}: started {g.get('started_utc')} ended {g.get('ended_utc') or '(open)'}")
    for r in spec.roles:
        parts = [f"{r.slug} [{r.ecosystem}]"]
        if r.coordinator:
            parts.append("COORD")
        parts.append(f"tag={r.tag} session={r.session or '(none)'}")
        if r.session and not r.session.startswith(("ses_DRYRUN", "cc_DRYRUN")):
            try:
                parts.append(f"state: {get_backend(r.ecosystem).status(r.session, spec.dir)}")
            except Exception as exc:  # noqa: BLE001
                parts.append(f"state: QUERY-FAILED ({exc})")
        age = age_min(hb_path(spec.hag, r.slug))
        parts.append(f"hb={'never' if age is None else f'{age:.0f}m ago'}")
        ws, w = _state.wait_status(spec.hag, r.slug)
        if ws != "none":
            parts.append(f"wait={ws}:{w.get('sub')} holder={w.get('holder')} rescan={w.get('rescan_utc')}")
        parts.append(f"doc={'ok' if r.doc and os.path.exists(r.doc) else 'MISSING'}")
        if r.queue and os.path.exists(r.queue):
            try:
                h, ids = read_queue(r.queue)
                parts.append(f"queue={len(ids)} items@{h}")
            except QueueReadError as exc:
                parts.append(f"queue=UNREADABLE ({exc})")
        print("  " + " | ".join(parts))
    ticks = []
    for job, interval in _doctor.TICK_INTERVAL_MIN.items():
        a = age_min(os.path.join(state_dir(spec.hag), f"tick-{job}"))
        ticks.append(f"{job}={'never' if a is None else f'{a:.0f}m'}"
                     + ("!" if a is not None and a > 3 * interval else ""))
    print("  watcher ticks: " + " ".join(ticks) + "  (! = older than 3x interval)")
    timers = _doctor.timer_state(spec.hag)
    print(f"  systemd timers: {timers or 'none installed'}")
    return 0


# ---- CLI ------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="hag.py",
                                 description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    _doctor.add_parser(sub)

    p = sub.add_parser("start", help="mint a new HAG and start its first generation")
    p.add_argument("--name", required=True)
    p.add_argument("--dir", required=True)
    p.add_argument("--role", action="append", default=[],
                   help="SLUG,ECO,TITLE,DOC[,QUEUE][,coord][,model=M][,effort=E][,seq=N] (repeatable)")
    p.add_argument("--repo", action="append", default=[],
                   help="GitHub repo owner/name the HAG works (reconcile ground truth; repeatable)")
    p.add_argument("--adopt-queue", default="",
                   help="retired queue file: carry last-done + base into new STATUS")
    p.add_argument("--no-coordinator", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--consumer-launched", action="append", default=[])
    p.add_argument("--defer-consumers", action="store_true")
    p.add_argument("--no-verify", action="store_true", help="skip consumer effect-verification poll")
    p.add_argument("--probe-claude", action="store_true")
    p.add_argument("--enable-watchers", action="store_true",
                   help="after a live start: install + enable the systemd watcher units")
    p.set_defaults(func=lambda a: cmd_start(a))

    p = sub.add_parser("restart", help="staged restart (full or --only subset)")
    p.add_argument("--spec", required=True, help="hag.json path")
    p.add_argument("--only", default="",
                   help="comma slugs to restart (default: all). Untouched roles keep "
                        "sessions+seqs; restarted roles bump seq; incarnation bumps either way")
    p.add_argument("--keep", action="append", default=[],
                   help="SLUG:ID to adopt an already-live session (any ecosystem; repeatable). "
                        "Kept roles join --only implicitly")
    p.add_argument("--seq", action="append", default=[],
                   help="SLUG:N to pin a role's session sequence explicitly (repeatable)")
    p.add_argument("--stages", default=",".join(ALL_STAGES))
    p.add_argument("--old-session", action="append", default=[])
    p.add_argument("--consumer-launched", action="append", default=[],
                   help="legacy alias of --keep (SLUG:ID, any ecosystem)")
    p.add_argument("--defer-consumers", action="store_true",
                   help="write consumer briefs only; launch by hand later")
    p.add_argument("--no-verify", action="store_true")
    p.add_argument("--probe-claude", action="store_true")
    p.add_argument("--no-coordinator", action="store_true")
    p.add_argument("--save-spec", default="", help="(default: the --spec path itself)")
    p.add_argument("--force", action="store_true", help="restart a retired spec anyway")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=lambda a: cmd_restart(a))

    p = sub.add_parser("retire", help="close the generation, mark the spec retired")
    p.add_argument("--spec", required=True)
    p.add_argument("--uninstall-units", action="store_true")
    p.add_argument("--note", default="")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_retire)

    _state.add_parser(sub)
    _lint.add_parser(sub)
    _report.add_parser(sub)

    p = sub.add_parser("watch", help="coordinator-side watchers (systemd timers)")
    p.add_argument("--hag", required=True)
    p.add_argument("mode", choices=("queue", "lanes", "pipeline"))
    p.add_argument("args", nargs="*",
                   help="queue: <queue-file> <notify> <dir> [test] | lanes: <notify> <dir> <lane...> [--test] | pipeline: <notify> <dir> [org] [proj]")
    p.set_defaults(func=lambda a: cmd_watch(a))

    p = sub.add_parser("queue-watch", help="consumer QUEUE self-watch")
    p.add_argument("--sidecar", required=True)
    p.add_argument("--interval", required=True, type=float)
    p.set_defaults(func=lambda a: cmd_queue_watch(a.sidecar, a.interval))

    p = sub.add_parser("heartbeat", help="stall daemon over hb + wait ticks")
    p.add_argument("--hag", default="")
    p.add_argument("--spec", default="")
    p.add_argument("--threshold-min", type=float, default=60)
    p.add_argument("--refire-min", type=float, default=60)
    p.add_argument("--notify", default="")
    p.add_argument("--dir", default="")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--touch", default="",
                   help="session-side tick: touch hb file for SLUG and exit (prefer `transition`)")
    p.set_defaults(func=lambda a: cmd_heartbeat_cli(a))

    p = sub.add_parser("brief", help="print/write one role's first prompt")
    p.add_argument("--spec", required=True)
    p.add_argument("--slug", required=True)
    p.add_argument("--coord-id", default="")
    p.add_argument("--out", default="")
    p.set_defaults(func=lambda a: cmd_brief(a))

    p = sub.add_parser("status", help="restart preconditions + live posture at a glance")
    p.add_argument("--spec", required=True)
    p.set_defaults(func=lambda a: cmd_status(HagSpec.load(a.spec)))

    p = sub.add_parser("install-systemd",
                       help="write (and optionally enable) systemd --user watcher units")
    p.add_argument("--spec", required=True)
    p.add_argument("--coord-id", default="")
    p.add_argument("--enable", action="store_true", help="daemon-reload + enable --now each timer")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_install_cli)

    p = sub.add_parser("uninstall-systemd",
                       help="disable + remove a retired HAG's watcher units")
    p.add_argument("--hag", required=True)
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=lambda a: cmd_uninstall_systemd(a.hag, dry_run=a.dry_run))

    args = ap.parse_args(argv)
    return args.func(args)


def cmd_install_cli(a: argparse.Namespace) -> int:
    try:
        spec = HagSpec.load(a.spec)
    except OSError as exc:
        print(f"cannot read spec {a.spec}: {exc}")
        return 1
    coord_id = a.coord_id
    if not coord_id and spec.coordinator:
        coord_id = spec.coordinator.session
    return cmd_install_systemd(spec, os.path.abspath(a.spec), coord_id,
                               dry_run=a.dry_run, enable=a.enable)


def cmd_watch(a: argparse.Namespace) -> int:
    if a.mode == "queue":
        if len(a.args) < 3:
            print("usage: hag.py watch --hag ID queue <queue-file> <notify> <dir> [test]")
            return 1
        return cmd_queue(a.hag, a.args[0], a.args[1], a.args[2],
                         test=len(a.args) > 3 and a.args[3] == "test")
    if a.mode == "lanes":
        if len(a.args) < 3:
            print("usage: hag.py watch --hag ID lanes <notify> <dir> <lane...> [--test]")
            return 1
        test = "--test" in a.args
        lanes = [x for x in a.args[2:] if x != "--test"]
        return cmd_lanes(a.hag, a.args[0], a.args[1], lanes, test=test)
    if len(a.args) < 2:
        print("usage: hag.py watch --hag ID pipeline <notify> <dir> [org] [proj]")
        return 1
    org = a.args[2] if len(a.args) > 2 else "https://dev.azure.com/speediedan"
    proj = a.args[3] if len(a.args) > 3 else "interpretune"
    return cmd_pipeline(a.hag, a.args[0], a.args[1], org, proj)


def cmd_heartbeat_cli(a: argparse.Namespace) -> int:
    if a.touch:
        print(hb_touch(a.hag, a.touch))
        return 0
    spec = HagSpec.load(a.spec) if a.spec else HagSpec(hag=a.hag, dir=a.dir)
    return cmd_heartbeat(spec, a.threshold_min, notify=a.notify,
                         dry_run=a.dry_run, refire_min=a.refire_min)


def cmd_brief(a: argparse.Namespace) -> int:
    spec = HagSpec.load(a.spec)
    role = spec.role(a.slug)
    if role is None:
        print(f"no role {a.slug} in {a.spec}")
        return 1
    text = brief_for(role, spec, a.coord_id)
    if a.out:
        with open(a.out, "w") as fh:
            fh.write(text + "\n")
        print(f"brief -> {a.out}")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
