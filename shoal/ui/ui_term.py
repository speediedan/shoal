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


def read_line(prompt: str) -> str | None:
    if not _tty():
        try:
            return input(prompt).strip()
        except EOFError:
            return None
    sys.stdout.write(prompt)
    sys.stdout.flush()
    buf = ""
    with _Cbreak():
        while True:
            k = _key()
            if k in (ESC, CTRL_C):
                sys.stdout.write("\n")
                return None
            if k in ("\r", "\n"):
                sys.stdout.write("\n")
                return buf.strip()
            if k in BACKSPACES:
                if buf:
                    buf = buf[:-1]
                    sys.stdout.write("\b \b")
            elif k and k.isprintable():
                buf += k
                sys.stdout.write(k)
            sys.stdout.flush()


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
