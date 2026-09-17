#!/usr/bin/env python3
"""HAG telemetry collector/analyzer (schema v1, stdlib only).

Usage:
  hag_telemetry.py emit --hag ID --session S --ecosystem E --type T [--field k=v ...]
      [--dir LOGDIR]            # append one event to today's log (creates dirs)
  hag_telemetry.py report --hag ID [--dir LOGDIR] [--hours N | --since ISO --until ISO]
      # human summary over an ARBITRARY window (hours, not just days): time-in-state
      # per session, instruct/report latency, queue latencies, invalid-state counts.

Storage: <logdir>/<hag>/events-YYYYMMDD.jsonl (UTC day). Analysis windows are free
(--hours N covers the last N hours across day boundaries); rotation stays daily.

Every event carries ts_utc, ts_pdt (America/Los_Angeles wall clock), hag, server
(speediedl default), session, ecosystem, and a `run` pin {instructions_sha, runbook_sha,
scripts_sha, schema_v} resolved best-effort from the live checkout (`dirty` markers when
uncommitted). Emit never fails the caller: wrap it in `|| true` from shell.
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

SCHEMA_V = "v1"
REPO = "/home/speediedan/repos/distributed-insight"
SERVER = "speediedl"

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
}

VALID_STATES = {"active", "waiting", "idle", "paused", "offline"}
VALID_SUB = {"local-ci", "remote-ci", "user-auth", "user-design", "session-blocked",
             "queue-empty"}
# NOTE (2026-09-15): `monitor-armed` was removed. Every real wait already has a precise
# substate: CI waits (however watched) are local-ci/remote-ci with the watch named in the
# `monitor` field; awaiting refill is queue-empty; awaiting a session is session-blocked.


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
        "scripts_sha": _sha("project_admin/shared_admin_scripts/watch-coordination.sh"),
        "schema_v": SCHEMA_V,
    }


def resolve_dir(d, hag):
    if d:
        return d
    cache = os.path.join("/mnt/cache_extended/speediedan/hags", hag, "telemetry")
    if os.path.isdir(cache):
        return cache
    return os.path.join(REPO, ".agents/hags", hag, "telemetry")


def emit(args):
    logdir = resolve_dir(args.dir, args.hag)
    now = datetime.datetime.now(datetime.timezone.utc)
    pdt = now.astimezone(PDT).strftime("%H:%M:%S") if PDT else ""
    ev = {
        "ts_utc": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "ts_pdt": pdt,
        "hag": args.hag,
        "server": os.environ.get("HAG_SERVER", SERVER),
        "session": args.session,
        "ecosystem": args.ecosystem,
        "type": args.type,
        "run": run_pin(),
    }
    for kv in args.field or []:
        k, _, v = kv.partition("=")
        ev[k] = v
    for f in TYPES.get(args.type, []):
        ev.setdefault(f, "")
    day = now.strftime("%Y%m%d")
    os.makedirs(logdir, exist_ok=True)
    with open(os.path.join(logdir, f"events-{day}.jsonl"), "a") as fh:
        fh.write(json.dumps(ev) + "\n")
    print(json.dumps(ev))


def _parse_ts(s):
    return datetime.datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=datetime.timezone.utc)


def load(logdir, hag, since, until):
    evs = []
    dirs = [logdir]
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
                    evs.append(e)
    return sorted(evs, key=lambda e: e["ts_utc"])


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
    invalid = [e for e in evs
               if e.get("type") == "session_transition"
               and (e.get("to_state") not in VALID_STATES
                    or (e.get("to_state") == "waiting" and e.get("to_sub") not in VALID_SUB))]
    print(f"\n## invalid states offered: {len(invalid)}")
    for e in invalid[:10]:
        print(f"- {e['ts_utc']} {e['session']}: {e.get('to_state')}/{e.get('to_sub')}")
    auth_claims = [e for e in evs if e.get("type") == "session_transition"
                   and e.get("to_state") == "waiting" and e.get("to_sub") == "user-auth"]
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
