#!/usr/bin/env python3
"""Heartbeat stall daemon (proposal §5, v1).

Contract (sessions are briefed on it): every row-state change goes through
`hag.py transition`, which touches `/tmp/<hag>/hb-<slug>` and writes or
clears `/tmp/<hag>/wait-<slug>`. The daemon (systemd timer, every 5 min)
classifies each role:

  fresh       heartbeat within threshold                          -> quiet
  covered     stale heartbeat, but an UNEXPIRED wait file exists  -> quiet
  covered*    consumer role whose queue STATUS names a waiting-on holder
              (legacy coverage for consumers that only edit STATUS) -> quiet
  stale-wait  wait file past its rescan_utc (failure-modes §2)    -> SUSPECT
  stale       heartbeat past threshold, no wait                   -> SUSPECT
  never       no heartbeat file at all                            -> SUSPECT
              (grace: quiet for the first threshold after generation start)

v0 fired the same suspects every 5 minutes. v1 dedupes: the set of suspects
is hashed into /tmp/<hag>/heartbeat.state and re-fired only when it changes
or after --refire-min (default 60). Every run ticks /tmp/<hag>/tick-heartbeat
so `status`/`doctor` can see the daemon itself is alive (a watcher that
never runs is the failure mode the v0 reconcile demonstrated).

Delivery (ledger mode, 2026-09-20): one ledger `msg kind=stall-suspect`
to EACH suspect role (its own watcher wakes it to re-verify its wait or
transition — self-recovery without a coordinator) plus one to every
filler/coordinator role and to `user`. Legacy (no ledger): session send to
the coordinator, or channel.md when flattened. Telemetry watch_event
either way. Ledger `state` events count as freshness for roles on other
hosts (no /tmp heartbeat reaches across hosts).
"""
from __future__ import annotations

import datetime
import hashlib
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from backends import get as get_backend  # noqa: E402
from roles import HAG_ROOT, HagSpec, state_dir  # noqa: E402

HB_PREFIX = "hb-"


def hb_path(hag: str, slug: str) -> str:
    return os.path.join(state_dir(hag), f"{HB_PREFIX}{slug}")


def touch(hag: str, slug: str) -> str:
    """Session-side tick. Returns the path touched."""
    p = hb_path(hag, slug)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "a"):
        os.utime(p, None)
    return p


def tick(hag: str, job: str) -> str:
    """Watcher/daemon self-tick: /tmp/<hag>/tick-<job>."""
    p = os.path.join(state_dir(hag), f"tick-{job}")
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


def status_updated_min(queue_file: str) -> float | None:
    """Minutes since the consumer's STATUS updated-utc (None if unparseable).

    A consumer that writes STATUS is alive by definition — a fresh STATUS
    covers a stale heartbeat the same way a verified wait does. (Learned
    2026-09-19: it-architect wrote STATUS at 23:14Z with a 16:12Z heartbeat
    and read as a stall; it was healthy and polling.)
    """
    try:
        text = open(queue_file).read()
        status = text[text.index("## STATUS"):]
    except Exception:
        return None
    m = re.search(r"^- updated-utc:\s*(\S+)", status, flags=re.M)
    if not m:
        return None
    raw = m.group(1).strip()
    try:
        if re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}Z$", raw):
            ts = datetime.datetime.strptime(raw, "%Y-%m-%dT%H:%MZ")
        else:
            ts = datetime.datetime.strptime(raw, "%Y-%m-%dT%H:%M:%SZ")
        ts = ts.replace(tzinfo=datetime.timezone.utc)
    except ValueError:
        return None
    return (datetime.datetime.now(datetime.timezone.utc) - ts).total_seconds() / 60


def status_wait_cover(queue_file: str) -> str | None:
    """Return the covering wait reason if STATUS shows a verified wait."""
    try:
        text = open(queue_file).read()
        status = text[text.index("## STATUS"):]
    except Exception:
        return None
    state = re.search(r"^- state:\s*(\S+)", status, flags=re.M)
    holder = re.search(r"^- waiting-on:\s*(.+)", status, flags=re.M)
    if holder and holder.group(1).strip().lower() not in ("", "none", "-"):
        return holder.group(1).strip()
    if state and state.group(1).strip() == "waiting" and holder:
        return holder.group(1).strip() or "waiting (no holder named)"
    return None


def watch_dead(hag: str, slug: str) -> bool:
    """True when the role's ledger-watch sidecar is absent or its pid is gone.

    A dead watch is deafness, not quietness: board polls stop, so a fill=self
    role with an empty queue idles forever while heartbeat reads mere idle.
    """
    try:
        from ledger_watch import load_sidecar, sidecar_path
    except Exception:
        return False
    sc = load_sidecar(sidecar_path(hag, slug))
    if not sc:
        return True
    pid = sc.get("pid", "")
    return not (pid and os.path.exists(f"/proc/{pid}"))


def _watch_last_tick(hag: str, slug: str) -> str:
    try:
        from ledger_watch import load_sidecar, sidecar_path
        return load_sidecar(sidecar_path(hag, slug)).get("last_tick", "never")
    except Exception:
        return "never"


def classify(spec: HagSpec, threshold_min: float) -> list[dict]:
    """One row per role: {slug, klass, age_min, detail, suspect: bool}."""
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
    if spec.ledger_mode:
        try:
            from ledger import Ledger, View
            led = Ledger(spec.hag, root=spec.ledger_dir)
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
        except Exception:  # noqa: BLE001
            pass
    for role in spec.roles:
        age = age_min(hb_path(spec.hag, role.slug))
        la = ledger_age.get(role.slug)
        if la is not None and (age is None or la < age):
            age = la  # a ledger write is a heartbeat (works across hosts)
        wstate, w = wait_status(spec.hag, role.slug)
        row = {"slug": role.slug, "age_min": age, "wait": w, "detail": ""}
        if age is None:
            if gen_age is not None and gen_age <= threshold_min:
                row.update(klass="never", suspect=False,
                           detail=f"no heartbeat yet (generation {gen_age:.0f}m old, grace)")
            else:
                row.update(klass="never", suspect=True, detail="NO HEARTBEAT FILE (never ticked)")
        elif wstate == "stale":
            row.update(klass="stale-wait", suspect=True,
                       detail=f"STALE WAIT past rescan {w.get('rescan_utc')} "
                              f"(holder: {w.get('holder')}, sub: {w.get('sub')}) — re-verify, never extend")
        elif age <= threshold_min:
            row.update(klass="fresh", suspect=False, detail=f"ok ({age:.0f}m)")
        elif wstate == "covered":
            row.update(klass="covered", suspect=False,
                       detail=f"stale {age:.0f}m but wait proven until {w.get('rescan_utc')} "
                              f"(holder: {w.get('holder')})")
        else:
            cover = status_wait_cover(role.queue) if role.queue else None
            if cover:
                row.update(klass="covered*", suspect=False,
                           detail=f"stale {age:.0f}m but STATUS waiting-on: {cover}")
            else:
                smin = status_updated_min(role.queue) if role.queue else None
                if smin is not None and smin <= threshold_min:
                    row.update(klass="covered*", suspect=False,
                               detail=f"stale {age:.0f}m but STATUS written "
                                      f"{smin:.0f}m ago (alive, no wait declared)")
                    rows.append(row)
                    continue
                deaf = (spec.ledger_mode
                        and getattr(role, "fill", "") == "self"
                        and watch_dead(spec.hag, role.slug))
                if deaf:
                    row.update(klass="idle-deaf", suspect=True,
                               detail=f"IDLE with empty queue and dead ledger-watch "
                                      f"(fill=self, no board poll since "
                                      f"{_watch_last_tick(spec.hag, role.slug)}): "
                                      f"WAKEUP-READ needed — show, claim/decline, re-arm")
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
    return rows


def cmd_heartbeat(spec, threshold_min: float, notify: str = "",
                  dry_run: bool = False, refire_min: float = 60) -> int:
    assert isinstance(spec, HagSpec)
    from watchers import hag_emit
    tick(spec.hag, "heartbeat")
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
    sfile = os.path.join(state_dir(spec.hag), "heartbeat.state")
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
    hag_emit(spec.hag, notify or "heartbeat", "script", "watch_event",
             kind="stall-suspect", note="; ".join(suspects),
             channel="within-ecosystem" if spec.coordinator else "shared-channel")
    if dry_run:
        print("[dry-run] would fire + record")
        return 0
    with open(sfile, "w") as fh:
        json.dump({"key": key, "fired_utc": now.strftime("%Y-%m-%dT%H:%M:%SZ")}, fh)
    if spec.ledger_mode:
        try:
            from ledger import Ledger
            led = Ledger(spec.hag, root=spec.ledger_dir)
            led.pull()
            targets = {r["slug"] for r in rows if r["suspect"]} | {r.slug for r in spec.fillers} | {"user"}
            for t in sorted(targets):
                mine = [r["detail"] for r in rows if r["suspect"] and r["slug"] == t]
                led.append("hag.py", "msg", to=t, kind="stall-suspect",
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
                    f"no board poll, queue empty, heartbeat stale): run hag.py show "
                    f"--hag {spec.hag} --slug {role.slug}, claim or decline the top "
                    f"board candidate, transition, re-arm ledger-watch and VERIFY "
                    f"the new PID with ps -p plus sidecar pid match. REPORT.")
                print(f"direct WAKEUP-READ sent to {role.slug} ({role.session})")
            except Exception as exc:  # noqa: BLE001
                print(f"ERROR: WAKEUP-READ send to {role.slug} failed: {exc}")
        return 0
    coord = spec.coordinator
    target = notify or (coord.session if coord else "")
    if coord and target:
        try:
            get_backend("opencode").send(target, spec.dir, body)
        except Exception as exc:  # noqa: BLE001
            print(f"ERROR: send to {target} failed: {exc}")
            return 2
    else:
        chan = os.path.join(HAG_ROOT, spec.hag, "channel.md")
        os.makedirs(os.path.dirname(chan), exist_ok=True)
        with open(chan, "a") as fh:
            fh.write(f"\n## {now:%Y-%m-%dT%H:%M:%SZ} {body}\n")
        print(f"flattened: appended to {chan}")
    return 0
