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
import json
import os
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
APP_PIN = "0.1.12"
CORE_PIN = "0.1.17"
DEAD_RELEASES_URL = "http://127.0.0.1:9"
APP_UNIT = "shoal-herdr-app"
FEED_UNIT = "shoal-graph-feed.service"
MODS = ("lattice-view", "shoal-home")
MOD_DIR = os.path.join(REPO, "plugins", "shoal-herdr-app", "mods")
REPORT_APP = "shoal"
# the plan's own label on each terminal it starts, so a later reconcile finds the pane again
PANE_LABEL = "shoal.pane"
FEED_FRESH_S = 60


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
QUESTION_SUBS = ("user-ask", "user-prompt", "user-design", "user-feedback")


def card_report(r: dict, tag: str) -> dict:
    """{state, kind, title, msg} for one role's status row. The state follows the 0.9.3 sidebar rules
    (`ui_herdr.herdr_state`): working for a turn or a job wait, blocked when the role needs you or cannot be
    woken, else idle. A blocked role says why in `kind`, which the app shows: auth for `user-auth`, question for
    an open prompt, an ask or a design/feedback wait, permission for a pending grant."""
    import ui_herdr
    import ui_status
    state = ui_herdr.herdr_state(r)
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
        elif r.get("asks") or sub in QUESTION_SUBS:
            kind = "question"
    bits = [ui_status.posture_label(r)]
    w = r.get("wait") or {}
    if ui_herdr.role_mode(r) == ui_herdr.MODE_JOB:
        bits = [f"{ui_herdr.MODE_JOB} {w.get('sub') or r.get('sub')}"]
    if w.get("target") and r.get("posture") == "wait":
        bits.append(f"on {str(w['target'])[:60]}")
    if r.get("active"):
        bits.append(str(r["active"])[:60])
    why = [ui_status.ATTENTION.get(x, x) for x in reasons if x not in ("parked", "dormant")]
    if why and state == "blocked":
        bits.append("(" + ", ".join(why)[:80] + ")")
    return {"state": state, "kind": kind, "title": f"{tag}/{r['slug']}", "msg": " ".join(bits)[:200]}


def report_text(r: dict, tag: str) -> str:
    rep = card_report(r, tag)
    return osc7501(rep["state"], rep["kind"], rep["title"], rep["msg"])


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
    env = ui_herdr.pane_env(spec)
    tag = ui_herdr.shoal_tag(spec.sid)
    tabs = [{"name": "overview", "layout": "stack",
             "panes": [_pane(f"{spec.sid}/overview", _cli_argv("ui", "dash", "--shoal", spec.sid), env),
                       _pane(f"{spec.sid}/ledger", _cli_argv("ui", "tail", "--shoal", spec.sid), env)]}]
    for name, chunk in ui_herdr.role_tabs(spec):
        if chunk:
            tabs.append({"name": name, "layout": "grid",
                         "panes": [_pane(ui_herdr.role_label(spec.sid, r.slug),
                                         _cli_argv("view", spec.sid, r.slug, "--stub"), env) for r in chunk]})
    tabs.append({"name": "lattice", "mod": "lattice-view", "props": lattice_props(spec.sid, feed)})
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

    def _new_tab(self, space_id: str, tab: dict) -> None:
        if tab.get("mod"):
            if tab["mod"] not in self.mods:
                self.say(f"tab {tab['name']}: mod {tab['mod']} is not installed here (`shoal ui app-install`)")
                return
            self.say(f"tab {tab['name']} ({tab['mod']})")
            if not self.dry_run:
                app_do("tab.new", {"space": space_id, "name": tab["name"], "mod": tab["mod"], "props": tab["props"]})
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

    def select_tab(self, space_id: str, name: str) -> None:
        """Show the named tab of a space (the last tab made is the one shown otherwise)."""
        if self.dry_run or not space_id:
            return
        space = next((x for x in app_ls().get("spaces", []) if x.get("id") == space_id), None)
        tab = next((t for t in (space or {}).get("tabs", []) if t.get("name") == name), None)
        if tab is not None:
            app_do("tab.select", {"id": tab["id"]})

    def _gaps(self, space: dict, tab_doc: dict, tab: dict) -> None:
        """Fill one existing terminal tab: a planned pane with no live terminal gets one; a planned pane whose
        terminal came back as a bare shell (a lost terminal, or a card left with `q`) is replaced."""
        have: dict[str, tuple[str, str]] = {}
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
        missing = [p for p in tab["panes"] if p["label"] not in have]
        stale = [p for p in tab["panes"] if have.get(p["label"], ("", ""))[1] == "shell"]
        for p in stale:
            self.say(f"{space['name']}/{tab['name']}: restart {p['label']} (its pane is a bare shell)")
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

    def apply(self, doc: dict, focus: bool = True, show: set[str] | None = None) -> list[str]:
        ls = app_ls()
        mine = [s for s in ls.get("spaces", []) if (s.get("machine") or None) == self.machine]
        opened = load_opened()
        key = self.machine or "local"
        seen = set(opened.get(key, []))
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
                sid = self._new_space(s)
                first = first or sid
                continue
            first = first or got["id"]
            tabs = {t.get("name"): t for t in got.get("tabs", [])}
            for t in s["tabs"]:
                if t["name"] not in tabs:
                    self._new_tab(got["id"], t)
                elif not t.get("mod"):
                    self._gaps(s, tabs[t["name"]], t)
        planned = {s["name"] for s in doc.get("spaces", [])}
        import ui_herdr
        tags = set(ui_herdr._TAGS.values()) if self.machine is None else set()
        for x in mine:
            if x.get("name") in tags and x["name"] not in planned:
                self.say(f"space {x['name']}: its shoal is hidden or retired (close it in the app when done)")
        if focus and first and not self.dry_run:
            app_do("space.select", {"id": first})
        if not self.dry_run and seen != set(opened.get(key, [])):
            opened[key] = sorted(seen)
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
             standalone: bool = True, show: set[str] | None = None, ledger_root: str = "") -> tuple[int, list[str]]:
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
        out += Reconciler(machine, dry_run).apply(doc, focus, show)
    except AppError as exc:
        return 2, out + [str(exc)]
    if not dry_run:
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


def home_space(dry_run: bool = False) -> list[str]:
    """A `shoal` space on this machine with the home screen (the shoal-home mod, else the terminal chooser in app
    mode) and the global lattice."""
    ls = app_ls()
    got = next((s for s in ls.get("spaces", []) if not s.get("machine") and s.get("name") == "shoal"), None)
    rec = Reconciler(None, dry_run)
    tabs = []
    if "shoal-home" in rec.mods:
        tabs.append({"name": "home", "mod": "shoal-home", "props": {}})
    else:
        tabs.append({"name": "home", "layout": "stack", "panes": [_pane("shoal/home", _cli_argv("ui", "launch"))]})
    tabs.append({"name": "lattice", "mod": "lattice-view", "props": lattice_props()})
    if got is None:
        if tabs[0].get("mod"):
            # a space starts with a terminal: the chooser, then the home mod beside it as its own tab
            tabs.insert(0, {"name": "launch", "layout": "stack",
                            "panes": [_pane("shoal/home", _cli_argv("ui", "launch"))]})
        rec.apply({"spaces": [{"name": "shoal", "kind": "home", "folder": "", "tabs": tabs, "select": "home"}]},
                  focus=True)
    else:
        have = {t.get("name") for t in got.get("tabs", [])}
        for t in tabs:
            if t["name"] not in have:
                rec._new_tab(got["id"], t)
        if not dry_run:
            app_do("space.select", {"id": got["id"]})
            rec.select_tab(got["id"], "home")
    return rec.out


def cmd_app_start(a) -> int:
    if a.write_desktop:
        path = desktop_path()
        text = desktop_text()
        if a.dry_run:
            print(f"[dry-run] write {path}\n{text}")
        else:
            os.makedirs(os.path.dirname(path), exist_ok=True)
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
    base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return os.path.join(base, "applications", "shoal-herdr-app.desktop")


def desktop_text() -> str:
    icon = os.path.join(MOD_DIR, "shoal-home", "assets", "fish-teal.png")
    if not os.path.exists(icon):
        icon = os.path.join(PKG, "ui", "assets", "fish-teal.png")
    exe = shlex.join(_cli_argv("ui", "app-start"))
    return ("[Desktop Entry]\nType=Application\nName=Shoal\nComment=Shoal groups in the Herdr app\n"
            f"Exec={exe}\nIcon={icon}\nTerminal=false\nCategories=Development;\nStartupWMClass=herdr-app\n")


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
    import hashlib
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
        for line in install_keys(a.dry_run):
            print(line)
    return 0


# The 0.9.3 paste keys, kept at the maintainer's request (2026-11-03): under kitty, Ctrl+V pasted text and
# Ctrl+Shift+V an image. The app ships them the other way round.
PASTE_KEYS = {"ctrl-v": "terminal.paste", "ctrl-shift-v": "terminal.paste_image"}
APP_DEFAULT_PASTE_KEYS = {"ctrl-v": "terminal.paste_image", "ctrl-shift-v": "terminal.paste"}


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
    if not isinstance(doc, dict) or not isinstance(doc.get("terminal", {}), dict):
        return [f"keys: {path} has no `terminal` object shoal can edit: left as it is"]
    term = doc.setdefault("terminal", {})
    out, changed = [], False
    for key, want in PASTE_KEYS.items():
        have = term.get(key)
        if have == want:
            continue
        if have not in (None, APP_DEFAULT_PASTE_KEYS[key]):
            out.append(f"keys: {key} is bound to {have} by you: left as it is (0.9.3 had {want})")
            continue
        term[key] = want
        changed = True
        out.append(("[dry-run] " if dry_run else "") + f"keys: {key} -> {want} (the 0.9.3 paste keys)")
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
