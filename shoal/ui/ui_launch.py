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
  it is, and its terminal client reads the merged local file (the client rows too; the server never reads it); a
  host with neither is provisioned with `herdr-install --local`. Then `ui open`, and a local client.
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


def entry(choice: str) -> tuple[str, str, bool]:
    """(kind, where, unreachable) for one chooser row."""
    import routes
    if choice == "local":
        return "local", f"{this_server()}, this host", False
    return "remote", choice, routes.reachable(choice) is False


# The chooser is a start screen in the style of a Neovim dashboard: a block-letter header, the hosts as a
# menu with their keys on the right, and cute fish swimming around it (maintainer, 2026-10-26).
_HEADER = (
    "███████╗██╗  ██╗ ██████╗  █████╗ ██╗     ",
    "██╔════╝██║  ██║██╔═══██╗██╔══██╗██║     ",
    "███████╗███████║██║   ██║███████║██║     ",
    "╚════██║██╔══██║██║   ██║██╔══██║██║     ",
    "███████║██║  ██║╚██████╔╝██║  ██║███████╗",
    "╚══════╝╚═╝  ╚═╝ ╚═════╝ ╚═╝  ╚═╝╚══════╝",
)
_HEADER_SGR = ("1;96", "1;96", "1;36", "1;36", "1;94", "1;94")  # blocks shade from cyan to blue
_OUTLINE_SGR = "34"
_TAGLINE = "stigmergic · heterogeneous · open · agent · lattice"
_FISH_COLORS = ("96", "93", "95", "92", "91", "94", "33", "35")  # each fish its own color
_BUBBLE_SGR = "2;36"
_BUBBLES = ("o", "O", ".")
# the side fish face the menu: two rows of bubbles over a three-row fish
_LEFT_FISH = ("   o    ", "  .  o  ", "  ___   ", "\\/ o \\  ", "/\\___/  ")
_RIGHT_FISH = ("    o   ", "  o  .  ", "   ___  ", "  / o \\/", "  \\___/\\")
_BANDS = ((("><(((*>", 0.03), ("o", 0.22), ("<><", 0.34), ("O", 0.58), ("><((*>", 0.66), ("<*))><", 0.86)),
          (("<*)))><", 0.08), (".", 0.27), ("><>", 0.40), ("o", 0.53), ("<><", 0.62), ("><((((*>", 0.80)))
_MENU_W = 54
_SIDE_GAP = 4

Seg = tuple[str, str]  # (text, SGR; "" for none)


def _paint(segs: list[Seg], color: bool) -> str:
    return "".join(f"\x1b[{s}m{t}\x1b[0m" if color and s and t.strip() else t for t, s in segs).rstrip()


def _width(segs: list[Seg]) -> int:
    return sum(len(t) for t, _ in segs)


def _centered(segs: list[Seg], width: int) -> list[Seg]:
    return [(" " * max(0, (width - _width(segs)) // 2), "")] + segs


def _band(items, width: int, n: int) -> tuple[list[Seg], int]:
    """One row of small fish and bubbles at fractions of the width; n numbers the fish for their colors."""
    segs: list[Seg] = []
    at = 0
    for text, frac in items:
        start = int(frac * width)
        if start < at + (1 if at else 0) or start + len(text) > width:
            continue
        bubble = text in _BUBBLES
        segs += [(" " * (start - at), ""), (text, _BUBBLE_SGR if bubble else _FISH_COLORS[n % len(_FISH_COLORS)])]
        n += 0 if bubble else 1
        at = start + len(text)
    return segs, n


def _header_row(row: str, i: int) -> list[Seg]:
    segs: list[Seg] = []
    for ch in row:
        sgr = "" if ch == " " else _HEADER_SGR[i] if ch == "█" else _OUTLINE_SGR
        if segs and segs[-1][1] == sgr:
            segs[-1] = (segs[-1][0] + ch, sgr)
        else:
            segs.append((ch, sgr))
    return segs


def _art(lines: tuple[str, ...], n: int) -> list[list[Seg]]:
    """A side fish: bubble rows dim, the fish rows in fish color n."""
    return [[(ln, _BUBBLE_SGR if set(ln) <= set(" o.O") else _FISH_COLORS[n % len(_FISH_COLORS)])] for ln in lines]


def unicode_ok(stream=None) -> bool:
    try:
        "█╗▸·".encode(getattr(stream or sys.stdout, "encoding", None) or "ascii")
        return True
    except (UnicodeEncodeError, LookupError):
        return False


def layout(cols: int | None = None) -> tuple[int, int]:
    """(page width, menu width) for a terminal of cols columns (page capped at 96)."""
    if cols is None:
        cols = shutil.get_terminal_size((80, 24)).columns
    width = max(24, min(cols, 96))
    return width, min(_MENU_W, width - 2)


def dashboard(rows: list[tuple[str, str, bool]], default: int, cols: int | None = None,
              color: bool | None = None, uni: bool | None = None) -> str:
    """The launch chooser's start screen for rows of (kind, where, unreachable), keys 1..n, the default marked.
    Sized to the terminal: the block header from 48 columns (else a spaced title), side fish beside the menu from
    80. Colors only on a terminal and never under NO_COLOR; box-drawing only where the stream can encode it.
    Pure and deterministic so tests can pin the shape."""
    width, menu_w = layout(cols)
    if color is None:
        color = sys.stdout.isatty() and not os.environ.get("NO_COLOR")
    if uni is None:
        uni = unicode_ok()
    dot = " · " if uni else " | "
    out: list[list[Seg]] = []
    top, n = _band(_BANDS[0], width, 0)
    out += [top, []]
    if uni and width >= len(_HEADER[0]) + 4:
        out += [_centered(_header_row(r, i), width) for i, r in enumerate(_HEADER)]
    else:
        out.append(_centered([("S H O A L", "1;96")], width))
    tag = _TAGLINE if uni else _TAGLINE.replace("·", "|")
    if len(tag) <= width:
        out.append(_centered([(tag, "33")], width))
    out += [[], _centered([("which host serves the view?", "1")], width), []]
    menu: list[list[Seg]] = []
    for i, (kind, where, down) in enumerate(rows, 1):
        mark = ("▸" if uni else ">") if i == default else " "
        tail = "  (unreachable now)" if down else ""
        key = str(i)
        room = max(4, menu_w - 2 - 8 - len(key) - 2)
        body = where[:max(1, room - len(tail))]
        gap = " " * max(1, menu_w - 2 - 8 - len(body) - len(tail) - len(key))
        menu += [[(mark, "1;93"), (" ", ""), (f"{kind:<7} ", "1" if i == default else ""),
                  (body, "" if i == default else "2"), (tail, "91"), (gap, ""), (key, "1;91")], []]
    menu = menu[:-1]
    sides = width >= menu_w + 2 * (len(_LEFT_FISH[0]) + _SIDE_GAP)
    left, right = (_art(_LEFT_FISH, n), _art(_RIGHT_FISH, n + 1)) if sides else ([], [])
    tall = max(len(menu), len(left))
    m0, a0 = (tall - len(menu)) // 2, (tall - len(left)) // 2
    for r in range(tall):
        row = menu[r - m0] if 0 <= r - m0 < len(menu) else []
        row = row + [(" " * (menu_w - _width(row)), "")]
        if sides:
            lw, gap = len(_LEFT_FISH[0]), (" " * _SIDE_GAP, "")
            la = left[r - a0] if 0 <= r - a0 < len(left) else [(" " * lw, "")]
            ra = right[r - a0] if 0 <= r - a0 < len(right) else [("", "")]
            row = la + [gap] + row + [gap] + ra
        out.append(_centered(row, width))
    out.append([])
    for note in ("local: this host's Herdr server, other hosts' roles over ssh",
                 "remote: that host's Herdr server, drawn here",
                 f"enter takes the marked host{dot}q quits{dot}or type an ssh target"):
        if len(note) <= width:
            out.append(_centered([(note, "2")], width))
    bottom, _ = _band(_BANDS[1], width, n + 2)
    out.append(bottom)
    return "\n".join(_paint(segs, color) for segs in out)


def choose(read=input) -> str | None:
    """The numbered chooser; None when cancelled (EOF or q)."""
    cands = candidates()
    last = last_choice()
    default = cands.index(last) + 1 if last in cands else 1
    print(dashboard([entry(c) for c in cands], default))
    width, menu_w = layout()
    try:
        pick = read(" " * max(0, (width - menu_w) // 2) + f"host [{default}]: ").strip()
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
    if kind is False:
        # a lane host's server reads config.toml, which holds only the server block; its own terminal client gets the
        # merged local file (the client rows too), for the client only: the running server never reads it
        conf = env["HERDR_CONFIG_PATH"] = ui_herdr.local_config_path()
        if not dry_run:
            try:
                new = ui_herdr.build_local_config()
                if new != (open(conf).read() if os.path.exists(conf) else None):
                    os.makedirs(os.path.dirname(conf), exist_ok=True)
                    open(conf + ".tmp", "w").write(new)
                    os.replace(conf + ".tmp", conf)
            except Exception as exc:  # noqa: BLE001 - a client without the rows still works
                print(f"shoal ui launch: client config not written ({exc}); attaching with config.toml")
                conf = env["HERDR_CONFIG_PATH"] = ui_herdr.config_path()
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
