#!/usr/bin/env python3
"""One durable Claude daemon per account, as a systemd --user service (architecture doc §7).

    shoal claude-daemon status                         each account's daemon: pid, origin, cgroup, what would end it
    shoal claude-daemon install [--dry-run]            write and enable the units (also: install-systemd --claude-daemon)
    shoal claude-daemon takeover [--account NAME]      move a live on-demand daemon's sessions under the service
    shoal claude-daemon uninstall [--dry-run]          remove the units; running daemons and their sessions go on
    shoal claude-daemon run --account NAME             the service's main process (systemd runs this)

Every background session of a Claude account is a worker of that account's one daemon. Claude Code starts the daemon on
demand from whichever client finds none, with that client's environment and in its cgroup, and it ends 5 s after its last
worker. A client in a Herdr terminal carries that terminal's `HC_SCOPE`, the daemon and every worker it spawns inherit
it, and herdr-core ends every process carrying the token when the terminal closes: the daemon has no SIGHUP handler, so it
dies without a log line, with every session on the account (twice on 2026-11-25, each within 30 s of a rebuild of
Herdr's role cards). `config.tool_env` and the viewers drop the token; this service makes the daemon independent of any
client.

The service runs `claude daemon run --origin foreground`: that origin never exits when idle, and an on-demand daemon
yields to it and never displaces it. (`--origin service` adds a remote recall flag that stops every worker; Claude Code
enables its own service install in no released build seen so far.) `run` is a small supervisor around it:
  - an on-demand daemon that holds the account is left alone until it exits, or until `takeover`;
  - once the service has held the account it takes it back from an on-demand daemon (an attached client can win the
    race to restart the daemon at each Claude Code update);
  - the successor a daemon starts of itself at an update is left to run, and watched.
KillMode=process: stopping or restarting the unit never stops a daemon or a session, and `run` picks up whatever runs.
"""
from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import accounts  # noqa: E402
import config  # noqa: E402

TEMPLATE = "shoal-claude-daemon@.service"
MARK = "# Written by `shoal claude-daemon install`; reinstall to change it."
SLICE = "shoal-claude.slice"  # outside app.slice, where the on-demand daemons and everything else of the user live
POLL_S = 1.0
GONE_S = 3.0  # a daemon seen gone this long has exited; one restarting for an update is back in about 0.2 s
BACKOFF_MAX_S = 60.0
SETTLED_S = 10.0  # a daemon that ran this long had the account; one that ended sooner backs off
ENSURE_WAIT_S = 10.0
TAKEOVER_WAIT_S = 30.0
# of the unit, not of the sessions the daemon starts (INVOCATION_ID changes how `backends.detached` launches)
UNIT_VARS = ("INVOCATION_ID", "JOURNAL_STREAM", "SYSTEMD_EXEC_PID", "NOTIFY_SOCKET", "MANAGERPID")


def _sleep(s: float) -> None:
    """Every wait of this module (tests replace this)."""
    time.sleep(s)


def _systemctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True, timeout=30)


def unit(account: str) -> str:
    """The account's unit, "" when its name cannot be a unit instance."""
    n = accounts.name("claude", account)
    return TEMPLATE.replace("@", f"@{n}") if re.fullmatch(r"[A-Za-z0-9_.-]+", n) else ""


def unit_path() -> str:
    import jobs
    return os.path.join(jobs.systemd_user_dir()[0], TEMPLATE)


def installed() -> bool:
    return os.path.exists(unit_path())


# ---- what runs

def _read(path: str) -> str:
    try:
        with open(path, "rb") as fh:
            return fh.read().decode(errors="replace")
    except OSError:
        return ""


def _start_time(pid: int) -> str:
    return (_read(f"/proc/{pid}/stat").rpartition(")")[2].split() + [""] * 20)[19]


def cgroup(pid: int) -> str:
    for line in _read(f"/proc/{pid}/cgroup").splitlines():
        if line.startswith("0::"):
            return line[3:]
    return ""


def scope_tag(pid: int) -> str:
    """The `HC_SCOPE` a process was started with ("" for none): herdr-core ends it when that terminal or exec ends."""
    for v in _read(f"/proc/{pid}/environ").split("\0"):
        if v.startswith(config.EXEC_SCOPE_VAR + "="):
            return v.partition("=")[2]
    return ""


def holder(account: str = "") -> dict | None:
    """The live daemon of an account, from its `daemon.lock`: {"pid", "origin", "cgroup", "service"}; None when the lock
    is absent or stale (a killed daemon leaves it behind). Reads files only, never the harness."""
    try:
        with open(os.path.join(accounts.home("claude", account), "daemon.lock")) as fh:
            d = json.load(fh)
        pid = int(d["pid"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    started = _start_time(pid)
    if not started or (d.get("procStart") and str(d["procStart"]) != started):
        return None
    cg = cgroup(pid)
    return {"pid": pid, "origin": str(d.get("origin") or "transient"), "cgroup": cg, "version": str(d.get("version") or ""),
            "service": bool(unit(account)) and cg.endswith("/" + unit(account))}


def tagged_workers(account: str = "") -> list[str]:
    """The account's live jobs whose worker carries a terminal's `HC_SCOPE` (from the daemon's roster)."""
    try:
        with open(os.path.join(accounts.home("claude", account), "daemon", "roster.json")) as fh:
            workers = json.load(fh).get("workers") or {}
        return sorted(j for j, w in workers.items() if isinstance(w, dict)
                      and any(scope_tag(int(w[k])) for k in ("pid", "replPid") if w.get(k)))
    except (OSError, ValueError, AttributeError, TypeError):
        return []


def report(account: str = "") -> tuple[str, str]:
    """(ok | info | warn, one line) about an account's daemon."""
    h, have = holder(account), installed()
    if h is None:
        if not have:
            return "info", "no daemon runs; the next client starts one on demand"
        return "warn", f"no daemon runs although {unit(account)} is installed (systemctl --user status {unit(account)})"
    what = f"pid {h['pid']} (Claude {h['version'] or '?'}, origin {h['origin']}) in {h['cgroup'] or '?'}"
    risk = exposure(account, h)
    if risk:
        return "warn", f"{what}: {risk}"
    if h["service"]:
        return "ok", f"{what}: the shoal service"
    if not os.path.isdir(os.path.join(os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}", "systemd")):
        return "info", f"{what}: started on demand (no systemd --user here)"
    return "warn", (f"{what}: started on demand, so whatever ends that cgroup ends every session on this account; "
                    + ("the service takes over when it exits, or at `shoal claude-daemon takeover`" if have
                       else "`shoal claude-daemon install` runs it as a service"))


def exposure(account: str = "", h: dict | None = None) -> str:
    """Why a Herdr terminal's end would end this account's daemon or sessions, "" when it would not."""
    h = h or holder(account)
    parts = ["its daemon"] if h and scope_tag(h["pid"]) else []
    jobs_ = tagged_workers(account) if h else []
    if jobs_:
        parts.append(f"its session(s) {', '.join(jobs_)}")
    if not parts:
        return ""
    return (f"{' and '.join(parts)} carry a Herdr terminal's HC_SCOPE and die when that terminal closes: run `shoal "
            f"claude-daemon takeover`, then stop and resume each such session at a quiet moment (its next direct wake does)")


def conditions(account_list) -> list[dict]:
    """Health's `claude-daemon`: an account this shoal's local roles use whose daemon or sessions a closing Herdr
    terminal would end. One instance per account and daemon."""
    out = []
    for a in sorted(set(account_list)):
        h = holder(a)
        risk = exposure(a, h) if h else ""
        if risk:
            out.append({"account": accounts.name("claude", a), "key": f"{accounts.name('claude', a)}:{h['pid']}",
                        "detail": f"Claude account {accounts.name('claude', a)} (daemon pid {h['pid']}): {risk}"})
    return out


# ---- before a launch

def ensure(account: str = "", wait_s: float = ENSURE_WAIT_S) -> str:
    """Before a client is started that would start a daemon on demand: start the account's service when it is installed
    and not holding the account, and wait for a daemon. Returns "service", "transient" (one already holds it: the
    client uses that) or "" (no service here, or it did not come up: the client starts one, as without shoal).
    Never raises: a launch goes on either way."""
    try:
        from backends import fake_sessions
        if fake_sessions() is not None or not unit(account) or not installed():
            return ""
        h = holder(account)
        if h is None or not h["service"]:
            r = _systemctl("start", "--no-block", unit(account))
            waited = 0.0
            while h is None and r.returncode == 0 and waited < wait_s:
                _sleep(0.2)
                waited += 0.2
                h = holder(account)
        return "" if h is None else "service" if h["service"] else "transient"
    except Exception:  # noqa: BLE001 - never the reason a session does not start
        return ""


# ---- the service

def daemon_env(account: str) -> dict:
    return {k: v for k, v in accounts.env("claude", account).items() if k not in UNIT_VARS}


def run(account: str, rounds: int | None = None) -> int:
    """The unit's main process (module docstring). `rounds`: how many passes to make (tests)."""
    from backends import harness_exe
    mine = {"held": False}

    def take(*_):
        mine["held"] = True

    signal.signal(signal.SIGUSR1, take)
    said, fails, seen = "", 0, False

    def say(line: str) -> None:
        nonlocal said
        if line != said:
            print(line, flush=True)
            said = line

    while rounds is None or rounds > 0:
        rounds = None if rounds is None else rounds - 1
        h = holder(account)
        if h is None and seen:
            seen = False
            _sleep(GONE_S)
            continue
        seen = h is not None
        if h and h["origin"] != "transient":
            mine["held"] = True
            say(f"daemon pid {h['pid']} (origin {h['origin']}) holds the account")
            _sleep(POLL_S)
            continue
        if h and not mine["held"]:
            say(f"on-demand daemon pid {h['pid']} in {h['cgroup']} holds the account: left alone until it exits or a takeover")
            _sleep(POLL_S)
            continue
        seen = False
        say(f"starting the daemon{' in place of on-demand pid ' + str(h['pid']) if h else ''}")
        began = time.monotonic()
        try:
            rc = subprocess.run([harness_exe("claude"), "daemon", "run", "--origin", "foreground"], stdin=subprocess.DEVNULL,
                                env=daemon_env(account)).returncode
        except OSError as exc:
            rc = f"({exc})"
        ran = time.monotonic() - began
        mine["held"] = mine["held"] or ran >= SETTLED_S
        fails = 0 if ran >= SETTLED_S else fails + 1
        say(f"the daemon ended rc={rc} after {ran:.0f} s")
        _sleep(min(BACKOFF_MAX_S, POLL_S * 2 ** min(fails, 10)) if fails else POLL_S)
    return 0


def unit_text() -> str:
    exe, script = config.cli_exec()
    env = "".join(f"Environment={k}={v}\n" for k, v in sorted(config.tool_env_extra().items()))
    return (f"{MARK}\n[Unit]\nDescription=shoal Claude daemon (account %i)\nStartLimitIntervalSec=60\nStartLimitBurst=10\n"
            f"[Service]\nType=simple\nWorkingDirectory=%h\nEnvironment=PATH={config.tool_path()}\n{env}"
            f"ExecStart={f'{exe} {script}'.rstrip()} claude-daemon run --account %i\n"
            # the daemon, its update successors and every session live in this cgroup: only `run` is ever signalled, and a
            # session's OOM kill does not stop the unit
            f"Restart=always\nRestartSec=1\nKillMode=process\nOOMPolicy=continue\nSlice={SLICE}\n"
            f"[Install]\nWantedBy=default.target\n")


def install(dry_run: bool = False) -> int:
    """Write the template and enable one instance per account with a state directory. Starts no daemon where one already
    runs (`run` waits for it), and stops nothing."""
    path, text, cur = unit_path(), unit_text(), _read(unit_path())
    if cur and not cur.startswith(MARK):
        print(f"{path} was not written by shoal: left alone")
        return 1
    todo = [a for a, h in accounts.homes("claude") if os.path.isdir(h)]
    if dry_run:
        print(f"[dry-run] {path}\n{text}" + "".join(f"[dry-run] enable --now {unit(a)}\n" for a in todo if unit(a)), end="")
        return 0
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + ".tmp", "w") as fh:
        fh.write(text)
    os.replace(path + ".tmp", path)
    print(f"wrote {path}")
    import jobs
    if not jobs.systemd_user_dir()[1]:  # a redirected unit dir (selftests) is not one the user manager reads
        return 0
    bad = 0
    _systemctl("daemon-reload")
    for a in todo:
        if not unit(a):
            print(f"account {accounts.name('claude', a)!r}: not a systemd instance name, skipped")
            continue
        r = _systemctl("enable", "--now", unit(a))
        bad += r.returncode != 0
        print(f"{'enabled' if r.returncode == 0 else 'ENABLE FAILED'}: {unit(a)}"
              + ("" if r.returncode == 0 else f" ({(r.stdout + r.stderr).strip()[-160:]})"))
    return 1 if bad else 0


def uninstall(dry_run: bool = False) -> int:
    path = unit_path()
    if not _read(path).startswith(MARK):
        print(f"{path} {'was not written by shoal: left alone' if os.path.exists(path) else 'is not installed'}")
        return 0
    for a in accounts.of("claude"):
        if unit(a) and dry_run:
            print(f"[dry-run] disable --now {unit(a)}")
        elif unit(a):
            _systemctl("disable", "--now", unit(a))
    if dry_run:
        print(f"[dry-run] remove {path}")
        return 0
    os.remove(path)
    _systemctl("daemon-reload")
    print(f"removed {path}; a daemon that runs goes on, and the next one starts on demand")
    return 0


def takeover(account: str = "") -> int:
    """Ask the account's service to take the account from the on-demand daemon that holds it now. That daemon yields:
    it closes without stopping a worker and the service's daemon adopts them, the same steps as its restart at every
    Claude Code update. The workers keep running in the cgroup they were started in."""
    n, u, h = accounts.name("claude", account), unit(account), holder(account)
    if h and h["service"]:
        print(f"{n}: daemon pid {h['pid']} already runs in the service")
        return 0
    pid = _systemctl("show", "-p", "MainPID", "--value", u).stdout.strip() if u else ""
    if not pid.isdigit() or pid == "0" or not cgroup(int(pid)).endswith("/" + u):
        print(f"{n}: the service is not running (shoal claude-daemon install)")
        return 1
    os.kill(int(pid), signal.SIGUSR1)  # `run` alone: never `systemctl kill`, which would signal the whole cgroup
    waited = 0.0
    while waited < TAKEOVER_WAIT_S:
        _sleep(0.5)
        waited += 0.5
        h = holder(account)
        if h and h["service"]:
            print(f"{n}: daemon pid {h['pid']} runs in the service; `bg adopt:` in {accounts.home('claude', account)}/daemon.log "
                  f"counts the sessions it took over")
            return 0
    print(f"{n}: not taken over in {TAKEOVER_WAIT_S:g} s (journalctl --user -u {u}); now: {report(account)[1]}")
    return 1


def cmd(a) -> int:
    if a.op == "status":
        for acct, _ in accounts.homes("claude"):
            level, line = report(acct)
            print(f"{level.upper():4} {accounts.name('claude', acct)}: {line}")
        return 0
    if a.op == "install":
        return install(a.dry_run)
    if a.op == "uninstall":
        return uninstall(a.dry_run)
    try:
        acct = accounts.normalize("claude", a.account)
    except ValueError as exc:
        print(f"claude-daemon: {exc}")
        return 1
    return takeover(acct) if a.op == "takeover" else run(acct)


def add_parser(sub) -> None:
    p = sub.add_parser("claude-daemon", help="run each Claude account's daemon as a durable systemd --user service")
    p.add_argument("op", choices=["status", "install", "takeover", "uninstall", "run"])
    p.add_argument("--account", default="", help="takeover, run: the Claude account (default: the provider's own)")
    p.add_argument("--dry-run", action="store_true", help="install, uninstall: print only")
    p.set_defaults(func=cmd)
