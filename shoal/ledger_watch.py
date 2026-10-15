#!/usr/bin/env python3
"""The one session watcher every role arms: `shoal ledger-watch`.

Every INTERVAL seconds: pull the ledger (git fetch + rebase; a failed fetch
is tolerated, five in a row is an instrument failure -> exit 2), fold it,
and compute what needs THIS role's attention:
  - open offers addressed to it or `*` (not declined by it),
  - msgs to it or `*` (not its own),
  - a foreign claim on an item it holds (claim lost / contested),
  - a withdraw of an item it holds or was offered,
  - board candidates when fill=self and its queue is empty (checked every
    BOARD_INTERVAL, not every tick).
Each event id is delivered at most once (sidecar `delivered=`); nothing
else wakes the session. On a wake the watcher fires ONE wake adapter and
exits — the session re-arms as its first act after acting:
  --wake exit             print the summary, exit 0 (Claude background command / Monitor)
  --wake send:<session>   `openchamber session send` LEDGER-WAKE to that id, then exit 0
  --wake print            same as exit, but keep looping (operator terminal)
Test mode (`--test`) fires the adapter once with LEDGER-WAKE-TEST and
exits: arming counts only after the test wake is seen in-transcript.

Sidecar: /tmp/<id>/ledger-watch-<slug>.state (session_id=, pid=,
armed_utc=, delivered=<comma ids, capped 400>, fetch_fail=, last_tick=).
"""
from __future__ import annotations

import datetime
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "telemetry"))
from ledger import OFFER_WRITERS  # noqa: E402

from ledger import Ledger, LedgerError, View, fmt_item, item_key, short  # noqa: E402
from roles import ShoalSpec, find_spec, state_dir  # noqa: E402
import config  # noqa: E402

TOOL_PATH = config.tool_path()
_ENV = config.tool_env()
DELIVERED_CAP = 400


def sidecar_path(sid: str, slug: str) -> str:
    return os.path.join(state_dir(sid), f"ledger-watch-{slug}.state")


def load_sidecar(path: str) -> dict[str, str]:
    d: dict[str, str] = {}
    try:
        with open(path) as fh:
            for line in fh:
                if "=" in line:
                    k, v = line.strip().split("=", 1)
                    d[k.strip()] = v.strip()
    except OSError:
        pass
    return d


def save_sidecar(path: str, d: dict[str, str]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        for k, v in d.items():
            fh.write(f"{k}={v}\n")
    os.replace(tmp, path)


def _exit_verdict(path: str, sc: dict[str, str], verdict: str) -> None:
    """Record a designed exit for THIS pid, so a later dead pid without one reads as
    killed (the verdict-line control, readme §4; the health quorum and kill-audit consume it)."""
    sc["exit"] = f"{os.getpid()} {verdict} {now_iso()}"
    save_sidecar(path, sc)
    print(f"{now_iso()} VERDICT {verdict} (pid {os.getpid()})")


def now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


_RESOLVED_SEEN: set[str] = set()  # wait keys seen resolved on an earlier tick (the waittargets grace)


def actionable(view: View, slug: str, role, spec, board: bool) -> list[tuple[str, str]]:
    """[(event id, one-line description)] for everything that needs slug now.

    With spec `features.workwake` (architecture doc §3.3) only WORK wakes the role: an
    offer while it is idle (or a `--now` offer from user), a change to
    its ACTIVE item, board candidates when idle with nothing queued, or a health wake
    request. Msgs are mail (read by `shoal next`), except a `user` msg sent `--wake`."""
    out = []
    workwake = bool((getattr(spec, "features", {}) or {}).get("workwake")) if spec is not None else False
    idle = view.is_idle(slug) if workwake else True
    q = view.current_queue(slug) or {}
    for w in _wake_requests(view.ledger.sid, slug):
        out.append(w)
    for o in view.queue(slug):
        if workwake and not idle:
            now_ok = o.get("now") and o["by"] in OFFER_WRITERS
            if not now_ok:
                continue  # folds in at the role's next boundary
        out.append((o["id"], f"OFFER {fmt_item(o)} to={o['to']} prio={o.get('prio', 50)} by={o['by']}"
                    + (" NOW" if o.get("now") else "")
                    + (f" base={o['base']}" if o.get("base") else "")
                    + (f" — {short(o.get('note', ''), 80)}" if o.get("note") else "")))
    for m in view.msgs_to(slug):
        if workwake and not (m.get("by") == "user" and m.get("wake")):
            continue  # mail, not a wake
        out.append((m["id"], f"MSG {m['by']}->{m['to']} [{m.get('kind', 'info')}] {short(m.get('text', ''), 120)}"
                    + (" (ack requested)" if m.get("ack") else "")))
    mine = {item_key(c["item"]) for c in view.active(slug)}
    if workwake and q.get("active"):
        mine = {item_key(q["active"])}  # only the ACTIVE item's integrity is worth a wake
    for key in mine:
        w = view.winner(key)
        if w is not None and w["by"] != slug:
            out.append((w["id"], f"CLAIM-LOST {key}: {w['by']} landed first — release kind=lost-race"))
    for e in view.ledger.events():
        if e.get("type") == "withdraw" and item_key(e.get("item", "")) in mine:
            out.append((e["id"], f"WITHDRAWN {e.get('item')} by {e['by']}: {short(e.get('reason', ''), 80)}"))
    # features.waittargets: a role waiting on a local target (a `job run` unit or a lease) is woken
    # when it resolves, whoever armed the waiter. A detached `job wait` reaches no session (measured
    # 2026-10-01: a job ended 27 s into a wait and the role slept on). Emitted on the second tick
    # that sees it resolved, so a session's own waiter acts first and replaces the wait.
    if (getattr(spec, "features", {}) or {}).get("waittargets"):
        try:
            from state import read_wait
            w = read_wait(view.ledger.sid, slug) or {}
            target = w.get("target") or ""
            if w.get("sub") in ("local-ci", "remote-ci") and target.split(":", 1)[0] in ("job", "lease"):
                import providers
                pending, why = providers.resolve(target, slug, view.ledger.sid)
                key = f"waitdone:{w.get('since_utc', '')}:{target}"
                if pending is False:
                    if key in _RESOLVED_SEEN:
                        out.append((key, f"WAIT RESOLVED {target} ({why}): your {w.get('sub')} wait holds "
                                         f"nothing now; run `next`, act on the result, write one `queue`"))
                    _RESOLVED_SEEN.add(key)
                else:
                    _RESOLVED_SEEN.discard(key)
        except Exception as exc:  # noqa: BLE001 - a wait check never breaks (or wakes) the watcher
            print(f"{now_iso()} wait-check error: {short(str(exc), 120)}", file=sys.stderr)
    # an idle role with only blocked next items is as starved as one with none (features.blockers):
    # without this, two recorded blockers silenced every board wake (measured 2026-09-29)
    open_next = list(q.get("next") or [])
    if (getattr(spec, "features", {}) or {}).get("blockers"):
        nbk = {item_key(k) for k in (q.get("next_blocked") or {})}
        open_next = [x for x in open_next if item_key(x) not in nbk]
    if board and role is not None and getattr(role, "fill", "assigned") == "self" and not view.queue(slug) \
            and (not workwake or (idle and not open_next)):
        try:
            taken = view.taken() | view.offered_open()
            declined = set(view.declined(slug))
            dig = None
            if (getattr(spec, "features", {}) or {}).get("digest"):
                from digest import candidates as digest_candidates, fmt_candidate
                dig = digest_candidates(spec.sid, slug, taken, declined, limit=5)
            if dig is not None:  # shared digest: no GitHub call from this watcher
                for c in dig:
                    out.append((f"board:{c['item']}", fmt_candidate(c)))
            else:
                from sources import candidates
                for c in candidates(spec.sources, getattr(role, "scope", None), taken, declined, limit=3):
                    out.append((f"board:{c['item']}", f"BOARD {c['item']}:{c['verb']} pos={c['pos']} — {short(c['title'], 70)}"))
        except Exception as exc:  # noqa: BLE001
            out.append(("board:error", f"BOARD-ERROR {short(str(exc), 120)} (self-fill unavailable this tick)"))
    return out


def _wake_requests(sid: str, slug: str) -> list[tuple[str, str]]:
    """Health-quorum wake requests (/tmp/<id>/wake-<slug>): one delivery per request line.
    The file is removed by the role's next `shoal queue` write (the acknowledgement)."""
    import json as _json
    out = []
    try:
        with open(os.path.join(state_dir(sid), f"wake-{slug}")) as fh:
            for line in fh:
                try:
                    d = _json.loads(line)
                except ValueError:
                    continue
                out.append((f"wake:{d.get('ts')}", f"HEALTH {d.get('condition', '')}: {short(d.get('text', ''), 160)}"))
    except OSError:
        pass
    return out


def muse_session_busy(session: str) -> bool:
    """True while a `muse exec` turn holds the session's run lock."""
    try:
        import muse
        return muse.busy(session)
    except Exception:  # noqa: BLE001 - an unreadable lock never holds a wake back
        return False


def session_busy(session: str, repo_dir: str) -> bool:
    """True when the opencode session is mid-turn (its status is known and not idle). Unknown reads
    as not busy, so a status probe that fails cannot hold a wake back forever."""
    try:
        from backends import get as get_backend
        s = get_backend("opencode").status(session, repo_dir) or ""
    except Exception:  # noqa: BLE001
        return False
    return bool(s) and "status:" in s and "status:idle" not in s


def code_stamp() -> float:
    """Newest modification time of shoal's own .py files (what a running watcher executes)."""
    import glob
    here = os.path.dirname(os.path.abspath(__file__))
    files = [f for d in ("", "telemetry") for f in glob.glob(os.path.join(here, d, "*.py"))]
    try:
        return max(os.path.getmtime(f) for f in files) if files else 0.0
    except OSError:
        return 0.0


def _limit_until(sid: str) -> str:
    try:
        from health import limit_until_sid
        return limit_until_sid(sid)
    except Exception:  # noqa: BLE001 - a missing or unreadable health state never holds a wake
        return ""


def claude_session_busy(session: str) -> bool:
    """True while the Claude session is mid-turn, or at an open question prompt, where a message would
    interrupt the turn or land on the prompt. A session that only ended its turn self-reported
    'blocked' (waiting on the maintainer, no prompt) is NOT busy: holding wakes from it deadlocked three
    lanes, each waiting for exactly the message being held (measured 2026-10-13)."""
    try:
        from health import claude_busy, claude_prompt_open
        st = next((v for k, v in claude_busy().items() if session and k.startswith(session[:8])), "")
        if st == "blocked":
            from backends import fake_sessions, get as get_backend
            row = {}
            fake = fake_sessions()
            rows = (fake.get("claude") or []) if fake is not None else get_backend("claude")._agents()
            row = next((r for r in rows if str(r.get("sessionId", "")).startswith(session[:8])), {})
            return claude_prompt_open(row, session)
    except Exception:  # noqa: BLE001
        return False
    return st == "busy"


def relay_deliver(sid: str, role, session: str, text: str) -> bool:
    """Queue one wake for the wake relay, which delivers it to the session as a message. A relay that
    is down picks the line up when it is back (relay-watch resumes from its cursor; health relaunches
    it under features.health.revive)."""
    from health import relay_alive, relay_queue_path, iso
    os.makedirs(os.path.dirname(relay_queue_path(sid)), exist_ok=True)
    tag = getattr(role, "tag", "") if role is not None else ""
    with open(relay_queue_path(sid), "a") as fh:
        fh.write(json.dumps({"ts": iso(), "to": tag, "slug": getattr(role, "slug", ""), "session": session,
                             "condition": "ledger-wake", "text": text}) + "\n")
    print(f"queued for the wake relay ({tag})" + ("" if relay_alive(sid) else ": relay down, delivered on relaunch"))
    return True


def fire(wake: str, text: str, repo_dir: str) -> bool:
    if wake.startswith("exec:"):
        sid = wake[len("exec:"):]
        try:
            from backends import get as get_backend
            get_backend("muse").send(sid, repo_dir, text)
        except Exception as exc:  # noqa: BLE001 - a busy session or an unrunnable muse: retried next tick
            print(f"ERROR: wake turn on {sid} not started: {str(exc)[-200:]}")
            return False
        print(f"started a turn on {sid}")
        return True
    if wake.startswith("send:"):
        sid = wake[len("send:"):]
        r = subprocess.run(["openchamber", "session", "send", "--session", sid, "--dir", repo_dir,
                            "--prompt", text], capture_output=True, text=True, timeout=60, env=_ENV)
        if r.returncode != 0:
            print(f"ERROR: wake send to {sid} failed rc={r.returncode}: {(r.stdout + r.stderr).strip()[-200:]}")
            return False
        print(f"woke {sid}")
        return True
    print(text)
    return True


def emit_wake(sid: str, slug: str, session: str, eco: str, reason: str, n: int) -> None:
    try:
        from shoal_telemetry import append_event, build_event, resolve_dir
        ev = build_event(sid, session or slug, eco or "?", "watch_event",
                         [f"kind=ledger-wake", f"slug={slug}", f"note={reason}", f"n={n}",
                          "channel=ledger"])
        append_event(resolve_dir(None, sid), ev)
    except Exception:
        pass


def cmd_ledger_watch(a) -> int:
    sid, slug = a.sid, a.slug
    try:
        sys.stdout.reconfigure(line_buffering=True)  # a watcher's output is read through pipes/logs
    except Exception:
        pass
    try:
        spec = ShoalSpec.load(find_spec(sid, a.spec))
    except OSError:
        spec = None
    role = spec.role(slug) if spec else None
    repo_dir = a.dir or (spec.dir if spec else os.getcwd())
    sc_path = a.sidecar or sidecar_path(sid, slug)
    sc = load_sidecar(sc_path)
    session = a.session or sc.get("session_id", "") or (role.session if role else "")
    # a Claude session knows its own id; a lane once passed its Remote Control bridge id instead
    # (measured 2026-10-12), which restart could not match to the launched session
    own = os.environ.get("CLAUDE_CODE_SESSION_ID", "")
    if own and session != own and not own.startswith(session or "\0"):
        print(f"note: --session {session or '(none)'} is not this Claude session; binding {own}")
        session = own
    persistent = (a.wake.startswith(("send:", "exec:")) or a.wake == "relay") and not a.test
    if persistent:
        # a send: or relay watcher keeps running after each wake (below), so arming twice would wake
        # twice: a second arm for the same role, wake and session finds the first alive and leaves it be
        old_pid = sc.get("pid", "")
        try:
            with open(f"/proc/{old_pid}/cmdline", "rb") as fh:
                argv = [x.decode(errors="replace") for x in fh.read().split(b"\0") if x]
            opt = {argv[i]: argv[i + 1] for i in range(len(argv) - 1) if argv[i].startswith("--")}
            same = (opt.get("--slug") == slug and opt.get("--wake") == a.wake
                    and (a.wake.startswith(("send:", "exec:")) or (opt.get("--session") or "")[:8] == (session or "")[:8]))
            if old_pid and int(old_pid) != os.getpid() and "ledger-watch" in argv and same:
                print(f"ALREADY ARMED: ledger-watch pid {old_pid} for {slug} ({a.wake}) is running and keeps "
                      f"running across wakes; nothing to do")
                return 0
        except (OSError, ValueError):
            pass
    sc.update(session_id=session, pid=str(os.getpid()), armed_utc=now_iso(),
              wake=a.wake, interval=str(a.interval))
    sc.setdefault("delivered", "")
    sc.setdefault("fetch_fail", "0")
    save_sidecar(sc_path, sc)
    led = Ledger(sid, root=a.ledger_dir or (spec.ledger_dir if spec else None), sync=not a.no_sync)
    if not led.exists() and led.sync:
        led.pull()
    if not led.exists():
        print(f"ERROR: no ledger for {sid} at {led.ldir} (run `shoal ledger-init`)")
        return 3
    label = f"LEDGER-WAKE {slug}@{sid}"
    if a.test:
        text = (f"LEDGER-WAKE-TEST {slug}@{sid}: dry-run wakeup proof at {now_iso()}; confirm arrival "
                f"in-transcript, then arm for real (same command without --test).")
        return 0 if fire(a.wake, text, repo_dir) else 2
    print(f"{now_iso()} armed: {slug}@{sid} every {a.interval:g}s wake={a.wake} sidecar={sc_path}")
    fails = int(sc.get("fetch_fail", "0") or 0)
    send_fails = 0
    last_board = 0.0
    first = True
    stamp = code_stamp() if persistent else 0.0
    while True:
        if not first:
            time.sleep(a.interval)
        first = False
        if persistent and code_stamp() != stamp:
            # a persistent watcher never re-arms, so an upgrade would never reach it: re-exec in place
            # (same pid, so the sidecar and ALREADY ARMED still match) once a code change has landed
            time.sleep(2)
            print(f"{now_iso()} shoal code changed: reloading the watcher in place", flush=True)
            os.execv(sys.executable, [sys.executable] + sys.argv)
        try:
            ok = led.pull()
        except (LedgerError, subprocess.TimeoutExpired) as exc:  # a failed pull is a failed fetch: counted and retried, never fatal
            print(f"{now_iso()} PULL-FAILED: {str(exc)[-200:]}", flush=True)
            ok = False
        if not ok:
            fails += 1
            print(f"{now_iso()} FETCH-FAILED ({fails}/5)")
            sc["fetch_fail"] = str(fails)
            save_sidecar(sc_path, sc)
            if fails >= 5:
                print("LEDGER-WATCH INSTRUMENT-FAILED: 5 consecutive fetch failures — re-arm after checking the remote")
                _exit_verdict(sc_path, sc, "INSTRUMENT-FAILED")
                return 2
        else:
            fails = 0
        board_now = time.time() - last_board >= a.board_interval
        if board_now:
            last_board = time.time()
        view = View(led, spec)
        acts = actionable(view, slug, role, spec, board=board_now and not a.no_board)
        delivered = set(filter(None, sc.get("delivered", "").split(",")))
        new = [(i, d) for i, d in acts if i not in delivered]
        sc["last_tick"] = now_iso()
        sc["fetch_fail"] = str(fails)
        if not new:
            save_sidecar(sc_path, sc)
            continue
        if persistent and (claude_session_busy(session) if a.wake == "relay"
                           else muse_session_busy(a.wake[len("exec:"):]) if a.wake.startswith("exec:")
                           else session_busy(session, repo_dir)):
            # never wake a lane mid-turn (or, for Claude, at an open question): the events stay
            # undelivered and go out at the next idle tick
            save_sidecar(sc_path, sc)
            continue
        if persistent and a.wake == "relay" and _limit_until(sid):
            # inside the account usage limit a relayed wake errors in the role's turn and is lost
            # (2026-10-14): hold the events; the first tick after the reset delivers them
            save_sidecar(sc_path, sc)
            continue
        ids = [i for i, _ in new]
        delivered |= set(ids)
        sc["delivered"] = ",".join(list(delivered)[-DELIVERED_CAP:])
        sc["last_wake"] = now_iso()
        save_sidecar(sc_path, sc)
        reason = ",".join(sorted({d.split()[0] for _, d in new}))
        text = (f"{label} at {now_iso()} ({len(new)} new): " + " | ".join(d for _, d in new)
                + f" || next: `shoal show --shoal {sid} --slug {slug}`; act (claim/msg/release), transition"
                + ("; your watcher keeps running (no re-arm)." if persistent else ", re-arm."))
        emit_wake(sid, slug, session, role.ecosystem if role else "", reason, len(new))
        fired = relay_deliver(sid, role, session or "", text) if a.wake == "relay" else fire(a.wake, text, repo_dir)
        if a.wake == "print":
            continue
        if persistent:
            # an opencode lane is woken by the send itself, so the watcher stays: no re-arm to forget
            # (measured 2026-10-12: shoal-aux took its wake, did not re-arm, and had no watcher for 20 min)
            if fired:
                send_fails = 0
                continue
            send_fails += 1
            sc["delivered"] = ",".join(x for x in sc["delivered"].split(",") if x not in set(ids))
            save_sidecar(sc_path, sc)
            if send_fails < 5:
                print(f"{now_iso()} WAKE-SEND-FAILED ({send_fails}/5): retrying next tick")
                continue
        _exit_verdict(sc_path, sc, "WAKE" if fired else "WAKE-SEND-FAILED")
        return 0 if fired else 2


def add_parser(sub) -> None:
    p = sub.add_parser("ledger-watch", help="the unified session watcher (wakes on offers/msgs/claims/board)")
    config.add_id_arg(p, required=True)
    p.add_argument("--slug", required=True)
    p.add_argument("--wake", default="exit", help="exit | relay (Claude, detached: the wake relay delivers) | send:<session-id> (opencode, detached) | exec:<session-id> (Muse: starts one `muse exec` turn per wake, detached) | print")
    p.add_argument("--interval", type=float, default=120, help="seconds between ledger pulls (default 120)")
    p.add_argument("--board-interval", type=float, default=600, help="seconds between board reads when self-filling")
    p.add_argument("--no-board", action="store_true", help="never read the priority table")
    p.add_argument("--session", default="", help="own session id (bound into the sidecar)")
    p.add_argument("--sidecar", default="")
    p.add_argument("--spec", default="")
    p.add_argument("--dir", default="", help="repo dir for the send adapter (default: spec dir)")
    p.add_argument("--ledger-dir", default="")
    p.add_argument("--no-sync", action="store_true", help="local ledger only (tests)")
    p.add_argument("--test", action="store_true", help="fire LEDGER-WAKE-TEST once and exit")
    p.set_defaults(func=cmd_ledger_watch)


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    sp = ap.add_subparsers(dest="cmd", required=True)
    add_parser(sp)
    a = ap.parse_args()
    sys.exit(a.func(a))
