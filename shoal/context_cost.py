#!/usr/bin/env python3
"""Context cost per session, from the harness's own transcripts (docs/handover.md, "Setting the limit"): no model calls.

    shoal context-report [--days N] [--json] [--all]      # this host's Claude sessions over the last N days (default 1)
    shoal context-report --snapshot                       # write today's snapshot for yesterday, if not written yet
    shoal context-report --handovers [--days N] [--json]  # handovers that happened, from `handover` telemetry: cost each

Every Claude call records its usage: cache reads, cache writes (split by TTL; Claude Code uses the 1 h cache), uncached
input and output. A session pays about a tenth of its context on every warm call and twice its context on a cold one
(the cache expired: a wake after an idle hour, a respawn), so its cost is context times calls, and a handover to a fresh
session repays when the saving per call covers one refresh turn plus the new session's first write. This report gives,
per session, the figures a limit is set from:

  calls, calls/day, cold calls (more than half the context written fresh), cold wakes (gaps over the cache TTL),
  average and current context, the cost split (warm reads, writes, cold writes, output), cost-equivalent in base input
  tokens, and the payback of a handover at the current context, in warm calls and in cold wakes;
  turns (a prompt from the maintainer, a peer or a task notification starts one), calls per turn and context growth per
  turn, and for each candidate limit (`--limits`, default 300,400,500k) a replay of the window with a handover at that
  limit: how many would have fired, and how many turns and hours apart, with the turns a fresh session would take to
  reach the limit at this window's growth.

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
LIMITS_K = (300, 400, 500)  # candidate handover limits the replay tries


def claude_dir() -> str:
    return os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")


def names() -> dict[str, dict]:
    """{conversation id: {"name", "job", "kind"}} from Claude's job records (current and original ids), the tombstones
    of pruned jobs (~/.claude/archived_sessions/jobs/), the shoal specs (roles, and their generation history) and the
    viewer registry (standalone sessions). A pruned incarnation keeps its name this way. Unknown ids are reported by
    id."""
    out: dict[str, dict] = {}
    # tombstones first: a live job record of the same id (none, once pruned) wins over its tombstone
    for f in glob.glob(os.path.join(claude_dir(), "archived_sessions", "jobs", "*", "state.json")) + \
            glob.glob(os.path.join(claude_dir(), "jobs", "*", "state.json")):
        job = os.path.basename(os.path.dirname(f))
        try:
            st = json.load(open(f))
        except (OSError, ValueError):
            continue
        flags = st.get("respawnFlags") or []
        nm = next((flags[i + 1] for i in range(len(flags) - 1) if flags[i] == "-n"), "") or str(st.get("name") or "")
        if st.get("sessionId"):
            out[str(st["sessionId"])] = {"name": nm or job, "job": job, "kind": "job"}
        try:  # a tombstone also names the conversation the job held when it was pruned
            for ln in open(os.path.join(os.path.dirname(f), "ARCHIVE_INFO.txt")):
                if ln.startswith("session_id:") and ln.split(":", 1)[1].strip():
                    out.setdefault(ln.split(":", 1)[1].strip(), {"name": nm or job, "job": job, "kind": "job"})
        except OSError:
            pass
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
            short = spec.sid.rsplit('-', 2)[0]
            for g in spec.generations:  # retired incarnations: `<slug>-r<seq>@<shoal>` when nothing else names them
                seqs = g.get("seqs") or {}
                for slug, sess in (g.get("sessions") or {}).items():
                    r = spec.role(slug)
                    if not sess or (r is not None and r.ecosystem != "claude"):
                        continue
                    nm = f"{slug}-r{seqs[slug]}@{short}" if seqs.get(slug) else f"{slug}@{short}"
                    hit = [k for k, v in out.items() if v["job"] == sess[:8] or k.startswith(sess[:8])]
                    for k in hit:
                        if out[k]["name"] in (out[k]["job"], ""):
                            out[k].update(name=nm, kind="role")
                    if not hit:  # a job id alone is its conversation's first 8 characters: report() looks it up so
                        out[sess] = {"name": nm, "job": sess[:8], "kind": "role"}
            for r in spec.roles:
                if r.ecosystem == "claude" and r.session:
                    for sid, v in out.items():
                        if v["job"] and (v["job"] == r.session[:8] or sid.startswith(r.session[:8])):
                            v.update(name=f"{r.slug}@{short}", kind="role")
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
    return read_session(path, since, until)[0]


def _turn_start(d: dict) -> bool:
    """A prompt that starts a turn: the maintainer's, a peer's message or a task notification (Claude Code records its
    `origin`). Tool results, skill bodies and other meta records continue a turn; a subagent's records are its own."""
    if d.get("type") != "user" or d.get("isSidechain"):
        return False
    if "origin" in d:
        return bool(d["origin"])
    c = (d.get("message") or {}).get("content")
    return not d.get("isMeta") and (isinstance(c, str) or any(
        isinstance(b, dict) and b.get("type") == "text" for b in (c or [])))  # older records carry no origin


def read_session(path: str, since: dt.datetime, until: dt.datetime) -> tuple[list[tuple], list[dt.datetime]]:
    """(calls as calls_of gives them, turn start times) in the window, from one pass over the transcript."""
    out, turns = [], []
    try:
        fh = open(path, errors="replace")
    except OSError:
        return out, turns
    with fh:
        for line in fh:
            call = '"usage"' in line and '"assistant"' in line
            if not call and ('"user"' not in line or '"isSidechain":true' in line):
                continue
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if d.get("type") != "assistant" and not _turn_start(d):
                continue
            try:
                t = dt.datetime.fromisoformat(str(d.get("timestamp", "")).replace("Z", "+00:00"))
            except ValueError:
                continue
            if t < since or t >= until:
                continue
            if d.get("type") == "user":
                turns.append(t)
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
    turns.sort()
    return out, turns


def turn_contexts(calls: list[tuple], turns: list[dt.datetime]) -> list[int]:
    """The context each turn started from: the last call's before its prompt (turns before the first call are skipped)."""
    out, i, ctx = [], 0, None
    for t in turns:
        while i < len(calls) and calls[i][0] <= t:
            ctx = calls[i][1]
            i += 1
        if ctx is not None:
            out.append(ctx)
    return out


def growth_per_turn(calls: list[tuple], turns: list[dt.datetime]) -> float:
    """Mean context growth from one turn's start to the next's, over the turns that grew (a compaction or a clear
    shrinks the context; it is not growth)."""
    ctx = turn_contexts(calls, turns)
    g = [b - a for a, b in zip(ctx, ctx[1:]) if b >= a]
    return sum(g) / len(g) if g else 0.0


def replay(calls: list[tuple], turns: list[dt.datetime], limit_k: float) -> dict:
    """The window again with a handover at `limit_k`: the simulated context follows the real one's growth, and at a
    turn's start past the limit it hands over to a fresh session (FRESH_CTX). A real shrink (compaction, clear) caps it.
    The first crossing comes from the window's starting context, so `first_after_turns` is partial; `turns_between`
    and `hours_between` are the full spans from one handover to the next."""
    lim = limit_k * 1000
    sim = prev = None
    n, since_turns, since_t, first, between, hours = 0, 0, None, None, [], []
    ti = 0
    for c in calls:
        while ti < len(turns) and turns[ti] <= c[0]:
            t = turns[ti]
            ti += 1
            if sim is not None and sim >= lim:
                n += 1
                if first is None:
                    first = since_turns
                else:
                    between.append(since_turns)
                    hours.append(round((t - since_t).total_seconds() / 3600, 1))
                sim, since_turns, since_t = FRESH_CTX, 0, t
            since_turns += 1
        if sim is None:
            sim = prev = c[1]
            since_t = c[0]
            continue
        sim = min(sim + max(c[1] - prev, 0), c[1])
        prev = c[1]
    g = growth_per_turn(calls, turns)
    return {"handovers": n, "first_after_turns": first, "turns_between": between, "hours_between": hours,
            "turns_from_fresh": round((lim - FRESH_CTX) / g) if g > 0 and lim > FRESH_CTX else None}


def summarize(calls: list[tuple], days: float, turns: list[dt.datetime] | None = None,
              limits: tuple = LIMITS_K) -> dict:
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
        "turns": len(turns or []),
        "calls_per_turn": round(len(calls) / len(turns), 1) if turns else None,
        "growth_per_turn_k": round(growth_per_turn(calls, turns or []) / 1000, 1),
        "handover": {f"{k:g}": replay(calls, turns or [], k) for k in limits},
    }


def report(days: float = 1.0, until: dt.datetime | None = None, everything: bool = False,
           limits: tuple = LIMITS_K) -> dict:
    """{host, since, until, days, weights, limits_k, sessions: [{id, name, kind, job, ...summary}]} for this host's
    Claude sessions with at least one call in the window (or at least 3 unless `everything`), costliest first."""
    until = until or dt.datetime.now(dt.timezone.utc)
    since = until - dt.timedelta(days=days)
    nm = names()
    rows = []
    for f in glob.glob(os.path.join(claude_dir(), "projects", "*", "*.jsonl")):
        if os.path.getmtime(f) < since.timestamp():
            continue
        calls, turns = read_session(f, since, until)
        if not calls or (len(calls) < 3 and not everything):
            continue
        sid = os.path.basename(f)[:-6]
        n = nm.get(sid) or nm.get(sid[:8], {})
        rows.append({"id": sid, "name": n.get("name") or sid[:8], "kind": n.get("kind") or "unknown",
                     "job": n.get("job") or "", **summarize(calls, days, turns, limits)})
    rows.sort(key=lambda r: -r["cost_eq_m"])
    return {"v": 1, "host": config.server() or os.uname().nodename, "since": since.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "until": until.strftime("%Y-%m-%dT%H:%M:%SZ"), "days": days, "weights": WEIGHTS,
            "limits_k": list(limits), "fresh_k": FRESH_CTX // 1000, "sessions": rows}


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
    limits = doc.get("limits_k") or []
    if limits:
        lines += ["", f"handover at a limit, replayed over this window: handovers fired (median turns and hours between); "
                      f"turns from a fresh {doc.get('fresh_k', FRESH_CTX // 1000)}k at this window's growth per turn",
                  f"{'session':<30} {'turns':>5} {'calls/t':>7} {'k/turn':>6}  " +
                  "  ".join(f"{f'{k:g}k':<24}" for k in limits)]
        for r in doc["sessions"]:
            nm = r["name"] if seen[r["name"]] == 1 else f"{r['name'][:20]} ({r['id'][:8]})"
            cells = []
            for k in limits:
                h = (r.get("handover") or {}).get(f"{k:g}") or {}
                tb, hb = sorted(h.get("turns_between") or []), sorted(h.get("hours_between") or [])
                mid = f" ({tb[len(tb) // 2]}t, {hb[len(hb) // 2]:g}h)" if tb else ""
                cell = f"{h.get('handovers', 0)}{mid}; fresh {h.get('turns_from_fresh') or '-'}t"
                cells.append(f"{cell:<24}")
            cpt = r.get("calls_per_turn")
            lines.append(f"{nm[:30]:<30} {r.get('turns', 0):>5} {(f'{cpt:g}' if cpt else '-'):>7} "
                         f"{r.get('growth_per_turn_k', 0):>6g}  " + "  ".join(cells))
    return "\n".join(lines)


#  ---- handovers that happened: the `handover` telemetry events (context.py) and the context-restart condition --------

def _parse(ts: str) -> dt.datetime | None:
    try:
        return dt.datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)
    except (TypeError, ValueError):
        return None


def _num(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def handover_events(since: dt.datetime, until: dt.datetime) -> list[dict]:
    """`handover` and context-restart `health_condition` events from every shoal's telemetry under the data root."""
    out = []
    for f in sorted(glob.glob(os.path.join(config.data_root(), "*", "telemetry", "events-*.jsonl"))):
        day = os.path.basename(f)[len("events-"):-len(".jsonl")]
        if not (since.strftime("%Y%m%d") <= day <= until.strftime("%Y%m%d")):
            continue
        try:
            fh = open(f, errors="replace")
        except OSError:
            continue
        with fh:
            for ln in fh:
                if '"handover"' not in ln and '"context-restart"' not in ln:
                    continue
                try:
                    e = json.loads(ln)
                except ValueError:
                    continue
                t = _parse(e.get("ts_utc", ""))
                if t is None or not since <= t <= until:
                    continue
                if e.get("type") == "handover" or (e.get("type") == "health_condition"
                                                   and e.get("condition") == "context-restart"):
                    out.append(e)
    return out


def handover_cost(context_k: float, fresh_k: float) -> dict:
    """The C43 model (docs/handover.md; same as the payback above): a handover costs one refresh turn at half the context
    plus the fresh session's first write (1 h cache, 2.0x); it saves the context difference on every later call."""
    h = 0.5 * context_k + WEIGHTS["w1h"] * fresh_k
    gain = max(context_k - fresh_k, 1)
    return {"cost_m": round(h / 1000, 2), "payback_warm_calls": round(h / (WEIGHTS["read"] * gain), 1),
            "payback_cold_wakes": round(h / (WEIGHTS["w1h"] * gain), 2)}


def handovers(days: float = 1.0, until: dt.datetime | None = None) -> dict:
    """{since, until, days, handovers: [one per old session], roles: [per shoal, role and UTC day]}. A handover is keyed by
    the session it retired: `advised` and `doc-refreshed` carry it as their session, `restart` and `first-turn` as
    old_session, the condition as `session:<id>`."""
    until = until or dt.datetime.now(dt.timezone.utc)
    since = until - dt.timedelta(days=days)
    hs: dict[tuple, dict] = {}
    for e in handover_events(since, until):
        if e.get("type") == "health_condition":
            old = str(e.get("key", "")).partition("session:")[2]
        else:
            old = e.get("old_session") or e.get("session", "")
        h = hs.setdefault((e.get("shoal", ""), e.get("slug", ""), old),
                          {"shoal": e.get("shoal", ""), "slug": e.get("slug", ""), "old_session": old})
        ts, ph = e.get("ts_utc", ""), e.get("phase") or f"condition-{e.get('event', '')}"
        h.setdefault(ph, ts)
        if ph == "advised":
            h["context_k"], h["limit_k"] = _num(e.get("context_k")), _num(e.get("limit_k"))
        elif ph == "doc-refreshed":
            h["context_k"] = _num(e.get("context_k")) or h.get("context_k")
            h["advised_to_ready_s"] = _num(e.get("since_advised_s"))
        elif ph == "restart":
            h["outcome"], h["ready_to_restart_s"] = e.get("outcome", ""), _num(e.get("since_ready_s"))
        elif ph == "first-turn":
            h["new_session"], h["restart_to_first_turn_s"] = e.get("new_session", ""), _num(e.get("since_restart_s"))
            h["fresh_k"] = _num(e.get("fresh_context_k"))
        elif ph == "condition-close":
            h["condition_min"] = _num(e.get("duration_min"))
    rows = []
    for h in hs.values():
        h["status"] = ("failed" if h.get("outcome") == "failed" else "complete" if "first-turn" in h else
                       "restarted" if "restart" in h else "ready" if "doc-refreshed" in h else "advised")
        h["day"] = (h.get("restart") or h.get("advised") or h.get("condition-open") or "")[:10]
        if h.get("context_k"):
            h["fresh_measured"] = bool(h.get("fresh_k"))
            h.update(handover_cost(h["context_k"], h.get("fresh_k") or FRESH_CTX // 1000))
        rows.append(h)
    rows.sort(key=lambda h: (h["day"], h["shoal"], h["slug"]))
    roles: dict[tuple, dict] = {}
    for h in rows:
        r = roles.setdefault((h["shoal"], h["slug"], h["day"]), {"shoal": h["shoal"], "slug": h["slug"], "day": h["day"],
                                                                 "handovers": 0, "complete": 0, "failed": 0, "cost_m": 0.0})
        r["handovers"] += 1
        r["complete"] += h["status"] == "complete"
        r["failed"] += h["status"] == "failed"
        r["cost_m"] = round(r["cost_m"] + (h.get("cost_m") or 0), 2)
    return {"v": 1, "since": since.strftime("%Y-%m-%dT%H:%M:%SZ"), "until": until.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "days": days, "weights": WEIGHTS, "fresh_k_default": FRESH_CTX // 1000, "handovers": rows,
            "roles": list(roles.values())}


def render_handovers(doc: dict) -> str:
    def s(v, unit=""):
        return "-" if v is None else f"{v:g}{unit}"
    import re

    def tag(sid):  # the id without its mint stamp, as the viewer shows it
        return re.sub(r"-\d{8}-\d{6}$", "", sid)
    lines = [f"handovers, {doc['since']} to {doc['until']} ({doc['days']:g} d; cost in M base-input tokens: refresh turn at "
             f"half the context plus the fresh first write at 2.0x; fresh {doc['fresh_k_default']}k assumed where unmeasured, "
             f"marked ~)",
             f"{'shoal':<16} {'role':<18} {'day':<10} {'status':<9} {'ctx':>5} {'fresh':>6} {'adv>rdy':>8} {'rdy>rst':>8} "
             f"{'rst>1st':>8} {'cond':>6} {'cost':>5}  payback (warm calls / cold wakes)"]
    for h in doc["handovers"]:
        fresh = h.get("fresh_k") or (doc["fresh_k_default"] if h.get("context_k") else None)
        ftxt = ("" if h.get("fresh_measured", True) else "~") + s(fresh, "k")
        pay = f"{h['payback_warm_calls']:g} / {h['payback_cold_wakes']:g}" if "cost_m" in h else "-"
        lines.append(f"{tag(h['shoal'])[:16]:<16} {h['slug'][:18]:<18} {h['day']:<10} {h['status']:<9} "
                     f"{s(h.get('context_k'), 'k'):>5} {ftxt:>6} {s(h.get('advised_to_ready_s'), 's'):>8} "
                     f"{s(h.get('ready_to_restart_s'), 's'):>8} {s(h.get('restart_to_first_turn_s'), 's'):>8} "
                     f"{s(h.get('condition_min'), 'm'):>6} {s(h.get('cost_m')):>5}  {pay}")
    for r in doc["roles"]:
        lines.append(f"total {tag(r['shoal'])} {r['slug']} {r['day']}: {r['handovers']} handover(s), {r['complete']} complete, "
                     f"{r['failed']} failed, {r['cost_m']:g}M")
    if not doc["handovers"]:
        lines.append("no handover events in the window")
    return "\n".join(lines)


def cmd_context_report(a) -> int:
    if getattr(a, "handovers", False):
        doc = handovers(a.days)
        print(json.dumps(doc) if a.json else render_handovers(doc))
        return 0
    if a.snapshot:
        p = write_snapshot()
        print(f"context-report: wrote {p}" if p else "context-report: today's snapshot already exists")
        return 0
    try:
        limits = tuple(float(x) for x in str(a.limits).split(",") if x.strip())
    except ValueError:
        print(f"context-report: --limits takes thousands of tokens, comma separated (got {a.limits!r})")
        return 2
    doc = report(a.days, everything=a.all, limits=limits)
    print(json.dumps(doc) if a.json else render(doc))
    return 0


def add_parser(sub) -> None:
    p = sub.add_parser("context-report", help="Claude context cost per session from transcripts: warm vs cold, payback (read-only)")
    p.add_argument("--days", type=float, default=1.0)
    p.add_argument("--json", action="store_true")
    p.add_argument("--all", action="store_true", help="include sessions with fewer than 3 calls in the window")
    p.add_argument("--snapshot", action="store_true", help="write yesterday's per-host snapshot (if not written yet)")
    p.add_argument("--handovers", action="store_true",
                   help="handovers that happened (handover telemetry, every shoal): phases, latencies, measured cost")
    p.add_argument("--limits", default=",".join(f"{k:g}" for k in LIMITS_K),
                   help="candidate handover limits to replay, in thousands of tokens (default 300,400,500)")
    p.set_defaults(func=cmd_context_report)
