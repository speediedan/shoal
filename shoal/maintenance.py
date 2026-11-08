#!/usr/bin/env python3
"""Host maintenance windows: a deliberate reboot, reset or hang test on one host (architecture doc §5.3).

A lease and a pool reservation are held locks, so each one dies with the boot it protects (measured 2026-11-07: a
watchdog test's GPU hold ended at its first reboot, every lane pre-check then passed, and a two-card job started 13 s
before the planned reset). A window is therefore recorded where reboots cannot end it:

- a marker on the host itself, `<data root>/_pool/maintenance.json`, read there by `pool reserve`, the pool probe
  (so `pool status` and placement read the host as ineligible) and `job run` started on that host;
- a `maintenance:<server>` mode event in every shoal ledger of the clone the command runs from, so `next` on any host
  prints it, `job run --on/--place` refuses the host, and health holds that host's silence page.

    shoal maintenance on --server S --until <ISO|+90m|+2h> --note "<why>" [--wake]
    shoal maintenance off --server S
    shoal maintenance status [--server S] [--json]

A window ends at `off` or at `--until`, whichever comes first, so a forgotten `off` does not hold a host for good.
"""
from __future__ import annotations

import datetime
import glob
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config  # noqa: E402

MODE_PREFIX = "maintenance:"


def utc(t: float) -> str:
    return datetime.datetime.fromtimestamp(t, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _epoch(ts: str) -> float | None:
    try:
        return datetime.datetime.strptime(str(ts)[:19], "%Y-%m-%dT%H:%M:%S").replace(
            tzinfo=datetime.timezone.utc).timestamp()
    except ValueError:
        return None


def parse_until(text: str, now: float | None = None) -> str:
    """An ISO UTC time, or `+<n>m` / `+<n>h` from now, as ISO UTC. Raises ValueError."""
    now = time.time() if now is None else now
    m = re.fullmatch(r"\+(\d+(?:\.\d+)?)([mh])", (text or "").strip())
    if m:
        return utc(now + float(m.group(1)) * (60 if m.group(2) == "m" else 3600))
    t = _epoch(text)
    if t is None:
        raise ValueError(f"--until {text!r}: give an ISO UTC time (YYYY-MM-DDTHH:MM:SSZ) or +<n>m / +<n>h")
    if t <= now:
        raise ValueError(f"--until {text} is already past")
    return utc(t)


def active(rec: dict | None, now: float | None = None) -> bool:
    """True while a window is on and its `until` has not passed."""
    if not isinstance(rec, dict) or rec.get("state") != "on":
        return False
    end = _epoch(rec.get("until") or "")
    return end is None or (time.time() if now is None else now) < end


def describe(rec: dict) -> str:
    note = f": {rec['note']}" if rec.get("note") else ""
    return f"{rec.get('server') or '?'} under maintenance until {rec.get('until') or '(no end)'}{note}"


# ---- the marker on the host itself ----------------------------------------------------------------

def marker_path() -> str:
    return os.path.join(config.data_root(), "_pool", "maintenance.json")


def read_local(now: float | None = None) -> dict | None:
    """This host's window while it is active, else None."""
    try:
        with open(marker_path()) as fh:
            rec = json.load(fh)
    except (OSError, ValueError):
        return None
    return rec if active(rec, now) else None


def write_local(state: str, until: str = "", note: str = "", by: str = "user") -> dict:
    rec = {"server": config.server(), "state": state, "until": until, "note": note, "by": by, "ts": utc(time.time())}
    p = marker_path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p + ".tmp", "w") as fh:
        json.dump(rec, fh, indent=1)
    os.replace(p + ".tmp", p)
    return rec


# ---- the ledger's view, for every host --------------------------------------------------------------

def from_view(view, server: str, now: float | None = None) -> dict | None:
    """The active window the ledger records for `server`, else None."""
    e = (getattr(view, "modes", None) or {}).get(MODE_PREFIX + server)
    return e if active(e, now) else None


def all_from_view(view, now: float | None = None) -> list[dict]:
    return [e for k, e in sorted((getattr(view, "modes", None) or {}).items())
            if k.startswith(MODE_PREFIX) and active(e, now)]


def from_events(events, server: str, now: float | None = None) -> dict | None:
    """As from_view, from raw ledger events (health's host beats read events, not a view)."""
    last = None
    for e in events:
        if e.get("type") == "system" and e.get("mode") == MODE_PREFIX + server and e.get("by") == "user":
            if last is None or str(e.get("ts", "")) >= str(last.get("ts", "")):
                last = e
    return last if active(last, now) else None


def banner(view, now: float | None = None) -> str:
    lines = [f"HOST MAINTENANCE: {describe(e)}. Start nothing there (no job, block, lease or reservation) until it "
             f"ends; work elsewhere goes on" for e in all_from_view(view, now)]
    return "\n".join(lines)


def server_of(target: str) -> str:
    """The server name for an ssh target or a route name (`user@host` is matched to a route by its `ssh`)."""
    import routes
    if not target:
        return config.server()
    rs = routes.routes()
    if target in rs:
        return target
    for name, r in rs.items():
        if str(r.get("ssh")) == target:
            return name
    host = target.split("@", 1)[-1]
    return host if host in rs or host == config.server() else host


def refusal(view, target: str = "", now: float | None = None) -> str:
    """Why work must not start on `target` (an ssh target, route name, or "" for this host), or ""."""
    server = server_of(target)
    rec = from_view(view, server, now) if view is not None else None
    if rec is None and server == config.server():
        rec = read_local(now)
    return f"{describe(rec)} (`shoal maintenance status`)" if rec else ""


# ---- the command --------------------------------------------------------------------------------

def _specs() -> list[str]:
    return sorted(glob.glob(os.path.join(config.ledger_dir(), "shoals", "*", "shoal.json")))


def _remote(server: str, args: list[str], timeout: float = 60) -> tuple[int, str]:
    import subprocess
    import routes
    r = routes.route(server)
    if r is None:
        return 2, f"no route to {server} (`[hosts.{server}]` in the host config)"
    try:
        p = subprocess.run(routes.ssh_argv(r) + [routes.remote_cmd(r, *args)], stdin=subprocess.DEVNULL,
                           capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 2, str(exc)
    return p.returncode, (p.stdout + p.stderr).strip()


def cmd_maintenance(a) -> int:
    from ledger import Ledger, LedgerError, View, sync_push
    from roles import ShoalSpec
    now = time.time()
    server = a.server or config.server()
    local = server == config.server()
    if a.state == "status":
        if a.local or local:
            rec = read_local(now)
            if a.json:
                print(json.dumps(rec or {"server": config.server(), "state": "off"}))
            else:
                print(describe(rec) if rec else f"{config.server()}: no maintenance window")
            return 0
        rc, out = _remote(server, ["maintenance", "status", "--local", "--json"])
        try:
            rec = json.loads(out.splitlines()[-1]) if rc == 0 and out else None
        except ValueError:
            rec, rc = None, 2
        if a.json or rc != 0:
            print(out if rc == 0 else f"ERROR: could not read {server}'s window: {out[-300:]}")
        else:
            print(describe(rec) if active(rec) else f"{server}: no maintenance window")
        return 0 if rc == 0 else 2
    until = ""
    if a.state == "on":
        try:
            until = parse_until(a.until, now)
        except ValueError as exc:
            print(f"ERROR: {exc}")
            return 1
        if not a.note:
            print("ERROR: maintenance on needs --note (what the window is for)")
            return 1
    what = f"maintenance on {server} until {until}: {a.note}" if a.state == "on" else f"maintenance off on {server}"
    if a.dry_run:
        print(f"[dry-run] {what}: marker on {server}" + ("" if a.local else f", mode event in {len(_specs())} shoal(s)"))
        return 0
    # the marker first: it is what holds through the host's reboots
    if a.local or local:
        write_local(a.state, until, a.note or "", by="user")
    else:
        args = ["maintenance", a.state, "--server", server, "--local"] + (["--until", until, "--note", a.note] if until else [])
        rc, out = _remote(server, args)
        if rc != 0:
            print(f"ERROR: could not set the marker on {server}: {out[-300:]}")
            return 2
    if a.local:
        print(f"{what} (marker only)")
        return 0
    roots = set()
    for path in _specs():
        try:
            spec = ShoalSpec.load(path)
            if getattr(spec, "retired_utc", ""):
                continue
            led = Ledger(spec.sid, root=spec.ledger_dir)
            led.append("user", "system", mode=MODE_PREFIX + server, server=server, state=a.state, until=until or None,
                       note=a.note or None, _commit=False)
            if a.wake and a.state == "on":
                led.append("user", "msg", to="*", kind="info", wake=True, _commit=False,
                           text=f"HOST MAINTENANCE on {server} until {until}: {a.note}. Start no job, block, lease or "
                                f"reservation there until it ends; `next` shows when it does.")
            led.commit_own("user", f"{spec.sid} maintenance {a.state} {server}")
            roots.add(led.root if led.sync else "")
        except (LedgerError, OSError, ValueError) as exc:
            print(f"WARNING: {os.path.basename(os.path.dirname(path))}: {exc}")
    pushed = all(sync_push(r) for r in roots if r) if not a.no_sync else True
    print(what + ("" if pushed else " (push pending: `shoal sync`)"))
    return 0


def add_parser(sub) -> None:
    p = sub.add_parser("maintenance", help="a host maintenance window (a deliberate reboot, reset or hang test): "
                                           "on|off|status; jobs, reservations and placement refuse the host")
    p.add_argument("state", choices=["on", "off", "status"])
    p.add_argument("--server", default="", help="the host (a server name; default this one)")
    p.add_argument("--until", default="", help="on: when it ends at the latest (ISO UTC, or +90m / +2h)")
    p.add_argument("--note", default="", help="on: what the window is for")
    p.add_argument("--wake", action="store_true", help="on: also wake every role with a session in each shoal")
    p.add_argument("--local", action="store_true", help="the marker on this host only (what --server uses remotely)")
    p.add_argument("--json", action="store_true")
    p.add_argument("--no-sync", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_maintenance)
