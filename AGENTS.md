# AGENTS.md — qrxfer

Guidance for coding agents working in this repository. User-facing documentation is `README.md`.

## What this project is

`qrxfer` is an encrypted, one-way file transfer through the screen: an animated full-screen grid of coloured cells, or QR codes. No network port is opened.

- Typical setup: the user reaches a remote Linux machine through a VNC viewer on the local machine.
  - The remote (VNC server side) runs `qrxfer send FILES...`, which covers its screen with the colour grid (QR codes with `--codec qr`).
  - The local machine (viewer side) runs `qrxfer recv`, which captures its own X11 screen, decodes the codes and rebuilds the files.
- Direction is inherently **screen-showing side → screen-watching side** only.
- No camera is involved; this machine has none.

## Layout

| Path | Purpose |
|---|---|
| `qrxfer.py` | The whole program, a single file meant to be copied to both machines |
| `tests/test_qrxfer.py` | unittest suite (54 tests; `tests/test_skill_transfer.py` covers the skill helper) |
| `install.sh` | Copies `qrxfer.py` to `~/.local/bin/qrxfer` (or `$QRXFER_BIN_DIR`), then runs `selftest` |
| `README.md` | User guide |
| `skills/qrxfer/` | Shared VNC download skill and deterministic helper (prepare, place, type, bundle, install, keepawake) |

`qrxfer.py` sections, in file order:

1. **Base45** (RFC 9285): the QR alphanumeric charset.
2. **Fountain code**:
   - Frames `0..k-1` are systematic; later frames XOR `degree` blocks.
   - The receiver does incremental GF(2) Gaussian elimination: `FountainDecoder`, and `rank == k` means complete.
     - Known blocks are kept as values only and substituted into repair frames first; stored rows hold only missing blocks.
       Keeping full masks made a grid transfer of 67k blocks spend ~0.1 s per repair frame and ~k²/16 bytes of memory.
     - Mask bits are columns handed out to missing blocks as repair frames name them, not block indices: at k ≈ 2 million (600 MB) masks over block indices grew to ~250 KB per row and many GB in total.
     - With k ≥ 8192, repair frames are skipped while systematic frames still arrive (a new one within the last max(1024, k/64) repair frames) and more than min(k/16, 32,768) blocks are missing: a late joiner of a grid sender, which repeats them.
       Eliminating such frames costs seconds per screen at k ≈ 2 million, and the capture loop misses the repeats meanwhile.
3. **Encryption**: stdlib only.
   - scrypt (`N=2^17, r=8, p=1`), falling back to PBKDF2 2^20 iterations where scrypt is unavailable.
   - SHAKE-256 keystream plus HMAC-SHA256, encrypt-then-MAC.
   - Container magic `QXF2`: the keystream is made of 1 MiB pieces, SHAKE-256 of `b"qrxfer-v2-stream"` + key + 8-byte big-endian piece number, because CPython's built-in SHAKE (the only one on Python 3.6) refuses 2^29 bytes (512 MiB) or more in one output.
   - `QXF1` (one SHAKE-256 output as long as the data, 1.1.1 and older) was dropped; `decrypt` rejects it.
4. **Archive**:
   - tar.xz packing on every core (`XzWriter`, `xz_threads`, `send --threads`), like `xz -T0`:
     - The tar stream is cut into `XZ_CHUNK` (24 MiB) pieces, each compressed into an independent .xz stream by `lzma.compress` in a thread pool; lzma releases the GIL, also on Python 3.6.
     - xz, `lzma` and tarfile read the concatenated streams as one file, so the receiver needs nothing new.
     - One thread per usable core, within a quarter of the RAM at 200 MiB per thread. The skill's `bundle` has its own copy of the writer, since the helper is typed in alone.
   - Extraction into a staging dir with the tar `data` filter; files are never overwritten (`name (1).ext`).
5. **QR encoder**: pure Python; alphanumeric mode, versions 1–40, levels L/M/Q/H.
   - Speed matters: the sender's CPU was the bottleneck on a real Python 3.6 remote.
   - Reed-Solomon parity is a shift register held in one int (`_rs_products` tables).
   - Modules are placed a whole row at a time with `operator.itemgetter` (`_QrTemplate.row_bits`).
   - `qr_encode(mask=None)` still searches the penalty-minimal mask; frames use the fixed `FRAME_MASK`.
6. **QR sender**:
   - `FrameStream` (fixed `FRAME_MASK`), `plan_grid` for the multi-code layout.
   - `choose_block_size` for `--density auto` (the default): it picks the QR version in `AUTO_VERSIONS` (10–17) that puts the most data on one screen.
   - The runners take a `make_stream(width, height, min_px)` callback, because the code size depends on the display area at start.
   - Tk window (PGM `PhotoImage`, with a `put()` fallback for Tk 8.5).
   - Terminal renderer: UTF-8 half blocks (built with an int/hex/`translate` trick), or an ASCII background-colour fallback chosen by a cursor-position probe.
   - The status line shows actual/target fps.
7. **Colour grid sender** (the default codec):
   - `plan_cells`: odd numbers of 3 px cells, a one-cell black/white timing ring and a one-cell black margin.
   - `GridStream`: one binary frame (`frame_bytes`) per row; after the first pass, each screen of repair frames is followed by one screen repeating the systematic frames, or three from k ≥ 2^16 (`MANY_REPEATS_K`).
     Round c of repeats (`repeat_block`) shows every block (c + 1) × 0.382 × rows (mod rows) rows above its row in the first pass, so a band of rows covered on the receiver's screen hides other blocks each round; aligned repeats hid the same ones again.
     Every screen after the first pass makes an equal share of the next repair screen, so all screens take about as long to make and stay up about as long.
   - Requests for missing blocks (`encode_request` / `parse_request`): keys typed into the VNC viewer by `recv --viewer` reach the grid's keyboard grab; `_SendState.handle_key` collects them from `x` to Return and hands the ranges to `GridStream.request`.
     After the first pass, requested blocks come next on screens of their own, last block first, a short last screen filled up with them again; then the stream goes on where it was. No repair shares are made meanwhile, so these screens come at first-pass speed.
     A newer request replaces the one being shown; damaged ones (a lost key, another transfer) are counted as bad on the status line.
   - `GridPainter`: pure-Python BGRX painting (one `join` over pre-rendered cells per line; frames become octal digits through a big-int XOR with the row's whitening).
   - `X11GridWindow` (libX11 through ctypes, prototypes in `_xlib()`): override-redirect full-screen window, `XPutImage` + `XSync`, keyboard grab (keys typed into the VNC viewer), hidden pointer, status line drawn with `XDrawImageString`.
   - `run_x11_grid_sender` installs SIGINT/SIGTERM handlers itself: a background job may start with SIGINT ignored.
8. **Receiver**:
   - `XlibGrabber`: MIT-SHM (`XShmGetImage` into a SysV segment per region size, ctypes libXext) when the X server shares memory with this client, else `XGetImage`; `grab(..., colour=True)` returns raw pixels (a copy), `.channels` gives the red/green/blue byte indices.
     - Captures must come more often than screens change: through a VNC hop at ~11.7 fps, 1.4× and more lost little, 0.9× needed twice the screens and 0.55× five times.
     - With the viewer redrawing, `XGetImage` of 2560×1351 averaged 56 ms (up to 160 ms) on the real desktop; MIT-SHM takes half to a third.
     - On 2026-10-27 (glamor on an Intel iGPU, xfwm4 compositing on) every MIT-SHM capture took 35–45 ms whatever its size, even 64×64 px, and other X clients waited meanwhile (a bare `XSync` 15 ms median); a running `recv` spent 85 % of its main thread in `XShmGetImage`, its decoder threads were idle 86 % of the time.
       Captures from two connections at once overlap (21 → 33 full-region captures per second), but each one still holds up the viewer.
   - Decoders `ZxingDecoder` (zxing-cpp) and `ZbarDecoder` (ctypes libzbar).
     - zbar returns nothing for an image with one torn code, so it scans each band between blank pixel rows (`_code_bands`) separately.
     - Each band gets a line-skipping pass first, then a full-density pass when that finds nothing or fewer codes than last time.
   - Grid: `locate_grid` finds the densest block of saturated colours, then reads every cell boundary from the timing rings (top/left first, bottom/right when covered); `read_grid_rows` samples cell centres with numpy.
     - `GridGeometry.complete` means the bottom ring was seen: a viewer draws the first screen top-down, and a capture half-way must not fix too few rows.
   - Readers: `QrReader`, `GridReader` (geometry shared by its clones, searched again on full captures, every 0.5 s while incomplete, and when a capture yields nothing), `AutoReader` (both; cropped captures use the kind found last).
     - `GridReader._watch`: rows failing in ≥ 90 % of a clone's last 40 captures are covered (a dock over the viewer); `collect_from_screen` prints that once a minute through `Progress.note`.
   - `Progress`: the rate of the last 30 s and the average since the first frame; the ETA uses the recent rate.
   - `RegionTracker` (crops to the codes, rescans the full screen every 4 s).
   - `collect_from_screen`: grabs on the main thread and decodes in `threads` worker threads, each with its own reader clone (ctypes and numpy release the GIL).
   - `BlockRequester` (`recv --viewer`, grid only): once a repair frame shows that the first pass is over, it types a request for `FountainDecoder.missing_ranges()` through `ViewerKeyboard` (xdotool in a thread: `windowfocus --sync`, `type --delay 1`, Return, focus given back).
     - `fit_request` merges ranges across their smallest gaps until the keys fit in 4,000 (≈ 5 s of typing at the ~870 keys/s the skill's bootstrap reached).
     - A capture whose block numbers fall in at least a third of its row pairs is a requested screen; once none came for 1.5 s, it asks again for what is still missing.
     - Three requests in a row without an answer within 8 s of typing, or without a new block, and it stops asking.
9. **Commands**: `send`, `recv`, `decrypt`, `genpass`, `selftest`, wired up in `build_parser`.

## Invariants — do not break

- **Sender side is stdlib-only and runs on Python ≥ 3.6.**
  - Don't add third-party imports to the send path. libX11 through ctypes is the only native library the grid window uses.
  - Avoid 3.7+ features (walrus, `int.bit_count`, `str.removeprefix`, `functools.cache`, runtime `list[int]` annotations).
  - Check with `vermin --no-tips qrxfer.py`; it must report `3.6`.
  - Receiver-only deps (numpy, libzbar/zxing-cpp) must stay lazily imported.
- **Both machines run the same qrxfer version; nothing is kept for compatibility with older copies.**
  Drop old formats and code paths instead of carrying them along. Mismatched copies must still fail with a clear error, never with garbage, so the wire format below is versioned:
  - frame header `>BIIHIH` plus CRC32;
  - Base45 wrapped as `QX:` … `.`;
  - `_SplitMix64` / `repair_indices`;
  - the container layout and keystream (`_keystream_xor`) and the KDF ids;
  - the grid: one `frame_bytes` frame per row of cells, cell colour = 4×red + 2×green + blue from the 8 RGB cube corners, bits most significant first, zero-padded to 3×cols bits and XORed with the first 3×cols bits of bytes y×n … (y+1)×n of `grid_whitening` (SHAKE-256 of `b"qrxfer grid"`, n = ceil(3×cols/8), y = row from the top); the black/white timing ring around the cells; cells of at least 2 px.
  - the request keys: `x`, then numbers of hex digits (the last one from `0-9a-f`, the others from `g-p r-w`, so no `q` or space), Return; the numbers are the transfer id, the gap from the previous range's end and the length minus one per range, and the CRC32 of the keys before it. Requested blocks are shown last first, which the receiver takes for the answer.
  - Any change needs a `FRAME_VERSION` (or container magic, or `REQUEST_KEY`) bump, so that an older copy rejects it, and must update the golden values in `FrameTest.test_wire_format_is_stable`, `GridFormatTest.test_wire_format_is_stable`, `RequestTest.test_wire_format_is_stable` and `CryptoTest.test_keystream_is_stable`.
- The repair **degree distribution** (`repair_degree`, `REPAIR_DEGREE_CAP = 256`) is sender-only, because the degree travels in each frame. It may be tuned freely; re-check the overhead with the fountain tests.
- Also sender-only, since every QR code carries its own version and mask: `FRAME_MASK`, `AUTO_VERSIONS`, the block size and the layout.
  - Before raising `AUTO_VERSIONS` above 17, measure zbar: its decode time per byte rises steeply for bigger codes.
- Sender-only for the grid as well, since the receiver measures the geometry: `GRID_CELL_PX`, `plan_cells`, the status line and the order of frames in `GridStream`, except that requested blocks come last first.
  - The receiver needs the ring directly beside the data cells, the black margin outside it, and whole rows: a row must hold one complete frame.
- The QR encoder must stay bit-identical to segno. `QrEncoderTest.test_matches_segno` runs when `segno` is importable; use a throwaway venv.
- Receiving must never overwrite existing files, and must keep using the tar `data` filter (or `_check_members_legacy` on old Pythons).

## How to verify changes

Match the checks to what changed. Documentation-only edits need none of the runtime checks.

```sh
python3 -m unittest discover -s tests        # system python3 here has numpy + libzbar; the segno test is skipped
python3 qrxfer.py selftest                   # environment report + offline pipeline
```

- **Optional extras.** Create them in a scratch venv, never inside the repo: `python3 -m venv --system-site-packages /tmp/.../venv`, then `pip install segno zxing-cpp pyflakes vermin`.
  - The full suite then also runs the segno comparison and the zxing decoder.
  - Lint with `pyflakes qrxfer.py tests/test_qrxfer.py`.
- **End-to-end on a virtual display** (no window manager; the sender sets an explicit geometry so full screen still works):

  ```sh
  Xvfb :93 -screen 0 1280x800x24 &               # remember $! and kill it afterwards
  DISPLAY=:93 python3 qrxfer.py send SRC -p pass.txt &        # the grid; remember $!
  DISPLAY=:93 python3 qrxfer.py recv -o OUT -p pass.txt --timeout 90
  DISPLAY=:93 xdotool key q                      # the grid grabs the keyboard; kill -INT must also exit cleanly
  diff -r SRC OUT/...
  ```

  - **QR codes in a Tk window**: the same with `send --codec qr`; the Tk sender must exit cleanly on SIGINT too.
  - **Terminal mode** (QR codes): run the sender inside `setsid dbus-run-session -- xfce4-terminal --disable-server --geometry=… -x sh -c "…send --terminal…"`, then kill it with `kill -- -<pgid>`.
    - The terminal window must fit the Xvfb screen; nothing maximizes it without a WM.
    - Also exercise the ASCII fallback, e.g. `LANG=C LC_ALL=C xterm +u8 -fn 6x13 -e …` (8-bit xterm) or xfce4-terminal under `LANG=C`.
  - **Screenshots**: `XlibGrabber().grab(...)` + PIL `Image.fromarray(...).save(...)`, then inspect the PNG.
  - **Through a real VNC hop**: needed for throughput work, because terminal redraw and VNC updates are where frames get lost.
    - No sudo is needed: `apt-get download tigervnc-standalone-server tigervnc-common`, then `dpkg -x` both into a scratch dir.
    - Run its `usr/bin/Xtigervnc :95 -geometry 2560x1400 -depth 24 -SecurityTypes None -localhost -rfbport 5995` as the "remote".
    - Show it on an Xvfb "local" screen: `DISPLAY=:93 vncviewer localhost::5995 -SecurityTypes=None -Shared=1 -RemoteResize=0 -AutoSelect=0 -PreferredEncoding=Tight -NoJPEG=1`.
    - Run the sender on :95 (for the terminal sender, `--font="DejaVu Sans Mono 6" --geometry=508x125+0+0` mimics the real remote run: 5×10 px cells) and `recv` on :93.
    - For the grid, the X server's encoding work is part of the sender's frame time, so measure fps through such a hop, not on a plain Xvfb.
    - Requests: `recv --viewer $(DISPLAY=:93 xdotool search --class 'TigerVNC Viewer')`. The hop loses almost nothing (1.3 % at `--fps 30`); to lose screens like the real desktop, run `recv` through a scratch wrapper that sleeps 45–60 ms in `XlibGrabber.grab` (`--fps 20`).
    - Run the sender with `python3 -u` when its output goes to a file: block-buffered, "Showing the grid" appears only at exit.
- **Shell pitfalls** (the agent Bash tool runs zsh):
  - Use `${=var}` to word-split a variable.
  - Never `pkill -f`/`grep | kill` with a pattern that also appears in your own command line; it kills your own shell (exit 144). Kill recorded PIDs or process groups instead.
  - `getpass` flushes pending input (TCSAFLUSH); pty-driven tests must answer each prompt only after it appears.
  - Background jobs of a non-interactive shell start with SIGINT ignored; `kill -INT` only works because the senders install their own handlers.
  - `xdotool type --window W` sends real XTEST keys only while W has the focus, and synthetic events (XSendEvent), which a TigerVNC viewer ignores, otherwise; on an Xvfb display without a window manager nothing focuses the viewer, so `xdotool windowfocus --sync W` first (the skill helper's `type` does).

Reference results from 2026-10-11 for the pre-release prototype (Xvfb 1280×800, 3×2 grid, density medium):

| Mode | Throughput |
|---|---|
| 6 fps (default) | ≈ 21 KB/s |
| 12 fps | ≈ 43 KB/s |
| 20 fps | ≈ 65 KB/s |
| `--terminal --fps 8` (2 codes) | ≈ 9 KB/s |

- Decoding still succeeds at 2 px/module with JPEG quality 30.
- A transfer on the real dual-monitor desktop (4000×2560) succeeded.
- A real VNC link was verified on 2026-10-12: Python 3.6 terminal sender → local zbar receiver, 555 files matched by SHA-256; an external symlink was preserved without fetching its target.
  - That prototype sender reached only ~4.2 fps with 5×2 codes (2662 frames in 63 s), because encoding was CPU-bound.

Reference results from 2026-10-12/13 for the colour grid, through a real TurboVNC link on a LAN (2560×1351 remote desktop, Python 3.6 sender, local TigerVNC viewer with default AutoSelect, random data, single runs):

| Setup | Result |
|---|---|
| Channel probe, 5 px and 3 px cells, 2–64 colours (incl. 8 cube corners), viewer AutoSelect or forced QualityLevel=1 | 0 symbol errors (one in 26 million for 64 colours at 3 px); TurboVNC uses JPEG only for smooth areas |
| Channel probe, 2 px cells | rare lossy rectangles: up to 0.03 % symbol errors for 2 and 4 colours |
| Channel probe, terminal half blocks with per-cell colours | 3–6.5 fps: VTE parsing 0.8–2.2 MB of escape sequences per frame is the limit |
| Grid, `--fps 20`, 3 px (849×439 cells, 127 KB per screen), 2 MB ×5 | 4–5 s each from starting `recv`; 0.9–1.4 MB/s from the first frame |
| Same, 20 MB ×2 | 24–25 s (≈ 9 s of it the sender's xz + scrypt, measured from VNC traffic), 1.3–1.4 MB/s (before the decoder fix the last 22 blocks took 2 minutes) |
| Same, 20 MB, receiver started 35 s late | 51 s, 0.39 MB/s |
| QR codes on the same link, `--codec qr --terminal --fps 20`, 2 MB | 37–45 KB/s (18 v15 codes, sender 14.3–14.6 fps) |

- The grid sender reached ~8 fps in the first pass and ~4 fps once it also computes repair frames (Python 3.6: ~0.1 ms per repair frame, 38 ms painting per screen); the rest is `XPutImage` + `XSync`, i.e. the X server encoding for VNC (28 ms for black/white up to 112 ms for 64 colours at 2 px).

Reference results from 2026-10-12 for 1.0.0 (1.1 MB random payload, single runs, zbar unless noted):

| Setup | Prototype-equivalent | 1.0.0 |
|---|---|---|
| Terminal via local VNC hop (508×125 cells, 5 px modules) | 5×2, 8 fps: 46 KB/s; 20 fps: 47 KB/s (sender reached 8.8 fps) | 6×3 v15: 12 fps 80, 20 fps 111–168, 30 fps 138–159 KB/s |
| Same, `--grid 6x2`, 20 fps | — | 110 KB/s |
| Same, zxing-cpp receiver, 20 fps | — | 140–166 KB/s |
| Tk, Xvfb 1280×800 (3×2 v17) | 6 fps: 21; 20 fps: 60 KB/s | 12 fps (default): 42; 20 fps: 71 KB/s |
| Tk, Xvfb 2560×1440 | 4×3, 12 fps: 64 KB/s | 6×4 v16, 12 fps: 155 KB/s |

- Encoding: v17 code 7.5 ms → 0.39 ms (`selftest` reports codes/s; run it on a remote to predict its fps ceiling).
- zbar decoding is ~8× slower than zxing-cpp, and its cost per byte rises above v17.
- The line-skipping pass is ~3× faster, but it finds nothing below ~3 px/module, hence the full-density fallback.
- xfce4-terminal (VTE 0.80) redraws a 508×125 screen of half blocks at ~20 fps here.
  - It often paints a frame's top rows over the previous frame and holds that for ~50 ms, and sometimes skips frames entirely.
  - It answers DECRQM for synchronized output (mode 2026) with "permanently reset", so that cannot help.

## Environment notes (as of 2026-10-11)

- Debian 13, XFCE on X11; two monitors forming a 4000×2560 root window.
- No sudo, no camera.
- Present on the system: `python3` 3.13 with numpy, cryptography and tkinter; `libzbar0`, `Xvfb`, `xdotool`, `xfce4-terminal`, `xterm`, `ffmpeg`, `tigervnc-viewer` 1.15.
- Not present: segno, zxing-cpp, OpenCV, a VNC server (see the VNC hop recipe above), Python 3.6.

## Language

English is the only language of this repository.

- Everything committed is written in English: documentation (`README.md`, `AGENTS.md`, `skills/`), code comments, docstrings, identifiers, CLI help and messages, test names and commit messages.
  - This overrides any personal or global convention that asks for documents or comments in another language.
- Translate text in another language when you touch it; don't add to it, and don't keep parallel translations (no `README.zh-CN.md`).
- The one exception is test data that exercises Unicode handling, such as the CJK file names in `tests/test_qrxfer.py` and `tests/test_skill_transfer.py`; keep it.
- Typographic symbols are fine (—, →, ×, ≈, ≥, box drawing). Before committing, this should list only the Unicode test data:

  ```sh
  git grep -nIP '[^\x00-\x7F—–→←▶×≈≥≤…²¹⁰⁷│┌┐└┘─]'
  ```

- This covers repository content only. The language an agent uses to talk with its user, and the language the skill reports in, follow that user.

## Conventions

- Code style: `%`-formatting and the existing compact style; keep `qrxfer.py` a single file.
- Project version lives in `__version__` (currently `1.2.2`).
  - Versions skip any component containing `4` (`1.0.3` → `1.0.5`, `1.3.9` → `1.5.0`).
  - Don't bump for intermediate edits.
- Documents have no `Revision History` table: git history is the record of revisions.
  - This overrides any personal or global convention that asks for one; remove such a table if it reappears.
- Keep development artifacts (venvs, test data, Xvfb captures) out of the repo; use a scratch directory.

## No personal information

This repository is public. Never add anything that identifies a person, a specific machine or an organisation.

- **No author information in files**: no names, initials, handles or e-mail addresses, and no AI model credits.
  - This overrides any personal or global convention that asks for an Author column, byline or `Name + <model>` credit.
  - The `LICENSE` holder is "The qrxfer contributors".
  - Authorship lives only in git commit metadata, under the repository-local pseudonymous identity (`git config --local user.name` / `user.email`).
    Set it in a fresh clone before the first commit, so a global real-name or work identity never reaches history.
- **No local or private details**: no home-directory paths or user names, real host names, internal project names, real IP addresses, credentials, or captures of real desktops.
  - Use `~`, placeholders (`SKILL_DIR`, `REPO`) or documentation addresses (`192.0.2.x`).
- **Check before committing**: scan the staged tree, e.g. `git grep --cached -nIiE '/home/|[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}'`, and review every hit.
  - History is published too: fix a leak in an unpushed commit by amending or rewriting it, not by adding a new commit on top.

## Known limitations / possible future work

- One direction only (see above); Wayland-only sessions may not be capturable with `XGetImage`.
- Captures of a 2560×1351 region on this machine (idle screen): `XGetImage` median 10.6 ms, MIT-SHM 3.5–4.4 ms; both slow down while the viewer redraws (see `XlibGrabber`).
- Practical for KB to hundreds of MB (grid measured up to 857 MB on the real link); time grows linearly with size.
  - A 600 MB transfer crashed a Python 3.6 sender in `encrypt` (fixed by `QXF2`). The decoder for its k ≈ 2 million blocks was simulated offline (2560×1351 grid, 439 rows per screen, first-pass loss 2 % as measured on the real link): done 517 screens after the first pass, 151 s of decoder CPU, 2.3 GB peak. The old decoder passed 8.8 GB and kept slowing down.
  - The tail is a coupon collector: each repair frame names ~192 of k blocks, so the last missing blocks need ~k/192 × ln(missing) frames; with much higher loss, elimination over tens of thousands of rows makes the decoder the bottleneck (5 % simulated: 577 screens, 930 s of decoder CPU plus 83 s in `result()`, 2.85 GB).
  - The real link (2026-10-20, 571 MB, k = 2,015,968, Python 3.6 sender at ~6 fps, 1.1.0 code) was much slower than that simulation, which assumed every screen gets read:
    - A receiver started during the first pass timed out after 90 minutes at 97 %.
    - One started during the repeat phase reached full rank after 3:16, then spent 13 more silent minutes in `result()` and decryption (12,200 s of CPU, 5.6 GB peak).
    - Below 7/8 known, the decoder eliminated every repair frame, which cost seconds per captured repair screen on the capture loop's thread. Meanwhile the receiver missed the repeat screens that carry the missing blocks.
    - While repair frames were still skipped it took in ~1,460 blocks/s (every repeat screen); afterwards 6–40 blocks/s, with 11–80 s between progress lines.
    - One cycle of repeat screens took ~23 minutes, and each repeat screen stayed up four times as long as a repair screen, because the next repair screen was made while it was shown.
  - Since then: repair frames wait until at most min(k/16, 32,768) blocks are missing, three repeat screens follow each repair screen from k ≥ 2^16, and every screen makes a share of the next repair screen.
    - A time-driven model checked these changes: sender screens take 61.5 ms plus 0.41 ms per repair frame made (the real remote), and the receiver reads a screen only if its capture loop is free while the screen is up, the real `FountainDecoder`'s CPU time keeping the loop busy.
      With the 1.1.0 code the model tracked the real late receiver within a few percent up to 30 minutes (94 %).
    - With the current code, at k = 2,015,968: late receiver 12.5 min (41 s decoder CPU, 2.4 s `result()`); receiver started first with 7 % of first-pass screens lost: 14.7 min, while 1.1.0 was at 97.8 % after 30 min.
    - Receiver started first, 2 % lost, two seeds run side by side on idle cores: 8.5 and 11.6 min, against 8.0 and 11.3 min for 1.1.0 (decoder CPU 71–75 s against 158–338 s); one repeat screen instead of three: 8.7 and 14.7 min.
    - Runs vary by minutes with where the screens were lost and with CPU contention, since the decoder's measured CPU time feeds back into what the receiver misses: compare only runs made side by side, each on its own physical core.
    - When 30 % of all screens are lost, the late receiver takes 40 min: each round of repeat screens fills only 70 % of the gaps.
  - Through a local TigerVNC hop (Python 3.6 sender in Docker, 20 MB = 67,346 blocks, single runs), receiver started 45 s after the sender: 1.1.0 83 s; one repeat screen per repair screen 40–44 s; three 20 s.
    Receiver started first: 27 s with one or three. The time-driven model agrees (late: 25–31 s against 52–64 s; first: equal), hence three repeat screens from 2^16 blocks.
  - A 1.9 GB source tree (18,925 files) through the real link on 2026-10-21 (skill run, 1.2.0 sender on Python 3.6): the grid appeared only after 32 minutes, 26 of them in single-threaded xz, hence `XzWriter`.
    - The skill's `bundle` (preset 6, 1.9 GB → 857 MB) took 18 min; `send` repacking that already-compressed payload (preset 6e) took 7.5 min more, before its passphrase prompt.
    - Python 3.6 in Docker pinned to 6 cores, after the change: bundling a 78 MB tree 31.6 → 12.1 s (four chunks), repacking 192 MiB of incompressible data 94 → 26 s (eight chunks, two rounds).
    - Then k = 2,887,084, timed from the first frame: the first pass took 9.9 min at 1.32–1.45 MB/s and left 4.6 % missing. Repeats alone (repair frames skipped above 32,768 missing) took 19.2 min more at 5–70 KB/s, the sender at 5.4–7.7 fps; repair frames then finished in 3.6 min.
      The progress line showed the average since the first frame, which fell to 433 KB/s and looked like a slowing link; hence the recent rate beside it.
    - A local dock (48 px, hidden while the viewer is the active window) covered the viewer's bottom 49 px whenever another window was active: grid rows 432–439 and the bottom ring failed in every capture, and the first round of repeats showed those blocks in the same rows again.
      Hence the shifted repeat rounds, the covered-row notice and the skill's `place`.
    - The time-driven model with this run's k and sender timing (first pass 0.0905 s per screen, 0.62 ms per repair frame made) and 4.6 % of screens lost: complete at 31.2 min (real: 32.7 min).
      - Skipping repair frames only above 131,072 missing blocks: 39.8 min; never: 42.8 min (decoder CPU 1,600–1,800 s, so most screens are missed).
      - The bottom 8 rows covered on every capture: aligned repeats 47.5 min, shifted repeats 33.5 min.
      - 2 % of screens lost: 24.9 min; 1 %: 13.7 min, since fewer than 32,768 blocks are missing right after the first pass.
    - Through a local VNC hop (Xtigervnc 1.15 → TigerVNC viewer on Xvfb, 20 MB, single runs): `--fps 20`, `25` and `30` all ran at 11.1–11.2 fps, the sender's own limit.
      Captures per screen, by slowing the receiver's grabs: 2.1×: 1.23 screens shown per screen of data; 1.6×: 1.07; 1.4×: 1.08; 0.9×: 1.97; 0.55×: 5.06.
  - Retest through the same real link that evening: 1.2.1 on both sides (the remote `script:` copy updated first), a skill run with `script:`, the viewer moved with `place`, a 351 MB test tree of 12,336 files (Python's stdlib, documentation, 150 MB of random data).
    - `bundle` took 30 s on the remote's 56 cores (single-threaded `xz -6` of the same tar: 3:42) and wrote 202.6 MB; `send` reached its passphrase prompt 16–21 s after the command was typed, and showed the grid 7 s after the second passphrase.
    - k = 683,902, timed from the first frame: the first pass took 2:05 at 1.5–1.6 MB/s (about 12.5 screens per second; the status line showed 14.8 fps at 23 %) and left 1.4 % missing.
      That is under 32,768 blocks, so repair frames took over at once (sender at 5.6 fps) and brought the rest in 43 s; complete after 2:48, decrypted and saved 2 s later. No covered-row notice; `install` passed.
    - Typing the 94-thousand-character bootstrap through the viewer lost keys once (zlib error before anything was created); the second attempt worked (1:48 and 2:10 of typing).
- The grid needs a 24-bit X display on the sender; it covers the whole screen, so the remote desktop is unusable while sending.
- Terminal mode is bounded by the terminal's redraw rate, not by qrxfer.
- Screensavers on either side hide the grid. Both idle timers start at the last keystroke typed through the viewer, so both fire in the middle of a long transfer (seen after 30 min on both sides, receiver stalled until woken). Keep both awake: pointer motion over the viewer wakes the remote without sending the sender a key; the skill's `keepawake` helper does that every 4 minutes.
- The skill still compresses twice: `send` packs and xz-compresses the bundle's payload.tar.xz again, now on every core but for nothing; a store-only pack for already-compressed input would save that pass.
- Retest on 2026-10-27 through the same real link, 1.2.1 on both sides, a skill run: 820.9 MB, k = 2,898,277, sender at ~15 fps in the first pass.
  - Receiver started first: the first pass took 8 min at 1.7 MB/s and left 9.3 % (≈ 270,000 blocks) missing, since `recv` made only ~21 captures per second (1.4 per screen, see `XlibGrabber`).
    The repeats then brought 40–80 KB/s for 18 min (each repeat screen held ~9 % new blocks; a round of repeats takes ~16 min at 8.9 fps, three of four screens), repair frames the last 1.1 % in 2 min: complete after 28:22. Extraction then failed with ENOSPC on a full tmpfs `/tmp`.
  - Receiver restarted during the repeats, its output moved off the tmpfs: 20 min for one round of repeats (98.4 %, i.e. only 1.6 % lost at 2.4 captures per screen), 7 min more of repeats, then repair frames; their last ~1,600 blocks took ~4 min (coupon collector). Complete after 30:59.
  - Hence `recv --viewer`: after the first pass, the missing blocks come at first-pass speed, ~1 min for 270,000 blocks instead of rounds of repeats. Not yet run through the real link.
  - A probe sampling grid rows from six X connections at once made the viewer show only ~75 % of the sender's screens: captures stall the viewer, so measure with as little capturing as possible.
- Elimination still runs on the capture loop's thread. A decoder thread with a bounded queue of repair frames would keep the capture going while it works, at the price of GIL contention.
- The grid sender's repair phase is CPU-bound in Python (`repair_indices`); a lower repair degree for large k could help, re-check the overhead with the fountain tests.
