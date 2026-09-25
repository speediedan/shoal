#!/usr/bin/env python3
"""shoal subcommands over the ledger (ledger.py): the verbs every role and
the maintainer use. Each is one command, one event, one commit, one push.

  offer     --shoal H --to SLUG|* --item repo#n:verb [--base repo@sha --prio N --note ..] [--by user]
  withdraw  --shoal H --item repo#n [--to SLUG --reason ..]
  claim     --shoal H --slug S --item repo#n:verb [--base ..]     first landed wins; loser told
  release   --shoal H --slug S --item repo#n --kind decline|blocked|handoff|lost-race [--reason .. --retry ..]
  done      --shoal H --slug S --item repo#n:verb [--kind merge|fix|.. --url .. --sha ..]
  msg       --shoal H --by S --to SLUG|*|user --text .. [--kind .. --re ID --ack]
  show      --shoal H [--slug S] [--board] [--json]                the status table / one role's view
  fill      --shoal H --slug S [--limit N] [--claim-top]           board candidates for a self-filler
  sync      --shoal H                                              pull + push pending ledger commits
  ledger-init  --spec <spec>                                    create shoals/<id>/ in the ledger repo
  ledger-lint  --shoal H                                           permission violations, contested claims

`--by` defaults to `user` for offer/withdraw/msg when run by a human; roles
pass their own slug to `msg`. A role that is not in the spec is refused as a
writer, and only `user` (or `shoal`) may offer or withdraw (flattened only).
"""
from __future__ import annotations

import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from ledger import OFFER_WRITERS  # noqa: E402
import config  # noqa: E402

from ledger import (DEFAULT_PRIO, Ledger, LedgerError, View, ensure_clone, fmt_item,  # noqa: E402
                    item_key, lint, render_all, render_role, split_item,
                    sync_pull, sync_push, unpushed)
from roles import DEFAULT_LEDGER, HagSpec, find_spec, state_dir  # noqa: E402


def _spec(a) -> HagSpec | None:
    try:
        return HagSpec.load(find_spec(a.hag, getattr(a, "spec", "")))
    except OSError:
        return None


def _ledger(a, spec: HagSpec | None) -> Ledger:
    root = getattr(a, "ledger_dir", "") or (spec.ledger_dir if spec else "")
    if spec and spec.ledger and spec.ledger.get("repo") and not os.path.isdir(root):
        ensure_clone(root, spec.ledger["repo"])
    led = Ledger(a.hag, root=root or None, sync=not getattr(a, "no_sync", False))
    if not led.exists() and led.sync:
        led.pull()  # a fresh clone on another host: the ledger may simply not be fetched yet
    if not led.exists():
        raise LedgerError(f"no ledger for {a.hag} at {led.ldir}: run `shoal ledger-init --spec <hag.json>`"
                          + (" (or `shoal sync` if this clone is behind)" if not led.sync else ""))
    return led


def _writer(a, spec: HagSpec | None, attr: str = "by", default: str = "user") -> str:
    by = getattr(a, attr, "") or default
    if spec is not None and by not in OFFER_WRITERS and spec.role(by) is None:
        raise LedgerError(f"writer {by!r} is not a role in {spec.hag} ({[r.slug for r in spec.roles]})")
    return by


def _guard(fn):
    def wrapped(a):
        try:
            return fn(a)
        except LedgerError as exc:
            print(f"ERROR: {exc}")
            return 1
    return wrapped


@_guard
def cmd_offer(a) -> int:
    spec = _spec(a)
    led = _ledger(a, spec)
    by = _writer(a, spec)
    led.pull()
    view = View(led, spec)
    if not view.perms.may_offer(by, a.to):
        print(f"ERROR: {by} may not offer to {a.to} (only user offers work; a role hands its own "
              f"claim to a peer with `release --kind handoff --to <role>`)")
        return 1
    key = item_key(a.item)
    if key in view.done:
        print(f"ERROR: {key} is already done ({view.done[key]['ts']} by {view.done[key]['by']})")
        return 1
    dup = [o for o in view.by_item.get(key, []) if o["status"] == "open" and o["to"] == a.to]
    if dup and not a.force:
        print(f"already offered to {a.to}: {fmt_item(dup[0])} [{dup[0]['id']}] (pass --force to add another)")
        return 0
    if a.dry_run:
        print(f"[dry-run] offer {a.item} to {a.to} by {by} prio={a.prio} base={a.base or '-'}")
        return 0
    ev = led.append(by, "offer", to=a.to, item=a.item, base=a.base, prio=int(a.prio),
                    note=a.note, src=a.src or by, now=(True if getattr(a, "now", False) else None))
    print(f"offered {fmt_item(ev)} to {a.to} by {by} [{ev['id']}]" + ("" if led.sync else " (local only)"))
    return 0


@_guard
def cmd_withdraw(a) -> int:
    spec = _spec(a)
    led = _ledger(a, spec)
    by = _writer(a, spec)
    led.pull()
    view = View(led, spec)
    key = item_key(a.item)
    targets = [o for o in view.by_item.get(key, []) if o["status"] in ("open", "claimed")
               and (not a.to or o["to"] == a.to)]
    if not targets:
        print(f"nothing to withdraw: no open offer of {key}" + (f" to {a.to}" if a.to else ""))
        return 1
    for o in targets:
        if not view.perms.may_withdraw(by, o):
            print(f"ERROR: {by} may not withdraw {o['id']} (offered by {o['by']}; only user withdraws)")
            return 1
    if a.dry_run:
        print(f"[dry-run] withdraw {key} ({len(targets)} offer(s)) by {by}")
        return 0
    ev = led.append(by, "withdraw", item=key, to=a.to, reason=a.reason)
    print(f"withdrew {key}" + (f" from {a.to}" if a.to else "") + f" [{ev['id']}]")
    return 0


@_guard
def cmd_claim(a) -> int:
    """First landed wins: check, append+push, re-check. A loser writes its own
    lost-race release so nobody has to notice for it."""
    spec = _spec(a)
    led = _ledger(a, spec)
    by = _writer(a, spec, "slug", "")
    if not by:
        print("ERROR: claim needs --slug")
        return 1
    led.pull()
    view = View(led, spec)
    key, verb = split_item(a.item)
    key = item_key(key)
    w = view.winner(key)
    if w is not None and w["by"] != by:
        print(f"CLAIM-REFUSED {key}: held by {w['by']} since {w['ts']} [{w['id']}]")
        return 4
    if w is not None and w["by"] == by:
        print(f"already held by {by}: {fmt_item(w)} [{w['id']}]")
        return 0
    if key in view.done:
        print(f"CLAIM-REFUSED {key}: done {view.done[key]['ts']} by {view.done[key]['by']}")
        return 4
    addressed = next((o["to"] for o in view.by_item.get(key, []) if o["status"] == "open"), None)
    if not view.perms.may_claim(by, addressed, a.src):
        print(f"CLAIM-REFUSED {key}: {by} (fill={view.perms.roles.get(by, {}).get('fill')}) may not claim "
              f"{'an unoffered item' if addressed is None else 'an offer to ' + addressed}")
        return 4
    if a.dry_run:
        print(f"[dry-run] claim {key}:{verb or '?'} by {by}")
        return 0
    role = spec.role(by) if spec else None
    ev = led.append(by, "claim", item=key, verb=verb or a.verb, base=a.base,
                    src=a.src or ("offer" if addressed else "board"),
                    session=(role.session if role else ""), _push=False)
    pushed = sync_push(led.root) if led.sync else True
    led.pull()
    view = View(led, spec)
    w = view.winner(key)
    if w is not None and w["by"] != by:
        led.append(by, "release", item=key, kind="lost-race",
                   reason=f"{w['by']} landed first [{w['id']}]")
        print(f"CLAIM-LOST {key}: {w['by']} landed first; released (lost-race)")
        return 4
    print(f"claimed {key}:{verb or a.verb or '?'} by {by} [{ev['id']}]"
          + ("" if pushed else " (push pending: run `shoal sync`)"))
    return 0


@_guard
def cmd_release(a) -> int:
    spec = _spec(a)
    led = _ledger(a, spec)
    by = _writer(a, spec, "slug", "")
    led.pull()
    view = View(led, spec)
    key = item_key(a.item)
    if not any(c["by"] == by for c in view.active(by) if item_key(c["item"]) == key) and a.kind != "decline":
        print(f"note: {by} holds no live claim on {key}; recording the {a.kind} anyway")
    to = getattr(a, "to", "") or ""
    if to:
        if a.kind != "handoff":
            print("ERROR: --to is only for --kind handoff (hand your own claim to a named role)")
            return 1
        if spec is not None and spec.role(to) is None:
            print(f"ERROR: --to {to!r} is not a role in {spec.hag}")
            return 1
        if not any(item_key(c["item"]) == key for c in view.active(by)):
            print(f"ERROR: {by} holds no live claim on {key}; only the holder can hand it off")
            return 1
    if a.dry_run:
        print(f"[dry-run] release {key} kind={a.kind} by {by}" + (f" -> offered to {to}" if to else ""))
        return 0
    ev = led.append(by, "release", item=key, kind=a.kind, reason=a.reason, retry=a.retry, to=to or None)
    if to:
        print(f"handed {key} to {to} (an implicit offer; {to} claims it at its next boundary, woken if idle)")
    print(f"released {key} ({a.kind}) by {by} [{ev['id']}]")
    return 0


@_guard
def cmd_done(a) -> int:
    spec = _spec(a)
    led = _ledger(a, spec)
    by = _writer(a, spec, "slug", "")
    led.pull()
    key, verb = split_item(a.item)
    if a.dry_run:
        print(f"[dry-run] done {key}:{verb} by {by}")
        return 0
    ev = led.append(by, "done", item=key, verb=verb, kind=a.kind or verb, url=a.url, sha=a.sha, note=a.note)
    print(f"done {fmt_item(ev)} by {by} [{ev['id']}] (reconcile verifies against GitHub)")
    return 0


@_guard
def cmd_msg(a) -> int:
    spec = _spec(a)
    led = _ledger(a, spec)
    by = _writer(a, spec)
    if spec is not None and a.to not in ("*", "user") and spec.role(a.to) is None:
        print(f"ERROR: unknown target {a.to!r} (roles: {[r.slug for r in spec.roles]}, or * / user)")
        return 1
    if a.dry_run:
        print(f"[dry-run] msg {by}->{a.to} [{a.kind}] {a.text[:80]}")
        return 0
    ev = led.append(by, "msg", to=a.to, text=a.text, kind=a.kind, re=a.re, ack=(True if a.ack else None),
                    wake=(True if getattr(a, "wake", False) else None))
    print(f"msg {by}->{a.to} [{a.kind}] [{ev['id']}]")
    return 0


def _candidates(spec: HagSpec | None, view: View, slug: str, limit: int = 3):
    if spec is None:
        return []
    role = spec.role(slug)
    if role is None or role.fill != "self":
        return []
    from sources import candidates
    return candidates(spec.sources, role.scope, view.taken() | view.offered_open(),
                      set(view.declined(slug)), limit=limit)


@_guard
def cmd_show(a) -> int:
    spec = _spec(a)
    led = _ledger(a, spec)
    if not a.no_pull:
        led.pull()
    view = View(led, spec)
    from heartbeat import age_min, hb_path
    if a.json:
        out = {"hag": a.hag, "roles": {}}
        for r in (spec.roles if spec else []):
            out["roles"][r.slug] = {"state": view.role_state(r.slug), "active": view.active(r.slug),
                                    "queue": view.queue(r.slug), "last_done": view.last_done(r.slug)}
        out["contested"] = view.contested
        out["violations"] = [(e, w) for e, w in view.violations]
        print(json.dumps(out, default=str, indent=1))
        return 0
    if a.slug:
        cands = None
        if a.board:
            try:
                cands = _candidates(spec, view, a.slug, limit=5)
            except Exception as exc:  # noqa: BLE001
                cands = []
                print(f"(board unavailable: {exc})")
        print(render_role(view, a.slug, cands, age_min(hb_path(a.hag, a.slug))))
        return 0
    if spec is None:
        print("show --spec needs a spec for the table; give --slug for one role")
        return 1
    hb = {r.slug: age_min(hb_path(a.hag, r.slug)) for r in spec.roles}
    cf = None
    if a.board:
        def cf(slug):  # noqa: E306
            try:
                return _candidates(spec, view, slug, limit=3)
            except Exception:
                return []
    print(render_all(view, spec, cf, hb))
    n = unpushed(led.root)
    if n:
        print(f"\n(unpushed ledger commits on this host: {n} — `shoal sync --shoal {a.hag}`)")
    return 0


@_guard
def cmd_fill(a) -> int:
    spec = _spec(a)
    led = _ledger(a, spec)
    led.pull()
    view = View(led, spec)
    role = spec.role(a.slug) if spec else None
    if role is None or role.fill != "self":
        print(f"{a.slug} is not a self-filling role (fill={getattr(role, 'fill', '?')}); nothing to fill")
        return 1
    if view.queue(a.slug) and not a.force:
        print(f"{a.slug} still has {len(view.queue(a.slug))} offered item(s); self-fill applies to an empty queue "
              "(pass --force to list candidates anyway)")
        return 0
    cands = _candidates(spec, view, a.slug, limit=a.limit)
    if not cands:
        print("no eligible board candidates (all taken, declined, closed, or out of scope)")
        return 0
    for c in cands:
        print(f"{c['pos']:3d} {c['item']}:{c['verb']}  {c['title'][:70]}  {c['url']}")
    if a.claim_top:
        c = cands[0]
        a.item, a.base, a.src, a.verb = f"{c['item']}:{c['verb']}", "", "board", c["verb"]
        return cmd_claim(a)
    return 0


@_guard
def cmd_sync(a) -> int:
    spec = _spec(a)
    led = _ledger(a, spec)
    ok = sync_pull(led.root)
    n = unpushed(led.root)
    pushed = sync_push(led.root) if n else True
    print(f"pull {'ok' if ok else 'FAILED (offline?)'}; unpushed before: {n}; push {'ok' if pushed else 'FAILED'}")
    return 0 if ok and pushed else 2


@_guard
def cmd_ledger_init(a) -> int:
    spec = HagSpec.load(a.spec)
    if not spec.ledger:
        spec.ledger = {"repo": a.repo or DEFAULT_LEDGER["repo"],
                       "dir": a.ledger_dir or DEFAULT_LEDGER["dir"]}
    elif a.repo and not spec.ledger.get("repo"):
        spec.ledger["repo"] = a.repo
    root = a.ledger_dir or spec.ledger_dir
    if not spec.ledger.get("repo") and not os.path.isdir(os.path.join(root, ".git")):
        print(f"ledger-init: no ledger repo and no clone at {root}: pass --ledger-repo URL (or set "
              "`ledger_repo` in the host config / SHOAL_LEDGER_REPO)")
        return 1
    if a.dry_run:
        print(f"[dry-run] ledger for {spec.hag}: clone {spec.ledger.get('repo')} at {root}, "
              f"create shoals/{spec.hag}/ledger/, snapshot spec, write ledger into {a.spec}")
        return 0
    ensure_clone(root, spec.ledger["repo"])
    led = Ledger(spec.hag, root=root, sync=not a.no_sync)
    if led.exists() and not a.force:
        print(f"ledger already exists at {led.ldir} (pass --force to re-snapshot the spec)")
    else:
        led.pull()
        led.init(spec.snapshot(), note=f"init from {a.spec}")
        print(f"initialized {led.ldir}")
    if a.force:
        led.snapshot_spec(spec.snapshot(), push=not a.no_sync)
    if led.sync:
        sync_push(root)
    spec.save(a.spec)
    print(f"spec {a.spec} now carries ledger={spec.ledger}")
    return 0


@_guard
def cmd_ledger_lint(a) -> int:
    spec = _spec(a)
    led = _ledger(a, spec)
    if not a.no_pull:
        led.pull()
    view = View(led, spec)
    findings = lint(view)
    if not findings:
        print(f"ok: {len(led.events())} events, no violations, no contested claims")
        return 0
    rc = 0
    for code, msg in findings:
        print(f"{code} {msg}")
        if code.startswith("E"):
            rc = 1
    return rc


def add_parsers(sub: argparse._SubParsersAction) -> None:
    def common(p, slug=False, by=False):
        config.add_id_arg(p, required=True)
        p.add_argument("--spec", default="")
        p.add_argument("--ledger-dir", default="")
        p.add_argument("--no-sync", action="store_true", help="local clone only (tests/offline)")
        p.add_argument("--dry-run", action="store_true")
        if slug:
            p.add_argument("--slug", required=True, help="the role acting")
        if by:
            p.add_argument("--by", default="user", help="writer: user (default) or a role slug")

    p = sub.add_parser("offer", help="put work on a role's queue (user only)")
    common(p, by=True)
    p.add_argument("--to", required=True, help="role slug or * (pool)")
    p.add_argument("--item", required=True, help="repo#n:verb")
    p.add_argument("--base", default="", help="repo@sha the item is valid against")
    p.add_argument("--prio", type=int, default=DEFAULT_PRIO, help="lower runs sooner (default 50)")
    p.add_argument("--note", default="")
    p.add_argument("--src", default="", help="user|board|peer (default: the writer)")
    p.add_argument("--now", action="store_true",
                   help="wake the target even while it is busy (honoured for user offers)")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_offer)

    p = sub.add_parser("withdraw", help="take an offer back")
    common(p, by=True)
    p.add_argument("--item", required=True)
    p.add_argument("--to", default="")
    p.add_argument("--reason", default="")
    p.set_defaults(func=cmd_withdraw)

    p = sub.add_parser("claim", help="own an item (first landed wins)")
    common(p, slug=True)
    p.add_argument("--item", required=True, help="repo#n:verb")
    p.add_argument("--base", default="")
    p.add_argument("--verb", default="")
    p.add_argument("--src", default="")
    p.set_defaults(func=cmd_claim)

    p = sub.add_parser("release", help="give an item up (decline|blocked|handoff|lost-race)")
    common(p, slug=True)
    p.add_argument("--item", required=True)
    p.add_argument("--kind", required=True, choices=("decline", "blocked", "handoff", "lost-race"))
    p.add_argument("--to", default="", help="handoff only: the role that should take your claim (an implicit offer)")
    p.add_argument("--reason", default="")
    p.add_argument("--retry", default="", help="condition under which to retry")
    p.set_defaults(func=cmd_release)

    p = sub.add_parser("done", help="record completion (reconcile verifies against GitHub)")
    common(p, slug=True)
    p.add_argument("--item", required=True, help="repo#n:verb")
    p.add_argument("--kind", default="")
    p.add_argument("--url", default="")
    p.add_argument("--sha", default="")
    p.add_argument("--note", default="")
    p.set_defaults(func=cmd_done)

    p = sub.add_parser("msg", help="message a role, everyone (*), or the user")
    common(p, by=True)
    p.add_argument("--to", required=True)
    p.add_argument("--text", required=True)
    p.add_argument("--wake", action="store_true",
                   help="wake the target now (honoured only when sent by user; msgs are otherwise mail)")
    p.add_argument("--kind", default="info")
    p.add_argument("--re", default="", help="event id this answers (acks)")
    p.add_argument("--ack", action="store_true", help="ask the target to ack")
    p.set_defaults(func=cmd_msg)

    p = sub.add_parser("show", help="status table (all roles) or one role's view")
    common(p)
    p.add_argument("--slug", default="")
    p.add_argument("--board", action="store_true", help="include board candidates for self-fillers")
    p.add_argument("--json", action="store_true")
    p.add_argument("--no-pull", action="store_true")
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("fill", help="board candidates for a self-filling role (optionally claim the top)")
    common(p, slug=True)
    p.add_argument("--limit", type=int, default=5)
    p.add_argument("--claim-top", action="store_true")
    p.add_argument("--force", action="store_true")
    p.add_argument("--item", default="")
    p.add_argument("--base", default="")
    p.add_argument("--verb", default="")
    p.add_argument("--src", default="")
    p.set_defaults(func=cmd_fill)

    p = sub.add_parser("sync", help="pull + push pending ledger commits")
    common(p)
    p.set_defaults(func=cmd_sync)

    p = sub.add_parser("ledger-init", help="create shoals/<id>/ in the ledger repo")
    p.add_argument("--spec", required=True)
    p.add_argument("--ledger-repo", "--repo", dest="repo", default="",
                   help="ledger repo url (default: config `ledger_repo`; required when that is empty)")
    p.add_argument("--ledger-dir", default="")
    p.add_argument("--force", action="store_true", help="re-snapshot the spec into an existing ledger")
    p.add_argument("--no-sync", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_ledger_init)

    p = sub.add_parser("ledger-lint", help="violations, contested claims, missing verbs/bases")
    common(p)
    p.add_argument("--no-pull", action="store_true")
    p.set_defaults(func=cmd_ledger_lint)
