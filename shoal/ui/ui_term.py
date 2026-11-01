#!/usr/bin/env python3
"""Terminal input for the UI's popups: one set of keys everywhere.

Herdr popups receive every key, including Escape, so line-mode `input()` echoes Escape as `^[` and a
typed `q` lands in the next prompt. These helpers read keys in cbreak mode instead:

- `read_line`: Enter submits; Escape or Ctrl+C cancels (returns None); Backspace edits; other escape
  sequences (arrows and so on) are ignored.
- `pause`: Enter, Escape or `q` closes.
- `confirm`: `y` is yes; anything else (Enter, Escape, `n`) is no.

Without a terminal (tests, pipes) they fall back to plain line reads.
"""
from __future__ import annotations

import os
import re
import select
import sys

ESC, CTRL_C, BACKSPACES = "\x1b", "\x03", ("\x7f", "\x08")


def _tty() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


class _Cbreak:
    def __enter__(self):
        import termios
        import tty
        self.fd = sys.stdin.fileno()
        self.old = termios.tcgetattr(self.fd)
        tty.setcbreak(self.fd)
        return self

    def __exit__(self, *exc):
        import termios
        termios.tcsetattr(self.fd, termios.TCSADRAIN, self.old)


def _key() -> str:
    """One key. A lone Escape is returned as ESC; an escape sequence is swallowed and returns ''."""
    ch = os.read(sys.stdin.fileno(), 1).decode(errors="replace")
    if ch != ESC:
        return ch
    seq = ""
    while select.select([sys.stdin], [], [], 0.03)[0]:
        seq += os.read(sys.stdin.fileno(), 1).decode(errors="replace")
    return ESC if not seq else ""


def _key_seq() -> str:
    """One key, keeping escape sequences whole: a lone Escape is ESC; an arrow, Home, End, Delete or a
    bracketed paste (ESC[200~ ... ESC[201~) comes back as its full sequence."""
    ch = os.read(sys.stdin.fileno(), 1).decode(errors="replace")
    if ch != ESC:
        return ch
    seq = ch
    while select.select([sys.stdin], [], [], 0.03)[0]:
        seq += os.read(sys.stdin.fileno(), 1).decode(errors="replace")
        if seq == ESC + "[200~":  # bracketed paste: read through to its end marker
            while not seq.endswith(ESC + "[201~"):
                seq += os.read(sys.stdin.fileno(), 1).decode(errors="replace")
            break
    return seq


def read_line(prompt: str, default: str = "") -> str | None:
    """A line of text, pre-filled with `default` to edit. Left/Right, Home/End (or Ctrl+A/Ctrl+E),
    Delete, Backspace and Ctrl+U edit it; a paste is inserted at the cursor (bracketed-paste markers are
    removed); Enter accepts; Escape or Ctrl+C cancels (None). The pilot found the earlier editor took
    only typing and Backspace, so a value could not be fixed in place."""
    if not _tty():
        try:
            return input(prompt).strip() or default.strip()
        except EOFError:
            return None
    buf, pos = list(default), len(default)

    def draw(buf: list = buf, pos: int = pos) -> None:
        sys.stdout.write("\r" + prompt + "".join(buf) + "\x1b[K")
        back = len(buf) - pos
        if back:
            sys.stdout.write(f"\x1b[{back}D")
        sys.stdout.flush()

    sys.stdout.write("\x1b[?2004h")  # bracketed paste on while reading: pastes arrive marked
    draw()
    try:
        return _edit(prompt, buf, pos, draw)
    finally:
        sys.stdout.write("\x1b[?2004l")
        sys.stdout.flush()


def _edit(prompt: str, buf: list, pos: int, draw) -> str | None:
    with _Cbreak():
        while True:
            k = _key_seq()
            if k in (ESC, CTRL_C):
                sys.stdout.write("\n")
                return None
            if k in ("\r", "\n"):
                sys.stdout.write("\n")
                return "".join(buf).strip()
            if k.startswith(ESC + "[200~"):
                text = k[len(ESC + "[200~"):-len(ESC + "[201~")].replace("\r", " ").replace("\n", " ")
                buf[pos:pos] = list(text)
                pos += len(text)
            elif k in (ESC + "[D", ESC + "OD"):
                pos = max(0, pos - 1)
            elif k in (ESC + "[C", ESC + "OC"):
                pos = min(len(buf), pos + 1)
            elif k in (ESC + "[H", ESC + "OH", ESC + "[1~", "\x01"):
                pos = 0
            elif k in (ESC + "[F", ESC + "OF", ESC + "[4~", "\x05"):
                pos = len(buf)
            elif k == ESC + "[3~":
                if pos < len(buf):
                    del buf[pos]
            elif k == "\x15":  # Ctrl+U clears
                buf, pos = [], 0
            elif k in BACKSPACES:
                if pos:
                    del buf[pos - 1]
                    pos -= 1
            elif len(k) == 1 and k.isprintable():
                buf.insert(pos, k)
                pos += 1
            draw(buf, pos)


def pause(prompt: str = "[enter, esc or q] close") -> None:
    if not _tty():
        try:
            input(prompt)
        except EOFError:
            pass
        return
    sys.stdout.write(prompt)
    sys.stdout.flush()
    with _Cbreak():
        while _key() not in ("\r", "\n", ESC, "q", "Q", CTRL_C):
            pass
    sys.stdout.write("\n")


def confirm(prompt: str) -> bool:
    if not _tty():
        try:
            return input(prompt).strip().lower() == "y"
        except EOFError:
            return False
    sys.stdout.write(prompt)
    sys.stdout.flush()
    with _Cbreak():
        k = _key()
    sys.stdout.write((k if k.isprintable() else "") + "\n")
    return k in ("y", "Y")


def key(prompt: str) -> str:
    """One key after a prompt: returns "enter", "esc" or the character pressed."""
    if not _tty():
        try:
            raw = input(prompt).strip()
        except EOFError:
            return "esc"
        return raw[:1] or "enter"
    sys.stdout.write(prompt)
    sys.stdout.flush()
    with _Cbreak():
        k = ""
        while not k:
            k = _key()
    sys.stdout.write("\n")
    return {"\r": "enter", "\n": "enter", ESC: "esc", CTRL_C: "esc"}.get(k, k)


def page(text: str) -> None:
    """Show a popup's text. When it fits the terminal, print it and close on Enter, Esc or q (the
    pilot found `less` keeps Esc as a prefix and Enter as scroll); when it does not, `less`, whose
    prompt says `[q] close`."""
    rows = 0
    if _tty():
        try:
            rows = os.get_terminal_size(sys.stdout.fileno()).lines
        except OSError:
            rows = 0
    lines = text.rstrip("\n").split("\n")
    if not rows or len(lines) < rows - 1:
        sys.stdout.write("\n".join(lines) + "\n")
        pause()
        return
    import subprocess
    subprocess.run(["less", "-R", "-K", "-Ps[q] close   [space] [arrows] scroll"], input=text, text=True)


_CSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")


def screen_size() -> tuple[int, int]:
    """(columns, rows) of stdout's terminal; (0, 0) when it is not one (tests, pipes)."""
    try:
        size = os.get_terminal_size(sys.stdout.fileno())
        return size.columns, size.lines
    except (OSError, ValueError):
        return 0, 0


def fit_frame(body: str, footer: str, cols: int = 0, rows: int = 0) -> str:
    """One full-screen redraw that never scrolls. A frame taller than the pane pushes its top lines
    into the scrollback on every tick, and ED 2 clears only the visible screen, so the scrollback
    collects one copy of the top block (the usage table) per refresh. The body is clipped to the
    rows left above the footer, with a marker for what was cut; ED 3 drops copies an older frame left;
    no newline follows the footer (one on the last row would scroll)."""
    if not (cols and rows):
        cols, rows = screen_size()
    lines = body.rstrip("\n").split("\n")
    if rows:
        tall = lambda ln: max(1, -(-len(_CSI.sub("", ln)) // cols)) if cols else 1  # noqa: E731
        room = rows - 1 - sum(tall(ln) for ln in footer.split("\n"))  # a blank row above the footer
        if sum(tall(ln) for ln in lines) > room:
            kept: list[str] = []
            used = tall("... 99999 more lines: enlarge the pane")  # the marker's rows
            for ln in lines:
                used += tall(ln)
                if used > room:
                    break
                kept.append(ln)
            lines = kept + [f"\x1b[2m... {len(lines) - len(kept)} more lines: enlarge the pane\x1b[0m"]
    return "\x1b[H\x1b[2J\x1b[3J" + "\n".join(lines) + "\n\n" + footer


class CodeWatch:
    """Re-exec a long-running UI loop (a role card, the dashboard, the ledger tail) when shoal's code
    changes, so an upgrade reaches panes that stay open for days (measured 2026-10-12: cards kept a
    fixed viewer bug until re-applied). The new stamp must hold for two checks before the re-exec,
    so a checkout caught mid-write is not loaded."""

    def __init__(self) -> None:
        self.pkg = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.stamp = self._stamp()
        self.seen = None

    def _stamp(self) -> float:
        import glob
        files = [f for d in ("", "ui", "telemetry") for f in glob.glob(os.path.join(self.pkg, d, "*.py"))]
        try:
            return max(os.path.getmtime(f) for f in files) if files else 0.0
        except OSError:
            return self.stamp if hasattr(self, "stamp") else 0.0

    def check(self) -> None:
        now = self._stamp()
        if now == self.stamp:
            self.seen = None
            return
        if self.seen != now:  # changed: wait one more check for it to settle
            self.seen = now
            return
        sys.stdout.write("\x1b[2m(shoal code changed: reloading this pane)\x1b[0m\n")
        sys.stdout.flush()
        os.execv(sys.executable, [sys.executable] + sys.argv)
