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
    BOARD_INTERVAL, not every tick); from a digest, only strong ones (a weak
    or declined-earlier candidate waits in `next`).
Each event id is delivered at most once (sidecar `delivered=`); nothing
else wakes the session. On a wake the watcher fires ONE wake adapter and
exits — the session re-arms as its first act after acting:
  --wake exit             print the summary, exit 0 (Claude background command / Monitor)
  --wake send:<session>   `openchamber session send` LEDGER-WAKE to that id, then exit 0
  --wake direct           Claude, detached: resume the session directly (stop and resume when
                          it is idle at its prompt); `relay` is read as `direct`
  --wake print            same as exit, but keep looping (operator terminal)
Test mode (`--test`) fires the adapter once with LEDGER-WAKE-TEST and
exits: arming counts only after the test wake is seen in-transcript.

Sidecar: /tmp/<id>/ledger-watch-<slug>.state (session_id=, pid=,
armed_utc=, delivered=<comma ids, capped 400>, fetch_fail=, last_tick=,
last_wake=). While a detached watcher holds new events for a person at the
session (an interactive client attached to an exec session, or a Claude
session at an open question) it also records held_since=, held_reason=
(`attached:<pid>` or `question`), held_events= and held_client=; they are
cleared once it delivers, or once nothing is held. `health` pages a hold for
an attached client once (`wakes-held`), and `next --peek` and `show` print it.
"""
from __future__ import annotations

import datetime
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "telemetry"))
from ledger import OFFER_WRITERS  # noqa: E402

from ledger import Ledger, LedgerError, View, fmt_item, item_key, short  # noqa: E402
from roles import ShoalSpec, canon_wake, find_spec, is_detached_wake, mail_read_since, state_dir  # noqa: E402
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
    # a msg the role's mail read cursor covers was read (`next` shows mail and moves the cursor): a watcher
    # armed since, by the role or the quorum, must not deliver it again (measured 2026-10-20: a quorum-delivered
    # wake was re-delivered 2 min later). Only the cursor counts, never a queue or state write: a wake held
    # mid-turn, or deferred for background work, is older than the turn-end queue write that should release it,
    # and a role that wrote that queue without `next` never saw it (measured 2026-10-26: two held wakes lost)
    for m in view.msgs_to(slug, since_ts=mail_read_since(view.ledger.sid, slug)):
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
            if w.get("sub") in ("local-ci", "remote-ci", "research-proto", "research-window") and target.split(":", 1)[0] in ("job", "lease"):
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
                # a weak candidate (no lane fit, or one the role passed on earlier) is no wake: it stays in `next`'s
                # BOARD list for the role's next boundary (measured 2026-11-20..26: 42 of 65 board wakes named only weak
                # or declined-earlier candidates, and 35 of those 42 turns did nothing)
                for c in dig:
                    if not c.get("weak"):
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


def attached_pid(session: str, ecosystem: str = "muse") -> int:
    """Pid of an interactive client (`codex resume`, `muse resume`) that holds an exec session, else 0."""
    try:
        if ecosystem == "codex":
            import codex_harness
            return int(codex_harness.attached_client(session) or 0)
        import muse
        return int(muse.attached_client(session) or 0)
    except Exception:  # noqa: BLE001 - an unreadable process table never holds a wake back
        return 0


def proc_argv(pid: int) -> list[str]:
    """A process's argv from /proc ([] when it cannot be read)."""
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as fh:
            return [x.decode(errors="replace") for x in fh.read().split(b"\0") if x]
    except OSError:
        return []


def proc_ppid(pid: int) -> int:
    """A process's parent pid from /proc (0 when it cannot be read)."""
    try:
        with open(f"/proc/{pid}/stat") as fh:
            return int(fh.read().rsplit(")", 1)[1].split()[1])
    except (OSError, ValueError, IndexError):
        return 0


CARD_TAILS = ("codex-tail", "muse-tail")
CARD_DEPTH = 8


def card_wrapper(argv: list[str], sid: str = "", slug: str = "", sessions: tuple[str, ...] = ()) -> str:
    """What a role card runs a native client under, when `argv` is one: its observer with native access allowed
    (`codex-tail --allow-interactive`, `muse-tail --allow-interactive`) or `shoal view` (a Claude card's `claude attach`).
    Given ids, it must name them: the observer its `--session` (one of `sessions`), the view the role (`sid` and `slug`)
    or its session. '' for anything else, a shell or a terminal a person started a client in by hand among them."""
    for i in range(min(len(argv) - 1, 3)):
        if os.path.basename(argv[i]) not in ("shoal", "cli.py"):
            continue
        sub, rest = argv[i + 1], argv[i + 2:]
        if sub in CARD_TAILS and "--allow-interactive" in rest:
            s = rest[rest.index("--session") + 1] if "--session" in rest[:-1] else ""
            if not sessions or s in sessions:
                return f"{sub} --allow-interactive"
        if sub == "view":
            named = not (sid or slug or sessions) or (sid and slug and sid in rest and slug in rest) \
                or any(s and s in rest for s in sessions)
            if named:
                return "shoal view"
        return ""
    return ""


def card_client(pid: int, sid: str = "", slug: str = "", sessions: tuple[str, ...] = ()) -> tuple[int, str]:
    """(the client a role card started, its wrapper) when `pid` is that client or runs under it, else (0, ''). The
    client is the wrapper's own child (`codex resume` under `codex-tail`, `claude attach` under `shoal view`): the
    process whose exit returns the card to its read-only view. Found by walking parents, at most CARD_DEPTH."""
    child = pid
    for _ in range(CARD_DEPTH):
        p = proc_ppid(child)
        if p <= 1:
            break
        w = card_wrapper(proc_argv(p), sid, slug, sessions)
        if w:
            return child, w
        child = p
    return 0, ""


def client_cmd(argv: list[str]) -> str:
    """`codex resume <thread>`, `claude attach <job>`: a client's command, by its binary's name only."""
    for i, x in enumerate(argv):
        if x in ("resume", "attach") and i >= 1:
            return " ".join([os.path.basename(argv[i - 1]), x] + argv[i + 1:i + 2])
    return os.path.basename(argv[0]) if argv else ""


def client_desc(pid: int, session: str) -> str:
    """`pid 123, `codex resume <thread>`, opened from its card`: what a person must exit, as far as /proc says.
    A client opened from the role's card runs under its observer (`codex-tail`, `muse-tail`: card_client)."""
    out = f"pid {pid}"
    argv = proc_argv(pid)
    if "resume" in argv[1:]:
        out += f", `{os.path.basename(argv[argv.index('resume') - 1])} resume {session}`"
    if card_client(pid)[0]:
        return out + ", opened from its card"
    return out


HOLD_KEYS = ("held_since", "held_reason", "held_events", "held_client")


def note_hold(sc: dict[str, str], reason: str, n: int, client: str = "") -> None:
    """Record (or clear) why a detached watcher holds n new events. Only a person at the session is recorded: an
    attached client (`attached:<pid>`) or an open question (`question`); a turn in flight ends by itself and is not.
    The since time is kept while the reason stays the same."""
    if not (reason.startswith("attached:") or reason == "question"):
        if sc.get("held_reason"):
            print(f"{now_iso()} hold ended ({sc['held_reason']}, since {sc.get('held_since', '?')})", flush=True)
        for k in HOLD_KEYS:
            sc.pop(k, None)
        return
    if sc.get("held_reason") != reason or not sc.get("held_since"):
        sc["held_since"] = now_iso()
        sc["held_reason"] = reason
        if client:
            sc["held_client"] = client
        else:
            sc.pop("held_client", None)
        print(f"{now_iso()} HOLDING {n} event(s): "
              + (f"an interactive client ({client}) is attached" if client else "the session is at an open question")
              + "; delivered once it is gone", flush=True)
    sc["held_events"] = str(n)


def current_hold(sc: dict[str, str], alive: bool) -> dict:
    """The hold a watcher's sidecar records, while it still stands: {reason, since, events, client, pid, attached}, or {}
    when the watcher is not alive, nothing is held, or the attached client has gone (the watcher delivers at its next
    tick)."""
    reason, since = sc.get("held_reason", ""), sc.get("held_since", "")
    if not alive or not reason or not since:
        return {}
    pid = 0
    if reason.startswith("attached:"):
        try:
            pid = int(reason.split(":", 1)[1])
        except ValueError:
            return {}
        if not os.path.exists(f"/proc/{pid}"):
            return {}
    try:
        n = int(sc.get("held_events") or 0)
    except ValueError:
        n = 0
    return {"reason": reason, "since": since, "events": n, "client": sc.get("held_client", "") or (f"pid {pid}" if pid else ""),
            "pid": pid, "attached": bool(pid)}


def hold_text(h: dict) -> str:
    """`holding 2 event(s) for an attached client pid 123 since 21:58Z` ('' for no hold)."""
    if not h:
        return ""
    since = h["since"]
    at = since[11:16] + "Z" if since[:10] == now_iso()[:10] else since[5:16] + "Z"
    if h.get("attached"):
        return f"holding {h['events']} event(s) for an attached client pid {h['pid']} since {at}"
    return f"holding {h['events']} event(s) at an open question since {at}"


def hold_note(sid: str, slug: str) -> str:
    """The hold of a role's watcher on this host, as `hold_text` says it ('' when none)."""
    sc = load_sidecar(sidecar_path(sid, slug))
    pid = sc.get("pid", "")
    return hold_text(current_hold(sc, bool(pid) and os.path.exists(f"/proc/{pid}")))


def muse_session_busy(session: str, ecosystem: str = "muse") -> bool:
    """True while an exec turn (or, for Muse and Codex, an attached interactive client) holds the session."""
    try:
        if ecosystem == "codex":
            import codex_harness
            return codex_harness.busy(session)
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


def _limit_until(sid: str, account: str | None = None) -> str:
    try:
        from health import limit_until_sid
        return limit_until_sid(sid, account)
    except Exception:  # noqa: BLE001 - a missing or unreadable health state never holds a wake
        return ""


def claude_session_busy(session: str) -> bool:
    """True while the Claude session is mid-turn, or at an open question prompt, where a message would
    interrupt the turn or land on the prompt. A session that only ended its turn self-reported
    'blocked' (waiting on the maintainer, no prompt) is NOT busy: holding wakes from it deadlocked three
    lanes, each waiting for exactly the message being held (measured 2026-10-13)."""
    return bool(claude_session_hold(session))


def claude_session_hold(session: str) -> str:
    """Why a Claude session takes no wake now: 'busy' (mid-turn), 'question' (at an open question prompt), or ''."""
    try:
        from health import claude_busy, claude_prompt_open
        st = next((v for k, v in claude_busy().items() if session and k.startswith(session[:8])), "")
        if st == "blocked":
            from backends import fake_sessions, get as get_backend
            row = {}
            fake = fake_sessions()
            rows = (fake.get("claude") or []) if fake is not None else get_backend("claude")._agents()
            from backends import claude_find
            row = claude_find(rows, session) or {}
            return "question" if claude_prompt_open(row, session) else ""
    except Exception:  # noqa: BLE001
        return ""
    return "busy" if st == "busy" else ""


def hold_reason(wake: str, session: str, ecosystem: str, repo_dir: str) -> str:
    """Why a detached watcher holds its new events this tick: `attached:<pid>` (an interactive client holds the exec
    session), `question` (a Claude session at an open question), `busy` (a turn in flight), or '' (deliver)."""
    if wake == "direct":
        return claude_session_hold(session)
    if wake.startswith("exec:"):
        thread = wake[len("exec:"):]
        pid = attached_pid(thread, ecosystem)
        if pid:
            return f"attached:{pid}"
        return "busy" if muse_session_busy(thread, ecosystem) else ""
    return "busy" if session_busy(session, repo_dir) else ""


def claude_deliver(sid: str, spec, role, session: str, text: str, through: bool = False) -> str:
    """A detached Claude watcher's delivery (`--wake direct`): resume a retired session, or stop and
    resume an idle one (features.health.direct_wake), or with `through` one at an open question (`through_question`).
    Returns the channel, as backends.ClaudeBackend.deliver names it."""
    if spec is None or role is None:
        return "no-adapter (no spec or role to deliver for)"
    from backends import get as get_backend
    try:
        return get_backend("claude").deliver(spec, role, session, text, through_question=through,
                                             confirm=(lambda: question_stays(spec, role, session)) if through else None)
    except (OSError, subprocess.SubprocessError) as exc:  # a CLI call that timed out: the events are kept and retried
        return f"FAILED ({short(str(exc), 120)})"


def is_answer(m: dict, slug: str, asks: list) -> bool:
    """A msg is the maintainer's answer to an ask slug has now: by `user`, to slug itself, woken, and naming one by its
    id or opening words (`re ask <n> [<id>] "<words>"`, the Asks dialog's Submit). A number alone is no proof of which
    ask was meant, and an ask since answered, replaced or reworded is not the one answered."""
    import ask_answers
    return (m.get("by") == "user" and m.get("to") == slug and bool(m.get("wake")) and m.get("kind") != "request"
            and any(r["how"] in ("id", "quote") for r in ask_answers.resolve(m.get("text", ""), asks)))


def question_stays(spec, role, session: str) -> str:
    """Why a watcher may not stop a Claude session's turn at its open question ('' when it may)."""
    from backends import direct_wake_mode, get as get_backend, only_waiters, work_in_flight
    from health import claude_job_state, claude_open_prompt
    import park
    if direct_wake_mode(spec) != "idle":
        return "features.health.direct_wake is dead: a live worker is never stopped"
    try:
        if not claude_open_prompt(session):
            return "its transcript shows no open question: the prompt is another kind, or just closed"
        row = get_backend("claude")._row(session, include_stopped=True) or {}
        st = claude_job_state(row)
        if work_in_flight(st) and not only_waiters(st, role.slug, row.get("pid")):
            return "it has background work in flight"
        pids = park.claude_attach_pids(role, row)
    except Exception as exc:  # noqa: BLE001 - unknown is not "nobody there"
        return f"its session could not be read: {short(str(exc), 80)}"
    return f"a client is attached: pid {pids[0]}, the person there answers it" if pids else ""


def through_question(view: View, slug: str, new: list[tuple[str, str]], spec, role, session: str, sc: dict) -> str:
    """`<msg>@<question>` when a watcher's undelivered events hold the maintainer's answer and the Claude session's open
    question may be closed to deliver it (stop the turn, resume it with the wake), else ''. Every other event (a
    broadcast, an offer, a queue order, a plain note) waits for the question. One answer never closes two questions:
    `sc['through']` keeps the last one delivered this way, and only a retry at the same question may repeat it."""
    ids = {i for i, _ in new}
    asks = list((view.current_queue(slug) or {}).get("asks") or []) if spec is not None and role is not None else []
    got = [m["id"] for m in view.msgs_to(slug) if m["id"] in ids and is_answer(m, slug, asks)]
    if not got or question_stays(spec, role, session):
        return ""
    from health import claude_open_prompt
    last, token = sc.get("through", ""), f"{got[-1]}@{claude_open_prompt(session)}"
    return "" if last.split("@")[0] == got[-1] and last != token else token


def fire(wake: str, text: str, repo_dir: str, ecosystem: str = "muse", model: str = "", effort: str = "") -> bool:
    if wake.startswith("exec:"):
        sid = wake[len("exec:"):]
        try:
            from backends import get as get_backend
            # a Codex turn without a model keeps the thread's last one, so a spec change never reached it (2026-11-18:
            # a thread put on an unavailable model by its native view went on failing after the spec was reverted)
            kw = {"model": model, "effort": effort} if ecosystem == "codex" else {}
            get_backend(ecosystem if ecosystem in ("muse", "codex") else "muse").send(sid, repo_dir, text, **kw)
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


def _mtime(path: str) -> float:
    try:
        return os.path.getmtime(path) if path else 0.0
    except OSError:
        return 0.0


def cmd_ledger_watch(a) -> int:
    sid, slug = a.sid, a.slug
    try:
        sys.stdout.reconfigure(line_buffering=True)  # a watcher's output is read through pipes/logs
    except Exception:
        pass
    try:
        spec_path = find_spec(sid, a.spec)
        spec = ShoalSpec.load(spec_path)
    except OSError:
        spec_path, spec = "", None
    spec_mtime = _mtime(spec_path)
    role = spec.role(slug) if spec else None
    repo_dir = a.dir or (spec.dir if spec else os.getcwd())
    sc_path = a.sidecar or sidecar_path(sid, slug)
    sc = load_sidecar(sc_path)
    session = a.session or sc.get("session_id", "") or (role.session if role else "")
    # a Claude session knows its own id; a lane once passed its Remote Control bridge id instead
    # (measured 2026-10-12), which restart could not match to the launched session
    own = os.environ.get("CLAUDE_CODE_SESSION_ID", "")
    if own and not a.wake.startswith("exec:") and session != own and not own.startswith(session or "\0"):
        print(f"note: --session {session or '(none)'} is not this Claude session; binding {own}")
        session = own
    # `relay`, the retired wake relay's mode, is read as `direct`: a watcher armed before the retirement
    # re-executes in place with its old argv after an upgrade, and must keep delivering with no re-arm
    a.wake = canon_wake(a.wake)
    persistent = is_detached_wake(a.wake) and not a.test
    if persistent:
        # a send:, exec: or direct watcher keeps running after each wake (below), so arming twice would wake
        # twice: a second arm for the same role, wake and session finds the first alive and leaves it be
        old_pid = sc.get("pid", "")
        try:
            with open(f"/proc/{old_pid}/cmdline", "rb") as fh:
                argv = [x.decode(errors="replace") for x in fh.read().split(b"\0") if x]
            opt = {argv[i]: argv[i + 1] for i in range(len(argv) - 1) if argv[i].startswith("--")}
            # the session the running watcher actually bound (it rebinds to its own Claude session) counts,
            # not only its argv: a role once armed with a stale id and a second arm started a duplicate
            bound = {(opt.get("--session") or "")[:8], (sc.get("session_id") or "")[:8]} - {""}
            same = (opt.get("--slug") == slug and canon_wake(opt.get("--wake") or "") == a.wake
                    and (a.wake.startswith(("send:", "exec:")) or (session or "")[:8] in bound))
            if old_pid and int(old_pid) != os.getpid() and "ledger-watch" in argv and same:
                print(f"ALREADY ARMED: ledger-watch pid {old_pid} for {slug} ({a.wake}) is running and keeps "
                      f"running across wakes; nothing to do")
                return 0
        except (OSError, ValueError):
            pass
    # fetch_fail starts over: a re-arm inheriting a dead watcher's count ended on its first miss, so every
    # quorum re-arm during an outage was spent within a tick (2026-11-04: one role's reached 7, then stayed dead)
    sc.update(session_id=session, pid=str(os.getpid()), armed_utc=now_iso(),
              wake=a.wake, interval=str(a.interval), fetch_fail="0")
    sc.setdefault("delivered", "")
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
    fails = 0
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
        m = _mtime(spec_path)
        if m and m != spec_mtime:  # the spec changed (a `set-fill`, say): read the role again, keep the session
            try:
                spec = ShoalSpec.load(spec_path)
                role = spec.role(slug) or role
            except (OSError, ValueError) as exc:
                print(f"{now_iso()} spec reload failed: {short(str(exc), 120)}", file=sys.stderr)
            spec_mtime = m
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
            note_hold(sc, "", 0)
            save_sidecar(sc_path, sc)
            continue
        held = hold_reason(a.wake, session, role.ecosystem if role else "muse", repo_dir) if persistent else ""
        through = through_question(view, slug, new, spec, role, session or "", sc) \
            if a.wake == "direct" and held in ("", "question") else ""
        if through:
            held = ""
        if held:
            # never wake a lane mid-turn, never collide with a person at it (an attached client, an open
            # question): the events stay undelivered and go out at the next idle tick. A person's hold is
            # recorded, so health can page it and `next`/`show` can say it (measured 2026-11-22: a client opened
            # from a card held a user offer for 6 h, and health paged it as a wake without effect)
            pid = int(held.split(":", 1)[1]) if held.startswith("attached:") else 0
            client = "" if not pid else sc.get("held_client", "") if sc.get("held_reason") == held else \
                client_desc(pid, a.wake[len("exec:"):])
            note_hold(sc, held, len(new), client)
            save_sidecar(sc_path, sc)
            continue
        if persistent and a.wake == "direct" and _limit_until(sid, getattr(role, "account", None) if role is not None else None):
            # inside the account usage limit a delivered wake errors in the role's turn and is lost
            # (2026-10-14): hold the events; the first tick after the reset delivers them
            note_hold(sc, "", 0)
            save_sidecar(sc_path, sc)
            continue
        ids = [i for i, _ in new]
        delivered |= set(ids)
        sc["delivered"] = ",".join(list(delivered)[-DELIVERED_CAP:])
        sc["last_wake"] = now_iso()
        note_hold(sc, "", 0)
        save_sidecar(sc_path, sc)
        reason = ",".join(sorted({d.split()[0] for _, d in new}))
        text = (f"{label} at {now_iso()} ({len(new)} new): " + " | ".join(d for _, d in new)
                + f" || next: `shoal show --shoal {sid} --slug {slug}`; act (claim/msg/release), transition"
                + ("; your watcher keeps running (no re-arm)." if persistent else ", re-arm."))
        emit_wake(sid, slug, session, role.ecosystem if role else "", reason, len(new))
        if a.wake == "direct":
            if through:
                was, sc["through"] = sc.get("through", ""), through
                save_sidecar(sc_path, sc)
            ch = claude_deliver(sid, spec, role, session or "", text, through=bool(through))
            print(f"{now_iso()} delivered via {ch}", flush=True)
            if ch.startswith("deferred"):
                # nothing failed: the session cannot take a direct wake yet (work in flight wakes it
                # anyway), so the events stay undelivered for a later tick
                sc["delivered"] = ",".join(x for x in sc["delivered"].split(",") if x not in set(ids))
                if through:
                    sc["through"] = was  # nothing was stopped
                if "at a question" in ch:
                    note_hold(sc, "question", len(new))
                save_sidecar(sc_path, sc)
                continue
            fired = not ch.startswith(("FAILED", "no-adapter"))
        else:
            fired = fire(a.wake, text, repo_dir, role.ecosystem if role else "muse",
                         getattr(role, "model", "") or "", getattr(role, "effort", "") or "")
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
    p.add_argument("--wake", default="exit", help="exit | direct (Claude, detached: resumes the session directly; `relay` is an old name for it) | send:<session-id> (opencode, detached) | exec:<session-id> (Muse: starts one `muse exec` turn per wake, detached) | print")
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
