#!/usr/bin/env python3
"""shoal health quorum (architecture doc §6): watchers of the watchers, no model tokens.

    shoal health --spec <shoal.json> --instance a|b [--dry-run]     # what the two timers run
    shoal relay-watch --shoal H                                     # the Claude relay's one waiter

Two identical systemd timers per host (`shoal-<id>-health-a` at *:0/5, `-b` at *:2/5) run
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

Wakes go through direct session wake (backends.Backend.wake), one implementation per
ecosystem: opencode -> a send to the session; Claude with a live ledger-watch -> a wake
request file the watcher delivers (/tmp/<id>/wake-<slug>); Claude with a dead watcher -> the
wake relay queue (/tmp/<id>/relay-queue), drained by a small Claude session through the
harness's cross-session message. With features.health.revive a dead Claude worker is respawned
before its wake, and a dead relay is relaunched before anyone is paged (relay-dead: relaunch at
sighting, page 15, page 360). In user-offline mode,
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
from ledger import OFFER_WRITERS, SYSTEM_WRITER  # noqa: E402

import config  # noqa: E402
import providers  # noqa: E402
from roles import (WAKE_RELAY_SLUG, ShoalSpec, find_spec, is_wake_relay, roles_dir, state_dir,  # noqa: E402
                   wake_relay_role)

TOOL_PATH = config.tool_path()
_ENV = config.tool_env()
PEER_STALE_MIN = 15
LADDERS = {
    "unacked-work": [(10, "wake"), (30, "wake"), (60, "page")],
    "wait-resolved": [(15, "wake"), (60, "page")],
    "watcher-dead": [(10, "wake"), (40, "page")],
    "stale-active": [(90, "wake"), (180, "page")],
    # features.waitdiscipline (architecture doc §3.5, §6): no wait is silent past its rescan,
    # and a peer's wait on you is work you are woken for
    "wait-expired": [(0, "wake"), (60, "wake"), (120, "page")],
    "blocking-unscheduled": [(30, "wake"), (120, "page")],
    "parked-work": [(30, "wake"), (90, "wake"), (180, "page")],
    "relay-dead": [(10, "page"), (360, "page")],
    "hold-long": [(0, "wake"), (240, "page")],
}
# features.health.revive: a dead relay is relaunched before anyone is paged. Measured 2026-10-02: the
# Claude background service failed its own upgrade respawn and reaped every worker, the relay among
# them; relay-dead paged for six days while every wake it would have carried went nowhere.
REVIVE_RELAY_LADDER = [(0, "relaunch"), (15, "page"), (360, "page")]
CI_WAITS = ("remote-ci", "local-ci")
USER_WAITS = ("user-auth", "user-design", "user-feedback")


def utcnow() -> datetime.datetime:
    """Now; SHOAL_HEALTH_SHIFT_MIN (tests only) moves the clock forward to exercise the ladders."""
    return datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(
        minutes=float(config.env("HEALTH_SHIFT_MIN", "0") or 0))


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


def continuation_cfg(spec: ShoalSpec) -> dict | None:
    """features.continuation: true or {"after_min": N}; None when off."""
    c = (spec.features or {}).get("continuation")
    if not c:
        return None
    return c if isinstance(c, dict) else {}


def health_cfg(spec: ShoalSpec) -> dict:
    h = (spec.features or {}).get("health")
    return h if isinstance(h, dict) else {}


# ---- wake adapters -------------------------------------------------------------

def watcher_alive(sid: str, slug: str) -> tuple[bool, dict]:
    from ledger_watch import load_sidecar, sidecar_path
    sc = load_sidecar(sidecar_path(sid, slug))
    pid = sc.get("pid", "")
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as fh:
            cmd = fh.read()
        return (b"ledger-watch" in cmd and slug.encode() in cmd), sc
    except (OSError, ValueError):
        return False, sc


def wake_path(sid: str, slug: str) -> str:
    return os.path.join(state_dir(sid), f"wake-{slug}")


def relay_queue_path(sid: str) -> str:
    return os.path.join(state_dir(sid), "relay-queue")


def wake_text(spec: ShoalSpec, slug: str, reason: str, condition: str = "") -> str:
    """The text of a shoal wake, prefixed with the offline banner while the maintainer is away."""
    text = (f"SHOAL-WAKE {slug}@{spec.sid} [{condition or 'request'}]: {reason} || run "
            f"`shoal next --shoal {spec.sid} --slug {slug}`, act, write ONE `shoal queue`, re-arm your watcher.")
    # every wake carries the offline state, because a role that skips `next` never sees the banner
    # (measured 2026-09-24: a lane raised a blocking merge question 3 h into an offline window)
    try:
        from ledger import Ledger, View
        from worker import offline_banner
        banner = offline_banner(View(Ledger(spec.sid, root=spec.ledger_dir, sync=False), spec))
        if banner:
            text = banner + " || " + text
    except Exception:  # noqa: BLE001 - a wake must never fail on its annotation
        pass
    return text


def request_wake(spec: ShoalSpec, slug: str, reason: str, condition: str = "", dry_run: bool = False) -> str:
    """Wake one role through its ecosystem's direct session wake (backends.Backend.wake);
    returns the channel used."""
    role = spec.role(slug)
    if role is None:
        return "no-such-role"
    text = wake_text(spec, slug, reason, condition)
    from backends import get as get_backend
    try:
        backend = get_backend(role.ecosystem)
    except Exception as exc:  # noqa: BLE001
        return f"no-adapter ({str(exc)[:80]})"
    return backend.wake(spec, role, text, condition=condition, dry_run=dry_run)


def relay_alive(sid: str) -> bool:
    p = os.path.join(state_dir(sid), "relay-watch.pid")
    try:
        pid = open(p).read().strip()
        with open(f"/proc/{pid}/cmdline", "rb") as fh:
            return b"relay-watch" in fh.read()
    except (OSError, ValueError):
        return False


def page_user(spec: ShoalSpec, text: str, state: dict, dry_run: bool = False) -> str:
    """A maintainer page is one ledger msg to `user`; deferred while the maintainer is offline."""
    from ledger import Ledger, View
    led = Ledger(spec.sid, root=spec.ledger_dir)
    view = View(led, spec)
    if view.user_offline() is not None:
        state.setdefault("deferred_pages", []).append({"ts": iso(), "text": text[:400]})
        return "deferred (user offline)"
    if dry_run:
        return "page (dry-run)"
    led.append(SYSTEM_WRITER, "msg", to="user", kind="health", text=text[:500])
    channel = str(health_cfg(spec).get("page") or "")
    if channel.startswith("gh-issue:"):  # a place the maintainer is notified, not only the ledger
        repo, _, num = channel[len("gh-issue:"):].partition("#")
        r = subprocess.run(["gh", "issue", "comment", num, "--repo", repo, "--body",
                            f"**shoal {spec.sid} page** ({iso()}): {text[:1500]}"],
                           capture_output=True, text=True, timeout=60, env=_ENV)
        return "page+gh-issue" if r.returncode == 0 else f"page (gh-issue FAILED rc={r.returncode})"
    return "page"


# ---- wait-target heuristics ------------------------------------------------------

def target_pending(target: str, slug: str, sid: str = "", upstream: bool = False) -> tuple[bool | None, str]:
    """(True pending | False resolved | None unknown, detail) for a wait target: a lookup in the
    provider registry (providers.py, docs/providers.md)."""
    return providers.resolve(target, slug, sid, upstream=upstream)


def short_line(s: str, n: int = 90) -> str:
    return re.sub(r"\s+", " ", s or "").strip()[:n]


# ---- session liveness -------------------------------------------------------------

# `claude agents --json` turn state -> liveness. `status` is not a turn signal: it reads `busy` for as
# long as any background task runs, and every role keeps its ledger-watch running in the background,
# so every Claude role read as mid-turn and no between-turns condition could fire for one (measured
# 2026-09-29: a role ended its turn with posture active and sat 6.6 h unflagged, `state` done,
# `status` busy). An unknown `state` falls back to `status`, which errs toward "busy".
CLAUDE_TURN_STATES = {"working": "busy", "blocked": "blocked", "done": "idle"}
# `state` stopped being a turn signal too (measured 2026-10-08, Claude Code 2.1.286): it reads
# `working` for as long as a background task runs, so every idle role with a live watcher read as
# mid-turn and turn-ended-active / stale-active could not fire. The job's own `tempo` (in
# <claude config dir>/jobs/<id>/state.json) is the turn signal: `active` mid-turn (positive control:
# a session measured mid-turn), `idle` between turns (6 of 6 idle sessions with live watchers),
# `blocked` on a question. Used when present; otherwise `state`, then `status`.
CLAUDE_TEMPO = {"active": "busy", "idle": "idle", "blocked": "blocked"}


def claude_job_tempo(row: dict) -> str:
    """The job's `tempo` ('' when unknown). A row may carry it (tests); else the job state file."""
    if row.get("tempo"):
        return str(row["tempo"])
    from backends import fake_sessions
    if fake_sessions() is not None:  # tests: never read the host's real job files
        return ""
    jid = str(row.get("id") or "")
    if not jid or not re.fullmatch(r"[0-9a-f]{6,64}", jid):
        return ""
    base = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")
    try:
        with open(os.path.join(base, "jobs", jid, "state.json")) as fh:
            return str(json.load(fh).get("tempo") or "")
    except (OSError, ValueError):
        return ""


def claude_liveness(row: dict) -> str:
    """'busy'|'blocked'|'idle' from the job's tempo, else `state`, else the raw `status`."""
    tempo = CLAUDE_TEMPO.get(claude_job_tempo(row))
    if tempo:
        return tempo
    st = str(row.get("state") or "")
    return CLAUDE_TURN_STATES.get(st) or str(row.get("status", ""))


def claude_busy() -> dict[str, str]:
    """sessionId -> 'busy'|'blocked'|'idle' from `claude agents --json` (parsed; never the TTY form)."""
    try:
        from backends import fake_sessions
        fake = fake_sessions()  # SHOAL_FAKE_SESSIONS (tests): no harness call
        if fake is not None:
            rows = list(fake.get("claude") or [])
        else:
            r = subprocess.run(["claude", "agents", "--json"], capture_output=True, text=True, timeout=30, env=_ENV)
            rows = json.loads(r.stdout or "[]")
        # a session blocked on a question to the maintainer is never "between turns"
        return {str(e.get("sessionId", "")): claude_liveness(e) for e in rows}
    except Exception:
        return {}


def session_between_turns(role, cbusy: dict[str, str], repo_dir: str) -> bool | None:
    # SHOAL_HEALTH_BETWEEN (tests only): comma slugs to treat as proven between turns
    if role.slug in [x for x in config.env("HEALTH_BETWEEN").split(",") if x]:
        return True
    if role.ecosystem == "claude":
        st = next((v for k, v in cbusy.items() if role.session and k.startswith(role.session)), None)
        return None if st is None else st not in ("busy", "blocked")
    if role.ecosystem == "opencode":
        try:
            from backends import get as get_backend
            s = get_backend("opencode").status(role.session, repo_dir)
            return "status:idle" in s if s else None
        except Exception:
            return None
    return None


# ---- shared predicates (the quorum's evaluate() and the operator UI's read model) -----------

def watcher_dead(alive: bool, posture: str, between: bool | None) -> bool:
    """A role's ledger-watch counts as dead when it is not running, the role is not offline, and the
    session is not known to be mid-turn (a session mid-turn arms its watcher at the turn's end).
    `between` is session_between_turns(): True between turns, False mid-turn or on a question,
    None unknown (unknown counts, as a missing watcher on an unknown session is the failure)."""
    return not alive and posture != "offline" and between is not False


def parked_free(spec: ShoalSpec, view, role, q: dict, posture: str, sub: str,
                live: bool = True) -> tuple[list[str], dict[str, str]]:
    """(next items with no current blocker, {item: why its blocker is stale}) for a role asleep on
    work: idle, or waiting on anything but its own local-ci job (which usually holds the resources
    its next items need). Empty for an observer role (fill=none) and while a freeze is on. `live`
    lets blocker checks call providers (the quorum); the UI passes False to stay off the network."""
    if not ((posture == "idle" or (posture == "waiting" and sub != "local-ci"))
            and getattr(role, "fill", "") != "none" and view.freeze() is None):
        return [], {}
    from ledger import item_key
    import blockers as _bl
    stale_nb = _bl.stale(spec, view, role.slug, q, live=live)  # {} unless features.blockers
    nbk = {item_key(x) for x in (q.get("next_blocked") or {})} - set(stale_nb)
    return [x for x in (q.get("next") or []) if item_key(x) not in nbk], stale_nb


# ---- the policy -----------------------------------------------------------------

def evaluate(spec: ShoalSpec, view, cbusy: dict[str, str]) -> list[dict]:
    """[{slug, condition, key, since, detail}] for every live condition instance."""
    from state import read_wait
    out = []
    cfg = health_cfg(spec)
    validity = float(cfg.get("validity_wake_min", 90))
    for role in spec.roles:
        if is_wake_relay(role):
            continue
        if not role.session:  # parked or retired: no liveness conditions at all
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
            now_ok = o.get("now") and o["by"] in OFFER_WRITERS
            if idle or now_ok:
                out.append({"slug": slug, "condition": "unacked-work", "key": f"offer:{o['id']}", "since": o["ts"],
                            "detail": f"offer {o.get('item')} by {o['by']} not acknowledged"})
        wf = wake_path(spec.sid, slug)
        if os.path.exists(wf):
            ts = iso(datetime.datetime.fromtimestamp(os.path.getmtime(wf), datetime.timezone.utc))
            out.append({"slug": slug, "condition": "unacked-work", "key": f"wakefile:{ts}", "since": ts,
                        "detail": "health wake request not yet answered by a queue write"})
        # waits
        w = read_wait(spec.sid, slug) or {}
        target = w.get("target") or st.get("target", "")
        if posture == "waiting" and sub not in USER_WAITS:
            pending, why = target_pending(target, slug, spec.sid, upstream=(sub == "upstream"))
            if pending is False:
                out.append({"slug": slug, "condition": "wait-resolved", "key": f"target:{target}", "since": iso(),
                            "detail": f"wait target {target} no longer pending ({why})", "first_seen": True})
            # `upstream` waits resolve through their target (wait-resolved), not a periodic re-verify.
            # The ladder is keyed by the target, so re-declaring the same wait keeps escalating: keyed
            # by the boundary, every rewrite restarted it at step 1 and it never paged (measured
            # 2026-09-26: five wakes in a day on a wait whose PR had merged, no page).
            if sub != "upstream" and (sub in CI_WAITS or target.split(":", 1)[0] in providers.kinds()):
                a = age_min(boundary)
                if validity > 0 and a is not None and a >= validity:
                    out.append({"slug": slug, "condition": "wait-validity",
                                "key": f"validity:{target}" if target else f"validity:{boundary}",
                                "since": boundary, "detail": f"waiting {a:.0f}m on {sub} ({target or 'no target'}; "
                                f"heuristic: {why}); re-verify the wait is still valid"})
        # watcher
        alive, sc = watcher_alive(spec.sid, slug)
        if watcher_dead(alive, posture, between):
            since = sc.get("armed_utc") or boundary or iso()
            ex = sc.get("exit", "")
            if ex:
                since = max(since, ex.split(" ")[-1])
            out.append({"slug": slug, "condition": "watcher-dead", "key": f"pid:{sc.get('pid', 'none')}", "since": since,
                        "first_seen": True,
                        "detail": f"ledger-watch not running ({'designed exit, no re-arm: ' + ex if ex else 'no exit verdict: killed or never armed'})"})
        # wait discipline: expired rescans, and peers blocked on unscheduled items
        if (spec.features or {}).get("waitdiscipline"):
            ru = st.get("rescan_utc", "")
            if posture == "waiting" and ru and parse(ru) is not None and parse(ru) < utcnow():
                ask = " Re-raise your prompted ask (question tool / AskUserQuestion) or pivot to the next unblocked item." \
                    if sub in USER_WAITS else " Re-verify the blocker (is it still pending?), then write a fresh wait or pivot."
                out.append({"slug": slug, "condition": "wait-expired", "key": f"rescan:{ru}", "since": ru,
                            "detail": f"{sub} wait on '{str(st.get('holder', ''))[:80]}' passed its rescan {ru} unproven.{ask}"})
            planned = {x.lower() for x in [q.get("active") or ""] + list(q.get("next") or []) if x}
            import blockers as _blk
            for b in view.blocked_by(slug):
                item = (b["item"] or "").lower()
                # a wait on an item this role cannot take is the waiter's to fix (parked-work wakes it);
                # waking this role about it cannot help (measured 2026-10-08: three wakes in a minute)
                if item and _blk.takeable(spec, view, f"{slug}:{item}"):
                    continue
                if item and item.split(":", 1)[0] not in {p.split(":", 1)[0] for p in planned}:
                    out.append({"slug": slug, "condition": "blocking-unscheduled", "key": f"block:{b['role']}:{item}",
                                "since": b["since"],
                                "detail": f"{b['role']} has waited on your {b['item']} since {b['since']}, and your queue "
                                          f"does not schedule it: schedule it (active/next) or pass it with a reason"})
        # parked work: asleep (idle or waiting) while a next item has no recorded blocker. Idle is
        # never woken by anything else, so without this a role can sit on a full queue forever
        # (measured 2026-09-27: jlens-expert idle with three next items for two hours; ie-integration
        # waiting upstream for #44 while #372 and #457 sat behind it unrecorded). Keyed by the items,
        # so re-declaring the same parked state keeps escalating.
        # a local-ci wait is exempt: the role's own machine-bound job (under a lease) usually holds the
        # resources its next items need; remote-ci, upstream, peer and maintainer waits leave it free
        # a freeze parks everything by design, so it is exempt while on
        free, stale_nb = parked_free(spec, view, role, q, posture, sub)
        if free:
            why_stale = "".join(f"; {k} blocker stale ({stale_nb[k]})" for k in sorted(stale_nb))
            out.append({"slug": slug, "condition": "parked-work", "key": f"parked:{','.join(sorted(free))}",
                        "since": boundary or iso(), "first_seen": True,
                        "detail": f"{posture} while next items have no current blocker ({', '.join(free)}"
                                  f"{why_stale}): "
                                  f"pop one (`queue --active <item>`) or record why each cannot start "
                                  f"(`queue --blocked ITEM=<role>:<repo#n>|ask:..|upstream:<target>|lease:<key>|"
                                  f"hold:<why>`); a busy lease is a wait on `lease:<key>`, never idle"})
        # long holds (features.blockers): a hold re-recorded past hold_page_min is a question for the
        # maintainer, raised once per hold; deferred while offline like every page
        import blockers as _bl
        for k, b, first, age, n in _bl.long_holds(spec, q):
            out.append({"slug": slug, "condition": "hold-long", "key": f"hold:{k}:{first}", "since": first,
                        "first_seen": True,  # the ladder runs from this sighting: wake now, page 240 min later
                        "detail": f"{k} held since {first} ({age / 60:.1f} h, re-recorded {n}x): {b[:120]}. "
                                  f"A hold this old is a question: record it as an ask (`queue --ask`) or name "
                                  f"a checkable blocker (`<role>:<repo#n>`, `upstream:<target>`); a hold still "
                                  f"standing 4 h after this wake pages the maintainer"})
        # continuation: a turn that ended while the posture says active (architecture doc §3.2)
        cont = continuation_cfg(spec)
        if cont is not None and posture == "active" and between:
            last = view.last_by.get(slug, "")
            act = q.get("active") or "(no active item)"
            out.append({"slug": slug, "condition": "turn-ended-active", "key": f"stop:{last}", "since": iso(),
                        "first_seen": True,
                        "detail": f"CONTINUE {act}: your turn ended with posture active and nothing running. Continue "
                                  f"the item now; if it cannot continue, write your real sleep posture: `queue --wait "
                                  f"<sub> --holder .. --target ..` after arming the waiter (GPU work: a job unit under "
                                  f"the lease), `--active <next item>` to pivot, or `--idle`"})
            continue_done = True
        else:
            continue_done = False
        # stale active (the crash catcher; superseded by continuation when that is on)
        if posture == "active" and between and not continue_done:
            last = view.last_by.get(slug, "")
            out.append({"slug": slug, "condition": "stale-active", "key": f"last:{last}", "since": last,
                        "detail": f"posture active, last ledger write {last}, session between turns"})
    return out


def relaunch_relay(spec: ShoalSpec, spec_path: str, dry_run: bool = False) -> str:
    """features.health.revive: `relay-launch` from the quorum, without its blocking verification
    (the next run sees relay-watch alive, or the ladder's page step fires). A relay session still
    mid-turn is left alone: relay-launch refuses it, and that refusal is the channel reported."""
    import argparse
    import contextlib
    import io
    buf = io.StringIO()
    a = argparse.Namespace(spec=spec_path, sid=spec.sid, force=False, verify_s=0, dry_run=dry_run)
    try:
        with contextlib.redirect_stdout(buf):
            rc = cmd_relay_launch(a)
    except Exception as exc:  # a failed launch must not stop the quorum run
        return f"relay-launch FAILED: {exc}"
    last = (buf.getvalue().strip().splitlines() or [""])[-1]
    return f"relay-launch rc={rc}: {last[:160]}"


def run(spec: ShoalSpec, instance: str, dry_run: bool = False, spec_path: str = "") -> int:
    from ledger import Ledger, View
    sdir = state_dir(spec.sid)
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
    led = Ledger(spec.sid, root=spec.ledger_dir)
    led.pull()
    view = View(led, spec)
    conds = evaluate(spec, view, claude_busy())
    relay = wake_relay_role(spec)
    revive = bool(health_cfg(spec).get("revive"))
    if (spec.features or {}).get("relay") and relay is not None and relay.session and not relay_alive(spec.sid):
        # the relay has no wake adapter of its own: its death is a page, or (revive) a relaunch first.
        # Evaluated ahead of the roles, so a relaunch is under way before this run queues their wakes.
        tk = os.path.join(sdir, "tick-relay-watch")
        since = open(tk).read().strip() if os.path.exists(tk) else iso()
        conds.insert(0, {"slug": relay.slug, "condition": "relay-dead", "key": f"relay:{since}", "since": since,
                         "first_seen": revive,  # revive: the ladder runs from this sighting (relaunch now, page 15 later)
                         "detail": f"relay-watch is not running (last tick {since}): Claude roles with a dead watcher "
                                   f"have no wake path; relaunch with `shoal relay-launch --shoal {spec.sid}`"})
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
        elif c["condition"] == "turn-ended-active":
            m = float((continuation_cfg(spec) or {}).get("after_min", 5))
            steps = [(m, "wake"), (m + 30, "wake"), (m + 90, "page")]
        elif c["condition"] == "relay-dead" and revive:
            steps = REVIVE_RELAY_LADDER
        else:
            steps = LADDERS[c["condition"]]
        while lad["step"] < len(steps) and a >= steps[lad["step"]][0]:
            at, action = steps[lad["step"]]
            if action == "relaunch":
                ch = relaunch_relay(spec, spec_path, dry_run)
            elif action == "wake":
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
    waits = []
    for s_, st in view.states.items():
        if spec.role(s_) is not None and not spec.role(s_).session:
            continue  # parked: its ask left with its session
        if st.get("state") == "waiting" and st.get("sub") in USER_WAITS:
            asks = (view.current_queue(s_) or {}).get("asks") or []
            role_ = spec.role(s_)
            waits.append(f"{role_.label if role_ else s_}: {st.get('sub')} ({str(st.get('holder', ''))[:80]})"
                         + (f" ASKS: {' | '.join(a[:120] for a in asks)}" if asks else " NO ASK RECORDED"))
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
                from watchers import shoal_emit
                shoal_emit(spec.sid, "health", "script", "watch_event", kind="health", slug=slug,
                         note=f"{what} -> {ch}", tier=("T3" if "page" in what or ch.startswith("page") else "T2"))
            except Exception:
                pass
    if not acts:
        print(f"{iso()} health-{instance}: ok ({len(conds)} condition(s) inside leeway)")
    return 0


def cmd_health(a) -> int:
    path = a.spec or find_spec(a.sid)
    spec = ShoalSpec.load(path)
    return run(spec, a.instance, dry_run=a.dry_run, spec_path=path)


def cmd_relay_watch(a) -> int:
    """The Claude relay's single background waiter: exits (waking the relay session) when
    the relay queue holds undelivered wakes, printing them for the relay to send."""
    sdir = state_dir(a.sid)
    os.makedirs(sdir, exist_ok=True)
    with open(os.path.join(sdir, "relay-watch.pid"), "w") as fh:
        fh.write(str(os.getpid()))
    qp, cp = relay_queue_path(a.sid), os.path.join(sdir, "relay-queue.cursor")
    tick = os.path.join(sdir, "tick-relay-watch")
    deadline = time.time() + a.max_hours * 3600 if a.max_hours > 0 else None
    print(f"{iso()} relay-watch armed (pid {os.getpid()}; runs until a wake is due"
          + (f" or {a.max_hours:g} h pass" if deadline else ", indefinitely") + ")", flush=True)
    while True:
        with open(tick, "w") as fh:  # self-tick: an old tick is a dead relay, never a quiet one
            fh.write(iso() + "\n")
        if deadline and time.time() > deadline:
            print(f"{iso()} VERDICT RELAY-MAX-HOURS {a.max_hours:g} (nothing to relay; re-arm)")
            return 0
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


RELAY_SLUG = WAKE_RELAY_SLUG
# The cheapest model that runs unattended: Haiku 4.5 reports "auto mode unavailable for this model",
# so a relay on it stops at its first permission prompt; Haiku 5.5 runs in auto mode (both measured
# 2026-10-08 with `claude --bg`). The `haiku` alias still resolves to 4.5, so the id is explicit.
RELAY_MODEL = "claude-haiku-5-5"
RELAY_DOC = """# {slug} plan_state ({sid})

- role: `{slug}` (infrastructure, not a worker; `fill=none`): the Claude implementation of direct
  session wake, model haiku
- generated by `shoal relay-init`; lives in the shoal's data directory, not the design repo
- design: `docs/architecture.md` §7 (direct session wake) in the shoal repository

## Mission

Wake a Claude role whose own ledger-watch is dead, when the health quorum decides a wake is due.
Nothing else: no project work, no claims, no messages of your own.

## Current state

Generated at relay-init. The relay holds no work items; its only state is its relay-watch pid and
the relay-queue cursor under `/tmp/{sid}/`.

## Loop

1. Run `{cli} relay-watch --shoal {sid}` as a background command. Arm it with the Bash tool, run_in_background=true and timeout=7200000, on exactly that command: no trailing `&`, no nohup, and NEVER the Monitor tool. A background Bash command wakes you when it exits, and is killed at its timeout: 30 min by default, 2 h at most, so always pass the maximum (measured 2026-10-08: without it, an idle lane spent a turn every 30 min re-arming); a Monitor expires (5 min by default, 30 min at most) and every expiry costs a turn. (Measured 2026-09-23: a relay that wrapped relay-watch in `&` plus a Monitor took 21 empty turns in 2 hours, relaying nothing.)
2. When it exits it prints one `RELAY-WAKE to=<session name> session=<id> :: <text>` line per
   wake. For each, call SendMessage with to=<session name> and the text verbatim. Add the `[ref]`
   from ListAgents only when two sessions share the name, choosing the one whose session id
   starts with <id>.
3. Re-arm relay-watch (same rule: Bash, run_in_background=true, timeout=7200000, no `&`, no Monitor) and prove its
   pid with TWO separate commands: first `cat /tmp/{sid}/relay-watch.pid`, then `ps -p <that
   number>`. Never use $(...) command substitution: it always needs a permission prompt.
4. End the turn with one line: `relayed N`.

Never transition yourself `offline` and never kill relay-watch: only the operator stops the relay
(`claude stop <id>`). If your session is being closed, leave relay-watch running and end the turn.

Keep the context small: read nothing else. A `killed` notification for relay-watch is a wake:
re-arm.

## Running monitors

- relay-watch: `{cli} relay-watch --shoal {sid}` (pid in `/tmp/{sid}/relay-watch.pid`)

## grants-needed

- None pending (the relay uses no grants: it only relays wakes the quorum decided).
"""


def relay_doc_path(sid: str, slug: str = "") -> str:
    return os.path.join(roles_dir(sid), slug or RELAY_SLUG, "plan_state.md")


def cmd_relay_init(a) -> int:
    """Generate the relay's start doc in the shoal data dir; add the role + features.relay to the spec."""
    path = a.spec or find_spec(a.sid)
    spec = ShoalSpec.load(path)
    existing = wake_relay_role(spec)
    slug = existing.slug if existing is not None else RELAY_SLUG
    doc = relay_doc_path(spec.sid, slug)
    text = RELAY_DOC.format(sid=spec.sid, cli=config.cli(), slug=slug)
    have = existing is not None
    plan = (f"relay-init {spec.sid}: doc {doc}"
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
            existing.doc = doc
        spec.features = dict(spec.features or {}, relay=True)
        from cli import persist_spec
        persist_spec(spec, path, note="relay-init")
    print(plan)
    return 0


def cmd_relay_launch(a) -> int:
    """(Re)launch the relay session alone: no restart, no incarnation bump. The relay's seq
    bumps (new tag), the new session id is recorded in the spec, and the launch counts only
    once relay-watch is proven running."""
    path = a.spec or find_spec(a.sid)
    spec = ShoalSpec.load(path)
    role = wake_relay_role(spec)
    if role is None:
        print(f"ERROR: no wake relay role in {spec.sid}: run `shoal relay-init --shoal {spec.sid}` first")
        return 1
    if relay_alive(spec.sid) and not a.force:
        print(f"relay-watch already running (pid {open(os.path.join(state_dir(spec.sid), 'relay-watch.pid')).read().strip()}); "
              f"nothing to do (--force to launch anyway)")
        return 0
    st = claude_busy().get(next((k for k in claude_busy() if role.session and k.startswith(role.session)), ""), "")
    if st == "busy" and not a.force:
        print(f"ERROR: the current relay session {role.session} is still executing a turn; stop it first "
              f"(`claude stop {role.session[:8]}`) or pass --force")
        return 1
    old = role.session
    role.seq += 1
    plan = f"relay-launch {spec.sid}: {role.label} model={role.model} (previous session {old or '-'}; incarnation unchanged)"
    if a.dry_run:
        print("[dry-run] " + plan)
        return 0
    from cli import persist_spec, write_brief
    from backends import get as get_backend
    brief = open(write_brief(spec, role)).read()
    role.session = get_backend("claude").create(f"{role.title}-r{role.seq}", spec.dir, brief=brief,
                                                name=role.tag, model=role.model, effort=role.effort)
    spec.record_generation()
    persist_spec(spec, path, note=f"relay-launch {role.tag}")
    print(plan + f" -> launched {role.session}")
    if a.verify_s <= 0:
        print(f"RELAY LAUNCHED {role.session}: relay-watch not verified here (the health quorum checks it next run)")
        return 0
    t0 = time.time()
    while time.time() - t0 < a.verify_s:
        if relay_alive(spec.sid):
            pid = open(os.path.join(state_dir(spec.sid), "relay-watch.pid")).read().strip()
            print(f"RELAY LOOP ALIVE: relay-watch pid {pid} (verify it was armed as a Bash background command: "
                  f"`claude logs {role.session}`)")
            return 0
        time.sleep(10)
    print(f"RELAY LOOP INCOMPLETE: no relay-watch within {a.verify_s:g}s; check `claude logs {role.session}` "
          f"(a pending grant ask or a failed arm)")
    return 4


def add_parsers(sub) -> None:
    p = sub.add_parser("health", help="health quorum run (what the two health timers execute)")
    p.add_argument("--spec", default="")
    config.add_id_arg(p, default="")
    p.add_argument("--instance", choices=["a", "b"], default="a")
    p.add_argument("--dry-run", action="store_true", help="evaluate and print actions; wake and page nobody")
    p.set_defaults(func=cmd_health)
    p = sub.add_parser("relay-init", help="generate the wake relay start doc (data dir) and add the role to the spec")
    config.add_id_arg(p, required=True)
    p.add_argument("--spec", default="")
    p.add_argument("--doc-only", action="store_true", help="write the start doc only; leave the spec alone")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_relay_init)
    p = sub.add_parser("relay-launch", help="(re)launch the wake relay session alone (no restart, no incarnation bump)")
    config.add_id_arg(p, required=True)
    p.add_argument("--spec", default="")
    p.add_argument("--force", action="store_true")
    p.add_argument("--verify-s", type=float, default=300)
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_relay_launch)
    p = sub.add_parser("relay-watch", help="the Claude relay session's waiter (exits when a wake is due)")
    config.add_id_arg(p, required=True)
    p.add_argument("--interval", type=float, default=15)
    p.add_argument("--max-hours", type=float, default=0,
                   help="exit after this long with nothing to relay (default 0: run indefinitely)")
    p.set_defaults(func=cmd_relay_watch)
