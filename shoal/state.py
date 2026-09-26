#!/usr/bin/env python3
"""Session-side state primitive: `shoal transition`.

ONE command per row-state change, whatever the ecosystem. It does the three
things every session was previously asked to do separately (and measurably
did not): tick the heartbeat, prove or clear a wait, emit the telemetry
event. Sessions that forget one of three forget it silently; a single
command cannot be half-done.

  shoal transition --shoal H --slug S --to active [--note ...]
  shoal transition --shoal H --slug S --to waiting --sub remote-ci \
      --holder "gate 1290 (approved, queued)" --rescan-min 30 [--monitor ...]
  shoal transition --shoal H --slug S --to idle|paused|offline

The same command also appends a `state` event to the HAG ledger (ledger.py,
2026-09-20), so every peer and every host sees the row state without anyone
transcribing it; `--no-ledger` skips that (tests).

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
import config  # noqa: E402

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
               monitor: str, note: str, target: str = "") -> dict:
    """`target` is machine-checkable where possible (the health quorum verifies it):
    lease:<key> | job:<unit> | gh-run:<owner/repo>:<run-id> | gh-pr:<owner/repo>#<n> |
    az-build:<id>; anything else is free text and only the periodic validity wake covers it."""
    now = datetime.datetime.now(datetime.timezone.utc)
    w = {"slug": slug, "sub": sub, "holder": holder, "monitor": monitor, "target": target,
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


# Wait discipline (spec features.waitdiscipline; architecture doc §3.5). Every wait expires:
# a rescan longer than its cap is clamped, because an expired rescan is the ONLY signal that
# a silent wait has gone stale (measured 2026-09-23: two lanes set 12 h rescans on waits
# nobody was answering and sat 14 h without a prompt reaching the maintainer).
USER_WAITS = ("user-auth", "user-design", "user-feedback")
RESCAN_CAP_MIN = {"user-auth": 240, "user-design": 240, "user-feedback": 240,
                  "session-blocked": 240, "queue-empty": 360, "local-ci": 180, "remote-ci": 180,
                  "upstream": 720}


def wait_discipline(spec) -> dict | None:
    """The spec's wait-discipline config ({} = defaults), or None when the feature is off."""
    f = (getattr(spec, "features", {}) or {}).get("waitdiscipline")
    if not f:
        return None
    return f if isinstance(f, dict) else {}


def check_wait(spec, slug: str, sub: str, rescan_min: float, blocked_on: str,
               asks: list[str] | None) -> tuple[float, list[str], str]:
    """(clamped rescan, notes, refusal) for a wait under the discipline. A user wait must
    name the prompted ask it waits on; a session-blocked wait must name the blocking role."""
    cfg = wait_discipline(spec)
    if cfg is None:
        return rescan_min, [], ""
    notes = []
    cap = float((cfg.get("rescan_cap_min") or {}).get(sub, RESCAN_CAP_MIN.get(sub, 240)))
    if rescan_min > cap:
        notes.append(f"rescan {rescan_min:g}m clamped to the {sub} cap {cap:g}m (an expired rescan is the only "
                     f"stale-wait signal; re-prove the wait when it expires)")
        rescan_min = cap
    if sub in USER_WAITS and not asks:
        return rescan_min, notes, (f"a {sub} wait needs the prompted ask it waits on: raise it first-person as a "
                                   f"question with options (opencode question tool / Claude AskUserQuestion) in "
                                   f"this turn, then record it: `shoal queue --slug {slug} --ask \"<question>\"`")
    if sub == "session-blocked":
        who = (blocked_on or "").split(":", 1)[0]
        if who == slug:  # measured 2026-09-26: a lane named itself to pass this check
            return rescan_min, notes, ("a session-blocked wait names ANOTHER role in this shoal; your own item "
                                       "waiting on something outside it (an upstream issue or release) is "
                                       "`--wait upstream --target gh-issue:<owner/repo>#<n>`, which the health "
                                       "quorum resolves when it closes")
        if not who or spec.role(who) is None:
            return rescan_min, notes, ("a session-blocked wait names the blocking role (and item): "
                                       "--blocked-on <role>[:<repo#n>] so the blocker sees it in `next` "
                                       "and the health quorum can follow the chain")
    return rescan_min, notes, ""


def check_target(sub: str, target: str, slug: str, hag: str = "") -> tuple[str, list[str], str]:
    """(canonical target, notes, refusal) for a wait target. A malformed target is refused (nothing
    could ever check it), and so is one already resolved (there is nothing left to wait on)."""
    import providers
    if not target:
        if sub == "upstream":
            return "", [], ("an upstream wait needs a checkable target: --target gh-issue:<owner/repo>#<n> "
                            "(or gh-pr:, gh-run:) so the quorum wakes you when it resolves")
        return "", [], ""
    canon, err = providers.normalize(target)
    if err:
        return target, [], f"wait target refused: {err}"
    pending, why = providers.resolve(canon, slug, hag)
    if pending is False:
        return canon, [], (f"wait target {canon} is already resolved ({why}): nothing is left to wait on. "
                           f"Take the next item (`queue --active <item>`) or name a target that is pending")
    notes = [f"target normalized to {canon}"] if canon != target else []
    if pending is None:
        notes.append(f"target {canon} cannot be checked now ({why}); the quorum will keep asking you to "
                     f"re-verify this wait")
    return canon, notes, ""


def current_asks(spec, slug: str) -> list[str]:
    try:
        from ledger import Ledger, View
        led = Ledger(spec.hag, root=spec.ledger_dir)
        led.pull()
        q = View(led, spec).current_queue(slug) or {}
        return list(q.get("asks") or [])
    except Exception:  # noqa: BLE001
        return []


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
    # resolve session id + ecosystem from the spec. A missing spec used to
    # degrade to session=<slug>, eco='?', which produced events no report could
    # attribute to a role; fail loudly instead, before any side effect.
    session, eco = a.session, a.ecosystem
    spec_path = find_spec(hag, a.spec)
    try:
        spec = HagSpec.load(spec_path)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"ERROR: no readable HAG spec for {hag!r} at {spec_path} ({exc}); "
              "refusing to emit an unattributable transition. Pass --spec <hag.json>.",
              file=sys.stderr)
        return 2
    role = spec.role(a.slug)
    if role is None:
        print(f"WARNING: slug {a.slug!r} is not a role in {spec_path}; the event "
              "will be reported as unresolved", file=sys.stderr)
    else:
        rs = role.session or ""
        # a prefix-compatible override (8-char short id vs full UUID) is the
        # same session: record the spec's canonical form
        if session and rs and (rs.startswith(session) or session.startswith(rs)):
            session = rs
        session = session or rs or a.slug
        eco = eco or role.ecosystem
    session = session or a.slug
    eco = eco or "?"
    blocked_on = getattr(a, "blocked_on", "") or ""
    if a.to == "waiting" and wait_discipline(spec) is not None:
        asks = getattr(a, "asks", None)
        if asks is None:
            asks = current_asks(spec, a.slug) if sub in USER_WAITS else []
        a.rescan_min, notes, refusal = check_wait(spec, a.slug, sub, a.rescan_min, blocked_on, asks)
        for n in notes:
            print(f"note: {n}")
        if refusal:
            print(f"ERROR: {refusal} (nothing written)")
            return 3
    if a.to == "waiting":
        a.target, tnotes, trefusal = check_target(sub, getattr(a, "target", "") or "", a.slug, hag)
        for n in tnotes:
            print(f"note: {n}")
        if trefusal:
            print(f"ERROR: {trefusal} (nothing written)")
            return 3
    # 1. heartbeat
    hb = hb_touch(hag, a.slug)
    # 2. wait proof
    if a.to == "waiting":
        w = write_wait(hag, a.slug, sub, a.holder, a.rescan_min, a.monitor, a.note,
                       target=getattr(a, "target", "") or "")
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
              "via=shoal transition"]
    if raw:
        fields.append(f"to_sub_raw={raw}")
    ev = build_event(hag, session, eco, "session_transition", fields)
    if a.dry_run:
        print("[dry-run]", json.dumps(ev))
    else:
        append_event(resolve_dir(a.dir, hag), ev)
    # 4. ledger (cross-host, cross-ecosystem visibility)
    ledger_line = ""
    if not a.no_ledger and not a.dry_run:
        try:
            from ledger import Ledger
            led = Ledger(hag, root=a.ledger_dir or spec.ledger_dir)
            if led.exists():
                led.pull()
                lev = led.append(a.slug, "state", state=a.to, sub=sub, holder=a.holder,
                                 blocked_on=(blocked_on if a.to == "waiting" else ""),
                                 tag=(role.tag if role is not None else ""),
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
    config.add_id_arg(p, required=True)
    p.add_argument("--slug", required=True, help="role slug (session id resolves from the spec)")
    p.add_argument("--to", required=True, help="active|waiting|idle|paused|offline")
    p.add_argument("--sub", default="", help="waiting substate (canonical or alias)")
    p.add_argument("--holder", default="", help="who/what clears the wait (required for waiting)")
    p.add_argument("--rescan-min", type=float, default=30,
                   help="minutes until the wait must be re-verified (default 30)")
    p.add_argument("--monitor", default="", help="armed watch, e.g. 'agent_ci_watch PR#580 @abc123'")
    p.add_argument("--blocked-on", default="", help="session-blocked waits: the blocking role[:repo#n]")
    p.add_argument("--target", default="", help="what the wait is on, machine-checkable where possible: "
                   "lease:<key> | job:<unit> | gh-run:<repo>:<id> | gh-pr:<repo>#<n> | az-build:<id>")
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
