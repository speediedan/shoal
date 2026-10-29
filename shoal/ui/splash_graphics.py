"""Bounded Kitty PNG probing/placements and terminal replies kept out of menu input."""
from __future__ import annotations

import base64
from collections import deque
from pathlib import Path
import os
import re
import secrets
import select
import struct
import time
import zlib

ESC = "\x1b"
ST = b"\x1b\\"
_DA = re.compile(rb"\x1b\[[?>]?[0-9;:]*c")


def command(fields: str, payload: str = "") -> str:
    return f"{ESC}_G{fields}{';' + payload if payload else ''}{ESC}\\"


def probe_png() -> bytes:
    """One transparent RGBA pixel: test PNG decoding without allocating a stored image."""
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"\0\0\0\0\0")) + chunk(b"IEND", b""))


class TerminalInput:
    """One input owner. Even delayed/fragmented graphics replies never become chooser keys."""
    def __init__(self, fd: int):
        self.fd = fd
        self.buffer = bytearray()
        self.keys: deque[str] = deque()
        self.reports: dict[int, tuple[int, str]] = {}
        self.query_reports: dict[int, str] = {}
        self.da = False
        self.expected_ids: set[int] = set()
        self.expected_numbers: set[int] = set()
        self.partial_since = 0.0
        import codecs
        self.decoder = codecs.getincrementaldecoder("utf-8")("replace")

    def feed(self, data: bytes) -> None:
        self.buffer.extend(data)
        while self.buffer:
            b = bytes(self.buffer)
            if b.startswith(b"\x1b_G"):
                end = b.find(ST, 3)
                if end < 0:
                    if len(b) > 8192:
                        self.buffer.clear()  # malformed control packet, never executable keys
                    break
                header, _, message = b[3:end].partition(b";")
                fields = dict(x.split(b"=", 1) for x in header.split(b",") if b"=" in x)
                reply = message.decode("ascii", "replace")
                try:
                    image_id = int(fields.get(b"i", b"0"))
                    number = int(fields.get(b"I", b"0"))
                    if image_id in self.expected_ids:
                        self.query_reports[image_id] = reply
                    if number in self.expected_numbers:
                        self.reports[number] = image_id, reply
                except ValueError:
                    pass
                del self.buffer[:end + 2]
                continue
            match = _DA.match(b)
            if match:
                self.da = True
                del self.buffer[:match.end()]
                continue
            # Hold an incomplete terminal response or key sequence until another read.
            if b in (b"\x1b", b"\x1b_", b"\x1b[", b"\x1b_G") or re.fullmatch(rb"\x1b\[[?>][0-9;:]*", b):
                break
            char = self.decoder.decode(bytes(self.buffer[:1]))
            del self.buffer[:1]
            self.keys.extend(char)
        self.partial_since = time.monotonic() if self.buffer else 0.0

    def poll(self, timeout: float = 0.1) -> None:
        if select.select([self.fd], [], [], max(0, timeout))[0]:
            data = os.read(self.fd, 4096)
            if not data:
                raise EOFError
            self.feed(data)
        elif self.buffer and time.monotonic() - self.partial_since >= 0.1:
            if bytes(self.buffer) in (b"\x1b", b"\x1b["):
                self.keys.extend(bytes(self.buffer).decode("ascii"))
                self.buffer.clear()
            # Partial graphics/DA responses stay control data, including after probe timeout.

    def getchar(self) -> str:
        if not self.keys:
            self.poll()
        return self.keys.popleft() if self.keys else ""

    def wait(self, ready, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while not ready():
            left = deadline - time.monotonic()
            if left <= 0:
                return False
            self.poll(min(0.05, left))
        return True


class SplashGraphics:
    """PNG data travels through the tty, so SSH needs no terminal-local file paths or helpers."""
    def __init__(self, reader: TerminalInput, out, directory: Path | None = None):
        self.reader, self.out = reader, out
        self.directory = directory or Path(__file__).with_name("assets")
        self.base = secrets.randbelow(2 ** 30 - 8) + 1
        self.ids: list[int] = []
        self.numbers: list[int] = []
        self.enabled = False

    def write(self, fields, payload=""):
        self.out.write(command(fields, payload))
        self.out.flush()

    def start(self, timeout: float = 0.35) -> bool:
        try:
            self.reader.expected_ids.add(self.base)
            self.write(f"a=q,t=d,f=100,i={self.base}", base64.b64encode(probe_png()).decode("ascii"))
            self.out.write(f"{ESC}[c")
            self.out.flush()
            self.reader.wait(lambda: self.base in self.reader.query_reports or self.reader.da, timeout)
            if self.reader.query_reports.get(self.base) != "OK":
                return False
            for index, name in enumerate(("fish-coral.png", "fish-teal.png", "fish-violet.png"), 1):
                data = (self.directory / name).read_bytes()
                if not data.startswith(b"\x89PNG\r\n\x1a\n") or len(data) > 8 * 1024 * 1024:
                    raise ValueError("invalid splash PNG")
                number = self.base + index
                self.numbers.append(number)
                self.reader.expected_numbers.add(number)
                encoded = base64.b64encode(data).decode("ascii")
                for offset in range(0, len(encoded), 4096):
                    more = int(offset + 4096 < len(encoded))
                    fields = f"a=t,t=d,f=100,I={number},m={more}" if not offset else f"m={more}"
                    self.write(fields, encoded[offset:offset + 4096])
                self.reader.wait(lambda: number in self.reader.reports, timeout)
                image_id, reply = self.reader.reports.get(number, (0, ""))
                if not image_id or reply != "OK":
                    raise ValueError("splash image rejected or not acknowledged")
                self.ids.append(image_id)
            self.enabled = True
            return True
        except (OSError, ValueError, EOFError):
            self.close()
            return False

    def hide(self) -> None:
        for image_id in self.ids:
            self.write(f"a=d,d=i,i={image_id},q=2")  # keep the uploaded pixels for redraw

    def place(self, rectangles: list[tuple[int, int, int, int]]) -> None:
        for image_id, (x, y, cols, rows) in zip(self.ids, rectangles):
            self.out.write(f"{ESC}[{y + 1};{x + 1}H")
            self.write(f"a=p,i={image_id},p=1,c={cols},r={rows},z=-1,C=1,q=2")
        self.out.write(f"{ESC}[H")

    def close(self) -> None:
        # Delete only this splash's allocations. Never clear another application's images.
        for index, number in enumerate(self.numbers):
            if index < len(self.ids):
                self.write(f"a=d,d=I,i={self.ids[index]},q=2")
            else:
                self.write(f"a=d,d=N,I={number},q=2")  # allocation not acknowledged yet
        self.ids.clear()
        self.numbers.clear()
        self.enabled = False
