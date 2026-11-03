#!/usr/bin/env python3
"""ssh routes to the other hosts of a shoal (architecture doc §12, "One view for every host").

A session can be attached only through its own host's harness, and that host's `shoal view <shoal>
<role>` already does it. So a viewer reaches a role on another host by running that command there over
ssh: a route is all it needs. Routes are private host configuration, never in a spec:

    [hosts.<server>]                 # the other host's `server` name
    ssh = "<user>@<name>"            # a MagicDNS name (survives address changes) or an address
    resolve = "tailscale"            # default when `tailscale` is on PATH: `tailscale ip -4 <name>`, so a host
                                     # whose resolver does not accept tailnet DNS still reaches it; "dns": as-is
    ssh_opts = ["-p", "2222"]        # optional extra ssh arguments
    cli = "shoal"                    # optional: the shoal command on that host (run under `bash -lc`)

A host with no route keeps the single-host rule: its roles are shown, never offered for attach.
"""
from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import time

import config

REACH_CACHE_S = 60
RESOLVE_CACHE_S = 600


def routes() -> dict[str, dict]:
    """{server: route} from `[hosts.<server>]`; a table without `ssh` is ignored."""
    v = config.file_values().get("hosts") or {}
    if not isinstance(v, dict):
        return {}
    return {str(k): dict(x) for k, x in v.items() if isinstance(x, dict) and str(x.get("ssh") or "").strip()}


def route(server: str) -> dict | None:
    return routes().get(server) if server else None


def ssh_bin() -> str:
    """SHOAL_SSH (tests: a stub) else ssh."""
    return config.env("SSH") or "ssh"


def _cache_dir() -> str:
    d = config.env("ROUTE_CACHE") or os.path.join("/tmp", f"shoal-routes-{os.getuid()}")  # tests: a scratch dir
    os.makedirs(d, exist_ok=True)
    return d


def _cached(name: str, max_age: float):
    p = os.path.join(_cache_dir(), name + ".json")
    try:
        d = json.load(open(p))
        if time.time() - float(d.get("t", 0)) < max_age:
            return d
    except (OSError, ValueError):
        pass
    return None


def _store(name: str, d: dict) -> None:
    d["t"] = time.time()
    p = os.path.join(_cache_dir(), name + ".json")
    # a temp name per process: every routed card probes on its own, and with one shared name a card lost the file
    # between another's write and rename and died on it. A cache that cannot be written only costs a later probe.
    tmp = p + f".{os.getpid()}.tmp"
    try:
        with open(tmp, "w") as fh:
            json.dump(d, fh)
        os.replace(tmp, p)
    except OSError:
        pass


def resolve_host(r: dict) -> str:
    """The address to connect to for a route's host part, or "" to let ssh resolve it. A tailnet name is
    looked up through the local tailscale daemon (cached), which needs no system DNS change."""
    target = str(r["ssh"])
    host = target.rsplit("@", 1)[-1]
    mode = str(r.get("resolve") or ("tailscale" if shutil.which("tailscale") else "dns")).lower()
    if mode != "tailscale" or config.env("SSH"):
        return ""
    if host.replace(".", "").isdigit() or ":" in host:
        return ""
    c = _cached("ip-" + host, RESOLVE_CACHE_S)
    if c is not None:
        return c.get("ip", "")
    try:
        out = subprocess.run(["tailscale", "ip", "-4", host], capture_output=True, text=True, timeout=10)
        ip = out.stdout.strip().splitlines()[0] if out.returncode == 0 and out.stdout.strip() else ""
    except (OSError, subprocess.TimeoutExpired):
        ip = ""
    _store("ip-" + host, {"ip": ip})
    return ip


def ssh_argv(r: dict, tty: bool = False) -> list[str]:
    argv = [ssh_bin()] + (["-tt"] if tty else []) + ["-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
                                                    "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3"]
    ip = resolve_host(r)
    if ip:
        argv += ["-o", f"HostName={ip}"]
    return argv + [str(x) for x in (r.get("ssh_opts") or [])] + [str(r["ssh"])]


def remote_cmd(r: dict, *args: str) -> str:
    """One shoal command on the route's host, under a login shell (its tool PATH and environment)."""
    cli = str(r.get("cli") or "shoal")
    return "bash -lc " + shlex.quote(" ".join([cli] + [shlex.quote(a) for a in args]))


def reachable(server: str, force: bool = False) -> bool | None:
    """True/False from a cached `ssh <route> true`; None when the host has no route."""
    r = route(server)
    if r is None:
        return None
    if not force:
        c = _cached("reach-" + server, REACH_CACHE_S)
        if c is not None:
            return bool(c.get("ok"))
    try:
        ok = subprocess.run(ssh_argv(r) + ["true"], stdin=subprocess.DEVNULL, capture_output=True,
                            timeout=12).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        ok = False
    _store("reach-" + server, {"ok": ok})
    return ok


def view_argv(server: str, sid: str, slug: str) -> list[str] | None:
    """`shoal view <shoal> <role>` on the role's own host, over ssh with a terminal; None without a route."""
    r = route(server)
    if r is None:
        return None
    return ssh_argv(r, tty=True) + [remote_cmd(r, "view", sid, slug)]


STATUS_CACHE_S = 30
STATUS_STALE_S = 600
# the fields of a role row that only the role's own host can know (its harness and its sidecars); the
# ledger-derived ones (posture, queue, asks, last write) are already right on every host
LIVE_FIELDS = ("session", "watcher", "can_progress", "needs_you", "context_k", "heartbeat_age_s")


def remote_rows(server: str, sid: str) -> tuple[dict, dict]:
    """({slug: row}, meta) for `sid`'s roles on `server`, from that host's `shoal ui status --rows-only`
    over its route: fresh within STATUS_CACHE_S, else pulled again; a failed pull keeps the last good rows
    (up to STATUS_STALE_S) and marks them stale. meta: {"ok", "age_s", "stale"}."""
    r = route(server)
    if r is None:
        return {}, {"ok": False, "age_s": None, "stale": True}
    key = f"status-{server}-{sid}"
    c = _cached(key, STATUS_CACHE_S)
    if c is None:
        prev = _cached(key, STATUS_STALE_S) or {}
        rows, ok = {}, False
        try:
            out = subprocess.run(ssh_argv(r) + [remote_cmd(r, "ui", "status", "--shoal", sid, "--rows-only", "--json")],
                                 stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=20)
            d = json.loads(out.stdout) if out.returncode in (0, 1) and out.stdout.strip() else {}
            s = next((x for x in d.get("shoals") or [] if x.get("id") == sid), None)
            if s is not None:
                rows, ok = {x["slug"]: x for x in s.get("roles") or []}, True
        except (OSError, subprocess.TimeoutExpired, ValueError):
            pass
        if ok:
            c = {"rows": rows, "ok": True, "pulled": time.time()}
            _store(key, c)
        else:
            c = {"rows": prev.get("rows") or {}, "ok": False, "pulled": prev.get("pulled")}
            _store(key, dict(c))  # retry after STATUS_CACHE_S, not on every read
    pulled = c.get("pulled")
    age = int(time.time() - pulled) if pulled else None
    return c.get("rows") or {}, {"ok": bool(c.get("ok")), "age_s": age,
                                 "stale": not c.get("ok") or age is None or age > STATUS_CACHE_S * 4}


def remote_standalone(server: str) -> tuple[list[dict], bool]:
    """(entries, ok) for the standalone sessions saved on `server` (its `shoal ui standalone --json`), each
    with its turn state there; cached STATUS_CACHE_S, the last good list kept on a failed pull (ok False)."""
    r = route(server)
    if r is None:
        return [], False
    key = f"standalone-{server}"
    c = _cached(key, STATUS_CACHE_S)
    if c is None:
        prev = _cached(key, STATUS_STALE_S) or {}
        try:
            out = subprocess.run(ssh_argv(r) + [remote_cmd(r, "ui", "standalone", "--json")], stdin=subprocess.DEVNULL,
                                 capture_output=True, text=True, timeout=20)
            got = json.loads(out.stdout).get("sessions") if out.returncode == 0 and out.stdout.strip() else None
        except (OSError, subprocess.TimeoutExpired, ValueError):
            got = None
        c = {"sessions": got, "ok": True} if isinstance(got, list) else {"sessions": prev.get("sessions") or [], "ok": False}
        _store(key, dict(c))
    return list(c.get("sessions") or []), bool(c.get("ok"))
