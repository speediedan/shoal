#!/usr/bin/env python3
"""Each role's recent spend, the weight quota balancing shares accounts out by (accounts.balance_load).

A role's spend is the cost-equivalent of its Claude calls over the last `[accounts] spend_window_h` (24) hours, in
millions of base input tokens per day (context_cost.WEIGHTS: cache read 0.1, 1 h write 2.0, 5 m write 1.25, uncached
input 1.0, output 5.0), summed over every session the role held in that window (its current one and the ones its spec's
generation history names) with their subagents. Counting roles treated a quiet role as a busy one: measured 2026-11-23,
a role that spent 21M in a week was moved between accounts 10 times in a day for share, each move a fresh session
with nothing to do.

Only this host's transcripts are read, incrementally (a byte offset per transcript in `role-spend.json` beside the usage
cache), so a run reads what was appended since the last one. A role on another host is weighed by the spend its own
host's quorum publishes on its `host-beat` (`beat`), the ledger being the only path between hosts. A role whose spend
is not known (another provider, a host that has not beaten) weighs as the mean known role (`accounts.balance_load`).
"""
from __future__ import annotations

import datetime as dt
import fcntl
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import config  # noqa: E402

FIRST_READ_MAX = 32 << 20  # a transcript first seen is read from at most this far before its end
BEAT_TAIL = 256 << 10  # the system writer's ledger file is read from this far before its end for the latest beats
_MEMO: dict = {}  # one measurement per process per minute: a run asks for every role at a quiet boundary


def window_h() -> float:
    """`[accounts] spend_window_h`: the hours a role's spend is averaged over (default 24, 1 to 168)."""
    import accounts
    return accounts.spend_window_h()


def cache_path() -> str:
    import subscription_usage
    return os.path.join(os.path.dirname(str(subscription_usage.cache_path())), "role-spend.json")


def _parse(ts: str) -> float | None:
    try:
        return dt.datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def sessions(specs: list, now: float, window_s: float) -> dict[str, tuple[str, str]]:
    """{conversation id prefix (8): (shoal id, slug)} for this host's Claude roles: each role's current session and the
    sessions its generation history names that ended within the window (a job id is its conversation's first 8)."""
    from roles import is_local
    out: dict[str, tuple[str, str]] = {}
    for spec in specs:
        claude = {r.slug for r in spec.roles if r.ecosystem == "claude" and is_local(r, spec)}
        for g in getattr(spec, "generations", None) or []:
            end = _parse(g.get("ended_utc") or "")
            if end is not None and end < now - window_s:
                continue
            for slug, sess in (g.get("sessions") or {}).items():
                if slug in claude and sess:
                    out[str(sess)[:8]] = (spec.sid, slug)
        for r in spec.roles:
            if r.slug in claude and r.session:
                out[r.session[:8]] = (spec.sid, r.slug)
    return out


def _transcripts(since: float, owners: dict):
    """(path, conversation id prefix) of every transcript on this host of a conversation `owners` names, written since
    `since`: each Claude account's `projects/<project>/<conversation>.jsonl` and its subagents'
    `<conversation>/subagents/*.jsonl`."""
    from backends import claude_bases
    for base in claude_bases():
        try:
            projects = list(os.scandir(os.path.join(base, "projects")))
        except OSError:
            continue
        for proj in projects:
            try:
                entries = list(os.scandir(proj.path))
            except OSError:
                continue
            for e in entries:
                if e.name[:8] not in owners:
                    continue
                try:
                    if e.name.endswith(".jsonl"):
                        if e.stat().st_mtime >= since:
                            yield e.path, e.name[:8]
                    elif e.is_dir():
                        for s in os.scandir(os.path.join(e.path, "subagents")):
                            if s.name.endswith(".jsonl") and s.stat().st_mtime >= since:
                                yield s.path, e.name[:8]
                except OSError:
                    continue


def cost_eq(usage: dict) -> float:
    """The cost-equivalent of one call's usage (context_cost.WEIGHTS)."""
    from context_cost import WEIGHTS as W
    cc = usage.get("cache_creation") or {}
    w_all = int(usage.get("cache_creation_input_tokens") or 0)
    w1h, w5m = int(cc.get("ephemeral_1h_input_tokens") or 0), int(cc.get("ephemeral_5m_input_tokens") or 0)
    if not (w1h or w5m):
        w1h = w_all  # older records carry only the total
    return (W["read"] * int(usage.get("cache_read_input_tokens") or 0) + W["w1h"] * w1h + W["w5m"] * w5m
            + W["in"] * int(usage.get("input_tokens") or 0) + W["out"] * int(usage.get("output_tokens") or 0))


def read_new(path: str, rec: dict) -> None:
    """Add the calls appended to a transcript since `rec["off"]` to `rec["hours"]` ({"YYYY-MM-DDTHH": cost-eq}). A call
    is recorded once per content block with the same message id: only its first line counts."""
    try:
        size = os.path.getsize(path)
        if size < rec.get("off", 0):  # rewritten: start over
            rec.update(off=0, hours={}, id="")
        start = rec.get("off", 0) or max(size - FIRST_READ_MAX, 0)
        with open(path, "rb") as fh:
            fh.seek(start)
            data = fh.read(size - start)
    except OSError:
        return
    if start and not rec.get("off"):
        data = data[data.find(b"\n") + 1:]  # a first read from inside the file starts at its next whole line
        start = size - len(data)
    end = data.rfind(b"\n") + 1  # a line still being written is read next time
    hours, last = rec.setdefault("hours", {}), rec.get("id", "")
    for line in data[:end].splitlines():
        if b'"usage"' not in line or b'"assistant"' not in line:
            continue
        try:
            d = json.loads(line)
        except ValueError:
            continue
        m = d.get("message") if d.get("type") == "assistant" else None
        if not isinstance(m, dict) or not isinstance(m.get("usage"), dict) or m.get("model") == "<synthetic>":
            continue
        mid = str(m.get("id") or "")
        if mid and mid == last:
            continue
        last = mid
        h = str(d.get("timestamp") or "")[:13]
        if len(h) == 13:
            hours[h] = hours.get(h, 0.0) + cost_eq(m["usage"])
    rec["off"], rec["id"] = start + end, last


def measure(specs: list, now: float | None = None) -> dict[tuple[str, str], float]:
    """{(shoal id, slug): M cost-eq per day} for this host's Claude roles with a session, over the spend window (0 for
    one that made no call in it). Reads only what was appended since the last measurement."""
    now = time.time() if now is None else now
    win = window_h() * 3600
    owners = sessions(specs, now, win)
    key = (tuple(sorted(owners.items())), int(now // 60))
    if _MEMO.get("key") == key:
        return dict(_MEMO["out"])
    out = {v: 0.0 for v in owners.values()}
    if not owners:
        return out
    path = cache_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    lock = open(path + ".lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)  # another shoal's quorum on this host measures the same transcripts
        try:
            cache = json.load(open(path))
            files = cache.get("files") if cache.get("v") == 1 else None
        except (OSError, ValueError, AttributeError):
            files = None
        files = files if isinstance(files, dict) else {}
        floor = dt.datetime.fromtimestamp(now - win, dt.timezone.utc).strftime("%Y-%m-%dT%H")
        seen = set()
        for p, id8 in _transcripts(now - win, owners):
            rec = files.setdefault(p, {})
            read_new(p, rec)
            rec["hours"] = {h: v for h, v in rec.get("hours", {}).items() if h >= floor}
            rec["role"] = "/".join(owners[id8])
            seen.add(p)
            out[owners[id8]] += sum(rec["hours"].values())
        files = {p: r for p, r in files.items() if p in seen or any(h >= floor for h in r.get("hours", {}))}
        tmp = path + ".tmp"
        with open(tmp, "w") as fh:
            json.dump({"v": 1, "files": files}, fh)
        os.replace(tmp, path)
    finally:
        lock.close()
    per_day = 24 / window_h()
    out = {k: round(v / 1e6 * per_day, 2) for k, v in out.items()}
    _MEMO.update(key=key, out=dict(out))
    return out


def beat(spec) -> dict[str, float]:
    """{slug: M per day} of this shoal's local Claude roles, for its `host-beat` (other hosts weigh them by it): this
    minute's measurement when the run's balancing already made one that covers the shoal, else one of the shoal alone."""
    got = _MEMO.get("out") if (_MEMO.get("key") or ((), -1))[1] == int(time.time() // 60) else None
    if not got or not any(sid == spec.sid for sid, _ in got):
        got = measure([spec])
    return {slug: v for (sid, slug), v in got.items() if sid == spec.sid}


def beats(spec, now: float | None = None) -> dict[str, dict[str, float]]:
    """{host: {slug: M per day}} from the newest `host-beat` with spend of each host in the shoal's ledger, read from the
    tail of the system writer's file; beats older than the spend window are left out."""
    import layout
    now = time.time() if now is None else now
    path = os.path.join(layout.group_dir(spec.ledger_dir, spec.sid), "ledger", "shoal.jsonl")
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            fh.seek(max(size - BEAT_TAIL, 0))
            lines = fh.read().splitlines()
    except OSError:
        return {}
    out: dict[str, tuple[str, dict]] = {}
    for ln in lines:
        if b'"host-beat"' not in ln or b'"spend"' not in ln:
            continue
        try:
            e = json.loads(ln)
        except ValueError:
            continue
        host, sp, ts = e.get("host"), e.get("spend"), str(e.get("ts") or "")
        t = _parse(ts)
        if not host or not isinstance(sp, dict) or t is None or t < now - window_h() * 3600:
            continue
        if ts >= out.get(host, ("", {}))[0]:
            out[host] = (ts, {str(k): float(v) for k, v in sp.items() if isinstance(v, (int, float))})
    return {h: v[1] for h, v in out.items()}


def weights(specs: list, now: float | None = None) -> dict[tuple[str, str], float | None]:
    """{(shoal id, slug): M per day, or None when unknown} for every role with a session in `specs`: measured here for
    this host's Claude roles, from its host's latest beat for another host's."""
    from roles import host_of, is_local
    local = measure(specs, now)
    out: dict[tuple[str, str], float | None] = {}
    for spec in specs:
        remote = None
        for r in spec.roles:
            if not (r.session and r.ecosystem):
                continue
            k = (spec.sid, r.slug)
            if k in local:
                out[k] = local[k]
            elif is_local(r, spec):
                out[k] = None
            else:
                if remote is None:
                    remote = beats(spec, now)
                out[k] = (remote.get(host_of(r, spec)) or {}).get(r.slug)
    return out

