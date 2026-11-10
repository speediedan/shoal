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
  wait-resolved      a machine-checkable wait target is no longer pending                  wake@15 page@60
  asks-on-return     the maintainer came back; a role holding asks has not written since    wake@3 (once a return)
  blocker-resolved   a next item's role or provider blocker cleared                         wake@0
  wait-validity      any CI / lease / job wait, every validity_wake_min (default 90)       wake@V  wake@2V page@3V
                     (not a target its provider confirms pending, under wait discipline)
  watcher-dead       ledger-watch pid gone, session between turns                          wake@10 page@40
  stale-active       posture active, no ledger write, session between turns                wake@90 page@180
  user-waits         waiting on the maintainer: never wakes the role; one daily digest page

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
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "telemetry"))
from ledger import OFFER_WRITERS, SYSTEM_WRITER  # noqa: E402

import config  # noqa: E402
import providers  # noqa: E402
from roles import ShoalSpec, find_spec, host_of, is_detached_wake, is_home, is_local, state_dir  # noqa: E402

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
    # bounded context (context.py): a role past its limit that marked a quiet boundary `--restart-ready` is
    # handed to a fresh session; `auto` mode does it from here, `page` (the default) asks the operator
    "context-restart": [(0, "page")],
    # the maintainer's return (user-offline off): a role holding asks raises them. `offline off` wakes only the roles of
    # the host it ran on, so each host's quorum wakes its own, once per return, after RETURN_GRACE_MIN
    "asks-on-return": [(0, "wake")],
}
CONTEXT_AUTO_LADDER = [(0, "restart"), (30, "page")]
ATTACHED_PAGE_MIN = 30  # a restart held for an attached client pages once after this


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
    try:
        import context as _ctx
        role = spec.role(slug)
        adv = _ctx.advisory(spec, role) if role is not None else ""
        if adv:
            text += " || " + adv
    except Exception:  # noqa: BLE001
        pass
    return text


def request_wake(spec: ShoalSpec, slug: str, reason: str, condition: str = "", dry_run: bool = False) -> str:
    """Wake one role through its ecosystem's direct session wake (backends.Backend.wake);
    returns the channel used."""
    role = spec.role(slug)
    if role is None:
        return "no-such-role"
    if not is_local(role, spec):
        # the role's own host delivers its wakes (its watcher reads the ledger; its quorum runs there)
        return f"remote (skipped: {slug} runs on {role.server or spec.home}, whose watcher and quorum wake it)"
    text = wake_text(spec, slug, reason, condition)
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


def claude_job_state(row: dict) -> dict:
    """The job's state file ({} when unknown). A row may carry `tempo`/`inFlight` itself (tests)."""
    if row.get("tempo"):
        return {"tempo": row["tempo"], "inFlight": row.get("inFlight") or {}}
    from backends import fake_sessions
    if fake_sessions() is not None:  # tests: never read the host's real job files
        return {}
    jid = str(row.get("id") or "")
    if not jid or not re.fullmatch(r"[0-9a-f]{6,64}", jid):
        return {}
    base = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")
    try:
        with open(os.path.join(base, "jobs", jid, "state.json")) as fh:
            st = json.load(fh)
        return st if isinstance(st, dict) else {}
    except (OSError, ValueError):
        return {}


def claude_job_tempo(row: dict) -> str:
    """The job's `tempo` ('' when unknown)."""
    return str(claude_job_state(row).get("tempo") or "")


def agents_in_flight(st: dict) -> int:
    """How many of the session's own subagents are running (`inFlight` kinds naming an agent). A
    background shell does not count: a bare `run_in_background` command kept `state` at working and
    hid ended turns (2026-10-08), so only delegated agent work holds a turn open."""
    fl = st.get("inFlight") or {}
    if not isinstance(fl, dict) or not any("agent" in str(k) for k in fl.get("kinds") or []):
        return 0
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
    if tempo == "idle" and agents_in_flight(st):
        return "busy"
    if tempo:
        return tempo
    st = str(row.get("state") or "")
    return CLAUDE_TURN_STATES.get(st) or str(row.get("status", ""))


_CLAUDE_ROWS: list = []  # the rows behind the last claude_busy() call


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
    base = os.path.join(os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude"), "projects")
    hits = []
    if session:
        try:
            from backends import claude_current
            cur = claude_current(session)
        except Exception:  # noqa: BLE001
            cur = session
        hits = glob.glob(os.path.join(base, "*", f"{cur}*.jsonl")) or glob.glob(os.path.join(base, "*", f"{session}*.jsonl"))
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
    """The id of the newest AskUserQuestion with no result yet in the session's transcript tail, else ""."""
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
        if "AskUserQuestion" not in line and "tool_result" not in line:
            continue
        try:
            content = (json.loads(line).get("message") or {}).get("content")
        except ValueError:
            continue
        for c in content if isinstance(content, list) else []:
            if not isinstance(c, dict):
                continue
            if c.get("type") == "tool_use" and c.get("name") == "AskUserQuestion":
                open_q[str(c.get("id") or "?")] = True
            elif c.get("type") == "tool_result":
                open_q.pop(str(c.get("tool_use_id")), None)
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
    woken: the reset already answered with a resume wake}."""
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
            aff = lim.setdefault("affected", {})
            aff[role.slug] = max(aff.get(role.slug, ""), ts)
    if lim.get("reset") and lim.get("woken") == lim["reset"] and lim["reset"] < floor:
        lim = {}  # answered and out of the lookback: forget it
    if lim:
        state["limit"] = lim
    else:
        state.pop("limit", None)
    return lim


def limit_until(lim: dict) -> str:
    """The reset time while the usage limit is in force (plus a short grace), else ""."""
    r = (lim or {}).get("reset", "")
    t = parse(r)
    if t is None or utcnow() >= t + datetime.timedelta(minutes=LIMIT_RESET_GRACE_MIN):
        return ""
    return r


def limit_until_sid(sid: str) -> str:
    """limit_until() from a shoal's health state, for processes outside the quorum (ledger-watch)."""
    try:
        return limit_until(json.load(open(os.path.join(state_dir(sid), "health.json"))).get("limit") or {})
    except (OSError, ValueError):
        return ""


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

def watcher_dead(alive: bool, posture: str, between: bool | None) -> bool:
    """A role's ledger-watch counts as dead when it is not running, the role is not offline, and the
    session is not known to be mid-turn (a session mid-turn arms its watcher at the turn's end).
    `between` is session_between_turns(): True between turns, False mid-turn or on a question,
    None unknown (unknown counts, as a missing watcher on an unknown session is the failure)."""
    return not alive and posture != "offline" and between is not False


def lazy_rearm(spec: ShoalSpec) -> bool:
    """features.health.lazy_rearm (default on): a dead watcher on a role with nothing to do is not
    worth a turn; the role is woken when work for it appears (architecture doc §7)."""
    return bool(health_cfg(spec).get("lazy_rearm", True))


def watcher_dormant(spec: ShoalSpec, view, role, posture: str) -> bool:
    """True when a dead watcher needs no wake now: lazy_rearm is on, the role is not active, and the
    ledger holds nothing its watcher would have woken it for (ledger_watch.actionable, board off: a
    self-fill role's board is the digest's or its next wake's). Its next real work raises its own
    condition (unacked-work, wait-resolved, a wake request), and that wake goes direct, re-arming
    included. Waking it only to re-arm cost a cold turn each (2026-10-20: six overnight)."""
    if not lazy_rearm(spec) or posture == "active":
        return False
    try:
        from ledger_watch import actionable, load_sidecar, sidecar_path
        done = set(filter(None, load_sidecar(sidecar_path(spec.sid, role.slug)).get("delivered", "").split(",")))
        return not [i for i, _ in actionable(view, role.slug, role, spec, board=False) if i not in done]
    except Exception:  # noqa: BLE001 - unknown is not dormant
        return False


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


def evaluate(spec: ShoalSpec, view, cbusy: dict[str, str]) -> list[dict]:
    """[{slug, condition, key, since, detail}] for every live condition instance."""
    from state import read_wait, wait_discipline
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
        posture = st.get("state") or ("idle" if idle else "active")
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
        if posture == "waiting" and sub not in USER_WAITS:
            pending, why = target_pending(target, slug, spec.sid, upstream=(sub == "upstream"))
            if pending is False:
                out.append({"slug": slug, "condition": "wait-resolved", "key": f"target:{target}", "since": iso(),
                            "detail": f"wait target {target} no longer pending ({why})", "first_seen": True})
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
                                f"re-verify the wait is still valid"})
        # watcher
        # a detached (--wake direct) watcher outlives its session's worker and resumes the session when
        # work arrives: a retired worker is dormant, and waking it only to re-arm cost a cold turn
        # (2026-10-20: six in one night)
        alive, sc = watcher_alive(spec.sid, slug)
        if watcher_dead(alive, posture, between) and not watcher_dormant(spec, view, role, posture):
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
            from state import wait_proven
            # shoal re-proves the wait itself; the model is woken only when that fails (or once a day)
            if posture == "waiting" and ru and parse(ru) is not None and parse(ru) < utcnow() and \
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
        # bounded context: the role marked a quiet boundary after its context passed the limit
        if q.get("restart_ready") and posture in ("idle", "waiting") and between:
            import context as _ctx
            o = _ctx.over(spec, role)
            if o is not None:
                out.append({"slug": slug, "condition": "context-restart", "key": f"session:{role.session}",
                            "since": q.get("ts") or iso(), "first_seen": True,
                            "detail": f"context {o[0]}k past its {o[1]}k limit and the role is restart-ready "
                                      f"(start doc refreshed, nothing mid-flight): `shoal restart --spec <spec> "
                                      f"--only {slug}` hands it to a fresh session"})
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
    p = os.path.join(os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude"), "daemon", "roster.json")
    try:
        d = json.load(open(p))
    except (OSError, ValueError):
        return {}
    return d if isinstance(d, dict) else {}


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


def context_restart(spec: ShoalSpec, spec_path: str, role, dry_run: bool = False, extra: tuple[str, ...] = ()) -> str:
    """features.health.context_restart `auto`: hand a restart-ready role to a fresh session. The old Claude
    session is stopped first and its detached watcher ended (neither wakes it), so nothing delivers to it
    afterwards; then `restart --only` launches the new one, detached from this run (it verifies the new
    loop for minutes, and the quorum holds a lock). `extra`: more restart arguments (`shoal handoff`'s model and
    provider)."""
    if role is None or not spec_path:
        return "FAILED (no role or spec path)"
    argv = shlex.split(config.cli()) + ["restart", "--spec", spec_path, "--only", role.slug, *extra]
    if dry_run:
        return "would restart (dry-run): " + " ".join(argv[-(4 + len(extra)):])
    log = os.path.join(state_dir(spec.sid), f"context-restart-{role.slug}.log")
    guard = None
    try:
        os.makedirs(state_dir(spec.sid), exist_ok=True)
        # one restart of a role at a time: the spec records the new session only when it ends, so a double click, or the
        # quorum and `handoff` together, would launch two. The detached restart inherits this descriptor, so the lock
        # lasts as long as it does and goes when it exits, however it exits.
        guard = open(os.path.join(state_dir(spec.sid), f"context-restart-{role.slug}.lock"), "a")
        try:
            fcntl.flock(guard, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return f"FAILED (a restart of {role.slug} is already running; its log: {log})"
        alive, sc = watcher_alive(spec.sid, role.slug)
        if alive and is_detached_wake(str(sc.get("wake", ""))):
            os.kill(int(sc["pid"]), 15)  # a detached watcher: ending it wakes nobody
        if role.ecosystem == "claude" and role.session:
            # every job on the conversation, watched so a respawned worker is stopped again: a copy left behind would hold
            # the role beside the new session
            import park
            stopped = park.stop_session(role)
            if "STILL LIVE" in stopped:
                __import__("context").note_restart(spec, role, f"FAILED ({stopped})")
                return f"FAILED ({stopped}; nothing restarted)"
        from backends import spawn_detached
        # its own scope, like a re-armed watcher: the quorum's oneshot unit would end it with its cgroup
        spawn_detached(argv, stdin=subprocess.DEVNULL, stdout=open(log, "a"), stderr=subprocess.STDOUT,
                       cwd=spec.dir or None, env=_ENV, pass_fds=(guard.fileno(),))
        out = f"restarting on a fresh session (log {log})"
    except (OSError, ValueError) as exc:
        out = f"FAILED ({exc})"
    finally:
        if guard is not None:
            guard.close()  # this process's copy: the restart's keeps the lock
    __import__("context").note_restart(spec, role, out)
    return out


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
                led.append("shoal", "system", kind="host-beat", host=me, instance=host_instance())
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
    conds = evaluate(spec, view, claude_busy())
    conds += host_beats(spec, led, state, dry_run)
    conds += watched_checkouts(spec, dry_run)
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
    ladders = state.setdefault("ladders", {})
    prev_keys = set(ladders)
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
        elif c["condition"] == "context-restart" and __import__("context").mode(spec, c["slug"]) == "auto":
            steps = CONTEXT_AUTO_LADDER
        else:
            steps = LADDERS[c["condition"]]
        if c["condition"] in wake_off:  # features.health.wake_off: this condition pages, never wakes
            steps = [x for x in steps if x[1] != "wake"]
        while lad["step"] < len(steps) and a >= steps[lad["step"]][0]:
            at, action = steps[lad["step"]]
            role_ = spec.role(c["slug"])
            if until and action == "wake" and role_ is not None and role_.ecosystem == "claude":
                # inside the usage limit a Claude turn errors and the wake is lost: hold the step (it runs
                # after the reset) and remember the role for the resume wake
                if c["slug"] not in lim.setdefault("held", []):
                    lim["held"].append(c["slug"])
                if lad.get("held") != until:
                    lad["held"] = until
                    acts.append((c["slug"], f"{c['condition']} step{lad['step'] + 1}:{action}",
                                 f"held (usage limit until {until})"))
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
                if (age_min(held) or 0.0) >= ATTACHED_PAGE_MIN and not lad.get("attached_paged"):
                    lad["attached_paged"] = iso()
                    acts.append((c["slug"], f"{c['condition']} step{lad['step'] + 1}:page", page_user(
                        spec, f"{c['slug']}: restart-ready, but its automatic restart is held: {client}. Detach the "
                        "client (its card or terminal) to let it restart", state, dry_run)))
                break
            for k in ("attached", "attached_said", "attached_paged"):
                lad.pop(k, None)
            if action == "wake" and c["condition"] == "watcher-dead":
                # the quorum re-arms a dead DETACHED watcher itself (no model turn); only an in-session
                # (exit) watcher still needs its role woken to re-arm (architecture doc §7)
                ch = rearm_watcher(spec, role_, state, dry_run)
                if ch and not ch.startswith("FAILED"):
                    acts.append((c["slug"], f"{c['condition']} step{lad['step'] + 1}:rearm", ch))
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
            if action == "restart":
                ch = context_restart(spec, spec_path, role_, dry_run)
            elif c["condition"] in ("host-silent", "host-name-clash", "context-restart", "checkout-stuck"):
                ch = page_user(spec, f"{c['slug']}: {c['detail']}", state, dry_run)
            else:
                ch = page_user(spec, f"{c['slug']}: {c['detail']} (woken {lad['step']}x without effect)", state, dry_run)
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
    # the usage limit has reset: one resume wake to every role it touched or whose wakes were held
    if lim.get("reset") and not until and lim.get("woken") != lim["reset"]:
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
