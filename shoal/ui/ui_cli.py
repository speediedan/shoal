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
    q.set_defaults(func=ui_status.cmd_status)

    q = u.add_parser("line", help="one status line for a tab bar")
    ui_status.add_args(q)
    q.set_defaults(func=ui_status.cmd_line)

    def session_arg(q):
        q.add_argument("--session", default="", help="herdr session name (default SHOAL_HERDR_SESSION, else shoal)")

    q = u.add_parser("open", help="herdr: one workspace per shoal plus saved standalone sessions (idempotent)")
    session_arg(q)
    q.add_argument("--shoal", dest="ids", action="append", default=[])
    q.add_argument("--ledger-root", default="")
    q.add_argument("--no-standalone", action="store_true")
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

    q = u.add_parser("pick", help="open a standalone (non-shoal) Claude or opencode session")
    session_arg(q)
    q.add_argument("--harness", default="", choices=("", "claude", "opencode", "muse", "codex"))
    q.add_argument("--id", dest="session_id", default="")
    q.add_argument("--label", default="")
    q.add_argument("--cwd", default="", help="with --harness/--id: the session's working directory")
    q.add_argument("--save", action="store_true", help="restore it on every `ui open`")
    q.set_defaults(func=ui_herdr.cmd_pick)

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
    q.add_argument("--write", action="store_true", help="merge into the herdr config file (backup kept)")
    q.add_argument("--path", default="")
    q.add_argument("--dry-run", action="store_true")
    q.set_defaults(func=ui_herdr.cmd_config)

    q = u.add_parser("herdr-install", help="lane host: config block, plugin link, server + feeder units, open")
    q.add_argument("--dry-run", action="store_true")
    q.set_defaults(func=ui_herdr.cmd_install)

    q = u.add_parser("doctor", help="adapter preflight: restore off, no integrations, schema, units")
    session_arg(q)
    q.set_defaults(func=ui_herdr.cmd_doctor)
