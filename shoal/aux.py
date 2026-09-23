#!/usr/bin/env python3
"""hag-aux: the residual lane (architecture doc §4.1; H8 proposal §3).

An ordinary `fill=self` worker whose spec carries `aux: true` and no brief of its own.
The digest ranks for it the free, workable board items whose best fit to any other
ACTIVE role (a scored role holding a session) is lowest, board-effective order breaking
ties, with unblockers of active lanes' work first. Its territory therefore follows the
roster: a parked lane's items become aux work, and return when the lane does, with no
re-brief. `next` prints an AUX header naming the lanes it stays out of.

  hag.py aux-init   --hag H [--ecosystem opencode] [--model M] [--effort E] [--doc-only] [--dry-run]
  hag.py aux-launch --hag H [--dry-run] [--force] [--verify-s 600]

`aux-init` writes the start doc to the HAG data dir and adds the role; `aux-launch` starts
its session alone (no restart, no incarnation bump) and counts only once the role's
ledger-watch is proven alive. `start --aux` and `add-role --aux` do the same at creation.
Needs `features.matcher`: without the fit matrix the aux ranking is board order only.
"""
from __future__ import annotations

import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from roles import HagSpec, Role, find_spec, roles_dir  # noqa: E402

AUX_SLUG = "hag-aux"
AUX_ECOSYSTEM = "opencode"
AUX_MODEL = "opencode-go/muse-spark-1.3-contributor"
AUX_EFFORT = "high"

AUX_DOC = """# {slug}: plan_state (start doc)

## Mission

You are the residual lane of HAG `{hag}`. You work the board items that no other active
lane's brief covers: hygiene, tests, docs, small features and unblockers that would
otherwise sit unowned. You never compete with a lane for its own work.

## How your work is chosen

- The digest's semantic matcher scores every open board item 0-3 against every other
  lane's brief. Your candidates are the free items classed `workable` whose HIGHEST fit to
  any active lane is lowest, in effective board order. Items that block an active lane's
  work come first (`unblocks` in the candidate line).
- `hag.py next` prints an `AUX` line naming the lanes you stay out of. The set changes
  when roles are added, parked or retired; `next` is always current, this doc is not.
- A candidate that plainly belongs to a named lane is passed, not worked:
  `--pass <item>:out-of-lane:suggest=<lane>`.
- Items classed `needs-maintainer` or `parked` never reach you. If one you hold turns out
  to need a maintainer decision, raise the prompted ask and pass or wait per the work loop.

## Current state

- Created {created} by `hag.py aux-init`. No items claimed yet.

## Decisions

- Aux work is ordinary work: claims, PRs, CI and merges follow the same rules as every lane.
- PR sizing: prefer batching related hygiene into one pipeline-triggering PR; keep
  docs-only changes separate (shared instructions, "PR sizing against a shared, gated CI queue").

## Running monitors

- ledger-watch: armed per your brief (sidecar `/tmp/{hag}/ledger-watch-{slug}.state`).

## grants-needed

- PRE-AUTHORIZED: pipeline build approvals once a build's CPU CI matrix is complete (shared
  instructions, "Build approvals are pre-authorized").
- STANDING: PR merges are asked first-person per merge, with defer and delegate options.
- None pending.
"""


def aux_doc_path(hag: str, slug: str = AUX_SLUG) -> str:
    return os.path.join(roles_dir(hag), slug, "plan_state.md")


def aux_role(hag: str, ecosystem: str = AUX_ECOSYSTEM, model: str = AUX_MODEL, effort: str = AUX_EFFORT,
             slug: str = AUX_SLUG) -> Role:
    return Role(slug=slug, ecosystem=ecosystem, title=slug, doc=aux_doc_path(hag, slug), fill="self",
                model=model, effort=effort, seq=1, aux=True,
                context={"brief": "", "never": {}})


def write_doc(hag: str, slug: str = AUX_SLUG) -> str:
    from matcher import now_iso
    path = aux_doc_path(hag, slug)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        fh.write(AUX_DOC.format(slug=slug, hag=hag, created=now_iso()))
    return path


def parse_aux_arg(val: str) -> tuple[str, str]:
    """`--aux ECOSYSTEM[:MODEL]` -> (ecosystem, model)."""
    eco, _, model = (val or AUX_ECOSYSTEM).partition(":")
    return eco or AUX_ECOSYSTEM, model or (AUX_MODEL if (eco or AUX_ECOSYSTEM) == "opencode" else "")


def header(spec: HagSpec, slug: str) -> str:
    """The AUX line in `next`: the lanes this role stays out of, from the live roster."""
    import matcher
    from digest import active_lanes
    lanes = matcher.lanes(spec)
    active = [s for s in active_lanes(spec, list(lanes)) if s != slug]
    parked = [s for s in lanes if s not in active and s != slug]
    parts = []
    for s in active:
        b = lanes[s].split(". ")[0].strip().rstrip(".")
        parts.append(f"{s} ({b[:110]})")
    on = matcher.config(spec) is not None
    return ("AUX: you are the residual lane. Stay out of: " + ("; ".join(parts) or "(no active lanes)")
            + (f". Parked lanes whose work is yours meanwhile: {', '.join(parked)}" if parked else "")
            + ". Candidates below are free workable items with the lowest best fit to those lanes"
            + ("" if on else " (features.matcher is off: ranking is board order only, weigh each by hand)")
            + "; pass one that belongs to a lane with --pass <item>:out-of-lane:suggest=<lane>.")


def cmd_aux_init(a) -> int:
    path = a.spec or find_spec(a.hag)
    spec = HagSpec.load(path)
    slug = a.slug or AUX_SLUG
    doc = aux_doc_path(spec.hag, slug)
    have = spec.role(slug)
    plan = (f"aux-init {spec.hag}: doc {doc}"
            + ("" if a.doc_only else f"; role {'present' if have else 'ADD'} {slug} ({a.ecosystem}, "
                                     f"model={a.model or '-'}, fill=self, aux)"))
    warn = "" if (spec.features or {}).get("matcher") else \
        " WARNING: features.matcher is off, so the aux ranking is board order only"
    if have is not None and not getattr(have, "aux", False):
        print(f"ERROR: {slug} exists and is not an aux role")
        return 1
    if a.dry_run:
        print("[dry-run] " + plan + warn)
        return 0
    write_doc(spec.hag, slug)
    if not a.doc_only:
        if have is None:
            spec.roles.append(aux_role(spec.hag, a.ecosystem, a.model, a.effort, slug))
        else:
            have.doc = doc
        errs = spec.validate()
        if errs:
            print("spec invalid:", *errs, sep="\n  ")
            return 1
        from hag import persist_spec
        persist_spec(spec, path, note=f"aux-init {slug}")
    print(plan + warn)
    return 0


def cmd_aux_launch(a) -> int:
    """Start (or restart) the aux session alone: no incarnation bump; seq bumps when a
    previous session existed. Counts once the role's ledger-watch is alive."""
    from health import watcher_alive
    path = a.spec or find_spec(a.hag)
    spec = HagSpec.load(path)
    slug = a.slug or AUX_SLUG
    role = spec.role(slug)
    if role is None or not getattr(role, "aux", False):
        print(f"ERROR: no aux role {slug} in {spec.hag}: run `hag.py aux-init --hag {spec.hag}` first")
        return 1
    alive, _ = watcher_alive(spec.hag, slug)
    if alive and not a.force:
        print(f"{slug} ledger-watch already running; nothing to do (--force to launch anyway)")
        return 0
    old = role.session
    if old:
        role.seq += 1
    plan = (f"aux-launch {spec.hag}: {role.label} {role.ecosystem} model={role.model or '-'} "
            f"(previous session {old or '-'}; incarnation unchanged)")
    if a.dry_run:
        print("[dry-run] " + plan)
        return 0
    from backends import get as get_backend
    from hag import persist_spec, write_brief
    brief = open(write_brief(spec, role)).read()
    be = get_backend(role.ecosystem)
    role.session = be.create(f"{role.title}-r{role.seq}", spec.dir, brief=brief,
                             name=role.tag, model=role.model, effort=role.effort)
    if role.ecosystem == "opencode":
        # opencode create takes no prompt: the brief must be sent, or the new
        # session idles forever with an empty transcript (measured 2026-09-23:
        # hag-aux-r1 sat promptless until the brief was sent by hand).
        be.send(role.session, spec.dir, brief, model=role.model)
    spec.record_generation()
    persist_spec(spec, path, note=f"aux-launch {role.tag}")
    print(plan + f" -> launched {role.session}")
    t0 = time.time()
    while time.time() - t0 < a.verify_s:
        alive, why = watcher_alive(spec.hag, slug)
        if alive:
            print(f"AUX LOOP ALIVE: {slug} ledger-watch pid {why.get('pid', '?')}")
            return 0
        time.sleep(15)
    print(f"AUX LOOP INCOMPLETE: no {slug} ledger-watch within {a.verify_s:g}s; check the session "
          f"{role.session} (a pending grants ask or a failed arm)")
    return 4


def add_parsers(sub) -> None:
    p = sub.add_parser("aux-init", help="write the hag-aux start doc (data dir) and add the aux role to the spec")
    p.add_argument("--hag", required=True)
    p.add_argument("--spec", default="")
    p.add_argument("--slug", default="", help=f"role slug (default {AUX_SLUG})")
    p.add_argument("--ecosystem", default=AUX_ECOSYSTEM, choices=["opencode", "claude"])
    p.add_argument("--model", default=AUX_MODEL)
    p.add_argument("--effort", default=AUX_EFFORT)
    p.add_argument("--doc-only", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_aux_init)
    p = sub.add_parser("aux-launch", help="(re)launch the aux session alone (no restart, no incarnation bump)")
    p.add_argument("--hag", required=True)
    p.add_argument("--spec", default="")
    p.add_argument("--slug", default="")
    p.add_argument("--force", action="store_true")
    p.add_argument("--verify-s", type=float, default=600)
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_aux_launch)
