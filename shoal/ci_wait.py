"""CI wait by repository: how long a change waits for CI, and why (architecture doc §9).

Three sources, per configured repository (`features.ci_wait.repos` in a spec):

- GitHub Actions: per workflow job, queue time (job created to started) and run time, with reruns.
- The Azure Pipelines gate: per build, the approval wait (the `Checkpoint.Approval` timeline record,
  where a serial gate's queueing shows) and the run time. Azure times are authoritative: the
  GitHub check run that mirrors the gate lags it. `az` runs under a login shell, so its credential
  stays in that environment and is never read or copied here.
- Local CI on the SHOAL hosts: `shoal job` records (lease wait before a placed job's grant, run
  time after it) and the GPU lease's own observed reservations from the resource sampler (direct
  test runs GitHub never sees).

Per PR head, "CI complete" is the last completion among every check counted on that head:
GitHub jobs plus gate builds, minus a per-repository `ignore` list (workflow, job or pipeline
names, as regular expressions), or exactly the `required` list when one is given.

Records go to one JSONL store per repository under the data root, keyed so a re-read replaces a
record (the last record for a key wins). Nothing here names a repository: the configuration does.

    "features": {"ci_wait": {
      "repos": [{"repo": "<owner>/<name>",
                 "azure": [{"org": "https://dev.azure.com/<org>", "project": "<project>"}],
                 "ignore": ["<workflow or check regex>"], "required": [],
                 "local": {"paths": ["/<name>/"], "holders": ["^<prefix>"]}}],
      "backfill_days": 7}}
"""
from __future__ import annotations

import argparse
import datetime as dt
import glob
import gzip
import json
import os
import re
import shlex
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

STORE_V = 1
GH_RECHECK_DAYS = 2        # re-read the last days for late completions and reruns
GH_MAX_PAGES = 10          # 1,000 runs per UTC day: the list endpoint's own cap
AZ_TOP = 200


# ---- time --------------------------------------------------------------------------

def _epoch(v):
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).replace("Z", "+00:00")
    # Azure writes up to seven fractional digits and some APIs fewer; this Python's fromisoformat
    # takes exactly three or six, so normalize to six
    s = re.sub(r"\.(\d+)", lambda m: "." + (m.group(1) + "000000")[:6], s)
    try:
        t = dt.datetime.fromisoformat(s)
    except ValueError:
        return None
    return t.timestamp() if t.tzinfo else None


def _day(t):
    return dt.datetime.fromtimestamp(float(t), dt.timezone.utc).strftime("%Y-%m-%d")


def _dur(a, b):
    return None if a is None or b is None else max(0.0, float(b) - float(a))


# ---- configuration -----------------------------------------------------------------

def feature(spec) -> dict:
    """The spec's `features.ci_wait` as a dict with a normalized `repos` list ({} when off)."""
    f = (getattr(spec, "features", None) or {}).get("ci_wait")
    if not isinstance(f, dict):
        return {}
    repos = []
    for r in f.get("repos") or []:
        if isinstance(r, dict) and re.match(r"^[^/\s]+/[^/\s]+$", str(r.get("repo") or "")):
            repos.append(dict(r, ignore=list(r.get("ignore") or []), required=list(r.get("required") or []),
                              azure=list(r.get("azure") or []), local=dict(r.get("local") or {})))
    return dict(f, repos=repos)


def _matches(name, patterns):
    return any(re.search(p, name or "") for p in patterns)


def counted(cfg, *names):
    """True when a check (any of its names: workflow, job, pipeline) counts toward CI complete."""
    names = [n for n in names if n]
    if cfg.get("required"):
        return any(_matches(n, cfg["required"]) for n in names)
    return not any(_matches(n, cfg.get("ignore") or []) for n in names)


# ---- external commands -------------------------------------------------------------

def default_runner(argv, login=False, timeout=60):
    """(returncode, stdout). `login` runs under `bash -lc`, so a credential exported only in login
    shells is available to the tool without this process ever reading it."""
    cmd = ["bash", "-lc", shlex.join(argv)] if login else argv
    try:
        p = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout
    except (OSError, subprocess.TimeoutExpired):
        return 1, ""


def _json(run, argv, login=False):
    rc, out = run(argv, login=login)
    if rc != 0 or not out.strip():
        return None
    try:
        return json.loads(out)
    except ValueError:
        return None


# ---- GitHub Actions ----------------------------------------------------------------

def gh_runs_for_day(run, repo, day):
    """Every workflow run created on one UTC day (the list endpoint caps at 1,000 per query)."""
    out = []
    for page in range(1, GH_MAX_PAGES + 1):
        d = _json(run, ["gh", "api", f"repos/{repo}/actions/runs?created={day}&per_page=100&page={page}"])
        if not isinstance(d, dict):
            return None if page == 1 else out
        batch = d.get("workflow_runs") or []
        out += batch
        if len(batch) < 100:
            break
    return out


def gh_jobs(run, repo, run_id):
    d = _json(run, ["gh", "api", f"repos/{repo}/actions/runs/{run_id}/jobs?filter=all&per_page=100"])
    return None if not isinstance(d, dict) else (d.get("jobs") or [])


def gh_job_records(repo, wf_run, jobs):
    out = []
    for j in jobs:
        created, started, done = _epoch(j.get("created_at")), _epoch(j.get("started_at")), _epoch(j.get("completed_at"))
        out.append({"type": "gh_job", "key": f"gh:{wf_run['id']}:{j.get('run_attempt', 1)}:{j['id']}",
                    "repo": repo, "workflow": wf_run.get("name") or "", "job": j.get("name") or "",
                    "event": wf_run.get("event") or "", "head_sha": j.get("head_sha") or wf_run.get("head_sha"),
                    "attempt": int(j.get("run_attempt") or wf_run.get("run_attempt") or 1),
                    "created": created, "started": started, "completed": done,
                    "conclusion": j.get("conclusion"), "runner": j.get("runner_name") or "",
                    "hosted": "self-hosted" not in [str(x) for x in j.get("labels") or []],
                    "queue_s": _dur(created, started), "run_s": _dur(started, done)})
    return out


def gh_pr_for_sha(run, repo, sha, cache):
    """The PR a head SHA belongs to (`commits/{sha}/pulls`), cached: a run's own `pull_requests` is
    empty for most closed PRs."""
    if sha in cache:
        return cache[sha]
    d = _json(run, ["gh", "api", f"repos/{repo}/commits/{sha}/pulls"])
    pr = None
    if isinstance(d, list):
        heads = [x for x in d if (x.get("head") or {}).get("sha") == sha] or d
        pr = heads[0].get("number") if heads else None
    cache[sha] = pr
    return pr


# ---- Azure Pipelines gate ----------------------------------------------------------

def az_builds(run, org, project):
    d = _json(run, ["az", "pipelines", "runs", "list", "--org", org, "--project", project,
                    "--top", str(AZ_TOP), "-o", "json"], login=True)
    return d if isinstance(d, list) else None


def az_timeline(run, org, project, build_id):
    d = _json(run, ["az", "devops", "invoke", "--area", "build", "--resource", "timeline",
                    "--route-parameters", f"project={project}", f"buildId={build_id}",
                    "--org", org, "-o", "json"], login=True)
    return (d or {}).get("records") if isinstance(d, dict) else None


def _build_repo(b):
    r = b.get("repository") or {}
    return r.get("id") if "/" in str(r.get("id") or "") else r.get("name") or ""


def az_build_record(repo, project, b, timeline):
    """One gate build: the approval wait is the `Checkpoint.Approval` record; with none (a
    scheduled or auto-passed run) the wait is queue to first job start."""
    trig = b.get("triggerInfo") or {}
    recs = timeline or []
    appr = next((r for r in recs if r.get("type") == "Checkpoint.Approval"), None)
    jobs = [t for t in (_epoch(r.get("startTime")) for r in recs if r.get("type") == "Job") if t is not None]
    queued, finished = _epoch(b.get("queueTime")), _epoch(b.get("finishTime"))
    started = min(jobs) if jobs else None
    a0, a1 = (_epoch(appr.get("startTime")), _epoch(appr.get("finishTime"))) if appr else (None, None)
    gate_wait = _dur(a0, a1) if appr else _dur(queued, started)
    return {"type": "az_build", "key": f"az:{b['id']}", "repo": repo, "project": project,
            "pipeline": (b.get("definition") or {}).get("name") or "", "build": b["id"],
            "pr": int(trig["pr.number"]) if str(trig.get("pr.number") or "").isdigit() else None,
            "head_sha": trig.get("pr.sourceSha") or b.get("sourceVersion"), "reason": b.get("reason") or "",
            "queued": queued, "approval_start": a0, "approval_end": a1,
            "approval_result": (appr or {}).get("result"), "started": started, "finished": finished,
            "status": b.get("status"), "result": b.get("result"),
            "gate_wait_s": gate_wait, "run_s": _dur(started, finished)}


# ---- local CI ----------------------------------------------------------------------

def _repo_from_path(path, rules):
    for cfg in rules:
        for p in (cfg.get("local") or {}).get("paths") or []:
            if p and p in (path or "").rstrip("/") + "/":  # a repository root has no trailing slash
                return cfg["repo"]
    return None


def _repo_from_holder(holder, rules):
    for cfg in rules:
        if _matches(holder, (cfg.get("local") or {}).get("holders") or []):
            return cfg["repo"]
    return None


def local_jobs(meta_dirs, rules):
    """`shoal job` records: a placed job's repo is its placement record's; any other job's comes
    from its working directory by the configured path rules (else it is unattributed). Lease wait
    is submit to grant (placed jobs only); run time is grant (or start) to its exit-code file."""
    out = []
    for d in meta_dirs:
        for f in sorted(glob.glob(os.path.join(d, "*.json"))):
            try:
                m = json.load(open(f))
            except (OSError, ValueError):
                continue
            if not isinstance(m, dict) or not m.get("unit"):
                continue
            pl = m.get("placement") or {}
            repo = pl.get("repo") if "/" in str(pl.get("repo") or "") else None
            repo = repo or _repo_from_path(m.get("cwd"), rules)
            if repo and repo not in {c["repo"] for c in rules}:
                continue
            start = _epoch(m.get("started_utc"))
            submit = _epoch(pl.get("submit_ts")) or start
            granted = _epoch(m.get("lease_granted")) if pl else start
            try:
                finished = os.path.getmtime(m["rc_file"]) if m.get("rc_file") and os.path.exists(m["rc_file"]) else None
            except OSError:
                finished = None
            out.append({"type": "local_job", "key": f"job:{m.get('shoal')}:{m['unit']}", "repo": repo,
                        "role": m.get("slug") or "", "name": m.get("name") or "",
                        "kind": m.get("class") or ("ci" if pl else "plain"), "host": m.get("host") or "",
                        "submit": submit, "granted": granted, "finished": finished,
                        "lease_wait_s": _dur(submit, granted) if pl else None,
                        "run_s": _dur(granted or start, finished)})
    return out


def local_leases(event_dirs, rules):
    """The GPU lease's observed reservations (resource sampler events): who held which key for how
    long. A holder is a repository by the configured holder rules, else unattributed; a holder
    naming the Azure gate's own build is marked `gate`, so it is never counted twice."""
    opened, out = {}, []
    files = []
    for d in event_dirs:
        files += glob.glob(os.path.join(d, "events-*.jsonl")) + glob.glob(os.path.join(d, "events-*.jsonl.gz"))
    for f in sorted(files):
        opener = gzip.open if f.endswith(".gz") else open
        try:
            fh = opener(f, "rt")
        except OSError:
            continue
        with fh:
            for line in fh:
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                t = e.get("type")
                if t not in ("lease_observed_acquire", "lease_observed_release"):
                    continue
                lease = e.get("lease") or {}
                lid = lease.get("lease_id")
                if not lid:
                    continue
                if t == "lease_observed_acquire":
                    opened[lid] = (e, lease)
                    continue
                acq, la = opened.pop(lid, (None, lease))
                holder = str(la.get("holder") or "")
                start = _epoch(la.get("since")) or (acq or {}).get("ts")
                end_lo, end_hi = e.get("release_after"), e.get("release_by") or e.get("ts")
                out.append({"type": "local_lease", "key": f"lease:{e.get('host')}:{lid}",
                            "repo": _repo_from_holder(holder, rules), "holder": holder,
                            "kind": "gate" if re.match(r"^azure-", holder) else "direct",
                            "lease_key": la.get("key"), "host": e.get("host"),
                            "acquired": start, "released": end_hi, "released_after": end_lo,
                            "held_s": _dur(start, end_hi)})
    return out


# ---- PR heads ----------------------------------------------------------------------

def pr_heads(records, cfg):
    """CI complete per PR head: the last completion among the head's counted checks, once none of
    them is still running. Job-minutes count every counted check's run time; rerun minutes are the
    part spent on attempts after the first."""
    by_sha = {}
    for r in records:
        if r["type"] == "gh_job" and r.get("pr") is not None:
            by_sha.setdefault(r["head_sha"], []).append(r)
        elif r["type"] == "az_build" and r.get("pr") is not None and r.get("head_sha"):
            by_sha.setdefault(r["head_sha"], []).append(r)
    out = []
    for sha, rs in by_sha.items():
        rs = [r for r in rs if counted(cfg, r.get("workflow"), r.get("job"), r.get("pipeline"))]
        if not rs:
            continue
        ends = [r.get("completed") if r["type"] == "gh_job" else r.get("finished") for r in rs]
        if any(e is None for e in ends):
            continue  # still running: not complete yet
        starts = [r.get("created") if r["type"] == "gh_job" else r.get("queued") for r in rs]
        starts = [s for s in starts if s is not None]
        first, done = (min(starts) if starts else None), max(ends)
        mins = sum((r.get("run_s") or 0) for r in rs) / 60
        rerun = sum((r.get("run_s") or 0) for r in rs if r.get("attempt", 1) > 1) / 60
        gate_run = sum((r.get("run_s") or 0) for r in rs if r["type"] == "az_build")
        hosted_run = sum((r.get("run_s") or 0) for r in rs if r["type"] == "gh_job" and r.get("hosted", True))
        out.append({"type": "pr_head", "key": f"head:{sha}", "repo": rs[0]["repo"], "head_sha": sha,
                    "pr": rs[0].get("pr"), "first_seen": first, "ci_complete": done,
                    "push_to_complete_s": _dur(first, done), "job_minutes": round(mins, 2),
                    "rerun_minutes": round(rerun, 2), "checks_counted": len(rs),
                    "gate_run_s": gate_run, "github_hosted_run_s": hosted_run,
                    "gate_builds": sum(1 for r in rs if r["type"] == "az_build"),
                    "gate_wait_s": sum((r.get("gate_wait_s") or 0) for r in rs if r["type"] == "az_build")})
    return out


# ---- the store ---------------------------------------------------------------------

def store_dir(root=None):
    if root:
        return root
    import config
    return os.path.join(config.data_root(), "_ci_wait")


def _store_path(root, repo):
    return os.path.join(root, repo.replace("/", "__") + ".jsonl")


def load(root, repo):
    """{key: record} for one repository; the last record for a key wins."""
    out = {}
    try:
        with open(_store_path(root, repo)) as fh:
            for line in fh:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if isinstance(r, dict) and r.get("key"):
                    out[r["key"]] = r
    except OSError:
        pass
    return out


def save(root, repo, records, known):
    """Append the records that are new or changed against `known`; returns how many."""
    os.makedirs(root, exist_ok=True)
    fresh = [r for r in records if known.get(r["key"]) != r]
    if fresh:
        with open(_store_path(root, repo), "a") as fh:
            for r in fresh:
                fh.write(json.dumps(r, sort_keys=True) + "\n")
                known[r["key"]] = r
    return len(fresh)


def _state(root):
    try:
        return json.load(open(os.path.join(root, "state.json")))
    except (OSError, ValueError):
        return {"v": STORE_V, "cursors": {}, "errors": {}}


def _save_state(root, st):
    os.makedirs(root, exist_ok=True)
    tmp = os.path.join(root, f"state.json.tmp{os.getpid()}")
    with open(tmp, "w") as fh:
        fh.write(json.dumps(st, sort_keys=True) + "\n")
    os.replace(tmp, os.path.join(root, "state.json"))


# ---- one collection pass -----------------------------------------------------------

def collect(cfg_all, root, meta_dirs=(), event_dirs=(), run=default_runner, now=None):
    """One incremental pass for every configured repository. Returns {repo: new record count}."""
    now = time.time() if now is None else float(now)
    st = _state(root)
    cur, errs = st.setdefault("cursors", {}), st.setdefault("errors", {})
    back = float(cfg_all.get("backfill_days", 7))
    rules = cfg_all.get("repos") or []
    jobs_local = local_jobs(meta_dirs, rules)
    leases_local = local_leases(event_dirs, rules)
    out = {}
    for cfg in rules:
        repo = cfg["repo"]
        known = load(root, repo)
        recs = []
        # GitHub: every UTC day from the cursor (less a re-check margin) through today
        since = float(cur.get(f"gh:{repo}", now - back * 86400)) - GH_RECHECK_DAYS * 86400
        prs = {}
        ok = True
        # runs already stored at their current `updated_at`, persisted across passes, so a re-check
        # of recent days fetches jobs only for runs that are new, rerun or finished since
        seen = st.setdefault("gh_seen", {}).get(repo, {})
        seen_now = {}
        t = max(since, now - back * 86400)
        while t <= now + 1:
            runs = gh_runs_for_day(run, repo, _day(t))
            if runs is None:
                ok = False
                break
            for wr in runs:
                if wr.get("status") != "completed":
                    continue
                rid = str(wr["id"])
                if seen.get(rid) == wr.get("updated_at"):
                    seen_now[rid] = seen[rid]
                    continue  # stored unchanged
                jobs = gh_jobs(run, repo, wr["id"])
                if jobs is None:
                    ok = False
                    continue
                pr = None
                if wr.get("event") in ("pull_request", "pull_request_target"):
                    pr = (wr.get("pull_requests") or [{}])[0].get("number") or gh_pr_for_sha(run, repo, wr["head_sha"], prs)
                for r in gh_job_records(repo, wr, jobs):
                    r["pr"] = pr
                    recs.append(r)
                seen_now[rid] = wr.get("updated_at")
            t += 86400
        # only the re-check window is listed again, so older entries can go; on a failed pass keep
        # what was known so nothing is fetched twice
        st["gh_seen"][repo] = seen_now if ok else dict(seen, **seen_now)
        if ok:
            cur[f"gh:{repo}"] = now
        else:
            errs[f"gh:{repo}"] = int(errs.get(f"gh:{repo}", 0)) + 1
        # the Azure gate: recent builds of this repository, each with its timeline
        for az in cfg.get("azure") or []:
            builds = az_builds(run, az["org"], az["project"])
            if builds is None:
                errs[f"az:{repo}"] = int(errs.get(f"az:{repo}", 0)) + 1
                continue
            for b in builds:
                if _build_repo(b) != repo or b.get("status") != "completed":
                    continue
                if (_epoch(b.get("finishTime")) or 0) < now - back * 86400:
                    continue  # outside the backfill window: no timeline read for old builds
                if known.get(f"az:{b['id']}", {}).get("finished") == _epoch(b.get("finishTime")):
                    continue
                tl = az_timeline(run, az["org"], az["project"], b["id"])
                recs.append(az_build_record(repo, az["project"], b, tl))
        recs += [r for r in jobs_local if r["repo"] == repo]
        recs += [r for r in leases_local if r["repo"] == repo]
        n = save(root, repo, recs, known)
        n += save(root, repo, pr_heads(list(known.values()), cfg), known)
        out[repo] = n
    _save_state(root, st)
    return out


# ---- the report --------------------------------------------------------------------

def _q(xs, p):
    xs = sorted(x for x in xs if x is not None)
    if not xs:
        return None
    return xs[min(len(xs) - 1, int(round(p * (len(xs) - 1))))]


def summarize(records, t0, t1, cfg=None):
    """The numbers for one repository over [t0, t1): push-to-CI-complete and gate wait (p50, p95),
    GitHub queue and run time, local lease wait and run time (CI and plain jobs; research jobs are
    not CI), CI surface per PR head, and the counted workflows, jobs and pipelines with the most
    wait per PR head (`cfg` applies the repository's ignore or required rules to that ranking)."""
    def within(t):
        return t is not None and t0 <= t < t1
    heads = [r for r in records if r["type"] == "pr_head" and within(r.get("ci_complete"))]
    gh = [r for r in records if r["type"] == "gh_job" and within(r.get("completed"))]
    az = [r for r in records if r["type"] == "az_build" and within(r.get("finished"))]
    lj = [r for r in records if r["type"] == "local_job" and r.get("kind") != "research" and within(r.get("finished"))]
    ll = [r for r in records if r["type"] == "local_lease" and r.get("kind") == "direct" and within(r.get("released"))]
    n_heads = len(heads)
    waits = {}
    rank = (lambda *n: counted(cfg, *n)) if cfg else (lambda *n: True)
    for r in gh:
        if not rank(r.get("workflow"), r.get("job")):
            continue
        waits.setdefault(f"github: {r['workflow']} / {r['job']}", []).append(r.get("queue_s") or 0)
    for r in az:
        if not rank("", "", r.get("pipeline")):
            continue
        waits.setdefault(f"gate: {r['pipeline']}", []).append(r.get("gate_wait_s") or 0)
    ranked = sorted(((k, sum(v) / max(n_heads, 1)) for k, v in waits.items()), key=lambda kv: -kv[1])
    mins = sum(r.get("job_minutes") or 0 for r in heads)

    def stats(xs):
        xs = [x for x in xs if x is not None]
        return {"avg": sum(xs) / len(xs) if xs else None, "p50": _q(xs, .5), "p95": _q(xs, .95),
                "max": max(xs) if xs else None, "n": len(xs)}
    return {"window": [t0, t1], "pr_heads": n_heads,
            # run time per PR head: the self-hosted GPU gate (heads that had a gate build) and the
            # GitHub-hosted CPU CI (every head), the two numbers targeted CI work moves
            "head_run_s": {"gate": stats([r.get("gate_run_s") for r in heads if r.get("gate_builds")]),
                           "github_hosted": stats([r.get("github_hosted_run_s") for r in heads])},
            "push_to_complete_s": {"p50": _q([r["push_to_complete_s"] for r in heads], .5),
                                   "p95": _q([r["push_to_complete_s"] for r in heads], .95)},
            "gate": {"builds": len(az), "approval_failed": sum(1 for r in az if r.get("approval_result") == "failed"),
                     "wait_s": {"p50": _q([r.get("gate_wait_s") for r in az], .5),
                                "p95": _q([r.get("gate_wait_s") for r in az], .95)},
                     "run_s_total": sum(r.get("run_s") or 0 for r in az)},
            "github": {"jobs": len(gh), "queue_s_total": sum(r.get("queue_s") or 0 for r in gh),
                       "run_s_total": sum(r.get("run_s") or 0 for r in gh),
                       "rerun_jobs": sum(1 for r in gh if r.get("attempt", 1) > 1)},
            "local": {"jobs": len(lj), "lease_wait_s_total": sum(r.get("lease_wait_s") or 0 for r in lj),
                      "run_s_total": sum(r.get("run_s") or 0 for r in lj),
                      "direct_leases": len(ll), "direct_held_s_total": sum(r.get("held_s") or 0 for r in ll)},
            "surface": {"job_minutes_per_head": round(mins / n_heads, 1) if n_heads else None,
                        "rerun_share": round(sum(r.get("rerun_minutes") or 0 for r in heads) / mins, 3) if mins else None},
            "most_wait_per_head_s": [[k, round(v, 1)] for k, v in ranked[:10]]}


def _fmt_s(v):
    return "-" if v is None else (f"{v / 3600:.1f} h" if v >= 5400 else f"{v / 60:.0f} min")


def line(repo, s):
    """One line per repository for the weekly report."""
    p, g, su = s["push_to_complete_s"], s["gate"], s["surface"]
    top = s["most_wait_per_head_s"][0] if s["most_wait_per_head_s"] else None
    return (f"{repo}: {s['pr_heads']} PR heads; push to CI complete p50 {_fmt_s(p['p50'])}, p95 {_fmt_s(p['p95'])}; "
            f"gate wait p50 {_fmt_s(g['wait_s']['p50'])} over {g['builds']} builds; "
            f"{su['job_minutes_per_head'] if su['job_minutes_per_head'] is not None else '-'} job-minutes per head, "
            f"rerun share {su['rerun_share'] if su['rerun_share'] is not None else '-'}"
            + (f"; most wait: {top[0]} ({_fmt_s(top[1])} per head)" if top else ""))


WINDOWS = (("24h", 86400.0), ("7d", 7 * 86400.0))


def _esc(v):
    return str(v).replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


def render_prometheus(cfg_all, root, now, errors=None):
    """Gauges per repository over 24 h and 7 d windows ending now: quantiles do not aggregate
    across scrapes, so the collector computes them. Labels are repository, window, source, phase
    and the counted check names (bounded by the workflows that exist), never a SHA or PR number."""
    L = []

    def metric(name, kind, help_):
        L.extend([f"# HELP {name} {help_}", f"# TYPE {name} {kind}"])

    rows = {c["repo"]: c for c in cfg_all.get("repos") or []}
    sums = {r: {w: summarize(list(load(root, r).values()), now - span, now, c) for w, span in WINDOWS}
            for r, c in rows.items()}
    metric("shoal_ci_wait_pr_heads", "gauge", "PR heads that reached CI complete in the window.")
    for r in sums:
        for w, s in sums[r].items():
            L.append(f'shoal_ci_wait_pr_heads{{repo="{_esc(r)}",window="{w}"}} {s["pr_heads"]}')
    for name, pick, help_ in (
            ("shoal_ci_wait_push_to_complete_seconds", lambda s: s["push_to_complete_s"],
             "Push (first check created) to CI complete per PR head, by quantile."),
            ("shoal_ci_wait_gate_wait_seconds", lambda s: s["gate"]["wait_s"],
             "Gate wait per build (the approval record, else queue to first job), by quantile.")):
        metric(name, "gauge", help_)
        for r in sums:
            for w, s in sums[r].items():
                for q, key in (("0.5", "p50"), ("0.95", "p95")):
                    v = pick(s)[key]
                    if v is not None:
                        L.append(f'{name}{{repo="{_esc(r)}",window="{w}",quantile="{q}"}} {v:.1f}')
    metric("shoal_ci_wait_seconds", "gauge", "Seconds of CI wait or run time in the window, by source and phase.")
    for r in sums:
        for w, s in sums[r].items():
            for src, phase, v in (("github", "queue", s["github"]["queue_s_total"]),
                                  ("github", "run", s["github"]["run_s_total"]),
                                  ("gate", "run", s["gate"]["run_s_total"]),
                                  ("local", "lease_wait", s["local"]["lease_wait_s_total"]),
                                  ("local", "run", s["local"]["run_s_total"]),
                                  ("direct", "held", s["local"]["direct_held_s_total"])):
                L.append(f'shoal_ci_wait_seconds{{repo="{_esc(r)}",window="{w}",source="{src}",phase="{phase}"}} {v:.1f}')
    metric("shoal_ci_wait_head_run_seconds", "gauge",
           "Run time per PR head: the self-hosted GPU gate (heads with a gate build) and GitHub-hosted CPU CI.")
    for r in sums:
        for w, s in sums[r].items():
            for src, st in (s.get("head_run_s") or {}).items():
                for k in ("avg", "p50", "p95", "max"):
                    if st.get(k) is not None:
                        L.append(f'shoal_ci_wait_head_run_seconds{{repo="{_esc(r)}",window="{w}",source="{src}",stat="{k}"}} {st[k]:.1f}')
    metric("shoal_ci_wait_job_minutes_per_head", "gauge", "Counted check run minutes per PR head.")
    metric("shoal_ci_wait_rerun_share", "gauge", "Share of counted check minutes spent on attempts after the first.")
    for r in sums:
        for w, s in sums[r].items():
            su = s["surface"]
            if su["job_minutes_per_head"] is not None:
                L.append(f'shoal_ci_wait_job_minutes_per_head{{repo="{_esc(r)}",window="{w}"}} {su["job_minutes_per_head"]}')
            if su["rerun_share"] is not None:
                L.append(f'shoal_ci_wait_rerun_share{{repo="{_esc(r)}",window="{w}"}} {su["rerun_share"]}')
    metric("shoal_ci_wait_check_wait_per_head_seconds", "gauge",
           "Wait per PR head attributable to one counted check (7 d window, the ten largest).")
    for r in sums:
        for check, v in sums[r]["7d"]["most_wait_per_head_s"]:
            L.append(f'shoal_ci_wait_check_wait_per_head_seconds{{repo="{_esc(r)}",check="{_esc(check)}"}} {v}')
    metric("shoal_ci_wait_source_errors_total", "counter", "Collection passes where one source failed.")
    for key, n in sorted((errors or {}).items()):
        src, _, repo = key.partition(":")
        L.append(f'shoal_ci_wait_source_errors_total{{repo="{_esc(repo)}",source="{_esc(src)}"}} {int(n)}')
    metric("shoal_ci_wait_last_success_timestamp_seconds", "gauge", "Last completed collection pass.")
    L.append(f"shoal_ci_wait_last_success_timestamp_seconds {now:.3f}")
    return "\n".join(L) + "\n"


def _write(path, text):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = f"{path}.tmp{os.getpid()}"
    with open(tmp, "w") as fh:
        fh.write(text)
    os.replace(tmp, path)


def cmd_report(a):
    import roles
    spec = roles.ShoalSpec.load(a.spec)
    cfg = feature(spec)
    repos = [c["repo"] for c in cfg.get("repos") or []]
    if a.repo:
        repos = [r for r in repos if r == a.repo]
        if not repos:
            print(f"ci-wait: {a.repo} is not configured in features.ci_wait.repos", file=sys.stderr)
            return 2
    t1 = _epoch(a.until) if a.until else time.time()
    t0 = _epoch(a.since)
    if t0 is None or t1 is None or t1 <= t0:
        print("ci-wait: --since must be an ISO time or epoch before --until", file=sys.stderr)
        return 2
    root = store_dir(a.store or None)
    by_repo = {c["repo"]: c for c in cfg.get("repos") or []}
    doc = {r: summarize(list(load(root, r).values()), t0, t1, by_repo.get(r)) for r in repos}
    if a.json:
        print(json.dumps(doc, indent=1))
        return 0
    for r, s in doc.items():
        p = s["push_to_complete_s"]
        g = s["gate"]
        print(f"{r}: {s['pr_heads']} PR heads, push to CI complete p50 {_fmt_s(p['p50'])} p95 {_fmt_s(p['p95'])}; "
              f"gate wait p50 {_fmt_s(g['wait_s']['p50'])} p95 {_fmt_s(g['wait_s']['p95'])} over {g['builds']} builds "
              f"({g['approval_failed']} approvals failed)")
        gh, lo, su = s["github"], s["local"], s["surface"]
        print(f"  github: {gh['jobs']} jobs, queue {_fmt_s(gh['queue_s_total'])}, run {_fmt_s(gh['run_s_total'])}, "
              f"{gh['rerun_jobs']} rerun jobs; local: {lo['jobs']} jobs, lease wait {_fmt_s(lo['lease_wait_s_total'])}, "
              f"run {_fmt_s(lo['run_s_total'])}; direct suites {lo['direct_leases']} held {_fmt_s(lo['direct_held_s_total'])}")
        print(f"  surface: {su['job_minutes_per_head'] if su['job_minutes_per_head'] is not None else '-'} job-minutes "
              f"per PR head, rerun share {su['rerun_share'] if su['rerun_share'] is not None else '-'}")
        for k, v in s["most_wait_per_head_s"][:5]:
            print(f"    {_fmt_s(v)} per head  {k}")
    return 0


def _meta_dirs(spec_paths):
    import roles
    from roles import state_dir
    out = []
    for p in spec_paths:
        try:
            out.append(os.path.join(state_dir(roles.ShoalSpec.load(p).sid), "jobs"))
        except Exception:  # noqa: BLE001 - an unreadable spec contributes no job records
            continue
    return out


def _event_dirs():
    import config
    return sorted(glob.glob(os.path.join(config.data_root(), "_resources", "*")))


def cmd_observe(a):
    import roles
    spec = roles.ShoalSpec.load(a.spec)
    cfg = feature(spec)
    if not cfg.get("repos"):
        print("ci-wait: no repositories configured (features.ci_wait.repos)", file=sys.stderr)
        return 2
    root = store_dir(a.store or None)
    while True:
        t0 = time.time()
        try:
            spec = roles.ShoalSpec.load(a.spec)  # re-read: repositories and rules may change
            cfg = feature(spec) or cfg
            got = collect(cfg, root, meta_dirs=_meta_dirs([a.spec] + list(a.jobs_spec or [])),
                          event_dirs=_event_dirs(), now=t0)
            if a.prom_out:
                _write(a.prom_out, render_prometheus(cfg, root, time.time(), _state(root).get("errors")))
            print(f"ci-wait observe: {got}", flush=True)
        except Exception as exc:  # noqa: BLE001 - a watcher reports and retries, it does not die
            print(f"ci-wait observe: {exc}", file=sys.stderr, flush=True)
            if not a.watch:
                return 1
        if not a.watch:
            return 0
        time.sleep(max(0.0, a.watch - (time.time() - t0)))


def add_parser(sub):
    p = sub.add_parser("ci-wait", help="CI wait by repository: GitHub Actions, the Azure gate, local CI")
    s = p.add_subparsers(dest="ci_wait_cmd", required=True)
    o = s.add_parser("observe", help="one incremental collection pass into the per-repository store")
    o.add_argument("--spec", required=True, help="the spec carrying features.ci_wait")
    o.add_argument("--jobs-spec", action="append", default=[],
                   help="another shoal whose `shoal job` records count (repeatable)")
    o.add_argument("--store", default="", help="store directory (default: <data_root>/_ci_wait)")
    o.add_argument("--watch", type=float, default=0, help="repeat every S seconds (e.g. 3600)")
    o.add_argument("--prom-out", default="", help="write the Prometheus text here after each pass")
    o.set_defaults(func=cmd_observe)
    r = s.add_parser("report", help="CI wait for one repository (or all configured) over a window")
    r.add_argument("--spec", required=True)
    r.add_argument("--repo", default="", help="<owner>/<name> (default: every configured repository)")
    r.add_argument("--since", required=True, help="window start (ISO-8601 or epoch seconds)")
    r.add_argument("--until", default="", help="window end (default: now)")
    r.add_argument("--store", default="")
    r.add_argument("--json", action="store_true")
    r.set_defaults(func=cmd_report)
