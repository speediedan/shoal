#!/usr/bin/env python3
"""The Herdr adapter (docs/ui.md): shoal's read model and operator verbs on Herdr's primitives.

  shoal ui open    [--shoal ID ...]         one workspace per shoal (overview + role cards), plus
                                            the saved standalone sessions; idempotent
  shoal ui feed    [--interval S] [--once]  push role state and tokens to Herdr (read-only to shoal)
  shoal ui menu                             the operator palette (a Herdr popup)
  shoal ui act VERB ...                     one operator verb: preview (`--dry-run`), confirm, run
  shoal ui pick    [--save]                 attach a standalone (non-shoal) Claude or opencode session
  shoal ui dash | tail | metrics            overview, ledger tail and metrics panes
  shoal ui herdr-config --client|--server   the managed Herdr config blocks
  shoal ui herdr-install [--dry-run]        server side: config block, plugin link, systemd units
  shoal ui doctor                           adapter preflight (restore off, no integrations, schema)

Rules this adapter keeps (design driver 1 and the coexistence constraints):
- It never types into a role's pane and never calls `pane.send_*`, `agent.prompt`, `agent.send_keys`
  or `pane.close`: every action is a shoal CLI verb, so `Backend.wake` accounting holds. The
  selftest greps this file for those calls.
- Role panes run `shoal view --stub`: a card that attaches only on Enter (never a resume). Layouts
  are restored by re-applying a whole tab (`layout.apply`), never by typing a command into a pane.
- Herdr is told role state with `pane.report_agent` (source `shoal`; Herdr has no TTL for it, so
  it holds the last reported state) and tokens with `pane.report_metadata` with a TTL: if the
  feeder dies the shoal tokens disappear instead of showing stale data.
"""
from __future__ import annotations

import datetime
import json
import os
import shlex
import socket
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
import ui_term  # noqa: E402

SOURCE = "shoal"
DEFAULT_SESSION = "shoal"
STANDALONE_WS = "standalone"
WS_PREFIX = "shoal:"
FEED_INTERVAL_S = 20
PLUGIN_DIR = os.path.join(REPO, "plugins", "shoal-herdr")
PLUGIN_ID = "shoal.herdr"
SHELLS = {"bash", "zsh", "fish", "sh", "dash", "ksh", "tcsh"}
# socket methods the adapter calls; `ui doctor` checks the running Herdr still offers them
METHODS = ("workspace.list", "workspace.create", "workspace.rename", "workspace.close", "workspace.report_metadata",
           "pane.list", "pane.get",
           "pane.process_info", "pane.report_agent", "pane.report_metadata", "layout.apply")


# ---- Herdr socket ----------------------------------------------------------------------

class HerdrError(RuntimeError):
    pass


def session_name() -> str:
    return config.env("HERDR_SESSION") or os.environ.get("HERDR_SESSION") or DEFAULT_SESSION


def config_dir() -> str:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, "herdr")


def socket_path(session: str = "") -> str:
    """An explicit session (argument, then SHOAL_HERDR_SESSION) wins; else the socket Herdr injected
    into this pane or plugin; else the default `shoal` session."""
    if not session and config.env("HERDR_SESSION"):
        session = config.env("HERDR_SESSION")
    if not session and os.environ.get("HERDR_SOCKET_PATH"):
        return os.environ["HERDR_SOCKET_PATH"]
    name = session or session_name()
    return os.path.join(config_dir(), "herdr.sock") if name == "default" else \
        os.path.join(config_dir(), "sessions", name, "herdr.sock")


class Herdr:
    def __init__(self, session: str = ""):
        self.path = socket_path(session)
        self._n = 0

    def call(self, method: str, params: dict | None = None, timeout: float = 10.0) -> dict:
        self._n += 1
        req = {"id": f"shoal-{os.getpid()}-{self._n}", "method": method, "params": params or {}}
        try:
            s = socket.socket(socket.AF_UNIX)
            s.settimeout(timeout)
            s.connect(self.path)
            s.sendall((json.dumps(req) + "\n").encode())
            buf = b""
            while not buf.endswith(b"\n"):
                c = s.recv(1 << 16)
                if not c:
                    break
                buf += c
            s.close()
        except OSError as exc:
            raise HerdrError(f"herdr socket {self.path}: {exc} (is the server running?)") from exc
        resp = json.loads(buf or b"{}")
        if "error" in resp:
            raise HerdrError(f"{method}: {resp['error'].get('code')}: {resp['error'].get('message')}")
        return resp.get("result") or {}

    def alive(self) -> bool:
        try:
            self.call("workspace.list", timeout=3)
            return True
        except (HerdrError, ValueError):
            return False

    def workspaces(self) -> list[dict]:
        return self.call("workspace.list").get("workspaces", [])

    def panes(self, workspace_id: str = "") -> list[dict]:
        return self.call("pane.list", {"workspace_id": workspace_id} if workspace_id else {}).get("panes", [])

    def foreground_is_shell(self, pane_id: str) -> bool:
        info = self.call("pane.process_info", {"pane_id": pane_id}).get("process_info", {})
        procs = info.get("foreground_processes") or []
        return bool(procs) and all(os.path.basename(p.get("name", "")) in SHELLS for p in procs)


# ---- commands the panes run ------------------------------------------------------------

def shoal_argv(*args: str) -> list[str]:
    """argv that runs THIS checkout's shoal CLI (the plugin wrapper resolves an interpreter with
    the host's TOML reader), so a pane runs the same code the plugin and feeder run."""
    return [os.path.join(PLUGIN_DIR, "bin", "shoal-ui"), *args]


def role_label(sid: str, slug: str) -> str:
    return f"{sid}/{slug}"


# ---- open: workspaces, overview and role cards -----------------------------------------

def _grid(nodes: list[dict]) -> dict:
    """A balanced BSP over pane nodes: two columns, rows stacked within each."""
    if len(nodes) == 1:
        return nodes[0]
    if len(nodes) <= 3:
        head, rest = nodes[0], nodes[1:]
        return {"type": "split", "direction": "down", "ratio": 1.0 / len(nodes), "first": head,
                "second": _grid(rest) if len(rest) > 1 else rest[0]}
    mid = (len(nodes) + 1) // 2
    return {"type": "split", "direction": "right", "ratio": 0.5,
            "first": _column(nodes[:mid]), "second": _column(nodes[mid:])}


def _column(nodes: list[dict]) -> dict:
    if len(nodes) == 1:
        return nodes[0]
    return {"type": "split", "direction": "down", "ratio": 1.0 / len(nodes), "first": nodes[0],
            "second": _column(nodes[1:])}


def pane_env(spec) -> dict:
    """Panes resolve the spec by id through the host config; a shoal from another ledger clone (or a
    launch-time SHOAL_DATA_ROOT) carries its roots into the pane's environment."""
    env = {}
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(spec.path)))) if getattr(spec, "path", "") else ""
    if root and os.path.abspath(root) != os.path.abspath(config.ledger_dir()):
        env["SHOAL_LEDGER_DIR"] = root
    if config.env("DATA_ROOT"):
        env["SHOAL_DATA_ROOT"] = config.env("DATA_ROOT")
    return env


def _node(label: str, cwd: str, command: list[str], env: dict) -> dict:
    n = {"type": "pane", "label": label, "cwd": cwd or os.path.expanduser("~"), "command": command}
    if env:
        n["env"] = env
    return n


def role_node(spec, role) -> dict:
    return _node(role_label(spec.sid, role.slug), spec.dir, shoal_argv("view", spec.sid, role.slug, "--stub"),
                 pane_env(spec))


def apply_tab(h: "Herdr", workspace_id: str, label: str, root: dict, tab_id: str = "", focus: bool = False) -> dict:
    """`layout.apply`: replace `tab_id` when given (Herdr builds the new tab, then closes the old),
    else add a tab to the workspace. Herdr takes one target or the other, never both."""
    target = {"tab_id": tab_id} if tab_id else {"workspace_id": workspace_id}
    return h.call("layout.apply", {**target, "tab_label": label, "focus": focus, "root": root})


def overview_root(spec) -> dict:
    return {"type": "split", "direction": "right", "ratio": 0.6,
            "first": _node(f"{spec.sid}/overview", spec.dir, shoal_argv("ui", "dash", "--shoal", spec.sid), pane_env(spec)),
            "second": _node(f"{spec.sid}/ledger", spec.dir, shoal_argv("ui", "tail", "--shoal", spec.sid), pane_env(spec))}


def disposable(h: "Herdr", pane_id: str) -> bool:
    """True only when everything in the pane's foreground is ours to replace: the adapter's own card,
    stub or overview process (this checkout's CLI running `view` or `ui`), or an idle shell. Anything
    else (an attached harness client, or whatever an operator started from the card's shell) makes
    the pane NOT disposable: an allowlist, so unknown work is never closed. A process_info error
    propagates (fails closed)."""
    info = h.call("pane.process_info", {"pane_id": pane_id}).get("process_info", {})
    procs = info.get("foreground_processes") or []
    if not procs:
        return False
    cli = os.path.join(PKG, "cli.py")
    for p in procs:
        name = os.path.basename(p.get("name", ""))
        argv = p.get("argv") or str(p.get("cmdline", "")).split()
        ours = any(os.path.abspath(x) == cli for x in argv if x.endswith("cli.py")) and \
            any(x in ("view", "ui") for x in argv)
        if not (ours or name in SHELLS):
            return False
    return True


def viewable_roles(spec) -> list:
    return [r for r in spec.roles if r.session]


def find_ws(h: Herdr, label: str) -> dict | None:
    return next((w for w in h.workspaces() if w.get("label") == label), None)


_TAGS: dict[str, str] = {}


def shoal_tag(sid: str) -> str:
    """The short name a shoal shows as (workspace label, the `$shoal` token): the id without its
    `-YYYYMMDD-HHMMSS` mint stamp, e.g. `it-mvp`, unless another LIVE shoal on this host shares
    that stem, in which case the full id. Retired shoals do not count: a group's next incarnation
    retires the previous one with the same stem, and a retired shoal gets no workspace."""
    import re
    if not _TAGS:
        import ui_status
        ids = [os.path.basename(os.path.dirname(p)) for p in ui_status.discover_all("", None, False)]
        stems = {}
        for i in ids:
            stems.setdefault(re.sub(r"-\d{8}-\d{6}$", "", i), []).append(i)
        for stem, members in stems.items():
            for i in members:
                _TAGS[i] = stem if len(members) == 1 else i
    return _TAGS.get(sid) or re.sub(r"-\d{8}-\d{6}$", "", sid)


def find_shoal_ws(h: Herdr, sid: str, dry_run: bool = False) -> dict | None:
    """The shoal's workspace, by its short label. A workspace under an older label (the legacy
    `shoal:<id>`, or the full id a stem collision once forced) is renamed in place; no pane is
    touched."""
    tag = shoal_tag(sid)
    ws = find_ws(h, tag)
    if ws is not None:
        return ws
    ws = next((w for w in (find_ws(h, x) for x in (WS_PREFIX + sid, sid) if x != tag) if w), None)
    if ws is not None and not dry_run:
        h.call("workspace.rename", {"workspace_id": ws["workspace_id"], "label": shoal_tag(sid)})
        ws = dict(ws, label=shoal_tag(sid))
    return ws


def ensure_shoal(h: Herdr, spec, dry_run: bool = False) -> list[str]:
    """One workspace per shoal, labelled with its short tag (`shoal_tag`): tab `overview` (dashboard
    + ledger tail) and tab `roles` (a card per role with a session). Re-running only fills gaps by
    re-applying a whole tab (a new role, or a cold restore that left bare shells). Nothing running
    is replaced or closed."""
    out = []
    label = shoal_tag(spec.sid)
    roles = viewable_roles(spec)
    ws = find_shoal_ws(h, spec.sid, dry_run)
    if ws is None:
        out.append(f"create workspace {label}: overview + {len(roles)} role cards")
        if dry_run:
            return out
        ws = h.call("workspace.create", {"label": label, "cwd": spec.dir or os.path.expanduser("~"),
                                         "focus": False})["workspace"]
        first_tab = ws.get("active_tab_id")
        apply_tab(h, ws["workspace_id"], "overview", overview_root(spec), tab_id=first_tab)
        if roles:
            apply_tab(h, ws["workspace_id"], "roles", _grid([role_node(spec, r) for r in roles]))
        return out
    panes = h.panes(ws["workspace_id"])
    by_label = {p.get("label"): p for p in panes if p.get("label")}
    want = [role_label(spec.sid, r.slug) for r in roles]
    cards = [p for p in panes if p.get("label") in want]
    missing = [lab for lab in want if lab not in by_label]
    tabs = {p.get("tab_id") for p in cards}
    shells = [p for p in cards if h.foreground_is_shell(p["pane_id"])]
    attached = [p["label"] for p in cards if not disposable(h, p["pane_id"])]
    # A cold Herdr restore brings every pane back as a bare shell; a new role has no card yet. Either
    # way the roles tab is re-applied whole (cards are stateless), never by typing into a pane, and
    # never while an operator is attached to a role in it.
    if missing or (cards and len(shells) == len(cards)):
        why = (f"add {', '.join(missing)}" if missing else "restore cards after a restart")
        if attached:
            out.append(f"roles tab needs a re-apply ({why}) but {', '.join(attached)} is in use (attached, "
                       f"or running something from its shell): free it, then run `shoal ui open` again")
        else:
            out.append(f"re-apply roles tab: {why}")
            if not dry_run and roles:
                apply_tab(h, ws["workspace_id"], "roles", _grid([role_node(spec, r) for r in roles]),
                          tab_id=next(iter(tabs)) if len(tabs) == 1 else "")
    ov = [by_label.get(f"{spec.sid}/overview"), by_label.get(f"{spec.sid}/ledger")]
    if any(p is None for p in ov) or all(h.foreground_is_shell(p["pane_id"]) for p in ov if p):
        out.append("re-apply overview tab")
        if not dry_run:
            tab = next((p.get("tab_id") for p in ov if p), None)
            apply_tab(h, ws["workspace_id"], "overview", overview_root(spec), tab_id=tab or "")
    return out or [f"{label}: up to date ({len(roles)} role cards)"]


# ---- standalone sessions ---------------------------------------------------------------

def standalone_path() -> str:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, "shoal", "ui-standalone.json")


def load_standalone() -> list[dict]:
    try:
        return list(json.load(open(standalone_path())).get("sessions", []))
    except (OSError, ValueError):
        return []


def save_standalone(entries: list[dict]) -> None:
    p = standalone_path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w") as fh:
        json.dump({"v": 1, "sessions": entries}, fh, indent=1)
    os.replace(tmp, p)


def standalone_label(e: dict) -> str:
    return f"{STANDALONE_WS}/{e.get('label') or e['session'][:12]}"


def standalone_argv(e: dict) -> list[str]:
    return shoal_argv("view", "--harness", e["harness"], "--session", e["session"],
                      "--label", e.get("label") or e["session"][:12], "--stub")


def standalone_ws_label(e: dict) -> str:
    """A standalone session's own workspace is named after the session, so the spaces list shows
    it; a name that collides with a shoal's tag gets a suffix."""
    name = (e.get("label") or e["session"][:12])[:40]
    return name + " (standalone)" if name in set(_TAGS.values()) else name


def open_standalone(h: Herdr, e: dict, dry_run: bool = False, focus: bool = False) -> str:
    """One workspace per standalone session, named after it (found by its pane label wherever it
    is, so sessions opened before this layout keep working)."""
    shoal_tag("")  # load the shoal tags so a colliding name is suffixed
    lab = standalone_label(e)
    node = {"type": "pane", "label": lab, "cwd": e.get("cwd") or os.path.expanduser("~"), "command": standalone_argv(e)}
    p = next((q for q in h.panes() if q.get("label") == lab), None)
    if p is not None:
        if h.foreground_is_shell(p["pane_id"]):
            if not dry_run:
                apply_tab(h, p["workspace_id"], e.get("label") or e["session"][:12], node, tab_id=p["tab_id"])
            return f"restore standalone {lab}"
        return f"standalone {lab}: open"
    if dry_run:
        return f"open standalone {lab}"
    ws = h.call("workspace.create", {"label": standalone_ws_label(e), "focus": False})["workspace"]
    apply_tab(h, ws["workspace_id"], "session", node, tab_id=ws.get("active_tab_id") or "", focus=focus)
    return f"open standalone {lab} (workspace {standalone_ws_label(e)!r})"


def migrate_standalone(h: Herdr, dry_run: bool = False) -> list[str]:
    """Retire the old single `standalone` workspace: when none of its sessions is attached, close
    it (cards only) so `open_standalone` rebuilds one workspace per session."""
    ws = find_ws(h, STANDALONE_WS)
    if ws is None:
        return []
    panes = h.panes(ws["workspace_id"])
    if not panes or not all(disposable(h, p["pane_id"]) for p in panes):
        return [f"the old `{STANDALONE_WS}` workspace has a pane in use (an attached session, or something "
                f"started from its shell): free it, then `ui open` again; it is left as it is"]
    if not dry_run:
        h.call("workspace.close", {"workspace_id": ws["workspace_id"]})
    return [f"retire the old `{STANDALONE_WS}` workspace (one workspace per session now)"]


def internal_session(name: str, role_slugs: set[str]) -> bool:
    """A session shoal itself runs that is not a standalone session: semantic matcher runs, and any
    incarnation of a shoal role (`<slug>-r<N>`, current or retired)."""
    import re
    n = (name or "").strip()
    if n.startswith("shoal-matcher"):
        return True
    m = re.match(r"^(.+)-r\d+$", n)
    return bool(m and m.group(1) in role_slugs)


def candidates_standalone(exclude: set[str]) -> list[dict]:
    """Live sessions on this host that are not shoal roles: Claude background sessions and
    OpenChamber's opencode sessions (most recently updated first). Matcher runs and old role
    incarnations are left out."""
    import ui_status
    slugs = role_slugs()
    out = []
    for e in ui_status.claude_rows():
        sid = str(e.get("sessionId", ""))
        if not sid or not e.get("pid") or any(sid.startswith(x) or x.startswith(sid) for x in exclude if x):
            continue
        if internal_session(str(e.get("name") or ""), slugs):
            continue
        out.append({"harness": "claude", "session": sid, "label": str(e.get("name") or sid[:8])[:40],
                    "state": e.get("state"), "cwd": e.get("cwd")})
    try:
        r = subprocess.run(["openchamber", "session", "list", "--limit", "15", "--with-status", "--json"],
                           capture_output=True, text=True, timeout=30, env=config.tool_env())
        for s in json.loads(r.stdout or "{}").get("sessions", []):
            if s.get("id") in exclude or internal_session(str(s.get("title") or ""), slugs):
                continue
            out.append({"harness": "opencode", "session": s["id"], "label": str(s.get("title") or s["id"][:12])[:40],
                        "state": (s.get("status") or {}).get("type"), "cwd": (s.get("location") or {}).get("directory")})
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    return out


def role_slugs() -> set[str]:
    import ui_status
    from roles import ShoalSpec
    out = set()
    for p in ui_status.discover_all("", None, True):
        try:
            out |= {r.slug for r in ShoalSpec.load(p).roles}
        except (OSError, ValueError):
            continue
    return out


def role_sessions() -> set[str]:
    import ui_status
    from roles import ShoalSpec
    ids = set()
    for p in ui_status.discover_all("", None, True):
        try:
            ids |= {r.session for r in ShoalSpec.load(p).roles if r.session}
        except (OSError, ValueError):
            continue
    return ids


# ---- feed: role state and tokens -------------------------------------------------------

def herdr_state(r: dict) -> str:
    reasons = r["can_progress"]["reasons"]
    if r["needs_you"] or "watcher-dead" in reasons:
        return "blocked"
    if r["session"] == "working":
        return "working"
    return "idle"


def role_tokens(r: dict) -> dict[str, str]:
    import ui_status
    nx = r["next"]
    return {
        "role": r["slug"],
        "posture": ui_status.posture_label(r),
        "item": (r["active"] or "-")[:80],
        "next": (f"{len(nx)}" + ("!" if r["blocker_stale"] else "")) if nx else "",
        "wait": (f"{r['wait']['sub']}:{(r['wait']['target'] or '')[:50]}" if r["wait"] else ""),
        "asks": str(len(r["asks"])) if r["asks"] else "",
        "offers": str(r["offers_open"]) if r["offers_open"] else "",
        "watch": "" if r["watcher"] == "alive" or r["session"] == "working" else r["watcher"],
        "age": ui_status._age(r["last_write_age_s"]),
        "why": ",".join(x for x in r["can_progress"]["reasons"] if x != "parked")[:80],
        "harness": r["harness"],
    }


def role_tokens_for(s: dict, r: dict) -> dict[str, str]:
    return dict(role_tokens(r), shoal=shoal_tag(s["id"]))


def shoal_tokens(s: dict) -> dict[str, str]:
    m = s["mode"]
    c = s.get("conditions") or {}
    return {
        "shoal_mode": "OFFLINE" if m["offline"] else ("FREEZE" if m["freeze"] else "ok"),
        "shoal_needs": str(s["needs_you"]), "shoal_stalled": str(s["stalled"]),
        "shoal_asks": str(s["asks_open"]), "shoal_pages": str(s["pages_24h"]),
        "shoal_relay": ("" if not s.get("relay") else ("ok" if s["relay"]["alive"] else "DEAD")),
        "shoal_conditions": str(len(c.get("open", []))) if c else "",
        "shoal_tooling": "" if s["tooling"]["current"] else f"{s['tooling']['sha']}->{s['tooling']['head']}",
    }


def feed_once(h: Herdr, interval: float, tick: int, seq: int, quiet: bool = True) -> int:
    import ui_status
    doc = ui_status.status(None, with_conditions=True,
                           refresh_conditions=False)  # conditions come from their own 5-minute cache
    ui_status.write_cache(doc)
    ttl = int(interval * 3 * 1000)
    wss = {w.get("label"): w for w in h.workspaces()}
    panes = {p.get("label"): p for p in h.panes() if p.get("label")}
    pushed = 0
    for s in doc["shoals"]:
        if s.get("error"):
            continue
        ws = wss.get(shoal_tag(s["id"])) or wss.get(WS_PREFIX + s["id"]) or wss.get(s["id"])
        if ws is not None:
            h.call("workspace.report_metadata", {"workspace_id": ws["workspace_id"], "source": SOURCE,
                                                 "tokens": shoal_tokens(s), "ttl_ms": ttl, "seq": seq})
        for r in s["roles"]:
            p = panes.get(role_label(s["id"], r["slug"]))
            if p is None:
                continue
            msg = ui_status.posture_label(r) + (f" {r['active']}" if r["active"] else "")
            h.call("pane.report_agent", {"pane_id": p["pane_id"], "source": SOURCE, "agent": r["slug"],
                                         "state": herdr_state(r), "message": msg[:80], "seq": seq})
            h.call("pane.report_metadata", {"pane_id": p["pane_id"], "source": SOURCE,
                                            "tokens": role_tokens_for(s, r), "ttl_ms": ttl, "seq": seq})
            pushed += 1
    # standalone Claude sessions: their turn state (the harness's own, no scraping)
    rows = ui_status.claude_rows()
    for e in load_standalone():
        p = panes.get(standalone_label(e))
        if p is None or e.get("harness") != "claude":
            continue
        row = ui_status.claude_session(rows, e["session"]) or {}
        st = ui_status.claude_turn(row) if row else "unknown"
        h.call("pane.report_agent", {"pane_id": p["pane_id"], "source": SOURCE, "agent": e.get("label") or "claude",
                                     "state": {"working": "working", "blocked": "blocked"}.get(st, "idle"),
                                     "message": f"standalone {st}", "seq": seq})
    if not quiet:
        print(f"{datetime.datetime.now():%H:%M:%S} fed {pushed} role panes; {ui_status.render_line(doc)}", flush=True)
    return pushed


def cmd_feed(a) -> int:
    h = Herdr(a.session)
    tick = 0
    while True:
        seq = int(time.time() * 1000)
        try:
            if not h.alive():
                print(f"feed: herdr session {h.path} not reachable; retrying", flush=True)
            else:
                feed_once(h, a.interval, tick, seq, quiet=not a.verbose)
        except Exception as exc:  # noqa: BLE001 - the feeder outlives any one bad tick
            print(f"feed: tick failed: {exc}", flush=True)
        if a.once:
            return 0
        tick += 1
        time.sleep(a.interval)


# ---- open / pick commands --------------------------------------------------------------

def cmd_open(a) -> int:
    import ui_status
    from roles import ShoalSpec
    h = Herdr(a.session)
    if not h.alive():
        print(f"ui open: no herdr server at {h.path} (start it: `systemctl --user start shoal-herdr`, "
              f"or attach with `herdr --session {session_name()}`)")
        return 1
    for p in ui_status.discover_all(a.ledger_root, a.ids or None):
        try:
            spec = ShoalSpec.load(p)
            spec.path = p
            for line in ensure_shoal(h, spec, a.dry_run):
                print(("[dry-run] " if a.dry_run else "") + line)
        except Exception as exc:  # noqa: BLE001
            print(f"ui open: {p}: {exc}")
    if not a.no_standalone:
        for line in migrate_standalone(h, a.dry_run):
            print(("[dry-run] " if a.dry_run else "") + line)
        for e in load_standalone():
            print(("[dry-run] " if a.dry_run else "") + open_standalone(h, e, a.dry_run))
    return 0


def _choose(items: list[str], prompt: str = "number") -> int | None:
    for i, it in enumerate(items, 1):
        print(f"  {i:>2}  {it}")
    raw = ui_term.read_line(f"{prompt} (esc or empty enter cancels): ")
    if not raw or not raw.isdigit() or not 1 <= int(raw) <= len(items):
        return None
    return int(raw) - 1


def cmd_pick(a) -> int:
    """Pick a live non-shoal session and open it in its own workspace, named after it."""
    if a.harness and a.session_id:
        e = {"harness": a.harness, "session": a.session_id, "label": a.label or a.session_id[:12]}
    else:
        cands = candidates_standalone(role_sessions())
        if not cands:
            print("no standalone sessions found (claude agents, openchamber session list)")
            return 1
        print("Standalone sessions (not shoal roles):")
        i = _choose([f"{c['harness']:<8} {c['label']:<40} {c['state'] or '?':<8} {c['session'][:16]}  {c.get('cwd') or ''}"
                     for c in cands])
        if i is None:
            return 1
        e = {k: cands[i][k] for k in ("harness", "session", "label", "cwd")}
        if a.label:
            e["label"] = a.label
    save = a.save
    if not a.harness and not save:
        save = ui_term.confirm("save it so `ui open` restores it? [y/N] ")
    if save:
        entries = [x for x in load_standalone() if x["session"] != e["session"]] + [e]
        save_standalone(entries)
        print(f"saved to {standalone_path()}")
    h = Herdr(a.session)
    print(open_standalone(h, e, focus=True))
    return 0


def cmd_forget(a) -> int:
    entries = load_standalone()
    keep = [x for x in entries if x["session"] != a.session_id and x.get("label") != a.session_id]
    save_standalone(keep)
    print(f"forgot {len(entries) - len(keep)} saved standalone session(s); open panes are left as they are")
    return 0


# ---- overview panes --------------------------------------------------------------------

def cmd_dash(a) -> int:
    import ui_status
    import ui_view
    while True:
        try:
            doc = ui_status.cached_status(a.ids or None)
            body = ui_status.render_text(doc)
        except Exception as exc:  # noqa: BLE001
            body = f"status unavailable: {exc}"
        sys.stdout.write("\x1b[2J\x1b[H" + body + f"\n\n\x1b[2m{datetime.datetime.now():%H:%M:%S}  "
                         "[r] refresh  [m] menu  [q] shell   (read-only; conditions cached 5 min)\x1b[0m\n")
        sys.stdout.flush()
        k = ui_view._getkey(a.interval)
        if k in ("q", "Q"):
            return 0
        if k == "m":
            subprocess.run(shoal_argv("ui", "menu"))


def _fmt_event(sid: str, e: dict) -> str:
    t = e.get("ts", "")[11:19]
    what = e.get("type", "?")
    bits = [e.get("item") or "", ("->" + e["to"]) if e.get("to") else "", e.get("kind") or "",
            e.get("state") or e.get("posture") or "", e.get("active") or ""]
    text = e.get("text") or e.get("reason") or e.get("note") or ""
    return f"{t} {sid[:14]:<14} {e.get('by', '?')[:22]:<22} {what:<7} " + " ".join(b for b in bits if b) \
        + (f"  {' '.join(str(text).split())[:90]}" if text else "")


def cmd_tail(a) -> int:
    """Follow the ledger files of the chosen shoals (read-only; no pull: the local clone is current
    on the lane host)."""
    import glob
    import ui_status
    from roles import ShoalSpec
    files: dict[str, tuple[str, int]] = {}
    from ledger import Ledger
    for p in ui_status.discover_all(a.ledger_root, a.ids or None):
        spec = ShoalSpec.load(p)
        ldir = Ledger(spec.sid, root=spec.ledger_dir or None, sync=False).ldir
        for f in glob.glob(os.path.join(ldir, "*.jsonl")):
            files[f] = (spec.sid, max(0, os.path.getsize(f) - (4000 if a.backlog else 0)))
    print(f"ledger tail: {len(files)} writer files (read-only)")
    while True:
        new = []
        for f, (sid, off) in list(files.items()):
            try:
                size = os.path.getsize(f)
                if size <= off:
                    continue
                with open(f) as fh:
                    fh.seek(off)
                    chunk = fh.read()
                files[f] = (sid, off + len(chunk.encode()))
                for line in chunk.splitlines():
                    try:
                        new.append((sid, json.loads(line)))
                    except ValueError:
                        continue
            except OSError:
                continue
        for sid, e in sorted(new, key=lambda x: x[1].get("ts", "")):
            print(_fmt_event(sid, e), flush=True)
        if a.once:
            return 0
        time.sleep(5)


# ---- metrics ---------------------------------------------------------------------------

def fm_report_path() -> str:
    return os.path.join(REPO, "plugins", "shoal-skills", "skills", "shoal-health-review", "scripts", "fm_report.py")


def metrics_doc(spec_path: str, days: int) -> dict:
    r = subprocess.run([sys.executable, fm_report_path(), "--spec", spec_path, "--days", str(days), "--json",
                        "--data-root", config.data_root()],
                       capture_output=True, text=True, timeout=120)
    try:
        return json.loads(r.stdout)
    except ValueError:
        return {"error": (r.stderr or r.stdout).strip()[-300:]}


SPARK = " .:-=+*#%@"


def render_fm(d: dict) -> str:
    """fm_report's `counts` ({"<day>|<metric>": n}) and `hours` as one row per metric: the window
    total, then a per-day bar so a trend (falling, flat, rising) reads at a glance."""
    seen = sorted({k.split("|", 1)[0] for k in (d.get("counts") or {})})
    try:  # every calendar day of the window, so a gap reads as a gap
        a, b = (datetime.date.fromisoformat(x[:10]) for x in d["window"])
        days = [str(a + datetime.timedelta(n)) for n in range((b - a).days + 1)]
    except (KeyError, ValueError, TypeError):
        days = seen
    per: dict[str, dict[str, float]] = {}
    for k, v in (d.get("counts") or {}).items():
        day, metric = k.split("|", 1)
        per.setdefault(metric, {})[day] = float(v)
    hours: dict[str, float] = {}
    for k, v in (d.get("hours") or {}).items():
        metric = k.split("|", 1)[-1]
        hours[metric] = hours.get(metric, 0.0) + float(v)
    if not per:
        return "  (no failure-mode events in the window)"
    lines = [f"  {'metric':<34} {'total':>6} {'hours':>7}  per day {days[0][5:]}..{days[-1][5:]}"]
    for metric in sorted(per, key=lambda m: (-sum(per[m].values()), m)):
        vals = [per[metric].get(day, 0.0) for day in days]
        top = max(vals) or 1.0
        bar = "".join(SPARK[min(len(SPARK) - 1, int(round(v / top * (len(SPARK) - 1))))] for v in vals)
        h = f"{hours[metric]:.1f}" if metric in hours else ""
        lines.append(f"  {metric:<34} {int(sum(vals)):>6} {h:>7}  {bar}")
    return "\n".join(lines)


def latest_reports(sid: str, n: int = 3) -> list[str]:
    import glob
    from roles import data_dir
    fs = glob.glob(os.path.join(data_dir(sid), "reports", "*.html")) + \
        glob.glob(os.path.join(data_dir(sid), "reports", "*.md"))
    return sorted(fs, key=os.path.getmtime, reverse=True)[:n]


def build_html(spec_path: str, hours: int = 168) -> str:
    """Render a rolling report with HTML (`shoal report`): reads telemetry and the ledger, writes
    only report files under the data root. Returns the HTML path ('' on failure)."""
    import glob
    from roles import ShoalSpec, data_dir
    sid = ShoalSpec.load(spec_path).sid
    out = os.path.join(data_dir(sid), "reports", f"ui-rolling-{hours}h-{datetime.datetime.now():%Y%m%dT%H%M}.html")
    r = run_shoal(["report", "--spec", spec_path, "--hours", str(hours), "--out-html", out], capture=True)
    return out if r.returncode == 0 and os.path.exists(out) else ""


def cmd_metrics(a) -> int:
    """High-level shoal metrics: failure-mode counts this window (fm_report) and the latest report
    files. The browser view is the report HTML (`shoal report --out-html`), which a viewing machine
    fetches over SSH."""
    import ui_status
    if a.latest_html:
        for p in ui_status.discover_all(a.ledger_root, a.ids or None):
            sid = os.path.basename(os.path.dirname(p))
            html = build_html(p) if a.build_html else next((f for f in latest_reports(sid) if f.endswith(".html")), "")
            if html:
                print(html)
        return 0
    for p in ui_status.discover_all(a.ledger_root, a.ids or None):
        sid = os.path.basename(os.path.dirname(p))
        d = metrics_doc(p, a.days)
        print(f"== {sid}: failure modes, last {a.days} days")
        if d.get("error"):
            print(f"  fm_report: {d['error']}")
        else:
            print(render_fm(d))
        for f in latest_reports(sid):
            print(f"  report: {f}")
    if a.pause:
        ui_term.pause("\n[enter, esc or q] close")
    return 0


# ---- actions (menu) --------------------------------------------------------------------

def actions_log() -> str:
    return os.path.join(config.data_root(), "ui-actions.jsonl")


def log_action(argv: list[str], rc: int) -> None:
    try:
        os.makedirs(os.path.dirname(actions_log()), exist_ok=True)
        with open(actions_log(), "a") as fh:
            fh.write(json.dumps({"ts": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                                 "argv": argv, "rc": rc, "host": socket.gethostname()}) + "\n")
    except OSError:
        pass


def run_shoal(args: list[str], capture: bool = False) -> subprocess.CompletedProcess:
    argv = [sys.executable, os.path.join(PKG, "cli.py"), *args]
    return subprocess.run(argv, capture_output=capture, text=True)


def confirm_run(args: list[str], cost_note: str = "") -> int:
    """Preview with --dry-run, confirm, then run once and log. The CLI does the permission checks."""
    print("\n$ shoal " + shlex.join(args))
    if "--dry-run" not in args:
        pv = run_shoal(args + ["--dry-run"], capture=True)
        print((pv.stdout + pv.stderr).strip() or "(no preview output)")
    if cost_note:
        print(f"\x1b[33m{cost_note}\x1b[0m")
    ok = ui_term.confirm("run it? [y/N] ")
    if not ok:
        print("cancelled; nothing written")
        return 1
    r = run_shoal(args)
    log_action(args, r.returncode)
    return r.returncode


def focused_role() -> tuple[str, str]:
    """(shoal id, role) of the card the operator was on. A plugin action captures Herdr's
    `focused_pane_id` and hands it to its popup as SHOAL_UI_FOCUS_PANE (a popup's own context may
    already have moved); then the popup's context; then the custom-command variables."""
    pane = os.environ.get("SHOAL_UI_FOCUS_PANE") or ""
    if not pane:
        try:
            pane = json.loads(os.environ.get("HERDR_PLUGIN_CONTEXT_JSON") or "{}").get("focused_pane_id") or ""
        except ValueError:
            pane = ""
    pane = pane or os.environ.get("HERDR_ACTIVE_PANE_ID") or os.environ.get("HERDR_PANE_ID") or ""
    if not pane:
        return "", ""
    try:
        p = Herdr().call("pane.get", {"pane_id": pane}).get("pane", {})
    except (HerdrError, ValueError):
        return "", ""
    sid, _, slug = str(p.get("label") or "").partition("/")
    if sid == STANDALONE_WS:
        return "", ""
    return (sid, slug) if slug and slug not in ("overview", "ledger") else (sid, "")


def pick_shoal(default: str = "") -> str:
    import ui_status
    ids = [os.path.basename(os.path.dirname(p)) for p in ui_status.discover_all()]
    if default in ids:
        return default
    if len(ids) == 1:
        return ids[0]
    print("shoal:")
    i = _choose(ids)
    return ids[i] if i is not None else ""


def pick_role(sid: str, default: str = "", allow_all: bool = False, ask: bool = True) -> str:
    """The focused card's role when there is one. A read-only verb takes it as is (`ask=False`);
    a write verb offers [enter] to keep it, [o] for another, [esc] to cancel."""
    from roles import ShoalSpec, find_spec
    spec = ShoalSpec.load(find_spec(sid))
    slugs = [r.slug for r in spec.roles if r.session] + (["*"] if allow_all else [])
    if default in slugs:
        if not ask:
            print(f"role: {default} (focused card)")
            return default
        k = ui_term.key(f"role {default} (focused card): [enter] keep, [o] another, [esc] cancel ")
        if k == "esc":
            return ""
        if k != "o":
            return default
    print("role:")
    i = _choose(slugs)
    return slugs[i] if i is not None else ""


def _ask(prompt: str) -> str:
    """A text answer; Escape cancels (returns "")."""
    return ui_term.read_line(prompt) or ""


# the pager says how to close it (the pilot got stuck at `(END)`); Ctrl+C also exits (-K)
PAGER = "less -R -K -Ps'[q] close   [space] [arrows] scroll'"


def act(verb: str, sid: str = "", slug: str = "") -> int:
    fs, fr = focused_role()
    sid = sid or pick_shoal(fs)
    if not sid:
        return 1
    if verb == "status":
        subprocess.run(["sh", "-c", f"{shlex.join(shoal_argv('ui', 'status', '--shoal', sid, '--conditions'))} | {PAGER}"])
        return 0
    if verb == "next":
        slug = slug or pick_role(sid, fr, ask=False)
        if slug:
            subprocess.run(["sh", "-c", f"{shlex.join(shoal_argv('next', '--shoal', sid, '--slug', slug, '--peek', '--no-sync'))} | {PAGER}"])
        return 0
    if verb == "asks":
        import ui_status
        doc = ui_status.status([sid], opencode_probe=False)
        rows = [r for r in doc["shoals"][0].get("roles", []) if r["asks"] or "blocked-on-question" in r["can_progress"]["reasons"]]
        if not rows:
            print("no open asks and no role waiting on a question")
            ui_term.pause()
            return 0
        for r in rows:
            print(f"\n{r['slug']}" + ("  (blocked on a question in its session: attach to answer it)"
                                      if "blocked-on-question" in r["can_progress"]["reasons"] else ""))
            for i, x in enumerate(r["asks"], 1):
                print(f"  ask {i}: {x}")
        pick = _ask("\nanswer which role? (slug; enter to close): ")
        if not pick:
            return 0
        how = _ask("[a] attach and answer in its question flow, [m] send the answer as mail with a wake: ").lower()
        if how == "a":
            return subprocess.run(shoal_argv("view", sid, pick)).returncode
        if how == "m":
            text = _ask("answer text: ")
            if text:
                return confirm_run(["msg", "--shoal", sid, "--to", pick, f"--text={text}", "--wake"],
                                   "a --wake from user starts one turn of that role (tokens)")
        return 0
    if verb == "msg":
        slug = slug or pick_role(sid, fr, allow_all=True)
        text = _ask("message: ") if slug else ""
        if not text:
            return 1
        wake = _ask("wake it now? (mail is otherwise read at its next boundary) [y/N] ").lower() == "y"
        args = ["msg", "--shoal", sid, "--to", slug, f"--text={text}"] + (["--wake"] if wake else [])
        note = ("a * message with --wake starts a turn in EVERY role" if slug == "*" and wake else
                "a --wake starts one turn of that role (tokens)" if wake else "")
        return confirm_run(args, note)
    if verb == "offer":
        slug = slug or pick_role(sid, fr)
        item = _ask("item (repo#n:fix|review): ") if slug else ""
        if not item:
            return 1
        note = _ask("note (optional): ")
        args = ["offer", "--shoal", sid, "--to", slug, f"--item={item}"] + ([f"--note={note}"] if note else [])
        return confirm_run(args, "an offer wakes an idle role (one turn)")
    if verb == "withdraw":
        item = _ask("item to withdraw (repo#n): ")
        return confirm_run(["withdraw", "--shoal", sid, "--item", item]) if item else 1
    if verb in ("offline", "freeze"):
        state = _ask(f"{verb} on or off? ").lower()
        if state not in ("on", "off"):
            return 1
        args = [verb, state, "--shoal", sid]
        note = _ask("note (optional): ")
        if note:
            args += ["--note", note]
        cost = "toggling a mode wakes every role once (tokens)"
        if verb == "freeze" and _ask("record it without waking every role (--no-wake)? [Y/n] ").lower() != "n":
            args.append("--no-wake")
            cost = ""
        return confirm_run(args, cost)
    if verb == "upgrade":
        from roles import find_spec
        print("Upgrades stay a CLI step (operator-only). Preview of what this commit would roll out:\n")
        run_shoal(["upgrade", "--spec", find_spec(sid), "--dry-run", "--skip-selftests"])
        print(f"\nTo apply with zero session wakes: shoal upgrade --spec {find_spec(sid)}   (--rearm lazy is the default)")
        ui_term.pause()
        return 0
    if verb == "conditions":
        import ui_status
        from ledger import Ledger, View
        from roles import ShoalSpec, find_spec, state_dir
        spec = ShoalSpec.load(find_spec(sid))
        c = ui_status.conditions(spec, View(Ledger(sid, root=spec.ledger_dir or None, sync=False), spec),
                                 state_dir(sid), refresh=True)
        for x in c.get("open", []):
            print(f"  {x['slug']}: {x['condition']}: {x['detail']}")
        print(f"{len(c.get('open', []))} open condition(s) (health quorum's own evaluate, refreshed now)")
        ui_term.pause()
        return 0
    print(f"unknown action {verb}")
    return 2


MENU = [
    ("status", "status of every role (with health conditions)"),
    ("asks", "open asks and roles waiting on a question: attach or answer"),
    ("next", "a role's queue, offers and mail (next --peek, marks nothing read)"),
    ("msg", "message a role or everyone (optionally with a wake)"),
    ("offer", "offer an item to a role"),
    ("withdraw", "withdraw an offer"),
    ("offline", "user-offline mode on/off"),
    ("freeze", "freeze mode on/off"),
    ("conditions", "health conditions now (refresh)"),
    ("metrics", "failure-mode metrics and report files"),
    ("pick", "open a standalone (non-shoal) session"),
    ("open", "re-apply the shoal layouts (fills gaps only)"),
    ("upgrade", "preview a shoal upgrade (applying stays a CLI step)"),
]


def cmd_menu(a) -> int:
    fs, fr = focused_role()
    print("\x1b[1mshoal\x1b[0m" + (f"  (focused: {fs}/{fr})" if fr else ""))
    i = _choose([f"{k:<11} {d}" for k, d in MENU], "action")
    if i is None:
        return 0
    k = MENU[i][0]
    if k == "metrics":
        return subprocess.run(shoal_argv("ui", "metrics", "--pause")).returncode
    if k == "pick":
        return subprocess.run(shoal_argv("ui", "pick")).returncode
    if k == "open":
        rc = subprocess.run(shoal_argv("ui", "open")).returncode
        ui_term.pause()
        return rc
    rc = act(k)
    if k not in ("status", "next", "asks", "upgrade", "conditions"):
        ui_term.pause()
    return rc


def cmd_act(a) -> int:
    rc = act(a.verb, a.target_shoal, a.role)
    if a.pause:
        ui_term.pause()
    return rc


# ---- config, install, doctor -----------------------------------------------------------

BEGIN, END = "# >>> shoal-ui managed block (shoal ui herdr-config) >>>", "# <<< shoal-ui managed block <<<"


def keys_block() -> str:
    """Operator keys (prefix then Alt+letter; Alt+s is the palette). Defined server side; the
    launcher attaches with `--remote-keybindings server` so these reach the laptop client."""
    keys = [("prefix+alt+s", "menu", "shoal: action palette"),
            ("prefix+shift+s", "menu", "shoal: action palette (no-Alt fallback)"),
            ("prefix+alt+a", "asks", "shoal: asks"),
            ("prefix+alt+n", "next", "shoal: next --peek (focused role)"), ("prefix+alt+m", "msg", "shoal: message"),
            ("prefix+alt+o", "pick", "shoal: open a standalone session"), ("prefix+alt+t", "status", "shoal: status"),
            ("prefix+alt+g", "metrics", "shoal: metrics")]
    out = []
    for k, action, desc in keys:
        out.append(f'[[keys.command]]\nkey = "{k}"\ntype = "plugin_action"\ncommand = "{PLUGIN_ID}.{action}"\n'
                   f'description = "{desc}"\n')
    return "\n".join(out)


def sidebar_block() -> str:
    """Client side (the viewing machine draws these): agent rows by role name, with the shoal's
    short tag dim beside it (the agents list is already grouped by workspace, one per shoal), and
    the spaces rows with the per-shoal tokens. The tab-bar status line is NOT here: a remote client
    draws the server's `tab_bar_right` (measured in the laptop pilot), so it is in server_block."""
    return """[ui]
status_indicators = "symbols"

[ui.sidebar.agents]
row_gap = 0
rows = [
  ["state_icon", "machine", { token = "agent", bold = true }, { token = "$shoal", dim = true }],
  [{ token = "$posture", rules = [{ equals = "DEAD", fg = "#f38ba8", bold = true }, { equals = "QUESTION", fg = "#fab387", bold = true }, { equals = "NOWATCH", fg = "#f38ba8" }, { contains = "?", fg = "#f9e2af", bold = true }, { starts_with = "wait", fg = "#89b4fa" }] }, "$item", "$age"],
  [{ token = "$asks", fg = "#fab387", bold = true }, { token = "$next", rules = [{ contains = "!", fg = "#f9e2af" }] }, "$wait", { token = "$why", dim = true }],
]

[ui.sidebar.spaces]
row_gap = 0
rows = [
  ["state_icon", "workspace"],
  [{ token = "$shoal_mode", rules = [{ equals = "ok", hide = true }, { equals = "OFFLINE", fg = "#89b4fa", bold = true }, { equals = "FREEZE", fg = "#f38ba8", bold = true }] }, { token = "$shoal_needs", rules = [{ equals = "0", hide = true }, { gt = 0, fg = "#fab387", bold = true }] }, { token = "$shoal_relay", rules = [{ equals = "ok", hide = true }, { equals = "DEAD", fg = "#f38ba8", bold = true }] }, { token = "$shoal_tooling", dim = true }],
  ["branch", "git_status"],
]
"""


def server_block() -> str:
    return f"""onboarding = false

[update]
# pinned by the host installer; never self-update a lane host's herdr
version_check = false

[session]
# NEVER resume agents on restore: resuming a running Claude role forks it, and an opencode resume
# starts a second server on the live store. Panes restore as shells; `shoal ui open` (the plugin's
# startup hook) puts the role cards back.
resume_agents_on_restore = false

[ui]
# the tab-bar status line renders from the SERVER's config, also for a `herdr --remote` client
tab_bar_right = [
  {{ type = "command", command = {json.dumps(shlex.join(shoal_argv("ui", "line")))}, interval_seconds = 30, timeout_seconds = 20 }},
  {{ type = "hostname" }},
]

{keys_block()}"""


def client_block() -> str:
    return sidebar_block()


def merge_block(path: str, block: str) -> str:
    """Replace our managed block in a TOML file, or append it. Refuses (raises) when the file
    already defines a table the block defines outside the block (TOML forbids duplicates)."""
    cur = open(path).read() if os.path.exists(path) else ""
    if BEGIN in cur and END in cur:
        pre, rest = cur.split(BEGIN, 1)
        post = rest.split(END, 1)[1]
    else:
        pre, post = cur, ""
    outside = pre + post
    import re
    for t in sorted(set(re.findall(r"^\[([A-Za-z0-9_.]+)\]\s*$", block, re.M))):
        if re.search(rf"^\[{re.escape(t)}\]\s*$", outside, re.M):
            raise HerdrError(f"{path} already defines [{t}] outside the managed block: move it into the block first")
    for k in re.findall(r"^([a-z_]+)\s*=", block.split("[", 1)[0], re.M):
        if re.search(rf"^{k}\s*=", outside.split("[", 1)[0], re.M):
            raise HerdrError(f"{path} already sets top-level `{k}` outside the managed block")
    body = f"{BEGIN}\n{block.rstrip()}\n{END}\n"
    # top-level keys must precede every table: our block goes first when it has top-level keys
    if block.lstrip().split("\n", 1)[0].find("=") > 0 and not block.lstrip().startswith("["):
        return body + "\n" + outside.strip() + ("\n" if outside.strip() else "")
    return (outside.rstrip() + "\n\n" if outside.strip() else "") + body


def cmd_config(a) -> int:
    block = client_block() if a.client else server_block()
    if not a.write:
        print(block)
        return 0
    path = a.path or os.path.join(config_dir(), "config.toml")
    try:
        new = merge_block(path, block)
    except HerdrError as exc:
        print(f"herdr-config: {exc}")
        return 1
    if a.dry_run:
        print(new)
        return 0
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if os.path.exists(path):
        import shutil
        shutil.copy2(path, path + ".bak-shoal")
    tmp = path + ".tmp"
    open(tmp, "w").write(new)
    os.replace(tmp, path)
    print(f"herdr-config: wrote the managed block to {path}")
    return 0


def unit_text(kind: str) -> str:
    herdr = config.env("HERDR_BIN") or os.path.expanduser("~/.local/bin/herdr")
    path = ":".join([os.path.expanduser("~/.local/bin"), config.tool_path() or "/usr/local/bin:/usr/bin:/bin"])
    if kind == "server":
        return f"""[Unit]
Description=herdr server for the shoal UI (session {session_name()})
[Service]
Type=simple
Environment=HERDR_SESSION={session_name()}
Environment=PATH={path}
ExecStart={herdr} server
Restart=on-failure
RestartSec=10
[Install]
WantedBy=default.target
"""
    return f"""[Unit]
Description=shoal UI feeder: role state and tokens into herdr (read-only to shoal)
After=shoal-herdr.service
[Service]
Type=simple
Environment=HERDR_SESSION={session_name()}
Environment=PATH={path}
ExecStart={shlex.join(shoal_argv("ui", "feed"))}
Restart=always
RestartSec=15
MemoryHigh=256M
[Install]
WantedBy=default.target
"""


def cmd_install(a) -> int:
    """Server side, idempotent: managed config block, plugin link, the herdr server and feeder
    user units. No session is touched; nothing here can wake a role."""
    steps = []
    unitdir = os.path.expanduser("~/.config/systemd/user")
    herdr = config.env("HERDR_BIN") or os.path.expanduser("~/.local/bin/herdr")
    if not os.path.exists(herdr):
        print(f"herdr-install: no herdr at {herdr}: install the pinned release first")
        return 1
    steps.append(("config block", lambda: cmd_config(argparse_ns(client=False, write=True, path="", dry_run=False))))
    steps.append(("plugin link", lambda: subprocess.run([herdr, "plugin", "link", PLUGIN_DIR], check=False).returncode))
    for name, kind in (("shoal-herdr.service", "server"), ("shoal-ui-feed.service", "feed")):
        def w(name=name, kind=kind):
            os.makedirs(unitdir, exist_ok=True)
            open(os.path.join(unitdir, name), "w").write(unit_text(kind))
            return 0
        steps.append((f"unit {name}", w))
    steps.append(("enable units", lambda: subprocess.run(
        ["sh", "-c", "systemctl --user daemon-reload && systemctl --user enable --now shoal-herdr.service "
                     "&& sleep 2 && systemctl --user enable --now shoal-ui-feed.service"], check=False).returncode))
    for label, fn in steps:
        if a.dry_run:
            print(f"[dry-run] {label}")
            continue
        rc = fn()
        print(f"{label}: {'ok' if not rc else f'rc={rc}'}")
        if rc:
            return rc
    if not a.dry_run:
        time.sleep(2)
        return cmd_open(argparse_ns(session="", ids=[], ledger_root="", dry_run=False, no_standalone=False))
    return 0


def argparse_ns(**kw):
    import argparse
    return argparse.Namespace(**kw)


def doctor_checks(session: str = "") -> list[tuple[str, bool, str]]:
    out = []
    herdr = config.env("HERDR_BIN") or os.path.expanduser("~/.local/bin/herdr")
    out.append(("herdr installed", os.path.exists(herdr), herdr))
    cfg = os.path.join(config_dir(), "config.toml")
    txt = open(cfg).read() if os.path.exists(cfg) else ""
    import re
    off = bool(re.search(r"^\s*resume_agents_on_restore\s*=\s*false", txt, re.M))
    out.append(("agent resume on restore is off", off, cfg))
    integ = []
    try:
        claude_settings = os.path.expanduser("~/.claude/settings.json")
        if "herdr" in (open(claude_settings).read() if os.path.exists(claude_settings) else ""):
            integ.append("claude hooks mention herdr")
    except OSError:
        pass
    for d in (os.path.expanduser("~/.config/opencode/plugin"), os.path.expanduser("~/.config/opencode/plugins")):
        if os.path.isdir(d) and any("herdr" in f for f in os.listdir(d)):
            integ.append(f"opencode plugin in {d}")
    out.append(("no herdr agent integrations on this lane host", not integ, "; ".join(integ) or "none"))
    h = Herdr(session)
    up = h.alive()
    out.append(("herdr server reachable", up, h.path))
    if os.path.exists(herdr):
        try:
            r = subprocess.run([herdr, "api", "schema", "--json"], capture_output=True, text=True, timeout=20)
            schema = r.stdout
            missing = [m for m in METHODS if f'"{m}"' not in schema]
            out.append(("socket methods the adapter uses", not missing, "missing: " + ", ".join(missing) if missing else "all present"))
        except (OSError, subprocess.SubprocessError) as exc:
            out.append(("socket methods the adapter uses", False, str(exc)))
        try:
            r = subprocess.run([herdr, "plugin", "list"], capture_output=True, text=True, timeout=20,
                               env=dict(os.environ, HERDR_SESSION=session or session_name()))
            out.append(("plugin linked", PLUGIN_ID in r.stdout, PLUGIN_DIR))
        except (OSError, subprocess.SubprocessError) as exc:
            out.append(("plugin linked", False, str(exc)))
    for unit in ("shoal-herdr.service", "shoal-ui-feed.service"):
        r = subprocess.run(["systemctl", "--user", "is-active", unit], capture_output=True, text=True)
        out.append((f"unit {unit} active", r.stdout.strip() == "active", r.stdout.strip()))
    return out


def cmd_doctor(a) -> int:
    bad = 0
    for name, ok, detail in doctor_checks(a.session):
        print(f"{'ok  ' if ok else 'FAIL'} {name}: {detail}")
        bad += not ok
    return 1 if bad else 0
