"""CI efficiency review: this week against the week before and a fixed baseline, the largest sources
of waste, and an experiments log whose predictions are checked against what was measured
(architecture doc §9).

The review reads only the rows `shoal efficiency snapshot` writes and the experiments log: no
GitHub, no ledger, no model call. It writes a dated Markdown report and appends one verdict per
open experiment to the log. Optionally (off by default) it then wakes one named standalone seat with
the report's path, so a session can propose the next rule or infrastructure change as an experiment.

An experiment is one JSONL line; a later line with the same `id` replaces it (so a pending change
becomes open by appending it again with its date):

    {"type": "experiment", "id": "<slug>", "date": "YYYY-MM-DD", "scope": "<owner>/<name>" or "shoal:<id>",
     "change": "<what changed>", "ref": "<owner>/<name>#<n>", "metric": "<a metric below>",
     "source": "<glob, for source_minutes_per_head>", "predicted": {"change": -0.25} or {"to": 160, "from": 217},
     "before": ["YYYY-MM-DD", "YYYY-MM-DD"], "after": "YYYY-MM-DD", "status": "open" or "pending", "noise": 0.1}

`date` is the UTC day the change took effect; the before window defaults to the seven days before
it and the after window starts the day after it (`after` names another first day, for a change
made early in its day). The review appends `{"type": "verdict", "id", "day", ...}` lines: `keep`
when the metric moved the predicted way by at least half the prediction (and more than the noise
floor) with no guardrail worse, `revert` when it moved the other way by more than the noise floor,
else `inconclusive`. A verdict is final once the after window holds `SETTLE_DAYS` days (or the
experiment has been open `MAX_OPEN_DAYS`); until then it is provisional and the next review
checks again.

    [efficiency]
    experiments = "<path>/experiments.jsonl"   # default: beside the snapshot rows
    report_dir = "<path>/reports"              # default: <rows dir>/reports
    baseline = ["YYYY-MM-DD", "YYYY-MM-DD"]    # a fixed window every review compares against
    review_seat = "<standalone seat label>"    # wake this seat after a review (default: none)
"""
from __future__ import annotations

import fnmatch
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import efficiency as ef  # noqa: E402

WINDOW_DAYS = 7
NOISE = 0.10          # a relative change smaller than this is not evidence either way
MIN_AFTER_DAYS = 3    # an experiment is not judged on fewer days after it
SETTLE_DAYS = 7       # a keep or revert is final once the after window holds this many days
MAX_AFTER_DAYS = 14   # the after window is the first this many days from its start
MAX_OPEN_DAYS = 28    # an experiment still inconclusive after this many days closes as inconclusive
TOP = 10


def _div(a, b, scale=1.0):
    return None if a is None or not b else round(a / b * scale, 3)


def _source_seconds(a, pattern):
    pat = (pattern or "*").lower()
    return sum(v for k, v in (a.get("by_source") or {}).items() if fnmatch.fnmatchcase(k.lower(), pat))


def _source_minutes_per_head(a, pattern):
    if not a.get("source_days"):
        return None  # rows from before sources were kept
    return _div(_source_seconds(a, pattern), a.get("source_heads"), 1 / 60)  # heads of the days with sources


# name: (better direction, description, value from a repository rollup)
REPO_METRICS = {
    "lines_per_job_minute": ("up", "merged lines per CI job-minute", lambda a: a["ratios"]["lines_per_job_minute"]),
    "lines_per_ci_wait_hour": ("up", "merged lines per CI wait hour", lambda a: a["ratios"]["lines_per_ci_wait_hour"]),
    "job_minutes_per_merged_pr": ("down", "CI job-minutes per merged PR", lambda a: a["ratios"]["job_minutes_per_merged_pr"]),
    "job_minutes_per_head": ("down", "CI job-minutes per PR head", lambda a: a["ratios"]["job_minutes_per_head"]),
    "push_to_complete_per_head_min": ("down", "push to CI complete per PR head, mean minutes",
                                      lambda a: _div(a["ci"].get("push_to_complete_s"), a["ci"].get("pr_heads"), 1 / 60)),
    "gate_wait_per_build_min": ("down", "gate wait per gate build, mean minutes",
                                lambda a: _div(a["ci"].get("gate_wait_s"), a["ci"].get("gate_builds"), 1 / 60)),
    "github_queue_per_job_min": ("down", "GitHub queue per counted job, mean minutes",
                                 lambda a: _div(a["ci"].get("github_queue_s"), a["ci"].get("github_jobs"), 1 / 60)),
    "local_lease_wait_per_job_min": ("down", "local CI lease wait per job, mean minutes",
                                     lambda a: _div(a["ci"].get("local_lease_wait_s"), a["ci"].get("local_jobs"), 1 / 60)),
    "merged_lines_per_day": ("up", "merged lines per day", lambda a: _div(a["merged"]["lines"], a["github_days"])),
    "job_minutes_per_day": ("down", "CI job-minutes per day", lambda a: _div(a["ci"].get("job_minutes"), a["days"])),
    "rerun_share": ("down", "guardrail: share of PR head minutes on reruns", lambda a: a["guard"]["rerun_share"]),
    "failed_gate_share": ("down", "guardrail: failed gate builds per gate build",
                          lambda a: _div(a["guard"].get("failed_gates"), a["ci"].get("gate_builds"))),
    "revert_share": ("down", "guardrail: reverted or revert PRs per merged PR",
                     lambda a: _div(a["guard"].get("reverted_prs"), a["merged"]["prs"])),
    "unleased_cpu_jobs_per_day": ("down", "guardrail: local CI jobs doing CPU work unleased, per day",
                                  lambda a: _div(a["guard"].get("unleased_cpu_jobs"), a["days"])
                                  if a["guard"].get("unleased_cpu_observed") else None),
    "source_minutes_per_head": ("down", "minutes per PR head of the sources matching `source` (a glob)", None),
}
SHOAL_METRICS = {
    "ci_lease_share": ("down", "share of eligible role time waiting on CI or a lease", "ci_lease_share"),
    "lease_share": ("down", "share of eligible role time waiting on a lease", "lease_share"),
    "lines_per_role_ci_wait_hour": ("up", "merged lines per role CI and lease wait hour", "lines_per_role_ci_wait_hour"),
    "AAR": ("up", "Average Active Roles", "AAR"),
    "RWF": ("up", "Role Work Fraction", "RWF"),
    "coverage": ("up", "role-time coverage", "coverage"),
}
# a guardrail worse than its tolerance (absolute) keeps a ratio's improvement from counting
GUARDRAILS = {"rerun_share": 0.05, "failed_gate_share": 0.10, "revert_share": 0.05, "unleased_cpu_jobs_per_day": 1.0}
HEADLINE = ("lines_per_job_minute", "lines_per_ci_wait_hour", "job_minutes_per_merged_pr", "job_minutes_per_head",
            "push_to_complete_per_head_min", "gate_wait_per_build_min", "merged_lines_per_day", "job_minutes_per_day")
WAIT_KINDS = ("github queue", "gate wait", "local lease wait")


def metric_value(scope, metric, up, source=""):
    """One metric for one scope from a rollup (`ef.rollup`); None when the window has no data for it."""
    if scope.startswith("shoal:"):
        a = up["shoals"].get(scope[len("shoal:"):])
        return None if a is None or metric not in SHOAL_METRICS else a["ratios"].get(SHOAL_METRICS[metric][2])
    a = up["repos"].get(scope)
    if a is None or metric not in REPO_METRICS:
        return None
    if metric == "source_minutes_per_head":
        return _source_minutes_per_head(a, source)
    return REPO_METRICS[metric][2](a)


def better(metric):
    return (REPO_METRICS.get(metric) or SHOAL_METRICS.get(metric) or ("down",))[0]


def window_days(scope, up):
    """Days the window holds data for a scope (repository rows, or shoal rows with role time)."""
    if scope.startswith("shoal:"):
        a = up["shoals"].get(scope[len("shoal:"):]) or {}
        return (a.get("rt") or {}).get("days", 0)
    return (up["repos"].get(scope) or {}).get("days", 0)


# ---- the experiments log ------------------------------------------------------------

def load_log(path):
    """(experiments {id: last record}, verdicts {id: [records, oldest first]}, every line in order)."""
    exps, verdicts, lines = {}, {}, []
    try:
        with open(path) as fh:
            for line in fh:
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(d, dict) or not d.get("id"):
                    continue
                lines.append(d)
                if d.get("type") == "verdict":
                    verdicts.setdefault(d["id"], []).append(d)
                else:
                    exps[d["id"]] = d
    except OSError:
        pass
    return exps, verdicts, lines


def save_verdicts(path, lines, new):
    """Rewrite the log atomically with `new` appended; a verdict for the same experiment and review
    day replaces the earlier one, so a re-run review does not count twice."""
    keys = {(v["id"], v["day"]) for v in new}
    keep = [d for d in lines if not (d.get("type") == "verdict" and (d["id"], d.get("day")) in keys)]
    ef._write(path, "".join(json.dumps(d, sort_keys=True) + "\n" for d in keep + new))


def predicted_change(exp, before):
    p = exp.get("predicted") or {}
    if p.get("change") is not None:
        return float(p["change"])
    if p.get("to") is not None:
        base = p.get("from") if p.get("from") is not None else before
        return None if not base else (float(p["to"]) - float(base)) / float(base)
    return None


def evaluate(exp, rows, end, verdicts=(), bounds=ef.DEFAULT_BUCKETS, min_coverage=ef.MIN_COVERAGE):
    """The state of one experiment at review day `end`: pending, closed (a final verdict stands),
    waiting (too few days after it), or a verdict record to append."""
    vid = exp["id"]
    final = [v for v in verdicts if v.get("final")]
    if final:
        return dict(final[-1], state="closed")
    if exp.get("status") == "pending" or not exp.get("date"):
        return {"id": vid, "state": "pending"}
    date = exp["date"]
    before = exp.get("before") or [ef.shift(date, -WINDOW_DAYS), ef.shift(date, -1)]
    bdays = _span(before[0], before[1])
    start = exp.get("after") or ef.shift(date, 1)
    adays = [d for d in _span(start, ef.shift(start, MAX_AFTER_DAYS - 1)) if d <= end]
    scope, metric, src = exp.get("scope") or "", exp.get("metric") or "", exp.get("source") or ""
    if metric not in REPO_METRICS and metric not in SHOAL_METRICS:
        return {"id": vid, "state": "invalid", "note": f"unknown metric {metric!r}"}
    ub = ef.rollup(rows, bdays, rows, bounds, min_coverage)
    ua = ef.rollup(rows, adays, rows, bounds, min_coverage)
    n_after = window_days(scope, ua) if adays else 0
    if n_after < MIN_AFTER_DAYS:
        return {"id": vid, "state": "waiting", "after_days": n_after}
    vb, va = metric_value(scope, metric, ub, src), metric_value(scope, metric, ua, src)
    p = predicted_change(exp, vb)
    m = None if vb in (None, 0) or va is None else round((va - vb) / abs(vb), 4)
    noise = float(exp.get("noise", NOISE))
    notes = []
    if p is None or p == 0:
        verdict = "inconclusive"
        notes.append("no predicted change to check")
    elif m is None:
        verdict = "inconclusive"
        notes.append("the metric has no value in one window")
    else:
        sign = 1 if p > 0 else -1
        if m * sign >= max(noise, abs(p) / 2):
            verdict = "keep"
        elif m * sign <= -noise:
            verdict = "revert"
        else:
            verdict = "inconclusive"
    worse = guardrails_worse(scope, ub, ua)
    if worse:
        notes.append("guardrail worse: " + ", ".join(worse))
        if verdict == "keep":
            verdict = "inconclusive"
    open_days = len(_span(start, end)) if start <= end else 0
    is_final = (verdict in ("keep", "revert") and n_after >= SETTLE_DAYS) or open_days >= MAX_OPEN_DAYS
    return {"type": "verdict", "id": vid, "day": end, "state": "judged", "computed_at": round(time.time(), 1),
            "scope": scope, "metric": metric, "source": src or None,
            "before": {"days": [bdays[0], bdays[-1]], "value": vb, "n": window_days(scope, ub)},
            "after": {"days": [adays[0], adays[-1]], "value": va, "n": n_after},
            "measured_change": m, "predicted_change": None if p is None else round(p, 4),
            "verdict": verdict, "final": bool(is_final), "notes": notes}


def guardrails_worse(scope, ub, ua):
    """Guardrails of a repository scope worse after than before by more than their tolerance."""
    if scope.startswith("shoal:"):
        return []
    out = []
    for g, tol in GUARDRAILS.items():
        b, a = metric_value(scope, g, ub), metric_value(scope, g, ua)
        if b is not None and a is not None and a - b > tol:
            out.append(f"{g} {b:g} to {a:g}")
    return out


def overlaps(exps):
    """{id: [ids]} of open experiments on the same scope whose after windows start within a week of
    each other: their measured changes are not theirs alone. Two lines for one change (the same
    `ref`) do not confound each other."""
    live = [e for e in exps.values() if e.get("date") and e.get("status") != "pending"]
    out = {}
    for e in live:
        s = e.get("after") or ef.shift(e["date"], 1)
        for o in live:
            if o is e or o.get("scope") != e.get("scope") or (e.get("ref") and o.get("ref") == e.get("ref")):
                continue
            t = o.get("after") or ef.shift(o["date"], 1)
            if abs(len(_span(min(s, t), max(s, t))) - 1) < WINDOW_DAYS:
                out.setdefault(e["id"], []).append(o["id"])
    return out


def _span(a, b):
    out, d = [], a
    while d <= b:
        out.append(d)
        d = ef.shift(d, 1)
    return out


# ---- the review ---------------------------------------------------------------------

def review(rows, end, baseline=None, exps=None, verdicts=None, bounds=ef.DEFAULT_BUCKETS,
           min_coverage=ef.MIN_COVERAGE, now=None):
    """Everything the report says, as data: windows, rollups, metric tables, the waste ranking and
    the experiments' states (new verdict records under `verdicts_new`)."""
    cur = ef.last_days(WINDOW_DAYS, end)
    prev = ef.last_days(WINDOW_DAYS, ef.shift(end, -WINDOW_DAYS))
    wins = {"current": cur, "previous": prev}
    if baseline:
        wins["baseline"] = _span(baseline[0], baseline[1])
    ups = {k: ef.rollup(rows, d, rows, bounds, min_coverage) for k, d in wins.items()}
    repos = sorted({r for u in ups.values() for r in u["repos"]})
    shoals = sorted({s for u in ups.values() for s in u["shoals"]})
    tables = {}
    for repo in repos:
        tables[repo] = {m: {w: metric_value(repo, m, ups[w]) for w in wins}
                        for m in HEADLINE + tuple(GUARDRAILS)}
    for sid in shoals:
        tables["shoal:" + sid] = {m: {w: metric_value("shoal:" + sid, m, ups[w]) for w in wins} for m in SHOAL_METRICS}
    exps, verdicts = exps or {}, verdicts or {}
    states = [evaluate(e, rows, end, verdicts.get(i, ()), bounds, min_coverage) for i, e in sorted(exps.items())]
    ov = overlaps(exps)
    for s in states:
        if s["id"] in ov and s.get("state") == "judged":
            s["notes"] = s.get("notes", []) + ["overlaps " + ", ".join(sorted(ov[s["id"]]))]
    return {"end": end, "windows": {k: [d[0], d[-1]] for k, d in wins.items()}, "ups": ups, "tables": tables,
            "coverage": {k: {"repos": {r: (a["days"], a["github_days"]) for r, a in u["repos"].items()},
                             "shoals": {s: (a["days"], a["low_coverage_days"]) for s, a in u["shoals"].items()}}
                         for k, u in ups.items()},
            "waste": waste(ups), "experiments": states,
            "verdicts_new": [{k: v for k, v in s.items() if k != "state"} for s in states if s.get("state") == "judged"]}


def waste(ups, top=TOP):
    """The largest sources in the current window, split into CI time (cost) and waits: hours, and
    minutes per PR head against the previous and baseline windows."""
    out = {"cost": [], "wait": []}
    cur = ups["current"]["repos"]
    for repo, a in cur.items():
        for src, sec in (a.get("by_source") or {}).items():
            kind = "wait" if src.split(":", 1)[0] in WAIT_KINDS else "cost"
            per = {w: _source_minutes_per_head(u["repos"][repo], _glob_escape(src)) if repo in u["repos"] else None
                   for w, u in ups.items()}
            out[kind].append({"repo": repo, "source": src, "hours": round(sec / 3600, 1), "per_head_min": per})
    for k in out:
        out[k] = sorted(out[k], key=lambda x: -x["hours"])[:top]
    return out


def _glob_escape(s):
    return "".join(f"[{c}]" if c in "*?[" else c for c in s)


# ---- the Markdown report ------------------------------------------------------------

def _fmt(v):
    if v is None:
        return "-"
    if isinstance(v, float):
        f = "{:,.1f}" if abs(v) >= 10 else "{:.2f}" if abs(v) >= 0.1 else "{:.3f}"
        return f.format(v).rstrip("0").rstrip(".") if "." in f.format(v) else f.format(v)
    return f"{v:,}" if isinstance(v, int) else str(v)


def _delta(a, b):
    if a is None or b in (None, 0):
        return "-"
    return f"{100 * (a - b) / abs(b):+.0f}%"


def _md(s):
    return str(s).replace("|", "\\|")


def render(doc):
    w = doc["windows"]
    has_base = "baseline" in w
    L = [f"# CI efficiency review, week ending {doc['end']}", "",
         f"Current window {w['current'][0]} to {w['current'][1]}; previous {w['previous'][0]} to {w['previous'][1]}"
         + (f"; baseline {w['baseline'][0]} to {w['baseline'][1]}." if has_base else "; no baseline window configured."),
         "Ratios divide sums over the window; guardrails sit beside them. Computed from the snapshot rows only.", ""]
    head = "| metric | better | current | previous | change |" + (" baseline | vs baseline |" if has_base else "")
    rule = "|---|---|---:|---:|---:|" + ("---:|---:|" if has_base else "")
    for scope, t in doc["tables"].items():
        L += [f"## {_md(scope)}", "", head, rule]
        for m, v in t.items():
            row = f"| {m} | {better(m)} | {_fmt(v['current'])} | {_fmt(v['previous'])} | {_delta(v['current'], v['previous'])} |"
            if has_base:
                row += f" {_fmt(v.get('baseline'))} | {_delta(v['current'], v.get('baseline'))} |"
            L.append(row)
        L.append("")
    for kind, title in (("cost", "CI time"), ("wait", "waits")):
        L += [f"## Largest sources of waste: {title} (current window)", "",
              "| # | repository | source | hours | min per head | previous | change |"
              + (" baseline | vs baseline |" if has_base else ""),
              "|---:|---|---|---:|---:|---:|---:|" + ("---:|---:|" if has_base else "")]
        for i, x in enumerate(doc["waste"][kind], 1):
            p = x["per_head_min"]
            row = (f"| {i} | {_md(x['repo'])} | {_md(x['source'])} | {_fmt(x['hours'])} | {_fmt(p.get('current'))} | "
                   f"{_fmt(p.get('previous'))} | {_delta(p.get('current'), p.get('previous'))} |")
            if has_base:
                row += f" {_fmt(p.get('baseline'))} | {_delta(p.get('current'), p.get('baseline'))} |"
            L.append(row)
        if not doc["waste"][kind]:
            L.append("| | (no sources recorded in this window) | | | | | |")
        L.append("")
    L += ["## Experiments", ""]
    exps = doc["experiments"]
    judged = [s for s in exps if s.get("state") == "judged"]
    if judged:
        L += ["| experiment | metric | before | after | measured | predicted | verdict | notes |",
              "|---|---|---:|---:|---:|---:|---|---|"]
        for s in judged:
            b, a = s["before"], s["after"]
            met = s["metric"] + (f" ({s['source']})" if s.get("source") else "")
            L.append(f"| {_md(s['id'])} | {_md(met)} | {_fmt(b['value'])} ({b['days'][0]} to {b['days'][1]}) | "
                     f"{_fmt(a['value'])} ({a['days'][0]} to {a['days'][1]}, {a['n']} d) | "
                     f"{_pct(s['measured_change'])} | {_pct(s['predicted_change'])} | "
                     f"{s['verdict']}{'' if s['final'] else ' (provisional)'} | {_md('; '.join(s.get('notes') or []))} |")
        L.append("")
    for state, title in (("waiting", "Waiting for days after the change"), ("pending", "Pending (not yet in effect)"),
                         ("closed", "Closed"), ("invalid", "Invalid")):
        xs = [s for s in exps if s.get("state") == state]
        if xs:
            L.append(f"{title}: " + ", ".join(
                _md(s["id"]) + (f" ({s.get('verdict')}, {s.get('day')})" if state == "closed" else "")
                + (f" ({s.get('after_days', 0)} of {MIN_AFTER_DAYS} days)" if state == "waiting" else "")
                + (f" ({s.get('note')})" if state == "invalid" else "") for s in xs) + ".")
    if not exps:
        L.append("No experiments logged.")
    L += ["", "## Data", ""]
    for k, c in doc["coverage"].items():
        parts = [f"{r} {d} days ({g} with merges counted)" for r, (d, g) in sorted(c["repos"].items())]
        parts += [f"shoal {s} {d} days ({lo} under the role-time coverage floor)" for s, (d, lo) in sorted(c["shoals"].items())]
        L.append(f"- {k}: " + ("; ".join(parts) if parts else "no rows") + ".")
    return "\n".join(L) + "\n"


def _pct(v):
    return "-" if v is None else f"{100 * v:+.0f}%"


# ---- waking a seat (opt-in) -----------------------------------------------------------

def wake_seat(label, text, dry_run=False):
    """Deliver `text` to a saved standalone seat on this host, the way an operator handoff asks one
    for a checkpoint, only when it is between turns. Returns what happened."""
    here = os.path.dirname(os.path.abspath(__file__))
    sys.path.insert(0, os.path.join(here, "ui"))
    import backends
    import handoff_flow
    try:
        entry = handoff_flow.standalone_entry(label)
    except handoff_flow.HandoffError as exc:
        return f"not woken: {exc}"
    if not handoff_flow.between(entry):
        return "not woken: the seat is mid-turn (it can read the report when it finishes)"
    if dry_run:
        return "dry-run: would wake"
    backend = backends.get(entry["harness"])
    if entry["harness"] == "claude":
        import handover
        spec, role = handover._pseudo(entry)
        return "woken: " + backend.deliver(spec, role, entry["session"], text)
    backend.send(entry["session"], entry.get("cwd") or os.getcwd(), text, model=entry.get("model", ""))
    return "woken: send"


# ---- commands -------------------------------------------------------------------------

def review_settings(a):
    import config
    f = config.file_values().get("efficiency") or {}
    f = f if isinstance(f, dict) else {}
    s = ef.settings(out=a.rows)
    base = os.path.dirname(os.path.abspath(s["out"]))
    s["experiments"] = os.path.expanduser(a.experiments or f.get("experiments") or os.path.join(base, "experiments.jsonl"))
    s["report_dir"] = os.path.expanduser(a.report_dir or f.get("report_dir") or os.path.join(base, "reports"))
    b = a.baseline.split(":") if a.baseline else (f.get("baseline") or [])
    if b and (len(b) != 2 or b[0] > b[1]):
        raise SystemExit("efficiency review: --baseline is START:END (YYYY-MM-DD, START first)")
    for d in b:
        try:
            ef.day_bounds(d)
        except ValueError:
            raise SystemExit("efficiency review: baseline days are YYYY-MM-DD")
    s["baseline"] = list(b) or None
    s["seat"] = a.wake_seat or f.get("review_seat") or ""
    return s


def cmd_review(a):
    s = review_settings(a)
    rows = ef.load_rows(s["out"])
    if not rows:
        print(f"efficiency review: no snapshot rows in {s['out']}", file=sys.stderr)
        return 2
    last = max(r["day"] for r in rows)
    end = a.day or last
    exps, verdicts, lines = load_log(s["experiments"])
    doc = review(rows, end, s["baseline"], exps, verdicts, s["buckets"], s["min_coverage"])
    text = render(doc)
    if a.json:
        print(json.dumps({k: v for k, v in doc.items() if k != "ups"}, indent=1, default=list))
    path = os.path.join(s["report_dir"], f"efficiency-review-{end}.md")
    if a.dry_run:
        if not a.json:
            print(text)
        return 0
    ef._write(path, text)
    if doc["verdicts_new"]:
        save_verdicts(s["experiments"], lines, doc["verdicts_new"])
    if not a.json:
        print(f"efficiency review: {path}")
        for v in doc["verdicts_new"]:
            print(f"  {v['id']}: {v['verdict']}{'' if v['final'] else ' (provisional)'}, {v['metric']} "
                  f"{_fmt(v['before']['value'])} to {_fmt(v['after']['value'])} ({_pct(v['measured_change'])}, "
                  f"predicted {_pct(v['predicted_change'])})")
    if last < ef.day_of(time.time() - 2 * ef.DAY_S):
        print(f"efficiency review: the newest snapshot row is for {last}: is the snapshot timer running?", file=sys.stderr)
    if s["seat"]:
        msg = (f"The weekly CI efficiency review is written: {path}. Read it and propose at most three rule or CI "
               f"infrastructure changes aimed at its largest sources of waste, each as an experiment line for "
               f"{s['experiments']} (metric, predicted change, date it takes effect). Make no change yourself.")
        print(f"efficiency review: seat {s['seat']}: {wake_seat(s['seat'], msg)}")
    return 0


def cmd_experiment(a):
    s = review_settings(argparse_ns(rows=a.rows, experiments=a.experiments))
    if a.metric not in REPO_METRICS and a.metric not in SHOAL_METRICS:
        print(f"efficiency experiment: unknown metric {a.metric!r} (one of {', '.join(list(REPO_METRICS) + list(SHOAL_METRICS))})",
              file=sys.stderr)
        return 2
    if a.date:
        ef.day_bounds(a.date)
    pred = {"change": a.predict_change} if a.predict_change is not None else {"to": a.predict_to}
    if a.predict_from is not None:
        pred["from"] = a.predict_from
    if pred.get("change") is None and pred.get("to") is None:
        print("efficiency experiment: give --predict-change or --predict-to", file=sys.stderr)
        return 2
    rec = {"type": "experiment", "id": a.id, "date": a.date or None, "scope": a.scope, "change": a.change,
           "ref": a.ref or None, "metric": a.metric, "source": a.source or None, "predicted": pred,
           "before": a.before.split(":") if a.before else None, "after": a.after or None,
           "status": "pending" if not a.date else "open", "logged_at": round(time.time(), 1)}
    rec = {k: v for k, v in rec.items() if v is not None}
    if a.dry_run:
        print(json.dumps(rec, sort_keys=True))
        return 0
    os.makedirs(os.path.dirname(os.path.abspath(s["experiments"])), exist_ok=True)
    with open(s["experiments"], "a") as fh:
        fh.write(json.dumps(rec, sort_keys=True) + "\n")
    print(f"efficiency experiment: {a.id} logged in {s['experiments']}" + ("" if a.date else " (pending: no date yet)"))
    return 0


def argparse_ns(**kw):
    import argparse
    base = {"rows": "", "experiments": "", "report_dir": "", "baseline": "", "wake_seat": ""}
    base.update({k: v or "" for k, v in kw.items()})
    return argparse.Namespace(**base)


def add_parsers(s):
    """`efficiency review` and `efficiency experiment` under the `efficiency` subcommands."""
    r = s.add_parser("review", help="this week against the last and a baseline; waste ranking; experiments checked")
    r.add_argument("--rows", default="", help="snapshot JSONL (default: [efficiency] out)")
    r.add_argument("--day", default="", help="the last UTC day of the current window (default: the newest row)")
    r.add_argument("--baseline", default="", help="START:END fixed baseline window (default: [efficiency] baseline)")
    r.add_argument("--experiments", default="", help="experiments log JSONL (default: beside the rows)")
    r.add_argument("--report-dir", default="", help="where the dated Markdown goes (default: <rows dir>/reports)")
    r.add_argument("--wake-seat", default="", help="after the review, wake this saved standalone seat with the "
                                                   "report's path (default: [efficiency] review_seat, else none)")
    r.add_argument("--dry-run", action="store_true", help="print the report; write nothing, wake no one")
    r.add_argument("--json", action="store_true", help="print the review as JSON")
    r.set_defaults(func=cmd_review)
    e = s.add_parser("experiment", help="log a rule or CI infrastructure change with the metric it should move")
    e.add_argument("--id", required=True, help="a short slug; logging the same id again replaces it")
    e.add_argument("--scope", required=True, help="<owner>/<name>, or shoal:<id> for a role-time metric")
    e.add_argument("--metric", required=True, help="a review metric (e.g. job_minutes_per_head, lease_share)")
    e.add_argument("--change", required=True, help="what changed, in a sentence")
    e.add_argument("--date", default="", help="UTC day it took effect (none: pending)")
    e.add_argument("--ref", default="", help="<owner>/<name>#<n>")
    e.add_argument("--source", default="", help="for source_minutes_per_head: a glob over source names")
    e.add_argument("--predict-change", type=float, default=None, help="relative change predicted, e.g. -0.25")
    e.add_argument("--predict-to", type=float, default=None, help="or the value predicted")
    e.add_argument("--predict-from", type=float, default=None, help="the value --predict-to is measured from")
    e.add_argument("--before", default="", help="START:END before window (default: the 7 days before --date)")
    e.add_argument("--after", default="", help="first day of the after window (default: the day after --date)")
    e.add_argument("--rows", default="")
    e.add_argument("--experiments", default="")
    e.add_argument("--dry-run", action="store_true")
    e.set_defaults(func=cmd_experiment)
