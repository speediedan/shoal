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
_FISH_COLORS = ("38;5;209", "38;5;80", "38;5;141")
# Original ASCII outline: a full fish, not a few punctuation marks that resemble broken text.
_ASCII_RIGHT = tuple(row.ljust(36) for row in r"""                 .-.
              .-'   `-.
    __      .'         `.
   /  `-.__/             \
  /       /         o     \
  \      |                 >
   >     |       .---.    /
  /      |      /    |  .'
  \       \    /     |.'
   \__.-'  `-._    .-'
              /`--'\
              \ __ /
               '--'""".splitlines())
_MIRROR = str.maketrans("/\\()<>[]{}'`", "\\/)(><][}{`'")
_ASCII_LEFT = tuple(row[::-1].translate(_MIRROR) for row in _ASCII_RIGHT)
_FISH = (_ASCII_RIGHT, _ASCII_RIGHT, _ASCII_LEFT)
_MENU_W = 54
_MIN_COLS = 72
_ASCII_COLS = len(_HEADER[0]) + 2 * (36 + 3) + 2


_IMAGE_SCALES = (2.0, 1.5, 1.0)  # PNG creatures at double size where the window has room, else the largest that fits


def min_size(n_rows: int) -> tuple[int, int]:
    """Native compact splash. Optional ASCII art never forces a giant minimum window."""
    return _MIN_COLS, 19 + 2 * max(1, n_rows)


def _bands(scale: float) -> tuple[int, int]:
    """(rows above the logo, rows of the side band beside it) for the image composition at `scale`."""
    return round(4 * scale), round(6 * scale)


def image_scale(cols: int, lines: int | None, n_rows: int) -> float:
    """The largest image scale whose whole composition fits; 0.0 below the compact minimum. Scale 1
    needs exactly `min_size`, so the minimum (and the resize request) never grows with the art."""
    for scale in _IMAGE_SCALES:
        top, side = _bands(scale)
        need_cols = max(_MIN_COLS, len(_HEADER[0]) + 2 * (round(12 * scale) + 2))
        need_lines = top + side + 9 + 2 * max(1, n_rows)
        if cols >= need_cols and (lines is None or lines >= need_lines):
            return scale
    return 0.0


def art_mode(cols: int, lines: int | None, n_rows: int, requested: str) -> str:
    if requested == "images":
        return "images" if image_scale(cols, lines, n_rows) else "none"
    if requested == "ascii" and cols >= _ASCII_COLS and (lines is None or lines >= 36 + 2 * max(1, n_rows)):
        return "ascii"
    return "none"

Seg = tuple[str, str]  # (text, SGR; "" for none)


def _paint(segs: list[Seg], color: bool) -> str:
    return "".join(f"\x1b[{s}m{t}\x1b[0m" if color and s and t.strip() else t for t, s in segs).rstrip()


def _width(segs: list[Seg]) -> int:
    return sum(len(t) for t, _ in segs)


def _centered(segs: list[Seg], width: int) -> list[Seg]:
    return [(" " * max(0, (width - _width(segs)) // 2), "")] + segs


def _header_row(row: str, i: int) -> list[Seg]:
    segs: list[Seg] = []
    for ch in row:
        sgr = "" if ch == " " else _HEADER_SGR[i] if ch == "█" else _OUTLINE_SGR
        if segs and segs[-1][1] == sgr:
            segs[-1] = (segs[-1][0] + ch, sgr)
        else:
            segs.append((ch, sgr))
    return segs


def _logo(width: int, uni: bool, art: str = "none", scale: float = 1.0) -> list[list[Seg]]:
    """Non-overlapping cell stamps keep every ASCII outline intact."""
    header = [_header_row(r, i) for i, r in enumerate(_HEADER)] if uni else [[("S H O A L", "1;96")]]
    hw = max(_width(r) for r in header)
    hx = (width - hw) // 2
    if art == "ascii":
        height, hy = 27, 14 + (13 - len(header)) // 2
    elif art == "images":
        top, side = _bands(scale)
        height, hy = top + side, top + (side - len(header)) // 2
    else:
        height, hy = len(header), 0
    canvas = [[(" ", "") for _ in range(width)] for _ in range(height)]

    def stamp(y, x, segs):
        for text, sgr in segs:
            for ch in text:
                if not (0 <= y < height and 0 <= x < width):
                    raise ValueError("splash stamp outside frame")
                if ch != " ":
                    if canvas[y][x][0] != " ":
                        raise ValueError("overlapping splash stamps")
                    canvas[y][x] = ch, sgr
                x += 1

    for i, row in enumerate(header):
        stamp(hy + i, hx, row)
    if art == "ascii":
        anchors = ((0, (width - 36) // 2), (14, hx - 39), (14, hx + hw + 3))
        for fish, sgr, (y, x) in zip(_FISH, _FISH_COLORS, anchors):
            for dy, row in enumerate(fish):
                stamp(y + dy, x, [(row, sgr)])
    result = []
    for row in canvas:
        segs: list[Seg] = []
        for ch, sgr in row:
            if segs and segs[-1][1] == sgr:
                segs[-1] = segs[-1][0] + ch, sgr
            else:
                segs.append((ch, sgr))
        result.append(segs)
    return result


def image_rectangles(cols: int, lines: int, n_rows: int) -> list[tuple[int, int, int, int]]:
    """Cell rectangles relative to the same composition as the text, at `image_scale`: the compact
    layout (8x4, 12x6, 6x4 cells) doubled in both dimensions where the window has room."""
    s = image_scale(cols, lines, n_rows) or 1.0
    band, side = _bands(s)
    top = max(0, (lines - (band + side + 2 * max(1, n_rows) + 7)) // 2)
    hx = (cols - len(_HEADER[0])) // 2
    c = lambda v: round(v * s)  # noqa: E731
    return [(cols // 2 - c(4), top, c(8), band),
            (hx - c(12) - 2, top + band, c(12), side),
            (hx + len(_HEADER[0]) + 2, top + band, c(12), side),
            (cols // 2 - c(17), top, c(6), band),
            (cols // 2 + c(9), top, c(8), band)]


def terminal_size() -> os.terminal_size:
    """Read the actual tty: inherited COLUMNS/LINES may still describe the parent window."""
    try:
        size = os.get_terminal_size(sys.stdout.fileno())
        if size.columns > 0 and size.lines > 0:
            return size
    except (OSError, ValueError):
        pass
    return shutil.get_terminal_size((80, 24))


def _frame(page: str) -> str:
    """Absolute row addressing works even when the tty's output newline conversion is off."""
    return "\x1b[0m\x1b[H" + "".join(
        f"\x1b[{i};1H\x1b[2K{row}" for i, row in enumerate(page.split("\n"), 1)
    ) + "\x1b[J"


def unicode_ok(stream=None) -> bool:
    try:
        "█╗▸·".encode(getattr(stream or sys.stdout, "encoding", None) or "ascii")
        return True
    except (UnicodeEncodeError, LookupError):
        return False


def layout(cols: int | None = None) -> tuple[int, int]:
    """(page width, menu width) for a terminal of cols columns: the page fills the window."""
    if cols is None:
        cols = terminal_size().columns
    width = max(24, cols)
    return width, min(_MENU_W, width - 2)


def dashboard(rows: list[tuple[str, str, bool]], default: int, cols: int | None = None,
              color: bool | None = None, uni: bool | None = None, lines: int | None = None, art: str = "ascii") -> str:
    """The launch chooser's start screen for rows of (kind, where, unreachable), keys 1..n, row `default`
    selected (marked, and reversed where color is on). Art is explicit: image rectangles are reserved
    only after an interactive probe, while large ASCII fish are omitted when they do not fit. The
    logo/menu composition stays centered, with vertical centering when `lines` is supplied. A narrow fallback
    uses a spaced title. Colors only on
    a terminal and never under NO_COLOR; box-drawing only where the stream can encode it.
    Pure and deterministic so tests can pin the shape."""
    width, menu_w = layout(cols)
    if color is None:
        color = sys.stdout.isatty() and not os.environ.get("NO_COLOR")
    if uni is None:
        uni = unicode_ok()
    dot = " · " if uni else " | "
    out: list[list[Seg]] = []
    mode = art_mode(width, lines, len(rows), art)
    out += _logo(width, uni and width >= len(_HEADER[0]) + 4, mode, image_scale(width, lines, len(rows)) or 1.0)
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
        sel = i == default
        menu += [[(mark, "1;93"), (" ", ""), (f"{kind:<7} ", "1;7" if sel else ""),
                  (body, "7" if sel else "2"), (tail, "91"), (gap, "7" if sel else ""), (key, "1;91")], []]
    menu = menu[:-1]
    for row in menu:
        out.append(_centered(row + [(" " * (menu_w - _width(row)), "")], width))
    out.append([])
    keys = f"↑↓ / j k move{dot}Enter choose{dot}t type SSH target{dot}q quit" if uni else \
        f"j/k move{dot}Enter choose{dot}t type SSH target{dot}q quit"
    for note in ("local: this host's Herdr server, other hosts' roles over ssh",
                 "remote: that host's Herdr server, drawn here", keys):
        if len(note) <= width:
            out.append(_centered([(note, "2")], width))
    rendered = [_paint(segs, color) for segs in out]
    if lines is not None and lines >= len(rendered):
        top = (lines - len(rendered)) // 2
        rendered = [""] * top + rendered + [""] * (lines - len(rendered) - top)
    return "\n".join(rendered)


def _next_key(getchar) -> str:
    """One key from a char callable: 'up', 'down', 'enter', 'esc', '' on a poll timeout
    (the caller redraws, which is how a resize re-centers), or the character itself."""
    ch = getchar()
    if ch == "":
        return ""
    if ch in ("\r", "\n"):
        return "enter"
    if ch == "\x03":  # Ctrl-C in cbreak mode
        return "esc"
    if ch != "\x1b":
        return ch
    for _ in range(5):  # the rest of an escape sequence must not be lost to a poll timeout
        nxt = getchar()
        if nxt:
            break
    else:
        return "esc"
    if nxt != "[":
        return "esc"
    return {"A": "up", "B": "down"}.get(getchar(), "esc")


def _choose_keys(cands: list[str], rows, selected: int, getchar, draw, line_read) -> str | None:
    """The selection loop over an injected key source (selected is 1-based, the default starts highlighted).
    draw(selected) paints; line_read() collects a typed ssh target after `t`. Pure of terminal handling."""
    while True:
        draw(selected)
        try:
            k = _next_key(getchar)
        except (EOFError, StopIteration):
            return None
        if k == "":
            continue  # poll timeout: redraw at the (possibly new) size
        if k in ("up", "k"):
            selected = max(1, selected - 1)
        elif k in ("down", "j"):
            selected = min(len(cands), selected + 1)
        elif k == "enter":
            return cands[selected - 1]
        elif k in ("esc", "q", "Q"):
            return None
        elif k in ("t", "/"):
            typed = line_read()
            if typed:
                return typed
        elif k.isdigit() and 1 <= int(k) <= len(cands):
            return cands[int(k) - 1]


def _choose_interactive(cands: list[str], rows, selected: int) -> str | None:
    """One tty input owner handles both keyboard input and bounded graphics negotiation."""
    import termios
    import tty
    from splash_graphics import SplashGraphics, TerminalInput
    fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd)
    out = sys.stdout
    reader = TerminalInput(fd)
    graphics = SplashGraphics(reader, out)
    need = min_size(len(rows))
    state: dict = {"drawn": None, "asked": False, "selected": selected}
    requested = config.env("SPLASH_ART", "auto") or "auto"
    requested = requested if requested in ("auto", "ascii", "none") else "auto"
    art = "none"

    def draw(sel: int) -> None:
        state["selected"] = sel
        size = terminal_size()
        small = size.columns < need[0] or size.lines < need[1]
        mode = art_mode(size.columns, size.lines, len(rows), art)
        key = (sel, size.columns, size.lines, small, mode)
        if key == state["drawn"]:
            return
        state["drawn"] = key
        out.write("\x1b[?2026h")  # synchronized update where supported, ignored elsewhere
        try:
            graphics.hide()
            if small:
                if not state["asked"]:
                    state["asked"] = True
                    out.write(f"\x1b[8;{need[1]};{need[0]}t")
                from textwrap import wrap
                msg = f"Enlarge window to {need[0]}x{need[1]} (now {size.columns}x{size.lines}). q quits."
                text = wrap(msg, max(1, size.columns - 2))[:size.lines]
                top = max(0, (size.lines - len(text)) // 2)
                page = "\n" * top + "\n".join(line.center(size.columns) for line in text)
            else:
                state["asked"] = False
                page = dashboard(rows, sel, cols=size.columns, lines=size.lines, art=mode)
            out.write(_frame(page))
            if mode == "images" and not small:
                graphics.place(image_rectangles(size.columns, size.lines, len(rows)))
        finally:
            out.write("\x1b[?2026l")
            out.flush()

    def line_read() -> str:
        # Do not hand input() a tty after the probe already queued typed characters.
        # This editor shares the same input owner and cannot lose a rapid `t<target><Enter>`.
        value: list[str] = []
        try:
            while True:
                draw(state["selected"])
                size = terminal_size()
                room = max(0, size.columns - len("ssh target: ") - 1)
                text = "".join(value)
                tail = text[-room:] if room else ""
                out.write(f"\x1b[{size.lines};1H\x1b[2Kssh target: {tail}")
                out.flush()
                key = _next_key(reader.getchar)
                if key == "enter":
                    return text.strip()
                if key == "esc":
                    return ""
                if key in ("\x7f", "\b"):
                    if value:
                        value.pop()
                elif len(key) == 1 and key.isprintable() and len(value) < 1024:
                    value.append(key)
        except EOFError:
            return ""
        finally:
            state["drawn"] = None

    try:
        tty.setcbreak(fd)
        out.write("\x1b[?1049h\x1b[?25l\x1b[?7l")
        out.flush()
        if requested == "auto" and os.environ.get("TERM") != "dumb" and graphics.start():
            art = "images"
        else:
            art = "ascii" if requested != "none" else "none"
        return _choose_keys(cands, rows, selected, reader.getchar, draw, line_read)
    finally:
        try:
            graphics.close()
            out.write("\x1b[0m\x1b[?2026l\x1b[?7h\x1b[?25h\x1b[?1049l")
            out.flush()
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, saved)


def choose(read=input) -> str | None:
    """The full-screen chooser: arrow keys or j/k move (the last choice starts highlighted), enter
    chooses, t types an ssh target, q quits. Off a terminal (or if the screen fails), the numbered
    prompt. None when cancelled."""
    cands = candidates()
    last = last_choice()
    default = cands.index(last) + 1 if last in cands else 1
    rows = [entry(c) for c in cands]
    if read is input and sys.stdin.isatty() and sys.stdout.isatty():
        try:
            return _choose_interactive(cands, rows, default)
        except Exception:  # noqa: BLE001 - a chooser must never strand the launcher
            pass
    print(dashboard(rows, default))
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
