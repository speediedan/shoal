#!/usr/bin/env python3
"""The shoal observation contract (v1): one read-only status document for every shoal on a host.

  shoal ui status [--shoal ID ...] [--all] [--include-retired] [--conditions] [--pull] [--json]
  shoal ui line   [--shoal ID ...] [--all]

Every UI adapter (docs/ui.md) binds to this document and to nothing else, so shoal semantics live
in one place: claim and offer resolution, read-time permissions, posture and blockers come from
`ledger.View`; liveness from the harness probes the health quorum already uses. The contract is
versioned (`v`) and additive: a reader ignores fields it does not know.

READ-ONLY by construction (design driver 1): no ledger pull unless `--pull`, no commit, no mail
marked read (this never calls `next`), no wake. `--conditions` runs the health quorum's own
`evaluate()` (it probes providers, so it is cached for `CONDITIONS_TTL_S` per shoal) and is the
only part that leaves the host.
"""
from __future__ import annotations

import datetime
import glob
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.dirname(HERE)
for _p in (PKG, HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import config  # noqa: E402
from layout import GROUPS_DIR, SPEC_FILE  # noqa: E402
from roles import ShoalSpec, is_wake_relay, state_dir  # noqa: E402

CONTRACT_V = 1
CONDITIONS_TTL_S = 300
PAGE_WINDOW_H = 24
USER_WAITS = ("user-auth", "user-design", "user-feedback")


def utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def iso(dt: datetime.datetime | None = None) -> str:
    return (dt or utcnow()).strftime("%Y-%m-%dT%H:%M:%SZ")


def age_s(ts: str, now: datetime.datetime | None = None) -> int | None:
    try:
        t = datetime.datetime.strptime((ts or "")[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=datetime.timezone.utc)
    except ValueError:
        return None
    return int(((now or utcnow()) - t).total_seconds())


# ---- discovery -------------------------------------------------------------------------

def ledger_root(explicit: str = "") -> str:
    return explicit or config.ledger_dir()


def ledger_roots(explicit: str = "") -> list[str]:
    """Where to look for shoals: the explicit root, else the host's ledger clone plus any extra
    roots in SHOAL_UI_LEDGER_ROOTS (colon-separated), so one UI can explore several ledger clones."""
    if explicit:
        return [explicit]
    roots = [config.ledger_dir()] + [r for r in config.env("UI_LEDGER_ROOTS").split(":") if r]
    return list(dict.fromkeys(os.path.expanduser(r) for r in roots))


def discover_all(explicit: str = "", ids: list[str] | None = None, include_retired: bool = False) -> list[str]:
    out = []
    for r in ledger_roots(explicit):
        out += [p for p in discover(r, ids, include_retired) if p not in out]
    return out


def discover(root: str, ids: list[str] | None = None, include_retired: bool = False) -> list[str]:
    """Spec paths under `<root>/shoals/*/shoal.json`, filtered to `ids` when given. Retired groups are
    skipped unless asked for (they launch nothing and their roles are gone)."""
    out = []
    for p in sorted(glob.glob(os.path.join(root, GROUPS_DIR, "*", SPEC_FILE))):
        sid = os.path.basename(os.path.dirname(p))
        if ids and sid not in ids:
            continue
        if not include_retired and not ids:
            try:
                if json.load(open(p)).get("retired_utc"):
                    continue
            except (OSError, ValueError):
                continue
        out.append(p)
    return out


# ---- harness liveness (one probe per call, shared by every shoal) ----------------------

def claude_rows() -> list[dict]:
    """`claude agents --json --all` rows (stopped sessions included: no `pid` is a dead worker,
    FM-14). SHOAL_FAKE_SESSIONS answers it in tests."""
    from backends import fake_sessions
    fake = fake_sessions()
    if fake is not None:
        return list(fake.get("claude") or [])
    try:
        r = subprocess.run(["claude", "agents", "--json", "--all"], capture_output=True, text=True,
                           timeout=30, env=config.tool_env())
        return json.loads(r.stdout or "[]") if r.returncode == 0 else []
    except (OSError, ValueError, subprocess.SubprocessError):
        return []


def claude_session(rows: list[dict], session: str) -> dict | None:
    if not session:
        return None
    for e in rows:
        sid = str(e.get("sessionId", ""))
        if sid == session or sid.startswith(session) or session.startswith(sid) and sid:
            return e
    return None


# session_state() -> health.session_between_turns() semantics: True between turns, False mid-turn
# or on a question, None unknown
BETWEEN = {"idle": True, "working": False, "blocked": False}

LIVENESS = {"busy": "working", "blocked": "blocked", "idle": "idle"}


def claude_turn(row: dict) -> str:
    """working | blocked | idle | dead | unknown for one `claude agents --json --all` row. The turn
    signal is the health quorum's own (`health.claude_liveness`: the job's tempo, then state), never
    `state` alone, which reads `working` while a background watcher runs. No pid is a dead worker."""
    import health as _h
    if not row.get("pid"):
        return "dead"
    return LIVENESS.get(_h.claude_liveness(row), "unknown")


def is_remote(role) -> bool:
    """A role whose session runs on another host: no local sidecar, no local harness row (as doctor)."""
    return bool(role.server) and role.server not in (os.uname().nodename, config.server())


def session_state(role, rows: list[dict], opencode_probe: bool, repo_dir: str = "") -> str:
    """working | blocked | idle | dead | unknown | remote | parked (no session)."""
    if not role.session:
        return "parked"
    if is_remote(role):
        return "remote"
    if role.ecosystem == "claude":
        e = claude_session(rows, role.session)
        if e is None:
            return "unknown"
        return claude_turn(e)
    if role.ecosystem == "opencode" and opencode_probe:
        try:
            from backends import get as get_backend
            s = get_backend("opencode").status(role.session, repo_dir or os.getcwd())
        except Exception:  # noqa: BLE001
            return "unknown"
        if "status:idle" in s:
            return "idle"
        if "status:busy" in s or "status:running" in s or "status:working" in s:
            return "working"
        return "unknown"
    return "unknown"


# ---- per role --------------------------------------------------------------------------

def _short(s, n: int = 80) -> str:
    s = " ".join(str(s or "").split())
    return s if len(s) <= n else s[: n - 1] + "~"


def role_row(spec: ShoalSpec, view, role, rows: list[dict], now: datetime.datetime,
             opencode_probe: bool = True) -> dict:
    import blockers as _bl
    import health as _h
    from heartbeat import age_min, hb_path
    from ledger import item_key
    slug = role.slug
    q = view.current_queue(slug) or {}
    st = view.role_state(slug) or {}
    idle = view.is_idle(slug)
    # `hposture` is the ledger's word (health's predicates take it); `posture` is the contract's
    hposture = st.get("state") or ("idle" if idle else "active")
    posture = "wait" if hposture == "waiting" else hposture
    boundary = max(q.get("ts", ""), st.get("ts", ""))
    sub = st.get("sub", "")
    nb = q.get("next_blocked") or {}
    try:
        stale_nb = _bl.stale(spec, view, slug, q, live=False)
    except Exception:  # noqa: BLE001
        stale_nb = {}
    nxt = []
    for x in q.get("next") or []:
        k = item_key(x)
        b = next((v for kk, v in nb.items() if item_key(kk) == k), None)
        nxt.append({"item": x, "blocker": b, "stale": k in stale_nb or None})
    remote = is_remote(role)
    alive, sc = (False, {}) if remote else _h.watcher_alive(spec.sid, slug)
    sess = session_state(role, rows, opencode_probe, spec.dir)
    asks = list(q.get("asks") or [])
    mail = [m for m in view.msgs_to(slug, boundary)]
    last = view.last_by.get(slug, "")
    hb = age_min(hb_path(spec.sid, slug))

    # can this role make progress? (the stalls measured 2026-10-08, in order of severity)
    reasons = []
    if not role.session:
        reasons.append("parked")
    else:
        if sess == "dead":
            reasons.append("dead-worker")
        if sess == "blocked":
            reasons.append("blocked-on-question")
        # the quorum's own predicate; a session mid-turn (or on a question) arms its watcher at the
        # turn's end, and a dead worker is reported as dead-worker instead
        if not remote and not is_wake_relay(role) and sess != "dead" and \
                _h.watcher_dead(alive, hposture, BETWEEN.get(sess)):
            reasons.append("watcher-dead")
        for x in q.get("next") or []:
            b = next((v for kk, v in nb.items() if item_key(kk) == item_key(x)), "")
            if b and _bl.kind(b) == "role" and _bl.takeable(spec, view, b):
                reasons.append("blocker-role-cannot-take")
                break
        # the quorum's own predicate, with blocker checks kept off the network (live=False)
        free, _ = _h.parked_free(spec, view, role, q, hposture, sub, live=False)
        if free:
            reasons.append("parked-work")
        if asks or sub in USER_WAITS:
            reasons.append("waiting-on-you")
    blocking = [r for r in reasons if r != "waiting-on-you" and r != "parked"]
    return {
        "slug": slug, "tag": role.tag, "harness": role.ecosystem, "model": role.model, "fill": role.fill,
        "server": role.server or "", "aux": bool(role.aux), "relay": is_wake_relay(role),
        "session_id": role.session or None,
        "posture": posture if role.session else "parked", "sub": sub or None,
        "posture_age_s": age_s(boundary, now) if boundary else None,
        "wait": ({"sub": sub, "target": st.get("target") or None, "holder": _short(st.get("holder"), 120) or None,
                  "rescan_utc": st.get("rescan_utc") or None} if posture == "wait" else None),
        "active": q.get("active") or None,
        "next": nxt,
        "blocker_stale": bool(stale_nb),
        "offers_open": len(view.queue(slug)),
        "mail_since_boundary": len(mail),
        "asks": [_short(a, 160) for a in asks],
        "watcher": "remote" if remote else ("alive" if alive else ("none" if not sc else "dead")),
        "session": sess,
        "last_write_age_s": age_s(last, now) if last else None,
        "heartbeat_age_s": int(hb * 60) if hb is not None else None,
        "can_progress": {"ok": not blocking, "reasons": reasons},
        "needs_you": bool({"dead-worker", "blocked-on-question", "waiting-on-you",
                           "blocker-role-cannot-take"} & set(reasons)),
        "view": [config.cli(), "view", spec.sid, slug],
    }


# ---- per shoal -------------------------------------------------------------------------

def tooling(spec: ShoalSpec) -> dict:
    sha = (spec.tooling or {}).get("sha", "")
    try:
        r = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, timeout=10, cwd=PKG)
        head = r.stdout.strip() if r.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        head = ""
    return {"sha": sha[:12] or None, "head": head[:12] or None,
            "current": bool(sha and head and head.startswith(sha[:12]))}


def conditions(spec: ShoalSpec, view, cache_dir: str, refresh: bool = False) -> dict:
    """The health quorum's live conditions, from its own `evaluate()` (the derivation, not a copy).
    It probes providers (gh) and the harnesses, so the result is cached per shoal for
    CONDITIONS_TTL_S and never computed on a keystroke path. Ladder step and age arrive with the
    `health_condition` telemetry event; until then this section carries instances only."""
    path = os.path.join(cache_dir, "ui-conditions.json")
    try:
        cached = json.load(open(path))
        a = age_s(cached.get("ts", ""))
        if not refresh and a is not None and a < CONDITIONS_TTL_S:
            cached["age_s"] = age_s(cached["ts"])
            return cached
    except (OSError, ValueError):
        pass
    import health as _h
    try:
        inst = _h.evaluate(spec, view, _h.claude_busy())
    except Exception as exc:  # noqa: BLE001
        return {"source": "health-dry-run", "ts": iso(), "age_s": 0, "open": [], "error": _short(exc, 200)}
    out = {"source": "health-dry-run", "ts": iso(), "age_s": 0,
           "open": [{"slug": c["slug"], "condition": c["condition"], "since": c.get("since"),
                     "detail": _short(c.get("detail"), 200)} for c in inst]}
    if any(is_wake_relay(r) for r in spec.roles) and not _h.relay_alive(spec.sid):
        out["open"].append({"slug": "wake-relay", "condition": "relay-dead", "since": None,
                            "detail": "relay-watch not running"})
    try:
        os.makedirs(cache_dir, exist_ok=True)
        tmp = path + f".{os.getpid()}.tmp"
        with open(tmp, "w") as fh:
            json.dump(out, fh)
        os.replace(tmp, path)
    except OSError:
        pass
    return out


def shoal_doc(spec_path: str, rows: list[dict], with_conditions: bool = False, pull: bool = False,
              opencode_probe: bool = True, refresh_conditions: bool = False) -> dict:
    import health as _h
    from ledger import Ledger, View
    spec = ShoalSpec.load(spec_path)
    now = utcnow()
    led = Ledger(spec.sid, root=spec.ledger_dir or None, sync=pull)
    if pull:
        led.pull()
    if not led.exists():
        return {"id": spec.sid, "spec": spec_path, "error": f"no ledger at {led.ldir}"}
    view = View(led, spec)
    roles = [role_row(spec, view, r, rows, now, opencode_probe) for r in spec.roles]
    pages = [m for m in view.msgs if m.get("to") == "user" and m.get("kind") == "health"
             and (age_s(m.get("ts", ""), now) if age_s(m.get("ts", ""), now) is not None else 10**9)
             < PAGE_WINDOW_H * 3600]
    off, frz = view.user_offline(), view.freeze()
    has_relay = any(is_wake_relay(r) for r in spec.roles)
    doc = {
        "id": spec.sid, "spec": spec_path, "incarnation": spec.incarnation,
        "gen": f"r{spec.incarnation + 1}", "retired": bool(spec.retired_utc), "dir": spec.dir,
        "mode": {"offline": bool(off), "offline_until": (off or {}).get("until"),
                 "freeze": bool(frz)},
        "relay": ({"alive": _h.relay_alive(spec.sid)} if has_relay else None),
        "pages_24h": len(pages),
        "last_page": _short(pages[-1].get("text"), 160) if pages else None,
        "asks_open": sum(len(r["asks"]) for r in roles),
        "needs_you": sum(1 for r in roles if r["needs_you"]),
        "stalled": sum(1 for r in roles if not r["can_progress"]["ok"] and r["session_id"]),
        "violations": len(view.violations), "contested": len(view.contested),
        "tooling": tooling(spec),
        "features": sorted((spec.features or {}).keys()),
        "roles": roles,
    }
    if with_conditions:
        doc["conditions"] = conditions(spec, view, state_dir(spec.sid), refresh_conditions)
    return doc


def status(ids: list[str] | None = None, root: str = "", include_retired: bool = False,
           with_conditions: bool = False, pull: bool = False, opencode_probe: bool = True,
           refresh_conditions: bool = False) -> dict:
    import socket
    rows = claude_rows()
    shoals = []
    for p in discover_all(root, ids, include_retired):
        try:
            shoals.append(shoal_doc(p, rows, with_conditions, pull, opencode_probe, refresh_conditions))
        except Exception as exc:  # noqa: BLE001 - one broken spec never hides the others
            shoals.append({"id": os.path.basename(os.path.dirname(p)), "spec": p, "error": _short(exc, 200)})
    return {"v": CONTRACT_V, "host": socket.gethostname(), "ts": iso(), "shoals": shoals}


# ---- shared cache: one status computation per host, many readers -----------------------

CACHE_MAX_AGE_S = 45


def cache_path() -> str:
    base = os.environ.get("XDG_RUNTIME_DIR") or "/tmp"
    return os.path.join(base, f"shoal-ui-{os.getuid()}", "status.json")


def write_cache(doc: dict) -> None:
    p = cache_path()
    try:
        os.makedirs(os.path.dirname(p), mode=0o700, exist_ok=True)
        tmp = p + f".{os.getpid()}.tmp"
        with open(tmp, "w") as fh:
            json.dump(doc, fh, default=str)
        os.replace(tmp, p)
    except OSError:
        pass


def cached_status(ids: list[str] | None = None, max_age_s: float = CACHE_MAX_AGE_S, force: bool = False) -> dict:
    """The all-shoal document the feeder last wrote, when fresh; else computed (and cached). Role
    cards, the overview and the status line read this, so N panes cost one computation per tick,
    not N. Filtered to `ids` after reading."""
    doc = None
    if not force:
        try:
            d = json.load(open(cache_path()))
            a = age_s(d.get("ts", ""))
            if d.get("v") == CONTRACT_V and a is not None and a <= max_age_s:
                doc = d
        except (OSError, ValueError):
            pass
    if doc is None:
        doc = status(None)
        write_cache(doc)
    if ids:
        doc = dict(doc, shoals=[s for s in doc["shoals"] if s.get("id") in ids])
    return doc


# ---- rendering -------------------------------------------------------------------------

def _age(s: int | None) -> str:
    if s is None:
        return "-"
    if s < 90:
        return f"{s}s"
    if s < 5400:
        return f"{s // 60}m"
    if s < 172800:
        return f"{s / 3600:.1f}h"
    return f"{s / 86400:.1f}d"


def posture_label(r: dict) -> str:
    """The one-word posture a sidebar shows: DEAD, ASK, IDLE? (idle on free work), else posture."""
    reasons = r["can_progress"]["reasons"]
    if "dead-worker" in reasons:
        return "DEAD"
    if "blocked-on-question" in reasons:
        return "QUESTION"
    if "watcher-dead" in reasons:
        return "NOWATCH"
    if "parked-work" in reasons:
        return f"{r['posture'].upper()}?"
    if r["posture"] == "wait" and r["sub"]:
        return f"wait:{r['sub']}"
    return r["posture"]


def render_text(doc: dict) -> str:
    out = []
    for s in doc["shoals"]:
        if s.get("error"):
            out.append(f"== {s['id']}: ERROR {s['error']}")
            continue
        m = s["mode"]
        flags = [x for x, on in (("OFFLINE", m["offline"]), ("FREEZE", m["freeze"])) if on]
        relay = s.get("relay")
        out.append(f"== {s['id']} {s['gen']}  " + " ".join(flags + [
            f"needs-you {s['needs_you']}", f"stalled {s['stalled']}", f"asks {s['asks_open']}",
            f"pages24h {s['pages_24h']}"] + ([f"relay {'ok' if relay['alive'] else 'DEAD'}"] if relay else [])
            + ([] if s["tooling"]["current"] else [f"tooling {s['tooling']['sha']} != head {s['tooling']['head']}"])))
        for r in s["roles"]:
            if r["relay"] or not r["session_id"]:
                continue
            why = ",".join(x for x in r["can_progress"]["reasons"] if x != "parked")
            nx = " ".join(n["item"] + ("[B]" if n["blocker"] else "") + ("!" if n["stale"] else "") for n in r["next"])
            out.append(f"  {r['slug']:<34} {posture_label(r):<16} {r['session']:<8} "
                       f"act={r['active'] or '-'}  next={nx or '-'}  last={_age(r['last_write_age_s'])}"
                       + (f"  asks={len(r['asks'])}" if r["asks"] else "")
                       + (f"  offers={r['offers_open']}" if r["offers_open"] else "")
                       + (f"  [{why}]" if why else ""))
        for c in (s.get("conditions") or {}).get("open", []):
            out.append(f"  ! {c['slug']}: {c['condition']}: {c['detail'][:110]}")
    return "\n".join(out)


def render_line(doc: dict) -> str:
    """One line for a tab bar: per shoal, needs-you / stalled / asks, and mode flags."""
    parts = []
    for s in doc["shoals"]:
        if s.get("error"):
            parts.append(f"{s['id'][:18]} ERR")
            continue
        m = s["mode"]
        bits = [f"{s['needs_you']}!" if s["needs_you"] else "ok"]
        if s["stalled"]:
            bits.append(f"{s['stalled']} stalled")
        if s["asks_open"]:
            bits.append(f"{s['asks_open']} asks")
        if m["offline"]:
            bits.append("OFF")
        if m["freeze"]:
            bits.append("FRZ")
        if s.get("relay") and not s["relay"]["alive"]:
            bits.append("relay DEAD")
        import re
        parts.append(f"{re.sub(r'-[0-9]{8}-[0-9]{6}$', '', s['id'])}: {' '.join(bits)}")
    return " | ".join(parts) or "no shoals"


def add_args(p) -> None:
    p.add_argument("--shoal", dest="ids", action="append", default=[], help="a shoal id (repeatable); default all")
    p.add_argument("--all", action="store_true", help="every non-retired shoal in the ledger clone (the default)")
    p.add_argument("--include-retired", action="store_true")
    p.add_argument("--ledger-root", default="", help="ledger clone root (default: host config)")
    p.add_argument("--no-opencode-probe", action="store_true", help="skip the per-role OpenChamber status call")


def cmd_status(a) -> int:
    doc = status(a.ids or None, a.ledger_root, a.include_retired, a.conditions, a.pull,
                 not a.no_opencode_probe, getattr(a, "refresh_conditions", False))
    print(json.dumps(doc, indent=1, default=str) if a.json else render_text(doc))
    return 0 if not any(s.get("error") for s in doc["shoals"]) else 1


def cmd_line(a) -> int:
    print(render_line(cached_status(a.ids or None) if not a.ledger_root else
                      status(a.ids or None, a.ledger_root, False, False, False, False)))
    return 0


# ---- quiet check: prove that viewing woke nobody ---------------------------------------

def _claude_transcript(session: str) -> str:
    base = os.path.expanduser("~/.claude/projects")
    hits = glob.glob(os.path.join(base, "*", f"{session}*.jsonl")) if session else []
    return max(hits, key=os.path.getmtime) if hits else ""


def _opencode_updated() -> dict[str, int]:
    from backends import fake_sessions
    if fake_sessions() is not None:
        return {}
    try:
        r = subprocess.run(["openchamber", "session", "list", "--limit", "50", "--json"], capture_output=True,
                           text=True, timeout=30, env=config.tool_env())
        return {s["id"]: int((s.get("time") or {}).get("updated") or 0)
                for s in json.loads(r.stdout or "{}").get("sessions", [])}
    except (OSError, ValueError, subprocess.SubprocessError):
        return {}


def quiet_snapshot(ids: list[str] | None = None) -> dict:
    """Per role: the harness's own activity evidence (Claude transcript size, opencode `updated`),
    the role's ledger line count and its posture. Read-only."""
    from roles import ShoalSpec
    from ledger import Ledger
    oc = _opencode_updated()
    rows = claude_rows()
    snap = {"ts": iso(), "roles": {}}
    for p in discover_all("", ids):
        spec = ShoalSpec.load(p)
        led = Ledger(spec.sid, root=spec.ledger_dir or None, sync=False)
        for r in spec.roles:
            if not r.session:
                continue
            key = f"{spec.sid}/{r.slug}"
            lines = 0
            try:
                lines = sum(1 for _ in open(led.writer_path(r.slug)))
            except OSError:
                pass
            ev = {"harness": r.ecosystem, "ledger_lines": lines, "session": session_state(r, rows, False)}
            if r.ecosystem == "claude":
                t = _claude_transcript(r.session)
                ev["transcript"] = t or None
                ev["transcript_bytes"] = os.path.getsize(t) if t else None
            elif r.ecosystem == "opencode":
                ev["updated_ms"] = oc.get(r.session)
            snap["roles"][key] = ev
    return snap


def transcript_since(path: str, offset: int) -> dict[str, int]:
    """What a Claude transcript gained after `offset` bytes: completed turns (`turn_duration`),
    prompts and notifications that start turns (`user` entries that are not tool results), recaps
    (`away_summary`: a model call Claude Code makes when a client returns, not a turn) and other."""
    out = {"turns": 0, "turn_inputs": 0, "recaps": 0, "other": 0}
    try:
        with open(path, "rb") as fh:
            fh.seek(offset)
            chunk = fh.read().decode(errors="replace")
    except OSError:
        return out
    for line in chunk.splitlines():
        try:
            e = json.loads(line)
        except ValueError:
            continue
        sub = e.get("subtype")
        if sub == "turn_duration":
            out["turns"] += 1
        elif sub == "away_summary":
            out["recaps"] += 1
        elif e.get("type") == "user" and not any(
                isinstance(c, dict) and c.get("type") == "tool_result"
                for c in ((e.get("message") or {}).get("content") or []) if isinstance(c, dict)):
            out["turn_inputs"] += 1
        else:
            out["other"] += 1
    return out


def cmd_quiet_check(a) -> int:
    """Compare two snapshots. A Claude role's transcript tail is classified (turns versus recaps),
    because `claude agents` state reads `working` while a background watcher runs, so the
    harness state is not an idle signal. Exit 1 when any role took a turn, ran a recap or wrote
    to its ledger file; a turn that an offer or wake explains is still reported, for review."""
    if a.save:
        snap = quiet_snapshot(a.ids or None)
        with open(a.save, "w") as fh:
            json.dump(snap, fh, indent=1)
        print(f"quiet-check: snapshot of {len(snap['roles'])} roles -> {a.save}")
        return 0
    if not a.compare:
        print("quiet-check: give --save FILE or --compare FILE")
        return 2
    old = json.load(open(a.compare))
    new = quiet_snapshot(a.ids or None)
    moved = 0
    print(f"quiet-check: {old['ts']} -> {new['ts']}")
    for key, o in sorted(old["roles"].items()):
        n = new["roles"].get(key)
        if n is None:
            print(f"  {key:<48} gone")
            continue
        grew = []
        if n.get("transcript") and (n.get("transcript_bytes") or 0) > (o.get("transcript_bytes") or 0):
            t = transcript_since(n["transcript"], o.get("transcript_bytes") or 0)
            grew += [f"{v} {k}" for k, v in t.items() if v]
        if (n.get("updated_ms") or 0) > (o.get("updated_ms") or 0):
            grew.append("opencode session updated")
        if n["ledger_lines"] > o["ledger_lines"]:
            grew.append(f"ledger +{n['ledger_lines'] - o['ledger_lines']}")
        moved += bool(grew)
        print(f"  {key:<48} {'MOVED: ' + ', '.join(grew) if grew else 'quiet'}")
    print("quiet-check: " + ("nothing moved" if not moved else f"{moved} role(s) moved: review each line"))
    return 1 if moved else 0
