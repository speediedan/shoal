#!/usr/bin/env python3
"""Research reservations: leases held for one role's research, taken out of the CI pool
(architecture doc §6, "Pool placement").

    shoal pool reserve [--host H] --resources gpu@<uuid>,...|gpus:N|cpu-heavy --for <shoal>/<role> \
        --until <ISO|job:NAME> [--note TEXT] [--timeout-min M]
    shoal pool release <id> [--host H]

A reservation is one or more held leases, taken with the host's lease helper in background-holder mode
(`--hold --pidfile`): one device per hold (`gpu@<uuid>` names it, `gpus:N` takes N best-fit devices),
and `cpu-heavy` as its own hold, so one GPU can be kept for research while the rest of the host stays in
the CI pool. Reservations are recorded in a host-local file (`<data_root>/_pool/reservations.json`),
reported by `pool probe`, and released by `pool release`, at `--until` (a time, or the end of the
owner's job of that name), or when their holder dies. The planner treats reserved resources as
unavailable to CI work; a `research` job placed with `--place` runs only inside a reservation its role owns.
While any reservation stands (or is still taking its holds), this host's Azure agent is out of rotation and a
build already waiting here on a reserved resource is re-queued (agent_rotation.py).

All or nothing (measured 2026-11-25: two queued `gpus:2` requests each took one device as devices freed one at a
time, then each waited for the other forever). A request is recorded at once as `waiting` and holds NOTHING while
it waits: it watches the helper's status until every resource it wants is free, then takes each with a zero-wait
hold (the pick, the takes and the commit under an flock on `_pool/acquire.lock`, so two requests never interleave
their takes); if any one is not granted, the ones taken are released at once and it goes back to watching. So no
request ever holds a device while it waits for another. Requests keep a fair order, as the helper's own queue does:
one does not try while an earlier request still waiting here wants any of the same (`gpus:N` overlaps every GPU
request), so later small requests cannot keep taking devices from under an earlier larger one. A device is never handed to two reservations: the pick skips every device another live
reservation lists (even when the helper reports it free), and the grant is committed under the reservations lock
only when no other live record names one of its devices. `pool release <id>` on a waiting request cancels it (its
`pool reserve` exits RESERVE-CANCELLED, so a launcher chained on it stops) and leaves nothing behind.

A hold outlives its caller: it runs in its own `systemd-run --user --scope` where one is available (measured
2026-11-25: a hold taken from inside a launcher job unit died with that unit seconds later, the reservation was
dropped as "holder gone", and the next reservation was granted the same device while the first job still ran on
it). A reservation whose holder is gone, or whose `--until` time passed, is kept while a job placed in it (its
`job:<name>`, or one recorded by `pool attach`) still runs on this host: the hold is taken again where it can be,
and the device stays out of every other reservation until that job ends.
"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import glob
import json
import os
import re
import secrets
import shlex
import signal
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import config  # noqa: E402
import pool  # noqa: E402

DEFAULT_TIMEOUT_MIN = 30
POLL_S = 5.0          # a waiting request re-reads the helper's status this often (SHOAL_POOL_POLL_S)
CANCEL_WAIT_S = 30.0  # `pool release` of a waiting request waits this long for its `pool reserve` to exit


class Cancelled(ValueError):
    """A waiting request was cancelled (`pool release`, or a signal to its `pool reserve`)."""


def _dir() -> str:
    return os.path.join(config.data_root(), "_pool")


def path() -> str:
    return os.path.join(_dir(), "reservations.json")


def _hdir() -> str:
    return os.path.join(_dir(), "holds")


def _poll_s() -> float:
    try:
        return max(0.05, float(config.env("POOL_POLL_S", str(POLL_S))))
    except ValueError:
        return POLL_S


@contextlib.contextmanager
def _locked():
    os.makedirs(_dir(), exist_ok=True)
    with open(os.path.join(_dir(), "reservations.lock"), "a") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        yield


def _text(p: str) -> str:
    with open(p) as fh:
        return fh.read()


def load() -> dict:
    try:
        d = json.loads(_text(path()))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(d: dict) -> None:
    with open(path() + ".tmp", "w") as fh:
        json.dump(d, fh, indent=1)
    os.replace(path() + ".tmp", path())


def state_of(rec: dict) -> str:
    """`waiting` (taking nothing yet), `cancelling`, or `held`. A record written before states existed is held."""
    return str(rec.get("state") or "held")


def parse_resources(text: str) -> list[dict]:
    """`gpu@<uuid>`, `gpus:N` and `cpu-heavy`, comma separated, as one item per hold."""
    out = []
    for tok in [t.strip() for t in str(text or "").split(",") if t.strip()]:
        if tok.startswith("gpu@") and len(tok) > 4:
            out.append({"kind": "device", "uuid": tok[4:]})
        elif re.fullmatch(r"gpus:\d+", tok) and int(tok[5:]) >= 1:
            out += [{"kind": "gpu"} for _ in range(int(tok[5:]))]
        elif tok == "cpu-heavy":
            out.append({"kind": "cpu"})
        else:
            raise ValueError(f"--resources: {tok!r} is not gpu@<uuid>, gpus:N or cpu-heavy")
    if not out:
        raise ValueError("--resources names nothing")
    if sum(i["kind"] == "cpu" for i in out) > 1:
        raise ValueError("--resources: cpu-heavy once")
    named = [i["uuid"] for i in out if i["kind"] == "device"]
    if len(set(named)) != len(named):
        raise ValueError("--resources: a device is named twice")
    return out


def parse_until(text: str, now: float) -> str:
    t = str(text or "").strip()
    if t.startswith("job:") and len(t) > 4:
        return t
    at = pool._epoch(t)
    if at is None:
        raise ValueError(f"--until {t!r}: an ISO time with a zone (2026-11-02T08:00:00Z) or job:<name>")
    if at <= now:
        raise ValueError(f"--until {t} is already past")
    return t


# ---- holds --------------------------------------------------------------------------------------------

_SCOPE: dict[str, bool] = {}


def scope_available() -> bool:
    """Can a hold run in its own `systemd-run --user --scope` here? SHOAL_HOLD_SCOPE=0 turns it off; otherwise
    one probe per process decides (no user manager, no scope: the hold then lives in its caller's cgroup)."""
    if config.env("HOLD_SCOPE", "auto").lower() in ("0", "off", "no", "false"):
        return False
    if "ok" not in _SCOPE:
        try:
            r = subprocess.run(["systemd-run", "--user", "--scope", "--quiet", "--collect", "true"],
                               capture_output=True, text=True, timeout=20, env=config.tool_env(),
                               stdin=subprocess.DEVNULL)
            _SCOPE["ok"] = r.returncode == 0
        except (OSError, subprocess.SubprocessError):
            _SCOPE["ok"] = False
    return _SCOPE["ok"]


def scope_argv(rid: str, n: int) -> list[str]:
    """The prefix that runs one hold's helper call (and so its background holder) in a scope of its own, outside
    the caller's cgroup: a launcher job unit that ends, or a harness that reaps its tool's processes, then cannot
    take the hold with it."""
    if not scope_available():
        return []
    return ["systemd-run", "--user", "--scope", "--quiet", "--collect",
            f"--unit=shoal-hold-{rid}-{n}-{secrets.token_hex(2)}", f"--description=shoal research hold {rid}"]


def hold_argv(lease_cmd: str, item: dict, pidfile: str, owner: str, gpus: dict, timeout_s: int,
              prefix: list[str] | None = None) -> tuple[list[str], dict]:
    """The helper call that takes one hold, and its environment. A named device is the only candidate
    the helper sees (GPU_LEASE_DEVICES), so its best fit can only pick that one."""
    env = dict(config.tool_env(), GPU_LEASE_PROJECT=f"research:{owner}")
    if item["kind"] == "cpu":
        args = ["--lease", "cpu-heavy"]
    else:
        args = ["--gpus", "1"]
        if item["kind"] == "device":
            g = gpus.get(item["uuid"]) or {}
            env["GPU_LEASE_DEVICES"] = f"{item['uuid']}:{int(g.get('vram_total_mib') or 0)}"
    return (list(prefix or []) + shlex.split(lease_cmd) + args
            + ["--timeout", str(int(timeout_s)), "--hold", "--pidfile", pidfile]), env


def _release_hold(lease_cmd: str, h: dict) -> None:
    if not lease_cmd:
        with contextlib.suppress(OSError, ValueError):
            os.killpg(int(_text(h["pidfile"]).strip()), signal.SIGTERM)
        return
    try:
        subprocess.run(shlex.split(lease_cmd) + ["--release", "--pidfile", h["pidfile"]], capture_output=True,
                       text=True, timeout=60, env=config.tool_env())
    except (OSError, subprocess.SubprocessError):
        pass


def _release_rid_files(rid: str, lease_cmd: str) -> None:
    """Release every hold pidfile a request left under its id (a cancelled or abandoned request, or one of an older
    copy of this code, which took its holds one at a time)."""
    for pf in glob.glob(os.path.join(_hdir(), f"{glob.escape(rid)}-*.pid")):
        _release_hold(lease_cmd, {"pidfile": pf})
        for x in (pf, pf + ".device", pf + ".err", pf + ".ready"):
            with contextlib.suppress(OSError):
                os.remove(x)


def _pid_alive(pidfile: str) -> bool:
    try:
        pid = int(_text(pidfile).strip())
        os.kill(pid, 0)
        return True
    except (OSError, ValueError):
        return False


def _proc_start(pid: int) -> str:
    """A process's start time (clock ticks since boot), so a reused pid is not taken for the request's process."""
    try:
        return _text(f"/proc/{int(pid)}/stat").rsplit(")", 1)[1].split()[19]
    except (OSError, ValueError, IndexError):
        return ""


def _proc_alive(pid, start: str = "") -> bool:
    """Is that process running (not exited and unreaped), and the same one (`start`) when given?"""
    try:
        pid = int(pid)
        if pid <= 0:
            return False
        os.kill(pid, 0)
    except (OSError, ValueError, TypeError):
        return False
    with contextlib.suppress(OSError, IndexError):
        if _text(f"/proc/{pid}/stat").rsplit(")", 1)[1].split()[0] == "Z":
            return False
    return not start or not _proc_start(pid) or _proc_start(pid) == start


def holders_alive(rec: dict) -> bool:
    return bool(rec.get("holds")) and all(_pid_alive(h["pidfile"]) for h in rec["holds"])


def alive(rec: dict) -> bool:
    """A held reservation is alive while its holders are, or while it is kept for a job that still runs on its
    devices (`holder_lost`); a waiting one while its `pool reserve` process is."""
    if state_of(rec) != "held":
        return _proc_alive(rec.get("pid"), str(rec.get("pid_start") or ""))
    return holders_alive(rec) or bool(rec.get("holder_lost"))


BUSY_RC = 75  # the lease helper's "not acquired" (EX_TEMPFAIL): wait and try again; any other failure is final


class HoldRefused(ValueError):
    """The helper refused a hold outright (a bad request, an unusable lease directory): waiting would not help."""


def _take(rid: str, picks: list[dict], lease_cmd: str, owner: str, gpus: dict) -> tuple[list[dict] | None, str]:
    """Each pick with a zero-wait hold, or none: on the first one not granted, the ones taken are released.
    Returns (holds, "") or (None, why); raises HoldRefused when the helper refused rather than found it busy."""
    os.makedirs(_hdir(), exist_ok=True)
    holds = []
    for n, item in enumerate(picks):
        pidfile = os.path.join(_hdir(), f"{rid}-{n}.pid")
        argv, env = hold_argv(lease_cmd, item, pidfile, owner, gpus, 0, scope_argv(rid, n))
        fatal = False
        try:
            r = subprocess.run(argv, capture_output=True, text=True, timeout=120, env=env, stdin=subprocess.DEVNULL)
            ok = r.returncode == 0 and _pid_alive(pidfile)
            fatal = r.returncode not in (0, BUSY_RC)
            why = (r.stderr or r.stdout).strip()[-300:] if not ok else ""
        except (OSError, subprocess.SubprocessError) as exc:
            ok, why, fatal = False, str(exc), True
        dev = ""
        if ok and item["kind"] != "cpu":
            with contextlib.suppress(OSError):
                dev = _text(pidfile + ".device").strip()
            if dev and dev != item["uuid"]:  # the helper granted another device than the one named: not ours to keep
                _release_hold(lease_cmd, {"pidfile": pidfile})
                ok, why = False, f"the helper granted {dev}, not gpu@{item['uuid']}"
        if not ok:
            for h in holds:
                _release_hold(lease_cmd, h)
            what = "cpu-heavy" if item["kind"] == "cpu" else f"gpu@{item['uuid']}"
            last = (why.splitlines() or ["no holder"])[-1][:160]
            if fatal:
                raise HoldRefused(f"the lease helper refused the hold on {what}: {last}")
            return None, f"{what} was not granted at once ({last}); released what was taken"
        hold = {"pidfile": pidfile, "kind": "cpu" if item["kind"] == "cpu" else "device"}
        if item["kind"] != "cpu":
            hold["device"] = dev or item["uuid"]
        holds.append(hold)
    return holds, ""


def pick(items: list[dict], gpus: dict, lease: dict, records: dict, rid: str = "") -> tuple[list[dict] | None, str]:
    """The concrete holds for a request, all of them free now, or (None, why not). Busy: a device or cpu-heavy the
    helper reports held, or wanted by a ticket already queued there (the helper would put that ticket first), or
    listed by another live reservation here (even one the helper reports free: a hold lost while its job still
    runs). `gpus:N` takes the N smallest free devices (the helper's best fit)."""
    busy: dict[str, str] = {}
    whole = cpu_busy = ""
    if lease.get("ok"):
        for row in lease.get("leases") or []:
            if not isinstance(row, dict) or not row.get("held"):
                continue
            key, who = str(row.get("key") or ""), str(row.get("holder") or "an unattributed holder")
            if key == "gpu" and row.get("mode", "exclusive") != "shared":
                whole = who
            elif key.startswith("gpu@"):
                busy[key[4:]] = who
            elif key == "cpu-heavy":
                cpu_busy = who
        for q in lease.get("queue") or []:
            if not isinstance(q, dict):
                continue
            who = f"queued {q.get('waiter') or 'waiter'} (ticket {q.get('ticket') or '?'})"
            if q.get("key") == "gpu":
                for w in q.get("want") or []:
                    if w == "all":
                        whole = whole or who
                    else:
                        busy.setdefault(str(w), who)
            elif q.get("key") == "cpu-heavy":
                cpu_busy = cpu_busy or who
    for r in records.values():
        if not isinstance(r, dict) or r.get("id") == rid or state_of(r) != "held" or not alive(r):
            continue
        who = f"reservation {r.get('id')} ({r.get('for')})"
        for d in r.get("devices") or []:
            busy[str(d.get("uuid"))] = who
        if r.get("cpu"):
            cpu_busy = who
    gpu_items = [i for i in items if i["kind"] != "cpu"]
    if gpu_items and whole:
        return None, f"the whole-server gpu lease is held by {whole}"
    picks, taken = [], set()
    for i in items:
        if i["kind"] == "device":
            if i["uuid"] in busy:
                return None, f"gpu@{i['uuid']} is held by {busy[i['uuid']]}"
            picks.append({"kind": "device", "uuid": i["uuid"]})
            taken.add(i["uuid"])
    n = sum(1 for i in items if i["kind"] == "gpu")
    if n:
        free = sorted((u for u in gpus if u not in busy and u not in taken),
                      key=lambda u: (int((gpus[u] or {}).get("vram_total_mib") or 0), u))
        if len(free) < n:
            held = "; ".join(f"{u} by {w}" for u, w in sorted(busy.items()) if u in gpus)
            return None, f"gpus:{n}: {len(free)} free now" + (f" (held: {held})" if held else "")
        picks += [{"kind": "device", "uuid": u} for u in free[:n]]
    if any(i["kind"] == "cpu" for i in items):
        if cpu_busy:
            return None, f"cpu-heavy is held by {cpu_busy}"
        picks.append({"kind": "cpu"})
    return picks, ""


def _job_rc_paths(rec: dict) -> list[str]:
    until = str(rec.get("until") or "")
    if not until.startswith("job:"):
        return []
    import jobs
    sid, _, role = str(rec.get("for") or "/").partition("/")
    unit = jobs.unit_name(sid, role, until[4:])
    return [jobs.paths(sid, unit)["rc"], jobs.remote_paths(unit, os.path.expanduser("~"))["rc"]]


def _rc_marks(rec: dict) -> dict:
    """{rc path: mtime} of the `--until job:` exit files present when the reservation was granted: a file left by
    an earlier run of the same job name is not that job finishing."""
    out = {}
    for p in _job_rc_paths(rec):
        with contextlib.suppress(OSError):
            out[p] = os.path.getmtime(p)
    return out


def _commit(rec: dict, holds: list[dict], gpus: dict) -> tuple[bool, str]:
    """Record the grant, under the reservations lock, only if the request still stands and no other live
    reservation names one of its devices (or cpu-heavy). (False, why) leaves the holds to the caller to release."""
    import pool_cli
    rid = rec["id"]
    devices = [{"uuid": h["device"], "vendor": (gpus.get(h["device"]) or {}).get("vendor") or ""}
               for h in holds if h.get("device")]
    mine = {d["uuid"] for d in devices}
    cpu = any(h["kind"] == "cpu" for h in holds)
    with _locked():
        d = load()
        cur = d.get(rid)
        if cur is not None and state_of(cur) != "waiting":
            return False, "cancelled"
        for r in d.values():
            if r.get("id") == rid or state_of(r) != "held" or not alive(r):
                continue
            clash = mine & {str(x.get("uuid")) for x in r.get("devices") or []}
            if clash or (cpu and r.get("cpu")):
                what = f"gpu@{sorted(clash)[0]}" if clash else "cpu-heavy"
                return False, f"{what} is held by reservation {r.get('id')} ({r.get('for')})"
        for k in ("pid", "pid_start", "waiting", "waiting_since", "queued_ts"):
            rec.pop(k, None)
        rec.update(state="held", holds=holds, devices=devices, cpu=cpu, granted_utc=pool_cli.utc(),
                   rc_marks=_rc_marks(rec))
        d[rid] = rec
        _save(d)
    return True, ""


def _wants(rec: dict) -> set[str]:
    """What a request competes for: `cpu`, `gpu@<uuid>` for a named device, `gpu` for any (gpus:N)."""
    out = set()
    for tok in [t.strip() for t in str(rec.get("want") or "").split(",") if t.strip()]:
        out.add("cpu" if tok == "cpu-heavy" else tok if tok.startswith("gpu@") else "gpu")
    return out


def _overlap(a: set[str], b: set[str]) -> bool:
    if "cpu" in a and "cpu" in b:
        return True
    ga, gb = {x for x in a if x != "cpu"}, {x for x in b if x != "cpu"}
    return bool(ga and gb) and ("gpu" in ga or "gpu" in gb or bool(ga & gb))


def ahead_of(rec: dict, records: dict) -> str:
    """Fair order, as the helper keeps its own queue: a request does not try while an earlier request still waiting
    here wants any of the same (a gpus:N request overlaps every GPU request), so a later small request cannot keep
    taking devices from under an earlier larger one. Returns why it waits, or ""."""
    me = (float(rec.get("queued_ts") or 0), str(rec.get("id")))
    mine = _wants(rec)
    for r in records.values():
        if not isinstance(r, dict) or r.get("id") == rec.get("id") or state_of(r) != "waiting" or not alive(r):
            continue
        if (float(r.get("queued_ts") or 0), str(r.get("id"))) < me and _overlap(mine, _wants(r)):
            return f"behind the earlier request {r.get('id')} ({r.get('for')}: {r.get('want')})"
    return ""


def _drop(rid: str) -> dict | None:
    with _locked():
        d = load()
        rec = d.pop(rid, None)
        if rec is not None:
            _save(d)
    return rec


def _set_waiting(rid: str, why: str) -> None:
    with _locked():
        d = load()
        if rid in d and state_of(d[rid]) == "waiting":
            d[rid]["waiting"] = why
            _save(d)


def _cancel_requested(rec: dict) -> bool:
    """Has `pool release` asked this waiting request to stop (state `cancelling`)? A record that vanished is written
    back instead: an older copy of `expire` (a `resources watch` started before an upgrade) drops a record with no
    holds as "holder gone", and that is no cancel."""
    with _locked():
        d = load()
        cur = d.get(rec["id"])
        if cur is None:
            d[rec["id"]] = rec
            _save(d)
            return False
    return state_of(cur) == "cancelling"


def _note(progress, text: str) -> None:
    if progress is not None:
        progress(text)


def reserve(resources: str, owner: str, until: str, note: str = "", timeout_min: float = DEFAULT_TIMEOUT_MIN,
            now: float | None = None, progress=None) -> dict:
    """Take the holds, all together, and record the reservation; a request is recorded as `waiting` from the start
    and holds nothing until every resource it wants is granted. Raises ValueError with the reason (Cancelled when a
    `pool release` or a signal ended the wait). `progress(text)` is told when the request has to wait and why."""
    now = time.time() if now is None else now
    if not config.pool_member():
        raise ValueError("this host is not in the pool (pool = false): it takes no reservations")
    import maintenance
    m = maintenance.read_local(now)
    if m is not None:
        raise ValueError(f"{maintenance.describe(m)}: it takes no reservations until the window ends")
    if not re.fullmatch(r"[^/\s]+/[^/\s]+", owner or ""):
        raise ValueError("--for must be <shoal>/<role>")
    items = parse_resources(resources)
    until = parse_until(until, now)
    lease_cmd = config.lease_cmd()
    if not lease_cmd:
        raise ValueError("no lease provider configured on this host (lease_cmd)")
    import pool_cli
    gpus = {g["uuid"]: g for g in pool_cli.probe(now).get("gpus") or [] if g.get("uuid")}
    for i in items:
        if i["kind"] == "device" and i["uuid"] not in gpus:
            raise ValueError(f"gpu@{i['uuid']} is not a device on this host ({', '.join(sorted(gpus)) or 'none'})")
    n_gpu = sum(1 for i in items if i["kind"] != "cpu")
    if n_gpu > len(gpus):
        raise ValueError(f"--resources asks for {n_gpu} GPU(s); this host has {len(gpus)}")
    rid = f"r{time.strftime('%m%d%H%M', time.gmtime(now))}-{secrets.token_hex(2)}"
    rec = {"id": rid, "for": owner, "until": until, "note": note, "host": config.server(),
           "created_utc": pool_cli.utc(now), "state": "waiting", "want": resources, "pid": os.getpid(),
           "pid_start": _proc_start(os.getpid()), "waiting": "starting", "waiting_since": pool_cli.utc(now),
           "queued_ts": time.time(),
           "holds": [], "devices": [], "cpu": False}
    with _locked():
        d = load()
        d[rid] = rec
        _save(d)
    import agent_rotation
    agent_rotation.mark_pending(rid)  # out of rotation before the wait: a job assigned meanwhile queues behind it
    out = None
    try:
        agent_rotation.tick(now, force=True)
        out = _acquire(rec, items, lease_cmd, gpus, timeout_min, progress)
    finally:
        agent_rotation.clear_pending(rid)
        if out is None or out.get("error"):  # failed, cancelled or interrupted: nothing of it is left
            _drop(rid)
            _release_rid_files(rid, lease_cmd)
    agent_rotation.tick(force=True)  # a failed reservation puts the agent back here; a held one sweeps waiting builds
    if out.get("error"):
        raise (Cancelled if out.get("cancelled") else ValueError)(out["error"])
    out["rotation"] = agent_rotation.describe(agent_rotation.summary())
    return out


def _acquire(rec: dict, items: list[dict], lease_cmd: str, gpus: dict, timeout_min: float, progress) -> dict:
    """Wait, holding nothing, until no earlier overlapping request waits here and every resource is free; then take
    them all at once, under acquire.lock (held only for the pick, the takes and the commit)."""
    import resource_usage
    rid = rec["id"]
    deadline = time.time() + float(timeout_min) * 60
    flag = {"why": ""}
    restore = {}
    if threading.current_thread() is threading.main_thread():
        def on_signal(signum, _frame):
            flag["why"] = f"cancelled by signal {signal.Signals(signum).name}"
        for s in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            with contextlib.suppress(ValueError, OSError):
                restore[s] = signal.signal(s, on_signal)
    os.makedirs(_dir(), exist_ok=True)
    said, told = "", False
    try:
        while True:
            if flag["why"] or _cancel_requested(rec):
                return {"error": f"{rid} cancelled before it was granted ({flag['why'] or 'pool release'}); "
                                 "it held nothing", "cancelled": True}
            why = ahead_of(rec, load())
            if not why:
                # the pick, the takes and the commit, one request at a time (a few seconds at most: nothing waits here)
                with open(os.path.join(_dir(), "acquire.lock"), "a") as lockfh:
                    fcntl.flock(lockfh, fcntl.LOCK_EX)
                    picks, why = pick(items, gpus, resource_usage.lease_observation(lease_cmd), load(), rid)
                    if picks is not None:
                        try:
                            holds, why = _take(rid, picks, lease_cmd, rec["for"], gpus)
                        except HoldRefused as exc:
                            return {"error": f"{exc}; nothing is held"}
                        if holds is not None:
                            ok, why = _commit(rec, holds, gpus)
                            if ok:
                                return rec
                            for h in holds:
                                _release_hold(lease_cmd, h)
                            if why == "cancelled":
                                continue
            if why != said:
                said = why
                _set_waiting(rid, why)
                if not told:
                    told = True
                    _note(progress, f"RESERVE-WAITING {rid} on {rec['host']} for {rec['for']} ({rec['want']}): {why}; "
                                    f"it holds nothing while it waits. Cancel with: {config.cli()} pool release {rid} "
                                    f"--host {rec['host']}")
                else:
                    _note(progress, f"{rid} still waiting: {why}")
            if time.time() >= deadline:
                return {"error": f"not granted within {float(timeout_min):g} min ({why}); it held nothing"}
            end = min(deadline, time.time() + _poll_s())
            while time.time() < end and not flag["why"]:
                time.sleep(min(0.2, max(0.0, end - time.time())))
    finally:
        for s, h in restore.items():
            with contextlib.suppress(ValueError, OSError):
                signal.signal(s, h)


def release(rid: str, reason: str = "released") -> dict | None:
    """Release a reservation's holds and drop its record; cancel a request still waiting for its grant (its
    `pool reserve` exits RESERVE-CANCELLED). None when there is no such reservation or request here."""
    lease_cmd = config.lease_cmd()
    with _locked():
        d = load()
        rec = d.get(rid)
        if rec is not None and state_of(rec) == "held":
            d.pop(rid)
            for h in rec.get("holds") or []:
                _release_hold(lease_cmd, h)
            _save(d)
        elif rec is not None:
            rec["state"] = "cancelling"  # the waiting process sees this at its next look and gives up
            _save(d)
    if rec is None:
        rec = _cancel_unrecorded(rid, lease_cmd)
        if rec is None:
            return None
        reason = rec["released"]
    elif state_of(rec) != "held":
        _cancel_waiting(rec, lease_cmd)
        reason = "cancelled (never granted; it held nothing)"
    rec["released"] = reason
    import agent_rotation
    agent_rotation.tick(force=True)  # the last reservation here ending puts the agent back in rotation
    rec["rotation"] = agent_rotation.describe(agent_rotation.summary())
    return rec


def _cancel_waiting(rec: dict, lease_cmd: str) -> None:
    """Let the waiting `pool reserve` see its cancel and exit (it looks at least every poll), signal it if it has not
    by then, and clear anything left under its id."""
    rid, pid, start = rec["id"], rec.get("pid"), str(rec.get("pid_start") or "")
    t0 = time.time()
    grace = min(CANCEL_WAIT_S / 3, 2 * _poll_s() + 5)
    signalled = False
    while _proc_alive(pid, start) and time.time() - t0 < CANCEL_WAIT_S:
        if not signalled and time.time() - t0 >= grace:
            with contextlib.suppress(OSError, ValueError, TypeError):
                os.kill(int(pid), signal.SIGTERM)
            signalled = True
        time.sleep(0.2)
    _drop(rid)
    _release_rid_files(rid, lease_cmd)


def _cancel_unrecorded(rid: str, lease_cmd: str) -> dict | None:
    """A request with no record: one an older copy of this code is still taking (its `.pending` marker names its
    process, its holds are pidfiles under its id). Stop that process and release what it took."""
    marker = os.path.join(_hdir(), f"{rid}.pending")
    pidfiles = glob.glob(os.path.join(_hdir(), f"{glob.escape(rid)}-*.pid"))
    if not os.path.exists(marker) and not pidfiles:
        return None
    with contextlib.suppress(OSError, ValueError):
        pid = int(_text(marker).strip())
        if _proc_alive(pid):
            os.kill(pid, signal.SIGTERM)
        for _ in range(50):
            if not _proc_alive(pid):
                break
            time.sleep(0.1)
    _release_rid_files(rid, lease_cmd)
    with contextlib.suppress(OSError):
        os.remove(marker)
    return {"id": rid, "host": config.server(), "for": "?", "state": "waiting", "devices": [], "cpu": False,
            "released": "cancelled (never granted)"}


def attach(rid: str, unit: str) -> dict | None:
    """Record a job unit placed in a reservation, so its end (not only `--until`) decides when the devices go."""
    with _locked():
        d = load()
        rec = d.get(rid)
        if rec is None:
            return None
        if unit not in (rec.get("jobs") or []):
            rec["jobs"] = list(rec.get("jobs") or []) + [unit]
            _save(d)
    return rec


def job_finished(sid: str, role: str, name: str, marks: dict | None = None) -> bool:
    """Has the owner's job of that name run to an exit code on this host (as a local or a placed
    remote unit)? A job that has not started yet is not finished, and an exit file an earlier run of the
    same name left (`marks`: those present at the grant, with their mtimes) is not this one finishing."""
    import jobs
    unit = jobs.unit_name(sid, role, name)
    if jobs.unit_state(unit).get("ActiveState") in ("active", "activating", "deactivating", "reloading"):
        return False
    marks = marks or {}
    for p in [jobs.paths(sid, unit)["rc"], jobs.remote_paths(unit, os.path.expanduser("~"))["rc"]]:
        if not os.path.exists(p):
            continue
        if p not in marks:
            return True
        with contextlib.suppress(OSError):
            if os.path.getmtime(p) != marks[p]:
                return True
    return False


def running_jobs(rec: dict) -> list[str]:
    """The job units placed in this reservation that still run here: its `job:<name>` and any attached."""
    import jobs
    sid, _, role = str(rec.get("for") or "/").partition("/")
    units = list(rec.get("jobs") or [])
    until = str(rec.get("until") or "")
    if until.startswith("job:"):
        units.append(jobs.unit_name(sid, role, until[4:]))
    out = []
    for u in dict.fromkeys(units):
        if jobs.unit_state(u).get("ActiveState") in ("active", "activating", "deactivating", "reloading"):
            out.append(u)
    return out


def _rehold(rec: dict, lease_cmd: str) -> str:
    """Take again, at once, each hold of `rec` whose holder is gone. Returns "" when all are held again, else why."""
    gpus = {d["uuid"]: {"vram_total_mib": 0} for d in rec.get("devices") or []}
    whys = []
    for n, h in enumerate(rec.get("holds") or []):
        if _pid_alive(h["pidfile"]):
            continue
        item = {"kind": "cpu"} if h.get("kind") == "cpu" else {"kind": "device", "uuid": h.get("device")}
        argv, env = hold_argv(lease_cmd, item, h["pidfile"], str(rec.get("for")), gpus, 0, scope_argv(rec["id"], n))
        try:
            r = subprocess.run(argv, capture_output=True, text=True, timeout=120, env=env, stdin=subprocess.DEVNULL)
            if r.returncode == 0 and _pid_alive(h["pidfile"]):
                continue
            whys.append(f"{'cpu-heavy' if item['kind'] == 'cpu' else 'gpu@' + str(item['uuid'])}: "
                        f"{((r.stderr or r.stdout).strip().splitlines() or ['not granted'])[-1][:120]}")
        except (OSError, subprocess.SubprocessError) as exc:
            whys.append(str(exc)[:120])
    return "; ".join(whys)


def _update(rid: str, **fields) -> None:
    with _locked():
        d = load()
        if rid in d:
            for k, v in fields.items():
                if v is None:
                    d[rid].pop(k, None)
                else:
                    d[rid][k] = v
            _save(d)


def expire(now: float | None = None, emit=True, peers: bool = False) -> list[dict]:
    """Release reservations past `--until` (a time passed, or the owner's job finished) and drop those
    whose holder died; drop a waiting request whose `pool reserve` is gone. A reservation due to end while a job
    placed in it still runs here is kept (and its hold taken again if its holder died) until that job ends: its
    device must not go to anyone else under a running job. Each release is a `released` event in the owner's
    shoal telemetry. `peers`: the agent rotation tick may read the other pool hosts' probes (the `resources
    watch` sample only)."""
    now = time.time() if now is None else now
    out = []
    lease_cmd = config.lease_cmd()
    import pool_cli
    for rid, rec in list(load().items()):
        if state_of(rec) != "held":
            if not alive(rec):
                gone = _drop(rid)
                _release_rid_files(rid, lease_cmd)
                if gone is not None:
                    gone["released"] = "abandoned (its pool reserve process is gone; it held nothing)"
                    out.append(gone)
            continue
        until, reason = str(rec.get("until") or ""), ""
        if until.startswith("job:"):
            sid, _, role = str(rec.get("for") or "/").partition("/")
            if job_finished(sid, role, until[4:], rec.get("rc_marks")):
                reason = f"expired ({until} finished)"
        elif (pool._epoch(until) or float("inf")) <= now:
            reason = f"expired (until {until})"
        lost = not holders_alive(rec)
        if not reason and lost:
            reason = "lost (holder gone)"
        if reason:
            running = running_jobs(rec)
            if running:
                note = {"since": rec.get("kept", {}).get("since") or pool_cli.utc(now), "why": reason,
                        "jobs": running}
                if lost:
                    why = _rehold(rec, lease_cmd) if lease_cmd else "no lease provider"
                    note["rehold"] = "held again" if not why else f"not held again: {why}"
                first = not rec.get("kept")
                _update(rid, kept=note, holder_lost=(note.get("rehold") or True) if lost else None)
                if first and emit:
                    _emit(rec, "reservation-kept", f"{rid} {reason}, but {', '.join(running)} still runs on it: kept "
                                                   f"until that ends" + (f" ({note['rehold']})" if lost else ""))
                continue
            r = release(rid, reason)
            if r is not None:
                out.append(r)
                if emit:
                    _emit(r, "released", f"{rid} {reason}")
        elif rec.get("kept") or rec.get("holder_lost"):
            _update(rid, kept=None, holder_lost=None)
    import agent_rotation
    agent_rotation.tick(now, peers=peers)  # level-triggered: a refused change is retried, a waiting build is swept
    return out


def report() -> list[dict]:
    """The reservations, and the requests still waiting for theirs, as `pool probe` reports them."""
    keys = ("id", "for", "until", "note", "devices", "cpu", "created_utc", "state", "want", "waiting", "waiting_since",
            "granted_utc", "jobs", "kept")
    return [{k: r.get(k) for k in keys} | {"state": state_of(r), "alive": alive(r)} for r in load().values()]


def _emit(rec: dict, kind: str, note: str) -> None:
    import jobs
    sid, _, role = str(rec.get("for") or "/").partition("/")
    if sid and sid != "?":
        jobs._emit(sid, role or "?", kind, note[:200],
                   {"reservation": {k: rec.get(k) for k in ("id", "for", "until", "note", "host", "devices", "cpu",
                                                            "released")}})


# ---- CLI --------------------------------------------------------------------------------------------

def _remote(host: str, args: list[str], timeout: float, stream_err: bool = False) -> tuple[int, dict | None, str]:
    """Run `shoal <args>` on a pool host over its route. `stream_err`: its stderr goes straight to ours (a waiting
    `pool reserve` says there that it waits, and how to cancel it)."""
    import routes
    r = routes.route(host)
    if r is None:
        return 1, None, f"{host} is not a pool host (no [hosts.{host}] route)"
    try:
        out = subprocess.run(routes.ssh_argv(r) + [routes.remote_cmd(r, *args)], stdin=subprocess.DEVNULL,
                             stdout=subprocess.PIPE, stderr=None if stream_err else subprocess.PIPE, text=True,
                             timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 2, None, str(exc)
    err = (out.stderr or "").strip()[-300:]
    try:
        return out.returncode, json.loads(out.stdout), err
    except ValueError:
        return out.returncode or 2, None, (out.stdout + (out.stderr or "")).strip()[-300:]


def cmd_reserve(a) -> int:
    local = config.server()
    import pool_cli
    if a.host and a.host != local and a.host not in pool_cli.pool_names():
        print(f"RESERVE-FAILED: {a.host} is not a pool host (pool: {', '.join(pool_cli.pool_names()) or 'none'})")
        return 1
    if a.host and a.host != local:
        args = ["pool", "reserve", "--resources", a.resources, "--for", a.owner, "--until", a.until, "--note", a.note,
                "--timeout-min", f"{a.timeout_min:g}", "--json", "--no-event"]
        rc, rec, err = _remote(a.host, args, a.timeout_min * 60 + 120, stream_err=True)
        if rc != 0 or not rec or rec.get("error"):
            why = (rec or {}).get("error") or err
            tag = "RESERVE-CANCELLED" if (rec or {}).get("cancelled") else "RESERVE-FAILED"
            print(json.dumps(rec or {"error": why}) if a.json else f"{tag} on {a.host}: {why}")
            return rc or 2
    else:
        def progress(text: str) -> None:
            print(text, file=sys.stderr, flush=True)
        try:
            rec = reserve(a.resources, a.owner, a.until, a.note, a.timeout_min, progress=progress)
        except Cancelled as exc:
            print(json.dumps({"error": str(exc), "cancelled": True}) if a.json else f"RESERVE-CANCELLED: {exc}")
            return 3
        except ValueError as exc:
            print(json.dumps({"error": str(exc)}) if a.json else f"RESERVE-FAILED: {exc}")
            return 2
    if not a.no_event:  # one event, on the host that asked (a remote call leaves it to the caller)
        _emit(rec, "reserved", f"{rec['id']} on {rec['host']} for {rec['for']} until {rec['until']}")
    if a.json:
        print(json.dumps(rec))
        return 0
    devs = ", ".join(d["uuid"] for d in rec.get("devices") or []) or "no GPU"
    print(f"RESERVED {rec['id']} on {rec['host']} for {rec['for']}: {devs}{' + cpu-heavy' if rec.get('cpu') else ''} "
          f"until {rec['until']}\nrelease with: {config.cli()} pool release {rec['id']}"
          + (f" --host {a.host}" if a.host and a.host != local else ""))
    if rec.get("rotation"):
        print(rec["rotation"])
    return 0


def cmd_release(a) -> int:
    local = config.server()
    if a.host and a.host != local:
        rc, rec, err = _remote(a.host, ["pool", "release", a.id, "--json", "--no-event"], CANCEL_WAIT_S + 120)
        if rc != 0 or not rec or rec.get("error"):
            print(f"RELEASE-FAILED on {a.host}: {(rec or {}).get('error') or err}")
            return rc or 2
    else:
        rec = release(a.id)
        if rec is None:
            print(json.dumps({"error": f"no reservation {a.id}"}) if a.json else
                  f"RELEASE-FAILED: no reservation or waiting request {a.id} here")
            return 1
    cancelled = str(rec.get("released") or "").startswith("cancelled")
    if not a.no_event:
        _emit(rec, "released", f"{rec['id']} {'cancelled before its grant' if cancelled else 'released'}")
    if a.json:
        print(json.dumps(rec))
        return 0
    if cancelled:
        print(f"CANCELLED {rec['id']} on {rec.get('host')} (was waiting for {rec.get('for')}; it held nothing): "
              f"its pool reserve exits RESERVE-CANCELLED")
    else:
        print(f"RELEASED {rec['id']} on {rec.get('host')} (was for {rec.get('for')})")
    if rec.get("rotation"):
        print(rec["rotation"])
    return 0


def cmd_attach(a) -> int:
    rec = attach(a.id, a.unit)
    print(json.dumps(rec if rec is not None else {"error": f"no reservation {a.id}"}))
    return 0 if rec is not None else 1


def add_parsers(ps) -> None:
    q = ps.add_parser("reserve", help="hold leases for one role's research, out of the CI pool (all or nothing)")
    q.add_argument("--host", default="", help="the pool host (default this one)")
    q.add_argument("--resources", required=True, metavar="gpu@<uuid>,...|gpus:N|cpu-heavy")
    q.add_argument("--for", dest="owner", required=True, metavar="<shoal>/<role>")
    q.add_argument("--until", required=True, metavar="ISO|job:NAME")
    q.add_argument("--note", default="")
    q.add_argument("--timeout-min", type=float, default=DEFAULT_TIMEOUT_MIN,
                   help="give up when the leases are not all free within this long (it holds nothing meanwhile)")
    q.add_argument("--json", action="store_true")
    q.add_argument("--no-event", action="store_true", help=argparse.SUPPRESS)
    q.set_defaults(func=cmd_reserve)
    q = ps.add_parser("release", help="release a research reservation, or cancel a request still waiting for one")
    q.add_argument("id")
    q.add_argument("--host", default="", help="the pool host holding it (default this one)")
    q.add_argument("--json", action="store_true")
    q.add_argument("--no-event", action="store_true", help=argparse.SUPPRESS)
    q.set_defaults(func=cmd_release)
    q = ps.add_parser("attach", help="record a research job placed in a reservation (job run --class research does)")
    q.add_argument("id")
    q.add_argument("unit")
    q.set_defaults(func=cmd_attach)
