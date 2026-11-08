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
"""
from __future__ import annotations

import argparse
import contextlib
import datetime
import fcntl
import json
import os
import re
import secrets
import shlex
import signal
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import config  # noqa: E402
import pool  # noqa: E402

DEFAULT_TIMEOUT_MIN = 30


def _dir() -> str:
    return os.path.join(config.data_root(), "_pool")


def path() -> str:
    return os.path.join(_dir(), "reservations.json")


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


def parse_resources(text: str) -> list[dict]:
    """`gpu@<uuid>,...`, `gpus:N` and `cpu-heavy`, comma separated, as one item per hold."""
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


def hold_argv(lease_cmd: str, item: dict, pidfile: str, owner: str, gpus: dict, timeout_s: int) -> tuple[list[str], dict]:
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
    return shlex.split(lease_cmd) + args + ["--timeout", str(int(timeout_s)), "--hold", "--pidfile", pidfile], env


def _release_hold(lease_cmd: str, h: dict) -> None:
    try:
        subprocess.run(shlex.split(lease_cmd) + ["--release", "--pidfile", h["pidfile"]], capture_output=True,
                       text=True, timeout=60, env=config.tool_env())
    except (OSError, subprocess.SubprocessError):
        pass


def _pid_alive(pidfile: str) -> bool:
    try:
        pid = int(_text(pidfile).strip())
        os.kill(pid, 0)
        return True
    except (OSError, ValueError):
        return False


def alive(rec: dict) -> bool:
    return bool(rec.get("holds")) and all(_pid_alive(h["pidfile"]) for h in rec["holds"])


def reserve(resources: str, owner: str, until: str, note: str = "", timeout_min: float = DEFAULT_TIMEOUT_MIN,
            now: float | None = None) -> dict:
    """Take the holds and record the reservation; on any failure, release what was taken. Raises
    ValueError with the reason."""
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
    rid = f"r{time.strftime('%m%d%H%M', time.gmtime(now))}-{secrets.token_hex(2)}"
    hdir = os.path.join(_dir(), "holds")
    os.makedirs(hdir, exist_ok=True)
    holds = []
    for n, item in enumerate(items):
        pidfile = os.path.join(hdir, f"{rid}-{n}.pid")
        argv, env = hold_argv(lease_cmd, item, pidfile, owner, gpus, int(timeout_min * 60))
        try:
            r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout_min * 60 + 60, env=env,
                               stdin=subprocess.DEVNULL)
            ok = r.returncode == 0 and _pid_alive(pidfile)
            why = (r.stderr or r.stdout).strip()[-300:] if not ok else ""
        except (OSError, subprocess.SubprocessError) as exc:
            ok, why = False, str(exc)
        if not ok:
            for h in holds:
                _release_hold(lease_cmd, h)
            raise ValueError(f"hold {n} ({item['kind']}) not acquired: {why or 'no holder'}")
        try:
            dev = _text(pidfile + ".device").strip()
        except OSError:
            dev = ""
        hold = {"pidfile": pidfile, "kind": item["kind"]}
        if item["kind"] != "cpu":
            hold["device"] = dev or item.get("uuid") or ""
        holds.append(hold)
    devices = [{"uuid": h["device"], "vendor": (gpus.get(h["device"]) or {}).get("vendor") or ""}
               for h in holds if h.get("device")]
    rec = {"id": rid, "for": owner, "until": until, "note": note, "host": config.server(),
           "created_utc": pool_cli.utc(now), "holds": holds, "devices": devices,
           "cpu": any(h["kind"] == "cpu" for h in holds)}
    with _locked():
        d = load()
        d[rid] = rec
        _save(d)
    return rec


def release(rid: str, reason: str = "released") -> dict | None:
    """Release a reservation's holds and drop its record; None when there is no such reservation."""
    with _locked():
        d = load()
        rec = d.pop(rid, None)
        if rec is None:
            return None
        lease_cmd = config.lease_cmd()
        for h in rec.get("holds") or []:
            if lease_cmd:
                _release_hold(lease_cmd, h)
            else:
                with contextlib.suppress(OSError, ValueError):
                    os.killpg(int(_text(h["pidfile"]).strip()), signal.SIGTERM)
        _save(d)
    rec["released"] = reason
    return rec


def job_finished(sid: str, role: str, name: str) -> bool:
    """Has the owner's job of that name run to an exit code on this host (as a local or a placed
    remote unit)? A job that has not started yet is not finished."""
    import jobs
    unit = jobs.unit_name(sid, role, name)
    if jobs.unit_state(unit).get("ActiveState") in ("active", "activating", "deactivating", "reloading"):
        return False
    rcs = [jobs.paths(sid, unit)["rc"], jobs.remote_paths(unit, os.path.expanduser("~"))["rc"]]
    return any(os.path.exists(p) for p in rcs)


def expire(now: float | None = None, emit=True) -> list[dict]:
    """Release reservations past `--until` (a time passed, or the owner's job finished) and drop those
    whose holder died. Each release is a `released` event in the owner's shoal telemetry."""
    now = time.time() if now is None else now
    out = []
    for rid, rec in list(load().items()):
        until, reason = str(rec.get("until") or ""), ""
        if until.startswith("job:"):
            sid, _, role = str(rec.get("for") or "/").partition("/")
            if job_finished(sid, role, until[4:]):
                reason = f"expired ({until} finished)"
        elif (pool._epoch(until) or float("inf")) <= now:
            reason = f"expired (until {until})"
        if not reason and not alive(rec):
            reason = "lost (holder gone)"
        if reason:
            r = release(rid, reason)
            if r is not None:
                out.append(r)
                if emit:
                    _emit(r, "released", f"{rid} {reason}")
    return out


def report() -> list[dict]:
    """The reservations as `pool probe` reports them."""
    return [{k: r.get(k) for k in ("id", "for", "until", "note", "devices", "cpu", "created_utc")} | {"alive": alive(r)}
            for r in load().values()]


def _emit(rec: dict, kind: str, note: str) -> None:
    import jobs
    sid, _, role = str(rec.get("for") or "/").partition("/")
    if sid:
        jobs._emit(sid, role or "?", kind, note[:200],
                   {"reservation": {k: rec.get(k) for k in ("id", "for", "until", "note", "host", "devices", "cpu",
                                                            "released")}})


# ---- CLI --------------------------------------------------------------------------------------------

def _remote(host: str, args: list[str], timeout: float) -> tuple[int, dict | None, str]:
    import routes
    r = routes.route(host)
    if r is None:
        return 1, None, f"{host} is not a pool host (no [hosts.{host}] route)"
    try:
        out = subprocess.run(routes.ssh_argv(r) + [routes.remote_cmd(r, *args)], stdin=subprocess.DEVNULL,
                             capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 2, None, str(exc)
    try:
        return out.returncode, json.loads(out.stdout), out.stderr.strip()[-300:]
    except ValueError:
        return out.returncode or 2, None, (out.stdout + out.stderr).strip()[-300:]


def cmd_reserve(a) -> int:
    local = config.server()
    import pool_cli
    if a.host and a.host != local and a.host not in pool_cli.pool_names():
        print(f"RESERVE-FAILED: {a.host} is not a pool host (pool: {', '.join(pool_cli.pool_names()) or 'none'})")
        return 1
    if a.host and a.host != local:
        args = ["pool", "reserve", "--resources", a.resources, "--for", a.owner, "--until", a.until, "--note", a.note,
                "--timeout-min", f"{a.timeout_min:g}", "--json", "--no-event"]
        rc, rec, err = _remote(a.host, args, a.timeout_min * 60 + 120)
        if rc != 0 or not rec or rec.get("error"):
            print(json.dumps({"error": (rec or {}).get("error") or err}) if a.json else
                  f"RESERVE-FAILED on {a.host}: {(rec or {}).get('error') or err}")
            return rc or 2
    else:
        try:
            rec = reserve(a.resources, a.owner, a.until, a.note, a.timeout_min)
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
    return 0


def cmd_release(a) -> int:
    local = config.server()
    if a.host and a.host != local:
        rc, rec, err = _remote(a.host, ["pool", "release", a.id, "--json", "--no-event"], 120)
        if rc != 0 or not rec or rec.get("error"):
            print(f"RELEASE-FAILED on {a.host}: {(rec or {}).get('error') or err}")
            return rc or 2
    else:
        rec = release(a.id)
        if rec is None:
            print(json.dumps({"error": f"no reservation {a.id}"}) if a.json else f"RELEASE-FAILED: no reservation {a.id} here")
            return 1
    if not a.no_event:
        _emit(rec, "released", f"{rec['id']} released")
    if a.json:
        print(json.dumps(rec))
        return 0
    print(f"RELEASED {rec['id']} on {rec.get('host')} (was for {rec.get('for')})")
    return 0


def add_parsers(ps) -> None:
    q = ps.add_parser("reserve", help="hold leases for one role's research, out of the CI pool")
    q.add_argument("--host", default="", help="the pool host (default this one)")
    q.add_argument("--resources", required=True, metavar="gpu@<uuid>,...|gpus:N|cpu-heavy")
    q.add_argument("--for", dest="owner", required=True, metavar="<shoal>/<role>")
    q.add_argument("--until", required=True, metavar="ISO|job:NAME")
    q.add_argument("--note", default="")
    q.add_argument("--timeout-min", type=float, default=DEFAULT_TIMEOUT_MIN,
                   help="give up when the leases are not free within this long")
    q.add_argument("--json", action="store_true")
    q.add_argument("--no-event", action="store_true", help=argparse.SUPPRESS)
    q.set_defaults(func=cmd_reserve)
    q = ps.add_parser("release", help="release a research reservation")
    q.add_argument("id")
    q.add_argument("--host", default="", help="the pool host holding it (default this one)")
    q.add_argument("--json", action="store_true")
    q.add_argument("--no-event", action="store_true", help=argparse.SUPPRESS)
    q.set_defaults(func=cmd_release)
