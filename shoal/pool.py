#!/usr/bin/env python3
"""CI pool placement: the pure model and planner behind `job run --place` and `shoal pool`
(architecture doc §6, "Pool placement").

Nothing here does I/O: host probes, job records and lease history arrive as plain dicts, so every rule
is unit-tested with synthetic N-host fixtures (tests/test_pool.py). `pool_cli.py` gathers the inputs
(`shoal pool probe --json` on each host) and acts on the advice.

- **Host capacity** comes from one probe per host: lease keys (each GPU device with vendor, VRAM and
  architecture tag, the whole-host `gpu` key, `cpu-heavy`), cores, available memory, named CI
  environments, the worktree root and the probe's time. A failed or stale probe makes the host
  ineligible, never an error.
- **Job demand**: GPU count, vendor (`any`, `nvidia`, `amd`), minimum VRAM, cpu-heavy, memory, a CI
  environment name, `repo@sha` for staging, and a pinned host.
- **Durations** are learned per (repo, normalized job name, host class) as p50 and p90, shrunk toward
  the `--est` prior. A running job's remaining time is `max(p50 - elapsed, tail)`; the tail grows once
  elapsed passes p90, so an overrunning holder pushes every prediction behind it later.
- **Occupancy** is the lease helper's FIFO queue: tickets with all-or-nothing wants, a later ticket never
  passing an earlier one it overlaps. Non-SHOAL holders and waiters (CI agents) are immovable occupancy.
- **Planner**: list scheduling over pending jobs in (priority, submit time) order, each to the
  compatible host with the earliest predicted finish (queue ahead plus staging), keeping the job's home
  on near-ties. Objective: the sum of predicted waits, plus staging per transfer.
- **Transfer advice** for a queued job (never a running one): a compatible, fresh host whose predicted
  start, staging included, beats the current host's by more than the margin, while moves < max_moves.
"""
from __future__ import annotations

import datetime
import math
import re

ALL = "all"
VENDORS = ("any", "nvidia", "amd")
DEFAULTS = {"margin_s": 180.0, "staging_s": 120.0, "max_moves": 2, "max_age_s": 120.0,
            "interval_s": 120.0, "default_est_s": 1800.0, "ci_budget_min": 90.0}
CLASSES = ("ci", "research")
FUTURE_SKEW_S = 30.0       # a probe dated further ahead than this is as untrustworthy as a stale one
PRIOR_WEIGHT = 2.0         # the --est prior counts as this many observed runs
P90_OVER_P50 = 1.5         # p90 assumed from a lone p50 (a prior or the default)
TAIL_FLOOR_S = 60.0        # a running job past its p50 is never predicted to end sooner than this
TAIL_FLOOR_FRAC = 0.1      # ... or than this fraction of its p50
OVERRUN_GROWTH = 0.5       # predicted remaining time added per second elapsed past p90
CPU_ONLY_ENV = {"CUDA_VISIBLE_DEVICES": "", "ROCR_VISIBLE_DEVICES": "", "HIP_VISIBLE_DEVICES": ""}


def feature(spec) -> dict:
    """features.placement: false/absent (off), true (on, defaults), or {"enabled": true, "margin_s",
    "staging_s", "max_moves", "interval_s", "max_age_s", "ci_budget_min"}. Only queue transfers need it
    on; `job run --place` and the CI runtime budget (`ci_budget_min`, 0 = none) work without it."""
    v = (getattr(spec, "features", {}) or {}).get("placement")
    out = dict(DEFAULTS, enabled=bool(v))
    if isinstance(v, dict):
        out["enabled"] = bool(v.get("enabled", True))
        for k in DEFAULTS:
            if isinstance(v.get(k), (int, float)) and v[k] >= 0:
                out[k] = v[k]
    return out


def options(opts: dict | None = None) -> dict:
    out = dict(DEFAULTS)
    out.update({k: v for k, v in (opts or {}).items() if k in DEFAULTS and v is not None})
    return out


# ---- names ------------------------------------------------------------------------------------------

def normalize_name(name: str) -> str:
    """A job name with its run-specific parts folded, so runs of one kind share an estimate:
    issue numbers, dates and hashes become `#` (`suite-891-ab` and `suite-902-ab` are one kind)."""
    s = str(name or "").strip().lower()
    s = re.sub(r"\b[0-9a-f]{7,40}\b", lambda m: "#" if re.search(r"\d", m.group(0)) else m.group(0), s)
    s = re.sub(r"\d+", "#", s)
    return re.sub(r"#(?:[._-]?#)+", "#", s)


def repo_identity(url: str) -> str:
    """`owner/name` from a remote URL (ssh or https), else the last path component (a local path)."""
    s = str(url or "").strip().rstrip("/")
    s = s[:-4] if s.endswith(".git") else s
    scp = re.match(r"^[^/@]+@[^:/]+:", s)
    remote = "://" in s or bool(scp)
    s = re.sub(r"^[a-z][a-z0-9+.-]*://[^/]+/", "", s)   # https://host/owner/name
    s = re.sub(r"^[^/@]+@[^:/]+:", "", s)               # git@host:owner/name
    parts = [p for p in s.split("/") if p]
    if not parts:
        return ""
    return "/".join(parts[-2:]) if remote and len(parts) >= 2 else parts[-1]


# ---- demand -----------------------------------------------------------------------------------------

def parse_needs(text: str) -> dict:
    """`cpu` or `gpu[:N][:vendor][:vramGiB]` -> {gpus, vendor, min_vram_gib}. The vendor is "" when not
    given (the caller decides the default); N is 1 when omitted."""
    s = str(text or "cpu").strip().lower()
    parts = s.split(":")
    if parts[0] == "cpu":
        if len(parts) > 1:
            raise ValueError(f"--needs cpu takes no qualifiers: {text!r}")
        return {"gpus": 0, "vendor": "", "min_vram_gib": 0.0}
    if parts[0] != "gpu":
        raise ValueError(f"--needs must be cpu or gpu[:N][:vendor][:vramGiB], got {text!r}")
    gpus, vendor, vram, nums = 1, "", 0.0, []
    for tok in parts[1:]:
        if not tok:
            continue
        if tok in VENDORS:
            vendor = tok
            continue
        t = re.sub(r"(gib|gb|g)$", "", tok)
        try:
            val = float(t)
        except ValueError:
            raise ValueError(f"--needs: unknown qualifier {tok!r} in {text!r}") from None
        if t != tok:  # an explicit unit is always the VRAM
            vram = val
        else:
            nums.append(val)
    if nums:
        gpus = int(nums[0])
        if gpus != nums[0] or gpus < 1:
            raise ValueError(f"--needs: GPU count must be a positive integer in {text!r}")
    if len(nums) > 1:
        vram = nums[1]
    if len(nums) > 2 or vram < 0 or not math.isfinite(vram):
        raise ValueError(f"--needs: too many or invalid numbers in {text!r}")
    return {"gpus": gpus, "vendor": vendor, "min_vram_gib": vram}


def demand(needs: str = "cpu", vendor_default: str = "any", cpu_heavy: bool = False, memory_gib: float = 0,
           env: str = "", repo: str = "", sha: str = "", pin: str = "", remote_block: str = "",
           job_class: str = "ci") -> dict:
    """One job's demand. `remote_block` is a reason no host but home can run it (no pushed SHA, a dirty
    tree, nothing to stage). A `ci` job never uses resources a research reservation holds."""
    d = parse_needs(needs)
    if d["gpus"] and not d["vendor"]:
        d["vendor"] = vendor_default or "any"
    d.update(cpu_heavy=bool(cpu_heavy), memory_gib=float(memory_gib or 0), env=str(env or ""),
             repo=str(repo or ""), sha=str(sha or ""), pin=str(pin or ""), remote_block=str(remote_block or ""),
             job_class=str(job_class or "ci"))
    return d


def lease_args(dem: dict) -> tuple[list[str], str]:
    """The lease-helper arguments for a demand, and its scope label. CPU-only work takes only
    `--lease cpu-heavy`, and only when cpu-heavy was asked for: a bare `--cpu-heavy` would also take the
    whole-host `gpu` key. A GPU request takes `--gpus 1 [--min-vram G]` (any count above one is the
    whole host, `--gpus all`), plus `--cpu-heavy` when asked."""
    g, cpu = int(dem.get("gpus") or 0), bool(dem.get("cpu_heavy"))
    if not g:
        return (["--lease", "cpu-heavy"], "cpu-heavy") if cpu else ([], "none")
    args = ["--gpus", "1" if g == 1 else ALL]
    scope = "gpu:1" if g == 1 else "gpu:all"
    vram = float(dem.get("min_vram_gib") or 0)
    if g == 1 and vram:
        v = f"{vram:g}"
        args += ["--min-vram", v]
        scope += f" min-vram {v}GiB"
    if cpu:
        args.append("--cpu-heavy")
        scope += "+cpu-heavy"
    return args, scope


def device_env(dem: dict) -> dict:
    """Device selectors a job gets beyond its lease: CPU-only work sees no GPU of either vendor."""
    return dict(CPU_ONLY_ENV) if not int(dem.get("gpus") or 0) else {}


# ---- durations --------------------------------------------------------------------------------------

def percentile(values, q: float) -> float:
    """Nearest-rank percentile of a non-empty sequence."""
    xs = sorted(float(v) for v in values)
    k = max(0, min(len(xs) - 1, math.ceil(q / 100.0 * len(xs)) - 1))
    return xs[k]


def _blend(samples, prior_s, default_s) -> dict:
    samples = [float(x) for x in samples if isinstance(x, (int, float)) and x >= 0]
    n = len(samples)
    if not n:
        if prior_s:
            return {"p50": float(prior_s), "p90": float(prior_s) * P90_OVER_P50, "n": 0, "source": "prior"}
        return {"p50": float(default_s), "p90": float(default_s) * P90_OVER_P50, "n": 0, "source": "default"}
    e50, e90 = percentile(samples, 50), percentile(samples, 90)
    if not prior_s:
        return {"p50": e50, "p90": max(e50, e90), "n": n, "source": "learned"}
    w = PRIOR_WEIGHT
    p50 = (w * prior_s + n * e50) / (w + n)
    p90 = max(p50, (w * prior_s * P90_OVER_P50 + n * e90) / (w + n))
    return {"p50": p50, "p90": p90, "n": n, "source": "blended"}


class Estimator:
    """Duration estimates. `history`: job records ({repo, name, host, run_s}); `holders`: per host,
    {normalized holder name: [durations s]} from that host's observed lease episodes."""

    def __init__(self, history=(), holders: dict | None = None, default_s: float = DEFAULTS["default_est_s"]):
        self.exact: dict = {}
        self.anyhost: dict = {}
        for r in history or ():
            if not isinstance(r, dict) or not isinstance(r.get("run_s"), (int, float)):
                continue
            key = (str(r.get("repo") or ""), normalize_name(r.get("name") or ""))
            self.exact.setdefault(key + (str(r.get("host") or ""),), []).append(float(r["run_s"]))
            self.anyhost.setdefault(key, []).append(float(r["run_s"]))
        self.holders = {h: {normalize_name(k): list(v) for k, v in (d or {}).items()} for h, d in (holders or {}).items()}
        self.default_s = float(default_s)

    def job(self, repo: str, name: str, host_class: str, prior_s: float | None = None) -> dict:
        key = (str(repo or ""), normalize_name(name))
        samples = self.exact.get(key + (str(host_class or ""),)) or self.anyhost.get(key) or []
        return _blend(samples, prior_s, self.default_s)

    def holder(self, host: str, name: str) -> dict:
        return _blend((self.holders.get(host) or {}).get(normalize_name(name)) or [], None, self.default_s)


def remaining(est: dict, elapsed: float, ceiling_s: float | None = None) -> float:
    """Predicted seconds left for a job `elapsed` seconds in: max(p50 - elapsed, tail), the tail
    growing with time past p90. `ceiling_s` (a CI job's runtime budget) is a soft ceiling: within the
    budget the prediction ends by the budget plus the tail floor; past it the ordinary tail applies."""
    p50, p90 = float(est["p50"]), float(est["p90"])
    elapsed = max(0.0, float(elapsed))
    floor = max(TAIL_FLOOR_S, TAIL_FLOOR_FRAC * p50)
    tail = floor + max(0.0, elapsed - p90) * OVERRUN_GROWTH
    left = max(p50 - elapsed, tail)
    if ceiling_s and elapsed < ceiling_s:
        left = min(left, ceiling_s - elapsed + floor)
    return left


def _ceiling(job: dict | None, o: dict) -> float | None:
    """A CI job's runtime budget in seconds (None for research work, or with no budget)."""
    if not job or str(job.get("class") or "ci") != "ci":
        return None
    b = float(o.get("ci_budget_min") or 0) * 60
    return b or None


# ---- host capacity ----------------------------------------------------------------------------------

def _epoch(value) -> float | None:
    try:
        d = datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return d.timestamp() if d.tzinfo is not None else None


def _merge_devices(a, b):
    return ALL if ALL in (a, b) else set(a) | set(b)


def occupancy(lease: dict) -> tuple[list[dict], list[dict]]:
    """(holders, tickets) from a lease provider's `--status --json` answer. A held exclusive `gpu`
    reserves every device; a shared `gpu` gate is not a claim (its device keys are); `cpu-heavy` is the
    host CPU/memory slot. Rows of one holder (same name and pid) merge. Queue rows merge per ticket: a
    `gpu` want of device candidates needs one of them, `all` (or no want) the whole host."""
    holders: dict = {}
    for row in (lease or {}).get("leases") or []:
        if not isinstance(row, dict) or not row.get("held"):
            continue
        key = str(row.get("key") or "")
        if key == "gpu":
            if row.get("mode") == "shared":
                continue
            dev, cpu = ALL, False
        elif key.startswith("gpu@"):
            dev, cpu = {key[4:]}, False
        elif key == "cpu-heavy":
            dev, cpu = set(), True
        else:
            continue
        name = str(row.get("holder") or "unattributed")
        h = holders.setdefault((name, row.get("pid")), {"name": name, "pid": row.get("pid"),
                                                         "since": row.get("since"), "devices": set(), "cpu": False,
                                                         "keys": []})
        h["devices"] = _merge_devices(h["devices"], dev)
        h["cpu"] = h["cpu"] or cpu
        h["keys"].append(key)
    tickets: dict = {}
    for i, row in enumerate((lease or {}).get("queue") or []):
        if not isinstance(row, dict):
            continue
        tid = str(row.get("ticket") or f"~{i:06d}")
        t = tickets.setdefault(tid, {"ticket": tid, "name": str(row.get("waiter") or "unattributed"),
                                     "devices": set(), "one": False, "cpu": False})
        key = str(row.get("key") or "")
        if key == "cpu-heavy":
            t["cpu"] = True
        elif key == "gpu":
            want = [str(w) for w in (row.get("want") or [ALL]) if str(w) != "cpu-heavy"] or [ALL]
            if ALL in want:
                t["devices"], t["one"] = ALL, False
            elif t["devices"] != ALL:
                t["devices"], t["one"] = list(dict.fromkeys(list(t["devices"]) + want)), True
    return list(holders.values()), sorted(tickets.values(), key=lambda t: _ticket_key(t["ticket"]))


def _ticket_key(tid: str):
    return (0, int(tid)) if str(tid).isdigit() else (1, str(tid))


def host_capacity(probe: dict, now: float, max_age_s: float = DEFAULTS["max_age_s"], name: str = "") -> dict:
    """A probe as the planner's host. `name` (the route name) wins over the probe's own; `fetched_ts`
    (when the home host received it, its clock) is used for freshness and for holders' elapsed time,
    so a skewed remote clock does not age a probe."""
    p = probe if isinstance(probe, dict) else {}
    reasons = []
    if p.get("pool") is False:
        reasons.append("not in the pool (pool = false on that host)")
    elif p.get("error"):
        err = " ".join(str(p["error"]).split())  # one line, whatever the probe printed
        reasons.append(f"probe failed: {err[:160]}")
    ts = p.get("ts")
    seen = p.get("fetched_ts", ts)
    if not reasons and (not isinstance(ts, (int, float)) or not isinstance(seen, (int, float))
                        or not -FUTURE_SKEW_S <= now - seen <= max_age_s):
        reasons.append("stale probe")
    lease = p.get("lease") or {}
    gpus = []
    for g in p.get("gpus") or []:
        if not isinstance(g, dict) or not g.get("uuid"):
            continue
        mib = g.get("vram_total_mib")
        gpus.append({"uuid": str(g["uuid"]), "vendor": str(g.get("vendor") or ""), "name": g.get("name"),
                     "vram_gib": float(mib) / 1024 if isinstance(mib, (int, float)) else None,
                     "arch": g.get("arch")})
    mem = (p.get("memory") or {}).get("available_bytes")
    holders, tickets = occupancy(lease)
    offset = (now - seen) if isinstance(seen, (int, float)) else 0.0
    for h in holders:
        since = _epoch(h.get("since"))
        h["elapsed_s"] = max(0.0, (ts - since) + offset) if since is not None and isinstance(ts, (int, float)) else 0.0
    known = [str(r.get("key")) for r in lease.get("leases") or [] if isinstance(r, dict) and r.get("key")]
    hname = str(name or p.get("host") or "?")
    resv = [r for r in p.get("reservations") or [] if isinstance(r, dict) and r.get("alive", True)]
    return {"name": hname, "probe_host": p.get("host"), "fresh": not reasons, "reasons": reasons,
            "lease_ok": bool(lease.get("ok")), "lease_cmd": str(p.get("lease_cmd") or ""), "keys": known,
            "gpus": gpus, "cores": p.get("cores"),
            "mem_avail_gib": float(mem) / 2 ** 30 if isinstance(mem, (int, float)) else None,
            "envs": p.get("ci_envs") if isinstance(p.get("ci_envs"), dict) else {},
            "worktree_root": str(p.get("worktree_root") or ""), "holders": holders, "tickets": tickets,
            "class": hname, "ts": ts, "reservations": resv,
            "reserved_devices": sorted({str(d.get("uuid")) for r in resv for d in r.get("devices") or []
                                        if isinstance(d, dict) and d.get("uuid")}),
            "reserved_cpu": any(r.get("cpu") for r in resv)}


def fitting_devices(host: dict, dem: dict, reserved_too: bool = False) -> list[str]:
    """Device ids that satisfy the demand's vendor and VRAM, smallest first (the helper's best fit). A
    CI job never sees a device a research reservation holds."""
    vendor, vram = dem.get("vendor") or "any", float(dem.get("min_vram_gib") or 0)
    held = set() if reserved_too or dem.get("job_class") == "research" else set(host.get("reserved_devices") or ())
    fit = [g for g in host["gpus"] if (vendor == "any" or g["vendor"] == vendor) and g["uuid"] not in held
           and (not vram or (g["vram_gib"] is not None and g["vram_gib"] + 1e-9 >= vram))]
    return [g["uuid"] for g in sorted(fit, key=lambda g: (g["vram_gib"] or 0, g["uuid"]))]


def compatible(host: dict, dem: dict, home: str) -> list[str]:
    """Why `host` cannot run `dem` ([] = it can)."""
    r = list(host.get("reasons") or [])
    if dem.get("pin") and host["name"] != dem["pin"]:
        r.append(f"pinned to {dem['pin']}")
    g = int(dem.get("gpus") or 0)
    if g or dem.get("cpu_heavy"):
        if not host["lease_cmd"]:
            r.append("no lease provider")
        elif not host["lease_ok"]:
            r.append("lease state unknown")
        else:
            if g and "gpu" not in host["keys"]:
                r.append("gpu lease key not observed")
            if dem.get("cpu_heavy") and "cpu-heavy" not in host["keys"]:
                r.append("cpu-heavy lease key not observed")
    ci = dem.get("job_class", "ci") != "research"
    if g == 1 and not fitting_devices(host, dem):
        r.append("every fitting GPU is reserved for research" if fitting_devices(host, dem, True) else
                 f"no {dem.get('vendor') or 'any'} GPU with {float(dem.get('min_vram_gib') or 0):g} GiB")
    if g > 1 and (len(host["gpus"]) < g or len(fitting_devices(host, dem, True)) < len(host["gpus"])):
        r.append(f"needs {g} fitting GPUs (the whole host)")
    elif g > 1 and ci and host.get("reserved_devices"):
        r.append("a research reservation holds part of the host (a whole-host request would wait for it)")
    if dem.get("cpu_heavy") and ci and host.get("reserved_cpu"):
        r.append("cpu-heavy is reserved for research")
    if dem.get("memory_gib") and (host["mem_avail_gib"] is None or host["mem_avail_gib"] < dem["memory_gib"]):
        r.append("insufficient or unknown memory")
    if dem.get("env"):
        e = host["envs"].get(dem["env"])
        if not isinstance(e, dict):
            r.append(f"env {dem['env']} missing")
        elif e.get("repos") and dem.get("repo") and dem["repo"] not in e["repos"]:
            r.append(f"env {dem['env']} does not serve {dem['repo']}")
    if host["name"] != home:
        if dem.get("remote_block"):
            r.append(dem["remote_block"])
        elif not (dem.get("repo") and dem.get("sha")):
            r.append("no repo@sha to stage")
        if not host["worktree_root"]:
            r.append("no worktree_root")
    return r


# ---- the queue model --------------------------------------------------------------------------------

def _devset(devices, all_devs):
    return set(all_devs) if devices == ALL else set(devices or ())


def simulate(host: dict, holders: list[dict], tickets: list[dict], now: float) -> tuple[dict, float]:
    """Predicted (start, end, devices) per ticket id, and the host's drain time. `holders`: {end,
    devices, cpu}. `tickets` in FIFO order: {id, dur, ready, devices (ALL, a set, or a best-fit-ordered
    list with one=True), cpu}. A ticket starts when everything it wants is free and no earlier ticket
    it overlaps is still waiting, taking everything at once (the lease helper's contract)."""
    devs = [g["uuid"] for g in host["gpus"]]
    free = {d: now for d in devs}
    cpu_free = now
    for h in holders:
        for d in _devset(h["devices"], devs) & set(free):
            free[d] = max(free[d], h["end"])
        if h.get("cpu"):
            cpu_free = max(cpu_free, h["end"])
    out, started = {}, []
    for t in tickets:
        want = _devset(t["devices"], devs)
        at = max(now, float(t.get("ready", now)))
        for wd, wc, s in started:
            if (wd & want) or (wc and t.get("cpu")):
                at = max(at, s)
        if t.get("cpu"):
            at = max(at, cpu_free)
        if t.get("one"):
            cands = [d for d in t["devices"] if d in free]
            take = set()
            if cands:
                best = min(cands, key=lambda d: (max(at, free[d]), cands.index(d)))
                at, take = max(at, free[best]), {best}
        else:
            take = want & set(free)
            at = max([at] + [free[d] for d in take])
        end = at + float(t["dur"])
        for d in take:
            free[d] = end
        if t.get("cpu"):
            cpu_free = end
        started.append((want, bool(t.get("cpu")), at))
        out[t["id"]] = (at, end, sorted(take))
    drain = max([now, cpu_free] + list(free.values()) + [e for _s, e, _d in out.values()])
    return out, drain


def _occupant_est(host, name, known, est, o):
    """(estimate, soft ceiling) for a holder or waiter: a known SHOAL job's from job history (a CI job
    capped by its budget), anything else's from its own lease history on that host."""
    j = known.get(name)
    if j is not None:
        return est.job(j.get("repo", ""), j.get("name", name), host["class"], j.get("prior_s")), _ceiling(j, o)
    return est.holder(host["name"], name), None


def _dur(e, ceiling):
    return min(e["p50"], ceiling) if ceiling else e["p50"]


def _fixed(host, now, est, known, movable, o=None):
    o = options(o)
    holders = []
    for h in host["holders"]:
        e, ceil = _occupant_est(host, h["name"], known, est, o)
        rem = remaining(e, h.get("elapsed_s", 0.0), ceil)
        holders.append(dict(h, end=now + rem, remaining_s=rem, external=h["name"] not in known, est=e))
    tickets = []
    for t in host["tickets"]:
        if t["name"] in movable:
            continue
        e, ceil = _occupant_est(host, t["name"], known, est, o)
        tickets.append(dict(t, id=f"ticket:{t['ticket']}", order=(0,) + _ticket_key(t["ticket"]), dur=_dur(e, ceil),
                            ready=now, external=t["name"] not in known))
    return holders, tickets


def _job_ticket(host, job, dem):
    g = int(dem.get("gpus") or 0)
    if g == 1:
        return {"devices": fitting_devices(host, dem), "one": True, "cpu": bool(dem.get("cpu_heavy"))}
    if g > 1:
        return {"devices": ALL, "one": False, "cpu": bool(dem.get("cpu_heavy"))}
    return {"devices": set(), "one": False, "cpu": bool(dem.get("cpu_heavy"))}


def _predict(host, job, planned, now, est, o, known, movable, seq):
    dem = job["demand"]
    e = est.job(job.get("repo", ""), job.get("name", job["id"]), host["class"], job.get("prior_s"))
    staged = host["name"] in (job.get("home"), job.get("current"))
    ready = now + (0.0 if staged else float(o["staging_s"]))
    if host["name"] == job.get("current") and job.get("ticket"):
        order = (0,) + _ticket_key(job["ticket"])
    elif host["name"] == job.get("current"):
        order = (0, 2, "")
    else:
        order = (2, seq)
    mine = dict(_job_ticket(host, job, dem), id=job["id"], dur=_dur(e, _ceiling(job, o)), ready=ready, order=order)
    holders, tickets = _fixed(host, now, est, known, movable, o)
    alltix = sorted(tickets + planned + [mine], key=lambda t: t["order"])
    res, _ = simulate(host, holders, alltix, now)
    start, end, devs = res[job["id"]]
    return {"start": start, "finish": end, "devices": devs, "est": e}, mine


def _job_dict(j):
    j = dict(j)
    j.setdefault("state", "new")
    j.setdefault("name", j["id"])
    j.setdefault("priority", 0)
    j.setdefault("submit_ts", 0.0)
    j.setdefault("moves", 0)
    j.setdefault("class", "ci")
    return j


def plan(hosts: list[dict], jobs: list[dict], now: float, est: Estimator | None = None, opts: dict | None = None,
         known: list[dict] = ()) -> dict:
    """List scheduling of the pending jobs (state new or queued) by earliest predicted finish.

    hosts: host_capacity() rows. jobs: {id (the job's unit, which is also its lease waiter name), name,
    repo, demand, home, current (queued: the host it waits on), ticket, state, submit_ts, priority,
    prior_s, moves}. known: other SHOAL jobs (running, or queued but not movable here) whose estimates
    should come from job history rather than holder history.

    Returns {"jobs": {id: result}, "hosts": {name: {drain, queue}}, "objective_s", "transfers"}. A new
    job's result names its host and reason; a queued job's carries `transfer` when advised."""
    o = options(opts)
    est = est or Estimator()
    jobs = [_job_dict(j) for j in jobs]
    hmap = {h["name"]: h for h in hosts}
    pending = [j for j in jobs if j["state"] in ("new", "queued")]
    for j in pending:  # a queued job keeps its place in its current host's FIFO (its waiter name is its id)
        cur = hmap.get(j.get("current") or "")
        if j["state"] == "queued" and not j.get("ticket") and cur is not None:
            j["ticket"] = next((t["ticket"] for t in cur["tickets"] if t["name"] == j["id"]), None)
    movable = {j["id"] for j in pending}
    knownd = {j["id"]: j for j in list(known) + jobs}
    planned: dict = {n: [] for n in hmap}
    results, objective, transfers = {}, 0.0, []
    for seq, j in enumerate(sorted(pending, key=lambda j: (j["priority"], j["submit_ts"], j["id"]))):
        rows = []
        for h in hmap.values():
            reasons = compatible(h, j["demand"], j["home"])
            row = {"host": h["name"], "eligible": not reasons, "reasons": reasons}
            if not reasons:
                pred, entry = _predict(h, j, planned[h["name"]], now, est, o, knownd, movable, seq)
                row.update(start=pred["start"], finish=pred["finish"], devices=pred["devices"],
                           p50_s=pred["est"]["p50"], est_source=pred["est"]["source"], _entry=entry)
            rows.append(row)
        ok = [r for r in rows if r["eligible"]]
        res = {"id": j["id"], "state": j["state"], "considered": rows, "transfer": None}
        if not ok:
            res.update(host=None, reason="no compatible host", start=None, finish=None, wait_s=None)
            results[j["id"]] = res
            continue
        best = min(ok, key=lambda r: (r["finish"], r["start"], r["host"]))
        if j["state"] == "queued":
            cur = next((r for r in ok if r["host"] == j.get("current")), None)
            chosen, reason = cur, "stays queued"
            others = [r for r in ok if r["host"] != j.get("current")]
            tgt = min(others, key=lambda r: (r["start"], r["finish"], r["host"])) if others else None
            here = hmap.get(j.get("current") or "")
            # a fresh current host that can no longer run the job (a research reservation took its
            # resources) is left whenever another host can; an unverifiable one is never left blind
            lost = cur is None and here is not None and here["fresh"]
            cur_start = float("inf") if lost else (cur["start"] if cur else None)
            if cur is None and not (lost and tgt is not None):
                chosen, reason = None, ("current host can no longer run it, and no other host can" if lost else
                                        "current host not verifiable (stale or failed probe)")
            elif tgt is not None and tgt["start"] + o["margin_s"] < cur_start:
                if j["moves"] >= int(o["max_moves"]):
                    reason = f"move cap reached ({j['moves']}/{int(o['max_moves'])})"
                else:
                    chosen, reason = tgt, ("current host can no longer run it" if lost else
                                           "earlier predicted start elsewhere")
                    res["transfer"] = {"from": j.get("current"), "to": tgt["host"], "from_start": cur_start,
                                       "to_start": tgt["start"], "gain_s": cur_start - tgt["start"],
                                       "staging_s": float(o["staging_s"]), "reason": reason}
                    transfers.append(dict(res["transfer"], id=j["id"]))
                    objective += float(o["staging_s"])
            if chosen is None:
                res.update(host=j.get("current"), reason=reason, start=None, finish=None, wait_s=None)
                results[j["id"]] = res
                continue
        else:
            anchor = next((r for r in ok if r["host"] == (j["demand"].get("pin") or j["home"])), None)
            if j["demand"].get("pin"):
                chosen, reason = best, "pinned"
            elif len(ok) == 1:
                chosen, reason = best, "only compatible host"
            elif anchor is not None and anchor is not best and anchor["finish"] <= best["finish"] + o["margin_s"]:
                chosen, reason = anchor, "home kept within margin"
            else:
                chosen, reason = best, "earliest predicted finish"
        planned[chosen["host"]].append(chosen["_entry"])
        res.update(host=chosen["host"], reason=reason, start=chosen["start"], finish=chosen["finish"],
                   wait_s=chosen["start"] - now)
        objective += chosen["start"] - now
        results[j["id"]] = res
    for res in results.values():
        for r in res["considered"]:
            r.pop("_entry", None)
    views = {}
    for h in hmap.values():
        views[h["name"]] = host_view(h, now, est, knownd, planned[h["name"]], movable, o)
    return {"now": now, "jobs": results, "hosts": views, "objective_s": objective, "transfers": transfers,
            "options": o}


def host_view(host: dict, now: float, est: Estimator, known: dict, planned=(), movable=(), o=None) -> dict:
    """Per host: holders with predicted ends, the queue (fixed tickets and planned jobs) with predicted
    starts, and the predicted drain."""
    if not host["fresh"]:
        return {"fresh": False, "reasons": host["reasons"], "holders": [], "queue": [], "drain": None}
    holders, tickets = _fixed(host, now, est, known, set(movable), o)
    alltix = sorted(list(tickets) + list(planned), key=lambda t: t["order"])
    res, drain = simulate(host, holders, alltix, now)
    hv = [{"name": h["name"], "keys": h.get("keys", []), "elapsed_s": h.get("elapsed_s", 0.0),
           "remaining_s": h["remaining_s"], "external": h["external"], "est_source": h["est"]["source"]}
          for h in holders]
    qv = [{"id": t["id"], "name": t.get("name", t["id"]), "ticket": t.get("ticket"), "cpu": bool(t.get("cpu")),
           "gpu": (ALL if t["devices"] == ALL else ("one" if t.get("one") else ("none" if not t["devices"] else "some"))),
           "start": res[t["id"]][0], "finish": res[t["id"]][1], "external": bool(t.get("external"))}
          for t in alltix]
    return {"fresh": True, "reasons": [], "holders": hv, "queue": qv, "drain": drain}


def place(hosts: list[dict], job: dict, now: float, est: Estimator | None = None, opts: dict | None = None,
          known: list[dict] = ()) -> dict:
    """The placement of one new job: plan() over just that job, other jobs fixed where they are."""
    j = dict(job, state="new")
    p = plan(hosts, [j], now, est, opts, known)
    return p["jobs"][j["id"]]
