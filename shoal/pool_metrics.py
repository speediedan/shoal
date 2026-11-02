"""Cross-host lease imbalance over compatible demand, for the resource sampler's Prometheus text.

A placed job (`shoal job run --place`) queued for its lease on one host is movable work only if
another pool host could run it: the pool planner's own `pool.compatible(host, demand, home)` says
so, from the job's recorded demand (GPU count, vendor, minimum VRAM, CPU-heavy, CI environment,
pin). This counts such waiters per (host, compatible target, lease key), so a rule can charge
imbalance only while the target holds nothing under that key: a CUDA-only job never counts
against an AMD host, and a pinned job never counts at all.

Read-only toward the pool: it calls the planner's functions and never places, moves or probes on
its own schedule beyond the planner's probe cache. It recomputes at most once a minute, probes
only when a placed job is queued, and reports a measured zero when none is.
"""
from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

MIN_INTERVAL_S = 60.0
PROBE_CACHE_S = 60.0
_last = {"t": None, "text": ""}


def _lease_key(dem):
    if int(dem.get("gpus") or 0):
        return "gpu"
    if dem.get("cpu_heavy"):
        return "cpu-heavy"
    return None  # a job that takes no host lease never queues for one


def waiting_compatible(now=None):
    """({host: queued placed jobs}, {(host, target, key): count of those `target` could run})."""
    import config
    import pool
    import pool_cli
    now = time.time() if now is None else float(now)
    queued = {}
    waiting = []
    for m in pool_cli.placed_metas():
        if m.get("lease_granted"):
            continue
        if pool_cli.job_state(m) != "queued":
            continue
        pl = m.get("placement") or {}
        here = pl.get("host") or m.get("host") or config.server()
        queued[here] = queued.get(here, 0) + 1
        waiting.append((m, pl, here))
    counts = {}
    if not waiting:
        return queued, counts
    hosts = pool_cli.capacities(pool_cli.gather(cache_s=PROBE_CACHE_S), now)
    for m, pl, here in waiting:
        dem = pl.get("demand") or pool.demand()
        key = _lease_key(dem)
        if key is None:
            continue
        home = pl.get("home") or config.server()
        for h in hosts:
            if h.get("name") != here and not pool.compatible(h, dem, home):
                k = (here, h["name"], key)
                counts[k] = counts.get(k, 0) + 1
    return queued, counts


def _esc(v):
    return str(v).replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


def render(now=None, host=None):
    """Prometheus text for the sampler, recomputed at most every MIN_INTERVAL_S. `host` names this
    host for the measured-zero series when nothing is queued anywhere."""
    now = time.time() if now is None else float(now)
    if _last["t"] is not None and now - _last["t"] < MIN_INTERVAL_S:
        return _last["text"]
    queued, counts = waiting_compatible(now)
    if host and host not in queued:
        queued[host] = 0
    L = ["# HELP shoal_pool_placed_queued Placed jobs queued for their lease on this host.",
         "# TYPE shoal_pool_placed_queued gauge"]
    for h, n in sorted(queued.items()):
        L.append(f'shoal_pool_placed_queued{{host="{_esc(h)}"}} {n}')
    L += ["# HELP shoal_pool_waiting_compatible Queued placed jobs another pool host could run, by lease key.",
          "# TYPE shoal_pool_waiting_compatible gauge"]
    for (h, t, k), n in sorted(counts.items()):
        L.append(f'shoal_pool_waiting_compatible{{host="{_esc(h)}",target="{_esc(t)}",key="{_esc(k)}"}} {n}')
    text = "\n".join(L) + "\n"
    _last.update(t=now, text=text)
    return text
