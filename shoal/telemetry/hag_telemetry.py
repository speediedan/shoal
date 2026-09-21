#!/usr/bin/env python3
"""HAG telemetry collector/analyzer (schema v1, stdlib only).

Usage:
  hag_telemetry.py emit --hag ID --session S --ecosystem E --type T [--field k=v ...]
      [--dir LOGDIR]            # append one event to today's log (creates dirs)
  hag_telemetry.py report --hag ID [--dir LOGDIR] [--hours N | --since ISO --until ISO]
      # human summary over an ARBITRARY window (hours, not just days): time-in-state
      # per session, instruct/report latency, queue latencies, invalid-state counts.

Storage: <HAG_DATA_ROOT>/<hag>/telemetry/events-YYYYMMDD.jsonl (UTC day) — the
bulk tier, never committed. The versioned <HAG_ROOT>/<hag>/telemetry/ path is
read as a legacy fallback only (events from before 2026-09-17 live there).
Analysis windows are free (--hours N covers the last N hours across day
boundaries); rotation stays daily.

Every event carries ts_utc, ts_pdt (America/Los_Angeles wall clock), hag, server
(speediedl default), session, ecosystem, and a `run` pin {instructions_sha, runbook_sha,
scripts_sha, schema_v} resolved best-effort from the live checkout (`dirty` markers when
uncommitted). Emit never fails the caller: wrap it in `|| true` from shell.

Vocabulary: invalid values are recorded verbatim and counted at analysis time
(never coerced). A small ALIASES map handles measured systematic drift
(`user-feedback` -> `user-design`, 7 offers in one incarnation): the emitter
writes the canonical value in `to_sub` and keeps the offered one in
`to_sub_raw`, and the report counts aliased offers separately from invalid
ones. Sessions should use `hag.py transition`, which emits through here.
"""
import argparse
import datetime
import json
import os
import subprocess
import sys

try:
    from zoneinfo import ZoneInfo
    PDT = ZoneInfo("America/Los_Angeles")
except Exception:  # pragma: no cover
    PDT = None

SCHEMA_V = "v2"  # v2 (2026-09-20): ledger_* event types + channel=ledger; v1 events still load
REPO = "/home/speediedan/repos/distributed-insight"
SERVER = "speediedl"
HAG_ROOT = os.environ.get("HAG_ROOT", os.path.join(REPO, ".agents/hags"))
HAG_DATA_ROOT = os.environ.get("HAG_DATA_ROOT", "/mnt/cache_extended/speediedan/hags")

TYPES = {
    "session_transition": ["from_state", "to_state"],
    "coord_comms": ["direction"],
    "watch_event": ["kind"],
    "queue_add": ["issue", "queue"],
    "queue_remove": ["issue", "queue", "disposition"],
    "issue_closed": ["issue", "repo"],
    "pr_opened": ["pr", "repo"],
    "pr_merged": ["pr", "repo"],
    "user_prompt": ["prompt_id"],
    "xeco_transition": ["queue", "event"],
    "generation": ["event", "gen"],   # started | ended | retired (spec lifecycle)
    "reconcile": ["new_events"],      # nightly ground-truth marker
    # ledger bridge (ledger.as_telemetry): one type per ledger verb; `state`
    # arrives as session_transition with via=ledger
    "ledger_offer": ["to", "item"],
    "ledger_withdraw": ["item"],
    "ledger_claim": ["item"],
    "ledger_release": ["item", "kind"],
    "ledger_done": ["item"],
    "ledger_msg": ["to", "kind"],
    "ledger_system": ["event"],
}

VALID_STATES = {"active", "waiting", "idle", "paused", "offline"}
VALID_SUB = {"local-ci", "remote-ci", "user-auth", "user-design", "session-blocked",
             "queue-empty"}
# NOTE (2026-09-15): `monitor-armed` was removed. Every real wait already has a precise
# substate: CI waits (however watched) are local-ci/remote-ci with the watch named in the
# `monitor` field; awaiting refill is queue-empty; awaiting a session is session-blocked.
ALIASES = {
    "user-feedback": "user-design",   # measured drift: 7 offers, 2026-09-16/17
    "design": "user-design",
    "auth": "user-auth",
    "ci": "remote-ci",
    "gate": "remote-ci",
    "blocked": "session-blocked",
    "queue-exhausted": "queue-empty",
}

SCRIPT_FILES = ("hag.py", "roles.py", "backends.py", "briefs.py", "watchers.py",
                "queue_watch.py", "heartbeat.py", "queue_section.py", "systemd.py",
                "state.py", "doctor.py", "lint.py",
                "ledger.py", "ledger_cli.py", "ledger_watch.py", "sources.py",
                "telemetry/hag_telemetry.py", "telemetry/hag-reconcile.py",
                "telemetry/hag_report.py")


def _sha(path):
    try:
        r = subprocess.run(["git", "rev-parse", f"HEAD:{path}"],
                           capture_output=True, text=True, cwd=REPO, timeout=10)
        if r.returncode != 0:
            return "untracked"
        head = r.stdout.strip()
        dirty = subprocess.run(["git", "status", "--porcelain", "--", path],
                               capture_output=True, text=True, cwd=REPO,
                               timeout=10).stdout.strip()
        return (head or "unknown") + ("+dirty" if dirty else "")
    except Exception:
        return "unknown"


def run_pin():
    return {
        "instructions_sha": _sha(".agents/instructions/shared-orchestration-instructions.md"),
        "runbook_sha": _sha(".agents/agent_coordination_runbook.md"),
        "scripts_sha": "+".join(
            _sha(f"project_admin/shared_admin_scripts/hag/{f}")[:12]
            for f in SCRIPT_FILES),
        "schema_v": SCHEMA_V,
    }


def resolve_dir(d, hag):
    """Event home. Explicit --dir wins; else the data root (created on demand)."""
    if d:
        return d
    return os.path.join(HAG_DATA_ROOT, hag, "telemetry")


def legacy_dirs(hag):
    return [os.path.join(HAG_ROOT, hag, "telemetry")]


def canonical_sub(value):
    """Return (canonical, aliased_from) for a waiting substate offer."""
    v = (value or "").strip()
    if v in VALID_SUB or not v:
        return v, ""
    if v in ALIASES:
        return ALIASES[v], v
    return v, ""


def build_event(hag, session, ecosystem, typ, fields):
    now = datetime.datetime.now(datetime.timezone.utc)
    pdt = now.astimezone(PDT).strftime("%H:%M:%S") if PDT else ""
    ev = {
        "ts_utc": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "ts_pdt": pdt,
        "hag": hag,
        "server": os.environ.get("HAG_SERVER", SERVER),
        "session": session,
        "ecosystem": ecosystem,
        "type": typ,
        "run": run_pin(),
    }
    for kv in fields or []:
        k, _, v = kv.partition("=")
        ev[k] = v
    for f in TYPES.get(typ, []):
        ev.setdefault(f, "")
    if typ == "session_transition" and ev.get("to_state") == "waiting":
        canon, raw = canonical_sub(ev.get("to_sub", ""))
        if raw:
            ev["to_sub"], ev["to_sub_raw"] = canon, raw
    return ev


def append_event(logdir, ev):
    day = ev["ts_utc"][:10].replace("-", "")
    os.makedirs(logdir, exist_ok=True)
    with open(os.path.join(logdir, f"events-{day}.jsonl"), "a") as fh:
        fh.write(json.dumps(ev) + "\n")


def emit(args):
    logdir = resolve_dir(args.dir, args.hag)
    ev = build_event(args.hag, args.session, args.ecosystem, args.type, args.field)
    append_event(logdir, ev)
    print(json.dumps(ev))
    return ev


def _parse_ts(s):
    return datetime.datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=datetime.timezone.utc)


def load(logdir, hag, since, until):
    """Events in [since, until] from the event home plus legacy dirs, sorted,
    de-duplicated by (ts, session, type, item-ish key)."""
    evs, seen = [], set()
    dirs = [logdir] + [d for d in legacy_dirs(hag) if d != logdir]
    legacy = os.path.join(logdir, hag)
    if os.path.isdir(legacy) and legacy != logdir:
        dirs.append(legacy)
    for d in dirs:
        if not os.path.isdir(d):
            continue
        for fn in sorted(os.listdir(d)):
            if not (fn.startswith("events-") and fn.endswith(".jsonl")):
                continue
            with open(os.path.join(d, fn)) as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        e = json.loads(line)
                    except Exception:
                        continue
                    try:
                        t = _parse_ts(e["ts_utc"])
                    except Exception:
                        continue
                    if since and t < since:
                        continue
                    if until and t > until:
                        continue
                    key = (e["ts_utc"], e.get("session"), e.get("type"),
                           e.get("issue") or e.get("pr") or e.get("item") or e.get("note", ""))
                    if key in seen:
                        continue
                    seen.add(key)
                    evs.append(e)
    return sorted(evs, key=lambda e: e["ts_utc"])


def classify_transition(e):
    """'valid' | 'aliased' | 'invalid' for a session_transition event."""
    st, sub = e.get("to_state"), e.get("to_sub", "")
    if st not in VALID_STATES:
        return "invalid"
    if st == "waiting":
        if sub in VALID_SUB:
            return "aliased" if e.get("to_sub_raw") else "valid"
        return "invalid"
    return "valid"


def report(args):
    args.dir = resolve_dir(args.dir, args.hag)
    now = datetime.datetime.now(datetime.timezone.utc)
    if args.hours is not None:
        since = now - datetime.timedelta(hours=args.hours)
        until = now
    else:
        since = _parse_ts(args.since) if args.since else None
        until = _parse_ts(args.until) if args.until else now
    evs = load(args.dir, args.hag, since, until)
    print(f"# HAG {args.hag} report")
    print(f"window: {since} .. {until} ({len(evs)} events, schema {SCHEMA_V})")
    if not evs:
        print("no events in window.")
        return
    by_type = {}
    for e in evs:
        by_type[e.get("type", "?")] = by_type.get(e.get("type", "?"), 0) + 1
    print("\n## events by type")
    for t, n in sorted(by_type.items()):
        print(f"- {t}: {n}")
    print("\n## last state per session")
    last = {}
    for e in evs:
        if e.get("type") == "session_transition":
            last[e["session"]] = (e.get("to_state"), e.get("to_sub", ""), e["ts_utc"])
    for s, (st, sub, ts) in sorted(last.items()):
        print(f"- {s}: {st}{('/' + sub) if sub else ''} @ {ts}")
    trans = [e for e in evs if e.get("type") == "session_transition"]
    invalid = [e for e in trans if classify_transition(e) == "invalid"]
    aliased = [e for e in trans if classify_transition(e) == "aliased"]
    print(f"\n## invalid states offered: {len(invalid)} (aliased, counted separately: {len(aliased)})")
    for e in invalid[:10]:
        print(f"- {e['ts_utc']} {e['session']}: {e.get('to_state')}/{e.get('to_sub')}")
    for e in aliased[:5]:
        print(f"- aliased {e['ts_utc']} {e['session']}: {e.get('to_sub_raw')} -> {e.get('to_sub')}")
    auth_claims = [e for e in trans if e.get("to_state") == "waiting" and e.get("to_sub") == "user-auth"]
    prompts = [e for e in evs if e.get("type") == "user_prompt"]
    print(f"\n## unverified-auth check: {len(auth_claims)} user-auth claims vs {len(prompts)} prompt events")
    if auth_claims and not prompts:
        print("WARNING: auth waits claimed with no raised ask on record — treat as suspect idle, not verified wait")
    print("\n## queue latencies (xeco)")
    for e in evs:
        if e.get("type") == "xeco_transition":
            print(f"- {e['ts_utc']} {e.get('queue')}/{e.get('item','?')}"
                  + (f":{e['kind']}" if e.get("kind") else "")
                  + f": {e.get('event')}"
                  + (f" queue_latency_s={e['queue_latency_s']}" if "queue_latency_s" in e else "")
                  + (f" completion_latency_s={e['completion_latency_s']}" if "completion_latency_s" in e else ""))
    print("\n## queue add/remove")
    for e in evs:
        if e.get("type") in ("queue_add", "queue_remove"):
            print(f"- {e['ts_utc']} {e.get('type')} {e.get('queue')}/{e.get('issue','?')}"
                  + (f":{e['kind']}" if e.get("kind") else "")
                  + (f" repo={e['repo']}" if e.get("repo") else "")
                  + (f" by={e.get('added_by') or e.get('removed_by') or '?'}" if (e.get('added_by') or e.get('removed_by')) else "")
                  + (f" disposition={e['disposition']}" if e.get("disposition") else ""))
    watch = [e for e in evs if e.get("type") == "watch_event"]
    if watch:
        print("\n## watch events")
        kinds = {}
        for e in watch:
            kinds[e.get("kind", "?")] = kinds.get(e.get("kind", "?"), 0) + 1
        for k, n in sorted(kinds.items()):
            print(f"- {k}: {n}")


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("emit")
    e.add_argument("--hag", required=True)
    e.add_argument("--session", required=True)
    e.add_argument("--ecosystem", required=True)
    e.add_argument("--type", required=True)
    e.add_argument("--field", action="append", default=[])
    e.add_argument("--dir", default=None)
    r = sub.add_parser("report")
    r.add_argument("--hag", required=True)
    r.add_argument("--dir", default=None)
    r.add_argument("--hours", type=float, default=None)
    r.add_argument("--since", default=None)
    r.add_argument("--until", default=None)
    args = ap.parse_args()
    if args.cmd == "emit":
        emit(args)
    else:
        report(args)


if __name__ == "__main__":
    main()
