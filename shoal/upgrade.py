#!/usr/bin/env python3
"""`shoal upgrade`: roll shoal/ tooling and feature flags into a LIVE shoal (architecture doc §11).

  shoal upgrade --spec S [--enable a.b] [--disable a.b] [--set a.b=JSON] [--rearm lazy|now]
                 [--skip-selftests] [--allow-dirty] [--dry-run]

Every change has one rollout class, and this command says which apply:
  live     loaded per run or per invocation (health, digest, matcher, report, next, queue):
           in effect from the next timer run or command, nothing to do;
  re-arm   code a RUNNING watcher executes (ledger-watch, relay-watch): the process keeps the
           code it loaded at spawn. `lazy` (default) lets each role pick the new code up at
           its next wake, since every wake ends in a re-arm; `now` wakes each stale watcher's
           role once to re-arm;
  brief    brief text: lanes see it at their next restart (`next` and `queue` output carry
           rule changes now);
  restart  a spec change a live session depends on (a model change, a role's ecosystem):
           never done here; the command names it and stops short.

Order: refuse a dirty shoal/ tree, run both selftests, diff shoal/ since the spec's recorded
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
SELFTESTS = ("selftest_ledger.sh", "selftest_h4.sh", "selftest_h8.sh")


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


def classify(files: list[str]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {"live": [], "re-arm (ledger-watch)": [], "re-arm (relay-watch)": [], "brief": []}
    for f in files:
        b = os.path.basename(f)
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
    relay_stale = bool(cls["re-arm (relay-watch)"]) and any(is_wake_relay(r) for r in spec.roles)
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
    if relay_stale:
        print("re-arm: relay-watch re-arms after every relay, so it loads the new code at its next wake")
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
    p.add_argument("--restart-note", default="", help="declare a change that needs a restart (printed, not done)")
    p.add_argument("--skip-selftests", action="store_true")
    p.add_argument("--allow-dirty", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_upgrade)
