#!/usr/bin/env python3
"""systemd-timer watchers (port of watch-coordination.sh): poll state, hash
it, and on change fire into the ledger — a monitor that only writes a log
is an orphaned writer no idle session ever re-reads.

Modes: lanes | pipeline. Invariants preserved from the shell version:
explicit tool PATH (cron's is bare), validate-before-hash (empty snapshot is
ERROR, never baseline), one heartbeat line per run, poisoned state files
deleted never trusted, state in /tmp/<hag>/ (never committed).

Test mode (--test) fires *-WATCH-TEST (a ledger `wake-test` msg) and exits 0.

The target argument is `ledger:<hag>` (flattened only, 2026-09-24; the
session-addressed fires and the legacy `queue` mode are removed): lanes
SERVER-RESPAWNED goes to `*` (every role re-verifies), pipeline STARVED goes
to the roles whose ledger state is waiting/remote-ci (else user). With spec
`features.health` both route through the health wake adapters instead.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "telemetry"))
try:
    from hag_telemetry import emit as _telemetry_emit  # noqa: E402
except Exception:  # pragma: no cover
    _telemetry_emit = None
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ledger import SYSTEM_WRITER  # noqa: E402
import config  # noqa: E402

TOOL_PATH = config.tool_path()
_ENV = config.tool_env()


def utcnow() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%FT%TZ")


class Args:  # minimal namespace for the telemetry emit() signature
    def __init__(self, logdir: str, hag: str, session: str, eco: str,
                 fields: list[str], typ: str = ""):
        self.dir = logdir
        self.hag = hag
        self.session = session
        self.ecosystem = eco
        self.field = fields
        # emit() reads args.type; before 2026-09-22 it was missing here, so
        # every hag_emit raised AttributeError and wrote nothing.
        self.type = typ


def hag_emit(hag: str, session: str, eco: str, typ: str,
             **fields: str) -> None:
    if _telemetry_emit is None:
        print("hag_emit failed: hag_telemetry not importable", file=sys.stderr)
        return
    try:
        import contextlib
        import io
        # emit() echoes the event JSON on stdout; keep watcher stdout unchanged
        with contextlib.redirect_stdout(io.StringIO()):
            _telemetry_emit(Args("", hag, session, eco,
                                 [f"{k}={v}" for k, v in fields.items()],
                                 typ=typ))
    except Exception as exc:  # noqa: BLE001
        # best-effort (never break the watcher), but never silent
        print(f"hag_emit failed: {exc}", file=sys.stderr)


def state_dir(hag: str) -> str:
    d = os.path.join("/tmp", hag)
    os.makedirs(d, exist_ok=True)
    return d


def tick(hag: str, job: str) -> None:
    """Self-tick: /tmp/<hag>/tick-<job>. A watcher that never runs must read
    as dead from `status`/`doctor`; the mtime is the evidence."""
    p = os.path.join(state_dir(hag), f"tick-{job}")
    try:
        with open(p, "a"):
            os.utime(p, None)
    except OSError:
        pass


def log(sdir: str, msg: str) -> None:
    line = f"{utcnow()} {msg}"
    print(line)
    try:
        with open(os.path.join(sdir, "watch.log"), "a") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def ledger_fire(hag: str, to: list[str], kind: str, text: str) -> bool:
    try:
        from ledger import Ledger, sync_push
        from roles import ShoalSpec, find_spec
        spec = ShoalSpec.load(find_spec(hag))
        led = Ledger(hag, root=spec.ledger_dir)
        led.pull()
        for t in to:
            led.append(SYSTEM_WRITER, "msg", to=t, kind=kind, text=text[:500], _push=False)
        sync_push(led.root)
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"{utcnow()} ERROR: ledger fire {kind} failed: {exc}")
        return False


def fire(target: str, repo_dir: str, tag: str, summary: str) -> bool:
    """Fire into the ledger (target `ledger:<hag>`); returns False (and logs)
    when dispatch fails — a failed fire must never be logged as a fire."""
    if not target.startswith("ledger:"):
        print(f"{utcnow()} ERROR: {tag}: target {target!r} is not ledger:<hag> "
              "(session-addressed fires were removed 2026-09-24)")
        return False
    hag = target[len("ledger:"):]
    kind = {"LANES-WATCH": "server-respawned", "PIPELINE-WATCH": "starved"}.get(tag.split("-TEST")[0], "info")
    routed = _health_route(hag, kind, f"{tag}: {summary}") if not tag.endswith("-TEST") else None
    if routed is not None:
        return routed
    to = ["*"]
    if kind == "starved":
        to = starved_targets(hag)
    if tag.endswith("-TEST"):
        kind = "wake-test"
    return ledger_fire(hag, to, kind, f"{tag}: {summary}")


def _health_route(hag: str, kind: str, text: str) -> bool | None:
    """With spec features.health, a sensor fire is WORK for specific roles, delivered through
    the health wake adapters instead of a ledger msg (msgs no longer wake anyone):
    starvation -> roles waiting on remote CI; server respawn -> every opencode role.
    None = feature off (caller falls back to the ledger msg)."""
    try:
        from roles import ShoalSpec, find_spec
        spec = ShoalSpec.load(find_spec(hag))
        if not (spec.features or {}).get("health") or kind not in ("starved", "server-respawned"):
            return None
        from health import request_wake
        if kind == "starved":
            to = [s for s in starved_targets(hag) if s != "user" and spec.role(s) is not None]
        else:
            to = [r.slug for r in spec.roles if r.ecosystem == "opencode" and r.session]
        for s in to:
            print(f"{utcnow()} {kind} -> {s}: {request_wake(spec, s, text, condition=kind)}")
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"{utcnow()} ERROR: health route {kind} failed: {exc}")
        return None


def starved_targets(hag: str) -> list[str]:
    """Roles whose ledger state is waiting on remote CI, else user."""
    try:
        from ledger import Ledger, View
        from roles import ShoalSpec, find_spec
        spec = ShoalSpec.load(find_spec(hag))
        v = View(Ledger(hag, root=spec.ledger_dir), spec)
        waiting = [s for s, st in v.states.items()
                   if st.get("state") == "waiting" and st.get("sub") == "remote-ci"]
        if waiting:
            return sorted(set(waiting))
    except Exception:
        pass
    return ["user"]


def health(sdir: str) -> None:
    r = subprocess.run(["openchamber", "status"], capture_output=True,
                       timeout=30, env=_ENV)
    if r.returncode != 0:
        log(sdir, "ERROR: openchamber server unreachable; refusing to snapshot.")
        sys.exit(2)


def md5(s: str) -> str:
    return hashlib.md5(s.encode()).hexdigest()


def lane_snapshot(lane: str, repo_dir: str) -> str:
    st = subprocess.run(["openchamber", "session", "status", "--session", lane,
                         "--dir", repo_dir], capture_output=True, text=True,
                        timeout=30, env=_ENV).stdout.strip().splitlines()
    last = subprocess.run(
        ["openchamber", "session", "messages", "--session", lane, "--dir",
         repo_dir, "--limit", "1", "--role", "assistant"],
        capture_output=True, text=True, timeout=30, env=_ENV).stdout
    m = re.search(r"\*20[0-9T:.\-]+Z", last)
    return f"{lane}|{st[0] if st else ''}|{m.group(0) if m else 'none'}"


def server_pid() -> str:
    try:
        import glob as _g
        files = sorted(_g.glob(os.path.expanduser(
            "~/.config/openchamber/managed-opencode/*.json")))
        if not files:
            return "?"
        with open(files[-1]) as fh:
            return str(json.load(fh).get("pid", "?"))
    except Exception:
        return "?"


def cmd_lanes(hag: str, target: str, repo_dir: str, lanes: list[str],
              test: bool = False) -> int:
    sdir = state_dir(hag)
    tick(hag, "lanes")
    health(sdir)
    if not lanes:
        print("at least one lane session id required.", file=sys.stderr)
        return 1
    if test:
        fire(target, repo_dir, "LANES-WATCH-TEST",
             f"dry-run wakeup proof for {len(lanes)} lane(s); confirm arrival in-transcript")
        print(f"test fired to {target}; confirm the wake-test msg in `shoal show`, then arm the timer")
        return 0
    snap = "\n".join(lane_snapshot(l, repo_dir) for l in lanes)
    snap += f"\nserver_pid={server_pid()}"
    if not re.search(r"status:(idle|busy)", snap):
        log(sdir, "ERROR: no lane resolved (all snapshots empty); refusing to snapshot.")
        return 2
    cur = md5(snap)
    state = os.path.join(sdir, f"lanes-{md5(target)}.state")
    pidfile = state + ".pid"
    newpid = re.search(r"server_pid=[0-9?]+", snap)
    newpid = newpid.group(0) if newpid else "server_pid=none"
    oldpid = open(pidfile).read().strip() if os.path.exists(pidfile) else "none"
    with open(pidfile, "w") as fh:
        fh.write(newpid)
    if oldpid != "none" and oldpid != newpid:
        hag_emit(hag, target, "opencode", "watch_event", kind="server-respawned",
                 note=f"{oldpid}->{newpid}", channel="within-ecosystem")
        hag_emit(hag, target, "opencode", "coord_comms", direction="watch-fire",
                 peer="lanes", note="SERVER-RESPAWNED", channel="within-ecosystem")
        fire(target, repo_dir, "LANES-WATCH",
             f"SERVER-RESPAWNED ({oldpid}->{newpid}, re-send unacked INSTRUCTs): "
             f"{snap.replace(chr(10), ';')}")
        log(sdir, f"LANES-WATCH SERVER-RESPAWNED fired: {oldpid}->{newpid}")
    last = open(state).read().strip() if os.path.exists(state) else "none"
    if cur != last:
        with open(state, "w") as fh:
            fh.write(cur)
        with open(state + ".txt", "w") as fh:
            fh.write(snap)
        if last == "none":
            print(f"{utcnow()} lanes baseline established for {target}")
            return 0
        print(f"{utcnow()} lanes state moved (ledger mode: transitions travel in the ledger, no fire)")
        return 0
    else:
        print(f"{utcnow()} lanes ok no-change -> {target}")
    return 0


def cmd_pipeline(hag: str, target: str, repo_dir: str, org: str,
                 proj: str) -> int:
    sdir = state_dir(hag)
    tick(hag, "pipeline")
    health(sdir)
    env = dict(_ENV)
    if not env.get("AZURE_DEVOPS_EXT_PAT"):
        try:
            with open(os.path.expanduser("~/.bashrc")) as fh:
                for line in fh:
                    m = re.match(r"export AZURE_DEVOPS_EXT_PAT=(\S+)", line.strip())
                    if m:
                        env["AZURE_DEVOPS_EXT_PAT"] = m.group(1).strip("'\"")
        except OSError:
            pass
    if not env.get("AZURE_DEVOPS_EXT_PAT"):
        log(sdir, "ERROR: AZURE_DEVOPS_EXT_PAT unset; refusing to judge.")
        return 2

    def builds(status: str) -> str | None:
        r = subprocess.run(
            ["az", "pipelines", "build", "list", "--organization", org,
             "--project", proj, "--status", status, "--top",
             "5" if status == "inProgress" else "10",
             "--query", "[].id" if status == "inProgress"
             else "[].{id:id,branch:sourceBranch}", "-o", "tsv"],
            capture_output=True, text=True, timeout=120, env=env)
        if r.returncode != 0:
            return None
        return r.stdout.replace("\n", "," if status == "inProgress" else ";").strip()

    running, queued = builds("inProgress"), builds("notStarted")
    if running is None or queued is None:
        log(sdir, "ERROR: az builds query failed; refusing to judge.")
        return 2
    cur = md5(f"{running}|{queued}")
    state = os.path.join(sdir, f"pipeline-{proj}.state")
    last = open(state).read().strip() if os.path.exists(state) else "none"
    if cur != last:
        with open(state, "w") as fh:
            fh.write(cur)
        if last == "none":
            print(f"{utcnow()} pipeline baseline established for {proj}")
            return 0
        if not running and queued:
            hag_emit(hag, target, "opencode", "coord_comms", direction="watch-fire",
                     peer="pipeline", note="depth-zero-with-queued",
                     channel="within-ecosystem")
            fire(target, repo_dir, "PIPELINE-WATCH",
                 f"STARVED: 0 running, queued: {queued} — scan approvals + "
                 "preconditions, release or hold-with-condition")
            log(sdir, f"PIPELINE-WATCH STARVED fired: {queued}")
        else:
            print(f"{utcnow()} pipeline state changed (running/queued set moved)")
    else:
        print(f"{utcnow()} pipeline ok no-change {proj} (running=[{running or 'none'}])")
    return 0
