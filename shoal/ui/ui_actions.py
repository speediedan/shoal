#!/usr/bin/env python3
"""Operator actions without a terminal: plan, then apply (the Herdr app's actions dialog; docs/ui.md).

    shoal ui act-plan VERB [--shoal ID] [--role SLUG] [--text T] [--item I] [--note N] [--wake]
                      [--state on|off] [--until ISO] [--no-wake] [--json]
    shoal ui act-apply --token T [--json]

The 0.9.3 palette (`ui_herdr.act`) asks in a terminal, previews a write verb with its own `--dry-run`, names its
wake cost and runs it after a typed confirm. A dialog cannot hold a terminal, so the same steps are split:

- **plan** builds the same shoal argv the palette builds, runs its `--dry-run`, names the cost, and stores the
  argv under a one-time token (mode 0600, in the per-user runtime dir, ten minutes). Read-only verbs (`next`,
  `status`, `conditions`, `asks`, `upgrade`, `roles`) return their text or data at once and store nothing.
- **apply** runs exactly the stored argv once, logs it to `ui-actions.jsonl` like the palette, and forgets the
  token. A caller can only confirm a plan it was shown; it cannot pass its own argv.

Every write is still a shoal CLI verb, which does its own checks. Nothing here types into a pane or wakes a
role except the verbs that say so in their cost note.
"""
from __future__ import annotations

import json
import os
import secrets
import shlex
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.dirname(HERE)
for _p in (PKG, HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

V = 1
PLAN_TTL_S = 600
WRITE_VERBS = ("msg", "offer", "withdraw", "offline", "freeze")
VIEW_VERBS = ("roles", "next", "status", "conditions", "asks", "upgrade")


class ActError(ValueError):
    pass


def plans_dir() -> str:
    import ui_status
    d = os.path.join(os.path.dirname(ui_status.cache_path()), "act-plans")
    os.makedirs(d, mode=0o700, exist_ok=True)
    return d


def _plan_path(token: str) -> str:
    if not token or not all(c in "0123456789abcdef" for c in token) or len(token) != 32:
        raise ActError("not a plan token")
    return os.path.join(plans_dir(), token + ".json")


def _store(plan: dict) -> str:
    token = secrets.token_hex(16)
    path = _plan_path(token)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(dict(plan, created=time.time()), fh)
    return token


def _sweep() -> None:
    """Drop plans past their time (a dialog closed without confirming leaves one behind)."""
    try:
        for f in os.listdir(plans_dir()):
            p = os.path.join(plans_dir(), f)
            if f.endswith(".json") and time.time() - os.path.getmtime(p) > PLAN_TTL_S:
                os.remove(p)
    except OSError:
        pass


def _spec(sid: str):
    from roles import ShoalSpec, find_spec
    if not sid:
        raise ActError("--shoal is required")
    try:
        return ShoalSpec.load(find_spec(sid))
    except Exception as exc:  # noqa: BLE001 - a bad id is the caller's error, said plainly
        raise ActError(f"no shoal {sid}: {exc}") from exc


def _role(spec, slug: str, allow_all: bool = False) -> str:
    slugs = [r.slug for r in spec.roles if r.session]
    if allow_all and slug == "*":
        return slug
    if slug not in slugs:
        raise ActError(f"{slug or '(none)'} is not a role of {spec.sid} with a session"
                       + (" (use * for everyone)" if allow_all else ""))
    return slug


def build(verb: str, a) -> tuple[list[str], str, bool]:
    """(shoal argv, cost note, confirm) for one write verb, as `ui_herdr.act` builds it."""
    spec = _spec(a.shoal)
    sid = spec.sid
    if verb == "msg":
        role = _role(spec, a.role, allow_all=True)
        if not (a.text or "").strip():
            raise ActError("msg needs --text")
        args = ["msg", "--shoal", sid, "--to", role, f"--text={a.text}"] + (["--wake"] if a.wake else [])
        cost = ("a * message with --wake starts a turn in EVERY role" if role == "*" and a.wake else
                "a --wake starts one turn of that role (tokens)" if a.wake else "")
        return args, cost, True
    if verb == "offer":
        role = _role(spec, a.role)
        if not (a.item or "").strip():
            raise ActError("offer needs --item (repo#n:fix|review)")
        args = ["offer", "--shoal", sid, "--to", role, f"--item={a.item}"] + ([f"--note={a.note}"] if a.note else [])
        return args, "an offer wakes an idle role (one turn)", True
    if verb == "withdraw":
        if not (a.item or "").strip():
            raise ActError("withdraw needs --item")
        return ["withdraw", "--shoal", sid, "--item", a.item], "", True
    if verb in ("offline", "freeze"):
        if a.state not in ("on", "off"):
            raise ActError(f"{verb} needs --state on|off")
        args = [verb, a.state, "--shoal", sid]
        if verb == "offline" and a.state == "on" and a.until:
            args += ["--until", a.until]
        if a.note:
            args += ["--note", a.note]
        if verb == "offline":
            # the palette runs offline without a confirm (maintainer, 2026-10-13); the dialog applies at once too
            return args, "", False
        if a.no_wake:
            return args + ["--no-wake"], "", True
        return args, "toggling a freeze wakes every role once (tokens)", True
    raise ActError(f"unknown write verb {verb}")


def _shoal(args: list[str], timeout: int = 120) -> subprocess.CompletedProcess:
    """One shoal verb of this checkout, captured, without color."""
    return subprocess.run([sys.executable, os.path.join(PKG, "cli.py"), *args], capture_output=True, text=True,
                          timeout=timeout, env=dict(os.environ, NO_COLOR="1"), stdin=subprocess.DEVNULL)


def plan(verb: str, a) -> dict:
    if verb in VIEW_VERBS:
        return view(verb, a)
    if verb not in WRITE_VERBS:
        raise ActError(f"unknown verb {verb} (write: {', '.join(WRITE_VERBS)}; view: {', '.join(VIEW_VERBS)})")
    args, cost, confirm = build(verb, a)
    pv = _shoal(args + ["--dry-run"])
    _sweep()
    token = _store({"verb": verb, "shoal": a.shoal, "role": a.role or "", "argv": args})
    return {"v": V, "kind": "shoal-act-plan", "verb": verb, "shoal": a.shoal, "role": a.role or "",
            "command": "shoal " + shlex.join(args), "preview": (pv.stdout + pv.stderr).strip() or "(no preview output)",
            "preview_rc": pv.returncode, "cost": cost, "confirm": confirm, "token": token, "expires_s": PLAN_TTL_S}


def apply(token: str) -> dict:
    import ui_herdr
    path = _plan_path(token)
    try:
        with open(path) as fh:
            p = json.load(fh)
        os.remove(path)  # one time: a second confirm finds nothing
    except FileNotFoundError as exc:
        raise ActError("no such plan (already applied, or expired): plan it again") from exc
    if time.time() - float(p.get("created", 0)) > PLAN_TTL_S:
        raise ActError("the plan expired: plan it again")
    args = [str(x) for x in p["argv"]]
    if not args or args[0] not in WRITE_VERBS:
        raise ActError("a stored plan names no write verb")
    r = _shoal(args)
    ui_herdr.log_action(args, r.returncode)
    return {"v": V, "kind": "shoal-act-result", "verb": p.get("verb"), "shoal": p.get("shoal"), "role": p.get("role"),
            "rc": r.returncode, "command": "shoal " + shlex.join(args), "output": (r.stdout + r.stderr).strip()}


def view(verb: str, a) -> dict:
    import ui_status
    base = {"v": V, "kind": "shoal-act-view", "verb": verb, "shoal": a.shoal or "", "role": a.role or ""}
    if verb == "roles":
        import ui_herdr
        doc = ui_status.cached_status([a.shoal] if a.shoal else None)
        hidden = set() if a.shoal else ui_status.hidden_shoals()
        shoals = []
        for s in doc.get("shoals", []):
            if s.get("error") or s.get("retired") or s["id"] in hidden:
                continue
            shoals.append({"id": s["id"], "tag": ui_herdr.shoal_tag(s["id"]), "roles": [
                {"slug": r["slug"], "host": r.get("host") or "", "needs_you": bool(r.get("needs_you")),
                 "posture": ui_status.posture_label(r)} for r in s.get("roles", []) if r.get("session_id")]})
        return {"v": V, "kind": "shoal-act-roles", "shoals": shoals}
    spec = _spec(a.shoal)
    if verb == "next":
        role = _role(spec, a.role)
        r = _shoal(["next", "--shoal", spec.sid, "--slug", role, "--peek", "--no-sync"])
        return dict(base, text=(r.stdout + r.stderr).rstrip())
    if verb == "status":
        r = _shoal(["ui", "status", "--shoal", spec.sid, "--conditions", "--width", "110"])
        return dict(base, text=(r.stdout + r.stderr).rstrip())
    if verb == "conditions":
        from ledger import Ledger, View
        from roles import state_dir
        c = ui_status.conditions(spec, View(Ledger(spec.sid, root=spec.ledger_dir or None, sync=False), spec),
                                 state_dir(spec.sid), refresh=True)
        lines = [f"{x['slug']}: {x['condition']}: {x['detail']}" for x in c.get("open", [])]
        lines.append(f"{len(c.get('open', []))} open condition(s) (the health quorum's own evaluate, refreshed now)")
        return dict(base, text="\n".join(lines), conditions=c.get("open", []))
    if verb == "asks":
        doc = ui_status.status([spec.sid], opencode_probe=False)
        s = doc["shoals"][0] if doc.get("shoals") else {}
        here = s.get("this_host", "")
        rows = []
        for r in s.get("roles", []):
            reasons = set(r["can_progress"]["reasons"])
            if r["asks"] or {"blocked-on-question", "blocked-says-waiting"} & reasons:
                rows.append({"role": r["slug"], "host": r.get("host") or "", "remote": bool(r.get("host")) and r.get("host") != here,
                             "asks": list(r["asks"]), "question": "blocked-on-question" in reasons,
                             "says_waiting": "blocked-says-waiting" in reasons})
        text = "\n".join([f"{x['role']}" + ("  (a question is open in its session: attach to answer it)" if x["question"] else "")
                          + "".join(f"\n  ask {i}: {q}" for i, q in enumerate(x["asks"], 1)) for x in rows]) \
            or "no open asks and no role waiting on a question"
        return dict(base, text=text, asks=rows)
    if verb == "upgrade":
        from roles import find_spec
        r = _shoal(["upgrade", "--spec", find_spec(spec.sid), "--dry-run", "--skip-selftests"], timeout=300)
        return dict(base, text="Upgrades stay a CLI step (operator only). Preview of what this commit would roll out:\n\n"
                    + (r.stdout + r.stderr).rstrip())
    raise ActError(f"unknown view {verb}")


def _emit(doc: dict, as_json: bool) -> None:
    if as_json:
        print(json.dumps(doc, indent=1))
        return
    kind = doc.get("kind")
    if kind == "shoal-act-plan":
        print(f"$ {doc['command']}\n{doc['preview']}" + (f"\n{doc['cost']}" if doc["cost"] else "")
              + f"\nconfirm: shoal ui act-apply --token {doc['token']}   (within {doc['expires_s'] // 60} minutes)")
    elif kind == "shoal-act-result":
        print(f"$ {doc['command']}  (exit {doc['rc']})\n{doc['output']}")
    elif kind == "shoal-act-roles":
        for s in doc["shoals"]:
            print(f"{s['tag']}: " + ", ".join(r["slug"] + ("!" if r["needs_you"] else "") for r in s["roles"]))
    else:
        print(doc.get("text") or doc.get("error") or "")


def cmd_act_plan(a) -> int:
    try:
        _emit(plan(a.verb, a), a.json)
        return 0
    except (ActError, subprocess.TimeoutExpired) as exc:
        _emit({"v": V, "kind": "shoal-act-error", "error": str(exc)}, a.json)
        return 2


def cmd_act_apply(a) -> int:
    try:
        doc = apply(a.token)
        _emit(doc, a.json)
        return 0 if doc["rc"] == 0 else 1
    except (ActError, subprocess.TimeoutExpired) as exc:
        _emit({"v": V, "kind": "shoal-act-error", "error": str(exc)}, a.json)
        return 2


def add_parsers(u) -> None:
    q = u.add_parser("act-plan", help="an operator verb without a terminal: a write verb's preview and confirm token, "
                     "or a read-only view (the Herdr app's actions dialog)")
    q.add_argument("verb", help=f"write: {', '.join(WRITE_VERBS)}; view: {', '.join(VIEW_VERBS)}")
    q.add_argument("--shoal", default="")
    q.add_argument("--role", default="", help="role slug (msg also takes *)")
    q.add_argument("--text", default="")
    q.add_argument("--item", default="")
    q.add_argument("--note", default="")
    q.add_argument("--wake", action="store_true")
    q.add_argument("--state", default="", help="offline, freeze: on|off")
    q.add_argument("--until", default="", help="offline on: expected return")
    q.add_argument("--no-wake", action="store_true", help="freeze: record it without waking every role")
    q.add_argument("--json", action="store_true")
    q.set_defaults(func=cmd_act_plan)
    q = u.add_parser("act-apply", help="run a planned write verb once, by its token (from act-plan)")
    q.add_argument("--token", required=True)
    q.add_argument("--json", action="store_true")
    q.set_defaults(func=cmd_act_apply)
