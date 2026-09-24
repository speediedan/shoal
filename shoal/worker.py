#!/usr/bin/env python3
"""The role's work loop: two verbs per boundary (architecture doc §3.2), plus user-offline mode.

    hag.py next  --hag H --slug S                        # READ: everything this boundary needs, one call
    hag.py queue --hag H --slug S --active X:verb --next a,b,c [--done Y --url U] [--claim ..]
                 [--pass ..:class[:suggest=R]] [--wait SUB --holder .. --target .. --rescan N]
                 [--idle] [--ask TEXT] [--reason TEXT]   # WRITE: one ledger commit
    hag.py offline on|off --hag H [--until TS] [--note ..]

`queue` folds claim, triage passes, done and transition into ONE commit that ends in a new
version of the role's own `queue` event: `{version, active, next (<= 3), asks, reason}`. The
latest version IS the role's queue and posture; others influence it only through offers
(`--now` to interrupt a busy role) and withdrawals. Idle = no active item: only then does new
work wake the role.

User-offline mode (`offline on`) tells every role the maintainer is away: record blocking asks
with `queue --ask` instead of waiting on them, stack PRs, and proceed with unblocked work.
Roles waiting on the user are woken once so they pivot. `offline off` is the RETURN protocol:
it prints every open ask with a staleness hint (the GitHub state of the items the ask names, and
the ledger's done items), and wakes the roles holding asks to (1) re-verify each ask and drop the
ones that no longer apply (`queue --drop-ask <n>`), then (2) raise the rest first-person as
prompted question sets: one question per ask, at most four per set, as many sets as needed. `next`
repeats that instruction while the role holds asks and the maintainer is online.
"""
from __future__ import annotations

import argparse
import datetime
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "telemetry"))

from ledger import (DEFAULT_PRIO, QUEUE_NEXT_MAX, Ledger, LedgerError, View, fmt_item,  # noqa: E402
                    item_key, short, split_item, sync_push)
from roles import HagSpec, find_spec, state_dir  # noqa: E402

PASS_CLASSES = ("out-of-lane", "blocked", "needs-maintainer", "needs-hardware", "duplicate-or-stale")


def now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _load(a, pull: bool = True):
    spec = HagSpec.load(find_spec(a.hag, getattr(a, "spec", "")))
    led = Ledger(spec.hag, root=getattr(a, "ledger_dir", "") or spec.ledger_dir or None,
                 sync=not getattr(a, "no_sync", False))
    if not led.exists():
        raise LedgerError(f"no ledger for {spec.hag} at {led.ldir}")
    if pull and led.sync:
        led.pull()
    return spec, led, View(led, spec)


def offline_banner(view: View) -> str:
    m = view.user_offline()
    if m is None:
        return ""
    until = f" until {m['until']}" if m.get("until") else ""
    note = f" ({m['note']})" if m.get("note") else ""
    return (f"USER OFFLINE since {m['ts']}{until}{note}: do not wait on the maintainer. Record each "
            f"blocking auth/design ask with `queue --ask`, stack PRs where the work depends on an "
            f"unmerged one, and proceed with unblocked items. You will be woken when they return.")


ASK_REF = re.compile(r"(?<![\w/])(?:(?P<owner>[A-Za-z0-9_.-]+)/)?(?P<repo>[A-Za-z0-9_.-]+)?#(?P<n>\d+)")
ASKS_PER_SET = 4  # AskUserQuestion takes at most four questions per call
RETURN_PROTOCOL = (
    "the maintainer is online. (1) Re-verify every recorded ask against current state (merged or closed "
    "PRs, finished items, rulings already given) and drop each one that no longer applies with `queue "
    "--drop-ask <n>`; the LIKELY-STALE hints are a starting point, not a verdict. (2) Raise the rest "
    "first-person as prompted question sets (AskUserQuestion / the question tool): one question per ask, "
    f"at most {ASKS_PER_SET} per set, as many sets as needed, each with grant / deny / defer options and "
    "your stacked-work option. (3) Drop each ask as it is answered.")


def ask_refs(ask: str, default_repo: str, known: dict[str, str] | None = None) -> list[str]:
    """`owner/repo#n` for every issue/PR reference in an ask. `#n` alone means the HAG's first
    repo; a bare `Repo#n` resolves through `known` (repo name -> owner/repo) and is skipped when
    unknown, never guessed."""
    out = []
    known = {k.lower(): v for k, v in (known or {}).items()}
    for m in ASK_REF.finditer(ask or ""):
        if m.group("owner") and m.group("repo"):
            full = f"{m.group('owner')}/{m.group('repo')}"
        elif m.group("repo"):
            full = known.get(m.group("repo").lower(), "")
        else:
            full = default_repo
        if full and f"{full}#{m.group('n')}" not in out:
            out.append(f"{full}#{m.group('n')}")
    return out


def known_repos(spec: HagSpec) -> dict[str, str]:
    """Repo name -> owner/repo from the spec and the board digest's items."""
    out = {r.split("/", 1)[-1]: r for r in spec.effective_repos if "/" in r}
    try:
        from digest import load
        doc = load(spec.hag, max_age_min=1e9) or {}
        for it in (doc.get("items") or {}).values():
            if "/" in it.get("repo", ""):
                out.setdefault(it["repo"].split("/", 1)[1], it["repo"])
    except Exception:  # noqa: BLE001
        pass
    return out


_REF_STATE: dict[str, str] = {}


def ref_state(ref: str) -> str:
    """OPEN | CLOSED | MERGED | UNKNOWN for one issue or PR (cached per process).
    HAG_FAKE_REF_STATE (JSON {ref: state}) replaces GitHub in selftests."""
    fake = os.environ.get("HAG_FAKE_REF_STATE")
    if fake:
        import json
        return json.loads(fake).get(ref, "UNKNOWN")
    if ref not in _REF_STATE:
        import subprocess
        repo, _, n = ref.partition("#")
        try:
            r = subprocess.run(["gh", "api", f"repos/{repo}/issues/{n}", "--jq",
                                '.state + (if .pull_request.merged_at then " merged" else "" end)'],
                               capture_output=True, text=True, timeout=30)
            out = r.stdout.strip().lower()
            _REF_STATE[ref] = ("MERGED" if out.endswith("merged") else out.upper()) if r.returncode == 0 and out else "UNKNOWN"
        except Exception:  # noqa: BLE001
            _REF_STATE[ref] = "UNKNOWN"
    return _REF_STATE[ref]


def ask_hint(spec: HagSpec, view: View, ask: str) -> str:
    """` (LIKELY STALE: ...)` when an item the ask names is merged, closed or done; else ``."""
    default = (spec.effective_repos or ["speediedan/interpretune"])[0]
    why = []
    for ref in ask_refs(ask, default, known_repos(spec)):
        if item_key(ref) in view.done:
            why.append(f"{ref.split('/', 1)[1]} done by {view.done[item_key(ref)]['by']}")
            continue
        st = ref_state(ref)
        if st in ("MERGED", "CLOSED"):
            why.append(f"{ref.split('/', 1)[1]} {st.lower()}")
    return f" (LIKELY STALE: {'; '.join(why)})" if why else ""


def candidates_for(spec: HagSpec, view: View, slug: str, exclude: set[str], limit: int) -> list[str]:
    taken = view.taken() | view.offered_open() | exclude
    declined = set(view.declined(slug))
    try:
        if (spec.features or {}).get("digest"):
            from digest import candidates as dc, fmt_candidate
            got = dc(spec.hag, slug, taken, declined, limit=limit)
            if got is not None:
                return [fmt_candidate(c, excerpt=160) for c in got]
        role = spec.role(slug)
        if role is None or role.fill != "self":
            return []
        from sources import candidates
        return [f"BOARD {c['item']}:{c['verb']} pos={c['pos']} :: {short(c['title'], 80)}"
                for c in candidates(spec.sources, role.scope, taken, declined, limit=limit)]
    except Exception as exc:  # noqa: BLE001
        return [f"(candidates unavailable: {short(str(exc), 100)})"]


def cmd_next(a) -> int:
    try:
        spec, led, view = _load(a)
    except (LedgerError, OSError) as exc:
        print(f"ERROR: {exc}")
        return 1
    slug = a.slug
    if spec.role(slug) is None:
        print(f"ERROR: {slug} is not a role in {spec.hag}")
        return 1
    me = spec.role(slug)
    out = [f"# next for {me.label} in HAG {spec.hag} (incarnation {spec.incarnation}) at {now_iso()}",
           f"IDENTITY: your Lane row label is `{me.label}` (base name, then your session tag; never the "
           f"incarnation number)"]
    banner = offline_banner(view)
    if banner:
        out.append(banner)
    q = view.current_queue(slug) or {}
    active = q.get("active") or ""
    nxt = list(q.get("next") or [])
    out.append(f"QUEUE v{q.get('version', 0)} ({q.get('ts', 'never written')}): active={active or '-'} "
               f"next={','.join(nxt) or '-'}" + (f" reason={q['reason']}" if q.get("reason") else ""))
    if q.get("asks"):
        online = view.user_offline() is None
        out.append("ASKS (open): " + " | ".join(
            f"[{i}] {x}" + (ask_hint(spec, view, x) if online else "") for i, x in enumerate(q["asks"], 1)))
        if online:
            out.append("ASKS TO RAISE: " + RETURN_PROTOCOL)
    # integrity of what the role holds or intends
    for it in ([active] if active else []) + nxt:
        key = item_key(it)
        w = view.winner(key)
        if key in view.done and view.done[key]["by"] != slug:
            out.append(f"GONE {it}: done by {view.done[key]['by']} {view.done[key]['ts']} (drop it)")
        elif w is not None and w["by"] != slug:
            out.append(f"TAKEN {it}: claimed by {w['by']} {w['ts']}"
                       + (" (you lost the race on your ACTIVE item: release kind=lost-race)" if it == active else " (drop it)"))
    held = view.held_unscheduled(slug) if q else [
        fmt_item(c) for c in view.active(slug) if item_key(c["item"]) != item_key(active)]
    if held:
        out.append(f"HELD-UNSCHEDULED {', '.join(held)}: you hold these claims but your queue does not list them "
                   f"(schedule them, or release them with --kind handoff|blocked so another role can take them)")
    for b in view.blocked_by(slug):
        mine = b["item"] and item_key(b["item"]) in {item_key(x) for x in [active] + nxt if x}
        out.append(f"BLOCKING {b['role']} since {b['since']} on {b['item'] or '(item unnamed)'}"
                   + ("" if mine else " -- NOT in your queue: schedule it (active or next) or say why in a pass"))
    st = view.role_state(slug) or {}
    if st.get("state") == "waiting" and st.get("rescan_utc") and st["rescan_utc"] < now_iso():
        out.append(f"RESCAN EXPIRED {st['rescan_utc']}: your {st.get('sub')} wait is unproven; re-verify it"
                   + (" and re-raise the prompted ask" if st.get("sub", "").startswith("user-") else "")
                   + ", then write a fresh `queue --wait` (or pivot to the next unblocked item)")
    offers = [o for o in view.queue(slug) if item_key(o["item"]) not in {item_key(x) for x in [active] + nxt if x}]
    for o in offers:
        out.append(f"OFFER {fmt_item(o)} prio={o.get('prio', DEFAULT_PRIO)} by={o['by']}"
                   + (" NOW" if o.get("now") else "") + (f" base={o['base']}" if o.get("base") else "")
                   + (f" :: {short(o.get('note', ''), 100)}" if o.get("note") else ""))
    if getattr(me, "aux", False):
        try:
            from aux import header as aux_header
            out.append(aux_header(spec, slug))
        except Exception as exc:  # noqa: BLE001
            out.append(f"AUX: (header unavailable: {short(str(exc), 100)})")
    short_by = QUEUE_NEXT_MAX - len(nxt) - (0 if active else 1)
    if short_by > 0:
        excl = {item_key(x) for x in [active] + nxt if x} | {item_key(o["item"]) for o in offers}
        for c in candidates_for(spec, view, slug, excl, limit=max(short_by, 3)):
            out.append(c)
    marker = os.path.join(state_dir(spec.hag), f"mail-read-{slug}")
    try:
        since = open(marker).read().strip()
    except OSError:
        since = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ")
    mail = view.msgs_to(slug, since_ts=since)
    if not getattr(a, "peek", False):  # a reviewer's read must not consume the role's mail
        os.makedirs(os.path.dirname(marker), exist_ok=True)
        with open(marker, "w") as fh:
            fh.write(max([since] + [m["ts"] for m in mail]) + "\n")
    if mail:
        out.append(f"MAIL {len(mail)} unread since your last `next` (act at this boundary; mail never wakes you):")
        for m in mail[-5:]:
            out.append(f"  {m['ts']} {m['by']}->{m['to']} [{m.get('kind', 'info')}] {short(m.get('text', ''), 140)}")
    try:
        from state import wait_status
        ws, w = wait_status(spec.hag, slug)
        if w:
            out.append(f"WAIT {ws}: {w.get('sub')} holder={w.get('holder')} target={w.get('target') or '-'} "
                       f"rescan={w.get('rescan_utc')}")
    except Exception:
        pass
    try:
        from ledger_watch import load_sidecar, sidecar_path
        sc = load_sidecar(sidecar_path(spec.hag, slug))
        pid = sc.get("pid", "")
        alive = bool(pid) and os.path.exists(f"/proc/{pid}")
        out.append(f"WATCH ledger-watch pid={pid or '-'} {'alive' if alive else 'NOT RUNNING: re-arm after this boundary'}")
    except Exception:
        pass
    out.append("# END EVERY TURN IN A SLEEP POSTURE: the last write of a turn is `queue --wait ..` (waiter armed) or "
               "`queue --idle`; `active` means you are still working. A turn that stops while active is woken to continue.")
    out.append(f"# then ONE write: hag.py queue --hag {spec.hag} --slug {slug} --active .. --next .. "
               f"[--done .. --url ..] [--claim ..] [--pass ..] [--wait ..|--idle]")
    print("\n".join(out))
    return 0


def _items(csv: str) -> list[str]:
    return [x.strip() for x in (csv or "").split(",") if x.strip()]


def cmd_queue(a) -> int:  # noqa: C901 - one verb, one commit, deliberately linear
    try:
        spec, led, view = _load(a)
    except (LedgerError, OSError) as exc:
        print(f"ERROR: {exc}")
        return 1
    slug = a.slug
    role = spec.role(slug)
    if role is None:
        print(f"ERROR: {slug} is not a role in {spec.hag}")
        return 1
    if a.idle and a.active:
        print("ERROR: --idle and --active are exclusive")
        return 1
    prev = view.current_queue(slug) or {}
    active = "" if a.idle else (a.active or (prev.get("active") or ""))
    nxt = _items(a.next) if a.next is not None else [x for x in (prev.get("next") or [])]
    if len(nxt) > QUEUE_NEXT_MAX:
        print(f"ERROR: at most {QUEUE_NEXT_MAX} next items (got {len(nxt)})")
        return 1
    if active and item_key(active) in {item_key(x) for x in nxt}:
        print("ERROR: the active item is also listed in --next")
        return 1
    # claims: explicit ones plus the active item when the role does not hold it yet
    claims = list(a.claim or [])
    if active and not any(item_key(c["item"]) == item_key(active) for c in view.active(slug)) \
            and item_key(active) not in {item_key(c) for c in claims}:
        claims.append(active)
    for c in claims:
        key = item_key(c)
        w = view.winner(key)
        if w is not None and w["by"] != slug:
            print(f"CLAIM-REFUSED {key}: held by {w['by']} since {w['ts']} (nothing written)")
            return 4
        if key in view.done:
            print(f"CLAIM-REFUSED {key}: done {view.done[key]['ts']} by {view.done[key]['by']} (nothing written)")
            return 4
        addressed = next((o["to"] for o in view.by_item.get(key, []) if o["status"] == "open"), None)
        if not view.perms.may_claim(slug, addressed, ""):
            print(f"CLAIM-REFUSED {key}: {slug} may not claim "
                  f"{'an unoffered item' if addressed is None else 'an offer to ' + addressed} (nothing written)")
            return 4
    passes = []
    for p in a.pass_ or []:
        parts = p.split(":")
        if len(parts) < 2 or parts[1] not in PASS_CLASSES:
            print(f"ERROR: --pass {p!r}: want ITEM:CLASS[:suggest=ROLE][:note=TEXT], CLASS in {PASS_CLASSES}")
            return 1
        rec = {"item": parts[0], "cls": parts[1], "suggest": "", "note": ""}
        for extra in parts[2:]:
            k, _, v = extra.partition("=")
            if k in ("suggest", "note"):
                rec[k] = v
        for s in filter(None, rec["suggest"].split(",")):
            if spec.role(s) is None:
                print(f"ERROR: suggest={s!r} is not a role in {spec.hag}")
                return 1
        passes.append(rec)
    if a.wait and not a.holder:
        print("ERROR: --wait needs --holder (who or what clears it)")
        return 1
    asks = [] if a.asks_clear else list(prev.get("asks") or [])
    if a.drop_ask:
        drop = set()
        for d in a.drop_ask:
            if d.isdigit() and 1 <= int(d) <= len(asks):
                drop.add(int(d) - 1)
            elif d in asks:
                drop.add(asks.index(d))
            else:
                print(f"ERROR: --drop-ask {d!r}: not an ask number (1..{len(asks)}) or ask text")
                return 1
        asks = [x for i, x in enumerate(asks) if i not in drop]
    asks += [x for x in (a.ask or []) if x not in asks]
    rescan_min = a.rescan_min
    if a.wait:
        import state as _st
        rescan_min, notes, refusal = _st.check_wait(spec, slug, a.wait, a.rescan_min, a.blocked_on or "", asks)
        for n in notes:
            print(f"note: {n}")
        if refusal:
            print(f"ERROR: {refusal} (nothing written)")
            return 3
    planned = {item_key(x) for x in [active] + nxt if x} | {item_key(c) for c in claims}
    for h in view.held_unscheduled(slug):
        if h not in planned:
            print(f"note: HELD-UNSCHEDULED {h}: you hold its claim but neither active nor next lists it; "
                  f"schedule it, or release it (`release --kind handoff|blocked`) so another role can take it")
    posture = "waiting" if a.wait else ("idle" if not active else "active")
    reason = a.reason or ("" if len(nxt) >= QUEUE_NEXT_MAX else "no further eligible items")
    version = int(prev.get("version", 0)) + 1
    plan = (f"{slug} queue v{version}: active={active or '-'} next={','.join(nxt) or '-'} posture={posture}"
            + (f"/{a.wait}" if a.wait else "") + f" claims={claims or '-'} done={a.done or '-'} "
            f"passes={[p['item'] + ':' + p['cls'] for p in passes] or '-'} asks={len(asks)}")
    if a.dry_run:
        print("[dry-run] " + plan)
        return 0
    # 1. local posture: heartbeat, wait proof, telemetry (the ledger state event rides in the batch)
    import state as _state
    tns = argparse.Namespace(
        hag=spec.hag, slug=slug, to=posture, sub=a.wait or "", holder=a.holder or "",
        rescan_min=rescan_min, monitor=a.monitor or "", target=a.target or "", from_state="", asks=asks,
        blocked_on=a.blocked_on or "",
        note=f"queue v{version}", channel="within-ecosystem", session="", ecosystem="", spec=getattr(a, "spec", ""),
        dir=None, no_ledger=True, ledger_dir="", dry_run=False)
    rc = _state.cmd_transition(tns)
    if rc != 0:
        return rc
    # 2. one ledger commit: done, claims, passes, state, queue
    sess = role.session
    for d in a.done or []:
        key, verb = split_item(d)
        led.append(slug, "done", _commit=False, item=key, verb=verb, kind=verb, url=a.url, sha=a.sha)
    for c in claims:
        key, verb = split_item(c)
        led.append(slug, "claim", _commit=False, item=key, verb=verb, src="queue", session=sess)
    for p in passes:
        led.append(slug, "release", _commit=False, item=item_key(p["item"]), kind="decline", cls=p["cls"],
                   suggest=p["suggest"] or None, reason=p["note"] or p["cls"])
    w = _state.read_wait(spec.hag, slug) if a.wait else None
    led.append(slug, "state", _commit=False, state=posture, sub=a.wait or "", holder=a.holder or "",
               rescan_utc=(w or {}).get("rescan_utc", ""), target=a.target or "", note=f"queue v{version}",
               blocked_on=(a.blocked_on or None) if a.wait else None, tag=role.tag,
               session=sess, host=os.uname().nodename)
    qev = led.append(slug, "queue", _commit=False, version=version, active=active or None, next=nxt,
                     asks=asks or None, reason=reason or None, posture=posture, session=sess, tag=role.tag)
    led.commit_own(slug, f"{spec.hag} {slug} queue v{version}")
    pushed = sync_push(led.root) if led.sync else True
    # 3. first-landed-wins re-check for the claims just made
    lost = []
    if claims:
        led.pull()
        v2 = View(led, spec)
        for c in claims:
            w2 = v2.winner(item_key(c))
            if w2 is not None and w2["by"] != slug:
                led.append(slug, "release", item=item_key(c), kind="lost-race", reason=f"{w2['by']} landed first [{w2['id']}]")
                lost.append(item_key(c))
    print(plan + f" [{qev['id']}]" + ("" if pushed else " (push pending: `hag.py sync`)")
          + (f" CLAIM-LOST {lost}: re-run `next` and repick" if lost else ""))
    if posture == "active":
        print("note: posture ACTIVE means you keep working in this turn. The LAST write of every turn is a sleep "
              "posture: `queue --wait <sub> --holder .. --target ..` (arm the waiter first) or `queue --idle`, or "
              "`--active <next item>` and continue. A turn that ends while active is woken to CONTINUE.")
    try:
        os.remove(os.path.join(state_dir(spec.hag), f"wake-{slug}"))  # a pending health wake is now answered
    except OSError:
        pass
    return 4 if lost else 0


def cmd_offline(a) -> int:
    try:
        spec, led, view = _load(a)
    except (LedgerError, OSError) as exc:
        print(f"ERROR: {exc}")
        return 1
    on = a.state == "on"
    cur = view.user_offline()
    if on and cur is not None and not a.force:
        print(f"already offline since {cur['ts']} (pass --force to restate)")
        return 0
    if not on and cur is None:
        print("maintainer already online; nothing to do")
        return 0
    # who to wake: going offline -> roles waiting on the user (so they pivot);
    # coming back -> roles holding open asks (so they raise them now)
    targets = []
    for r in spec.roles:
        q = view.current_queue(r.slug) or {}
        st = view.role_state(r.slug) or {}
        if on and st.get("state") == "waiting" and st.get("sub") in ("user-auth", "user-design", "user-feedback"):
            targets.append(r.slug)
        if not on and q.get("asks"):
            targets.append(r.slug)
    if a.dry_run:
        print(f"[dry-run] user-offline {a.state}; would wake {targets or 'nobody'}")
        return 0
    ev = led.append("user", "system", mode="user-offline", state=a.state, until=a.until or None, note=a.note or None)
    reason = ("USER-OFFLINE: the maintainer is away" if on else "USER-ONLINE: " + RETURN_PROTOCOL)
    from health import request_wake
    for s in targets:
        request_wake(spec, s, reason, condition=f"user-offline-{a.state}")
    print(f"user-offline {a.state} [{ev['id']}]; woke {targets or 'nobody'}")
    if not on:
        stale = total = 0
        for r in spec.roles:
            q = view.current_queue(r.slug) or {}
            for i, ask in enumerate(q.get("asks") or [], 1):
                hint = ask_hint(spec, view, ask)
                total += 1
                stale += bool(hint)
                print(f"  ASK {r.slug} [{i}]: {ask}{hint}")
        if total:
            print(f"{total} recorded ask(s), {stale} likely stale. Each woken role re-verifies its asks, drops the "
                  f"stale ones, and raises the rest to you as prompted question sets (at most {ASKS_PER_SET} "
                  f"per set).")
    return 0


def add_parsers(sub) -> None:
    p = sub.add_parser("next", help="READ at a boundary: queue, offers, candidates, mail, waits (one call)")
    p.add_argument("--hag", required=True)
    p.add_argument("--slug", required=True)
    p.add_argument("--peek", action="store_true", help="read-only view for operators/reviewers: mail stays unread")
    p.add_argument("--spec", default="")
    p.add_argument("--no-sync", action="store_true")
    p.set_defaults(func=cmd_next)

    p = sub.add_parser("queue", help="WRITE at a boundary: done/claims/passes/posture + queue version, one commit")
    p.add_argument("--hag", required=True)
    p.add_argument("--slug", required=True)
    p.add_argument("--spec", default="")
    p.add_argument("--active", default="", help="the item worked now, repo#n:verb (claimed if not held)")
    p.add_argument("--next", default=None, help=f"comma list, at most {QUEUE_NEXT_MAX}; omit to keep the previous")
    p.add_argument("--idle", action="store_true", help="no active item (new work may wake you)")
    p.add_argument("--done", action="append", help="repo#n:verb completed at this boundary (repeatable)")
    p.add_argument("--url", default="", help="evidence URL for --done")
    p.add_argument("--sha", default="")
    p.add_argument("--claim", action="append", help="extra claims (repeatable)")
    p.add_argument("--pass", dest="pass_", action="append", help="ITEM:CLASS[:suggest=ROLE][:note=TEXT]")
    p.add_argument("--wait", default="", help="waiting substate: local-ci|remote-ci|session-blocked|user-auth|...")
    p.add_argument("--holder", default="")
    p.add_argument("--target", default="", help="lease:<key>|job:<unit>|gh-run:<repo>:<id>|gh-pr:<repo>#<n>|az-build:<id>")
    p.add_argument("--rescan-min", type=float, default=90)
    p.add_argument("--blocked-on", default="", help="session-blocked waits: the blocking role[:repo#n]")
    p.add_argument("--monitor", default="")
    p.add_argument("--ask", action="append", help="record a blocking maintainer ask (user-offline mode)")
    p.add_argument("--asks-clear", action="store_true", help="drop all recorded asks (they were answered)")
    p.add_argument("--drop-ask", action="append", default=[], metavar="N|TEXT",
                   help="drop one recorded ask by its number in `next` (1-based) or its text (repeatable)")
    p.add_argument("--reason", default="", help="why next is shorter than the maximum")
    p.add_argument("--no-sync", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_queue)

    p = sub.add_parser("offline", help="user-offline mode for every role: on|off")
    p.add_argument("state", choices=["on", "off"])
    p.add_argument("--hag", required=True)
    p.add_argument("--spec", default="")
    p.add_argument("--until", default="", help="expected return (free text or ISO time)")
    p.add_argument("--note", default="")
    p.add_argument("--force", action="store_true")
    p.add_argument("--no-sync", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_offline)
