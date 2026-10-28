#!/usr/bin/env python3
"""qrxfer - encrypted file transfer between two computers through the screen.

The sender packs files into a compressed tar archive, encrypts it with a passphrase,
cuts it into fountain-coded frames and shows them as an animated full-screen colour
grid (or as QR codes). The receiver watches its own screen (for example a VNC viewer
showing the sender's desktop), collects frames in any order until the archive can be
rebuilt, then decrypts and extracts it. No network port is opened by qrxfer.

  qrxfer send FILE_OR_DIR...   show files as an animated colour grid (stdlib only)
  qrxfer recv [-o DIR]         capture the screen and receive the files
  qrxfer genpass               print a strong random passphrase
  qrxfer decrypt FILE.qxf      decrypt a transfer saved after a wrong passphrase
  qrxfer selftest              check this machine and the whole pipeline
"""

import argparse
import collections
import ctypes
import ctypes.util
import functools
import getpass
import hashlib
import hmac
import importlib.util
import io
import itertools
import operator
import os
import re
import secrets
import shutil
import signal
import struct
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import unicodedata
import zlib

__version__ = "1.2.2"


class QrxferError(Exception):
    """A user-facing error: printed without a traceback."""


class WrongPassphrase(QrxferError):
    pass


def human_size(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return "%d %s" % (n, unit) if unit == "B" else "%.1f %s" % (n, unit)
        n /= 1024.0


def human_time(seconds):
    m, s = divmod(int(round(seconds)), 60)
    h, m = divmod(m, 60)
    return "%d:%02d:%02d" % (h, m, s) if h else "%d:%02d" % (m, s)


# ---------------------------------------------------------------------------
# Base45 (RFC 9285): maps bytes onto the QR alphanumeric charset (same order),
# which packs 5.5 bits per character and avoids byte-mode charset guessing.
# ---------------------------------------------------------------------------

B45_ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ $%*+-./:"
_B45_INDEX = {c: i for i, c in enumerate(B45_ALPHABET)}


def b45encode(data):
    out = []
    a = B45_ALPHABET
    for i in range(0, len(data) - 1, 2):
        e, rest = divmod(data[i] * 256 + data[i + 1], 45 * 45)
        d, c = divmod(rest, 45)
        out += (a[c], a[d], a[e])
    if len(data) % 2:
        d, c = divmod(data[-1], 45)
        out += (a[c], a[d])
    return "".join(out)


def b45decode(text):
    try:
        vals = [_B45_INDEX[ch] for ch in text]
    except KeyError as exc:
        raise ValueError("invalid base45 character %s" % exc) from None
    if len(vals) % 3 == 1:
        raise ValueError("invalid base45 length")
    out = bytearray()
    for i in range(0, len(vals) - 2, 3):
        n = vals[i] + vals[i + 1] * 45 + vals[i + 2] * 2025
        if n > 0xFFFF:
            raise ValueError("invalid base45 triplet")
        out += n.to_bytes(2, "big")
    if len(vals) % 3 == 2:
        n = vals[-2] + vals[-1] * 45
        if n > 0xFF:
            raise ValueError("invalid base45 pair")
        out.append(n)
    return bytes(out)


# ---------------------------------------------------------------------------
# Fountain code. Frames 0..k-1 carry the source blocks verbatim (systematic);
# later frames carry the XOR of `degree` pseudo-randomly chosen blocks. The
# receiver solves the linear system over GF(2) incrementally, so roughly any k
# frames rebuild the data, whichever frames the capture missed.
# ---------------------------------------------------------------------------

_M64 = (1 << 64) - 1


class _SplitMix64:
    """Tiny PRNG with a fixed, platform-independent output sequence."""

    def __init__(self, seed):
        self.state = seed & _M64

    def next64(self):
        self.state = (self.state + 0x9E3779B97F4A7C15) & _M64
        z = self.state
        z = ((z ^ (z >> 30)) * 0xBF58476D1CE4E5B9) & _M64
        z = ((z ^ (z >> 27)) * 0x94D049BB133111EB) & _M64
        return z ^ (z >> 31)

    def below(self, n):
        return (self.next64() * n) >> 64


def repair_indices(tid, seq, k, degree):
    """Return the `degree` distinct source block indices mixed into repair frame `seq`."""
    rng = _SplitMix64((tid << 32) | seq)
    # Partial Fisher-Yates shuffle of range(k), storing only the swapped slots.
    swapped = {}
    out = []
    for i in range(degree):
        j = i + rng.below(k - i)
        out.append(swapped.get(j, j))
        swapped[j] = swapped.get(i, i)
    return out


REPAIR_DEGREE_CAP = 256


def repair_degree(tid, seq, k):
    """Sender-side choice of how many blocks a repair frame mixes.

    Uniform in [cap/2, cap] with cap = min(k, 256): mixed parities keep the system
    solvable, and dense rows fill the last few gaps quickly. The degree travels in
    every frame, so the receiver never recomputes this.
    """
    top = min(k, REPAIR_DEGREE_CAP)
    low = max(1, top // 2)
    return low + _SplitMix64(((tid << 32) | seq) ^ 0x5DEECE66D).below(top - low + 1)


FRAME_VERSION = 1
# Base45 output may start or end with a space, so frames are wrapped in markers
# that survive any whitespace trimming by QR decoders and identify our codes.
FRAME_PREFIX, FRAME_SUFFIX = "QX:", "."
_FRAME_HDR = struct.Struct(">BIIHIH")  # version, tid, blob_len, block_size, seq, degree
_CRC = struct.Struct(">I")
FRAME_OVERHEAD = _FRAME_HDR.size + _CRC.size


class Frame:
    __slots__ = ("tid", "blob_len", "block_size", "seq", "degree", "payload")

    def __init__(self, tid, blob_len, block_size, seq, degree, payload):
        self.tid, self.blob_len, self.block_size = tid, blob_len, block_size
        self.seq, self.degree, self.payload = seq, degree, payload

    @property
    def k(self):
        return -(-self.blob_len // self.block_size)


def frame_bytes(frame):
    """The binary frame: header, payload, CRC32. A grid row carries exactly this."""
    body = _FRAME_HDR.pack(FRAME_VERSION, frame.tid, frame.blob_len, frame.block_size,
                           frame.seq, frame.degree) + frame.payload
    return body + _CRC.pack(zlib.crc32(body))


def pack_frame(frame):
    return FRAME_PREFIX + b45encode(frame_bytes(frame)) + FRAME_SUFFIX


def parse_frame(text):
    """Decode one scanned QR text; raises ValueError for anything that is not ours."""
    text = text.strip()
    if not (text.startswith(FRAME_PREFIX) and text.endswith(FRAME_SUFFIX)):
        raise ValueError("not a qrxfer frame")
    return parse_frame_bytes(b45decode(text[len(FRAME_PREFIX):-len(FRAME_SUFFIX)]))


def parse_frame_bytes(raw):
    """Check and decode one binary frame; raises ValueError for anything that is not ours."""
    if len(raw) <= FRAME_OVERHEAD:
        raise ValueError("frame too short")
    body = raw[:-_CRC.size]
    if zlib.crc32(body) != _CRC.unpack(raw[-_CRC.size:])[0]:
        raise ValueError("frame checksum mismatch")
    version, tid, blob_len, block_size, seq, degree = _FRAME_HDR.unpack_from(body)
    if version != FRAME_VERSION:
        raise ValueError("unsupported frame version %d (run the same qrxfer version on both machines)" % version)
    frame = Frame(tid, blob_len, block_size, seq, degree, body[_FRAME_HDR.size:])
    if (block_size == 0 or blob_len == 0 or len(frame.payload) != block_size
            or degree == 0 or degree > frame.k or (seq < frame.k and degree != 1)):
        raise ValueError("malformed frame")
    return frame


class FountainEncoder:
    def __init__(self, blob, block_size, tid):
        if not blob:
            raise ValueError("nothing to send")
        if not 0 < block_size <= 0xFFFF:
            raise ValueError("block size out of range")
        if len(blob) > 0xFFFFFFFF:
            raise QrxferError("transfer too large (4 GiB maximum)")
        self.tid = tid & 0xFFFFFFFF
        self.blob_len = len(blob)
        self.block_size = block_size
        self.k = -(-len(blob) // block_size)
        padded = blob.ljust(self.k * block_size, b"\0")
        self._blocks = [int.from_bytes(padded[i:i + block_size], "little")
                        for i in range(0, len(padded), block_size)]

    def frame(self, seq):
        if seq < self.k:
            degree, value = 1, self._blocks[seq]
        else:
            degree = repair_degree(self.tid, seq, self.k)
            value = 0
            for i in repair_indices(self.tid, seq, self.k, degree):
                value ^= self._blocks[i]
        return Frame(self.tid, self.blob_len, self.block_size, seq, degree,
                     value.to_bytes(self.block_size, "little"))


class FountainDecoder:
    """Incremental Gaussian elimination over GF(2).

    Blocks known on their own (systematic frames, or equations solved down to one
    block) are kept as values only. Other equations are a bitmask of the still-missing
    blocks plus the XOR of those blocks (both Python ints), kept in echelon form keyed
    by their highest set bit, so `rank` is exact progress: the data is complete when
    rank == k. Known blocks are substituted into repair frames before elimination:
    rows that kept every block would make each later repair frame cascade through all
    of them (with tens of thousands of blocks and a few percent missing, ~0.1 s per
    frame), and a bitmask per known block would need ~k*k/16 bytes.

    Mask bits are columns handed out to missing blocks as repair frames name them, not
    block indices: with two million blocks, masks over block indices took 250 KB each.
    A block named for the first time gets the highest column, so its equation is stored
    as it is. Once every missing block with a column heads a row, an equation over
    those blocks alone adds nothing and is dropped without elimination: near the end of
    a big transfer most repair frames are such, and each would walk thousands of rows.
    """

    # Late joiners: while systematic frames still arrive (a grid sender repeats them)
    # and many blocks are missing, repair frames would only add long rows; skip them.
    # Their elimination also holds up the capture loop, which then misses the repeated
    # frames: with two million blocks and 5 % missing it took seconds per screen.
    SKIP_MIN_K = 8192
    SKIP_WINDOW = 1024      # repair frames since a new systematic one; at least k/64
    SKIP_MISSING = 1 << 15  # skip while more than this many, or k/16, are missing

    def __init__(self, tid, blob_len, block_size):
        self.tid, self.blob_len, self.block_size = tid, blob_len, block_size
        self.k = -(-blob_len // block_size)
        self.received = 0
        self._known = {}  # block -> value
        self._rows = {}   # highest column -> (mask, value) of the other equations
        self._column = {}  # block -> its bit in the masks
        self._blocks = []  # column -> block
        self._open = 0     # columns whose block is still missing
        self._seen = set()
        self._skip_window = max(self.SKIP_WINDOW, self.k >> 6)
        self._since_systematic = self._skip_window  # repair frames since the last new systematic one

    @classmethod
    def for_frame(cls, frame):
        return cls(frame.tid, frame.blob_len, frame.block_size)

    @property
    def rank(self):
        return len(self._known) + len(self._rows)

    @property
    def complete(self):
        return self.rank == self.k

    def add(self, frame):
        """Feed one frame; returns True if it added new information."""
        if (frame.tid, frame.blob_len, frame.block_size) != (self.tid, self.blob_len, self.block_size):
            raise ValueError("frame belongs to another transfer")
        if frame.seq in self._seen or self.complete:
            return False
        self._seen.add(frame.seq)
        value = int.from_bytes(frame.payload, "little")
        known = self._known
        if frame.seq < self.k:
            self.received += 1
            self._since_systematic = 0
            i = frame.seq
            if i in known:
                return False
            known[i] = value
            column = self._column.get(i)
            if column is None:
                return True
            self._open -= 1
            row = self._rows.pop(column, None)
            if row is None:
                return True
            # Block i headed an equation over other blocks too: knowing it leaves one
            # about the others.
            mask, value = row[0] ^ (1 << column), row[1] ^ value
        else:
            self._since_systematic += 1
            if (self.k >= self.SKIP_MIN_K and self._since_systematic <= self._skip_window
                    and self.k - len(known) > min(self.k >> 4, self.SKIP_MISSING)):
                return False
            self.received += 1
            mask, fresh = 0, False
            columns, blocks = self._column, self._blocks
            for i in repair_indices(self.tid, frame.seq, self.k, frame.degree):
                v = known.get(i)
                if v is None:
                    column = columns.get(i)
                    if column is None:
                        column = columns[i] = len(blocks)
                        blocks.append(i)
                        self._open += 1
                        fresh = True
                    mask |= 1 << column
                else:
                    value ^= v
            if not fresh and len(self._rows) == self._open:
                return False
        rows, blocks = self._rows, self._blocks
        while mask:
            pivot = mask.bit_length() - 1
            v = known.get(blocks[pivot])
            if v is not None:  # learnt after this row was stored
                mask ^= 1 << pivot
                value ^= v
                continue
            row = rows.get(pivot)
            if row is None:
                if mask & (mask - 1):
                    rows[pivot] = (mask, value)
                else:
                    known[blocks[pivot]] = value
                    self._open -= 1
                return True
            mask ^= row[0]
            value ^= row[1]
        return False

    def missing_ranges(self):
        """(start, stop) runs of the blocks not known on their own yet."""
        have = bytearray(self.k)
        for i in self._known:
            have[i] = 1
        out, start = [], have.find(0)
        while start >= 0:
            stop = have.find(1, start)
            stop = self.k if stop < 0 else stop
            out.append((start, stop))
            start = have.find(0, stop)
        return out

    def result(self):
        if not self.complete:
            raise ValueError("transfer incomplete")
        known, rows, blocks = self._known, self._rows, self._blocks
        solved = [0] * len(blocks)  # by column
        for pivot in range(len(blocks)):
            value = known.get(blocks[pivot])
            if value is None:
                mask, value = rows[pivot]
                # Walk the set bits below the pivot via the mask's binary string.
                bits = bin(mask ^ (1 << pivot))
                base = len(bits) - 1  # bits[pos] is column base - pos
                pos = bits.find("1", 2)
                while pos != -1:
                    value ^= solved[base - pos]
                    pos = bits.find("1", pos + 1)
            solved[pivot] = value
        columns, bs = self._column, self.block_size
        return b"".join((known[i] if i in known else solved[columns[i]]).to_bytes(bs, "little")
                        for i in range(self.k))[:self.blob_len]


# ---------------------------------------------------------------------------
# Encryption, standard library only so the sending side needs no packages:
# scrypt (PBKDF2 where OpenSSL lacks it) derives an encryption key and a MAC key
# from the passphrase and a fresh random salt; the data is XORed with a
# SHAKE-256 keystream and authenticated with HMAC-SHA256 (encrypt-then-MAC).
# ---------------------------------------------------------------------------

# The keystream is made of 1 MiB SHAKE-256 pieces numbered after the key: CPython's
# built-in SHAKE (hashlib's only one on Python 3.6) refuses 2**29 bytes or more at once.
CONTAINER_MAGIC = b"QXF2"
_STREAM_CHUNK = 1 << 20
KDF_SCRYPT, KDF_PBKDF2 = 1, 2
_CONTAINER_HDR = struct.Struct(">4sBBBB16s")  # magic, kdf, param a/b/c, salt
_TAG_LEN = 32


def _derive_keys(secret, kdf, a, b, c, salt):
    password = secret.encode("utf-8")
    if kdf == KDF_SCRYPT:
        # a = log2(N), b = r, c = p; bounded so a hostile header cannot exhaust memory.
        if not (10 <= a <= 22 and 1 <= b <= 32 and 1 <= c <= 16 and (128 * b << a) <= 1 << 30):
            raise QrxferError("unsupported scrypt parameters in the encrypted data")
        if not hasattr(hashlib, "scrypt"):
            raise QrxferError("this Python/OpenSSL has no scrypt; decrypt on a newer system")
        n = 1 << a
        key = hashlib.scrypt(password, salt=salt, n=n, r=b, p=c, dklen=64,
                             maxmem=128 * b * (n + c + 2) + (1 << 20))
    elif kdf == KDF_PBKDF2:
        if not 10 <= a <= 24:
            raise QrxferError("unsupported PBKDF2 parameters in the encrypted data")
        key = hashlib.pbkdf2_hmac("sha256", password, salt, 1 << a, dklen=64)
    else:
        raise QrxferError("unknown key derivation function %d (upgrade qrxfer)" % kdf)
    return key[:32], key[32:]


def _keystream_xor(key, data):
    """data XOR the keystream, as a list of pieces of up to 1 MiB (whole-data ints would
    need several copies of a big transfer in memory)."""
    data, n = memoryview(data), len(data)
    out = []
    for pos in range(0, n, _STREAM_CHUNK):
        size = min(_STREAM_CHUNK, n - pos)
        stream = hashlib.shake_256(b"qrxfer-v2-stream" + key + struct.pack(">Q", pos // _STREAM_CHUNK)).digest(size)
        out.append((int.from_bytes(data[pos:pos + size], "little") ^ int.from_bytes(stream, "little"))
                   .to_bytes(size, "little"))
    return out


def encrypt(plaintext, secret):
    params = (KDF_SCRYPT, 17, 8, 1) if hasattr(hashlib, "scrypt") else (KDF_PBKDF2, 20, 0, 0)
    salt = os.urandom(16)
    enc_key, mac_key = _derive_keys(secret, *(params + (salt,)))
    parts = [_CONTAINER_HDR.pack(CONTAINER_MAGIC, *(params + (salt,)))] + _keystream_xor(enc_key, plaintext)
    mac = hmac.new(mac_key, digestmod=hashlib.sha256)
    for part in parts:
        mac.update(part)
    parts.append(mac.digest())
    return b"".join(parts)


def decrypt(blob, secret):
    magic = blob[:4]
    if len(blob) < _CONTAINER_HDR.size + _TAG_LEN or not magic.startswith(b"QXF"):
        raise QrxferError("this is not qrxfer encrypted data")
    if magic != CONTAINER_MAGIC:
        raise QrxferError("unsupported encrypted data format %r (run the same qrxfer version on both machines)"
                          % magic.decode("latin-1"))
    _, kdf, a, b, c, salt = _CONTAINER_HDR.unpack_from(blob)
    enc_key, mac_key = _derive_keys(secret, kdf, a, b, c, salt)
    view = memoryview(blob)
    body, tag = view[:-_TAG_LEN], bytes(view[-_TAG_LEN:])
    if not hmac.compare_digest(hmac.new(mac_key, body, hashlib.sha256).digest(), tag):
        raise WrongPassphrase("wrong passphrase (or corrupted data)")
    return b"".join(_keystream_xor(enc_key, body[_CONTAINER_HDR.size:]))


# Generated passphrases: 16 Crockford base32 characters (80 bits) in 4 groups.
# Anything shaped like one is compared case-insensitively, without separators,
# and with O/I/L read as 0/1/1, so it is hard to mistype on the other machine.
_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_CODE_RE = re.compile(r"[0-9A-Za-z]{4}(?:[ -]?[0-9A-Za-z]{4}){3}")


def generate_passphrase():
    code = "".join(secrets.choice(_CROCKFORD) for _ in range(16))
    return "-".join(code[i:i + 4] for i in range(0, 16, 4))


def normalize_passphrase(text):
    text = unicodedata.normalize("NFC", text.strip())
    if _CODE_RE.fullmatch(text):
        code = re.sub(r"[ -]", "", text).upper().translate(str.maketrans("OIL", "011"))
        return "-".join(code[i:i + 4] for i in range(0, 16, 4))
    return text


def obtain_passphrase(args, role):
    """Passphrase from --passphrase-file, $QRXFER_PASSPHRASE or an interactive prompt."""
    if getattr(args, "passphrase_file", None):
        try:
            with open(args.passphrase_file, encoding="utf-8") as fh:
                secret = normalize_passphrase(fh.read())
        except OSError as exc:
            raise QrxferError("cannot read passphrase file: %s" % exc)
        if not secret:
            raise QrxferError("passphrase file %s is empty" % args.passphrase_file)
        return secret, False
    env = os.environ.get("QRXFER_PASSPHRASE")
    if env:
        return normalize_passphrase(env), False
    if not sys.stdin.isatty():
        raise QrxferError("no terminal to ask for a passphrase; use --passphrase-file")
    can_generate = role in ("send", "recv")
    while True:
        prompt = "Passphrase (Enter = generate one): " if can_generate else "Passphrase: "
        secret = normalize_passphrase(getpass.getpass(prompt))
        if not secret and not can_generate:
            continue
        if not secret:
            secret = generate_passphrase()
            print("Generated passphrase:  %s" % secret)
            print("Type it on the other machine when qrxfer asks for it.")
            return secret, True
        if role == "send":
            if normalize_passphrase(getpass.getpass("Repeat passphrase: ")) != secret:
                print("Passphrases do not match, try again.")
                continue
            if len(secret) < 8:
                print("Warning: short passphrases can be brute-forced; prefer a generated one.")
        return secret, True


# ---------------------------------------------------------------------------
# Archive packing / safe extraction
# ---------------------------------------------------------------------------

def _unique_name(name, used):
    stem, ext = os.path.splitext(name)
    candidate, i = name, 1
    while candidate in used:
        candidate = "%s (%d)%s" % (stem, i, ext)
        i += 1
    used.add(candidate)
    return candidate


def _unique_path(path):
    stem, ext = os.path.splitext(path)
    candidate, i = path, 1
    while os.path.lexists(candidate):
        candidate = "%s (%d)%s" % (stem, i, ext)
        i += 1
    return candidate


XZ_CHUNK = 24 << 20  # like xz -T: three times the 8 MiB dictionary of preset 6
XZ_THREAD_MEMORY = 200 << 20  # a preset 6 encoder (94 MiB) plus chunks waiting in and out


def xz_threads():
    """One compression thread per usable core, within a quarter of the RAM (like xz -T0)."""
    try:
        cores = len(os.sched_getaffinity(0))
    except AttributeError:
        cores = os.cpu_count() or 1
    try:
        ram = os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE")
    except (AttributeError, ValueError, OSError):
        return cores
    return max(1, min(cores, ram // 4 // XZ_THREAD_MEMORY))


class XzWriter(object):
    """Write-only file that compresses on several threads, like xz -T: every XZ_CHUNK
    bytes become an independent .xz stream, and xz, lzma and tarfile read the
    concatenated streams as one file. lzma releases the GIL while it compresses."""

    def __init__(self, out, preset, threads):
        import lzma
        from concurrent.futures import ThreadPoolExecutor
        self.out, self.preset, self.threads, self.compress = out, preset, threads, lzma.compress
        self.pool = ThreadPoolExecutor(threads)
        self.buf, self.jobs, self.empty = bytearray(), collections.deque(), True

    def write(self, data):
        self.buf += data
        if len(self.buf) >= XZ_CHUNK:
            self._submit()
        return len(data)

    def _submit(self):
        self.jobs.append(self.pool.submit(self.compress, bytes(self.buf), preset=self.preset))
        self.buf, self.empty = bytearray(), False
        while len(self.jobs) > 2 * self.threads:  # bounds the memory in flight
            self.out.write(self.jobs.popleft().result())

    def __enter__(self):
        return self

    def __exit__(self, kind, value, tb):
        if kind is None:
            if self.buf or self.empty:  # an empty input still makes one valid stream
                self._submit()
            while self.jobs:
                self.out.write(self.jobs.popleft().result())
        for job in self.jobs:
            job.cancel()
        self.pool.shutdown()


def pack_paths(paths, threads=None):
    """Return (tar archive bytes, number of files, total file bytes)."""
    buf = io.BytesIO()
    try:
        import lzma
        out = XzWriter(buf, 6 | lzma.PRESET_EXTREME, threads or xz_threads())
    except ImportError:
        import gzip
        out = gzip.GzipFile(fileobj=buf, mode="wb", compresslevel=9)
    stats = [0, 0]

    def clean(info):
        if not (info.isreg() or info.isdir() or info.issym()):
            return None  # skip devices, sockets and fifos
        info.uid = info.gid = 0
        info.uname = info.gname = ""
        if info.isreg():
            stats[0] += 1
            stats[1] += info.size
        return info

    used = set()
    with out, tarfile.open(fileobj=out, mode="w|") as tar:
        for path in paths:
            if not os.path.lexists(path):
                raise QrxferError("no such file or directory: %s" % path)
            name = os.path.basename(os.path.normpath(os.path.abspath(path))) or "root"
            try:
                tar.add(path, arcname=_unique_name(name, used), filter=clean)
            except OSError as exc:
                raise QrxferError("cannot read %s: %s" % (exc.filename or path, exc.strerror or exc))
    if not stats[0] and not any(os.path.isdir(p) for p in paths):
        raise QrxferError("nothing to send")
    return buf.getvalue(), stats[0], stats[1]


def _check_members_legacy(tar):
    # Only used on Pythons without tarfile extraction filters (< 3.8.17/3.12).
    for m in tar.getmembers():
        parts = m.name.replace("\\", "/").split("/")
        if m.name.startswith("/") or ".." in parts:
            raise QrxferError("refusing unsafe path in archive: %s" % m.name)
        if m.issym() or m.islnk():
            target = m.linkname.replace("\\", "/")
            if target.startswith("/") or ".." in target.split("/"):
                raise QrxferError("refusing unsafe link in archive: %s" % m.name)
        elif not (m.isreg() or m.isdir()):
            raise QrxferError("refusing special file in archive: %s" % m.name)


def unpack_archive(data, outdir):
    """Extract into outdir without overwriting anything; returns the new top-level paths."""
    os.makedirs(outdir, exist_ok=True)
    staging = tempfile.mkdtemp(prefix=".qrxfer-", dir=outdir)
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as tar:
            if hasattr(tarfile, "data_filter"):
                tar.extractall(staging, filter="data")
            else:
                _check_members_legacy(tar)
                tar.extractall(staging)
        saved = []
        for name in sorted(os.listdir(staging)):
            dest = _unique_path(os.path.join(outdir, name))
            os.replace(os.path.join(staging, name), dest)
            saved.append(dest)
        return saved
    except (tarfile.TarError, EOFError) as exc:
        raise QrxferError("received archive is damaged: %s" % exc)
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def path_summary(path):
    if os.path.isdir(path) and not os.path.islink(path):
        files = size = 0
        for root, _, names in os.walk(path):
            for n in names:
                p = os.path.join(root, n)
                if os.path.isfile(p) and not os.path.islink(p):
                    files += 1
                    size += os.path.getsize(p)
        return "directory, %d file%s, %s" % (files, "" if files == 1 else "s", human_size(size))
    if os.path.islink(path):
        return "symlink -> %s" % os.readlink(path)
    return human_size(os.path.getsize(path))


# ---------------------------------------------------------------------------
# Minimal QR Code encoder (ISO/IEC 18004 model 2, alphanumeric mode), so the
# sending machine needs nothing beyond the Python standard library.
# ---------------------------------------------------------------------------

QR_LEVELS = "LMQH"
_QR_FORMAT_LEVEL = {"L": 1, "M": 0, "Q": 3, "H": 2}
# One line per version 1..40, one cell per level L|M|Q|H:
# EC codewords per block, group-1 blocks, data codewords per group-1 block,
# group-2 blocks (which hold one more data codeword each).
_QR_EC_TABLE = """\
7 1 19 0|10 1 16 0|13 1 13 0|17 1 9 0
10 1 34 0|16 1 28 0|22 1 22 0|28 1 16 0
15 1 55 0|26 1 44 0|18 2 17 0|22 2 13 0
20 1 80 0|18 2 32 0|26 2 24 0|16 4 9 0
26 1 108 0|24 2 43 0|18 2 15 2|22 2 11 2
18 2 68 0|16 4 27 0|24 4 19 0|28 4 15 0
20 2 78 0|18 4 31 0|18 2 14 4|26 4 13 1
24 2 97 0|22 2 38 2|22 4 18 2|26 4 14 2
30 2 116 0|22 3 36 2|20 4 16 4|24 4 12 4
18 2 68 2|26 4 43 1|24 6 19 2|28 6 15 2
20 4 81 0|30 1 50 4|28 4 22 4|24 3 12 8
24 2 92 2|22 6 36 2|26 4 20 6|28 7 14 4
26 4 107 0|22 8 37 1|24 8 20 4|22 12 11 4
30 3 115 1|24 4 40 5|20 11 16 5|24 11 12 5
22 5 87 1|24 5 41 5|30 5 24 7|24 11 12 7
24 5 98 1|28 7 45 3|24 15 19 2|30 3 15 13
28 1 107 5|28 10 46 1|28 1 22 15|28 2 14 17
30 5 120 1|26 9 43 4|28 17 22 1|28 2 14 19
28 3 113 4|26 3 44 11|26 17 21 4|26 9 13 16
28 3 107 5|26 3 41 13|30 15 24 5|28 15 15 10
28 4 116 4|26 17 42 0|28 17 22 6|30 19 16 6
28 2 111 7|28 17 46 0|30 7 24 16|24 34 13 0
30 4 121 5|28 4 47 14|30 11 24 14|30 16 15 14
30 6 117 4|28 6 45 14|30 11 24 16|30 30 16 2
26 8 106 4|28 8 47 13|30 7 24 22|30 22 15 13
28 10 114 2|28 19 46 4|28 28 22 6|30 33 16 4
30 8 122 4|28 22 45 3|30 8 23 26|30 12 15 28
30 3 117 10|28 3 45 23|30 4 24 31|30 11 15 31
30 7 116 7|28 21 45 7|30 1 23 37|30 19 15 26
30 5 115 10|28 19 47 10|30 15 24 25|30 23 15 25
30 13 115 3|28 2 46 29|30 42 24 1|30 23 15 28
30 17 115 0|28 10 46 23|30 10 24 35|30 19 15 35
30 17 115 1|28 14 46 21|30 29 24 19|30 11 15 46
30 13 115 6|28 14 46 23|30 44 24 7|30 59 16 1
30 12 121 7|28 12 47 26|30 39 24 14|30 22 15 41
30 6 121 14|28 6 47 34|30 46 24 10|30 2 15 64
30 17 122 4|28 29 46 14|30 49 24 10|30 24 15 46
30 4 122 18|28 13 46 32|30 48 24 14|30 42 15 32
30 20 117 4|28 40 47 7|30 43 24 22|30 10 15 67
30 19 118 6|28 18 47 31|30 34 24 34|30 20 15 61
"""
QR_EC_BLOCKS = {
    (version, level): tuple(int(v) for v in cell.split())
    for version, line in enumerate(_QR_EC_TABLE.splitlines(), 1)
    for level, cell in zip(QR_LEVELS, line.split("|"))
}

_GF_EXP = [0] * 512
_GF_LOG = [0] * 256
_x = 1
for _i in range(255):
    _GF_EXP[_i] = _GF_EXP[_i + 255] = _x
    _GF_LOG[_x] = _i
    _x <<= 1
    if _x & 0x100:
        _x ^= 0x11D
del _x, _i


@functools.lru_cache(maxsize=None)
def _rs_generator_logs(degree):
    """Logs of the Reed-Solomon generator coefficients (x - a^0)...(x - a^(degree-1))."""
    poly = [1]
    for i in range(degree):
        nxt = poly + [0]
        for j, coef in enumerate(poly):
            if coef:
                nxt[j + 1] ^= _GF_EXP[_GF_LOG[coef] + i]
        poly = nxt
    return tuple(_GF_LOG[c] for c in poly[1:])


@functools.lru_cache(maxsize=None)
def _rs_products(degree):
    """For each byte c: c times the generator (minus its leading 1) as a degree-byte int."""
    gen = _rs_generator_logs(degree)
    return tuple(int.from_bytes(bytes(_GF_EXP[_GF_LOG[c] + g] for g in gen), "big") if c else 0
                 for c in range(256))


def _rs_remainder(data, degree):
    """Reed-Solomon parity bytes: polynomial division as a shift register held in one int."""
    products = _rs_products(degree)
    shift, full = 8 * (degree - 1), (1 << 8 * degree) - 1
    rem = 0
    for b in data:
        rem = ((rem << 8) & full) ^ products[(rem >> shift) ^ b]
    return rem.to_bytes(degree, "big")


def qr_alignment_positions(version):
    if version == 1:
        return []
    count = version // 7 + 2
    step = 26 if version == 32 else (version * 4 + count * 2 + 1) // (count * 2 - 2) * 2
    size = version * 4 + 17
    return [6] + [size - 7 - i * step for i in reversed(range(count - 1))]


_QR_MASKS = (
    lambda x, y: (x + y) % 2 == 0,
    lambda x, y: y % 2 == 0,
    lambda x, y: x % 3 == 0,
    lambda x, y: (x + y) % 3 == 0,
    lambda x, y: (x // 3 + y // 2) % 2 == 0,
    lambda x, y: x * y % 2 + x * y % 3 == 0,
    lambda x, y: (x * y % 2 + x * y % 3) % 2 == 0,
    lambda x, y: ((x + y) % 2 + x * y % 3) % 2 == 0,
)


class _QrTemplate:
    """Everything about one QR version that does not depend on the payload.

    Rows are Python ints with bit x = column x, 1 = dark module.
    """

    def __init__(self, version):
        size = self.size = version * 4 + 17
        func = [[False] * size for _ in range(size)]
        dark = [[False] * size for _ in range(size)]

        def put(x, y, is_dark):
            func[y][x] = True
            dark[y][x] = bool(is_dark)

        for cx, cy in ((3, 3), (size - 4, 3), (3, size - 4)):  # finders + separators
            for dy in range(-4, 5):
                for dx in range(-4, 5):
                    if 0 <= cx + dx < size and 0 <= cy + dy < size:
                        put(cx + dx, cy + dy, max(abs(dx), abs(dy)) not in (2, 4))
        pos = qr_alignment_positions(version)
        corners = {(pos[0], pos[0]), (pos[0], pos[-1]), (pos[-1], pos[0])} if pos else set()
        for ay in pos:
            for ax in pos:
                if (ax, ay) not in corners:
                    for dy in range(-2, 3):
                        for dx in range(-2, 3):
                            put(ax + dx, ay + dy, max(abs(dx), abs(dy)) != 1)
        for i in range(size):  # timing patterns
            if not func[6][i]:
                put(i, 6, i % 2 == 0)
            if not func[i][6]:
                put(6, i, i % 2 == 0)
        fmt = [(8, i, i) for i in range(6)] + [(8, 7, 6), (8, 8, 7), (7, 8, 8)]
        fmt += [(14 - i, 8, i) for i in range(9, 15)]
        fmt += [(size - 1 - i, 8, i) for i in range(8)]
        fmt += [(8, size - 15 + i, i) for i in range(8, 15)]
        for x, y, _ in fmt:
            put(x, y, False)
        self.format_positions = fmt
        put(8, size - 8, True)  # the always-dark module
        if version >= 7:
            rem = version
            for _ in range(12):
                rem = (rem << 1) ^ ((rem >> 11) * 0x1F25)
            bits = version << 12 | rem
            for i in range(18):
                a, b = size - 11 + i % 3, i // 3
                put(a, b, bits >> i & 1)
                put(b, a, bits >> i & 1)

        # Data module positions in placement order (two-column zigzag, skipping column 6).
        self.positions = []
        right = size - 1
        while right >= 1:
            if right == 6:
                right = 5
            upward = ((right + 1) & 2) == 0
            for vert in range(size):
                y = size - 1 - vert if upward else vert
                for x in (right, right - 1):
                    if not func[y][x]:
                        self.positions.append((y, 1 << x))
            right -= 2
        # Per row, the stream bit index of each module from column size-1 down to 0;
        # -1 (function modules, remainder bits) picks a "0" appended to the bit string,
        # so int("".join(getter(bits + "0")), 2) is the row's data with bit x = column x.
        index = [[-1] * size for _ in range(size)]
        for i, (y, bit) in enumerate(self.positions[:len(self.positions) // 8 * 8]):
            index[y][bit.bit_length() - 1] = i
        self.row_bits = [operator.itemgetter(*reversed(row)) for row in index]

        self.function_dark = [sum(1 << x for x in range(size) if dark[y][x]) for y in range(size)]
        self.mask_rows = [
            [sum(1 << x for x in range(size) if not func[y][x] and m(x, y)) for y in range(size)]
            for m in _QR_MASKS
        ]


@functools.lru_cache(maxsize=None)
def _qr_template(version):
    return _QrTemplate(version)


def _qr_format_bits(level, mask):
    data = _QR_FORMAT_LEVEL[level] << 3 | mask
    rem = data
    for _ in range(10):
        rem = (rem << 1) ^ ((rem >> 9) * 0x537)
    return (data << 10 | rem) ^ 0x5412


_RUN_RE = re.compile(r"0{5,}|1{5,}")
_FINDER_LIKE_RE = re.compile(r"(?=00001011101|10111010000)")


def _qr_penalty(rows, size):
    lines = [format(r, "0%db" % size) for r in rows]
    score = 0
    for line in lines + ["".join(col) for col in zip(*lines)]:
        for run in _RUN_RE.finditer(line):
            score += len(run.group()) - 2
        score += 40 * len(_FINDER_LIKE_RE.findall("0000" + line + "0000"))
    full = (1 << size) - 1
    for a, b in zip(rows, rows[1:]):
        same_v = ~(a ^ b) & full
        same_h = ~(a ^ (a >> 1)) & (full >> 1)
        score += 3 * bin(same_v & (same_v >> 1) & same_h).count("1")
    total = size * size
    dark = sum(bin(r).count("1") for r in rows)
    return score + 10 * (abs(dark * 20 - total * 10) // total)


def qr_version_for(length, level):
    """Smallest QR version holding `length` alphanumeric characters, or None."""
    for version in range(1, 41):
        cc_bits = 9 if version <= 9 else 11 if version <= 26 else 13
        ec, b1, d1, b2 = QR_EC_BLOCKS[version, level]
        if 4 + cc_bits + 11 * (length // 2) + 6 * (length % 2) <= (b1 * d1 + b2 * (d1 + 1)) * 8:
            return version
    return None


def qr_encode(text, level="L", mask=None):
    """Encode an alphanumeric-charset string; returns (version, size, rows)."""
    n = len(text)
    version = qr_version_for(n, level)
    if version is None:
        raise ValueError("too much data for one QR code")
    cc_bits = 9 if version <= 9 else 11 if version <= 26 else 13
    ec, b1, d1, b2 = QR_EC_BLOCKS[version, level]
    capacity = (b1 * d1 + b2 * (d1 + 1)) * 8

    vals = [_B45_INDEX[ch] for ch in text]
    acc, nbits = 0b0010 << cc_bits | n, 4 + cc_bits
    for i in range(0, n - 1, 2):
        acc = acc << 11 | (vals[i] * 45 + vals[i + 1])
    nbits += 11 * (n // 2)
    if n % 2:
        acc = acc << 6 | vals[-1]
        nbits += 6
    pad = min(4, capacity - nbits)  # terminator
    pad += (-(nbits + pad)) % 8
    acc <<= pad
    nbits += pad
    data = acc.to_bytes(nbits // 8, "big")
    data += bytes((0xEC, 0x11)[i % 2] for i in range(capacity // 8 - len(data)))

    blocks, pos = [], 0
    for i in range(b1 + b2):
        length = d1 + (i >= b1)
        blocks.append(data[pos:pos + length])
        pos += length
    ecc = [_rs_remainder(block, ec) for block in blocks]
    chain = itertools.chain.from_iterable
    # Interleave: byte i of every block (group-2 blocks hold one more), then the parity.
    stream = (bytes(chain(zip(*blocks))) + bytes(block[d1] for block in blocks[b1:])
              + bytes(chain(zip(*ecc))))

    tpl = _qr_template(version)
    size = tpl.size
    bits = format(int.from_bytes(stream, "big"), "0%db" % (len(stream) * 8)) + "0"
    rows = [int("".join(get(bits)), 2) for get in tpl.row_bits]

    best = None
    for m in (range(8) if mask is None else (mask,)):
        mask_rows = tpl.mask_rows[m]
        final = [fd | (r ^ mr) for fd, r, mr in zip(tpl.function_dark, rows, mask_rows)]
        fbits = _qr_format_bits(level, m)
        for x, y, b in tpl.format_positions:
            if fbits >> b & 1:
                final[y] |= 1 << x
        score = _qr_penalty(final, size) if mask is None else 0
        if best is None or score < best[0]:
            best = (score, final)
    return version, size, best[1]


# ---------------------------------------------------------------------------
# Sender: frame stream, layout and the two displays (Tk window, terminal)
# ---------------------------------------------------------------------------

QUIET = 4  # quiet-zone modules around and between codes
DENSITY = {"low": 300, "medium": 600, "high": 1000}
# Code sizes --density auto picks from (57..85 modules). Bigger codes would waste less
# space on quiet zones, but zbar's decoding time per byte rises steeply above ~v17.
AUTO_VERSIONS = range(10, 18)
# Frames carry encrypted, random-looking data, so all eight masks score about the
# same penalty and decode equally well; a fixed mask skips the penalty search, which
# took ~85% of the encoding time. The checkerboard also keeps the zero padding of a
# short last block free of long runs.
FRAME_MASK = 0


class FrameStream:
    """Turns consecutive fountain frames into QR codes."""

    unit = "code"

    def __init__(self, encoder, level):
        self.encoder, self.level = encoder, level
        self.seq = 0
        self.version = qr_version_for(len(pack_frame(encoder.frame(0))), level)
        if self.version is None:
            raise QrxferError("frames do not fit in a QR code; use a lower --density")
        self.size = self.version * 4 + 17

    def phase(self):
        k = self.encoder.k
        return "first pass %d%%" % (self.seq * 100 // k) if self.seq < k else "repair frames"

    def take(self, count):
        codes = []
        for _ in range(count):
            codes.append(qr_encode(pack_frame(self.encoder.frame(self.seq)), self.level, FRAME_MASK)[2])
            self.seq += 1
        return codes


def max_block_size(version, level):
    """Largest block size whose frames still fit in a QR code of this version."""
    ec, b1, d1, b2 = QR_EC_BLOCKS[version, level]
    bits = (b1 * d1 + b2 * (d1 + 1)) * 8 - 4 - (9 if version <= 9 else 11 if version <= 26 else 13)
    chars = bits // 11 * 2 + (bits % 11 >= 6) - len(FRAME_PREFIX) - len(FRAME_SUFFIX)
    return chars // 3 * 2 + (chars % 3 == 2) - FRAME_OVERHEAD


def plan_grid(avail_w, avail_h, size, min_px, max_codes, grid=None):
    """Choose (cols, rows, px) to show as many codes as fit with modules >= min_px."""
    pitch = size + QUIET

    def px_for(cols, rows):
        return min(avail_w // (cols * pitch + QUIET), avail_h // (rows * pitch + QUIET))

    if grid:
        return grid[0], grid[1], px_for(*grid)
    best = (0, 0, 1, 1)
    for rows in range(1, max_codes + 1):
        for cols in range(1, max_codes // rows + 1):
            px = px_for(cols, rows)
            if px >= min_px and (cols * rows, px) > best[:2]:
                best = (cols * rows, px, cols, rows)
    if best[0] == 0:
        return 1, 1, px_for(1, 1)
    return best[2], best[3], best[1]


def choose_block_size(avail_w, avail_h, min_px, max_codes, grid, level):
    """--density auto: the block size that puts the most data on one screen.

    With an explicit --grid this is the largest code that still fits that grid.
    Returns None if nothing fits with modules >= min_px.
    """
    best = None
    for version in AUTO_VERSIONS:
        block = max_block_size(version, level)
        cols, rows, px = plan_grid(avail_w, avail_h, version * 4 + 17, min_px, max_codes, grid)
        if px >= min_px and (best is None or (cols * rows * block, px) > best[0]):
            best = ((cols * rows * block, px), block)
    return best and best[1]


class _SendState:
    def __init__(self, stream, opts):
        self.stream, self.opts = stream, opts
        self.fps = opts.fps
        self.paused = False
        self.codes = None
        self.quit = False
        self.started = time.monotonic()
        self.shown = collections.deque(maxlen=16)  # when recent screens appeared
        self.typed = None  # keys of a request for blocks being typed
        self.requests = self.bad_requests = 0

    def advance(self, count):
        if self.codes is None or len(self.codes) != count or not self.paused:
            self.codes = self.stream.take(count)
            self.shown.append(time.monotonic())
        return self.codes

    def actual_fps(self):
        """Screens per second really shown; lower than --fps when this CPU is too slow."""
        if len(self.shown) < 2 or self.shown[-1] <= self.shown[0]:
            return self.fps
        return (len(self.shown) - 1) / (self.shown[-1] - self.shown[0])

    def handle_key(self, key):
        """Returns False when the user asked to quit."""
        if self.typed is not None:
            if key in ("\r", "\n", "Return", "KP_Enter"):
                self._request("".join(self.typed))
                self.typed = None
                return True
            if len(key) == 1 and key in _REQUEST_LAST + _REQUEST_MORE and len(self.typed) < REQUEST_MAX_KEYS:
                self.typed.append(key)
                return True
            self.typed = None  # any other key ends the request and counts as itself
            self.bad_requests += 1
        if key == REQUEST_KEY and hasattr(self.stream, "request"):
            self.typed = []
            return True
        if key in ("q", "Q", "Escape", "\x1b"):
            return False
        if key in ("space", " "):
            self.paused = not self.paused
            self.shown.clear()
        elif key in ("plus", "equal", "KP_Add", "+", "="):
            self.fps = min(30.0, self.fps * 1.25)
            self.shown.clear()
        elif key in ("minus", "underscore", "KP_Subtract", "-", "_"):
            self.fps = max(0.5, self.fps / 1.25)
            self.shown.clear()
        return True

    def _request(self, keys):
        enc = self.stream.encoder
        try:
            ranges = parse_request(keys, enc.tid, enc.k)
        except ValueError:
            self.bad_requests += 1
            return
        self.requests += 1
        self.stream.request(ranges)

    def status(self, count):
        fps = self.actual_fps()
        rate = count * fps * self.stream.encoder.block_size
        state = "PAUSED" if self.paused else "%.1f/%.1f fps" % (fps, self.fps)
        asked = ("  requests %d (%d bad)" % (self.requests, self.bad_requests)
                 if self.requests or self.bad_requests else "")
        return ("qrxfer  frame %d  %s%s  %d %s%s x %s = %s/s  |  keys: space pause, +/- speed, q quit"
                % (self.stream.seq, self.stream.phase(), asked, count, self.stream.unit,
                   "" if count == 1 else "s", state, human_size(rate)))


def qr_to_pgm(rows, size):
    data = b"".join(format(r, "0%db" % size)[::-1].encode("ascii") for r in rows)
    return b"P5 %d %d 255\n" % (size, size) + data.translate(bytes.maketrans(b"01", b"\xff\x00"))


def _tk_photo(tk, rows, size, use_pgm):
    """A 1-pixel-per-module PhotoImage; PGM data is fast, put() also works on Tk 8.5."""
    if use_pgm[0]:
        try:
            return tk.PhotoImage(data=qr_to_pgm(rows, size))
        except tk.TclError:
            use_pgm[0] = False
    img = tk.PhotoImage(width=size, height=size)
    img.put(" ".join("{%s}" % " ".join("#000" if r >> x & 1 else "#fff" for x in range(size)) for r in rows))
    return img


def run_tk_sender(make_stream, opts):
    """Show the codes in a (full-screen) Tk window. make_stream(width, height, min_px)
    returns the FrameStream once the drawing area is known."""
    try:
        import tkinter as tk
    except ImportError:
        raise QrxferError("tkinter is missing (install python3-tk) - or use --terminal")
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        raise QrxferError("cannot open a window (%s) - is DISPLAY set? Otherwise use --terminal" % exc)
    root.title("qrxfer send")
    root.configure(bg="white")
    status = tk.Label(root, bg="white", fg="#555555", anchor="w", font="TkFixedFont")
    status.pack(side="bottom", fill="x")
    canvas = tk.Canvas(root, bg="white", highlightthickness=0, bd=0)
    canvas.pack(fill="both", expand=True)
    screen_w, screen_h = root.winfo_screenwidth(), root.winfo_screenheight()
    if opts.windowed:
        win_w, win_h = screen_w * 2 // 3, screen_h * 2 // 3
        root.geometry("%dx%d" % (win_w, win_h))
    else:
        win_w, win_h = screen_w, screen_h
        # Explicit geometry too: without a window manager "-fullscreen" alone does nothing.
        root.geometry("%dx%d+0+0" % (screen_w, screen_h))
        root.attributes("-fullscreen", True)
    try:
        stream = make_stream(win_w, win_h - status.winfo_reqheight(), opts.min_px)
    except BaseException:
        root.destroy()
        raise
    state = _SendState(stream, opts)
    images = []
    use_pgm = [True]
    timer = {"next": time.monotonic(), "job": None}

    def on_key(event):
        if event.keysym in ("f", "F"):
            root.attributes("-fullscreen", not int(root.attributes("-fullscreen")))
        elif not state.handle_key(event.keysym):
            root.destroy()

    def tick():
        if state.quit:
            root.destroy()
            return
        w, h = canvas.winfo_width(), canvas.winfo_height()
        if w < 64 or h < 64:  # not mapped yet
            timer["job"] = root.after(50, tick)
            return
        size = stream.size
        cols, rows, px = plan_grid(w, h, size, opts.min_px, opts.max_codes, opts.grid)
        if px < 1:
            canvas.delete("all")
            canvas.create_text(w // 2, h // 2, text="Window too small for the QR codes - enlarge it")
            timer["job"] = root.after(200, tick)
            return
        codes = state.advance(cols * rows)
        pitch = (size + QUIET) * px
        x0 = (w - cols * pitch - QUIET * px) // 2 + QUIET * px
        y0 = (h - rows * pitch - QUIET * px) // 2 + QUIET * px
        canvas.delete("all")
        del images[:]
        for i, code in enumerate(codes):
            img = _tk_photo(tk, code, size, use_pgm)
            if px > 1:
                img = img.zoom(px)
            images.append(img)
            canvas.create_image(x0 + i % cols * pitch, y0 + i // cols * pitch, image=img, anchor="nw")
        status.configure(text=state.status(len(codes)))
        now = time.monotonic()
        timer["next"] = max(timer["next"] + 1.0 / state.fps, now)
        timer["job"] = root.after(max(1, int((timer["next"] - now) * 1000)), tick)

    def on_signal(signum, frame):
        # Ctrl+C would otherwise surface inside a Tk callback and be swallowed.
        state.quit = True

    root.bind("<Key>", on_key)
    root.after(100, tick)
    previous = {sig: signal.signal(sig, on_signal) for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        root.mainloop()
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    return state


_HALF_BLOCK_DIGITS = str.maketrans("0123", " \u2584\u2580\u2588")  # 2 * top + bottom module


def _row_msb_left(row, size, px):
    """Code row (bit x = column x) as an int whose most significant bit is column 0."""
    bits = format(row, "0%db" % size)[::-1]
    if px > 1:
        bits = "".join(ch * px for ch in bits)
    return int(bits, 2)


def _module_canvas(codes, size, cols, px, width, height):
    """Lay codes out on a width x height module grid; rows are ints, MSB = left."""
    canvas = [0] * height
    pitch = (size + QUIET) * px
    nrows = -(-len(codes) // cols)
    x0 = (width - cols * pitch - QUIET * px) // 2 + QUIET * px
    y0 = (height - nrows * pitch - QUIET * px) // 2 + QUIET * px
    for i, code in enumerate(codes):
        ox = x0 + i % cols * pitch
        oy = y0 + i // cols * pitch
        shift = width - ox - size * px
        for y, row in enumerate(code):
            scaled = _row_msb_left(row, size, px) << shift
            for t in range(px):
                canvas[oy + y * px + t] |= scaled
    return [format(r, "0%db" % width) for r in canvas]


def terminal_grid_units(columns, lines, utf8):
    """Module grid available in a terminal: half blocks give 1x2 modules per cell,
    the ASCII fallback uses two background-coloured spaces per module."""
    return (columns, 2 * lines) if utf8 else (columns // 2, lines)


def render_terminal(codes, size, cols, px, columns, lines, utf8=True):
    """Return `lines` strings that draw the codes in a terminal of `columns` width."""
    width, height = terminal_grid_units(columns, lines, utf8)
    canvas = _module_canvas(codes, size, cols, px, width, height)
    if utf8:
        # Read both 0/1 rows as hex numbers: 2*top + bottom then has one digit 0-3 per
        # column (no carries), which translate() turns into the matching half block.
        return [format(2 * int(top, 16) + int(bottom, 16), "0%dx" % width).translate(_HALF_BLOCK_DIGITS)
                for top, bottom in zip(canvas[0::2], canvas[1::2])]
    pad = " " * (columns - 2 * width)
    return ["".join(("\x1b[40m" if run[0] == "1" else "\x1b[47;107m") + "  " * len(run)
                    for run in _RUNS_RE.findall(row)) + "\x1b[47;107m" + pad
            for row in canvas]


_RUNS_RE = re.compile(r"0+|1+")


def _terminal_is_utf8(fd, out):
    """Print two UTF-8 characters and ask the terminal where the cursor went. The "e"
    with an accent matters: an 8-bit xterm draws only the first byte of the block
    character (the other two are C1 controls), but both bytes of the accented e."""
    import select
    out.write("\r\u2580\u00e9\x1b[6n")
    out.flush()
    reply = b""
    deadline = time.monotonic() + 0.5
    while b"R" not in reply:
        timeout = deadline - time.monotonic()
        if timeout <= 0 or not select.select([fd], [], [], timeout)[0]:
            break
        reply += os.read(fd, 64)
    out.write("\r\x1b[K")
    m = re.search(rb"\x1b\[\d+;(\d+)R", reply)
    return m is None or int(m.group(1)) == 3  # no answer: assume a modern terminal


def run_terminal_sender(make_stream, opts):
    """Draw the codes in this terminal; make_stream as for run_tk_sender (one module
    per character cell column, so min_px is 1)."""
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        raise QrxferError("--terminal needs an interactive terminal")
    import select
    import termios
    import tty

    fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd)
    out = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", write_through=True)  # any locale
    state = None
    alternate = False
    try:
        tty.setcbreak(fd)
        utf8 = _terminal_is_utf8(fd, out)
        columns, lines = shutil.get_terminal_size()
        width, height = terminal_grid_units(columns, lines - 1, utf8)
        stream = make_stream(width, height, 1)
        sys.stdout.flush()
        state = _SendState(stream, opts)
        out.write("\x1b[?1049h\x1b[?25l")  # alternate screen, hide cursor
        alternate = True
        next_t = time.monotonic()
        while True:
            columns, lines = shutil.get_terminal_size()
            lines -= 1  # status line
            width, height = terminal_grid_units(columns, lines, utf8)
            cols, rows, px = plan_grid(width, height, stream.size, 1, opts.max_codes, opts.grid)
            if px < 1:
                need = stream.size + 2 * QUIET
                need_cols, need_lines = (need, need // 2 + 2) if utf8 else (2 * need, need + 1)
                out.write("\x1b[0m\x1b[H\x1b[2JTerminal too small: need at least %d columns x %d lines "
                          "(maximize it, shrink the font, or use --density low). q quits."
                          % (need_cols, need_lines))
            else:
                codes = state.advance(cols * rows)
                frame_lines = render_terminal(codes, stream.size, cols, px, columns, lines, utf8)
                status = state.status(len(codes))[:columns - 1]
                out.write("\x1b[H\x1b[30;47;107m" + "\r\n".join(frame_lines)
                          + "\x1b[0m\r\n" + status + "\x1b[K")
            out.flush()
            next_t = max(next_t + 1.0 / state.fps, time.monotonic())
            while True:  # keys are polled even when drawing cannot keep up with --fps
                ready, _, _ = select.select([fd], [], [], max(0.0, next_t - time.monotonic()))
                if not ready:
                    break
                if not state.handle_key(os.read(fd, 1).decode("latin-1")):
                    return state
    except KeyboardInterrupt:
        if state is None:
            raise
        return state
    finally:
        if alternate:
            out.write("\x1b[0m\x1b[?25h\x1b[?1049l")
        out.flush()
        out.detach()
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)


# ---------------------------------------------------------------------------
# Colour grid sender: a full-screen X11 window (libX11 through ctypes, so still no
# packages) filled with cells in the 8 corner colours of the RGB cube, 3 bits each.
# Every row of cells carries one binary fountain frame, so a torn screen only loses
# the rows the tear crosses. A black/white timing ring around the cells shows the
# receiver every cell boundary. VNC servers send this sharp-edged content with
# few colours losslessly, and it carries ~10x more per screen than QR codes.
# ---------------------------------------------------------------------------

GRID_CELL_PX = 3       # default cell size in pixels
GRID_STATUS_PX = 18    # status line below the grid
GRID_MIN_BLOCK = 32    # bytes per row below which the screen is too small
GRID_MAX_CELL_PX = 16  # largest cell size the receiver looks for
GRID_MIN_COLS = 59     # narrowest row that holds a frame (block of 1 byte)


def plan_cells(width, height, px):
    """Data cells (cols, rows) for a width x height pixel area. Both counts are odd, so
    all four corners of the one-cell timing ring are white; a one-cell black margin
    separates the ring from whatever surrounds the area."""
    cols, rows = width // px - 4, height // px - 4
    return cols - (cols + 1) % 2, rows - (rows + 1) % 2


def grid_row_bytes(cols):
    """Frame bytes that one row of `cols` cells carries (3 bits per cell)."""
    return 3 * cols // 8


def grid_block_size(cols):
    """Fountain block size that one row of `cols` cells carries."""
    return grid_row_bytes(cols) - FRAME_OVERHEAD


@functools.lru_cache(maxsize=4)
def grid_whitening(nbytes, rows):
    """Fixed pattern for `rows` rows of `nbytes` each: row y XORs its cells with bytes
    y*nbytes..(y+1)*nbytes, so cells look random even where frames hold runs of zeros
    or fields that all rows share (header, padding of a short last block)."""
    return hashlib.shake_256(b"qrxfer grid").digest(nbytes * rows)


def _grid_row_pattern(cols, rows, y):
    """Row y's whitening as a 3*cols-bit int (the first bits of its pattern bytes)."""
    nbytes = (3 * cols + 7) // 8
    row = grid_whitening(nbytes, rows)[y * nbytes:(y + 1) * nbytes]
    return int.from_bytes(row, "big") >> (8 * nbytes - 3 * cols)


def grid_row_digits(raw, cols, y):
    """Frame bytes shown in row y (0 = top) as `cols` octal digits, one per cell: colour
    = 4*red + 2*green + blue. The frame's bits come first, most significant first, then
    zeros up to 3*cols bits; all of them are XORed with the row's whitening."""
    n = int.from_bytes(raw, "big") << (3 * cols - 8 * len(raw))
    return format(n ^ _grid_row_pattern(cols, y + 1, y), "0%do" % cols)


# A receiver asks for the blocks it still misses by typing keys into the VNC viewer,
# which reach the grid sender's keyboard grab: REQUEST_KEY, numbers, Return. A number is
# hex digits, most significant first, its last digit from _REQUEST_LAST and the others
# from _REQUEST_MORE, so it needs no separator; neither set has q (stops the sender) or
# space (pauses it). The numbers: the transfer id, then per range of blocks the gap from
# the end of the range before (from 0 for the first) and the length minus one, then the
# CRC32 of the keys between REQUEST_KEY and it.
REQUEST_KEY = "x"
_REQUEST_LAST, _REQUEST_MORE = "0123456789abcdef", "ghijklmnoprstuvw"
REQUEST_MAX_KEYS = 1 << 16


def _request_number(n):
    digits = [_REQUEST_LAST[n & 15]]
    n >>= 4
    while n:
        digits.append(_REQUEST_MORE[n & 15])
        n >>= 4
    return "".join(reversed(digits))


def encode_request(tid, ranges):
    """The keys asking the sender of transfer tid for these (start, stop) block ranges."""
    body, end = [_request_number(tid)], 0
    for start, stop in ranges:
        body += [_request_number(start - end), _request_number(stop - start - 1)]
        end = stop
    body = "".join(body)
    return REQUEST_KEY + body + _request_number(zlib.crc32(body.encode("ascii")))


def parse_request(keys, tid, k):
    """The (start, stop) ranges of a request typed after REQUEST_KEY; ValueError unless
    the keys arrived intact and ask for blocks of this transfer."""
    numbers, begin, n = [], 0, 0
    for pos, key in enumerate(keys):
        digit = _REQUEST_LAST.find(key)
        if digit >= 0:
            numbers.append((n << 4 | digit, begin))
            begin, n = pos + 1, 0
            continue
        digit = _REQUEST_MORE.find(key)
        if digit < 0:
            raise ValueError("unexpected key %r in a request" % key)
        n = n << 4 | digit
    if begin != len(keys) or len(numbers) < 2 or len(numbers) % 2:
        raise ValueError("incomplete request")
    crc, at = numbers.pop()
    if crc != zlib.crc32(keys[:at].encode("ascii")) or numbers[0][0] != tid:
        raise ValueError("request damaged or for another transfer")
    ranges, end = [], 0
    for i in range(1, len(numbers), 2):
        start = end + numbers[i][0]
        end = start + numbers[i + 1][0] + 1
        if end > k:
            raise ValueError("request beyond the last block")
        ranges.append((start, end))
    return ranges


class GridStream:
    """Turns fountain frames into grid rows, one frame per row of cells. After the first
    pass, each screen of new repair frames is followed by screens repeating the
    systematic frames, so a receiver started late gets most blocks on their own (cheap
    to decode).

    From 65,536 blocks on, three repeat screens follow each repair screen instead of one:
    a receiver started late then gets the blocks twice as fast, while one that started
    first finishes as soon. Every screen after the first pass also makes a share of the
    next repair screen, so all screens take about as long to make and stay up about as
    long: made at once, the repair frames kept the screen before them up four times as
    long as the repair screen itself.

    Each round of repeats puts every block a different number of rows away from where
    the first pass showed it (golden-section steps): a band of rows covered on the
    receiver's screen, such as a dock popping up over the bottom of the VNC viewer, then
    hides different blocks every time instead of the same ones.

    A receiver may ask for the blocks it misses (see encode_request). After the first
    pass, those come next, on screens of their own and last block first, then the
    stream goes on where it was.
    """

    unit = "row"
    MANY_REPEATS_K = 1 << 16  # blocks from which three repeat screens follow a repair screen

    def __init__(self, encoder, cols, rows):
        self.encoder, self.cols, self.rows = encoder, cols, rows
        self.seq = 0  # frames shown
        self.repeats = 3 if encoder.k >= self.MANY_REPEATS_K else 1
        self._repair, self._repeat, self._after = encoder.k, 0, 0  # _repeat: repeat frames shown
        self._step = max(1, int(rows * 0.381966 + 0.5)) if rows > 1 else 0  # rows shifted per round
        self._ahead = []  # frames of the next repair screen, made in shares
        self._wanted, self._serving, self._left = None, None, 0  # the latest request

    def phase(self):
        k = self.encoder.k
        if self.seq < k:
            return "first pass %d%%" % (self.seq * 100 // k)
        if self._wanted is not None:
            return "requested blocks, %d left" % (self._left if self._serving else self._wanted[1])
        return "repair + repeats"

    def request(self, ranges):
        """Show the blocks of these (start, stop) ranges next; replaces an earlier request."""
        total = sum(stop - start for start, stop in ranges)
        self._wanted, self._serving = ((ranges, total) if total else None), None

    def _requested(self, count):
        # Last block first, which no other screen does: that tells the receiver its request
        # arrived. A short last screen is filled up with the same blocks again.
        if self._serving is None:
            ranges, self._left = self._wanted
            self._serving = (b for _ in itertools.count() for start, stop in reversed(ranges)
                             for b in range(stop - 1, start - 1, -1))
        out = [frame_bytes(self.encoder.frame(next(self._serving))) for _ in range(count)]
        self._left -= count
        if self._left <= 0:
            self._wanted = self._serving = None
        return out

    def _repairs(self, count):
        out = [frame_bytes(self.encoder.frame(seq)) for seq in range(self._repair, self._repair + count)]
        self._repair += count
        return out

    def repeat_block(self, n):
        """Block shown as repeat frame n. Repeat frames fill whole screens, so frame n sits
        in row n % rows; round c starts at a block below `rows` chosen so that each block
        lands (c + 1) * _step rows (mod rows) above its row in the first pass."""
        k, rows = self.encoder.k, self.rows
        c, j = divmod(n, k)
        start = (c * k + (c + 1) * self._step) % rows if rows > 1 else 0
        return (j + start) % k

    def take(self, count):
        k = self.encoder.k
        if self.seq < k:  # the first pass; its last screen is filled up with repair frames
            n = min(count, k - self.seq)
            out = [frame_bytes(self.encoder.frame(seq)) for seq in range(self.seq, self.seq + n)]
            out += self._repairs(count - n)
        elif self._wanted is not None:
            out = self._requested(count)
        else:
            if self._after % (self.repeats + 1):
                out = [frame_bytes(self.encoder.frame(self.repeat_block(n)))
                       for n in range(self._repeat, self._repeat + count)]
                self._repeat += count
            else:
                out, self._ahead = self._ahead[:count], self._ahead[count:]
                out += self._repairs(count - len(out))
            self._after += 1
            share = min(-(-count // (self.repeats + 1)), count - len(self._ahead))
            self._ahead += self._repairs(max(0, share))
        self.seq += count
        return out


class GridPainter:
    """Renders a screen of rows, ring included, as 32-bit pixels. Each line of cells is
    one join over a table of pre-rendered cells, fast enough for Python 3.6."""

    def __init__(self, cols, rows, px, pixel):
        self.cols, self.rows, self.px = cols, rows, px
        self.patterns = None
        self.width, self.height = (cols + 2) * px, (rows + 2) * px
        self.table = [b""] * 256
        for s in range(8):
            self.table[ord("0") + s] = pixel(255 * (s >> 2), 255 * (s >> 1 & 1), 255 * (s & 1)) * px
        # Ring cell (x, y) is white (7) where x + y is even, black (0) elsewhere.
        self.ring = self._line(("70" * (cols + 2))[:cols + 2])

    def _line(self, cells):
        return b"".join(map(self.table.__getitem__, cells.encode("ascii"))) * self.px

    def paint(self, frames):
        if self.patterns is None:
            self.patterns = [_grid_row_pattern(self.cols, self.rows, y) for y in range(self.rows)]
        shift, fmt = 3 * self.cols - 8 * grid_row_bytes(self.cols), "0%do" % self.cols
        lines = [self.ring]
        for y, raw in enumerate(frames):
            n = (int.from_bytes(raw, "big") << shift) ^ self.patterns[y]
            edge = "70"[y % 2 == 0]  # ring cells (0, y + 1) and (cols + 1, y + 1)
            lines.append(self._line(edge + format(n, fmt) + edge))
        lines.append(self.ring)
        return b"".join(lines)


class _XImage(ctypes.Structure):
    _fields_ = [
        ("width", ctypes.c_int), ("height", ctypes.c_int), ("xoffset", ctypes.c_int),
        ("format", ctypes.c_int), ("data", ctypes.c_void_p), ("byte_order", ctypes.c_int),
        ("bitmap_unit", ctypes.c_int), ("bitmap_bit_order", ctypes.c_int), ("bitmap_pad", ctypes.c_int),
        ("depth", ctypes.c_int), ("bytes_per_line", ctypes.c_int), ("bits_per_pixel", ctypes.c_int),
        ("red_mask", ctypes.c_ulong), ("green_mask", ctypes.c_ulong), ("blue_mask", ctypes.c_ulong),
    ]


class _XSetWindowAttributes(ctypes.Structure):
    _fields_ = [
        ("background_pixmap", ctypes.c_ulong), ("background_pixel", ctypes.c_ulong),
        ("border_pixmap", ctypes.c_ulong), ("border_pixel", ctypes.c_ulong),
        ("bit_gravity", ctypes.c_int), ("win_gravity", ctypes.c_int), ("backing_store", ctypes.c_int),
        ("backing_planes", ctypes.c_ulong), ("backing_pixel", ctypes.c_ulong), ("save_under", ctypes.c_int),
        ("event_mask", ctypes.c_long), ("do_not_propagate_mask", ctypes.c_long),
        ("override_redirect", ctypes.c_int), ("colormap", ctypes.c_ulong), ("cursor", ctypes.c_ulong),
    ]


class _XColor(ctypes.Structure):
    _fields_ = [("pixel", ctypes.c_ulong), ("red", ctypes.c_ushort), ("green", ctypes.c_ushort),
                ("blue", ctypes.c_ushort), ("flags", ctypes.c_char), ("pad", ctypes.c_char)]


_XLIB = []


def _xlib():
    """libX11 with the prototypes used by the grid window and the screen grabber."""
    if _XLIB:
        return _XLIB[0]
    libname = ctypes.util.find_library("X11") or "libX11.so.6"
    try:
        x = ctypes.CDLL(libname)
    except OSError:
        raise QrxferError("libX11 not found; the grid window and screen capture need an X11 session")
    vp, ul, i, ui = ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int, ctypes.c_uint
    image = ctypes.POINTER(_XImage)
    for name, res, args in (
            ("XOpenDisplay", vp, [ctypes.c_char_p]), ("XCloseDisplay", i, [vp]),
            ("XDefaultScreen", i, [vp]), ("XRootWindow", ul, [vp, i]),
            ("XDisplayWidth", i, [vp, i]), ("XDisplayHeight", i, [vp, i]),
            ("XDefaultDepth", i, [vp, i]), ("XDefaultVisual", vp, [vp, i]),
            ("XGetImage", image, [vp, ul, i, i, ui, ui, ul, i]), ("XDestroyImage", i, [image]),
            ("XCreateImage", image, [vp, vp, ui, i, i, vp, ui, ui, i, i]),
            ("XPutImage", i, [vp, ul, vp, image, i, i, i, i, ui, ui]),
            ("XCreateWindow", ul, [vp, ul, i, i, ui, ui, ui, i, ui, vp, ul,
                                   ctypes.POINTER(_XSetWindowAttributes)]),
            ("XStoreName", i, [vp, ul, ctypes.c_char_p]), ("XSelectInput", i, [vp, ul, ctypes.c_long]),
            ("XMapRaised", i, [vp, ul]), ("XDestroyWindow", i, [vp, ul]),
            ("XGrabKeyboard", i, [vp, ul, i, i, i, ul]), ("XUngrabKeyboard", i, [vp, ul]),
            ("XPending", i, [vp]), ("XNextEvent", i, [vp, vp]),
            ("XLookupString", i, [vp, ctypes.c_char_p, i, vp, vp]),
            ("XCreateGC", vp, [vp, ul, ul, vp]), ("XFreeGC", i, [vp, vp]),
            ("XSetForeground", i, [vp, vp, ul]), ("XSetBackground", i, [vp, vp, ul]),
            ("XDrawImageString", i, [vp, ul, vp, i, i, ctypes.c_char_p, i]),
            ("XCreateBitmapFromData", ul, [vp, ul, ctypes.c_char_p, ui, ui]),
            ("XCreatePixmapCursor", ul, [vp, ul, ul, ctypes.POINTER(_XColor), ctypes.POINTER(_XColor), ui, ui]),
            ("XDefineCursor", i, [vp, ul, ul]), ("XFreeCursor", i, [vp, ul]), ("XFreePixmap", i, [vp, ul]),
            ("XGetClassHint", i, [vp, ul, vp]), ("XFree", i, [vp]),
            ("XSync", i, [vp, i])):
        getattr(x, name).restype = res
        getattr(x, name).argtypes = args
    _XLIB.append(x)
    return x


def _pixel_format(image):
    """For a 32-bit TrueColor XImage: pixel(r, g, b) -> its 4 bytes, and the byte index
    of red, green and blue within a pixel."""
    im = image.contents
    if im.bits_per_pixel != 32:
        raise QrxferError("unsupported screen depth: %d bits per pixel" % im.bits_per_pixel)
    shifts = []
    for mask in (im.red_mask, im.green_mask, im.blue_mask):
        shift = (mask & -mask).bit_length() - 1
        if shift < 0 or mask >> shift != 0xFF:
            raise QrxferError("unsupported visual (colour masks %#x %#x %#x)"
                              % (im.red_mask, im.green_mask, im.blue_mask))
        shifts.append(shift)
    order = "big" if im.byte_order else "little"

    def pixel(r, g, b):
        return (r << shifts[0] | g << shifts[1] | b << shifts[2]).to_bytes(4, order)

    return pixel, tuple(s // 8 if order == "little" else 3 - s // 8 for s in shifts)


class X11GridWindow:
    """A full-screen override-redirect window that shows grid screens with XPutImage.

    Override-redirect puts it exactly over the whole screen, with or without a window
    manager. Such a window never gets the focus, so it grabs the keyboard (keys typed
    into the VNC viewer reach it), and it hides the mouse pointer, which would
    otherwise cover cells."""

    def __init__(self):
        self.x = x = _xlib()
        self.dpy = x.XOpenDisplay(None)
        if not self.dpy:
            raise QrxferError("cannot open X display %r - run this inside the desktop session, "
                              "or use --terminal" % os.environ.get("DISPLAY", ""))
        self.screen = x.XDefaultScreen(self.dpy)
        self.width = x.XDisplayWidth(self.dpy, self.screen)
        self.height = x.XDisplayHeight(self.dpy, self.screen)
        self.win = self.image = self.gc = None
        self.grabbed = False

    def open(self, cols, rows, px):
        x, dpy, scr = self.x, self.dpy, self.screen
        if x.XDefaultDepth(dpy, scr) != 24:
            raise QrxferError("the grid needs a 24-bit colour display (depth here: %d); use --codec qr"
                              % x.XDefaultDepth(dpy, scr))
        visual = x.XDefaultVisual(dpy, scr)
        attrs = _XSetWindowAttributes(override_redirect=1, background_pixel=0)
        cw_back_pixel, cw_override_redirect = 1 << 1, 1 << 9
        self.win = x.XCreateWindow(dpy, x.XRootWindow(dpy, scr), 0, 0, self.width, self.height, 0, 24, 1,
                                   visual, cw_back_pixel | cw_override_redirect, ctypes.byref(attrs))
        x.XStoreName(dpy, self.win, b"qrxfer send")
        x.XSelectInput(dpy, self.win, 1)  # KeyPressMask
        blank = x.XCreateBitmapFromData(dpy, self.win, bytes(8), 8, 8)
        colour = _XColor()
        cursor = x.XCreatePixmapCursor(dpy, blank, blank, colour, colour, 0, 0)
        x.XDefineCursor(dpy, self.win, cursor)
        x.XFreeCursor(dpy, cursor)
        x.XFreePixmap(dpy, blank)
        x.XMapRaised(dpy, self.win)
        x.XSync(dpy, 0)
        for _ in range(20):  # GrabSuccess once the window is viewable and no one else grabs
            if x.XGrabKeyboard(dpy, self.win, 0, 1, 1, 0) == 0:
                self.grabbed = True
                break
            time.sleep(0.05)
        w, h = (cols + 2) * px, (rows + 2) * px
        self.buf = ctypes.create_string_buffer(w * h * 4)
        self.image = x.XCreateImage(dpy, visual, 24, 2, 0, self.buf, w, h, 32, 0)  # ZPixmap
        pixel, _ = _pixel_format(self.image)
        self.painter = GridPainter(cols, rows, px, pixel)
        self.origin = ((self.width - w) // 2, (self.height - GRID_STATUS_PX - h) // 2)
        im = self.image.contents
        self.gc = x.XCreateGC(dpy, self.win, 0, None)
        x.XSetForeground(dpy, self.gc, im.red_mask | im.green_mask | im.blue_mask)
        x.XSetBackground(dpy, self.gc, 0)
        self.event = (ctypes.c_long * 24)()  # an XEvent union
        self.text = ctypes.create_string_buffer(8)

    def show(self, frames):
        data = self.painter.paint(frames)
        ctypes.memmove(self.buf, data, len(data))
        self.x.XPutImage(self.dpy, self.win, self.gc, self.image, 0, 0, self.origin[0], self.origin[1],
                         self.painter.width, self.painter.height)
        self.x.XSync(self.dpy, 0)  # wait for the server: no queue of stale screens builds up

    def status(self, text):
        data = text.encode("ascii", "replace")[:self.width // 6].ljust(self.width // 6)
        self.x.XDrawImageString(self.dpy, self.win, self.gc, 6, self.height - 5, data, len(data))

    def keys(self):
        """Characters typed since the last call."""
        out = []
        while self.x.XPending(self.dpy):
            self.x.XNextEvent(self.dpy, self.event)
            if ctypes.c_int.from_buffer(self.event).value == 2:  # KeyPress
                n = self.x.XLookupString(self.event, self.text, 8, None, None)
                if n > 0:
                    out.append(self.text.raw[:n].decode("latin-1"))
        return out

    def close(self):
        x, dpy = self.x, self.dpy
        if not dpy:
            return
        self.dpy = None
        if self.grabbed:
            x.XUngrabKeyboard(dpy, 0)
        if self.image:
            self.image.contents.data = None  # the pixels belong to self.buf
            x.XDestroyImage(self.image)
        if self.gc:
            x.XFreeGC(dpy, self.gc)
        if self.win:
            x.XDestroyWindow(dpy, self.win)
        x.XCloseDisplay(dpy)


def run_x11_grid_sender(window, make_stream, opts):
    """Show grid screens in `window` (an X11GridWindow). make_stream(width, height, px)
    returns the GridStream for the area above the status line."""
    state = None
    stop = []

    def on_signal(signum, frame):
        # Set explicitly: a background job may start with SIGINT ignored.
        stop.append(signum)

    previous = {sig: signal.signal(sig, on_signal) for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        stream = make_stream(window.width, window.height - GRID_STATUS_PX, opts.cell_px)
        window.open(stream.cols, stream.rows, opts.cell_px)
        state = _SendState(stream, opts)
        note = "" if window.grabbed else "  (keyboard busy: stop with Ctrl+C in the terminal)"
        shown = None
        next_t = time.monotonic()
        while not stop:
            rows = state.advance(stream.rows)
            if rows is not shown:  # the same list while paused
                window.show(rows)
                shown = rows
            window.status(state.status(len(rows)) + note)
            next_t = max(next_t + 1.0 / state.fps, time.monotonic())
            while not stop:  # keys are polled even when drawing cannot keep up with --fps
                for key in window.keys():
                    if key == "\x03" or not state.handle_key(key):
                        return state
                delay = next_t - time.monotonic()
                if delay <= 0:
                    break
                time.sleep(min(delay, 0.02))
        return state
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
        window.close()


# ---------------------------------------------------------------------------
# Receiver: screen capture, grid and QR decoding, and the collection loop
# ---------------------------------------------------------------------------

def _need_numpy():
    try:
        import numpy
        return numpy
    except ImportError:
        raise QrxferError("numpy is required to receive: sudo apt install python3-numpy (or pip install numpy)")


class _XShmSegmentInfo(ctypes.Structure):
    _fields_ = [("shmseg", ctypes.c_ulong), ("shmid", ctypes.c_int), ("shmaddr", ctypes.c_void_p),
                ("readOnly", ctypes.c_int)]


class XlibGrabber:
    """Screen capture of any region of the root window: through MIT-SHM when the X server
    offers it for this connection (a shared memory segment per region size), otherwise
    through XGetImage. Each screen must be caught while it is up, and receiving falls
    apart once captures come less often than screens change (measured through a VNC hop).
    With the VNC viewer redrawing, an XGetImage of a 2560x1351 region took 56 ms on
    average and up to 160 ms on a real desktop; MIT-SHM takes half to a third of that."""

    name = "Xlib"
    SHM_SIZES = 4  # region sizes that keep a segment

    def __init__(self):
        self.np = _need_numpy()
        x = self.x = _xlib()
        # A failed request must not abort the process (Xlib's default error handler exits).
        self._on_error = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)(lambda d, e: 0)
        x.XSetErrorHandler(self._on_error)
        self.dpy = x.XOpenDisplay(None)
        if not self.dpy:
            raise QrxferError("cannot open X display %r; run this inside the desktop session"
                              % os.environ.get("DISPLAY", ""))
        screen = x.XDefaultScreen(self.dpy)
        self.root = x.XRootWindow(self.dpy, screen)
        self.width = x.XDisplayWidth(self.dpy, screen)
        self.height = x.XDisplayHeight(self.dpy, screen)
        img = self._get(0, 0, 1, 1)
        try:
            self.channels = _pixel_format(img)[1]  # byte index of red, green, blue
        finally:
            x.XDestroyImage(img)
        self._shm, self._segments = None, collections.OrderedDict()
        try:
            self._shm_open(screen)
        except (OSError, AttributeError, QrxferError):
            self._shm_close()

    def _shm_open(self, screen):
        xext = ctypes.CDLL(ctypes.util.find_library("Xext") or "libXext.so.6")
        libc = ctypes.CDLL(None, use_errno=True)
        vp, ul, i, ui = ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int, ctypes.c_uint
        info = ctypes.POINTER(_XShmSegmentInfo)
        for lib, name, res, args in (
                (xext, "XShmQueryExtension", i, [vp]),
                (xext, "XShmCreateImage", ctypes.POINTER(_XImage), [vp, vp, ui, i, vp, info, ui, ui]),
                (xext, "XShmAttach", i, [vp, info]), (xext, "XShmDetach", i, [vp, info]),
                (xext, "XShmGetImage", i, [vp, ul, ctypes.POINTER(_XImage), i, i, ul]),
                (libc, "shmget", i, [i, ctypes.c_size_t, i]), (libc, "shmat", vp, [i, vp, i]),
                (libc, "shmdt", i, [vp]), (libc, "shmctl", i, [i, i, vp])):
            getattr(lib, name).restype = res
            getattr(lib, name).argtypes = args
        if not xext.XShmQueryExtension(self.dpy):
            return
        self._shm = (xext, libc, self.x.XDefaultVisual(self.dpy, screen), self.x.XDefaultDepth(self.dpy, screen))
        # The extension may be there while the segment cannot be shared (an X server on
        # another machine): then the first capture fails and XGetImage is used instead.
        if self._shm_view(0, 0, 1, 1) is None:
            raise QrxferError("MIT-SHM unusable")
        self.name = "Xlib (MIT-SHM)"

    def _shm_close(self):
        for size in list(self._segments):
            self._shm_free(self._segments.pop(size))
        self._shm, self.name = None, XlibGrabber.name

    def _shm_free(self, segment):
        img, info, _ = segment
        xext, libc = self._shm[:2]
        xext.XShmDetach(self.dpy, ctypes.byref(info))
        self.x.XSync(self.dpy, 0)
        img.contents.data = None  # the segment is not XDestroyImage's to free
        self.x.XDestroyImage(img)
        libc.shmdt(info.shmaddr)

    def _shm_view(self, left, top, width, height):
        """The region's pixels as a view of the shared segment, or None."""
        xext, libc, visual, depth = self._shm
        segment = self._segments.pop((width, height), None)
        if segment is None:
            info = _XShmSegmentInfo()
            img = xext.XShmCreateImage(self.dpy, visual, depth, 2, None, ctypes.byref(info), width, height)
            if not img:
                return None
            size = img.contents.bytes_per_line * height
            info.shmid = libc.shmget(0, size, 0o1600)  # IPC_PRIVATE; IPC_CREAT | 0600
            addr = libc.shmat(info.shmid, None, 0) if info.shmid >= 0 else None
            if addr in (None, ctypes.c_void_p(-1).value):
                if info.shmid >= 0:
                    libc.shmctl(info.shmid, 0, None)
                img.contents.data = None
                self.x.XDestroyImage(img)
                return None
            info.shmaddr = img.contents.data = addr
            xext.XShmAttach(self.dpy, ctypes.byref(info))
            self.x.XSync(self.dpy, 0)
            libc.shmctl(info.shmid, 0, None)  # IPC_RMID: gone once both sides let go, even after a crash
            view = self.np.frombuffer((ctypes.c_uint8 * size).from_address(addr), self.np.uint8)
            segment = (img, info, view.reshape(height, img.contents.bytes_per_line // 4, 4))
            while len(self._segments) >= self.SHM_SIZES:
                self._shm_free(self._segments.popitem(last=False)[1])
        self._segments[(width, height)] = segment
        if not xext.XShmGetImage(self.dpy, self.root, segment[0], left, top, 0xFFFFFFFF):
            return None
        return segment[2][:, :width]

    def _get(self, left, top, width, height):
        img = self.x.XGetImage(self.dpy, self.root, left, top, width, height, 0xFFFFFFFF, 2)
        if not img:
            raise QrxferError("screen capture failed")
        return img

    def grab(self, left, top, width, height, colour=False):
        """The region as a numpy array: 2-D (the green channel; codes are black on white,
        so one channel is as good as luminance), or with colour the raw 32-bit pixels as
        (height, width, 4) bytes, red, green and blue at the indices in .channels."""
        if self._shm:
            # A copy: decoder threads still read it while the next capture is made.
            pixels = self._shm_view(left, top, width, height)
            if pixels is not None:
                return pixels.copy() if colour else self.np.ascontiguousarray(pixels[..., self.channels[1]])
        img = self._get(left, top, width, height)
        try:
            im = img.contents
            if im.bits_per_pixel != 32:
                raise QrxferError("unsupported screen depth: %d bits per pixel" % im.bits_per_pixel)
            raw = ctypes.string_at(im.data, im.bytes_per_line * height)
            pixels = self.np.frombuffer(raw, self.np.uint8).reshape(height, im.bytes_per_line // 4, 4)
            if colour:
                return pixels[:, :width]
            return self.np.ascontiguousarray(pixels[:, :width, self.channels[1]])
        finally:
            self.x.XDestroyImage(img)


class ZxingDecoder:
    name = "zxing-cpp"

    def __init__(self):
        import zxingcpp
        self.z = zxingcpp
        self.fmt = zxingcpp.BarcodeFormat.QRCode

    def decode(self, gray):
        out = []
        for r in self.z.read_barcodes(gray, formats=self.fmt, try_rotate=False):
            p = r.position
            pts = [(p.top_left.x, p.top_left.y), (p.top_right.x, p.top_right.y),
                   (p.bottom_right.x, p.bottom_right.y), (p.bottom_left.x, p.bottom_left.y)]
            out.append((r.text, pts))
        return out


def _code_bands(gray):
    """Split a capture at blank pixel rows into (top, bottom) bands: each run of rows
    with dark pixels plus the blank rows on both sides. No code spans a blank row.
    Columns dark from top to bottom (window borders, desktop beside it) are ignored."""
    light = gray.max(axis=0) >= 128
    if not light.any():
        return [(0, gray.shape[0])]
    dark = ((gray if light.all() else gray[:, light]).min(axis=1) < 128).tolist()
    runs = []
    for y, d in enumerate(dark):
        if d:
            if runs and runs[-1][1] == y:
                runs[-1][1] = y + 1
            else:
                runs.append([y, y + 1])
    if len(runs) < 2:
        return [(0, len(dark))]
    return [(runs[i - 1][1] if i else 0, runs[i + 1][0] if i + 1 < len(runs) else len(dark))
            for i in range(len(runs))]


class ZbarDecoder:
    """libzbar through ctypes, so no Python binding package is needed.

    zbar returns nothing at all for an image in which one code is torn (a terminal or
    VNC viewer caught halfway through redrawing), so each band of codes is scanned on
    its own. A first pass scans only every second pixel row and column, about 3x faster;
    it misses codes below ~3 px per module, so when it finds nothing, or fewer codes than
    the band held last time, the full-density pass runs as well.
    """

    name = "zbar"

    def __init__(self):
        libname = ctypes.util.find_library("zbar") or "libzbar.so.0"
        try:
            z = self.z = ctypes.CDLL(libname)
        except OSError:
            raise ImportError("libzbar not found")
        vp = ctypes.c_void_p
        for fn, res, args in (
                ("zbar_image_scanner_create", vp, []),
                ("zbar_image_scanner_set_config", ctypes.c_int, [vp, ctypes.c_int, ctypes.c_int, ctypes.c_int]),
                ("zbar_image_create", vp, []),
                ("zbar_image_destroy", None, [vp]),
                ("zbar_image_set_format", None, [vp, ctypes.c_ulong]),
                ("zbar_image_set_size", None, [vp, ctypes.c_uint, ctypes.c_uint]),
                ("zbar_image_set_data", None, [vp, vp, ctypes.c_ulong, vp]),
                ("zbar_scan_image", ctypes.c_int, [vp, vp]),
                ("zbar_image_first_symbol", vp, [vp]),
                ("zbar_symbol_next", vp, [vp]),
                ("zbar_symbol_get_data", vp, [vp]),
                ("zbar_symbol_get_data_length", ctypes.c_uint, [vp]),
                ("zbar_symbol_get_loc_size", ctypes.c_uint, [vp]),
                ("zbar_symbol_get_loc_x", ctypes.c_int, [vp, ctypes.c_uint]),
                ("zbar_symbol_get_loc_y", ctypes.c_int, [vp, ctypes.c_uint])):
            getattr(z, fn).restype = res
            getattr(z, fn).argtypes = args
        self.coarse, self.fine = self._scanner(2), self._scanner(1)
        self.expect = []  # codes found in each band of the previous capture
        self.y800 = int.from_bytes(b"Y800", "little")

    def _scanner(self, density):
        z = self.z
        scanner = z.zbar_image_scanner_create()
        z.zbar_image_scanner_set_config(scanner, 0, 0, 0)    # all symbologies off
        z.zbar_image_scanner_set_config(scanner, 64, 0, 1)   # ZBAR_QRCODE on
        for cfg in (0x100, 0x101):                           # ZBAR_CFG_X_DENSITY, _Y_DENSITY
            z.zbar_image_scanner_set_config(scanner, 0, cfg, density)
        return scanner

    def decode(self, gray):
        out = []
        bands = _code_bands(gray)
        if len(self.expect) != len(bands):
            self.expect = [0] * len(bands)
        for i, (top, bottom) in enumerate(bands):
            band = gray[top:bottom]
            found = self._scan(self.coarse, band)
            if not found or len(found) < self.expect[i]:
                found = max(found, self._scan(self.fine, band), key=len)
            self.expect[i] = len(found)
            out += [(text, [(x, y + top) for x, y in pts]) for text, pts in found]
        return out

    def _scan(self, scanner, gray):
        z = self.z
        h, w = gray.shape
        img = z.zbar_image_create()
        try:
            z.zbar_image_set_format(img, self.y800)
            z.zbar_image_set_size(img, w, h)
            z.zbar_image_set_data(img, gray.ctypes.data, w * h, None)
            z.zbar_scan_image(scanner, img)
            out = []
            sym = z.zbar_image_first_symbol(img)
            while sym:
                text = ctypes.string_at(z.zbar_symbol_get_data(sym), z.zbar_symbol_get_data_length(sym))
                pts = [(z.zbar_symbol_get_loc_x(sym, i), z.zbar_symbol_get_loc_y(sym, i))
                       for i in range(z.zbar_symbol_get_loc_size(sym))]
                out.append((text.decode("latin-1"), pts))
                sym = z.zbar_symbol_next(sym)
            return out
        finally:
            z.zbar_image_destroy(img)


QR_DECODERS = {"zxing": ZxingDecoder, "zbar": ZbarDecoder}


def open_qr_decoder(choice="auto"):
    names = ["zxing", "zbar"] if choice == "auto" else [choice]
    errors = []
    for name in names:
        try:
            return QR_DECODERS[name]()
        except (ImportError, OSError) as exc:
            errors.append("%s: %s" % (name, exc))
    raise QrxferError("no QR decoder available (%s).\n  Install one of: sudo apt install libzbar0  |  "
                      "sudo apt install python3-zxing-cpp  |  pip install zxing-cpp" % "; ".join(errors))


def _longest_run(np, flags, gap):
    """(start, stop) of the longest run of True in a 1-D bool array, bridging gaps of up
    to `gap` False entries, or None."""
    if not flags.any():
        return None
    edges = np.flatnonzero(np.diff(np.concatenate(([0], flags.view(np.int8), [0]))))
    starts, stops = edges[0::2], edges[1::2]
    joined = np.flatnonzero(starts[1:] - stops[:-1] > gap)  # runs that stay separate
    starts = starts[np.concatenate(([0], joined + 1))]
    stops = stops[np.concatenate((joined, [len(stops) - 1]))]
    i = int(np.argmax(stops - starts))
    return int(starts[i]), int(stops[i])


def _chroma(np, img, ch):
    """Strongly coloured pixels: red, green, blue, cyan, magenta, yellow (6 of the 8
    cell colours), which desktops rarely show over large areas."""
    r, g, b = img[..., ch[0]], img[..., ch[1]], img[..., ch[2]]
    return (np.maximum(np.maximum(r, g), b) > 160) & (np.minimum(np.minimum(r, g), b) < 96)


def _dense_box(np, mask, gap):
    """(y0, y1, x0, x1): the longest block of rows dense in mask, then of columns within
    it. Lines count as dense from 35% of the densest one (75% of cells are coloured, but
    a grid of few rows has columns with fewer by chance); `gap` sparse lines are bridged
    (a scaled view blends colours at cell edges)."""
    counts = mask.sum(1)
    ys = _longest_run(np, counts > counts.max() * 0.35, gap) if counts.max() else None
    if ys is None:
        return None
    counts = mask[ys[0]:ys[1]].sum(0)
    xs = _longest_run(np, counts > counts.max() * 0.35, gap)
    return xs and (ys[0], ys[1], xs[0], xs[1])


def _ring_cuts(np, lum, chroma, edge, outward, lo, hi, cells):
    """Cell boundaries along the timing ring beside one edge of the data area.

    Axis 0 of lum and chroma crosses the ring: line `edge` is the first one outside the
    data and `outward` (+1 or -1) leads away from it; lo:hi is the data area along axis
    1. Returns the boundary positions, from lo to hi, or None if no clean ring with at
    least `cells` cells is there (a window or the mouse pointer may cover it)."""
    found = []
    for step in range(GRID_MAX_CELL_PX + 3):
        line = edge + outward * step
        if not 0 <= line < lum.shape[0]:
            break
        seg = lum[line, lo:hi]
        light = seg > 127
        if (chroma[line, lo:hi].mean() < 0.05 and ((seg > 64) & (seg < 192)).mean() < 0.15
                and np.count_nonzero(light[1:] != light[:-1]) >= 8):
            found.append(line)
        elif found or step >= 3:
            break
    if not found:
        return None
    a, b = max(0, lo - 3), min(lum.shape[1], hi + 3)
    light = lum[found[len(found) // 2], a:b] > 127
    cuts = np.flatnonzero(light[1:] != light[:-1]) + 1 + a
    if len(cuts) <= cells:
        return None
    # The cuts nearest the data edges are its first and last boundary; anything beyond
    # them is the outer edge of a ring cell (narrow in a scaled view) or the margin.
    cuts = cuts[int(np.argmin(np.abs(cuts - lo))):int(np.argmin(np.abs(cuts - hi))) + 1]
    if len(cuts) <= cells or abs(int(cuts[0]) - lo) > 2 or abs(int(cuts[-1]) - hi) > 2:
        return None
    gaps = np.diff(cuts)
    gap = float(np.median(gaps))
    if gap < 1.5 or np.abs(gaps - gap).max() > max(1.0, 0.35 * gap):
        return None
    return cuts


class GridGeometry:
    """Pixel coordinates of every cell centre of a grid (numpy int arrays). complete means
    the bottom ring was seen too: a VNC viewer draws the first screen from the top down,
    and a capture of it half-way would otherwise fix a grid with too few rows."""

    def __init__(self, xs, ys, complete=True):
        self.xs, self.ys, self.complete = xs, ys, complete
        self.box = (int(xs[0]), int(ys[0]), int(xs[-1]) + 1, int(ys[-1]) + 1)  # x0, y0, x1, y1

    def moved(self, dx, dy):
        return GridGeometry(self.xs + dx, self.ys + dy, self.complete)

    def within(self, box):
        x, y, w, h = box
        return x <= self.box[0] and y <= self.box[1] and self.box[2] <= x + w and self.box[3] <= y + h


def locate_grid(np, img, ch):
    """Find a grid in a capture of raw pixels (red, green, blue at byte indices ch).

    The data area is the densest block of strongly coloured pixels; the timing rings
    just outside it give every cell boundary, so a scaled VNC view (non-integer cell
    size) still works. Top and left rings are read first, bottom and right ones when
    something covers those. Returns GridGeometry in capture coordinates, or None."""
    coarse = _dense_box(np, _chroma(np, img[::2, ::2], ch), 1)  # cells are at least 2 px wide
    if coarse is None:
        return None
    pad = GRID_MAX_CELL_PX + 8
    top, left = max(0, 2 * coarse[0] - pad), max(0, 2 * coarse[2] - pad)
    crop = img[top:2 * coarse[1] + pad, left:2 * coarse[3] + pad]
    chroma = _chroma(np, crop, ch)
    box = _dense_box(np, chroma, 2)
    if box is None:
        return None
    y0, y1, x0, x1 = box
    if x1 - x0 < 64 or y1 - y0 < 12 or chroma[y0:y1, x0:x1].mean() < 0.55:
        return None
    lum = crop[..., ch[1]]
    cols = GRID_MIN_COLS
    xs = _ring_cuts(np, lum, chroma, y0 - 1, -1, x0, x1, cols)
    bottom = _ring_cuts(np, lum, chroma, y1, 1, x0, x1, cols)
    if xs is None:
        xs = bottom
    ys = _ring_cuts(np, lum.T, chroma.T, x0 - 1, -1, y0, y1, 3)
    if ys is None:
        ys = _ring_cuts(np, lum.T, chroma.T, x1, 1, y0, y1, 3)
    if xs is None or ys is None:
        return None
    return GridGeometry((xs[:-1] + xs[1:]) // 2 + left, (ys[:-1] + ys[1:]) // 2 + top, bottom is not None)


def read_grid_rows(np, img, xs, ys, ch):
    """Sample each cell centre (3 bits: red, green, blue) and return every row's frame bytes."""
    cells = img[np.ix_(ys, xs)]
    bits = np.stack([cells[..., ch[0]] > 127, cells[..., ch[1]] > 127, cells[..., ch[2]] > 127], -1)
    packed = np.packbits(bits.reshape(len(ys), -1), axis=1)
    packed ^= np.frombuffer(grid_whitening(packed.shape[1], len(ys)), np.uint8).reshape(packed.shape)
    nbytes = grid_row_bytes(len(xs))
    return [row[:nbytes].tobytes() for row in packed]


class QrReader:
    """Frames from the QR codes in a grayscale capture."""

    rgb = False
    track_margin = None

    def __init__(self, decoder):
        self.decoder = decoder
        self.name = "QR codes (%s)" % decoder.name

    def clone(self):
        return QrReader(type(self.decoder)())

    def read(self, gray, box, full):
        """Returns [(frame, bounding box on the screen or None)]."""
        out = []
        for text, pts in self.decoder.decode(gray):
            try:
                frame = parse_frame(text)
            except ValueError:
                continue
            found = None
            if pts:
                xs = [p[0] for p in pts]
                ys = [p[1] for p in pts]
                found = (box[0] + min(xs), box[1] + min(ys), box[0] + max(xs), box[1] + max(ys))
            out.append((frame, found))
        return out


class GridReader:
    """Frames from a colour grid. The geometry is found again on every full-screen
    capture and reused for the cropped ones; rows whose CRC fails (torn, covered) are
    skipped. Clones share the geometry."""

    rgb = True
    track_margin = GRID_MAX_CELL_PX + 8  # cropped captures keep the timing rings
    name = "a colour grid"

    RECHECK_EVERY = 0.5  # seconds between searches while the bottom ring is unseen
    COVER_READS = 40     # captures per clone between checks for rows that never read
    NOTICE_EVERY = 60.0  # seconds between notices about such rows

    def __init__(self, np, channels, shared=None):
        self.np, self.channels = np, channels
        self.shared = ({"geometry": None, "searched": 0.0, "notice": None, "noticed": None}
                       if shared is None else shared)
        self._fails, self._reads = None, 0  # per clone: failures by row since the last check

    def clone(self):
        return GridReader(self.np, self.channels, self.shared)

    def take_notice(self):
        """A message about rows that something on the screen keeps covering, once."""
        notice, self.shared["notice"] = self.shared["notice"], None
        return notice

    def read(self, img, box, full):
        geo = self.shared["geometry"]
        located = (full or geo is None or not geo.complete
                   and time.monotonic() - self.shared["searched"] >= self.RECHECK_EVERY)
        if located:
            geo = self._locate(img, box, geo)
        out = self._rows(img, box, geo)
        if not out and not located:  # the window may have moved: look again here
            geo = self._locate(img, box, None)
            out = self._rows(img, box, geo)
        return out

    def _locate(self, img, box, current):
        """Search this capture; a complete grid found earlier is kept over a partial one."""
        self.shared["searched"] = time.monotonic()
        found = locate_grid(self.np, img, self.channels)
        if found is None or current is not None and current.complete and not found.complete:
            return current
        found = self.shared["geometry"] = found.moved(box[0], box[1])
        return found

    def _rows(self, img, box, geo):
        out, failed = [], []
        if geo is None or not geo.within(box):
            return out
        for y, raw in enumerate(read_grid_rows(self.np, img, geo.xs - box[0], geo.ys - box[1], self.channels)):
            try:
                out.append((parse_frame_bytes(raw), geo.box))
            except ValueError:
                failed.append(y)
        if len(out) > len(failed):
            self._watch(geo, failed)
        return out

    def _watch(self, geo, failed):
        # Torn captures fail a row here and there; rows failing in (nearly) every capture
        # are covered, e.g. by a dock over the bottom of the VNC viewer. Their blocks
        # then only arrive in later rounds of repeats, so say so.
        rows = len(geo.ys)
        if self._fails is None or len(self._fails) != rows:
            self._fails, self._reads = [0] * rows, 0
        for y in failed:
            self._fails[y] += 1
        self._reads += 1
        if self._reads < self.COVER_READS:
            return
        dead = [y for y, n in enumerate(self._fails) if n * 10 >= self._reads * 9]
        self._fails, self._reads = [0] * rows, 0
        now, noticed = time.monotonic(), self.shared["noticed"]
        if dead and len(dead) < rows // 2 and (noticed is None or now - noticed >= self.NOTICE_EVERY):
            self.shared["noticed"] = now
            spans, start = [], dead[0]
            for a, b in zip(dead, dead[1:] + [None]):
                if b != a + 1:
                    spans.append("%d" % (start + 1) if a == start else "%d-%d" % (start + 1, a + 1))
                    start = b
            self.shared["notice"] = (
                "Grid row%s %s of %d (screen y %d-%d) failed in nearly all of the last %d captures: "
                "is something covering that part of the window, such as a panel or dock?"
                % ("" if len(dead) == 1 else "s", ", ".join(spans), rows, geo.ys[dead[0]],
                   geo.ys[dead[-1]], self.COVER_READS))


class AutoReader:
    """Grid or QR codes, whichever the sender shows: full-screen captures try both,
    cropped ones only the kind found last."""

    rgb = True

    def __init__(self, grid, qr, shared=None):
        self.grid, self.qr = grid, qr
        self.shared = {"mode": None} if shared is None else shared
        self.name = grid.name + (" or " + qr.name if qr else "")

    @property
    def track_margin(self):
        return self.grid.track_margin if self.shared["mode"] == "grid" else None

    def clone(self):
        return AutoReader(self.grid.clone(), self.qr and self.qr.clone(), self.shared)

    def take_notice(self):
        return self.grid.take_notice()

    def read(self, img, box, full):
        mode = self.shared["mode"]
        if full or mode != "qr":
            found = self.grid.read(img, box, full)
            if found:
                self.shared["mode"] = "grid"
                return found
        if self.qr is None or not (full or mode != "grid"):
            return []
        found = self.qr.read(self.grid.np.ascontiguousarray(img[..., self.grid.channels[1]]), box, full)
        if found:
            self.shared["mode"] = "qr"
        return found


class RegionTracker:
    """Capture only the area around the codes, with periodic full-screen rescans."""

    RESCAN_EVERY = 4.0
    LOST_AFTER = 2.0

    def __init__(self, width, height, fixed=None):
        self.width, self.height = width, height
        self.fixed = fixed
        self.region = None
        self.last_full = self.last_seen = 0.0

    def next_box(self):
        if self.fixed:
            return self.fixed, False
        now = time.monotonic()
        if self.region is None or now - self.last_full >= self.RESCAN_EVERY:
            self.last_full = now
            return (0, 0, self.width, self.height), True
        return self.region, False

    def update(self, boxes, margin=None):
        now = time.monotonic()
        if not boxes:
            if self.region and now - self.last_seen > self.LOST_AFTER:
                self.region = None
            return
        self.last_seen = now
        x0 = min(b[0] for b in boxes)
        y0 = min(b[1] for b in boxes)
        x1 = max(b[2] for b in boxes)
        y1 = max(b[3] for b in boxes)
        if margin is None:
            margin = max(24, max(b[2] - b[0] for b in boxes) // 6)
        x0, y0 = max(0, x0 - margin), max(0, y0 - margin)
        x1, y1 = min(self.width, x1 + margin), min(self.height, y1 + margin)
        if self.region:
            rx, ry, rw, rh = self.region
            if x0 < rx + rw and rx < x1 and y0 < ry + rh and ry < y1:  # overlapping: grow
                x0, y0 = min(x0, rx), min(y0, ry)
                x1, y1 = max(x1, rx + rw), max(y1, ry + rh)
        self.region = (x0, y0, x1 - x0, y1 - y0)


class Progress:
    """The progress line. Its rate is the recent one (RECENT seconds), with the average
    since the first frame beside it: once the first pass is over, blocks arrive far more
    slowly, and the average alone kept looking like a slow link."""

    RECENT = 30.0

    def __init__(self, stream=sys.stderr):
        self.out = stream
        self.tty = stream.isatty()
        self.t0 = None
        self.last = 0.0
        self.waiting_shown = False
        self.samples = collections.deque()  # (time, rank) over the last RECENT seconds

    def note(self, text):
        self.out.write(("\r%s\x1b[K\n" if self.tty else "%s\n") % text)
        self.out.flush()

    def update(self, sess, force=False):
        now = time.monotonic()
        if sess is None:
            if not self.waiting_shown:
                self.out.write("Waiting for qrxfer codes on screen...\n")
                self.out.flush()
                self.waiting_shown = True
            return
        if self.t0 is None:
            self.t0 = now
        if not force and now - self.last < (0.25 if self.tty else 5.0):
            return
        self.last = now
        frac = sess.rank / float(sess.k)
        elapsed = now - self.t0
        average = sess.rank / elapsed if elapsed >= 1.0 else 0.0  # too early to tell before 1 s
        samples = self.samples
        samples.append((now, sess.rank))
        while len(samples) > 2 and now - samples[1][0] >= self.RECENT:
            samples.popleft()
        span = now - samples[0][0]
        rate = (sess.rank - samples[0][1]) / span if span >= 1.0 else average
        eta = human_time((sess.k - sess.rank) / rate) if rate > 0 else "--:--"
        filled = int(frac * 28)
        speed = (lambda r: human_size(r * sess.block_size) if r > 0 else "--")
        line = ("[%s%s] %5.1f%%  %d/%d blocks  %s/s (average %s/s)  ETA %s  frames %d"
                % ("#" * filled, "-" * (28 - filled), frac * 100, sess.rank, sess.k,
                   speed(rate), speed(average), eta, sess.received))
        self.out.write(("\r" + line + "\x1b[K") if self.tty else line + "\n")
        self.out.flush()

    def done(self):
        if self.tty and self.t0 is not None:
            self.out.write("\n")
        self.out.flush()


def fit_request(tid, ranges, max_keys):
    """encode_request for these ranges, merged across their smallest gaps until the keys
    fit in max_keys (the blocks in merged gaps come again). Returns keys and ranges."""
    keys = encode_request(tid, ranges)
    while len(keys) > max_keys and len(ranges) > 1:
        keep = max(1, int(len(ranges) * 0.95 * max_keys / len(keys))) - 1  # gaps that stay
        order = sorted(range(1, len(ranges)), key=lambda i: ranges[i][0] - ranges[i - 1][1])
        cuts = set(order[len(order) - keep:]) if keep else set()
        merged = [ranges[0]]
        for i in range(1, len(ranges)):
            if i in cuts:
                merged.append(ranges[i])
            else:
                merged[-1] = (merged[-1][0], ranges[i][1])
        ranges = merged
        keys = encode_request(tid, ranges)
    return keys, ranges


class _XClassHint(ctypes.Structure):
    _fields_ = [("res_name", ctypes.c_void_p), ("res_class", ctypes.c_void_p)]


def _window_class(window):
    """WM_CLASS of an X window as "name class", or "" without one."""
    x = _xlib()
    dpy = x.XOpenDisplay(None)
    if not dpy:
        raise QrxferError("cannot open X display %r" % os.environ.get("DISPLAY", ""))
    try:
        hint, parts = _XClassHint(), []
        if x.XGetClassHint(dpy, window, ctypes.byref(hint)):
            for text in (hint.res_name, hint.res_class):
                if text:
                    parts.append(ctypes.string_at(text).decode("latin-1"))
                    x.XFree(text)
        return " ".join(parts)
    finally:
        x.XCloseDisplay(dpy)


class ViewerKeyboard:
    """Types into the VNC viewer window with xdotool, in a thread, while the capture goes
    on. xdotool sends real (XTEST) key events only while that window has the focus, and
    VNC viewers ignore any others, so it is focused first and the focus is given back
    afterwards; if the focus moves away meanwhile, the rest of the keys go nowhere."""

    def __init__(self, window):
        if not shutil.which("xdotool"):
            raise QrxferError("--viewer needs xdotool to type into the VNC viewer")
        name = _window_class(window)
        if "vnc" not in name.lower():
            raise QrxferError("window %#x is not a VNC viewer (WM_CLASS %r)" % (window, name))
        self.window, self.error, self._thread = str(window), None, None

    def busy(self):
        return self._thread is not None and self._thread.is_alive()

    def type(self, keys):
        self.error = None
        self._thread = threading.Thread(target=self._type, args=(keys,))
        self._thread.daemon = True
        self._thread.start()

    def _type(self, keys):
        def xdotool(*args):
            return subprocess.run(("xdotool",) + args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  universal_newlines=True, check=True, timeout=30 + len(keys) / 100.0).stdout

        previous = None
        try:
            try:
                previous = xdotool("getwindowfocus").strip()
            except subprocess.CalledProcessError:
                pass
            xdotool("windowfocus", "--sync", self.window)
            xdotool("type", "--window", self.window, "--clearmodifiers", "--delay", "1", keys)
            xdotool("key", "--window", self.window, "--clearmodifiers", "Return")
        except subprocess.CalledProcessError as exc:
            self.error = "xdotool %s: %s" % (exc.cmd[1], (exc.stderr or "").strip() or "exit %d" % exc.returncode)
        except (OSError, subprocess.SubprocessError) as exc:
            self.error = str(exc)
        finally:
            if previous and previous != self.window:
                subprocess.call(["xdotool", "windowfocus", previous],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


class BlockRequester:
    """Asks the grid sender for the blocks still missing by typing a request into the VNC
    viewer (encode_request): once its first pass is over (a repair frame shows up), and
    again each time it has shown what was asked for. Otherwise the missing blocks only
    come back with the repeat screens, which mostly hold blocks already here: with 9 %
    of 2.9 million blocks missing after an 8-minute first pass, they took 18 more.

    The sender shows requested blocks last first, as no other screen does: captures of
    falling block numbers tell that it answered, and once they stop, that it is done.
    Requests that stay unanswered or bring nothing new are given up after a few."""

    MIN_K = 64          # smaller transfers end with a few repair frames anyway
    MAX_KEYS = 4000     # about 5 s of typing; more ranges are merged
    ANSWER_WAIT = 8.0   # seconds from the end of typing to the first requested screen
    SERVED_AFTER = 1.5  # seconds without requested screens after they came
    TRIES = 3           # unanswered or fruitless requests in a row before giving up

    def __init__(self, keyboard):
        self.keyboard = keyboard
        self.tid, self.off = None, False

    def update(self, sess, frames, note):
        """Call after each capture with the frames read from it; note(text) reports."""
        if self.off or sess.k < self.MIN_K:
            return
        if sess.tid != self.tid:  # a new transfer
            self.tid, self.over, self.fails, self.typing = sess.tid, False, 0, False
            self.asked = self.answered = self.last = None
        now, falling, previous = time.monotonic(), 0, None
        for frame in frames:
            if frame.tid != sess.tid:
                continue
            if frame.seq >= sess.k:
                self.over, previous = True, None
            else:
                falling += previous is not None and frame.seq <= previous
                previous = frame.seq
        if falling >= 2 and falling * 3 >= len(frames) and self.asked is not None:
            self.answered = self.answered or now
            self.last = now
        if self.keyboard.busy():
            return
        if self.typing:
            self.typing = False
            if self.keyboard.error:
                note("Could not type into the VNC viewer (%s); no more requests." % self.keyboard.error)
                self.off = True
                return
            self.asked = now
        if sess.complete or not self.over:
            return
        if self.asked is None:
            self._ask(sess, note)
        elif self.answered is None:
            if now - self.asked >= self.ANSWER_WAIT:
                self._again(sess, note, "The sender did not answer")
        elif now - self.last >= self.SERVED_AFTER:
            if sess.rank > self.rank:
                self.fails = 0
                self._ask(sess, note)
            else:
                self._again(sess, note, "None of the requested blocks arrived")

    def _again(self, sess, note, why):
        self.fails += 1
        if self.fails < self.TRIES:
            note(why + "; asking again.")
            self._ask(sess, note)
        else:
            note(why + " %d times in a row; no more requests, the repeat screens bring the rest."
                 % self.fails)
            self.off = True

    def _ask(self, sess, note):
        missing = sess.missing_ranges()
        keys, ranges = fit_request(sess.tid, missing, self.MAX_KEYS)
        self.keyboard.type(keys)
        self.typing, self.asked, self.answered, self.last, self.rank = True, None, None, None, sess.rank
        want, asked = sum(b - a for a, b in missing), sum(b - a for a, b in ranges)
        note("Asking the sender for the %d missing blocks%s: typing %d keys into the VNC viewer."
             % (want, "" if asked == want else " (%d with gaps, in %d ranges)" % (asked, len(ranges)),
                len(keys)))


def default_decode_threads():
    return max(1, min(4, (os.cpu_count() or 1) - 1))


def collect_from_screen(grabber, reader, region=None, progress=None, timeout=None, threads=1,
                        requester=None):
    """Watch the screen until one transfer is complete; returns its FountainDecoder.

    reader is a QrReader, GridReader or AutoReader (a bare QR decoder is wrapped). With
    threads > 1, up to that many captures are decoded at once (each thread with its own
    reader clone) while the next one is grabbed; XGetImage, numpy and the QR decoders
    run without the GIL, so the screen is sampled several times faster. A
    BlockRequester, if given, asks a grid sender for the blocks still missing.
    """
    from concurrent.futures import ThreadPoolExecutor
    import queue

    if not hasattr(reader, "read"):
        reader = QrReader(reader)
    sessions = {}
    current = None
    tracker = RegionTracker(grabber.width, grabber.height, region)
    progress = progress or Progress()
    started = time.monotonic()
    idle = queue.Queue()
    idle.put(reader)
    for _ in range(threads - 1):
        idle.put(reader.clone())

    def decode(img, box, full):
        rd = idle.get()
        try:
            return rd.read(img, box, full)
        finally:
            idle.put(rd)

    pending = collections.deque()
    with ThreadPoolExecutor(threads) as pool:
        while True:
            box, full = tracker.next_box()
            img = grabber.grab(*box, colour=True) if reader.rgb else grabber.grab(*box)
            pending.append(pool.submit(decode, img, box, full))
            while pending and (len(pending) >= threads or pending[0].done()):
                found, frames = [], pending.popleft().result()
                for frame, where in frames:
                    if where:
                        found.append(where)
                    sess = sessions.get(frame.tid)
                    if sess is None:
                        sess = sessions[frame.tid] = FountainDecoder.for_frame(frame)
                    sess.add(frame)
                    current = sess
                    if sess.complete:
                        progress.update(sess, force=True)
                        progress.done()
                        return sess
                tracker.update(found, reader.track_margin)
                if requester is not None and current is not None and (
                        isinstance(reader, GridReader) or getattr(reader, "shared", {}).get("mode") == "grid"):
                    requester.update(current, [frame for frame, _ in frames], progress.note)
            notice = reader.take_notice() if hasattr(reader, "take_notice") else None
            if notice:
                progress.note(notice)
            progress.update(current)
            if timeout is not None and time.monotonic() - started > timeout:
                progress.done()
                raise QrxferError("timed out after %d s" % timeout)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def _parse_grid(text):
    if text in (None, "", "auto"):
        return None
    m = re.fullmatch(r"(\d+)(?:x(\d+))?", text)
    if not m or int(m.group(1)) < 1 or int(m.group(2) or 1) < 1:
        raise argparse.ArgumentTypeError("grid must be 'auto' or COLSxROWS, e.g. 3x2")
    return int(m.group(1)), int(m.group(2) or 1)


def _parse_density(text):
    if text == "auto":
        return None
    if text in DENSITY:
        return DENSITY[text]
    try:
        value = int(text)
    except ValueError:
        value = 0
    if not 16 <= value <= 2800:
        raise argparse.ArgumentTypeError("density is auto, low, medium, high or a byte count 16..2800")
    return value


def cmd_send(args):
    codec = args.codec
    if codec == "grid" and args.terminal:
        raise QrxferError("--terminal draws QR codes only; drop --codec grid")
    window = None
    if codec != "qr" and not args.terminal:
        try:  # fail before the passphrase prompt, not after it
            window = X11GridWindow()
        except QrxferError as exc:
            if codec == "grid" or not (sys.stdin.isatty() and sys.stdout.isatty()):
                raise
            print("No X11 display (%s): showing QR codes in this terminal instead." % exc)
            args.terminal = True
    try:
        return _send(args, window)
    finally:
        if window is not None:
            window.close()


def _send(args, window):
    threads = args.threads or xz_threads()
    # Big inputs take minutes before the passphrase prompt: say what is going on.
    print("Packing and compressing on up to %d thread%s..." % (threads, "" if threads == 1 else "s"),
          flush=True)
    archive, nfiles, nbytes = pack_paths(args.paths, threads)
    secret, _ = obtain_passphrase(args, "send")
    blob = encrypt(archive, secret)
    del archive  # a big transfer keeps enough copies in memory as it is
    tid = secrets.randbits(32)
    print("qrxfer %s: %d file%s, %s -> %s compressed and encrypted"
          % (__version__, nfiles, "" if nfiles == 1 else "s", human_size(nbytes), human_size(len(blob))))

    def make_stream(avail_w, avail_h, min_px):
        # The code size is fixed for the whole transfer, so --density auto sizes it
        # for the display area at start (resize/shrink the font before sending).
        block = args.density or choose_block_size(avail_w, avail_h, min_px, args.max_codes,
                                                  args.grid, args.ecc) or DENSITY["medium"]
        stream = FrameStream(FountainEncoder(blob, block, tid), args.ecc)
        cols, rows, px = plan_grid(avail_w, avail_h, stream.size, min_px, args.max_codes, args.grid)
        print("  %d blocks of %d bytes, QR version %d-%s (%d modules), %s"
              % (stream.encoder.k, block, stream.version, args.ecc, stream.size,
                 "%dx%d codes per screen" % (cols, rows) if px >= 1 else "display too small"))
        print("  needs ~%d codes: about %s at %.1f fps if every code is captured"
              % (stream.encoder.k, human_time(stream.encoder.k / (cols * rows * args.fps)), args.fps))
        print("Showing codes. Stop with q / Esc once the receiver reports completion.")
        return stream

    def make_grid_stream(avail_w, avail_h, px):
        cols, rows = plan_cells(avail_w, avail_h, px)
        block = grid_block_size(cols)
        if block < GRID_MIN_BLOCK or rows < 3:
            raise QrxferError("the %dx%d screen is too small for %d px grid cells; use a smaller "
                              "--cell-px or --codec qr" % (avail_w, avail_h + GRID_STATUS_PX, px))
        stream = GridStream(FountainEncoder(blob, block, tid), cols, rows)
        print("  %d blocks of %d bytes, a %dx%d grid of %d px cells: %d rows = %s per screen"
              % (stream.encoder.k, block, cols, rows, px, rows, human_size(rows * block)))
        print("  about %s at %.1f fps if every row is captured"
              % (human_time(stream.encoder.k / (rows * args.fps)), args.fps))
        print("Showing the grid full screen. Stop with q / Esc (typed into the VNC viewer) "
              "once the receiver reports completion.")
        return stream

    if window is not None:
        state = run_x11_grid_sender(window, make_grid_stream, args)
    else:
        runner = run_terminal_sender if args.terminal else run_tk_sender
        state = runner(make_stream, args)
    stream = state.stream
    print("Stopped after %d frames in %s." % (stream.seq, human_time(time.monotonic() - state.started)))
    if stream.seq < stream.encoder.k:
        print("Note: fewer frames than blocks were shown, so the receiver cannot have finished.")
    return 0


def _save_received(plain, outdir):
    saved = unpack_archive(plain, outdir)
    print("Saved %d item%s to %s:" % (len(saved), "" if len(saved) == 1 else "s", outdir))
    for path in saved:
        print("  %s  (%s)" % (os.path.basename(path), path_summary(path)))


def _decrypt_interactively(blob, secret, interactive, outdir):
    attempts = 3 if interactive else 1
    for attempt in range(attempts):
        try:
            return decrypt(blob, secret)
        except WrongPassphrase:
            if attempt + 1 < attempts:
                print("Wrong passphrase, try again.")
                secret = normalize_passphrase(getpass.getpass("Passphrase: "))
    os.makedirs(outdir, exist_ok=True)
    path = _unique_path(os.path.join(outdir, time.strftime("qrxfer-%Y%m%d-%H%M%S.qxf")))
    with open(path, "wb") as fh:
        fh.write(blob)
    raise QrxferError("wrong passphrase. The encrypted transfer was kept as %s\n"
                      "  decrypt it later with:  qrxfer decrypt %s" % (path, path))


def _parse_region(text):
    m = re.fullmatch(r"(\d+),(\d+),(\d+),(\d+)", text or "")
    if not m or int(m.group(3)) < 16 or int(m.group(4)) < 16:
        raise argparse.ArgumentTypeError("region must be X,Y,WIDTH,HEIGHT")
    return tuple(int(v) for v in m.groups())


def _parse_window(text):
    try:
        window = int(text, 0)
    except ValueError:
        window = 0
    if window <= 0:
        raise argparse.ArgumentTypeError("window must be an X window id, e.g. 0x3a00007 or 60817415")
    return window


def cmd_recv(args):
    if os.environ.get("XDG_SESSION_TYPE") == "wayland":
        print("Warning: Wayland session - X11 capture may only see X11 windows.", file=sys.stderr)
    grabber = XlibGrabber()
    decoder = None
    if args.codec != "grid":
        try:
            decoder = open_qr_decoder(args.decoder)
        except QrxferError as exc:
            if args.codec == "qr":
                raise
            print("Note: QR codes cannot be read (%s); watching for grids only."
                  % str(exc).split("\n")[0], file=sys.stderr)
    if args.codec == "qr":
        reader = QrReader(decoder)
    else:
        grid = GridReader(grabber.np, grabber.channels)
        reader = grid if args.codec == "grid" else AutoReader(grid, decoder and QrReader(decoder))
    region = args.region
    if region and (region[0] + region[2] > grabber.width or region[1] + region[3] > grabber.height):
        raise QrxferError("region lies outside the %dx%d screen" % (grabber.width, grabber.height))
    requester = None
    if args.viewer is not None:
        if args.codec == "qr":
            raise QrxferError("--viewer asks a grid sender for blocks; drop --codec qr")
        requester = BlockRequester(ViewerKeyboard(args.viewer))
    secret, interactive = obtain_passphrase(args, "recv")
    outdir = os.path.abspath(args.output)
    print("Watching the %dx%d screen for %s (Ctrl+C aborts). Keep the sender visible and unscaled."
          % (grabber.width, grabber.height, reader.name), flush=True)
    started = time.monotonic()
    sess = collect_from_screen(grabber, reader, region, timeout=args.timeout,
                               threads=args.threads or default_decode_threads(), requester=requester)
    elapsed = time.monotonic() - started
    # Solving the stored equations can take minutes for a big transfer.
    print("All %d blocks arrived after %s; reassembling the data (the sender may be stopped)..."
          % (sess.k, human_time(elapsed)), flush=True)
    blob = sess.result()
    print("Received %s in %s from %d frames. Decrypting..." % (human_size(len(blob)), human_time(elapsed), sess.received),
          flush=True)
    plain = _decrypt_interactively(blob, secret, interactive, outdir)
    _save_received(plain, outdir)
    return 0


def cmd_decrypt(args):
    try:
        with open(args.file, "rb") as fh:
            blob = fh.read()
    except OSError as exc:
        raise QrxferError("cannot read %s: %s" % (args.file, exc))
    secret, interactive = obtain_passphrase(args, "decrypt")
    outdir = os.path.abspath(args.output)
    plain = _decrypt_interactively(blob, secret, interactive, outdir)
    _save_received(plain, outdir)
    return 0


def cmd_genpass(args):
    print(generate_passphrase())
    return 0


def _render_codes_image(np, codes, size, cols, px):
    """Render codes like the Tk sender does, as a grayscale numpy image."""
    rows = -(-len(codes) // cols)
    pitch = size + QUIET
    img = np.full(((rows * pitch + QUIET) * px, (cols * pitch + QUIET) * px), 255, np.uint8)
    for i, code in enumerate(codes):
        bits = np.array([[r >> x & 1 for x in range(size)] for r in code], np.uint8)
        tile = np.kron(1 - bits, np.ones((px, px), np.uint8)) * 255
        y, x = (QUIET + i // cols * pitch) * px, (QUIET + i % cols * pitch) * px
        img[y:y + size * px, x:x + size * px] = tile
    return img


def _selftest_grid(np, blob, rnd, report):
    """Grid screens painted as the sender does, then found and read like a capture."""
    cols, rows = plan_cells(960, 400, GRID_CELL_PX)
    encoder = FountainEncoder(blob, grid_block_size(cols), rnd.getrandbits(32))
    stream = GridStream(encoder, cols, rows)
    painter = GridPainter(cols, rows, GRID_CELL_PX, lambda r, g, b: bytes((b, g, r, 0)))
    sess = None
    shown = used = 0
    t = time.perf_counter()
    stream.seq = encoder.k // 3
    while (sess is None or not sess.complete) and shown < 6 * encoder.k + 40:
        frames = stream.take(rows)
        shown += rows
        if np is not None:
            pixels = np.frombuffer(painter.paint(frames), np.uint8).reshape(painter.height, painter.width, 4)
            capture = np.full((painter.height + 50, painter.width + 70, 4), 40, np.uint8)
            capture[20:20 + painter.height, 30:30 + painter.width] = pixels
            geo = locate_grid(np, capture, (2, 1, 0))
            frames = read_grid_rows(np, capture, geo.xs, geo.ys, (2, 1, 0)) if geo else []
        for raw in frames:
            if rnd.random() < 0.25:  # simulate rows lost to tearing
                continue
            frame = parse_frame_bytes(raw)
            sess = sess or FountainDecoder.for_frame(frame)
            used += 1
            sess.add(frame)
    good = sess is not None and sess.complete and sess.result() == blob
    report("grid paint -> read -> fountain", good, "%d rows shown, %d used, %s, %.2f s"
           % (shown, used, "located and read with numpy" if np is not None else "reading skipped",
              time.perf_counter() - t))
    return good


def cmd_selftest(args):
    import random

    ok = True

    def report(label, good, detail=""):
        print("  [%s] %s%s" % ("ok" if good else ("--" if good is None else "FAIL"), label,
                               (": " + detail) if detail else ""))

    print("qrxfer %s self-test, Python %s" % (__version__, sys.version.split()[0]))
    print("Sending side:")
    report("scrypt key derivation", hasattr(hashlib, "scrypt"), "" if hasattr(hashlib, "scrypt") else "PBKDF2 fallback")
    if importlib.util.find_spec("lzma"):
        report("xz compression", True)
    else:
        report("xz compression", None, "missing, gzip will be used")
    screen = (2560, 1440)
    try:
        window = X11GridWindow()
        screen = (window.width, window.height)
        window.close()
        cols, rows = plan_cells(screen[0], screen[1] - GRID_STATUS_PX, GRID_CELL_PX)
        report("X11 grid window", True, "%dx%d screen: %dx%d cells, %s per screen"
               % (screen + (cols, rows, human_size(rows * grid_block_size(cols)))))
    except QrxferError as exc:
        report("X11 grid window", None, "%s - QR codes need --terminal or tkinter" % exc)
    cols, rows = plan_cells(screen[0], screen[1] - GRID_STATUS_PX, GRID_CELL_PX)
    stream = GridStream(FountainEncoder(os.urandom(rows * grid_block_size(cols)), grid_block_size(cols), 1),
                        cols, rows)
    painter = GridPainter(cols, rows, GRID_CELL_PX, lambda r, g, b: bytes((b, g, r, 0)))
    t = time.perf_counter()
    painter.paint(stream.take(rows))
    spent = time.perf_counter() - t
    report("grid painting speed", True, "%.0f ms per %dx%d screen (fps limit ~%.0f before the X server's share)"
           % (spent * 1000, screen[0], screen[1], 1 / spent))
    if importlib.util.find_spec("tkinter"):
        report("tkinter window", True)
    else:
        report("tkinter window", None, "missing - only needed for QR codes in a window (or use --terminal)")
    stream = FrameStream(FountainEncoder(os.urandom(12000), DENSITY["medium"], 1), "L")
    stream.take(1)  # build the cached version tables first
    t = time.perf_counter()
    stream.take(12)
    report("QR encoding speed", True, "%.0f codes/s at version %d (fps limit = this / codes per screen)"
           % (12 / (time.perf_counter() - t), stream.version))
    print("Receiving side:")
    np = None
    try:
        np = _need_numpy()
        report("numpy", True)
    except QrxferError:
        report("numpy", None, "missing - needed only to receive (python3-numpy)")
    decoder = None
    try:
        decoder = open_qr_decoder(args.decoder)
        report("QR decoder", True, decoder.name)
    except QrxferError:
        report("QR decoder", None, "none - needed only to receive (libzbar0 or python3-zxing-cpp)")
    if os.environ.get("DISPLAY") and np is not None:
        try:
            g = XlibGrabber()
            t = time.perf_counter()
            g.grab(0, 0, min(800, g.width), min(800, g.height))
            report("screen capture", True, "%dx%d, 800x800 region in %.0f ms (%s)"
                   % (g.width, g.height, (time.perf_counter() - t) * 1000, g.name))
        except QrxferError as exc:
            report("screen capture", False, str(exc))
    else:
        report("screen capture", None, "no DISPLAY")

    print("Pipeline:")
    rnd = random.Random(7)
    tmp = tempfile.mkdtemp(prefix="qrxfer-selftest-")
    try:
        src = os.path.join(tmp, "src")
        os.makedirs(os.path.join(src, "sub dir"))
        with open(os.path.join(src, "hello.txt"), "w", encoding="utf-8") as fh:
            fh.write("h\u00e9llo w\u00f6rld\n" * 200)
        rand_bytes = bytes(rnd.getrandbits(8) for _ in range(6000))
        with open(os.path.join(src, "sub dir", "data.bin"), "wb") as fh:
            fh.write(rand_bytes)
        t = time.perf_counter()
        archive, _, _ = pack_paths([src])
        blob = encrypt(archive, "selftest passphrase")
        report("pack + encrypt", True, "%s in %.2f s" % (human_size(len(blob)), time.perf_counter() - t))
        encoder = FountainEncoder(blob, 400, rnd.getrandbits(32))
        stream = FrameStream(encoder, "L")
        sess = None
        decoded = shown = 0
        t = time.perf_counter()
        stream.seq = encoder.k // 3  # join late, as a receiver started after the sender would
        while sess is None or not sess.complete:
            codes = stream.take(4)
            shown += 4
            if shown > 6 * encoder.k + 40:
                break
            if decoder is not None and np is not None:
                image = _render_codes_image(np, codes, stream.size, 2, 3)
                texts = [txt for txt, _ in decoder.decode(image)]
            else:
                first = stream.seq - 4
                texts = [pack_frame(encoder.frame(first + i)) for i in range(4)]
            for txt in texts:
                if rnd.random() < 0.25:  # simulate frames lost to tearing
                    continue
                frame = parse_frame(txt)
                sess = sess or FountainDecoder.for_frame(frame)
                decoded += 1
                sess.add(frame)
        good = sess is not None and sess.complete
        how = ("rendered + decoded with %s" % decoder.name) if decoder and np is not None else "QR rendering skipped"
        report("QR encode -> decode -> fountain", good, "%d codes shown, %d used, %s, %.2f s"
               % (shown, decoded, how, time.perf_counter() - t))
        ok &= good
        good = _selftest_grid(np, blob, rnd, report) and good
        ok &= good
        if good:
            plain = decrypt(sess.result(), "selftest passphrase")
            out = os.path.join(tmp, "out")
            saved = unpack_archive(plain, out)
            got = os.path.join(saved[0], "sub dir", "data.bin")
            with open(got, "rb") as fh:
                same = fh.read() == rand_bytes
            report("decrypt + extract", same)
            ok &= same
            try:
                decrypt(sess.result(), "wrong")
                report("wrong passphrase rejected", False)
                ok = False
            except WrongPassphrase:
                report("wrong passphrase rejected", True)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("Self-test %s." % ("passed" if ok else "FAILED"))
    return 0 if ok else 1


def build_parser():
    p = argparse.ArgumentParser(
        prog="qrxfer", description="Encrypted file transfer through an animated colour grid or QR codes on screen.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Typical use with VNC:\n"
               "  local:   qrxfer recv            (press Enter to generate a passphrase)\n"
               "  remote:  qrxfer send FILES...   (type that passphrase, the grid appears)\n"
               "Details: qrxfer COMMAND --help")
    p.add_argument("--version", action="version", version="qrxfer " + __version__)
    sub = p.add_subparsers(dest="command", metavar="COMMAND")
    sub.required = True

    s = sub.add_parser("send", help="show files as an animated colour grid or QR codes")
    s.add_argument("paths", nargs="+", metavar="FILE_OR_DIR")
    s.add_argument("-p", "--passphrase-file", metavar="FILE", help="read the passphrase from FILE")
    s.add_argument("--codec", choices=["auto", "grid", "qr"], default="auto",
                   help="grid: full-screen colour grid (X11, fastest); qr: QR codes in a Tk window or "
                        "with --terminal; auto (default): grid, or terminal QR codes without an X display")
    s.add_argument("--fps", type=float, default=12.0,
                   help="screens per second (default 12; +/- keys adjust live)")
    s.add_argument("--cell-px", type=int, choices=range(2, GRID_MAX_CELL_PX + 1), default=GRID_CELL_PX,
                   metavar="N", help="grid: cell size in pixels (default %d)" % GRID_CELL_PX)
    s.add_argument("--density", type=_parse_density, default=None,
                   help="QR: data per code: auto (default: the code size that carries the most per screen), "
                        "low, medium, high or bytes (300/600/1000)")
    s.add_argument("--grid", type=_parse_grid, default=None,
                   help="QR: codes per screen: auto (default) or COLSxROWS, e.g. 6x2 (1 = one big code); "
                        "with --density auto the codes are sized to fill that grid")
    s.add_argument("--max-codes", type=int, default=24, help="QR: upper limit for --grid auto (default 24)")
    s.add_argument("--min-px", type=int, default=4, help="QR: smallest module size in pixels for --grid auto (default 4)")
    s.add_argument("--ecc", choices=list(QR_LEVELS), default="L", help="QR error correction level (default L)")
    s.add_argument("--terminal", action="store_true", help="QR: draw codes in this terminal instead of a window")
    s.add_argument("--windowed", action="store_true", help="QR: use a normal window instead of full screen")
    s.add_argument("--threads", type=int, choices=range(1, 1025), metavar="N",
                   help="compress on N threads (default: one per CPU, within a quarter of the RAM)")
    s.set_defaults(func=cmd_send)

    r = sub.add_parser("recv", help="capture the screen and receive files")
    r.add_argument("-o", "--output", default=".", help="directory for received files (default: current)")
    r.add_argument("-p", "--passphrase-file", metavar="FILE", help="read the passphrase from FILE")
    r.add_argument("--codec", choices=["auto", "grid", "qr"], default="auto",
                   help="what to look for: auto (default) finds a colour grid or QR codes")
    r.add_argument("--decoder", choices=["auto"] + sorted(QR_DECODERS), default="auto",
                   help="QR decoder library (default: zxing-cpp if installed, else zbar)")
    r.add_argument("--region", type=_parse_region, help="only watch X,Y,WIDTH,HEIGHT of the screen")
    r.add_argument("--timeout", type=float, help="give up after this many seconds")
    r.add_argument("--threads", type=int, choices=range(1, 17), metavar="N",
                   help="decode N captures at once (default: CPUs - 1, at most 4)")
    r.add_argument("--viewer", type=_parse_window, metavar="WINDOW",
                   help="grid: X window id of the VNC viewer (decimal or 0x hex); after the sender's "
                        "first pass, ask it for the missing blocks by typing into that window "
                        "(needs xdotool; takes the keyboard focus for a few seconds each time)")
    r.set_defaults(func=cmd_recv)

    d = sub.add_parser("decrypt", help="decrypt a saved .qxf transfer")
    d.add_argument("file")
    d.add_argument("-o", "--output", default=".", help="directory for extracted files (default: current)")
    d.add_argument("-p", "--passphrase-file", metavar="FILE")
    d.set_defaults(func=cmd_decrypt)

    g = sub.add_parser("genpass", help="print a strong random passphrase")
    g.set_defaults(func=cmd_genpass)

    t = sub.add_parser("selftest", help="check dependencies and run the whole pipeline offline")
    t.add_argument("--decoder", choices=["auto"] + sorted(QR_DECODERS), default="auto")
    t.set_defaults(func=cmd_selftest)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except QrxferError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nAborted.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
