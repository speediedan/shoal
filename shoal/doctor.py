#!/usr/bin/env python3
"""Preflight + live health: `shoal doctor [--spec shoal.json]`.

Runs before a launch (no spec: host checks only) and any time after
(with a spec: adds per-shoal checks). Every check prints one line
`OK|WARN|FAIL <name>: <detail>`; exit 1 on any FAIL. Nothing is mutated.

Host checks: python version, tool binaries on the explicit tool PATH
(openchamber, claude, gh, az, git, systemctl), openchamber server
reachable, AZURE_DEVOPS_EXT_PAT resolvable, gh auth, shoal roots writable,
linger enabled,
stale systemd units pointing at missing files.

shoal checks: spec valid; start docs present with required sections; live
session ids present + reachable; heartbeat/wait posture per role; watcher
ticks vs 3x interval; systemd timers present and active for this shoal (each
.timer unit's own ActiveState); parked roles (no session) report INFO;
telemetry dir writable + last event age; reconcile tick age. Ledger: clone
present with a reachable remote, no unpushed commits older than an hour,
lint clean (no violations, no contested claims), and one ledger-watch
sidecar per role bound to its session with a live pid.
"""
from __future__ import annotations

import argparse
import datetime
import glob
import os
import re
import shutil
import sqlite3
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import config  # noqa: E402
from backends import get as get_backend  # noqa: E402
from heartbeat import age_min, classify  # noqa: E402
from roles import SHOAL_DATA_ROOT, ShoalSpec, state_dir, telemetry_dir  # noqa: E402

ENV = config.tool_env()
TICK_INTERVAL_MIN = {"lanes": 5, "pipeline": 10, "heartbeat": 5,
                     "reconcile": 24 * 60, "weekly-report": 7 * 24 * 60}


def tick_jobs(spec) -> dict:
    """The self-ticking jobs this spec actually installs (features change the set):
    health-a/-b replace heartbeat, digest joins, reconcile runs twice a day."""
    feats = (getattr(spec, "features", {}) or {}) if spec is not None else {}
    jobs = dict(TICK_INTERVAL_MIN)
    jobs["reconcile"] = 12 * 60
    if feats.get("health"):
        jobs.pop("heartbeat", None)
        jobs["health-a"] = 5
        jobs["health-b"] = 5
    if feats.get("digest") and getattr(spec, "sources", None):
        jobs["digest"] = 10
    return jobs


class Doc:
    def __init__(self):
        self.fails = 0
        self.warns = 0

    def ok(self, name, detail=""):
        print(f"OK   {name}: {detail}")

    def info(self, name, detail=""):
        print(f"INFO {name}: {detail}")

    def warn(self, name, detail=""):
        self.warns += 1
        print(f"WARN {name}: {detail}")

    def fail(self, name, detail=""):
        self.fails += 1
        print(f"FAIL {name}: {detail}")


def run(argv, timeout=30, env=None):
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                          env=env or ENV)


def host_checks(d: Doc) -> None:
    v = sys.version_info
    (d.ok if v >= (3, 10) else d.fail)("python", f"{sys.executable} {v.major}.{v.minor}")
    cfg = config.summary()
    d.ok("config", f"{cfg['config_file']}; lease_cmd={cfg['lease_cmd'] or '(none)'}; cli={cfg['cli']}")
    try:
        import skills as _skills
        st = _skills.stale()
        (d.warn if st else d.ok)("skill copies", (f"stale: {', '.join(st)} (shoal upgrade syncs them)" if st else
                                               f"current in {len(config.skill_homes())} home(s)"))
    except Exception as exc:  # noqa: BLE001
        d.warn("skill copies", f"check failed: {exc}")
    for tool in ("openchamber", "claude", "gh", "az", "git", "systemctl"):
        p = shutil.which(tool, path=ENV["PATH"])
        (d.ok if p else d.fail)(f"tool {tool}", p or "not found on tool PATH")
    try:
        r = run(["openchamber", "status"], timeout=30)
        port = next((ln.strip(" │◆") for ln in r.stdout.splitlines() if "port" in ln), "?")
        (d.ok if r.returncode == 0 else d.fail)("openchamber server", port[:80])
    except Exception as exc:  # noqa: BLE001
        d.fail("openchamber server", str(exc))
    try:
        r = run(["gh", "auth", "status"], timeout=30)
        (d.ok if r.returncode == 0 else d.fail)("gh auth", (r.stdout + r.stderr).strip().splitlines()[-1][:80] if (r.stdout + r.stderr).strip() else "?")
    except Exception as exc:  # noqa: BLE001
        d.fail("gh auth", str(exc))
    pat = os.environ.get("AZURE_DEVOPS_EXT_PAT", "")
    if not pat:
        try:
            for line in open(os.path.expanduser("~/.bashrc")):
                if re.match(r"export AZURE_DEVOPS_EXT_PAT=", line.strip()):
                    pat = "from ~/.bashrc"
                    break
        except OSError:
            pass
    (d.ok if pat else d.warn)("AZURE_DEVOPS_EXT_PAT", "resolvable" if pat else "unset (pipeline watcher will refuse to judge)")
    for name, root in (("SHOAL_DATA_ROOT", SHOAL_DATA_ROOT),):
        if os.path.isdir(root) and os.access(root, os.W_OK):
            d.ok(name, root)
        elif not os.path.isdir(root):
            d.fail(name, f"{root} missing (mkdir -p it; the bulk tier holds telemetry)")
        else:
            d.fail(name, f"{root} not writable")
    try:
        import getpass
        r = run(["loginctl", "show-user", os.environ.get("USER") or getpass.getuser(), "-p", "Linger"])
        (d.ok if "Linger=yes" in r.stdout else d.warn)("linger", r.stdout.strip() or "unknown (timers die at logout)")
    except Exception as exc:  # noqa: BLE001
        d.warn("linger", str(exc))
    unit_dir = os.path.expanduser("~/.config/systemd/user")
    stale = []
    for svc in glob.glob(os.path.join(unit_dir, "shoal-*.service")):
        for line in open(svc):
            if line.startswith("ExecStart="):
                parts = line[len("ExecStart="):].split()
                if len(parts) > 1 and parts[1].startswith("/") and not os.path.exists(parts[1]):
                    stale.append(os.path.basename(svc))
    (d.warn if stale else d.ok)("systemd units", f"{len(stale)} unit(s) point at missing scripts: {stale}" if stale else "no stale ExecStart paths")


def provider_failures(session_ids: list[str], hours: float = 24) -> dict:
    """Read-only scan of the opencode message store for failed-send records.

    Catches the provider-outage class (e.g. stale encrypted-reasoning blobs
    rejected with "not issued to this caller"): those failures land as error
    message rows, never as assistant output, so heartbeat/watcher checks read
    them as a quiet world. Returns counts + a sample; never raises.
    The store layout (v1 `message`, v2 `session_message`) is read through
    opencode_store, so the scan follows an opencode 2.x upgrade.
    """
    out: dict = {"total": 0, "by_session": {}, "last_utc": "",
                 "last_hour": False, "sample": "", "store": ""}
    try:
        from opencode_store import connect_ro, db_path, message_source
        db = db_path()
        if not os.path.isfile(db) or not session_ids:
            return out
        cutoff_ms = (datetime.datetime.now(datetime.timezone.utc).timestamp()
                     - hours * 3600) * 1000
        con = connect_ro(db)
        try:
            out["store"], table, extra = message_source(con)
            q = (f"SELECT session_id, time_created, substr(data, 1, 300) FROM {table} "
                 f"WHERE time_created >= ? AND data LIKE '%was not issued to this caller%'{extra}")
            for sid, ts, head in con.execute(q, (cutoff_ms,)):
                if sid not in session_ids and not any(
                        sid.startswith(s[:8]) or s.startswith(sid[:8])
                        for s in session_ids):
                    continue
                out["total"] += 1
                out["by_session"][sid] = out["by_session"].get(sid, 0) + 1
                tutc = datetime.datetime.fromtimestamp(
                    ts / 1000, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%MZ")
                if not out["last_utc"] or tutc > out["last_utc"]:
                    out["last_utc"] = tutc
                    m = re.search(r"Upstream request failed: ([^\"\\]{0,140})", head)
                    out["sample"] = m.group(1) if m else head[:120]
                if ts >= (cutoff_ms + (hours - 1) * 3600000):
                    out["last_hour"] = True
        finally:
            con.close()
    except Exception:
        pass
    return out


def tools_skew_note(armed_utc: str) -> str:
    """Non-empty when the watch was armed before the newest shoal/ tool commit:
    the running process executes pre-fix code (measured 2026-09-21: a holder
    gate fixed after arming left a live watcher silently not offering)."""
    try:
        top = subprocess.run(["git", "-C", HERE, "rev-parse", "--show-toplevel"],
                             capture_output=True, text=True, timeout=30).stdout.strip()
        rel = os.path.relpath(HERE, top) if top else ""
        out = subprocess.run(["git", "log", "-1", "--format=%cI", "--",
                              (rel + "/") if rel and rel != "." else "."],
                             capture_output=True, text=True, timeout=30,
                             cwd=top or HERE).stdout.strip()
        # %cI is strict ISO 8601 with the committer's offset; %ci parsed as UTC was off by it
        tools_ts = datetime.datetime.fromisoformat(out.replace("Z", "+00:00")).astimezone(datetime.timezone.utc)
        armed = datetime.datetime.strptime(armed_utc, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=datetime.timezone.utc)
    except Exception:
        return ""
    if armed < tools_ts:
        return (f"armed {armed_utc} predates tool update "
                f"{tools_ts.strftime('%Y-%m-%dT%H:%MZ')}: kill + re-arm")
    return ""


def parse_timer_show(text: str) -> dict[str, dict[str, str]]:
    """`systemctl show -p Id -p ActiveState -p SubState -p UnitFileState` output (blank-line
    separated blocks) -> {Id: {ActiveState, SubState, UnitFileState}}."""
    out: dict[str, dict[str, str]] = {}
    for block in re.split(r"\n\s*\n", text.strip()):
        props = dict(ln.split("=", 1) for ln in block.splitlines() if "=" in ln)
        if props.get("Id"):
            out[props["Id"]] = props
    return out


def timer_state(sid: str) -> dict[str, dict[str, str]]:
    """{job: {ActiveState, SubState, UnitFileState}} for this shoal's timers, judged from each
    `.timer` unit's own state. `list-timers` was parsed before, and its NEXT column reads
    n/a while the timer's service is activating or has just run, so a healthy timer read
    inactive (measured 2026-09-28: three timers FAIL right after `install-systemd --enable`
    while `systemctl --user is-active` said active)."""
    pat = f"shoal-{sid}-*.timer"
    names: set[str] = set()
    for argv in (["systemctl", "--user", "list-unit-files", "--no-legend", "--no-pager", pat],
                 ["systemctl", "--user", "list-units", "--all", "--no-legend", "--no-pager", "--plain", pat]):
        try:
            r = run(argv, timeout=30)
        except Exception:  # noqa: BLE001
            continue
        for line in r.stdout.splitlines():
            tok = line.split()
            if tok and tok[0].startswith(f"shoal-{sid}-") and tok[0].endswith(".timer"):
                names.add(tok[0])
    if not names:
        return {}
    r = run(["systemctl", "--user", "show", "-p", "Id", "-p", "ActiveState", "-p", "SubState",
             "-p", "UnitFileState", *sorted(names)], timeout=30)
    out = {}
    for unit, props in parse_timer_show(r.stdout).items():
        out[unit[len(f"shoal-{sid}-"):-len(".timer")]] = props
    return out


def timer_verdict(timers: dict[str, dict[str, str]]) -> tuple[str, str]:
    """(ok|warn|fail, detail). A timer is good when its own ActiveState is active (SubState
    waiting, running or elapsed); a running or just-finished service never makes it read
    inactive. Inactive or failed is a FAIL; active but disabled is a WARN (it will not come
    back after a reboot)."""
    bad = {k: f"{v.get('ActiveState', '?')}/{v.get('SubState', '?')}, {v.get('UnitFileState', '?')}"
           for k, v in timers.items() if v.get("ActiveState") != "active"}
    if bad:
        return "fail", f"{len(timers)} installed; not active: " + "; ".join(f"{k} ({why})" for k, why in sorted(bad.items()))
    disabled = sorted(k for k, v in timers.items() if v.get("UnitFileState") in ("disabled", "masked"))
    if disabled:
        return "warn", f"{len(timers)} active; disabled (gone after a reboot): {disabled}"
    return "ok", f"{sorted(timers)} active"


def group_checks(d: Doc, spec: ShoalSpec, spec_path: str, threshold_min: float) -> None:
    errs = spec.validate()
    (d.ok if not errs else d.fail)("spec", f"{spec_path} incarnation={spec.incarnation} ({spec.display_gen}) roles={[r.slug for r in spec.roles]}"
                                   if not errs else "; ".join(errs))
    if spec.retired_utc:
        d.warn("spec retired", f"retired_utc={spec.retired_utc}; launches refuse this spec")
    for r in spec.roles:
        if r.doc and not os.path.isabs(r.doc):
            d.warn(f"doc path {r.slug}", f"relative role doc {r.doc!r} resolves against the caller's cwd; "
                                         f"fix with `shoal spec absolutize-docs --spec {spec_path} --base DIR`")
        if not r.doc or not os.path.exists(r.doc):
            d.fail(f"doc {r.slug}", f"missing: {r.doc}")
            continue
        text = open(r.doc).read().lower()
        missing = [s for s in ("grants-needed", "current state") if s not in text]
        (d.ok if not missing else d.fail)(f"doc {r.slug}", os.path.basename(r.doc) if not missing else f"missing sections {missing}")
    live = [r for r in spec.roles if r.session]
    if not live:
        d.warn("sessions", "no live session ids recorded (pre-launch, or start ran --dry-run)")
    for r in live:
        try:
            st = get_backend(r.ecosystem).status(r.session, spec.dir)
            bad = "not-listed" in st or "unreachable" in st
            (d.warn if bad else d.ok)(f"session {r.slug}", f"{r.session} {st[:60]}")
        except Exception as exc:  # noqa: BLE001
            d.warn(f"session {r.slug}", f"{r.session}: {exc}")
    if live:
        for row in classify(spec, threshold_min):
            if row["klass"] == "parked":
                d.info(f"heartbeat {row['slug']}", row["detail"])
                continue
            (d.warn if row["suspect"] else d.ok)(f"heartbeat {row['slug']}", f"{row['klass']}: {row['detail']}")
        for job, interval in tick_jobs(spec).items():
            age = age_min(os.path.join(state_dir(spec.sid), f"tick-{job}"))
            if age is None:
                d.warn(f"tick {job}", "never ran")
            elif age > 3 * interval:
                d.fail(f"tick {job}", f"{age:.0f}m old > 3x{interval}m: watcher dead, not world quiet")
            else:
                d.ok(f"tick {job}", f"{age:.0f}m ago")
        timers = timer_state(spec.sid)
        if not timers:
            d.fail("systemd timers", f"none installed for {spec.sid} (shoal install-systemd --enable)")
        else:
            verdict, detail = timer_verdict(timers)
            {"ok": d.ok, "warn": d.warn, "fail": d.fail}[verdict]("systemd timers", detail)
    ledger_checks(d, spec)
    matcher_checks(d, spec)
    tdir = telemetry_dir(spec.sid)
    if os.path.isdir(tdir):
        files = sorted(glob.glob(os.path.join(tdir, "events-*.jsonl")))
        last = age_min(files[-1]) if files else None
        d.ok("telemetry", f"{tdir}: {len(files)} day file(s), last write {'never' if last is None else f'{last:.0f}m ago'}")
    else:
        d.warn("telemetry", f"{tdir} absent (created on first emit)")
    if live:
        fails = provider_failures([r.session for r in live if r.session], hours=24)
        if fails["store"]:
            d.ok("opencode store", f"{fails['store']} layout (provider-error scan reads "
                                   f"{'session_message' if fails['store'] == 'v2' else 'message'})")
        if fails["total"]:
            recent = fails["last_utc"] or "unknown"
            (d.fail if fails["last_hour"] else d.warn)(
                "provider errors",
                f"{fails['total']} failed-send records in 24h across {len(fails['by_session'])} session(s), "
                f"last {recent} :: {fails['sample'][:160]}")


def matcher_checks(d: Doc, spec: ShoalSpec) -> None:
    """features.matcher (architecture §4.1): the model that last answered, and freshness."""
    import matcher
    c = matcher.config(spec)
    if c is None:
        return
    st = matcher.load_state(spec.sid)
    mode = "live" if c.get("live") else "shadow"
    if c.get("backend") == "opencode-run" and not os.path.exists(matcher.openchamber_bin()):
        d.fail("matcher", f"{mode}: openchamber not found at {matcher.openchamber_bin()} (model calls go "
                          f"through the OpenChamber-managed server)")
        return
    act = (st.get("active") or {}).get("model")
    if not act:
        d.warn("matcher", f"{mode}: no model has answered yet (configured {c.get('model')}); "
                          f"`shoal digest --shoal {spec.sid} --refresh` runs it")
        return
    tier = matcher.tier_of(st["active"])
    pinned = "" if act == c.get("model") else f" (FALLBACK: configured {c.get('model')} did not answer)"
    paid = {"go": "; PAID tier: Go subscription usage", "zen": "; PAID tier: Zen balance"}.get(tier, "")
    (d.warn if paid else d.ok)("matcher", f"{mode}: model {act} ({tier} tier){pinned}{paid}, last answered "
                                          f"{st.get('last_ok', '?')}, "
                    f"{sum(1 for k in matcher.load_cache(spec.sid) if not k.startswith('_'))} items cached")


def ledger_checks(d: Doc, spec: ShoalSpec) -> None:
    try:
        from ledger import Ledger, View, git, lint, unpushed
        from ledger_watch import load_sidecar, sidecar_path
    except Exception as exc:  # noqa: BLE001
        d.fail("ledger modules", str(exc))
        return
    root = spec.ledger_dir
    if not os.path.isdir(os.path.join(root, ".git")):
        d.fail("ledger clone", f"{root} is not a git clone (shoal ledger-init --spec ...)")
        return
    d.ok("ledger clone", root)
    from layout import spec_path
    d.ok("ledger layout", os.path.relpath(spec_path(root, spec.sid), root))
    try:
        r = git(root, "ls-remote", "--exit-code", "--heads", "origin", timeout=30, check=False)
        (d.ok if r.returncode == 0 else d.fail)("ledger remote", "reachable" if r.returncode == 0
                                                else f"unreachable: {(r.stdout + r.stderr).strip()[-120:]}")
    except Exception as exc:  # noqa: BLE001
        d.fail("ledger remote", str(exc))
    led = Ledger(spec.sid, root=root)
    if not led.exists():
        d.fail("ledger dir", f"{led.ldir} absent (shoal ledger-init --spec ...)")
        return
    n = unpushed(root)
    if n:
        r = git(root, "log", "-1", "--format=%ct", "origin/HEAD..HEAD", check=False)
        (d.warn)("ledger unpushed", f"{n} local commit(s) not on the remote (shoal sync --shoal {spec.sid})")
    else:
        d.ok("ledger unpushed", "none")
    view = View(led, spec)
    findings = lint(view)
    errs = [m for c, m in findings if c.startswith("E")]
    warns = [m for c, m in findings if c.startswith("W")]
    (d.fail if errs else d.ok)("ledger lint", f"{len(led.events())} events; "
                               + ("; ".join(errs)[:200] if errs else f"clean ({len(warns)} warn)"))
    for r in spec.roles:
        if not r.session:
            # parked or retired: a leftover sidecar's dead pid is history, not a failure
            d.info(f"ledger-watch {r.slug}", "parked (no session)")
            continue
        sc = load_sidecar(sidecar_path(spec.sid, r.slug))
        if not sc:
            d.warn(f"ledger-watch {r.slug}", "not armed (no sidecar)")
            continue
        sid, pid = sc.get("session_id", ""), sc.get("pid", "")
        bound = sid and r.session and (sid.startswith(r.session) or r.session.startswith(sid))
        alive = bool(pid) and os.path.exists(f"/proc/{pid}")
        skew = tools_skew_note(sc.get("armed_utc", ""))
        if r.server and r.server != os.uname().nodename:
            d.ok(f"ledger-watch {r.slug}", f"remote host {r.server}: sidecar not expected here")
        elif not alive:
            d.fail(f"ledger-watch {r.slug}", f"pid {pid or '?'} not running (last tick {sc.get('last_tick', 'never')}); re-arm")
        elif not bound:
            d.warn(f"ledger-watch {r.slug}", f"alive (pid {pid}) but bound to {sid or '(none)'} != spec {r.session or '(none)'}")
        else:
            d.ok(f"ledger-watch {r.slug}", f"alive pid {pid}, bound {sid[:12]}, last tick {sc.get('last_tick', 'never')}"
                 + (f"; STALE-CODE {skew}" if skew else ""))
    if spec.sources:
        try:
            from sources import github_project_items
            for src in spec.sources:
                items = github_project_items(src["owner"], int(src["number"]))
                open_n = sum(1 for i in items if i["state"] == "OPEN")
                d.ok(f"source {src['owner']}/{src['number']}", f"{len(items)} items, {open_n} open (board order = priority)")
        except Exception as exc:  # noqa: BLE001
            d.fail("sources", f"priority table unreadable: {exc}")


def cmd_doctor(a: argparse.Namespace) -> int:
    d = Doc()
    print("== host")
    host_checks(d)
    if a.spec:
        print("== shoal")
        try:
            spec = ShoalSpec.load(a.spec)
        except Exception as exc:  # noqa: BLE001
            d.fail("spec", f"{a.spec}: {exc}")
            spec = None
        if spec is not None:
            group_checks(d, spec, a.spec, a.threshold_min)
    print(f"== doctor: {d.fails} FAIL, {d.warns} WARN")
    return 1 if d.fails else 0


def add_parser(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("doctor", help="preflight + live health (read-only)")
    p.add_argument("--spec", default="")
    p.add_argument("--threshold-min", type=float, default=60)
    p.set_defaults(func=cmd_doctor)
