"""Regressions for terminal replies, PNG lifecycle and non-overlapping splash layout."""
import base64
import io
import os
import fcntl
import pty
import select
import subprocess
import termios
import time
from pathlib import Path
import re
import struct
import sys
import tempfile
import unittest
import zlib

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "shoal/ui"))
import splash_graphics as sg
import ui_launch as ui


class Terminal(io.StringIO):
    def __init__(self, reader, supported=True):
        super().__init__()
        self.reader, self.supported = reader, supported
        self.uploads = []
        self.number, self.chunks = 0, []

    def write(self, text):
        result = super().write(text)
        match = re.fullmatch(r"\x1b_G(.*?)(?:;(.*))?\x1b\\", text)
        if not match:
            if text == "\x1b[c" and not self.supported:
                self.reader.feed(b"\x1b[?62;4c")
            return result
        header, payload = match.groups()
        fields = dict(x.split("=", 1) for x in header.split(","))
        if fields.get("a") == "q" and self.supported:
            self.reader.feed(f"\x1b_Gi={fields['i']};OK\x1b\\".encode())
        if fields.get("a") == "t":
            self.number = int(fields["I"])
            self.chunks = []
        if payload and fields.get("a") != "q":
            assert len(payload) <= 4096
            self.chunks.append(payload)
            if fields.get("m") == "0":
                self.uploads.append(base64.b64decode("".join(self.chunks)))
                self.reader.feed(f"\x1b_Gi={300 + len(self.uploads)},I={self.number};OK\x1b\\".encode())
        return result


class SplashTests(unittest.TestCase):
    def test_fragmented_and_late_replies_preserve_keys(self):
        r = sg.TerminalInput(-1)
        r.expected_ids.add(123)
        data = b"j\x1b_Gi=123;OK\x1b\\\x1b[?62;4c\x1b[Btguest@target.invalid\r"
        for byte in data:
            r.feed(bytes([byte]))
        self.assertEqual("".join(r.keys), "j\x1b[Btguest@target.invalid\r")
        self.assertEqual(r.query_reports, {123: "OK"})
        self.assertTrue(r.da)
        r.feed(b"\x1b_Gi=999;query failed\x1b\\")
        self.assertNotIn(999, r.query_reports)
        self.assertNotIn("failed", "".join(r.keys))

    def test_probe_negative_timeout_and_no_drawing(self):
        r = sg.TerminalInput(-1)
        out = Terminal(r, supported=False)
        g = sg.SplashGraphics(r, out)
        self.assertFalse(g.start(timeout=0))
        self.assertFalse(out.uploads)
        self.assertNotIn("a=p", out.getvalue())
        # No response at all also falls back, and a late response stays out of keys.
        r2 = sg.TerminalInput(-1)
        g2 = sg.SplashGraphics(r2, io.StringIO())
        self.assertFalse(g2.start(timeout=0))
        r2.feed(f"\x1b_Gi={g2.base};ENOTSUP\x1b\\q".encode())
        self.assertEqual(list(r2.keys), ["q"])

    def test_chunks_placements_cache_and_scoped_cleanup(self):
        with tempfile.TemporaryDirectory() as d:
            data = sg.probe_png() + b"padding" * 1000
            for name in sg.ASSET_NAMES:
                (Path(d) / name).write_bytes(data)
            r = sg.TerminalInput(-1)
            out = Terminal(r)
            g = sg.SplashGraphics(r, out, Path(d))
            self.assertTrue(g.start(timeout=0))
            self.assertEqual(out.uploads, [data] * len(sg.ASSET_NAMES))
            for size in ((72, 23), (160, 50), (90, 30)):
                g.hide()
                g.place(ui.image_rectangles(*size, 2))
            self.assertEqual(len(out.uploads), len(sg.ASSET_NAMES), "selection/resize reuses uploaded data")
            self.assertIn("z=-1,C=1,q=2", out.getvalue())
            self.assertIn("d=i,i=301", out.getvalue())
            g.close()
            self.assertFalse(g.ids)
            for image_id in range(301, 301 + len(sg.ASSET_NAMES)):
                self.assertIn(f"a=d,d=I,i={image_id},q=2", out.getvalue())
            self.assertNotIn("d=A", out.getvalue())
            self.assertNotIn("d=a", out.getvalue())

    def test_bad_asset_releases_partial_allocations(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "fish-coral.png").write_bytes(sg.probe_png())
            r = sg.TerminalInput(-1)
            out = Terminal(r)
            g = sg.SplashGraphics(r, out, Path(d))
            self.assertFalse(g.start(timeout=0))
            self.assertIn("a=d,d=I,i=301,q=2", out.getvalue())
            self.assertFalse(g.ids)

    def test_complete_ascii_and_bounded_images(self):
        rows = [("local", "here", False), ("remote", "other.invalid", False)]
        for cols, lines in ((121, 40), (160, 50), (200, 70)):
            page = ui.dashboard(rows, 1, cols=cols, lines=lines, color=False, art="ascii")
            picture = page.split("\n")
            self.assertEqual(len(picture), lines)
            self.assertTrue(all(len(row) <= cols for row in picture))
            top = (lines - 38) // 2
            hx = (cols - len(ui._HEADER[0])) // 2
            anchors = ((0, (cols - 36) // 2), (14, hx - 39), (14, hx + 44))
            for fish, (y, x) in zip(ui._FISH, anchors):
                for dy, row in enumerate(fish):
                    self.assertEqual(picture[top + y + dy].ljust(cols)[x:x + 36], row)
        for cols, lines in ((72, 23), (80, 24), (120, 40), (150, 30)):
            page = ui.dashboard(rows, 1, cols=cols, lines=lines, color=False, art="ascii")
            self.assertNotIn(".---.", page, "incomplete fish are hidden")
            self.assertIn("which host serves", page)
            rects = ui.image_rectangles(cols, lines, 2)
            self.assertEqual(len(rects), len(sg.ASSET_NAMES))
            for i, (x, y, w, h) in enumerate(rects):
                self.assertTrue(0 <= x < x + w <= cols and 0 <= y < y + h <= lines)
                for xx, yy, ww, hh in rects[:i]:
                    self.assertTrue(x + w <= xx or xx + ww <= x or y + h <= yy or yy + hh <= y)
        self.assertNotEqual(ui._FISH[0], ui._FISH[2])
        self.assertEqual({len(row) for f in ui._FISH for row in f}, {36})

    def test_image_scale_doubles_where_the_window_has_room(self):
        self.assertEqual(ui.min_size(2), (72, 23), "the art never raises the compact minimum")
        for cols, lines, scale in ((72, 23, 1), (90, 30, 1.5), (92, 33, 1.5), (93, 32, 1.5),
                                   (93, 33, 2), (120, 40, 2), (71, 23, 0), (72, 22, 0)):
            self.assertEqual(ui.image_scale(cols, lines, 2), scale, (cols, lines))
        base = [(w, h) for _, _, w, h in ui.image_rectangles(72, 23, 2)]
        self.assertEqual(base, [(8, 4), (12, 6), (12, 6), (6, 4), (8, 4)])
        rows = [("local", "here", False), ("remote", "other.invalid", False)]
        for cols, lines in ((93, 33), (120, 40), (160, 50)):
            rects = ui.image_rectangles(cols, lines, 2)
            self.assertEqual([(w, h) for _, _, w, h in rects], [(2 * w, 2 * h) for w, h in base])
            page = ui.dashboard(rows, 1, cols=cols, lines=lines, color=False, art="images").split("\n")
            for i, (x, y, w, h) in enumerate(rects):
                self.assertTrue(0 <= x < x + w <= cols and 0 <= y < y + h <= lines, (cols, lines, rects[i]))
                for xx, yy, ww, hh in rects[:i]:
                    self.assertTrue(x + w <= xx or xx + ww <= x or y + h <= yy or yy + hh <= y, (rects[i], (xx, yy)))
                for row in page[y:y + h]:  # the composition leaves the cells under each creature blank
                    self.assertEqual(row.ljust(cols)[x:x + w].strip(), "", (cols, lines, rects[i]))

    def test_positive_probe_pty_typing_resize_and_cleanup(self):
        master, slave = pty.openpty()
        attrs = termios.tcgetattr(slave)
        def resize(cols, lines):
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", lines, cols, 0, 0))
        resize(80, 24)
        child = """import sys
sys.path.insert(0, sys.argv[1])
import ui_launch, splash_graphics
from pathlib import Path
original = splash_graphics.SplashGraphics
class FixtureGraphics(original):
    def __init__(self, reader, out):
        super().__init__(reader, out, Path(sys.argv[2]))
splash_graphics.SplashGraphics = FixtureGraphics
rows = [('local', 'here', False), ('remote', 'other.invalid', False)]
print('RESULT', ui_launch._choose_interactive(['local', 'other'], rows, 1), flush=True)
"""
        with tempfile.TemporaryDirectory() as d:
            for name in sg.ASSET_NAMES:
                (Path(d) / name).write_bytes(sg.probe_png())
            env = dict(os.environ, TERM="xterm-kitty", SHOAL_SPLASH_ART="auto")
            env.pop("NO_COLOR", None)
            p = subprocess.Popen([sys.executable, "-c", child, str(Path(ui.__file__).parent), d],
                                 stdin=slave, stdout=slave, stderr=slave, env=env)
            output, pending = bytearray(), bytearray()
            uploads, number, typed = [], 0, False
            def until(check):
                nonlocal number, typed
                deadline = time.monotonic() + 8
                while not check() and time.monotonic() < deadline:
                    if not select.select([master], [], [], .05)[0]:
                        continue
                    data = os.read(master, 65536)
                    output.extend(data)
                    pending.extend(data)
                    while True:
                        start = pending.find(b"\x1b_G")
                        if start < 0:
                            if len(pending) > 2:
                                del pending[:-2]
                            break
                        end = pending.find(b"\x1b\\", start)
                        if end < 0:
                            break
                        header = bytes(pending[start + 3:end]).partition(b";")[0]
                        fields = dict(x.split(b"=", 1) for x in header.split(b",") if b"=" in x)
                        del pending[:end + 2]
                        if fields.get(b"a") == b"q":
                            # Keyboard arrives during negotiation, before either response.
                            os.write(master, b"j" + b"\x1b_Gi=" + fields[b"i"] + b";OK\x1b\\\x1b[?62;4c")
                            typed = True
                        if fields.get(b"a") == b"t":
                            number = int(fields[b"I"])
                        if fields.get(b"m") == b"0":
                            uploads.append(number)
                            os.write(master, f"\x1b_Gi={300 + len(uploads)},I={number};OK\x1b\\".encode())
                self.assertTrue(check(), bytes(output[-2000:]))
            try:
                until(lambda: b"\x1b[1;7mremote" in output)
                self.assertTrue(typed)
                self.assertEqual(len(uploads), len(sg.ASSET_NAMES))
                until(lambda: output.count(b"a=p,") >= 2 * len(sg.ASSET_NAMES))
                before = output.count(b"a=p,")
                resize(120, 40)
                until(lambda: output.count(b"a=p,") >= before + len(sg.ASSET_NAMES))
                self.assertEqual(len(uploads), len(sg.ASSET_NAMES))
                resize(32, 8)
                until(lambda: b"Enlarge window to 72x23" in output)
                resize(80, 24)
                before = output.count(b"a=p,")
                until(lambda: output.count(b"a=p,") >= before + len(sg.ASSET_NAMES))
                os.write(master, b"tguest@target.invalid\r")
                until(lambda: b"RESULT guest@target.invalid" in output)
                self.assertEqual(p.wait(timeout=5), 0)
                self.assertEqual(termios.tcgetattr(slave), attrs)
                for image_id in range(301, 301 + len(sg.ASSET_NAMES)):
                    self.assertIn(f"a=d,d=I,i={image_id},q=2".encode(), output)
            finally:
                if p.poll() is None:
                    p.terminate()
                    p.wait(timeout=5)
                os.close(master)
                os.close(slave)

    def test_packaged_png_integrity(self):
        directory = Path(ui.__file__).with_name("assets")
        for name in sg.ASSET_NAMES:
            data = (directory / name).read_bytes()
            self.assertTrue(data.startswith(b"\x89PNG\r\n\x1a\n"))
            pos, compressed = 8, bytearray()
            while pos < len(data):
                size = struct.unpack(">I", data[pos:pos + 4])[0]
                kind, body = data[pos + 4:pos + 8], data[pos + 8:pos + 8 + size]
                crc = struct.unpack(">I", data[pos + 8 + size:pos + 12 + size])[0]
                self.assertEqual(zlib.crc32(kind + body), crc)
                if kind == b"IHDR":
                    w, h, depth, color, *_ = struct.unpack(">IIBBBBB", body)
                    self.assertEqual((depth, color), (8, 6), "real RGBA PNG")
                if kind == b"IDAT":
                    compressed.extend(body)
                pos += size + 12
            self.assertEqual(len(zlib.decompress(compressed)), h * (4 * w + 1))


if __name__ == "__main__":
    unittest.main()
