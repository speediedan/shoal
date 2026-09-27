#!/usr/bin/env python3
"""Job units: long local work outside the harness's reapable registry (architecture §5.3).

A test suite, GPU render or A/B run launched as a harness background command dies with
the session and is killed by the Claude harness memory-pressure reaper. As a job unit it
runs under `systemd-run --user` in its own cgroup with a `MemoryHigh` cap, survives the
session, and is followed by a tiny in-session waiter that is safe to re-arm:

    shoal job run  --shoal H --slug S --name suite [--memory-high 24G] [--cwd DIR] -- pytest -x tests/
    shoal job wait --shoal H --slug S --name suite      # run as the background command; re-arm freely
    shoal job status --shoal H [--slug S]
    shoal job stop --shoal H --slug S --name suite

The command runs inside a wrapper that writes its exit code to a status file, because a
collected transient unit forgets its result. `wait` prints exactly one verdict line on
every designed exit: JOB-VERDICT <unit> result=success|failed exit=<rc> | UNIT-GONE-NO-STATUS
| TIMEOUT | NOT-FOUND, followed by the log tail.
"""
from __future__ import annotations

import datetime
import json
import os
import re
import shlex
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "telemetry"))

import config  # noqa: E402
from roles import data_dir, state_dir  # noqa: E402

# Environment a job inherits (systemd-run --user starts from the manager's env, not the
# caller's). Credentials are deliberately not in this list: pass them with --env.
PASS_ENV = ("PATH", "HOME", "USER", "LANG", "VIRTUAL_ENV", "PYTHONPATH", "HF_HOME", "IT_CACHE",
            "VENV_BASE", "CUDA_VISIBLE_DEVICES", "UV_CACHE_DIR", "PIP_CACHE_DIR", "TMPDIR",
            "GH_TOKEN_FILE")


def now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


UNIT_PREFIX = "shoal"
LEGACY_UNIT_PREFIX = "hag"  # job units started before the rename


def unit_name(hag: str, slug: str, name: str, prefix: str = UNIT_PREFIX) -> str:
    raw = f"{prefix}-{hag}-{slug}-{name}"
    return re.sub(r"[^A-Za-z0-9:_.\-]", "-", raw)


def find_unit(hag: str, slug: str, name: str) -> str:
    """The unit a job name refers to: the `shoal-` unit, unless only a legacy `hag-` job of that
    name was ever recorded here (a job started by the pre-rename tool and still followed)."""
    unit = unit_name(hag, slug, name)
    legacy = unit_name(hag, slug, name, LEGACY_UNIT_PREFIX)
    if not os.path.exists(paths(hag, unit)["meta"]) and os.path.exists(paths(hag, legacy)["meta"]):
        return legacy
    return unit


def paths(hag: str, unit: str) -> dict[str, str]:
    jdir = os.path.join(data_dir(hag), "jobs")
    sdir = os.path.join(state_dir(hag), "jobs")
    return {"log": os.path.join(jdir, f"{unit}.log"), "rc": os.path.join(sdir, f"{unit}.rc"),
            "meta": os.path.join(sdir, f"{unit}.json"), "jdir": jdir, "sdir": sdir}


def _systemctl(*argv: str) -> subprocess.CompletedProcess:
    return subprocess.run(["systemctl", "--user", *argv], capture_output=True, text=True, timeout=30)


def unit_state(unit: str) -> dict[str, str]:
    r = _systemctl("show", f"{unit}.service", "-p", "LoadState,ActiveState,SubState,Result,ExecMainStatus")
    d = {}
    for line in r.stdout.splitlines():
        k, _, v = line.partition("=")
        d[k] = v
    return d


def _emit(hag: str, slug: str, kind: str, note: str) -> None:
    try:
        from hag_telemetry import append_event, build_event, resolve_dir
        append_event(resolve_dir(None, hag), build_event(hag, slug, "script", "watch_event",
                                                         [f"kind={kind}", f"slug={slug}", f"note={note}"]))
    except Exception as exc:  # noqa: BLE001
        print(f"(telemetry emit failed: {exc})", file=sys.stderr)


def tail(path: str, n: int = 15) -> str:
    try:
        with open(path, "rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            fh.seek(max(0, size - 8192))
            return "\n".join(fh.read().decode("utf-8", "replace").splitlines()[-n:])
    except OSError:
        return "(no log)"


def cmd_run(a) -> int:
    cmd = a.cmd[1:] if a.cmd and a.cmd[0] == "--" else a.cmd
    if not cmd:
        print("ERROR: job run needs a command after --")
        return 1
    try:  # a freeze (before a restart or maintenance window) starts no new jobs
        from ledger import Ledger, View
        from roles import ShoalSpec, find_spec
        _spec = ShoalSpec.load(find_spec(a.hag, getattr(a, "spec", "") or ""))
        _fz = View(Ledger(a.hag, root=_spec.ledger_dir, sync=False), _spec).freeze()
    except Exception:  # noqa: BLE001 - no spec/ledger reachable (tests, ad-hoc use): not frozen
        _fz = None
    if _fz is not None:
        print(f"JOB-REFUSED: freeze active since {_fz['ts']} ({_fz.get('note') or 'no note'}); "
              f"the maintainer lifts it with `shoal freeze off`")
        return 5
    unit = unit_name(a.hag, a.slug, a.name)
    p = paths(a.hag, unit)
    st = unit_state(unit)
    if st.get("ActiveState") in ("active", "activating", "reloading"):
        print(f"JOB-ALREADY-RUNNING {unit} (follow it with `shoal job wait --shoal {a.hag} --slug {a.slug} --name {a.name}`)")
        return 4
    cwd = os.path.abspath(a.cwd or os.getcwd())
    wrapper = ["/bin/bash", "-c", f'"$@"; rc=$?; echo "$rc" > {shlex.quote(p["rc"])}; exit "$rc"', "shoal-job"]
    argv = ["systemd-run", "--user", "--unit", unit, "--collect", "--quiet",
            "-p", f"WorkingDirectory={cwd}",
            "-p", f"StandardOutput=append:{p['log']}", "-p", f"StandardError=append:{p['log']}"]
    if a.memory_high:
        argv += ["-p", f"MemoryHigh={a.memory_high}"]
    if a.memory_max:
        argv += ["-p", f"MemoryMax={a.memory_max}"]
    for k in PASS_ENV:
        if os.environ.get(k):
            argv += ["-E", f"{k}={os.environ[k]}"]
    for kv in a.env or []:
        argv += ["-E", kv]
    argv += wrapper + cmd
    if a.dry_run:
        print("[dry-run] " + " ".join(shlex.quote(x) for x in argv))
        return 0
    os.makedirs(p["jdir"], exist_ok=True)
    os.makedirs(p["sdir"], exist_ok=True)
    for f in (p["rc"],):
        if os.path.exists(f):
            os.remove(f)
    with open(p["log"], "a") as fh:
        fh.write(f"\n### {now_iso()} job {unit} start: {' '.join(shlex.quote(x) for x in cmd)} (cwd {cwd})\n")
    r = subprocess.run(argv, capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        print(f"ERROR: systemd-run failed rc={r.returncode}: {(r.stdout + r.stderr).strip()[-300:]}")
        return 2
    meta = {"unit": unit, "hag": a.hag, "slug": a.slug, "name": a.name, "cmd": cmd, "cwd": cwd,
            "started_utc": now_iso(), "log": p["log"], "rc_file": p["rc"],
            "memory_high": a.memory_high, "memory_max": a.memory_max}
    with open(p["meta"], "w") as fh:
        json.dump(meta, fh, indent=1)
    _emit(a.hag, a.slug, "job-start", f"{unit} {' '.join(cmd)[:120]}")
    print(f"JOB-STARTED {unit} log={p['log']}\n"
          f"follow with (background command, re-arm freely): "
          f"{config.cli()} job wait --shoal {a.hag} --slug {a.slug} --name {a.name}")
    return 0


def resolve(a) -> tuple[str, dict[str, str]]:
    unit = a.unit or find_unit(a.hag, a.slug, a.name)
    return unit, paths(a.hag, unit)


def cmd_wait(a) -> int:
    unit, p = resolve(a)
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass
    deadline = time.time() + a.timeout_min * 60 if a.timeout_min else None
    seen = False
    print(f"{now_iso()} waiting on {unit} (pid {os.getpid()})")
    while True:
        st = unit_state(unit)
        active = st.get("ActiveState") in ("active", "activating", "deactivating", "reloading")
        seen = seen or active
        if not active:
            rc = None
            try:
                with open(p["rc"]) as fh:
                    rc = int(fh.read().strip())
            except (OSError, ValueError):
                pass
            if rc is not None:
                verdict = f"JOB-VERDICT {unit} result={'success' if rc == 0 else 'failed'} exit={rc}"
            elif st.get("LoadState") == "not-found" and not seen and not os.path.exists(p["meta"]):
                verdict = f"JOB-VERDICT {unit} NOT-FOUND (never started here)"
            else:
                verdict = (f"JOB-VERDICT {unit} UNIT-GONE-NO-STATUS result={st.get('Result', '?')} "
                           f"(killed outside its command: see `journalctl --user -u {unit}.service`)")
            print(verdict)
            print(f"--- log tail ({p['log']}):\n{tail(p['log'], a.tail)}")
            _emit(a.hag, a.slug or "?", "job-verdict", verdict[len("JOB-VERDICT "):][:160])
            return 0 if rc == 0 else 1
        if deadline and time.time() > deadline:
            print(f"JOB-VERDICT {unit} TIMEOUT after {a.timeout_min:g} min (job still running; re-arm to keep following)")
            return 5
        time.sleep(a.poll)


def cmd_status(a) -> int:
    sdir = paths(a.hag, "x")["sdir"]
    try:
        metas = sorted(f for f in os.listdir(sdir) if f.endswith(".json"))
    except OSError:
        metas = []
    if not metas:
        print("no jobs recorded")
        return 0
    for f in metas:
        with open(os.path.join(sdir, f)) as fh:
            m = json.load(fh)
        if a.slug and m.get("slug") != a.slug:
            continue
        st = unit_state(m["unit"])
        rc = "-"
        try:
            with open(m["rc_file"]) as fh:
                rc = fh.read().strip()
        except OSError:
            pass
        print(f"{m['unit']}: {st.get('ActiveState', '?')}/{st.get('SubState', '?')} rc={rc} "
              f"started={m['started_utc']} :: {' '.join(m['cmd'])[:80]}")
    return 0


def cmd_stop(a) -> int:
    unit, _ = resolve(a)
    if a.dry_run:
        print(f"[dry-run] systemctl --user stop {unit}.service")
        return 0
    r = _systemctl("stop", f"{unit}.service")
    print(f"stopped {unit}" if r.returncode == 0 else f"ERROR: {r.stderr.strip()[-200:]}")
    return r.returncode


def add_parsers(sub) -> None:
    p = sub.add_parser("job", help="job units: long local work outside the harness (run/wait/status/stop)")
    jsub = p.add_subparsers(dest="job_cmd", required=True)

    def common(q, need_name=True):
        config.add_id_arg(q, required=True)
        q.add_argument("--slug", default="" if not need_name else None, required=need_name)
        q.add_argument("--name", required=need_name, default="")

    q = jsub.add_parser("run", help="start CMD as a transient user unit")
    common(q)
    q.add_argument("--spec", default="", help="the group spec (for the freeze check; found from the id otherwise)")
    q.add_argument("--memory-high", default="24G", help="MemoryHigh cap (throttles, never kills); '' = none")
    q.add_argument("--memory-max", default="", help="MemoryMax (OOM-kills inside the unit only); default none")
    q.add_argument("--cwd", default="")
    q.add_argument("--env", action="append", help="K=V passed into the unit (repeatable)")
    q.add_argument("--dry-run", action="store_true")
    q.add_argument("cmd", nargs="...")
    q.set_defaults(func=cmd_run)

    q = jsub.add_parser("wait", help="follow a job to its verdict (safe to re-arm)")
    config.add_id_arg(q, required=True)
    q.add_argument("--slug", default="")
    q.add_argument("--name", default="")
    q.add_argument("--unit", default="")
    q.add_argument("--poll", type=float, default=15)
    q.add_argument("--timeout-min", type=float, default=0)
    q.add_argument("--tail", type=int, default=15)
    q.set_defaults(func=cmd_wait)

    q = jsub.add_parser("status", help="list recorded jobs")
    config.add_id_arg(q, required=True)
    q.add_argument("--slug", default="")
    q.set_defaults(func=cmd_status)

    q = jsub.add_parser("stop", help="stop a job unit")
    config.add_id_arg(q, required=True)
    q.add_argument("--slug", default="")
    q.add_argument("--name", default="")
    q.add_argument("--unit", default="")
    q.add_argument("--dry-run", action="store_true")
    q.set_defaults(func=cmd_stop)
