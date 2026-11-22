#!/usr/bin/env python3
"""Azure agent rotation for research reservations (architecture doc §6, "Research reservations").

A research reservation (`shoal pool reserve`) holds a host's device or its cpu-heavy slot out of the CI pool, and the
planner keeps shoal's own placed jobs off it. An Azure Pipelines self-hosted agent on that host is outside shoal: its GPU
job takes the host's leases itself, so it waits on the reserved device until its lease budget runs out and fails
(measured 2026-11-20 and 2026-11-21: two pull request gates failed that way behind an overnight reservation). Azure
admits a GPU job to an agent by demands on the agent's user capabilities, so while any reservation stands on this host:

- **Out of rotation.** shoal removes the configured user capabilities (`[azure_agent] capabilities`, default
  `ci-any-vendor`, the job class an any-vendor gate demands) from this host's agent, so those jobs go to another agent.
  Every reservation counts, a GPU or cpu-heavy one alike: a whole-server job (`--gpus all --cpu-heavy`) waits on any of
  them. What was removed, with its value, is written to `<data_root>/_pool/agent-rotation.json` before Azure is asked
  (a crash between the two leaves a record whose restore is a no-op), and is put back exactly, capability by capability,
  when the last reservation here ends (release, `--until`, a holder that died), only where it is still absent: a value
  the maintainer set meanwhile wins. Only an agent this file recorded changing is ever restored. The state is
  level-triggered: each tick (`pool probe`, every `resources watch` sample) compares "a reservation stands" with the
  record and retries a refused or failed change every `RETRY_S`; a reservation or a release acts at once. An agent that
  is the last enabled one with those capabilities stays in rotation, since its jobs would fail at once rather than wait.
- **Swap.** When every pool host whose agent shoal manages holds a reservation, one agent has to stay in rotation, and it
  should be the one whose host has the most usable free CI capacity: (unreserved GPUs of at least `large_gpu_gib`,
  cpu-heavy free, unreserved GPUs), compared in that order. Each host still changes only its own agent. The `resources
  watch` tick reads the other pool hosts' `pool probe` (every `PEER_REFRESH_S` while a reservation stands here) and every
  host computes the same choice from the same views (keep_choice): the host chosen and out of rotation puts its
  capabilities back (`kept: best`), the agent the last-agent guard kept then leaves on its next try because another
  agent now has them, and the chosen host stays in while the choice stands. The agent already in rotation keeps its
  place unless another host ranks higher on that comparison (no swap on a VRAM difference alone), so repeated ticks
  never flap. A view that is stale or unreadable, or an enabled agent with the capabilities that no pool host reports,
  changes nothing: the rule applies only when every agent that could take the jobs is accounted for.
- **Re-queue.** A build already waiting in this host's lease queue for something a reservation holds (its waiter label
  carries the build id, `waiter_pattern`) and running on this agent is cancelled at its stage and the stage retried, so
  the same build (its pull request status, reason and parameters kept) is assigned again by its demands, to another
  agent. Only when this agent no longer meets the job's demands and another enabled agent does; otherwise (a
  vendor-pinned job, `requeue = false`, no credential) it is left to wait, and its owner told. Each build is handled
  once per out-of-rotation period. Owners are the roles on this host whose wait target names `az-build:<id>`, else
  `user` of the reservation's shoal, told by a ledger message. A build whose cancel was sent is followed up on every
  tick whatever the reservations and the rotation are by then (its stage retried once it reads completed, or it is
  recorded cancelled after CANCEL_SETTLE_S): measured 2026-11-21, a reservation released 15 s after a cancel left the
  build cancelled and never retried, because the follow-up ran only while the agent was out and the release dropped
  the record.

The PAT (`[azure_agent] pat`, a 0600 file) needs Agent Pools (Read & manage) for the capabilities and Build (Read &
execute) for the re-queue. When it is missing or refused the reservation still succeeds; the record says the agent was
NOT taken out of rotation, and `pool status`, `pool reserve` and `doctor` say so. The token is never printed or logged.
"""
from __future__ import annotations

import base64
import contextlib
import datetime
import fcntl
import glob
import json
import os
import re
import stat
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import config  # noqa: E402

RETRY_S = 300            # a refused or failed change is tried again this long after (a reservation or release: at once)
CANCEL_SETTLE_S = 900    # a cancelled stage still not completed by then is left cancelled, and its owner told
PEER_REFRESH_S = 120     # the other pool hosts' probes are read this often while a reservation stands here (swap)
PEER_MAX_AGE_S = 600     # a view of them older than this is no view: nothing is swapped on it
HTTP_TIMEOUT_S = 20
AGENTS_API = "7.0"
BUILD_API = "7.1"
SCOPES = "Agent Pools (Read & manage) and Build (Read & execute)"


class Refused(RuntimeError):
    """The credential: no PAT file, one others can read, or a token Azure refused (401, 403, its sign-in page)."""


class ApiError(RuntimeError):
    def __init__(self, msg: str, code: int = 0):
        super().__init__(msg)
        self.code = code


class Kept(RuntimeError):
    """The agent stays in rotation by design: it is the last one with the capabilities, or (`best`) the swap rule chose
    it to stay in."""

    def __init__(self, msg: str, best: bool = False):
        super().__init__(msg)
        self.best = best


def utc(epoch: float | None = None) -> str:
    return datetime.datetime.fromtimestamp(time.time() if epoch is None else epoch,
                                           datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---- the credential and the REST client ------------------------------------------------------------------------------

def read_pat(path: str) -> str:
    """The token from a file only its owner can read (one others can read is already leaked)."""
    if not path:
        raise Refused("no PAT file configured ([azure_agent] pat)")
    try:
        mode = os.stat(path).st_mode
    except OSError:
        raise Refused(f"no PAT file at {path} ([azure_agent] pat)") from None
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise Refused(f"PAT file {path} is readable by others (mode {stat.filemode(mode)}); chmod 600 it")
    try:
        with open(path) as fh:
            pat = fh.read().strip()
    except OSError as exc:
        raise Refused(f"PAT file {path}: {exc.strerror}") from None
    if not pat:
        raise Refused(f"PAT file {path} is empty")
    return pat


class Api:
    """Azure DevOps REST with a PAT. The token lives only in the Authorization header: no message, repr or log has it."""

    def __init__(self, org: str, pat: str):
        self.org = org.rstrip("/")
        self._auth = "Basic " + base64.b64encode(f":{pat}".encode()).decode()

    def __repr__(self) -> str:
        return f"Api({self.org})"

    def call(self, method: str, path: str, query: dict | None = None, body=None, project: str = ""):
        """`project` empty: an organization-level API (agent pools). A name or an id."""
        base = f"{self.org}/{urllib.parse.quote(project)}" if project else self.org
        url = f"{base}/_apis/{path}?{urllib.parse.urlencode(query or {})}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers={
            "Authorization": self._auth, "Content-Type": "application/json", "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                raise Refused(f"HTTP {exc.code} on {method} {path}") from None
            try:
                why = str(json.loads(exc.read() or b"{}").get("message") or "")[:200]
            except (ValueError, OSError, AttributeError):
                why = ""
            raise ApiError(f"HTTP {exc.code} on {method} {path}" + (f": {why}" if why else ""), exc.code) from None
        except (urllib.error.URLError, OSError) as exc:
            raise ApiError(f"{method} {path}: {getattr(exc, 'reason', exc)}") from None
        try:
            return json.loads(raw) if raw else None
        except ValueError:
            raise Refused(f"{method} {path}: the answer is not JSON (Azure answers a refused token with its sign-in "
                          f"page)") from None


def make_api(cfg: dict) -> Api:
    org = config.azure_org()
    if not org:
        raise Refused("azure_org is unset")
    return Api(org, read_pat(cfg["pat"]))


# ---- state -----------------------------------------------------------------------------------------------------------

def _dir() -> str:
    return os.path.join(config.data_root(), "_pool")


def path() -> str:
    return os.path.join(_dir(), "agent-rotation.json")


def log_path() -> str:
    return os.path.join(_dir(), "agent-rotation.jsonl")


@contextlib.contextmanager
def _locked():
    os.makedirs(_dir(), exist_ok=True)
    with open(os.path.join(_dir(), "agent-rotation.lock"), "a") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        yield


def load_state() -> dict:
    try:
        with open(path()) as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(st: dict) -> None:
    if not st:
        with contextlib.suppress(OSError):
            os.remove(path())
        return
    os.makedirs(_dir(), exist_ok=True)
    with open(path() + ".tmp", "w") as fh:
        json.dump(st, fh, indent=1)
    os.replace(path() + ".tmp", path())


def _log(kind: str, note: str, **fields) -> None:
    """One line in agent-rotation.jsonl and on stderr (stdout stays a command's own output, JSON included)."""
    row = {"ts": utc(), "kind": kind, "note": note, **fields}
    with contextlib.suppress(OSError):
        os.makedirs(_dir(), exist_ok=True)
        with open(log_path(), "a") as fh:
            fh.write(json.dumps(row) + "\n")
    print(f"agent rotation: {kind}: {note}", file=sys.stderr)


def _pending_dir() -> str:
    return os.path.join(_dir(), "holds")


def mark_pending(rid: str) -> None:
    """A reservation still taking its holds counts as standing (its waits behind current holders can be long, and an
    Azure job assigned meanwhile would queue behind it)."""
    os.makedirs(_pending_dir(), exist_ok=True)
    with open(os.path.join(_pending_dir(), f"{rid}.pending"), "w") as fh:
        fh.write(str(os.getpid()))


def clear_pending(rid: str) -> None:
    with contextlib.suppress(OSError):
        os.remove(os.path.join(_pending_dir(), f"{rid}.pending"))


def _pending() -> list[str]:
    out = []
    for p in glob.glob(os.path.join(_pending_dir(), "*.pending")):
        try:
            with open(p) as fh:
                os.kill(int(fh.read().strip()), 0)
            out.append(os.path.basename(p)[:-len(".pending")])
        except (OSError, ValueError):
            with contextlib.suppress(OSError):
                os.remove(p)  # its reserve process is gone
    return out


def _standing() -> list[str]:
    import pool_reserve
    return sorted(set(pool_reserve.load()) | set(_pending()))


# ---- capabilities ----------------------------------------------------------------------------------------------------

def caps_of(agent: dict) -> dict:
    return {**(agent.get("systemCapabilities") or {}), **(agent.get("userCapabilities") or {})}


def _version(v: str) -> tuple:
    return tuple(int(x) if x.isdigit() else 0 for x in re.split(r"[.-]", str(v or "")))


def satisfies(caps: dict, demands: list) -> bool:
    """Does an agent with `caps` meet every demand (`NAME`, `NAME -equals V`, `NAME -gtVersion V`)? An operator this
    does not know never matches, so nothing moves on a guess."""
    low = {str(k).casefold(): v for k, v in caps.items()}
    for d in demands or []:
        parts = str(d).split()
        if not parts:
            continue
        have = low.get(parts[0].casefold())
        if have is None:
            return False
        if len(parts) == 1:
            continue
        op, val = parts[1].lower(), " ".join(parts[2:])
        if op == "-equals":
            if str(have).casefold() != val.casefold():
                return False
        elif op == "-gtversion":
            if _version(have) < _version(val):
                return False
        else:
            return False
    return True


def find_agent(api, cfg: dict, recorded: dict | None = None) -> tuple[int, dict]:
    """(pool id, the agent with its capabilities): the recorded one by id, else this host's by name."""
    q = {"includeCapabilities": "true", "includeAssignedRequest": "true", "api-version": AGENTS_API}
    if recorded and recorded.get("id") and recorded.get("pool_id"):
        pid = int(recorded["pool_id"])
        return pid, api.call("GET", f"distributedtask/pools/{pid}/agents/{int(recorded['id'])}", q) or {}
    pools = api.call("GET", "distributedtask/pools", {"poolName": cfg["pool"], "api-version": AGENTS_API}) or {}
    ids = [p.get("id") for p in pools.get("value") or [] if p.get("name") == cfg["pool"]]
    if not ids:
        raise ApiError(f"no agent pool named {cfg['pool']!r} ([azure_agent] pool)")
    listed = api.call("GET", f"distributedtask/pools/{ids[0]}/agents", dict(q, agentName=cfg["name"])) or {}
    match = [a for a in listed.get("value") or [] if a.get("name") == cfg["name"]]
    if not match:
        raise ApiError(f"no agent named {cfg['name']!r} in pool {cfg['pool']!r} ([azure_agent] name)")
    return int(ids[0]), match[0]


def list_agents(api, pool_id: int) -> list[dict]:
    got = api.call("GET", f"distributedtask/pools/{pool_id}/agents",
                   {"includeCapabilities": "true", "api-version": AGENTS_API}) or {}
    return [a for a in got.get("value") or [] if isinstance(a, dict)]


def _put_caps(api, pool_id: int, agent_id: int, caps: dict) -> dict:
    """PUT the whole user capability map (the API replaces it)."""
    return api.call("PUT", f"distributedtask/pools/{pool_id}/agents/{agent_id}/usercapabilities",
                    {"api-version": AGENTS_API}, caps) or {}


def _take_out(st: dict, cfg: dict, now: float, api, stay: str = "", known=(), chosen: str = "") -> None:
    """Remove the capabilities from this host's agent. `stay` (the swap rule chose this host, and why): keep it in
    instead, unless an agent no view accounts for (`known`: the agent names the views report) has the capabilities.
    `chosen`: another host the rule chose, named when the last-agent guard keeps this one until that one comes in."""
    prev = dict(st.get("removed") or {})
    try:
        api = api or make_api(cfg)
        pool_id, agent = find_agent(api, cfg, st.get("agent"))
        user = dict(agent.get("userCapabilities") or {})
        take = [c for c in cfg["capabilities"] if c in user and c not in prev]
        if take:
            others = [a for a in list_agents(api, pool_id) if a.get("id") != agent.get("id") and a.get("enabled")]
            holders = [o for o in others if all(c in caps_of(o) for c in take)]
            if stay and not [o for o in holders if o.get("name") not in known]:
                raise Kept(stay, best=True)
            if not holders:
                raise Kept(f"no other enabled agent in pool {cfg['pool']} has {', '.join(take)}, so jobs demanding it "
                           f"would fail at once instead of waiting"
                           + (f"; {chosen} has more usable free CI capacity, so it comes back into rotation on its "
                              f"next tick and this agent leaves after it" if chosen else ""))
        st["agent"] = {"id": agent.get("id"), "name": agent.get("name"), "pool_id": pool_id, "pool": cfg["pool"]}
        removed = dict(prev, **{c: user[c] for c in take})
        st.update(removed=removed, out=False, error="removing")
        _save(st)  # write-ahead: the original values are on disk before Azure changes
        if take:
            got = _put_caps(api, pool_id, int(agent["id"]), {k: v for k, v in user.items() if k not in removed})
            left = [c for c in take if c in (got.get("userCapabilities") or {})]
            if "userCapabilities" in got and left:
                raise ApiError(f"the answer still lists {', '.join(left)}")
        st.update(out=True, error="", since_utc=utc(now), retry_ts=0)
        st.pop("kept", None)
        _log("out", f"agent {agent.get('name')} out of rotation for reservation(s) {', '.join(st.get('reservations') or [])}"
             + (f": removed {', '.join(take)}" if take else f": it already lacks {', '.join(cfg['capabilities'])}"),
             agent=agent.get("name"), removed=sorted(removed))
    except Kept as exc:
        st.update(out=False, error=f"kept in rotation: {exc}", retry_ts=now + RETRY_S,
                  kept="best" if exc.best else "last")
        _forget_unapplied(st, prev)
        _log("kept", str(exc), agent=cfg["name"])
    except Refused as exc:
        # a refused GET or PUT changed nothing on the agent: the record keeps only what was removed before
        st.update(out=False, error=f"{exc}; the PAT needs {SCOPES}", retry_ts=now + RETRY_S)
        _forget_unapplied(st, prev)
        _log("not-out", f"agent {cfg['name']} NOT taken out of rotation: {st['error']}", agent=cfg["name"])
    except ApiError as exc:
        # a PUT that failed in flight may or may not have applied: the write-ahead record stays (its restore only adds
        # back a capability that is absent)
        st.update(out=False, error=str(exc), retry_ts=now + RETRY_S)
        _log("not-out", f"agent {cfg['name']} NOT taken out of rotation: {exc}", agent=cfg["name"])


def _forget_unapplied(st: dict, prev: dict) -> None:
    st["removed"] = prev
    if not prev:
        st.pop("removed", None)


def _restore(st: dict, cfg: dict, now: float, api) -> bool:
    """Put back what the record says was removed, where it is still absent. True when the record is done."""
    ag, removed = st.get("agent") or {}, dict(st.get("removed") or {})
    if not removed or not ag.get("id"):
        return True
    try:
        api = api or make_api(cfg)
        try:
            agent = api.call("GET", f"distributedtask/pools/{int(ag['pool_id'])}/agents/{int(ag['id'])}",
                             {"includeCapabilities": "true", "api-version": AGENTS_API}) or {}
        except ApiError as exc:
            if exc.code == 404:
                _log("restore-skipped", f"agent {ag.get('name')} is gone from pool {ag.get('pool')}: nothing to restore",
                     agent=ag.get("name"))
                return True
            raise
        user = dict(agent.get("userCapabilities") or {})
        back = {c: v for c, v in removed.items() if c not in user}
        if back:
            got = _put_caps(api, int(ag["pool_id"]), int(ag["id"]), dict(user, **back))
            missing = [c for c in back if c not in (got.get("userCapabilities") or {})]
            if "userCapabilities" in got and missing:
                raise ApiError(f"the answer lacks {', '.join(missing)}")
        kept = sorted(set(removed) - set(back))
        _log("restored", f"agent {ag.get('name')} back in rotation: restored {', '.join(sorted(back)) or 'nothing'}"
             + (f" ({', '.join(kept)} already set again, kept as found)" if kept else ""),
             agent=ag.get("name"), restored=sorted(back))
        return True
    except (Refused, ApiError) as exc:
        st.update(error=f"{exc}" + (f"; the PAT needs {SCOPES}" if isinstance(exc, Refused) else ""),
                  retry_ts=now + RETRY_S)
        _log("not-restored", f"agent {ag.get('name')} NOT restored yet: {st['error']} (retried every "
             f"{RETRY_S // 60} min)", agent=ag.get("name"))
        return False


def _come_in(st: dict, cfg: dict, now: float, api, why: str, known) -> None:
    """The swap rule chose this host while its agent is out: put the capabilities back (where still absent) although a
    reservation stands, unless an enabled agent no view accounts for has them (then the rule does not apply)."""
    try:
        api = api or make_api(cfg)
        pool_id, agent = find_agent(api, cfg, st.get("agent"))
        others = [a for a in list_agents(api, pool_id) if a.get("id") != agent.get("id") and a.get("enabled")]
        alien = sorted(o.get("name") or "?" for o in others if o.get("name") not in known
                       and all(c in caps_of(o) for c in cfg["capabilities"]))
    except (Refused, ApiError) as exc:
        st.update(retry_ts=now + RETRY_S)
        _log("swap-failed", f"agent {cfg['name']} not brought back in for the swap yet: {exc}", agent=cfg["name"])
        return
    if alien:
        st["retry_ts"] = now + RETRY_S
        _log("swap-skipped", f"agent {cfg['name']} stays out: {', '.join(alien)} also has "
             f"{', '.join(cfg['capabilities'])} and no pool host reports it, so its load is unknown", agent=cfg["name"])
        return
    if _restore(st, cfg, now, api):
        st.pop("removed", None)
        st.update(out=False, kept="best", error=f"kept in rotation: {why}", since_utc=utc(now), retry_ts=now + RETRY_S)
        _log("in-for-swap", f"agent {cfg['name']} back in rotation while reserved: {why}", agent=cfg["name"])


def _moving(st: dict) -> list[str]:
    """Builds whose stage cancel was sent and whose retry is still to come."""
    return [b for b, v in (st.get("builds") or {}).items() if isinstance(v, dict) and v.get("step") == "cancel-sent"]


def _carry(st: dict) -> dict:
    """What outlives an out-of-rotation period: the builds still to be retried (and their back-off)."""
    keep = {b: st["builds"][b] for b in _moving(st)}
    if not keep:
        return {}
    return {"builds": keep, **({"sweep_retry_ts": st["sweep_retry_ts"]} if st.get("sweep_retry_ts") else {})}


def reconcile(now: float | None = None, force: bool = False, api=None, peers: list | None = None) -> dict:
    """Bring the agent's rotation in line with "a reservation stands here" (standing reservations and ones still taking
    their holds), and with the swap rule while the other pool hosts' views (`peers`, else those recorded within
    PEER_MAX_AGE_S) account for every agent. A change refused or failed is retried after RETRY_S, or at once with
    `force`. Returns the record."""
    cfg = config.azure_agent()
    now = time.time() if now is None else now
    if not cfg["enabled"] and not os.path.exists(path()):
        return {}
    with _locked():
        st = load_state()
        standing = _standing() if cfg["enabled"] else []
        due = force or now >= float(st.get("retry_ts") or 0)
        st["reservations"] = standing
        if peers is not None:
            st["peers"] = {"ts": now, "hosts": peers}
        if standing:
            views = _views(st, cfg, standing, now)
            keep, why = keep_choice(views) if views else ("", "")
            me = config.server()
            known = {v.get("agent") for v in views if v.get("agent")}
            unknown = any(not v.get("fresh") for v in views)
            if not st.get("out"):
                if due and not (unknown and st.get("kept") == "best"):  # an unreadable view moves nothing
                    _take_out(st, cfg, now, api, stay=why if keep == me else "", known=known,
                              chosen=keep if keep and keep != me else "")
            elif keep == me and due and st.get("removed"):  # only what shoal removed ever comes back
                _come_in(st, cfg, now, api, why, known)
        elif st.get("removed"):
            if due and _restore(st, cfg, now, api):
                st = _carry(st)
        else:
            st = _carry(st)  # nothing was changed on the agent: a failure record has nothing left to say
        _save(st)
    return st


# ---- the swap: which agent stays in rotation when every pool host is reserved ----------------------------------------

def host_view(host: str, agent: str, gpus: list, reserved_devices, reserved_cpu: bool, reserved: bool,
              in_rotation: bool, large_gib: float, fresh: bool = True) -> dict:
    """One host as the swap rule sees it. `gpus`: dicts with `uuid` and `vram_gib` (or `vram_total_mib`)."""
    held = {str(d) for d in reserved_devices or ()}
    free = []
    for g in gpus or []:
        if not isinstance(g, dict) or not g.get("uuid") or str(g["uuid"]) in held:
            continue
        gib = g.get("vram_gib")
        if gib is None and isinstance(g.get("vram_total_mib"), (int, float)):
            gib = float(g["vram_total_mib"]) / 1024
        free.append(round(float(gib), 1) if isinstance(gib, (int, float)) else 0.0)
    free.sort(reverse=True)
    return {"host": host, "agent": agent, "fresh": fresh, "reserved": bool(reserved), "in": bool(in_rotation),
            "free_gib": free, "large": sum(1 for x in free if x + 1e-9 >= large_gib), "cpu_free": not reserved_cpu}


def _rank(v: dict) -> tuple:
    """The comparison a swap needs a host to win: large free GPUs, cpu-heavy free, free GPUs."""
    return (int(v.get("large") or 0), int(bool(v.get("cpu_free"))), len(v.get("free_gib") or []))


def _says(v: dict) -> str:
    free = v.get("free_gib") or []
    return (f"{v.get('host')} {len(free)} unreserved GPU{'' if len(free) == 1 else 's'}"
            + (f" ({', '.join(f'{x:g}' for x in free)} GiB)" if free else "")
            + ("" if v.get("cpu_free") else ", cpu-heavy reserved"))


def keep_choice(views: list[dict]) -> tuple[str, str]:
    """(the host whose agent stays in rotation, why), or ("", why) when the rule does not apply: fewer than two hosts,
    a view that is not fresh, or a host with no reservation (every reserved host then leaves, as before). Every host
    computes it from the same views, so they agree. An agent already in rotation keeps its place unless another host
    ranks higher (`_rank`); among equals the larger free GPU, then the name, decides."""
    if len(views) < 2:
        return "", "no other pool host's agent to compare with"
    stale = [str(v.get("host")) for v in views if not v.get("fresh")]
    if stale:
        return "", f"no fresh view of {', '.join(stale)}"
    free = [str(v.get("host")) for v in views if not v.get("reserved")]
    if free:
        return "", f"{', '.join(free)} holds no reservation"
    best = max(_rank(v) for v in views)
    incumbents = [v for v in views if v.get("in") and _rank(v) == best]
    pick = min(incumbents or views, key=lambda v: (tuple(-x for x in _rank(v)),
                                                   -max(v.get("free_gib") or [0.0]), str(v.get("host"))))
    rest = "; ".join(_says(v) for v in views if v is not pick)
    return str(pick["host"]), (f"every pool host holds a reservation, and {pick['host']} has the most usable free CI "
                               f"capacity ({_says(pick)}; {rest})")


def _my_view(cfg: dict, st: dict, standing: list) -> dict:
    import pool_cli
    import pool_reserve
    host = config.server()
    gpus = (pool_cli._latest_sample(host) or {}).get("gpus") or []
    if not gpus:
        import resource_usage
        gpus, _errors = resource_usage.gpu_observations()
    resv = list(pool_reserve.load().values())
    devices = {str(d.get("uuid")) for r in resv for d in r.get("devices") or [] if isinstance(d, dict) and d.get("uuid")}
    return host_view(host, (st.get("agent") or {}).get("name") or cfg["name"], gpus, devices,
                     any(r.get("cpu") for r in resv), bool(standing), not st.get("out"), cfg["large_gpu_gib"])


def _views(st: dict, cfg: dict, standing: list, now: float) -> list[dict]:
    """This host and the recorded peer views, or [] when there are none recent enough (or the swap is off)."""
    p = st.get("peers") or {}
    hosts = [h for h in p.get("hosts") or [] if isinstance(h, dict)]
    if not cfg.get("swap") or not hosts or now - float(p.get("ts") or 0) > PEER_MAX_AGE_S:
        return []
    return [_my_view(cfg, st, standing)] + hosts


def _peer_probes(now: float, names: list[str]) -> dict[str, dict]:
    """{host: its `pool probe`} over the hosts' routes (never this host's own: that would tick here again)."""
    import pool_cli
    return {n: pool_cli.remote_probe(n) for n in names}


def peer_views(probes: dict[str, dict], now: float, large_gib: float) -> list[dict]:
    """The other pool hosts as the swap rule sees them. A failed or stale probe is a view that is not fresh (nothing is
    swapped on it); a host whose agent shoal does not manage is left out, so its agent, if it has the capabilities, is
    one no view accounts for and the rule does not apply."""
    import pool
    out = []
    for n, p in probes.items():
        cap = pool.host_capacity(p, now, name=n)
        if not cap["fresh"]:
            out.append({"host": n, "fresh": False, "why": "; ".join(cap["reasons"])[:200]})
            continue
        rot = cap.get("agent_rotation") or {}
        if not rot.get("agent"):
            continue
        out.append(host_view(n, str(rot["agent"]), cap["gpus"], cap["reserved_devices"], cap["reserved_cpu"],
                             bool(cap["reservations"] or rot.get("reservations")), not rot.get("out"), large_gib))
    return out


def _fetch_peers(now: float) -> list | None:
    """The other pool hosts' views, when a reservation stands here and the recorded ones are older than PEER_REFRESH_S;
    None when nothing is to be read."""
    cfg = config.azure_agent()
    if not cfg["enabled"] or not cfg["swap"] or not _standing():
        return None
    if 0 <= now - float((load_state().get("peers") or {}).get("ts") or 0) < PEER_REFRESH_S:
        return None
    import pool_cli
    me = config.server()
    names = [n for n in pool_cli.pool_names() if n != me]
    if not names:
        return None
    return peer_views(_peer_probes(now, names), now, cfg["large_gpu_gib"])


# ---- builds waiting here on a reserved resource ----------------------------------------------------------------------

def blocked_builds(lease: dict, devices: set, cpu: bool, pattern: str) -> dict[int, str]:
    """{build id: waiter} for Azure builds queued in this host's lease queue for something a reservation holds: a
    whole-server want while any device is reserved, a one-of want whose every candidate is reserved, cpu-heavy while
    it is reserved."""
    rx = re.compile(pattern)
    out: dict[int, str] = {}
    for row in (lease or {}).get("queue") or []:
        if not isinstance(row, dict):
            continue
        m = rx.match(str(row.get("waiter") or ""))
        if not m:
            continue
        key = str(row.get("key") or "")
        want = [str(w) for w in row.get("want") or [] if str(w) != "cpu-heavy"]
        if key == "cpu-heavy":
            hit = cpu
        elif key == "gpu":
            hit = bool(devices) and (not want or "all" in want or all(w in devices for w in want))
        elif key.startswith("gpu@"):
            hit = key[4:] in devices
        else:
            hit = False
        if hit:
            out[int(m.group(1))] = str(row.get("waiter"))
    return out


def requeue_decision(build_id: int, agent: dict, others: list[dict], cfg: dict) -> tuple[str, str]:
    """("requeue" | "leave", why) for a build waiting here on a reserved resource. Re-queue only this agent's own job,
    only when the agent no longer meets its demands (else a retry lands here again) and another enabled agent does."""
    name = agent.get("name") or cfg["name"]
    req = agent.get("assignedRequest") or {}
    owner = (req.get("owner") or {}).get("id")
    if owner != build_id:
        return "leave", f"agent {name} is not running it (its job is {'build ' + str(owner) if owner else 'none'})"
    if not cfg["requeue"]:
        return "leave", "[azure_agent] requeue = false"
    demands = req.get("demands") or []
    if satisfies(caps_of(agent), demands):
        return "leave", f"agent {name} still meets its demands, so a retry would land here again"
    fit = sorted(o.get("name") or "?" for o in others
                 if o.get("enabled") and o.get("id") != agent.get("id") and satisfies(caps_of(o), demands))
    if not fit:
        return "leave", f"no other enabled agent meets its demands ({'; '.join(demands)})"
    return "requeue", f"{', '.join(fit)} meet{'s' if len(fit) == 1 else ''} its demands"


def _stage_of(api, project: str, build_id: int, agent_name: str) -> str:
    """The stage (its reference name) of the build's job in progress on this agent, from the timeline."""
    tl = api.call("GET", f"build/builds/{build_id}/timeline", {"api-version": BUILD_API}, project=project) or {}
    recs = {r.get("id"): r for r in tl.get("records") or [] if isinstance(r, dict)}
    for r in recs.values():
        if r.get("type") == "Job" and r.get("workerName") == agent_name and r.get("state") == "inProgress":
            p, hops = recs.get(r.get("parentId")), 0
            while p is not None and p.get("type") != "Stage" and hops < 8:
                p, hops = recs.get(p.get("parentId")), hops + 1
            if p is not None:
                return str(p.get("identifier") or p.get("name") or "")
    return ""


def _stage_state(api, project: str, build_id: int, stage: str) -> str:
    tl = api.call("GET", f"build/builds/{build_id}/timeline", {"api-version": BUILD_API}, project=project) or {}
    for r in tl.get("records") or []:
        if isinstance(r, dict) and r.get("type") == "Stage" and stage in (r.get("identifier"), r.get("name")):
            return str(r.get("state") or "")
    return ""


def _cancel(api, project: str, build_id: int, stage: str) -> str:
    """Cancel the build's stage (Stages - Update, state cancel); a build whose stage cannot be cancelled is cancelled
    whole. Returns which."""
    try:
        api.call("PATCH", f"build/builds/{build_id}/stages/{urllib.parse.quote(stage)}", {"api-version": BUILD_API},
                 {"state": "cancel", "forceRetryAllJobs": False}, project=project)
        return "stage"
    except ApiError:
        api.call("PATCH", f"build/builds/{build_id}", {"api-version": BUILD_API}, {"status": "cancelling"},
                 project=project)
        return "build"


def _retry(api, project: str, build_id: int, stage: str) -> None:
    """Retry the stage's failed and cancelled jobs (as the web UI's rerun does): the same build, assigned again."""
    api.call("PATCH", f"build/builds/{build_id}/stages/{urllib.parse.quote(stage)}", {"api-version": BUILD_API},
             {"state": "retry", "forceRetryAllJobs": False, "retryDependencies": True}, project=project)


def sweep(now: float | None = None, api=None) -> list[dict]:
    """Act on the Azure builds waiting in this host's lease queue for a reserved resource (blocked_builds): re-queue
    each one the decision allows, tell its owner the outcome. Finding new ones needs a reservation here (and the agent
    out to move them); a build whose cancel was sent is followed up whatever the reservations or the rotation are now,
    so one that ends between the cancel and the retry never strands it. Reads only the local lease queue unless one is
    found or one is mid re-queue. Returns the outcomes recorded this call."""
    cfg = config.azure_agent()
    now = time.time() if now is None else now
    import pool_reserve
    resv = list(pool_reserve.load().values()) if cfg["enabled"] else []
    blocked: dict[int, str] = {}
    if resv and config.lease_cmd():
        import resource_usage
        devices = {str(d.get("uuid")) for r in resv for d in r.get("devices") or []
                   if isinstance(d, dict) and d.get("uuid")}
        cpu = any(r.get("cpu") for r in resv)
        lease = resource_usage.lease_observation(config.lease_cmd())
        blocked = blocked_builds(lease, devices, cpu, cfg["waiter_pattern"])
    if not blocked and not _moving(load_state()):
        return []
    done: list[dict] = []
    with _locked():
        st = load_state()
        builds = st.setdefault("builds", {})
        # a build left only because the agent was not out yet is taken up again once it is
        new = {b: w for b, w in blocked.items() if str(b) not in builds
               or (st.get("out") and builds[str(b)].get("step") == "left-not-out")}
        if not new and not _moving(st):
            return []
        if now < float(st.get("sweep_retry_ts") or 0) and not new:
            return []
        client = [api]

        def azure():
            client[0] = client[0] or make_api(cfg)
            return client[0]

        def record(b, step, why, **kw):
            v = builds.setdefault(str(b), {})
            v.update(step=step, why=why, ts=now, ts_utc=utc(now), **kw)
            done.append(dict(v, build=int(b)))

        st.pop("sweep_retry_ts", None)
        snap = [{k: r.get(k) for k in ("id", "for", "until")} for r in resv]
        try:
            if new and not st.get("out"):
                for b, w in new.items():  # no credential, refused, or kept: nothing can move it; its owner hears why
                    record(b, "left-not-out", f"agent {cfg['name']} is not out of rotation "
                           f"({st.get('error') or 'not yet'})", waiter=w)
            elif new:
                pool_id, agent = find_agent(azure(), cfg, st.get("agent"))
                others = list_agents(azure(), pool_id)
                for b, w in new.items():
                    verdict, why = requeue_decision(b, agent, others, cfg)
                    if verdict == "leave":
                        record(b, "left", why, waiter=w)
                        continue
                    project = str((agent.get("assignedRequest") or {}).get("scopeId") or "")
                    stage = _stage_of(azure(), project, b, agent.get("name") or cfg["name"])
                    if not project or not stage:
                        record(b, "left", "its job's stage could not be read from the build timeline", waiter=w)
                        continue
                    how = _cancel(azure(), project, b, stage)
                    # the reservations are kept with it: its owner is told after they end, if they end first
                    builds[str(b)] = {"step": "cancel-sent", "why": why, "ts": now, "ts_utc": utc(now), "waiter": w,
                                      "project": project, "stage": stage, "cancel": how, "resv": snap}
                    _log("cancel-sent", f"build {b} ({w}) waited here on a reserved resource: {how} cancel sent for "
                         f"stage {stage}, to be retried ({why})", build=b)
        except (Refused, ApiError) as exc:
            st["sweep_retry_ts"] = now + RETRY_S
            _log("sweep-failed", f"builds waiting on a reserved resource not handled yet: {exc}")
        for b in _moving(st):
            v = builds[b]
            late = now - float(v.get("ts") or now) > CANCEL_SETTLE_S
            try:
                if _stage_state(azure(), v["project"], int(b), v["stage"]) == "completed":
                    try:
                        _retry(azure(), v["project"], int(b), v["stage"])
                        record(b, "retried", v.get("why", ""))
                    except ApiError as exc:
                        record(b, "cancelled", f"cancelled, but the retry of stage {v['stage']} failed: {exc}")
                elif late:
                    record(b, "cancelled", f"the cancel of stage {v['stage']} did not complete within "
                           f"{CANCEL_SETTLE_S // 60} min")
            except (Refused, ApiError) as exc:
                if late:
                    record(b, "cancelled", f"stage {v['stage']} could not be read to retry it: {exc}")
                else:
                    st["sweep_retry_ts"] = now + RETRY_S
                    _log("sweep-failed", f"build {b}: its stage {v['stage']} not retried yet: {exc}", build=int(b))
        _save(st)
    for ev in done:
        _log(ev["step"], f"build {ev['build']}: {ev['why']}", build=ev["build"])
        _tell(ev, resv or list(ev.get("resv") or []), cfg)
    return done


def _waiting_roles(build_id: int) -> list[tuple[str, str]]:
    """(shoal, role) of the roles on this host whose wait target is this build."""
    import roles
    rx = re.compile(rf"^az-build:(?:[\w.-]+/)?{int(build_id)}$")
    out = []
    for p in sorted(glob.glob(os.path.join(roles.state_dir("*"), "wait-*"))):
        try:
            with open(p) as fh:
                w = json.load(fh)
        except (OSError, ValueError):
            continue
        if isinstance(w, dict) and rx.match(str(w.get("target") or "")):
            out.append((os.path.basename(os.path.dirname(p)), str(w.get("slug") or os.path.basename(p)[5:])))
    return out


def message(ev: dict, resv: list[dict], cfg: dict) -> tuple[str, str]:
    """(ledger msg kind, text) for one build outcome."""
    b, host = ev["build"], config.server()
    ids = ", ".join(sorted(str(r.get("id")) for r in resv))
    until = ", ".join(sorted({str(r.get("until")) for r in resv})) or "unknown"
    head = (f"az-build:{b} was waiting on {host}'s leases for a device that research reservation(s) {ids} hold"
            if ids else f"az-build:{b} was waiting on {host}'s leases for a device a research reservation held")
    if ev["step"] == "retried":
        return "ci-requeued", (f"{head}. shoal took agent {cfg['name']} out of rotation, cancelled stage {ev.get('stage')} "
                               f"and retried it, so the job is assigned again ({ev.get('why')}). Same build id: a wait "
                               f"on it still holds.")
    if ev["step"] == "cancelled":
        return "ci-cancelled", (f"{head}. shoal cancelled it to move it, and it is NOT queued again: {ev.get('why')}. "
                                f"Rerun its failed jobs in Azure (another agent takes them) or push again.")
    return "ci-blocked", (f"{head}, and shoal left it there: {ev.get('why')}. It fails when its lease budget runs out "
                          f"unless the reservation ends first (until {until}).")


def _tell(ev: dict, resv: list[dict], cfg: dict) -> None:
    """The build's owners (roles waiting on it here, else `user` of each reservation's shoal) hear the outcome by a
    ledger message; each reservation's shoal gets a telemetry event. `resv` may be the reservations recorded with the
    build's cancel (they may have ended since), or empty: then only roles waiting on the build hear it."""
    kind, text = message(ev, resv, cfg)
    targets = _waiting_roles(ev["build"]) or sorted({(str(r.get("for") or "/").split("/")[0], "user") for r in resv})
    for sid, to in targets:
        if not sid:
            continue
        try:
            from ledger import SYSTEM_WRITER, Ledger
            led = Ledger(sid)
            with contextlib.suppress(Exception):
                led.pull()
            led.append(SYSTEM_WRITER, "msg", to=to, kind=kind, text=text)
        except Exception as exc:  # noqa: BLE001 - a ledger that cannot take it never stops the sweep
            _log("tell-failed", f"build {ev['build']}: no ledger message to {sid}/{to}: {str(exc)[:160]}")
    import pool_reserve
    for r in resv:
        with contextlib.suppress(Exception):
            pool_reserve._emit(r, kind, f"az-build:{ev['build']} {ev['step']}: {ev.get('why', '')}"[:200])


def tick(now: float | None = None, force: bool = False, peers: bool = False) -> dict:
    """Reconcile, then sweep: what `pool probe`, each `resources watch` sample, a reservation and a release run. Never
    raises: an agent's rotation never stops a reservation, a release or a probe. `peers` (the `resources watch` tick
    only): read the other pool hosts' probes for the swap rule when due. A probe's own tick never does, since another
    host's probe of this one would then probe that host again."""
    try:
        now = time.time() if now is None else now
        view = _fetch_peers(now) if peers else None
        st = reconcile(now, force=force, peers=view)
        if st.get("reservations") or _moving(st):
            sweep(now)
        return st
    except Exception as exc:  # noqa: BLE001
        print(f"agent rotation: error: {str(exc)[:200]}", file=sys.stderr)
        return load_state()


# ---- reporting -------------------------------------------------------------------------------------------------------

def summary() -> dict:
    """This host's agent rotation, for `pool probe` ({} when off and nothing is recorded)."""
    cfg = config.azure_agent()
    st = load_state()
    if not cfg["enabled"] and not st:
        return {}
    return {"agent": (st.get("agent") or {}).get("name") or cfg["name"], "pool": cfg["pool"],
            "capabilities": cfg["capabilities"], "out": bool(st.get("out")), "removed": sorted(st.get("removed") or {}),
            "error": str(st.get("error") or ""), "since_utc": st.get("since_utc") or "",
            "reservations": st.get("reservations") or [], "kept": str(st.get("kept") or ""),
            "builds": {b: {k: v.get(k) for k in ("step", "why", "ts_utc", "stage")}
                       for b, v in (st.get("builds") or {}).items()}}


def describe(s: dict) -> str:
    """One line for `pool reserve`, `pool release`, `pool status` and `doctor` ("" when there is nothing to say)."""
    if not s:
        return ""
    name = s.get("agent") or "?"
    if s.get("removed") and not s.get("reservations"):
        return f"azure agent {name} NOT restored yet ({', '.join(s['removed'])}): {s.get('error') or 'pending'}"
    if s.get("out"):
        what = ", ".join(s.get("removed") or []) or f"nothing: it already lacked {', '.join(s.get('capabilities') or [])}"
        return (f"azure agent {name} OUT of rotation since {s.get('since_utc')} (removed {what}); restored when the "
                f"last reservation here ends")
    if s.get("reservations") and s.get("kept") == "best":
        why = str(s.get("error") or "").removeprefix("kept in rotation: ")
        return (f"azure agent {name} kept IN rotation while reserved (swap): {why}. Azure GPU jobs land here on its "
                f"unreserved devices")
    if s.get("reservations"):
        return (f"azure agent {name} NOT out of rotation: {s.get('error') or 'not yet'}. Azure GPU jobs can still "
                f"land here and wait on the reservation")
    return ""


def doctor_lines() -> list[tuple[str, str]]:
    """(level, text) for `shoal doctor`: ok, info or warn. Reads files only: no Azure call."""
    cfg = config.azure_agent()
    if not config.pool_member():
        return [("info", "off: this host is not in the pool (pool = false), so it takes no reservations")]
    if not cfg["enabled"]:
        st = load_state()
        if st.get("removed"):
            return [("warn", describe(summary()) or "a removal is recorded while the feature is off")]
        why = "azure_org is unset" if not config.azure_org() else "[azure_agent] enabled = false or no capabilities"
        return [("info", f"off ({why}): a research reservation here leaves any Azure agent in rotation")]
    out = []
    try:
        read_pat(cfg["pat"])
        out.append(("ok", f"agent {cfg['name']} in pool {cfg['pool']}: a research reservation here removes "
                          f"{', '.join(cfg['capabilities'])} (PAT file {cfg['pat']}; it needs {SCOPES})"))
    except Refused as exc:
        out.append(("warn", f"{exc}: a research reservation here will NOT take agent {cfg['name']} out of rotation, so "
                            f"Azure GPU jobs can land here and wait on it (give a PAT with {SCOPES})"))
    line = describe(summary())
    if line:
        s = summary()
        out.append(("info" if s.get("out") else "warn", line))
    return out
