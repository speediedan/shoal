#!/usr/bin/env python3
"""Per-incarnation HAG report (Markdown + HTML, stdlib only). Spec-driven.

  hag.py report --spec hag.json [--gen rN | --hours N | --since ISO --until ISO]
                [--out-md F] [--out-html F] [--gpu] [--coord-transcript]

Window: --gen reads the generation record in the spec (started_utc ..
ended_utc-or-now); --hours is a rolling window; --since/--until explicit.
Default output: <HAG_ROOT>/<hag>/reports/incarnation-<gen>-<YYYYMMDDTHHMM>-<...>.md
(versioned, small, cited from hag_system_reviews.md) and the same stem .html
under <HAG_DATA_ROOT>/<hag>/reports/ (bulk).

Sections: header + protocol pins, outcomes, time-in-state per session,
transition quality (valid/aliased/invalid), wait discipline (stale waits),
watch events + stall recovery latency, coordinator comms, queue-file
history (git), optional GPU pool share, failure-mode signatures (mapped to
hag_failure_modes.md section numbers), and a VERDICT block the reviewer
fills (protocol proposal or explicit no-change — the rule from
hag_system_reviews.md). Every number is computed from events; a missing
instrument prints as `unobserved`, never as zero.
"""
from __future__ import annotations

import argparse
import datetime
import html as _html
import json
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
from hag_telemetry import (VALID_SUB, classify_transition, load, resolve_dir)  # noqa: E402
from roles import HAG_DATA_ROOT, HAG_ROOT, HagSpec  # noqa: E402

try:
    from zoneinfo import ZoneInfo
    LOCAL = ZoneInfo("America/Los_Angeles")
except Exception:  # pragma: no cover
    LOCAL = None

SUBS = ["local-ci", "remote-ci", "user-auth", "user-design", "session-blocked", "queue-empty"]
TOOL_PATH = ("/home/speediedan/.nvm/versions/node/v24.13.0/bin:"
             "/home/speediedan/.bun/bin:/usr/bin:/bin")
ENV = dict(os.environ, PATH=TOOL_PATH + ":" + os.environ.get("PATH", ""))


def parse_ts(s):
    return datetime.datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc)


def fmt(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def local(dt):
    return dt.astimezone(LOCAL).strftime("%Y-%m-%d %H:%M %Z") if LOCAL else fmt(dt)


def dur(secs):
    secs = int(max(secs, 0))
    h, r = divmod(secs, 3600)
    m, s = divmod(r, 60)
    return f"{h}h{m:02d}m" if h else f"{m}m{s:02d}s"


def sh(argv, timeout=60):
    r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, env=ENV)
    return r.stdout if r.returncode == 0 else ""


# ---- analyses ---------------------------------------------------------------

def state_rows(evs, w0, w1, spec):
    tot = (w1 - w0).total_seconds()
    by = {}
    for e in evs:
        if e.get("type") == "session_transition":
            by.setdefault(e["session"], []).append((parse_ts(e["ts_utc"]), e))
    slug_of = {r.session: r.slug for r in spec.roles if r.session}
    for g in spec.generations:
        for slug, sid in (g.get("sessions") or {}).items():
            slug_of.setdefault(sid, slug)
    rows = []
    for s, l in sorted(by.items()):
        l.sort(key=lambda x: x[0])
        agg = {"active": 0.0, "idle": 0.0, "waiting": 0.0, **{f"w:{x}": 0.0 for x in SUBS},
               "w:other": 0.0, "paused/offline": 0.0}
        unobs = (l[0][0] - w0).total_seconds()

        def add(st, sub, secs):
            if st in ("active", "idle"):
                agg[st] += secs
            elif st == "waiting":
                agg["waiting"] += secs
                agg[f"w:{sub}" if f"w:{sub}" in agg else "w:other"] += secs
            elif st in ("paused", "offline"):
                agg["paused/offline"] += secs
            else:
                agg["w:other"] += secs

        cur_t, cur, cur_sub = None, None, ""
        for t, e in l:
            if cur is not None:
                add(cur, cur_sub, (t - cur_t).total_seconds())
            cur_t, cur, cur_sub = t, e.get("to_state"), e.get("to_sub", "")
        add(cur, cur_sub, (w1 - cur_t).total_seconds())
        slug = slug_of.get(s, e.get("slug", "") if l else "")
        eco = l[0][1].get("ecosystem", "?")
        rows.append((slug or "?", eco, s, agg, unobs, len(l)))
    return rows, tot


def transition_quality(evs):
    q = {"valid": 0, "aliased": 0, "invalid": 0}
    bad, per = [], {}
    for e in evs:
        if e.get("type") != "session_transition":
            continue
        c = classify_transition(e)
        q[c] += 1
        per.setdefault(e["session"], {"valid": 0, "aliased": 0, "invalid": 0})[c] += 1
        if c != "valid":
            bad.append((e["ts_utc"], e["session"], e.get("to_state"), e.get("to_sub_raw") or e.get("to_sub"), c))
    return q, per, bad


def wait_discipline(evs, w1):
    """Waiting segments emitted with rescan_min: stale = segment outlived its rescan."""
    segs, stale = [], []
    by = {}
    for e in evs:
        if e.get("type") == "session_transition":
            by.setdefault(e["session"], []).append(e)
    for s, l in by.items():
        for i, e in enumerate(l):
            if e.get("to_state") != "waiting":
                continue
            t0 = parse_ts(e["ts_utc"])
            t1 = parse_ts(l[i + 1]["ts_utc"]) if i + 1 < len(l) else w1
            secs = (t1 - t0).total_seconds()
            segs.append((s, e.get("to_sub", ""), secs, e.get("holder", "")))
            try:
                rescan = float(e.get("rescan_min") or 0)
            except ValueError:
                rescan = 0
            if rescan and secs > rescan * 60 * 1.5:
                stale.append((e["ts_utc"], s, e.get("to_sub", ""), dur(secs), f"{rescan:g}m", e.get("holder", "")))
    by_sub = {}
    for _s, sub, secs, _h in segs:
        by_sub.setdefault(sub or "unstated", []).append(secs)
    return segs, stale, by_sub


def watch_summary(evs):
    kinds = {}
    for e in evs:
        if e.get("type") == "watch_event":
            kinds[e.get("kind", "?")] = kinds.get(e.get("kind", "?"), 0) + 1
    fires = {}
    for e in evs:
        if e.get("type") == "coord_comms" and e.get("direction") == "watch-fire":
            k = f"{e.get('peer', '?')}: {e.get('note', '')[:40]}"
            fires[k] = fires.get(k, 0) + 1
    # stall recovery: stall-suspect -> next session_transition naming one of the suspects
    recov = []
    trans = [e for e in evs if e.get("type") == "session_transition"]
    for e in evs:
        if e.get("type") == "watch_event" and e.get("kind") == "stall-suspect":
            t0 = parse_ts(e["ts_utc"])
            slugs = re.findall(r"([\w-]+):\s*(?:STALE|NO HEARTBEAT|STALE WAIT)", e.get("note", ""))
            nxt = next((x for x in trans if parse_ts(x["ts_utc"]) > t0
                        and (x.get("slug") in slugs or any(sl in x.get("session", "") for sl in slugs))), None)
            recov.append((e["ts_utc"], ", ".join(slugs) or "?",
                          dur((parse_ts(nxt["ts_utc"]) - t0).total_seconds()) if nxt else "unrecovered in window"))
    return kinds, fires, recov


def coord_summary(evs):
    dirs, gauges = {}, []
    for e in evs:
        if e.get("type") != "coord_comms":
            continue
        k = f"{e.get('direction', '?')}/{e.get('peer', '?')}"
        dirs[k] = dirs.get(k, 0) + 1
        if e.get("queue_depth_gpu") not in (None, ""):
            gauges.append((e["ts_utc"], e.get("queue_depth_gpu"), e.get("queued_unapproved", ""),
                           e.get("local_gpu_jobs", ""), e.get("board_open", "")))
    return dirs, gauges


def queue_summary(evs):
    adds = [e for e in evs if e.get("type") == "queue_add"]
    rems = [e for e in evs if e.get("type") == "queue_remove"]
    disp = {}
    for e in rems:
        disp[e.get("disposition") or "?"] = disp.get(e.get("disposition") or "?", 0) + 1
    xeco = [e for e in evs if e.get("type") == "xeco_transition"]
    return adds, rems, disp, xeco


def queue_git_history(repo, qpath, w0, w1):
    rel = os.path.relpath(qpath, repo)
    out = sh(["git", "-C", repo, "log", "--format=%H|%aI|%s", "--", rel], timeout=60)
    rows = []
    for line in out.splitlines():
        parts = line.split("|", 2)
        if len(parts) < 3:
            continue
        h, ts, msg = parts
        try:
            t = datetime.datetime.fromisoformat(ts).astimezone(datetime.timezone.utc)
        except Exception:
            continue
        if not (w0 <= t <= w1):
            continue
        content = sh(["git", "-C", repo, "show", f"{h}:{rel}"], timeout=30)
        st = ac = up = ""
        m = re.search(r"## STATUS(.*?)(?:\n## |\Z)", content, re.S)
        if m:
            sec = m.group(1)
            g = lambda k: (re.search(rf"^- {k}:\s*(.+)$", sec, re.M) or [None, ""])[1]  # noqa: E731
            st, ac, up = (g("state") or "")[:40], (g("active-issue") or "")[:60], (g("updated-utc") or "")[:20]
        nq = len(re.findall(r"^\d+\.\s+\*\*", content, re.M))
        rows.append((fmt(t), h[:8], msg[:70], nq, st, ac, up))
    return sorted(rows)


def outcomes(evs):
    merged = [(e.get("repo", ""), e.get("pr"), e.get("title", "")) for e in evs if e.get("type") == "pr_merged"]
    closed = [(e.get("repo", ""), e.get("issue"), e.get("title", "")) for e in evs if e.get("type") == "issue_closed"]
    recon = [e for e in evs if e.get("type") == "reconcile"]
    return merged, closed, recon


def gpu_share(w0, w1, org="https://dev.azure.com/speediedan", proj="interpretune"):
    env = dict(ENV)
    if not env.get("AZURE_DEVOPS_EXT_PAT"):
        try:
            for line in open(os.path.expanduser("~/.bashrc")):
                m = re.match(r"export AZURE_DEVOPS_EXT_PAT=(\S+)", line.strip())
                if m:
                    env["AZURE_DEVOPS_EXT_PAT"] = m.group(1).strip("'\"")
        except OSError:
            pass
    r = subprocess.run(["az", "pipelines", "build", "list", "--organization", org, "--project", proj,
                        "--status", "completed", "--top", "100",
                        "--query", "[].{start:startTime,finish:finishTime}", "-o", "json"],
                       capture_output=True, text=True, timeout=120, env=env)
    if r.returncode != 0:
        return None
    iv = []
    for b in json.loads(r.stdout or "[]"):
        try:
            s, f = parse_ts(b["start"][:19] + "Z"), parse_ts(b["finish"][:19] + "Z")
        except Exception:
            continue
        a, bb = max(s, w0), min(f, w1)
        if bb > a:
            iv.append([a, bb])
    iv.sort()
    merged = []
    for a, b in iv:
        if merged and a <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    busy = sum((b - a).total_seconds() for a, b in merged)
    w = (w1 - w0).total_seconds()
    return len(iv), busy, (100 * busy / w if w else 0)


def signatures(evs, stale_waits, quality, recov):
    """Failure-mode signatures (hag_failure_modes.md § numbers) detected from events."""
    def count(pred):
        return sum(1 for e in evs if pred(e))
    sig = [
        ("§1 depth-zero starvation (PIPELINE-WATCH STARVED fires)",
         count(lambda e: e.get("type") == "coord_comms" and "depth-zero" in e.get("note", ""))),
        ("§2 stale waits (segment outlived its rescan by 1.5x)", len(stale_waits)),
        ("§5/§10 stall-suspect fires (heartbeat daemon)", count(lambda e: e.get("type") == "watch_event" and e.get("kind") == "stall-suspect")),
        ("§7 server respawn (LANES-WATCH SERVER-RESPAWNED)", count(lambda e: e.get("type") == "watch_event" and "respawn" in e.get("kind", ""))),
        ("vocabulary drift: aliased offers", quality["aliased"]),
        ("vocabulary drift: invalid offers", quality["invalid"]),
        ("unrecovered stall-suspects in window", sum(1 for r in recov if r[2].startswith("unrecovered"))),
    ]
    return sig


# ---- rendering -------------------------------------------------------------

def table(headers, rows):
    out = ["| " + " | ".join(headers) + " |", "| " + " | ".join("---" for _ in headers) + " |"]
    for r in rows:
        out.append("| " + " | ".join(str(x).replace("|", "\\|") for x in r) + " |")
    return "\n".join(out) if rows else "_none in window_"


def md_to_html(md: str, title: str) -> str:
    out = ["<!doctype html><html><head><meta charset='utf-8'>",
           f"<title>{_html.escape(title)}</title>",
           "<style>body{font-family:sans-serif;margin:2em;max-width:1200px}table{border-collapse:collapse;margin:.5em 0}",
           "td,th{border:1px solid #999;padding:3px 8px;font-size:13px;vertical-align:top}th{background:#eee}",
           "code{background:#f4f4f4;padding:0 3px}pre{background:#f4f4f4;padding:8px}</style></head><body>"]
    in_table = in_list = in_pre = False
    for line in md.splitlines():
        if line.startswith("```"):
            in_pre = not in_pre
            out.append("<pre>" if in_pre else "</pre>")
            continue
        if in_pre:
            out.append(_html.escape(line))
            continue
        if line.startswith("|"):
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if all(re.fullmatch(r"-+", c) for c in cells):
                continue
            if not in_table:
                out.append("<table>")
                in_table = True
                out.append("<tr>" + "".join(f"<th>{_html.escape(c)}</th>" for c in cells) + "</tr>")
            else:
                out.append("<tr>" + "".join(f"<td>{_html.escape(c)}</td>" for c in cells) + "</tr>")
            continue
        if in_table:
            out.append("</table>")
            in_table = False
        if line.startswith("- "):
            if not in_list:
                out.append("<ul>")
                in_list = True
            out.append(f"<li>{_html.escape(line[2:])}</li>")
            continue
        if in_list:
            out.append("</ul>")
            in_list = False
        m = re.match(r"^(#+)\s+(.*)", line)
        if m:
            n = min(len(m.group(1)), 4)
            out.append(f"<h{n}>{_html.escape(m.group(2))}</h{n}>")
        elif line.strip():
            out.append(f"<p>{_html.escape(line)}</p>")
    if in_table:
        out.append("</table>")
    if in_list:
        out.append("</ul>")
    out.append("</body></html>")
    return "\n".join(out)


def build(spec, spec_path, w0, w1, evs, gen_label, opts):
    L = []
    tot = (w1 - w0).total_seconds()
    L.append(f"# HAG {spec.hag} incarnation report ({gen_label})")
    L.append("")
    L.append(f"- window: {fmt(w0)} .. {fmt(w1)} UTC ({local(w0)} .. {local(w1)}), duration {dur(tot)}")
    L.append(f"- events: {len(evs)} · generated {fmt(datetime.datetime.now(datetime.timezone.utc))} by hag_report.py · spec {spec_path}")
    pins = {}
    for e in evs:
        r = e.get("run") or {}
        k = (r.get("instructions_sha", "?")[:10], r.get("runbook_sha", "?")[:10], r.get("scripts_sha", "?")[:26], r.get("schema_v", "?"))
        pins[k] = pins.get(k, 0) + 1
    L.append("")
    L.append("## Protocol pins seen (instructions / runbook / scripts / schema → events)")
    L.append(table(["instructions", "runbook", "scripts", "schema", "events"],
                   [(*k, n) for k, n in sorted(pins.items(), key=lambda x: -x[1])]))
    L.append("")
    L.append("## Roles")
    L.append(table(["slug", "ecosystem", "coord", "model", "effort", "session", "queue"],
                   [(r.slug, r.ecosystem, "yes" if r.coordinator else "", r.model or "(default)", r.effort or "(default)",
                     r.session or "(none)", os.path.basename(r.queue) if r.queue else "") for r in spec.roles]))
    merged, closed, recon = outcomes(evs)
    L.append("")
    L.append("## Outcomes (GitHub ground truth via reconcile events)")
    L.append(f"- PRs merged: {len(merged)} · issues closed: {len(closed)} · reconcile runs in window: {len(recon)}"
             + (" (**no reconcile ran: outcome counts are lane-reported only**)" if not recon else ""))
    if merged:
        L.append(table(["repo", "PR", "title"], merged))
    if closed:
        L.append(table(["repo", "issue", "title"], closed))
    rows, tot = state_rows(evs, w0, w1, spec)
    L.append("")
    L.append("## Time in state per session (seconds; share of window; `unobserved` = before first event)")
    if rows:
        hdr = ["slug", "eco", "session", "active", "idle", "waiting"] + [f"w:{s}" for s in SUBS] + ["w:other", "paused/offline", "unobserved", "transitions"]
        L.append(table(hdr, [(slug, eco, s[:18], *[f"{agg[k]:.0f} ({100 * agg[k] / tot:.0f}%)" for k in
                                                  ["active", "idle", "waiting"] + [f"w:{x}" for x in SUBS] + ["w:other", "paused/offline"]],
                              f"{unobs:.0f}", n) for slug, eco, s, agg, unobs, n in rows]))
    else:
        L.append("_no session_transition events: time-in-state unobserved for every role_")
    missing = [r.slug for r in spec.roles if r.session and r.session not in {x[2] for x in rows}]
    if missing:
        L.append(f"- roles with NO transitions in window (unobserved, not idle): {', '.join(missing)}")
    q, per, bad = transition_quality(evs)
    L.append("")
    L.append("## Transition quality")
    L.append(f"- valid: {q['valid']} · aliased: {q['aliased']} · invalid: {q['invalid']}")
    if bad:
        L.append(table(["ts", "session", "to_state", "offered sub", "class"], bad[:20]))
    segs, stale, by_sub = wait_discipline(evs, w1)
    L.append("")
    L.append("## Wait discipline")
    if by_sub:
        L.append(table(["substate", "segments", "mean", "max"],
                       [(k, len(v), dur(sum(v) / len(v)), dur(max(v))) for k, v in sorted(by_sub.items())]))
    else:
        L.append("_no waiting segments emitted_")
    L.append(f"- stale waits (segment outlived its declared rescan by 1.5x): {len(stale)}"
             + ("" if any(e.get("rescan_min") for e in evs) else " (no rescan_min on any event: pre-transition-primitive incarnation, unobserved)"))
    if stale:
        L.append(table(["ts", "session", "sub", "lasted", "rescan", "holder"], stale[:15]))
    kinds, fires, recov = watch_summary(evs)
    L.append("")
    L.append("## Watchers")
    L.append(table(["watch_event kind", "count"], sorted(kinds.items())) if kinds else "_no watch_event records_")
    L.append(table(["watch-fire (peer: note)", "count"], sorted(fires.items())) if fires else "_no watch-fire coord_comms_")
    if recov:
        L.append("Stall-suspect → next transition of a named role:")
        L.append(table(["fired", "suspects", "recovery"], recov))
    dirs, gauges = coord_summary(evs)
    L.append("")
    L.append("## Coordinator comms (direction/peer counts)")
    L.append(table(["direction/peer", "count"], sorted(dirs.items())) if dirs else "_no coord_comms events_")
    if gauges:
        L.append(table(["ts", "gpu depth", "unapproved", "local gpu jobs", "board open"], gauges[-12:]))
    adds, rems, disp, xeco = queue_summary(evs)
    L.append("")
    L.append("## Queues (telemetry)")
    L.append(f"- queue_add: {len(adds)} · queue_remove: {len(rems)} · dispositions: {disp or '{}'} · xeco transitions: {len(xeco)}")
    for r in spec.roles:
        if not r.queue or not os.path.exists(r.queue):
            continue
        hist = queue_git_history(opts.repo, r.queue, w0, w1)
        L.append("")
        L.append(f"### Queue file history: {os.path.basename(r.queue)} ({len(hist)} commits in window)")
        L.append(table(["ts", "commit", "subject", "QUEUE items", "state", "active-issue", "updated-utc"], hist[-25:]))
    if opts.gpu:
        g = gpu_share(w0, w1)
        L.append("")
        L.append("## GPU pool (gated pipeline, completed builds)")
        L.append("_az query failed_" if g is None else f"- builds overlapping: {g[0]} · union-busy {dur(g[1])} = {g[2]:.1f}% of window")
    sig = signatures(evs, stale, q, recov)
    L.append("")
    L.append("## Failure-mode signatures (hag_failure_modes.md)")
    L.append(table(["signature", "count"], sig))
    L.append("")
    L.append("## VERDICT (reviewer fills; a report without one is not a review)")
    hints = []
    if q["aliased"]:
        hints.append(f"aliased offers {q['aliased']}: promote or retire the alias in hag_telemetry.ALIASES")
    if q["invalid"]:
        hints.append(f"invalid offers {q['invalid']}: brief vocabulary or add an alias")
    if stale:
        hints.append(f"stale waits {len(stale)}: re-scan discipline not held; consider shorter rescan or watch re-arm")
    if not recon:
        hints.append("no reconcile ran: outcomes unverified; check the reconcile timer tick")
    if not rows:
        hints.append("no transitions recorded: the transition primitive was not used; brief it or the incarnation is unmeasurable")
    L.append("- auto-hints: " + ("; ".join(hints) if hints else "none"))
    L.append("- PROPOSAL / NO-CHANGE: _fill in_")
    L.append("- next incarnation measured against: _fill in_")
    return "\n".join(L) + "\n"


def cmd_report(a: argparse.Namespace) -> int:
    spec = HagSpec.load(a.spec)
    now = datetime.datetime.now(datetime.timezone.utc)
    gen_label = a.gen or spec.display_gen or "window"
    if a.since or a.until:
        w0 = parse_ts(a.since) if a.since else now - datetime.timedelta(hours=168)
        w1 = parse_ts(a.until) if a.until else now
    elif a.hours:
        w0, w1 = now - datetime.timedelta(hours=a.hours), now
        gen_label = f"rolling-{a.hours:g}h"
    else:
        want = a.gen or spec.display_gen
        g = next((g for g in reversed(spec.generations)
                  if g.get("gen") == want or str(g.get("incarnation")) == want), None)
        if not g or not g.get("started_utc"):
            print(f"no generation record for {want} in {a.spec}; give --hours or --since/--until")
            return 1
        w0 = parse_ts(g["started_utc"])
        w1 = parse_ts(g["ended_utc"]) if g.get("ended_utc") else now
    evs = load(resolve_dir(a.dir, spec.hag), spec.hag, w0, w1)
    md = build(spec, a.spec, w0, w1, evs, gen_label, a)
    stem = f"incarnation-{gen_label}-{w0.strftime('%Y%m%dT%H%M')}-{w1.strftime('%Y%m%dT%H%M')}"
    out_md = a.out_md or os.path.join(HAG_ROOT, spec.hag, "reports", stem + ".md")
    out_html = a.out_html or os.path.join(HAG_DATA_ROOT, spec.hag, "reports", stem + ".html")
    if a.dry_run:
        print(md)
        print(f"[dry-run] would write {out_md} and {out_html}")
        return 0
    for p, body in ((out_md, md), (out_html, md_to_html(md, f"HAG {spec.hag} {gen_label}"))):
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as fh:
            fh.write(body)
    print(f"wrote {out_md}\nwrote {out_html} ({len(evs)} events)")
    return 0


def add_parser(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("report", help="per-incarnation report (md + html), spec-driven")
    p.add_argument("--spec", required=True)
    p.add_argument("--gen", default="", help="generation to window on: display (rN) or incarnation int (default: current)")
    p.add_argument("--hours", type=float, default=0)
    p.add_argument("--since", default="")
    p.add_argument("--until", default="")
    p.add_argument("--out-md", default="")
    p.add_argument("--out-html", default="")
    p.add_argument("--dir", default=None, help="telemetry dir override")
    p.add_argument("--repo", default="/home/speediedan/repos/distributed-insight")
    p.add_argument("--gpu", action="store_true", help="add gated-pool busy share via az")
    p.add_argument("--dry-run", action="store_true", help="print the markdown, write nothing")
    p.set_defaults(func=cmd_report)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    sp = ap.add_subparsers(dest="cmd", required=True)
    add_parser(sp)
    a = ap.parse_args()
    sys.exit(a.func(a))
