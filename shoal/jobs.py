#!/usr/bin/env python3
"""Job units: long local work outside the harness's reapable registry (architecture §5.3).

A test suite, GPU render or A/B run launched as a harness background command dies with
the session and is killed by the Claude harness memory-pressure reaper. As a job unit it
runs under `systemd-run --user` in its own cgroup with a `MemoryHigh` cap, survives the
session, and is followed by a tiny in-session waiter that is safe to re-arm:

    shoal job run  --shoal H --slug S --name suite [--memory-high 24G] [--cwd DIR] -- pytest -x tests/
    shoal job run  --shoal H --slug S --name suite --on user@host --cwd /remote/dir -- <lease-wrapped cmd>
    shoal job wait --shoal H --slug S --name suite      # run as the background command; re-arm freely
    shoal job status --shoal H [--slug S]
    shoal job stop --shoal H --slug S --name suite

With `--on`, the same transient-unit model runs on another host over ssh (the unit, its log
and its rc file live under ~/shoal-jobs there; the local meta records the host, and
wait/status/stop follow it over ssh). The remote cwd and everything the command needs
(checkout, venv, the lease wrapper) must already exist there: this moves compute, not state.

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


def unit_name(sid: str, slug: str, name: str, prefix: str = UNIT_PREFIX) -> str:
    raw = f"{prefix}-{sid}-{slug}-{name}"
    return re.sub(r"[^A-Za-z0-9:_.\-]", "-", raw)


def find_unit(sid: str, slug: str, name: str) -> str:
    """The unit a job name refers to."""
    return unit_name(sid, slug, name)


def paths(sid: str, unit: str) -> dict[str, str]:
    jdir = os.path.join(data_dir(sid), "jobs")
    sdir = os.path.join(state_dir(sid), "jobs")
    return {"log": os.path.join(jdir, f"{unit}.log"), "rc": os.path.join(sdir, f"{unit}.rc"),
            "meta": os.path.join(sdir, f"{unit}.json"), "jdir": jdir, "sdir": sdir,
            # a placed job's grant-time marker, its start claim (a move takes it first) and its outputs
            "granted": os.path.join(sdir, f"{unit}.granted"), "claim": os.path.join(sdir, f"{unit}.claim"),
            "out": os.path.join(jdir, f"{unit}.out")}


def _systemctl(*argv: str) -> subprocess.CompletedProcess:
    return subprocess.run(["systemctl", "--user", *argv], capture_output=True, text=True, timeout=30)


REMOTE_DIR = "~/shoal-jobs"


def _ssh_argv(host: str) -> list[str]:
    """ssh argv for `host`: a `[hosts.<server>]` route (by server name or by its ssh target) brings its
    options and address resolution; anything else is a plain ssh target."""
    import routes
    rs = routes.routes()
    r = rs.get(host) or next((x for x in rs.values() if str(x.get("ssh")) == host), None)
    if r is not None:
        return routes.ssh_argv(r)
    return ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", host]


def _ssh(host: str, script: str, timeout: int = 60) -> subprocess.CompletedProcess:
    """One non-interactive ssh call running a shell script on `host` (patchable in tests)."""
    return subprocess.run(_ssh_argv(host) + [script], stdin=subprocess.DEVNULL,
                          capture_output=True, text=True, timeout=timeout, env=config.tool_env())


def remote_paths(unit: str, home: str = "") -> dict[str, str]:
    """Absolute remote paths (systemd expands no `~`); `home` from remote_home()."""
    base = f"{home}/shoal-jobs" if home else REMOTE_DIR
    return {"log": f"{base}/{unit}.log", "rc": f"{base}/{unit}.rc", "granted": f"{base}/{unit}.granted",
            "claim": f"{base}/{unit}.claim", "out": f"{base}/{unit}.out"}


def remote_home(host: str) -> str:
    try:
        r = _ssh(host, 'printf %s "$HOME"', timeout=30)
        return r.stdout.strip() if r.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


def remote_state(host: str, unit: str) -> dict[str, str]:
    """{ActiveState, LoadState, rc, granted, rc_mtime} for a remote unit in one ssh call; {} when
    unreachable. `granted` is a placed job's grant-time marker (empty until its lease is granted)."""
    rp = remote_paths(unit)
    script = (f"systemctl --user show {shlex.quote(unit)}.service "
              f"-p LoadState,ActiveState 2>/dev/null; echo ---; cat {rp['rc']} 2>/dev/null || true; "
              f"echo ---; cat {rp['granted']} 2>/dev/null || true; echo ---; stat -c %Y {rp['rc']} 2>/dev/null || true")
    try:
        r = _ssh(host, script, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return {}
    if r.returncode != 0:
        return {}
    parts = (r.stdout.split("---\n") + ["", "", "", ""])[:4]
    d = {}
    for line in parts[0].splitlines():
        k, _, v = line.partition("=")
        d[k] = v
    d["rc"], d["granted"], d["rc_mtime"] = parts[1].strip(), parts[2].strip(), parts[3].strip()
    return d


def unit_state(unit: str) -> dict[str, str]:
    r = _systemctl("show", f"{unit}.service", "-p", "LoadState,ActiveState,SubState,Result,ExecMainStatus")
    d = {}
    for line in r.stdout.splitlines():
        k, _, v = line.partition("=")
        d[k] = v
    return d


def _emit(sid: str, slug: str, kind: str, note: str, extra: dict | None = None) -> None:
    """One watch_event; `extra` adds structured fields (a placement's hosts and predicted starts)."""
    try:
        from shoal_telemetry import append_event, build_event, resolve_dir
        ev = build_event(sid, slug, "script", "watch_event", [f"kind={kind}", f"slug={slug}", f"note={note}"])
        ev.update(extra or {})
        append_event(resolve_dir(None, sid), ev)
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


def last_line(path: str) -> str:
    """A running job's latest log line: what it is doing now, e.g. queued behind CI before it starts
    (measured 2026-10-13: a job read as 'running' for 2 h while its first step waited for an idle
    Azure queue, and nothing outside the log said so)."""
    try:
        with open(path, "rb") as fh:
            fh.seek(max(0, os.path.getsize(path) - 4096))
            lines = [x.strip() for x in fh.read().decode(errors="replace").splitlines() if x.strip()]
        return lines[-1][:160] if lines else ""
    except OSError:
        return ""


def job_meta(sid: str, name: str, slug: str = "") -> dict:
    """The recorded meta of a job by name (any role's, or `slug`'s), or {}."""
    sdir = paths(sid, "x")["sdir"]
    try:
        for f in os.listdir(sdir):
            if f.endswith(".json"):
                m = json.load(open(os.path.join(sdir, f)))
                if m.get("name") == name and (not slug or m.get("slug") == slug):
                    return m
    except (OSError, ValueError):
        pass
    return {}


def _run_remote(a, cmd: list[str], unit: str, p: dict) -> int:
    host = a.on
    cwd = a.cwd or ""
    if not cwd:
        print("ERROR: job run --on needs --cwd (a directory that already exists on the remote host)")
        return 1
    home = "" if a.dry_run else remote_home(host)
    if not a.dry_run and not home:
        print(f"ERROR: ssh to {host} failed (no home directory answer); is the host reachable non-interactively?")
        return 2
    rp = remote_paths(unit, home)
    st = remote_state(host, unit)
    if st.get("ActiveState") in ("active", "activating", "reloading"):
        print(f"JOB-ALREADY-RUNNING {unit} on {host} (follow it with `shoal job wait --shoal {a.sid} --slug {a.slug} --name {a.name}`)")
        return 4
    inner = f'"$@"; rc=$?; echo "$rc" > {rp["rc"]}; exit "$rc"'
    rargv = ["systemd-run", "--user", "--unit", unit, "--collect", "--quiet",
             "-p", f"WorkingDirectory={cwd}",
             "-p", f"StandardOutput=append:{rp['log']}", "-p", f"StandardError=append:{rp['log']}"]
    if a.memory_high:
        rargv += ["-p", f"MemoryHigh={a.memory_high}"]
    if a.memory_max:
        rargv += ["-p", f"MemoryMax={a.memory_max}"]
    for kv in a.env or []:
        rargv += ["-E", kv]
    rargv += ["/bin/bash", "-c", inner, "shoal-job"] + cmd
    launch = " ".join(shlex.quote(x) for x in rargv)
    script = (f"test -d {shlex.quote(cwd)} || {{ echo 'no such remote cwd: {cwd}' >&2; exit 3; }}; "
              f"mkdir -p {shlex.quote(os.path.dirname(rp['log']))} && rm -rf {rp['rc']} {rp['granted']} {rp['claim']} && "
              f"printf '\\n### job {unit} start on %s\\n' \"$(date -u +%FT%TZ)\" >> {rp['log']} && {launch}")
    if a.dry_run:
        print(f"[dry-run] ssh {host} {script}")
        return 0
    try:
        r = _ssh(host, script)
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"ERROR: ssh to {host} failed: {exc}")
        return 2
    if r.returncode != 0:
        print(f"ERROR: remote start on {host} failed rc={r.returncode}: {(r.stdout + r.stderr).strip()[-300:]}")
        return 2
    os.makedirs(p["sdir"], exist_ok=True)
    meta = {"unit": unit, "shoal": a.sid, "slug": a.slug, "name": a.name, "cmd": cmd, "cwd": cwd,
            "started_utc": now_iso(), "host": host, "log": rp["log"], "rc_file": rp["rc"],
            "memory_high": a.memory_high, "memory_max": a.memory_max, "class": getattr(a, "job_class", "ci")}
    with open(p["meta"], "w") as fh:
        json.dump(meta, fh, indent=1)
    _emit(a.sid, a.slug, "job-start", f"{unit} on {host} {' '.join(cmd)[:100]}")
    print(f"JOB-STARTED {unit} on {host} log={host}:{rp['log']}\n"
          f"follow with (background command, re-arm freely): "
          f"{config.cli()} job wait --shoal {a.sid} --slug {a.slug} --name {a.name}")
    return 0


def cmd_run(a) -> int:
    cmd = a.cmd[1:] if a.cmd and a.cmd[0] == "--" else a.cmd
    if not cmd:
        print("ERROR: job run needs a command after --")
        return 1
    try:  # a freeze (before a restart or maintenance window) starts no new jobs
        from ledger import Ledger, View
        from roles import ShoalSpec, find_spec
        _spec = ShoalSpec.load(find_spec(a.sid, getattr(a, "spec", "") or ""))
        _fz = View(Ledger(a.sid, root=_spec.ledger_dir, sync=False), _spec).freeze()
    except Exception:  # noqa: BLE001 - no spec/ledger reachable (tests, ad-hoc use): not frozen
        _fz = None
    if _fz is not None:
        print(f"JOB-REFUSED: freeze active since {_fz['ts']} ({_fz.get('note') or 'no note'}); "
              f"the maintainer lifts it with `shoal freeze off`")
        return 5
    unit = unit_name(a.sid, a.slug, a.name)
    p = paths(a.sid, unit)
    if getattr(a, "place", ""):
        if getattr(a, "on", ""):
            print("ERROR: --place and --on are exclusive (--place <host> pins a pool host)")
            return 1
        import pool_cli
        return pool_cli.run_placed(a, cmd, unit, p)
    if getattr(a, "on", ""):
        return _run_remote(a, cmd, unit, p)
    st = unit_state(unit)
    if st.get("ActiveState") in ("active", "activating", "reloading"):
        print(f"JOB-ALREADY-RUNNING {unit} (follow it with `shoal job wait --shoal {a.sid} --slug {a.slug} --name {a.name}`)")
        return 4
    return _run_local(a, cmd, unit, p)


def _run_local(a, cmd: list[str], unit: str, p: dict) -> int:
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
    for f in (p["rc"], p["granted"]):
        if os.path.exists(f):
            os.remove(f)
    if os.path.isdir(p["claim"]):
        os.rmdir(p["claim"])
    with open(p["log"], "a") as fh:
        fh.write(f"\n### {now_iso()} job {unit} start: {' '.join(shlex.quote(x) for x in cmd)} (cwd {cwd})\n")
    r = subprocess.run(argv, capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        print(f"ERROR: systemd-run failed rc={r.returncode}: {(r.stdout + r.stderr).strip()[-300:]}")
        return 2
    meta = {"unit": unit, "shoal": a.sid, "slug": a.slug, "name": a.name, "cmd": cmd, "cwd": cwd,
            "started_utc": now_iso(), "log": p["log"], "rc_file": p["rc"],
            "memory_high": a.memory_high, "memory_max": a.memory_max, "class": getattr(a, "job_class", "ci")}
    with open(p["meta"], "w") as fh:
        json.dump(meta, fh, indent=1)
    _emit(a.sid, a.slug, "job-start", f"{unit} {' '.join(cmd)[:120]}")
    print(f"JOB-STARTED {unit} log={p['log']}\n"
          f"follow with (background command, re-arm freely): "
          f"{config.cli()} job wait --shoal {a.sid} --slug {a.slug} --name {a.name}")
    return 0


def resolve(a) -> tuple[str, dict[str, str]]:
    unit = a.unit or find_unit(a.sid, a.slug, a.name)
    return unit, paths(a.sid, unit)


def _meta_for(a, unit: str) -> dict:
    try:
        return json.load(open(paths(a.sid, unit)["meta"]))
    except (OSError, ValueError):
        return {}


def yield_for_rescan(a, unit: str) -> bool:
    """A monitor must not keep its session uninterruptible past that session's rescan promise."""
    if not a.slug:
        return False
    from state import wait_status
    status, proof = wait_status(a.sid, a.slug)
    if status != "stale" or not proof or proof.get("sub") not in ("local-ci", "remote-ci"):
        return False
    kind, _, ref = str(proof.get("target") or "").partition(":")
    if kind != "job":
        return False
    expected = ref.removesuffix(".service") if ref.startswith("shoal-") else find_unit(a.sid, a.slug, ref)
    if expected != unit.removesuffix(".service"):
        return False
    text = f"{unit} RESCAN-DUE: monitor yielded; job unit was not stopped. Revalidate and write a fresh queue/wait."
    print("JOB-WAIT-VERDICT " + text)
    _emit(a.sid, a.slug, "job-wait-yield", text)
    return True


# a placed job moved hosts while its waiter followed it: re-read the meta and follow it there
MOVED = -1000


def _placed_poll(a, unit: str, meta: dict, granted: str) -> bool:
    """Per-poll duties while a job's unit is active: a CI job's runtime budget (an `overrun` event and
    one page past it; never a kill) and, for a placed job, recording the lease grant and, as its single
    supervisor under features.placement, moving it while still queued. True when it moved."""
    import pool_cli
    if meta.get("started_utc"):
        pool_cli.budget_check(a, unit, meta, granted)
    if not meta.get("placement"):
        return False
    return pool_cli.supervise(a, unit, meta, granted) == "moved"


def _placed_gone(a, unit: str, meta: dict) -> str:
    """For a placed job whose unit is gone: "moved" (the meta now names another run), "moving" (a move
    is between stopping the unit and starting it elsewhere) or "" (a real verdict)."""
    if not meta.get("placement"):
        return ""
    now_meta = _meta_for(a, unit)
    if (now_meta.get("host") or "", now_meta.get("started_utc")) != (meta.get("host") or "", meta.get("started_utc")):
        return "moved"
    mv = (now_meta.get("placement") or {}).get("moving") or {}
    return "moving" if mv and time.time() - float(mv.get("ts") or 0) < 900 else ""


def _placed_verdict(a, unit: str, meta: dict, rc, granted: str, end_ts) -> None:
    if meta.get("placement"):
        import pool_cli
        pool_cli.on_verdict(a, unit, meta, rc, granted, end_ts)


def _wait_remote(a, unit: str, meta: dict) -> int:
    host = meta["host"]
    rp = {"log": meta.get("log") or remote_paths(unit)["log"]}
    deadline = time.time() + a.timeout_min * 60 if a.timeout_min else None
    misses = 0
    print(f"{now_iso()} waiting on {unit} on {host} (pid {os.getpid()})")
    while True:
        st = remote_state(host, unit)
        if not st:
            misses += 1
            if misses >= 10:
                print(f"JOB-VERDICT {unit} UNREACHABLE ({host} answered no ssh in {misses} polls; "
                      f"the job may still be running there)")
                return 6
        else:
            misses = 0
            if st.get("ActiveState") in ("active", "activating", "deactivating", "reloading"):
                if _placed_poll(a, unit, meta, st.get("granted", "")):
                    return MOVED
            else:
                gone = _placed_gone(a, unit, meta) if not st.get("rc") else ""
                if gone == "moved":
                    return MOVED
                if gone == "moving":
                    time.sleep(a.poll)
                    continue
                rc = int(st["rc"]) if (st.get("rc") or "").lstrip("-").isdigit() else None
                if rc is not None:
                    verdict = f"JOB-VERDICT {unit} result={'success' if rc == 0 else 'failed'} exit={rc}"
                elif st.get("LoadState") == "not-found" and not meta:
                    verdict = f"JOB-VERDICT {unit} NOT-FOUND (never started there)"
                else:
                    verdict = f"JOB-VERDICT {unit} UNIT-GONE-NO-STATUS (see {host} journal for {unit}.service)"
                print(verdict)
                try:
                    r = _ssh(host, f"tail -n {int(a.tail)} {rp['log']} 2>/dev/null", timeout=30)
                    print(f"--- log tail ({host}:{rp['log']}):\n{r.stdout.rstrip() or '(no log)'}")
                except (OSError, subprocess.TimeoutExpired):
                    print("--- log tail: (unreachable)")
                end = float(st["rc_mtime"]) if (st.get("rc_mtime") or "").isdigit() else None
                _placed_verdict(a, unit, meta, rc, st.get("granted", ""), end)
                _emit(a.sid, a.slug or "?", "job-verdict", verdict[len("JOB-VERDICT "):][:160])
                return 0 if rc == 0 else 1
        if deadline and time.time() > deadline:
            print(f"JOB-VERDICT {unit} TIMEOUT after {a.timeout_min:g} min (job still running; re-arm to keep following)")
            return 5
        if yield_for_rescan(a, unit):
            return 75
        time.sleep(a.poll)


def _read(path: str) -> str:
    try:
        with open(path) as fh:
            return fh.read().strip()
    except (OSError, TypeError):
        return ""


def _wait_local(a, unit: str, p: dict, meta: dict) -> int:
    deadline = time.time() + a.timeout_min * 60 if a.timeout_min else None
    seen = False
    print(f"{now_iso()} waiting on {unit} (pid {os.getpid()})")
    while True:
        st = unit_state(unit)
        active = st.get("ActiveState") in ("active", "activating", "deactivating", "reloading")
        seen = seen or active
        if active and _placed_poll(a, unit, meta, _read(p.get("granted"))):
            return MOVED
        if not active:
            rc = None
            try:
                with open(p["rc"]) as fh:
                    rc = int(fh.read().strip())
            except (OSError, ValueError):
                pass
            gone = _placed_gone(a, unit, meta) if rc is None else ""
            if gone == "moved":
                return MOVED
            if gone == "moving":
                time.sleep(a.poll)
                continue
            if rc is not None:
                verdict = f"JOB-VERDICT {unit} result={'success' if rc == 0 else 'failed'} exit={rc}"
            elif st.get("LoadState") == "not-found" and not seen and not os.path.exists(p["meta"]):
                verdict = f"JOB-VERDICT {unit} NOT-FOUND (never started here)"
            else:
                verdict = (f"JOB-VERDICT {unit} UNIT-GONE-NO-STATUS result={st.get('Result', '?')} "
                           f"(killed outside its command: see `journalctl --user -u {unit}.service`)")
            print(verdict)
            print(f"--- log tail ({p['log']}):\n{tail(p['log'], a.tail)}")
            if meta.get("placement"):
                end = os.path.getmtime(p["rc"]) if rc is not None else None
                _placed_verdict(a, unit, meta, rc, _read(p.get("granted")), end)
            _emit(a.sid, a.slug or "?", "job-verdict", verdict[len("JOB-VERDICT "):][:160])
            return 0 if rc == 0 else 1
        if deadline and time.time() > deadline:
            print(f"JOB-VERDICT {unit} TIMEOUT after {a.timeout_min:g} min (job still running; re-arm to keep following)")
            return 5
        if yield_for_rescan(a, unit):
            return 75
        time.sleep(a.poll)


def cmd_wait(a) -> int:
    unit, p = resolve(a)
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass
    while True:
        meta = _meta_for(a, unit)
        rc = _wait_remote(a, unit, meta) if meta.get("host") else _wait_local(a, unit, p, meta)
        if rc != MOVED:
            return rc
        print(f"{now_iso()} {unit} moved; following it to its new host")


def cmd_status(a) -> int:
    sdir = paths(a.sid, "x")["sdir"]
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
        if m.get("host"):
            st = remote_state(m["host"], m["unit"])
            where = f" on {m['host']}"
            granted = (f" lease_granted={m.get('lease_granted') or st.get('granted') or '-'}"
                       if m.get("placement") else "")
            print(f"{m['unit']}{where}: {st.get('ActiveState', 'unreachable')} rc={st.get('rc') or '-'} "
                  f"started={m['started_utc']}{granted} :: {' '.join(m['cmd'])[:80]}")
            continue
        st = unit_state(m["unit"])
        rc = "-"
        try:
            with open(m["rc_file"]) as fh:
                rc = fh.read().strip()
        except OSError:
            pass
        last = last_line(m.get("log", "")) if st.get("ActiveState") == "active" else ""
        granted = (f" lease_granted={m.get('lease_granted') or _read(paths(a.sid, m['unit'])['granted']) or '-'}"
                   if m.get("placement") else "")
        print(f"{m['unit']}: {st.get('ActiveState', '?')}/{st.get('SubState', '?')} rc={rc} "
              f"started={m['started_utc']}{granted} :: {' '.join(m['cmd'])[:80]}" + (f"\n    last: {last}" if last else ""))
    return 0


def cmd_stop(a) -> int:
    unit, _ = resolve(a)
    meta = _meta_for(a, unit)
    if meta.get("host"):
        if a.dry_run:
            print(f"[dry-run] ssh {meta['host']} systemctl --user stop {unit}.service")
            return 0
        try:
            r = _ssh(meta["host"], f"systemctl --user stop {shlex.quote(unit)}.service")
        except (OSError, subprocess.TimeoutExpired) as exc:
            print(f"ERROR: ssh to {meta['host']} failed: {exc}")
            return 2
        print(f"stopped {unit} on {meta['host']}" if r.returncode == 0 else f"ERROR: {r.stderr.strip()[-200:]}")
        return r.returncode
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
    q.add_argument("--on", default="", help="ssh target (user@host): run the unit there instead; "
                                            "--cwd must exist on that host, lease wrapping is the command's job")
    q.add_argument("--env", action="append", help="K=V passed into the unit (repeatable)")
    q.add_argument("--dry-run", action="store_true")
    q.add_argument("--class", dest="job_class", choices=("ci", "research"), default="ci",
                   help="ci (default): a runtime budget (features.placement.ci_budget_min, 90 min) past which "
                        "`job wait` pages once, never kills; research: no budget, and with --place it runs "
                        "only inside a reservation its role owns (`shoal pool reserve`)")
    g = q.add_argument_group("pool placement (architecture doc §6; omit --place for a plain job)")
    g.add_argument("--place", default="", metavar="auto|HOST",
                   help="auto: the pool host with the earliest predicted finish; HOST: pin that pool host")
    g.add_argument("--needs", default="cpu", metavar="cpu|gpu[:N][:vendor][:vramGiB]",
                   help="cpu (default) or gpu: count, vendor any|nvidia|amd (default: the home host's), min VRAM")
    g.add_argument("--cpu-heavy", action="store_true",
                   help="also take the cpu-heavy lease (CPU-only work takes only `--lease cpu-heavy`)")
    g.add_argument("--memory-gib", type=float, default=0, help="host memory the job needs available")
    g.add_argument("--ci-env", default="", metavar="NAME", help="a named CI environment ([ci_envs.NAME] on the host)")
    g.add_argument("--repo", default="", metavar="PATH", help="the local checkout whose pushed SHA is staged elsewhere")
    g.add_argument("--ref", default="", metavar="SHA", help="the commit to run (default: the checkout's HEAD)")
    g.add_argument("--est", type=float, default=0, metavar="MIN", help="duration prior in minutes")
    g.add_argument("--priority", type=int, default=0, help="planning order: lower places first (default 0)")
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
