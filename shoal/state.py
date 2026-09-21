#!/usr/bin/env python3
"""Session-side state primitive: `hag.py transition`.

ONE command per row-state change, whatever the ecosystem. It does the three
things every session was previously asked to do separately (and measurably
did not): tick the heartbeat, prove or clear a wait, emit the telemetry
event. Sessions that forget one of three forget it silently; a single
command cannot be half-done.

  hag.py transition --hag H --slug S --to active [--note ...]
  hag.py transition --hag H --slug S --to waiting --sub remote-ci \
      --holder "gate 1290 (approved, queued)" --rescan-min 30 [--monitor ...]
  hag.py transition --hag H --slug S --to idle|paused|offline

Ledger mode (2026-09-20): the same command also appends a `state` event to
the HAG ledger (ledger.py), so every peer and every host sees the row state
without a coordinator transcribing it; `--no-ledger` skips that (tests).

Wait files (`/tmp/<hag>/wait-<slug>`, JSON) are the lane-side wait proof
the v0 heartbeat daemon lacked: a wait is covered while it is unexpired
(now < rescan_utc); past its rescan time it is a STALE WAIT (failure-modes
§2: re-verify, never extend) and the daemon says so. Transitioning to any
non-waiting state clears the file. Session ids resolve from the spec by
slug when --session is not given, so briefs never have to carry them.
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "telemetry"))

from roles import HagSpec, find_spec, state_dir  # noqa: E402
from heartbeat import touch as hb_touch  # noqa: E402

try:
    from hag_telemetry import (VALID_STATES, VALID_SUB, ALIASES, append_event,  # noqa: E402
                               build_event, resolve_dir)
except Exception:  # pragma: no cover
    VALID_STATES, VALID_SUB, ALIASES = set(), set(), {}
    append_event = build_event = resolve_dir = None


def wait_path(hag: str, slug: str) -> str:
    return os.path.join(state_dir(hag), f"wait-{slug}")


def read_wait(hag: str, slug: str) -> dict | None:
    try:
        with open(wait_path(hag, slug)) as fh:
            return json.load(fh)
    except Exception:
        return None


def wait_status(hag: str, slug: str, now: datetime.datetime | None = None) -> tuple[str, dict | None]:
    """('none' | 'covered' | 'stale', wait) for the slug's wait file."""
    w = read_wait(hag, slug)
    if not w:
        return "none", None
    now = now or datetime.datetime.now(datetime.timezone.utc)
    try:
        rescan = datetime.datetime.strptime(w["rescan_utc"], "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=datetime.timezone.utc)
    except Exception:
        return "stale", w
    return ("covered" if now < rescan else "stale"), w


def write_wait(hag: str, slug: str, sub: str, holder: str, rescan_min: float,
               monitor: str, note: str) -> dict:
    now = datetime.datetime.now(datetime.timezone.utc)
    w = {"slug": slug, "sub": sub, "holder": holder, "monitor": monitor,
         "note": note, "since_utc": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
         "rescan_utc": (now + datetime.timedelta(minutes=rescan_min)).strftime("%Y-%m-%dT%H:%M:%SZ")}
    p = wait_path(hag, slug)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(w, fh)
    os.replace(tmp, p)
    return w


def clear_wait(hag: str, slug: str) -> bool:
    try:
        os.remove(wait_path(hag, slug))
        return True
    except OSError:
        return False


def cmd_transition(a: argparse.Namespace) -> int:
    hag = a.hag
    if a.to not in VALID_STATES:
        print(f"ERROR: --to must be one of {sorted(VALID_STATES)} (got {a.to!r}); "
              "invalid states are not emitted by this command")
        return 1
    sub, raw = a.sub or "", ""
    if a.to == "waiting":
        if not sub:
            print("ERROR: waiting needs --sub <local-ci|remote-ci|user-auth|user-design|"
                  "session-blocked|queue-empty>")
            return 1
        if sub not in VALID_SUB:
            if sub in ALIASES:
                raw, sub = sub, ALIASES[sub]
                print(f"note: --sub {raw} aliased to canonical {sub}")
            else:
                print(f"ERROR: unknown waiting substate {sub!r}; canonical: {sorted(VALID_SUB)}")
                return 1
        if not a.holder:
            print("ERROR: waiting needs --holder (who or what clears this wait)")
            return 1
    # resolve session id + ecosystem from the spec when not given
    session, eco = a.session, a.ecosystem
    spec = None
    try:
        spec = HagSpec.load(find_spec(hag, a.spec))
    except OSError:
        pass
    if spec is not None:
        role = spec.role(a.slug)
        if role is not None:
            session = session or role.session or a.slug
            eco = eco or role.ecosystem
    session = session or a.slug
    eco = eco or "?"
    # 1. heartbeat
    hb = hb_touch(hag, a.slug)
    # 2. wait proof
    if a.to == "waiting":
        w = write_wait(hag, a.slug, sub, a.holder, a.rescan_min, a.monitor, a.note)
        wait_line = f"wait proven until {w['rescan_utc']} (holder: {a.holder})"
    else:
        wait_line = "wait cleared" if clear_wait(hag, a.slug) else "no wait to clear"
    # 3. telemetry
    if build_event is None:
        print("WARNING: telemetry module unavailable; hb + wait written, no event")
        return 0
    fields = [f"from_state={a.from_state or ''}", f"to_state={a.to}",
              f"to_sub={sub}", f"holder={a.holder}", f"monitor={a.monitor or 'none'}",
              f"rescan_min={a.rescan_min:g}" if a.to == "waiting" else "rescan_min=",
              f"note={a.note}", f"channel={a.channel}", f"slug={a.slug}",
              "via=hag.py transition"]
    if raw:
        fields.append(f"to_sub_raw={raw}")
    ev = build_event(hag, session, eco, "session_transition", fields)
    if a.dry_run:
        print("[dry-run]", json.dumps(ev))
    else:
        append_event(resolve_dir(a.dir, hag), ev)
    # 4. ledger (cross-host, cross-ecosystem visibility)
    ledger_line = ""
    if spec is not None and spec.ledger_mode and not a.no_ledger and not a.dry_run:
        try:
            from ledger import Ledger
            led = Ledger(hag, root=a.ledger_dir or spec.ledger_dir)
            if led.exists():
                led.pull()
                lev = led.append(a.slug, "state", state=a.to, sub=sub, holder=a.holder,
                                 rescan_utc=(w["rescan_utc"] if a.to == "waiting" else ""),
                                 rescan_min=(f"{a.rescan_min:g}" if a.to == "waiting" else ""),
                                 monitor=a.monitor, note=a.note, session=session,
                                 host=os.uname().nodename)
                ledger_line = f" | ledger {lev['id']}"
            else:
                ledger_line = " | ledger: MISSING (ledger-init)"
        except Exception as exc:  # noqa: BLE001
            ledger_line = f" | ledger FAILED ({str(exc)[:80]})"
    print(f"{a.slug}: -> {a.to}{('/' + sub) if sub else ''} | hb {hb} | {wait_line}"
          f" | event {'(dry-run)' if a.dry_run else ev['ts_utc']}{ledger_line}")
    return 0


def add_parser(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("transition",
                       help="row-state change: heartbeat + wait proof + telemetry in one")
    p.add_argument("--hag", required=True)
    p.add_argument("--slug", required=True, help="role slug (session id resolves from the spec)")
    p.add_argument("--to", required=True, help="active|waiting|idle|paused|offline")
    p.add_argument("--sub", default="", help="waiting substate (canonical or alias)")
    p.add_argument("--holder", default="", help="who/what clears the wait (required for waiting)")
    p.add_argument("--rescan-min", type=float, default=30,
                   help="minutes until the wait must be re-verified (default 30)")
    p.add_argument("--monitor", default="", help="armed watch, e.g. 'agent_ci_watch PR#580 @abc123'")
    p.add_argument("--from", dest="from_state", default="")
    p.add_argument("--note", default="")
    p.add_argument("--channel", default="within-ecosystem")
    p.add_argument("--session", default="", help="override the session id (else from spec)")
    p.add_argument("--ecosystem", default="")
    p.add_argument("--spec", default="")
    p.add_argument("--dir", default=None, help="telemetry dir override")
    p.add_argument("--no-ledger", action="store_true", help="skip the ledger state event")
    p.add_argument("--ledger-dir", default="", help="ledger clone override (tests / other hosts)")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_transition)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    sp = ap.add_subparsers(dest="cmd", required=True)
    add_parser(sp)
    sys.exit(ap.parse_args().func(ap.parse_args()))
