#!/usr/bin/env python3
"""hag.py — scripted HAG management (start + generation restart + watchers).

Replaces hag-restart.sh / watch-coordination.sh / hag_queue_watch.sh with one
CLI over a role/spec model (roles.py) and per-ecosystem backends (backends.py).
Runbook §1. Telemetry lives in telemetry/ (stdlib); this CLI is stdlib-only
too — no third-party dependencies by design (cron-safe, no venv).

Subcommands:
  start        mint a new HAG (id, dirs, hag.json, queue files), then restart
               stages handoffs+restart into live sessions.
  restart      staged generation restart from --spec hag.json:
               handoffs, sync-plans, retire, restart. Every stage honors
               --dry-run independently. Non-opencode roles auto-launch by
               default (claude --bg); --consumer-launched records an
               already-live session, --defer-consumers writes briefs only.
  watch        coordinator-side watchers: queue | lanes | pipeline (cron).
  queue-watch  consumer QUEUE self-watch (background, exits on unseen items).
  heartbeat    stall daemon over /tmp/<hag>/hb-<slug> ticks (cron */5).
  brief        print/write one role's first prompt (restart uses this).
  status       restart preconditions at a glance: session state, hb age,
               handoff presence, queue counts.

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

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from backends import ClaudeBackend, UnsupportedError, get as get_backend  # noqa: E402
from briefs import brief_for  # noqa: E402
from heartbeat import cmd_heartbeat, touch as hb_touch  # noqa: E402
from queue_section import QueueReadError, read_queue  # noqa: E402
from queue_watch import cmd_queue_watch  # noqa: E402
from roles import HAG_ROOT, HagSpec, Role, default_spec_path  # noqa: E402
from watchers import cmd_lanes, cmd_pipeline, cmd_queue  # noqa: E402

ALL_STAGES = ("handoffs", "sync-plans", "retire", "restart")


def want(stages: str, name: str) -> bool:
    return name in [s.strip() for s in stages.split(",") if s.strip()]


def parse_role(spec: str) -> Role:
    """SLUG,ECO,TITLE,HANDOFF,PLAN[,QUEUE][,coord][,model=<m>][,grant=<text>...]."""
    parts = spec.split(",")
    if len(parts) < 5:
        raise ValueError(f"bad --role spec (want SLUG,ECO,TITLE,HANDOFF,PLAN[,...]): {spec!r}")
    slug, eco, title, handoff, plan = parts[:5]
    queue, coord, model, grants = "", False, "", []
    for tok in parts[5:]:
        if tok == "coord":
            coord = True
        elif tok.startswith("model="):
            model = tok[len("model="):]
        elif tok.startswith("grant="):
            grants.append(tok[len("grant="):])
        elif not queue:
            queue = tok
        else:
            raise ValueError(f"bad --role token {tok!r} in {spec!r}")
    return Role(slug=slug, ecosystem=eco, title=title, handoff=handoff,
                plan=plan, queue=queue, coordinator=coord, model=model,
                grants=grants)


def load_spec(args: argparse.Namespace) -> HagSpec:
    if not getattr(args, "spec", ""):
        print("restart needs --spec <hag.json> (legacy coord/lane flags removed 2026-09-19)")
        sys.exit(1)
    spec = HagSpec.load(args.spec)
    if getattr(args, "gen", ""):
        spec.gen = args.gen
    if getattr(args, "no_coordinator", False):
        for r in spec.roles:
            r.coordinator = False
    return spec
    return build_legacy(args)


# ---- stages ---------------------------------------------------------------

def stage_handoffs(spec: HagSpec, dry_run: bool) -> int:
    print("=== 0a. handoffs (template-seeded, sessions fill them)")
    print("handoffs live in each goal's handoffs/ dir; this stage verifies presence + metadata headers:")
    rc = 0
    for role in spec.roles:
        if dry_run:
            print(f"[dry-run] verify {role.handoff} (role, queue, base, blockers, grants, metadata header)")
            continue
        try:
            text = open(role.handoff).read()
        except OSError:
            print(f"MISSING: {role.handoff} ({role.slug} must write it before restart)")
            rc = 5
            continue
        if re.search(r"^- handing-off-model:|^# Handoff", text, flags=re.M):
            print(f"ok: {role.handoff}")
        else:
            print(f"INCOMPLETE: {role.handoff} (metadata header missing)")
            rc = 5
    return rc


def stage_sync_plans(spec: HagSpec, dry_run: bool) -> int:
    print("=== 0b. sync-plans (only where orchestration-standard plan_state docs exist)")
    for role in spec.roles:
        if role.plan.endswith(("_coordination.md", "_plan_state.md")):
            print(f"plan doc in scope: {role.plan} (sessions instructed separately to sync;"
                  " verify mtimes moved before restart)")
        else:
            print(f"skip (no orchestration-standard plan): {role.plan or '(none)'}")
    return 0


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
    path = os.path.join(d, f"{role.slug}-{spec.gen}.md")
    with open(path, "w") as fh:
        fh.write(brief_for(role, spec, coord_id) + "\n")
    return path


def stage_restart(spec: HagSpec, old_sessions: list[str], launched: dict[str, str],
                  defer_consumers: bool, probe_claude: bool,
                  dry_run: bool) -> int:
    coord = None if spec.flattened else spec.coordinator
    if spec.flattened:
        print("=== flattened mode: no coordinator (registry/loop-test skipped, shared-channel briefs)")
    if coord:
        print(f"=== 1. coordinator: {coord.title}-{spec.gen}")
    be_oc = get_backend("opencode")
    coord_id = ""
    if coord:
        if dry_run:
            print("[dry-run] create + brief coordinator")
            coord_id = "ses_DRYRUNCOORD"
        else:
            coord_id = be_oc.create(f"{coord.title}-{spec.gen}", spec.dir)
            coord.session = coord_id
            print(f"coordinator: {coord_id}")
            be_oc.send(coord_id, spec.dir, brief_for(coord, spec))
    print("=== 2. lanes/roles")
    for role in spec.roles:
        if role.coordinator:
            continue
        be = get_backend(role.ecosystem)
        brief_path = os.path.join(HAG_ROOT, spec.hag, "briefs",
                                  f"{role.slug}-{spec.gen}.md")
        if not dry_run:
            brief_path = write_brief(spec, role, coord_id)
        tag = f"{role.slug} ({role.title}-{spec.gen}, {role.ecosystem})"
        if role.ecosystem == "opencode":
            if dry_run:
                print(f"[dry-run] create + brief lane {tag} with coord {coord_id or '(none)'}")
                role.session = f"ses_DRYRUN_{role.slug}"
            else:
                role.session = be.create(f"{role.title}-{spec.gen}", spec.dir)
                print(f"lane {tag}: {role.session}")
                be.send(role.session, spec.dir, brief_for(role, spec, coord_id))
        else:
            # Non-opencode ecosystem: default is auto-launch (no manual step).
            text = brief_for(role, spec, coord_id)
            print(f"consumer {tag}: brief -> {brief_path}")
            if role.slug in launched:
                role.session = launched[role.slug]
                print(f"  recorded live as {role.session} (--consumer-launched)")
            elif defer_consumers or dry_run:
                print(f"  {'[dry-run] ' if dry_run else ''}DEFERRED "
                      f"(--defer-consumers): launch by hand when ready.")
            else:
                try:
                    role.session = be.create(
                        f"{role.title}-{spec.gen}", spec.dir,
                        brief=text, name=f"{role.slug}-{spec.gen}",
                        model=role.model)
                except (UnsupportedError, RuntimeError) as exc:
                    print(f"  FAIL: auto-launch failed: {exc}")
                    return 4
                print(f"  launched: {role.session} (backend: {be.ecosystem})")
    if coord and not spec.flattened:
        print("=== 3. registry + loop test (coordinator-executed)")
        reg = (f"REGISTRY ({spec.gen}, direct-relay): "
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
                return 3
    print("=== 5. claude/codex consumers")
    print(f"  sockets (no turn): {ClaudeBackend.probe_sockets()}")
    if probe_claude and not dry_run:
        print("  probe turn:", ClaudeBackend.probe_turn()[:200])
    print("DONE. Old generation retires by neglect;"
          " new spec sessions recorded in hag.json (save it with --save-spec).")
    return 0


# ---- start ----------------------------------------------------------------

QUEUE_TEMPLATE = """# QUEUE + STATUS: {slug} (HAG {hag})

## QUEUE (coordinator-owned; consumer: read-only)

(empty — coordinator seeds top-down with bases)

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


def cmd_start(args: argparse.Namespace) -> int:
    ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d-%H%M%S")
    hag = f"{args.name}-{ts}"
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
    spec = HagSpec(hag=hag, dir=args.dir, gen="r1", roles=roles)
    errs = spec.validate()
    if errs:
        print("spec invalid:", *errs, sep="\n  ")
        return 1
    for r in roles:
        if r.queue or r.coordinator or r.ecosystem == "opencode":
            continue
        r.queue = os.path.join(hagdir, "queues", f"{r.slug}.md")
    if args.dry_run:
        print(f"[dry-run] new HAG {hag} at {hagdir}")
        print(f"[dry-run] spec: {spec.to_dict()}")
        if args.adopt_queue:
            print(f"[dry-run] adopt last-done + base from {args.adopt_queue}")
        rc = stage_handoffs(spec, True) or stage_sync_plans(spec, True)
        return rc
    os.makedirs(os.path.join(hagdir, "queues"), exist_ok=True)
    os.makedirs(os.path.join(hagdir, "telemetry"), exist_ok=True)
    spec_path = os.path.join(hagdir, "hag.json")
    for r in roles:
        if r.coordinator or not r.queue or os.path.exists(r.queue):
            continue
        ts = datetime.datetime.now(datetime.timezone.utc).strftime("%FT%TZ")
        last_done = adopt_status(args.adopt_queue, ts) if args.adopt_queue else " none (fresh HAG)"
        with open(r.queue, "w") as fh:
            fh.write(QUEUE_TEMPLATE.format(slug=r.slug, hag=hag, ts=ts,
                                           last_done=last_done))
    spec.save(spec_path)
    print(f"new HAG {hag} at {hagdir} (spec: {spec_path})")
    args.spec = spec_path
    args.hag = hag
    args.stages = "handoffs,restart"
    return cmd_restart(args)


def cmd_restart(args: argparse.Namespace) -> int:
    spec = load_spec(args)
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
    old = list(args.old_session or [])
    if getattr(args, "spec", "") and old_sessions_from_spec(args.spec, spec.gen):
        old += old_sessions_from_spec(args.spec, spec.gen)
    rc = 0
    if want(stages, "handoffs"):
        rc = stage_handoffs(spec, dry) or rc
    if want(stages, "sync-plans"):
        rc = stage_sync_plans(spec, dry) or rc
    if want(stages, "retire"):
        rc = stage_retire(spec, old, dry) or rc
    if want(stages, "restart"):
        rc = stage_restart(spec, old, launched,
                           args.defer_consumers, args.probe_claude, dry) or rc
    else:
        print(f"DONE (stages: {stages}).")
    if getattr(args, "save_spec", "") and not dry and rc == 0:
        HagSpec(hag=spec.hag, dir=spec.dir, gen=spec.gen,
                roles=spec.roles).save(args.save_spec)
        print(f"spec with live session ids saved to {args.save_spec}")
    return rc


def old_sessions_from_spec(path: str, gen: str) -> list[str]:
    try:
        old = HagSpec.load(path)
    except Exception:
        return []
    if old.gen == gen:
        return []
    return [r.session for r in old.roles if r.session]


# ---- status ---------------------------------------------------------------

def cmd_status(spec: HagSpec) -> int:
    print(f"HAG {spec.hag} gen={spec.gen} dir={spec.dir} "
          f"mode={'flattened' if spec.flattened else 'coordinated'}")
    be_oc = get_backend("opencode")
    for r in spec.roles:
        parts = [f"{r.slug} [{r.ecosystem}]"]
        if r.coordinator:
            parts.append("COORD")
        parts.append(f"session={r.session or '(none)'}")
        if r.ecosystem == "opencode" and r.session and not r.session.startswith("ses_DRYRUN"):
            try:
                parts.append(f"state: {be_oc.status(r.session, spec.dir)}")
            except Exception as exc:  # noqa: BLE001
                parts.append(f"state: QUERY-FAILED ({exc})")
        from heartbeat import age_min, hb_path
        age = age_min(hb_path(spec.hag, r.slug))
        parts.append(f"hb={'never' if age is None else f'{age:.0f}m ago'}")
        parts.append(f"handoff={'ok' if r.handoff and os.path.exists(r.handoff) else 'MISSING'}")
        if r.queue and os.path.exists(r.queue):
            try:
                h, ids = read_queue(r.queue)
                parts.append(f"queue={len(ids)} items@{h}")
            except QueueReadError as exc:
                parts.append(f"queue=UNREADABLE ({exc})")
        print("  " + " | ".join(parts))
    return 0


# ---- CLI ------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="hag.py",
                                 description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("start", help="mint a new HAG and start its first generation")
    p.add_argument("--name", required=True)
    p.add_argument("--dir", required=True)
    p.add_argument("--role", action="append", default=[],
                   help="SLUG,ECO,TITLE,HANDOFF,PLAN[,QUEUE][,coord][,model=M][,grant=TXT] (repeatable)")
    p.add_argument("--adopt-queue", default="",
                   help="retired queue file: carry last-done + base into new STATUS")
    p.add_argument("--no-coordinator", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--consumer-launched", action="append", default=[])
    p.add_argument("--defer-consumers", action="store_true")
    p.add_argument("--probe-claude", action="store_true")
    p.set_defaults(func=lambda a: cmd_start(a))

    p = sub.add_parser("restart", help="staged generation restart")
    p.add_argument("--spec", required=True, help="hag.json path")
    p.add_argument("--gen", default="")
    p.add_argument("--stages", default=",".join(ALL_STAGES))
    p.add_argument("--old-session", action="append", default=[])
    p.add_argument("--consumer-launched", action="append", default=[],
                   help="SLUG:ID for an already-live non-opencode session (skip auto-launch)")
    p.add_argument("--defer-consumers", action="store_true",
                   help="write consumer briefs only; launch by hand later")
    p.add_argument("--probe-claude", action="store_true")
    p.add_argument("--no-coordinator", action="store_true")
    p.add_argument("--save-spec", default="")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=lambda a: cmd_restart(a))

    p = sub.add_parser("watch", help="coordinator-side watchers (cron)")
    p.add_argument("--hag", required=True)
    p.add_argument("mode", choices=("queue", "lanes", "pipeline"))
    p.add_argument("args", nargs="*",
                   help="queue: <queue-file> <notify> <dir> [test] | lanes: <notify> <dir> <lane...> [--test] | pipeline: <notify> <dir> [org] [proj]")
    p.set_defaults(func=lambda a: cmd_watch(a))

    p = sub.add_parser("queue-watch", help="consumer QUEUE self-watch")
    p.add_argument("--sidecar", required=True)
    p.add_argument("--interval", required=True, type=float)
    p.set_defaults(func=lambda a: cmd_queue_watch(a.sidecar, a.interval))

    p = sub.add_parser("heartbeat", help="stall daemon over hb ticks")
    p.add_argument("--hag", default="")
    p.add_argument("--spec", default="")
    p.add_argument("--threshold-min", type=float, default=60)
    p.add_argument("--notify", default="")
    p.add_argument("--dir", default="")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--touch", default="",
                   help="session-side tick: touch hb file for SLUG and exit")
    p.set_defaults(func=lambda a: cmd_heartbeat_cli(a))

    p = sub.add_parser("brief", help="print/write one role's first prompt")
    p.add_argument("--spec", required=True)
    p.add_argument("--slug", required=True)
    p.add_argument("--coord-id", default="")
    p.add_argument("--out", default="")
    p.set_defaults(func=lambda a: cmd_brief(a))

    p = sub.add_parser("status", help="restart preconditions at a glance")
    p.add_argument("--spec", required=True)
    p.set_defaults(func=lambda a: cmd_status(HagSpec.load(a.spec)))

    args = ap.parse_args(argv)
    return args.func(args)


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
                         dry_run=a.dry_run)


def cmd_brief(a: argparse.Namespace) -> int:
    spec = HagSpec.load(a.spec)
    role = next((r for r in spec.roles if r.slug == a.slug), None)
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
