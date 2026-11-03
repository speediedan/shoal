#!/usr/bin/env python3
"""`shoal ui ...` and `shoal view`: argument wiring for the UI read model and the Herdr adapter.
The commands themselves live in ui_status (read model), ui_view (viewer) and ui_herdr (adapter)."""
from __future__ import annotations

import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import backends  # noqa: E402
import ui_herdr  # noqa: E402
import ui_status  # noqa: E402
import ui_view  # noqa: E402


def cmd_pane_open(a) -> int:
    """A plugin action's body: open one of the plugin's popup panes (keys bind to actions). The
    card focused when the key was pressed rides along as SHOAL_UI_FOCUS_PANE, so `next`, `msg` and
    the others act on it."""
    import json
    herdr = os.environ.get("HERDR_BIN_PATH") or "herdr"
    argv = [herdr, "plugin", "pane", "open", "--plugin", ui_herdr.PLUGIN_ID, "--entrypoint", a.entrypoint]
    try:
        focus = json.loads(os.environ.get("HERDR_PLUGIN_CONTEXT_JSON") or "{}").get("focused_pane_id") or ""
    except ValueError:
        focus = ""
    if focus:
        argv += ["--env", f"SHOAL_UI_FOCUS_PANE={focus}"]
    return subprocess.run(argv).returncode


def add_parsers(sub) -> None:
    ui_view.add_parser(sub)
    p = sub.add_parser("ui", help="operator UI: read model (status/line) and the Herdr adapter")
    u = p.add_subparsers(dest="ui_cmd", required=True)

    q = u.add_parser("status", help="the observation contract (v1) for every shoal on this host (read-only)")
    ui_status.add_args(q)
    q.add_argument("--conditions", action="store_true", help="add live health conditions (cached 5 min)")
    q.add_argument("--refresh-conditions", action="store_true", help="recompute the conditions now")
    q.add_argument("--pull", action="store_true", help="pull the ledger first (multi-host; default no)")
    q.add_argument("--json", action="store_true")
    q.add_argument("--rows-only", action="store_true",
                   help="this host's own role rows as JSON, no usage and no remote merge (what another host's "
                        "viewer pulls over its route)")
    q.set_defaults(func=ui_status.cmd_status)

    import ui_graph
    q = u.add_parser("graph", help="the graph document (v1): roles, the maintainer and outside seats as nodes, "
                                   "ledger traffic as edges, the event tail (read-only)")
    ui_graph.add_args(q)
    q.add_argument("--since", default="", metavar="CURSOR", help="only events after this cursor (from a previous doc)")
    q.add_argument("--json", action="store_true")
    q.set_defaults(func=ui_graph.cmd_graph)

    q = u.add_parser("graph-feed", help="write the graph document atomically every interval (graph.json beside "
                                        "the status cache); reads only appended ledger bytes per tick")
    ui_graph.add_args(q)
    q.add_argument("--interval", type=float, default=ui_graph.FEED_INTERVAL_S)
    q.add_argument("--out", default="", help="output path (default: graph.json beside the status cache)")
    q.add_argument("--once", action="store_true", help="one tick, then exit")
    q.add_argument("--verbose", action="store_true")
    q.set_defaults(func=ui_graph.cmd_graph_feed)

    q = u.add_parser("line", help="one status line for a tab bar")
    ui_status.add_args(q)
    q.set_defaults(func=ui_status.cmd_line)

    import subscription_usage
    q = u.add_parser("usage", help="cached subscription meters (no model turns); --refresh reads account APIs")
    q.add_argument("--providers", default="", help="comma-separated codex,claude,muse,opencode-go; default UI setting")
    q.add_argument("--refresh", action="store_true", help="read account APIs now, without inference")
    q.add_argument("--json", action="store_true")
    q.set_defaults(func=subscription_usage.cmd_usage)

    def session_arg(q):
        q.add_argument("--session", default="", help="herdr session name (default SHOAL_HERDR_SESSION, else shoal)")

    q = u.add_parser("open", help="herdr: one workspace per shoal plus saved standalone sessions (idempotent)")
    session_arg(q)
    q.add_argument("--shoal", dest="ids", action="append", default=[])
    q.add_argument("--ledger-root", default="")
    q.add_argument("--no-standalone", action="store_true")
    q.add_argument("--show", action="append", default=[], metavar="LABEL",
                   help="reopen a standalone session that was closed in the viewer (repeatable)")
    q.add_argument("--reapply", action="store_true",
                   help="also restart role cards on the current code (refused while any card is in use)")
    q.add_argument("--dry-run", action="store_true")
    q.set_defaults(func=ui_herdr.cmd_open)

    q = u.add_parser("hide", help="viewer setting: leave a shoal out of the UI (the shoal keeps running)")
    q.add_argument("shoal", nargs="+")
    q.set_defaults(func=ui_herdr.cmd_hide, unhide=False)
    q = u.add_parser("show", help="viewer setting: bring a hidden shoal back into the UI")
    q.add_argument("shoal", nargs="+")
    q.set_defaults(func=ui_herdr.cmd_hide, unhide=True)

    q = u.add_parser("set", help="viewer setting: roles_per_tab, sidebar_max_width, role_order.<shoal> ('' resets)")
    q.add_argument("key")
    q.add_argument("value")
    q.set_defaults(func=ui_herdr.cmd_set)

    q = u.add_parser("feed", help="herdr: push role state and tokens (read-only to shoal)")
    session_arg(q)
    q.add_argument("--interval", type=float, default=ui_herdr.FEED_INTERVAL_S)
    q.add_argument("--once", action="store_true")
    q.add_argument("--verbose", action="store_true")
    q.set_defaults(func=ui_herdr.cmd_feed)

    q = u.add_parser("menu", help="the operator palette (a herdr popup)")
    q.set_defaults(func=ui_herdr.cmd_menu)

    q = u.add_parser("act", help="one operator verb: preview, confirm, run")
    q.add_argument("verb", choices=[k for k, _ in ui_herdr.MENU if k not in ("metrics", "pick", "open")])
    q.add_argument("target_shoal", nargs="?", default="", metavar="SHOAL")
    q.add_argument("role", nargs="?", default="", metavar="ROLE")
    q.add_argument("--pause", action="store_true")
    q.set_defaults(func=ui_herdr.cmd_act)

    q = u.add_parser("pick", help="open a standalone session from Claude, opencode, Muse or Codex")
    session_arg(q)
    q.add_argument("--harness", default="", choices=("", *backends.ecosystems()))
    q.add_argument("--id", dest="session_id", default="")
    q.add_argument("--label", default="")
    q.add_argument("--cwd", default="", help="with --harness/--id: the session's working directory")
    q.add_argument("--model", default="", help="the seat's model (saved; its card resumes with it)")
    q.add_argument("--effort", default="", help="the seat's effort (saved; its card resumes with it)")
    q.add_argument("--save", action="store_true", help="restore it on every `ui open`")
    q.add_argument("--pause", action="store_true", help="keep errors visible until dismissed (popup use)")
    q.set_defaults(func=ui_herdr.cmd_pick)

    q = u.add_parser("legend", help="what the viewer's state icons and glyphs mean")
    q.set_defaults(func=ui_herdr.cmd_legend)

    q = u.add_parser("standalone", help="this host's saved standalone sessions with their turn state (read-only)")
    q.add_argument("--json", action="store_true")
    q.set_defaults(func=ui_herdr.cmd_standalone)

    import handover as _handover
    q = u.add_parser("handover", help="context handover for a saved standalone session: opt in, toggle, show (docs/handover.md)")
    q.add_argument("target", nargs="?", default="", metavar="LABEL_OR_JOB")
    q.add_argument("--doc", default="", help="the session's start doc (plan_state)")
    q.add_argument("--mode", default="", choices=("", "off", "advise", "auto"),
                   help="off; advise (ask it to refresh its start doc past the limit); auto (then reset it onto that doc)")
    q.add_argument("--k", type=int, default=0, help=f"limit in thousands of tokens (default {_handover.DEFAULT_K})")
    q.add_argument("--list", action="store_true", help="every saved local session's handover settings and context")
    q.add_argument("--dry-run", action="store_true")
    q.set_defaults(func=_handover.cmd_handover)

    q = u.add_parser("forget", help="drop a saved standalone session (open panes stay)")
    q.add_argument("session_id", metavar="ID_OR_LABEL")
    q.set_defaults(func=ui_herdr.cmd_forget)

    q = u.add_parser("dash", help="overview pane: status of the chosen shoals, refreshed")
    q.add_argument("--shoal", dest="ids", action="append", default=[])
    q.add_argument("--interval", type=float, default=30)
    q.set_defaults(func=ui_herdr.cmd_dash)

    q = u.add_parser("tail", help="ledger tail pane (read-only)")
    q.add_argument("--shoal", dest="ids", action="append", default=[])
    q.add_argument("--ledger-root", default="")
    q.add_argument("--backlog", action="store_true", default=True)
    q.add_argument("--once", action="store_true")
    q.set_defaults(func=ui_herdr.cmd_tail)

    q = u.add_parser("metrics", help="failure-mode metrics and the latest report files")
    q.add_argument("--shoal", dest="ids", action="append", default=[])
    q.add_argument("--ledger-root", default="")
    q.add_argument("--days", type=int, default=7)
    q.add_argument("--pause", action="store_true")
    q.add_argument("--latest-html", action="store_true", help="print the newest report HTML path per shoal")
    q.add_argument("--build-html", action="store_true", help="with --latest-html: render a rolling 7-day report first")
    q.set_defaults(func=ui_herdr.cmd_metrics)

    q = u.add_parser("quiet-check", help="prove viewing woke nobody: snapshot role activity, then compare")
    q.add_argument("--shoal", dest="ids", action="append", default=[])
    q.add_argument("--save", default="", help="write a snapshot to this file")
    q.add_argument("--compare", default="", help="compare now against a saved snapshot")
    q.set_defaults(func=ui_status.cmd_quiet_check)

    q = u.add_parser("pane-open", help="(plugin action body) open a plugin popup")
    q.add_argument("entrypoint")
    q.set_defaults(func=cmd_pane_open)

    q = u.add_parser("herdr-config", help="print or write the managed herdr config block")
    g = q.add_mutually_exclusive_group(required=True)
    g.add_argument("--client", action="store_true", help="sidebar rows and status line (the viewing machine)")
    g.add_argument("--server", action="store_true", help="restore off, keys (the lane host)")
    g.add_argument("--local", action="store_true",
                   help="both blocks merged, in the local view's own file (a host whose client also serves the view)")
    q.add_argument("--write", action="store_true", help="merge into the herdr config file (backup kept)")
    q.add_argument("--path", default="")
    q.add_argument("--dry-run", action="store_true")
    q.set_defaults(func=ui_herdr.cmd_config)

    q = u.add_parser("herdr-install", help="lane host: config block, plugin link, server + feeder units, open")
    q.add_argument("--dry-run", action="store_true")
    q.add_argument("--local", action="store_true",
                   help="the local view: its own config file (HERDR_CONFIG_PATH), units carrying it, plugin, open")
    q.add_argument("--check", action="store_true", help="with --local: report only, against the local file")
    q.add_argument("--shoal", dest="ids", action="append", default=[], help="with --local: shoals to open (default all)")
    q.set_defaults(func=ui_herdr.cmd_install)

    import ui_launch
    q = u.add_parser("launch", help="open the shoal Herdr view from this terminal: choose the host (local or a route)")
    q.add_argument("--server", default="", help="SERVER (a [hosts.*] route name), an ssh target, or local: never asks")
    q.add_argument("--no-open", action="store_true", help="attach only: do not prepare the host or run ui open")
    q.add_argument("--no-choose", action="store_true", help="never ask: the last choice, else local")
    q.add_argument("--shoal", dest="ids", action="append", default=[], help="shoals to open (default all)")
    q.add_argument("--dry-run", action="store_true", help="print what would run, attach nothing")
    q.add_argument("--list", action="store_true", help="print the hosts the chooser offers and exit (read-only)")
    q.add_argument("--json", action="store_true", help="with --list: the host list as JSON (the app's home screen)")
    q.add_argument("--summary", action="store_true", help="with --list: each reachable host's shoals and needs-you "
                   "counts (its `ui graph`, cached 30 s)")
    q.set_defaults(func=ui_launch.cmd_launch)

    # the Herdr app adapter (ui_app.py; docs/ui.md "The Herdr app")
    import ui_app
    q = u.add_parser("app-plan", help="Herdr app: this host's shoal spaces, tabs and panes as a plan (read-only)")
    q.add_argument("--shoal", dest="ids", action="append", default=[], help="shoals to plan (default all shown)")
    q.add_argument("--no-standalone", action="store_true", help="leave saved standalone sessions out")
    q.add_argument("--ledger-root", default="", help="a ledger clone to read instead of the configured ones")
    q.add_argument("--json", action="store_true")
    q.set_defaults(func=ui_app.cmd_app_plan)
    q = u.add_parser("app-open", help="Herdr app: open (or fill gaps in) a host's shoal spaces in the running app")
    q.add_argument("--machine", default="", help="local (default) or a [hosts.*] route name (added as an app machine)")
    q.add_argument("--shoal", dest="ids", action="append", default=[], help="shoals to open (default all shown)")
    q.add_argument("--show", action="append", default=[], help="reopen a standalone space closed in the app")
    q.add_argument("--no-standalone", action="store_true", help="leave saved standalone sessions out")
    q.add_argument("--no-focus", action="store_true", help="do not switch to the first space")
    q.add_argument("--ledger-root", default="", help="local: a ledger clone to read instead of the configured ones")
    q.add_argument("--dry-run", action="store_true", help="print what would be added, change nothing")
    q.set_defaults(func=ui_app.cmd_app_open)
    q = u.add_parser("app-start", help=f"Herdr app: start it with the pin held ({ui_app.APP_PIN}), open the home space")
    q.add_argument("--write-desktop", action="store_true", help="also write a desktop entry that runs app-start")
    q.add_argument("--dry-run", action="store_true")
    q.set_defaults(func=ui_app.cmd_app_start)
    q = u.add_parser("app-install", help="Herdr app: the graph feed unit here, and the shoal mods linked into the app")
    q.add_argument("--check", action="store_true", help="report only (the app doctor rows)")
    q.add_argument("--dry-run", action="store_true")
    q.set_defaults(func=ui_app.cmd_app_install)

    import herdr_pin
    q = u.add_parser("herdr-get", help=f"install the pinned Herdr release (checksum-verified; pin {herdr_pin.PIN})")
    q.add_argument("--version", default="", help="override the pin (bump PIN in herdr_pin.py instead, deliberately)")
    q.add_argument("--check", action="store_true", help="exit 0 when the installed version is the pin")
    q.set_defaults(func=herdr_pin.cmd_get)

    q = u.add_parser("doctor", help="adapter preflight: restore off, no integrations, schema, units")
    session_arg(q)
    q.set_defaults(func=ui_herdr.cmd_doctor)
