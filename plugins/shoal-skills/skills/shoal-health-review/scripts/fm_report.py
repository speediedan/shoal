#!/usr/bin/env python3
"""Read-only failure-mode (FM) metrics for a SHOAL group, derived from the streams it already writes.

Metric names carry the catalog id they measure (references/failure-modes.md), so a review can read
frequency and duration per failure mode and see whether each is trending down. Sources (read-only):
the ledger (<ledger dir>/shoals/<id>/ledger/*.jsonl, from the spec), telemetry
(<data root>/<id>/telemetry/events-*.jsonl) and job unit records (<state dir>/jobs/*.json + *.rc,
which do not survive a reboot). Standard library only.

usage: fm_report.py --spec <shoal.json> [--data-root DIR] [--days 7] [--json]
       (--data-root defaults to $SHOAL_DATA_ROOT; your host map names it)
"""
import argparse, collections, datetime as dt, glob, json, os, re

HOLD_TTL_MIN = 240.0          # blockers.DEFAULTS["hold_ttl_min"]
JOB_GAP_MIN = 10.0            # FM-7 threshold: job end -> next ledger write by the role
ACTIVE_SILENT_MIN = 90.0      # FM-1 proxy: active with no ledger write (stale-active ladder step 1)
FM6_PAIR_S = 300              # FM-6: blank-target wait within 5 min of a targeted one


def P(s):
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))


def ikey(x):
    return (x or "").split(":", 1)[0].lower()


def load_ledger(root):
    evs = []
    for f in glob.glob(os.path.join(root, "*.jsonl")):
        for line in open(f):
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if e.get("ts"):
                e["_t"] = P(e["ts"])
                evs.append(e)
    return sorted(evs, key=lambda e: e["_t"])


def load_tel(d):
    evs = []
    for f in sorted(glob.glob(os.path.join(d, "events-*.jsonl"))):
        for line in open(f):
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if e.get("ts_utc"):
                e["_t"] = P(e["ts_utc"])
                evs.append(e)
    return sorted(evs, key=lambda e: e["_t"])


def split_days(t0, t1, w0, w1, acc, key):
    """Add the seconds of [t0,t1) clipped to [w0,w1) to acc[(day, key)] split by UTC day."""
    t0, t1 = max(t0, w0), min(t1, w1)
    while t0 < t1:
        nd = dt.datetime.combine(t0.date() + dt.timedelta(days=1), dt.time(), t0.tzinfo)
        e = min(nd, t1)
        acc[(t0.date().isoformat(), key)] += (e - t0).total_seconds()
        t0 = e


def role_timelines(led):
    """Per role: list of snapshots (t, posture, sub, next, next_blocked, meta, target)."""
    tl, cur = collections.defaultdict(list), {}
    for e in led:
        r = e.get("by")
        if e["type"] not in ("queue", "state") or not r:
            continue
        c = cur.setdefault(r, {"posture": None, "sub": "", "next": [], "nb": {}, "meta": {}, "target": ""})
        if e["type"] == "queue":
            c.update(posture=e.get("posture") or c["posture"], next=e.get("next") or [],
                     nb=e.get("next_blocked") or {}, meta=e.get("next_blocked_meta") or {})
        else:
            c.update(posture=e.get("state"), sub=e.get("sub") or "", target=e.get("target") or "")
        tl[r].append((e["_t"], dict(c)))
    return tl


def trend(vals: list[float]) -> str:
    """down | flat | up: the later half of the window against the earlier half (per-day means). A
    failure mode whose rate is not going down across reviews is the signal for a design change."""
    if len(vals) < 4:
        return "n/a"
    h = len(vals) // 2
    a, b = sum(vals[:h]) / h, sum(vals[h:]) / (len(vals) - h)
    if a == b == 0:
        return "flat"
    if b < 0.8 * a:
        return "down"
    return "up" if b > 1.25 * max(a, 1e-9) else "flat"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--spec", required=True, help="the group's shoal.json")
    ap.add_argument("--data-root", default=os.environ.get("SHOAL_DATA_ROOT", ""))
    ap.add_argument("--state-dir", default="", help="default /tmp/<id> (shoal's scratch state)")
    ap.add_argument("--days", type=float, default=7)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    spec = json.load(open(a.spec))
    S = spec["shoal"]
    if not a.data_root:
        ap.error("no data root: pass --data-root or set SHOAL_DATA_ROOT (your host map names it)")
    roles = {r["slug"] for r in spec.get("roles", []) if r.get("fill") != "none"}
    state = a.state_dir or os.path.join("/tmp", S)
    led = load_ledger(os.path.join(spec["ledger"]["dir"], "shoals", S, "ledger"))
    tel = load_tel(os.path.join(a.data_root, S, "telemetry"))
    w1 = dt.datetime.now(dt.timezone.utc)
    w0 = w1 - dt.timedelta(days=a.days)
    inw = lambda t: w0 <= t < w1  # noqa: E731
    day = lambda t: t.date().isoformat()  # noqa: E731
    C = collections.Counter()            # (day, metric) -> count
    H = collections.Counter()            # (day, metric) -> seconds

    # --- health ladder actions (telemetry watch_event kind=health) -> wakes/pages per condition
    rx = re.compile(r"^(\S+) step(\d+):(wake|page) -> ")
    for e in tel:
        if e.get("type") != "watch_event" or not inw(e["_t"]):
            continue
        if e.get("kind") == "health":
            m = rx.match(e.get("note", ""))
            if m:
                C[(day(e["_t"]), f"{m.group(3)}:{m.group(1)}")] += 1
                if m.group(3) == "page":
                    C[(day(e["_t"]), "pages:total")] += 1
            elif "page" in e.get("note", ""):
                C[(day(e["_t"]), "pages:total")] += 1
                C[(day(e["_t"]), "page:user-waits-digest")] += 1
        elif e.get("kind") == "stall-suspect":
            C[(day(e["_t"]), "pages:total")] += 1

    tls = role_timelines(led)
    last_write = collections.defaultdict(list)
    for e in led:
        if e.get("by"):
            last_write[e["by"]].append(e["_t"])

    # --- FM-9 hold renewals; FM-2/3/10 posture x blocker intervals; FM-1 proxy; FM-8
    R = collections.Counter()            # (role, metric) -> seconds
    for r, seq in tls.items():
        if r not in roles:
            continue  # system, operator and relay writers, and roles no longer in the spec
        lw = last_write[r][-1]
        end = w1 if (w1 - lw).total_seconds() < 86400 else lw   # retired role: stop at its last write
        prev_meta = {}
        for i, (t, c) in enumerate(seq):
            t1 = seq[i + 1][0] if i + 1 < len(seq) else end
            for k, m in (c["meta"] or {}).items():
                n = int(m.get("n", 0) or 0)
                if n > int(prev_meta.get(k, {}).get("n", 0) or 0) and inw(t) and k in prev_meta:
                    C[(day(t), "FM9:hold_renewals")] += 1
            prev_meta = c["meta"] or {}
            nxt = [ikey(x) for x in c["next"]]
            nbk = {ikey(k) for k in c["nb"]}
            holds = {ikey(k): v for k, v in c["nb"].items() if str(v).startswith("hold:")}
            meta = {ikey(k): v for k, v in (c["meta"] or {}).items()}
            # stale = hold first recorded more than hold_ttl_min ago (age, not truth: a proxy for FM-3)
            stale = [k for k in holds if (meta.get(k) or {}).get("first")
                     and (t - P(meta[k]["first"])).total_seconds() / 60 > HOLD_TTL_MIN]
            all_blocked = bool(nxt) and set(nxt) <= nbk
            if c["posture"] == "idle" and all_blocked:
                split_days(t, t1, w0, w1, H, "FM2:idle_all_next_blocked_h")
                split_days(t, t1, w0, w1, R, (r, "idle_all_blocked"))
                if stale:
                    split_days(t, t1, w0, w1, H, "FM3:idle_on_hold_gt4h_h")
                    split_days(t, t1, w0, w1, R, (r, "idle_on_hold_gt4h"))
            if c["posture"] == "idle":
                split_days(t, t1, w0, w1, R, (r, "idle_total"))
            if c["posture"] == "waiting" and c["sub"] == "local-ci" and stale:
                split_days(t, t1, w0, w1, H, "FM10:localci_with_stale_hold_h")
            if c["posture"] == "active":
                # silent active: no ledger write by the role for > ACTIVE_SILENT_MIN inside the segment
                ws = [x for x in last_write[r] if t <= x < t1] + [t1]
                p = t
                for x in ws:
                    if (x - p).total_seconds() / 60 > ACTIVE_SILENT_MIN:
                        split_days(p + dt.timedelta(minutes=ACTIVE_SILENT_MIN), x, w0, w1, H, "FM1:active_silent_h")
                        C[(day(p), "FM1:active_silent_segments")] += 1 if inw(p) else 0
                    p = x
            # FM-6: blank-target CI wait right after a targeted one
            if (c["posture"] == "waiting" and c["sub"] in ("local-ci", "remote-ci") and not c["target"] and i
                    and seq[i - 1][1]["target"] and seq[i - 1][1]["sub"] == c["sub"] and (t - seq[i - 1][0]).total_seconds() <= FM6_PAIR_S and inw(t)):
                C[(day(t), "FM6:target_blanked")] += 1

    # FM-8: done items still listed in next
    for d in (e for e in led if e["type"] == "done"):
        r, k = d.get("by"), ikey(d.get("item"))
        seq = tls.get(r, [])
        later = [(t, c) for t, c in seq if t > d["_t"]]
        t_in = next((t for t, c in later if k in {ikey(x) for x in c["next"]}), None)
        if t_in is None:
            continue
        t_out = next((t for t, c in later if t > t_in and k not in {ikey(x) for x in c["next"]}), w1)
        if inw(t_in) or inw(t_out):
            C[(day(max(t_in, w0)), "FM8:done_items_in_next")] += 1
            split_days(t_in, t_out, w0, w1, H, "FM8:done_in_next_h")

    # FM-7: job unit end (rc mtime) -> verdict telemetry -> next ledger write by the role
    verdicts = {}
    for e in tel:
        if e.get("kind") == "job-verdict":
            verdicts.setdefault(e.get("note", "").split(" ")[0], e["_t"])
    jl = []
    for jf in glob.glob(os.path.join(state, "jobs", "*.json")):
        rc = jf[:-5] + ".rc"
        if not os.path.exists(rc):
            continue
        try:
            j = json.load(open(jf))
        except ValueError:
            continue
        end = dt.datetime.fromtimestamp(os.path.getmtime(rc), dt.timezone.utc)
        if not inw(end):
            continue
        slug = j.get("slug", "")
        st = [c for t, c in tls.get(slug, []) if t <= end]
        if slug not in roles or not st or st[-1]["posture"] != "waiting":
            continue  # only jobs the role was waiting on at end
        nw = next((x for x in last_write.get(slug, []) if x > end), None)
        gap = (nw - end).total_seconds() / 60 if nw else (w1 - end).total_seconds() / 60
        v = verdicts.get(j.get("unit", ""))
        C[(day(end), "jobs:ended")] += 1
        if v is None:
            C[(day(end), "FM7:no_verdict")] += 1
        if gap > JOB_GAP_MIN:
            C[(day(end), "FM7:end_to_write_gt10m")] += 1
            if v is None:  # no waiter verdict AND a long gap: the strong FM-7 signature
                C[(day(end), "FM7:no_verdict_and_gap")] += 1
        jl.append((slug, gap, (v - end).total_seconds() if v else None))

    days = sorted({d for d, _ in C} | {d for d, _ in H})
    metrics = sorted({m for _, m in C}) + sorted({m for _, m in H})
    if a.json:
        print(json.dumps({"window": [w0.isoformat(), w1.isoformat()],
                          "counts": {f"{d}|{m}": v for (d, m), v in C.items()},
                          "hours": {f"{d}|{m}": round(v / 3600, 2) for (d, m), v in H.items()}}, indent=1))
        return
    print(f"window {w0:%Y-%m-%dT%H:%MZ} .. {w1:%Y-%m-%dT%H:%MZ}  (UTC days)")
    print("metric".ljust(36) + "".join(d[5:].rjust(7) for d in days) + "  total  trend")
    for m in metrics:
        src = H if m.endswith("_h") else C
        vals = [src.get((d, m), 0) / (3600 if src is H else 1) for d in days]
        fmt = (lambda v: f"{v:7.1f}") if src is H else (lambda v: f"{int(v):7d}")
        print(m.ljust(36) + "".join(fmt(v) for v in vals) + fmt(sum(vals)) + "  " + trend(vals))
    RR = collections.Counter()
    for (_d, (r, m)), v in R.items():
        RR[(r, m)] += v
    R = RR
    roles = sorted({r for r, _ in R})
    print("per role (h): idle_total / idle_all_next_blocked / idle_on_hold_gt4h")
    for r in roles:
        print(f"  {r:34s} {R[(r,'idle_total')]/3600:6.1f} {R[(r,'idle_all_blocked')]/3600:6.1f} {R[(r,'idle_on_hold_gt4h')]/3600:6.1f}")
    lat = sorted(x[2] for x in jl if x[2] is not None)
    if lat:
        print(f"job end->verdict latency s: n={len(lat)} p50={lat[len(lat)//2]:.0f} p90={lat[int(len(lat)*.9)]:.0f} max={lat[-1]:.0f}")
    g = sorted(x[1] for x in jl)
    if g:
        print(f"job end->next ledger write min: n={len(g)} p50={g[len(g)//2]:.1f} p90={g[int(len(g)*.9)]:.1f}")


if __name__ == "__main__":
    main()
