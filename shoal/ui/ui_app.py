#!/usr/bin/env python3
"""The Herdr app adapter (docs/ui.md "The Herdr app"; architecture doc §12).

    shoal ui app-plan [--shoal ID]... [--json]
    shoal ui app-open [--machine NAME|local] [--shoal ID]... [--dry-run] [--no-focus] [--no-standalone]
    shoal ui app-start [--dry-run] [--write-desktop]
    shoal ui app-install [--dry-run] [--check]

The Herdr app (a desktop window) draws spaces, tabs and panes that live in each machine's herdr-core. The
adapter splits the work the way the data lives:

- **plan** (`app-plan`, on the host that runs the shoals): which spaces, tabs and panes that host's shoals and
  saved standalone sessions want, as JSON. It knows the specs, the short tags, the routes and the registry.
  Read-only.
- **reconcile** (`app-open`, where the app runs): reads the plan (in process for this host, over the route for
  another), compares it with `herdr-app ls`, and adds what is missing with `herdr-app do`. A pane is replaced only
  when its terminal came back as a bare shell; anything else in a pane is left as it is. Nothing is typed into a
  pane, no session is resumed, no role is woken.
- **report** (`card_report`): a role card in a herdr-core terminal prints its own OSC 7501 status, so the app's
  agents list, space and tab dots, sounds and toasts follow the role. The 0.9.3 feeder's socket push is not used.

herdr 0.9.3 (`ui open`, `ui feed`, `ui launch`) is untouched and keeps working beside it.
"""
from __future__ import annotations

import base64
import fcntl
import glob
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.dirname(HERE)
REPO = os.path.dirname(PKG)
for _p in (PKG, HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import config  # noqa: E402

PLAN_V = 1
# The Herdr app and herdr-core releases every shoal host runs. The app updates itself (and then the core) unless
# its release feed is unreachable, so every shoal-managed launch points HERDR_RELEASES_URL at a dead loopback URL;
# upgrades happen by rerunning the installer deliberately, lane host first.
APP_PIN = "0.1.13"
CORE_PIN = "0.1.18"
DEAD_RELEASES_URL = "http://127.0.0.1:9"
APP_UNIT = "shoal-herdr-app"
FEED_UNIT = "shoal-graph-feed.service"
MODS = ("lattice-view", "shoal-home", "shoal-actions", "shoal-status", "shoal-usage", "shoal-board")
MOD_DIR = os.path.join(REPO, "plugins", "shoal-herdr-app", "mods")
REPORT_APP = "shoal"
# the plan's own label on each terminal it starts, so a later reconcile finds the pane again
PANE_LABEL = "shoal.pane"
FEED_FRESH_S = 60
STARVED_AFTER_S = 7200
ROLE_TAB = re.compile(r"^roles( \d+)?$")
# Bumped when what a tab is built from changes, so a tab made before it is rebuilt once: mod tabs hold no work, and
# a roles tab is rebuilt only if every card in it is idle. 2: lattice tabs read their own machine's feed; roles in a grid.
TABS_V = 2


class AppError(RuntimeError):
    pass


# ---- report: OSC 7501 from a role card ---------------------------------------------------

def in_core_terminal(env=None) -> bool:
    """True inside a herdr-core terminal (the Herdr app's). Only the variable's presence is read."""
    return "HC_TERMINAL_ID" in (os.environ if env is None else env)


def osc7501(state: str, kind: str = "", title: str = "", msg: str = "", app: str = REPORT_APP) -> str:
    """One OSC 7501 status record (herdr-core `status.rs`): fields joined by ':', title and msg base64."""
    fields = [f"state={state}"]
    if kind:
        fields.append(f"kind={kind}")
    if app:
        fields.append(f"app={app}")
    for key, value in (("title", title), ("msg", msg)):
        if value:
            fields.append(f"{key}=" + base64.b64encode(value.encode()).decode())
    return "\x1b]7501;" + ":".join(fields) + "\x1b\\"


OSC_CLEAR = "\x1b]7501;state=clear\x1b\\"
# Set by a card for the length of an attach: under a client the core still names the card as the terminal's foreground.
ATTACH_LABEL = "shoal.attached"
# a blocked card's cause where no report kind says it: herdr-core drops a report whose kind it does not know
# (auth, question and permission are the ones it keeps), so the cause goes in a label the agents list's badge reads
NEED_LABEL = "shoal.need"
# how many ledger asks a card's question badge stands for ("" when its question is an open prompt, or none): the agents
# list's badge opens them in the actions dialog, while a prompt is answered in the session
ASKS_LABEL = "shoal.asks"
ASKS_BADGE_LABEL = "shoal.asks-badge"  # "1" when the badge's question is those asks (not an open prompt): its click opens them
QUESTION_SUBS = ("user-ask", "user-prompt", "user-design", "user-feedback")
# the agents list's sections, from the derived agent state: a card's group and its state there. Both "" leave the row in
# Herdr's own sections, by its reported status (a status without the derived state, or a harness that reports nothing)
GROUP_LABEL = "shoal.group"
STATE_LABEL = "shoal.state"
DETAIL_LABEL = "shoal.detail"  # the row's tooltip: the ask's first line, the blocker or target
DETAIL_MAX = 200
AGENT_GROUPS = ("needs-you", "working", "waiting", "blocked", "held")


def role_agent(r: dict) -> tuple[str, str, str]:
    """(group, state, detail) of a role's row in the agents list: its derived `agent` (`ui_status.role_row`), in the
    Dismissed group once you dismissed it (its group's state still shown). All "" while the status carries no derived
    state."""
    agent = r.get("agent") if isinstance(r.get("agent"), dict) else {}
    group, state = str(agent.get("group") or ""), str(agent.get("state") or "")
    if group not in AGENT_GROUPS:
        return "", "", ""
    detail = " ".join(str(agent.get("detail") or "").split())[:DETAIL_MAX]
    return ("dismissed" if r.get("dismissed") else group), state, detail


SEAT_WAIT_GRACE_S = 600  # a declared wait covers the turn that declared it: one ending later than this lapses it


def _ts(s: str) -> float | None:
    import datetime as _dt
    try:
        return _dt.datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def seat_wait(entry: dict | None, rep: dict | None) -> dict | None:
    """The wait a standalone seat declared (`shoal ui wait`), while it still holds: it covers the turn that declared it,
    so a later turn's end (more than SEAT_WAIT_GRACE_S after it) lapses it unless the seat declares it again, as a role
    writes a fresh sleep status every turn. A harness that reports no turn end keeps it until it is cleared."""
    w = (entry or {}).get("wait")
    if not isinstance(w, dict) or not (w.get("on") or w.get("target")):
        return None
    since, ended = _ts(w.get("since") or ""), _ts((rep or {}).get("ended") or "")
    if since is not None and ended is not None and ended > since + SEAT_WAIT_GRACE_S:
        return None
    return w


def standalone_agent(rep: dict | None, dismissed: bool = False, entry: dict | None = None) -> tuple[str, str, str]:
    """(group, state, detail) of a standalone seat's row, from its card's report (`standalone_report`, or `native_report`
    for Muse and Codex) and its registry `entry`. Working for a turn; needs you at an open question; waiting while its own
    background tasks run (they re-invoke it); blocked or waiting on what it declared (`shoal ui wait`: another session's
    review, a PR); after a reply, needs you (`your-turn`) until you dismiss it (`shoal dismiss --session`), which lapses
    at its next reply (maintainer, 2026-11-11); held when idle or stopped. `dismissed`: closed in the viewer."""
    if rep is None:
        return "", "", ""
    entry = entry or {}
    msg = str(rep.get("msg") or "")
    w = seat_wait(entry, rep)
    if rep["state"] == "working":
        group, state = "working", "working"
    elif rep.get("kind") == "question":
        group, state = "needs-you", "question"
    elif rep.get("bg"):
        group, state = "waiting", "background"
    elif w is not None:
        group, state = ("blocked", "session-blocked") if w.get("on") else ("waiting", str(w.get("sub") or "waiting"))
        msg = " ".join(x for x in (f"on {w['on']}" if w.get("on") else "", str(w.get("target") or ""),
                                   str(w.get("why") or "")) if x)
    elif rep.get("turn") == "blocked":
        ack = entry.get("needs_dismissed") if isinstance(entry.get("needs_dismissed"), dict) else {}
        seen = bool(rep.get("ended")) and ack.get("ended") == rep.get("ended")
        group, state = ("dismissed" if seen else "needs-you"), "your-turn"
    else:
        group, state = "held", {"idle": "idle"}.get(rep.get("turn", ""), "stopped")
    return ("dismissed" if dismissed else group), state, msg[:DETAIL_MAX]


def _seat(entries: list[dict], key: str) -> dict | None:
    """A local registry entry by label, job or session id (prefix)."""
    return next((e for e in entries if not e.get("host") and (key in (e.get("label"), e.get("job"))
                                                             or str(e.get("session", "")).startswith(key))), None)


def cmd_seat_wait(a) -> int:
    """`shoal ui wait <seat> --on <who> [--target T] [--why W] | --clear`: a standalone seat records what it waits on, as a
    role writes `queue --wait`: another session's review (`--on`: blocked, session-blocked), or a target such as a PR or a
    run (waiting). It covers the turn that declares it: declare it again at each turn's end while it still holds."""
    import ui_herdr
    entries = ui_herdr.load_standalone(raw=True)
    e = _seat(entries, a.target)
    if e is None:
        print(f"ui wait: no saved local session named {a.target!r}")
        return 1
    label = e.get("label") or e["session"][:12]
    if a.clear:
        if a.dry_run:
            print(f"[dry-run] {label}: would clear its wait")
            return 0
        had = e.pop("wait", None)
        ui_herdr.save_standalone(entries)
        print(f"{label}: wait cleared" if had else f"{label}: had no wait")
        return 0
    if not (a.on or a.target_ref):
        print("ui wait: say what it waits on: --on <session or role> (another session's review), or --target <kind:ref>")
        return 2
    import datetime as _dt
    w = {"on": a.on or None, "target": a.target_ref or None, "why": a.why or None,
         "sub": "session-blocked" if a.on else (a.sub or "waiting"),
         "since": _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
    w = {k: v for k, v in w.items() if v}
    if a.dry_run:
        print(f"[dry-run] {label}: would record wait {w}")
        return 0
    e["wait"] = w
    ui_herdr.save_standalone(entries)
    print(f"{label}: waits {('on ' + a.on) if a.on else ''} {a.target_ref or ''}".rstrip()
          + " (covers this turn: declare it again at each turn's end while it holds; `--clear` when it ends)")
    return 0


def cmd_seat_dismiss(key: str, undo: bool = False, dry_run: bool = False) -> int:
    """`shoal dismiss --session <seat>`: clear a standalone seat's finished reply (`your-turn`) from needs you until its
    next reply ends."""
    import ui_herdr
    import ui_status
    entries = ui_herdr.load_standalone(raw=True)
    e = _seat(entries, key)
    if e is None:
        print(f"dismiss: no saved local session named {key!r}")
        return 1
    label = e.get("label") or e["session"][:12]
    if undo:
        if not e.get("needs_dismissed"):
            print(f"{label}: nothing to bring back")
            return 1
        if not dry_run:
            e.pop("needs_dismissed", None)
            ui_herdr.save_standalone(entries)
        print(f"{'[dry-run] ' if dry_run else ''}{label}: shows as needing you again if it still does")
        return 0
    if (e.get("harness") or "claude") != "claude":
        print(f"{label}: a {e.get('harness')} seat reports no finished reply, so there is nothing to dismiss")
        return 1
    rep = standalone_report(ui_status.claude_rows(), e["session"], e.get("job", ""), label)
    if rep is None or rep.get("turn") != "blocked" or not rep.get("ended"):
        print(f"{label}: does not need you now (no finished reply waiting), so there is nothing to dismiss")
        return 1
    if dry_run:
        print(f"[dry-run] {label}: would clear its finished reply from needs you until its next reply")
        return 0
    e["needs_dismissed"] = {"ended": rep["ended"]}
    ui_herdr.save_standalone(entries)
    print(f"{label}: dismissed until its next reply")
    return 0


def native_report(harness: str, session: str, label: str) -> dict | None:
    """{state, kind, title, msg, turn} for a Muse or Codex standalone seat, from its harness's status line: working while
    a turn runs (an attached client counts only while its journal moves), else idle. Neither reports a turn's end."""
    try:
        import importlib
        mod = importlib.import_module({"muse": "muse", "codex": "codex_harness"}[harness])
        line = mod.status_line(session)
    except Exception:  # noqa: BLE001 - no report rather than a wrong one
        return None
    busy = "status:busy" in line and ("attached" not in line or " active" in line)
    return {"state": "working" if busy else "idle", "kind": "", "need": "", "title": f"standalone/{label}",
            "msg": "running a turn" if busy else "between turns", "turn": "working" if busy else "idle", "bg": 0, "ended": ""}


def starved(r: dict) -> bool:
    """A fill=assigned role left with an empty queue past the grace, asking nothing: usually deliberate, so the agents list
    reads it as idle rather than needs-you (the lattice's `STARVED_AFTER_S`; accounting keeps `wait:assignment`)."""
    reasons = set(r["can_progress"]["reasons"])
    age = r.get("status_age_s")
    return (r.get("fill") == "assigned" and r.get("sub") == "queue-empty" and not r.get("asks") and not r.get("next")
            and not r.get("grants_pending") and isinstance(age, (int, float)) and age >= STARVED_AFTER_S
            and not reasons & {"blocked-on-question", "blocked-says-waiting", "watcher-dead", "dead-worker"})


def card_report(r: dict, tag: str) -> dict:
    """{state, kind, title, msg} for one role's status row. The state follows the 0.9.3 sidebar rules
    (`ui_herdr.herdr_state`): working for a turn or a job wait, blocked when the role needs you or cannot be
    woken, else idle. A blocked role says why in `kind`, which the app shows: auth for `user-auth`, question for
    an open prompt, an ask or a design/feedback wait, permission for a pending grant."""
    import ui_herdr
    import ui_status
    state = ui_herdr.herdr_state(r)
    if state == "blocked" and starved(r):
        state = "idle"
    # a role whose host cannot be read: what the row holds is its last state, and neither work nor a need can be claimed.
    # By the age of its last good pull: one failed pull keeps the row, or a dropped connection would hide a real need
    away = False
    if r.get("remote_status"):
        import routes
        age = r["remote_status"].get("age_s")
        away = age is None or age > routes.STATUS_CACHE_S * 4
    if away:
        state = "idle"
    reasons = r["can_progress"]["reasons"]
    kind = ""
    if state == "blocked":
        sub = r.get("sub") or ""
        # an open prompt first: it is the thing to answer in the session, whatever the ledger wait says
        if "blocked-on-question" in reasons:
            kind = "question"
        elif sub == "user-auth":
            kind = "auth"
        elif r.get("grants_pending"):
            kind = "permission"
        elif r.get("asks") or r.get("says") or sub in QUESTION_SUBS or "blocked-says-waiting" in reasons:
            kind = "question"
    # what a blocked card with no kind is blocked on, for its badge: a stated reason behind every `!`
    need = ""
    if state == "blocked" and not kind:
        need = ("dead" if "dead-worker" in reasons else "nowatch" if "watcher-dead" in reasons
                else "stuck" if "blocker-role-cannot-take" in reasons else "")
    bits = [ui_status.status_label(r)]
    if away:
        bits = [f"{r.get('host') or 'its host'} cannot be reached; last known: {bits[0]}"]
    w = r.get("wait") or {}
    if ui_herdr.role_mode(r) == ui_herdr.MODE_JOB and not away:
        bits = [f"{ui_herdr.MODE_JOB} {w.get('sub') or r.get('sub')}"]
    if w.get("target") and r.get("status") == "wait":
        bits.append(f"on {str(w['target'])[:60]}")
    if r.get("active"):
        bits.append(str(r["active"])[:60])
    says = r.get("says") or ""
    if says and state == "blocked":
        bits.append(says)
    asks = [str(x) for x in r.get("asks") or [] if str(x).strip()]
    if asks and state == "blocked" and kind == "question" and "blocked-on-question" not in reasons:
        # the badge says a question; the message says which: the asks first, the status after (it is cut at 200)
        each = max(40, (150 - 10) // len(asks))
        bits.insert(0, f"{len(asks)} ask{'s' if len(asks) > 1 else ''}: " + "; ".join(a[:each].rstrip() + ("…" if len(a) > each else "") for a in asks) + " ·")
    # every ask the role has, for the row's right-click; whether they are the badge's question, for its click
    n_asks = len(asks) if asks and state == "blocked" else 0
    asks_badge = bool(n_asks) and kind == "question" and "blocked-on-question" not in reasons
    why = [ui_status.ATTENTION.get(x, x) for x in reasons if x not in ("parked", "dormant") and not (says and x == "blocked-says-waiting")]
    if why and state == "blocked":
        bits.append("(" + ", ".join(why)[:80] + ")")
    # the role's own host in the title: the card's machine says only where the card runs (`/` and `@` never occur in a slug)
    where = f"@{r['host']}" if r.get("host") else ""
    return {"state": state, "kind": kind, "need": need, "asks": n_asks, "asks_badge": asks_badge, "title": f"{tag}/{r['slug']}{where}",
            "msg": " ".join(bits)[:200]}


class DoneLatch:
    """A role's `done`, reported once: when the row's last done is newer than any this card has seen and the role rests
    (would report idle), the card says `done`, which the app shows as finished until someone looks. The card writes that
    report once (`once`), since a written `done` would raise it again after the look; anything else the role does next
    replaces it, and an attach clears it (`looked`): the operator was in the session. What was done before the card
    started is not news."""

    def __init__(self):
        self.seen = None
        self.item = None

    def apply(self, r: dict, rep: dict) -> dict:
        d = r.get("last_done") or {}
        ts = str(d.get("ts") or "")
        if self.seen is None:
            self.seen = ts
        elif ts > self.seen:
            self.seen, self.item = ts, str(d.get("item") or "an item")
        if self.item and rep["state"] != "idle":
            self.item = None
        if not self.item:
            return rep
        return dict(rep, state="done", kind="", need="", msg=f"done {self.item[:60]}; {rep['msg']}"[:200])

    def once(self, rep: dict) -> bool:
        return rep["state"] == "done"

    def looked(self) -> None:
        self.item = None


def standalone_report(rows: list[dict], session: str, job: str, label: str) -> dict | None:
    """{state, kind, title, msg} for a saved standalone Claude session, from the same listing its lattice card reads
    (`claude agents`): working for a turn, blocked on a question only while a prompt is open, else idle. A standalone seat
    is a conversation with you, so the end of each reply reads `blocked` in the listing: that is your turn to type, not a
    question, and as needs-you it buried the seats that do ask (2026-11-08). None when the listing failed."""
    import health as _h
    import ui_status
    from backends import claude_holders
    if not rows:
        return None
    row = next(iter(claude_holders(rows, session, job)), {})
    turn = ui_status.claude_turn(row) if row else "dead"
    state, kind, msg = "idle", "", {"working": "running a turn", "idle": "alive, between turns"}.get(turn, "stopped")
    st = _h.claude_job_state(row) if row else {}
    fl = st.get("inFlight") if isinstance(st.get("inFlight"), dict) else {}
    try:
        bg = int(fl.get("tasks") or 0) + int(fl.get("queued") or 0)
    except (TypeError, ValueError):
        bg = 0
    if turn == "working":
        state = "working"
    elif turn == "blocked" and ui_status.pending_question(row, session):
        state, kind, msg = "blocked", "question", "waits at a question"
    elif bg and turn != "dead":
        # between turns with its own background tasks running (a suite, a CI watch): it is waiting on its own work, which
        # re-invokes it, not held (measured 2026-11-11: a seat running suites read held)
        msg = f"between turns: {bg} background task{'s' if bg > 1 else ''} running"
    elif turn == "blocked":
        msg = "your turn: waiting for a reply"
    # when its last turn ended, so a dismissal lapses at the next reply and a declared wait at a later turn: the job's
    # `lastTerminalAt` (set at done or stopped), else its `updatedAt` (a job waiting for your reply records no terminal
    # time; measured 2026-11-11). An `updatedAt` that moves without a reply only brings a dismissed row back: the safe side
    ended = str(st.get("lastTerminalAt") or st.get("updatedAt") or "") if turn in ("blocked", "idle") else ""
    return {"state": state, "kind": kind, "need": "", "title": f"standalone/{label}", "msg": msg, "turn": turn,
            "bg": bg if turn != "dead" else 0, "ended": ended}


def standalone_text(rep: dict, label: str, latch: DoneLatch, ended: str, last: str | None) -> str:
    """The standalone card's report for this tick, as `report_text` writes a role's: a turn that ended (`ended`, when the
    card saw it) is the `done`, written once."""
    rep = latch.apply({"last_done": {"item": "its turn", "ts": ended} if ended else None}, rep)
    osc = osc7501(rep["state"], rep["kind"], rep["title"], rep["msg"], app=label)
    return "" if latch.once(rep) and osc == last else osc


def report_text(r: dict, tag: str, latch: DoneLatch | None = None, last: str | None = None) -> str:
    """The card's report for this tick: "" when it is a `done` already written (`last`), which must not be written again."""
    rep = card_report(r, tag)
    if latch is not None:
        rep = latch.apply(r, rep)
    # the app heads the pane with the reported `app`, so the role's name is what a window shows, not "shoal"
    osc = osc7501(rep["state"], rep["kind"], rep["title"], rep["msg"], app=r["slug"])
    return "" if latch is not None and latch.once(rep) and osc == last else osc


# ---- plan: what this host's shoals want ---------------------------------------------------

def _cli_argv(*args: str) -> list[str]:
    import ui_herdr
    return ui_herdr.shoal_argv(*args)


def _wrap_env(argv: list[str], env: dict) -> list[str]:
    """`space.new`, `tab.new` and `pane.split` take an argv but no environment: carry it with env(1)."""
    return (["env"] + [f"{k}={v}" for k, v in sorted(env.items())] + argv) if env else argv


def feed_state() -> dict:
    """The graph feed's file on this host and whether it is being written (fresh within FEED_FRESH_S)."""
    import ui_graph
    path = ui_graph.graph_path()
    try:
        fresh = time.time() - os.path.getmtime(path) < FEED_FRESH_S
    except OSError:
        fresh = False
    return {"path": path, "fresh": fresh, "unit": FEED_UNIT}


def lattice_props(sid: str = "", feed: dict | None = None) -> dict:
    """The lattice tab's props: the feed file (cheap: one read per poll) when the feed runs, else one
    `ui graph` run per poll at a slow interval."""
    feed = feed or feed_state()
    if feed["fresh"]:
        p = {"source": {"kind": "file", "path": feed["path"]}, "poll_ms": 5000}
    else:
        argv = ["shoal", "ui", "graph", "--json"] + (["--shoal", sid] if sid else [])
        p = {"source": {"kind": "exec", "argv": argv}, "poll_ms": 15000}
    if sid:
        p["shoal"] = sid
    return p


def _pane(label: str, argv: list[str], env: dict | None = None) -> dict:
    return {"label": label, "argv": _wrap_env(argv, env or {})}


def shoal_space(spec, feed: dict) -> dict:
    import ui_herdr
    import ui_status
    env = ui_herdr.pane_env(spec)
    tag = ui_herdr.shoal_tag(spec.sid)
    panes = [_pane(f"{spec.sid}/overview", _cli_argv("ui", "dash", "--shoal", spec.sid), env)]
    if ui_status.ui_config().get("overview_ledger") is True:  # hidden by default: the dashboard's `l` shows it on demand
        panes.append(_pane(f"{spec.sid}/ledger", _cli_argv("ui", "tail", "--shoal", spec.sid), env))
    tabs = [{"name": "overview", "layout": "stack", "panes": panes}]
    for name, chunk in ui_herdr.role_tabs(spec):
        if chunk:
            tabs.append({"name": name, "layout": "grid",
                         "panes": [_pane(ui_herdr.role_label(spec.sid, r.slug),
                                         _cli_argv("view", spec.sid, r.slug, "--stub"), env) for r in chunk]})
    # no lattice tab here: the top bar's Lattice opens the global one, drilled into this shoal (2026-11-08)
    return {"name": tag, "kind": "shoal", "shoal": spec.sid, "folder": spec.dir or "", "tabs": tabs}


def standalone_space(e: dict) -> dict:
    import ui_herdr
    ui_herdr.shoal_tag("")  # load the tags, so a colliding name gets its suffix
    return {"name": ui_herdr.standalone_ws_label(e), "kind": "standalone", "standalone": ui_herdr.standalone_label(e),
            "folder": e.get("cwd") or "",
            "tabs": [{"name": "session", "layout": "stack",
                      "panes": [_pane(ui_herdr.standalone_label(e), ui_herdr.standalone_argv(e))]}]}


def home_here(spec) -> bool:
    """True when this host is the shoal's home (its spec `home`; none set counts as here). Every host reads
    every shoal through the shared ledger, so a space per shoal on every machine duplicated every card."""
    import config as _config
    home = getattr(spec, "home", "") or ""
    return not home or home == (_config.server() or os.uname().nodename.split(".")[0])


def plan(ids: list[str] | None = None, ledger_root: str = "", standalone: bool = True) -> dict:
    """This host's wanted spaces: the shoals whose home it is (any shoal named in `ids`), and its saved
    standalone sessions. Hidden shoals are left out unless named; retired ones never appear."""
    import ui_herdr
    import ui_launch
    import ui_status
    from roles import ShoalSpec
    feed = feed_state()
    hidden = set() if ids else ui_status.hidden_shoals()
    spaces, notes = [], []
    for p in ui_status.discover_all(ledger_root, ids or None):
        try:
            spec = ShoalSpec.load(p)
            spec.path = p
            if spec.sid in hidden:
                continue
            if not ids and not home_here(spec):
                notes.append(f"{spec.sid}: its home is {spec.home} (open it there, or name it with --shoal)")
                continue
            spaces.append(shoal_space(spec, feed))
        except Exception as exc:  # noqa: BLE001 - one broken spec never hides the others
            notes.append(f"{p}: {exc}")
    if standalone and not ids:
        spaces += [standalone_space(e) for e in ui_herdr.local_standalone()]
    if not feed["fresh"]:
        notes.append(f"graph feed not running here: lattice tabs poll `ui graph` slowly (`shoal ui app-install` "
                     f"starts {FEED_UNIT})")
    return {"v": PLAN_V, "kind": "shoal-app-plan", "host": ui_launch.this_server(), "feed": feed,
            "spaces": spaces, "notes": notes}


def cmd_app_plan(a) -> int:
    doc = plan(a.ids, getattr(a, "ledger_root", ""), not a.no_standalone)
    if a.json:
        print(json.dumps(doc, indent=1))
        return 0
    for s in doc["spaces"]:
        print(f"space {s['name']} ({s['kind']}): " + ", ".join(
            f"{t['name']} [{t['mod'] if t.get('mod') else len(t['panes'])}]" for t in s["tabs"]))
    for n in doc["notes"]:
        print("note: " + n)
    return 0


# ---- the app: herdr-app and hc -------------------------------------------------------------

def app_bin() -> str:
    return config.env("HERDR_APP_BIN") or shutil.which("herdr-app") or os.path.expanduser("~/.local/bin/herdr-app")


def hc_bin() -> str:
    return config.env("HC_BIN") or shutil.which("hc") or os.path.expanduser("~/.local/bin/hc")


def app_home() -> str:
    return os.environ.get("HERDR_APP_HOME") or os.path.expanduser("~/.config/herdr-app")


def _run_json(argv: list[str], timeout: float = 30) -> dict:
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AppError(f"{os.path.basename(argv[0])}: {exc}") from exc
    try:
        doc = json.loads(r.stdout or "{}")
    except ValueError:
        doc = {}
    if r.returncode or (isinstance(doc, dict) and doc.get("error")):
        err = (doc.get("error") if isinstance(doc, dict) else "") or (r.stderr or r.stdout).strip()[-300:]
        raise AppError(f"{' '.join(argv[1:3])}: {err}")
    return doc if isinstance(doc, dict) else {"value": doc}


def app_ls() -> dict:
    return _run_json([app_bin(), "ls"])


CONNECT_WAIT_S = 30


def app_do(command: str, args: dict) -> dict:
    """One app command. A machine added a moment ago is still connecting (`<machine> is connecting`), so a
    structural command on it is retried for up to CONNECT_WAIT_S."""
    argv = [app_bin(), "do", command, json.dumps({k: v for k, v in args.items() if v is not None})]
    deadline = time.time() + CONNECT_WAIT_S
    while True:
        try:
            return _run_json(argv)
        except AppError as exc:
            if "is connecting" not in str(exc) or time.time() > deadline:
                raise
            time.sleep(1)


def app_running() -> bool:
    try:
        return subprocess.run([app_bin(), "status"], capture_output=True, timeout=10,
                              stdin=subprocess.DEVNULL).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def hc_get(address: str) -> dict:
    return _run_json([hc_bin(), "get", address, "--json"], timeout=20)


def hc_label(address: str, labels: dict) -> None:
    _run_json([hc_bin(), "label", address] + [f"{k}={v}" for k, v in labels.items()] + ["--json"], timeout=20)


def machines() -> list[str]:
    try:
        return [str(m.get("name")) for m in _run_json([hc_bin(), "machine", "ls", "--json"]).get("machines", [])
                if m.get("name") and not m.get("temporary")]
    except AppError:
        return []


def seat_ids(argv) -> tuple[str, str] | None:
    """(harness, session) a standalone card's `view --harness H --session S` argv names; None for anything else (a
    role card, a harness client in the foreground)."""
    argv = [str(x) for x in argv or []]
    if "view" not in argv or "--harness" not in argv or "--session" not in argv:
        return None
    i, j = argv.index("--harness"), argv.index("--session")
    return (argv[i + 1], argv[j + 1]) if i + 1 < len(argv) and j + 1 < len(argv) else None


def foreground_kind(t: dict) -> str:
    """'shell' (an idle shell: the pane may be replaced), 'card' (a shoal card, dash, tail or view), or 'other'
    (anything else, such as an attached harness client: never replaced). The same allowlist as 0.9.3."""
    import ui_herdr
    fg = t.get("foreground") or {}
    name = os.path.basename(str(fg.get("name") or ""))
    argv = [str(x) for x in (fg.get("argv") or [])]
    if name.lstrip("-") in ui_herdr.SHELLS and all(x.startswith("-") for x in argv[1:]):
        return "shell"
    if any(x.endswith(("/cli.py", "/shoal-ui")) for x in argv) and any(x in ("view", "ui") for x in argv):
        return "card"
    return "other"


# ---- reconcile: add what the plan wants and the app lacks ----------------------------------

def _pane_of(ls: dict, terminal: str) -> str | None:
    for s in ls.get("spaces", []):
        for t in s.get("tabs", []):
            for x in t.get("terminals", []):
                if x.get("terminal") == terminal:
                    return x.get("pane")
    return None


class Reconciler:
    """Adds the plan's missing spaces, tabs and panes in the running app (`herdr-app do`). `dry_run` only says
    what it would do. Every terminal it starts is labelled `shoal.pane=<label>` so the next run finds it."""

    def __init__(self, machine: str | None, dry_run: bool = False):
        self.machine = machine or None
        self.dry_run = dry_run
        self.out: list[str] = []
        self.mods = {m for m in MODS if os.path.exists(os.path.join(app_home(), "mods", m))}

    def say(self, line: str) -> None:
        self.out.append(("[dry-run] " if self.dry_run else "") + line)

    def _label(self, terminal: str, label: str) -> None:
        try:
            hc_label(terminal, {PANE_LABEL: label})
        except AppError as exc:
            self.say(f"label {terminal}: {exc}")

    def _split(self, pane: str, direction: str, p: dict) -> str:
        r = app_do("pane.split", {"direction": direction, "pane": pane, "argv": p["argv"]})
        if r.get("terminal"):
            self._label(r["terminal"], p["label"])
        return r.get("pane") or ""

    def _fill(self, first_pane: str, layout: str, panes: list[dict]) -> None:
        """Lay `panes` out beside `first_pane`: a stack (each split down from the last), or a grid of two
        columns once there are four or more (the 0.9.3 `_grid` shape)."""
        if not panes:
            return
        if layout != "grid" or len(panes) + 1 <= 3:
            last = first_pane
            for p in panes:
                last = self._split(last, "down", p) or last
            return
        n = len(panes) + 1
        mid = (n + 1) // 2
        left, right = panes[:mid - 1], panes[mid - 1:]
        head = self._split(first_pane, "right", right[0])
        last = first_pane
        for p in left:
            last = self._split(last, "down", p) or last
        last = head
        for p in right[1:]:
            last = self._split(last, "down", p) or last

    def _mod_props(self, tab: dict) -> dict:
        """A mod runs in the app, wherever its space's terminals run, so its source must name that machine: without
        `on` a lattice tab in a space on another machine reads this computer's feed."""
        props = dict(tab.get("props") or {})
        src = props.get("source")
        if self.machine and isinstance(src, dict) and src.get("kind") in ("file", "exec") and not src.get("on"):
            props["source"] = dict(src, on=self.machine)
        return props

    def _new_tab(self, space_id: str, tab: dict) -> None:
        if tab.get("mod"):
            if tab["mod"] not in self.mods:
                self.say(f"tab {tab['name']}: mod {tab['mod']} is not installed here (`shoal ui app-install`)")
                return
            self.say(f"tab {tab['name']} ({tab['mod']})")
            if not self.dry_run:
                app_do("tab.new", {"space": space_id, "name": tab["name"], "mod": tab["mod"], "props": self._mod_props(tab)})
            return
        self.say(f"tab {tab['name']}: {len(tab['panes'])} pane(s)")
        if self.dry_run:
            return
        first = tab["panes"][0]
        r = app_do("tab.new", {"space": space_id, "name": tab["name"], "argv": first["argv"]})
        if r.get("terminal"):
            self._label(r["terminal"], first["label"])
        pane = r.get("pane") or _pane_of(app_ls(), r.get("terminal", ""))
        if pane:
            self._fill(pane, tab.get("layout", "stack"), tab["panes"][1:])

    def _new_space(self, s: dict) -> str | None:
        first_tab = next((t for t in s["tabs"] if not t.get("mod")), None)
        self.say(f"space {s['name']}" + (f" on {self.machine}" if self.machine else ""))
        if self.dry_run:
            for t in s["tabs"]:
                if t is first_tab:
                    self.say(f"tab {t['name']}: {len(t['panes'])} pane(s)")
                else:
                    self._new_tab("", t)
            return None
        args = {"name": s["name"], "folder": s.get("folder") or None, "machine": self.machine or "",
                "tab": first_tab["name"] if first_tab else None,
                "argv": first_tab["panes"][0]["argv"] if first_tab else None}
        r = app_do("space.new", args)
        space_id = r.get("space") or ""
        if first_tab is not None:
            if r.get("terminal"):
                self._label(r["terminal"], first_tab["panes"][0]["label"])
            pane = r.get("pane") or _pane_of(app_ls(), r.get("terminal", ""))
            if pane:
                self._fill(pane, first_tab.get("layout", "stack"), first_tab["panes"][1:])
        for t in s["tabs"]:
            if t is not first_tab:
                self._new_tab(space_id, t)
        self.select_tab(space_id, s.get("select") or s["tabs"][0]["name"])
        return space_id

    def _relayout(self, s: dict, got: dict, built: dict, sync: bool = False) -> bool:
        """Rebuild a shoal's roles tabs when they no longer hold the planned roles, or were built before TABS_V (a
        tab grown one pane at a time is a stack, not a grid). A card is only a viewer, so closing it stops nothing, but
        one in use (an attached session, or anything else that is not a card), or one that cannot be read (a machine out of
        reach, whose tabs may hold anything), keeps every tab as it is. The quiet `sync` leaves a space with no roles tab
        alone: someone closed them. True when it dealt with the roles tabs."""
        planned = [t for t in s["tabs"] if ROLE_TAB.match(t["name"]) and not t.get("mod")]
        existing = [t for t in got.get("tabs", []) if ROLE_TAB.match(t.get("name") or "")]
        if not planned and not existing:
            return False
        if sync and not existing:
            return True
        have: dict[str, set[str]] = {}
        busy = False
        for tab in existing:
            labels = set()
            for x in tab.get("terminals", []):
                try:
                    t = hc_get(x["terminal"])
                except AppError:
                    busy = True
                    continue
                lab = (t.get("labels") or {}).get(PANE_LABEL)
                if lab:
                    labels.add(lab)
                busy = busy or not lab or foreground_kind(t) == "other" or bool((t.get("labels") or {}).get(ATTACH_LABEL))
            have[tab["name"]] = labels
        want = {t["name"]: {p["label"] for p in t["panes"]} for t in planned}
        key = f"{self.machine or 'local'}/{s['name']}"
        if have == want and built.get(key) == TABS_V:
            return False
        if busy:
            if have != want:
                self.say(f"{s['name']}: roles changed, but a card is in use: tabs left as they are until it is detached")
            return True
        self.say(f"{s['name']}: lay the roles out again ({len(planned)} tab(s))")
        if self.dry_run:
            return True
        for tab in existing:
            app_do("tab.close", {"id": tab["id"], "confirmed": True})
        for t in planned:
            self._new_tab(got["id"], t)
        names = [t["name"] for t in s["tabs"]]
        space = next((x for x in app_ls().get("spaces", []) if x.get("id") == got["id"]), None)
        tabs = {t.get("name"): t for t in (space or {}).get("tabs", [])}
        for i, name in enumerate((n for n in names if n in tabs), 1):  # new tabs land last: back to the planned order
            app_do("tab.move", {"id": tabs[name]["id"], "index": i})
        built[key] = TABS_V
        return True

    def _refresh_mod(self, space_id: str, tab_doc: dict, t: dict, built: dict, space_name: str) -> None:
        """Rebuild a mod tab made before TABS_V: its props are fixed when it is made."""
        key = f"{self.machine or 'local'}/{space_name}/{t['name']}"
        if built.get(key) == TABS_V:
            return
        self.say(f"{space_name}/{t['name']}: rebuild ({t['mod']})")
        if self.dry_run or t["mod"] not in self.mods:
            return
        app_do("tab.close", {"id": tab_doc["id"], "confirmed": True})
        self._new_tab(space_id, t)
        built[key] = TABS_V

    def _drop_ledger(self, space: dict, tab_doc: dict, tab: dict) -> None:
        """Close a ledger tail pane the plan no longer has: a tab made before the overview hid it, or the setting turned off."""
        if len(tab_doc.get("terminals", [])) < 2:
            return
        want = {p["label"] for p in tab["panes"]}
        for x in tab_doc["terminals"]:
            try:
                t = hc_get(x["terminal"])
            except AppError:
                continue
            lab = (t.get("labels") or {}).get(PANE_LABEL) or ""
            if lab.endswith("/ledger") and lab not in want and foreground_kind(t) != "other":  # not one a person is using
                self.say(f"{space['name']}/{tab['name']}: close {lab}")
                if not self.dry_run:
                    app_do("pane.close", {"pane": x["pane"]})

    def select_tab(self, space_id: str, name: str) -> None:
        """Show the named tab of a space (the last tab made is the one shown otherwise)."""
        if self.dry_run or not space_id:
            return
        space = next((x for x in app_ls().get("spaces", []) if x.get("id") == space_id), None)
        tab = next((t for t in (space or {}).get("tabs", []) if t.get("name") == name), None)
        if tab is not None:
            app_do("tab.select", {"id": tab["id"]})

    def _gaps(self, space: dict, tab_doc: dict, tab: dict, moved_only: bool = False) -> None:
        """Fill one existing terminal tab: a planned pane with no live terminal gets one; a planned pane whose
        terminal came back as a bare shell (a lost terminal, or a card left with `q`) is replaced, and so is a
        standalone card at its own screen that still shows the harness or session it was opened with after its seat
        moved (measured 2026-11-10: a seat handed from Codex to Claude kept its Codex card on another machine's app
        until the tab was rebuilt by hand). A card with a harness client open in it is left as it is."""
        have: dict[str, tuple[str, str]] = {}
        moved: set[str] = set()
        panes = []
        for x in tab_doc.get("terminals", []):
            try:
                t = hc_get(x["terminal"])
            except AppError:
                continue
            panes.append(x["pane"])
            lab = (t.get("labels") or {}).get(PANE_LABEL)
            if lab:
                have[lab] = (x["pane"], foreground_kind(t))
                want = next((p for p in tab["panes"] if p["label"] == lab), None)
                if want is not None and seat_ids((t.get("foreground") or {}).get("argv") or []) not in \
                        (None, seat_ids(want["argv"])):
                    moved.add(lab)
        # the quiet sync replaces only a moved seat's card: a missing pane or a shell left with `q` waits for app-open
        missing = [] if moved_only else [p for p in tab["panes"] if p["label"] not in have]
        stale = [p for p in tab["panes"] if p["label"] in moved or (not moved_only and have.get(p["label"], ("", ""))[1] == "shell")]
        for p in stale:
            why = "its seat moved to another session" if p["label"] in moved else "its pane is a bare shell"
            self.say(f"{space['name']}/{tab['name']}: restart {p['label']} ({why})")
        for p in missing:
            self.say(f"{space['name']}/{tab['name']}: add {p['label']}")
        if self.dry_run or not (missing or stale):
            return
        for p in stale:
            pane = have[p["label"]][0]
            new = self._split(pane, "down", p)
            if new:
                app_do("pane.close", {"pane": pane})
                panes = [new if q == pane else q for q in panes]
        last = panes[-1] if panes else ""
        for p in missing:
            if not last:
                break
            last = self._split(last, "down", p) or last

    def apply(self, doc: dict, focus: bool = True, show: set[str] | None = None, sync: bool = False) -> list[str]:
        """`sync`: the status bar's quiet pass: only roles tabs follow the roles, and what a person closed stays closed."""
        ls = app_ls()
        mine = [s for s in ls.get("spaces", []) if (s.get("machine") or None) == self.machine]
        opened = load_opened()
        key = self.machine or "local"
        seen = set(opened.get(key, []))
        built = dict(opened.get("_built") or {})
        first = None
        for s in doc.get("spaces", []):
            got = next((x for x in mine if x.get("name") == s["name"]), None)
            lab = s.get("standalone")
            if got is None and lab and lab in seen and lab not in (show or set()) and \
                    s["name"] not in (show or set()):
                # opened here before and gone now: closed in the app on purpose, so it stays closed
                self.say(f"standalone {s['name']}: closed in the app, left closed (`--show {s['name']}` reopens it)")
                continue
            if lab:
                seen.add(lab)
            if got is None:
                if sync:
                    continue
                sid = self._new_space(s)
                first = first or sid
                if not self.dry_run:
                    built[f"{key}/{s['name']}"] = TABS_V
                    built.update({f"{key}/{s['name']}/{t['name']}": TABS_V for t in s["tabs"] if t.get("mod")})
                continue
            first = first or got["id"]
            tabs = {t.get("name"): t for t in got.get("tabs", [])}
            relaid = s.get("kind") == "shoal" and self._relayout(s, got, built, sync)
            for t in s["tabs"]:
                if relaid and ROLE_TAB.match(t["name"]) and not t.get("mod"):
                    continue
                if t["name"] not in tabs:
                    if not sync:
                        self._new_tab(got["id"], t)
                        if t.get("mod") and not self.dry_run:
                            built[f"{key}/{s['name']}/{t['name']}"] = TABS_V
                elif t.get("mod"):
                    self._refresh_mod(got["id"], tabs[t["name"]], t, built, s["name"])
                elif not sync or s.get("kind") == "standalone":
                    self._gaps(s, tabs[t["name"]], t, moved_only=sync)
                if t["name"] == "overview" and t["name"] in tabs:
                    self._drop_ledger(s, tabs[t["name"]], t)
            # a lattice tab shoal made here before the top bar's Lattice replaced them: closed, once
            lt = tabs.get("lattice")
            if lt is not None and not any(t["name"] == "lattice" for t in s["tabs"]) and built.get(f"{key}/{s['name']}/lattice"):
                self.say(f"{s['name']}: close its lattice tab (the top bar's Lattice opens the global one on this shoal)")
                if not self.dry_run:
                    app_do("tab.close", {"id": lt["id"], "confirmed": True})
                    built.pop(f"{key}/{s['name']}/lattice", None)
        planned = {s["name"] for s in doc.get("spaces", [])}
        import ui_herdr
        tags = set(ui_herdr._TAGS.values()) if self.machine is None else set()
        for x in mine:
            if x.get("name") in tags and x["name"] not in planned:
                self.say(f"space {x['name']}: its shoal is hidden or retired (close it in the app when done)")
        if focus and first and not self.dry_run:
            app_do("space.select", {"id": first})
        if not self.dry_run and (seen != set(opened.get(key, [])) or built != (opened.get("_built") or {})):
            opened[key] = sorted(seen)
            opened["_built"] = built
            save_opened(opened)
        return self.out


# ---- app-open, app-start, app-install ------------------------------------------------------

def _state_path() -> str:
    base = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    return os.path.join(base, "shoal", "app-open.json")


def load_opened() -> dict:
    """{machine: [standalone labels opened in the app]}: the app's own record (the 0.9.3 registry flags stay the
    0.9.3 viewer's), so a standalone space closed in the app is not reopened."""
    try:
        d = json.load(open(_state_path()))
        return d.get("opened", {}) if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def save_opened(opened: dict) -> None:
    p = _state_path()
    try:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p + ".tmp", "w") as fh:
            json.dump({"opened": opened}, fh)
        os.replace(p + ".tmp", p)
    except OSError:
        pass


def remote_plan(server: str, ids: list[str], standalone: bool) -> dict:
    import routes
    r = routes.route(server)
    if r is None:
        raise AppError(f"{server}: no route in [hosts.{server}] (the host config)")
    args = ["ui", "app-plan", "--json"] + [x for i in ids for x in ("--shoal", i)] + ([] if standalone else ["--no-standalone"])
    try:
        p = subprocess.run(routes.ssh_argv(r) + [routes.remote_cmd(r, *args)], capture_output=True, text=True,
                           timeout=60, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AppError(f"{server}: {exc}") from exc
    if p.returncode == 255:
        raise AppError(f"{server} is unreachable now (ssh exit 255): `shoal doctor` checks its route")
    try:
        doc = json.loads(p.stdout)
    except ValueError as exc:
        raise AppError(f"{server}: no plan (exit {p.returncode}; is its shoal current? {p.stderr.strip()[-200:]})") from exc
    if doc.get("kind") != "shoal-app-plan":
        raise AppError(f"{server}: unexpected plan document")
    return doc


def ensure_machine(server: str, dry_run: bool = False) -> str:
    """The herdr-core machine for a route, added once as `hc machine add <route ssh> --name <server>` (the route
    names the machine, so there is no second list)."""
    import routes
    if server in machines():
        return server
    r = routes.route(server)
    if r is None:
        raise AppError(f"{server}: no route, so no machine to add")
    if dry_run:
        return server
    _run_json([hc_bin(), "machine", "add", str(r["ssh"]), "--name", server, "--json"], timeout=90)
    return server


def open_app(choice: str, ids: list[str] | None = None, dry_run: bool = False, focus: bool = True,
             standalone: bool = True, show: set[str] | None = None, ledger_root: str = "",
             sync: bool = False) -> tuple[int, list[str]]:
    import ui_launch
    ids = ids or []
    out: list[str] = []
    if not dry_run and not app_running():
        return 1, ["the Herdr app is not running here (`shoal ui app-start` starts it with the pin held)"]
    try:
        if ui_launch.is_local(choice):
            machine, doc = None, plan(ids, ledger_root, standalone)
        else:
            known = choice in machines()
            machine = ensure_machine(choice, dry_run)
            if not known:
                out.append(("[dry-run] " if dry_run else "") + f"add machine {machine} (its route)")
            doc = remote_plan(choice, ids, standalone)
        out += [f"note ({doc.get('host')}): {n}" for n in doc.get("notes", [])]
        if dry_run and not app_running():
            out += [f"[dry-run] space {s['name']}: " + ", ".join(t["name"] for t in s["tabs"]) for s in doc["spaces"]]
            return 0, out
        out += Reconciler(machine, dry_run).apply(doc, focus, show, sync)
    except AppError as exc:
        return 2, out + [str(exc)]
    if not dry_run and not sync:
        ui_launch.remember("local" if ui_launch.is_local(choice) else choice)
    return 0, out


def cmd_app_open(a) -> int:
    choice = a.machine or "local"
    rc, lines = open_app(choice, a.ids, a.dry_run, not a.no_focus, not a.no_standalone,
                         {x.strip() for x in a.show or []}, a.ledger_root)
    for line in lines:
        print(line)
    if rc == 0 and not [x for x in lines if not x.startswith("note ")]:
        print(f"{choice}: every planned space is open")
    return rc


def cmd_app_sync(_a) -> int:
    """One quiet pass over this computer and every machine (the shoal-status mod runs it on a slow timer): roles tabs
    follow added and removed roles, nothing takes the focus, and a machine out of reach is only noted."""
    if not app_running():
        return 0
    os.makedirs(os.path.dirname(_state_path()), exist_ok=True)
    with open(_state_path() + ".sync-lock", "a") as lock:
        try:  # a pass does ssh per machine and rebuilds tabs: a timer firing during a slow one, or a second bar, waits its turn
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return 0
        for choice in ["local"] + machines():
            _rc, lines = open_app(choice, focus=False, sync=True)
            for line in lines:
                if not line.startswith("note "):
                    print(f"{choice}: {line}")
    return 0


def global_lattice_props() -> dict:
    """The global lattice's props on this computer. A host that is no shoal's home runs nothing that pulls its ledger
    clone (the feed never does), so its own feed goes stale: it reads a shoal home's feed instead, the machine it last
    opened first. A host with a shoal of its own, or no machine in reach, reads its own."""
    import ui_launch
    try:
        if not any(s.get("kind") == "shoal" for s in plan(standalone=False).get("spaces", [])):
            names = machines()
            last = ui_launch.last_choice()
            for m in ([last] if last in names else []) + [n for n in names if n != last]:
                try:
                    props = lattice_props("", remote_plan(m, [], False)["feed"])
                except AppError:
                    continue
                return dict(props, source=dict(props["source"], on=m))
    except AppError:
        pass
    return lattice_props()


def open_lattice(dry_run: bool = False) -> list[str]:
    """Show a lattice from anywhere: the tab of the shoal space in view (reopened if it was closed), else the global
    one. Closing a tab hides it from the quiet sync; this and `ui app-open` bring it back."""
    cur = next((s for s in app_ls().get("spaces", []) if s.get("active")), None)
    if cur is not None:
        tab = next((t for t in cur.get("tabs", []) if t.get("name") == "lattice"), None)
        rec = Reconciler(cur.get("machine") or None, dry_run)
        if tab is not None:
            if not dry_run:
                app_do("tab.select", {"id": tab["id"]})
            return [f"{cur['name']}: lattice"]
        try:
            doc = remote_plan(cur["machine"], [], False) if cur.get("machine") else plan(standalone=False)
        except AppError:
            doc = {}
        want = next((s for s in doc.get("spaces", []) if s.get("name") == cur.get("name")), None)
        t = next((x for x in (want or {}).get("tabs", []) if x.get("mod") == "lattice-view"), None)
        if t is not None:
            rec._new_tab(cur["id"], t)
            rec.select_tab(cur["id"], "lattice")
            return rec.out
    return home_space(dry_run, select="lattice")


def cmd_app_lattice(a) -> int:
    if not app_running():
        print("the Herdr app is not running here (`shoal ui app-start` starts it with the pin held)")
        return 1
    try:
        for line in open_lattice(a.dry_run):
            print(line)
    except AppError as exc:
        print(str(exc))
        return 2
    return 0


def cmd_app_home(a) -> int:
    if not app_running():
        print("the Herdr app is not running here (`shoal ui app-start` starts it with the pin held)")
        return 1
    try:
        for line in home_space(a.dry_run):
            print(line)
    except AppError as exc:
        print(str(exc))
        return 2
    return 0


def start_argv() -> list[str]:
    """How the app is started: a transient user unit (so it outlives this terminal), with the pin held and only
    the display variables passed through (names chosen here; nothing else from the environment)."""
    env = {"HERDR_RELEASES_URL": DEAD_RELEASES_URL}
    for k in ("DISPLAY", "WAYLAND_DISPLAY", "XDG_RUNTIME_DIR", "XDG_SESSION_TYPE", "HERDR_APP_HOME"):
        if os.environ.get(k):
            env[k] = os.environ[k]
    if shutil.which("systemd-run"):
        return (["systemd-run", "--user", "--collect", f"--unit={APP_UNIT}"] + [f"--setenv={k}={v}" for k, v in env.items()]
                + [app_bin()])
    return ["env"] + [f"{k}={v}" for k, v in env.items()] + [app_bin()]


HOME_SPACE = "SHOAL-home"
HOME_TABS = ("home", "lattice", "board", "usage")  # what the top bar's Home, Lattice, Board and Usage open
LAUNCH_TAB = "launch"  # the terminal a new space has to start with: closed once the mod tabs are there


def _home_current(space: dict) -> bool:
    """Every home tab is there and made at TABS_V, and the starting terminal is gone, so there is nothing to reconcile."""
    built = load_opened().get("_built") or {}
    have = {t.get("name") for t in space.get("tabs", [])}
    return LAUNCH_TAB not in have and all(n in have and built.get(f"local/{HOME_SPACE}/{n}") == TABS_V for n in HOME_TABS)


def home_space(dry_run: bool = False, select: str = "home") -> list[str]:
    """A `SHOAL-home` space on this machine with the home screen (the shoal-home mod, else the terminal chooser in app
    mode), the global lattice and the usage dashboard. The sidebar does not list it (shoal's copy of the spaces mod
    leaves it out): the top bar's Home, Lattice and Usage are the way in."""
    ls = app_ls()
    local = [s for s in ls.get("spaces", []) if not s.get("machine")]
    got = next((s for s in local if s.get("name") == HOME_SPACE), None)
    if got is not None and _home_current(got):  # go straight there: the lattice's props below can ask every machine
        tab = next((t for t in got.get("tabs", []) if t.get("name") == select), None)
        if not dry_run:
            app_do("space.select", {"id": got["id"]})
            if tab is not None:
                app_do("tab.select", {"id": tab["id"]})
        return [f"{HOME_SPACE}: {select}"]
    old = next((s for s in local if s.get("name") == "shoal"), None)  # its earlier name
    if got is None and old is not None:
        if not dry_run:
            app_do("space.rename", {"id": old["id"], "name": HOME_SPACE})
        got = dict(old, name=HOME_SPACE)
    rec = Reconciler(None, dry_run)
    tabs = []
    if "shoal-home" in rec.mods:
        tabs.append({"name": "home", "mod": "shoal-home", "props": {}})
    else:
        tabs.append({"name": "home", "layout": "stack", "panes": [_pane("shoal/home", _cli_argv("ui", "launch"))]})
    tabs.append({"name": "lattice", "mod": "lattice-view", "props": global_lattice_props()})
    if "shoal-board" in rec.mods:  # it reads the boards from the first machine with a digest
        tabs.append({"name": "board", "mod": "shoal-board", "props": {}})
    if "shoal-usage" in rec.mods:
        tabs.append({"name": "usage", "mod": "shoal-usage", "props": {}})
    if got is None:
        if tabs[0].get("mod"):
            # a space starts with a terminal: the chooser, closed below once the mod tabs are made
            tabs.insert(0, {"name": LAUNCH_TAB, "layout": "stack",
                            "panes": [_pane("shoal/home", _cli_argv("ui", "launch"))]})
        rec.apply({"spaces": [{"name": HOME_SPACE, "kind": "home", "folder": "", "tabs": tabs, "select": select}]},
                  focus=True)
    else:
        # `apply` makes the missing tabs and rebuilds a lattice tab older than TABS_V, which points it at the live feed
        rec.apply({"spaces": [{"name": HOME_SPACE, "kind": "home", "folder": "", "tabs": tabs}]}, focus=False)
        if not dry_run:
            app_do("space.select", {"id": got["id"]})
            rec.select_tab(got["id"], select)
    _close_launch(rec, dry_run)
    return rec.out


def _close_launch(rec: "Reconciler", dry_run: bool) -> None:
    """Close the home space's starting terminal once a mod tab stands in for it: Home is the chooser now."""
    space = next((s for s in app_ls().get("spaces", []) if not s.get("machine") and s.get("name") == HOME_SPACE), None)
    tabs = (space or {}).get("tabs", [])
    launch = next((t for t in tabs if t.get("name") == LAUNCH_TAB), None)
    if launch is None or not any(t.get("name") == "home" for t in tabs):
        return
    rec.say(f"{HOME_SPACE}: close its {LAUNCH_TAB} tab (Home is the chooser)")
    if not dry_run:
        app_do("tab.close", {"id": launch["id"], "confirmed": True})


def cmd_app_board(a) -> int:
    if not app_running():
        print("the Herdr app is not running here (`shoal ui app-start` starts it with the pin held)")
        return 1
    try:
        for line in home_space(a.dry_run, select="board"):
            print(line)
    except AppError as exc:
        print(str(exc))
        return 2
    return 0


def cmd_app_usage(a) -> int:
    if not app_running():
        print("the Herdr app is not running here (`shoal ui app-start` starts it with the pin held)")
        return 1
    try:
        for line in home_space(a.dry_run, select="usage"):
            print(line)
    except AppError as exc:
        print(str(exc))
        return 2
    return 0


def cmd_app_start(a) -> int:
    if a.write_desktop:
        path = desktop_path()
        text = desktop_text()
        if a.dry_run:
            print(f"[dry-run] write {path}\n{text}")
        else:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            backup = path + ".bak-shoal"
            if os.path.exists(path) and not os.path.exists(backup):  # one time: the entry Herdr's installer wrote
                shutil.copy2(path, backup)
            with open(path + ".tmp", "w") as fh:
                fh.write(text)
            os.replace(path + ".tmp", path)
            print(f"wrote {path}")
    if not os.path.exists(app_bin()):
        print(f"herdr-app is not installed here (pin {APP_PIN}); install it, then `shoal ui doctor`")
        return 1
    if not app_running():
        argv = start_argv()
        if a.dry_run:
            print("[dry-run] " + shlex.join(argv))
            return 0
        subprocess.run(argv, capture_output=True, stdin=subprocess.DEVNULL)
        for _ in range(60):
            if app_running():
                break
            time.sleep(0.5)
        else:
            print("the Herdr app did not come up within 30 s (`journalctl --user -u shoal-herdr-app` says why)")
            return 1
        print(f"started the Herdr app (release feed held at {DEAD_RELEASES_URL}: no self-update)")
    try:
        for line in home_space(a.dry_run):
            print(line)
    except AppError as exc:
        print(f"home space: {exc}")
        return 1
    return 0


def desktop_path() -> str:
    """The app's own entry, replaced: a desktop shell matches a window to the entry named after its app id, so only this name
    pins and groups with the window. It also means no launcher here starts the app without the pin held."""
    base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return os.path.join(base, "applications", "herdr-app.desktop")


def desktop_icon() -> str:
    """The `shoal` icon in the user's icon theme (`install_icon`, also the name the 0.9.3 launcher uses), else the icon file."""
    base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    if glob.glob(os.path.join(base, "icons", "hicolor", "*", "apps", "shoal.*")):
        return "shoal"
    return os.path.join(PKG, "ui", "assets", "shoal-256.png")


ICONS = (("scalable", "shoal.svg"), ("64x64", "shoal-64.png"), ("256x256", "shoal-256.png"))


def install_icon(dry_run: bool = False) -> list[str]:
    """The shoal icon (a fish before the seaweed lattice of the splash) into the user's icon theme, under the name every
    shoal launcher uses (`Icon=shoal`). A different icon already there is kept once as `.bak-shoal`."""
    base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    theme = os.path.join(base, "icons", "hicolor")
    out = []
    for size, name in ICONS:
        src = os.path.join(PKG, "ui", "assets", name)
        if not os.path.exists(src):
            continue
        dst = os.path.join(theme, size, "apps", "shoal" + os.path.splitext(name)[1])
        new = open(src, "rb").read()
        if os.path.exists(dst) and open(dst, "rb").read() == new:
            continue
        out.append(("[dry-run] " if dry_run else "") + f"icon {dst}")
        if dry_run:
            continue
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        if os.path.exists(dst) and not os.path.exists(dst + ".bak-shoal"):
            shutil.copy2(dst, dst + ".bak-shoal")
        with open(dst + ".tmp", "wb") as fh:
            fh.write(new)
        os.replace(dst + ".tmp", dst)
    if out and not dry_run:
        os.utime(theme)  # a desktop shell rescans a theme whose folder changed
    return out


def desktop_text() -> str:
    exe = shlex.join(_cli_argv("ui", "app-start"))
    return ("[Desktop Entry]\nType=Application\nName=Shoal\nComment=Shoal groups in the Herdr app, with the pin held\n"
            f"Exec={exe}\nIcon={desktop_icon()}\nTerminal=false\nCategories=Development;\nStartupWMClass=herdr-app\n")


def feed_unit_text() -> str:
    import ui_herdr
    parts = [os.path.expanduser("~/.local/bin")] + (config.tool_path() or "/usr/local/bin:/usr/bin:/bin").split(":")
    path = ":".join(dict.fromkeys(x for x in parts if x))
    return f"""[Unit]
Description=shoal graph feed: the graph document for the Herdr app's lattice view (read-only to shoal)
[Service]
Type=simple
Environment=PATH={path}
ExecStart={shlex.join(ui_herdr.shoal_argv("ui", "graph-feed"))}
Restart=always
RestartSec=15
Nice=10
MemoryHigh=512M
[Install]
WantedBy=default.target
"""


COPY_MARK = ".shoal-copy"


def copied(name: str) -> bool:
    """A mod with pictures is copied into the app home, not linked: Herdr app 0.1.12's `image()` does not load a
    PNG from a mod folder that is a symlink to outside its home (`svg()` does)."""
    return os.path.isdir(os.path.join(MOD_DIR, name, "assets"))


def tree_digest(root: str) -> str:
    """sha256 over a mod folder's files, leaving out what the app writes there (hidden files, type files,
    jsconfig.json) and the copy mark."""
    h = hashlib.sha256()
    for d, dirs, files in os.walk(root):
        dirs[:] = sorted(x for x in dirs if not x.startswith(".") and x != "node_modules")
        for f in sorted(files):
            if f.startswith(".") or f.endswith(".d.ts") or f == "jsconfig.json":
                continue
            p = os.path.join(d, f)
            h.update(os.path.relpath(p, root).encode() + b"\0")
            with open(p, "rb") as fh:
                h.update(fh.read())
    return h.hexdigest()


def link_state(name: str) -> tuple[bool, str]:
    link = os.path.join(app_home(), "mods", name)
    want = os.path.join(MOD_DIR, name)
    if not os.path.lexists(link):
        return False, f"{link} missing"
    if copied(name):
        if os.path.islink(link) or not os.path.exists(os.path.join(link, COPY_MARK)):
            return False, f"{link} is not a shoal copy (`shoal ui app-install` copies it)"
        same = tree_digest(link) == tree_digest(want)
        return same, f"{link} copied from {want}" + ("" if same else ": stale (rerun `shoal ui app-install`)")
    real = os.path.realpath(link)
    return real == os.path.realpath(want), f"{link} -> {real}"


def install_mod(name: str, dry_run: bool = False) -> str:
    """Link (or, for a mod with pictures, copy) one mod into the app home. A folder there that shoal did not put
    there is left alone. Returns what it did, or "" when it was current."""
    src = os.path.join(MOD_DIR, name)
    dest = os.path.join(app_home(), "mods", name)
    ok, _ = link_state(name)
    if ok:
        return ""
    ours = os.path.islink(dest) or not os.path.lexists(dest) or os.path.exists(os.path.join(dest, COPY_MARK))
    if not ours:
        return f"mod {name}: {dest} is a folder shoal did not make: left as it is (move it aside, then rerun)"
    pre = "[dry-run] " if dry_run else ""
    if dry_run:
        return pre + (f"copy {src} to {dest}" if copied(name) else f"link {dest} -> {src}")
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    if copied(name):
        tmp = dest + ".shoal-new"
        shutil.rmtree(tmp, ignore_errors=True)
        shutil.copytree(src, tmp, ignore=shutil.ignore_patterns(".*", "*.d.ts", "jsconfig.json", "node_modules"))
        with open(os.path.join(tmp, COPY_MARK), "w") as fh:
            fh.write(f"copied from {src}; `shoal ui app-install` refreshes it\n")
        if os.path.islink(dest):
            os.remove(dest)
        elif os.path.isdir(dest):
            shutil.rmtree(dest)
        os.rename(tmp, dest)
        return f"copy {src} to {dest}"
    if os.path.islink(dest):
        os.remove(dest)
    elif os.path.isdir(dest):
        shutil.rmtree(dest)  # an earlier shoal copy
    os.symlink(src, dest)
    return f"link {dest} -> {src}"


def cmd_app_install(a) -> int:
    """This host's part of the app adapter: the graph feed unit (any host that runs shoals) and, where the app
    is installed, the shoal mods installed into its home from this checkout (linked; copied when they carry pictures). Idempotent."""
    import ui_herdr
    if a.check:
        return cmd_app_doctor(a)
    if ui_herdr.from_worktree() and not a.dry_run:
        print("ui app-install: run it from the live checkout's install (this is a git worktree: the unit and the mod "
              "links would point into it); --dry-run shows the plan")
        return 2
    unit_dir = os.path.expanduser("~/.config/systemd/user")
    path = os.path.join(unit_dir, FEED_UNIT)
    text = feed_unit_text()
    old = open(path).read() if os.path.exists(path) else None
    if old != text:
        print(("[dry-run] " if a.dry_run else "") + f"write {path}")
        if not a.dry_run:
            os.makedirs(unit_dir, exist_ok=True)
            with open(path + ".tmp", "w") as fh:
                fh.write(text)
            os.replace(path + ".tmp", path)
            subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)
    act = ["systemctl", "--user", "enable", "--now", FEED_UNIT]
    if old is not None and old != text:
        act = ["systemctl", "--user", "restart", FEED_UNIT]
    print(("[dry-run] " if a.dry_run else "") + shlex.join(act))
    if not a.dry_run:
        subprocess.run(act, capture_output=True)
        if old is not None and old != text:
            subprocess.run(["systemctl", "--user", "enable", FEED_UNIT], capture_output=True)
    if os.path.exists(app_bin()):
        for name in MODS:
            if os.path.isdir(os.path.join(MOD_DIR, name)):
                line = install_mod(name, a.dry_run)
                if line:
                    print(line)
        for line in install_keys(a.dry_run) + install_theme(a.dry_run) + install_layout(a.dry_run) + install_icon(a.dry_run):
            print(line)
    return 0


# ---- the shoal layout: Herdr's own, with role names in Agents, host actions in Spaces and shoal's status bar

SHOAL_LAYOUT = "shoal"
CLONE_MARK = ".shoal-clone"
AGENTS_HELPER = """// shoal: a role card's row names the role, in its shoal's color, on the host it really runs on.
const SHOAL_MARK = { ansi6: "#22a99a", ansi3: "#e3814f", ansi4: "#5b8def", ansi5: "#8b7cf6", ansi2: "#3fae6a", ansi1: "#d9649a" };
function shoalRole(a) {
  const term = terminals().find((x) => x.address === a.address);
  const label = term?.labels?.["shoal.pane"] ?? "";
  const cut = label.lastIndexOf("/");
  if (cut < 0 || label.startsWith("standalone/")) return null;
  const title = term?.report?.title ?? "";
  const at = title.indexOf("@");
  const stored = (shared("space.colors") ?? {})[a.space] ?? "ansi6";
  return { role: label.slice(cut + 1), host: at >= 0 ? title.slice(at + 1) : null, color: stored.startsWith("#") ? stored : SHOAL_MARK[stored] ?? null };
}

// A row shows its role. Focus alone moves to a pane the zoom hides, so a zoomed view stays zoomed, on this role:
// the zoom key (pane.zoom, ctrl-shift-z) gives the grid back.
function shoalGo(a) {
  const all = panes();
  const there = all.find((p) => p.pane === a.pane);
  const zoomed = all.some((p) => p.zoomed && (p.tab === there?.tab || p.focused));
  run("pane.focus", { pane: a.pane });
  if (zoomed && !there?.zoomed) run("pane.zoom", { pane: a.pane });
}

// Right-click a role's row: its asks when it has some (they are what it needs of you), else the dialog's Dismiss,
// which clears its needs-you until something new is raised.
function shoalDismiss(a, cx) {
  shoalDialog(a, cx, shoalHasAsks(a) ? "asks" : "dismiss");
}

// A role card's ledger asks (its shoal.asks label): the right-click opens them whenever there are some. The badge's click
// does only when they are its question (shoal.asks-badge): an open prompt keeps the row's click, which focuses its pane.
function shoalHasAsks(a) {
  return Number(terminals().find((x) => x.address === a.address)?.labels?.["shoal.asks"] ?? 0) > 0;
}
function shoalAsks(a, cx) {
  if (terminals().find((x) => x.address === a.address)?.labels?.["shoal.asks-badge"] === "1") shoalDialog(a, cx, "asks");
}

function shoalDialog(a, cx, verb) {
  const label = terminals().find((x) => x.address === a.address)?.labels?.["shoal.pane"] ?? "";
  const cut = label.lastIndexOf("/");
  if (cut < 0 || label.startsWith("standalone/")) return;
  cx.stop_propagation();
  const at = a.address.indexOf("/");
  openOverlay({ mod: "shoal-actions", kind: "dialog", width: 640, height: 600, props: { shoal: label.slice(0, cut), role: label.slice(cut + 1), verb, machine: at > 0 ? a.address.slice(0, at) : null } });
}

// A blocked role's badge says what it wants: its report's kind, or the card's shoal.need label where no kind fits
// (the core keeps only auth, question and permission), in the lattice's marks. `!` is left for a cause nobody named.
const SHOAL_NEED = { dead: "\u2715", nowatch: "\u25cc", stuck: "\u2298" };
function shoalBadge(a) {
  const term = a.address ? terminals().find((x) => x.address === a.address) : null;
  const kind = term?.report?.kind;
  return kind === "question" ? "?" : kind === "auth" ? "\u26b7" : kind === "permission" ? "\u2691"
    : SHOAL_NEED[term?.labels?.["shoal.need"]] ?? "!";
}

// The priority view's sections follow a card's derived agent state (its shoal.group label: needs-you, working, waiting,
// blocked, held, dismissed); a row without one keeps Herdr's own, by its reported status, on the same scale. Done is
// Herdr's (finished, not looked at yet): a card's done is its status dot, inside its group.
const SHOAL_RANK = { "needs-you": 10, working: 9, waiting: 7, blocked: 6, held: 5, dismissed: 1 };
const HERDR_RANK = { 4: 10, 3: 8, 2: 9, 1: 4 }; // Herdr's blocked, done, working, idle
const SHOAL_SECTION = { 10: "Needs you", 9: "Working", 8: "Done", 7: "Waiting", 6: "Blocked", 5: "Held", 4: "Idle", 1: "Dismissed" };
const SHOAL_SECTION_STATUS = { 10: "blocked", 9: "working", 8: "done" };
function shoalLabel(a, key) {
  return terminals().find((x) => x.address === a.address)?.labels?.[key] ?? "";
}
function shoalRank(a, herdr) {
  return SHOAL_RANK[shoalLabel(a, "shoal.group")] ?? HERDR_RANK[herdr] ?? 0;
}
// A row's detail in the priority view: a role's space and its state; a seat's state in place of its tab.
function shoalMore(a, sr, full, fallback) {
  const state = shoalLabel(a, "shoal.state");
  if (sr) return full ? [a.space_name, state].filter(Boolean).join(" \u00b7 ") || null : null;
  return full && state ? state : fallback;
}

export default class Agents extends View {"""
# (exact text in Herdr's shipped mod, its replacement): a text that is gone leaves that block on Herdr's own mod
CLONES = {
    "agents": ("shoal-agents", [
        ("export default class Agents extends View {", AGENTS_HELPER),
        ("      const more = detail.get(a.address);\n",
         "      const sr = shoalRole(a);\n      const more = shoalMore(a, sr, byPriority || a.pinned, detail.get(a.address));\n"),
        # the priority view's sections by the derived agent state (shoalRank), Herdr's status where a row has none
        ("const urgency = new Map(list.map((a) => [a.address, Math.max(this.rank(a), ...this.tree(all, a.address).map((s) => RANK[kidStatus(s.t, this.listed)] ?? 0))]));",
         "const urgency = new Map(list.map((a) => [a.address, shoalRank(a, Math.max(this.rank(a), ...this.tree(all, a.address).map((s) => RANK[kidStatus(s.t, this.listed)] ?? 0)))]));"),
        ("      const r = rankOf(a) || RANK.idle;\n",
         "      const r = rankOf(a) || HERDR_RANK[RANK.idle];\n"),
        ('label: byPriority ? SECTION[r] : a.space_name || "", status: byPriority ? SECTION_STATUS[r] : null',
         'label: byPriority ? SHOAL_SECTION[r] : a.space_name || "", status: byPriority ? SHOAL_SECTION_STATUS[r] : null'),
        ('const machine = a.address.includes("/") ? a.address.split("/")[0] : null;',
         'const machine = sr?.host ?? (a.address.includes("/") ? a.address.split("/")[0] : null);'),
        ("const name = full ? a.space_name || label(a) :", "const name = sr ? sr.role : full ? a.space_name || label(a) :"),
        (".text_color(idle ? t.muted : t.foreground).child(name))", ".text_color(sr?.color ?? (idle ? t.muted : t.foreground)).child(name))"),
        ('.items_center().justify_center().child("!");',
         '.items_center().justify_center().id(`shoal-badge-${a.address}`).cursor_pointer().on_click((_e, cx) => shoalAsks(a, cx)).child(shoalBadge(a));'),
        ('\n          .on_click(() => run("pane.focus", { pane: a.pane }))', '\n          .on_click(() => shoalGo(a))'),
        ('\n        .on_click(() => run("pane.focus", { pane: a.pane }))\n        .when(Boolean(pic)', '\n        .on_click(() => shoalGo(a))\n        .on_mouse_down("right", (_e, cx) => shoalDismiss(a, cx))\n        .when(Boolean(shoalLabel(a, "shoal.detail")), (el) => el.tooltip(shoalLabel(a, "shoal.detail")))\n        .when(Boolean(pic)'),
    ]),
    # the top bar: Home and Lattice beside Files and Changes, where they are always in reach (the status bar's own
    # commands, so one path does the switching: an open tab at once, else the CLI)
    "tabs": ("shoal-tabs", [
        ('      .when(enabled.includes("files"), el => el.child(panelButton("files", "Files", "mods/files/file.svg")))',
         '      .child(h_flex().id("shoal-home").h(26).px(8).gap(5).flex_none().items_center().rounded(6).text_size(12).bg(t.chrome)'
         '.text_color(t.muted).hover(s => s.bg(t.hover).text_color(t.foreground)).on_mouse_down("left", stop)'
         '.on_click(() => run("shoal-status.home")).child("\u2302 Home"))\n'
         '      .child(h_flex().id("shoal-lattice").h(26).px(8).gap(5).flex_none().items_center().rounded(6).text_size(12).bg(t.chrome)'
         '.text_color(t.muted).hover(s => s.bg(t.hover).text_color(t.foreground)).on_mouse_down("left", stop)'
         '.on_click(() => run("shoal-status.lattice")).child("\u25ce Lattice"))\n'
         '      .child(h_flex().id("shoal-board").h(26).px(8).gap(5).flex_none().items_center().rounded(6).text_size(12).bg(t.chrome)'
         '.text_color(t.muted).hover(s => s.bg(t.hover).text_color(t.foreground)).on_mouse_down("left", stop)'
         '.on_click(() => run("shoal-status.board")).child("Board"))\n'
         '      .child(h_flex().id("shoal-actions").h(26).px(8).gap(5).flex_none().items_center().rounded(6).text_size(12).bg(t.chrome)'
         '.text_color(t.muted).hover(s => s.bg(t.hover).text_color(t.foreground)).on_mouse_down("left", stop)'
         '.on_click(() => run("mod.open", { name: "shoal-actions" })).child("Actions"))\n'
         '      .child(h_flex().id("shoal-usage").h(26).px(8).gap(5).flex_none().items_center().rounded(6).text_size(12).bg(t.chrome)'
         '.text_color(t.muted).hover(s => s.bg(t.hover).text_color(t.foreground)).on_mouse_down("left", stop)'
         '.on_click(() => run("shoal-status.usage")).child("Usage"))\n'
         '      .when(enabled.includes("files"), el => el.child(panelButton("files", "Files", "mods/files/file.svg")))'),
        # the home space's lattice, board and usage tabs are reached by those buttons: the tab strip leaves them out
        # rather than show each twice (maintainer, 2026-11-12); the tabs stay, so the buttons still select them
        ("    const shown = space?.tabs ?? [];",
         "    const shown = (space?.tabs ?? []).filter(c => !(space?.name === "
         + json.dumps(HOME_SPACE) + " && [\"lattice\", \"board\", \"usage\"].includes(c.name)));"),
    ]),
    # the window's mark, top left: SHOAL and its icon (copied in, CLONE_FILES), as the splash and the launchers show it
    "workspace-chrome": ("shoal-chrome", [
        ('.child(image("frame/welcome/herdr.png").size(20).rounded(5)).child(div().font_semibold().text_color(t.foreground).child("Herdr"))',
         '.child(image("mods/shoal-chrome/shoal.png").size(20).rounded(5)).child(div().font_semibold().text_color(t.foreground).child("SHOAL"))'),
    ]),
    "spaces": ("shoal-spaces", [
        ('const head = h_flex().id(`machine-${m ?? "local"}`).h(26)',
         'const head = h_flex().id(`machine-${m ?? "local"}`)'
         '.on_mouse_down("right", (_e, cx) => { cx.stop_propagation(); openOverlay({ mod: "shoal-actions", kind: "dialog", '
         'width: 640, height: 600, props: { scope: m ? `host:${m}` : "all", machine: m } }); }).h(26)'),
        # the home space is reached by the top bar's Home, Lattice, Board and Usage, not listed under This computer
        ("    return out.filter(r => this.shown.has(r.w.id));",
         f'    return out.filter(r => this.shown.has(r.w.id) && !(r.w.machine == null && r.w.name === "{HOME_SPACE}"));'),
    ]),
}
LAYOUT_BLOCKS = {"agents": "shoal-agents", "spaces": "shoal-spaces", "workspace-status": "shoal-status",
                 "workspace-chrome": "shoal-chrome", "tabs": "shoal-tabs"}
# files a clone carries that its shipped mod has not: {clone file: shoal's source}. Copied, since the app loads no picture
# through a link; their bytes are part of the clone's mark, so a new icon redoes the copy
CLONE_FILES = {"workspace-chrome": {"shoal.png": os.path.join(PKG, "ui", "assets", "shoal-64.png")}}


def patch_clone(text: str, name: str, patches: list[tuple[str, str]]) -> str | None:
    """Herdr's mod text with shoal's edits, as `mod.clone` would name it; None when one of the anchors is gone (a
    new Herdr release moved that code), so no half-patched copy runs."""
    clone = CLONES[name][0]
    for old, new in patches:
        if text.count(old) != 1:
            return None
        text = text.replace(old, new)
    return text.replace(f"mods/{name}/", f"mods/{clone}/").replace(f'mod: "{name}"', f'mod: "{clone}"')


def install_clone(name: str, dry_run: bool = False) -> tuple[bool, str]:
    """(usable, what happened) for one patched copy of a shipped mod. It is redone whenever the shipped text changes."""
    clone, patches = CLONES[name]
    src = os.path.join(app_home(), "mods", name)
    dest = os.path.join(app_home(), "mods", clone)
    try:
        text = open(os.path.join(src, "main.js")).read()
    except OSError as exc:
        return False, f"mod {clone}: {exc}"
    extra = CLONE_FILES.get(name, {})
    try:
        blobs = {k: hashlib.sha256(open(v, "rb").read()).hexdigest() for k, v in extra.items()}
    except OSError as exc:
        return False, f"mod {clone}: {exc}"
    mark = hashlib.sha256(json.dumps([text, patches, blobs]).encode()).hexdigest()
    try:
        if open(os.path.join(dest, CLONE_MARK)).read().strip() == mark:
            return True, ""
    except OSError:
        pass
    out = patch_clone(text, name, patches)
    if out is None:
        return False, f"mod {clone}: this Herdr release changed {name}: its block keeps Herdr's own (update shoal's patch)"
    if dry_run:
        return True, f"[dry-run] mod {clone}: patched copy of {name}"
    tmp = dest + ".shoal-new"
    shutil.rmtree(tmp, ignore_errors=True)
    shutil.copytree(src, tmp, ignore=shutil.ignore_patterns("*.d.ts", "jsconfig.json", "node_modules"))
    with open(os.path.join(tmp, "main.js"), "w") as fh:
        fh.write(out)
    for k, v in extra.items():
        shutil.copy2(v, os.path.join(tmp, k))
    with open(os.path.join(tmp, CLONE_MARK), "w") as fh:
        fh.write(mark + "\n")
    shutil.rmtree(dest, ignore_errors=True)
    os.rename(tmp, dest)  # the app hot-reloads a half-written folder: only a whole one moves in
    return True, f"mod {clone}: patched copy of {name}"


def shoal_layout_doc(main: dict, usable: set[str]) -> dict:
    """Herdr's `main` layout with `LAYOUT_BLOCKS` swapped where the replacement is usable."""
    doc = json.loads(json.dumps(main))

    def walk(node: dict) -> None:
        content = node.get("content")
        if isinstance(content, dict) and LAYOUT_BLOCKS.get(content.get("mod")) in usable:
            content["mod"] = LAYOUT_BLOCKS[content["mod"]]
            if content["mod"] == "shoal-status" and "shoal-tabs" in usable:
                # Home and Lattice are in the top bar: the status bar leaves them out
                content["props"] = dict(content.get("props") or {}, topbar=True)
        for child in node.get("children") or []:
            walk(child)
    walk(doc)
    doc["shoal_layout"] = True
    return doc


def install_layout(dry_run: bool = False) -> list[str]:
    """The `shoal` layout (ctrl-shift-l still cycles back to Herdr's). A file of that name shoal did not write is left."""
    out, usable = [], set()
    for name in CLONES:
        ok, line = install_clone(name, dry_run)
        if ok:
            usable.add(CLONES[name][0])
        if line:
            out.append(line)
    if os.path.lexists(os.path.join(app_home(), "mods", "shoal-status")) or dry_run:
        usable.add("shoal-status")
    src = os.path.join(app_home(), "layouts", "main.json")
    path = os.path.join(app_home(), "layouts", f"{SHOAL_LAYOUT}.json")
    try:
        main = json.load(open(src))
    except (OSError, ValueError) as exc:
        return out + [f"layout {SHOAL_LAYOUT}: {src}: {exc}"]
    try:
        have = json.load(open(path))
    except FileNotFoundError:
        have = None
    except (OSError, ValueError) as exc:
        return out + [f"layout {SHOAL_LAYOUT}: {path}: {exc}: left as it is"]
    if have is not None and not (isinstance(have, dict) and have.get("shoal_layout")):
        return out + [f"layout {SHOAL_LAYOUT}: {path} is not shoal's: left as it is"]
    doc = shoal_layout_doc(main, usable)
    if doc != have:
        out.append(("[dry-run] " if dry_run else "") + f"layout {SHOAL_LAYOUT}: {', '.join(sorted(usable))}")
        if not dry_run:
            with open(path + ".tmp", "w") as fh:
                json.dump(doc, fh, indent=2)
                fh.write("\n")
            os.replace(path + ".tmp", path)
    if have is None and not dry_run and app_running():
        try:
            app_do("layout.switch", {"name": SHOAL_LAYOUT})
        except AppError as exc:
            out.append(f"layout {SHOAL_LAYOUT}: not switched ({exc})")
    return out


# The 0.9.3 paste keys, kept at the maintainer's request (2026-11-03): under kitty, Ctrl+V pasted text and
# Ctrl+Shift+V an image. The app ships them the other way round.
PASTE_KEYS = {"ctrl-v": "terminal.paste", "ctrl-shift-v": "terminal.paste_image"}
APP_DEFAULT_PASTE_KEYS = {"ctrl-v": "terminal.paste_image", "ctrl-shift-v": "terminal.paste"}
# The actions dialog (the 0.9.3 palette's Alt+s, after the Herdr prefix): one global key, mouse for the rest.
ACTION_KEYS = {"ctrl-alt-s": "mod.open:shoal-actions", "ctrl-alt-l": "shoal-status.lattice", "ctrl-alt-n": "shoal-status.next"}
THEME = "shoal-ocean"
THEME_DIR = os.path.join(REPO, "plugins", "shoal-herdr-app", "themes")


def install_theme(dry_run: bool = False) -> list[str]:
    """The shoal ocean theme: dark chrome and white-on-black terminals. The theme file is shoal's own (refreshed
    when it changed); settings.json's `theme` is set only when the person set none (one-time backup first)."""
    src = os.path.join(THEME_DIR, THEME + ".json")
    if not os.path.exists(src):
        return []
    out, pre = [], "[dry-run] " if dry_run else ""
    dest = os.path.join(app_home(), "themes", THEME + ".json")
    body = open(src).read()
    if (open(dest).read() if os.path.exists(dest) else None) != body:
        out.append(pre + f"theme: write {dest}")
        if not dry_run:
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with open(dest + ".tmp", "w") as fh:
                fh.write(body)
            os.replace(dest + ".tmp", dest)
    path = os.path.join(app_home(), "settings.json")
    try:
        text = open(path).read()
        doc = json.loads(text)
    except FileNotFoundError:
        text, doc = None, {}
    except ValueError as exc:
        return out + [f"theme: {path} is not valid JSON ({exc}): left as it is"]
    if not isinstance(doc, dict):
        return out + [f"theme: {path} is not an object: left as it is"]
    have = doc.get("theme")
    if have == THEME:
        return out
    if have not in (None, ""):
        return out + [f"theme: settings.json picks {have!r}: left as it is (shoal's is {THEME!r})"]
    out.append(pre + f"theme: settings.json theme -> {THEME}")
    if not dry_run:
        backup = path + ".bak-shoal-theme"
        if text is not None and not os.path.exists(backup):
            with open(backup, "w") as fh:
                fh.write(text)
        doc["theme"] = THEME
        with open(path + ".tmp", "w") as fh:
            json.dump(doc, fh, indent=2)
            fh.write("\n")
        os.replace(path + ".tmp", path)
    return out


def theme_state() -> tuple[bool, str]:
    try:
        have = json.load(open(os.path.join(app_home(), "settings.json"))).get("theme")
    except (OSError, ValueError, AttributeError):
        have = None
    present = os.path.exists(os.path.join(app_home(), "themes", THEME + ".json"))
    return have == THEME and present, f"settings theme = {have or 'unset'}; {THEME}.json {'present' if present else 'missing'}"


def keys_state() -> tuple[bool, str]:
    try:
        term = (json.load(open(os.path.join(app_home(), "keys.json"))).get("terminal") or {})
    except (OSError, ValueError) as exc:
        return False, f"keys.json unreadable ({exc})"
    have = {k: term.get(k) for k in PASTE_KEYS}
    return have == PASTE_KEYS, ", ".join(f"{k} = {v or 'unbound'}" for k, v in have.items())


def install_keys(dry_run: bool = False) -> list[str]:
    """Set the 0.9.3 paste keys in the app's keys.json `terminal` section. Only a key that is unbound or holds the
    app's own default is changed; a binding the person chose is left and named. The app reloads the file live."""
    path = os.path.join(app_home(), "keys.json")
    try:
        text = open(path).read()
        doc = json.loads(text)
    except FileNotFoundError:
        text, doc = None, {}
    except ValueError as exc:
        return [f"keys: {path} is not valid JSON ({exc}): left as it is"]
    want_keys = [("terminal", k, v, APP_DEFAULT_PASTE_KEYS[k], "the 0.9.3 paste keys") for k, v in PASTE_KEYS.items()]
    if os.path.isdir(os.path.join(MOD_DIR, "shoal-actions")):
        want_keys += [("global", k, v, None, "the actions dialog, the lattice and the next needs-you agent") for k, v in ACTION_KEYS.items()]
    if not isinstance(doc, dict) or any(not isinstance(doc.get(s, {}), dict) for s in {w[0] for w in want_keys}):
        return [f"keys: {path} has a section shoal can edit that is not an object: left as it is"]
    out, changed = [], False
    for section, key, want, default, why in want_keys:
        sec = doc.setdefault(section, {})
        have = sec.get(key)
        if have == want:
            continue
        if have not in (None, default):
            out.append(f"keys: {section} {key} is bound to {have} by you: left as it is (shoal would use it for {why})")
            continue
        sec[key] = want
        changed = True
        out.append(("[dry-run] " if dry_run else "") + f"keys: {key} -> {want} ({why})")
    if changed and not dry_run:
        backup = path + ".bak-shoal-paste"
        if text is not None and not os.path.exists(backup):  # one-time, before shoal's first rewrite
            with open(backup, "w") as fh:
                fh.write(text)
        with open(path + ".tmp", "w") as fh:
            json.dump(doc, fh, indent=2)
            fh.write("\n")
        os.replace(path + ".tmp", path)
    return out


# ---- doctor -----------------------------------------------------------------------------------

def _version(argv: list[str]) -> str:
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=15, stdin=subprocess.DEVNULL)
        words = (r.stdout or r.stderr).split()
        return next((w for w in words if w[:1].isdigit()), "")
    except (OSError, subprocess.TimeoutExpired):
        return ""


def doctor_checks() -> list[tuple[str, bool, str]]:
    """Rows for `shoal ui doctor` on a host with the Herdr app or herdr-core installed (none otherwise)."""
    out = []
    has_app = os.path.exists(app_bin())
    has_core = os.path.exists(hc_bin())
    if not (has_app or has_core):
        return out
    if has_app:
        v = _version([app_bin(), "--version"])
        out.append(("herdr-app at the pin", v == APP_PIN, f"{v or 'none'} (pin {APP_PIN})"))
        for name in MODS:
            if os.path.isdir(os.path.join(MOD_DIR, name)):
                out.append((f"app mod {name} installed from this checkout",) + link_state(name))
        out.append(("app paste keys as in 0.9.3",) + keys_state())
        out.append(("app theme (shoal ocean)",) + theme_state())
        path = os.path.join(app_home(), "layouts", f"{SHOAL_LAYOUT}.json")
        out.append((f"app layout {SHOAL_LAYOUT}", os.path.exists(path), path if os.path.exists(path) else "missing (rerun `shoal ui app-install`)"))
    if has_core:
        v = _version([hc_bin(), "--version"])
        out.append(("herdr-core at the pin", v == CORE_PIN, f"{v or 'none'} (pin {CORE_PIN})"))
    if has_app:
        import routes
        import ui_launch
        have = set(machines())
        want = [h for h in sorted(routes.routes()) if h != ui_launch.this_server()]
        miss = [h for h in want if h not in have]
        # informational: a route becomes a machine the first time it is opened
        out.append(("app machines", True,
                    (", ".join(sorted(have)) or "none") + ("; added on first open: " + ", ".join(miss) if miss else "")))
    feed = feed_state()
    try:
        state = subprocess.run(["systemctl", "--user", "is-active", FEED_UNIT], capture_output=True, text=True,
                               timeout=10, stdin=subprocess.DEVNULL).stdout.strip()
    except (OSError, subprocess.TimeoutExpired) as exc:
        state = f"unknown ({exc})"
    out.append((f"unit {FEED_UNIT} active", state == "active",
                state + ("" if feed["fresh"] else f"; {feed['path']} not fresh")))
    return out


def cmd_app_doctor(a) -> int:
    bad = 0
    for name, ok, detail in doctor_checks():
        print(f"{'ok  ' if ok else 'FAIL'} {name}: {detail}")
        bad += not ok
    return 1 if bad else 0
