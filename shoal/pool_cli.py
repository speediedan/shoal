#!/usr/bin/env python3
"""`shoal pool probe|status|plan` and the I/O half of `job run --place` (architecture doc §6, "Pool placement").

The pool is this host plus every `[hosts.<server>]` route in the host config. Each host describes itself
with `shoal pool probe --json` (this host in process, the others over their route's ssh, short timeouts);
a failed or stale probe makes that host ineligible, never an error. `pool.py` plans; this module stages,
wraps, launches, supervises and records:

    shoal pool probe --json                 # this host: leases, devices, memory, cores, CI envs, worktree root
    shoal pool status [--json]              # per host: capacity, holders, queue, predicted drain
    shoal pool plan [--dry-run] [--json]    # the plan over this host's queued placed jobs, and any transfers
    shoal job run ... --place auto|<host> --needs cpu|gpu[:N][:vendor][:vramGiB] [--cpu-heavy]
                  [--ci-env NAME] [--repo PATH --ref SHA] [--est MIN] -- CMD

A placed job runs through the ordinary job-unit machinery (`--on` for another host), wrapped in the
target host's lease helper with the declared scope. Inside the lease, before CMD, it takes its start
claim and writes a grant-time marker, recorded as `lease_granted` in the job meta. Its outputs go to
`$SHOAL_JOB_OUT`, copied back to this host's job dir at the verdict. Under `features.placement`, the
job's own `job wait` (the only writer that moves it) re-plans while the job is still queued for its
lease and moves it when another host would start it sooner by more than the margin plus staging.
"""
from __future__ import annotations

import argparse
import datetime
import fcntl
import glob
import json
import os
import shlex
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "telemetry"))

import config  # noqa: E402
import pool  # noqa: E402
import routes  # noqa: E402

PROBE_TIMEOUT_S = 20
PROBE_CACHE_S = 20
SAMPLE_MAX_AGE_S = 120
HOLDER_HISTORY_CACHE_S = 600
HOLDER_HISTORY_DAYS = 14
STAGE_TIMEOUT_S = 900


def utc(epoch: float | None = None) -> str:
    return datetime.datetime.fromtimestamp(time.time() if epoch is None else epoch,
                                           datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def pool_dir() -> str:
    return os.path.join(config.data_root(), "_pool")


# ---- this host's probe ------------------------------------------------------------------------------

def _latest_sample(host: str) -> dict | None:
    """The newest resource sample this host's `resources watch` wrote (only the files' tails are read)."""
    d = os.path.join(config.data_root(), "_resources", host)
    for f in sorted(glob.glob(os.path.join(d, "events-*.jsonl")))[-2:][::-1]:
        try:
            with open(f, "rb") as fh:
                fh.seek(0, 2)
                fh.seek(max(0, fh.tell() - 512 * 1024))
                lines = fh.read().decode("utf-8", "replace").splitlines()
        except OSError:
            continue
        for line in reversed(lines):
            if '"resource_sample"' not in line:
                continue
            try:
                v = json.loads(line)
            except ValueError:
                continue
            if isinstance(v, dict) and v.get("type") == "resource_sample":
                return v
    return None


def gpu_arch(gpus: list[dict], kfd: str = "/sys/class/kfd/kfd/topology/nodes") -> dict[str, str]:
    """{uuid: arch tag}: NVIDIA compute capability (`sm_89`) and AMD gfx target (`gfx1100`), read without
    waking a suspended AMD device. Unknown stays absent."""
    out: dict[str, str] = {}
    if any(g.get("vendor") == "nvidia" and not g.get("arch") for g in gpus):
        try:
            import resource_usage
            r = resource_usage.run(["nvidia-smi", "--query-gpu=uuid,compute_cap", "--format=csv,noheader"])
            for line in r.stdout.splitlines() if r.returncode == 0 else []:
                uid, _, cap = [x.strip() for x in line.partition(",")]
                if uid and cap and cap.replace(".", "").isdigit():
                    out[uid] = "sm_" + cap.replace(".", "")
        except (OSError, subprocess.SubprocessError, ImportError):
            pass
    if any(g.get("vendor") == "amd" and not g.get("arch") for g in gpus):
        for prop in glob.glob(os.path.join(kfd, "*", "properties")):
            try:
                kv = dict(line.split(None, 1) for line in open(prop).read().splitlines() if " " in line)
                uid, ver = int(kv.get("unique_id", "0")), int(kv.get("gfx_target_version", "0"))
            except (OSError, ValueError):
                continue
            if uid and ver:
                out["GPU-" + format(uid, "x").zfill(16)] = \
                    f"gfx{ver // 10000}{(ver // 100) % 100:x}{ver % 100:x}"
    return out


def holder_history(host: str, now: float | None = None) -> dict[str, list[float]]:
    """{normalized holder: [seconds]} from this host's observed lease releases (the resource stream), so
    a holder the pool cannot move (a CI agent) still gets a duration from its own history. Cached."""
    now = time.time() if now is None else now
    cache = os.path.join(pool_dir(), f"holder-history-{host}.json")
    try:
        c = json.load(open(cache))
        if now - float(c.get("t", 0)) < HOLDER_HISTORY_CACHE_S:
            return c.get("h") or {}
    except (OSError, ValueError):
        pass
    d = os.path.join(config.data_root(), "_resources", host)
    seen, out = set(), {}
    for f in sorted(glob.glob(os.path.join(d, "events-*.jsonl")))[-HOLDER_HISTORY_DAYS:]:
        try:
            with open(f) as fh:
                for line in fh:
                    if "lease_observed_release" not in line:
                        continue
                    try:
                        ev = json.loads(line)
                    except ValueError:
                        continue
                    lease = ev.get("lease") or {}
                    since = pool._epoch(lease.get("since"))
                    end = ev.get("release_by")
                    key = (lease.get("holder"), lease.get("since"))
                    if since is None or not isinstance(end, (int, float)) or key in seen:
                        continue
                    seen.add(key)
                    out.setdefault(pool.normalize_name(str(lease.get("holder") or "unattributed")), []).append(
                        round(max(0.0, end - since), 1))
        except OSError:
            continue
    out = {k: v[-50:] for k, v in out.items()}
    try:
        os.makedirs(pool_dir(), exist_ok=True)
        with open(cache + ".tmp", "w") as fh:
            json.dump({"t": now, "h": out}, fh)
        os.replace(cache + ".tmp", cache)
    except OSError:
        pass
    return out


def probe(now: float | None = None) -> dict:
    """This host as a pool member: lease status from the configured helper's `--status --json`, the
    latest resource sample's devices and memory (a live read when there is none fresh), cores, CI
    environments, worktree root and the probe's time."""
    import resource_usage
    now = time.time() if now is None else now
    host = config.server()
    s = _latest_sample(host)
    if s and 0 <= now - float(s.get("ts") or 0) <= SAMPLE_MAX_AGE_S:
        gpus, memory, src = s.get("gpus") or [], s.get("memory") or {}, "sample"
    else:
        gpus, _errors = resource_usage.gpu_observations()
        memory, src = resource_usage.memory_observation(), "live"
    keep = ("uuid", "name", "vendor", "vram_total_mib", "vram_used_mib", "utilization_pct", "runtime_status", "arch")
    gpus = [{k: g.get(k) for k in keep if k in g} for g in gpus if isinstance(g, dict)]
    arch = gpu_arch(gpus)
    for g in gpus:
        if not g.get("arch") and arch.get(g.get("uuid")):
            g["arch"] = arch[g["uuid"]]
    lease = resource_usage.lease_observation(config.lease_cmd())
    try:
        cores = len(os.sched_getaffinity(0))
    except (AttributeError, OSError):
        cores = os.cpu_count()
    return {"v": 1, "type": "pool_probe", "host": host, "ts": now, "ts_utc": utc(now), "cores": cores,
            "memory": memory, "gpus": gpus, "gpu_source": src,
            "lease": {k: lease.get(k) for k in ("ok", "error", "leases", "queue") if k in lease},
            "lease_cmd": config.lease_cmd(), "ci_envs": config.ci_envs(), "worktree_root": config.worktree_root(),
            "holder_history": holder_history(host, now)}


# ---- the pool's probes ------------------------------------------------------------------------------

def pool_names() -> list[str]:
    home = config.server()
    return [home] + sorted(n for n in routes.routes() if n != home)


def remote_probe(server: str, timeout: float = PROBE_TIMEOUT_S, cache_s: float = PROBE_CACHE_S) -> dict:
    """`shoal pool probe --json` on `server` over its route; {"host", "error"} when that fails."""
    r = routes.route(server)
    if r is None:
        return {"host": server, "error": "no route"}
    key = f"pool-probe-{server}"
    c = routes._cached(key, cache_s) if cache_s else None
    if c is not None and isinstance(c.get("probe"), dict):
        return c["probe"]
    try:
        out = subprocess.run(routes.ssh_argv(r) + [routes.remote_cmd(r, "pool", "probe", "--json")],
                             stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout)
        if out.returncode != 0:
            raise ValueError(f"rc={out.returncode}: {(out.stderr or out.stdout).strip()[-160:]}")
        d = json.loads(out.stdout)
        if not isinstance(d, dict) or d.get("type") != "pool_probe":
            raise ValueError("not a pool probe")
    except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
        return {"host": server, "error": str(exc)[:200], "fetched_ts": time.time()}
    d["fetched_ts"] = time.time()
    if cache_s:
        routes._store(key, {"probe": d})
    return d


def gather(only: list[str] | None = None, cache_s: float = PROBE_CACHE_S) -> dict[str, dict]:
    """{name: probe} for the pool (or `only` those hosts): this host in process, the rest in parallel."""
    home = config.server()
    names = [n for n in pool_names() if not only or n in only]
    out: dict[str, dict] = {}
    threads = []
    for n in names:
        if n == home:
            continue
        t = threading.Thread(target=lambda n=n: out.__setitem__(n, remote_probe(n, cache_s=cache_s)), daemon=True)
        t.start()
        threads.append(t)
    if home in names:
        try:
            out[home] = dict(probe(), fetched_ts=time.time())
        except Exception as exc:  # noqa: BLE001 - a probe failure makes the host ineligible, never an error
            out[home] = {"host": home, "error": str(exc)[:200], "fetched_ts": time.time()}
    for t in threads:
        t.join(PROBE_TIMEOUT_S + 5)
    return {n: out.get(n) or {"host": n, "error": "probe timed out", "fetched_ts": time.time()} for n in names}


def capacities(probes: dict[str, dict], now: float, max_age_s: float = pool.DEFAULTS["max_age_s"]) -> list[dict]:
    return [pool.host_capacity(p, now, max_age_s, name=n) for n, p in probes.items()]


# ---- job history and local placed jobs --------------------------------------------------------------

def history_path() -> str:
    return os.path.join(pool_dir(), "history.jsonl")


def load_history(limit: int = 5000) -> list[dict]:
    try:
        with open(history_path()) as fh:
            lines = fh.readlines()[-limit:]
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            v = json.loads(line)
        except ValueError:
            continue
        if isinstance(v, dict):
            out.append(v)
    return out


def estimator(probes: dict[str, dict]) -> pool.Estimator:
    return pool.Estimator(load_history(), {n: (p or {}).get("holder_history") or {} for n, p in probes.items()})


def _meta_path(sid: str, unit: str) -> str:
    import jobs
    return jobs.paths(sid, unit)["meta"]


def save_meta(sid: str, unit: str, meta: dict) -> None:
    path = _meta_path(sid, unit)
    with open(path + ".tmp", "w") as fh:
        json.dump(meta, fh, indent=1)
    os.replace(path + ".tmp", path)


def placed_metas(sid: str = "") -> list[dict]:
    """This host's placed job records (every shoal, or `sid`'s)."""
    import jobs
    pattern = os.path.join(os.path.dirname(jobs.paths(sid or "*", "x")["meta"]), "*.json")
    out = []
    for f in sorted(glob.glob(pattern)):
        try:
            m = json.load(open(f))
        except (OSError, ValueError):
            continue
        if isinstance(m, dict) and isinstance(m.get("placement"), dict) and m.get("unit"):
            out.append(m)
    return out


def job_state(meta: dict) -> str:
    """queued (unit active, no grant yet), running, done or unknown, from the unit and its marker."""
    import jobs
    unit = meta["unit"]
    if meta.get("host"):
        st = jobs.remote_state(meta["host"], unit)
        if not st:
            return "unknown"
        granted = st.get("granted")
    else:
        st = jobs.unit_state(unit)
        granted = jobs._read(jobs.paths(meta.get("shoal") or "", unit)["granted"])
    if st.get("ActiveState") not in ("active", "activating", "deactivating", "reloading"):
        return "done"
    return "running" if (granted or meta.get("lease_granted")) else "queued"


def planner_job(meta: dict, state: str) -> dict:
    pl = meta.get("placement") or {}
    return {"id": meta["unit"], "name": meta.get("name") or meta["unit"], "repo": pl.get("repo") or "",
            "demand": pl.get("demand") or pool.demand(), "home": pl.get("home") or config.server(),
            "current": pl.get("host") or "", "state": state, "submit_ts": float(pl.get("submit_ts") or 0),
            "priority": int(pl.get("priority") or 0), "prior_s": pl.get("prior_s"), "moves": int(pl.get("moves") or 0),
            "class": meta.get("class") or "ci"}


def local_jobs(sid: str = "", skip: str = "") -> list[dict]:
    """Planner jobs for this host's placed jobs that are still live (queued or running)."""
    out = []
    for m in placed_metas(sid):
        if m["unit"] == skip:
            continue
        st = "running" if m.get("lease_granted") else job_state(m)
        if st in ("queued", "running"):
            out.append(planner_job(m, st))
    return out


def record_history(meta: dict, rc, end_ts: float | None) -> None:
    """One duration record per finished placed run (grant to exit), the planner's learned estimates."""
    pl = meta.get("placement") or {}
    start = pool._epoch(meta.get("lease_granted"))
    if start is None or end_ts is None or meta.get("history_recorded") == meta.get("started_utc"):
        return
    rec = {"ts": utc(), "repo": pl.get("repo") or "", "name": pool.normalize_name(meta.get("name") or ""),
           "job": meta.get("name"), "host": pl.get("host") or "", "unit": meta.get("unit"), "shoal": meta.get("shoal"),
           "run_s": round(max(0.0, end_ts - start), 1),
           "wait_s": round(max(0.0, start - float(pl.get("submit_ts") or start)), 1), "rc": rc}
    os.makedirs(pool_dir(), exist_ok=True)
    with open(history_path(), "a") as fh:
        fh.write(json.dumps(rec, sort_keys=True) + "\n")
    meta["history_recorded"] = meta.get("started_utc")


# ---- staging, wrapping, launching -------------------------------------------------------------------

def _git(repo: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True, timeout=60)


def source_of(repo: str, ref: str) -> dict:
    """{path, url, sha, identity, block}: the checkout's origin and the commit to run. `block` says why
    only home can run it (no checkout, no origin, a dirty tree without --ref, a SHA not on any
    remote-tracking branch). Raises ValueError for a ref that does not resolve."""
    if not repo:
        if ref:
            raise ValueError("--ref needs --repo (the checkout the commit belongs to)")
        return {"path": "", "url": "", "sha": "", "identity": "", "block": "no --repo to stage elsewhere"}
    path = os.path.abspath(repo)
    if _git(path, "rev-parse", "--git-dir").returncode != 0:
        raise ValueError(f"--repo {path} is not a git checkout")
    url = _git(path, "remote", "get-url", "origin").stdout.strip()
    block = ""
    if ref:
        r = _git(path, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
        if r.returncode != 0:
            raise ValueError(f"--ref {ref} is not a commit in {path}")
        sha = r.stdout.strip()
    else:
        sha = _git(path, "rev-parse", "HEAD").stdout.strip()
        if _git(path, "status", "--porcelain", "--untracked-files=no").stdout.strip():
            block = "dirty tree without --ref (commit and push, or pass --ref)"
    if not url:
        block = block or "the checkout has no origin remote"
    elif not _git(path, "branch", "-r", "--contains", sha).stdout.strip():
        block = block or "the SHA is on no remote-tracking branch (push it first)"
    return {"path": path, "url": url, "sha": sha, "identity": pool.repo_identity(url or path), "block": block}


def _safe(s: str) -> str:
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in s) or "repo"


def stage_script(url: str, sha: str, root: str, identity: str, worktree: str) -> str:
    """Fetch the repo's origin into a mirror under the worktree root and add a detached worktree at
    `sha`. Exit 9 when the SHA is not on the remote: a pushed SHA is required, source is never copied."""
    q = shlex.quote
    mirror = f"{root}/.mirrors/{_safe(identity.replace('/', '__'))}.git"
    return "; ".join([
        "set -e", f"mkdir -p {q(root + '/.mirrors')}",
        f"if [ ! -d {q(mirror)} ]; then git clone -q --mirror {q(url)} {q(mirror)}; fi",
        f"git -C {q(mirror)} fetch -q --prune origin || echo 'shoal: fetch from origin failed' >&2",
        f"git -C {q(mirror)} cat-file -e {q(sha + '^{commit}')} 2>/dev/null || "
        f"git -C {q(mirror)} fetch -q origin {q(sha)} 2>/dev/null || true",
        f"git -C {q(mirror)} cat-file -e {q(sha + '^{commit}')} 2>/dev/null || "
        f"{{ echo 'SHA-NOT-ON-REMOTE {sha}' >&2; exit 9; }}",
        f"if [ -e {q(worktree)} ]; then git -C {q(mirror)} worktree remove --force {q(worktree)} 2>/dev/null "
        f"|| rm -rf {q(worktree)}; git -C {q(mirror)} worktree prune; fi",
        f"git -C {q(mirror)} worktree add -q --detach {q(worktree)} {q(sha)}",
        f"echo STAGED {q(worktree)}"])


def inner_script(marker: str, claim: str, out: str, dev_env: dict, envname: str = "", envd: dict | None = None) -> str:
    """What runs inside the lease, before CMD: take the start claim (a move that took it first cancels
    this start), write the grant-time marker, export SHOAL_JOB_OUT, device selectors and the CI env."""
    q = shlex.quote
    lines = [f"mkdir {q(claim)} 2>/dev/null || {{ echo 'shoal: start claim already taken (the job moved); "
             f"not starting' >&2; exit 75; }}",
             f"date -u +%Y-%m-%dT%H:%M:%SZ > {q(marker)}", f"mkdir -p {q(out)}", f"export SHOAL_JOB_OUT={q(out)}"]
    lines += [f"export {k}={q(v)}" for k, v in dev_env.items()]
    if envname:
        lines.append(f"export SHOAL_CI_ENV={q(envname)}")
        py = (envd or {}).get("python") or ""
        if py:
            bindir = os.path.dirname(py)
            lines += [f"export SHOAL_ENV_PYTHON={q(py)}", f'export PATH={q(bindir)}:"$PATH"']
            if os.path.basename(bindir) == "bin":
                lines.append(f"export VIRTUAL_ENV={q(os.path.dirname(bindir))}")
    lines.append('exec "$@"')
    return "; ".join(lines)


def wrap(cmd: list[str], dem: dict, lease_cmd: str, marker: str, claim: str, out: str,
         envname: str = "", envd: dict | None = None) -> tuple[list[str], str]:
    """(argv, lease scope) for CMD on a target: the lease helper with the demand's scope (none for
    plain CPU work), then the inner script, then CMD."""
    largs, scope = pool.lease_args(dem)
    inner = ["/bin/bash", "-c", inner_script(marker, claim, out, pool.device_env(dem), envname, envd), "shoal-place"]
    if largs:
        return shlex.split(lease_cmd) + largs + ["--"] + inner + list(cmd), scope
    return inner + list(cmd), scope


def launch(req: dict, target: dict, unit: str, p: dict, home: str, dry_run: bool = False) -> tuple[int, dict]:
    """Stage (on a host other than home), wrap and start the job unit on `target` through the plain job
    machinery (`_run_remote` / `_run_local`). Returns (rc, info)."""
    import jobs
    dem = req["demand"]
    remote = target["name"] != home
    info: dict = {"host": target["name"], "worktree": ""}
    if remote:
        hdir = "" if dry_run else jobs.remote_home(target["name"])
        if not dry_run and not hdir:
            print(f"ERROR: ssh to {target['name']} failed (no home directory answer)")
            return 2, info
        rp = jobs.remote_paths(unit, hdir)
        root = target["worktree_root"].rstrip("/")
        wt = f"{root}/{unit}"
        script = stage_script(req["url"], req["sha"], root, req["repo"], wt)
        if dry_run:
            print(f"[dry-run] stage on {target['name']}: {script}")
        else:
            try:
                r = jobs._ssh(target["name"], script, timeout=STAGE_TIMEOUT_S)
            except (OSError, subprocess.TimeoutExpired) as exc:
                print(f"ERROR: staging on {target['name']} failed: {exc}")
                return 2, info
            if r.returncode == 9:
                print(f"JOB-REFUSED: {req['sha'][:12]} is not on {req['url']} as seen from {target['name']} "
                      f"(a pushed SHA is required; source is never copied)")
                return 2, info
            if r.returncode != 0:
                print(f"ERROR: staging on {target['name']} failed rc={r.returncode}: {(r.stdout + r.stderr).strip()[-300:]}")
                return 2, info
        cwd, marker, claim, out = wt, rp["granted"], rp["claim"], rp["out"]
        info["worktree"] = wt
    else:
        cwd, marker, claim, out = req["cwd"], p["granted"], p["claim"], p["out"]
    envd = (target.get("envs") or {}).get(req.get("ci_env") or "") or {}
    wrapped, scope = wrap(req["cmd"], dem, target["lease_cmd"], marker, claim, out, req.get("ci_env") or "", envd)
    info.update(cwd=cwd, scope=scope, wrapped=wrapped, marker=marker, claim=claim, out=out)
    ns = argparse.Namespace(sid=req["sid"], slug=req["slug"], name=req["name"], on=target["name"] if remote else "",
                            cwd=cwd, env=list(req.get("env") or []) + [f"GPU_LEASE_PROJECT={unit}"],
                            memory_high=req.get("memory_high", ""), memory_max=req.get("memory_max", ""),
                            dry_run=dry_run, spec="", job_class=req.get("class") or "ci")
    rc = jobs._run_remote(ns, wrapped, unit, p) if remote else jobs._run_local(ns, wrapped, unit, p)
    return rc, info


def _considered(res: dict, now: float) -> list[dict]:
    return [{"host": r["host"], "eligible": r["eligible"], "reasons": r["reasons"],
             "start_utc": utc(r["start"]) if r.get("start") is not None else None,
             "start_in_s": round(r["start"] - now) if r.get("start") is not None else None,
             "finish_in_s": round(r["finish"] - now) if r.get("finish") is not None else None,
             "p50_s": round(r["p50_s"]) if r.get("p50_s") is not None else None,
             "est_source": r.get("est_source")} for r in res.get("considered") or []]


def _fmt_in(s) -> str:
    if s is None:
        return "-"
    s = int(round(s))
    return f"+{s // 60}m" if s < 3600 else f"+{s // 3600}h{(s % 3600) // 60:02d}m"


def print_considered(rows: list[dict]) -> None:
    for r in rows:
        if r["eligible"]:
            print(f"  {r['host']:<16} start {_fmt_in(r['start_in_s']):>8}  finish {_fmt_in(r['finish_in_s']):>8}"
                  f"  (p50 {_fmt_in(r['p50_s'])} {r['est_source']})")
        else:
            print(f"  {r['host']:<16} ineligible: {'; '.join(r['reasons'])}")


def _vendor_default(hosts: list[dict], home: str) -> str:
    """A GPU job with no declared vendor stays on its home host's vendor (any, when home has both or none)."""
    h = next((x for x in hosts if x["name"] == home), None)
    vendors = {g["vendor"] for g in (h or {}).get("gpus") or [] if g.get("vendor")}
    return vendors.pop() if len(vendors) == 1 else "any"


def _spec_feature(sid: str) -> dict:
    try:
        from roles import ShoalSpec, find_spec
        return pool.feature(ShoalSpec.load(find_spec(sid, "")))
    except Exception:  # noqa: BLE001 - no spec reachable: transfers stay off, defaults apply
        return pool.feature(None)


def run_placed(a, cmd: list[str], unit: str, p: dict) -> int:
    """`job run --place auto|<host>`: plan, stage, wrap, launch, record."""
    import jobs
    now = time.time()
    home = config.server()
    old = jobs._meta_for(a, unit)
    if old:
        st = jobs.remote_state(old["host"], unit) if old.get("host") else jobs.unit_state(unit)
        if st.get("ActiveState") in ("active", "activating", "reloading"):
            print(f"JOB-ALREADY-RUNNING {unit}{' on ' + old['host'] if old.get('host') else ''} "
                  f"(follow it with `shoal job wait --shoal {a.sid} --slug {a.slug} --name {a.name}`)")
            return 4
    pin = "" if a.place == "auto" else a.place
    if pin and pin not in pool_names():
        print(f"ERROR: --place {pin}: not a pool host (pool: {', '.join(pool_names())}; "
              f"another host joins with a [hosts.<server>] route)")
        return 1
    try:
        pool.parse_needs(a.needs)
        src = source_of(a.repo, a.ref)
    except ValueError as exc:
        print(f"ERROR: {exc}")
        return 1
    probes = gather(only=[pin] if pin else None)
    f = _spec_feature(a.sid)
    hosts = capacities(probes, time.time(), f["max_age_s"])
    dem = pool.demand(a.needs, _vendor_default(hosts, home), a.cpu_heavy, a.memory_gib, a.ci_env,
                      src["identity"], src["sha"], pin, src["block"])
    prior_s = a.est * 60 if a.est else None
    job = {"id": unit, "name": a.name, "repo": src["identity"], "demand": dem, "home": home, "submit_ts": now,
           "priority": a.priority, "prior_s": prior_s, "class": a.job_class}
    known = [planner_job(m, "known") for m in placed_metas() if m["unit"] != unit]
    res = pool.place(hosts, job, time.time(), estimator(probes), f, known)
    rows = _considered(res, now)
    _, scope = pool.lease_args(dem)
    print(f"PLACEMENT {unit}: {res['host'] or 'none'} ({res['reason']}) scope={scope}"
          + (f" sha={src['sha'][:12]}" if src["sha"] else ""))
    print_considered(rows)
    if not res["host"]:
        print(f"JOB-REFUSED: no compatible pool host for {a.needs}"
              + (f" (pinned to {pin})" if pin else ""))
        return 3
    target = next(h for h in hosts if h["name"] == res["host"])
    req = {"sid": a.sid, "slug": a.slug, "name": a.name, "cmd": list(cmd), "env": list(a.env or []),
           "memory_high": a.memory_high, "memory_max": a.memory_max,
           "cwd": os.path.abspath(a.cwd or src["path"] or os.getcwd()), "repo_path": src["path"], "url": src["url"],
           "sha": src["sha"], "repo": src["identity"], "ci_env": a.ci_env, "demand": dem, "class": a.job_class}
    rc, info = launch(req, target, unit, p, home, a.dry_run)
    if rc != 0 or a.dry_run:
        return rc
    meta = jobs._meta_for(a, unit)
    decision = {"chosen": res["host"], "reason": res["reason"], "considered": rows, "scope": info["scope"],
                "sha": src["sha"], "repo": src["identity"], "predicted_start_utc": utc(res["start"]),
                "predicted_wait_s": round(res["wait_s"] or 0)}
    meta.update(cmd=list(cmd), wrapped_cmd=info["wrapped"], lease_granted=None, placement={
        "host": res["host"], "home": home, "reason": res["reason"], "scope": info["scope"], "sha": src["sha"],
        "repo": src["identity"], "url": src["url"], "demand": dem, "prior_s": prior_s, "priority": a.priority,
        "submit_ts": now, "submit_utc": utc(now), "moves": 0, "moved": [], "marker": info["marker"],
        "claim": info["claim"], "out": info["out"], "worktree": info["worktree"], "decision": decision,
        "request": {k: req[k] for k in ("cmd", "env", "memory_high", "memory_max", "cwd", "repo_path", "url", "sha",
                                         "repo", "ci_env", "class")}})
    save_meta(a.sid, unit, meta)
    jobs._emit(a.sid, a.slug, "placed", f"{unit} on {res['host']} ({res['reason']}) scope={info['scope']}",
               {"placement": decision})
    print(f"JOB-PLACED {unit} on {res['host']} ({res['reason']}; predicted start {_fmt_in(res['wait_s'])})")
    return 0


# ---- supervision (job wait) -------------------------------------------------------------------------

def _supervisor(a, unit: str) -> bool:
    """One `job wait` per job supervises (records the grant, moves the job); re-armed copies only wait."""
    if getattr(a, "_pool_lock", None) is not None:
        return True
    import jobs
    path = os.path.join(jobs.paths(a.sid, unit)["sdir"], f"{unit}.supervisor.lock")
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fh = open(path, "a")
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    a._pool_lock = fh
    return True


def supervise(a, unit: str, meta: dict, granted: str) -> str | None:
    """A placed job's poll while its unit is active: record `lease_granted` from the grant marker, and
    under features.placement re-plan the still-queued job on an interval and move it on transfer advice.
    "moved" when it moved."""
    import jobs
    if not _supervisor(a, unit):
        return None
    if granted and not meta.get("lease_granted"):
        meta["lease_granted"] = granted.strip()
        save_meta(a.sid, unit, meta)
        where = meta.get("host") or config.server()
        print(f"{jobs.now_iso()} JOB-LEASE-GRANTED {unit} on {where} lease_granted={meta['lease_granted']}")
        jobs._emit(a.sid, a.slug or meta.get("slug") or "?", "lease-granted", f"{unit} on {where} at {meta['lease_granted']}")
        return None
    if meta.get("lease_granted") or granted:
        return None
    if not hasattr(a, "_pool_feature"):
        a._pool_feature = _spec_feature(a.sid)
    f = a._pool_feature
    if not f["enabled"]:
        return None
    pl = meta["placement"]
    now = time.time()
    last = max(float(getattr(a, "_pool_last_plan", 0) or 0), float(pl.get("last_move_ts") or pl.get("submit_ts") or 0))
    if now - last < float(f["interval_s"]):
        return None
    a._pool_last_plan = now
    probes = gather()
    hosts = capacities(probes, time.time(), f["max_age_s"])
    others = local_jobs(skip=unit)
    mine = planner_job(meta, "queued")
    p = pool.plan(hosts, [mine] + [j for j in others if j["state"] == "queued"], time.time(), estimator(probes), f,
                  known=[j for j in others if j["state"] != "queued"])
    res = p["jobs"].get(unit) or {}
    adv = res.get("transfer")
    if not adv:
        return None
    target = next((h for h in hosts if h["name"] == adv["to"]), None)
    return "moved" if target is not None and transfer(a, unit, meta, adv, target, _considered(res, now)) else None


def budget_check(a, unit: str, meta: dict, granted: str) -> None:
    """A CI job past its runtime budget (`features.placement.ci_budget_min`, default 90 min; counted from
    `lease_granted`, or from the unit's start when no lease is used) gets one `overrun` event and one page,
    to its role (a ledger msg) and to the user (the health page path). The job is never killed."""
    import jobs
    if str(meta.get("class") or "ci") != "ci" or meta.get("overrun_paged") == meta.get("started_utc"):
        return
    if meta.get("placement"):
        start = meta.get("lease_granted") or (granted or "").strip()
        if not start:
            return  # still queued for its lease: the budget has not started
    else:
        start = meta.get("started_utc")
    t0 = pool._epoch(start)
    if not hasattr(a, "_pool_feature"):
        a._pool_feature = _spec_feature(a.sid)
    budget = float(a._pool_feature.get("ci_budget_min") or 0)
    if t0 is None or not budget or time.time() - t0 < budget * 60:
        return
    meta["overrun_paged"] = meta.get("started_utc")
    try:
        save_meta(a.sid, unit, meta)
    except OSError:
        pass
    slug = meta.get("slug") or a.slug or "?"
    where = f" on {meta['host']}" if meta.get("host") else ""
    text = (f"job {meta.get('name') or unit} ({unit}{where}) overran its CI budget: "
            f"{(time.time() - t0) / 60:.0f} min since {start} > {budget:g} min. It keeps running (never killed); "
            f"split it, run it as --class research inside a reservation, or raise features.placement.ci_budget_min.")
    print(f"{jobs.now_iso()} JOB-OVERRUN {unit}: {text}")
    jobs._emit(a.sid, slug, "overrun", text[:200], {"budget_min": budget, "since": start})
    try:
        from ledger import Ledger, SYSTEM_WRITER
        from roles import ShoalSpec, find_spec
        import health
        spec = ShoalSpec.load(find_spec(a.sid, ""))
        if spec.role(slug) is not None:
            Ledger(spec.sid, root=spec.ledger_dir).append(SYSTEM_WRITER, "msg", to=slug, kind="health", text=text[:500])
        health.page_user(spec, text, {})
    except Exception as exc:  # noqa: BLE001 - a page that cannot be delivered never stops the waiter
        print(f"(overrun page not delivered: {str(exc)[:160]})")


def _claim(meta: dict) -> bool:
    """Take the job's start claim where it waits: success means its command can never start there."""
    import jobs
    claim = (meta.get("placement") or {}).get("claim") or ""
    if not claim:
        return False
    if meta.get("host"):
        try:
            return jobs._ssh(meta["host"], f"mkdir {shlex.quote(claim)}", timeout=30).returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            return False
    try:
        os.mkdir(claim)
        return True
    except OSError:
        return False


def _stop(meta: dict, unit: str) -> None:
    """Stop the unit: TERM reaches its waiting lease request, which removes its queue ticket."""
    import jobs
    if meta.get("host"):
        try:
            jobs._ssh(meta["host"], f"systemctl --user stop {shlex.quote(unit)}.service", timeout=60)
        except (OSError, subprocess.TimeoutExpired):
            pass
    else:
        jobs._systemctl("stop", f"{unit}.service")


def transfer(a, unit: str, meta: dict, adv: dict, target: dict, considered: list[dict]) -> bool:
    """Move a queued placed job to `target`: claim its start where it waits (so it can never start
    there), stop that unit, restage and resubmit on the target with the original submit time kept."""
    import jobs
    pl = meta["placement"]
    src = pl["host"]
    if not _claim(meta):
        print(f"{jobs.now_iso()} JOB-MOVE-SKIPPED {unit}: its lease was granted on {src} (a running job never moves)")
        return False
    pl["moving"] = {"ts": time.time(), "to": target["name"]}
    save_meta(a.sid, unit, meta)
    _stop(meta, unit)
    req = dict(pl.get("request") or {}, sid=a.sid, slug=meta.get("slug") or a.slug, name=meta.get("name") or a.name,
               demand=pl.get("demand") or pool.demand())
    p = jobs.paths(a.sid, unit)
    home = pl.get("home") or config.server()
    rc, info = launch(req, target, unit, p, home)
    moved_to = target["name"]
    if rc != 0:
        print(f"{jobs.now_iso()} JOB-MOVE-FAILED {unit} -> {target['name']} rc={rc}; resubmitting on {src}")
        probes = gather(only=[src])
        back = next((h for h in capacities(probes, time.time(), 3600) if h["name"] == src), None)
        rc, info = launch(req, back, unit, p, home) if back is not None else (2, {})
        moved_to = src
        if rc != 0:
            pl.pop("moving", None)
            save_meta(a.sid, unit, meta)
            print(f"{jobs.now_iso()} JOB-MOVE-FAILED {unit}: resubmission on {src} failed too")
            return True  # the waiter re-reads the meta and reports the unit's real state
    new = jobs._meta_for(a, unit)
    rec = {"from": src, "to": moved_to, "ts_utc": utc(), "from_start_in_s": round(adv["from_start"] - time.time()),
           "to_start_in_s": round(adv["to_start"] - time.time()), "considered": considered}
    pl = dict(pl, host=moved_to, moves=int(pl.get("moves") or 0) + (moved_to != src), last_move_ts=time.time(),
              moved=list(pl.get("moved") or []) + [rec], marker=info.get("marker", pl.get("marker")),
              claim=info.get("claim", pl.get("claim")), out=info.get("out", pl.get("out")),
              worktree=info.get("worktree", pl.get("worktree")), scope=info.get("scope", pl.get("scope")))
    pl.pop("moving", None)
    new.update(cmd=list(req.get("cmd") or new.get("cmd") or []), wrapped_cmd=info.get("wrapped"),
               lease_granted=None, placement=pl)
    save_meta(a.sid, unit, new)
    if moved_to != src:
        jobs._emit(a.sid, req["slug"] or "?", "moved", f"{unit} {src} -> {moved_to} (start "
                   f"{_fmt_in(rec['from_start_in_s'])} -> {_fmt_in(rec['to_start_in_s'])})", {"placement": rec})
        print(f"{jobs.now_iso()} JOB-MOVED {unit} {src} -> {moved_to} (predicted start "
              f"{_fmt_in(rec['from_start_in_s'])} -> {_fmt_in(rec['to_start_in_s'])}; move {pl['moves']})")
    return True


def copy_back(host: str, remote_out: str, local_out: str) -> str:
    """Copy a remote job's SHOAL_JOB_OUT into this host's job dir; returns what was done."""
    import jobs
    try:
        r = subprocess.run(jobs._ssh_argv(host) + [f"cd {shlex.quote(remote_out)} 2>/dev/null && tar -cf - ."],
                           stdin=subprocess.DEVNULL, capture_output=True, timeout=600, env=config.tool_env())
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"outputs not copied ({exc})"
    if r.returncode != 0 or not r.stdout:
        return "no outputs"
    os.makedirs(local_out, exist_ok=True)
    x = subprocess.run(["tar", "-C", local_out, "-xf", "-"], input=r.stdout, capture_output=True, timeout=600)
    return f"outputs copied to {local_out}" if x.returncode == 0 else f"outputs not copied ({x.stderr[-200:]!r})"


def on_verdict(a, unit: str, meta: dict, rc, granted: str, end_ts) -> None:
    """At a placed job's verdict: record the grant (a short job may finish between polls), copy a remote
    job's outputs back, learn its duration, and drop a successful run's staged worktree."""
    import jobs
    pl = meta["placement"]
    if granted and not meta.get("lease_granted"):
        meta["lease_granted"] = granted.strip()
    if meta.get("lease_granted"):
        print(f"--- lease_granted={meta['lease_granted']}")
    if meta.get("host"):
        local_out = jobs.paths(a.sid, unit)["out"]
        print(f"--- {copy_back(meta['host'], pl.get('out') or jobs.remote_paths(unit)['out'], local_out)}")
        if rc == 0 and pl.get("worktree"):
            try:
                jobs._ssh(meta["host"], f"cd / && git -C {shlex.quote(pl['worktree'])} rev-parse --git-common-dir "
                          f">/dev/null 2>&1 && git -C \"$(git -C {shlex.quote(pl['worktree'])} rev-parse --git-common-dir)\" "
                          f"worktree remove --force {shlex.quote(pl['worktree'])}", timeout=60)
            except (OSError, subprocess.TimeoutExpired):
                pass
    if rc is not None:
        record_history(meta, rc, end_ts)
    save_meta(a.sid, unit, meta)


# ---- pool status / plan -----------------------------------------------------------------------------

def snapshot(sid: str = "") -> dict:
    now = time.time()
    probes = gather()
    f = pool.feature(None)
    hosts = capacities(probes, time.time(), f["max_age_s"])
    js = local_jobs(sid)
    p = pool.plan(hosts, [j for j in js if j["state"] == "queued"], time.time(), estimator(probes), f,
                  known=[j for j in js if j["state"] != "queued"])
    return {"now": now, "probes": probes, "hosts": hosts, "plan": p}


def _gib(v) -> str:
    return f"{v:.0f} GiB" if isinstance(v, (int, float)) else "?"


def cmd_probe(a) -> int:
    print(json.dumps(probe(), indent=None if a.json else 1))
    return 0


def cmd_status(a) -> int:
    s = snapshot(getattr(a, "sid", "") or "")
    home = config.server()
    if a.json:
        out = {"now_utc": utc(s["now"]), "home": home, "hosts": []}
        for h in s["hosts"]:
            v = s["plan"]["hosts"].get(h["name"]) or {}
            out["hosts"].append({k: h[k] for k in ("name", "fresh", "reasons", "lease_ok", "cores", "mem_avail_gib",
                                                   "gpus", "envs", "worktree_root")}
                                | {"holders": v.get("holders", []), "queue": v.get("queue", []),
                                   "drain_in_s": round(v["drain"] - s["now"]) if v.get("drain") else None})
        print(json.dumps(out, indent=1, default=str))
        return 0
    print(f"pool: {len(s['hosts'])} host(s), home {home}, {utc(s['now'])}")
    for h in s["hosts"]:
        v = s["plan"]["hosts"].get(h["name"]) or {}
        tag = " (home)" if h["name"] == home else ""
        if not h["fresh"]:
            print(f"{h['name']}{tag}: INELIGIBLE ({'; '.join(h['reasons'])})")
            continue
        print(f"{h['name']}{tag}: cores {h['cores']}  memory {_gib(h['mem_avail_gib'])} available  "
              f"lease {'ok' if h['lease_ok'] else 'unknown'}  predicted drain {_fmt_in((v.get('drain') or s['now']) - s['now'])}")
        for g in h["gpus"]:
            print(f"  gpu {g['uuid']} {g['vendor']} {_gib(g['vram_gib'])} {g.get('arch') or ''}".rstrip())
        if h["envs"]:
            print(f"  ci envs: {', '.join(sorted(h['envs']))}")
        for x in v.get("holders", []):
            print(f"  holder {x['name']} [{','.join(x['keys'])}] {'external ' if x['external'] else ''}"
                  f"elapsed {_fmt_in(x['elapsed_s'])} remaining {_fmt_in(x['remaining_s'])} ({x['est_source']})")
        for q in v.get("queue", []):
            print(f"  queued {q.get('ticket') or '-'} {q['name']} gpu={q['gpu']} cpu-heavy={'yes' if q['cpu'] else 'no'} "
                  f"start {_fmt_in(q['start'] - s['now'])} finish {_fmt_in(q['finish'] - s['now'])}")
    return 0


def cmd_plan(a) -> int:
    s = snapshot(getattr(a, "sid", "") or "")
    p = s["plan"]
    f = _spec_feature(a.sid) if getattr(a, "sid", "") else None
    if a.json:
        print(json.dumps({"now_utc": utc(s["now"]), "objective_s": round(p["objective_s"]),
                          "transfers": p["transfers"], "jobs": p["jobs"],
                          "transfers_enabled": bool(f and f["enabled"])}, indent=1, default=str))
        return 0
    print(f"plan over {len(p['jobs'])} queued placed job(s) on {config.server()}: predicted wait sum "
          f"{_fmt_in(p['objective_s'])}")
    for jid, r in sorted(p["jobs"].items()):
        if r.get("transfer"):
            t = r["transfer"]
            print(f"  MOVE {jid}: {t['from']} -> {t['to']} (start {_fmt_in(t['from_start'] - s['now'])} -> "
                  f"{_fmt_in(t['to_start'] - s['now'])})")
        else:
            print(f"  stay {jid}: {r['host'] or '?'} ({r['reason']}; start {_fmt_in(r['wait_s'])})")
    state = ("on: each job's `job wait` makes its own move" if f and f["enabled"] else
             "off (features.placement): nothing moves" if f else "per shoal (pass --shoal to check features.placement)")
    print(f"transfers: {state}. `pool plan` never moves a job itself.")
    return 0


def add_parsers(sub) -> None:
    p = sub.add_parser("pool", help="CI pool placement: probe hosts, show capacity and queues, plan transfers")
    ps = p.add_subparsers(dest="pool_cmd", required=True)
    q = ps.add_parser("probe", help="describe this host as a pool member (JSON)")
    q.add_argument("--json", action="store_true", help="compact JSON (the output is JSON either way)")
    q.set_defaults(func=cmd_probe)
    q = ps.add_parser("status", help="per host: capacity, holders, queue, predicted drain")
    config.add_id_arg(q, default="", help="only this shoal's placed jobs in the plan")
    q.add_argument("--json", action="store_true")
    q.set_defaults(func=cmd_status)
    q = ps.add_parser("plan", help="the plan over this host's queued placed jobs and the transfers it advises")
    config.add_id_arg(q, default="", help="only this shoal's placed jobs (and its features.placement state)")
    q.add_argument("--dry-run", action="store_true", help="accepted for symmetry: `pool plan` never moves a job")
    q.add_argument("--json", action="store_true")
    q.set_defaults(func=cmd_plan)
