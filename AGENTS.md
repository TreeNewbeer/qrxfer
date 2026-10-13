# AGENTS.md — qrxfer

Guidance for coding agents working in this repository. User-facing documentation is `README.md`.

## What this project is

`qrxfer` is an encrypted, one-way file transfer through animated QR codes on a screen. No network port is opened.

- Typical setup: the user reaches a remote Linux machine through a VNC viewer on the local machine.
  - The remote (VNC server side) runs `qrxfer send FILES...`, which shows a grid of QR codes.
  - The local machine (viewer side) runs `qrxfer recv`, which captures its own X11 screen, decodes the codes and rebuilds the files.
- Direction is inherently **screen-showing side → screen-watching side** only.
- No camera is involved; this machine has none.

## Layout

| Path | Purpose |
|---|---|
| `qrxfer.py` | The whole program, a single file meant to be copied to both machines |
| `tests/test_qrxfer.py` | unittest suite (24 tests) |
| `install.sh` | Copies `qrxfer.py` to `~/.local/bin/qrxfer` (or `$QRXFER_BIN_DIR`), then runs `selftest` |
| `README.md` | User guide |
| `skills/qrxfer/` | Shared VNC download skill and deterministic bundle/install helper |

`qrxfer.py` sections, in file order:

1. **Base45** (RFC 9285): the QR alphanumeric charset.
2. **Fountain code**:
   - Frames `0..k-1` are systematic; later frames XOR `degree` blocks.
   - The receiver does incremental GF(2) Gaussian elimination: `FountainDecoder`, and `rank == k` means complete.
3. **Encryption**: stdlib only.
   - scrypt (`N=2^17, r=8, p=1`), falling back to PBKDF2 2^20 iterations where scrypt is unavailable.
   - SHAKE-256 keystream plus HMAC-SHA256, encrypt-then-MAC.
   - Container magic `QXF1`.
4. **Archive**:
   - tar.xz packing.
   - Extraction into a staging dir with the tar `data` filter; files are never overwritten (`name (1).ext`).
5. **QR encoder**: pure Python; alphanumeric mode, versions 1–40, levels L/M/Q/H.
   - Speed matters: the sender's CPU was the bottleneck on a real Python 3.6 remote.
   - Reed-Solomon parity is a shift register held in one int (`_rs_products` tables).
   - Modules are placed a whole row at a time with `operator.itemgetter` (`_QrTemplate.row_bits`).
   - `qr_encode(mask=None)` still searches the penalty-minimal mask; frames use the fixed `FRAME_MASK`.
6. **Sender**:
   - `FrameStream` (fixed `FRAME_MASK`), `plan_grid` for the multi-code layout.
   - `choose_block_size` for `--density auto` (the default): it picks the QR version in `AUTO_VERSIONS` (10–17) that puts the most data on one screen.
   - The runners take a `make_stream(width, height, min_px)` callback, because the code size depends on the display area at start.
   - Tk window (PGM `PhotoImage`, with a `put()` fallback for Tk 8.5).
   - Terminal renderer: UTF-8 half blocks (built with an int/hex/`translate` trick), or an ASCII background-colour fallback chosen by a cursor-position probe.
   - The status line shows actual/target fps.
7. **Receiver**:
   - `XlibGrabber` (ctypes `XGetImage`).
   - Decoders `ZxingDecoder` (zxing-cpp) and `ZbarDecoder` (ctypes libzbar).
     - zbar returns nothing for an image with one torn code, so it scans each band between blank pixel rows (`_code_bands`) separately.
     - Each band gets a line-skipping pass first, then a full-density pass when that finds nothing or fewer codes than last time.
   - `RegionTracker` (crops to the codes, rescans the full screen every 4 s).
   - `collect_from_screen`: grabs on the main thread and decodes in `threads` worker threads, each with its own decoder instance (ctypes releases the GIL).
8. **Commands**: `send`, `recv`, `decrypt`, `genpass`, `selftest`, wired up in `build_parser`.

## Invariants — do not break

- **Sender side is stdlib-only and runs on Python ≥ 3.6.**
  - Don't add third-party imports to the send path.
  - Avoid 3.7+ features (walrus, `int.bit_count`, `str.removeprefix`, `functools.cache`, runtime `list[int]` annotations).
  - Check with `vermin --no-tips qrxfer.py`; it must report `3.6`.
  - Receiver-only deps (numpy, libzbar/zxing-cpp) must stay lazily imported.
- **Wire format is shared by two machines that may run different copies:**
  - frame header `>BIIHIH` plus CRC32;
  - Base45 wrapped as `QX:` … `.`;
  - `_SplitMix64` / `repair_indices`;
  - the `QXF1` container layout and KDF ids.
  - Any change needs a `FRAME_VERSION` (or container magic) bump, and must update the golden values in `FrameTest.test_wire_format_is_stable`.
- The repair **degree distribution** (`repair_degree`, `REPAIR_DEGREE_CAP = 256`) is sender-only, because the degree travels in each frame. It may be tuned freely; re-check the overhead with the fountain tests.
- Also sender-only, since every QR code carries its own version and mask: `FRAME_MASK`, `AUTO_VERSIONS`, the block size and the layout.
  - Before raising `AUTO_VERSIONS` above 17, measure zbar: its decode time per byte rises steeply for bigger codes.
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
  DISPLAY=:93 python3 qrxfer.py send SRC -p pass.txt &        # remember $!
  DISPLAY=:93 python3 qrxfer.py recv -o OUT -p pass.txt --timeout 90
  kill -INT <sender pid>                         # the Tk sender must exit cleanly on SIGINT
  diff -r SRC OUT/...
  ```

  - **Terminal mode**: run the sender inside `setsid dbus-run-session -- xfce4-terminal --disable-server --geometry=… -x sh -c "…send --terminal…"`, then kill it with `kill -- -<pgid>`.
    - The terminal window must fit the Xvfb screen; nothing maximizes it without a WM.
    - Also exercise the ASCII fallback, e.g. `LANG=C LC_ALL=C xterm +u8 -fn 6x13 -e …` (8-bit xterm) or xfce4-terminal under `LANG=C`.
  - **Screenshots**: `XlibGrabber().grab(...)` + PIL `Image.fromarray(...).save(...)`, then inspect the PNG.
  - **Through a real VNC hop**: needed for throughput work, because terminal redraw and VNC updates are where frames get lost.
    - No sudo is needed: `apt-get download tigervnc-standalone-server tigervnc-common`, then `dpkg -x` both into a scratch dir.
    - Run its `usr/bin/Xtigervnc :95 -geometry 2560x1400 -depth 24 -SecurityTypes None -localhost -rfbport 5995` as the "remote".
    - Show it on an Xvfb "local" screen: `DISPLAY=:93 vncviewer localhost::5995 -SecurityTypes=None -Shared=1 -RemoteResize=0 -AutoSelect=0 -PreferredEncoding=Tight -NoJPEG=1`.
    - Run the terminal sender on :95 (`--font="DejaVu Sans Mono 6" --geometry=508x125+0+0` mimics the real remote run: 5×10 px cells) and `recv` on :93.
- **Shell pitfalls** (the agent Bash tool runs zsh):
  - Use `${=var}` to word-split a variable.
  - Never `pkill -f`/`grep | kill` with a pattern that also appears in your own command line; it kills your own shell (exit 144). Kill recorded PIDs or process groups instead.
  - `getpass` flushes pending input (TCSAFLUSH); pty-driven tests must answer each prompt only after it appears.

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
- Project version lives in `__version__` (currently `1.0.0`).
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
- Region capture costs 10–30 ms and a full-screen grab ~73 ms on this machine; XShm could speed it up if needed.
- Practical for KB to tens of MB; time grows linearly with size.
- Terminal mode is bounded by the terminal's redraw rate, not by qrxfer.
- 1.0.0 has not yet been measured on the real Python 3.6 VNC link.
