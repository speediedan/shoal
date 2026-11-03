#!/usr/bin/env python3
"""The Herdr adapter (docs/ui.md): shoal's read model and operator verbs on Herdr's primitives.

  shoal ui open    [--shoal ID ...]         one workspace per shoal (overview + role cards), plus
                                            the saved standalone sessions; idempotent
  shoal ui feed    [--interval S] [--once]  push role state and tokens to Herdr (read-only to shoal)
  shoal ui menu                             the operator palette (a Herdr popup)
  shoal ui act VERB ...                     one operator verb: preview (`--dry-run`), confirm, run
  shoal ui pick    [--save]                 attach a standalone session from any supported harness
  shoal ui dash | tail | metrics            overview, ledger tail and metrics panes
  shoal ui herdr-config --client|--server   the managed Herdr config blocks
  shoal ui herdr-install [--dry-run]        server side: config block, plugin link, systemd units
  shoal ui doctor                           adapter preflight (restore off, no integrations, schema)

Rules this adapter keeps (design driver 1 and the coexistence constraints):
- It never types into a role's pane and never calls `pane.send_*`, `agent.prompt`, `agent.send_keys`
  or `pane.close`: every action is a shoal CLI verb, so `Backend.wake` accounting holds. The
  selftest greps this file for those calls. `pane.move` is allowed: it relocates a live card between
  tabs and changes no process.
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
FEED_MIN_TTL_S = 150  # token lifetime floor: one slow tick must not blank every sidebar row
PLUGIN_DIR = os.path.join(REPO, "plugins", "shoal-herdr")
PLUGIN_ID = "shoal.herdr"
SHELLS = {"bash", "zsh", "fish", "sh", "dash", "ksh", "tcsh"}
# socket methods the adapter calls; `ui doctor` checks the running Herdr still offers them
METHODS = ("workspace.list", "workspace.create", "workspace.rename", "workspace.close", "workspace.report_metadata",
           "pane.list", "pane.get",
           "pane.process_info", "pane.report_agent", "pane.report_metadata", "layout.apply", "tab.close", "tab.rename", "pane.move")


# ---- Herdr socket ----------------------------------------------------------------------

class HerdrError(RuntimeError):
    pass


def session_name() -> str:
    return config.env("HERDR_SESSION") or os.environ.get("HERDR_SESSION") or DEFAULT_SESSION


def config_dir() -> str:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, "herdr")


def config_path() -> str:
    """The config file this Herdr reads: `HERDR_CONFIG_PATH` (Herdr's own override, set for a server and its
    clients that keep a config of their own), else `config.toml` in the config directory."""
    return os.path.expanduser(os.environ.get("HERDR_CONFIG_PATH") or "") or os.path.join(config_dir(), "config.toml")


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
    """The dashboard above, the ledger tail as a strip below it: `ledger_share` percent of the tab
    (viewer setting, default 12: the tail starts minimized)."""
    import ui_status
    try:
        share = min(80, max(5, int(ui_status.ui_config().get("ledger_share") or 12)))
    except (TypeError, ValueError):
        share = 12
    return {"type": "split", "direction": "down", "ratio": round(1 - share / 100, 2),
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
    """Roles that get a card: every role with a session that this host can attach."""
    import routes
    from roles import host_of, is_local
    # a role on another host gets a card only when this host has an ssh route to it (`[hosts.<server>]`):
    # its session can be attached only on its own host, so the card runs that host's `shoal view` over ssh.
    # Without a route it stays in the overview table, with its host, and is answered by mail.
    return [r for r in spec.roles if r.session and (is_local(r, spec) or routes.route(host_of(r, spec)) is not None)]


def role_tabs(spec) -> list[tuple[str, list]]:
    """The roles split into tabs of `roles_per_tab` cards (viewer setting, default 4: a 2x2 grid).
    Order: the viewer's `role_order.<shoal>` list when set, else self-filling lanes, then assigned
    lanes, then infrastructure (fill=none), each in spec order. One tab is `roles`; several are
    `roles 1`, `roles 2`, ..."""
    import ui_status
    cfg = ui_status.ui_config()
    try:
        per = max(1, int(cfg.get("roles_per_tab") or 4))
    except (TypeError, ValueError):
        per = 4
    roles = viewable_roles(spec)
    order = (cfg.get("role_order") or {}).get(spec.sid) or []
    if order:
        rank = {s: i for i, s in enumerate(order)}
        roles = sorted(roles, key=lambda r: (rank.get(r.slug, len(rank)), spec.roles.index(r)))
    else:
        group = {"self": 0, "assigned": 1}
        roles = sorted(roles, key=lambda r: (group.get(getattr(r, "fill", ""), 2), spec.roles.index(r)))
    chunks = [roles[i:i + per] for i in range(0, len(roles), per)] or [[]]
    if len(chunks) == 1:
        return [("roles", chunks[0])]
    return [(f"roles {i + 1}", c) for i, c in enumerate(chunks)]


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


def retired_ids() -> list[str]:
    import ui_status
    live = set(ui_status.discover_all("", None, False))
    return [os.path.basename(os.path.dirname(p)) for p in ui_status.discover_all("", None, True) if p not in live]


def release_retired(h: Herdr, close: bool, dry_run: bool = False) -> list[str]:
    """Free a retired shoal's short tag for its successor (same stem, same tag). A workspace whose
    panes belong to a retired shoal is closed when every pane is disposable (`close`, from `ui
    open`), else relabelled with the retired shoal's full id, so the successor never inherits it or
    cards bound to retired sessions. One-way coupling: `shoal retire` never calls the UI; the UI
    reacts to a retired spec."""
    out = []
    gone = retired_ids()
    if not gone:
        return out
    panes = h.panes()
    for w in h.workspaces():
        mine = [p for p in panes if p.get("workspace_id") == w["workspace_id"]]
        owners = {str(p.get("label") or "").split("/", 1)[0] for p in mine if "/" in str(p.get("label") or "")}
        rid = next((r for r in gone if r in owners), None)
        if rid is None or owners - {rid}:
            continue
        if close and mine and all(disposable(h, p["pane_id"]) for p in mine):
            out.append(f"close workspace {w.get('label')!r} of retired shoal {rid}")
            if not dry_run:
                h.call("workspace.close", {"workspace_id": w["workspace_id"]})
        elif w.get("label") != rid:
            out.append(f"relabel workspace {w.get('label')!r} of retired shoal {rid} to its full id"
                       + (" (a pane is in use)" if close else ""))
            if not dry_run:
                h.call("workspace.rename", {"workspace_id": w["workspace_id"], "label": rid})
    return out


def release_hidden(h: Herdr, dry_run: bool = False, keep: list[str] | None = None) -> list[str]:
    """Close the workspace of each shoal the viewer hides (`shoal ui hide`), when every pane in it is
    ours to replace; one in use is left open and named. The shoal itself is untouched."""
    import ui_status
    out = []
    for sid in sorted(ui_status.hidden_shoals() - set(keep or [])):  # a shoal named on the command line wins
        for label in {shoal_tag(sid), sid, WS_PREFIX + sid}:
            w = find_ws(h, label)
            if w is None:
                continue
            panes = [p for p in h.panes() if p.get("workspace_id") == w["workspace_id"]]
            if panes and not all(disposable(h, p["pane_id"]) for p in panes):
                out.append(f"hidden shoal {sid}: workspace {label!r} is in use (detach, then `shoal ui open` again)")
                continue
            out.append(f"close workspace {label!r} of hidden shoal {sid}")
            if not dry_run:
                h.call("workspace.close", {"workspace_id": w["workspace_id"]})
    return out


UI_SETTINGS = {"roles_per_tab": "cards per roles tab (default 4: a 2x2 grid)",
               "usage_providers": "comma-separated subscription providers; empty disables account polling",
               "usage_bridge": "JSON object: providers and command argv for an operator-owned quota reader",
               "sidebar_max_width": "sidebar max width: columns, or a share of the window like 50% (laptop launcher)",
               "role_order": "role_order.<shoal> = comma-separated role slugs, first shown first",
               "pane_borders": "auto | off: off drops Herdr's plain pane titles so each card's colored header is on top (laptop)",
               "auto_rebalance": "true (default) | false: the feeder re-lays role tabs after a role change or a new roles_per_tab",
               "ledger_share": "percent of the overview tab for the ledger tail strip (default 12)",
               "agent_sort": "priority (default: needs-you first, Herdr's attention queue) | spaces (grouped by shoal)",
               "line_width": "columns the tab-bar status line may use (default 80); wider shows more shoals in full",
               "image_paste_key": "Herdr's remote image paste key (default alt+v; Ctrl+V pastes text in the kitty window)"}


def cmd_set(a) -> int:
    import ui_status
    d = ui_status.ui_config()
    key, _, sub = a.key.partition(".")
    if key not in UI_SETTINGS or (key == "role_order") != bool(sub):
        print("ui set: one of " + ", ".join(UI_SETTINGS) + " (role_order needs .<shoal>)")
        return 1
    if key == "usage_bridge":
        if a.value:
            try:
                b = json.loads(a.value)
                import subscription_usage
                assert isinstance(b, dict) and isinstance(b["command"], list) and b["command"]
                assert all(isinstance(s, str) and s for s in b["command"])
                assert isinstance(b["providers"], list) and b["providers"]
                assert all(s in subscription_usage.PROVIDERS for s in b["providers"])
            except (ValueError, KeyError, TypeError, AssertionError):
                print("usage_bridge needs providers and a nonempty command argv array")
                return 1
            d[key] = b
        else:
            d.pop(key, None)
    elif key == "usage_providers":
        import subscription_usage
        providers = [s.strip() for s in a.value.split(",") if s.strip()]
        if any(s not in subscription_usage.PROVIDERS for s in providers):
            print("ui set: supported usage providers: " + ",".join(subscription_usage.PROVIDERS))
            return 1
        d[key] = list(dict.fromkeys(providers))
    elif key == "role_order":
        d.setdefault("role_order", {})[sub] = [s.strip() for s in a.value.split(",") if s.strip()]
    elif a.value == "":
        d.pop(key, None)
    else:
        d[key] = int(a.value) if a.value.isdigit() else {"true": True, "false": False}.get(a.value.lower(), a.value)
    ui_status.save_ui_config(d)
    print(f"{a.key} = {a.value or '(default)'} ({ui_status.ui_config_path()})")
    if key in ("usage_providers", "usage_bridge"):
        print("the feeder refreshes account meters in the background; `shoal ui usage --refresh` reads now")
    elif key in ("roles_per_tab", "role_order", "auto_rebalance"):
        try:  # lay them out now; the feeder would within a minute (cards in use are moved, never replaced)
            h = Herdr()
            lines = rebalance_all(h) if h.alive() and ui_status.ui_config().get("auto_rebalance", True) is not False \
                else ["run `shoal ui open` to lay the roles out again (refused while a card is in use)"]
        except Exception as exc:  # noqa: BLE001
            lines = [f"run `shoal ui open` to lay the roles out again ({exc})"]
        for line in lines or ["roles tabs already match"]:
            print(line)
    else:
        print("on the laptop: re-run shoal-herdr-setup.sh, then relaunch (the launcher resolves a % width)")
    return 0


def cmd_hide(a) -> int:
    import ui_status
    d = ui_status.ui_config()
    hidden = set(d.get("hidden_shoals") or [])
    if a.unhide:
        hidden -= set(a.shoal)
    else:
        hidden |= set(a.shoal)
    d["hidden_shoals"] = sorted(hidden)
    ui_status.save_ui_config(d)
    print(f"hidden shoals: {', '.join(d['hidden_shoals']) or '(none)'} ({ui_status.ui_config_path()})")
    print("run `shoal ui open` to " + ("add its workspace back" if a.unhide else "close its workspace (when nothing in it is in use)"))
    return 0


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


def consolidation_moves(spec, by_label: dict, tabs_want: list) -> tuple[list[tuple[str, str, str, str]], dict[str, str]]:
    """Regroup live role cards into the configured tabs WITHOUT replacing any of them: ([(pane_id,
    home tab id, target pane id, split)], {home tab id: tab label}). A chunk's home is the tab already
    holding most of its cards (one chunk per tab); a card elsewhere moves to its home with Herdr's
    `pane.move`, which relocates the live pane (an attached session keeps running). It is used when a
    relayout is wanted but a card is attached, so the re-apply the unattached case uses is refused."""
    homes: list[str | None] = []
    used: set = set()
    for _, chunk in tabs_want:
        count: dict[str, int] = {}
        for r in chunk:
            p = by_label.get(role_label(spec.sid, r.slug))
            if p:
                count[p["tab_id"]] = count.get(p["tab_id"], 0) + 1
        pick = next((tid for tid, _n in sorted(count.items(), key=lambda kv: -kv[1]) if tid not in used), None)
        homes.append(pick)
        used.add(pick)
    moves, labels = [], {}
    for (label_, chunk), home in zip(tabs_want, homes):
        if home is None:
            continue
        labels[home] = label_
        here = [by_label[role_label(spec.sid, r.slug)]["pane_id"] for r in chunk
                if by_label.get(role_label(spec.sid, r.slug), {}).get("tab_id") == home]
        for r in chunk:
            p = by_label.get(role_label(spec.sid, r.slug))
            if not p or p["tab_id"] == home or not here:
                continue
            n = len(here)
            moves.append((p["pane_id"], home, here[-1] if n == 1 else here[n - 2], "down" if n == 1 else "right"))
            here.append(p["pane_id"])
    return moves, labels


def ensure_shoal(h: Herdr, spec, dry_run: bool = False, reapply: bool = False) -> list[str]:
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
        for label_, chunk in role_tabs(spec):
            if chunk:
                apply_tab(h, ws["workspace_id"], label_, _grid([role_node(spec, r) for r in chunk]))
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
    tabs_want = role_tabs(spec)
    tab_of = {p.get("label"): p.get("tab_id") for p in cards}
    groups = [{tab_of.get(role_label(spec.sid, r.slug)) for r in chunk} for _, chunk in tabs_want]
    # the card of a role no longer viewable (removed, parked, retired) lingers in its tab; a free one is relaid away
    gone = [p for p in panes if str(p.get("label", "")).startswith(spec.sid + "/") and p.get("label") not in want
            and p.get("label") not in (f"{spec.sid}/overview", f"{spec.sid}/ledger") and disposable(h, p["pane_id"])]
    relayout = bool(cards) and not missing and (any(len(g) != 1 for g in groups) or bool(gone)
                                                 or len({next(iter(g)) for g in groups if len(g) == 1}) != len(groups))
    if missing or (cards and len(shells) == len(cards)) or (reapply and cards) or relayout:
        why = (f"add {', '.join(missing)}" if missing else "restore cards after a restart"
               if len(shells) == len(cards) else f"remove {', '.join(p['label'] for p in gone)}" if gone
               else "lay the roles out in tabs of the configured size" if relayout
               else "restart the cards on the current code (--reapply)")
        cards = cards + gone  # their tabs are relaid (or closed) with the rest
        if attached and missing:
            # a role added while the operator is attached to cards (fts-docs, 2026-10-13: all three fts
            # cards attached): an attached card is never replaced, so the new cards open in a tab of their
            # own. The next `ui open` with the cards free lays every tab out at the configured size again.
            new = [r for r in roles if role_label(spec.sid, r.slug) in missing]
            out.append(f"add {', '.join(missing)} in a new tab ({', '.join(attached)} in use, so the roles "
                       f"tabs are kept; free them and run `shoal ui open` to lay all roles out together)")
            if not dry_run:
                # number every roles tab once there are several: a lone `roles` becomes `roles 1`
                for i, tid in enumerate(sorted(x for x in tabs if x), 1):
                    h.call("tab.rename", {"tab_id": tid, "label": f"roles {i}"})
                apply_tab(h, ws["workspace_id"], f"roles {len(tabs) + 1}", _grid([role_node(spec, r) for r in new]))
        elif attached and relayout and not reapply and len(shells) != len(cards):
            # the roles are spread over more tabs than the configured size needs (a role was added while
            # cards were attached: roles 2 held three, roles 3 one) but a card is in use, so nothing may be
            # re-applied: move the stray cards into their tabs instead, live sessions untouched
            moves, labels = consolidation_moves(spec, {p["label"]: p for p in cards}, tabs_want)
            if not moves:  # a split that needs a new tab: no live pane can be moved into a tab that does not exist
                out.append(f"roles tabs need a new tab ({why}) but {', '.join(attached)} is in use: it waits until "
                           f"the cards are free")
            else:
                out.append(f"move {len(moves)} card(s) into the configured tabs ({', '.join(attached)} in use, so "
                           f"no card is replaced)")
            if moves and not dry_run:
                for pane_id, home, target, split in moves:
                    h.call("pane.move", {"pane_id": pane_id, "destination": {"type": "tab", "tab_id": home,
                                                                              "split": split,
                                                                              "target_pane_id": target}})
                for tid, label_ in labels.items():
                    h.call("tab.rename", {"tab_id": tid, "label": label_})
        elif attached:
            out.append(f"roles tab needs a re-apply ({why}) but {', '.join(attached)} is in use (attached, "
                       f"or running something from its shell): free it, then run `shoal ui open` again")
        else:
            out.append(f"re-apply roles tab{'s' if len(tabs_want) > 1 else ''}: {why}")
            if not dry_run and roles:
                old_tabs = [x for x in dict.fromkeys(p.get("tab_id") for p in cards) if x]
                for i, (label_, chunk) in enumerate(tabs_want):
                    if chunk:
                        apply_tab(h, ws["workspace_id"], label_, _grid([role_node(spec, r) for r in chunk]),
                                  tab_id=old_tabs[i] if i < len(old_tabs) else "")
                for extra in old_tabs[len(tabs_want):]:  # every card in it was checked disposable above
                    h.call("tab.close", {"tab_id": extra})
    ov = [by_label.get(f"{spec.sid}/overview"), by_label.get(f"{spec.sid}/ledger")]
    ov_reapply = reapply and all(p is not None for p in ov) and all(disposable(h, p["pane_id"]) for p in ov)
    if any(p is None for p in ov) or all(h.foreground_is_shell(p["pane_id"]) for p in ov if p) or ov_reapply:
        out.append("re-apply overview tab")
        if not dry_run:
            tab = next((p.get("tab_id") for p in ov if p), None)
            apply_tab(h, ws["workspace_id"], "overview", overview_root(spec), tab_id=tab or "")
    n_tabs = len(role_tabs(spec))
    return out or [f"{label}: up to date ({len(roles)} role cards" + (f" in {n_tabs} tabs)" if n_tabs > 1 else ")")]


# ---- standalone sessions ---------------------------------------------------------------

def standalone_path() -> str:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, "shoal", "ui-standalone.json")


def unique_by_label(entries: list[dict]) -> tuple[list[dict], list[dict]]:
    """(kept, dropped): two saved sessions with one label share a pane label, so their feeder reports
    collided (equal sequence numbers: the first, an old idle seat, won and a working seat read idle,
    2026-10-19). The later entry wins, as the newer registration is the live one."""
    last = {}
    for i, e in enumerate(entries):
        last[e.get("label") or e["session"][:12]] = i
    kept = [e for i, e in enumerate(entries) if last[e.get("label") or e["session"][:12]] == i]
    return kept, [e for e in entries if e not in kept]


def load_standalone(raw: bool = False) -> list[dict]:
    try:
        entries = list(json.load(open(standalone_path())).get("sessions", []))
    except (OSError, ValueError):
        return []
    return entries if raw else unique_by_label(entries)[0]


def save_standalone(entries: list[dict]) -> None:
    p = standalone_path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w") as fh:
        json.dump({"v": 1, "sessions": entries}, fh, indent=1)
    os.replace(tmp, p)


BGFOLLOW_BACKUP = ".bak-bgfollow-"


def update_standalone(match: dict, updates: dict, backup: bool = False) -> bool:
    """Merge `updates` into the saved entry `match` names (same harness, session and label), by atomic replace
    (save_standalone). With `backup`, the registry file is first copied once to `ui-standalone.json.bak-bgfollow-<utc>`
    (only when no such backup exists yet), so the first `/bg` follow can be undone by hand. False when the entry is
    gone (another writer moved it): nothing is written then."""
    import glob
    import shutil
    entries = load_standalone(raw=True)
    lab = match.get("label") or match["session"][:12]
    hit = next((x for x in entries if not x.get("host") and x.get("harness") == match.get("harness")
                and x.get("session") == match.get("session") and (x.get("label") or x["session"][:12]) == lab), None)
    if hit is None:
        return False
    p = standalone_path()
    if backup and os.path.exists(p) and not glob.glob(glob.escape(p) + BGFOLLOW_BACKUP + "*"):
        shutil.copy2(p, p + BGFOLLOW_BACKUP + time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()))
    hit.update(updates)
    save_standalone(entries)
    return True


def standalone_label(e: dict) -> str:
    return f"{STANDALONE_WS}/{e.get('label') or e['session'][:12]}"


def standalone_argv(e: dict) -> list[str]:
    return shoal_argv("view", "--harness", e["harness"], "--session", e["session"],
                      "--label", e.get("label") or e["session"][:12], "--stub",
                      *(["--host", e["host"]] if e.get("host") else []),
                      *(["--job", e["job"]] if e.get("job") else []))


def local_standalone() -> list[dict]:
    """This host's own saved standalone sessions, open in its viewer (not ones mirrored from other hosts)."""
    return [e for e in load_standalone() if not e.get("host") and not e.get("dismissed")]


def cmd_standalone(a) -> int:
    """`shoal ui standalone [--json]`: this host's saved standalone sessions with their turn state, which is
    what another host's viewer pulls over its route to give them cards there."""
    import ui_status
    rows = ui_status.claude_rows()
    out = []
    for e in local_standalone():
        st, hstate = standalone_state(e, rows)
        out.append({k: e[k] for k in ("harness", "session", "job", "label", "cwd", "model", "effort") if e.get(k)} | {"turn": st, "state": hstate})
    if getattr(a, "json", False):
        print(json.dumps({"v": 1, "host": config.server(), "sessions": out}))
    else:
        for x in out:
            print(f"{x['harness']:<8} {x.get('label', ''):<40} {x['turn']:<9} {x['session'][:16]}")
    return 0


def record_claude_jobs(entries: list[dict]) -> bool:
    """Give each local Claude entry its job id (stable across `/clear`), resolved while the stored conversation id
    still names it; the card then finds the session by job even after its conversation id changes."""
    import ui_status
    rows = None
    changed = False
    for e in entries:
        if e.get("harness") != "claude" or e.get("host") or e.get("job"):
            continue
        rows = ui_status.claude_rows() if rows is None else rows
        r = ui_status.claude_session(rows, e["session"])
        if r is not None and r.get("id"):
            e["job"] = str(r["id"])
            changed = True
    return changed


def reconcile_remote_standalone(entries: list[dict]) -> tuple[bool, list[str]]:
    """Mirror each routed host's saved standalone sessions into this registry (`host` set, label suffixed
    `@<host>` so it cannot collide with a local one), so `ui open` gives them cards whose Enter attaches over
    ssh, and the usual flags (opened, dismissed) work on them. A reachable host's list is the truth (gone
    there, gone here); an unreachable host's mirrored entries stay as they were."""
    import routes
    changed, notes = False, []
    for host in sorted(routes.routes()):
        got, ok = routes.remote_standalone(host)
        if not ok:
            notes.append(f"standalone on {host}: not reachable now; its mirrored sessions are left as they were")
            continue
        want = {(g["harness"], g["session"]): g for g in got if g.get("harness") and g.get("session")}
        keep = []
        for e in entries:
            if e.get("host") == host and (e["harness"], e["session"]) not in want:
                changed = True
                notes.append(f"standalone {e.get('label')}: no longer saved on {host}; dropped here")
                continue
            keep.append(e)
        have = {(e["harness"], e["session"]) for e in keep if e.get("host") == host}
        for k, g in want.items():
            if k not in have:
                keep.append({"harness": g["harness"], "session": g["session"], "host": host,
                             **({"job": g["job"]} if g.get("job") else {}),
                             "label": f"{(g.get('label') or g['session'][:12])[:32]}@{host}"})
                changed = True
        entries[:] = keep
    return changed, notes


def remote_standalone_state(e: dict) -> tuple[str, str]:
    """(turn word, herdr state) for a mirrored session, from its host's own probe (cached pull)."""
    import routes
    got, ok = routes.remote_standalone(e["host"])
    g = next((x for x in got if x.get("session") == e["session"]), None)
    if g is None:
        return "unknown", "idle"
    return (g.get("turn") or "unknown") + ("" if ok else "~"), g.get("state") or "idle"


def standalone_ws_label(e: dict) -> str:
    """A standalone session's own workspace is named after the session, so the spaces list shows
    it; a name that collides with a shoal's tag gets a suffix."""
    name = (e.get("label") or e["session"][:12])[:40]
    return name + " (standalone)" if name in set(_TAGS.values()) else name


def open_standalone(h: Herdr, e: dict, dry_run: bool = False, focus: bool = False, reapply: bool = False) -> str:
    """One workspace per standalone session, named after it (found by its pane label wherever it
    is, so sessions opened before this layout keep working)."""
    shoal_tag("")  # load the shoal tags so a colliding name is suffixed
    lab = standalone_label(e)
    node = {"type": "pane", "label": lab, "cwd": e.get("cwd") or os.path.expanduser("~"), "command": standalone_argv(e)}
    p = next((q for q in h.panes() if q.get("label") == lab), None)
    if p is not None:
        shell = h.foreground_is_shell(p["pane_id"])
        if shell or (reapply and disposable(h, p["pane_id"])):
            if not dry_run:
                apply_tab(h, p["workspace_id"], e.get("label") or e["session"][:12], node, tab_id=p["tab_id"])
            return f"{'restore' if shell else 'restart on the current code'} standalone {lab}"
        return f"standalone {lab}: open" + (" (in use: not re-applied)" if reapply else "")
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
    """Available sessions on this host, excluding roles, old incarnations and internal runs."""
    import backends
    slugs = role_slugs()
    out = []
    for harness in backends.ecosystems():
        try:
            for row in backends.get(harness).sessions():
                sid = str(row.get("session") or "")
                label = str(row.get("label") or sid[:12])
                if not sid or any(sid.startswith(x) or x.startswith(sid) for x in exclude if x):
                    continue
                if internal_session(label, slugs):
                    continue
                out.append(dict(row, harness=harness, session=sid, label=label[:40]))
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
            print(f"{harness} sessions unavailable: {exc}")
    # a saved entry stays pickable when its harness does not list it (a headless thread, a stopped or ended
    # Claude session, a failed listing) or it was dismissed: picking it again reopens its card
    listed = {c["session"] for c in out}
    for e in load_standalone(raw=True):
        if e.get("host") or e["session"] in listed or any(e["session"].startswith(x) for x in exclude if x):
            continue
        out.append(dict(e, state="dismissed" if e.get("dismissed") else "registered"))
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

MODE_TURN, MODE_JOB = "\u25b6", "\u25f7"  # a turn is running / waiting on its own job, CI build or lease


def role_mode(r: dict) -> str:
    """Why a role reads `working` in Herdr, because the half-moon alone cannot say: MODE_TURN while its
    session is mid-turn (an open question prompt excepted: that is the maintainer's to answer), MODE_JOB
    while it waits on a job or CI build running for it, else empty (asleep, idle or needing you)."""
    reasons = r["can_progress"]["reasons"]
    if r["session"] == "working" and "blocked-on-question" not in reasons:
        return MODE_TURN
    import ui_status
    if ui_status.working_wait(r) and "watcher-dead" not in reasons and r["session"] != "dead":
        return MODE_JOB
    return ""


def herdr_state(r: dict) -> str:
    """Herdr's agent state (idle | working | blocked). A session mid-turn is `working` even when the
    role also waits on the maintainer: the sidebar showed a busy lane as blocked (2026-10-12), and
    the needs-you signal stays in the row's posture and ask tokens. An open question prompt is the
    exception: it is the maintainer's to answer even while subagents keep the job working, so it reads
    `blocked` (unless the role waits on its own job or CI build). `blocked` (drawn as x in symbols
    mode) means the role needs the maintainer, or cannot be woken (watcher dead). `working` covers a
    turn and a wait on its own job; the `mode` token (role_mode) says which."""
    if role_mode(r):
        return "working"
    if r["needs_you"] or "watcher-dead" in r["can_progress"]["reasons"]:
        return "blocked"
    return "idle"


def role_tokens(r: dict) -> dict[str, str]:
    import ui_status
    nx = r["next"]
    return {
        "role": r["slug"],
        "mode": role_mode(r),
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
    # `host` only on a multi-host shoal, so a single-host sidebar is unchanged
    return dict(role_tokens(r), shoal=shoal_tag(s["id"]), host=(r.get("host", "") if s.get("multi_host") else ""))


def shoal_tokens(s: dict) -> dict[str, str]:
    m = s["mode"]
    c = s.get("conditions") or {}
    return {
        "shoal_mode": "OFFLINE" if m["offline"] else ("FREEZE" if m["freeze"] else "ok"),
        "shoal_needs": str(s["needs_you"]), "shoal_stalled": str(s["stalled"]),
        "shoal_asks": str(s["asks_open"]), "shoal_pages": str(s["pages_24h"]),
        "shoal_conditions": str(len(c.get("open", []))) if c else "",
        "shoal_tooling": "" if s["tooling"]["current"] else f"{s['tooling']['sha']}->{s['tooling']['head']}",
    }


def standalone_state(e: dict, rows: list[dict]) -> tuple[str, str]:
    """(turn word, herdr state) for one standalone session, from its harness's own probe: Claude
    from the agents listing, muse, codex and opencode from their backend status lines. For Muse and
    Codex `status:busy` is a running turn OR a client merely attached and waiting for the maintainer;
    the backend adds `active` or `quiet` by the session file's age, and `attached quiet` reads as
    idle (word `attached`), so a seat waiting for you is not drawn as working."""
    import ui_status
    harness = e.get("harness") or ""
    if harness == "claude":
        row = ui_status.claude_session(rows, e["session"], e.get("job", "")) or {}
        st = ui_status.claude_turn(row) if row else "unknown"
        if row and st != "dead" and ui_status.pending_question(row, e["session"]):
            return "QUESTION", "blocked"
        return st, {"working": "working", "blocked": "blocked"}.get(st, "idle")
    if harness in ("muse", "codex", "opencode"):
        try:
            from backends import get as get_backend
            s = get_backend(harness).status(e["session"], e.get("cwd") or os.path.expanduser("~"))
        except Exception:  # noqa: BLE001 - a failed probe reports idle, never stops the tick
            return "unknown", "idle"
        if "status:busy" in s and "attached quiet" in s:
            return "attached", "idle"
        if "status:busy" in s or "status:running" in s or "status:working" in s:
            return "working", "working"
        return ("idle", "idle") if "status:idle" in s else ("unknown", "idle")
    return "unknown", "idle"


def holds_agent(h: Herdr, pane_id: str) -> bool:
    """False when the pane's foreground is a bare shell. Herdr clears an agent report from such a pane
    within a fraction of a second, so reporting it only made the role flash into the agents list (a
    parked or blocked row jumped to the top and vanished again, once per tick, and its needs-you state
    was visible for that blink only). A card that went bare (parked when it was laid out, a cold
    restore) comes back with `shoal ui open --reapply`. A failed probe reports as before."""
    try:
        return not h.foreground_is_shell(pane_id)
    except HerdrError:
        return True


HANDOVER_EVERY = 15  # feeder ticks (20 s each)
REBALANCE_EVERY = 3  # feeder ticks: the roles tabs follow role changes and roles_per_tab within a minute


def _handover_pass(quiet: bool = True) -> None:
    try:
        import handover as _handover
        for n in _handover.tick():
            print(f"{datetime.datetime.now():%H:%M:%S} handover: {n}", flush=True)
    except Exception as exc:  # noqa: BLE001 - the feeder never stops on a handover pass
        print(f"handover pass failed: {str(exc)[:160]}", flush=True)


def feed_once(h: Herdr, interval: float, tick: int, seq: int, quiet: bool = True) -> int:
    import ui_status
    import subscription_usage
    settings = ui_status.ui_config()
    subscription_usage.refresh_async(settings.get("usage_providers", []), bridge=settings.get("usage_bridge"))
    ui_status.BACKGROUND_CONDITIONS = True  # a tick never waits on the provider probes behind conditions
    doc = ui_status.status(None, with_conditions=True,
                           refresh_conditions=False)  # conditions come from their own 5-minute cache
    ui_status.write_cache(doc)
    ttl = int(max(interval * 3, FEED_MIN_TTL_S) * 1000)
    release_retired(h, close=False)  # relabel only: the feeder never closes anything
    wss = {w.get("label"): w for w in h.workspaces()}
    panes = {p.get("label"): p for p in h.panes() if p.get("label")}
    pushed = 0
    bare: list[str] = []
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
            if holds_agent(h, p["pane_id"]):
                h.call("pane.report_agent", {"pane_id": p["pane_id"], "source": SOURCE, "agent": r["slug"],
                                             "state": herdr_state(r), "message": msg[:80], "seq": seq})
            else:
                bare.append(r["slug"])
            h.call("pane.report_metadata", {"pane_id": p["pane_id"], "source": SOURCE,
                                            "tokens": role_tokens_for(s, r), "ttl_ms": ttl, "seq": seq})
            pushed += 1
    # standalone sessions: their turn state (each harness's own probe, no scraping). Every harness
    # must be reported here: an unreported pane falls back to herdr's process detection, which names
    # it after the harness binary (three muse seats all reading "muse" in the agents pane, 2026-10-16)
    rows = ui_status.claude_rows()
    for e in load_standalone():
        p = panes.get(standalone_label(e))
        if p is None:
            continue
        if not holds_agent(h, p["pane_id"]):
            bare.append(e.get("label") or e.get("harness") or "standalone")
            continue
        st, hstate = remote_standalone_state(e) if e.get("host") else standalone_state(e, rows)
        h.call("pane.report_agent", {"pane_id": p["pane_id"], "source": SOURCE,
                                     "agent": e.get("label") or e.get("harness") or "standalone",
                                     "state": hstate, "message": f"standalone {st}", "seq": seq})
        h.call("pane.report_metadata", {"pane_id": p["pane_id"], "source": SOURCE, "ttl_ms": ttl, "seq": seq,
                                        "tokens": {"role": e.get("label") or "", "shoal": e.get("harness") or "",
                                                   "posture": st, "mode": MODE_TURN if hstate == "working" else ""}})
    if not quiet:
        print(f"{datetime.datetime.now():%H:%M:%S} fed {pushed} role panes; {ui_status.render_line(doc)}"
              + (f"; bare shell cards not reported (`shoal ui open --reapply`): {', '.join(bare)}" if bare else ""),
              flush=True)
    if tick % REBALANCE_EVERY == 0 and settings.get("auto_rebalance", True) is not False:
        for line in rebalance_all(h):
            print(f"{datetime.datetime.now():%H:%M:%S} rebalance: {line}", flush=True)
    if tick % HANDOVER_EVERY == 0:
        # standalone-session handover (handover.py): about every 5 minutes, in the background, so a delivery (which
        # can take seconds) never delays a tick
        import threading
        threading.Thread(target=_handover_pass, args=(quiet,), daemon=True).start()
    return pushed


_REBALANCED: dict[str, list[str]] = {}  # the last pass's lines per shoal (the feeder process)


def rebalance_all(h: Herdr) -> list[str]:
    """Lay every open shoal's roles out again where its tabs no longer match: a role added or removed, or a new
    `roles_per_tab` or `role_order` (auto_rebalance, maintainer 2026-10-26). The same `ensure_shoal` pass as
    `ui open`, so a card in use is never replaced (it is moved, or the change waits); only shoals that already
    have a workspace are touched, and lines that change nothing are dropped."""
    import ui_status
    from roles import ShoalSpec
    open_ws = {w.get("label") for w in h.workspaces()}
    out = []
    seen = {}
    for p in ui_status.discover_all("", None):
        try:
            spec = ShoalSpec.load(p)
            spec.path = p
            if shoal_tag(spec.sid) not in open_ws:
                continue
            lines = [f"{shoal_tag(spec.sid)}: {line}" for line in ensure_shoal(h, spec) if ": up to date (" not in line]
            seen[spec.sid] = lines
            if lines != _REBALANCED.get(spec.sid):  # a change that waits on a card in use is reported once
                out += lines
        except Exception as exc:  # noqa: BLE001 - one shoal never stops the pass
            out.append(f"{p}: {exc}")
    _REBALANCED.clear()
    _REBALANCED.update(seen)
    return out


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
    labelled_before = any(p.get("label") for p in h.panes())  # a fresh server holds nothing to compare with
    try:
        for line in release_retired(h, close=True, dry_run=a.dry_run):
            print(("[dry-run] " if a.dry_run else "") + line)
    except Exception as exc:  # noqa: BLE001
        print(f"ui open: releasing retired workspaces: {exc}")
    try:
        for line in release_hidden(h, a.dry_run, keep=a.ids):
            print(("[dry-run] " if a.dry_run else "") + line)
    except Exception as exc:  # noqa: BLE001
        print(f"ui open: closing hidden shoals' workspaces: {exc}")
    for p in ui_status.discover_all(a.ledger_root, a.ids or None):
        try:
            spec = ShoalSpec.load(p)
            spec.path = p
            for line in ensure_shoal(h, spec, a.dry_run, getattr(a, "reapply", False)):
                print(("[dry-run] " if a.dry_run else "") + line)
        except Exception as exc:  # noqa: BLE001
            print(f"ui open: {p}: {exc}")
    if not a.no_standalone:
        for line in migrate_standalone(h, a.dry_run):
            print(("[dry-run] " if a.dry_run else "") + line)
        entries = load_standalone(raw=True)
        changed, notes = reconcile_remote_standalone(entries)
        if record_claude_jobs(entries):
            changed = True
        for line in notes:
            print(("[dry-run] " if a.dry_run else "") + line)
        if changed and not a.dry_run:
            save_standalone(entries)
        show = {x.strip() for x in getattr(a, "show", []) or []}
        for line in sync_standalone(h, entries, show, labelled_before, a.dry_run, getattr(a, "reapply", False)):
            print(("[dry-run] " if a.dry_run else "") + line)
    return 0


def sync_standalone(h: Herdr, entries: list[dict], show: set[str], labelled_before: bool, dry_run: bool,
                    reapply: bool) -> list[str]:
    """Open each saved standalone session, except one the maintainer closed in the viewer. An entry is
    `opened` once a workspace has been made for it; later missing that workspace while Herdr still holds
    other labelled panes (so it is not a fresh server with nothing restored) means it was closed on
    purpose: it becomes `dismissed` and stays closed across restarts of the viewer. `--show <label>`
    (or `ui pick --save`) brings it back, `ui forget` drops it for good."""
    out, changed = [], False
    present = {q.get("label") for q in h.panes()}
    entries[:], dropped = unique_by_label(entries)
    for d in dropped:
        out.append(f"standalone {d.get('label') or d['session'][:12]}: dropped a saved duplicate label "
                   f"({d['harness']} {d['session'][:8]}); the later registration is kept")
        changed = True
    for e in entries:
        name = e.get("label") or e["session"][:12]
        shown = name in show or e["session"] in show
        if shown and e.pop("dismissed", None):
            changed = True
        if e.get("dismissed"):
            out.append(f"standalone {name}: closed in the viewer, left closed (`shoal ui open --show {name}` restores it)")
            continue
        if e.get("opened") and not shown and standalone_label(e) not in present and labelled_before:
            e["dismissed"] = True
            changed = True
            out.append(f"standalone {name}: closed in the viewer, left closed (`shoal ui open --show {name}` restores it)")
            continue
        out.append(open_standalone(h, e, dry_run, reapply=reapply))
        if not e.get("opened"):
            e["opened"] = True
            changed = True
    if changed and not dry_run:
        save_standalone(entries)
    return out


def _choose(items: list[str], prompt: str = "number") -> int | None:
    for i, it in enumerate(items, 1):
        print(f"  {i:>2}  {it}")
    raw = ui_term.read_line(f"{prompt} (esc or empty enter cancels): ")
    if not raw or not raw.isdigit() or not 1 <= int(raw) <= len(items):
        return None
    return int(raw) - 1


def cmd_pick(a) -> int:
    """Keep picker failures visible in a popup; intentional cancellation closes it."""
    try:
        rc = _pick(a)
    except (HerdrError, OSError) as exc:
        print(f"pick: {exc}")
        rc = 1
    if rc and getattr(a, "pause", False):
        ui_term.pause()
    return rc


def _pick(a) -> int:
    """Pick a non-shoal session and open its card without attaching the harness."""
    if a.harness and a.session_id:
        e = {"harness": a.harness, "session": a.session_id, "label": a.label or a.session_id[:12]}
        if getattr(a, "cwd", ""):
            e["cwd"] = os.path.abspath(os.path.expanduser(a.cwd))
    else:
        cands = candidates_standalone(role_sessions())
        if not cands:
            print("no standalone sessions found (Claude, opencode, Codex, Muse). "
                  "For a known session, use `shoal ui pick --harness HARNESS --id ID --save`.")
            return 1
        print("Standalone sessions (not shoal roles):")
        i = _choose([f"{c['harness']:<8} {c['label']:<40} {c['state'] or '?':<8} {c['session'][:16]}  {c.get('cwd') or ''}"
                     for c in cands])
        if i is None:
            return 0
        e = {k: cands[i][k] for k in ("harness", "session", "label", "cwd", "job", "model", "effort")
             if cands[i].get(k) is not None}
        if a.label:
            e["label"] = a.label
    for k in ("model", "effort"):  # the seat's pair, saved so its card resumes with them (ui_view.viewer)
        if getattr(a, k, ""):
            e[k] = getattr(a, k)
    save = a.save
    if not a.harness and not save:
        save = ui_term.confirm("save it so `ui open` restores it? [y/N] ")
    if save:
        e["opened"] = True  # it opens right below: closing it in the viewer later is a deliberate dismissal
        label = e.get("label") or e["session"][:12]
        for x in load_standalone(raw=True):
            if x["session"] != e["session"] and (x.get("label") or x["session"][:12]) == label:
                print(f"replacing the saved {x['harness']} session {x['session'][:8]} that had the same label "
                      f"({label}): one label, one card")
            if x["session"] == e["session"]:  # a re-pick keeps everything saved on it (doc, handover state)
                e = {**{k: v for k, v in x.items() if k != "dismissed"}, **e}
            elif (x.get("label") or x["session"][:12]) == label:
                # a new session in the same seat keeps the seat's settings; model and effort only within one
                # harness (a seat moved from Codex to Claude must not carry a Codex model)
                for k in ("doc", "handover", "handover_k") + (("model", "effort") if x["harness"] == e["harness"] else ()):
                    if k not in e and x.get(k):
                        e[k] = x[k]
        # when the seat was saved: a `/bg` successor of a session never yet seen by its card must start after it
        # (ui_view.bg_since); a re-pick keeps the first save's time
        e.setdefault("saved_utc", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
        entries = [x for x in load_standalone(raw=True) if x["session"] != e["session"]
                   and (x.get("label") or x["session"][:12]) != label] + [e]
        save_standalone(entries)
        print(f"saved to {standalone_path()}")
    h = Herdr(a.session)
    print(open_standalone(h, e, focus=True))
    return 0


def forget_interactive() -> int:
    """Pick a saved standalone session, forget it (ui open stops reopening it), and close its
    workspace when nothing in it is in use. Closing the workspace by hand only lasted until the next
    launch, which reopens every saved session (the maintainer, 2026-10-13)."""
    entries = load_standalone(raw=True)
    if not entries:
        print("no saved standalone sessions")
        return 0
    i = _choose([f"{e.get('label') or e['session'][:12]}  ({e['harness']})" for e in entries], "forget which")
    if i is None:
        return _cancelled()
    e = entries[i]
    save_standalone([x for x in entries if x is not e])
    print(f"forgot {e.get('label') or e['session']}")
    try:
        h = Herdr()
        w = find_ws(h, standalone_ws_label(e))
        if w is not None:
            panes = h.panes(w["workspace_id"])
            if all(disposable(h, p["pane_id"]) for p in panes):
                h.call("workspace.close", {"workspace_id": w["workspace_id"]})
                print(f"closed workspace {w.get('label')!r}")
            else:
                print(f"workspace {w.get('label')!r} is in use: close it by hand (it will not come back)")
    except Exception as exc:  # noqa: BLE001
        print(f"workspace not closed: {exc}")
    return 0


def cmd_forget(a) -> int:
    entries = load_standalone(raw=True)
    keep = [x for x in entries if x["session"] != a.session_id and x.get("label") != a.session_id]
    save_standalone(keep)
    print(f"forgot {len(entries) - len(keep)} saved standalone session(s); open panes are left as they are")
    return 0


# ---- overview panes --------------------------------------------------------------------

def cmd_dash(a) -> int:
    import ui_status
    import ui_view
    watch = ui_term.CodeWatch()
    while True:
        watch.check()
        cols, rows = ui_term.screen_size()
        try:
            doc = ui_status.cached_status(a.ids or None)
            body = ui_status.render_text(doc, width=cols)
        except Exception as exc:  # noqa: BLE001
            body = f"status unavailable: {exc}"
        # Fit the pane: a taller frame scrolls the usage table into the scrollback on every tick.
        sys.stdout.write(ui_term.fit_frame(body, f"\x1b[2m{datetime.datetime.now():%H:%M:%S}  [r] refresh  "
                                           "[m] menu  [q] shell   (read-only; conditions cached 5 min)\x1b[0m",
                                           cols, rows))
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
    watch = ui_term.CodeWatch()
    while True:
        watch.check()
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


def _ask(prompt: str, default: str = "") -> str:
    """A text answer, pre-filled with `default` to edit in place; Escape cancels (returns "")."""
    return ui_term.read_line(prompt, default) or ""


def _cancelled() -> int:
    print("cancelled; nothing written")
    return 1


def next_morning(hour: int = 9) -> str:
    """The next local `hour`:00 with its UTC offset (e.g. 2026-10-13T09:00-07:00): the usual offline end."""
    import datetime as _dt
    now = _dt.datetime.now().astimezone()
    t = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    if t <= now:
        t += _dt.timedelta(days=1)
    off = t.strftime("%z")
    return t.strftime("%Y-%m-%dT%H:%M") + f"{off[:3]}:{off[3:]}"


# the pager says how to close it (the pilot got stuck at `(END)`); Ctrl+C also exits (-K)


def act(verb: str, sid: str = "", slug: str = "") -> int:
    fs, fr = focused_role()
    sid = sid or pick_shoal(fs)
    if not sid:
        return 1
    if verb == "status":
        r = subprocess.run(shoal_argv("ui", "status", "--shoal", sid, "--conditions"), capture_output=True, text=True,
                           env=dict(os.environ, SHOAL_UI_COLOR="1"))
        ui_term.page((r.stdout + r.stderr).rstrip())
        return 0
    if verb == "next":
        slug = slug or pick_role(sid, fr, ask=False)
        if slug:
            r = subprocess.run(shoal_argv("next", "--shoal", sid, "--slug", slug, "--peek", "--no-sync"),
                               capture_output=True, text=True)
            ui_term.page((r.stdout + r.stderr).rstrip())
        return 0
    if verb == "asks":
        import ui_status
        doc = ui_status.status([sid], opencode_probe=False)
        rows = [r for r in doc["shoals"][0].get("roles", []) if r["asks"] or {"blocked-on-question", "blocked-says-waiting"} & set(r["can_progress"]["reasons"])]
        if not rows:
            print("no open asks and no role waiting on a question")
            ui_term.pause()
            return 0
        here = doc["shoals"][0].get("this_host", "")
        remote = {r["slug"]: r.get("host", "") for r in rows if r.get("host") and r.get("host") != here}
        for r in rows:
            if r["slug"] in remote:
                print(f"\n{r['slug']}  (runs on {remote[r['slug']]}: answer by mail with a wake here, or attach from that "
                      f"host's viewer)")
            else:
                print(f"\n{r['slug']}" + ("  (blocked on a question in its session: attach to answer it)"
                                          if "blocked-on-question" in r["can_progress"]["reasons"] else
                                          "  (says it waits on you, with no prompt: answer by mail with a wake, or attach and type)"
                                          if "blocked-says-waiting" in r["can_progress"]["reasons"] else ""))
            for i, x in enumerate(r["asks"], 1):
                print(f"  ask {i}: {x}")
        pick = _ask("\nanswer which role? (slug; enter to close): ")
        if not pick:
            return 0
        if pick in remote:
            how = "m" if _ask(f"{pick} runs on {remote[pick]}: [m] send the answer as mail with a wake: ").lower() == "m" else ""
        else:
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
        if verb == "offline" and state == "on":
            until = ui_term.read_line("back at (edit, or clear it for no time): ", next_morning())
            if until is None:
                return _cancelled()
            if until:
                args += ["--until", until]
        note = ui_term.read_line("note (optional): ")
        if note is None:
            return _cancelled()
        if note:
            args += ["--note", note]
        if verb == "offline":
            # no preview or confirm (maintainer, 2026-10-13): Escape at any prompt above cancels
            print("\n$ shoal " + shlex.join(args))
            r = run_shoal(args)
            log_action(args, r.returncode)
            return r.returncode
        cost = ("wakes the roles waiting on you, once, so they pivot (tokens)" if verb == "offline"
                else "toggling a freeze wakes every role once (tokens)")
        if verb == "freeze" and _ask("record it without waking every role (--no-wake)? [Y/n] ").lower() != "n":
            args.append("--no-wake")
            cost = ""
        return confirm_run(args, cost)
    if verb == "grants":
        r = run_shoal(["grants", "--shoal", sid, "--json"], capture=True)
        try:
            doc = json.loads(r.stdout)
        except ValueError:
            print((r.stdout + r.stderr).strip() or "grants: no output")
            return 1
        rows = doc.get("grants") or []
        if not doc.get("grants_enabled"):
            print(f"signed grants are off for {sid} (enable: shoal upgrade --enable grants, then sign seeds)")
        for g in rows:
            print(f"  {g['id']}  {g['level']:<5} {g['role']:<24} {g['state']:<10} {g['scope']}")
        print(f"{len(rows)} grant(s)" + ("" if rows else " (sign one with: shoal grant --shoal " + sid + " --scope ...)"))
        act = _choose(["inspect one", "grant (signs here, in your terminal)", "revoke one", "done"])
        if act is None or act == 3:
            return 0
        if act == 1:
            who = pick_role(sid, fr, allow_all=True)
            if not who:
                return _cancelled()
            scope = _ask("scope (what it authorizes): ")
            if not scope:
                return _cancelled()
            kind = _ask("kind tag (optional): ")
            until = _ask("until (ISO UTC, empty = until revoked): ")
            args = ["grant", "--shoal", sid, "--scope", scope]
            if who != "*":
                args += ["--role", who]
            if kind:
                args += ["--kind", kind]
            if until:
                args += ["--until", until]
            return confirm_run(args, "signing asks for your key (touch or passphrase) in this terminal")
        if not rows:
            print("nothing to inspect or revoke yet")
            return 0
        i = _choose([f"{g['id']} [{g['role']}] {g['scope']}" for g in rows])
        if i is None:
            return _cancelled()
        if act == 0:
            run_shoal(["grants", "--shoal", sid, "--inspect", rows[i]["id"]])
            ui_term.pause()
            return 0
        return confirm_run(["revoke", "--shoal", sid, "--id", rows[i]["id"]],
                           "roles holding only this grant lose the authorization at their next boundary")
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
    ("grants", "standing grants: list, inspect, grant, revoke (signed)"),
    ("freeze", "freeze mode on/off"),
    ("conditions", "health conditions now (refresh)"),
    ("metrics", "failure-mode metrics and report files"),
    ("pick", "open a standalone (non-shoal) session"),
    ("forget", "stop reopening a saved standalone session (and close its workspace)"),
    ("legend", "what the state icons and glyphs mean"),
    ("keys", "paste, copy and navigation keys (the image paste key is here)"),
    ("open", "re-apply the shoal layouts (fills gaps only)"),
    ("upgrade", "preview a shoal upgrade (applying stays a CLI step)"),
]


def legend_text(color: bool = True) -> str:
    """What the viewer's icons and glyphs mean. Herdr draws the state icon from the state shoal reports; the
    glyph beside the name is shoal's `mode` token, which says WHY a role reads working."""
    b = ("\x1b[1m", "\x1b[0m") if color else ("", "")
    return "\n".join([
        f"{b[0]}icons in the viewer{b[1]}",
        "",
        "  state icon (Herdr draws it from the state shoal reports; its colour is part of the meaning)",
        "    half-moon, yellow   working: a turn is running, or the role waits on its own job, CI build or lease",
        "    hollow circle, green  idle: ready for input (a role holding a blocker, or asleep, is idle)",
        "    x, red              blocked: the role needs you (a question, or it says it waits) or its watcher is dead",
        "    dot, grey           unknown: nothing reported (a card that is a bare shell, a space with no agent)",
        "    A space (workspace) shows the summary of its agents.",
        "",
        f"  glyph beside the name (why it reads working)",
        f"    {MODE_TURN}  a turn is running            {MODE_JOB}  waiting on a job, CI build or lease running for it",
        "    (none) asleep with a watcher, idle, or needing you",
        "",
        "  second row, the posture word: wait:<kind> orange, blocked:<kind> and QUESTION red, active green, idle orange",
        "  Standalone seats: `attached` = a client is open but nothing moved in the session for two minutes (idle);",
        "  `working` = the session's own activity is recent or a turn is running.",
    ])


def keys_help() -> str:
    """The keys this viewer relies on. Herdr's own keybind menu does not list `remote_image_paste`, so
    the palette states it (maintainer, 2026-10-14: the image paste key was nowhere to be found)."""
    import ui_status
    img = str(ui_status.ui_config().get("image_paste_key") or "alt+v")
    return "\n".join([
        "\x1b[1mkeys in the shoal viewer\x1b[0m",
        "",
        "  paste text            Ctrl+V   (the shoal kitty window maps it to its clipboard paste)",
        "  paste an IMAGE        Ctrl+Shift+V   shoal-paste-image: copies the clipboard image to this host and",
        "                        types its path into the focused session (diagnose on the laptop with",
        "                        `shoal-paste-image --check`)",
        f"                        {img.title()}   Herdr's own remote image paste (a raw key inside the client; if it",
        "                        does nothing, nothing here can say why: use Ctrl+Shift+V)",
        "  copy                  select with the mouse (OSC 52 copies it to the laptop clipboard in kitty)",
        "  action palette        Ctrl+B then Alt+S   (Ctrl+B then Shift+S without Alt)",
        "  zoom a pane           Ctrl+B then Z       (the Z after a tab name marks a zoomed pane)",
        "  detach an attached card   Ctrl+Z (claude) or Ctrl+C (opencode); the session keeps running.",
        "                        Only for a card that attached (`claude attach`): in a terminal that runs",
        "                        `claude --resume` itself, Ctrl+Z SUSPENDS the whole session.",
        "",
        "  image paste says nothing when the clipboard holds no image: check on the laptop with",
        "  `wl-paste --list-types` (Wayland) or `xclip -selection clipboard -t TARGETS -o` (X11);",
        "  an image shows as image/png.",
    ])


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
        return subprocess.run(shoal_argv("ui", "pick", "--pause")).returncode
    if k == "keys":
        print(keys_help())
        ui_term.pause()
        return 0
    if k == "legend":
        print(legend_text())
        ui_term.pause()
        return 0
    if k == "forget":
        rc = forget_interactive()
        ui_term.pause()
        return rc
    if k == "open":
        rc = subprocess.run(shoal_argv("ui", "open")).returncode
        ui_term.pause()
        return rc
    rc = act(k)
    if k not in ("status", "next", "asks", "upgrade", "conditions"):
        ui_term.pause()
    return rc


def cmd_legend(a) -> int:
    print(legend_text(sys.stdout.isatty()))
    return 0


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
    import ui_status
    img = str(ui_status.ui_config().get("image_paste_key") or "alt+v")
    # Ctrl+V pastes text (the laptop's kitty window maps it to its clipboard paste), so Herdr's
    # remote image paste moves off Ctrl+V to this key (maintainer, 2026-10-13)
    out = [f'[keys]\nremote_image_paste = "{img}"\n']
    for k, action, desc in keys:
        out.append(f'[[keys.command]]\nkey = "{k}"\ntype = "plugin_action"\ncommand = "{PLUGIN_ID}.{action}"\n'
                   f'description = "{desc}"\n')
    return "\n".join(out)


def sidebar_block() -> str:
    """Client side (the viewing machine draws these): agent rows by role name, with the shoal's
    short tag dim beside it (the agents list is already grouped by workspace, one per shoal), and
    the spaces rows with the per-shoal tokens. The tab-bar status line is NOT here: a remote client
    draws the server's `tab_bar_right` (measured in the laptop pilot), so it is in server_block."""
    import ui_status
    smw = str(ui_status.ui_config().get("sidebar_max_width") or "").strip()
    width = ""
    if smw.endswith("%") and smw[:-1].isdigit():
        # a share of the window: the laptop launcher resolves it from the window width at each launch
        width = f"# shoal-sidebar-max: {smw}\nsidebar_max_width = 60\n"
    elif smw.isdigit():
        width = f"sidebar_max_width = {int(smw)}\n"
    sort = str(ui_status.ui_config().get("agent_sort") or "priority").strip()
    if sort in ("priority", "spaces"):
        # "priority" is Herdr's attention queue: blocked (shoal: a question or another need for the
        # maintainer) first, then the rest by state; "spaces" groups agents under their shoal
        width += f"agent_panel_sort = {json.dumps(sort)}\n"
    pb = str(ui_status.ui_config().get("pane_borders") or "").strip()
    if pb in ("auto", "off", "on"):
        # "off" drops Herdr's plain-text pane titles, so each card's own colored first line is the top
        width += f"pane_borders = {json.dumps(pb)}\n"
    return """[ui]
status_indicators = "symbols"
""" + width + """
[ui.sidebar.agents]
row_gap = 0
rows = [
  ["state_icon", "machine", { token = "agent", bold = true }, { token = "$mode", rules = [{ equals = "\u25b6", fg = "#a6e3a1" }, { equals = "\u25f7", fg = "#fab387" }] }, { token = "$shoal", dim = true }, { token = "$host", dim = true }],
  [{ token = "$posture", rules = [{ equals = "DEAD", fg = "#f38ba8", bold = true }, { equals = "QUESTION", fg = "#f38ba8", bold = true }, { equals = "BLOCKED", fg = "#f38ba8" }, { starts_with = "blocked", fg = "#f38ba8" }, { equals = "NOWATCH", fg = "#f38ba8" }, { contains = "?", fg = "#fab387", bold = true }, { starts_with = "wait", fg = "#fab387" }, { equals = "idle", fg = "#fab387" }, { equals = "active", fg = "#a6e3a1" }] }, "$item", "$age"],
  [{ token = "$asks", fg = "#fab387", bold = true }, { token = "$next", rules = [{ contains = "!", fg = "#f9e2af" }] }, "$wait", { token = "$why", dim = true }],
]

[ui.sidebar.spaces]
row_gap = 0
rows = [
  ["state_icon", "workspace"],
  [{ token = "$shoal_mode", rules = [{ equals = "ok", hide = true }, { equals = "OFFLINE", fg = "#89b4fa", bold = true }, { equals = "FREEZE", fg = "#f38ba8", bold = true }] }, { token = "$shoal_needs", rules = [{ equals = "0", hide = true }, { gt = 0, fg = "#fab387", bold = true }] }, { token = "$shoal_tooling", dim = true }],
  ["branch", "git_status"],
]
"""


def server_sidebar() -> str:
    """The sidebar's maximum width in the SERVER block too: a `herdr --remote` client kept Herdr's
    default cap with the setting only in its own config (2026-10-13), as it draws the server's
    tab_bar_right. The server cannot know the client window, so a share like 50% becomes a generous
    column cap here; the client block keeps the per-window value."""
    import ui_status
    smw = str(ui_status.ui_config().get("sidebar_max_width") or "").strip()
    if smw.endswith("%") and smw[:-1].isdigit():
        return f"sidebar_max_width = {max(36, 4 * int(smw[:-1]))}\n"
    return f"sidebar_max_width = {int(smw)}\n" if smw.isdigit() else ""


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
{server_sidebar()}tab_bar_right = [
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


def local_config_path() -> str:
    """The local view's own Herdr config (a host whose terminal client also serves the view): the client and
    server blocks both define [ui], so they cannot share one managed block; the local server and its client read
    this file through HERDR_CONFIG_PATH, and `config.toml` stays the remote client's file."""
    return os.path.expanduser(config.env("LOCAL_HERDR_CONF") or os.path.join(config_dir(), "shoal-local.toml"))


def _tables(text: str) -> tuple[list[str], list[list]]:
    """(top-level lines, [[header, body lines]]) in file order."""
    import re
    top, out, cur = [], [], None
    for line in text.splitlines():
        if re.match(r"^\[\[?([A-Za-z0-9_.]+)\]\]?\s*$", line):
            cur = [line, []]
            out.append(cur)
        elif cur is None:
            top.append(line)
        else:
            cur[1].append(line)
    return top, out


def _key(line: str) -> str | None:
    import re
    m = re.match(r"^([a-z_]+)\s*=", line)
    return m.group(1) if m else None


def local_block(mine: str = "") -> str:
    """The server and client blocks merged into one: every server table, then the client's. [ui] is the one table
    both define: the client's keys win (its sidebar width marker is resolved at launch from the window) and the
    server's tab_bar_right stays. Top-level keys the user's own settings (`mine`) already set are left out."""
    s_top, s_tabs = _tables(server_block())
    c_top, c_tabs = _tables(client_block())
    merged = [[h, list(b)] for h, b in s_tabs]
    for h, body in c_tabs:
        same = [m for m in merged if m[0] == h and not h.startswith("[[")]
        if not same:
            merged.append([h, list(body)])
            continue
        mb = same[0][1]
        ckeys = {_key(x) for x in body if _key(x)}
        mb[:] = [x for x in mb if _key(x) not in ckeys]
        mb.extend(["", *body])
    my_top = {_key(x) for x in mine.split("\n[", 1)[0].splitlines() if _key(x)}
    top = "\n".join(x for x in s_top + c_top if _key(x) not in my_top).strip()
    return (top + "\n\n" if top else "") + "\n\n".join(h + "\n" + "\n".join(b).strip("\n") + "\n" for h, b in merged)


def build_local_config(base: str = "") -> str:
    """The local view's whole file: the user's own `config.toml` settings (any shoal managed block removed) plus one
    merged shoal block. Validated as TOML."""
    import re
    base = base or os.path.join(config_dir(), "config.toml")
    cur = open(base).read() if os.path.exists(base) else ""
    if BEGIN in cur and END in cur:
        pre, rest = cur.split(BEGIN, 1)
        cur = pre + rest.split(END, 1)[1]
    out = (cur.rstrip() + "\n\n" if cur.strip() else "") + f"{BEGIN}\n{local_block(cur).rstrip()}\n{END}\n"
    out = re.sub(r"\n{3,}", "\n\n", out)
    try:
        import tomllib as _toml  # type: ignore[import-not-found]
    except ImportError:  # Python 3.10
        try:
            import tomli as _toml  # type: ignore[import-not-found,no-redef]
        except ImportError:
            _toml = None
    if _toml is not None:
        _toml.loads(out)  # a merge that does not parse is never written
    return out


def cmd_config(a) -> int:
    if getattr(a, "local", False):
        path = a.path or local_config_path()
        try:
            new = build_local_config()
        except Exception as exc:  # noqa: BLE001 - a merge that does not parse is reported, never written
            print(f"herdr-config: the local file does not build: {exc}")
            return 1
        if not a.write or a.dry_run:
            print(new)
            return 0
        if new != (open(path).read() if os.path.exists(path) else None):
            os.makedirs(os.path.dirname(path), exist_ok=True)
            open(path + ".tmp", "w").write(new)
            os.replace(path + ".tmp", path)
            print(f"herdr-config: wrote {path}")
        else:
            print(f"herdr-config: {path} is current")
        return 0
    block = client_block() if a.client else server_block()
    if not a.write:
        print(block)
        return 0
    path = a.path or config_path()
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


def unit_text(kind: str, local_conf: str = "") -> str:
    """A user unit for the Herdr server or the feeder. `local_conf`: the local view's own config file, carried as
    HERDR_CONFIG_PATH so the server and the feeder read it (herdr-install --local)."""
    herdr = config.env("HERDR_BIN") or os.path.expanduser("~/.local/bin/herdr")
    parts = [os.path.expanduser("~/.local/bin")] + (config.tool_path() or "/usr/local/bin:/usr/bin:/bin").split(":")
    path = ":".join(dict.fromkeys(x for x in parts if x))  # tool_path may already hold ~/.local/bin
    conf = f"Environment=HERDR_CONFIG_PATH={local_conf}\n" if local_conf else ""
    if kind == "server":
        return f"""[Unit]
Description=herdr server for the shoal UI (session {session_name()}{', local view' if local_conf else ''})
[Service]
Type=simple
Environment=HERDR_SESSION={session_name()}
{conf}Environment=PATH={path}
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
{conf}Environment=PATH={path}
ExecStart={shlex.join(shoal_argv("ui", "feed"))}
Restart=always
RestartSec=15
MemoryHigh=640M
[Install]
WantedBy=default.target
"""


def install_local(a) -> int:
    """`herdr-install --local [--check] [--dry-run]`: the local view on a host whose terminal client also serves it
    (a laptop). Its own config file (local_config_path, through HERDR_CONFIG_PATH), the server and feeder units
    carrying it, the plugin link, then `ui open`. Idempotent; a unit or config change restarts what reads it.
    --check reports only, against the file the local server really reads. Nothing here wakes a role."""
    conf = local_config_path()
    os.environ["HERDR_CONFIG_PATH"] = conf
    herdr = config.env("HERDR_BIN") or os.path.expanduser("~/.local/bin/herdr")
    env = dict(os.environ, HERDR_SESSION=session_name(), HERDR_CONFIG_PATH=conf)
    if getattr(a, "check", False):
        bad = 0
        for name, ok, detail in doctor_checks():
            print(f"{'ok  ' if ok else 'FAIL'} {name}: {detail}")
            bad += not ok
        return 1 if bad else 0
    if not os.path.exists(herdr):
        print(f"herdr-install: no herdr at {herdr}: `shoal ui herdr-get` installs the pinned release")
        return 1
    if from_worktree() and not a.dry_run:
        print(f"herdr-install: refusing to provision from a git worktree ({REPO}): the units and the status line "
              f"would point into it. Run it from the checkout the live install uses (`--dry-run` and `--check` are fine here)")
        return 2
    new = build_local_config()
    reload_ = new != (open(conf).read() if os.path.exists(conf) else None)
    unitdir = os.path.expanduser("~/.config/systemd/user")
    units = {"shoal-herdr.service": unit_text("server", conf), "shoal-ui-feed.service": unit_text("feed", conf)}
    changed = [n for n, t in units.items() if t != (open(os.path.join(unitdir, n)).read()
                                                    if os.path.exists(os.path.join(unitdir, n)) else None)]
    if a.dry_run:
        print(f"[dry-run] local config {conf}: {'write' if reload_ else 'current'}")
        for n in units:
            print(f"[dry-run] unit {n}: {'write' if n in changed else 'current'}")
        print("[dry-run] plugin link, enable the server and the feeder, ui open")
        return 0
    if reload_:
        os.makedirs(os.path.dirname(conf), exist_ok=True)
        open(conf + ".tmp", "w").write(new)
        os.replace(conf + ".tmp", conf)
        print(f"local config: wrote {conf}")
    chk = subprocess.run([herdr, "config", "check"], capture_output=True, text=True, env=env)
    said = (chk.stdout + chk.stderr).strip()
    if "config: ok" not in (said.splitlines() or [""])[0]:
        print(f"herdr-install: herdr rejects {conf}: {said[:300]}")
        return 1
    os.makedirs(unitdir, exist_ok=True)
    for n in changed:
        open(os.path.join(unitdir, n), "w").write(units[n])
        print(f"unit {n}: wrote")
    sh = lambda *c: subprocess.run(list(c), capture_output=True, text=True, env=env)  # noqa: E731
    if changed:
        sh("systemctl", "--user", "daemon-reload")
    if PLUGIN_ID not in sh(herdr, "plugin", "list").stdout:
        sh(herdr, "plugin", "link", PLUGIN_DIR)
    sh("systemctl", "--user", "enable", "--now", "shoal-herdr.service")
    if "shoal-herdr.service" in changed:
        sh("systemctl", "--user", "restart", "shoal-herdr.service")
    for _ in range(10):
        if Herdr(session_name()).alive():
            break
        time.sleep(1)
    if reload_:
        sh(herdr, "server", "reload-config")
    sh("systemctl", "--user", "enable", "--now", "shoal-ui-feed.service")
    if "shoal-ui-feed.service" in changed:
        sh("systemctl", "--user", "restart", "shoal-ui-feed.service")
    print("local view: server and feeder up")
    return cmd_open(argparse_ns(session="", ids=list(getattr(a, "ids", []) or []), ledger_root="", dry_run=False,
                                no_standalone=False))


def cmd_install(a) -> int:
    """Server side, idempotent: managed config block, plugin link, the herdr server and feeder
    user units. No session is touched; nothing here can wake a role."""
    if getattr(a, "local", False):
        return install_local(a)
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


def _claude_hooks_text(path: str) -> str:
    """The `hooks` section of a Claude settings file as text: Herdr's integration is a hook, so a permission
    rule or a plugin name that merely contains "herdr" is not one. A file that does not parse is read whole."""
    if not os.path.exists(path):
        return ""
    raw = open(path).read()
    try:
        return json.dumps((json.loads(raw) or {}).get("hooks") or {})
    except (ValueError, AttributeError):
        return raw


def plugin_link_state(listing: str) -> tuple[bool, str]:
    """(ok, detail) from `herdr plugin list`: ok when the plugin is listed. The detail is the path it is really
    linked from, and says so when that is not this checkout's plugin (a link to a stale or other checkout; measured
    2026-10-21: a check run from a worktree printed the worktree path while the link pointed at the live checkout)."""
    import re
    m = re.search(rf"^- {re.escape(PLUGIN_ID)}\b.*\[(?:local:)?([^\]]+)\]", listing, re.M)
    if PLUGIN_ID not in listing:
        return False, f"not linked (this code's plugin: {PLUGIN_DIR})"
    linked = os.path.expanduser(m.group(1)) if m else ""
    if linked and os.path.realpath(linked) != os.path.realpath(PLUGIN_DIR):
        return True, f"{linked} (this code is {PLUGIN_DIR}: run from the checkout the units use)"
    return True, linked or PLUGIN_DIR


def from_worktree() -> bool:
    """True when this code runs from a git worktree (its `.git` is a file), not a main checkout: provisioning
    from there writes units and a status line that point into the worktree."""
    return os.path.isfile(os.path.join(REPO, ".git"))


def doctor_checks(session: str = "") -> list[tuple[str, bool, str]]:
    out = []
    herdr = config.env("HERDR_BIN") or os.path.expanduser("~/.local/bin/herdr")
    out.append(("herdr installed", os.path.exists(herdr), herdr))
    import herdr_pin
    have = herdr_pin.installed(os.path.dirname(os.path.dirname(herdr))) if os.path.exists(herdr) else ""
    out.append(("herdr at the pin", have == herdr_pin.PIN,
                f"{have or 'none'} (pin {herdr_pin.PIN}; `shoal ui herdr-get` installs it)"))
    cfg = config_path()
    txt = open(cfg).read() if os.path.exists(cfg) else ""
    import re
    off = bool(re.search(r"^\s*resume_agents_on_restore\s*=\s*false", txt, re.M))
    out.append(("agent resume on restore is off", off, cfg))
    integ = []
    try:
        claude_settings = os.path.expanduser("~/.claude/settings.json")
        if "herdr" in _claude_hooks_text(claude_settings):
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
            out.append(("plugin linked",) + plugin_link_state(r.stdout))
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
    # the Herdr app rows inform here and decide `ui app-install --check`; the 0.9.3 view does not need them
    try:
        import ui_app
        rows = ui_app.doctor_checks()
    except Exception as exc:  # noqa: BLE001 - the app rows never hang or crash the 0.9.3 doctor
        rows = []
        print(f"warn app: checks unavailable ({exc})")
    for name, ok, detail in rows:
        print(f"{'ok  ' if ok else 'warn'} app: {name}: {detail}")
    return 1 if bad else 0
