#!/usr/bin/env python3
"""Context cost per session, from the harness's own transcripts (docs/handover.md, "Setting the limit"): no model calls.

    shoal context-report [--days N] [--json] [--all]      # this host's Claude sessions over the last N days (default 1)
    shoal context-report --snapshot                       # write today's snapshot for yesterday, if not written yet

Every Claude call records its usage: cache reads, cache writes (split by TTL; Claude Code uses the 1 h cache), uncached
input and output. A session pays about a tenth of its context on every warm call and twice its context on a cold one
(the cache expired: a wake after an idle hour, a respawn), so its cost is context times calls, and a handover to a fresh
session repays when the saving per call covers one refresh turn plus the new session's first write. This report gives,
per session, the figures a limit is set from:

  calls, calls/day, cold calls (more than half the context written fresh), cold wakes (gaps over the cache TTL),
  average and current context, the cost split (warm reads, writes, cold writes, output), cost-equivalent in base input
  tokens, and the payback of a handover at the current context, in warm calls and in cold wakes.

Snapshots (one per host per UTC day, written by the first health run after midnight; `--snapshot` by hand) go to
<data_root>/_context/<server>-<YYYY-MM-DD>.json, so limits can be tuned on a few days of data, roles included.
"""
from __future__ import annotations

import datetime as dt
import glob
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import config  # noqa: E402

# cost weights relative to one base input token (Anthropic prompt caching): cache read 0.1, 1 h cache write 2.0, 5 m cache
# write 1.25, uncached input 1.0; output 5.0 (the Opus/Sonnet/Haiku output-to-input price ratio)
WEIGHTS = {"read": 0.1, "w1h": 2.0, "w5m": 1.25, "in": 1.0, "out": 5.0}
CACHE_TTL_S = 3600
COLD_MIN_CTX = 20000
FRESH_CTX = 75000  # a fresh session's first context (brief plus start doc), for the payback estimate


def claude_dir() -> str:
    return os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")


def names() -> dict[str, dict]:
    """{conversation id: {"name", "job", "kind"}} from Claude's job records (current and original ids), the shoal specs
    (roles) and the viewer registry (standalone sessions). Unknown ids are reported by id."""
    out: dict[str, dict] = {}
    for f in glob.glob(os.path.join(claude_dir(), "jobs", "*", "state.json")):
        job = os.path.basename(os.path.dirname(f))
        try:
            st = json.load(open(f))
        except (OSError, ValueError):
            continue
        flags = st.get("respawnFlags") or []
        nm = next((flags[i + 1] for i in range(len(flags) - 1) if flags[i] == "-n"), "")
        if st.get("sessionId"):
            out[str(st["sessionId"])] = {"name": nm or job, "job": job, "kind": "job"}
    try:
        from backends import get as get_backend
        for r in get_backend("claude")._agents(include_stopped=True):
            sid, job = str(r.get("sessionId") or ""), str(r.get("id") or "")
            if sid:
                prev = out.get(sid, {})
                out[sid] = {"name": str(r.get("name") or prev.get("name") or job), "job": job, "kind": prev.get("kind", "job")}
    except Exception:  # noqa: BLE001 - names are a convenience
        pass
    try:
        from roles import ShoalSpec
        for p in glob.glob(os.path.join(config.ledger_dir(), "shoals", "*", "shoal.json")):
            spec = ShoalSpec.load(p)
            for r in spec.roles:
                if r.ecosystem == "claude" and r.session:
                    for sid, v in out.items():
                        if v["job"] and (v["job"] == r.session[:8] or sid.startswith(r.session[:8])):
                            v.update(name=f"{r.slug}@{spec.sid.rsplit('-', 2)[0]}", kind="role")
    except Exception:  # noqa: BLE001
        pass
    try:
        p = os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"), "shoal", "ui-standalone.json")
        for e in json.load(open(p)).get("sessions", []):
            for sid, v in out.items():
                if e.get("harness") == "claude" and not e.get("host") and \
                        (v["job"] == e.get("job") or sid.startswith(str(e.get("session", ""))[:8])):
                    v.update(name=e.get("label") or v["name"], kind="standalone")
    except (OSError, ValueError):
        pass
    return out


def calls_of(path: str, since: dt.datetime, until: dt.datetime) -> list[tuple]:
    """[(time, context, read, write_1h, write_5m, input, output, model)] for each model call in the window."""
    out = []
    try:
        fh = open(path, errors="replace")
    except OSError:
        return out
    with fh:
        for line in fh:
            if '"usage"' not in line or '"assistant"' not in line:
                continue
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if d.get("type") != "assistant":
                continue
            try:
                t = dt.datetime.fromisoformat(str(d.get("timestamp", "")).replace("Z", "+00:00"))
            except ValueError:
                continue
            if t < since or t >= until:
                continue
            m = d.get("message") or {}
            u = m.get("usage") or {}
            cc = u.get("cache_creation") or {}
            read, cin = int(u.get("cache_read_input_tokens") or 0), int(u.get("input_tokens") or 0)
            w_all = int(u.get("cache_creation_input_tokens") or 0)
            w1h, w5m = int(cc.get("ephemeral_1h_input_tokens") or 0), int(cc.get("ephemeral_5m_input_tokens") or 0)
            if not (w1h or w5m) and w_all:
                w1h = w_all  # older records carry only the total
            out.append((t, cin + read + w_all, read, w1h, w5m, cin, int(u.get("output_tokens") or 0), str(m.get("model") or "")))
    out.sort()
    return out


def summarize(calls: list[tuple], days: float) -> dict:
    W = WEIGHTS
    read = sum(c[2] for c in calls)
    w1h, w5m = sum(c[3] for c in calls), sum(c[4] for c in calls)
    cin, outp = sum(c[5] for c in calls), sum(c[6] for c in calls)
    cold = [c for c in calls if c[1] > COLD_MIN_CTX and (c[3] + c[4]) > 0.5 * c[1]]
    gaps = sum(1 for a, b in zip(calls, calls[1:]) if (b[0] - a[0]).total_seconds() > CACHE_TTL_S)
    cost_read, cost_write = W["read"] * read, W["w1h"] * w1h + W["w5m"] * w5m
    cost_cold = sum(W["w1h"] * c[3] + W["w5m"] * c[4] for c in cold)
    cost = cost_read + cost_write + W["in"] * cin + W["out"] * outp
    ctx_now = calls[-1][1] if calls else 0
    avg = sum(c[1] for c in calls) / len(calls) if calls else 0
    # a handover costs about one refresh turn at the current context plus the fresh session's first write; it saves the
    # difference in context on every later call
    h = 0.5 * ctx_now + W["w1h"] * FRESH_CTX
    gain = max(ctx_now - FRESH_CTX, 1)
    return {
        "calls": len(calls), "calls_per_day": round(len(calls) / max(days, 1 / 24), 1),
        "cold_calls": len(cold), "cold_wakes": gaps,
        "active_hours": len({c[0].strftime("%Y%m%d%H") for c in calls}),
        "context_avg_k": round(avg / 1000), "context_now_k": round(ctx_now / 1000),
        "tokens": {"cache_read": read, "cache_write_1h": w1h, "cache_write_5m": w5m, "input": cin, "output": outp},
        "cost_eq_m": round(cost / 1e6, 2),
        "share": {k: (round(v / cost, 3) if cost else 0) for k, v in
                  (("warm_reads", cost_read), ("writes", cost_write), ("cold_writes", cost_cold), ("output", W["out"] * outp))},
        "payback": {"warm_calls": round(h / (W["read"] * gain), 1), "cold_wakes": round(h / (W["w1h"] * gain), 2)},
        "model": calls[-1][7] if calls else "",
    }


def report(days: float = 1.0, until: dt.datetime | None = None, everything: bool = False) -> dict:
    """{host, since, until, days, weights, sessions: [{id, name, kind, job, ...summary}]} for this host's Claude sessions
    with at least one call in the window (or at least 3 unless `everything`), costliest first."""
    until = until or dt.datetime.now(dt.timezone.utc)
    since = until - dt.timedelta(days=days)
    nm = names()
    rows = []
    for f in glob.glob(os.path.join(claude_dir(), "projects", "*", "*.jsonl")):
        if os.path.getmtime(f) < since.timestamp():
            continue
        calls = calls_of(f, since, until)
        if not calls or (len(calls) < 3 and not everything):
            continue
        sid = os.path.basename(f)[:-6]
        n = nm.get(sid, {})
        rows.append({"id": sid, "name": n.get("name") or sid[:8], "kind": n.get("kind") or "unknown",
                     "job": n.get("job") or "", **summarize(calls, days)})
    rows.sort(key=lambda r: -r["cost_eq_m"])
    return {"v": 1, "host": config.server() or os.uname().nodename, "since": since.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "until": until.strftime("%Y-%m-%dT%H:%M:%SZ"), "days": days, "weights": WEIGHTS, "sessions": rows}


def snapshot_path(day: dt.date) -> str:
    host = config.server() or os.uname().nodename
    return os.path.join(config.data_root(), "_context", f"{host}-{day.isoformat()}.json")


def write_snapshot(day: dt.date | None = None) -> str:
    """Yesterday's (UTC) report as one file per host and day; '' when it already exists. Atomic: several health runs on
    one host race for it, and exactly one writes it."""
    day = day or (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=1)).date()
    p = snapshot_path(day)
    if os.path.exists(p):
        return ""
    os.makedirs(os.path.dirname(p), exist_ok=True)
    try:
        fd = os.open(p + ".lock", os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return ""
    try:
        until = dt.datetime.combine(day + dt.timedelta(days=1), dt.time(), tzinfo=dt.timezone.utc)
        doc = report(1.0, until)
        with open(p + ".tmp", "w") as fh:
            json.dump(doc, fh, indent=1)
        os.replace(p + ".tmp", p)
        return p
    finally:
        os.close(fd)
        os.remove(p + ".lock")


def render(doc: dict) -> str:
    lines = [f"Claude context cost on {doc['host']}, {doc['since']} to {doc['until']} ({doc['days']:g} d; cost in M base-input "
             f"tokens: read 0.1, 1 h write 2.0, output 5.0)",
             f"{'session':<30} {'kind':<10} {'calls':>5} {'/day':>6} {'cold':>4} {'wakes':>5} {'avg':>5} {'now':>5} "
             f"{'read%':>5} {'cold%':>5} {'cost':>7}  payback (warm calls / cold wakes)"]
    seen: dict[str, int] = {}
    for r in doc["sessions"]:
        seen[r["name"]] = seen.get(r["name"], 0) + 1
    for r in doc["sessions"]:
        s = r["share"]
        nm = r["name"] if seen[r["name"]] == 1 else f"{r['name'][:20]} ({r['id'][:8]})"  # one session, two conversations
        lines.append(f"{nm[:30]:<30} {r['kind']:<10} {r['calls']:>5} {r['calls_per_day']:>6g} {r['cold_calls']:>4} "
                     f"{r['cold_wakes']:>5} {r['context_avg_k']:>4}k {r['context_now_k']:>4}k {100 * s['warm_reads']:>5.0f} "
                     f"{100 * s['cold_writes']:>5.0f} {r['cost_eq_m']:>7.1f}  {r['payback']['warm_calls']:g} / "
                     f"{r['payback']['cold_wakes']:g}")
    lines.append(f"total {sum(r['cost_eq_m'] for r in doc['sessions']):.1f}M over {len(doc['sessions'])} sessions")
    return "\n".join(lines)


def cmd_context_report(a) -> int:
    if a.snapshot:
        p = write_snapshot()
        print(f"context-report: wrote {p}" if p else "context-report: today's snapshot already exists")
        return 0
    doc = report(a.days, everything=a.all)
    print(json.dumps(doc) if a.json else render(doc))
    return 0


def add_parser(sub) -> None:
    p = sub.add_parser("context-report", help="Claude context cost per session from transcripts: warm vs cold, payback (read-only)")
    p.add_argument("--days", type=float, default=1.0)
    p.add_argument("--json", action="store_true")
    p.add_argument("--all", action="store_true", help="include sessions with fewer than 3 calls in the window")
    p.add_argument("--snapshot", action="store_true", help="write yesterday's per-host snapshot (if not written yet)")
    p.set_defaults(func=cmd_context_report)
