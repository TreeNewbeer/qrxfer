"""Unit and pipeline tests for qrxfer (python3 -m unittest discover -s tests)."""

import hashlib
import importlib.util
import io
import os
import random
import shutil
import sys
import tarfile
import tempfile
import time
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


class GridFormatTest(unittest.TestCase):
    def test_wire_format_is_stable(self):
        # Changing these breaks compatibility between qrxfer copies on two machines.
        self.assertEqual(q.plan_cells(2560, 1333, 3), (849, 439))
        self.assertEqual(q.grid_block_size(849), 297)
        self.assertEqual(q.grid_whitening(4, 2).hex(), "81b3d07deb4eb7ae")
        self.assertEqual(q.grid_row_digits(bytes(range(37)), 99, 1),
                         "21354774664077066637547350227415465572125712571046337573162"
                         "1430342700641265004755261375036701753427")
        frame = q.FountainEncoder(bytes(range(200)), 100, 0xC0FFEE).frame(1)
        raw = q.frame_bytes(frame)
        self.assertEqual(raw[:21].hex(), "0100c0ffee000000c8006400000001000164656667")
        self.assertEqual(q.b45decode(q.pack_frame(frame)[3:-1]), raw)  # QR codes carry the same bytes
        self.assertEqual(q.parse_frame_bytes(raw).payload, frame.payload)

    def test_stream_order_and_shares_of_repair_work(self):
        # After the first pass, each repair screen is followed by one repeat screen (three
        # for big transfers), and every screen makes the same share of the next repair one.
        for big in (False, True):
            enc = q.FountainEncoder(os.urandom(1000 * 8 - 3), 8, 5)
            stream = q.GridStream(enc, 99, 30)
            if big:
                stream.repeats = 3
            self.assertEqual(stream.repeats, 3 if big else 1)
            screens, made = [], []
            while len(screens) < 34 + 4 * (stream.repeats + 1):
                before = stream._repair
                screens.append([q.parse_frame_bytes(raw).seq for raw in stream.take(30)])
                made.append(stream._repair - before)
            self.assertEqual(sum(screens[:34], []), list(range(1000)) + list(range(1000, 1020)))
            after, repair, repeat = screens[34:], 1020, 0
            for i, seqs in enumerate(after):
                if i % (stream.repeats + 1):
                    self.assertEqual(seqs, [stream.repeat_block(repeat + j) for j in range(30)])
                    repeat += 30
                else:
                    self.assertEqual(seqs, list(range(repair, repair + 30)))
                    repair += 30
            self.assertEqual(made[:34], [0] * 33 + [20])
            cycle = stream.repeats + 1
            made[34] -= 30  # nothing was made ahead of the first repair screen
            share = -(-30 // cycle)
            self.assertTrue(all(30 - (cycle - 1) * share <= m <= share for m in made[34:]), made)
            self.assertEqual({sum(made[i:i + cycle]) for i in range(34, len(made) - cycle + 1, cycle)}, {30})
        for k, repeats in ((q.GridStream.MANY_REPEATS_K - 1, 1), (q.GridStream.MANY_REPEATS_K, 3)):
            self.assertEqual(q.GridStream(q.FountainEncoder(bytes(k), 1, 5), 99, 30).repeats, repeats)

    def test_repeat_rounds_move_blocks_to_other_rows(self):
        # A band of rows covered on the receiver (a dock over the viewer) must hide other
        # blocks on each round of repeats than in the first pass, where block b sat in row
        # b % rows: every round shows each block once, shifted by its own number of rows.
        for k, rows in ((1000, 30), (2887084 // 50, 439)):
            stream = q.GridStream(q.FountainEncoder(bytes(k), 1, 5), 99, rows)
            shifts = []
            for c in range(3):
                blocks = [stream.repeat_block(n) for n in range(c * k, (c + 1) * k)]
                self.assertEqual(sorted(blocks), list(range(k)))
                moved = {(b - n) % rows for n, b in enumerate(blocks, c * k) if b >= rows}
                self.assertEqual(len(moved), 1, (k, rows, c))
                shifts.append(moved.pop())
            self.assertNotIn(0, shifts)
            self.assertEqual(len(set(shifts)), 3)
            if rows == 439:  # a full 2560x1351 screen: the 8 rows a dock hid came back elsewhere
                hidden = {b for b in range(rows, k) if b % rows >= rows - 8}
                for c, shift in enumerate(shifts):
                    self.assertFalse({b for b in hidden if (b - shift) % rows >= rows - 8}, c)

    def test_requested_blocks_come_next_last_first(self):
        # A request is shown after the first pass, on screens of its own and last block
        # first, filling up its last screen with the same blocks again; then the stream
        # goes on exactly as it would have.
        def screens(stream, n):
            return [[q.parse_frame_bytes(raw).seq for raw in stream.take(30)] for _ in range(n)]

        enc = q.FountainEncoder(os.urandom(1000 * 8), 8, 5)
        plain, asked = q.GridStream(enc, 99, 30), q.GridStream(enc, 99, 30)
        expected = screens(plain, 40)
        asked.request([(10, 20), (500, 545)])  # during the first pass: served after it
        self.assertEqual(asked.phase(), "first pass 0%")
        got = screens(asked, 34)
        self.assertEqual(asked.phase(), "requested blocks, 55 left")
        got += screens(asked, 2)
        self.assertEqual(got[34], list(range(544, 514, -1)))
        self.assertEqual(got[35], list(range(514, 499, -1)) + list(range(19, 9, -1)) + list(range(544, 539, -1)))
        self.assertEqual(asked.phase(), "repair + repeats")
        got += screens(asked, 6)
        self.assertEqual(got[:34] + got[36:], expected)
        asked.request([(7, 8)])
        asked.request([(3, 4)])  # replaces the one before
        self.assertEqual(screens(asked, 1), [[3] * 30])
        asked.request([])
        self.assertEqual(asked.phase(), "repair + repeats")

    def test_painter_colours_and_ring(self):
        painter = q.GridPainter(3, 1, 2, lambda r, g, b: bytes((r, g, b, 0)))
        rows = [bytes(1)]
        digits = q.grid_row_digits(rows[0], 3, 0)
        px = painter.paint(rows)
        self.assertEqual((painter.width, painter.height, len(px)), (10, 6, 10 * 6 * 4))

        def cell(x, y):
            o = (2 * y * 10 + 2 * x) * 4
            return px[o:o + 3]
        white, black = b"\xff\xff\xff", b"\x00\x00\x00"
        self.assertEqual([cell(x, 0) for x in range(5)], [white, black, white, black, white])
        self.assertEqual([cell(0, 1), cell(4, 1), cell(0, 2)], [black, black, white])
        for x, d in enumerate(digits, 1):
            s = int(d)
            self.assertEqual(cell(x, 1), bytes((255 * (s >> 2), 255 * (s >> 1 & 1), 255 * (s & 1))))


def render_grid_screen(np, frames, cols, rows, px=3):
    """A grid as GridPainter draws it, in BGRX bytes like XGetImage returns."""
    painter = q.GridPainter(cols, rows, px, lambda r, g, b: bytes((b, g, r, 0)))
    return np.frombuffer(painter.paint(frames), np.uint8).reshape(painter.height, painter.width, 4).copy()


def place_on_desktop(np, grid, top=40, left=70):
    """Put a grid on a busy desktop: grey wallpaper with saturated icons and black margins."""
    rnd = np.random.RandomState(1)
    h, w = grid.shape[:2]
    screen = np.full((h + top + 90, w + left + 110, 4), 128, np.uint8)
    screen[..., 0] = 200  # bluish wallpaper
    for _ in range(12):  # small coloured icons
        y, x = rnd.randint(0, screen.shape[0] - 24), rnd.randint(0, screen.shape[1] - 24)
        screen[y:y + 24, x:x + 24, :3] = rnd.randint(0, 2, 3) * 255
    screen[top - 6:top + h + 6, left - 6:left + w + 6] = 0
    screen[top:top + h, left:left + w] = grid
    return screen


@unittest.skipUnless(HAVE_NUMPY, "numpy missing")
class RequestTest(unittest.TestCase):
    def test_wire_format_is_stable(self):
        # Changing these breaks requests between qrxfer copies on two machines.
        keys = q.encode_request(0xC0FFEE, [(0, 1), (5, 9), (300, 301)])
        self.assertEqual(keys, "xtgwwve0043hi30kniotsd")
        self.assertEqual(q.parse_request(keys[1:], 0xC0FFEE, 301), [(0, 1), (5, 9), (300, 301)])

    def test_keys_roundtrip_and_damage(self):
        rnd = random.Random(4)
        # No key may also be one of the sender's commands: q quits, space pauses, +/- speed.
        typed = set(q._REQUEST_LAST + q._REQUEST_MORE + q.REQUEST_KEY)
        self.assertEqual(len(typed), 33)
        self.assertFalse(typed & set("qQ +=-_\r\n\x1b"))
        for _ in range(50):
            cuts = sorted(rnd.sample(range(1, 3000000), 2 * rnd.randrange(0, 40)))
            ranges = list(zip(cuts[::2], cuts[1::2]))
            tid = rnd.getrandbits(32)
            keys = q.encode_request(tid, ranges)
            self.assertEqual(keys[0], q.REQUEST_KEY)
            self.assertEqual(q.parse_request(keys[1:], tid, 3000000), ranges)
            for bad in (keys[1:-1], keys[2:], keys[1:] + "0"):
                with self.assertRaises(ValueError):
                    q.parse_request(bad, tid, 3000000)
            pos = rnd.randrange(1, len(keys))
            damaged = keys[1:pos] + q._REQUEST_LAST[(q._REQUEST_LAST.find(keys[pos]) + 1) % 16] + keys[pos + 1:]
            with self.assertRaises(ValueError):  # a different key, or a digit of the wrong kind
                q.parse_request(damaged, tid, 3000000)
            with self.assertRaises(ValueError):
                q.parse_request(keys[1:], tid ^ 1, 3000000)
            if ranges:
                with self.assertRaises(ValueError):
                    q.parse_request(keys[1:], tid, ranges[-1][1] - 1)
        for bad in ("", "0", "x0", "g"):
            with self.assertRaises(ValueError):
                q.parse_request(bad, 0, 10)

    def test_fit_request_merges_the_smallest_gaps(self):
        rnd = random.Random(5)
        start, ranges = 0, []
        for _ in range(3000):
            start += rnd.randrange(1, 2000)
            ranges.append((start, start + rnd.randrange(1, 440)))
            start = ranges[-1][1]
        keys, merged = q.fit_request(7, ranges, 4000)
        self.assertLessEqual(len(keys), 4000)
        self.assertGreater(len(keys), 3000)
        self.assertEqual(q.parse_request(keys[1:], 7, start), merged)
        covered = set()
        for a, b in merged:
            covered.update(range(a, b))
        self.assertTrue(all(set(range(a, b)) <= covered for a, b in ranges))
        kept = [b[0] - a[1] for a, b in zip(merged, merged[1:])]
        lost = sorted(b[0] - a[1] for a, b in zip(ranges, ranges[1:]) if b[0] - a[1] not in kept)
        self.assertLessEqual(lost[-1], min(kept))  # only the smallest gaps were merged away
        self.assertEqual(q.fit_request(7, ranges[:5], 4000)[1], ranges[:5])
        self.assertEqual(q.fit_request(7, ranges, 20)[1], [(ranges[0][0], start)])

    def test_sender_takes_requests_from_keys(self):
        class Opts:
            fps = 10.0

        enc = q.FountainEncoder(os.urandom(1000 * 8), 8, 0x1234)
        stream = q.GridStream(enc, 99, 30)
        state = q._SendState(stream, Opts())
        for key in q.encode_request(0x1234, [(10, 20)]) + "\r":
            self.assertTrue(state.handle_key(key))
        self.assertEqual(stream._wanted, ([(10, 20)], 10))
        for key in q.encode_request(0x1234, [(30, 40)])[:-1] + "\r":  # a key lost on the way
            state.handle_key(key)
        for key in list(q.encode_request(0x4321, [(30, 40)])) + ["Return"]:  # another transfer; a Tk keysym
            state.handle_key(key)
        self.assertEqual((state.requests, state.bad_requests), (1, 2))
        self.assertIn("requests 1 (2 bad)", state.status(30))
        for key in "x12 ":  # any other key ends a request and does its own thing
            state.handle_key(key)
        self.assertTrue(state.paused)
        self.assertEqual(state.bad_requests, 3)
        self.assertFalse(state.handle_key("q"))
        self.assertEqual(stream._wanted, ([(10, 20)], 10))
        qr = q._SendState(q.FrameStream(enc, "L"), Opts())  # QR codes take no requests
        self.assertTrue(qr.handle_key("x"))
        self.assertIsNone(qr.typed)


class GridReadTest(unittest.TestCase):
    CH = (2, 1, 0)

    def setUp(self):
        import numpy as np
        self.np = np
        self.cols, self.rows = q.plan_cells(1200, 640, 3)
        self.enc = q.FountainEncoder(os.urandom(60000), q.grid_block_size(self.cols), 99)
        self.stream = q.GridStream(self.enc, self.cols, self.rows)

    def read(self, screen, expect_rows=None):
        geo = q.locate_grid(self.np, screen, self.CH)
        self.assertIsNotNone(geo)
        self.assertEqual((len(geo.xs), len(geo.ys)), (self.cols, expect_rows or self.rows))
        return q.read_grid_rows(self.np, screen, geo.xs, geo.ys, self.CH)

    def test_paint_locate_read(self):
        frames = self.stream.take(self.rows)
        screen = place_on_desktop(self.np, render_grid_screen(self.np, frames, self.cols, self.rows))
        self.assertEqual(self.read(screen), frames)
        screen[10:32] = (230, 80, 0, 0)  # a saturated blue title bar 8 px above the grid
        self.assertEqual(self.read(screen), frames)
        self.assertIsNone(q.locate_grid(self.np, place_on_desktop(self.np, screen[:0, :0]), self.CH))

    def test_spare_last_column_located(self):
        # 849 columns carry 318 bytes and 3 spare bits: the whole last column is spare,
        # but whitening colours it like the others, so the grid is found with all of it.
        enc = q.FountainEncoder(bytes(random.Random(4).getrandbits(8) for _ in range(9000)),
                                q.grid_block_size(849), 4)
        frames = q.GridStream(enc, 849, 41).take(41)
        geo = q.locate_grid(self.np, place_on_desktop(self.np, render_grid_screen(self.np, frames, 849, 41)),
                            self.CH)
        self.assertEqual((len(geo.xs), len(geo.ys)), (849, 41))

    def test_zero_padding_still_located(self):
        # A short last block is mostly zero padding; whitening keeps its rows coloured.
        enc = q.FountainEncoder(b"\x01", q.grid_block_size(self.cols), 3)
        frames = [q.frame_bytes(enc.frame(0))] * self.rows
        screen = place_on_desktop(self.np, render_grid_screen(self.np, frames, self.cols, self.rows))
        self.assertEqual(self.read(screen), frames)

    def test_covered_rings_and_torn_rows(self):
        np = self.np
        a, b = self.stream.take(self.rows), self.stream.take(self.rows)
        new, old = render_grid_screen(np, b, self.cols, self.rows), render_grid_screen(np, a, self.cols, self.rows)
        torn = old.copy()
        torn[:150] = new[:150]                    # a redraw that reached data row 48 (y 147..149)
        torn[150:153, :600] = new[150:153, :600]  # ... and half of row 49
        screen = place_on_desktop(np, torn)
        screen[-160:-40, -260:-60] = (90, 90, 90, 0)  # a widget over the bottom right corner
        rows = self.read(screen)
        self.assertEqual(rows[:49], b[:49])
        self.assertNotIn(rows[49], (a[49], b[49]))
        with self.assertRaises(ValueError):
            q.parse_frame_bytes(rows[49])
        self.assertEqual(rows[50:150], a[50:150])
        # A mouse pointer over the left ring: the right ring gives the rows instead.
        screen = place_on_desktop(np, new)
        screen[40 + 90:40 + 110, 66:84] = (230, 230, 230, 0)
        self.assertEqual(self.read(screen)[:28], b[:28])

    def test_half_drawn_first_screen(self):
        # A VNC viewer draws the first screen from the top down; a capture half-way must
        # not fix a grid with too few rows.
        np = self.np
        frames = self.stream.take(self.rows)
        grid = render_grid_screen(np, frames, self.cols, self.rows)
        full = place_on_desktop(np, grid)
        half = full.copy()
        half[40 + 300:40 + grid.shape[0]] = (200, 128, 128, 0)  # still the wallpaper
        geo = q.locate_grid(np, half, self.CH)
        self.assertFalse(geo.complete)
        self.assertEqual(len(geo.ys), 99)
        reader = q.GridReader(np, self.CH)
        box = (0, 0, full.shape[1], full.shape[0])
        self.assertEqual(len(reader.read(half, box, False)), 99)
        self.assertEqual(len(reader.read(full, box, False)), 99)  # not searched again yet
        reader.shared["searched"] -= reader.RECHECK_EVERY
        self.assertEqual([f.seq for f, _ in reader.read(full, box, False)],
                         [q.parse_frame_bytes(raw).seq for raw in frames])
        self.assertTrue(reader.shared["geometry"].complete)

    def test_scaled_views(self):
        np = self.np
        frames = self.stream.take(self.rows)
        grid = render_grid_screen(np, frames, self.cols, self.rows)
        for scale in (1.25, 0.8, 4 / 3.0):
            h, w = grid.shape[:2]
            ys = (np.arange(int(h * scale)) / scale).astype(int)
            xs = (np.arange(int(w * scale)) / scale).astype(int)
            scaled = np.ascontiguousarray(grid[ys][:, xs])
            self.assertEqual(self.read(place_on_desktop(np, scaled)), frames, scale)


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

    def test_many_blocks_fast(self):
        # A grid carries tens of thousands of blocks: a 3% gap after the first pass must
        # close quickly, and a receiver started late must catch up through the repeated
        # systematic frames.
        blob = os.urandom(9000 * 16 - 7)
        enc = q.FountainEncoder(blob, 16, 21)
        self.assertEqual(q.grid_block_size(99), 16)
        rnd = random.Random(5)
        for late in (False, True):
            stream = q.GridStream(enc, 99, 300)
            if late:
                stream.take(300)
                stream.seq = enc.k + 7
            dec, shown, started = None, 0, time.monotonic()
            while dec is None or not dec.complete:
                for raw in stream.take(300):
                    if rnd.random() < 0.03:
                        continue
                    frame = q.parse_frame_bytes(raw)
                    dec = dec or q.FountainDecoder.for_frame(frame)
                    dec.add(frame)
                shown += 300
                self.assertLess(shown, 3 * enc.k, late)
            self.assertEqual(dec.result(), blob)
            self.assertLess(time.monotonic() - started, 30, late)
            if not late:
                self.assertLess(shown, 1.2 * enc.k)

    def test_rank_matches_plain_elimination(self):
        # Substituting known blocks, columns and dropping equations over determined blocks
        # must keep `rank` exact: compare with plain elimination over block bitmasks, for a
        # first pass with losses, then repair frames mixed with repeated systematic ones.
        for seed in range(4):
            rnd = random.Random(seed)
            blob = os.urandom(6000 * 4 - 3)
            enc = q.FountainEncoder(blob, 4, seed)
            dec = q.FountainDecoder.for_frame(enc.frame(0))
            pivots = {}
            seqs = [s for s in range(enc.k) if rnd.random() > 0.02]
            repair = enc.k
            while not dec.complete:
                if seqs:
                    seq = seqs.pop(0)
                elif rnd.random() < 0.3:
                    seq = rnd.randrange(enc.k)
                else:
                    seq, repair = repair, repair + 1
                frame = enc.frame(seq)
                mask = 1 << seq if seq < enc.k else sum(
                    1 << i for i in q.repair_indices(frame.tid, seq, enc.k, frame.degree))
                while mask and mask.bit_length() - 1 in pivots:
                    mask ^= pivots[mask.bit_length() - 1]
                if mask:
                    pivots[mask.bit_length() - 1] = mask
                rank = dec.rank
                self.assertEqual(dec.add(frame), len(pivots) > rank)
                self.assertEqual(dec.rank, len(pivots))
                missing = [c for c, b in enumerate(dec._blocks) if b not in dec._known]
                self.assertEqual(dec._open, len(missing))
                self.assertLess(repair, 2 * enc.k)
            self.assertEqual(dec.result(), blob)

    def test_repair_only_after_heavy_loss(self):
        # Skipping heavy repair frames must stop when no systematic frames follow (QR codes).
        enc = q.FountainEncoder(os.urandom(300 * 20), 20, 3)
        dec = q.FountainDecoder.for_frame(enc.frame(0))
        dec.SKIP_MIN_K = 0
        for seq in range(0, enc.k, 2):
            dec.add(enc.frame(seq))
        seq = enc.k
        while not dec.complete:
            dec.add(enc.frame(seq))
            seq += 1
            self.assertLess(seq, enc.k + q.FountainDecoder.SKIP_WINDOW + 4 * enc.k)
        self.assertEqual(dec.received, enc.k // 2 + seq - enc.k - q.FountainDecoder.SKIP_WINDOW)

    def test_repair_skipped_while_repeats_bring_many_missing_blocks(self):
        # Repair frames wait until at most min(k/16, SKIP_MISSING) blocks are missing, as
        # long as new systematic frames keep coming within max(1024, k/64) repair frames.
        blob = os.urandom(20000 * 4 - 1)
        enc = q.FountainEncoder(blob, 4, 9)
        dec = q.FountainDecoder.for_frame(enc.frame(0))
        dec.SKIP_MISSING = 600
        self.assertEqual(dec._skip_window, q.FountainDecoder.SKIP_WINDOW)
        self.assertEqual(q.FountainDecoder(0, 1 << 17, 1)._skip_window, 2048)
        missing = list(range(0, enc.k, 10))  # 2000 blocks; k/16 is 1250
        for seq in range(enc.k):
            if seq % 10:
                dec.add(enc.frame(seq))
        repair = enc.k
        while len(missing) > 600:
            for _ in range(500):
                self.assertFalse(dec.add(enc.frame(repair)))
                repair += 1
            for _ in range(100):
                dec.add(enc.frame(missing.pop()))
        self.assertEqual((dec.rank, dec.received), (enc.k - 600, enc.k - 600))
        self.assertEqual(repair, enc.k + 14 * 500)
        self.assertTrue(dec.add(enc.frame(repair)))  # 600 missing: no longer skipped
        while not dec.complete:
            repair += 1
            dec.add(enc.frame(repair))
        self.assertEqual(dec.result(), blob)
        self.assertLess(repair - enc.k - 14 * 500, 800)  # 668

    def test_missing_ranges(self):
        enc = q.FountainEncoder(os.urandom(100 * 4), 4, 1)
        dec = q.FountainDecoder.for_frame(enc.frame(0))
        self.assertEqual(dec.missing_ranges(), [(0, 100)])
        for seq in list(range(10)) + list(range(20, 99)):
            dec.add(enc.frame(seq))
        self.assertEqual(dec.missing_ranges(), [(10, 20), (99, 100)])
        dec.add(enc.frame(99))
        self.assertEqual(dec.missing_ranges(), [(10, 20)])

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
        self.assertEqual(blob[:4], b"QXF2")
        self.assertEqual(len(blob), len(data) + q._CONTAINER_HDR.size + q._TAG_LEN)
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

    def test_keystream_is_stable(self):
        # Wire format: both machines must derive the same keystream.
        key = bytes(range(32))
        n = 2 * q._STREAM_CHUNK + 12345
        data = bytes(range(256)) * (n // 256) + bytes(n % 256)
        pieces = q._keystream_xor(key, data)
        self.assertEqual([len(p) for p in pieces], [q._STREAM_CHUNK] * 2 + [12345])
        self.assertEqual(hashlib.sha256(b"".join(pieces)).hexdigest(),
                         "92c008b674b2ff07afe970fd1ab646354d2a0e82916355ba3d69986e7997b45a")

    def test_data_of_several_keystream_pieces(self):
        # Python 3.6's SHAKE cannot make 512 MiB at once, hence the pieces.
        data = os.urandom(2 * q._STREAM_CHUNK + 777)
        blob = q.encrypt(data, "secret")
        self.assertEqual(len(blob), len(data) + q._CONTAINER_HDR.size + q._TAG_LEN)
        self.assertEqual(q.decrypt(blob, "secret"), data)
        with self.assertRaises(q.WrongPassphrase):
            q.decrypt(blob, "Secret")
        tampered = bytearray(blob)
        tampered[-100] ^= 1
        with self.assertRaises(q.WrongPassphrase):
            q.decrypt(bytes(tampered), "secret")
        for magic in (b"QXF1", b"QXF9"):  # QXF1 was the single-stream format of 1.1.1 and older
            with self.assertRaisesRegex(q.QrxferError, "same qrxfer version"):
                q.decrypt(magic + blob[4:], "secret")

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

    def test_parallel_xz_reads_as_one_file(self):
        import lzma
        from unittest import mock
        data = os.urandom(20000) + b"compressible text " * 3000
        with mock.patch.object(q, "XZ_CHUNK", 4096):
            buf = io.BytesIO()
            with q.XzWriter(buf, 6, 3) as xz:
                for i in range(0, len(data), 1000):
                    xz.write(data[i:i + 1000])
            # one independent stream per chunk (five writes reach 4096), put back together in order
            self.assertGreaterEqual(buf.getvalue().count(b"\xfd7zXZ\x00"), len(data) // 5000)
            self.assertEqual(lzma.decompress(buf.getvalue()), data)
            empty = io.BytesIO()
            with q.XzWriter(empty, 6, 2):
                pass
            self.assertEqual(lzma.decompress(empty.getvalue()), b"")
            with self.assertRaises(ZeroDivisionError):
                with q.XzWriter(io.BytesIO(), 6, 2) as xz:
                    xz.write(os.urandom(50000))
                    1 / 0
            src = os.path.dirname(self.write("tree/a.bin", os.urandom(30000)))
            self.write("tree/sub/b.txt", data)
            for threads in (1, 4):
                archive, nfiles, nbytes = q.pack_paths([src], threads)
                self.assertEqual((nfiles, nbytes), (2, 30000 + len(data)))
                out = os.path.join(self.tmp, "out%d" % threads)
                q.unpack_archive(archive, out)
                with open(os.path.join(out, "tree/sub/b.txt"), "rb") as fh:
                    self.assertEqual(fh.read(), data)

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


class FakeGridGrabber:
    """Pretends to be the screen with the sender's grid on it, in BGRX like XGetImage."""

    channels = (2, 1, 0)
    band = None  # (rows, columns) of pixels that something covers on the screen

    def __init__(self, np, stream, every=3):
        self.np, self.stream, self.every = np, stream, every
        self.calls = 0
        self.screen = None
        self.width = self.height = None
        self.grab(0, 0, 1, 1, colour=True)
        self.calls = 0

    def grab(self, left, top, width, height, colour=False):
        if self.calls % self.every == 0:  # the screen changes every few captures
            frames = self.stream.take(self.stream.rows)
            self.screen = place_on_desktop(self.np, render_grid_screen(self.np, frames, self.stream.cols,
                                                                       self.stream.rows))
            self.height, self.width = self.screen.shape[:2]
            if self.band is not None:
                self.screen[self.band] = 128
        self.calls += 1
        region = self.screen[top:top + height, left:left + width]
        return region if colour else self.np.ascontiguousarray(region[..., 1])


class LossyGridGrabber(FakeGridGrabber):
    """A FakeGridGrabber on a fake clock (30 captures/s) whose screens are lost on the way
    a fifth of the time: the sender shows them, but they are replaced before any capture."""

    def __init__(self, np, stream, clock, rnd, every=3):
        self.clock, self.rnd = clock, rnd
        FakeGridGrabber.__init__(self, np, stream, every)

    def grab(self, left, top, width, height, colour=False):
        self.clock[0] += 1 / 30.0
        if self.calls % self.every == 0 and self.screen is not None and self.rnd.random() < 0.2:
            self.stream.take(self.stream.rows)
        return FakeGridGrabber.grab(self, left, top, width, height, colour)


class SenderOpts:
    fps = 10.0


class SenderKeyboard:
    """Types requests straight into a grid sender's key handling, instead of a VNC viewer."""

    error = None

    def __init__(self, state):
        self.state, self.typed = state, []

    def busy(self):
        return False

    def type(self, keys):
        self.typed.append(keys)
        if self.state is not None:
            for key in keys + "\r":
                self.state.handle_key(key)


class ColourGrabber:
    """Shows a grayscale FakeGrabber's screen in colour, as AutoReader captures it."""

    channels = (2, 1, 0)

    def __init__(self, gray):
        self.gray, self.width, self.height = gray, gray.width, gray.height

    def grab(self, left, top, width, height, colour=False):
        return self.gray.np.repeat(self.gray.grab(left, top, width, height)[..., None], 4, axis=2)


@unittest.skipUnless(HAVE_NUMPY, "numpy missing")
class GridReceivePipelineTest(unittest.TestCase):
    def test_collect_grid_and_qr_automatically(self):
        import numpy as np
        blob = q.encrypt(os.urandom(150000), "pw")
        cols, rows = q.plan_cells(900, 500, 3)
        for threads in (1, 3):
            stream = q.GridStream(q.FountainEncoder(blob, q.grid_block_size(cols), 77 + threads), cols, rows)
            stream.seq = 200  # receiver started late
            grabber = FakeGridGrabber(np, stream)
            reader = q.AutoReader(q.GridReader(np, grabber.channels), None)
            sess = q.collect_from_screen(grabber, reader, progress=q.Progress(io.StringIO()),
                                         timeout=60, threads=threads)
            self.assertEqual(sess.result(), blob)
            self.assertEqual(reader.shared["mode"], "grid")
        decoders = available_decoders()
        if decoders:  # the same reader also finds QR codes
            stream = q.FrameStream(q.FountainEncoder(blob[:20000], 500, 5), "L")
            grabber = ColourGrabber(FakeGrabber(np, stream, cols=3, px=3))
            reader = q.AutoReader(q.GridReader(np, grabber.channels), q.QrReader(decoders[0]))
            sess = q.collect_from_screen(grabber, reader, progress=q.Progress(io.StringIO()), timeout=60)
            self.assertEqual(sess.result(), blob[:20000])
            self.assertEqual(reader.shared["mode"], "qr")


    def test_requests_bring_the_blocks_lost_in_the_first_pass(self):
        # A fifth of the screens never reach the viewer. Asked for, the sender shows the
        # missing blocks right after its first pass, instead of with rounds of repeats.
        import numpy as np
        from unittest import mock
        blob = q.encrypt(os.urandom(150000), "pw")
        cols, rows = q.plan_cells(900, 500, 3)
        shown = {}
        for ask in (False, True):
            stream = q.GridStream(q.FountainEncoder(blob, q.grid_block_size(cols), 41), cols, rows)
            clock = [0.0]
            grabber = LossyGridGrabber(np, stream, clock, random.Random(7))
            keyboard = SenderKeyboard(q._SendState(stream, SenderOpts()))
            out = io.StringIO()
            with mock.patch.object(q.time, "monotonic", lambda: clock[0]), \
                    mock.patch.object(q.FountainDecoder, "SKIP_MIN_K", 0), \
                    mock.patch.object(q.BlockRequester, "SERVED_AFTER", 0.2):  # as for many screens
                sess = q.collect_from_screen(grabber, q.GridReader(np, grabber.channels),
                                             progress=q.Progress(out), timeout=600,
                                             requester=q.BlockRequester(keyboard) if ask else None)
            self.assertEqual(sess.result(), blob)
            shown[ask] = stream.seq // rows
            if ask:
                self.assertGreaterEqual(len(keyboard.typed), 2)
                self.assertEqual(keyboard.state.requests, len(keyboard.typed))
                self.assertIn("Asking the sender for the", out.getvalue())
        k = -(-len(blob) // q.grid_block_size(cols))
        self.assertLess(shown[True], k / rows + 12)
        self.assertGreater(shown[False], 1.5 * shown[True])

    def test_requests_given_up_without_answers(self):
        from unittest import mock
        enc = q.FountainEncoder(os.urandom(200 * 4), 4, 6)
        sess = q.FountainDecoder.for_frame(enc.frame(0))
        for seq in range(0, 200, 3):
            sess.add(enc.frame(seq))
        keyboard, notes, clock = SenderKeyboard(None), [], [0.0]
        requester = q.BlockRequester(keyboard)
        with mock.patch.object(q.time, "monotonic", lambda: clock[0]):
            requester.update(sess, [enc.frame(5)], notes.append)
            self.assertEqual(keyboard.typed, [])  # the first pass is still on
            for t in range(40):
                clock[0] = t
                requester.update(sess, [enc.frame(5), enc.frame(300)], notes.append)
        self.assertEqual(len(keyboard.typed), 3)
        self.assertEqual(q.parse_request(keyboard.typed[0][1:], 6, 200), sess.missing_ranges())
        self.assertEqual(len(notes), 6)
        self.assertIn("did not answer 3 times in a row; no more requests", notes[-1])
        self.assertTrue(requester.off)

    def test_covered_rows_are_reported_and_still_arrive(self):
        # A dock over the bottom of the viewer hides the same rows on every capture: the
        # receiver says so, and the repeats bring those blocks in other rows.
        import numpy as np
        from unittest import mock
        blob = q.encrypt(os.urandom(150000), "pw")
        cols, rows = q.plan_cells(900, 500, 3)

        stream = q.GridStream(q.FountainEncoder(blob, q.grid_block_size(cols), 31), cols, rows)
        grabber = FakeGridGrabber(np, stream)
        geo = q.locate_grid(np, grabber.screen, grabber.channels)
        grabber.band = (slice(geo.ys[-3] - 1, geo.ys[-1] + 2), slice(geo.xs[cols // 3], geo.xs[cols // 2]))
        out = io.StringIO()
        with mock.patch.object(q.GridReader, "COVER_READS", 10):
            sess = q.collect_from_screen(grabber, q.GridReader(np, grabber.channels),
                                         progress=q.Progress(out), timeout=60)
        self.assertEqual(sess.result(), blob)
        self.assertIn("Grid rows %d-%d of %d" % (rows - 2, rows, rows), out.getvalue())
        self.assertIn("is something covering", out.getvalue())

    def test_progress_shows_the_recent_rate_beside_the_average(self):
        from unittest import mock

        class Sess:
            k, block_size, received = 10000, 100, 0
            rank = 0

        out, sess, clock = io.StringIO(), Sess(), [0.0]
        progress = q.Progress(out)
        with mock.patch.object(q.time, "monotonic", lambda: clock[0]):
            for t, rank in ((0, 0), (60, 9000), (90, 9010), (120, 9020)):
                clock[0], sess.rank = float(t), rank
                progress.update(sess, force=True)
        last = out.getvalue().strip().splitlines()[-1]
        # 10 blocks in the last 30 s, 9020 in 120 s on average
        self.assertIn("33 B/s (average 7.3 KB/s)", last)
        self.assertIn("ETA 49:00", last)


class CliTest(unittest.TestCase):
    def test_send_codec_conflicts(self):
        err = io.StringIO()
        old, sys.stderr = sys.stderr, err
        try:
            self.assertEqual(q.main(["send", "--codec", "grid", "--terminal", "x"]), 1)
        finally:
            sys.stderr = old
        self.assertIn("--terminal draws QR codes only", err.getvalue())

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
