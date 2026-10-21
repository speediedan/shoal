#!/usr/bin/env python3
"""`shoal ui launch`: open the shoal Herdr view from this host's terminal (architecture doc §12, "Launching the view").

    shoal ui launch [--server SERVER|local] [--no-open] [--no-choose] [--shoal ID]...

Which host serves the view is asked at launch: this host ("local") first, then every host with an ssh route
(`[hosts.<server>]`), each marked unreachable when its cached probe fails. Enter takes the last choice (kept in
$XDG_STATE_HOME/shoal/launch.last); a typed ssh target also works. `--server` never asks, and neither does a
launch without a terminal (it takes the last choice, else local). No configuration beyond the routes, by design
(maintainer, 2026-10-21: none until Herdr machines are in use).

- **local:** this host's own Herdr server. A host provisioned for the local view (`herdr-install --local`, its
  units carry HERDR_CONFIG_PATH) keeps using that file; a lane host (its server reads `config.toml`) is started as
  it is; a host with neither is provisioned with `herdr-install --local`. Then `ui open`, and a local client.
- **remote:** over the route, the host's own `shoal-herdr` unit is started and its `ui open` run (it fills layout
  gaps only), then `herdr --remote <route> --session <s> --remote-keybindings server` draws that host's view here.
  An unreachable host fails within about 10 s with the `doctor` hint and returns to the chooser: it never attaches
  a client that hangs.

Launching never wakes a role and costs no tokens: cards attach to a session only when Enter is pressed on one.
"""
from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.dirname(HERE)
for _p in (PKG, HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import config  # noqa: E402

PREP_TIMEOUT_S = 60
SIDEBAR_MARK = re.compile(r"^# shoal-sidebar-max: (\d{1,3})%$")


def state_path() -> str:
    base = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    return os.path.join(base, "shoal", "launch.last")


def last_choice() -> str:
    try:
        return open(state_path()).read().strip()
    except OSError:
        return ""


def remember(choice: str) -> None:
    try:
        os.makedirs(os.path.dirname(state_path()), exist_ok=True)
        open(state_path(), "w").write(choice + "\n")
    except OSError:
        pass


def this_server() -> str:
    return config.server() or os.uname().nodename.split(".")[0]


def candidates() -> list[str]:
    """'local' first, then every routed host (by its server name), this host itself left out."""
    import routes
    me = this_server()
    return ["local"] + [h for h in sorted(routes.routes()) if h != me]


def is_local(choice: str) -> bool:
    c = choice.rsplit("@", 1)[-1].split(".")[0]
    return choice == "local" or c == this_server()


def label(choice: str) -> str:
    import routes
    if choice == "local":
        return f"local   this host ({this_server()}): its own Herdr server; other hosts' roles over ssh"
    up = routes.reachable(choice)
    mark = "" if up is not False else "   (unreachable now)"
    return f"remote  {choice}: that host's Herdr server, drawn here{mark}"


def choose(read=input) -> str | None:
    """The numbered chooser; None when cancelled (EOF or q)."""
    cands = candidates()
    last = last_choice()
    default = cands.index(last) + 1 if last in cands else 1
    print("shoal ui launch: which host serves the view?")
    for i, c in enumerate(cands, 1):
        print(f"  {'*' if i == default else ' '}{i}) {label(c)}")
    try:
        pick = read(f"host [{default}]: ").strip()
    except EOFError:
        return None
    if pick in ("q", "Q"):
        return None
    if not pick:
        return cands[default - 1]
    if pick.isdigit() and 1 <= int(pick) <= len(cands):
        return cands[int(pick) - 1]
    return pick  # a typed server name or ssh target


def herdr_bin() -> str:
    return config.env("HERDR_BIN") or shutil.which("herdr") or os.path.expanduser("~/.local/bin/herdr")


def resolve_sidebar(path: str, cols: int | None = None) -> None:
    """A sidebar width given as a share of the window (`ui set sidebar_max_width 50%`, carried as a
    `# shoal-sidebar-max: N%` marker over the `sidebar_max_width` line) becomes columns of THIS window, because
    Herdr takes columns only."""
    try:
        lines = open(path).read().split("\n")
    except OSError:
        return
    if cols is None:
        cols = shutil.get_terminal_size((160, 40)).columns
    changed = False
    for i, line in enumerate(lines[:-1]):
        m = SIDEBAR_MARK.match(line)
        if m and lines[i + 1].startswith("sidebar_max_width ="):
            w = max(18, cols * int(m.group(1)) // 100)
            new = f"sidebar_max_width = {w}"
            if lines[i + 1] != new:
                lines[i + 1], changed = new, True
    if changed:
        open(path + ".tmp", "w").write("\n".join(lines))
        os.replace(path + ".tmp", path)


def unit_reads_local_conf() -> bool | None:
    """True when this host's shoal-herdr unit carries HERDR_CONFIG_PATH (provisioned for the local view), False
    for a lane host's unit (it reads config.toml), None when there is no unit."""
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    p = os.path.join(base, "systemd", "user", "shoal-herdr.service")
    try:
        return "HERDR_CONFIG_PATH=" in open(p).read()
    except OSError:
        return None


def launch_local(opened: bool, ids: list[str], dry_run: bool = False) -> int:
    import ui_herdr
    kind = unit_reads_local_conf()
    env = dict(os.environ, HERDR_SESSION=ui_herdr.session_name())
    if kind is not False:
        env["HERDR_CONFIG_PATH"] = ui_herdr.local_config_path()
    if opened and not dry_run:
        print(f"shoal ui launch: preparing {ui_herdr.session_name()} on this host (no role is woken)...")
        if kind is False:
            # a lane host: its server and feeder are already provisioned; start them and fill layout gaps
            subprocess.run(["systemctl", "--user", "start", "shoal-herdr.service", "shoal-ui-feed.service"],
                           capture_output=True)
            rc = ui_herdr.cmd_open(ui_herdr.argparse_ns(session="", ids=ids, ledger_root="", dry_run=False,
                                                        no_standalone=False))
        else:
            os.environ["HERDR_CONFIG_PATH"] = env["HERDR_CONFIG_PATH"]
            rc = ui_herdr.install_local(ui_herdr.argparse_ns(check=False, dry_run=False, ids=ids))
        if rc:
            print("shoal ui launch: local preparation failed (attaching anyway; `shoal ui doctor` says why)")
    conf = env.get("HERDR_CONFIG_PATH") or ui_herdr.config_path()
    argv = [herdr_bin(), "--session", ui_herdr.session_name()]
    if dry_run:
        print(shlex.join(["exec"] + argv) + f"   (config {conf})")
        return 0
    remember("local")
    resolve_sidebar(conf)
    sys.stdout.flush()
    os.execvpe(argv[0], argv, env)
    return 0  # not reached


def remote_target(server: str) -> tuple[dict | None, str]:
    """(route, the ssh target Herdr should use): a route's host resolved like routes.ssh_argv does (a MagicDNS name
    through tailscale), or a typed target as given."""
    import routes
    r = routes.route(server)
    if r is None:
        return {"ssh": server, "resolve": "dns"}, server
    if r.get("herdr_target"):
        return r, str(r["herdr_target"])
    ip = routes.resolve_host(r)
    user = str(r["ssh"]).rsplit("@", 1)[0] if "@" in str(r["ssh"]) else ""
    return r, (f"{user}@{ip}" if user else ip) if ip else str(r["ssh"])


def launch_remote(server: str, opened: bool, ids: list[str], dry_run: bool = False) -> int:
    """2: the host could not be prepared (unreachable, or its preparation failed fast); else execs the client."""
    import routes
    import ui_herdr
    r, target = remote_target(server)
    sess = ui_herdr.session_name()
    if not dry_run:
        # every mode probes first, attach-only included: an unreachable host gets the doctor hint and the chooser
        # again, never Herdr's own ssh error (measured 2026-10-21 with --no-open)
        if routes.route(server) is not None:
            up = routes.reachable(server, force=True)
        else:  # a typed target: probe it the same way
            try:
                up = subprocess.run(routes.ssh_argv(r) + ["true"], stdin=subprocess.DEVNULL, capture_output=True,
                                    timeout=12).returncode == 0
            except (OSError, subprocess.TimeoutExpired):
                up = False
        if up is False:
            print(f"shoal ui launch: {server} is unreachable now (ssh BatchMode, 5 s): `shoal doctor` checks its route; "
                  f"nothing was attached")
            return 2
    if opened:
        only = "".join(f" --shoal {shlex.quote(i)}" for i in ids)
        script = (f"export PATH=$HOME/.local/bin:$PATH HERDR_SESSION={sess} SHOAL_HERDR_SESSION={sess}; "
                  f"systemctl --user start shoal-herdr.service 2>/dev/null; "
                  f"for i in 1 2 3 4 5 6 7 8 9 10; do shoal ui open --dry-run >/dev/null 2>&1 && break; sleep 1; done; "
                  f"shoal ui open{only}")
        argv = routes.ssh_argv(r) + ["bash -lc " + shlex.quote(script)]
        if dry_run:
            print(shlex.join(argv))
        else:
            print(f"shoal ui launch: preparing {sess} on {server} (no role is woken)...")
            try:
                rc = subprocess.run(argv, stdin=subprocess.DEVNULL, timeout=PREP_TIMEOUT_S).returncode
            except subprocess.TimeoutExpired:
                rc = 124
            if rc == 255:
                print(f"shoal ui launch: could not reach {server} (ssh exit 255): `shoal doctor` checks its route; "
                      f"nothing was attached")
                return 2
            if rc:
                print(f"shoal ui launch: preparation on {server} failed (exit {rc}); attaching anyway "
                      f"(`shoal ui doctor` on {server} says why)")
    argv = [herdr_bin(), "--remote", target, "--session", sess, "--remote-keybindings", "server"]
    if dry_run:
        print(shlex.join(["exec"] + argv))
        return 0
    remember(server)
    resolve_sidebar(os.path.join(ui_herdr.config_dir(), "config.toml"))
    sys.stdout.flush()
    os.execvp(argv[0], argv)
    return 0  # not reached


def cmd_launch(a) -> int:
    if os.environ.get("HERDR_SOCKET_PATH") or os.environ.get("HERDR_SESSION"):
        # inside a Herdr pane the view would be drawn on the host that serves it, nested in itself
        print("shoal ui launch: run this in a plain terminal, not inside the Herdr viewer")
        return 2
    opened = not a.no_open
    while True:
        if a.server:
            choice = a.server
        elif not a.no_choose and sys.stdin.isatty() and sys.stdout.isatty():
            choice = choose()
            if choice is None:
                return 1
        else:
            choice = last_choice() or "local"
        choice = "local" if is_local(choice) else choice
        # only a choice that worked is remembered: a host that just failed must never become the default
        # (measured 2026-10-21: an unreachable test host became the chooser's Enter)
        if choice == "local":
            return launch_local(opened, a.ids, a.dry_run)
        rc = launch_remote(choice, opened, a.ids, a.dry_run)
        if rc != 2 or a.server or a.no_choose or not (sys.stdin.isatty() and sys.stdout.isatty()):
            return rc
        print()  # back to the chooser
