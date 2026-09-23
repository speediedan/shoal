#!/usr/bin/env python3
"""Preflight + live health: `hag.py doctor [--spec hag.json]`.

Runs before a launch (no spec: host checks only) and any time after
(with a spec: adds per-HAG checks). Every check prints one line
`OK|WARN|FAIL <name>: <detail>`; exit 1 on any FAIL. Nothing is mutated.

Host checks: python version, tool binaries on the explicit tool PATH
(openchamber, claude, gh, az, git, systemctl), openchamber server
reachable, AZURE_DEVOPS_EXT_PAT resolvable, gh auth, HAG roots writable,
linger enabled, legacy HAG cron lines (the measured silent-failure mode),
stale systemd units pointing at missing files.

HAG checks: spec valid; start docs present with required sections; queue
files lint clean; live session ids present + reachable; heartbeat/wait
posture per role; watcher ticks vs 3x interval; systemd timers present and
active for this HAG; sidecars bound to the recorded consumer session id;
telemetry dir writable + last event age; reconcile tick age. Ledger mode
(2026-09-20): clone present with a reachable remote, no unpushed commits
older than an hour, lint clean (no violations, no contested claims), and
one ledger-watch sidecar per role bound to its session with a live pid.
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

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from backends import TOOL_PATH, get as get_backend  # noqa: E402
from heartbeat import age_min, classify  # noqa: E402
from roles import HAG_DATA_ROOT, HAG_ROOT, HagSpec, state_dir, telemetry_dir  # noqa: E402

ENV = dict(os.environ, PATH=TOOL_PATH + ":" + os.environ.get("PATH", ""))
TICK_INTERVAL_MIN = {"queue": 10, "lanes": 5, "pipeline": 10, "heartbeat": 5,
                     "reconcile": 24 * 60, "weekly-report": 7 * 24 * 60}


def tick_jobs(spec) -> dict:
    """The self-ticking jobs this spec actually installs (features change the set):
    health-a/-b replace heartbeat, digest joins, reconcile runs twice a day."""
    feats = (getattr(spec, "features", {}) or {}) if spec is not None else {}
    jobs = dict(TICK_INTERVAL_MIN)
    jobs["reconcile"] = 12 * 60
    if getattr(spec, "ledger_mode", False):
        jobs.pop("queue", None)
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
    for name, root in (("HAG_ROOT", HAG_ROOT), ("HAG_DATA_ROOT", HAG_DATA_ROOT)):
        if os.path.isdir(root) and os.access(root, os.W_OK):
            d.ok(name, root)
        elif not os.path.isdir(root):
            d.fail(name, f"{root} missing (mkdir -p it; the bulk tier holds telemetry)")
        else:
            d.fail(name, f"{root} not writable")
    try:
        r = run(["loginctl", "show-user", os.environ.get("USER", "speediedan"), "-p", "Linger"])
        (d.ok if "Linger=yes" in r.stdout else d.warn)("linger", r.stdout.strip() or "unknown (timers die at logout)")
    except Exception as exc:  # noqa: BLE001
        d.warn("linger", str(exc))
    try:
        r = run(["crontab", "-l"])
        legacy = [l for l in r.stdout.splitlines()
                  if not l.startswith("#") and re.search(r"watch-coordination\.sh|hag-reconcile|hag_queue_watch|hag-restart", l)]
        if legacy:
            missing = [l for l in legacy if not os.path.exists((l.split()[5:6] or [""])[0])]
            (d.fail if missing else d.warn)("legacy HAG cron lines",
                                            f"{len(legacy)} line(s); {len(missing)} point at MISSING scripts "
                                            "(silent failure every run — remove with `crontab -e`)")
        else:
            d.ok("legacy HAG cron lines", "none")
    except Exception as exc:  # noqa: BLE001
        d.warn("crontab", str(exc))
    unit_dir = os.path.expanduser("~/.config/systemd/user")
    stale = []
    for svc in glob.glob(os.path.join(unit_dir, "hag-*.service")):
        for line in open(svc):
            if line.startswith("ExecStart="):
                parts = line[len("ExecStart="):].split()
                if len(parts) > 1 and not os.path.exists(parts[1]):
                    stale.append(os.path.basename(svc))
    (d.warn if stale else d.ok)("systemd hag units", f"{len(stale)} unit(s) point at missing scripts: {stale}" if stale else "no stale ExecStart paths")


def provider_failures(session_ids: list[str], hours: float = 24) -> dict:
    """Read-only scan of the opencode message store for failed-send records.

    Catches the provider-outage class (e.g. stale encrypted-reasoning blobs
    rejected with "not issued to this caller"): those failures land as error
    message rows, never as assistant output, so heartbeat/watcher checks read
    them as a quiet world. Returns counts + a sample; never raises.
    Storage layout is version-sensitive (v1: opencode.db message/data) —
    re-validate after any opencode major upgrade.
    """
    out: dict = {"total": 0, "by_session": {}, "last_utc": "",
                 "last_hour": False, "sample": ""}
    try:
        db = os.path.join(os.path.expanduser("~"), ".local", "share",
                          "opencode", "opencode.db")
        if not os.path.isfile(db) or not session_ids:
            return out
        cutoff_ms = (datetime.datetime.now(datetime.timezone.utc).timestamp()
                     - hours * 3600) * 1000
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=20)
        try:
            q = (f"SELECT session_id, time_created, substr(data, 1, 300) FROM message "
                 f"WHERE time_created >= ? AND data LIKE '%was not issued to this caller%'")
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
    """Non-empty when the watch was armed before the newest hag/ tool commit:
    the running process executes pre-fix code (measured 2026-09-21: a holder
    gate fixed after arming left a live watcher silently not offering)."""
    try:
        out = subprocess.run(["git", "log", "-1", "--format=%ci", "--",
                              "project_admin/shared_admin_scripts/hag/"],
                             capture_output=True, text=True, timeout=30,
                             cwd="/home/speediedan/repos/distributed-insight").stdout.strip()
        tools_ts = datetime.datetime.strptime(out[:19], "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=datetime.timezone.utc)
        armed = datetime.datetime.strptime(armed_utc, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=datetime.timezone.utc)
    except Exception:
        return ""
    if armed < tools_ts:
        return f"armed {armed_utc} predates tool update {out[:16]}Z — kill + re-arm"
    return ""


def timer_state(hag: str) -> dict[str, str]:
    r = run(["systemctl", "--user", "list-timers", "--all", "--no-pager", "--no-legend"], timeout=30)
    out = {}
    for line in r.stdout.splitlines():
        m = re.search(rf"(hag-{re.escape(hag)}-([\w.-]+?)\.timer)", line)
        if m:
            head = line.split(m.group(1))[0].strip()
            # columns are NEXT LEFT LAST PASSED: a real weekday timestamp in
            # NEXT means scheduled+waiting; LAST "n/a" only means never fired
            # yet (reconcile/weekly on a fresh HAG) — not inactive.
            scheduled = bool(re.match(r"^(Mon|Tue|Wed|Thu|Fri|Sat|Sun)\s", head))
            out[m.group(2)] = "scheduled" if scheduled else "inactive"
    return out


def hag_checks(d: Doc, spec: HagSpec, spec_path: str, threshold_min: float) -> None:
    errs = spec.validate()
    (d.ok if not errs else d.fail)("spec", f"{spec_path} incarnation={spec.incarnation} ({spec.display_gen}) roles={[r.slug for r in spec.roles]}"
                                   if not errs else "; ".join(errs))
    if spec.retired_utc:
        d.warn("spec retired", f"retired_utc={spec.retired_utc}; launches refuse this spec")
    for r in spec.roles:
        if not r.doc or not os.path.exists(r.doc):
            d.fail(f"doc {r.slug}", f"missing: {r.doc}")
            continue
        text = open(r.doc).read().lower()
        missing = [s for s in ("grants-needed", "current state") if s not in text]
        (d.ok if not missing else d.fail)(f"doc {r.slug}", os.path.basename(r.doc) if not missing else f"missing sections {missing}")
    qfiles = [r.queue for r in spec.roles if r.queue]
    if qfiles:
        from lint import lint_file
        for q in qfiles:
            f = lint_file(q, 24)
            errs = [f"{c} {m}" for c, m in f if c.startswith("E")]
            (d.ok if not errs else d.fail)(f"queue {os.path.basename(q)}", "lint clean" if not errs else "; ".join(errs)[:200])
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
            (d.warn if row["suspect"] else d.ok)(f"heartbeat {row['slug']}", f"{row['klass']}: {row['detail']}")
        for job, interval in tick_jobs(spec).items():
            age = age_min(os.path.join(state_dir(spec.hag), f"tick-{job}"))
            if age is None:
                d.warn(f"tick {job}", "never ran")
            elif age > 3 * interval:
                d.fail(f"tick {job}", f"{age:.0f}m old > 3x{interval}m: watcher dead, not world quiet")
            else:
                d.ok(f"tick {job}", f"{age:.0f}m ago")
        timers = timer_state(spec.hag)
        if not timers:
            d.fail("systemd timers", f"none installed for {spec.hag} (hag.py install-systemd --enable)")
        else:
            inactive = [k for k, v in timers.items() if v != "scheduled"]
            (d.fail if inactive else d.ok)("systemd timers", f"{len(timers)} installed; inactive: {inactive}" if inactive else f"{sorted(timers)} scheduled")
        for r in spec.roles:
            if not r.queue:
                continue
            sc = os.path.join(state_dir(spec.hag), f"hag-watch-{spec.hag}-{r.slug}.state")
            if not os.path.exists(sc):
                d.warn(f"sidecar {r.slug}", f"{sc} absent (consumer self-watch not armed)")
                continue
            sid = ""
            for line in open(sc):
                if line.startswith("session_id="):
                    sid = line.split("=", 1)[1].strip()
            bound = sid and r.session and (sid.startswith(r.session) or r.session.startswith(sid))
            (d.ok if bound else d.warn)(f"sidecar {r.slug}", f"bound to {sid or '(none)'}" + ("" if bound else f" != spec {r.session or '(none)'}"))
    if spec.ledger_mode:
        ledger_checks(d, spec)
        matcher_checks(d, spec)
    tdir = telemetry_dir(spec.hag)
    if os.path.isdir(tdir):
        files = sorted(glob.glob(os.path.join(tdir, "events-*.jsonl")))
        last = age_min(files[-1]) if files else None
        d.ok("telemetry", f"{tdir}: {len(files)} day file(s), last write {'never' if last is None else f'{last:.0f}m ago'}")
    else:
        d.warn("telemetry", f"{tdir} absent (created on first emit)")
    if live:
        fails = provider_failures([r.session for r in live if r.session], hours=24)
        if fails["total"]:
            recent = fails["last_utc"] or "unknown"
            (d.fail if fails["last_hour"] else d.warn)(
                "provider errors",
                f"{fails['total']} failed-send records in 24h across {len(fails['by_session'])} session(s), "
                f"last {recent} :: {fails['sample'][:160]}")


def matcher_checks(d: Doc, spec: HagSpec) -> None:
    """features.matcher (architecture §4.1): the model that last answered, and freshness."""
    import matcher
    c = matcher.config(spec)
    if c is None:
        return
    st = matcher.load_state(spec.hag)
    mode = "live" if c.get("live") else "shadow"
    if c.get("backend") == "opencode-run" and not os.path.exists(matcher.opencode_bin()):
        d.fail("matcher", f"{mode}: opencode not found at {matcher.opencode_bin()}")
        return
    act = (st.get("active") or {}).get("model")
    if not act:
        d.warn("matcher", f"{mode}: no model has answered yet (configured {c.get('model')}); "
                          f"`hag.py digest --hag {spec.hag} --refresh` runs it")
        return
    tier = matcher.tier_of(st["active"])
    pinned = "" if act == c.get("model") else f" (FALLBACK: configured {c.get('model')} did not answer)"
    paid = {"go": "; PAID tier: Go subscription usage", "zen": "; PAID tier: Zen balance"}.get(tier, "")
    (d.warn if paid else d.ok)("matcher", f"{mode}: model {act} ({tier} tier){pinned}{paid}, last answered "
                                          f"{st.get('last_ok', '?')}, "
                    f"{sum(1 for k in matcher.load_cache(spec.hag) if not k.startswith('_'))} items cached")


def ledger_checks(d: Doc, spec: HagSpec) -> None:
    try:
        from ledger import Ledger, View, git, lint, unpushed
        from ledger_watch import load_sidecar, sidecar_path
    except Exception as exc:  # noqa: BLE001
        d.fail("ledger modules", str(exc))
        return
    root = spec.ledger_dir
    if not os.path.isdir(os.path.join(root, ".git")):
        d.fail("ledger clone", f"{root} is not a git clone (hag.py ledger-init --spec ...)")
        return
    d.ok("ledger clone", root)
    try:
        r = git(root, "ls-remote", "--exit-code", "--heads", "origin", timeout=30, check=False)
        (d.ok if r.returncode == 0 else d.fail)("ledger remote", "reachable" if r.returncode == 0
                                                else f"unreachable: {(r.stdout + r.stderr).strip()[-120:]}")
    except Exception as exc:  # noqa: BLE001
        d.fail("ledger remote", str(exc))
    led = Ledger(spec.hag, root=root)
    if not led.exists():
        d.fail("ledger dir", f"{led.ldir} absent (hag.py ledger-init --spec ...)")
        return
    n = unpushed(root)
    if n:
        r = git(root, "log", "-1", "--format=%ct", "origin/HEAD..HEAD", check=False)
        (d.warn)("ledger unpushed", f"{n} local commit(s) not on the remote (hag.py sync --hag {spec.hag})")
    else:
        d.ok("ledger unpushed", "none")
    view = View(led, spec)
    findings = lint(view)
    errs = [m for c, m in findings if c.startswith("E")]
    warns = [m for c, m in findings if c.startswith("W")]
    (d.fail if errs else d.ok)("ledger lint", f"{len(led.events())} events; "
                               + ("; ".join(errs)[:200] if errs else f"clean ({len(warns)} warn)"))
    for r in spec.roles:
        if r.fill == "none" and not r.session:
            continue
        sc = load_sidecar(sidecar_path(spec.hag, r.slug))
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
        print("== hag")
        try:
            spec = HagSpec.load(a.spec)
        except Exception as exc:  # noqa: BLE001
            d.fail("spec", f"{a.spec}: {exc}")
            spec = None
        if spec is not None:
            hag_checks(d, spec, a.spec, a.threshold_min)
    print(f"== doctor: {d.fails} FAIL, {d.warns} WARN")
    return 1 if d.fails else 0


def add_parser(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("doctor", help="preflight + live health (read-only)")
    p.add_argument("--spec", default="")
    p.add_argument("--threshold-min", type=float, default=60)
    p.set_defaults(func=cmd_doctor)
