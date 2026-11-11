"""Adapters that build `role_time` observation records from the ledger clone and from host-local
native session status (architecture doc §9, role time accounting).

`role_time.py` is pure; this module is the impure edge, and it stays read-only toward every role:
it never writes a queue, never wakes a session, never runs a model turn and never calls a wait
provider live (`blockers.check(..., live=False)`: age-only evidence, labelled as such). Two entry
points:

- `shoal role-time native-status --spec S [--watch S]`: probe the sessions that run on THIS host
  and write a small JSON cache (also run from `shoal resources watch --role-time-spec S`). Native
  turn state must be observed where the session runs; another host's cache file is only ever read,
  with its own write timestamp, so a stale or absent cache becomes unknown time, never work.
- `shoal role-time observe --spec S [...] [--watch S] [--prom-dir DIR]`: the home-side integrator.
  Each tick folds the ledger clone (freshness is the clone's last fetch, `.git/FETCH_HEAD` mtime,
  never the newest event), merges local probes with any `--native-cache` files, appends observation
  JSONL for `shoal role-time report`, and feeds the checkpointed `Accumulator`, whose Prometheus
  text is written atomically (temp file plus rename) for a textfile collector.

Population comes from spec and generation history, not from ledger writers: `fill: none` roles are
background unless `features.role_time.foreground` names them, and `features.role_time.background`
can force a role out. Membership lines are append-only facts, so each role's spans are written to
an observation file once, as first seen; after a generation change, start a new observation file
for exact reconciled spans (the live accumulator stays exact either way: it follows current
membership every tick). Attached-but-idle harness viewers are not work: an attached Codex/Muse
client whose durable session file is moving (or unreadable) is reported as the raw state
`attached`, which the classifier charges to unknown, and only a quiet attached client reads as
between turns.
"""
from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import blockers  # noqa: E402
import role_time as rt  # noqa: E402
from ledger import Ledger, View, item_key  # noqa: E402

LEDGER_BUDGET_S = 180.0   # three periods of the existing 120 s ledger pull cadence, plus margin
NATIVE_BUDGET_S = 90.0    # three periods of a 30 s probe tick (subprocess probes included)
CACHE_V = 1


def _epoch(v) -> float | None:
    """Epoch seconds for an ISO-8601 string (Z or offset; naive reads as UTC) or a number."""
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return float(v)
    try:
        t = dt.datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=dt.timezone.utc)
    return t.timestamp()


def ledger_fetched_at(root: str) -> float | None:
    """When the ledger clone last fetched successfully: the FETCH_HEAD mtime, or None when the
    clone carries no fetch record (freshness is then unknown, never assumed)."""
    dot = os.path.join(root, ".git")
    cands = [os.path.join(dot, "FETCH_HEAD")]
    if os.path.isfile(dot):  # a linked worktree: `gitdir: <path>` plus a commondir pointer
        try:
            with open(dot) as fh:
                gd = fh.read().strip().removeprefix("gitdir:").strip()
            cands.append(os.path.join(gd, "FETCH_HEAD"))
            with open(os.path.join(gd, "commondir")) as fh:
                cd = fh.read().strip()
            cands.append(os.path.join(gd, cd, "FETCH_HEAD"))
        except OSError:
            pass
    for p in cands:
        try:
            return os.path.getmtime(p)
        except OSError:
            continue
    return None


def _role_time_cfg(spec) -> dict:
    c = (getattr(spec, "features", None) or {}).get("role_time")
    return c if isinstance(c, dict) else {}


def foreground_roles(spec):
    """(foreground [Role], background {slug}) for accounting: `fill: none` is background, with
    `features.role_time.foreground` / `.background` slug lists as explicit overrides."""
    cfg = _role_time_cfg(spec)
    force_in = set(cfg.get("foreground") or [])
    force_out = set(cfg.get("background") or [])
    fg, bg = [], set()
    for r in spec.roles:
        if r.slug in force_out or (r.fill == "none" and r.slug not in force_in):
            bg.add(r.slug)
        else:
            fg.append(r)
    return fg, bg


def removals(events):
    """{slug: removal time} from the ledger's `role-removed` system events (the latest per role)."""
    out = {}
    for e in events or []:
        if e.get("type") == "system" and e.get("event") == "role-removed" and e.get("role"):
            t = _epoch(e.get("ts"))
            if t is not None:
                out[e["role"]] = t
    return out


def _clip(spans, end):
    """Spans cut at `end`: a span running past it ends there; a span starting at or after it (a
    role added back later) is kept as it is."""
    out = []
    for s, e in spans:
        if s < end and (e is None or e > end):
            out.append((s, end))
        else:
            out.append((s, e))
    return out


def membership_spans(spec, fallback_start: float | None = None, removed=None):
    """{slug: [(start, end-or-None)]} for foreground roles, from generation history: a role is a
    member for each generation whose sessions or seqs name it, from that generation's start to its
    end (or the next generation's start, or the shoal's retirement). Roles no generation names get
    one open span from `fallback_start` (the first ledger event, else now): visible, not invented
    further back than the evidence. A role with a `role-removed` event (`removed`, from `removals`)
    leaves the population at that event."""
    fg, _ = foreground_roles(spec)
    retired = _epoch(getattr(spec, "retired_utc", "") or "")
    gens = list(getattr(spec, "generations", []) or [])
    spans: dict[str, list] = {r.slug: [] for r in fg}
    for i, g in enumerate(gens):
        start = _epoch(g.get("started_utc"))
        if start is None:
            continue
        end = _epoch(g.get("ended_utc"))
        if end is None and i + 1 < len(gens):
            end = _epoch(gens[i + 1].get("started_utc"))
        if end is None:
            end = retired
        members = set(g.get("sessions") or {}) | set(g.get("seqs") or {})
        for slug in members & set(spans):
            if end is None or end > start:
                spans[slug].append((start, end))
    fb = fallback_start if fallback_start is not None else time.time()
    for slug, ss in spans.items():
        if not ss:
            ss.append((fb, retired))
        ss.sort()
        merged = []
        for s, e in ss:  # merge touching or overlapping generation spans into one lifetime
            if merged and (merged[-1][1] is None or s <= merged[-1][1]):
                ps, pe = merged[-1]
                merged[-1] = (ps, None if pe is None or e is None else max(pe, e))
            else:
                merged.append((s, e))
        spans[slug] = _clip(merged, removed[slug]) if removed and slug in removed else merged
    return spans


def _is_member(spans, slug, now) -> bool:
    return any(s <= now and (e is None or now < e) for s, e in spans.get(slug, []))


# ---- the ledger half of a snapshot -------------------------------------------------

def _blocker_records(spec, view, slug, q):
    """Per-item blocker validity for the snapshot: `blockers.check` with `live=False` (no provider
    call from an accounting loop), the expiry the integrator must split at, and the evidence kind.
    Role and ask blockers are judged from the ledger itself; the rest are age-only here."""
    if not q:
        return {}
    cfg = blockers.cfg(spec) or blockers.DEFAULTS
    meta = q.get("next_blocked_meta") or {}
    out = {}
    for item, b in (q.get("next_blocked") or {}).items():
        k = item_key(item)
        b = str(b)
        status, _why = blockers.check(spec, view, slug, q, k, b, meta.get(k) or meta.get(item), live=False)
        since = _epoch((meta.get(k) or meta.get(item) or {}).get("since") or q.get("ts"))
        ttl_min = cfg["hold_ttl_min"] if blockers.kind(b) == "hold" else cfg["peer_ttl_min"]
        out[k] = {"blocker": b, "status": status,
                  "expires_at": (since + ttl_min * 60.0) if since is not None and status == "live" else None,
                  "evidence": "ledger" if blockers.kind(b) in ("role", "ask") else "age-only"}
    return out


def ledger_part(spec, view, slug, fetched_at, budget_s=LEDGER_BUDGET_S):
    """The snapshot's `ledger` source: the role's declared sleep status (last `state` event, else
    its queue version's status) and queue, as of the clone's last fetch. A refill is a recorded
    request, not an empty queue: an open ask on the current queue version is that record."""
    st = view.role_state(slug) or {}
    q = view.current_queue(slug) or {}
    state = str(st.get("state") or q.get("status") or q.get("posture") or "")  # `posture`: queue events before 2026-11-11
    # queue items arrive as `<owner>/<repo>#<n>:<verb>` while blockers are keyed by item key:
    # both sides of the snapshot use item keys, or a blocked item would read as runnable
    nxt = [item_key(str(x)) for x in (q.get("next") or [])]
    role = spec.role(slug)
    fill = str(getattr(role, "fill", "") or "")
    return {"observed_at": fetched_at, "budget_s": float(budget_s), "state": state,
            "sub": str(st.get("sub") or ""), "target": str(st.get("target") or ""),
            "refill_requested": bool(q.get("asks")),
            "active": item_key(str(q.get("active"))) if q.get("active") else "", "next": nxt,
            "blockers": _blocker_records(spec, view, slug, q), "fill": fill,
            "sources": sources_part(spec.sid, slug) if fill == "self" else None,
            "job": job_part(spec.sid, slug, str(st.get("target") or ""))}


def job_part(sid, slug, target):
    """For a `job:<name>` wait target: whether that job was started with placement, whether its
    lease was granted (`lease_granted` in the home meta, or this host's grant marker) and whether it
    has finished (its rc file). For a remote placed job the role's `job wait` supervisor copies the
    remote grant into the home meta, so its absence there means not granted yet (if that supervisor
    died, the lease wait reads long until the role writes a new status)."""
    if not target.startswith("job:"):
        return None
    try:
        import jobs
        m = jobs.job_meta(sid, target[len("job:"):], slug)
    except Exception:  # noqa: BLE001 - a missing or unreadable record is no evidence either way
        return None
    if not m:
        return None
    granted = bool(m.get("lease_granted"))
    if not granted and not m.get("host"):
        granted = os.path.exists(jobs.paths(sid, m.get("unit", ""))["granted"])
    return {"placed": bool(m.get("placement")), "granted": granted,
            "done": bool(m.get("rc_file")) and os.path.exists(m["rc_file"])}


def sources_part(sid, slug):
    """A fill=self role's last recorded priority-table read (`worker.record_sources`), as a source
    with its own freshness budget, or None. Never reads a provider."""
    from roles import state_dir
    try:
        with open(os.path.join(state_dir(sid), f"sources-{slug}.json")) as fh:
            doc = json.load(fh)
        return {"observed_at": float(doc["checked_at"]), "budget_s": float(doc.get("budget_s", 0)),
                "candidates": int(doc["candidates"])}
    except (OSError, ValueError, KeyError, TypeError):
        return None


def lifecycle_part(spec, role, view):
    """Park and planned handover from lifecycle evidence. A parked role needs no native lookup to
    be excluded; `restart_ready` on its last queue version with no session yet is a handover."""
    parked = bool(getattr(role, "parked", None)) and not role.session
    q = view.current_queue(role.slug) or {}
    handover = bool(not role.session and not parked and q.get("restart_ready"))
    return {"parked": parked, "handover": handover, "conflict": "", "session_recorded": bool(role.session)}


# ---- native turn state, probed where the session runs ------------------------------

def _native(state, now, raw="", budget_s=NATIVE_BUDGET_S, observed=True):
    return {"state": state, "observed_at": float(now) if observed else None,
            "budget_s": float(budget_s), "wait_event": "", "raw": str(raw)[:120]}


def _lock_held(path) -> bool:
    """True when another process holds the harness session lock (an exec turn). A missing lock
    file is not held; taking the test lock ourselves proves the opposite, so release at once."""
    if not os.path.exists(path):
        return False
    try:
        with open(path, "a+") as fh:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            fcntl.flock(fh, fcntl.LOCK_UN)
    except OSError:
        return False
    return False


def _match(cbusy, session):
    for k, v in cbusy.items():
        if k == session or (len(k) >= 8 and (k.startswith(session) or session.startswith(k))):
            return v
    return None


def _dormant(sid, role):
    """A Claude background job whose turn ended (`done`, no worker pid) while its ledger watcher is
    alive and armed for this session is between turns, waiting to be woken, not missing: the
    ledger status classifies it. The watcher sidecar is host-local, so this runs where the
    session does and travels in the native cache."""
    import health
    if not sid:
        return False
    alive, sc = health.watcher_alive(sid, role.slug)
    want = sc.get("session_id", "")
    return alive and (not want or want == role.session
                      or (len(want) >= 8 and (want.startswith(role.session) or role.session.startswith(want))))


def _probe_claude(roles, now, sid=""):
    """One harness listing for every local Claude role; `health.claude_liveness` (the job tempo
    file) is the turn signal, never the raw CLI state a background watcher holds at working."""
    import health
    from backends import claude_find
    cbusy = health.claude_busy()
    rows = list(getattr(health, "_CLAUDE_ROWS", []))
    out = {}
    for role in roles:
        if not role.session:
            out[role.slug] = _native("absent", now, "no session in spec")
            continue
        st = _match(cbusy, role.session)
        if st == "busy":
            out[role.slug] = _native("turn", now, "claude tempo active")
        elif st == "idle":
            out[role.slug] = _native("between", now, "claude tempo idle")
        elif st == "blocked":
            row = claude_find(rows, role.session) or {}
            if health.claude_prompt_open(row, role.session):
                out[role.slug] = _native("prompt", now, "claude question open")
            else:
                out[role.slug] = _native("between", now, "claude blocked, no open question")
        elif not cbusy:
            out[role.slug] = _native("unknown", now, "claude listing empty or failed", observed=False)
        elif health.claude_worker_gone(role, cbusy):
            if _dormant(sid, role):
                out[role.slug] = _native("between", now, "claude job done, ledger watcher armed (dormant)")
            else:
                out[role.slug] = _native("absent", now, "claude worker gone")
        else:
            out[role.slug] = _native("not-listed", now, "claude session not listed")
    return out


def _probe_lock_harness(mod, role, now, label):
    """Codex and Muse share a shape: one exec process per turn holds a session lock, and an
    interactive client may merely be attached. The lock with no client is a turn. An attached
    client is never counted as work: quiet (its durable session file has not moved inside the
    harness's active window) is between turns, and moving or unreadable is the raw state
    `attached`, which accounting charges to unknown rather than guessing."""
    session = role.session
    if not session:
        return _native("absent", now, "no session in spec")
    attached = bool(mod.attached_client(session))
    held = _lock_held(os.path.join(mod.sdir(session), "lock"))
    if held and not attached:
        last = mod.last_run(session)
        return _native("turn", now, f"{label} exec lock held since {last.get('started') or '?'}")
    if attached:
        age = mod.activity_age(session)
        window = float(getattr(mod, "ACTIVE_WINDOW_S", 120))
        if age is not None and age > window:
            return _native("between", now, f"{label} attached, session file quiet {age:.0f}s")
        return _native("attached", now,
                       f"{label} attached, activity {'unknown' if age is None else f'{age:.0f}s ago'}")
    if mod.last_run(session) or os.path.isdir(mod.sdir(session)):
        return _native("between", now, f"{label} no lock held")
    return _native("absent", now, f"{label} never run on this host")


def _probe_opencode(spec, role, now):
    from backends import get as get_backend
    if not role.session:
        return _native("absent", now, "no session in spec")
    s = get_backend("opencode").status(role.session, spec.dir or "")
    if not s:
        return _native("unknown", now, "opencode status empty", observed=False)
    if "status:busy" in s:
        return _native("turn", now, s)
    if "status:idle" in s:
        return _native("between", now, s)
    return _native(s.split()[0][:40] if s.split() else "unknown", now, s)


def probe_native(spec, roles, now=None):
    """{slug: native} for the given roles that run on THIS host, plus ({slug: probe_ms},
    {slug: error}). A probe failure is a missing source (unknown time), never a guessed state.
    Roles on other hosts are omitted: their own host's cache is the source."""
    from roles import is_local
    now = time.time() if now is None else float(now)
    local = [r for r in roles if is_local(r, spec)]
    out, ms, errs = {}, {}, {}
    claude_roles = [r for r in local if r.ecosystem == "claude"]
    if claude_roles:
        t0 = time.perf_counter()
        try:
            batch = _probe_claude(claude_roles, now, spec.sid)
            took = (time.perf_counter() - t0) * 1000.0
            for r in claude_roles:
                out[r.slug] = batch[r.slug]
                ms[r.slug] = round(took, 1)
        except Exception as exc:  # noqa: BLE001 - a failed listing is missing evidence, per role
            for r in claude_roles:
                out[r.slug] = _native("unknown", now, f"claude probe failed: {exc}", observed=False)
                errs[r.slug] = str(exc)[:120]
    for role in local:
        if role.ecosystem == "claude":
            continue
        t0 = time.perf_counter()
        try:
            if role.ecosystem == "muse":
                import muse as muse_h
                out[role.slug] = _probe_lock_harness(muse_h, role, now, "muse")
            elif role.ecosystem == "codex":
                import codex_harness as codex_h
                out[role.slug] = _probe_lock_harness(codex_h, role, now, "codex")
            elif role.ecosystem == "opencode":
                out[role.slug] = _probe_opencode(spec, role, now)
            else:
                out[role.slug] = _native(str(role.ecosystem), now, "no native probe for this ecosystem")
        except Exception as exc:  # noqa: BLE001
            out[role.slug] = _native("unknown", now, f"{role.ecosystem} probe failed: {exc}", observed=False)
            errs[role.slug] = str(exc)[:120]
        ms[role.slug] = round((time.perf_counter() - t0) * 1000.0, 1)
    return out, ms, errs


# ---- the per-host native status cache ------------------------------------------------

def default_cache_path(sid):
    from roles import state_dir
    return os.path.join(state_dir(sid), "role-time-native.json")


def _atomic_write(path, text):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = f"{path}.tmp{os.getpid()}"
    with open(tmp, "w") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def write_native_cache(spec, path=None, now=None):
    """Probe this host's sessions for one spec and write the cache atomically. Returns the cache
    document. Only local roles appear: absence from the cache is itself information."""
    import socket
    now = time.time() if now is None else float(now)
    path = path or default_cache_path(spec.sid)
    fg, _bg = foreground_roles(spec)
    natives, ms, errs = probe_native(spec, fg, now)
    doc = {"v": CACHE_V, "shoal": spec.sid, "host": socket.gethostname(), "written_at": now,
           "roles": {slug: {"native": n, "probe_ms": ms.get(slug)} for slug, n in sorted(natives.items())},
           "errors": errs}
    _atomic_write(path, json.dumps(doc, indent=1, sort_keys=True) + "\n")
    return doc


REMOTE_FETCH_TIMEOUT_S = 10


def fetch_remote_natives(spec, roles, dest_dir):
    """Copy each other host's native-status cache for this shoal into `dest_dir` as
    `role-time-native-<server>.json`, over that host's `[hosts.<server>]` route (routes.py). Only
    hosts that run one of `roles` are read; a host's cache sits at the same state-dir path there as
    here. A file read, never a harness probe or provider call: the remote host's own sampler keeps
    it fresh. A failed read keeps the last copy, whose per-role `observed_at` then ages into
    unknown time on its own. Returns {server: read ok}."""
    import shlex
    import subprocess
    import routes
    from roles import host_of, is_local, state_dir
    out = {}
    for server in sorted({host_of(r, spec) for r in roles if not is_local(r, spec)} - {""}):
        r = routes.route(server)
        doc = None
        if r is not None:
            path = os.path.join(state_dir(spec.sid), "role-time-native.json")
            try:
                p = subprocess.run(routes.ssh_argv(r) + ["cat " + shlex.quote(path)], stdin=subprocess.DEVNULL,
                                   capture_output=True, text=True, timeout=REMOTE_FETCH_TIMEOUT_S)
                doc = json.loads(p.stdout) if p.returncode == 0 and p.stdout.strip() else None
            except (OSError, subprocess.TimeoutExpired, ValueError):
                doc = None
        ok = isinstance(doc, dict) and doc.get("v") == CACHE_V and doc.get("shoal") == spec.sid
        if ok:
            _atomic_write(os.path.join(dest_dir, f"role-time-native-{server}.json"),
                          json.dumps(doc, indent=1, sort_keys=True) + "\n")
        out[server] = ok
    return out


def read_native_caches(paths, sid):
    """{slug: native} merged from cache files for one shoal; the freshest `observed_at` wins.
    A corrupt or foreign-shoal file contributes nothing."""
    out: dict[str, dict] = {}
    for p in paths or []:
        try:
            with open(p) as fh:
                doc = json.load(fh)
        except (OSError, ValueError):
            continue
        if doc.get("v") != CACHE_V or doc.get("shoal") != sid:
            continue
        for slug, row in (doc.get("roles") or {}).items():
            n = (row or {}).get("native") or {}
            prev = out.get(slug)
            if prev is None or (n.get("observed_at") or 0) >= (prev.get("observed_at") or 0):
                out[slug] = n
    return out


# ---- the home-side integrator --------------------------------------------------------

def build_snapshots(spec, view, fetched_at, natives, now, seq0=0, ledger_budget_s=LEDGER_BUDGET_S):
    """One snapshot per foreground role at observation time `now`. `natives` maps slug to a native
    source dict; a role with none gets no native source at all, so its time reads unknown."""
    from roles import host_of
    fg, _bg = foreground_roles(spec)
    out = {}
    for i, role in enumerate(fg):
        out[role.slug] = {"t": float(now), "seq": int(seq0) + i, "host": host_of(role, spec),
                          "session_tag": role.tag, "lifecycle": lifecycle_part(spec, role, view),
                          "native": natives.get(role.slug),
                          "ledger": ledger_part(spec, view, role.slug, fetched_at, ledger_budget_s)}
    return out


def _render_extra(acc, snaps, errors, now, remote=None):
    """Source freshness, eligibility and observation-error series beside the accumulator's own
    counters, so a missing or stale source is visible in the same scrape as the time it lost."""
    sh = rt._esc(acc.shoal)
    L = ["# HELP shoal_role_source_timestamp_seconds Last successful read of one role-time source.",
         "# TYPE shoal_role_source_timestamp_seconds gauge"]
    for slug in sorted(snaps):
        for source in ("native", "ledger"):
            src = snaps[slug].get(source) or {}
            if src.get("observed_at") is not None:
                L.append(f'shoal_role_source_timestamp_seconds{{shoal="{sh}",role="{rt._esc(slug)}",'
                         f'source="{source}"}} {float(src["observed_at"]):.3f}')
    L += ["# HELP shoal_role_eligible Role is a member and not currently excluded.",
          "# TYPE shoal_role_eligible gauge"]
    for slug in sorted(acc.counters):
        cur = acc.state.get(slug, ("unknown", "no-observation"))
        L.append(f'shoal_role_eligible{{shoal="{sh}",role="{rt._esc(slug)}"}} '
                 f'{1 if acc.member[slug] and cur[0] != "excluded" else 0}')
    L += ["# HELP shoal_role_observation_errors_total Ticks where one source was missing or failed.",
          "# TYPE shoal_role_observation_errors_total counter"]
    for slug in sorted(acc.counters):
        for source in ("native", "ledger"):
            L.append(f'shoal_role_observation_errors_total{{shoal="{sh}",role="{rt._esc(slug)}",'
                     f'source="{source}"}} {int((errors.get(slug) or {}).get(source, 0))}')
    L += ["# HELP shoal_role_time_exporter_last_success_timestamp_seconds Last completed observe tick.",
          "# TYPE shoal_role_time_exporter_last_success_timestamp_seconds gauge",
          f'shoal_role_time_exporter_last_success_timestamp_seconds{{shoal="{sh}"}} {float(now):.3f}']
    if remote:
        L += ["# HELP shoal_role_remote_cache_ok Last read of another host's native-status cache succeeded.",
              "# TYPE shoal_role_remote_cache_ok gauge"]
        for server, ok in sorted(remote.items()):
            L.append(f'shoal_role_remote_cache_ok{{shoal="{sh}",server="{rt._esc(server)}"}} {1 if ok else 0}')
    return "\n".join(L) + "\n"


# Closed, compressed daily journal segments are kept this many days, then removed.
SEGMENT_RETAIN_DAYS = 365


def _utc_day(t):
    return dt.datetime.fromtimestamp(float(t), dt.timezone.utc).strftime("%Y%m%d")


def _gzip_replace(plain):
    """Compress one closed segment to `<plain>.gz` atomically, then remove the plain file. An
    existing compressed copy of the same day keeps its content and gains a gzip member. A crash
    between the replace and the removal leaves both; readers prefer the compressed copy, and the
    next sweep removes the plain one."""
    import gzip
    import shutil
    gz = plain + ".gz"
    tmp = f"{gz}.tmp{os.getpid()}"
    with open(plain, "rb") as src, open(tmp, "wb") as raw:
        if os.path.exists(gz):
            with open(gz, "rb") as old:
                shutil.copyfileobj(old, raw)
        with gzip.GzipFile(fileobj=raw, mode="ab") as dst:
            shutil.copyfileobj(src, dst)
        raw.flush()
        os.fsync(raw.fileno())
    os.replace(tmp, gz)
    os.unlink(plain)


class Observer:
    """One shoal's live integrator: ledger fold plus native sources in, observation JSONL and
    Prometheus text out. State beside the observation file (append counter, membership as written,
    observation-error counts, the open segment's day) and the accumulator checkpoint survive
    restarts; the accumulator itself charges any unobserved restart interval to unknown.

    The journal rotates at the UTC day boundary: the live file becomes the closed segment
    `<stem>-YYYYMMDD.jsonl`, is compressed, and a new live file starts with a segment header
    (policy version) and the full membership. Compressed segments older than `retain_days` are
    removed. `role-time report --input <live file>` reads the segments and the live file together."""

    def __init__(self, spec_path, obs_path=None, checkpoint_path=None, prom_path=None,
                 cache_paths=(), probe=True, pull=False, pull_interval_s=120.0,
                 ledger_budget_s=LEDGER_BUDGET_S, state_root=None, retain_days=SEGMENT_RETAIN_DAYS,
                 remote=True):
        from roles import ShoalSpec, state_dir
        self.spec_path = spec_path
        spec0 = ShoalSpec.load(spec_path)
        self.sid = spec0.sid
        # a deployment with a durable state root keeps observations and the checkpoint out of any
        # scratch area; the default is the shoal's usual state directory
        base = os.path.join(state_root, self.sid) if state_root else state_dir(self.sid)
        self.obs_path = obs_path or os.path.join(base, "role-time-observations.jsonl")
        self.checkpoint_path = checkpoint_path or os.path.join(base, "role-time-accumulator.json")
        self.state_path = self.obs_path + ".state.json"
        self.prom_path = prom_path
        self.cache_paths = list(cache_paths or [])
        self.probe = probe
        self.pull = pull
        self.pull_interval_s = float(pull_interval_s)
        self.ledger_budget_s = float(ledger_budget_s)
        self.retain_days = float(retain_days)
        self.remote = remote
        self.remote_ok = {}
        self.acc = None
        self.last_pull = 0.0
        self.state = self._load_state()

    def _first_day(self):
        """The UTC day of the live file's first snapshot (a journal written before rotation
        existed spans several days and is named for its first)."""
        try:
            with open(self.obs_path) as fh:
                for line in fh:
                    try:
                        d = json.loads(line)
                    except ValueError:
                        continue
                    if d.get("type") == "snapshot" and d.get("t") is not None:
                        return _utc_day(d["t"])
        except OSError:
            pass
        return None

    def _sweep(self, today):
        """Compress closed segments left plain (a crash mid-rotation) and apply retention."""
        import glob
        stem = self.obs_path[:-len(".jsonl")] if self.obs_path.endswith(".jsonl") else self.obs_path
        cutoff = _utc_day(dt.datetime.strptime(today, "%Y%m%d").replace(tzinfo=dt.timezone.utc).timestamp()
                          - self.retain_days * 86400)
        for p in sorted(glob.glob(glob.escape(stem) + "-*.jsonl*")):
            day = rt._segment_day(p)
            if day is None:
                continue
            if p.endswith(".jsonl"):
                if os.path.exists(p + ".gz"):
                    os.unlink(p)  # the compressed copy is complete
                elif day < today:
                    _gzip_replace(p)
                    p += ".gz"
            if p.endswith(".gz") and day < cutoff:
                os.unlink(p)

    def _rotate(self, now):
        """Close the live segment when the UTC day changes. Returns True when a new segment
        starts, so the caller writes its header and full membership."""
        today = _utc_day(now)
        live = os.path.exists(self.obs_path) and os.path.getsize(self.obs_path) > 0
        day = self.state.get("segment_day") or (self._first_day() if live else None) or today
        # a live segment written under another classification policy closes at once, so its
        # header keeps describing every snapshot in it (reports replay each under its own policy)
        if live and (day != today or self.state.get("segment_policy") != rt.POLICY_VERSION):
            stem = self.obs_path[:-len(".jsonl")] if self.obs_path.endswith(".jsonl") else self.obs_path
            closed = f"{stem}-{day}.jsonl"
            if os.path.exists(closed):  # a second close of the same day (clock step): append
                with open(self.obs_path, "rb") as src, open(closed, "ab") as dst:
                    dst.write(src.read())
                os.unlink(self.obs_path)
            else:
                os.replace(self.obs_path, closed)
            _gzip_replace(closed)  # joins any compressed copy of the same day as a new member
            live = False
        if self.state.get("segment_day") != today or not live:
            self.state["segment_day"] = today
            self._sweep(today)
        if not live:
            self.state["membership"] = {}  # rewritten in full at the head of the new segment
            self.state["segment_policy"] = rt.POLICY_VERSION
            return True
        return False

    def _load_state(self):
        try:
            with open(self.state_path) as fh:
                d = json.load(fh)
            if d.get("v") == 1 and d.get("shoal") == self.sid:
                return d
        except (OSError, ValueError):
            pass
        return {"v": 1, "shoal": self.sid, "seq": 0, "membership": {}, "errors": {}}

    def _save_state(self):
        _atomic_write(self.state_path, json.dumps(self.state, sort_keys=True) + "\n")

    def tick(self, now=None):
        """One observation tick. Returns (snapshots, fetched_at)."""
        from ledger import LedgerError
        from roles import ShoalSpec
        now = time.time() if now is None else float(now)
        spec = ShoalSpec.load(self.spec_path)  # re-read: parks, sessions and generations move
        led = Ledger(spec.sid, root=spec.ledger_dir, sync=self.pull)
        if self.pull and now - self.last_pull >= self.pull_interval_s:
            try:
                led.pull()
                self.last_pull = now
            except LedgerError:
                pass  # keep the local clone; its fetch timestamp now tells the truth about age
        fetched = ledger_fetched_at(spec.ledger_dir)
        view = View(led, spec)
        fg, _bg = foreground_roles(spec)
        cache_paths = list(self.cache_paths)
        if self.remote:  # other hosts' roles: their own caches, copied beside the journal
            base = os.path.dirname(self.obs_path) or "."
            self.remote_ok = fetch_remote_natives(spec, fg, base)
            cache_paths += [os.path.join(base, f"role-time-native-{s}.json") for s in self.remote_ok]
        natives = read_native_caches(cache_paths, spec.sid)
        if self.probe:
            probed, _ms, _errs = probe_native(spec, fg, now)
            for slug, n in probed.items():  # a direct local probe is fresher than any cache
                if slug not in natives or (n.get("observed_at") or 0) >= (natives[slug].get("observed_at") or 0):
                    natives[slug] = n
        first_ts = None
        evs = led.events()
        if evs:
            first_ts = _epoch(evs[0].get("ts"))
        removed = removals(evs)
        spans = membership_spans(spec, fallback_start=first_ts if first_ts is not None else now,
                                 removed=removed)
        seq0 = int(self.state.get("seq", 0))
        snaps = build_snapshots(spec, view, fetched, natives, now, seq0, self.ledger_budget_s)
        errors = self.state.setdefault("errors", {})
        for slug, snap in snaps.items():
            e = errors.setdefault(slug, {"native": 0, "ledger": 0})
            if not snap.get("native") or snap["native"].get("observed_at") is None:
                e["native"] += 1
            if fetched is None:
                e["ledger"] += 1
        d = os.path.dirname(self.obs_path)
        if d:
            os.makedirs(d, exist_ok=True)
        lines = []
        if self._rotate(now):
            lines.append({"type": "segment", "policy": rt.POLICY_VERSION, "shoal": spec.sid,
                          "opened": float(now)})
        written = self.state.setdefault("membership", {})
        # every role this journal has given spans to; a role that has left the spec is closed at
        # its removal event, else at the first tick that no longer finds it (the last membership
        # record for a role wins when segments are read together)
        seen = self.state.setdefault("spans_seen", {k: v for k, v in written.items()})
        for slug in sorted(set(seen) - set(spans)):
            end = removed.get(slug, now)
            rows = [[s, e] for s, e in _clip([(s, e) for s, e in seen.pop(slug)], end) if s < end]
            lines.append({"type": "membership", "role": slug, "spans": rows, "exit": "left-spec"})
            written.pop(slug, None)
        for slug in sorted(spans):
            rows = [[s, e] for s, e in spans[slug]]
            if written.get(slug) != rows:  # in full per segment, and again whenever it changes
                written[slug] = rows
                lines.append({"type": "membership", "role": slug, "spans": rows})
            seen[slug] = rows
        for slug in sorted(snaps):
            lines.append(dict(snaps[slug], type="snapshot", role=slug))
        with open(self.obs_path, "a") as fh:
            for row in lines:
                fh.write(json.dumps(row, sort_keys=True) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        self.state["seq"] = seq0 + len(snaps)
        if self.acc is None:
            self.acc = rt.Accumulator.load(self.checkpoint_path, spec.sid,
                                           [r.slug for r in fg], now=now)
        members = {r.slug for r in fg if _is_member(spans, r.slug, now)}
        for slug in list(self.acc.counters):
            want = slug in members
            if self.acc.member.get(slug, True) != want:
                self.acc.set_member(slug, want, now)
        for slug in sorted(snaps):
            self.acc.ensure(slug, now)
            if slug in members and not self.acc.member.get(slug, True):
                self.acc.set_member(slug, True, now)
            self.acc.observe(slug, snaps[slug])
        self.acc.advance_all(now)
        self.acc.save(self.checkpoint_path)
        self._save_state()
        if self.prom_path:
            text = (self.acc.render_prometheus(expected=members)
                    + _render_extra(self.acc, snaps, errors, now, self.remote_ok))
            _atomic_write(self.prom_path, text)
        return snaps, fetched


# ---- CLI -----------------------------------------------------------------------------

def _positive(v):
    f = float(v)
    if not 0 < f < float("inf"):
        raise argparse.ArgumentTypeError("must be positive and finite")
    return f


def cmd_native_status(a):
    from roles import ShoalSpec
    while True:
        t0 = time.time()
        try:
            spec = ShoalSpec.load(a.spec)
            doc = write_native_cache(spec, a.out or None, t0)
            states = {s: r["native"]["state"] for s, r in doc["roles"].items()}
            print(f"role-time native-status {spec.sid}: {states or 'no local roles'}", flush=True)
        except Exception as exc:  # noqa: BLE001 - a watcher reports and retries, it does not die
            print(f"role-time native-status: {exc}", file=sys.stderr, flush=True)
            if not a.watch:
                return 1
        if not a.watch:
            return 0
        time.sleep(max(0.0, a.watch - (time.time() - t0)))


def cmd_observe(a):
    observers = []
    for spec_path in a.spec:
        from roles import ShoalSpec
        sid = ShoalSpec.load(spec_path).sid
        prom = a.prom_out or (os.path.join(a.prom_dir, f"shoal_role_time_{sid}.prom") if a.prom_dir else None)
        if a.prom_out and len(a.spec) > 1:
            print("role-time observe: --prom-out names one file; use --prom-dir for several specs",
                  file=sys.stderr)
            return 2
        observers.append(Observer(spec_path, prom_path=prom, cache_paths=a.native_cache,
                                  probe=not a.no_probe, pull=a.pull, pull_interval_s=a.pull_interval,
                                  ledger_budget_s=a.ledger_budget,
                                  state_root=a.state_dir or None, retain_days=a.retain_days,
                                  remote=not a.no_remote))
    while True:
        t0 = time.time()
        rc = 0
        for ob in observers:
            try:
                snaps, fetched = ob.tick(t0)
                cls = {s: "/".join(rt.classify(sn, t0)[:2]) for s, sn in sorted(snaps.items())}
                age = "unknown" if fetched is None else f"{t0 - fetched:.0f}s"
                print(f"role-time observe {ob.sid}: ledger fetched {age} ago; {cls}", flush=True)
            except Exception as exc:  # noqa: BLE001 - one bad shoal must not stop the exporter
                print(f"role-time observe {ob.sid}: {exc}", file=sys.stderr, flush=True)
                rc = 1
        if not a.watch:
            return rc
        time.sleep(max(0.0, a.watch - (time.time() - t0)))


def add_subcommands(s):
    """The adapter verbs under `shoal role-time` (registered by role_time.add_parser)."""
    n = s.add_parser("native-status",
                     help="probe this host's native session status and write the per-host cache")
    n.add_argument("--spec", required=True, help="the shoal spec (shoal.json)")
    n.add_argument("--out", default="", help="cache file (default: the shoal state dir)")
    n.add_argument("--watch", type=_positive, default=0, help="repeat every S seconds")
    n.set_defaults(func=cmd_native_status)
    o = s.add_parser("observe",
                     help="fold ledger plus native status into observation JSONL and Prometheus text")
    o.add_argument("--spec", action="append", required=True, help="a shoal spec (repeatable)")
    o.add_argument("--watch", type=_positive, default=0, help="repeat every S seconds")
    o.add_argument("--prom-out", default="", help="Prometheus textfile (one spec only)")
    o.add_argument("--prom-dir", default="", help="write shoal_role_time_<id>.prom per spec here")
    o.add_argument("--native-cache", action="append", default=[],
                   help="a native-status cache file, e.g. another host's (repeatable)")
    o.add_argument("--no-remote", action="store_true",
                   help="do not read other hosts' native-status caches over their [hosts] routes")
    o.add_argument("--no-probe", action="store_true",
                   help="do not probe local sessions directly; caches only")
    o.add_argument("--pull", action="store_true", help="pull the ledger clone on an interval")
    o.add_argument("--pull-interval", type=_positive, default=120.0)
    o.add_argument("--ledger-budget", type=_positive, default=LEDGER_BUDGET_S,
                   help="seconds a ledger fetch may be believed")
    o.add_argument("--state-dir", default="",
                   help="durable root for observations and checkpoints (<dir>/<shoal>/...); "
                        "default: each shoal's state directory")
    o.add_argument("--retain-days", type=_positive, default=SEGMENT_RETAIN_DAYS,
                   help="days closed, compressed daily journal segments are kept")
    o.set_defaults(func=cmd_observe)



