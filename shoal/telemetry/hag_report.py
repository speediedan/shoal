#!/usr/bin/env python3
"""Per-incarnation HAG report (Markdown + HTML, stdlib only). Spec-driven.

  hag.py report --spec hag.json [--gen rN | --hours N | --since ISO --until ISO]
                [--out-md F] [--out-html F] [--gpu] [--coord-transcript]

Window: --gen reads the generation record in the spec (started_utc ..
ended_utc-or-now); --hours is a rolling window; --since/--until explicit.
Default output: <HAG_DATA_ROOT>/<hag>/reports/incarnation-<gen>-<YYYYMMDDTHHMM>-<...>.md
(the per-hag local home; editable review artifact carrying the VERDICT).
HTML is a pure function of the .md: pass --out-html to render one, otherwise
skipped (regenerate anytime; nothing reads it programmatically).
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

Ledger mode (2026-09-20): the HAG ledger is folded in (ledger.as_telemetry)
and three sections are added — Ledger economics (offers by source, claims
and self-fill share, releases by kind, done verified against reconcile,
offer→claim and claim→done latencies), Wake efficiency (ledger-wake
events → first ledger action by that role; wakes with no action are the
token-waste figure), and Messages (by kind and target; broadcasts; ack
latency). Failure signatures gain §13 (wasted wakes), §14 (contested
claims), §15 (permission violations) and §16 (broadcast storms).
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
from roles import HAG_DATA_ROOT, HAG_ROOT, HagSpec, home_dir  # noqa: E402

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

def sub_of(e):
    """Waiting substate of a transition; '' for every non-waiting state (ledger
    and local emitters disagree on whether a non-waiting event carries a sub)."""
    return (e.get("to_sub", "") or "") if e.get("to_state") == "waiting" else ""


def role_resolver(spec):
    """Map an event to a spec role slug, or None. Order: the event's own `slug`
    field, a session equal to a slug (pre-spec NO-SPEC transitions), then a
    session prefix match (8-char short ids vs full UUIDs, either direction)
    against current roles first and then generation records newest first."""
    slugs = {r.slug for r in spec.roles}
    pairs = [(r.session, r.slug) for r in spec.roles if r.session]
    for g in reversed(spec.generations or []):
        for slug, sid in (g.get("sessions") or {}).items():
            if sid and slug in slugs:
                pairs.append((sid, slug))
    cache = {}

    def resolve(e):
        sl = e.get("slug") or ""
        if sl in slugs:
            return sl
        sess = e.get("session") or ""
        if sess in cache:
            return cache[sess]
        out = sess if sess in slugs else None
        if out is None and len(sess) >= 8:
            for sid, slug in pairs:
                if len(sid) >= 8 and (sid.startswith(sess) or sess.startswith(sid)):
                    out = slug
                    break
        cache[sess] = out
        return out
    return resolve


def seed_states(pre, resolve):
    """Last pre-window transition per role: {slug: (state, sub, ts_utc)}."""
    seed = {}
    for e in sorted(pre, key=lambda x: x["ts_utc"]):
        if e.get("type") != "session_transition":
            continue
        slug = resolve(e)
        if slug:
            seed[slug] = (e.get("to_state"), sub_of(e), e["ts_utc"])
    return seed


STATE_KEYS = (["active", "idle", "waiting"] + [f"w:{x}" for x in SUBS]
              + ["w:other", "paused/offline", "other-state"])


def state_rows(evs, w0, w1, spec, seed=None, resolve=None):
    """One row per spec role. A role's state at w0 is seeded from its last
    pre-window transition, so `unobserved` means no transition ever before
    that point. Returns (rows, window_secs, unresolved_count); each row is
    (slug, eco, sessions, agg, unobs, n, changes, reemits, seeded)."""
    tot = (w1 - w0).total_seconds()
    resolve = resolve or role_resolver(spec)
    seed = seed or {}
    by = {r.slug: [] for r in spec.roles}
    unresolved = 0
    for e in evs:
        if e.get("type") != "session_transition":
            continue
        slug = resolve(e)
        if slug is None:
            unresolved += 1
            continue
        by[slug].append((parse_ts(e["ts_utc"]), e))
    rows = []
    for r in spec.roles:
        lst = sorted(by[r.slug], key=lambda x: x[0])
        agg = {k: 0.0 for k in STATE_KEYS}

        def add(st, sub, secs):
            if st in ("active", "idle"):
                agg[st] += secs
            elif st == "waiting":
                agg["waiting"] += secs
                agg[f"w:{sub}" if f"w:{sub}" in agg else "w:other"] += secs
            elif st in ("paused", "offline"):
                agg["paused/offline"] += secs
            else:
                agg["other-state"] += secs

        sd = seed.get(r.slug)
        if sd:
            cur_t, cur, cur_sub, prev, unobs = w0, sd[0], sd[1], (sd[0], sd[1]), 0.0
        else:
            cur_t, cur, cur_sub, prev = None, None, "", None
            unobs = (lst[0][0] - w0).total_seconds() if lst else tot
        changes = reemits = 0
        for t, e in lst:
            key = (e.get("to_state"), sub_of(e))
            if key == prev:
                reemits += 1
            else:
                changes += 1
            prev = key
            if cur is not None:
                add(cur, cur_sub, (t - cur_t).total_seconds())
            cur_t, cur, cur_sub = t, key[0], key[1]
        if cur is not None:
            add(cur, cur_sub, (w1 - cur_t).total_seconds())
        sessions = sorted({(e.get("session") or "")[:8] for _t, e in lst} - {""})
        rows.append((r.slug, r.ecosystem, ",".join(sessions) or "-", agg, unobs, len(lst),
                     changes, reemits, bool(sd)))
    return rows, tot, unresolved


def transition_quality(evs, resolve=None):
    q = {"valid": 0, "aliased": 0, "invalid": 0}
    bad, per = [], {}
    for e in evs:
        if e.get("type") != "session_transition":
            continue
        c = classify_transition(e)
        q[c] += 1
        who = (resolve(e) if resolve else None) or f"unresolved:{e.get('session', '?')}"
        per.setdefault(who, {"valid": 0, "aliased": 0, "invalid": 0})[c] += 1
        if c != "valid":
            bad.append((e["ts_utc"], who, e.get("to_state"), e.get("to_sub_raw") or e.get("to_sub"), c))
    return q, per, bad


def wait_discipline(evs, w1, resolve=None):
    """Waiting segments emitted with rescan_min: stale = segment outlived its rescan."""
    segs, stale = [], []
    by = {}
    for e in evs:
        if e.get("type") == "session_transition":
            who = (resolve(e) if resolve else None) or f"unresolved:{e.get('session', '?')}"
            by.setdefault(who, []).append(e)
    for s, l in by.items():
        l.sort(key=lambda x: x["ts_utc"])
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


# ---- ledger analyses --------------------------------------------------------

def _repo_num(repo, num):
    """('interpretune', '594') from ('speediedan/interpretune', 594)."""
    return ((repo or "").rsplit("/", 1)[-1].lower(), str(num or ""))


def ledger_economics(evs, w1, truth=None):
    offers = [e for e in evs if e.get("type") == "ledger_offer"]
    claims = [e for e in evs if e.get("type") == "ledger_claim"]
    rels = [e for e in evs if e.get("type") == "ledger_release"]
    dones = [e for e in evs if e.get("type") == "ledger_done"]
    wds = [e for e in evs if e.get("type") == "ledger_withdraw"]
    by_src, by_writer = {}, {}
    for o in offers:
        by_src[o.get("src") or "?"] = by_src.get(o.get("src") or "?", 0) + 1
        by_writer[o.get("slug", "?")] = by_writer.get(o.get("slug", "?"), 0) + 1
    claim_src = {}
    for c in claims:
        claim_src[c.get("src") or "?"] = claim_src.get(c.get("src") or "?", 0) + 1
    rel_kind = {}
    for r in rels:
        rel_kind[r.get("kind") or "?"] = rel_kind.get(r.get("kind") or "?", 0) + 1
    # verification: done item (repo#n) appears as a reconcile pr_merged or
    # issue_closed for the SAME repo and number. `truth` is the ground truth
    # from any time (a reconcile after the window still verifies a done in it);
    # it defaults to the in-window events.
    truth = evs if truth is None else truth
    verified_keys = {_repo_num(e.get("repo"), e.get("pr") or e.get("issue"))
                     for e in truth if e.get("type") in ("pr_merged", "issue_closed")}
    ver = 0
    for d in dones:
        m = re.search(r"([\w.-]+)#(\d+)", d.get("item", ""))
        if m and _repo_num(m.group(1), m.group(2)) in verified_keys:
            ver += 1
    # latencies per role
    first_offer = {}
    for o in offers:
        k = (o.get("item", "").lower(), o.get("to"))
        first_offer.setdefault(k, parse_ts(o["ts_utc"]))
    lat_oc, lat_cd = {}, {}
    claim_at = {}
    for c in claims:
        key = c.get("item", "").lower()
        t = parse_ts(c["ts_utc"])
        claim_at[(key, c.get("slug"))] = t
        t0 = first_offer.get((key, c.get("slug"))) or first_offer.get((key, "*"))
        if t0:
            lat_oc.setdefault(c.get("slug", "?"), []).append((t - t0).total_seconds())
    for d in dones:
        key = d.get("item", "").lower()
        t0 = claim_at.get((key, d.get("slug")))
        if t0:
            lat_cd.setdefault(d.get("slug", "?"), []).append((parse_ts(d["ts_utc"]) - t0).total_seconds())
    per_role = {}
    for e in offers:
        per_role.setdefault(e.get("to", "?"), {"offered": 0, "claimed": 0, "self": 0, "done": 0, "declined": 0})["offered"] += 1
    for c in claims:
        row = per_role.setdefault(c.get("slug", "?"), {"offered": 0, "claimed": 0, "self": 0, "done": 0, "declined": 0})
        row["claimed"] += 1
        if (c.get("src") or "") in ("board", "self"):
            row["self"] += 1
    for d in dones:
        per_role.setdefault(d.get("slug", "?"), {"offered": 0, "claimed": 0, "self": 0, "done": 0, "declined": 0})["done"] += 1
    for r in rels:
        if r.get("kind") == "decline":
            per_role.setdefault(r.get("slug", "?"), {"offered": 0, "claimed": 0, "self": 0, "done": 0, "declined": 0})["declined"] += 1
    return {"offers": offers, "claims": claims, "rels": rels, "dones": dones, "wds": wds,
            "by_src": by_src, "by_writer": by_writer, "claim_src": claim_src, "rel_kind": rel_kind,
            "verified": ver, "lat_oc": lat_oc, "lat_cd": lat_cd, "per_role": per_role}


def wake_efficiency(evs, w1, act_window_min=30):
    """ledger-wake watch_events → next ledger action (claim/done/release/msg/
    state) by the same role. No action within act_window = wasted wake."""
    wakes = [e for e in evs if e.get("type") == "watch_event" and e.get("kind") == "ledger-wake"]
    acts = [e for e in evs if e.get("type") in ("ledger_claim", "ledger_done", "ledger_release",
                                                "ledger_msg", "session_transition")]
    rows, per = [], {}
    for w in wakes:
        t0 = parse_ts(w["ts_utc"])
        slug = w.get("slug", "?")
        nxt = next((a for a in acts if a.get("slug") == slug and parse_ts(a["ts_utc"]) > t0), None)
        lat = (parse_ts(nxt["ts_utc"]) - t0).total_seconds() if nxt else None
        wasted = lat is None or lat > act_window_min * 60
        p = per.setdefault(slug, {"wakes": 0, "wasted": 0, "lat": []})
        p["wakes"] += 1
        p["wasted"] += 1 if wasted else 0
        if lat is not None and not wasted:
            p["lat"].append(lat)
        rows.append((w["ts_utc"], slug, w.get("note", "")[:40], w.get("n", ""), dur(lat) if lat is not None else "no action in window",
                     "WASTED" if wasted else ""))
    return wakes, rows, per


def message_stats(evs):
    msgs = [e for e in evs if e.get("type") == "ledger_msg"]
    by_kind, by_to = {}, {}
    for m in msgs:
        by_kind[m.get("kind") or "info"] = by_kind.get(m.get("kind") or "info", 0) + 1
        by_to[m.get("to") or "?"] = by_to.get(m.get("to") or "?", 0) + 1
    acks = []
    ids = {m.get("ledger_id"): m for m in msgs if m.get("ledger_id")}
    for m in msgs:
        if m.get("re") in ids:
            acks.append((parse_ts(m["ts_utc"]) - parse_ts(ids[m["re"]]["ts_utc"])).total_seconds())
    unacked = sum(1 for m in msgs if m.get("ack") and not any(x.get("re") == m.get("ledger_id") for x in msgs))
    return msgs, by_kind, by_to, acks, unacked


def stall_suspect_minutes(evs):
    """Distinct minutes with a stall-suspect fire. The heartbeat daemon records
    a fire as one watch_event (when its emit works) plus one ledger msg per
    target, so both sources are unioned by minute rather than summed."""
    return {e["ts_utc"][:16] for e in evs
            if (e.get("type") == "watch_event" and e.get("kind") == "stall-suspect")
            or (e.get("type") == "ledger_msg" and e.get("kind") == "stall-suspect")}


def signatures(evs, stale_waits, quality, recov):
    """Failure-mode signatures (hag_failure_modes.md § numbers) detected from events."""
    def count(pred):
        return sum(1 for e in evs if pred(e))
    sig = [
        ("§1 depth-zero starvation (PIPELINE-WATCH STARVED fires)",
         count(lambda e: e.get("type") == "coord_comms" and "depth-zero" in e.get("note", ""))),
        ("§2 stale waits (segment outlived its rescan by 1.5x)", len(stale_waits)),
        ("§5/§10 stall-suspect fires (heartbeat daemon; watch_event + ledger msg, deduped by minute)",
         len(stall_suspect_minutes(evs))),
        ("§7 server respawn (LANES-WATCH SERVER-RESPAWNED)", count(lambda e: e.get("type") == "watch_event" and "respawn" in e.get("kind", ""))),
        ("vocabulary drift: aliased offers", quality["aliased"]),
        ("vocabulary drift: invalid offers", quality["invalid"]),
        ("unrecovered stall-suspects in window", sum(1 for r in recov if r[2].startswith("unrecovered"))),
    ]
    return sig


def ledger_signatures(evs, wake_per, msgs_by_to, view_lint):
    wasted = sum(p["wasted"] for p in wake_per.values())
    wakes = sum(p["wakes"] for p in wake_per.values())
    contested = sum(1 for c, _ in view_lint if c == "E2")
    viol = sum(1 for c, _ in view_lint if c == "E1")
    return [
        ("§13 wasted wakes (ledger-wake with no action in 30 min)", f"{wasted} of {wakes}"),
        ("§14 contested claims (two live claims on one item)", contested),
        ("§15 permission violations (ignored events)", viol),
        ("§16 broadcast msgs (to=*) in window", msgs_by_to.get("*", 0)),
    ]


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


def recovery_sections(evs, w0, w1):
    """Recovery tiers, job units, digest refreshes (architecture doc §4-§6) and host
    PSI / harness reaps from the H4 instruments (failure modes §21)."""
    L = ["", "## Health quorum actions, wakes per item, job units, digest"]
    tiers, jobs, digests = {}, {"start": 0, "success": 0, "failed": 0, "gone": 0}, 0
    wakes, msg_wakes = {}, 0
    for e in evs:
        if e.get("type") != "watch_event":
            continue
        k = e.get("kind", "")
        if k == "health":
            what = (e.get("note", "") or "").split(" -> ")[0]
            key = f"{e.get('tier', 'T2')} {what.split(' step')[0].split(' ', 1)[-1] if ' ' in what else what}"
            tiers[key] = tiers.get(key, 0) + 1
        elif k == "stall-suspect":
            t = e.get("tier") or "T2"
            tiers[f"{t} heartbeat page (legacy)"] = tiers.get(f"{t} heartbeat page (legacy)", 0) + 1
        elif k == "ledger-wake":
            s_ = e.get("slug", "?")
            wakes[s_] = wakes.get(s_, 0) + 1
            msg_wakes += "MSG" in (e.get("note", "") or "").split(",")
        elif k == "job-start":
            jobs["start"] += 1
        elif k == "job-verdict":
            n = e.get("note", "")
            jobs["success" if "result=success" in n else "gone" if "UNIT-GONE" in n else "failed"] += 1
        elif k == "digest-refresh":
            digests += 1
    done_by, queue_v = {}, {}
    for e in evs:
        if e.get("type") == "ledger_done":
            done_by[e.get("slug", "?")] = done_by.get(e.get("slug", "?"), 0) + 1
        if e.get("type") == "ledger_queue":
            queue_v[e.get("slug", "?")] = queue_v.get(e.get("slug", "?"), 0) + 1
    L.append(f"- job units: {jobs['start']} started, {jobs['success']} success, {jobs['failed']} failed, "
             f"{jobs['gone']} killed outside their command (bar: 0) · digest refreshes: {digests} · "
             f"msg-induced wakes: {msg_wakes} (bar under workwake: 0)")
    rows = [[s_, wakes.get(s_, 0), done_by.get(s_, 0), queue_v.get(s_, 0),
             f"{wakes.get(s_, 0) / done_by[s_]:.1f}" if done_by.get(s_) else "-"]
            for s_ in sorted(set(wakes) | set(done_by) | set(queue_v))]
    if rows:
        L.append(table(["role", "wakes", "done", "queue versions", "wakes per done item"], rows))
    L.append(table(["tier", "count"], sorted(tiers.items())) if tiers else "_no health-quorum actions_")
    instr = os.path.join(os.environ.get("HAG_DATA_ROOT", "/mnt/cache_extended/speediedan/hags"), "_instruments")
    psi_n, kills = 0, []
    try:
        with open(os.path.join(instr, "psi_events.jsonl")) as fh:
            for line in fh:
                if '"kind":"event"' in line:
                    ts = parse_ts(json.loads(line)["ts"][:19] + "Z")
                    psi_n += w0 <= ts <= w1
    except (OSError, ValueError, KeyError):
        psi_n = -1
    try:
        with open(os.path.join(instr, "kills.jsonl")) as fh:
            for line in fh:
                k = json.loads(line)
                if w0 <= parse_ts(k["ts"][:19] + "Z") <= w1:
                    kills.append([k["ts"][:19], k.get("session", ""), k.get("version", ""), k.get("psi", "")[:70],
                                  k.get("summary", "")[:60]])
    except (OSError, ValueError, KeyError):
        pass
    L.append("")
    L.append("## Host memory pressure and harness reaps (psi_watch + kill-audit)")
    L.append(f"- host PSI trigger fires in window: {'unavailable' if psi_n < 0 else psi_n}"
             f" · genuine harness reaps: {len(kills)} (run `hag.py kill-audit` first to record new ones)")
    if kills:
        L.append(table(["ts", "session", "harness", "psi context", "command"], kills[-15:]))
    return L


def build(spec, spec_path, w0, w1, evs, gen_label, opts):
    L = []
    tot = (w1 - w0).total_seconds()
    L.append(f"# HAG {spec.hag} incarnation report ({gen_label})")
    L.append("")
    L.append(f"- window: {fmt(w0)} .. {fmt(w1)} UTC ({local(w0)} .. {local(w1)}), duration {dur(tot)}")
    L.append(f"- events: {len(evs)} · generated {fmt(datetime.datetime.now(datetime.timezone.utc))} by hag_report.py · spec {spec_path}")
    pins, ledger_folded = {}, 0
    for e in evs:
        r = e.get("run") or {}
        if e.get("channel") == "ledger" and not r:
            ledger_folded += 1  # ledger-derived (as_telemetry): carries no run pin
            continue
        k = (r.get("instructions_sha", "?")[:10], r.get("runbook_sha", "?")[:10], r.get("scripts_sha", "?")[:26], r.get("schema_v", "?"))
        pins[k] = pins.get(k, 0) + 1
    L.append("")
    L.append("## Protocol pins seen (instructions / runbook / scripts / schema → events)")
    L.append(table(["instructions", "runbook", "scripts", "schema", "events"],
                   [(*k, n) for k, n in sorted(pins.items(), key=lambda x: -x[1])]))
    L.append(f"- ledger-folded events (no run pin, excluded above): {ledger_folded}")
    L.append("")
    L.append("## Roles")
    L.append(table(["slug", "ecosystem", "coord", "fill", "model", "effort", "session", "server", "queue"],
                   [(r.slug, r.ecosystem, "yes" if r.coordinator else "",
                     getattr(r, "fill", "assigned") + ("+fills" if getattr(r, "fills_others", False) else ""),
                     r.model or "(default)", r.effort or "(default)",
                     r.session or "(none)", getattr(r, "server", "") or "local",
                     os.path.basename(r.queue) if r.queue else ("ledger" if spec.ledger_mode else "")) for r in spec.roles]))
    if spec.ledger_mode:
        srcs = ", ".join(f"{x.get('kind', '?')}:{x.get('owner', '?')}/{x.get('number', '?')}" for x in spec.sources) or "none"
        L.append(f"- mode: ledger ({spec.ledger.get('repo', '')}), {'flattened' if spec.flattened else 'coordinated'}; sources: {srcs}")
    merged, closed, recon = outcomes(evs)
    L.append("")
    L.append("## Outcomes (GitHub ground truth via reconcile events)")
    L.append(f"- PRs merged: {len(merged)} · issues closed: {len(closed)} · reconcile runs in window: {len(recon)}"
             + (" (**no reconcile ran: outcome counts are lane-reported only**)" if not recon else ""))
    if merged:
        L.append(table(["repo", "PR", "title"], merged))
    if closed:
        L.append(table(["repo", "issue", "title"], closed))
    resolve = role_resolver(spec)
    seed = getattr(opts, "seed", None) or {}
    rows, tot, unresolved = state_rows(evs, w0, w1, spec, seed=seed, resolve=resolve)
    L.append("")
    L.append("## Time in state per role (seconds; share of window; `unobserved` = no transition ever "
             "before that point; state at window start is seeded from the last pre-window transition)")
    pct = lambda x: f"{x:.0f} ({100 * x / tot:.0f}%)" if tot else f"{x:.0f}"  # noqa: E731
    hdr = (["slug", "eco", "sessions", "active", "idle", "waiting"]
           + [f"of which w:{x}" for x in SUBS] + ["of which w:other", "paused/offline", "other state",
                                                  "unobserved", "transitions", "changes", "re-emits", "seeded"])
    L.append(table(hdr, [(slug, eco, sess, *[pct(agg[k]) for k in STATE_KEYS[:-2]],
                          pct(agg["paused/offline"]), pct(agg["other-state"]), pct(unobs), n, ch, rem,
                          "yes" if sd else "no")
                         for slug, eco, sess, agg, unobs, n, ch, rem, sd in rows]))
    L.append(f"- unresolved transitions (no spec role by slug or session prefix; not in any row): {unresolved}")
    missing = [r[0] for r in rows if r[5] == 0]
    if missing:
        L.append(f"- roles with NO transitions in window: {', '.join(missing)}"
                 " (time carried from the seeded pre-window state where `seeded` is yes, else unobserved)")
    q, per, bad = transition_quality(evs, resolve)
    L.append("")
    L.append("## Transition quality")
    L.append(f"- valid: {q['valid']} · aliased: {q['aliased']} · invalid: {q['invalid']}"
             f" · ledger twins dropped (same role/state within 5 s of a local transition): "
             f"{getattr(opts, 'twins_dropped', 0)}")
    if bad:
        L.append(table(["ts", "role", "to_state", "offered sub", "class"], bad[:20]))
    segs, stale, by_sub = wait_discipline(evs, w1, resolve)
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
        L.append(table(["ts", "role", "sub", "lasted", "rescan", "holder"], stale[:15]))
    kinds, fires, recov = watch_summary(evs)
    L.append("")
    L.append("## Watchers")
    L.append(table(["watch_event kind", "count"], sorted(kinds.items())) if kinds else "_no watch_event records_")
    L.append(table(["watch-fire (peer: note)", "count"], sorted(fires.items())) if fires else "_no watch-fire coord_comms_")
    if recov:
        L.append("Stall-suspect → next transition of a named role:")
        L.append(table(["fired", "suspects", "recovery"], recov))
    L.extend(recovery_sections(evs, w0, w1))
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
    if spec.ledger_mode:
        ec = ledger_economics(evs, w1, truth=getattr(opts, "truth", None))
        L.append("")
        L.append("## Ledger economics (offers, claims, completions)")
        L.append(f"- offers: {len(ec['offers'])} by src {ec['by_src'] or '{}'} · withdrawn: {len(ec['wds'])} · "
                 f"claims: {len(ec['claims'])} by src {ec['claim_src'] or '{}'} · releases: {ec['rel_kind'] or '{}'} · "
                 f"done: {len(ec['dones'])} (verified against GitHub via reconcile, repo+number, any reconcile run: {ec['verified']})")
        if ec["per_role"]:
            L.append(table(["role", "offered to", "claimed", "self-filled", "done", "declined",
                            "offer→claim median", "claim→done median"],
                           [(slug, v["offered"], v["claimed"], v["self"], v["done"], v["declined"],
                             dur(sorted(ec["lat_oc"].get(slug, []))[len(ec["lat_oc"].get(slug, [])) // 2]) if ec["lat_oc"].get(slug) else "—",
                             dur(sorted(ec["lat_cd"].get(slug, []))[len(ec["lat_cd"].get(slug, [])) // 2]) if ec["lat_cd"].get(slug) else "—")
                            for slug, v in sorted(ec["per_role"].items())]))
        else:
            L.append("_no ledger offers/claims in window_")
        wakes, wrows, wper = wake_efficiency(evs, w1)
        L.append("")
        L.append("## Wake efficiency (ledger-wake → first action by the woken role)")
        if wper:
            L.append(table(["role", "wakes", "wasted (no action ≤30m)", "wake→action median"],
                           [(slug, p["wakes"], p["wasted"], dur(sorted(p["lat"])[len(p["lat"]) // 2]) if p["lat"] else "—")
                            for slug, p in sorted(wper.items())]))
            L.append(table(["ts", "role", "reason", "n", "first action", ""], wrows[-20:]))
        else:
            L.append("_no ledger-wake events: watchers never fired (or never emitted) — unobserved_")
        msgs, by_kind, by_to, acks, unacked = message_stats(evs)
        L.append("")
        L.append("## Messages (ledger msg)")
        L.append(f"- msgs: {len(msgs)} by kind {by_kind or '{}'} · by target {by_to or '{}'} · "
                 f"ack latency median {dur(sorted(acks)[len(acks) // 2]) if acks else '—'} · unacked ack-requests: {unacked}")
        if getattr(opts, "view_lint", None):
            L.append("")
            L.append("## Ledger lint (live)")
            for code, msg in opts.view_lint[:20]:
                L.append(f"- {code} {msg}")
    if opts.gpu:
        g = gpu_share(w0, w1)
        L.append("")
        L.append("## GPU pool (gated pipeline, completed builds)")
        L.append("_az query failed_" if g is None else f"- builds overlapping: {g[0]} · union-busy {dur(g[1])} = {g[2]:.1f}% of window")
    sig = signatures(evs, stale, q, recov)
    if spec.ledger_mode:
        _w, _r, wper2 = wake_efficiency(evs, w1)
        _m, _k, by_to2, _a, _u = message_stats(evs)
        sig += ledger_signatures(evs, wper2, by_to2, getattr(opts, "view_lint", []) or [])
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
    if not any(r[5] for r in rows):
        hints.append("no transitions recorded: the transition primitive was not used; brief it or the incarnation is unmeasurable")
    if spec.ledger_mode:
        _w, _r, wper3 = wake_efficiency(evs, w1)
        tw = sum(p["wakes"] for p in wper3.values())
        ww = sum(p["wasted"] for p in wper3.values())
        if tw and ww / tw > 0.3:
            hints.append(f"wasted wakes {ww}/{tw} (>30%): tighten actionable rules or raise --interval")
        if not tw:
            hints.append("no ledger-wake events: watchers unarmed or wake telemetry not written")
        for code, msg in (getattr(opts, "view_lint", []) or []):
            if code == "E2":
                hints.append("contested claim(s) live: a loser must release kind=lost-race")
                break
    L.append("- auto-hints: " + ("; ".join(hints) if hints else "none"))
    L.append("- PROPOSAL / NO-CHANGE: _fill in_")
    L.append("- next incarnation measured against: _fill in_")
    return "\n".join(L) + "\n"


def sibling_truth(hag):
    """GitHub ground truth written by OTHER HAGs' reconcile runs. A merge or
    close is a fact about GitHub, not about the HAG whose timer happened to see
    it (the r5 HAG's dones were reconciled by its successor's timer). Only
    events marked reconciled=true count; best-effort, never fatal."""
    out = []
    try:
        names = sorted(os.listdir(HAG_DATA_ROOT))
    except OSError:
        return out
    for h in names:
        d = os.path.join(HAG_DATA_ROOT, h, "telemetry")
        if h == hag or not os.path.isdir(d):
            continue
        try:
            out += [e for e in load(d, h, None, None)
                    if e.get("type") in ("pr_merged", "issue_closed") and e.get("reconciled") == "true"]
        except Exception:  # noqa: BLE001
            continue
    return out


def drop_ledger_twins(local, ledger_evs, resolve, tol_s=5):
    """Drop a ledger-derived `state` event (session_transition via=ledger) when
    the same role has a local telemetry transition with the same to_state and
    waiting sub within tol_s. `hag.py transition` writes both, the ledger one
    landing 1-2 s later, so an exact-timestamp match never deduped them. Each
    local transition absorbs at most one twin. Returns (kept, dropped_ts)."""
    idx = {}
    for e in local:
        if e.get("type") == "session_transition" and e.get("via") != "ledger":
            slug = resolve(e)
            if slug:
                idx.setdefault(slug, []).append([parse_ts(e["ts_utc"]), e.get("to_state"), sub_of(e), False])
    kept, dropped = [], []
    for e in ledger_evs:
        if e.get("type") == "session_transition":
            slug = resolve(e)
            t = parse_ts(e["ts_utc"])
            hit = None
            for cand in idx.get(slug, []) if slug else []:
                if (not cand[3] and cand[1] == e.get("to_state") and cand[2] == sub_of(e)
                        and abs((t - cand[0]).total_seconds()) <= tol_s):
                    if hit is None or abs((t - cand[0]).total_seconds()) < abs((t - hit[0]).total_seconds()):
                        hit = cand
            if hit is not None:
                hit[3] = True
                dropped.append(t)
                continue
        kept.append(e)
    return kept, dropped


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
    # Load everything up to w1: the pre-window transitions seed each role's
    # state at w0, and reconcile ground truth from any time verifies dones.
    everything = load(resolve_dir(a.dir, spec.hag), spec.hag, None, None)
    a.truth = [e for e in everything if e.get("type") in ("pr_merged", "issue_closed")]
    a.truth += sibling_truth(spec.hag)
    upto = [e for e in everything if parse_ts(e["ts_utc"]) <= w1]
    resolve = role_resolver(spec)
    a.view_lint = []
    a.twins_dropped = []
    if spec.ledger_mode:
        try:
            from ledger import Ledger, View, as_telemetry, lint as ledger_lint
            led = Ledger(spec.hag, root=spec.ledger_dir)
            if led.exists():
                if not a.no_pull:
                    led.pull()
                folded, a.twins_dropped = drop_ledger_twins(upto, as_telemetry(led, spec, None, w1), resolve)
                upto.extend(folded)
                upto.sort(key=lambda e: e["ts_utc"])
                a.view_lint = ledger_lint(View(led, spec))
        except Exception as exc:  # noqa: BLE001
            print(f"WARN: ledger not folded in: {exc}")
    evs = [e for e in upto if parse_ts(e["ts_utc"]) >= w0]
    a.seed = seed_states([e for e in upto if parse_ts(e["ts_utc"]) < w0], resolve)
    # twins dropped inside the window only (the pre-window ones affect seeding, not counts)
    a.twins_dropped = sum(1 for t in a.twins_dropped if t >= w0)
    md = build(spec, a.spec, w0, w1, evs, gen_label, a)
    stem = f"incarnation-{gen_label}-{w0.strftime('%Y%m%dT%H%M')}-{w1.strftime('%Y%m%dT%H%M')}"
    out_md = a.out_md or os.path.join(home_dir(spec.hag), "reports", stem + ".md")
    out_html = a.out_html or ""
    if a.dry_run:
        print(md)
        print(f"[dry-run] would write {out_md}" + (f" and {out_html}" if out_html else " (no html: pass --out-html)"))
        return 0
    os.makedirs(os.path.dirname(out_md), exist_ok=True)
    with open(out_md, "w") as fh:
        fh.write(md)
    if out_html:
        os.makedirs(os.path.dirname(out_html), exist_ok=True)
        with open(out_html, "w") as fh:
            fh.write(md_to_html(md, f"HAG {spec.hag} {gen_label}"))
    print(f"wrote {out_md}" + (f"\nwrote {out_html}" if out_html else "") + f" ({len(evs)} events)")
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
    p.add_argument("--no-pull", action="store_true", help="do not pull the ledger first")
    p.add_argument("--dry-run", action="store_true", help="print the markdown, write nothing")
    p.set_defaults(func=cmd_report)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    sp = ap.add_subparsers(dest="cmd", required=True)
    add_parser(sp)
    a = ap.parse_args()
    sys.exit(a.func(a))
