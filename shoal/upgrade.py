#!/usr/bin/env python3
"""`shoal upgrade`: roll shoal/ tooling and feature flags into a LIVE shoal (architecture doc §11).

  shoal upgrade --spec S [--enable a.b] [--disable a.b] [--set a.b=JSON] [--rearm lazy|now]
                 [--rearm-verify-s N] [--skip-selftests] [--allow-dirty] [--dry-run]

Every change has one rollout class, and this command says which apply:
  live     loaded per run or per invocation (health, digest, matcher, report, next, queue):
           in effect from the next timer run or command, nothing to do;
  re-arm   code a RUNNING watcher executes (ledger-watch, relay-watch): the process keeps the
           code it loaded at spawn. `lazy` (default) lets each role pick the new code up at
           its next wake, since every wake ends in a re-arm; `now` wakes each stale watcher's
           role once to re-arm, and kills a relay-watch started before the change (the kill
           wakes the relay session, which re-arms), then waits up to `--rearm-verify-s` for
           the new relay-watch pid;
  brief    brief text: lanes see it at their next restart (`next` and `queue` output carry
           rule changes now);
  restart  a spec change a live session depends on (a model change, a role's ecosystem):
           never done here; the command names it and stops short;
  ui       the operator UI's long-running feeder (`shoal-ui-feed`, docs/ui.md) keeps the code it
           loaded (ledger, health, blockers, roles, backends, ui): on any module change it is
           restarted, which wakes no session (it only reads).

Order: refuse a dirty shoal/ tree, run the selftests, diff shoal/ since the spec's recorded
`tooling.sha`, classify, apply feature edits, record the new sha with a ledger `system`
event, then re-arm per `--rearm`. `--dry-run` prints the whole plan and changes nothing
(selftests still run: they are isolated).
"""
from __future__ import annotations

import copy
import datetime
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
TESTS_DIR = os.path.join(os.path.dirname(HERE), "tests")
sys.path.insert(0, HERE)
import config  # noqa: E402

from roles import ShoalSpec, find_spec, is_wake_relay  # noqa: E402

REARM_LEDGER = {"ledger_watch.py", "ledger.py", "roles.py", "digest.py", "sources.py", "watchers.py",
                "heartbeat.py"}
REARM_RELAY = {"health.py", "ledger.py", "roles.py"}
BRIEF = {"briefs.py"}
SELFTESTS = ("selftest_ledger.sh", "selftest_h4.sh", "selftest_h8.sh", "selftest_ui.sh", "selftest_muse.sh", "selftest_codex.sh")


def now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def git(*argv: str) -> str:
    r = subprocess.run(["git", *argv], capture_output=True, text=True, timeout=60, cwd=HERE)
    return r.stdout.strip() if r.returncode == 0 else ""


def rel_pkg() -> str:
    top = git("rev-parse", "--show-toplevel")
    return os.path.relpath(HERE, top)


def repo_id() -> str:
    """Identity of the repository the package runs from: its `origin` URL, else the
    toplevel directory's name. Recorded as `tooling.repo` so a sha from another
    repository is never diffed against this one."""
    url = git("remote", "get-url", "origin")
    if url:
        return url
    top = git("rev-parse", "--show-toplevel")
    return os.path.basename(top) if top else ""


def usable_sha(tooling: dict) -> tuple[str, str]:
    """(recorded sha usable as a diff base or "", why not). A sha recorded against another
    repository, or one this repository does not have, counts as none recorded."""
    prev = (tooling or {}).get("sha", "")
    if not prev:
        return "", ""
    rec_repo = (tooling or {}).get("repo", "")
    cur = repo_id()
    if rec_repo and cur and rec_repo != cur:
        return "", f"recorded sha {prev} is from {rec_repo}, not {cur}"
    r = subprocess.run(["git", "cat-file", "-e", f"{prev}^{{commit}}"], capture_output=True, text=True,
                       timeout=60, cwd=HERE)
    if r.returncode != 0:
        return "", f"recorded sha {prev} is not a commit in this repository"
    return prev, ""


UI_FEED_UNIT = "shoal-ui-feed.service"


def classify(files: list[str]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {"live": [], "re-arm (ledger-watch)": [], "re-arm (relay-watch)": [], "brief": [],
                                 "ui (restart shoal-ui-feed)": []}
    for f in files:
        b = os.path.basename(f)
        if b.endswith(".py"):  # the feeder keeps ledger, health, blockers, roles and backends loaded
            out["ui (restart shoal-ui-feed)"].append(b)
        if b in BRIEF:
            out["brief"].append(b)
        if b in REARM_LEDGER:
            out["re-arm (ledger-watch)"].append(b)
        if b in REARM_RELAY:
            out["re-arm (relay-watch)"].append(b)
        if b.endswith((".py", ".sh")) and b not in BRIEF:
            out["live"].append(b)
    return out


def set_path(d: dict, dotted: str, value) -> None:
    keys = dotted.split(".")
    cur = d
    for k in keys[:-1]:
        if not isinstance(cur.get(k), dict):  # `true` or absent becomes an object
            cur[k] = {}
        cur = cur[k]
    cur[keys[-1]] = value


def feature_edits(features: dict, a) -> tuple[dict, list[str]]:
    new = copy.deepcopy(features or {})
    changes = []
    for p in a.enable or []:
        set_path(new, p, True)
        changes.append(f"{p}=true")
    for p in a.disable or []:
        set_path(new, p, False)
        changes.append(f"{p}=false")
    for kv in a.set or []:
        p, _, raw = kv.partition("=")
        try:
            val = json.loads(raw)
        except ValueError:
            val = raw
        set_path(new, p, val)
        changes.append(f"{p}={json.dumps(val)}")
    return new, changes


def rearm_since(prev: str, files: list[str], cwd: str = HERE) -> str:
    """Commit time (ISO) of the newest change since `prev` to code a running watcher executes,
    or "". `files` are repo-root paths (`git diff --name-only`), while this runs inside the
    package, so the pathspecs carry `:(top)`: without it nothing matched, the time came back
    empty, and upgrade reported no stale watcher while doctor flagged them (STALE-CODE)."""
    rearm = [f":(top){f}" for f in files if os.path.basename(f) in REARM_LEDGER | REARM_RELAY]
    if not rearm:
        return ""
    r = subprocess.run(["git", "log", "-1", "--format=%cI", f"{prev}..HEAD", "--", *rearm],
                       capture_output=True, text=True, timeout=60, cwd=cwd)
    return r.stdout.strip() if r.returncode == 0 else ""


def stale_watchers(spec: ShoalSpec, since_iso: str) -> list[tuple[str, str]]:
    """(slug, pid) of alive ledger-watch processes armed before `since_iso`."""
    from ledger_watch import load_sidecar, sidecar_path
    out = []
    for r in spec.roles:
        sc = load_sidecar(sidecar_path(spec.sid, r.slug))
        pid = sc.get("pid", "")
        if pid and os.path.exists(f"/proc/{pid}") and sc.get("armed_utc", "") < since_iso:
            out.append((r.slug, pid))
    return out


def relay_pid_path(sid: str) -> str:
    from roles import state_dir
    return os.path.join(state_dir(sid), "relay-watch.pid")


def relay_watch_proc(sid: str) -> tuple[str, str]:
    """(pid, armed_utc) of the running relay-watch, or ("", ""). The pid file is written
    once at spawn, so its mtime is the arm time."""
    import health
    p = relay_pid_path(sid)
    if not health.relay_alive(sid):
        return "", ""
    try:
        pid = open(p).read().strip()
        armed = datetime.datetime.fromtimestamp(os.path.getmtime(p), datetime.timezone.utc)
    except (OSError, ValueError):
        return "", ""
    return pid, armed.strftime("%Y-%m-%dT%H:%M:%SZ")


def stale_relay(spec: ShoalSpec, since_iso: str) -> tuple[str, str]:
    """(pid, armed_utc) of a live relay-watch armed before `since_iso`, else ("", "")."""
    if not since_iso or not any(is_wake_relay(r) for r in spec.roles):
        return "", ""
    pid, armed = relay_watch_proc(spec.sid)
    return (pid, armed) if pid and armed < since_iso else ("", "")


def rearm_verify_s(a) -> float:
    """How long `--rearm now` waits for the relay to re-arm: the flag, else
    SHOAL_REARM_VERIFY_S (the suites set a few seconds), else 180."""
    v = getattr(a, "rearm_verify_s", None)
    if v is None:
        v = config.env("REARM_VERIFY_S", "180") or 180
    return max(0.0, float(v))


def rearm_relay(spec: ShoalSpec, pid: str, verify_s: float, poll_s: float = 1.0) -> bool:
    """Kill the stale relay-watch and prove the re-arm: a new pid in the pid file with a
    live relay-watch behind it, within `verify_s`. Killing a Claude background command
    wakes its session, and the relay's loop re-arms on a kill (measured 2026-09-28: a
    relay announced a restart and ended its turn without re-arming, so the re-arm is
    verified, never assumed)."""
    import signal
    import time
    import health
    try:
        os.kill(int(pid), signal.SIGTERM)
        print(f"relay-watch: killed pid {pid} (pre-upgrade code); the relay session re-arms on the kill")
    except (OSError, ValueError) as exc:
        print(f"relay-watch: kill of pid {pid} failed ({exc})")
    t0 = time.time()
    while True:
        try:
            cur = open(relay_pid_path(spec.sid)).read().strip()
        except OSError:
            cur = ""
        if cur and cur != pid and health.relay_alive(spec.sid):
            print(f"relay-watch re-armed: pid {cur}")
            return True
        if time.time() - t0 >= verify_s:
            break
        time.sleep(poll_s)
    print(f"WARN relay-watch NOT re-armed within {verify_s:g}s (old pid {pid}): the relay session must run "
          f"`{config.cli()} relay-watch --shoal {spec.sid}` (Bash tool, run_in_background=true); if it does not, "
          f"relaunch it with `{config.cli()} relay-launch --shoal {spec.sid}`")
    return False


def ui_feed_active() -> bool:
    try:
        r = subprocess.run(["systemctl", "--user", "is-active", UI_FEED_UNIT], capture_output=True, text=True, timeout=10)
        return r.stdout.strip() == "active"
    except (OSError, subprocess.SubprocessError):
        return False


def restart_ui_feed() -> str:
    """The feeder is read-only to shoal and wakes nobody: a restart costs no tokens."""
    try:
        r = subprocess.run(["systemctl", "--user", "restart", UI_FEED_UNIT], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        return f"restart FAILED: {exc}"
    return "restarted" if r.returncode == 0 else f"restart FAILED: {(r.stderr or r.stdout).strip()[-120:]}"


def run_selftests() -> tuple[bool, str]:
    for t in SELFTESTS:
        r = subprocess.run(["bash", os.path.join(TESTS_DIR, t)], capture_output=True, text=True, timeout=1800)
        if r.returncode != 0:
            return False, f"{t} FAILED: {(r.stdout + r.stderr).strip()[-600:]}"
    return True, "selftests passed: " + ", ".join(SELFTESTS)


def cmd_upgrade(a) -> int:
    path = a.spec or find_spec(a.sid)
    spec = ShoalSpec.load(path)
    pkgdir = rel_pkg()
    sha = git("rev-parse", "--short=12", "HEAD")
    dirty = git("status", "--porcelain", "--", ".")
    if dirty and not a.allow_dirty:
        print(f"{'WARNING' if a.dry_run else 'ERROR'}: uncommitted changes under {pkgdir}: commit first "
              f"(the recorded sha must name the code that runs)\n{dirty}")
        if not a.dry_run:
            return 1
    recorded = (spec.tooling or {}).get("sha", "")
    prev, unusable = usable_sha(spec.tooling)
    if unusable:
        print(f"note: {unusable}; treating it as none recorded")
    if prev:
        files = [f for f in git("diff", "--name-only", f"{prev}..HEAD", "--", ".").splitlines() if f]
        since = rearm_since(prev, files)
    else:
        files = [f for f in git("ls-files", "--", ".").splitlines() if f.endswith((".py", ".sh"))]
        since = git("log", "-1", "--format=%cI", "--", ".")
    since_utc = ""
    if since:
        since_utc = datetime.datetime.fromisoformat(since.replace("Z", "+00:00")).astimezone(datetime.timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ")
    cls = classify(files)
    new_features, changes = feature_edits(spec.features, a)
    stale = stale_watchers(spec, since_utc) if (cls["re-arm (ledger-watch)"] and since_utc) else []
    relay_pid, relay_armed = stale_relay(spec, since_utc) if cls["re-arm (relay-watch)"] else ("", "")
    print(f"# upgrade {spec.sid}: tooling {prev or '(none recorded)'} -> {sha}"
          + (" (dirty tree)" if dirty else ""))
    print(f"changed under {pkgdir}: {len(files)} file(s)"
          + ("" if prev else " (no recorded sha: every module treated as changed)"))
    for k, v in cls.items():
        if v:
            print(f"  {k}: {', '.join(sorted(set(v)))}")
    print("features: " + ("; ".join(changes) if changes else "no change"))
    if stale:
        print(f"re-arm ({a.rearm}): ledger-watch armed before {since_utc}: "
              + ", ".join(f"{s} (pid {p})" for s, p in stale)
              + (" -> each role picks the new code up at its next wake" if a.rearm == "lazy"
                 else " -> one wake each now"))
    elif cls["re-arm (ledger-watch)"]:
        print("re-arm: no running ledger-watch predates the change")
    if relay_pid:
        print(f"re-arm ({a.rearm}): relay-watch armed before {since_utc}: pid {relay_pid} (armed {relay_armed})"
              + (" -> it loads the new code when it next relays and re-arms" if a.rearm == "lazy"
                 else " -> killed now; the relay re-arms on the kill, verified for "
                      f"{rearm_verify_s(a):g}s"))
    elif cls["re-arm (relay-watch)"] and any(is_wake_relay(r) for r in spec.roles):
        print("re-arm: no running relay-watch predates the change")
    ui_restart = bool(cls["ui (restart shoal-ui-feed)"]) and ui_feed_active()
    if cls["ui (restart shoal-ui-feed)"]:
        print(f"ui: {UI_FEED_UNIT} " + ("is restarted after the upgrade (reads only; wakes no session)" if ui_restart
                                         else "is not running here; nothing to restart"))
    if cls["brief"]:
        print("brief: brief text changed; lanes read it at their next restart (`next`/`queue` carry rules now)")
    if a.restart_note:
        print(f"restart NEEDED (declared): {a.restart_note}; run `shoal restart --spec {path} --only ...` "
              f"at a pause boundary")
    else:
        print("restart: none needed (no declared spec change a live session depends on)")
    if a.skip_selftests:
        print("selftests: SKIPPED (--skip-selftests)")
    else:
        ok, msg = run_selftests()
        print(msg)
        if not ok:
            return 2
    import skills as _skills
    stale_skills = _skills.stale()
    if stale_skills:
        print(f"skills: {len(stale_skills)} user-scope cop{'y' if len(stale_skills) == 1 else 'ies'} stale "
              f"({', '.join(stale_skills)}); sessions load them at start, so the next restart picks them up")
    if a.dry_run:
        print("[dry-run] nothing written")
        return 0
    if stale_skills:
        print(f"skills: synced {len(_skills.sync())} cop{'y' if len(stale_skills) == 1 else 'ies'}")
    spec.features = new_features
    spec.tooling = {"sha": sha, "repo": repo_id(), "prev": recorded, "upgraded_utc": now_iso(),
                    "changed": sorted({os.path.basename(f) for f in files}),
                    "features_changed": changes}
    from cli import persist_spec
    persist_spec(spec, path, note=f"upgrade tooling {recorded or '-'}..{sha}" + (f"; {', '.join(changes)}" if changes else ""))
    try:
        from ledger import Ledger
        Ledger(spec.sid, root=spec.ledger_dir).append(
            "shoal", "system", kind="upgrade", old=recorded, new=sha, features=changes,
            text=f"tooling upgraded {recorded or '-'} -> {sha}" + (f"; features {', '.join(changes)}" if changes else ""))
    except Exception as exc:  # noqa: BLE001
        print(f"(ledger system event failed: {exc})")
    if a.rearm == "now":
        import health
        for slug, pid in stale:
            ch = health.request_wake(spec, slug, f"tooling upgraded to {sha}: your ledger-watch (pid {pid}) runs "
                                                 f"pre-upgrade code; after this boundary re-arm it with the same "
                                                 f"command", condition="upgrade-rearm")
            print(f"woke {slug} to re-arm -> {ch}")
        if relay_pid:
            rearm_relay(spec, relay_pid, rearm_verify_s(a))
    if ui_restart:
        print(f"ui: {UI_FEED_UNIT} {restart_ui_feed()}")
    print(f"upgraded {spec.sid} to tooling {sha}")
    if any(c.startswith("matcher") for c in changes):
        print(f"matcher: the digest timer applies it within 10 minutes; `shoal digest --shoal {spec.sid} --refresh` "
              f"applies it now")
    return 0


def add_parser(sub) -> None:
    p = sub.add_parser("upgrade", help="roll shoal/ tooling and feature flags into a live shoal (classes: live, "
                                       "re-arm, brief, restart)")
    p.add_argument("--spec", default="")
    config.add_id_arg(p, default="")
    p.add_argument("--enable", action="append", default=[], metavar="FEATURE.PATH",
                   help="set a feature path true, e.g. matcher.live (repeatable)")
    p.add_argument("--disable", action="append", default=[], metavar="FEATURE.PATH")
    p.add_argument("--set", action="append", default=[], metavar="FEATURE.PATH=JSON",
                   help='e.g. matcher={"live": false} or matcher.model="opencode/space-bunny-free"')
    p.add_argument("--rearm", choices=["lazy", "now"], default="lazy")
    p.add_argument("--rearm-verify-s", type=float, default=None,
                   help="with --rearm now: seconds to wait for the relay-watch re-arm (default "
                        "SHOAL_REARM_VERIFY_S, else 180; 0 checks once)")
    p.add_argument("--restart-note", default="", help="declare a change that needs a restart (printed, not done)")
    p.add_argument("--skip-selftests", action="store_true")
    p.add_argument("--allow-dirty", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_upgrade)
