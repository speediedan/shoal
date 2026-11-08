#!/usr/bin/env python3
"""shoal telemetry collector/analyzer (schema v1, stdlib only).

Usage:
  shoal_telemetry.py emit --shoal ID --session S --ecosystem E --type T [--field k=v ...]
      [--dir LOGDIR]            # append one event to today's log (creates dirs)
  shoal_telemetry.py report --shoal ID [--dir LOGDIR] [--hours N | --since ISO --until ISO]
      # human summary over an ARBITRARY window (hours, not just days): time-in-state
      # per session, instruct/report latency, queue latencies, invalid-state counts.

Storage: <SHOAL_DATA_ROOT>/<id>/telemetry/events-YYYYMMDD.jsonl (UTC day) — the
bulk tier, never committed. The retired versioned telemetry path, archived at <SHOAL_DATA_ROOT>/<id>/legacy-di/telemetry/, is
read as a legacy fallback only (events from before 2026-09-17 live there).
Analysis windows are free (--hours N covers the last N hours across day
boundaries); rotation stays daily.

Every event carries ts_utc, ts_pdt (the configured local wall clock; the field name
is historical), shoal (the group id), server (the configured host name), session, ecosystem, and a `run` pin {protocol_sha, scripts_sha,
schema_v} resolved best-effort from the checkout the package runs from, relative to that
repository's own toplevel (`dirty` markers when uncommitted; events before 2026-09-24 carry
the legacy {instructions_sha, runbook_sha} pair instead of protocol_sha). Emit never fails the caller: wrap it in `|| true` from shell.

Vocabulary: invalid values are recorded verbatim and counted at analysis time
(never coerced). A small ALIASES map handles measured systematic drift
(`user-feedback` -> `user-design`, 7 offers in one incarnation): the emitter
writes the canonical value in `to_sub` and keeps the offered one in
`to_sub_raw`, and the report counts aliased offers separately from invalid
ones. Sessions should use `shoal transition`, which emits through here.
"""
import argparse
import datetime
import json
import os
import subprocess
import sys

PKG_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO_DIR = os.path.dirname(PKG_DIR)
sys.path.insert(0, PKG_DIR)
import config  # noqa: E402

SCHEMA_V = "v2"  # v2 (2026-09-20): ledger_* event types + channel=ledger; v1 events still load
SERVER = config.server()
PDT = config.tzinfo()  # local wall clock for ts_pdt (historical field name)
SHOAL_DATA_ROOT = config.data_root()
# the protocol skill ships in the repository (path relative to its root); its sha is the protocol pin
PROTOCOL_SKILL = "plugins/shoal-skills/skills/shoal-protocol/SKILL.md"

TYPES = {
    "session_transition": ["from_state", "to_state"],
    "coord_comms": ["direction"],
    "watch_event": ["kind"],
    # one health-quorum condition instance (a ladder key): event=open when first seen, event=close
    # with since and duration_min when it clears, so onset and duration are measurable per condition
    "health_condition": ["event", "slug", "condition", "key"],
    "queue_add": ["issue", "queue"],
    "queue_remove": ["issue", "queue", "disposition"],
    "issue_closed": ["issue", "repo"],
    "pr_opened": ["pr", "repo"],
    "pr_merged": ["pr", "repo"],
    "user_prompt": ["prompt_id"],
    "xeco_transition": ["queue", "event"],
    "generation": ["event", "gen"],   # started | ended | retired (spec lifecycle)
    "reconcile": ["new_events"],      # nightly ground-truth marker
    "handover": ["slug", "phase"],    # advised | doc-refreshed | restart | first-turn (context.py)
    "pruned": ["harness", "mode", "name"],  # one old incarnation removed from a harness listing (prune.py)
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
VALID_SUB = {"local-ci", "remote-ci", "research-proto", "user-auth", "user-design", "session-blocked",
             "queue-empty", "upstream"}
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
    "research": "research-proto",
    "research_proto": "research-proto",
    "proto": "research-proto",
    "prototype": "research-proto",
}

SCRIPT_FILES = ("cli.py", "roles.py", "backends.py", "briefs.py", "watchers.py",
                "heartbeat.py", "systemd.py", "state.py", "doctor.py",
                "ledger.py", "ledger_cli.py", "ledger_watch.py", "sources.py",
                "config.py", "providers.py", "telemetry/shoal_telemetry.py", "telemetry/shoal-reconcile.py",
                "telemetry/shoal_report.py")


_TOP: list = []


def _toplevel():
    """The toplevel of the git repository holding this package ("" outside git)."""
    if not _TOP:
        try:
            _TOP.append(subprocess.run(["git", "-C", PKG_DIR, "rev-parse", "--show-toplevel"],
                                       capture_output=True, text=True, timeout=10).stdout.strip())
        except Exception:
            _TOP.append("")
    return _TOP[0]


def _sha(rel, base=PKG_DIR):
    """Blob sha of a file (path relative to `base`, the package dir by default) at HEAD of the
    repository the package lives in, `+dirty` when modified; wherever that checkout is."""
    try:
        top = _toplevel()
        if not top:
            return "untracked"
        path = os.path.relpath(os.path.join(base, rel), top)
        r = subprocess.run(["git", "rev-parse", f"HEAD:{path}"],
                           capture_output=True, text=True, cwd=top, timeout=10)
        if r.returncode != 0:
            return "untracked"
        head = r.stdout.strip()
        dirty = subprocess.run(["git", "status", "--porcelain", "--", path],
                               capture_output=True, text=True, cwd=top,
                               timeout=10).stdout.strip()
        return (head or "unknown") + ("+dirty" if dirty else "")
    except Exception:
        return "unknown"


def run_pin():
    return {
        "protocol_sha": _sha(PROTOCOL_SKILL, REPO_DIR),
        "scripts_sha": "+".join(_sha(f)[:12] for f in SCRIPT_FILES),
        "schema_v": SCHEMA_V,
    }


def resolve_dir(d, sid):
    """Event home. Explicit --dir wins; else the data root (created on demand)."""
    if d:
        return d
    return os.path.join(SHOAL_DATA_ROOT, sid, "telemetry")


def legacy_dirs(sid):
    """Pre-2026-09-17 telemetry lived in the retired versioned control plane; archived copies
    sit under <data root>/<id>/legacy-di/telemetry."""
    return [os.path.join(SHOAL_DATA_ROOT, sid, "legacy-di", "telemetry")]


def canonical_sub(value):
    """Return (canonical, aliased_from) for a waiting substate offer."""
    v = (value or "").strip()
    if v in VALID_SUB or not v:
        return v, ""
    if v in ALIASES:
        return ALIASES[v], v
    return v, ""


def build_event(sid, session, ecosystem, typ, fields):
    now = datetime.datetime.now(datetime.timezone.utc)
    pdt = now.astimezone(PDT).strftime("%H:%M:%S") if PDT else ""
    ev = {
        "ts_utc": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "ts_pdt": pdt,
        "shoal": sid,
        "server": config.env("SERVER", SERVER),
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
    logdir = resolve_dir(args.dir, args.sid)
    ev = build_event(args.sid, args.session, args.ecosystem, args.type, args.field)
    append_event(logdir, ev)
    print(json.dumps(ev))
    return ev


def _parse_ts(s):
    return datetime.datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=datetime.timezone.utc)


def load(logdir, sid, since, until):
    """Events in [since, until] from the event home plus legacy dirs, sorted,
    de-duplicated by (ts, session, type, item-ish key)."""
    evs, seen = [], set()
    dirs = [logdir] + [d for d in legacy_dirs(sid) if d != logdir]
    legacy = os.path.join(logdir, sid)
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
    args.dir = resolve_dir(args.dir, args.sid)
    now = datetime.datetime.now(datetime.timezone.utc)
    if args.hours is not None:
        since = now - datetime.timedelta(hours=args.hours)
        until = now
    else:
        since = _parse_ts(args.since) if args.since else None
        until = _parse_ts(args.until) if args.until else now
    evs = load(args.dir, args.sid, since, until)
    print(f"# shoal {args.sid} report")
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
    config.add_id_arg(e, required=True)
    e.add_argument("--session", required=True)
    e.add_argument("--ecosystem", required=True)
    e.add_argument("--type", required=True)
    e.add_argument("--field", action="append", default=[])
    e.add_argument("--dir", default=None)
    r = sub.add_parser("report")
    config.add_id_arg(r, required=True)
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
