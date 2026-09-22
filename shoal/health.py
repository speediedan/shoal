#!/usr/bin/env python3
"""HAG health quorum (architecture doc §6): watchers of the watchers, no model tokens.

    hag.py health --spec <hag.json> --instance a|b [--dry-run]     # what the two timers run
    hag.py relay-watch --hag H                                     # the Claude relay's one waiter

Two identical systemd timers per host (`hag-<hag>-health-a` at *:0/5, `-b` at *:2/5) run
`health`. They share one state file under an flock, so whichever runs first acts; each run
self-ticks and checks its peer's tick, and a peer silent past 15 min pages the maintainer
once. That mutual check is the quorum.

Each run evaluates every worker role against the policy table below and, when a condition
has outlived its leeway, climbs that condition's ladder: wake, wake again, page the
maintainer. A condition INSTANCE is keyed by its evidence (offer id, dead pid, target, last
boundary), so a new instance restarts the ladder and one instance never escalates twice.

  condition          evidence                                   ladder (minutes since evidence)
  unacked-work       open offer to an idle role (or --now) newer than its queue version   wake@10 wake@30 page@60
  wait-resolved      a machine-checkable wait target is no longer pending                  wake@15 page@60
  wait-validity      any CI / lease / job wait, every validity_wake_min (default 90)       wake@V  wake@2V page@3V
  watcher-dead       ledger-watch pid gone, session between turns                          wake@10 page@40
  stale-active       posture active, no ledger write, session between turns                wake@90 page@180
  user-waits         waiting on the maintainer: never wakes the role; one daily digest page

Wake adapters, one per ecosystem: opencode -> `openchamber session send`; Claude with a
live ledger-watch -> a wake request file the watcher delivers (/tmp/<hag>/wake-<slug>);
Claude with a dead watcher -> the relay queue (/tmp/<hag>/relay-queue), drained by a small
Claude relay session through the harness's cross-session message. In user-offline mode,
maintainer pages are deferred and delivered as one summary when the maintainer is back.
"""
from __future__ import annotations

import datetime
import fcntl
import json
import os
import re
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "telemetry"))

from roles import HagSpec, find_spec, roles_dir, state_dir  # noqa: E402

TOOL_PATH = ("/home/speediedan/.nvm/versions/node/v24.13.0/bin:"
             "/home/speediedan/.bun/bin:/usr/bin:/bin:/usr/local/bin")
_ENV = dict(os.environ, PATH=TOOL_PATH + ":" + os.environ.get("PATH", ""))
PEER_STALE_MIN = 15
LADDERS = {
    "unacked-work": [(10, "wake"), (30, "wake"), (60, "page")],
    "wait-resolved": [(15, "wake"), (60, "page")],
    "watcher-dead": [(10, "wake"), (40, "page")],
    "stale-active": [(90, "wake"), (180, "page")],
}
CI_WAITS = ("remote-ci", "local-ci")
USER_WAITS = ("user-auth", "user-design", "user-feedback")


def utcnow() -> datetime.datetime:
    """Now; HAG_HEALTH_SHIFT_MIN (tests only) moves the clock forward to exercise the ladders."""
    return datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(
        minutes=float(os.environ.get("HAG_HEALTH_SHIFT_MIN", "0") or 0))


def iso(dt: datetime.datetime | None = None) -> str:
    return (dt or utcnow()).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse(ts: str) -> datetime.datetime | None:
    try:
        return datetime.datetime.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=datetime.timezone.utc)
    except (TypeError, ValueError):
        return None


def age_min(ts: str) -> float | None:
    t = parse(ts or "")
    return None if t is None else (utcnow() - t).total_seconds() / 60


def health_cfg(spec: HagSpec) -> dict:
    h = (spec.features or {}).get("health")
    return h if isinstance(h, dict) else {}


# ---- wake adapters -------------------------------------------------------------

def watcher_alive(hag: str, slug: str) -> tuple[bool, dict]:
    from ledger_watch import load_sidecar, sidecar_path
    sc = load_sidecar(sidecar_path(hag, slug))
    pid = sc.get("pid", "")
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as fh:
            cmd = fh.read()
        return (b"ledger-watch" in cmd and slug.encode() in cmd), sc
    except (OSError, ValueError):
        return False, sc


def wake_path(hag: str, slug: str) -> str:
    return os.path.join(state_dir(hag), f"wake-{slug}")


def relay_queue_path(hag: str) -> str:
    return os.path.join(state_dir(hag), "relay-queue")


def request_wake(spec: HagSpec, slug: str, reason: str, condition: str = "", dry_run: bool = False) -> str:
    """Wake one role through its ecosystem's designated adapter; returns the channel used."""
    role = spec.role(slug)
    if role is None:
        return "no-such-role"
    text = (f"HAG-WAKE {slug}@{spec.hag} [{condition or 'request'}]: {reason} || run "
            f"`hag.py next --hag {spec.hag} --slug {slug}`, act, write ONE `hag.py queue`, re-arm your watcher.")
    if role.ecosystem == "opencode":
        if dry_run:
            return "opencode-send (dry-run)"
        from backends import get as get_backend
        try:
            get_backend("opencode").send(role.session, spec.dir, text)
            return "opencode-send"
        except Exception as exc:  # noqa: BLE001
            return f"opencode-send FAILED ({str(exc)[:80]})"
    alive, _ = watcher_alive(spec.hag, slug)
    if alive:
        if not dry_run:
            os.makedirs(state_dir(spec.hag), exist_ok=True)
            with open(wake_path(spec.hag, slug), "a") as fh:
                fh.write(json.dumps({"ts": iso(), "condition": condition, "text": text}) + "\n")
        return "wake-file" + (" (dry-run)" if dry_run else "")
    relay = next((r for r in spec.roles if getattr(r, "fill", "") == "none" and r.slug == "hag-relay"), None)
    if relay is None or not (spec.features or {}).get("relay"):
        return "no-adapter (claude watcher dead, relay disabled)"
    if not relay_alive(spec.hag):
        return "no-adapter (claude watcher dead, relay waiter dead)"
    if not dry_run:
        with open(relay_queue_path(spec.hag), "a") as fh:
            fh.write(json.dumps({"ts": iso(), "to": role.tag, "slug": slug, "session": role.session,
                                 "condition": condition, "text": text}) + "\n")
    return "relay" + (" (dry-run)" if dry_run else "")


def relay_alive(hag: str) -> bool:
    p = os.path.join(state_dir(hag), "relay-watch.pid")
    try:
        pid = open(p).read().strip()
        with open(f"/proc/{pid}/cmdline", "rb") as fh:
            return b"relay-watch" in fh.read()
    except (OSError, ValueError):
        return False


def page_user(spec: HagSpec, text: str, state: dict, dry_run: bool = False) -> str:
    """A maintainer page is one ledger msg to `user`; deferred while the maintainer is offline."""
    from ledger import Ledger, View
    led = Ledger(spec.hag, root=spec.ledger_dir)
    view = View(led, spec)
    if view.user_offline() is not None:
        state.setdefault("deferred_pages", []).append({"ts": iso(), "text": text[:400]})
        return "deferred (user offline)"
    if dry_run:
        return "page (dry-run)"
    led.append("hag.py", "msg", to="user", kind="health", text=text[:500])
    return "page"


# ---- wait-target heuristics ------------------------------------------------------

def target_pending(target: str, slug: str) -> tuple[bool | None, str]:
    """(True pending | False resolved/invalid | None unknown, detail) for a wait target."""
    if not target:
        return None, "no target"
    kind, _, rest = target.partition(":")
    try:
        if kind == "job":
            r = subprocess.run(["systemctl", "--user", "show", f"{rest}.service", "-p", "ActiveState"],
                               capture_output=True, text=True, timeout=20)
            st = r.stdout.strip().split("=", 1)[-1]
            return st in ("active", "activating", "deactivating"), f"unit {st or 'gone'}"
        if kind == "lease":
            lease = os.path.join(os.path.dirname(HERE), "gpu_lease.sh")
            r = subprocess.run([lease, "--status"], capture_output=True, text=True, timeout=30, env=_ENV)
            txt = r.stdout + r.stderr
            mine = slug in txt
            key_line = next((ln for ln in txt.splitlines() if rest and rest in ln), "")
            if r.returncode != 0 and not txt:
                return None, "lease status unavailable"
            return (mine or bool(key_line and re.search(r"held|queue|wait", key_line, re.I))), \
                (short_line(key_line) or ("role in lease queue" if mine else "role not in lease holders/queue"))
        if kind == "gh-run":
            repo, _, rid = rest.rpartition(":")
            r = subprocess.run(["gh", "run", "view", rid, "--repo", repo, "--json", "status,conclusion"],
                               capture_output=True, text=True, timeout=30, env=_ENV)
            d = json.loads(r.stdout or "{}")
            return d.get("status") not in ("completed",), f"run {d.get('status')}/{d.get('conclusion')}"
        if kind == "gh-pr":
            repo, _, n = rest.partition("#")
            r = subprocess.run(["gh", "pr", "checks", n, "--repo", repo, "--json", "bucket"],
                               capture_output=True, text=True, timeout=30, env=_ENV)
            b = [c.get("bucket") for c in json.loads(r.stdout or "[]")]
            if not b:
                return None, "no checks reported (never green on empty)"
            return "pending" in b, f"checks {sorted(set(b))}"
        if kind == "az-build":
            r = subprocess.run(["az", "pipelines", "build", "show", "--id", rest, "--query", "status", "-o", "tsv"],
                               capture_output=True, text=True, timeout=60, env=_ENV)
            st = r.stdout.strip()
            if not st:
                return None, "az build status unavailable"
            return st in ("notStarted", "inProgress", "postponed"), f"build {st}"
    except Exception as exc:  # noqa: BLE001
        return None, f"check failed: {str(exc)[:80]}"
    return None, "free-text target (validity wake only)"


def short_line(s: str, n: int = 90) -> str:
    return re.sub(r"\s+", " ", s or "").strip()[:n]


# ---- session liveness -------------------------------------------------------------

def claude_busy() -> dict[str, str]:
    """sessionId -> 'busy'|'idle' from `claude agents --json` (parsed; never the TTY form)."""
    try:
        r = subprocess.run(["claude", "agents", "--json"], capture_output=True, text=True, timeout=30, env=_ENV)
        return {str(e.get("sessionId", "")): str(e.get("status", "")) for e in json.loads(r.stdout or "[]")}
    except Exception:
        return {}


def session_between_turns(role, cbusy: dict[str, str], repo_dir: str) -> bool | None:
    if role.ecosystem == "claude":
        st = next((v for k, v in cbusy.items() if role.session and k.startswith(role.session)), None)
        return None if st is None else st != "busy"
    if role.ecosystem == "opencode":
        try:
            from backends import get as get_backend
            s = get_backend("opencode").status(role.session, repo_dir)
            return "status:idle" in s if s else None
        except Exception:
            return None
    return None


# ---- the policy -----------------------------------------------------------------

def evaluate(spec: HagSpec, view, cbusy: dict[str, str]) -> list[dict]:
    """[{slug, condition, key, since, detail}] for every live condition instance."""
    from state import read_wait
    out = []
    cfg = health_cfg(spec)
    validity = float(cfg.get("validity_wake_min", 90))
    for role in spec.roles:
        if getattr(role, "fill", "") == "none" and role.slug == "hag-relay":
            continue
        if not role.session:
            continue
        slug = role.slug
        q = view.current_queue(slug) or {}
        st = view.role_state(slug) or {}
        boundary = max(q.get("ts", ""), st.get("ts", ""))
        idle = view.is_idle(slug)
        posture = st.get("state") or ("idle" if idle else "active")
        sub = st.get("sub", "")
        between = session_between_turns(role, cbusy, spec.dir)
        # unacked work
        for o in view.queue(slug):
            if o["ts"] < boundary:  # strictly older: a same-second offer cannot have been seen
                continue
            now_ok = o.get("now") and (o["by"] in ("user", "hag.py") or view.perms.roles.get(o["by"], {}).get("can_fill_others"))
            if idle or now_ok:
                out.append({"slug": slug, "condition": "unacked-work", "key": f"offer:{o['id']}", "since": o["ts"],
                            "detail": f"offer {o.get('item')} by {o['by']} not acknowledged"})
        wf = wake_path(spec.hag, slug)
        if os.path.exists(wf):
            ts = iso(datetime.datetime.fromtimestamp(os.path.getmtime(wf), datetime.timezone.utc))
            out.append({"slug": slug, "condition": "unacked-work", "key": f"wakefile:{ts}", "since": ts,
                        "detail": "health wake request not yet answered by a queue write"})
        # waits
        w = read_wait(spec.hag, slug) or {}
        target = w.get("target") or st.get("target", "")
        if posture == "waiting" and sub not in USER_WAITS:
            pending, why = target_pending(target, slug)
            if pending is False:
                out.append({"slug": slug, "condition": "wait-resolved", "key": f"target:{target}", "since": iso(),
                            "detail": f"wait target {target} no longer pending ({why})", "first_seen": True})
            if sub in CI_WAITS or target.split(":", 1)[0] in ("lease", "job", "gh-run", "gh-pr", "az-build"):
                a = age_min(boundary)
                if validity > 0 and a is not None and a >= validity:
                    out.append({"slug": slug, "condition": "wait-validity", "key": f"validity:{boundary}",
                                "since": boundary, "detail": f"waiting {a:.0f}m on {sub} ({target or 'no target'}; "
                                f"heuristic: {why}); re-verify the wait is still valid"})
        # watcher
        alive, sc = watcher_alive(spec.hag, slug)
        if not alive and posture != "offline" and between is not False:
            since = sc.get("armed_utc") or boundary or iso()
            ex = sc.get("exit", "")
            if ex:
                since = max(since, ex.split(" ")[-1])
            out.append({"slug": slug, "condition": "watcher-dead", "key": f"pid:{sc.get('pid', 'none')}", "since": since,
                        "first_seen": True,
                        "detail": f"ledger-watch not running ({'designed exit, no re-arm: ' + ex if ex else 'no exit verdict: killed or never armed'})"})
        # stale active
        if posture == "active" and between:
            last = view.last_by.get(slug, "")
            out.append({"slug": slug, "condition": "stale-active", "key": f"last:{last}", "since": last,
                        "detail": f"posture active, last ledger write {last}, session between turns"})
    return out


def run(spec: HagSpec, instance: str, dry_run: bool = False) -> int:
    from ledger import Ledger, View
    sdir = state_dir(spec.hag)
    os.makedirs(sdir, exist_ok=True)
    with open(os.path.join(sdir, f"tick-health-{instance}"), "w") as fh:
        fh.write(iso() + "\n")
    lock = open(os.path.join(sdir, "health.lock"), "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print(f"{iso()} health-{instance}: peer instance holds the lock; it acts this cycle")
        return 0
    spath = os.path.join(sdir, "health.json")
    try:
        state = json.load(open(spath))
    except (OSError, ValueError):
        state = {}
    acts = []
    # quorum: the peer must be ticking
    peer = "b" if instance == "a" else "a"
    pt = os.path.join(sdir, f"tick-health-{peer}")
    pa = age_min(open(pt).read().strip()) if os.path.exists(pt) else None
    if pa is not None and pa > PEER_STALE_MIN and state.get("peer_paged") != open(pt).read().strip():
        acts.append(("user", "peer-dead", page_user(spec, f"health-{peer} has not ticked for {pa:.0f} min "
                                                     f"(health-{instance} still running)", state, dry_run)))
        state["peer_paged"] = open(pt).read().strip()
    led = Ledger(spec.hag, root=spec.ledger_dir)
    led.pull()
    view = View(led, spec)
    conds = evaluate(spec, view, claude_busy())
    ladders = state.setdefault("ladders", {})
    live_keys = set()
    for c in conds:
        k = f"{c['slug']}|{c['condition']}|{c['key']}"
        live_keys.add(k)
        lad = ladders.setdefault(k, {"since": c["since"] if not c.get("first_seen") else iso(), "step": 0})
        a = age_min(lad["since"]) or 0.0
        if c["condition"] == "wait-validity":
            v = float(health_cfg(spec).get("validity_wake_min", 90))
            steps = [(0, "wake"), (v, "wake"), (2 * v, "page")]
        else:
            steps = LADDERS[c["condition"]]
        while lad["step"] < len(steps) and a >= steps[lad["step"]][0]:
            at, action = steps[lad["step"]]
            if action == "wake":
                ch = request_wake(spec, c["slug"], c["detail"], c["condition"], dry_run)
                if ch.startswith("no-adapter"):
                    ch += "; " + page_user(spec, f"{c['slug']}: {c['detail']} (and no wake adapter: {ch})", state, dry_run)
            else:
                ch = page_user(spec, f"{c['slug']}: {c['detail']} (woken {lad['step']}x without effect)", state, dry_run)
            acts.append((c["slug"], f"{c['condition']} step{lad['step'] + 1}:{action}", ch))
            lad["step"] += 1
            break  # at most one action per condition per run
    for k in list(ladders):
        if k not in live_keys:
            del ladders[k]  # condition cleared: its ladder resets
    # daily digest of maintainer waits; deferred pages once the maintainer is back
    today = iso()[:10]
    waits = [f"{s}:{st.get('sub')} ({st.get('holder', '')})" for s, st in view.states.items()
             if st.get("state") == "waiting" and st.get("sub") in USER_WAITS]
    if waits and state.get("digest_day") != today and view.user_offline() is None:
        acts.append(("user", "user-waits digest", page_user(spec, "roles waiting on you: " + "; ".join(waits), state, dry_run)))
        state["digest_day"] = today
    if view.user_offline() is None and state.get("deferred_pages"):
        dp = state.pop("deferred_pages")
        acts.append(("user", "deferred pages", page_user(spec, f"{len(dp)} pages deferred while offline: "
                                                          + " | ".join(p["text"][:120] for p in dp), state, dry_run)))
    if not dry_run:
        with open(spath + ".tmp", "w") as fh:
            json.dump(state, fh, indent=1)
        os.replace(spath + ".tmp", spath)
        try:
            from ledger import sync_push
            sync_push(led.root)
        except Exception:
            pass
    for slug, what, ch in acts:
        print(f"{iso()} health-{instance}: {slug} {what} -> {ch}")
        if not dry_run:
            try:
                from watchers import hag_emit
                hag_emit(spec.hag, "health", "script", "watch_event", kind="health", slug=slug,
                         note=f"{what} -> {ch}", tier=("T3" if "page" in what or ch.startswith("page") else "T2"))
            except Exception:
                pass
    if not acts:
        print(f"{iso()} health-{instance}: ok ({len(conds)} condition(s) inside leeway)")
    return 0


def cmd_health(a) -> int:
    spec = HagSpec.load(a.spec or find_spec(a.hag))
    return run(spec, a.instance, dry_run=a.dry_run)


def cmd_relay_watch(a) -> int:
    """The Claude relay's single background waiter: exits (waking the relay session) when
    the relay queue holds undelivered wakes, printing them for the relay to send."""
    sdir = state_dir(a.hag)
    os.makedirs(sdir, exist_ok=True)
    with open(os.path.join(sdir, "relay-watch.pid"), "w") as fh:
        fh.write(str(os.getpid()))
    qp, cp = relay_queue_path(a.hag), os.path.join(sdir, "relay-queue.cursor")
    print(f"{iso()} relay-watch armed (pid {os.getpid()})", flush=True)
    while True:
        try:
            lines = open(qp).read().splitlines()
        except OSError:
            lines = []
        cur = int(open(cp).read().strip() or 0) if os.path.exists(cp) else 0
        if len(lines) > cur:
            for ln in lines[cur:]:
                d = json.loads(ln)
                print(f"RELAY-WAKE to={d['to']} session={d.get('session', '')} :: {d['text']}")
            with open(cp, "w") as fh:
                fh.write(str(len(lines)))
            print(f"{iso()} VERDICT RELAY-DELIVER {len(lines) - cur} (send each with SendMessage by name, then re-arm)")
            return 0
        time.sleep(a.interval)


RELAY_SLUG = "hag-relay"
RELAY_MODEL = "claude-haiku-4-5-20251001"
RELAY_DOC = """# hag-relay plan_state ({hag})

- role: `hag-relay` (infrastructure, not a worker; `fill=none`), model haiku
- generated by `hag.py relay-init`; lives in the HAG's data directory, not the design repo
- design: `.agents/hag_architecture.md` §6 (wake adapters)

## Mission

Wake a Claude role whose own ledger-watch is dead, when the health quorum decides a wake is due.
Nothing else: no project work, no claims, no messages of your own.

## Current state

Generated at relay-init. The relay holds no work items; its only state is its relay-watch pid and
the relay-queue cursor under `/tmp/{hag}/`.

## Loop

1. Run `python3 {hagpy} relay-watch --hag {hag}` as a background command.
2. When it exits it prints one `RELAY-WAKE to=<session name> session=<id> :: <text>` line per
   wake. For each, call SendMessage with to=<session name> and the text verbatim. Add the `[ref]`
   from ListAgents only when two sessions share the name, choosing the one whose session id
   starts with <id>.
3. Re-arm relay-watch and prove its pid.
4. End the turn with one line: `relayed N`.

Keep the context small: read nothing else. A `killed` notification for relay-watch is a wake:
re-arm.

## Running monitors

- relay-watch: `python3 {hagpy} relay-watch --hag {hag}` (pid in `/tmp/{hag}/relay-watch.pid`)

## grants-needed

- None pending (the relay uses no grants: it only relays wakes the quorum decided).
"""


def relay_doc_path(hag: str) -> str:
    return os.path.join(roles_dir(hag), RELAY_SLUG, "plan_state.md")


def cmd_relay_init(a) -> int:
    """Generate the relay's start doc in the HAG data dir; add the role + features.relay to the spec."""
    path = a.spec or find_spec(a.hag)
    spec = HagSpec.load(path)
    doc = relay_doc_path(spec.hag)
    text = RELAY_DOC.format(hag=spec.hag, hagpy=os.path.join(HERE, "hag.py"))
    have = spec.role(RELAY_SLUG) is not None
    plan = (f"relay-init {spec.hag}: doc {doc}"
            + ("" if a.doc_only else f"; role {'present' if have else 'ADD'} ({RELAY_MODEL}, fill=none); features.relay=true"))
    if a.dry_run:
        print("[dry-run] " + plan)
        return 0
    os.makedirs(os.path.dirname(doc), exist_ok=True)
    with open(doc, "w") as fh:
        fh.write(text)
    if not a.doc_only:
        from roles import Role
        if not have:
            spec.roles.append(Role(slug=RELAY_SLUG, ecosystem="claude", title=RELAY_SLUG, doc=doc,
                                   fill="none", model=RELAY_MODEL, seq=1))
        else:
            spec.role(RELAY_SLUG).doc = doc
        spec.features = dict(spec.features or {}, relay=True)
        from hag import persist_spec
        persist_spec(spec, path, note="relay-init")
    print(plan)
    return 0


def add_parsers(sub) -> None:
    p = sub.add_parser("health", help="health quorum run (what the two health timers execute)")
    p.add_argument("--spec", default="")
    p.add_argument("--hag", default="")
    p.add_argument("--instance", choices=["a", "b"], default="a")
    p.add_argument("--dry-run", action="store_true", help="evaluate and print actions; wake and page nobody")
    p.set_defaults(func=cmd_health)
    p = sub.add_parser("relay-init", help="generate the hag-relay start doc (data dir) and add the role to the spec")
    p.add_argument("--hag", required=True)
    p.add_argument("--spec", default="")
    p.add_argument("--doc-only", action="store_true", help="write the start doc only; leave the spec alone")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_relay_init)
    p = sub.add_parser("relay-watch", help="the Claude relay session's waiter (exits when a wake is due)")
    p.add_argument("--hag", required=True)
    p.add_argument("--interval", type=float, default=15)
    p.set_defaults(func=cmd_relay_watch)
