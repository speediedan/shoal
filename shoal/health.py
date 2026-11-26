#!/usr/bin/env python3
"""shoal health quorum (architecture doc §6): watchers of the watchers, no model tokens.

    shoal health --spec <shoal.json> --instance a|b [--dry-run]     # what the two timers run

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
  wait-resolved      a machine-checkable wait target is no longer pending, or every reservation  wake@15 restart@60
                     a research wait on a `time:` target names has ended
  asks-on-return     the maintainer came back; a role holding asks has not written since    wake@3 (once a return)
  blocker-resolved   a next item's role or provider blocker cleared                         wake@0
  wait-validity      any CI / lease / job wait, every validity_wake_min (default 90)       wake@V  wake@2V restart@3V
                     (not a target its provider confirms pending, under wait discipline; a restart is held
                     mid-turn, and pages only when it fails)
  lease-orphan       (watch_leases) a lease its provider marks orphaned, since its owner ended   page@30
  watcher-dead       ledger-watch pid gone, session between turns                          wake@10 page@40
  wakes-held         the role's live watcher holds new events for an attached client       page@30 (once a hold)
                     (`codex resume`, `muse resume`); while it holds, the role's other ladders hold too.
                     Offline, a card's client is ended at 60 (detach_cards; a held Claude restart too)
  stale-active       status active, no ledger write, session between turns                wake@90 page@180
  user-waits         waiting on the maintainer: never wakes the role; one daily digest page
  restart-retry      a restart that failed (restart_record), its Claude session not running    restart after its backoff
                     (any restart that fails stays due: 5, 10, 20, then 30 min apart; paged after 3 in a row)
  harness-missing    a harness CLI this host's roles need resolves nowhere                     page@30
  job-throttled      a job on this host whose cgroup's full memory pressure averages 50% or more over 60 s
                     (jobs.THROTTLE_FULL_PCT): a WARN line when first seen, a page if it stays so    page@30
  claude-daemon      a Claude daemon or session a closing Herdr terminal would end (its HC_SCOPE)  page@10

Wakes go through direct session wake (backends.Backend.wake), one implementation per
ecosystem: opencode -> a send to the session; Claude with a live ledger-watch -> a wake
request file the watcher delivers (/tmp/<id>/wake-<slug>); Claude with a dead watcher -> a
direct wake (`claude --bg --resume`, after a stop when the session is idle at its prompt). In
user-offline mode, maintainer pages are deferred and delivered as one summary when the
maintainer is back.
"""
from __future__ import annotations

import datetime
import fcntl
import json
import os
import re
import shlex
import signal
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "telemetry"))
from ledger import OFFER_WRITERS, SYSTEM_WRITER  # noqa: E402

import accounts  # noqa: E402
from briefs import HEADLESS  # noqa: E402 - harnesses whose turns have no question tool (Codex, Muse)
import config  # noqa: E402
import providers  # noqa: E402
from roles import ShoalSpec, find_spec, host_of, is_detached_wake, is_home, is_local, state_dir  # noqa: E402

TOOL_PATH = config.tool_path()
_ENV = config.tool_env()
PEER_STALE_MIN = 15
ATTACHED_PAGE_MIN = 30  # a restart held for an attached client pages once after this (and `wakes-held` pages at it)
# features.health.detach_cards (default on): while the maintainer is offline, a role card's native client left attached
# past this grace is ended (SIGTERM to that client only; the session is untouched and the card falls back to its
# read-only view), so the wakes or the restart it holds go through (2026-11-22..24: three stalls of hours, each ended by
# hand the same way). An hour is twice the hold's page, so an online maintainer always hears of a hold first
CARD_DETACH_MIN = 60
# a Claude context restart held for a context at CARD_DETACH_FAR_X times its limit or more: every turn then costs at
# least that many fresh sessions' worth, and the model's window is near (2026-11-24: about 913k of a 1M window)
CARD_DETACH_FAR_MIN = 15
CARD_DETACH_FAR_X = 2
CARD_DETACH_WAIT_S = 5.0
LADDERS = {
    "unacked-work": [(10, "wake"), (30, "wake"), (60, "page")],
    # a role that slept through its wake on a resolved wait is handed to a fresh session at that wait: a page asked the
    # maintainer to do what the quorum can (2026-11-18); a page only when the restart fails
    "wait-resolved": [(15, "wake"), (60, "restart")],
    "watcher-dead": [(10, "wake"), (40, "page")],
    # an interactive client attached to an exec session (a native view opened from the role's card and left open) holds
    # every wake its watcher would deliver, rightly, but silently: one page per hold (2026-11-22: a user offer sat 6 h)
    "wakes-held": [(ATTACHED_PAGE_MIN, "page")],
    "stale-active": [(90, "wake"), (180, "page")],
    # features.waitdiscipline (architecture doc §3.5, §6): no wait is silent past its rescan,
    # and a peer's wait on you is work you are woken for
    "wait-expired": [(0, "wake"), (60, "wake"), (120, "page")],
    "blocking-unscheduled": [(30, "wake"), (120, "page")],
    "parked-work": [(30, "wake"), (90, "wake"), (180, "page")],
    "hold-long": [(0, "wake"), (240, "page")],
    # a blocker that cleared is news only its role can judge. Once: if the role does nothing, parked-work escalates
    "blocker-resolved": [(0, "wake")],
    # multi-host: a host that stopped beating has no quorum for its roles and nothing here can wake them,
    # so its silence is a page once per episode (the key carries its last beat)
    "host-silent": [(0, "page")],
    # two hosts configured with one `server` name both think the same roles are theirs (double watchers, wakes,
    # launches): a page to rename one, once per clash
    "host-name-clash": [(0, "page")],
    # features.health.watch_checkouts: a shared checkout that cannot take its upstream. Sessions then route around it
    # and it only gets worse, so it is a page, after an hour so that one in use mid-rebase stays quiet
    "checkout-stuck": [(60, "page")],
    # features.health.watch_leases: a lease its provider marks orphaned, counted from when its owner ended. The provider's
    # reaper frees one within minutes (say a 5 min grace, run every 5 min), so still held at 30 is the reaper failing
    "lease-orphan": [(30, "page")],
    # bounded context (context.py): a role past its limit that marked a quiet boundary `--restart-ready` is
    # handed to a fresh session; `auto` mode does it from here, `page` (the default) asks the operator
    "context-restart": [(0, "page")],
    # past its limit in `auto`, at a quiet boundary, with a start doc that fails its contract: the role fixes it first
    "context-doc": [(0, "wake"), (60, "page")],
    # the maintainer's return (user-offline off): a role holding asks raises them. `offline off` wakes only the roles of
    # the host it ran on, so each host's quorum wakes its own, once per return, after RETURN_GRACE_MIN
    "asks-on-return": [(0, "wake")],
    # open asks with no question on screen while the maintainer is online (`unraised_condition`): raised again, twice
    "asks-unraised": [(0, "wake"), (120, "wake")],
    # [accounts] quota_handoff: a role at a quiet boundary whose account nears a 5 h or weekly limit, or is clearly over
    # its share, is handed to the account balancing picks (accounts.balance_pick); a share move is held through the
    # role's cooldown and this host's settle (balance_hold)
    "quota-handoff": [(0, "restart")],
    # a restart that failed (restart_record) whose role no other restarting condition covers this run: tried again once
    # its backoff has passed, and paged after RESTART_FAIL_PAGE failures in a row
    "restart-retry": [(0, "restart")],
    # a harness CLI a local role needs resolves nowhere (`config.harness_missing`): an update takes a minute or so, so
    # one still missing after 30 min is a broken install, paged once
    "harness-missing": [(30, "page")],
    # a job unit on this host whose own cgroup stalls on memory most of the time (jobs.THROTTLE_FULL_PCT): its unit
    # reads `active` while it makes little progress (2026-11-25: an hour in D state at 3 MB/s behind a slice over its
    # MemoryHigh, its GPU reservation idle). A WARN line when first seen; a page if it is still so 30 min later
    "job-throttled": [(30, "page")],
    # a Claude daemon or session that carries a Herdr terminal's HC_SCOPE: closing that terminal ends it, and for the
    # daemon every session on the account (2026-11-25, twice). Nothing is wrong yet, so a page only after 10 min
    "claude-daemon": [(10, "page")],
}
# the conditions whose ladders restart their role
RESTARTING = ("context-restart", "quota-handoff", "wait-resolved", "wait-validity", "restart-retry")
CONTEXT_AUTO_LADDER = [(0, "restart"), (30, "page")]
# wakes without effect on these end in a context restart at the role's recorded wait, not a page (a page only when the
# restart fails); held while the session is mid-turn
RESTART_NOT_PAGE = ("wait-resolved", "wait-validity")
# conditions that do not reach the role through its watcher, so a hold for an attached client does not hold their ladders
HOLD_EXEMPT = ("wakes-held", "watcher-dead", "host-silent", "host-name-clash", "checkout-stuck", "lease-orphan",
               "harness-missing", "job-throttled", "claude-daemon")


def claude_clients(role) -> tuple[list[int], tuple[str, ...]]:
    """(the `claude attach` pids holding a Claude role's session, its session and job ids), one listing read."""
    import park
    from backends import get as get_backend
    row = get_backend("claude")._row(role.session, include_stopped=True) or {}
    if row.get("kind") == "interactive":  # a terminal session, not a card's client
        return [], ()
    return park.claude_attach_pids(role, row), (role.session, park.claude_job(role, row))


def restart_client(role) -> str:
    """Why a person may be in a Claude role's session ('' when nothing says so, or for another harness): the quorum's
    automatic restart waits for them to detach."""
    if role is None or role.ecosystem != "claude" or not role.session:
        return ""
    try:
        import park
        return park._claude_client(role)
    except Exception:  # noqa: BLE001 - an unreadable listing never blocks the restart that would report it
        return ""
# wake economy (architecture doc §7, "When roles wake"): held while the maintainer is away, re-woken for
# the same evidence at most once per REWAKE_MIN, quiet for a while after the maintainer returns (the
# return wakes roles with asks itself), and at least WAKE_GAP_MIN between two health wakes of one role
OFFLINE_HOLD = ("parked-work", "hold-long", "blocking-unscheduled")
REWAKE_CONDS = ("parked-work", "hold-long", "blocker-resolved")
REWAKE_MIN = 720
ONLINE_SETTLE_MIN = 20
WAKE_GAP_MIN = 30
REARM_RETRY_MIN = 15
RETURN_GRACE_MIN = 3     # `offline off`'s own wake goes first on its host
RETURN_WINDOW_MIN = 720  # a return older than this is not news to a role that has not written since
ASKS_RAISE_MIN = 10      # an open ask with no question on screen this long after the role's last write is re-raised
HOST_BEAT_MIN = 15
HOST_SILENT_MIN = 60
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


def detach_cfg(spec: ShoalSpec) -> dict | None:
    """features.health.detach_cards: true (the default) or {"after_min": N}; None when false."""
    v = health_cfg(spec).get("detach_cards", True)
    if not v:
        return None
    after = v.get("after_min", CARD_DETACH_MIN) if isinstance(v, dict) else CARD_DETACH_MIN
    try:
        return {"after_min": float(after)}
    except (TypeError, ValueError):
        return {"after_min": float(CARD_DETACH_MIN)}


# ---- a card's native client left attached while the maintainer is offline -----------------------------------------

def signal_client(pid: int) -> None:
    """SIGTERM to one client process (tests replace this)."""
    os.kill(pid, signal.SIGTERM)


def client_gone(pid: int) -> bool:
    return not os.path.exists(f"/proc/{pid}")


def own_process(pid: int) -> bool:
    """True when `pid` runs on this host as this user: the quorum never signals another user's process."""
    try:
        return os.stat(f"/proc/{pid}").st_uid == os.getuid()
    except OSError:
        return False


def card_detach(spec: ShoalSpec, role, find, since: str, held: str, lad: dict, acts: list, step: str, offline: bool,
                dry_run: bool = False, far: bool = False) -> bool:
    """End a role card's native client that holds `held` (its wakes, or its restart) since `since`, when the maintainer
    is offline, features.health.detach_cards is on and the hold has outlived its grace (CARD_DETACH_MIN, or
    CARD_DETACH_FAR_MIN when `far`). Only a client positively identified as a card's (ledger_watch.card_client: its
    parents reach the role's observer or `shoal view`, naming the role or one of its session ids) and owned by this user on
    this host is signalled, once per hold and with SIGTERM only; a client started by hand is never touched. Records the
    detach as a ledger msg to `user`. `find()` gives (the attached client pids, the role's session ids), read only once
    the grace is up. True when the client was ended (its hold then lifts at the watcher's next tick, or the next run's
    restart), so the caller's ladder waits for that rather than paging the hold."""
    if lad.get("detached") or lad.get("detach_failed"):
        # once a hold: a client that would not go is the page's to raise, and so is one opened after the maintainer is back
        return bool(lad.get("detached")) and offline
    cfg = detach_cfg(spec)
    if cfg is None or not offline or role is None:
        return False
    grace = min(cfg["after_min"], CARD_DETACH_FAR_MIN) if far else cfg["after_min"]
    held_min = age_min(since) or 0.0
    if held_min < grace:
        return False
    try:
        pids, sessions = find()
    except Exception:  # noqa: BLE001 - an unreadable listing never ends a client
        return False
    if not pids:
        return False
    import ledger_watch as lw
    client, wrapper = 0, ""
    for pid in pids:
        client, wrapper = lw.card_client(pid, spec.sid, role.slug, sessions)
        if client:
            break
    if not client or not own_process(client):
        key = ",".join(str(p) for p in pids)
        if lad.get("not_card") != key:
            lad["not_card"] = key
            acts.append((role.slug, step, f"not detached: the client (pid {key}) is not a card's viewer on this host "
                                          "(a client started by hand is left to its owner)"))
        return False
    cmd = lw.client_cmd(lw.proc_argv(client)) or "?"
    text = (f"{role.slug}: detached the card's interactive client (pid {client}, `{cmd}`) after {held_min:.0f} min while "
            f"you were offline; it held {held}; the card is back to its read-only view")
    if dry_run:
        acts.append((role.slug, step, f"would detach (dry-run): {text}"))
        return False
    lad["detach_failed"] = iso()
    try:
        signal_client(client)
    except OSError as exc:
        acts.append((role.slug, step, f"detach FAILED (pid {client}: {exc})"))
        return False
    deadline = time.time() + CARD_DETACH_WAIT_S
    while not client_gone(client) and time.time() < deadline:
        time.sleep(0.2)
    if not client_gone(client):
        acts.append((role.slug, step, f"detach FAILED (pid {client}, `{cmd}`, under {wrapper}, did not exit on SIGTERM "
                                      f"within {CARD_DETACH_WAIT_S:g} s; left as it is)"))
        return False
    lad["detached"] = lad.pop("detach_failed")
    try:
        from ledger import Ledger
        Ledger(spec.sid, root=spec.ledger_dir).append(SYSTEM_WRITER, "msg", to="user", kind="health", text=text[:500])
        rec = "recorded"
    except Exception as exc:  # noqa: BLE001 - the detach is done; a failed record is said in the run's output
        rec = f"record FAILED ({str(exc)[:60]})"
    acts.append((role.slug, step, f"detached ({wrapper}; {rec}): {text}"))
    return True


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


def watcher_hold(sid: str, slug: str) -> dict:
    """The hold the role's live watcher records for a person at its session (ledger_watch.current_hold), or {}."""
    from ledger_watch import current_hold
    alive, sc = watcher_alive(sid, slug)
    return current_hold(sc, alive)


def wake_path(sid: str, slug: str) -> str:
    return os.path.join(state_dir(sid), f"wake-{slug}")


def wake_text(spec: ShoalSpec, slug: str, reason: str, condition: str = "", banner: str | None = None) -> str:
    """The text of a shoal wake, prefixed with the offline banner while the maintainer is away. `banner`: that banner
    as the caller already read it (a verb waking several roles reads the ledger once, not once per wake)."""
    text = (f"SHOAL-WAKE {slug}@{spec.sid} [{condition or 'request'}]: {reason} || run "
            f"`shoal next --shoal {spec.sid} --slug {slug}`, act, write ONE `shoal queue`, re-arm your watcher.")
    # every wake carries the offline state, because a role that skips `next` never sees the banner
    # (measured 2026-09-24: a lane raised a blocking merge question 3 h into an offline window)
    try:
        if banner is None:
            from ledger import Ledger, View
            from worker import offline_banner
            banner = offline_banner(View(Ledger(spec.sid, root=spec.ledger_dir, sync=False), spec))
        if banner:
            text = banner + " || " + text
    except Exception:  # noqa: BLE001 - a wake must never fail on its annotation
        pass
    try:
        import context as _ctx
        role = spec.role(slug)
        adv = _ctx.advisory(spec, role) if role is not None else ""
        if adv:
            text += " || " + adv
    except Exception:  # noqa: BLE001
        pass
    return text


def request_wake(spec: ShoalSpec, slug: str, reason: str, condition: str = "", dry_run: bool = False,
                 banner: str | None = None) -> str:
    """Wake one role through its ecosystem's direct session wake (backends.Backend.wake);
    returns the channel used. `banner`: the offline banner as already read (wake_text)."""
    role = spec.role(slug)
    if role is None:
        return "no-such-role"
    if not is_local(role, spec):
        # the role's own host delivers its wakes (its watcher reads the ledger; its quorum runs there)
        return f"remote (skipped: {slug} runs on {role.server or spec.home}, whose watcher and quorum wake it)"
    text = wake_text(spec, slug, reason, condition, banner)
    from backends import get as get_backend
    try:
        backend = get_backend(role.ecosystem)
    except Exception as exc:  # noqa: BLE001
        return f"no-adapter ({str(exc)[:80]})"
    return backend.wake(spec, role, text, condition=condition, dry_run=dry_run)


def page_channels(spec: ShoalSpec) -> list[str]:
    """The spec's page channels (`features.health.page`): one channel string, or a list of them.
    Known forms: `gh-issue:<owner/repo>#<n>` and `ntfy:<target>`, where the ntfy target is a full
    URL, a `<host>/<topic>` (https assumed), or a bare topic on ntfy.sh."""
    page = health_cfg(spec).get("page")
    if isinstance(page, str) and page:
        return [page]
    if isinstance(page, list):
        return [str(p) for p in page if p]
    return []


def ntfy_url(channel: str) -> str:
    rest = channel[len("ntfy:"):]
    if rest.startswith(("http://", "https://")):
        return rest
    return f"https://{rest}" if "/" in rest else f"https://ntfy.sh/{rest}"


def post_ntfy(channel: str, title: str, text: str) -> None:
    """One ntfy publish (raises on failure; the caller reports it like a failed gh-issue post).
    An access token, when the server needs one, comes from $SHOAL_NTFY_TOKEN, never the spec."""
    import urllib.request
    import config
    req = urllib.request.Request(ntfy_url(channel), data=text.encode(), method="POST")
    req.add_header("Title", title)
    req.add_header("Priority", "high")
    req.add_header("Tags", "warning")
    token = config.env("NTFY_TOKEN")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=15) as resp:  # noqa: S310 - the URL is the spec's own
        resp.read()


def deliver_page(spec: ShoalSpec, text: str) -> str:
    """Deliver one page to every configured channel; returns the suffix naming what happened
    (`+gh-issue+ntfy`, or a FAILED note per failed channel). Never raises: a dead channel must
    not stop the quorum run that paged."""
    out = []
    for channel in page_channels(spec):
        if channel.startswith("gh-issue:"):  # a place the maintainer is notified, not only the ledger
            repo, _, num = channel[len("gh-issue:"):].partition("#")
            r = subprocess.run(["gh", "issue", "comment", num, "--repo", repo, "--body",
                                f"**shoal {spec.sid} page** ({iso()}): {text[:1500]}"],
                               capture_output=True, text=True, timeout=60, env=_ENV)
            out.append("+gh-issue" if r.returncode == 0 else f" (gh-issue FAILED rc={r.returncode})")
        elif channel.startswith("ntfy:"):
            try:
                post_ntfy(channel, f"shoal {spec.sid} page", text[:1000])
                out.append("+ntfy")
            except Exception as exc:  # noqa: BLE001
                out.append(f" (ntfy FAILED {str(exc)[:60]})")
    return "".join(out)


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
    return "page" + deliver_page(spec, text)


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
# 2026-09-29: a role ended its turn with status active and sat 6.6 h unflagged, `state` done,
# `status` busy). An unknown `state` falls back to `status`, which errs toward "busy".
CLAUDE_TURN_STATES = {"working": "busy", "blocked": "blocked", "done": "idle"}
# `state` stopped being a turn signal too (measured 2026-10-08, Claude Code 2.1.286): it reads
# `working` for as long as a background task runs, so every idle role with a live watcher read as
# mid-turn and turn-ended-active / stale-active could not fire. The job's own `tempo` (in
# <claude config dir>/jobs/<id>/state.json) is the turn signal: `active` mid-turn (positive control:
# a session measured mid-turn), `idle` between turns (6 of 6 idle sessions with live watchers),
# `blocked` on a question. Used when present; otherwise `state`, then `status`.
CLAUDE_TEMPO = {"active": "busy", "idle": "idle", "blocked": "blocked"}


def claude_job_state(row: dict) -> dict:
    """The job's state file ({} when unknown), with `_age_s`: seconds since the file was last written. A row may carry
    `tempo`/`inFlight` (and `age_s`) itself (tests)."""
    if row.get("tempo"):
        return {"tempo": row["tempo"], "inFlight": row.get("inFlight") or {}, "fan": row.get("fan") or [],
                "_age_s": float(row.get("age_s") or 0)}
    from backends import fake_sessions
    if fake_sessions() is not None:  # tests: never read the host's real job files
        return {}
    jid = str(row.get("id") or "")
    if not jid or not re.fullmatch(r"[0-9a-f]{6,64}", jid):
        return {}
    from backends import job_base
    path = os.path.join(job_base(jid), "jobs", jid, "state.json")
    try:
        with open(path) as fh:
            st = json.load(fh)
        if not isinstance(st, dict):
            return {}
        st["_age_s"] = max(0.0, time.time() - os.path.getmtime(path))
        return st
    except (OSError, ValueError):
        return {}


def claude_job_tempo(row: dict) -> str:
    """The job's `tempo` ('' when unknown)."""
    return str(claude_job_state(row).get("tempo") or "")


def agents_in_flight(st: dict, row: dict | None = None) -> int:
    """How many of the session's own subagents are running (`inFlight` kinds naming an agent). A
    background shell does not count: a bare `run_in_background` command kept `state` at working and
    hid ended turns (2026-10-08), so only delegated agent work holds a turn open. Where the job lists its tasks
    (`fan`), only the subagents not finished by their own record count (backends.live_tasks): a finished one Claude
    Code still counted kept a seat reading working (2026-11-25)."""
    fl = st.get("inFlight") or {}
    if not isinstance(fl, dict) or not any("agent" in str(k) for k in fl.get("kinds") or []):
        return 0
    if any(isinstance(t, dict) and t.get("kind") not in ("todo", None) for t in st.get("fan") or []):
        from backends import live_tasks
        return live_tasks(row or {}, st)["agents"]
    try:
        return int(fl.get("tasks") or 0)
    except (TypeError, ValueError):
        return 0


def claude_liveness(row: dict) -> str:
    """'busy'|'blocked'|'idle' from the job's tempo, else `state`, else the raw `status`. A session
    whose turn ended while its own subagents run is still working: their reports re-enter it
    (measured 2026-10-13: three research agents writing proposals read as an idle session)."""
    st = claude_job_state(row)
    tempo = CLAUDE_TEMPO.get(str(st.get("tempo") or ""))
    if tempo == "idle" and agents_in_flight(st, row):
        return "busy"
    if tempo:
        return tempo
    st = str(row.get("state") or "")
    return CLAUDE_TURN_STATES.get(st) or str(row.get("status", ""))


_CLAUDE_ROWS: list = []  # the rows behind the last claude_busy() call


def claude_busy() -> dict[str, str]:
    """sessionId -> 'busy'|'blocked'|'idle' from `claude agents --json` (parsed; never the TTY form), across every
    Claude account on this host (backends.claude_agents)."""
    try:
        from backends import claude_agents, fake_sessions
        fake = fake_sessions()  # SHOAL_FAKE_SESSIONS (tests): no harness call
        if fake is not None:
            rows = list(fake.get("claude") or [])
        elif not accounts.several("claude"):
            from backends import run_harness
            r = run_harness("claude", ["agents", "--json"], wait_s=0, capture_output=True, text=True, timeout=30, env=_ENV)
            rows = json.loads(r.stdout or "[]")
        else:
            rows = claude_agents()
        _CLAUDE_ROWS[:] = rows  # session_between_turns reads a blocked session's row without a second listing
        # keyed by every id a role may have stored: the current conversation, the job id and the job's original
        # conversation (a `/clear` changes the first; backends.claude_find)
        from backends import claude_original
        out = {}
        for e in rows:
            live = claude_liveness(e)
            for k in (str(e.get("sessionId", "")), str(e.get("id", "")),
                      str(e.get("originalSessionId") or claude_original(str(e.get("id", ""))))):
                if k:
                    out.setdefault(k, live)
        return out
    except Exception:
        return {}


def claude_worker_gone(role, cbusy: dict[str, str]) -> bool:
    """True when a Claude role's session is listed (`claude agents --json --all`) with no worker.
    `cbusy` comes from the plain listing, which omits such a session, so only an absent session is
    probed again."""
    if not role.session or any(k.startswith(role.session[:8]) for k in cbusy):
        return False
    try:
        from backends import get as get_backend
        return bool(get_backend("claude").session_dead(role.session))
    except Exception:  # noqa: BLE001
        return False


def claude_transcript(session: str) -> str:
    """The newest transcript file of a Claude session, or "": of its CURRENT conversation, since a `/clear` starts
    a new transcript under a new id (backends.claude_current)."""
    import glob
    from backends import claude_bases
    hits = []
    if session:
        try:
            from backends import claude_current
            cur = claude_current(session)
        except Exception:  # noqa: BLE001
            cur = session
        for b in claude_bases():  # whichever account holds the session (one directory when none is declared)
            base = os.path.join(b, "projects")
            hits = glob.glob(os.path.join(base, "*", f"{cur}*.jsonl")) or glob.glob(os.path.join(base, "*", f"{session}*.jsonl"))
            if hits:
                break
    return max(hits, key=os.path.getmtime) if hits else ""


def claude_prompt_open(row: dict, session: str, tail_bytes: int = 524288) -> bool:
    """A Claude session is at a question prompt: `claude agents` says it waits for input (measured
    2026-10-08: `waitingFor` "input needed" on an AskUserQuestion), or its transcript tail has an
    AskUserQuestion with no result yet. A session that only ended its turn saying it waits on the
    maintainer reads `blocked` too (measured 2026-10-12, waitingFor None, no prompt)."""
    if row.get("waitingFor") or row.get("status") == "waiting":
        return True
    return bool(claude_open_prompt(session, tail_bytes))


def claude_open_prompt(session: str, tail_bytes: int = 524288) -> str:
    """The id of the newest AskUserQuestion with no result yet in the session's transcript tail, else "". A prompt
    after it closes it too: a turn stopped at the question and resumed (the maintainer's answer, `ledger_watch.through_question`)
    leaves the question without a result."""
    from backends import fake_sessions
    if fake_sessions() is not None:
        return ""
    path = claude_transcript(session)
    if not path:
        return ""
    try:
        with open(path, "rb") as fh:
            fh.seek(max(0, os.path.getsize(path) - tail_bytes))
            chunk = fh.read().decode(errors="replace")
    except OSError:
        return ""
    open_q: dict[str, bool] = {}  # in order, so the last is the newest
    for line in chunk.splitlines():
        if "AskUserQuestion" not in line and "tool_result" not in line and not (open_q and '"user"' in line):
            continue
        try:
            d = json.loads(line)
            content = (d.get("message") or {}).get("content")
        except (ValueError, AttributeError):
            continue
        blocks = [c for c in content if isinstance(c, dict)] if isinstance(content, list) else []
        for c in blocks:
            if c.get("type") == "tool_use" and c.get("name") == "AskUserQuestion":
                open_q[str(c.get("id") or "?")] = True
            elif c.get("type") == "tool_result":
                open_q.pop(str(c.get("tool_use_id")), None)
        # a message queued while a question is open is written as a queue operation, so a prompt here starts a new turn
        if (d.get("type") == "user" and content and not d.get("isMeta") and not d.get("isSidechain")
                and not any(c.get("type") == "tool_result" for c in blocks)):
            open_q.clear()
    return next(reversed(open_q), "")


# ---- the account usage limit ------------------------------------------------------------
# The Claude usage limit is account-wide (measured 2026-10-14, 03:06Z to the 04:30Z reset): a turn started
# by a message gets an API error (transcript: `"error": "rate_limit"`, `quotaLimits.resetsAt` epoch), a
# turn started by a task notification gets a grace note and the session usually stops. Every wake or
# relaunch spent inside the limit is lost, so health holds them until the reset and then wakes each
# role the limit touched once (a role lost its wake path and stayed down 13 h otherwise).
LIMIT_LOOKBACK_H = 6
LIMIT_RESET_GRACE_MIN = 2
LIMIT_GRACE_TEXT = "[Usage limit reached"


def claude_limit_marks(session: str, tail_bytes: int = 1048576) -> list[tuple[str, str, int]]:
    """[(ts, 'error'|'grace', resetsAt epoch or 0)] for usage-limit entries in a Claude session's
    transcript tail."""
    path = claude_transcript(session)
    if not path:
        return []
    try:
        with open(path, "rb") as fh:
            fh.seek(max(0, os.path.getsize(path) - tail_bytes))
            chunk = fh.read().decode(errors="replace")
    except OSError:
        return []
    out = []
    for line in chunk.splitlines():
        if "rate_limit" not in line and LIMIT_GRACE_TEXT not in line:
            continue
        try:
            d = json.loads(line)
        except ValueError:
            continue
        ts = str(d.get("timestamp") or "")[:19] + "Z"
        if d.get("error") == "rate_limit":
            try:
                reset = int((d.get("quotaLimits") or {}).get("resetsAt") or 0)
            except (TypeError, ValueError):
                reset = 0
            out.append((ts, "error", reset))
        elif d.get("type") == "user":
            content = (d.get("message") or {}).get("content")
            text = content if isinstance(content, str) else " ".join(
                str(c.get("text", "")) for c in content or [] if isinstance(c, dict))
            if text.lstrip().startswith(LIMIT_GRACE_TEXT):
                out.append((ts, "grace", 0))
    return out


def update_limit(spec: ShoalSpec, state: dict) -> dict:
    """Fold the Claude roles' usage-limit marks (last LIMIT_LOOKBACK_H hours) into state['limit']:
    {reset: newest known reset, affected: {slug: newest mark}, held: [slugs whose wakes were held],
    woken: the reset already answered with a resume wake}. With several Claude accounts on the host, `accounts` keeps
    each account's own newest reset: a limit is one account's, and holds only its roles' wakes (`limit_until`)."""
    lim = state.get("limit") or {}
    floor = iso(utcnow() - datetime.timedelta(hours=LIMIT_LOOKBACK_H))
    for role in spec.roles:
        if role.ecosystem != "claude" or not role.session or not is_local(role, spec):
            continue
        for ts, _kind, reset in claude_limit_marks(role.session):
            if ts < floor:
                continue
            if reset:
                r = iso(datetime.datetime.fromtimestamp(reset, datetime.timezone.utc))
                if r > lim.get("reset", ""):
                    lim["reset"] = r
                if accounts.several("claude"):
                    per = lim.setdefault("accounts", {})
                    k = accounts.name("claude", role.account)
                    per[k] = max(per.get(k, ""), r)
            aff = lim.setdefault("affected", {})
            aff[role.slug] = max(aff.get(role.slug, ""), ts)
    if lim.get("reset") and lim.get("woken") == lim["reset"] and lim["reset"] < floor:
        lim = {}  # answered and out of the lookback: forget it
    if lim:
        state["limit"] = lim
    else:
        state.pop("limit", None)
    return lim


def limit_until(lim: dict, account: str | None = None) -> str:
    """The reset time while the usage limit is in force (plus a short grace), else "". With `account` (a role's) and
    per-account resets recorded, only that account's limit counts."""
    per = (lim or {}).get("accounts")
    if account is not None and isinstance(per, dict):
        r = per.get(accounts.name("claude", account), "")
    else:
        r = (lim or {}).get("reset", "")
    t = parse(r)
    if t is None or utcnow() >= t + datetime.timedelta(minutes=LIMIT_RESET_GRACE_MIN):
        return ""
    return r


def limit_until_sid(sid: str, account: str | None = None) -> str:
    """limit_until() from a shoal's health state, for processes outside the quorum (ledger-watch)."""
    try:
        return limit_until(json.load(open(os.path.join(state_dir(sid), "health.json"))).get("limit") or {}, account)
    except (OSError, ValueError):
        return ""


def claude_background(role) -> int:
    """How many background tasks (shells, monitors, subagents) a Claude role's job has in flight, from the listing
    `claude_busy` last read: each one re-invokes the session when it ends, so a stop while any runs does not hold."""
    from backends import claude_find, work_in_flight
    e = claude_find(_CLAUDE_ROWS, role.session) if role.session else None
    st = claude_job_state(e) if e else {}
    fl = st.get("inFlight") or {}
    try:  # a left-over queued count is not work (backends.QUEUED_STALE_S)
        return int(fl.get("tasks") or 0) + int(fl.get("queued") or 0) if isinstance(fl, dict) and work_in_flight(st) else 0
    except (TypeError, ValueError):
        return 0


def session_between_turns(role, cbusy: dict[str, str], repo_dir: str) -> bool | None:
    # SHOAL_HEALTH_BETWEEN (tests only): comma slugs to treat as proven between turns
    if role.slug in [x for x in config.env("HEALTH_BETWEEN").split(",") if x]:
        return True
    if role.ecosystem == "claude":
        st = next((v for k, v in cbusy.items() if role.session and k.startswith(role.session)), None)
        if st == "blocked":
            # `blocked` means the turn ended: on an open prompt (a question; never between turns), or on a last
            # message that only reads like a question to the user (status idle, no prompt), which IS between
            # turns. Treating both as a question stranded a restart-ready role at 534k for 14 min with no
            # context-restart condition (measured 2026-10-26 on a research role). Same rule as the watcher's
            # claude_session_busy.
            from backends import claude_find
            return not claude_prompt_open(claude_find(_CLAUDE_ROWS, role.session) or {}, role.session)
        return None if st is None else st != "busy"
    if role.ecosystem == "opencode":
        try:
            from backends import get as get_backend
            s = get_backend("opencode").status(role.session, repo_dir)
            return "status:idle" in s if s else None
        except Exception:
            return None
    if role.ecosystem in ("muse", "codex"):
        try:
            from backends import get as get_backend
            s = get_backend(role.ecosystem).status(role.session, repo_dir)
            return "status:idle" in s if s else None
        except Exception:
            return None
    return None


# ---- shared predicates (the quorum's evaluate() and the operator UI's read model) -----------

def watcher_dead(alive: bool, role_status: str, between: bool | None) -> bool:
    """A role's ledger-watch counts as dead when it is not running, the role is not offline, and the
    session is not known to be mid-turn (a session mid-turn arms its watcher at the turn's end).
    `between` is session_between_turns(): True between turns, False mid-turn or on a question,
    None unknown (unknown counts, as a missing watcher on an unknown session is the failure)."""
    return not alive and role_status != "offline" and between is not False


def lazy_rearm(spec: ShoalSpec) -> bool:
    """features.health.lazy_rearm (default on): a dead watcher on a role with nothing to do is not
    worth a turn; the role is woken when work for it appears (architecture doc §7)."""
    return bool(health_cfg(spec).get("lazy_rearm", True))


def watcher_dormant(spec: ShoalSpec, view, role, role_status: str) -> bool:
    """True when a dead watcher needs no wake now: lazy_rearm is on, the role is not active, and the
    ledger holds nothing its watcher would have woken it for (ledger_watch.actionable, board off: a
    self-fill role's board is the digest's or its next wake's). Its next real work raises its own
    condition (unacked-work, wait-resolved, a wake request), and that wake goes direct, re-arming
    included. Waking it only to re-arm cost a cold turn each (2026-10-20: six overnight)."""
    if not lazy_rearm(spec) or role_status == "active":
        return False
    try:
        from ledger_watch import actionable, load_sidecar, sidecar_path
        done = set(filter(None, load_sidecar(sidecar_path(spec.sid, role.slug)).get("delivered", "").split(",")))
        return not [i for i, _ in actionable(view, role.slug, role, spec, board=False) if i not in done]
    except Exception:  # noqa: BLE001 - unknown is not dormant
        return False


def parked_free(spec: ShoalSpec, view, role, q: dict, role_status: str, sub: str,
                live: bool = True) -> tuple[list[str], dict[str, str]]:
    """(next items with no current blocker, {item: why its blocker is stale}) for a role asleep on
    work: idle, or waiting on anything but its own local-ci job (which usually holds the resources
    its next items need). Empty for an observer role (fill=none) and while a freeze is on. `live`
    lets blocker checks call providers (the quorum); the UI passes False to stay off the network."""
    if not ((role_status == "idle" or (role_status == "waiting" and sub != "local-ci"))
            and getattr(role, "fill", "") != "none" and view.freeze() is None):
        return [], {}
    from ledger import item_key
    import blockers as _bl
    stale_nb = _bl.stale(spec, view, role.slug, q, live=live)  # {} unless features.blockers
    nbk = {item_key(x) for x in (q.get("next_blocked") or {})} - set(stale_nb)
    return [x for x in (q.get("next") or []) if item_key(x) not in nbk], stale_nb


# ---- the policy -----------------------------------------------------------------

def validity_applies(sub: str, target: str, pending, disciplined: bool) -> bool:
    """Whether a wait gets periodic `wait-validity` wakes: any CI wait, or any wait on a provider target, except
    `upstream` (it resolves through its target) and, under wait discipline, a target its provider confirms pending.
    That one is a valid wait: wait-resolved wakes the role when it ends, and its rescan cap still expires it. Waking it
    to re-verify cost a cold turn on a build that was running (measured 2026-10-24: az-build:1661, inProgress, woken
    at 90 minutes)."""
    if sub == "upstream" or (pending is True and disciplined):
        return False
    return sub in CI_WAITS + ("research-proto", "research-window") or target.split(":", 1)[0] in providers.kinds()


def return_condition(view, slug: str, q: dict) -> dict | None:
    """`asks-on-return` for a role holding asks that has not written since the maintainer came back. `offline off`
    wakes only the roles on the host it ran on, so a role on another host was never woken and raised nothing
    (measured 2026-11-08); its own host's quorum wakes it here, once per return."""
    um = (getattr(view, "modes", None) or {}).get("user-offline") or {}
    back = str(um.get("ts") or "")
    if um.get("state") != "off" or um.get("by") != "user" or not back or not q.get("asks"):
        return None
    age = age_min(back)
    if age is None or not RETURN_GRACE_MIN <= age < RETURN_WINDOW_MIN or view.last_by.get(slug, "") >= back:
        return None
    from worker import RETURN_PROTOCOL
    return {"slug": slug, "condition": "asks-on-return", "key": f"return:{back}", "since": back,
            "detail": f"USER-ONLINE since {back}: " + RETURN_PROTOCOL}


def unraised_condition(view, slug: str, q: dict, role_status: str, between: bool | None,
                       ecosystem: str = "claude") -> dict | None:
    """`asks-unraised`: a role holding open asks, at a quiet boundary with no question open, while the maintainer is
    online. A role that records an ask (`queue --ask`) and then sleeps on a CI wait leaves the ask showing as needs-you
    with nothing to answer when the maintainer attaches (measured 2026-11-18: fts-core's merge ask for PR 230 behind a
    remote-ci wait), and only the maintainer's next return raised it. Keyed on the session and the asks, so a new ask
    re-raises and an ask raised and left unanswered is not raised over and over. Never for a headless role (Codex,
    Muse): its turns have no question tool, and its asks are answered from the Asks view's answer set."""
    if ecosystem in HEADLESS:
        return None
    asks = q.get("asks") or []
    if not asks or role_status not in ("idle", "waiting") or between is not True or view.user_offline() is not None:
        return None
    last = view.last_by.get(slug, "") or q.get("ts") or ""
    age = age_min(last)
    if age is None or age < ASKS_RAISE_MIN:
        return None
    import hashlib
    from worker import RETURN_PROTOCOL
    fp = hashlib.sha256("\n".join(asks).encode()).hexdigest()[:10]
    return {"slug": slug, "condition": "asks-unraised", "key": f"asks:{fp}", "since": last,
            "detail": f"ASKS TO RAISE: {len(asks)} open ask(s) and no question open for them while the maintainer is "
                      f"online. Keep the wait you recorded (its watcher stays armed; the question on screen is the "
                      f"ask, so record no user-auth wait over it): " + keep_wait_protocol(RETURN_PROTOCOL)
                      + " Then go back to your wait."}


def keep_wait_protocol(protocol: str) -> str:
    """The return protocol without its user-auth wait step: a role re-raising asks behind a CI wait keeps that wait."""
    return protocol.replace("record a user-auth wait, then raise", "then raise")


def evaluate(spec: ShoalSpec, view, cbusy: dict[str, str]) -> list[dict]:
    """[{slug, condition, key, since, detail}] for every live condition instance."""
    from state import named_ended, read_wait, wait_discipline
    out = []
    cfg = health_cfg(spec)
    validity = float(cfg.get("validity_wake_min", 90))
    for role in spec.roles:
        if not role.session:  # parked or retired: no liveness conditions at all
            continue
        if not is_local(role, spec):
            # another host watches it: its sidecar, wake files and session are there, so this host
            # would read every remote role as watcher-dead and page it (multi-host, docs/limitations.md)
            continue
        slug = role.slug
        q = view.current_queue(slug) or {}
        st = view.role_state(slug) or {}
        boundary = max(q.get("ts", ""), st.get("ts", ""))
        idle = view.is_idle(slug)
        role_status = st.get("state") or ("idle" if idle else "active")
        sub = st.get("sub", "")
        between = session_between_turns(role, cbusy, spec.dir)
        # unacked work
        for request in view.unacked_to(slug):
            out.append({"slug": slug, "condition": "unacked-work", "key": f"request:{request['id']}",
                        "since": request["ts"], "detail": f"operational request {request['id']} from "
                        f"{request['by']} needs a linked acknowledgment (msg --re {request['id']})"})
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
        pending = None
        if role_status == "waiting" and sub not in USER_WAITS:
            pending, why = target_pending(target, slug, spec.sid, upstream=(sub == "upstream"))
            if pending is False:
                detail = f"wait target {target} no longer pending ({why})"
                if target.startswith("job:"):
                    import jobs as _jobs
                    sib = _jobs.running_siblings(spec.sid, slug, target[4:])
                    if sib:
                        detail += (f"; still running: {', '.join('job:' + x for x in sib)}. Act on the finished one, then "
                                   f"re-point your wait at one still running (`queue --wait local-ci --target job:{sib[0]}`)")
                out.append({"slug": slug, "condition": "wait-resolved", "key": f"target:{target}", "since": iso(),
                            "detail": detail, "first_seen": True, "between": between})
            # a research wait on a guessed `time:` that names the reservations it really waits on: resolved when they
            # end, though the time is still ahead (measured 2026-11-26: four runs read such a wait ok while the GPUs it
            # waited on stood free)
            ended, ewhy = named_ended(sub, target, st.get("holder") or w.get("holder") or "")
            if ended:
                out.append({"slug": slug, "condition": "wait-resolved", "key": f"resv:{','.join(ended)}",
                            "since": iso(), "first_seen": True, "between": between,
                            "detail": f"the reservation(s) your {sub} wait names ended ({ewhy}), ahead of {target}: "
                                      f"what you waited for may be free. Read `shoal pool status`, then reserve it, "
                                      f"or queue your own request behind the one now ahead and wait on "
                                      f"`--target resv:<its id>`"})
            # `upstream` waits resolve through their target (wait-resolved), not a periodic re-verify.
            # The ladder is keyed by the target, so re-declaring the same wait keeps escalating: keyed
            # by the boundary, every rewrite restarted it at step 1 and it never paged (measured
            # 2026-09-26: five wakes in a day on a wait whose PR had merged, no page).
            if validity_applies(sub, target, pending, wait_discipline(spec) is not None):
                a = age_min(boundary)
                if validity > 0 and a is not None and a >= validity:
                    seen = "" if not target else f": UNVERIFIABLE, {why}" if pending is None else f": {why}"
                    out.append({"slug": slug, "condition": "wait-validity",
                                "key": f"validity:{target}" if target else f"validity:{boundary}",
                                "since": boundary, "detail": f"waiting {a:.0f}m on {sub} ({target or 'no target'}{seen}); "
                                f"re-verify the wait is still valid", "between": between})
        # watcher
        # a detached (--wake direct) watcher outlives its session's worker and resumes the session when
        # work arrives: a retired worker is dormant, and waking it only to re-arm cost a cold turn
        # (2026-10-20: six in one night)
        alive, sc = watcher_alive(spec.sid, slug)
        dead = watcher_dead(alive, role_status, between)
        quiet = dormant = dead and watcher_dormant(spec, view, role, role_status)
        if dormant and role.session and health_cfg(spec).get("rearm", True):
            # a detached watcher costs no turn to re-arm, so a dormant role gets its watcher back too: without one, every
            # wake to it is a stop and resume (2026-11-25: a failed restart ended its watcher, and nothing re-armed it)
            from briefs import arming
            dormant = not is_detached_wake(arming(role, spec)[0])
        if dead and not dormant:
            since = sc.get("armed_utc") or boundary or iso()
            ex = sc.get("exit", "")
            if ex:
                since = max(since, ex.split(" ")[-1])
            out.append({"slug": slug, "condition": "watcher-dead", "key": f"pid:{sc.get('pid', 'none')}", "since": since,
                        "first_seen": True, "rearm_only": quiet,
                        "detail": f"ledger-watch not running ({'designed exit, no re-arm: ' + ex if ex else 'no exit verdict: killed or never armed'})"})
        from ledger_watch import current_hold
        hold = current_hold(sc, alive)
        if hold.get("attached"):
            # the watcher holds every new event for a person's client, by design: say so, once per hold, with what to exit
            held_min = age_min(hold["since"]) or 0.0
            out.append({"slug": slug, "condition": "wakes-held", "key": f"{hold['reason']}:{hold['since']}",
                        "since": hold["since"],
                        "detail": f"an interactive client ({hold['client']}) has held {hold['events']} wake(s) for "
                                  f"{held_min:.0f} min; detach it (exit the client) so the watcher can deliver"})
        # wait discipline: expired rescans, and peers blocked on unscheduled items
        if (spec.features or {}).get("waitdiscipline"):
            ru = st.get("rescan_utc", "")
            from state import wait_proven
            # shoal re-proves the wait itself; the model is woken only when that fails (or once a day)
            if role_status == "waiting" and ru and parse(ru) is not None and parse(ru) < utcnow() and \
                    not wait_proven(spec, view, slug, st, q, pending if st.get("target") == target else None)[0]:
                ask = " Re-raise your prompted ask (question tool / AskUserQuestion) or pivot to the next unblocked item." \
                    if sub in USER_WAITS else " Re-verify the blocker (is it still pending?), then write a fresh wait or pivot."
                out.append({"slug": slug, "condition": "wait-expired", "key": f"rescan:{ru}", "since": ru,
                            "detail": f"{sub} wait on '{str(st.get('holder', ''))[:80]}' passed its rescan {ru} unproven.{ask}"})
            from ledger import item_key
            planned = {item_key(x) for x in [q.get("active") or ""] + list(q.get("next") or []) if x}
            import blockers as _blk
            for b in view.blocked_by(slug):
                item = (b["item"] or "").lower()
                # a wait on an item this role cannot take is the waiter's to fix (parked-work wakes it);
                # waking this role about it cannot help (measured 2026-10-08: three wakes in a minute)
                if item and _blk.takeable(spec, view, f"{slug}:{item}"):
                    continue
                if item and item_key(item) not in planned:
                    out.append({"slug": slug, "condition": "blocking-unscheduled", "key": f"block:{b['role']}:{item}",
                                "since": b["since"],
                                "detail": f"{b['role']} has waited on your {b['item']} since {b['since']}, and your queue "
                                          f"does not schedule it: schedule it (active/next) or pass it with a reason"})
        # parked work: asleep (idle or waiting) while a next item has no recorded blocker. Idle is
        # never woken by anything else, so without this a role can sit on a full queue forever
        # (measured 2026-09-27: one role idle with three next items for two hours; another
        # waiting upstream for #44 while #372 and #457 sat behind it unrecorded). Keyed by the items,
        # so re-declaring the same parked state keeps escalating.
        # a local-ci wait is exempt: the role's own machine-bound job (under a lease) usually holds the
        # resources its next items need; remote-ci, upstream, peer and maintainer waits leave it free
        # a freeze parks everything by design, so it is exempt while on
        free, stale_nb = parked_free(spec, view, role, q, role_status, sub)
        if free:
            why_stale = "".join(f"; {k} blocker stale ({stale_nb[k]})" for k in sorted(stale_nb))
            out.append({"slug": slug, "condition": "parked-work", "key": f"parked:{','.join(sorted(free))}",
                        "since": boundary or iso(), "first_seen": True,
                        "detail": f"{role_status} while next items have no current blocker ({', '.join(free)}"
                                  f"{why_stale}): "
                                  f"pop one (`queue --active <item>`) or record why each cannot start "
                                  f"(`queue --blocked ITEM=<role>:<repo#n>|ask:..|upstream:<target>|lease:<key>|"
                                  f"<its own run: job:<name>|gh-run:<owner/repo>:<id>|az-build:<id>|time:<ISO UTC>>|"
                                  f"hold:<why>`); a busy lease is a wait on `lease:<key>`, never idle"})
            # a blocker that cleared is news, unlike the rest of parked-work: nothing else wakes the role, and without
            # this a wake_off spec reaches the maintainer 3 h later for a look that needs only the role. The wake says to
            # look, not to start: cleared is not a go (measured 2026-11-06: a peer finished the blocking item, its mail saying
            # the maintainer had deferred the next one did not wake the role, and the role slept on behind a stale blocker)
            import blockers as _bl
            from ledger import item_key
            meta = q.get("next_blocked_meta") or {}
            news = _bl.cleared(stale_nb, q)
            for x in free:
                k = item_key(x)
                if k in news:
                    b = (q.get("next_blocked") or {}).get(k, "")
                    out.append({"slug": slug, "condition": "blocker-resolved", "first_seen": True,
                                "key": f"cleared:{k}:{b}:{(meta.get(k) or {}).get('since', '')}",
                                "since": boundary or iso(),
                                "detail": f"{x}: its blocker {b[:120]} cleared ({news[k][:160]}). That is not by itself "
                                          f"a go: read your mail and the item's latest notes, then take it "
                                          f"(`queue --active {x}`) or record what holds it now"})
        # long holds (features.blockers): a hold re-recorded past hold_page_min is a question for the
        # maintainer, raised once per hold; deferred while offline like every page
        import blockers as _bl
        for k, b, first, age, n in _bl.long_holds(spec, q):
            out.append({"slug": slug, "condition": "hold-long", "key": f"hold:{k}:{first}", "since": first, "fp": f"hold:{k}",
                        "first_seen": True,  # the ladder runs from this sighting: wake now, page 240 min later
                        "detail": f"{k} held since {first} ({age / 60:.1f} h, re-recorded {n}x): {b[:120]}. "
                                  f"A hold this old is a question: record it as an ask (`queue --ask`) or name "
                                  f"a checkable blocker (`<role>:<repo#n>`, `upstream:<target>`); a hold still "
                                  f"standing 4 h after this wake pages the maintainer"})
        rc = return_condition(view, slug, q)
        if rc is not None:
            out.append(rc)
        else:
            ur = unraised_condition(view, slug, q, role_status, between, role.ecosystem)
            if ur is not None:
                out.append(ur)
        # bounded context: past its limit at a quiet boundary. In `page` mode the role marks that boundary itself
        # (restart-ready); in `auto` any quiet boundary counts, with no advise-then-refresh round trip (maintainer,
        # 2026-11-11): a role keeps its start doc current at every boundary, so it is handed over on the doc as it stands,
        # once that doc meets its contract (else it is woken to fix it: no blind handover)
        if role_status in ("idle", "waiting") and between:
            import context as _ctx
            auto = _ctx.mode(spec, slug) == "auto"
            o = _ctx.over(spec, role) if (q.get("restart_ready") or auto) else None
            doc = None
            if o is not None and auto and not q.get("restart_ready"):
                import startdoc as _sd
                doc = _sd.check(role.doc) if role.doc else {"ok": False, "problems": ["no start doc in the spec"]}
            if o is not None and doc is not None and not doc["ok"]:
                out.append({"slug": slug, "condition": "context-doc", "key": f"session:{role.session}",
                            "since": q.get("ts") or iso(), "first_seen": True,
                            "detail": f"context {o[0]}k past its {o[1]}k limit, but the start doc fails its contract "
                                      f"({'; '.join(doc['problems'])[:160]}): fix {role.doc or 'it'} (`shoal doc-check`), "
                                      f"then end your turn; the handover follows at your next quiet boundary"})
            elif o is not None:
                out.append({"slug": slug, "condition": "context-restart", "key": f"session:{role.session}",
                            "since": q.get("ts") or iso(), "first_seen": True, "over": list(o),
                            "detail": f"context {o[0]}k past its {o[1]}k limit at a quiet boundary (nothing mid-flight): "
                                      f"`shoal restart --spec <spec> --only {slug}` hands it to a fresh session"})
            else:
                qh = quota_condition(spec, role, q, auto)
                if qh is not None:
                    out.append(qh)
        # continuation: a turn that ended while the status says active (architecture doc §3.2)
        cont = continuation_cfg(spec)
        if cont is not None and role_status == "active" and between:
            last = view.last_by.get(slug, "")
            act = q.get("active") or "(no active item)"
            out.append({"slug": slug, "condition": "turn-ended-active", "key": f"stop:{last}", "since": iso(),
                        "first_seen": True,
                        "detail": f"CONTINUE {act}: your turn ended with status active and nothing running. Continue "
                                  f"the item now; if it cannot continue, write your real sleep status: `queue --wait "
                                  f"<sub> --holder .. --target ..` after arming the waiter (GPU work: a job unit under "
                                  f"the lease), `--active <next item>` to pivot, or `--idle`"})
            continue_done = True
        else:
            continue_done = False
        # stale active (the crash catcher; superseded by continuation when that is on)
        if role_status == "active" and between and not continue_done:
            last = view.last_by.get(slug, "")
            out.append({"slug": slug, "condition": "stale-active", "key": f"last:{last}", "since": last,
                        "detail": f"status active, last ledger write {last}, session between turns"})
    return out


def rearm_watcher(spec: ShoalSpec, role, state: dict, dry_run: bool = False) -> str:
    """Re-arm a dead DETACHED ledger-watch from the quorum, with no model turn: Claude `--wake direct`,
    opencode `send:` and muse/codex `exec:` watchers run setsid-detached, so shoal can start one as well as
    the role can (features.health.rearm, default on). Returns what it did, or "" when the role's watcher is
    not detached (an in-session `exit` watcher needs its role woken) or was re-armed within
    REARM_RETRY_MIN (a watcher that will not stay up is a wake, then a page)."""
    if not health_cfg(spec).get("rearm", True) or role is None or not role.session:
        return ""
    from briefs import arming
    wake = arming(role, spec)[0]
    rearmed = state.setdefault("rearmed", {})
    if not is_detached_wake(wake) or (age_min(rearmed.get(role.slug, "")) or 1e9) < REARM_RETRY_MIN:
        return ""
    wake = wake.replace("<your-session-id>", role.session)
    argv = shlex.split(config.cli()) + ["ledger-watch", "--shoal", spec.sid, "--slug", role.slug, "--wake", wake,
                                        "--session", role.session, "--interval", "120"]
    if dry_run:
        return f"re-arm by the quorum ({wake.split(':')[0]}; no wake) (dry-run)"
    try:
        from backends import spawn_detached
        os.makedirs(state_dir(spec.sid), exist_ok=True)
        log = open(os.path.join(state_dir(spec.sid), f"ledger-watch-{role.slug}.log"), "a")
        # its own scope: the quorum is a oneshot unit, and KillMode=control-group ends every process left
        # in its cgroup when it exits (measured 2026-10-20: a re-armed watcher died before its first tick)
        spawn_detached(argv, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                       cwd=spec.dir or None, env=_ENV)
    except OSError as exc:
        return f"FAILED re-arm ({exc})"
    rearmed[role.slug] = iso()
    return f"re-armed by the quorum ({wake.split(':')[0]}; no wake)"


def is_local_any(spec: ShoalSpec) -> bool:
    """True when at least one role of the shoal runs on this host (its transcripts are here)."""
    return any(is_local(r, spec) for r in spec.roles)


def claude_installed_version() -> str:
    """The installed Claude Code version (`claude --version`), '' when unknown."""
    from backends import fake_sessions, tool_which
    if fake_sessions() is not None:
        return config.env("FAKE_CLAUDE_VERSION")
    b = tool_which("claude")
    if not b:
        return ""
    try:
        out = subprocess.run([b, "--version"], capture_output=True, text=True, timeout=20, env=_ENV).stdout
    except (OSError, subprocess.SubprocessError):
        return ""
    m = re.search(r"\d+\.\d+\.\d+", out)
    return m.group(0) if m else ""


def claude_roster() -> dict:
    """{job id: {pid, cliVersion, ...}} from the background service's roster: the version each worker RUNS (a job's
    state.json `cliVersion` updates only at its next turn). SHOAL_FAKE_ROSTER (tests)."""
    raw = config.env("FAKE_ROSTER")
    if raw:
        return json.loads(raw)
    from backends import claude_bases
    out: dict = {}
    for b in claude_bases():  # each account runs its own background service; job ids are unique across them
        try:
            d = json.load(open(os.path.join(b, "daemon", "roster.json")))
        except (OSError, ValueError):
            continue
        # the service nests its workers under `workers` ({proto, supervisorPid, updatedAt, workers: {job: ...}}, seen
        # 2026-11-15; reading the top level found no job, so stale-worker upkeep matched nothing); a flat map still reads
        w = d.get("workers") if isinstance(d, dict) and isinstance(d.get("workers"), dict) else d
        if isinstance(w, dict):
            for k, v in w.items():
                if isinstance(v, dict):
                    out.setdefault(k, v)
    return out


def stale_worker_upkeep(spec: ShoalSpec, acts: list, dry_run: bool = False, state: dict | None = None) -> None:
    """Respawn this host's idle Claude role workers that run an older Claude Code than the installed one (no turn:
    `claude respawn` brings a new worker on the same conversation, idle at its prompt). An old worker's own updater
    keeps reinstalling the current version, and every reinstall restarts the background service and drops every
    attached client (measured 2026-10-22: 124 restarts in a day, driven by three workers on 2.1.292 and 2.1.304). A
    busy stale worker is reported, not touched (features.health.stale_upkeep, default on)."""
    if not health_cfg(spec).get("stale_upkeep", True):
        return
    want = claude_installed_version()
    roster = claude_roster()
    if not want or not roster:
        return
    from backends import claude_find, fake_sessions, get as get_backend
    be = get_backend("claude")
    rows = list((fake_sessions() or {}).get("claude") or []) if fake_sessions() is not None else be._agents(True)
    for role in spec.roles:
        if role.ecosystem != "claude" or not role.session or not is_local(role, spec):
            continue
        e = claude_find(rows, role.session)
        job = str((e or {}).get("id") or "")
        have = str((roster.get(job) or {}).get("cliVersion") or "")
        if not job or not have or have == want or not (e or {}).get("pid"):
            continue
        st = claude_job_state(e)
        fl = st.get("inFlight") or {}
        idle = claude_liveness(e) == "idle" and not (isinstance(fl, dict) and (fl.get("tasks") or fl.get("kinds")))
        if not idle:
            seen = (state if state is not None else {}).setdefault("stale_reported", {})
            if seen.get(job) != have:  # once per worker and version, not every run
                seen[job] = have
                acts.append((role.slug, "stale worker", f"runs Claude {have}, installed {want}; busy, so left as it "
                                                        f"is (relaunch it at a quiet moment)"))
            continue
        acts.append((role.slug, "stale worker", f"runs Claude {have}, installed {want}: "
                                                + be.respawn(job, dry_run) + " (no turn)"))


def quota_condition(spec: ShoalSpec, role, q: dict, auto: bool) -> dict | None:
    """`[accounts] quota_handoff`: the quota-handoff condition for a local role at a quiet boundary, or None. The role is
    handed over as a context restart is: in `auto` context mode at any quiet boundary once its start doc meets its
    contract, else only when it marked itself restart-ready. The target is `accounts.quota_pick` over the balancing pool
    (its own provider only when `[accounts] pin` names it or the role carries a provider pin: accounts.pin_of), never a
    provider this host will not run or a Claude account that has not trusted the shoal's folder."""
    import accounts
    if role is None or not role.session or not accounts.quota_handoff_on() or not is_local(role, spec):
        return None
    if not (q.get("restart_ready") or auto):
        return None
    try:
        pick = balance_pick(spec, role, bool(accounts.pin_of(role)))
    except Exception:  # noqa: BLE001 - no usage cache or specs: the role stays
        return None
    if pick is None:
        return None
    p, acct, why, kind = pick
    import park
    new_eco = p if p != role.ecosystem else ""
    if new_eco and park._ecosystem_refusal(new_eco):
        return None
    if p == "claude":
        from backends import claude_untrusted
        if claude_untrusted(acct, spec.dir):
            return None
    if not q.get("restart_ready") and role.doc:
        import startdoc as _sd
        if not _sd.check(role.doc)["ok"]:
            return None  # no blind handover: the role's own boundary (context-doc) gets its doc fixed first
    extra = park.move_args(role, new_eco, acct)[0]
    return {"slug": role.slug, "condition": "quota-handoff", "key": f"session:{role.session}:{p}/{acct}",
            "since": q.get("ts") or iso(), "first_seen": True, "extra": extra, "kind": kind,
            "from": f"{role.ecosystem}/{role.account or ''}",
            "detail": f"{accounts.name(role.ecosystem, role.account)} -> {accounts.name(p, acct)}: {why}"}


# This run's balancing inputs (`balance_observe`, once per run before the conditions): the load every pick of the run
# reads, and the health state the persistence, cooldown and return records live in. Empty outside a run: a pick then
# skips the persistence check (tests call the conditions directly).
_BALANCE: dict = {}


def balance_observe(state: dict) -> str:
    """Once per run, with `[accounts] quota_handoff` or `rebalance` on: read the load (accounts.balance_load: spend per
    account) for every pick this run, and record since when each account has been over its share by the margin
    (state["balance_over"]), which a share move needs for `share_hold_min`. Says what it found."""
    _BALANCE.clear()
    if not (accounts.quota_handoff_on() or accounts.rebalance_on()):
        state.pop("balance_over", None)
        return ""
    load, counts, per, unit = accounts.balance_load()
    prev = state.get("balance_over") or {}
    over = {f"{p}/{a}": prev.get(f"{p}/{a}") or iso() for p, a in accounts.over_accounts(load)}
    state["balance_over"] = over
    _BALANCE.update(load=load, counts=counts, per=per, unit=unit, state=state)
    return (f"{', '.join(f'{k} since {v}' for k, v in sorted(over.items()))} over its share" if over else "")


def balance_pick(spec: ShoalSpec, role, pinned: bool) -> tuple[str, str, str, str] | None:
    """(provider, account, why, kind) from accounts.balance_pick for this role, kind "limit" (off an account near a
    limit) or "share"; None to stay. The same pick for the quota handoff and the rebalanced context restart."""
    if _BALANCE.get("off"):
        return None
    if _BALANCE:
        load, counts, per, unit, st = (_BALANCE[k] for k in ("load", "counts", "per", "unit", "state"))
        since = (st.get("balance_over") or {}).get(f"{role.ecosystem}/{role.account or ''}")
        over_for = (age_min(since) or 0.0) if since else 0.0
        lr = (st.get("quota_left") or {}).get(role.slug) or {}
        left = tuple(lr["from"].split("/", 1)) if lr.get("from") and \
            (age_min(lr.get("at", "")) or 1e9) < accounts.RETURN_H * 60 else None
    else:
        load, counts, per, unit = accounts.balance_load()
        over_for, left = None, None
    pick = accounts.balance_pick(role.ecosystem, role.account, load, pinned, slug=role.slug,
                                 weight=per.get((spec.sid, role.slug), 1.0 if unit == "roles" else 0.0),
                                 counts=counts, unit=unit, left=left, over_for_min=over_for)
    if pick is None:
        return None
    return (*pick, "limit" if " is near its limit (" in pick[2] else "share")


def balance_hold(kind: str, slug: str, state: dict, quota_done: bool) -> str:
    """Why a balancing move of `kind` is held this run ("" when it is not): one quota move per quorum run; a share move
    also waits out the role's `quota_cooldown_min` since its last move and this host's settle (accounts.SETTLE_MIN since
    any shoal's share move here), so the specs and readings catch up. A move off an account near its limit waits for
    neither."""
    if quota_done:
        return "another role moved this run"
    if kind != "share":
        return ""
    since = age_min((state.get("quota_moved") or {}).get(slug, ""))
    if since is not None and since < accounts.quota_cooldown_min():
        return f"moved {since:.0f} min ago (cooldown {accounts.quota_cooldown_min():g})"
    settle = age_min(accounts.last_share_move())
    if settle is not None and settle < accounts.SETTLE_MIN:
        return f"a role on this host moved for share {settle:.0f} min ago (settle {accounts.SETTLE_MIN:g})"
    return ""


def balance_moved(state: dict, slug: str, kind: str, src: str, what: str) -> None:
    """Record a balancing move: the role's cooldown and the account it left (for the return margin), and for a share
    move this host's settle."""
    state.setdefault("quota_moved", {})[slug] = iso()
    state.setdefault("quota_left", {})[slug] = {"from": src, "at": iso()}
    if kind == "share":
        accounts.note_share_move(iso(), what)


def rebalance(spec: ShoalSpec, role) -> tuple[str, ...]:
    """`[accounts] rebalance`: (the restart argument, what it does, kind, the account left) to start this role's
    automatic restart on the account `balance_pick` moves it to within its provider (the quota handoff's pick: one
    rule, so neither undoes the other), or () to keep its account: the setting off, another host's role, no move due, a
    share move held (`balance_hold`: this host's settle; the restart itself is no extra session, so the role's cooldown
    does not hold it), or a Claude account that does not trust the shoal's folder (the restart would stop the old
    session, then fail to start the new one)."""
    if role is None or not accounts.rebalance_on() or not is_local(role, spec):
        return ()
    try:
        pick = balance_pick(spec, role, True)
    except Exception:  # noqa: BLE001 - no usage cache or settings: the account stays
        return ()
    if pick is None or pick[0] != role.ecosystem or pick[1] == (role.account or ""):
        return ()
    _p, acct, why, kind = pick
    if balance_hold(kind, "", {}, False):
        return ()
    if role.ecosystem == "claude":
        from backends import claude_untrusted
        if claude_untrusted(acct, spec.dir):
            return ()
    return (f"--set-account={role.slug}:{acct or role.ecosystem}",
            f"account {accounts.name(role.ecosystem, role.account)} -> {accounts.name(role.ecosystem, acct)} "
            f"([accounts] rebalance: {why})", kind, f"{role.ecosystem}/{role.account or ''}")


# A restart that fails stays due: the role keeps a record of it (restart_record) and the quorum tries again after a
# backoff that grows with each failure in a row, paging once after RESTART_FAIL_PAGE of them with the error. Measured
# 2026-11-25: a context restart failed on a `claude` binary an update had removed, nothing tried again, and the role
# reached twice its limit an hour later; a handoff stopped its role's session, then failed on the same, and left the role
# with no session at all.
RESTART_FAIL_PAGE = 3
RESTART_BACKOFF_MIN = (5, 10, 20, 30)  # after the 1st, 2nd, 3rd, 4th and later failures in a row
ALREADY_RUNNING = "a restart of "  # the lock's refusal: another restart is under way, not a failure


def restart_record_path(sid: str, slug: str) -> str:
    return os.path.join(state_dir(sid), f"context-restart-{slug}.json")


def restart_record(sid: str, slug: str) -> dict:
    """The role's last restart attempt ({} when none is on record): {session (the one it replaces), state (launched |
    failed), at, extra (its restart arguments), fails (in a row), first (the first of them), error, retry_at, log,
    log_off, paged}."""
    try:
        d = json.load(open(restart_record_path(sid, slug)))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def save_restart_record(sid: str, slug: str, rec: dict | None) -> None:
    p = restart_record_path(sid, slug)
    if not rec:
        try:
            os.remove(p)
        except OSError:
            pass
        return
    os.makedirs(state_dir(sid), exist_ok=True)
    with open(p + ".tmp", "w") as fh:
        json.dump(rec, fh, indent=1)
    os.replace(p + ".tmp", p)


def restart_backoff_min(fails: int) -> float:
    return float(RESTART_BACKOFF_MIN[min(max(fails, 1), len(RESTART_BACKOFF_MIN)) - 1])


def note_restart_failure(spec: ShoalSpec, role, error: str, extra: tuple[str, ...] = ()) -> dict:
    """Count one failed restart of `role` in its record (a new count when the record was another session's) and set when
    the quorum may try again. Returns the record."""
    rec = restart_record(spec.sid, role.slug)
    if rec.get("session") != role.session:
        rec = {}
    fails = int(rec.get("fails") or 0) + 1
    rec.update(session=role.session, state="failed", at=iso(), fails=fails, first=rec.get("first") or iso(),
               error=short_line(error, 240), extra=list(extra or rec.get("extra") or []),
               retry_at=iso(utcnow() + datetime.timedelta(minutes=restart_backoff_min(fails))))
    save_restart_record(spec.sid, role.slug, rec)
    return rec


def restart_lock_held(sid: str, slug: str) -> bool:
    """True while a restart of the role runs (it holds the role's restart lock)."""
    p = os.path.join(state_dir(sid), f"context-restart-{slug}.lock")
    if not os.path.exists(p):
        return False
    with open(p, "a") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return True
        fcntl.flock(fh, fcntl.LOCK_UN)
    return False


def log_failure(path: str, offset: int) -> str:
    """The line that says why a detached restart failed, from its log after `offset`: its last FAIL line, else its last."""
    try:
        with open(path, errors="replace") as fh:
            fh.seek(max(0, int(offset or 0)))
            lines = [x.strip() for x in fh.read().splitlines() if x.strip()]
    except (OSError, ValueError):
        return "its log is unreadable"
    fail = [x for x in lines if "FAIL" in x or "Error" in x or "REFUSED" in x]
    return (fail or lines or ["it ended with nothing in its log"])[-1]


def restart_harnesses(role, extra: tuple[str, ...]) -> list[str]:
    """The harness CLIs a restart of `role` runs: its own provider's (Claude's stop lists and stops jobs) and the new
    one's (`--set-ecosystem`)."""
    from doctor import ECOSYSTEM_TOOLS
    new = next((x.split(":", 1)[1] for x in extra if x.startswith(f"--set-ecosystem={role.slug}:")), "")
    tools = [ECOSYSTEM_TOOLS.get(role.ecosystem, "")] if role.ecosystem == "claude" else []
    return [t for t in dict.fromkeys(tools + [ECOSYSTEM_TOOLS.get(new or role.ecosystem, "")]) if t]


def context_restart(spec: ShoalSpec, spec_path: str, role, dry_run: bool = False, extra: tuple[str, ...] = ()) -> str:
    """features.health.context_restart `auto`: hand a restart-ready role to a fresh session. The old Claude
    session is stopped first and its detached watcher ended (neither wakes it), so nothing delivers to it
    afterwards; then `restart --only` launches the new one, detached from this run (it verifies the new
    loop for minutes, and the quorum holds a lock). `extra`: more restart arguments (`shoal handoff`'s model and
    provider). Every harness CLI it needs must resolve before anything stops (waiting out an update, bounded), and a
    failure at any step is recorded (`note_restart_failure`) so the quorum tries again: a role it stopped and could
    not relaunch is relaunched from its start doc at the next try."""
    if role is None or not spec_path:
        return "FAILED (no role or spec path)"
    argv = shlex.split(config.cli()) + ["restart", "--spec", spec_path, "--only", role.slug, *extra]
    if dry_run:
        return "would restart (dry-run): " + " ".join(argv[-(4 + len(extra)):])
    log = os.path.join(state_dir(spec.sid), f"context-restart-{role.slug}.log")
    guard = None
    ended = ""  # the detached watcher this ended, which a failure re-arms
    stopped = ""
    try:
        os.makedirs(state_dir(spec.sid), exist_ok=True)
        # one restart of a role at a time: the spec records the new session only when it ends, so a double click, or the
        # quorum and `handoff` together, would launch two. The detached restart inherits this descriptor, so the lock
        # lasts as long as it does and goes when it exits, however it exits.
        guard = open(os.path.join(state_dir(spec.sid), f"context-restart-{role.slug}.lock"), "a")
        try:
            fcntl.flock(guard, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return f"FAILED ({ALREADY_RUNNING}{role.slug} is already running; its log: {log})"
        import backends
        for tool in restart_harnesses(role, extra):
            backends.harness_exe(tool)  # before anything stops: a CLI mid-update is waited for, a missing one refuses
        alive, sc = watcher_alive(spec.sid, role.slug)
        if alive and is_detached_wake(str(sc.get("wake", ""))):
            os.kill(int(sc["pid"]), 15)  # a detached watcher: ending it wakes nobody
            ended = "detached"
        if role.ecosystem == "claude" and role.session:
            # every job on the conversation, watched so a respawned worker is stopped again: a copy left behind would hold
            # the role beside the new session
            import park
            stopped = "a stop was under way when it failed"  # a stop that raises may have stopped the jobs already
            try:
                stopped = park.stop_session(role)
            except OSError:
                # past this point the old session may be stopped already, so leaving now could leave the role with
                # none (measured 2026-11-25: a handoff's stop went through, then its check of the stop failed on a
                # `claude` an update had removed). Once more, after the CLI is back (bounded): a stop is idempotent
                for tool in restart_harnesses(role, extra):
                    backends.harness_exe(tool)
                stopped = park.stop_session(role)
            if "STILL LIVE" in stopped:
                # the watcher ended above would leave a live session with nobody to wake it (measured 2026-11-18:
                # it-architect, re-armed by hand): re-arm it now, as the quorum does a dead one
                rearmed = rearm_watcher(spec, role, {}, dry_run) if ended else ""
                __import__("context").note_restart(spec, role, f"FAILED ({stopped})")
                note_restart_failure(spec, role, stopped, extra)
                return f"FAILED ({stopped}; nothing restarted" + (f"; watcher: {rearmed}" if rearmed else "") + ")"
        offset = os.path.getsize(log) if os.path.exists(log) else 0
        from backends import spawn_detached
        # its own scope, like a re-armed watcher: the quorum's oneshot unit would end it with its cgroup
        spawn_detached(argv, stdin=subprocess.DEVNULL, stdout=open(log, "a"), stderr=subprocess.STDOUT,
                       cwd=spec.dir or None, env=_ENV, pass_fds=(guard.fileno(),))
        out = f"restarting on a fresh session (log {log})"
        rec = restart_record(spec.sid, role.slug)
        if rec.get("session") != role.session:
            rec = {}
        rec.update(session=role.session, state="launched", at=iso(), extra=list(extra), log=log, log_off=offset)
        save_restart_record(spec.sid, role.slug, rec)
    except (OSError, ValueError) as exc:
        out = f"FAILED ({exc})"
        rec = note_restart_failure(spec, role, str(exc), extra)
        if stopped:
            # the old session is stopped (or may be): the role has no session until a restart takes. Say so where the
            # caller reads it; the quorum relaunches it from its start doc at the next try
            out += (f"; its old session was stopped first ({short_line(stopped, 60)}), so it may have none running: the quorum "
                    f"relaunches it from its start doc from {rec['retry_at'][11:16]}Z, or run `shoal restart --spec "
                    f"<spec> --only {role.slug}`")
        elif ended:
            rearmed = rearm_watcher(spec, role, {}, dry_run)
            if rearmed:
                out += f"; watcher: {rearmed}"
    finally:
        if guard is not None:
            guard.close()  # this process's copy: the restart's keeps the lock
    __import__("context").note_restart(spec, role, out)
    return out


def restart_gate(spec: ShoalSpec, role) -> str:
    """Why a restart of `role` must wait this run ("" when it may go): one launched and still running, or one that failed
    and waits out its backoff (restart_record)."""
    if role is None:
        return ""
    rec = restart_record(spec.sid, role.slug)
    if not rec or rec.get("session") != role.session:
        return ""
    if rec.get("state") == "launched" and restart_lock_held(spec.sid, role.slug):
        return f"its restart launched at {rec.get('at', '?')} is still running"
    if rec.get("state") == "failed":
        when = parse(rec.get("retry_at", ""))
        if when is not None and when > utcnow():
            return (f"its restart failed {rec.get('fails', 1)}x in a row (last: {short_line(rec.get('error', ''), 120)}); "
                    f"next try from {rec['retry_at'][11:16]}Z")
    return ""


def restart_failed(spec: ShoalSpec, role, ch: str, c: dict, n_before: int, state: dict, dry_run: bool = False) -> str:
    """What a failed restart adds to its action line: the count in a row, the next try, and the page once the count
    reaches RESTART_FAIL_PAGE. A stand-in restart that wrote no record gets one here."""
    if ALREADY_RUNNING in ch:
        return ""  # another restart holds the lock: not a failure of this one
    rec = restart_record(spec.sid, role.slug)
    if rec.get("session") != role.session or int(rec.get("fails") or 0) <= n_before or rec.get("state") != "failed":
        rec = note_restart_failure(spec, role, ch[len("FAILED"):].strip(" ()") or ch, tuple(c.get("extra") or ()))
    return f" (failure {rec['fails']} in a row; next try from {rec['retry_at'][11:16]}Z)" + \
        restart_page(spec, role, rec, c.get("detail", ""), state, dry_run)


def restart_page(spec: ShoalSpec, role, rec: dict, detail: str, state: dict, dry_run: bool = False) -> str:
    """The page once a role's restart has failed RESTART_FAIL_PAGE times in a row ("" before that, and after it was sent)."""
    n = int(rec.get("fails") or 0)
    if n < RESTART_FAIL_PAGE or rec.get("paged"):
        return ""
    text = (f"{role.slug}: its restart has failed {n} times in a row since {rec.get('first', '?')}; last error: "
            f"{rec.get('error', '?')}. The quorum keeps trying every {restart_backoff_min(n):g} min"
            + (f" ({detail})" if detail else "") + f". Restart log: {rec.get('log') or 'none (it failed before launch)'}")
    out = "; " + page_user(spec, text, state, dry_run)
    if not dry_run:
        rec["paged"] = iso()
        save_restart_record(spec.sid, role.slug, rec)
    return out


def restart_retries(spec: ShoalSpec, spec_path: str, conds: list[dict], cbusy: dict[str, str], state: dict, acts: list,
                    dry_run: bool = False) -> list[dict]:
    """Follow every local role's last restart (restart_record) and return the `restart-retry` conditions due this run.
    A record whose session the spec no longer names is done (the restart took, or the role was parked or moved on). A
    launched restart that ended with the spec still naming the old session failed: counted, with the line its log ends
    on. A failed one is retried by the role's own restarting condition when one is raised; else, for a Claude role whose
    session is not running (a stop that went through, or a listing that failed), by `restart-retry`, so a role the
    failure left with no session is relaunched from its start doc. A session listed as running is left to its own
    conditions: it is not cut off mid-turn."""
    out = []
    fresh = None
    for role in spec.roles:
        if not is_local(role, spec):
            continue
        rec = restart_record(spec.sid, role.slug)
        if not rec:
            continue
        if rec.get("state") == "launched" and rec.get("session") == role.session and \
                not restart_lock_held(spec.sid, role.slug):
            # the restart may have recorded its session after this run loaded the spec: read it again before judging
            if fresh is None:
                try:
                    fresh = ShoalSpec.load(spec_path) if spec_path else spec
                except (OSError, ValueError):
                    fresh = spec
            now = fresh.role(role.slug)
            if now is None or now.session != rec.get("session"):
                role = now or role
            elif not dry_run:
                rec = note_restart_failure(spec, role, "the detached restart ended without recording a new session: "
                                           + log_failure(rec.get("log", ""), rec.get("log_off", 0)),
                                           tuple(rec.get("extra") or ()))
                acts.append((role.slug, "restart", f"FAILED: {rec['error']} (failure {rec['fails']} in a row; next try "
                                                   f"from {rec['retry_at'][11:16]}Z)"
                             + restart_page(spec, role, rec, "", state, dry_run)))
        if not role.session or role.session != rec.get("session"):
            if not dry_run:
                if int(rec.get("fails") or 0) and role.session:
                    acts.append((role.slug, "restart", f"recovered: session {role.session[:8]} replaced "
                                                       f"{str(rec.get('session', ''))[:8]} after {rec['fails']} failed "
                                                       f"attempt(s)"))
                save_restart_record(spec.sid, role.slug, None)
            continue
        if rec.get("state") != "failed":
            continue
        if any(c["slug"] == role.slug and c["condition"] in RESTARTING for c in conds):
            continue
        if role.ecosystem != "claude" or any(k.startswith(role.session[:8]) for k in cbusy):
            continue  # its session runs: its own conditions restart it at a quiet boundary, gated by the backoff
        out.append({"slug": role.slug, "condition": "restart-retry", "key": f"session:{role.session}",
                    "since": rec.get("first") or iso(), "extra": list(rec.get("extra") or []), "between": None,
                    "detail": f"its restart failed {rec.get('fails', 1)}x in a row (last: "
                              f"{short_line(rec.get('error', ''), 120)}) and its session is not running"})
    return out


def harness_checks(spec: ShoalSpec) -> list[dict]:
    """`harness-missing`: a harness CLI this host's roles need that resolves nowhere (`config.harness_missing`), one
    condition per CLI, keyed by the CLI. None under SHOAL_FAKE_SESSIONS: a test host need not have any harness CLI."""
    from doctor import ECOSYSTEM_TOOLS
    from backends import fake_sessions
    if fake_sessions() is not None:
        return []
    tools = sorted({ECOSYSTEM_TOOLS[r.ecosystem] for r in spec.roles
                    if r.session and is_local(r, spec) and r.ecosystem in ECOSYSTEM_TOOLS and r.ecosystem != "opencode"})
    out = []
    for t in tools:
        why = config.harness_missing(t)
        if why:
            out.append({"slug": t, "condition": "harness-missing", "key": f"tool:{t}", "since": iso(), "first_seen": True,
                        "detail": f"{why}: no {t} session on this host can start, stop or be woken directly. An update "
                                  f"takes a minute or so; reinstall it (`shoal doctor` says where it was looked for)"})
    return out


def claude_daemon_checks(spec: ShoalSpec) -> list[dict]:
    """`claude-daemon`: a Claude account this shoal's roles use on this host whose daemon or sessions a closing Herdr
    terminal would end (`claude_daemon.conditions`)."""
    from backends import fake_sessions
    if fake_sessions() is not None:
        return []
    import claude_daemon
    used = {r.account or "" for r in spec.roles if r.ecosystem == "claude" and r.session and is_local(r, spec)}
    return [{"slug": f"host:{config.server()}", "condition": "claude-daemon", "key": c["key"], "since": iso(),
             "first_seen": True, "detail": c["detail"]} for c in claude_daemon.conditions(used)]


def multi_host(spec: ShoalSpec) -> bool:
    """True when the shoal's roles span more than one host. A shoal that names its home but runs every
    role there (the usual case) is single-host: it neither beats nor judges, so it writes no ledger noise."""
    return len({host_of(r, spec) for r in spec.roles}) > 1


def stuck_checkout(path: str, fetch: bool = True) -> tuple[str, str]:
    """(state, detail) for a shared checkout that cannot take its upstream, else ('', ''): an operation left in
    progress, a local file differing from one upstream changed (what stops a rebase), or commits on both sides.
    Behind alone is not stuck: an idle host's checkout just has not pulled. A path that is not a checkout on this host
    is not judged."""
    def git(*a: str) -> subprocess.CompletedProcess:
        return subprocess.run(["git", "-C", path, *a], capture_output=True, text=True, timeout=60, env=_ENV)
    try:
        if git("rev-parse", "--is-inside-work-tree").returncode:
            return "", ""
        if fetch:
            git("fetch", "-q")  # a failed fetch judges the last known upstream
        counts = git("rev-list", "--left-right", "--count", "HEAD...@{upstream}")
        if counts.returncode:
            return "", ""
        ahead, behind = (int(x) for x in counts.stdout.split())
        gitdir = git("rev-parse", "--absolute-git-dir").stdout.strip()
        for mark in ("rebase-merge", "rebase-apply", "MERGE_HEAD", "CHERRY_PICK_HEAD"):
            if os.path.exists(os.path.join(gitdir, mark)):
                return "in-progress", f"a git operation ({mark}) was left in progress"
        changed = set(git("diff", "--name-only", "HEAD...@{upstream}").stdout.splitlines())
        local = set(git("ls-files", "-m", "-o", "--exclude-standard").stdout.splitlines())
        blocked = [p for p in sorted(changed & local)
                   if git("hash-object", "--", p).stdout != git("rev-parse", f"@{{upstream}}:{p}").stdout]
        if blocked:
            return "blocked", f"{len(blocked)} local file(s) differ from what upstream changed, such as {blocked[0]}"
        if ahead and behind:
            return "diverged", f"{ahead} ahead and {behind} behind its upstream"
    except (OSError, ValueError, subprocess.SubprocessError):
        pass  # an unreadable checkout never stops the quorum
    return "", ""


def watched_checkouts(spec: ShoalSpec, dry_run: bool = False) -> list[dict]:
    """features.health.watch_checkouts: shared checkouts this host's quorum watches. List them on one spec per host,
    or each quorum pages about the same checkout. Off when unset."""
    out = []
    for raw in health_cfg(spec).get("watch_checkouts") or []:
        path = os.path.expanduser(str(raw))
        kind, detail = stuck_checkout(path, fetch=not dry_run)
        if kind:
            out.append({"slug": f"host:{config.server()}", "condition": "checkout-stuck", "key": f"{kind}:{path}",
                        "since": iso(), "first_seen": True,
                        "detail": f"{path}: {detail}. Sessions route around a stuck checkout, so it only gets worse: "
                                  f"resolve it, keeping every session's work"})
    return out


def orphaned_leases(spec: ShoalSpec, lease: dict | None = None) -> list[dict]:
    """features.health.watch_leases: a lease on this host its provider marks `orphan` (the holder's owner is gone, such as
    an abandoned CI build whose container still holds the lock). The provider's own reaper should free it within minutes,
    so one that outlives `lease-orphan`'s ladder since the owner ended is the reaper failing: a page. Turn it on in one
    spec per host, or each quorum pages about the same lease. `lease`: the provider's status (tests)."""
    if not health_cfg(spec).get("watch_leases"):
        return []
    if lease is None:
        import resource_usage
        lease = resource_usage.lease_observation(config.lease_cmd())
    import pool
    out: dict[str, dict] = {}
    for row in lease.get("leases") or []:
        o = row.get("orphan") if isinstance(row, dict) and row.get("held") else None
        if not isinstance(o, dict):
            continue
        key = f"{row.get('holder') or 'unattributed'}:{row.get('pid')}"
        if key in out:
            out[key]["keys"].append(str(row.get("key")))
            continue
        t = pool._epoch(o.get("since")) if o.get("since") else None
        since = iso(datetime.datetime.fromtimestamp(t, datetime.timezone.utc)) if t is not None else iso()
        out[key] = {"slug": f"host:{config.server()}", "condition": "lease-orphan", "key": key, "since": since,
                    "first_seen": t is None, "keys": [str(row.get("key"))], "orphan": o, "holder": row.get("holder")}
    for c in out.values():
        o = c.pop("orphan")
        c["detail"] = (f"lease {','.join(c.pop('keys'))} is held by {c.pop('holder')}, whose owner is gone "
                       f"({o.get('reason') or 'no reason given'}; {o.get('action') or 'no action recorded'}). Everything "
                       f"queued behind it waits: the provider's reaper did not free it, so free it by hand")
    return list(out.values())


def throttled_jobs(spec: ShoalSpec, found: list | None = None) -> list[dict]:
    """`job-throttled`: a job of this shoal running on this host whose cgroup is throttled (jobs.throttled_jobs), one
    instance per job run (its unit and start), counted from when health first saw it so. `found`: (meta, pressure)
    pairs (tests)."""
    if found is None:
        import jobs as _jobs
        try:
            found = _jobs.throttled_jobs(spec.sid)
        except Exception:  # noqa: BLE001 - an unreadable job never breaks a health run
            found = []
    import jobs as _jobs
    out = []
    for m, pr in found:
        out.append({"slug": str(m.get("slug") or "?"), "condition": "job-throttled",
                    "key": f"{m.get('unit')}@{m.get('started_utc') or '?'}", "since": iso(), "first_seen": True,
                    "detail": f"job {m.get('name') or m.get('unit')} ({m.get('unit')}) reads active but is throttled: "
                              f"{_jobs.pressure_text(pr)}. Stop a job sharing its slice, or raise the slice's ceiling "
                              f"([jobs] slice_memory_high in the host config)"})
    return out


def host_beats(spec: ShoalSpec, led, state: dict, dry_run: bool = False) -> list[dict]:
    """Multi-host liveness of whole hosts. Every host with a live local role writes a `host-beat` system
    event at most every `host_beat_min` (its quorum is alive); home raises `host-silent` for another host
    whose last beat is older than `host_silent_min`. A host that never beat is not judged (no false page
    when the feature arrives under a running shoal). A single-host shoal does neither."""
    if not multi_host(spec):
        return []
    cfg = health_cfg(spec)
    beat_min = float(cfg.get("host_beat_min", HOST_BEAT_MIN))
    silent_min = float(cfg.get("host_silent_min", HOST_SILENT_MIN))
    me = config.server()
    if any(r.session and is_local(r, spec) for r in spec.roles):
        last = state.get("host_beat", "")
        if not dry_run and (not last or (age_min(last) or 0.0) >= beat_min):
            try:
                led.append("shoal", "system", kind="host-beat", host=me, instance=host_instance(), **beat_spend(spec))
                state["host_beat"] = iso()
            except Exception as exc:  # noqa: BLE001 - a beat must never stop the quorum
                print(f"{iso()} health: host-beat write failed: {exc}")
    out = name_clash(spec, led, silent_min)
    if not is_home(spec):
        return out
    beats: dict[str, str] = {}
    for e in led.events():
        if e.get("type") == "system" and e.get("kind") == "host-beat" and e.get("host"):
            beats[e["host"]] = max(beats.get(e["host"], ""), e.get("ts", ""))
    for h in sorted({host_of(r, spec) for r in spec.roles if r.session} - {"", me}):
        ts = beats.get(h)
        a = age_min(ts) if ts else None
        if a is not None and a >= silent_min and maintenance_window(led, h) is None:
            roles_there = ", ".join(r.slug for r in spec.roles if r.session and host_of(r, spec) == h)
            out.append({"slug": f"host:{h}", "condition": "host-silent", "key": f"beat:{ts}", "since": ts,
                        "detail": f"host {h} has not beaten since {ts} ({a:.0f} min): its roles ({roles_there}) have "
                                  f"no health quorum and nothing on {me} can wake them. Asleep or offline is fine; "
                                  f"otherwise check its timers (`ssh {h} systemctl --user list-timers 'shoal-*'`)"})
    return out


def beat_spend(spec: ShoalSpec) -> dict:
    """{"spend": {slug: M cost-eq per day}} of this shoal's local Claude roles for its host-beat, with `[accounts]
    quota_handoff` or `rebalance` on: another host's balancing weighs this host's roles by it (role_spend.beats). {}
    when balancing is off or nothing is measured; a measurement that fails never stops the beat."""
    if not (accounts.quota_handoff_on() or accounts.rebalance_on()):
        return {}
    try:
        import role_spend
        got = role_spend.beat(spec)
    except Exception:  # noqa: BLE001 - the beat is liveness first
        return {}
    return {"spend": got} if got else {}


def maintenance_window(led, server: str) -> dict | None:
    """The active maintenance window for `server` (maintenance.py): a planned reboot or test there is not a silence."""
    try:
        import maintenance
        return maintenance.from_events(led.events(), server)
    except Exception:  # noqa: BLE001 - never stops the quorum; no window read means the page stands
        return None


def host_instance() -> str:
    """A stable, non-identifying id for THIS machine (a hash of /etc/machine-id, else of the hostname and
    data root): beats carry it so two hosts that chose the same `server` name can be told apart. Server
    names are labels each host picks; nothing authenticates them (git push access is the only authority)."""
    import hashlib
    try:
        seed = open("/etc/machine-id").read().strip()
    except OSError:
        seed = ""
    seed = seed or f"{os.uname().nodename}:{config.data_root()}"
    return hashlib.sha256(("shoal-host:" + seed).encode()).hexdigest()[:12]


def name_clash(spec: ShoalSpec, led, window_min: float) -> list[dict]:
    """`host-name-clash` when another machine beat under THIS host's `server` name within the window: both
    would treat the same roles as local. Detection, not prevention: the fix is renaming one host's `server`."""
    me, mine = config.server(), host_instance()
    others = sorted({e.get("instance") for e in led.events()
                     if e.get("type") == "system" and e.get("kind") == "host-beat" and e.get("host") == me
                     and e.get("instance") and e.get("instance") != mine
                     and (age_min(e.get("ts", "")) or 10**9) < window_min})
    if not others:
        return []
    return [{"slug": f"host:{me}", "condition": "host-name-clash", "key": f"clash:{','.join(others)}", "since": iso(),
             "first_seen": True,
             "detail": f"another machine (instance {', '.join(others)}) beats as `{me}`, this host's `server` name "
                       f"(instance {mine}): both treat the same roles as local. Give one host a different `server` "
                       f"in its config/host.toml"}]


def emit_conditions(sid: str, opened: dict[str, str], closed: dict[str, str]) -> None:
    """`health_condition` telemetry: one event per ladder key that opened or closed this run. A key
    is `<slug>|<condition>|<evidence>`. Best effort: telemetry never stops a quorum run."""
    try:
        from watchers import shoal_emit
    except Exception:  # noqa: BLE001
        return
    for event, keys in (("open", opened), ("close", closed)):
        for k, since in sorted(keys.items()):
            slug, condition, key = (k.split("|", 2) + ["", ""])[:3]
            extra = {}
            if event == "close" and since:
                a = age_min(since)
                extra["duration_min"] = f"{a:.1f}" if a is not None else ""
            shoal_emit(sid, "health", "script", "health_condition", event=event, slug=slug,
                       condition=condition, key=key, since=since, **extra)


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
    if not dry_run and is_local_any(spec):
        try:  # balancing reads this host's cached usage: keep it current here, not only where the UI feeder runs
            import accounts
            accounts.refresh_readings()
        except Exception as exc:  # noqa: BLE001 - a usage read never breaks a health run
            acts.append(("health", "usage readings", f"skipped ({str(exc)[:80]})"))
    _BALANCE.clear()
    if is_local_any(spec):
        try:  # one load for every balancing pick this run, and since when each account is over its share
            balance_observe(state)
        except Exception as exc:  # noqa: BLE001 - balancing inputs never break a health run: no share move this run
            _BALANCE.clear()
            _BALANCE["off"] = True
            acts.append(("health", "balance", f"skipped ({str(exc)[:80]})"))
    cbusy = claude_busy()
    conds = evaluate(spec, view, cbusy)
    conds += restart_retries(spec, spec_path, conds, cbusy, state, acts, dry_run)
    conds += harness_checks(spec)
    conds += claude_daemon_checks(spec)
    conds += host_beats(spec, led, state, dry_run)
    conds += watched_checkouts(spec, dry_run)
    conds += orphaned_leases(spec)
    conds += throttled_jobs(spec)
    if not dry_run and is_local_any(spec) and health_cfg(spec).get("context_snapshot", True):
        try:
            # one context-cost snapshot per host per UTC day (context_cost.py): the first run after midnight writes it
            import context_cost
            p_ = context_cost.write_snapshot()
            if p_:
                acts.append(("health", "context snapshot", p_))
        except Exception as exc:  # noqa: BLE001 - a snapshot never breaks a health run
            acts.append(("health", "context snapshot", f"skipped ({str(exc)[:80]})"))
    try:
        stale_worker_upkeep(spec, acts, dry_run, state)
    except Exception as exc:  # noqa: BLE001 - upkeep never breaks a health run
        acts.append(("health", "stale worker", f"upkeep skipped ({str(exc)[:80]})"))
    if view.freeze() is None:
        try:
            import park  # a parked role whose --until target resolved is relaunched (park.py)
            park.auto_unpark(spec, spec_path, state, acts, dry_run)
        except Exception as exc:  # noqa: BLE001 - never breaks a health run
            acts.append(("health", "unpark", f"skipped ({str(exc)[:80]})"))
    lim = update_limit(spec, state)
    until = limit_until(lim)
    hc = health_cfg(spec)
    wake_off = set(hc.get("wake_off") or [])
    offline_hold = set(hc.get("offline_hold", OFFLINE_HOLD))
    rewake_min = float(hc.get("rewake_min", REWAKE_MIN))
    settle_min = float(hc.get("online_settle_min", ONLINE_SETTLE_MIN))
    gap_min = float(hc.get("wake_gap_min", WAKE_GAP_MIN))
    offline = view.user_offline() is not None
    um = view.modes.get("user-offline") or {}
    back = age_min(um.get("ts", "")) if um.get("state") == "off" else None
    settling = back is not None and back < settle_min
    woke = state.setdefault("woke", {})
    for k_ in [k_ for k_, t_ in woke.items() if (age_min(t_) or 0.0) >= rewake_min]:
        del woke[k_]
    last_wake = state.setdefault("last_wake", {})
    merged: dict[str, list] = {}  # slug -> [(condition, detail, act index)]: one wake per role per run
    quota_done = False  # one quota handoff per run
    ladders = state.setdefault("ladders", {})
    prev_keys = set(ladders)
    for c in conds:
        if c["condition"] == "job-throttled" and f"{c['slug']}|{c['condition']}|{c['key']}" not in prev_keys:
            acts.append((c["slug"], "job-throttled", f"WARN {c['detail']}"))  # a page follows only if it lasts
    live_keys = set()
    holds: dict[str, dict] = {}

    def hold_of(slug_: str) -> dict:
        if slug_ not in holds:
            try:
                holds[slug_] = watcher_hold(spec.sid, slug_) if spec.role(slug_) is not None else {}
            except Exception:  # noqa: BLE001 - an unreadable sidecar never holds a ladder
                holds[slug_] = {}
        return holds[slug_]
    for c in conds:
        k = f"{c['slug']}|{c['condition']}|{c['key']}"
        live_keys.add(k)
        lad = ladders.setdefault(k, {"since": c["since"] if not c.get("first_seen") else iso(), "step": 0})
        a = age_min(lad["since"]) or 0.0
        if c["condition"] == "wait-validity":
            v = float(health_cfg(spec).get("validity_wake_min", 90))
            steps = [(0, "wake"), (v, "wake"), (2 * v, "restart")]
        elif c["condition"] == "turn-ended-active":
            m = float((continuation_cfg(spec) or {}).get("after_min", 5))
            steps = [(m, "wake"), (m + 30, "wake"), (m + 90, "page")]
        elif c["condition"] == "context-restart" and __import__("context").mode(spec, c["slug"]) == "auto":
            steps = CONTEXT_AUTO_LADDER
        elif c["condition"] == "quota-handoff":
            steps = LADDERS["quota-handoff"]
        else:
            steps = LADDERS[c["condition"]]
        if c["condition"] in wake_off:  # features.health.wake_off: this condition pages, never wakes
            steps = [x for x in steps if x[1] != "wake"]
        hold = hold_of(c["slug"]) if c["condition"] not in HOLD_EXEMPT else {}
        if hold.get("attached"):
            # the role's watcher holds every wake for an attached client: a wake from here would be held too, and a page
            # saying "woken Nx without effect" would be false (2026-11-22: two wakes never delivered, paged as such).
            # `wakes-held` pages the hold itself; this ladder waits, and counts from the delivery once the client exits
            lad["held_by"] = hold["reason"]
            if lad["step"] < len(steps) and a >= steps[lad["step"]][0] and lad.get("held_said") != hold["reason"]:
                lad["held_said"] = hold["reason"]
                acts.append((c["slug"], f"{c['condition']} step{lad['step'] + 1}:{steps[lad['step']][1]}",
                             f"held (its watcher holds every wake for an attached client, pid {hold['pid']}; "
                             f"this ladder counts from when it delivers)"))
            continue
        if c["condition"] == "wakes-held":
            # offline, a card's client left attached past the grace is ended, so the watcher delivers what it holds
            h_ = hold_of(c["slug"])
            r_ = spec.role(c["slug"])
            if h_.get("attached") and r_ is not None and card_detach(
                    spec, r_, lambda: ([h_["pid"]], (r_.session,)), h_["since"], f"{h_['events']} wake(s)", lad, acts,
                    "wakes-held:detach", offline, dry_run):
                continue
        if lad.pop("held_by", None):
            # the client went away and the watcher delivers what it held: what was held was never a wake without effect
            lad.pop("held_said", None)
            lad["since"], lad["step"] = iso(), 0
            a = 0.0
        while lad["step"] < len(steps) and a >= steps[lad["step"]][0]:
            at, action = steps[lad["step"]]
            role_ = spec.role(c["slug"])
            # several accounts: only this role's account's limit holds it
            until_r = limit_until(lim, role_.account) if role_ is not None and lim.get("accounts") else until
            if until_r and action == "wake" and role_ is not None and role_.ecosystem == "claude":
                # inside the usage limit a Claude turn errors and the wake is lost: hold the step (it runs
                # after the reset) and remember the role for the resume wake
                if c["slug"] not in lim.setdefault("held", []):
                    lim["held"].append(c["slug"])
                if lad.get("held") != until_r:
                    lad["held"] = until_r
                    acts.append((c["slug"], f"{c['condition']} step{lad['step'] + 1}:{action}",
                                 f"held (usage limit until {until_r})"))
                break
            if action == "restart" and c["condition"] in RESTART_NOT_PAGE and c.get("between") is False:
                # mid-turn the role may be acting on it right now: a restart would cut that turn short
                if lad.get("quiet") != "mid-turn":
                    lad["quiet"] = "mid-turn"
                    acts.append((c["slug"], f"{c['condition']} step{lad['step'] + 1}:restart", "held (mid-turn)"))
                break
            gate = restart_gate(spec, role_) if action == "restart" else ""
            if gate:
                # a restart under way, or one that failed and waits out its backoff: the ladder stays where it is
                if lad.get("quiet") != gate:
                    lad["quiet"] = gate
                    acts.append((c["slug"], f"{c['condition']} step{lad['step'] + 1}:restart", f"held ({gate})"))
                break
            client = restart_client(role_) if action == "restart" else ""
            if client:
                # a person attached to the session keeps its worker alive, so a stop fails STILL LIVE and the ladder moved
                # on to its page with no retry (2026-11-10, it-tune): hold the restart until they detach, paging once
                held = lad.setdefault("attached", iso())
                if lad.get("attached_said") != client:
                    lad["attached_said"] = client
                    acts.append((c["slug"], f"{c['condition']} step{lad['step'] + 1}:{action}",
                                 f"held ({client}; it restarts once the client detaches)"))
                # offline, a card's `claude attach` left open past the grace is ended; the restart runs next time
                ov = c.get("over") or ()
                far = c["condition"] == "context-restart" and len(ov) == 2 and ov[0] >= CARD_DETACH_FAR_X * ov[1]
                what = "its context restart" if c["condition"] == "context-restart" else f"its restart ({c['condition']})"
                if card_detach(spec, role_, lambda: claude_clients(role_), held, what, lad, acts,
                               f"{c['condition']}:detach", offline, dry_run, far):
                    break
                if (age_min(held) or 0.0) >= ATTACHED_PAGE_MIN and not lad.get("attached_paged"):
                    lad["attached_paged"] = iso()
                    acts.append((c["slug"], f"{c['condition']} step{lad['step'] + 1}:page", page_user(
                        spec, f"{c['slug']}: restart-ready, but its automatic restart is held: {client}. Detach the "
                        "client (its card or terminal) to let it restart", state, dry_run)))
                break
            for k in ("attached", "attached_said", "attached_paged") + (
                    ("detached", "detach_failed", "not_card") if action == "restart" else ()):
                lad.pop(k, None)  # a restart no longer held: a client attached later is a new hold
            if action == "wake" and c["condition"] == "watcher-dead":
                # the quorum re-arms a dead DETACHED watcher itself (no model turn); only an in-session
                # (exit) watcher still needs its role woken to re-arm (architecture doc §7)
                ch = rearm_watcher(spec, role_, state, dry_run)
                if ch and not ch.startswith("FAILED"):
                    acts.append((c["slug"], f"{c['condition']} step{lad['step'] + 1}:rearm", ch))
                    lad["step"] += 1
                    break
                if c.get("rearm_only"):
                    # nothing waits for this role: a wake would only spend a turn to re-arm. The ladder goes on to its
                    # page if the watcher cannot be kept up
                    acts.append((c["slug"], f"{c['condition']} step{lad['step'] + 1}:wake",
                                 f"skipped (nothing for it to do; re-arm {ch or 'not due yet'})"))
                    lad["step"] += 1
                    break
            if action == "wake":
                fp = f"{c['slug']}|{c['condition']}|{c.get('fp') or c['key']}"
                hold = ""
                if offline and c["condition"] in offline_hold:
                    # the maintainer is away: a role re-woken about a parked queue or a long hold finds the
                    # same blocker and goes back to sleep (2026-10-20: three cold wakes overnight)
                    hold = "maintainer offline"
                elif settling and c["condition"] != "asks-on-return":  # the return's own wake is not held by it
                    hold = f"settling {settle_min:g} min after the maintainer's return"
                elif c["slug"] not in merged and (age_min(last_wake.get(c["slug"], "")) or 1e9) < gap_min:
                    hold = f"woken {age_min(last_wake[c['slug']]):.0f} min ago (gap {gap_min:g})"
                if hold:
                    if lad.get("quiet") != hold.split(" ")[0]:
                        lad["quiet"] = hold.split(" ")[0]
                        acts.append((c["slug"], f"{c['condition']} step{lad['step'] + 1}:wake", f"held ({hold})"))
                    break
                lad.pop("quiet", None)
                if c["condition"] in REWAKE_CONDS and fp in woke:
                    # the same parked set or hold already had its wake: nothing has moved it since
                    acts.append((c["slug"], f"{c['condition']} step{lad['step'] + 1}:wake",
                                 f"skipped (woken for the same at {woke[fp]}; rewake_min {rewake_min:g})"))
                    lad["step"] += 1
                    break
                if c["condition"] in REWAKE_CONDS:
                    woke[fp] = iso()
                merged.setdefault(c["slug"], []).append((c["condition"], c["detail"], len(acts)))
                acts.append((c["slug"], f"{c['condition']} step{lad['step'] + 1}:wake", ""))
                lad["step"] += 1
                break
            n_before = int(restart_record(spec.sid, c["slug"]).get("fails") or 0) if action == "restart" else 0
            if action == "restart" and c["condition"] == "quota-handoff":
                # one quota move per run; a share move also waits out the role's cooldown and this host's settle: the
                # readings and the load the next pick reads catch up with each move first (balance_hold)
                kind = c.get("kind", "share")
                hold = balance_hold(kind, c["slug"], state, quota_done)
                if hold:
                    if lad.get("quiet") != hold.split(" ")[0]:
                        lad["quiet"] = hold.split(" ")[0]
                        acts.append((c["slug"], f"{c['condition']} step{lad['step'] + 1}:restart", f"held ({hold})"))
                    break
                ch = context_restart(spec, spec_path, role_, dry_run, tuple(c.get("extra") or ()))
                if not ch.startswith("FAILED"):
                    if not dry_run:
                        balance_moved(state, c["slug"], kind, c.get("from", ""), f"{spec.sid}/{c['slug']}")
                    quota_done = True
                    ch += f"; {c['detail']}"
            elif action == "restart" and c["condition"] == "restart-retry":
                ch = context_restart(spec, spec_path, role_, dry_run, tuple(c.get("extra") or ()))
            elif action == "restart":
                moved = rebalance(spec, role_)
                ch = context_restart(spec, spec_path, role_, dry_run, moved[:1])
                if moved and not ch.startswith("FAILED"):
                    ch += f"; {moved[1]}"
                    if not dry_run and len(moved) > 3:  # the quota handoff's cooldown and return margin see it
                        balance_moved(state, c["slug"], moved[2], moved[3], f"{spec.sid}/{c['slug']}")
                if c["condition"] in RESTART_NOT_PAGE and not ch.startswith("FAILED"):
                    ch = f"woken {lad['step']}x without effect: {ch}"
            elif c["condition"] in ("host-silent", "host-name-clash", "context-restart", "checkout-stuck", "lease-orphan",
                                    "wakes-held", "harness-missing", "job-throttled", "claude-daemon"):
                ch = page_user(spec, f"{c['slug']}: {c['detail']}", state, dry_run)
            else:
                hold = hold_of(c["slug"])
                # a Claude session at an open question takes no wake either: its wakes wait there, undelivered
                how = (f"woken {lad['step']}x, but its watcher has held its wakes undelivered since {hold['since']}: the "
                       f"session is at an open question; answer it in its chat or from the Asks dialog" if hold.get("reason") == "question"
                       else f"woken {lad['step']}x without effect")
                ch = page_user(spec, f"{c['slug']}: {c['detail']} ({how})", state, dry_run)
            if action == "restart" and ch.startswith("FAILED") and role_ is not None and not dry_run:
                # it stays due: the ladder keeps this step, tried again after its backoff (restart_gate); paged with
                # the error after RESTART_FAIL_PAGE failures in a row
                ch += restart_failed(spec, role_, ch, c, n_before, state, dry_run)
                acts.append((c["slug"], f"{c['condition']} step{lad['step'] + 1}:{action}", ch))
                break
            acts.append((c["slug"], f"{c['condition']} step{lad['step'] + 1}:{action}", ch))
            lad["step"] += 1
            break  # at most one action per condition per run
    # one wake per role per run, carrying every condition that is due (each used to be its own turn)
    for slug_, items in merged.items():
        conds_ = "+".join(dict.fromkeys(x[0] for x in items))
        detail = items[0][1] if len(items) == 1 else " | ".join(f"[{x[0]}] {x[1]}" for x in items)
        ch = request_wake(spec, slug_, detail, conds_, dry_run)
        if ch.startswith("no-adapter"):
            ch += "; " + page_user(spec, f"{slug_}: {detail} (and no wake adapter: {ch})", state, dry_run)
        elif not ch.startswith("remote"):
            last_wake[slug_] = iso()
        for n_, (_, _, i_) in enumerate(items):
            acts[i_] = (acts[i_][0], acts[i_][1], ch if n_ == 0 else f"merged into the {items[0][0]} wake")
    # the usage limit has reset: one resume wake to every role it touched or whose wakes were held; with several accounts,
    # each account's roles as soon as that account's own limit resets (another account's later reset does not hold them)
    per = lim.get("accounts") or {}
    if per:
        woken = lim.setdefault("woken_accounts", {})
        for acct_name, r_reset in sorted(per.items()):
            if woken.get(acct_name) == r_reset or limit_until({"reset": r_reset}):
                continue
            floor = iso(parse(r_reset) - datetime.timedelta(hours=LIMIT_LOOKBACK_H))
            touched = {s for s, ts in (lim.get("affected") or {}).items() if ts >= floor} | set(lim.get("held") or [])
            for s in sorted(touched):
                r_ = spec.role(s)
                if r_ is None or not r_.session or accounts.name("claude", r_.account) != acct_name:
                    continue
                acts.append((s, "usage-limit reset:wake", request_wake(
                    spec, s, f"the {acct_name} account's usage limit reset at {r_reset}: a turn that errored, a turn you "
                             f"stopped on the grace note, or a wake lost to the limit resumes now", "limit-reset", dry_run)))
                if s in (lim.get("held") or []):
                    lim["held"].remove(s)
            woken[acct_name] = r_reset
        if all(woken.get(k) == v for k, v in per.items()):
            lim["woken"] = lim.get("reset", "")
    elif lim.get("reset") and not until and lim.get("woken") != lim["reset"]:
        floor = iso(parse(lim["reset"]) - datetime.timedelta(hours=LIMIT_LOOKBACK_H))
        touched = {s for s, ts in (lim.get("affected") or {}).items() if ts >= floor} | set(lim.get("held") or [])
        for s in sorted(touched):
            r_ = spec.role(s)
            if r_ is None or not r_.session:
                continue
            acts.append((s, "usage-limit reset:wake", request_wake(
                spec, s, f"the account usage limit reset at {lim['reset']}: a turn that errored, a turn you "
                         f"stopped on the grace note, or a wake lost to the limit resumes now", "limit-reset", dry_run)))
        lim["woken"] = lim["reset"]
        lim["held"] = []
    closed = {}
    for k in list(ladders):
        if k not in live_keys:
            closed[k] = ladders[k].get("since", "")
            del ladders[k]  # condition cleared: its ladder resets
    if not dry_run:
        emit_conditions(spec.sid, {k: ladders[k].get("since", "") for k in live_keys - prev_keys}, closed)
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
    if waits and state.get("digest_day") != today and view.user_offline() is None and is_home(spec):
        # a group duty: every host's quorum sees the same ledger, so only home sends it
        acts.append(("user", "user-waits digest", page_user(spec, "roles waiting on you: " + "; ".join(waits), state, dry_run)))
        state["digest_day"] = today
    if view.user_offline() is None and state.get("deferred_pages"):
        dp = state.pop("deferred_pages")
        acts.append(("user", "deferred pages", page_user(spec, f"{len(dp)} pages deferred while offline: "
                                                          + " | ".join(p["text"][:120] for p in dp), state, dry_run)))
    if view.freeze() is None:
        try:  # after the ladders: a condition raised this run keeps its role out, and nothing above acts on a stopped session
            import park
            park.auto_park(spec, spec_path, view, conds, state, acts, dry_run)
        except Exception as exc:  # noqa: BLE001 - never breaks a health run
            acts.append(("health", "auto-park", f"skipped ({str(exc)[:80]})"))
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


def add_parsers(sub) -> None:
    p = sub.add_parser("health", help="health quorum run (what the two health timers execute)")
    p.add_argument("--spec", default="")
    config.add_id_arg(p, default="")
    p.add_argument("--instance", choices=["a", "b"], default="a")
    p.add_argument("--dry-run", action="store_true", help="evaluate and print actions; wake and page nobody")
    p.set_defaults(func=cmd_health)
