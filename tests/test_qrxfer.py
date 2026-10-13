"""Unit and pipeline tests for qrxfer (python3 -m unittest discover -s tests)."""

import importlib.util
import io
import os
import random
import shutil
import sys
import tarfile
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import qrxfer as q  # noqa: E402

HAVE_NUMPY = importlib.util.find_spec("numpy") is not None
HAVE_SEGNO = importlib.util.find_spec("segno") is not None


def available_decoders():
    found = []
    for name, cls in sorted(q.QR_DECODERS.items()):
        try:
            found.append(cls())
        except (ImportError, OSError):
            pass
    return found


class Base45Test(unittest.TestCase):
    def test_rfc9285_vectors(self):
        self.assertEqual(q.b45encode(b"AB"), "BB8")
        self.assertEqual(q.b45encode(b"Hello!!"), "%69 VD92EX0")
        self.assertEqual(q.b45encode(b"base-45"), "UJCLQE7W581")
        self.assertEqual(q.b45decode("QED8WEX0"), b"ietf!")

    def test_roundtrip_and_errors(self):
        rnd = random.Random(1)
        for n in range(0, 40):
            data = bytes(rnd.getrandbits(8) for _ in range(n))
            self.assertEqual(q.b45decode(q.b45encode(data)), data)
        for bad in ("A", "GGW", "a1", "ZZ"):  # bad length, triplet overflow, bad char, pair overflow
            with self.assertRaises(ValueError):
                q.b45decode(bad)


class FrameTest(unittest.TestCase):
    def test_pack_parse_and_corruption(self):
        enc = q.FountainEncoder(os.urandom(1000), 300, 77)
        text = q.pack_frame(enc.frame(5))
        self.assertTrue(text.startswith("QX:") and text.endswith("."))
        frame = q.parse_frame("  " + text + "\n")
        self.assertEqual((frame.tid, frame.seq, frame.k, frame.block_size), (77, 5, 4, 300))
        body = text[3:-1]
        for i in (0, len(body) // 2, len(body) - 2):
            flipped = body[:i] + ("0" if body[i] != "0" else "1") + body[i + 1:]
            with self.assertRaises(ValueError):
                q.parse_frame("QX:" + flipped + ".")
        for bad in ("hello", "QX:.", "http://example.com"):
            with self.assertRaises(ValueError):
                q.parse_frame(bad)

    def test_wire_format_is_stable(self):
        # Changing these breaks compatibility between qrxfer copies on two machines.
        self.assertEqual(q.repair_indices(0xDEADBEEF, 1000, 500, 8), [155, 43, 108, 21, 210, 267, 481, 382])
        self.assertEqual(q._SplitMix64(42).next64(), 13679457532755275413)
        self.assertEqual(q.FRAME_OVERHEAD, 21)


class FountainTest(unittest.TestCase):
    def transfer(self, k, block, loss, start, seed):
        rnd = random.Random(seed)
        blob = bytes(rnd.getrandbits(8) for _ in range(k * block - rnd.randrange(block)))
        enc = q.FountainEncoder(blob, block, rnd.getrandbits(32))
        dec = None
        seq = start
        while dec is None or not dec.complete:
            self.assertLess(seq - start, 4 * k + 60, "decoder did not converge")
            frame = q.parse_frame(q.pack_frame(enc.frame(seq)))
            seq += 1
            if rnd.random() < loss:
                continue
            dec = dec or q.FountainDecoder.for_frame(frame)
            dec.add(frame)
            dec.add(frame)  # duplicates (same code captured twice) are ignored
        self.assertEqual(dec.result(), blob)
        return dec.received

    def test_scenarios(self):
        for k in (1, 2, 3, 10, 64, 300):
            for loss, start in ((0.0, 0), (0.2, 0), (0.1, k // 2), (0.1, 3 * k)):
                received = self.transfer(k, 40, loss, start, seed=k * 31 + start)
                if k >= 64:
                    self.assertLess(received, k * 1.1 + 5)

    def test_other_transfer_rejected(self):
        a = q.FountainEncoder(b"a" * 100, 10, 1)
        b = q.FountainEncoder(b"b" * 100, 10, 2)
        dec = q.FountainDecoder.for_frame(a.frame(0))
        with self.assertRaises(ValueError):
            dec.add(b.frame(0))


class CryptoTest(unittest.TestCase):
    def test_roundtrip_wrong_pass_and_tamper(self):
        data = os.urandom(5000)
        blob = q.encrypt(data, "secret")
        self.assertEqual(q.decrypt(blob, "secret"), data)
        self.assertNotIn(data[:64], blob)
        with self.assertRaises(q.WrongPassphrase):
            q.decrypt(blob, "Secret")
        for pos in (10, len(blob) // 2, len(blob) - 1):
            tampered = bytearray(blob)
            tampered[pos] ^= 1
            with self.assertRaises(q.QrxferError):
                q.decrypt(bytes(tampered), "secret")
        self.assertNotEqual(q.encrypt(data, "secret"), blob)  # fresh salt every time

    def test_pbkdf2_fallback_and_bounds(self):
        salt = os.urandom(16)
        k1 = q._derive_keys("pw", q.KDF_PBKDF2, 10, 0, 0, salt)
        self.assertEqual(len(k1[0]), 32)
        with self.assertRaises(q.QrxferError):
            q._derive_keys("pw", q.KDF_SCRYPT, 30, 8, 1, salt)  # absurd memory request
        with self.assertRaises(q.QrxferError):
            q._derive_keys("pw", 9, 0, 0, 0, salt)
        with self.assertRaises(q.QrxferError):
            q.decrypt(b"not a container at all, definitely not" * 3, "pw")

    def test_passphrase_normalisation(self):
        code = q.generate_passphrase()
        self.assertRegex(code, r"^[0-9A-HJKMNP-TV-Z]{4}(-[0-9A-HJKMNP-TV-Z]{4}){3}$")
        self.assertEqual(q.normalize_passphrase(code.lower().replace("-", " ")), code)
        self.assertEqual(q.normalize_passphrase("abcd efgh ijkl mnop"), "ABCD-EFGH-1JK1-MN0P")
        self.assertEqual(q.normalize_passphrase("  my Pass phrase  "), "my Pass phrase")


class ArchiveTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)

    def write(self, rel, data=b"x"):
        path = os.path.join(self.tmp, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(data)
        return path

    def test_pack_unpack_without_overwriting(self):
        a = self.write("a/report.txt", b"one")
        b = self.write("b/report.txt", b"two")
        d = os.path.dirname(self.write("文档 dir/sub/x.bin", os.urandom(300)))
        archive, nfiles, nbytes = q.pack_paths([a, b, os.path.dirname(d)])
        self.assertEqual((nfiles, nbytes), (3, 306))
        out = os.path.join(self.tmp, "out")
        self.write("out/report.txt", b"existing")
        saved = sorted(os.path.basename(p) for p in q.unpack_archive(archive, out))
        self.assertEqual(saved, ["report (1).txt", "report (2).txt", "文档 dir"])
        with open(os.path.join(out, "report.txt"), "rb") as fh:
            self.assertEqual(fh.read(), b"existing")
        self.assertEqual(sorted(os.listdir(out)), ["report (1).txt", "report (2).txt", "report.txt", "文档 dir"])

    def test_missing_input(self):
        with self.assertRaises(q.QrxferError):
            q.pack_paths([os.path.join(self.tmp, "nope")])

    def test_unsafe_archive_rejected(self):
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tar:
            info = tarfile.TarInfo("../escape.txt")
            info.size = 3
            tar.addfile(info, io.BytesIO(b"bad"))
        out = os.path.join(self.tmp, "out")
        with self.assertRaises((q.QrxferError, tarfile.TarError)):
            q.unpack_archive(buf.getvalue(), out)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "escape.txt")))


class QrEncoderTest(unittest.TestCase):
    def test_versions_and_capacity(self):
        for level in q.QR_LEVELS:
            for version in range(1, 41):
                ec, b1, d1, b2 = q.QR_EC_BLOCKS[version, level]
                total = b1 * (d1 + ec) + b2 * (d1 + 1 + ec)
                tpl = q._qr_template(version)
                self.assertEqual(total * 8, len(tpl.positions) - len(tpl.positions) % 8, (version, level))
        self.assertEqual(q.qr_version_for(4296, "L"), 40)
        self.assertIsNone(q.qr_version_for(4297, "L"))

    @unittest.skipUnless(HAVE_SEGNO, "segno not installed")
    def test_matches_segno(self):
        import segno
        rnd = random.Random(3)
        for length in (1, 2, 30, 150, 700, 1300, 2600, 4000):
            for level in q.QR_LEVELS:
                if q.qr_version_for(length, level) is None:
                    continue
                text = "".join(rnd.choice(q.B45_ALPHABET) for _ in range(length))
                mask = rnd.randrange(8)
                version, size, rows = q.qr_encode(text, level, mask=mask)
                ref = segno.make_qr(text, error=level.lower(), mode="alphanumeric", mask=mask, boost_error=False)
                self.assertEqual(version, ref.version)
                self.assertEqual(rows, [sum(1 << x for x, v in enumerate(r) if v) for r in ref.matrix])

    @unittest.skipUnless(HAVE_NUMPY and available_decoders(), "numpy or QR decoder missing")
    def test_decoders_read_rendered_grid(self):
        import numpy as np
        enc = q.FountainEncoder(os.urandom(5000), 600, 5)
        stream = q.FrameStream(enc, "M")
        codes = stream.take(6)
        expected = {q.pack_frame(enc.frame(i)) for i in range(6)}
        for dec in available_decoders():
            # zbar's line-skipping first pass misses 2 px modules; its fallback must not.
            for px in (5, 2, 5, 2):
                img = q._render_codes_image(np, codes, stream.size, 3, px)
                self.assertEqual({t for t, _ in dec.decode(img)}, expected, (dec.name, px))

    @unittest.skipUnless(HAVE_NUMPY and available_decoders(), "numpy or QR decoder missing")
    def test_torn_capture_keeps_intact_rows(self):
        # A terminal caught mid-redraw: the top of the first code row already shows the
        # next frame. The other two rows must still decode (zbar alone returned nothing).
        import numpy as np
        enc = q.FountainEncoder(os.urandom(30000), 481, 8)
        stream = q.FrameStream(enc, "L")
        old = q._render_codes_image(np, stream.take(9), stream.size, 3, 4)
        new = q._render_codes_image(np, stream.take(9), stream.size, 3, 4)
        torn = np.vstack([new[:150], old[150:]])
        torn = np.ascontiguousarray(np.pad(torn, ((0, 0), (20, 30)), constant_values=30))  # dark sides
        self.assertEqual(len(q._code_bands(torn)), 3)
        expected = {q.pack_frame(enc.frame(i)) for i in range(3, 9)}
        for dec in available_decoders():
            self.assertEqual({t for t, _ in dec.decode(torn)}, expected, dec.name)

    @unittest.skipUnless(HAVE_NUMPY and available_decoders(), "numpy or QR decoder missing")
    def test_fixed_mask_reads_zero_padding(self):
        # A short last block is mostly zero padding: the one low-entropy frame.
        import numpy as np
        enc = q.FountainEncoder(b"\x01", 1211, 9)
        stream = q.FrameStream(enc, "L")
        self.assertEqual(stream.version, 25)
        img = q._render_codes_image(np, stream.take(1), stream.size, 1, 2)
        for dec in available_decoders():
            self.assertEqual([t for t, _ in dec.decode(img)], [q.pack_frame(enc.frame(0))], dec.name)

    def test_pgm_and_terminal_orientation(self):
        version, size, rows = q.qr_encode("QX:HELLO.", "L")
        pgm = q.qr_to_pgm(rows, size)
        pixels = pgm.split(b"\n", 1)[1]
        for y in (0, 7, size - 1):
            for x in (0, 3, size - 1):
                self.assertEqual(pixels[y * size + x] == 0, bool(rows[y] >> x & 1))
        self.assertEqual(self.terminal_modules(rows, size, utf8=True), [[r >> x & 1 for x in range(size)] for r in rows])
        self.assertEqual(self.terminal_modules(rows, size, utf8=False), [[r >> x & 1 for x in range(size)] for r in rows])

    def terminal_modules(self, rows, size, utf8):
        """Render one code in a terminal and read the modules back from the text."""
        columns, lines = (size + 20, (size + 9) // 2 + 3) if utf8 else (2 * size + 21, size + 10)
        out = q.render_terminal([rows], size, 1, 1, columns, lines, utf8)
        self.assertEqual(len(out), lines)
        grid = []
        if utf8:
            top = {" ": 0, "\u2584": 0, "\u2580": 1, "\u2588": 1}
            bottom = {" ": 0, "\u2584": 1, "\u2580": 0, "\u2588": 1}
            for line in out:
                self.assertEqual(len(line), columns)
                grid.append([top[c] for c in line])
                grid.append([bottom[c] for c in line])
        else:
            import re
            for line in out:
                cells = []
                for colour, spaces in re.findall(r"\x1b\[(40|47;107)m( *)", line):
                    cells += [1 if colour == "40" else 0] * len(spaces)
                self.assertEqual(len(cells), columns)
                grid.append(cells[0::2])
        ys = [i for i, r in enumerate(grid) if any(r)]
        xs = [i for i in range(len(grid[0])) if any(r[i] for r in grid)]
        return [row[xs[0]:xs[0] + size] for row in grid[ys[0]:ys[0] + size]]


class LayoutTest(unittest.TestCase):
    def test_plan_grid(self):
        cols, rows, px = q.plan_grid(1280, 780, 85, 4, 12)
        self.assertEqual((cols, rows, px), (3, 2, 4))
        self.assertEqual(q.plan_grid(400, 300, 85, 4, 12)[:2], (1, 1))  # too small: one code, smaller px
        self.assertEqual(q.plan_grid(4000, 2400, 85, 4, 12, grid=(2, 1)), (2, 1, 21))

    def test_region_tracker(self):
        t = q.RegionTracker(1000, 800)
        box, full = t.next_box()
        self.assertTrue(full)
        t.update([(100, 100, 300, 300), (350, 100, 550, 300)])
        box, full = t.next_box()
        self.assertFalse(full)
        x, y, w, h = box
        self.assertTrue(x < 100 and y < 100 and x + w > 550 and y + h > 300)

    def test_block_size_fits_version(self):
        for level in q.QR_LEVELS:
            for version in range(5, 41):
                block = q.max_block_size(version, level)
                for size, same in ((block, True), (block + 1, False)):
                    text = q.pack_frame(q.FountainEncoder(b"x" * size, size, 1).frame(0))
                    self.assertEqual(q.qr_version_for(len(text), level) == version, same, (version, level))
        self.assertEqual(q.max_block_size(17, "L"), 601)  # so medium (600) is version 17-L

    def test_auto_density(self):
        # A 508x125 terminal (the real VNC run): 6x3 version-15 codes carry 8.7 KB per
        # screen where 5x2 at the old fixed 600 bytes carried 6 KB.
        block = q.choose_block_size(508, 250, 1, 24, None, "L")
        self.assertEqual(block, q.max_block_size(15, "L"))
        self.assertEqual(q.plan_grid(508, 250, 77, 1, 24), (6, 3, 1))
        # An explicit grid gets the largest code that still fits it: 6 up + 6 down.
        self.assertEqual(q.choose_block_size(508, 250, 1, 24, (6, 2), "L"), q.max_block_size(15, "L"))
        self.assertEqual(q.choose_block_size(1280, 780, 4, 24, None, "L"), q.max_block_size(17, "L"))
        self.assertIsNone(q.choose_block_size(80, 46, 1, 24, None, "L"))  # too small for any


class FakeGrabber:
    """Pretends to be the screen: a dark desktop with the sender's codes on it."""

    def __init__(self, np, stream, cols, px, every=3):
        self.np, self.stream, self.cols, self.px, self.every = np, stream, cols, px, every
        self.width, self.height = 1600, 1000
        self.calls = 0
        self.screen = None

    def grab(self, left, top, width, height):
        if self.calls % self.every == 0:  # the screen changes every few captures
            codes = self.stream.take(self.cols * 2)
            tile = q._render_codes_image(self.np, codes, self.stream.size, self.cols, self.px)
            self.screen = self.np.full((self.height, self.width), 40, self.np.uint8)
            self.screen[150:150 + tile.shape[0], 230:230 + tile.shape[1]] = tile
        self.calls += 1
        return self.np.ascontiguousarray(self.screen[top:top + height, left:left + width])


@unittest.skipUnless(HAVE_NUMPY and available_decoders(), "numpy or QR decoder missing")
class ReceivePipelineTest(unittest.TestCase):
    def test_collect_decrypt_extract(self):
        import numpy as np
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        src = os.path.join(tmp, "payload.bin")
        data = os.urandom(20000)
        with open(src, "wb") as fh:
            fh.write(data)
        archive, _, _ = q.pack_paths([src])
        blob = q.encrypt(archive, "pw")
        for threads in (1, 3):
            enc = q.FountainEncoder(blob, 500, 4242 + threads)
            stream = q.FrameStream(enc, "L")
            stream.seq = 17  # receiver started late
            grabber = FakeGrabber(np, stream, cols=3, px=3)
            progress = q.Progress(io.StringIO())
            sess = q.collect_from_screen(grabber, available_decoders()[0], progress=progress,
                                         timeout=60, threads=threads)
            self.assertEqual(sess.result(), blob)
        saved = q.unpack_archive(q.decrypt(sess.result(), "pw"), os.path.join(tmp, "out"))
        with open(saved[0], "rb") as fh:
            self.assertEqual(fh.read(), data)


class CliTest(unittest.TestCase):
    def test_genpass(self):
        out = io.StringIO()
        old, sys.stdout = sys.stdout, out
        try:
            self.assertEqual(q.main(["genpass"]), 0)
        finally:
            sys.stdout = old
        self.assertRegex(out.getvalue().strip(), r"^\w{4}-\w{4}-\w{4}-\w{4}$")

    def test_decrypt_command(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        src = os.path.join(tmp, "note.txt")
        with open(src, "w") as fh:
            fh.write("hi")
        archive, _, _ = q.pack_paths([src])
        qxf = os.path.join(tmp, "t.qxf")
        with open(qxf, "wb") as fh:
            fh.write(q.encrypt(archive, "pw"))
        pf = os.path.join(tmp, "pass")
        with open(pf, "w") as fh:
            fh.write("pw\n")
        out = os.path.join(tmp, "out")
        old, sys.stdout = sys.stdout, io.StringIO()
        try:
            self.assertEqual(q.main(["decrypt", qxf, "-o", out, "-p", pf]), 0)
        finally:
            sys.stdout = old
        with open(os.path.join(out, "note.txt")) as fh:
            self.assertEqual(fh.read(), "hi")


if __name__ == "__main__":
    unittest.main()
