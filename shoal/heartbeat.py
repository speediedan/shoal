#!/usr/bin/env python3
"""Heartbeat stall daemon (proposal §5, v1).

Contract (sessions are briefed on it): every row-state change goes through
`shoal transition`, which touches `/tmp/<id>/hb-<slug>` and writes or
clears `/tmp/<id>/wait-<slug>`. The daemon (systemd timer, every 5 min)
classifies each role:

  fresh       heartbeat within threshold                          -> quiet
  covered     stale heartbeat, but an UNEXPIRED wait file exists  -> quiet
  stale-wait  wait file past its rescan_utc (failure-modes §2)    -> SUSPECT
  stale       heartbeat past threshold, no wait                   -> SUSPECT
  never       no heartbeat file at all                            -> SUSPECT
              (grace: quiet for the first threshold after generation start)

v0 fired the same suspects every 5 minutes. v1 dedupes: the set of suspects
is hashed into /tmp/<id>/heartbeat.state and re-fired only when it changes
or after --refire-min (default 60). Every run ticks /tmp/<id>/tick-heartbeat
so `status`/`doctor` can see the daemon itself is alive (a watcher that
never runs is the failure mode the v0 reconcile demonstrated).

Delivery (2026-09-20): one ledger `msg kind=stall-suspect` to EACH suspect
role (its own watcher wakes it to re-verify its wait or transition:
self-recovery) plus one to `user`, and a telemetry watch_event. Ledger
`state` events count as freshness for roles on other hosts (no /tmp
heartbeat reaches across hosts). Specs with `features.health` run the
health quorum (health.py) instead of this daemon; `doctor` still uses
`classify` for its per-role posture lines.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ledger import SYSTEM_WRITER  # noqa: E402
from backends import get as get_backend  # noqa: E402
from roles import ShoalSpec, state_dir  # noqa: E402

HB_PREFIX = "hb-"


def hb_path(sid: str, slug: str) -> str:
    return os.path.join(state_dir(sid), f"{HB_PREFIX}{slug}")


def touch(sid: str, slug: str) -> str:
    """Session-side tick. Returns the path touched."""
    p = hb_path(sid, slug)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "a"):
        os.utime(p, None)
    return p


def tick(sid: str, job: str) -> str:
    """Watcher/daemon self-tick: /tmp/<id>/tick-<job>."""
    p = os.path.join(state_dir(sid), f"tick-{job}")
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "a"):
        os.utime(p, None)
    return p


def age_min(path: str) -> float | None:
    try:
        return (datetime.datetime.now().timestamp()
                - os.path.getmtime(path)) / 60
    except OSError:
        return None


def watch_dead(sid: str, slug: str) -> bool:
    """True when the role's ledger-watch sidecar is absent or its pid is gone.

    A dead watch is deafness, not quietness: board polls stop, so a fill=self
    role with an empty queue idles forever while heartbeat reads mere idle.
    """
    try:
        from ledger_watch import load_sidecar, sidecar_path
    except Exception:
        return False
    sc = load_sidecar(sidecar_path(sid, slug))
    if not sc:
        return True
    pid = sc.get("pid", "")
    return not (pid and os.path.exists(f"/proc/{pid}"))


def quota_paused(role, repo_dir: str = "") -> str:
    """Dispatch to the role ecosystem's backend (see backends.quota_paused)."""
    try:
        return get_backend(role.ecosystem).quota_paused(role.session or "", repo_dir)
    except Exception:
        return ""


def watches_path(sid: str, slug: str) -> str:
    return os.path.join(state_dir(sid), "watches", f"{slug}.json")


def register_watch(sid: str, slug: str, kind: str, target: str, pid: str,
                   session: str, rescan_min: float = 0) -> str:
    """Record one armed per-session monitor. Sessions call this at arm time
    (alongside the plan_state inventory); the daemon validates liveness and
    the +50% rescan rule against the registry."""
    p = watches_path(sid, slug)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    try:
        reg = json.load(open(p))
    except Exception:
        reg = {"watches": []}
    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    reg["watches"] = [w for w in reg.get("watches", [])
                      if not (w.get("kind") == kind and w.get("target") == target)]
    reg["watches"].append({"kind": kind, "target": target, "pid": str(pid),
                           "session": session, "armed_utc": now,
                           "rescan_min": rescan_min})
    with open(p, "w") as fh:
        json.dump(reg, fh, indent=1)
    return p


def registry_findings(sid: str, slug: str) -> list[str]:
    """Invalid registered watches: dead pid, or rescan exceeded by >50%."""
    try:
        reg = json.load(open(watches_path(sid, slug)))
    except Exception:
        return []
    out = []
    now = datetime.datetime.now(datetime.timezone.utc)
    for w in reg.get("watches", []):
        pid = str(w.get("pid", ""))
        if not pid or not os.path.exists(f"/proc/{pid}"):
            out.append(f"watch dead: {w.get('kind')}:{w.get('target')} (pid {pid or '?'} gone)")
            continue
        try:
            armed = datetime.datetime.strptime(w.get("armed_utc", ""), "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=datetime.timezone.utc)
        except Exception:
            continue
        rm = float(w.get("rescan_min") or 0)
        if rm > 0 and (now - armed).total_seconds() > rm * 90:
            out.append(f"watch overdue: {w.get('kind')}:{w.get('target')} "
                       f"(armed {w.get('armed_utc')}, rescan {rm:g}m +50% lapsed)")
    return out


def _watch_last_tick(sid: str, slug: str) -> str:
    try:
        from ledger_watch import load_sidecar, sidecar_path
        return load_sidecar(sidecar_path(sid, slug)).get("last_tick", "never")
    except Exception:
        return "never"


def _stale_active(spec, role, last_state: dict, threshold_min: float,
                  now_utc) -> str | None:
    """STALE-ACTIVE detector (failure-modes §20): the ledger row says `active`
    but the session is provably between turns and no wait is proven — a turn
    ended (or crashed) without its closing transition.

    Returns the page detail, or None when the row is not stale-active. Only
    fires on positive idle evidence (opencode CLI `status:idle`); a busy
    session, a failed status query, or a non-opencode ecosystem never fires,
    so a long work burst can never false-page."""
    if role.ecosystem != "opencode" or not role.session:
        return None
    st, sts = last_state.get(role.slug, ("", ""))
    if st != "active" or not sts:
        return None
    try:
        stt = datetime.datetime.strptime(sts, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=datetime.timezone.utc)
    except ValueError:
        return None
    s_age = (now_utc - stt).total_seconds() / 60
    if s_age <= threshold_min:
        return None
    try:
        cli = get_backend("opencode").status(role.session, spec.dir)
    except Exception:
        return None
    if not cli or "status:idle" not in cli:
        return None
    return (f"STALE-ACTIVE: ledger row active since {sts} ({s_age:.0f}m), "
            f"session CLI-idle, no wait proven — the turn ended without a "
            f"closing transition: transition to waiting (reason + watchers) "
            f"or idle, or resume work. [{cli.strip()[:80]}]")


def classify(spec: ShoalSpec, threshold_min: float) -> list[dict]:
    """One row per role: {slug, klass, age_min, detail, suspect: bool}.

    Single paging criterion: a role pages IFF it is quiet (no ledger write
    AND no heartbeat for > threshold, past generation grace) AND an obligation
    is outstanding (stale wait, invalid registered watch, or idle-deaf
    self-filler). quota-paused never pages. Klasses are diagnosis; suspect is
    the page bit."""
    from state import wait_status  # local import: state imports heartbeat
    gen = spec.current_generation() or {}
    gen_age = None
    try:
        started = datetime.datetime.strptime(gen.get("started_utc", ""), "%Y-%m-%dT%H:%M:%SZ")
        gen_age = (datetime.datetime.utcnow() - started).total_seconds() / 60
    except Exception:
        pass
    rows = []
    ledger_age: dict[str, float] = {}
    last_state: dict[str, tuple[str, str]] = {}  # slug -> (state, ts) of last state event
    now_utc = datetime.datetime.now(datetime.timezone.utc)
    try:
        from ledger import Ledger, View
        led = Ledger(spec.sid, root=spec.ledger_dir)
        if led.exists():
            led.pull()
            v = View(led, spec)
            now = datetime.datetime.now(datetime.timezone.utc)
            for slug, ts in v.last_by.items():
                try:
                    t = datetime.datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc)
                    ledger_age[slug] = (now - t).total_seconds() / 60
                except ValueError:
                    pass
            for slug, ev in v.states.items():
                if isinstance(ev, dict) and ev.get("state"):
                    last_state[slug] = (ev["state"], ev.get("ts", ""))
    except Exception:  # noqa: BLE001
        pass
    for role in spec.roles:
        age = age_min(hb_path(spec.sid, role.slug))
        la = ledger_age.get(role.slug)
        if la is not None and (age is None or la < age):
            age = la  # a ledger write is a heartbeat (works across hosts)
        wstate, w = wait_status(spec.sid, role.slug)
        row = {"slug": role.slug, "age_min": age, "wait": w, "detail": ""}
        if age is None:
            if gen_age is not None and gen_age <= threshold_min:
                row.update(klass="never", suspect=False,
                           detail=f"no heartbeat yet (generation {gen_age:.0f}m old, grace)")
            else:
                row.update(klass="never", suspect=True, detail="NO HEARTBEAT FILE (never ticked)")
        elif wstate == "stale":
            if role.session:
                q = quota_paused(role, spec.dir)
                if q:
                    row.update(klass="quota-paused", suspect=False,
                               detail=f"provider quota exhausted (reset: {q}) — wait "
                                      f"re-verification deferred, not paged, not evictable")
                    rows.append(row)
                    continue
            row.update(klass="stale-wait", suspect=True,
                       detail=f"STALE WAIT past rescan {w.get('rescan_utc')} "
                              f"(holder: {w.get('holder')}, sub: {w.get('sub')}) — re-verify, never extend")
        else:
            stale_active = None
            if wstate != "covered":
                stale_active = _stale_active(spec, role, last_state,
                                             threshold_min, now_utc)
            if stale_active is not None:
                row.update(klass="stale-active", suspect=True, detail=stale_active)
            elif age is not None and age <= threshold_min:
                row.update(klass="fresh", suspect=False, detail=f"ok ({age:.0f}m)")
            elif wstate == "covered":
                row.update(klass="covered", suspect=False,
                           detail=f"stale {age:.0f}m but wait proven until {w.get('rescan_utc')} "
                                  f"(holder: {w.get('holder')})")
            else:
                deaf = (getattr(role, "fill", "") == "self"
                        and watch_dead(spec.sid, role.slug))
                if deaf:
                    row.update(klass="idle-deaf", suspect=True,
                                detail=f"IDLE with empty queue and dead ledger-watch "
                                       f"(fill=self, no board poll since "
                                       f"{_watch_last_tick(spec.sid, role.slug)}): "
                                       f"WAKEUP-READ needed — show, claim/decline, re-arm")
                    rows.append(row)
                    continue
                if role.session:
                    q = quota_paused(role, spec.dir)
                    if q:
                        row.update(klass="quota-paused", suspect=False,
                                    detail=f"provider quota exhausted (reset: {q}) — not a stall, "
                                           f"not evictable;suppress pages until reset")
                        rows.append(row)
                        continue
                extra = ""
                if role.ecosystem == "opencode" and role.session:
                    try:
                        extra = get_backend("opencode").status(role.session, spec.dir)
                    except Exception as exc:  # noqa: BLE001
                        extra = f"status query failed: {exc}"
                row.update(klass="stale", suspect=True,
                            detail=f"STALE {age:.0f}m > {threshold_min:.0f}m, no wait proven"
                                   + (f" [{extra}]" if extra else ""))
        rows.append(row)
    for row in rows:
        if row["suspect"] or row.get("klass") == "quota-paused":
            continue
        quiet = row["age_min"] is None or row["age_min"] > threshold_min
        if not quiet:
            continue
        findings = registry_findings(spec.sid, row["slug"])
        if findings:
            row.update(klass="watch-invalid", suspect=True,
                       detail="; ".join(findings) + " — re-arm or re-register")
    return rows


def cmd_heartbeat(spec, threshold_min: float,
                  dry_run: bool = False, refire_min: float = 60) -> int:
    assert isinstance(spec, ShoalSpec)
    from watchers import shoal_emit
    tick(spec.sid, "heartbeat")
    rows = classify(spec, threshold_min)
    for r in rows:
        print(f"{r['slug']}: {r['klass']} — {r['detail']}")
    suspects = [f"{r['slug']}: {r['detail']}" for r in rows if r["suspect"]]
    if not suspects:
        print("heartbeat: all covered/fresh")
        return 0
    body = "STALL-SUSPECT: " + " ; ".join(suspects)
    # dedupe: fire on change of the suspect SET (by slug+klass), or after refire_min
    key = hashlib.md5("|".join(sorted(f"{r['slug']}:{r['klass']}" for r in rows
                                       if r["suspect"])).encode()).hexdigest()
    sfile = os.path.join(state_dir(spec.sid), "heartbeat.state")
    prev = {}
    try:
        prev = json.load(open(sfile))
    except Exception:
        prev = {}
    now = datetime.datetime.now(datetime.timezone.utc)
    last_fire = None
    try:
        last_fire = datetime.datetime.strptime(prev.get("fired_utc", ""), "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=datetime.timezone.utc)
    except Exception:
        pass
    same = prev.get("key") == key
    recent = last_fire is not None and (now - last_fire).total_seconds() < refire_min * 60
    if same and recent:
        print(f"(suppressed: same suspect set fired {((now - last_fire).total_seconds() / 60):.0f}m ago; "
              f"refire after {refire_min:g}m)")
        return 0
    print(body)
    if dry_run:
        print("[dry-run] would fire + record")
        return 0
    shoal_emit(spec.sid, "heartbeat", "script", "watch_event",
             kind="stall-suspect", note="; ".join(suspects),
             tier=("T3" if any(r.get("klass") == "idle-deaf" and getattr(spec.role(r["slug"]), "ecosystem", "")
                               == "claude" for r in rows) else "T2"),
             channel="shared-channel")
    with open(sfile, "w") as fh:
        json.dump({"key": key, "fired_utc": now.strftime("%Y-%m-%dT%H:%M:%SZ")}, fh)
    try:
        from ledger import Ledger
        led = Ledger(spec.sid, root=spec.ledger_dir)
        led.pull()
        targets = {r["slug"] for r in rows if r["suspect"]} | {"user"}
        for t in sorted(targets):
            mine = [r["detail"] for r in rows if r["suspect"] and r["slug"] == t]
            led.append(SYSTEM_WRITER, "msg", to=t, kind="stall-suspect",
                       text=("; ".join(mine) + " — re-verify your wait (transition) or act"
                             if mine else body), _push=False)
        from ledger import sync_push
        sync_push(led.root)
        print(f"ledger: stall-suspect msgs to {sorted(targets)}")
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: ledger delivery failed: {exc}")
        return 2
    for r in rows:
        if r.get("klass") != "idle-deaf":
            continue
        role = spec.role(r["slug"])
        if role is None or role.ecosystem != "opencode" or not role.session:
            continue
        try:
            get_backend("opencode").send(
                role.session, spec.dir,
                f"OPERATOR WAKEUP-READ (heartbeat: your ledger-watch is dead — "
                f"no board poll, queue empty, heartbeat stale): run shoal show "
                f"--shoal {spec.sid} --slug {role.slug}, claim or decline the top "
                f"board candidate, transition, re-arm ledger-watch and VERIFY "
                f"the new PID with ps -p plus sidecar pid match. REPORT.")
            print(f"direct WAKEUP-READ sent to {role.slug} ({role.session})")
        except Exception as exc:  # noqa: BLE001
            print(f"ERROR: WAKEUP-READ send to {role.slug} failed: {exc}")
    return 0
