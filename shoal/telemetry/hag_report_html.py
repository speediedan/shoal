#!/usr/bin/env python3
"""Per-incarnation HAG system review as HTML (stdlib only).

Usage:
  hag_report_html.py --hag ID --since ISO --until ISO --out FILE
                     [--coord SESID] [--coord-dir DIR] [--queue PATH] [--repo DIR] [--gpu]

Reads telemetry JSONL (new HAG dirs under /mnt/cache_extended F:/speediedan/hags, legacy
versioned path as fallback), the coordinator transcript (message-type counts per recipient),
the queue file's git history (consumer transitions without touching the session), and
optionally gated-pool union-busy share via `az`.

All times UTC; state times in seconds; rows sum to the window where fully observed.
"""
import argparse
import datetime
import html
import json
import os
import re
import subprocess
import sys

CACHE = "/mnt/cache_extended/F:/speediedan/hags"
REPO = "/home/speediedan/repos/distributed-insight"


def sh(*a, timeout=120):
    r = subprocess.run(list(a), capture_output=True, text=True, timeout=timeout)
    return r.stdout


def parse_ts(s):
    return datetime.datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=datetime.timezone.utc)


def load_events(hag, since, until):
    evs = []
    dirs = [os.path.join(CACHE, hag, "telemetry"),
            os.path.join(REPO, ".agents/hags", hag, "telemetry")]
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
                        t = parse_ts(e["ts_utc"])
                    except Exception:
                        continue
                    if since <= t <= until:
                        evs.append(e)
    return sorted(evs, key=lambda e: e["ts_utc"])


SUBS = ["local-ci", "remote-ci", "user-auth", "user-design", "session-blocked",
        "queue-empty"]


def state_table(evs, w0, w1):
    # Attribute each segment by the ENTERING event's to_state/to_sub (the transition that
    # started it), not the exiting event's from_* (often untranscribed). Segments before the
    # first event are unobserved, never zero-filled.
    tot = (w1 - w0).total_seconds()
    by = {}
    for e in evs:
        if e.get("type") == "session_transition":
            by.setdefault(e["session"], []).append((parse_ts(e["ts_utc"]), e))
    rows = []
    for s, l in sorted(by.items()):
        l.sort()
        agg = {"active": 0.0, "idle": 0.0, "waiting:all": 0.0,
               **{f"waiting:{x}": 0.0 for x in SUBS}, "waiting:unstated": 0.0,
               "paused/offline": 0.0}
        unobs = (l[0][0] - w0).total_seconds() if l else tot

        def add(st, sub, secs):
            if st == "active":
                agg["active"] += secs
            elif st == "idle":
                agg["idle"] += secs
            elif st == "waiting":
                agg["waiting:all"] += secs
                agg[f"waiting:{sub}" if f"waiting:{sub}" in agg else "waiting:unstated"] += secs
            elif st in ("paused", "offline"):
                agg["paused/offline"] += secs
            else:
                agg["waiting:unstated"] += secs

        cur_t, cur, cur_sub = w0, None, ""
        for t, e in l:
            if cur is not None:
                add(cur, cur_sub, (t - cur_t).total_seconds())
            cur_t, cur, cur_sub = t, e.get("to_state"), e.get("to_sub", "")
        last = l[-1][1]
        add(last.get("to_state"), last.get("to_sub", ""), (w1 - cur_t).total_seconds())
        rows.append((s, agg, unobs))
    return rows, tot


def transitions(evs):
    ok = bad = rec = inv = 0
    for e in evs:
        if e.get("type") != "session_transition":
            continue
        st, sub = e.get("to_state"), e.get("to_sub", "")
        if st not in ("active", "waiting", "idle", "paused", "offline") or \
           (st == "waiting" and sub not in SUBS):
            bad += 1
            inv += 1
        else:
            ok += 1
    for e in evs:
        if e.get("type") == "watch_event" and "respawn" in e.get("kind", ""):
            bad += 1
            rec += 1
    return ok, bad, rec, inv


def coord_table(coord, cdir):
    out = sh("openchamber", "session", "messages", "--session", coord, "--dir", cdir,
             "--all", timeout=180)
    instruct = {"A": 0, "B": 0, "C": 0}
    reports = {"A": 0, "B": 0, "C": 0}
    for m in re.finditer(r"LANE-INSTRUCT[^\n]*", out):
        seg = m.group(0)
        for k, pat in (("A", "ie-integration"), ("B", "jlens"), ("C", "architect|QUEUE|fable")):
            if re.search(pat, seg, re.I):
                instruct[k] += 1
                break
    for m in re.finditer(r"LANE-REPORT[^\n]*", out):
        seg = out[max(0, m.start() - 200):m.start()]
        placed = False
        for k, pat in (("A", "ie-integration"), ("B", "jlens"), ("C", "architect|fable")):
            if re.search(pat, seg, re.I):
                reports[k] += 1
                placed = True
                break
        if not placed:
            reports["C"] += 0
    n_user = out.count("**User**")
    n_asst = out.count("**Assistant**")
    return instruct, reports, n_user, n_asst


def fable_rows(repo, qpath, since, until):
    log = sh("git", "-C", repo, "log", "--format=%H|%aI|%s", "--", qpath, timeout=60)
    rows = []
    for line in log.splitlines():
        parts = line.split("|", 2)
        if len(parts) < 3:
            continue
        h, ts, msg = parts
        try:
            t = datetime.datetime.fromisoformat(ts)
            if t.tzinfo is None:
                t = t.replace(tzinfo=datetime.timezone.utc)
        except Exception:
            continue
        if not (since <= t <= until):
            continue
        tloc = pdt(t).strftime("%Y%m%d_%H%M%S")
        try:
            content = sh("git", "-C", repo, "show", f"{h}:{qpath}", timeout=30)
        except Exception:
            continue
        m = re.search(r"## STATUS(.*?)(?:\n## |\Z)", content, re.S)
        st = ac = up = ""
        if m:
            sec = m.group(1)
            st = re.search(r"^- state:\s*(.+)$", sec, re.M)
            ac = re.search(r"^- active-issue:\s*(.+)$", sec, re.M)
            up = re.search(r"^- updated-utc:\s*(.+)$", sec, re.M)
            st = st.group(1).strip()[:60] if st else ""
            ac = ac.group(1).strip()[:80] if ac else ""
            up = up.group(1).strip()[:20] if up else ""
        rows.append((tloc, msg[:90], st, ac, up))
    return sorted(rows)


def gpu_share(since, until):
    try:
        out = sh("az", "pipelines", "build", "list", "--organization",
                 "https://dev.azure.com/speediedan", "--project", "interpretune",
                 "--status", "completed", "--top", "60",
                 "--query", "[].{start:startTime,finish:finishTime}", "-o", "json", timeout=90)
        iv = []
        for b in json.loads(out or "[]"):
            try:
                s = parse_ts((b["start"] or "")[:19] + "Z")
                f = parse_ts((b["finish"] or "")[:19] + "Z")
            except Exception:
                continue
            a, bb = max(s, since), min(f, until)
            if bb > a:
                iv.append((a, bb))
        iv.sort()
        merged = []
        for a, b in iv:
            if merged and a <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], b)
            else:
                merged.append([a, b])
        tot = sum((b - a).total_seconds() for a, b in merged)
        w = (until - since).total_seconds()
        return len(iv), tot, 100 * tot / w if w else 0
    except Exception as ex:
        return 0, 0, -1


try:
    from zoneinfo import ZoneInfo
    LOCAL = ZoneInfo("America/Los_Angeles")
except Exception:
    LOCAL = None


def pdt(ts_utc):
    t = ts_utc if isinstance(ts_utc, datetime.datetime) else parse_ts(ts_utc)
    return t.astimezone(LOCAL) if LOCAL else t


def stamp(dt):
    return pdt(dt).strftime("%Y%m%d_%H%M%S")


def dur(secs):
    h, r = divmod(int(secs), 3600)
    m, s = divmod(r, 60)
    return f"{h}h {m}m {s}s"


def session_names(cdir):
    try:
        out = sh("openchamber", "session", "list", "--dir", cdir, "--all",
                 "--limit", "100", "--json", timeout=60)
        d = json.loads(out)
        ss = d if isinstance(d, list) else d.get("sessions", d)
        return {s["id"]: s.get("title", "") for s in ss if isinstance(s, dict) and "id" in s}
    except Exception:
        return {}


SYSTEM_PREFIXES = ("ORCHESTRATOR", "MONITOR-FIRED", "QUEUE-WATCH", "LANES-WATCH",
                   "PIPELINE-WATCH", "RELAY-", "REGISTRY", "HAG ", "TELEMETRY",
                   "STALL ", "DRILL", "TYPO", "WORDING", "PROTOCOL", "GATE ",
                   "PIPELINE-STARVATION", "LIVENESS", "WATCHERS", "THREE ",
                   "CORRECTION", "HAG RESTRUCTURE", "QUEUE HYGIENE", "FABLE ",
                   "MIS-", "REFILL", "QUEUE ", "APPROVAL", "LIVE ", "SESSION-LEVEL",
                   "LANE-REPORT", "LANE-INSTRUCT", "You are ", "RESUME ",
                   "REFILL ", "GATE ORDERS", "APPROVALS ", "STATUS ") 


def audit_user_turns(out):
    blocks = re.split(r"\*\*User\*\*", out)[1:]
    sys_n = man_n = 0
    man_first = []
    for b in blocks:
        lines = [x for x in (l.strip() for l in b.strip().splitlines()) if x]
        lines = [x for x in lines if not re.match(r"^\*20\d", x)]
        if not lines:
            continue
        head = lines[0][:120]
        if head.startswith(tuple(SYSTEM_PREFIXES)):
            sys_n += 1
        else:
            man_n += 1
            if len(man_first) < 15:
                man_first.append(head)
    return sys_n, man_n, man_first


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hag", required=True)
    ap.add_argument("--since", required=True)
    ap.add_argument("--until", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--coord", default="")
    ap.add_argument("--coord-dir", default="/home/speediedan/repos/interpretune")
    ap.add_argument("--queue", default=".agents/hags/it-mvp-2026-09/queues/it-architect.md")
    ap.add_argument("--repo", default=REPO)
    ap.add_argument("--gpu", action="store_true")
    a = ap.parse_args()
    since, until = parse_ts(a.since), parse_ts(a.until)
    evs = load_events(a.hag, since, until)
    rows, tot = state_table(evs, since, until)
    ok, bad, rec, inv = transitions(evs)
    merged = [e for e in evs if e.get("type") == "pr_merged"]
    closed = [e for e in evs if e.get("type") == "issue_closed"]
    names = session_names(a.coord_dir)
    eco_of = {}
    for e in evs:
        eco_of.setdefault(e.get("session"), e.get("ecosystem", "?"))

    def pct(v):
        return f"{v:.0f} ({100 * v / tot:.0f}%)" if tot else f"{v:.0f}"

    h = ["<html><head><meta charset='utf-8'><title>HAG review</title>",
         "<style>body{font-family:sans-serif;margin:2em}table{border-collapse:collapse}",
         "td,th{border:1px solid #999;padding:4px 8px;font-size:13px}th{background:#eee}</style>",
         "</head><body>",
         f"<h1>HAG {html.escape(a.hag)} incarnation</h1>",
         f"<p>HAG incarnation duration: {dur(tot)}</p>",
         f"<p>Start (PDT): {stamp(since)}</p>",
         f"<p>End (PDT): {stamp(until)}</p>",
         f"<p>events: {len(evs)} · PRs merged (telemetry): " +
         ", ".join(html.escape(str(e.get('pr'))) for e in merged) +
         f" · issues closed: " + ", ".join(html.escape(str(e.get('issue'))) for e in closed) + "</p>",
         "<h2>State seconds per session (substates sum to waiting:all; rows sum to window where observed)</h2>",
         "<table><tr><th>ecosystem</th><th>session</th><th>active</th><th>idle</th><th>waiting:all</th>"]
    h += [f"<th>waiting:{s}</th>" for s in SUBS] + ["<th>waiting:unstated</th><th>unobserved</th></tr>"]
    for s, agg, unobs in rows:
        title = names.get(s, "")
        label = f"{html.escape(s)} ({html.escape(title)})" if title else html.escape(s)
        eco = eco_of.get(s, "?")
        h.append("<tr><td>{}</td><td>{}</td>{}</tr>".format(
            html.escape(str(eco)), label,
            "".join(f"<td>{pct(agg[k])}</td>" for k in
                    ["active", "idle", "waiting:all"] + [f"waiting:{x}" for x in SUBS] +
                    ["waiting:unstated"]) + f"<td>{unobs:.0f}</td>"))
    h.append("</table>")
    h.append(f"<h2>Transitions</h2><p>successful: {ok} · failed: {bad} · recovered autonomously: {rec} · invalid offered: {inv}</p>")
    if a.coord:
        ins, rep, nu, na = coord_table(a.coord, a.coord_dir)
        sys_n, man_n, man_first = audit_user_turns(
            sh("openchamber", "session", "messages", "--session", a.coord, "--dir",
               a.coord_dir, "--all", timeout=180))
        h.append("<h2>Coordinator messages</h2><table><tr><th>recipient</th><th>INSTRUCTs sent</th><th>REPORTs received</th></tr>")
        for k, nm in (("A", "ie-integration-r2"), ("B", "jlens-expert-r2"), ("C", "it-architect")):
            h.append(f"<tr><td>{k} ({nm})</td><td>{ins[k]}</td><td>{rep[k]}</td></tr>")
        h.append("</table>")
        h.append(f"<p>coordinator transcript blocks: {nu} user-role / {na} assistant — of which user-role system (sends, fires, relays): {sys_n}, manual: {man_n}</p>")
        if man_first:
            h.append("<p>manual user-role blocks (first lines):</p><ul>" +
                     "".join(f"<li>{html.escape(x)}</li>" for x in man_first) + "</ul>")
    slug = os.path.basename(a.queue).replace(".md", "")
    h.append("<h2>Consumer queue-file commits (no session contact)</h2>")
    h.append("<table><tr><th>consumer</th><th>queue file</th><th>ecosystem</th><th>time (PDT)</th><th>commit subject</th><th>state</th><th>active-issue</th><th>updated-utc</th></tr>")
    frows = fable_rows(a.repo, a.queue, since, until)
    for t, msg, st, ac, up in frows:
        h.append(f"<tr><td>{html.escape(slug)}</td><td>{html.escape(a.queue)}</td><td>claude (heterogeneous consumer)</td><td>{html.escape(t)}</td><td>{html.escape(msg)}</td><td>{html.escape(st)}</td><td>{html.escape(ac)}</td><td>{html.escape(up)}</td></tr>")
    h.append("</table>")
    if a.gpu:
        n, tot_s, pct = gpu_share(since, until)
        h.append(f"<h2>GPU pool</h2><p>completed builds overlapping: {n} · union-busy {tot_s:.0f}s = {pct:.1f}% of window</p>")
    h.append("</body></html>")
    open(a.out, "w").write("\n".join(h))
    print(f"wrote {a.out} ({len(evs)} events, {len(frows)} consumer commits)")


if __name__ == "__main__":
    main()
