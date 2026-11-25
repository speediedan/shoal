"""CI efficiency: implemented work per unit of CI and wait overhead (architecture doc §9).

One snapshot row per repository and UTC day, and one per shoal and day where role time applies,
so rules and CI infrastructure can be tuned against a number that does not reward splitting work:

- **Throughput** is merged code lines (additions plus deletions of the PRs merged that day, less
  lockfiles, generated and vendored paths by configurable globs), never a PR count on its own.
- **Cost** is CI time from the `shoal ci-wait` store (job-minutes, push to CI complete, gate wait,
  local lease wait and run) and, per shoal, the role-time CI and lease wait seconds.
- **Ratios** divide sums (lines per CI wait hour, lines per CI job-minute) and repeat as medians per
  PR size bucket, so many small PRs do not look efficient just because each one is cheap.
- **Guardrails** sit beside every ratio: rerun share, failed gates, reverted PRs and CI jobs that did
  sustained CPU work without a lease (`unleased_cpu` on a local job record, when present).
- **Sources** (`by_source`) keep the day's seconds per CI source, so the weekly review
  (`efficiency_review.py`, which reads only these rows) can rank where the time went.

Only GitHub is read here (`gh api`, as ci_wait reads it): the search API for the PRs merged over
a run of days (one paced query per week of days) and each PR's file list (cached: a merged PR does
not change). Everything else is a file read. Nothing here names a repository, a host or a path:
repositories come from the spec's `features.ci_wait.repos`, paths from `[efficiency]` in the host
config or the command line.

    [efficiency]
    out = "<path>/efficiency.jsonl"          # the snapshot rows (default <data_root>/_efficiency/)
    cache_dir = "<path>"                     # merged PR file lists (default: beside `out`)
    store = "<path>/_ci_wait"                # the ci-wait store (default <data_root>/_ci_wait)
    role_time_dir = "<path>/role-time"       # the role-time observer's --state-dir
    exclude = ["*.lock", "vendored/*"]       # replaces the default exclusion globs (fnmatch)
    buckets = [50, 500]                      # PR size bucket bounds, in counted changed lines
    min_coverage = 0.5                       # role-time ratios need this share of eligible time classified
"""
from __future__ import annotations

import datetime as dt
import fnmatch
import json
import os
import re
import statistics
import sys
import time
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ci_wait  # noqa: E402
import role_time  # noqa: E402

ROW_V = 1
DAY_S = 86400.0
# lockfiles, pinned CI requirements, notebooks (their outputs cannot be told from their source in a
# diff, so a notebook is not counted at all), generated code, vendored trees and the vendored skill
# copies. A glob is fnmatch's: `*` crosses `/`, so `*.lock` matches a lockfile at any depth.
DEFAULT_EXCLUDE = ("*.lock", "package-lock.json", "pnpm-lock.yaml", "requirements/ci/*.txt", "*.ipynb",
                   "*_pb2.py", "*_pb2.pyi", "*.min.js", "*.min.css", "generated/*", "*/generated/*",
                   "vendored/*", "*/vendored/*", "vendor/*", "*/vendor/*", ".agents/skills/*")
DEFAULT_BUCKETS = (50, 500)
CI_WAITS = ("local-ci", "remote-ci")  # role-time wait events that are waiting on CI
LEASE_WAITS = ("lease",)
GH_SEARCH_PAGES = 10   # the search API returns at most 1,000 results
GH_SEARCH_DAYS = 7     # days per search query, well under that cap
GH_SEARCH_PAUSE = 2.5  # seconds between search calls: the search API allows 30 a minute
GH_SEARCH_RETRIES = 2  # a failed search page is retried after a backoff (a rate limit clears in a minute)
GH_FILE_PAGES = 30     # the files endpoint lists at most 3,000 files
REVERTS_RE = re.compile(r"\breverts?\s+(?:[\w.-]+/[\w.-]+)?#(\d+)", re.I)
REVERT_TITLE_RE = re.compile(r"\s*revert(?:s|ed)?\b(?!-)", re.I)  # "Revert ...", never "Revert-proof ..."
MIN_COVERAGE = 0.5  # a shoal's role-time ratios need the journal to have classified this share of eligible time
JOURNAL_LOOKBACK_DAYS = 2  # closed role-time segments older than this before the first day are not read


# ---- days --------------------------------------------------------------------------

def day_bounds(day):
    t0 = dt.datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=dt.timezone.utc).timestamp()
    return t0, t0 + DAY_S


def day_of(t):
    return dt.datetime.fromtimestamp(float(t), dt.timezone.utc).strftime("%Y-%m-%d")


def shift(day, n):
    return day_of(day_bounds(day)[0] + n * DAY_S)


def last_days(n, until_day):
    """The `n` days ending with `until_day`, oldest first."""
    return [shift(until_day, -k) for k in range(n - 1, -1, -1)]


# ---- settings ----------------------------------------------------------------------

def settings(**flags) -> dict:
    """`[efficiency]` from the host config with generic defaults; a non-empty flag wins."""
    import config
    f = config.file_values().get("efficiency") or {}
    f = f if isinstance(f, dict) else {}
    base = os.path.join(config.data_root(), "_efficiency")
    s = {"out": os.path.join(base, "efficiency.jsonl"), "cache_dir": base, "role_time_dir": "", "store": "",
         "exclude": list(DEFAULT_EXCLUDE), "buckets": list(DEFAULT_BUCKETS), "min_coverage": MIN_COVERAGE}
    for k in s:
        v = f.get(k)
        if v not in (None, "", []):
            s[k] = [str(x) for x in v] if k == "exclude" else [float(x) for x in v] if k == "buckets" else v
    cache_set = f.get("cache_dir") not in (None, "")
    for k, v in flags.items():
        if v not in (None, "", []):
            s[k] = v
            cache_set = cache_set or k == "cache_dir"
    for k in ("out", "cache_dir", "role_time_dir", "store"):
        s[k] = os.path.expanduser(str(s[k])) if s[k] else ""
    if not cache_set:
        s["cache_dir"] = os.path.dirname(os.path.abspath(s["out"]))  # the PR file cache sits beside the rows
    s["buckets"] = sorted(float(b) for b in s["buckets"])
    s["min_coverage"] = float(s["min_coverage"])
    return s


def bucket_names(bounds):
    b = [int(x) if float(x).is_integer() else x for x in bounds]
    return [f"lt{b[0]}"] + [f"{b[i - 1]}to{b[i]}" for i in range(1, len(b))] + [f"ge{b[-1]}"]


def bucket(lines, bounds):
    return bucket_names(bounds)[sum(1 for b in bounds if lines >= b)]


def excluded(path, globs):
    return any(fnmatch.fnmatchcase(path, g) for g in globs)


# ---- GitHub ------------------------------------------------------------------------

class GitHubReader:
    """The PRs merged over a run of UTC days and a PR's file list, through `gh api`. Tests inject a
    fake with the same two methods; each returns None when GitHub could not be read.

    The search API allows 30 calls a minute, so one query covers up to `GH_SEARCH_DAYS` days, calls
    are spaced `pause` seconds apart, and a failed page is retried after a backoff."""

    def __init__(self, run=None, pause=GH_SEARCH_PAUSE, retries=GH_SEARCH_RETRIES, sleep=time.sleep):
        self.run = run or ci_wait.default_runner
        self.pause, self.retries, self.sleep = pause, retries, sleep
        self._last = None
        self._names: dict[str, str] = {}

    def current_name(self, repo):
        """The repository's name now: a transferred or renamed one answers its new `owner/name` through the REST
        redirect, which search does not follow (measured 2026-11-25: after two repositories moved to an organization,
        `repo:<old owner>/<name>` failed every search, so the daily snapshot exited 1 and counted none of their
        merges). The configured name stays the key of every stored row; on a failed lookup the name is used as given."""
        if repo not in self._names:
            d = ci_wait._json(self.run, ["gh", "api", f"repos/{repo}"])
            name = d.get("full_name") if isinstance(d, dict) else None
            self._names[repo] = name if isinstance(name, str) and "/" in name else repo
        return self._names[repo]

    def _search(self, argv):
        for attempt in range(self.retries + 1):
            if self._last is not None:
                self.sleep(max(0.0, self.pause - (time.monotonic() - self._last)))
            if attempt:
                self.sleep(30.0 * attempt)
            d = ci_wait._json(self.run, argv)
            self._last = time.monotonic()
            if isinstance(d, dict):
                return d
        return None

    def merged_prs(self, repo, first_day, last_day=None):
        """Every PR merged in [first_day, last_day] (UTC days, inclusive), or None. The search
        qualifier's day edges are not documented as UTC, so the query reads a day either side and
        keeps exactly the merges inside the UTC days."""
        last_day = last_day or first_day
        t0, t1 = day_bounds(first_day)[0], day_bounds(last_day)[1]
        q = f"repo:{self.current_name(repo)} is:pr is:merged merged:{shift(first_day, -1)}..{shift(last_day, 1)}"
        out = []
        for page in range(1, GH_SEARCH_PAGES + 1):
            d = self._search(["gh", "api", f"search/issues?q={urllib.parse.quote(q)}&per_page=100&page={page}"])
            if d is None:
                return None  # a partial list would undercount the days it covers
            items = d.get("items") or []
            for it in items:
                merged = ci_wait._epoch((it.get("pull_request") or {}).get("merged_at"))
                if merged is not None and t0 <= merged < t1:
                    out.append({"number": it.get("number"), "title": it.get("title") or "",
                                "body": it.get("body") or "", "merged_at": merged})
            if len(items) < 100:
                break
        return out

    def pr_files(self, repo, number):
        out = []
        for page in range(1, GH_FILE_PAGES + 1):
            d = ci_wait._json(self.run, ["gh", "api", f"repos/{repo}/pulls/{number}/files?per_page=100&page={page}"])
            if not isinstance(d, list):
                return None  # a partial file list would undercount the PR, and would be cached for good
            out += [[f.get("filename") or "", int(f.get("additions") or 0), int(f.get("deletions") or 0)] for f in d]
            if len(d) < 100:
                break
        return out


def _cache_path(cache_dir, repo):
    return os.path.join(cache_dir, "prs-" + repo.replace("/", "__") + ".json")


def _load_json(path, default):
    try:
        with open(path) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def _write(path, text):
    ci_wait._write(path, text)


def _chunks(days, n):
    """Runs of at most `n` consecutive days (a gap in `days` starts a new run)."""
    out = []
    for d in sorted(days):
        if out and len(out[-1]) < n and shift(out[-1][-1], 1) == d:
            out[-1].append(d)
        else:
            out.append([d])
    return out


def merged_by_day(gh, repo, days, cache_dir):
    """{day: merged PRs with their `files`, or None when GitHub could not be read for that day}. A
    merged PR's file list is cached for good; one that cannot be read is returned with `files` None,
    so the row says what it could not count."""
    path = _cache_path(cache_dir, repo) if cache_dir else ""
    cache = _load_json(path, {}) if path else {}
    dirty = False
    out = {}
    for run in _chunks(days, GH_SEARCH_DAYS):
        prs = gh.merged_prs(repo, run[0], run[-1])
        for day in run:
            out[day] = None if prs is None else []
        for p in prs or []:
            key = str(p["number"])
            if key not in cache:
                files = gh.pr_files(repo, p["number"])
                if files is not None:
                    cache[key] = files
                    dirty = True
            p["files"] = cache.get(key)
            out[day_of(p["merged_at"])].append(p)
    if dirty and path:
        _write(path, json.dumps(cache, sort_keys=True) + "\n")
    return out


# ---- one repository day ------------------------------------------------------------

def _median(xs):
    xs = [x for x in xs if x is not None]
    return round(statistics.median(xs), 3) if xs else None


def _ratio(a, b, scale=1.0):
    return round(a / (b / scale), 3) if b else None


def _quant(xs, p):
    return ci_wait._q(xs, p)


def is_revert(title):
    return bool(REVERT_TITLE_RE.match(str(title or "")))


def revert_targets(pr):
    """PR numbers a PR says it reverts (GitHub's revert button writes `Reverts <owner>/<name>#N`)."""
    if not is_revert(pr.get("title")):
        return []
    return sorted({int(n) for n in REVERTS_RE.findall((pr.get("body") or "") + " " + (pr.get("title") or ""))})


def repo_day(repo, day, records, prs, cfg_repo, exclude, bounds, gh_ok=True, now=None):
    """The snapshot row for one repository and UTC day. `records` are the repository's ci-wait store
    records, `prs` the PRs merged that day with their `files` ([[path, additions, deletions]])."""
    t0, t1 = day_bounds(day)
    cfg_repo = cfg_repo or {}

    def within(t):
        return t is not None and t0 <= t < t1

    def counted(r):
        return ci_wait.counted(cfg_repo, r.get("workflow"), r.get("job"), r.get("pipeline"))

    heads_by_pr = {}
    for r in records:
        if r.get("type") == "pr_head" and r.get("pr") is not None:
            heads_by_pr.setdefault(int(r["pr"]), []).append(r)
    names = bucket_names(bounds)
    merged = {"prs": len(prs), "lines": 0, "additions": 0, "deletions": 0, "lines_excluded": 0,
              "files_unread": 0, "buckets": {b: 0 for b in names}}
    pr_rows = []
    for p in sorted(prs, key=lambda p: p["merged_at"]):
        files = p.get("files")
        if files is None:
            merged["files_unread"] += 1
            continue
        add = sum(a for f, a, d in files if not excluded(f, exclude))
        rem = sum(d for f, a, d in files if not excluded(f, exclude))
        lines = add + rem
        merged["lines"] += lines
        merged["additions"] += add
        merged["deletions"] += rem
        merged["lines_excluded"] += sum(a + d for f, a, d in files if excluded(f, exclude))
        b = bucket(lines, bounds)
        merged["buckets"][b] += 1
        heads = heads_by_pr.get(int(p["number"]), [])
        pr_rows.append({"n": int(p["number"]), "title": str(p.get("title") or "")[:120], "lines": lines,
                        "bucket": b, "merged_at": p["merged_at"], "heads": len(heads),
                        "ci_wait_s": round(sum(h.get("push_to_complete_s") or 0 for h in heads), 1),
                        "job_minutes": round(sum(h.get("job_minutes") or 0 for h in heads), 2),
                        "gate_wait_s": round(sum(h.get("gate_wait_s") or 0 for h in heads), 1),
                        "revert": is_revert(p.get("title")), "reverts": revert_targets(p)})
    if not gh_ok:
        merged = None
    heads = [r for r in records if r.get("type") == "pr_head" and within(r.get("ci_complete"))]
    gh = [r for r in records if r.get("type") == "gh_job" and within(r.get("completed")) and counted(r)]
    az = [r for r in records if r.get("type") == "az_build" and within(r.get("finished")) and counted(r)]
    lj = [r for r in records if r.get("type") == "local_job" and r.get("kind") != "research"
          and within(r.get("finished"))]
    ll = [r for r in records if r.get("type") == "local_lease" and r.get("kind") == "direct"
          and within(r.get("released"))]
    p2c = [r.get("push_to_complete_s") for r in heads]
    gw = [r.get("gate_wait_s") for r in az]
    ci = {"pr_heads": len(heads),
          "push_to_complete_s": {"sum": round(sum(x or 0 for x in p2c), 1), "p50": _quant(p2c, .5), "p95": _quant(p2c, .95)},
          "github_jobs": len(gh), "github_queue_s": round(sum(r.get("queue_s") or 0 for r in gh), 1),
          "github_run_s": round(sum(r.get("run_s") or 0 for r in gh), 1),
          "gate_builds": len(az), "gate_run_s": round(sum(r.get("run_s") or 0 for r in az), 1),
          "gate_wait_s": {"sum": round(sum(x or 0 for x in gw), 1), "p50": _quant(gw, .5), "p95": _quant(gw, .95)},
          "local_jobs": len(lj), "local_lease_wait_s": round(sum(r.get("lease_wait_s") or 0 for r in lj), 1),
          "local_run_s": round(sum(r.get("run_s") or 0 for r in lj), 1),
          "direct_held_s": round(sum(r.get("held_s") or 0 for r in ll), 1),
          "head_job_minutes": round(sum(r.get("job_minutes") or 0 for r in heads), 2),
          "rerun_minutes": round(sum(r.get("rerun_minutes") or 0 for r in heads), 2)}
    # every CI minute spent that day, whatever became of the change (an abandoned PR's CI is cost too)
    ci["job_minutes"] = round((ci["github_run_s"] + ci["gate_run_s"] + ci["local_run_s"] + ci["direct_held_s"]) / 60, 2)
    # time a change waited for CI: push to CI complete per PR head, plus local CI's lease wait and run
    ci["ci_wait_s"] = round(ci["push_to_complete_s"]["sum"] + ci["local_lease_wait_s"] + ci["local_run_s"], 1)
    unleased = [r for r in lj if "unleased_cpu" in r]
    guard = {"rerun_share": round(ci["rerun_minutes"] / ci["head_job_minutes"], 3) if ci["head_job_minutes"] else None,
             "failed_gates": sum(1 for r in az if r.get("result") == "failed"),
             "gate_approvals_failed": sum(1 for r in az if r.get("approval_result") == "failed"),
             "reverts_merged": sum(1 for p in pr_rows if p["revert"]),
             "reverted_targets": sorted({n for p in pr_rows for n in p["reverts"]}),
             # an older job record carries no `unleased_cpu` field: `observed` says how many could say
             "unleased_cpu_jobs": sum(1 for r in unleased if r.get("unleased_cpu")),
             "unleased_cpu_observed": len(unleased)}
    row = {"v": ROW_V, "kind": "repo", "repo": repo, "day": day,
           "computed_at": round(time.time() if now is None else float(now), 1),
           "merged": merged, "ci": ci, "guardrails": guard, "prs": pr_rows,
           "by_source": by_source(gh, az, lj, ll),
           "sources": {"github": "ok" if gh_ok else "error", "ci_wait_records": len(records)}}
    row["ratios"] = ratios(row)
    row["by_bucket"] = by_bucket(pr_rows, bounds)
    return row


def by_source(gh, az, lj, ll):
    """{source: seconds} for one day, the ledger `efficiency review` ranks waste from. A source is a
    kind and a name: `github run` / `github queue` / `github rerun` per counted workflow and job,
    `gate wait` / `gate run` per pipeline, `local lease wait` / `local run` per host (a job record
    with no host ran on the collecting host: `local`), and `direct hold` per lease key and host."""
    out = {}

    def add(k, v):
        if v:
            out[k] = round(out.get(k, 0.0) + float(v), 1)
    for r in gh:
        name = f"{r.get('workflow') or '?'} / {r.get('job') or '?'}"
        add(f"github run: {name}", r.get("run_s"))
        add(f"github queue: {name}", r.get("queue_s"))
        if int(r.get("attempt") or 1) > 1:
            add(f"github rerun: {name}", r.get("run_s"))
    for r in az:
        add(f"gate wait: {r.get('pipeline') or '?'}", r.get("gate_wait_s"))
        add(f"gate run: {r.get('pipeline') or '?'}", r.get("run_s"))
    for r in lj:
        add(f"local lease wait: {r.get('host') or 'local'}", r.get("lease_wait_s"))
        add(f"local run: {r.get('host') or 'local'}", r.get("run_s"))
    for r in ll:
        add(f"direct hold: {r.get('lease_key') or '?'} on {r.get('host') or 'local'}", r.get("held_s"))
    return out


def ratios(row):
    m, ci = row.get("merged") or {}, row["ci"]
    lines = m.get("lines")
    if lines is None:
        return {"lines_per_ci_wait_hour": None, "lines_per_job_minute": None, "job_minutes_per_merged_pr": None}
    return {"lines_per_ci_wait_hour": _ratio(lines, ci["ci_wait_s"], 3600),
            "lines_per_job_minute": _ratio(lines, ci["job_minutes"]),
            # no CI minutes at all reads as "not measured" (a repository the store does not cover yet), never as free
            "job_minutes_per_merged_pr": round(ci["job_minutes"] / m["prs"], 2) if m.get("prs") and ci["job_minutes"] else None}


def by_bucket(pr_rows, bounds):
    """Per size bucket, medians over the PRs in it of each PR's own lines per CI wait hour and per
    job-minute (its CI is every head the store saw for it), so a split PR is judged by its own cost."""
    out = {}
    for b in bucket_names(bounds):
        ps = [p for p in pr_rows if bucket(p["lines"], bounds) == b]
        out[b] = {"prs": len(ps), "lines": sum(p["lines"] for p in ps),
                  "median_lines_per_ci_wait_hour": _median([_ratio(p["lines"], p["ci_wait_s"], 3600) for p in ps]),
                  "median_lines_per_job_minute": _median([_ratio(p["lines"], p["job_minutes"]) for p in ps]),
                  "median_job_minutes": _median([p["job_minutes"] for p in ps if p["heads"]]),
                  "median_ci_wait_s": _median([p["ci_wait_s"] for p in ps if p["heads"]])}
    return out


# ---- one shoal day (role time) -----------------------------------------------------

def load_days(journal, days):
    """The role-time journal as `role-time report --input <journal>` reads it, kept only where the
    given UTC days need it: ({day: {role: [snapshot]}}, membership). One pass over every journal
    file: a day keeps its own snapshots and, per role, the last one before it starts (the state the
    day opens in). Same line rules as `role_time.load_observations` (policy from the segment header,
    `seq` the append order), with one difference in membership: a segment opens with every member's
    spans in full, so a role its header leaves out stopped being a member when the segment opened.
    (The report keeps the role's last record from an older segment, often an open span, and so
    charges a role removed from the spec as unknown for every day after.)"""
    days = sorted(set(days))
    bounds = [day_bounds(d) for d in days]
    starts = [b[0] for b in bounds]
    inside = {d: {} for d in days}
    prior = {d: {} for d in days}
    mem = {}
    # a closed daily segment well before the first day only holds older state (each segment opens
    # with every role's membership in full, and roles are observed every tick), so it is skipped
    floor = shift(days[0], -JOURNAL_LOOKBACK_DAYS).replace("-", "")
    for f in role_time.observation_files(journal):
        seg = role_time._segment_day(f)
        if seg and seg < floor:
            continue
        head = {}
        try:
            _read_journal(f, days, bounds, starts, inside, prior, mem, head)
        except (OSError, EOFError):
            pass  # a segment truncated by a crash: what was read of it stands
        if head.get("opened") is not None:
            t = head["opened"]
            for role in set(mem) - head["listed"]:
                mem[role] = [(a, t if e is None else min(e, t)) for a, e in mem[role] if a < t]
    out, carry = {}, {}
    for day in days:
        for role, snap in prior[day].items():
            if role not in carry or (snap["t"], snap["seq"]) >= (carry[role]["t"], carry[role]["seq"]):
                carry[role] = snap
        obs = {r: [carry[r]] for r in carry}
        for r, snaps in inside[day].items():
            obs.setdefault(r, []).extend(snaps)
            carry[r] = max(snaps + [carry.get(r, snaps[0])], key=lambda x: (x["t"], x["seq"]))
        out[day] = obs
    return out, mem


def _read_journal(f, days, bounds, starts, inside, prior, mem, head):
    """One journal file into `load_days`' accumulators; `head` gets the segment's `opened` time and
    the roles its membership lists."""
    import gzip
    opener = gzip.open if f.endswith(".gz") else open
    policy = role_time.LEGACY_POLICY  # until a segment header says otherwise
    with opener(f, "rt") as fh:
        for n, line in enumerate(fh):
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except ValueError:
                continue  # a torn final append is not an observation
            kind = d.get("type")
            if kind == "segment":
                policy = d.get("policy") or role_time.LEGACY_POLICY
                if d.get("opened") is not None:
                    head.update(opened=role_time._epoch(d["opened"]), listed=set())
            elif kind == "membership":
                mem[d["role"]] = [(role_time._epoch(a), None if e is None else role_time._epoch(e))
                                  for a, e in d["spans"]]
                head.setdefault("listed", set()).add(d["role"])
            elif kind == "snapshot":
                t = role_time._epoch(d["t"])
                snap = dict(d, t=t, seq=d.get("seq", n), policy=d.get("policy") or policy)
                for day, (t0, t1) in zip(days, bounds):
                    if t0 <= t < t1:
                        inside[day].setdefault(d["role"], []).append(snap)
                # the last snapshot before a day's start is kept for the first day starting after
                # it; a later day inherits it (or something newer) in `load_days`
                k = next((i for i, t0 in enumerate(starts) if t0 > t), None)
                if k is not None:
                    cur = prior[days[k]].get(d["role"])
                    if cur is None or (t, snap["seq"]) >= (cur["t"], cur["seq"]):
                        prior[days[k]][d["role"]] = snap


def role_time_day(obs, mem, day):
    """Work, CI and lease waits, AAR, Role Work Fraction and coverage for one UTC day."""
    t0, t1 = day_bounds(day)
    per_role, _ = role_time.account(obs, mem, t0, t1)
    s = role_time.summarize(per_role, t1 - t0)
    ev = s["events"]
    return {"delta_s": t1 - t0, "T_e": round(s["T_e"], 1), "work_s": round(s["T"]["work"], 1),
            "wait_s": round(s["T"]["wait"], 1), "unknown_s": round(s["T"]["unknown"], 1),
            "waits": {e: round(ev.get(f"wait:{e}", 0.0), 1) for e in CI_WAITS + LEASE_WAITS},
            "AAR": _round(s["AAR"]), "RWF": _round(s["RWF"]), "coverage": _round(s["coverage"]),
            "eligible_population": _round(s["eligible_population"])}


def shoal_day(sid, day, rt, repos, repo_rows, now=None, min_coverage=MIN_COVERAGE):
    """The snapshot row for one shoal and UTC day: its role time (`role_time_day`, None without a
    journal) and the merged lines of the shoal's repositories per role CI and lease wait hour. A
    repository with no counted row that day (not configured for ci-wait, or GitHub unreadable) is
    listed in `repos_uncounted`, so a sum over fewer repositories is never mistaken for the whole."""
    got = [r for r in repo_rows if r.get("repo") in repos and r.get("merged")]
    row = {"v": ROW_V, "kind": "shoal", "shoal": sid, "day": day, "repos": sorted(repos),
           "repos_uncounted": sorted(set(repos) - {r["repo"] for r in got}),
           "computed_at": round(time.time() if now is None else float(now), 1), "role_time": rt,
           "merged_lines": sum(r["merged"]["lines"] for r in got) if got else None,
           "merged_prs": sum(r["merged"]["prs"] for r in got) if got else None}
    row["ratios"] = shoal_ratios(rt, row["merged_lines"], min_coverage)
    return row


def _round(v, n=4):
    return None if v is None else round(v, n)


def _coverage(rt):
    return (rt["T_e"] - rt["unknown_s"]) / rt["T_e"] if rt and rt.get("T_e") else None


def shoal_ratios(rt, lines, min_coverage=MIN_COVERAGE):
    """CI and lease share of eligible role time, AAR, RWF and coverage. Below `min_coverage` (the
    journal could not classify most of the eligible time: an observer down, a source missing) every
    ratio but coverage is None, not a number that reads as idle or as fast."""
    cov = _coverage(rt)
    none = {"ci_lease_share": None, "lease_share": None, "lines_per_role_ci_wait_hour": None, "AAR": None, "RWF": None,
            "coverage": _round(cov)}
    if cov is None or cov < min_coverage or cov <= 0:
        return none
    ci_s = sum(rt["waits"].get(e, 0) for e in CI_WAITS + LEASE_WAITS)
    return {"ci_lease_share": _round(ci_s / rt["T_e"]),
            "lease_share": _round(sum(rt["waits"].get(e, 0) for e in LEASE_WAITS) / rt["T_e"]),
            "lines_per_role_ci_wait_hour": _ratio(lines, ci_s, 3600) if lines is not None else None,
            "AAR": _round((rt["work_s"] + rt["wait_s"]) / rt["delta_s"]) if rt["delta_s"] else None,
            "RWF": _round(rt["work_s"] / rt["T_e"]),
            "coverage": _round(cov)}


# ---- the rows file -----------------------------------------------------------------

def row_key(r):
    return (r.get("kind"), r.get("repo") or r.get("shoal"), r.get("day"))


def load_rows(path):
    out = []
    try:
        with open(path) as fh:
            for line in fh:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if isinstance(r, dict) and r.get("day"):
                    out.append(r)
    except OSError:
        pass
    last = {row_key(r): r for r in out}  # a hand-appended duplicate: the last one wins
    return sorted(last.values(), key=lambda r: (r["day"], r["kind"], row_key(r)[1]))


def save_rows(path, rows, new):
    """Replace each new row's (kind, name, day) and rewrite the file atomically: a re-run of a day
    replaces that day's row and nothing else."""
    by = {row_key(r): r for r in rows}
    for r in new:
        by[row_key(r)] = r
    out = sorted(by.values(), key=lambda r: (r["day"], r["kind"], row_key(r)[1]))
    _write(path, "".join(json.dumps(r, sort_keys=True) + "\n" for r in out))
    return out


# ---- the snapshot ------------------------------------------------------------------

def snapshot(days, repos_cfg, s, gh, shoals=(), now=None):
    """Rows for every configured repository and shoal over `days`, written into `s["out"]`. `shoals` is
    [(sid, journal, repos)]. A day whose GitHub read fails keeps a row already stored for it with its
    merges counted. Returns (every row, the rows written now, [(repo, day)] GitHub could not read)."""
    rows = load_rows(s["out"])
    have = {row_key(r): r for r in rows}
    root = ci_wait.store_dir(s.get("store") or None)
    new, failed = [], []
    for cfg in repos_cfg:
        repo = cfg["repo"]
        records = list(ci_wait.load(root, repo).values())
        merged = merged_by_day(gh, repo, days, s.get("cache_dir"))
        for day in days:
            prs = merged.get(day)
            if prs is None:
                failed.append((repo, day))
                old = have.get(("repo", repo, day))
                if old and (old.get("sources") or {}).get("github") == "ok":
                    continue  # GitHub is down: the stored row is better than a row without merges
            new.append(repo_day(repo, day, records, prs or [], cfg, s["exclude"], s["buckets"],
                                gh_ok=prs is not None, now=now))
    for sid, journal, repos in shoals:
        obs, mem = load_days(journal, days) if journal else ({}, {})
        for day in days:
            rr = {row_key(r): r for r in rows if r["kind"] == "repo" and r["day"] == day}
            rr.update({row_key(r): r for r in new if r["kind"] == "repo" and r["day"] == day})
            rt = role_time_day(obs[day], mem, day) if journal else None
            new.append(shoal_day(sid, day, rt, repos, list(rr.values()), now=now,
                                 min_coverage=s.get("min_coverage", MIN_COVERAGE)))
    return save_rows(s["out"], rows, new), new, failed


# ---- windows -----------------------------------------------------------------------

CI_SUMS = ("job_minutes", "ci_wait_s", "head_job_minutes", "rerun_minutes", "local_lease_wait_s", "local_run_s",
           "github_queue_s", "github_run_s", "gate_run_s", "direct_held_s", "pr_heads", "github_jobs", "gate_builds",
           "local_jobs")

def reverted_numbers(rows):
    """{repo: {PR numbers some merged PR says it reverts}} over every stored row."""
    out = {}
    for r in rows:
        if r.get("kind") == "repo":
            out.setdefault(r["repo"], set()).update(int(n) for n in (r.get("guardrails") or {}).get("reverted_targets") or [])
    return out


def rollup(rows, days, all_rows=None, bounds=DEFAULT_BUCKETS, min_coverage=MIN_COVERAGE):
    """Per repository and per shoal over `days`: ratios of sums (never a mean of daily ratios),
    medians per size bucket over every PR merged in the window, and the guardrails. A PR counts as
    reverted when it is a revert or a merged PR anywhere in `all_rows` says it reverts it. A ratio
    pairs lines with the cost of the same days only: a day whose merges GitHub could not give adds
    its CI to `ci` but not to the ratios, and a shoal day whose role-time coverage is under
    `min_coverage` is left out of its role time and ratios (counted in `low_coverage_days`)."""
    days = set(days)
    rev = reverted_numbers(all_rows if all_rows is not None else rows)
    out = {"repos": {}, "shoals": {}}
    for r in rows:
        if r.get("day") not in days:
            continue
        if r["kind"] == "repo":
            a = out["repos"].setdefault(r["repo"], {"days": 0, "github_days": 0, "source_days": 0, "source_heads": 0,
                                                    "prs": [], "ci": {}, "ci_paired": {}, "guard": {}, "by_source": {}})
            a["days"] += 1
            if "by_source" in r:  # rows written before sources were kept have none
                a["source_days"] += 1
                a["source_heads"] += r["ci"].get("pr_heads") or 0
                for k, v in r["by_source"].items():
                    a["by_source"][k] = a["by_source"].get(k, 0.0) + v
            sums = [a["ci"]]
            if r.get("merged") is not None:
                a["github_days"] += 1
                a["prs"] += r.get("prs") or []
                sums.append(a["ci_paired"])
            for c in sums:
                for k in CI_SUMS:
                    c[k] = c.get(k, 0) + (r["ci"].get(k) or 0)
                for k, sub in (("push_to_complete_s", "sum"), ("gate_wait_s", "sum")):
                    c[k] = c.get(k, 0) + ((r["ci"].get(k) or {}).get(sub) or 0)
            g = r.get("guardrails") or {}
            for k in ("failed_gates", "gate_approvals_failed", "reverts_merged", "unleased_cpu_jobs", "unleased_cpu_observed"):
                a["guard"][k] = a["guard"].get(k, 0) + (g.get(k) or 0)
        elif r["kind"] == "shoal":
            a = out["shoals"].setdefault(r["shoal"], {"days": 0, "low_coverage_days": 0, "rt": None, "lines": None,
                                                      "lines_paired": None})
            a["days"] += 1
            rt = r.get("role_time")
            if rt and (_coverage(rt) or 0) < min_coverage:
                a["low_coverage_days"] += 1
                rt = None
            if rt and r.get("merged_lines") is not None:
                a["lines_paired"] = (a["lines_paired"] or 0) + r["merged_lines"]
            if rt:
                t = a["rt"] or {"delta_s": 0, "T_e": 0, "work_s": 0, "wait_s": 0, "unknown_s": 0, "waits": {}, "days": 0}
                t["days"] += 1
                for k in ("delta_s", "T_e", "work_s", "wait_s", "unknown_s"):
                    t[k] += rt.get(k) or 0
                for e, v in (rt.get("waits") or {}).items():
                    t["waits"][e] = t["waits"].get(e, 0) + v
                a["rt"] = t
            if r.get("merged_lines") is not None:
                a["lines"] = (a["lines"] or 0) + r["merged_lines"]
    for repo, a in out["repos"].items():
        prs, ci, guard = a.pop("prs"), a["ci"], a["guard"]
        lines = sum(p["lines"] for p in prs) if a["github_days"] else None
        a["merged"] = {"prs": len(prs), "lines": lines,
                       "buckets": {b: sum(1 for p in prs if p["bucket"] == b) for b in bucket_names(bounds)}}
        a["ratios"] = ratios({"merged": a["merged"] if lines is not None else None, "ci": a.pop("ci_paired")})
        a["ratios"]["job_minutes_per_head"] = round(ci["head_job_minutes"] / ci["pr_heads"], 1) if ci["pr_heads"] else None
        a["by_bucket"] = by_bucket(prs, bounds)
        guard["rerun_share"] = round(ci["rerun_minutes"] / ci["head_job_minutes"], 3) if ci["head_job_minutes"] else None
        guard["reverted_prs"] = sum(1 for p in prs if p.get("revert") or p["n"] in rev.get(repo, set()))
    for sid, a in out["shoals"].items():
        a["ratios"] = shoal_ratios(a["rt"], a.pop("lines_paired"), min_coverage)
    return out


# ---- Prometheus ---------------------------------------------------------------------

WINDOWS = (("1d", 1), ("7d", 7))


def render_prometheus(rows, now=None, bounds=DEFAULT_BUCKETS, min_coverage=MIN_COVERAGE):
    """Gauges per repository and shoal over the last stored day and the last seven days (windows end
    at the latest day in the file, not at the scrape). Labels are repository, shoal, window and size
    bucket: never a PR number or SHA."""
    L = []
    days = sorted({r["day"] for r in rows})
    if not days:
        return "\n".join(["# HELP shoal_efficiency_last_success_timestamp_seconds Last snapshot run.",
                          "# TYPE shoal_efficiency_last_success_timestamp_seconds gauge",
                          f"shoal_efficiency_last_success_timestamp_seconds {time.time() if now is None else now:.3f}"]) + "\n"
    end = days[-1]
    ups = {w: rollup(rows, last_days(n, end), rows, bounds, min_coverage) for w, n in WINDOWS}
    esc = ci_wait._esc

    def metric(name, help_):
        L.extend([f"# HELP {name} {help_}", f"# TYPE {name} gauge"])

    def emit(name, label, pick, help_, scope="repos"):
        metric(name, help_)
        for w, up in ups.items():
            for key, a in sorted(up[scope].items()):
                v = pick(a)
                if v is not None:
                    L.append(f'{name}{{{label}="{esc(key)}",window="{w}"}} {v}')

    R = "repos"
    emit("shoal_efficiency_merged_lines", "repo", lambda a: a["merged"]["lines"],
         "Counted changed lines (additions plus deletions, exclusions applied) of the PRs merged in the window.")
    emit("shoal_efficiency_merged_prs", "repo", lambda a: a["merged"]["prs"] if a["merged"]["lines"] is not None else None,
         "PRs merged in the window.")
    emit("shoal_efficiency_lines_per_ci_wait_hour", "repo", lambda a: a["ratios"]["lines_per_ci_wait_hour"],
         "Merged lines per hour of CI wait (push to CI complete per PR head plus local CI lease wait and run).")
    emit("shoal_efficiency_lines_per_job_minute", "repo", lambda a: a["ratios"]["lines_per_job_minute"],
         "Merged lines per CI job-minute (GitHub, gate and local CI run time).")
    emit("shoal_efficiency_job_minutes_per_merged_pr", "repo", lambda a: a["ratios"]["job_minutes_per_merged_pr"],
         "CI job-minutes in the window per PR merged in it.")
    emit("shoal_efficiency_ci_job_minutes", "repo", lambda a: round(a["ci"]["job_minutes"], 2), "CI job-minutes in the window.")
    emit("shoal_efficiency_ci_wait_seconds", "repo", lambda a: round(a["ci"]["ci_wait_s"], 1), "CI wait seconds in the window.")
    emit("shoal_efficiency_rerun_share", "repo", lambda a: a["guard"]["rerun_share"],
         "Guardrail: share of PR head CI minutes spent on attempts after the first.")
    emit("shoal_efficiency_failed_gates", "repo", lambda a: a["guard"]["failed_gates"],
         "Guardrail: gate builds that failed in the window.")
    emit("shoal_efficiency_reverted_prs", "repo", lambda a: a["guard"]["reverted_prs"],
         "Guardrail: PRs merged in the window that are reverts or were reverted later.")
    emit("shoal_efficiency_unleased_cpu_jobs", "repo", lambda a: a["guard"]["unleased_cpu_jobs"],
         "Guardrail: local CI jobs that did sustained CPU work without a lease.")
    for name, k, help_ in (("shoal_efficiency_merged_prs_by_size", "prs", "PRs merged in the window by size bucket."),
                           ("shoal_efficiency_bucket_lines_per_job_minute", "median_lines_per_job_minute",
                            "Median over the bucket's merged PRs of each PR's lines per CI job-minute."),
                           ("shoal_efficiency_bucket_lines_per_ci_wait_hour", "median_lines_per_ci_wait_hour",
                            "Median over the bucket's merged PRs of each PR's lines per CI wait hour.")):
        metric(name, help_)
        for w, up in ups.items():
            for repo, a in sorted(up[R].items()):
                if a["merged"]["lines"] is None:
                    continue
                for b, v in a["by_bucket"].items():
                    if v.get(k) is not None:
                        L.append(f'{name}{{repo="{esc(repo)}",window="{w}",bucket="{b}"}} {v[k]}')
    S = "shoals"
    emit("shoal_efficiency_ci_lease_share", "shoal", lambda a: a["ratios"]["ci_lease_share"],
         "Share of eligible role time spent waiting on CI or a lease.", S)
    emit("shoal_efficiency_aar", "shoal", lambda a: a["ratios"]["AAR"], "Average Active Roles over the window.", S)
    emit("shoal_efficiency_role_work_fraction", "shoal", lambda a: a["ratios"]["RWF"], "Role Work Fraction over the window.", S)
    emit("shoal_efficiency_role_time_coverage", "shoal", lambda a: a["ratios"]["coverage"],
         "Share of eligible role time the journal classified (not unknown) over the window.", S)
    emit("shoal_efficiency_lines_per_role_ci_wait_hour", "shoal", lambda a: a["ratios"]["lines_per_role_ci_wait_hour"],
         "Merged lines of the shoal's repositories per hour of role CI and lease wait.", S)
    metric("shoal_efficiency_day_timestamp_seconds", "End of the last UTC day the windows include.")
    L.append(f"shoal_efficiency_day_timestamp_seconds {day_bounds(end)[1]:.0f}")
    metric("shoal_efficiency_last_success_timestamp_seconds", "Last snapshot run.")
    L.append(f"shoal_efficiency_last_success_timestamp_seconds {time.time() if now is None else now:.3f}")
    return "\n".join(L) + "\n"


# ---- commands ----------------------------------------------------------------------

def _spec_repos(path):
    import roles
    spec = roles.ShoalSpec.load(path)
    return spec, ci_wait.feature(spec).get("repos") or []


def _shoals(paths, role_time_dir):
    """[(sid, journal, repos)] for each role-time spec: its journal is `<role_time_dir>/<sid>/
    role-time-observations.jsonl` (the observer's `--state-dir` layout; without one, the shoal's
    state directory, the observer's default), its repositories the spec's own `repos` (the ones it
    works), else its `features.ci_wait` ones. A missing journal is said, and the row has no role time."""
    import roles
    out = []
    for p in paths:
        spec = roles.ShoalSpec.load(p)
        repos = set(spec.repos or []) or {c["repo"] for c in ci_wait.feature(spec).get("repos") or []}
        base = os.path.join(role_time_dir, spec.sid) if role_time_dir else roles.state_dir(spec.sid)
        journal = os.path.join(base, "role-time-observations.jsonl")
        if not role_time.observation_files(journal):
            print(f"efficiency: no role-time journal for {spec.sid} at {journal}", file=sys.stderr)
            journal = ""
        out.append((spec.sid, journal, repos))
    return out


def _days(a):
    yesterday = day_of(time.time() - DAY_S)
    end = a.day or yesterday
    try:
        day_bounds(end)
    except ValueError:
        raise SystemExit("efficiency: --day must be YYYY-MM-DD")
    return last_days(max(1, int(a.backfill or 1)), end)


def cmd_snapshot(a):
    spec, repos_cfg = _spec_repos(a.spec)
    if a.repo:
        repos_cfg = [c for c in repos_cfg if c["repo"] in a.repo]
    if not repos_cfg:
        print("efficiency: no repositories (features.ci_wait.repos in the spec, or --repo)", file=sys.stderr)
        return 2
    s = settings(out=a.out, store=a.store, role_time_dir=a.role_time_dir, cache_dir=a.cache_dir)
    shoals = _shoals(a.role_time_spec or [], s["role_time_dir"])
    rows, new, failed = snapshot(_days(a), repos_cfg, s, GitHubReader(), shoals)
    if a.prom_out:
        _write(a.prom_out, render_prometheus(rows, bounds=s["buckets"], min_coverage=s["min_coverage"]))
    if a.json:
        print(json.dumps(new, indent=1))
    else:
        for r in new:
            print(line(r))
    for repo, day in failed:
        print(f"efficiency: GitHub unreadable for {repo} on {day}: merges not counted"
              + (" (the stored row is kept)" if not any(row_key(r) == ("repo", repo, day) for r in new) else ""),
              file=sys.stderr)
    return 1 if failed else 0


def _num(v, fmt="{:.1f}"):
    return "-" if v is None else fmt.format(v)


def line(r):
    if r["kind"] == "shoal":
        x, rt = r["ratios"], r.get("role_time") or {}
        un = r.get("repos_uncounted") or []
        return (f"{r['day']} {r['shoal']}: CI and lease share {_num(x['ci_lease_share'], '{:.3f}')}, AAR {_num(x['AAR'], '{:.2f}')}, "
                f"RWF {_num(x['RWF'], '{:.3f}')}, coverage {_num(rt.get('coverage'), '{:.3f}')}, "
                f"{_num(r.get('merged_lines'), '{}')} merged lines, {_num(x['lines_per_role_ci_wait_hour'])} per role CI wait hour"
                + (f" [not counted: {', '.join(un)}]" if un else "") + ("" if rt else " [no role-time journal]"))
    m, x, g, ci = r.get("merged") or {}, r["ratios"], r["guardrails"], r["ci"]
    return (f"{r['day']} {r['repo']}: {m.get('prs', '-')} merged PRs, {m.get('lines', '-')} lines "
            f"({' '.join(f'{k}:{v}' for k, v in (m.get('buckets') or {}).items())}); {ci['job_minutes']:.0f} job-min, "
            f"CI wait {ci['ci_wait_s'] / 3600:.1f} h; {_num(x['lines_per_ci_wait_hour'])} lines per CI wait hour, "
            f"{_num(x['lines_per_job_minute'], '{:.2f}')} per job-minute; rerun share {_num(g['rerun_share'], '{:.3f}')}, "
            f"{g['failed_gates']} failed gates, {g['reverts_merged']} reverts, {g['unleased_cpu_jobs']} unleased CPU jobs"
            + ("" if (r.get("sources") or {}).get("github") == "ok" else " [GitHub unreadable: no merges counted]"))


def add_parser(sub):
    p = sub.add_parser("efficiency", help="CI efficiency: merged work per unit of CI and wait overhead, and its weekly review")
    s = p.add_subparsers(dest="efficiency_cmd", required=True)
    o = s.add_parser("snapshot", help="compute one row per repository (and shoal) per UTC day into the JSONL")
    o.add_argument("--spec", required=True, help="the spec carrying features.ci_wait (its repositories)")
    o.add_argument("--repo", action="append", default=[], help="only this <owner>/<name> (repeatable)")
    o.add_argument("--role-time-spec", action="append", default=[],
                   help="a shoal whose role time is summarized per day (repeatable)")
    o.add_argument("--day", default="", help="the UTC day YYYY-MM-DD (default: yesterday)")
    o.add_argument("--backfill", type=int, default=1, help="the N days ending with --day (default 1)")
    o.add_argument("--out", default="", help="snapshot JSONL (default: [efficiency] out, else <data_root>/_efficiency/)")
    o.add_argument("--store", default="", help="ci-wait store (default: <data_root>/_ci_wait)")
    o.add_argument("--role-time-dir", default="", help="the role-time observer's --state-dir")
    o.add_argument("--cache-dir", default="", help="merged PR file cache (default: beside the default out)")
    o.add_argument("--prom-out", default="", help="write Prometheus text here (atomically)")
    o.add_argument("--json", action="store_true", help="print the new rows as JSON")
    o.set_defaults(func=cmd_snapshot)
    import efficiency_review
    efficiency_review.add_parsers(s)
    return s
