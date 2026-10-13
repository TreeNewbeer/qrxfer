# qrxfer — encrypted file transfer through the screen

`qrxfer` lets two Linux machines exchange files **through nothing but the screen**. The sender packs, compresses and encrypts the files, cuts them into fountain-code frames and shows them as an animated full-screen grid of coloured cells (or, as a fallback, as QR codes). The receiver captures its own screen (for example a VNC window showing the remote desktop), and once it has enough data it decrypts and unpacks the files automatically. No network port is opened and no camera is needed.

```
 Remote machine (VNC server)                   Local machine (VNC viewer)
┌──────────────────────────┐   VNC screen  ┌───────────────────────────┐
│ qrxfer send FILES/DIRS   │ ───────────▶  │ qrxfer recv               │
│  tar.xz → encrypt →      │               │  capture → read the grid  │
│  fountain code →         │               │  (or QR codes) → fountain │
│  full-screen colour grid │               │  decode → verify, decrypt │
└──────────────────────────┘               └───────────────────────────┘
```

## Features

- **Colour grid, ~30× faster than QR codes**: the sender fills the whole screen with 3-pixel cells in the 8 corner colours of the RGB cube (black, white, red, green, blue, cyan, magenta, yellow: 3 bits per cell). Each row of cells carries one fountain frame with its own CRC, and a black/white timing ring around the cells gives the receiver every cell boundary. VNC servers send such sharp-edged content with few colours losslessly; on a real TurboVNC link a 20 MB file arrived at 1.3–1.4 MB/s (24–25 s including the sender's compression and encryption) where QR codes managed 37–45 KB/s.
- **No dependencies on the sender**: only the Python ≥ 3.6 standard library. The grid window talks to libX11 through ctypes, which every X desktop has, so no Tk is needed. QR codes remain available with `--codec qr`: in a Tk window, or with `--terminal` directly in the terminal (built-in pure-Python QR encoder, verified bit for bit against segno; the terminal mode detects UTF-8 support and falls back to plain ASCII background colours).
- **Few dependencies on the receiver**: numpy reads the grid; libzbar (usually already present on a Debian desktop) or zxing-cpp (faster) is only needed for QR codes. The screen is captured directly through libX11; OpenCV is not needed. The receiver recognises a grid or QR codes by itself.
- **Fountain code**: the receiver can start at any time; lost, torn or skipped frames do not matter. About k frames (k = number of data blocks) are enough to rebuild the data, with no need to wait for the next cycle.
- **QR codes: many codes per screen, code size chosen automatically**: when sending starts, the QR code size (versions 10–17) and the layout are chosen together from the window or terminal size so that each screen carries the most data. For example, a 508×125-cell terminal gets 6×3 = 18 codes and a 2560×1440 screen gets 6×4 = 24 codes. You can also force a layout such as `--grid 6x2` (6 on top, 6 below); the code size then shrinks to fit exactly.
- **Fast encoding**: the sender is pure Python. A grid screen (849×439 cells on a 2560×1351 desktop) is painted in about 38 ms on Python 3.6; a QR code takes about 0.4 ms (v17, Python 3.13), with a fixed mask, shift-register Reed-Solomon encoding and whole-row module placement.
- **Automatic region tracking**: the receiver first searches the whole screen, then captures only the area that holds the grid or the codes, and rescans the full screen periodically (so it keeps up if the VNC window moves).
- **Multithreaded, tear-tolerant decoding**: screen capture and decoding run as a parallel pipeline (up to 4 decoding threads by default). A VNC viewer or terminal caught half-way through a redraw costs only the grid rows (or the row of QR codes) the tear crosses; for QR codes, zbar would return nothing for the whole image, so the receiver reads each band of codes separately.
- **Safe extraction**: absolute paths, `..` and device files are rejected; existing files are never overwritten (a name clash becomes `name (1).ext`).

## Installation

Copy `qrxfer.py` to both machines (it is a single file). Alternatively, run the install script, which copies it to `~/.local/bin/qrxfer` and runs the self-test:

```sh
./install.sh                          # install to ~/.local/bin/qrxfer
QRXFER_BIN_DIR=/opt/bin ./install.sh  # install to another directory
```

| Role | Needs | Debian/Ubuntu install |
|---|---|---|
| Sender (remote) | Python ≥ 3.6 and an X11 desktop with 24-bit colour; tkinter only for QR codes in a window | nothing (`sudo apt install python3-tk` for `--codec qr` without `--terminal`) |
| Receiver (local) | numpy, an X11 session; libzbar0 or zxing-cpp for QR codes | `sudo apt install python3-numpy`; for QR codes `libzbar0` or `python3-zxing-cpp` |

`qrxfer selftest` checks what this machine can do and runs the whole pipeline once offline (pack → encrypt → grid paint → grid read and QR encode → QR read → fountain decode → decrypt → unpack). On the sender it also reports the grid size and painting time per screen.

## Quick start (VNC)

1. **Local machine** (receiver):

   ```sh
   qrxfer recv -o ~/Downloads/qrx
   ```

   Press Enter at the passphrase prompt to generate a random passphrase (such as `7KQ2-MX9P-4TWA-ZC8R`: 80 bits of entropy, case-insensitive, with `O/I/L` read as `0/1/1`). It is shown in the local terminal.

2. **Remote machine** (inside the VNC window):

   ```sh
   qrxfer send report.pdf project/
   ```

   Enter the same passphrase twice; a full-screen grid of coloured cells then covers the remote desktop, with a status line at the bottom.

3. When the local progress bar reaches 100 %, the receiver decrypts, unpacks and lists the saved files. Then press `q` in the VNC window to close the grid: it holds the keyboard while it is shown, so keys typed into the VNC viewer reach it.

Either side may start first. After the first pass over the data, the sender alternates screens of repair frames with screens that repeat the original frames, so a receiver started late still catches up quickly.

### Sender keys

| Key | Action |
|---|---|
| `+` / `-` | Faster / slower (×1.25 per press) |
| Space | Pause / resume |
| `f` | Toggle full screen (QR codes in a Tk window) |
| `q` / `Esc` / Ctrl+C | Quit |

## Agent skill: download from a VNC desktop

The repository ships an agent skill whose text lives in `skills/qrxfer/SKILL.md`. Once installed, type this in Claude Code:

```text
/qrxfer <server> <password> <source path> <dest path>
/qrxfer <server> <password> [script:<remote qrxfer path>] source:<source path> dest:<dest path>
```

Quote arguments that contain spaces. In the second form the labelled values may come in any order, and a label may be followed by a space (`dest: ~/Downloads`); server and password always come first, unlabelled. A single colon in the server is a VNC display number, e.g. `192.0.2.10:69` means port 5969; write an explicit port as `192.0.2.10::5969`. The source is an absolute path on the remote machine and the destination is the local parent directory: a source directory `project` is saved as `project` under dest, and an existing item with the same name makes the download land under a new name with a numeric suffix. The password is the VNC login password; the transfer itself uses a separately generated random passphrase, and the skill stores no credentials.

Without `script:`, the skill types this repository's `qrxfer.py` into a temporary directory on the remote through the VNC keyboard (about 79 thousand characters). If qrxfer is already on the remote machine, pass its absolute path as `script:` and only the small packing helper is typed in (about 12 thousand). The skill reads that copy's version and SHA-256 first and runs it, but never changes or deletes it; a copy from before the colour grid sends QR codes in the terminal instead.

The skill reuses or opens a VNC session, receives the files through the colour grid, verifies SHA-256 digests, the directory structure and symlinks item by item, and saves the result only after verification passes. The remote side needs no Tk; the grid window needs only libX11, and the terminal QR mode remains as a fallback. An external symlink is kept as a link; its target is not copied. The local helper's install step needs Python ≥ 3.12; packing on the remote side still needs only the Python ≥ 3.6 standard library.

To install it, symlink this repository directory into your personal skill directories, so both clients share one copy of the text:

```sh
ln -s "$PWD/skills/qrxfer" ~/.claude/skills/qrxfer   # Claude Code
ln -s "$PWD/skills/qrxfer" ~/.agents/skills/qrxfer   # Codex
```

Claude Code uses the directory name as the command name ([documentation](https://code.claude.com/docs/en/skills#how-a-skill-gets-its-command-name)); in Codex, use `$qrxfer` with the same arguments, or ask explicitly for the skill ([skill documentation](https://learn.chatgpt.com/docs/build-skills)). If a new session does not list the entry yet, reopen the session.

This is a skill invocation run by an agent, not a shell command. It requires the local machine to have an X11 desktop and to be able to reach the VNC server.

## Command reference

```
qrxfer send FILE_OR_DIR...  [-p FILE] [--codec auto|grid|qr] [--fps 12] [--cell-px 3]
                            QR codes: [--density auto|low|medium|high|BYTES] [--grid auto|COLSxROWS]
                            [--max-codes 24] [--min-px 4] [--ecc L|M|Q|H] [--terminal] [--windowed]
qrxfer recv    [-o DIR] [-p FILE] [--codec auto|grid|qr] [--decoder auto|zxing|zbar] [--region X,Y,W,H]
               [--timeout SECONDS] [--threads N]
qrxfer decrypt FILE.qxf [-o DIR] [-p FILE]
qrxfer genpass
qrxfer selftest
```

- `-p/--passphrase-file FILE`: read the passphrase from a file instead of prompting; the `QRXFER_PASSPHRASE` environment variable also works.
- `--codec` (send): `grid` shows the full-screen colour grid; `qr` shows QR codes in a Tk window, or in the terminal with `--terminal`. The default `auto` uses the grid, and QR codes in the terminal when no X display can be opened (or when `--terminal` is given).
- `--codec` (recv): what to look for. The default `auto` finds either; `grid` needs no QR decoder at all.
- `--fps`: screens per second, 12 by default. The status line shows actual/target fps; when the actual value falls behind the target, the sender's CPU, the X server (which encodes the screen for VNC) or the terminal has reached its limit.
- `--cell-px`: grid cell size in pixels, 3 by default. 2 px puts more on each screen but leaves less margin (rare lossy VNC rectangles then cost a few rows); larger cells help with a scaled VNC view.
- `--density`: data carried by each QR code. The default `auto` picks, when sending starts, the code size (versions 10–17) that puts the most data on one screen for the display area. The code size stays fixed for the whole transfer, so in terminal mode **set the window size and font size before starting**. It can also be fixed as `low` = 300 B, `medium` = 600 B (version 17-L), `high` = 1000 B, or a byte count.
- `--grid`: layout of codes on each screen. The default `auto` fits as many codes as possible (at most `--max-codes`) while keeping modules at least `--min-px` pixels. `COLSxROWS` fixes the layout; for example `--grid 6x2` means 6 on top and 6 below. With `--density auto`, the largest code that fits that layout is chosen.
- `--region`: only watch one part of the screen (rarely needed; the codes are located automatically).
- `--threads N`: number of captures decoded at the same time; by default the CPU count minus 1, at most 4.
- When decryption fails (wrong passphrase) you get 3 retries; after that the encrypted data is saved as `qrxfer-YYYYMMDD-HHMMSS.qxf`, so you can retry later with `qrxfer decrypt` without sending again.

## Speed and tuning

### Colour grid

Measured on 2026-10-13 through a real TurboVNC link on a LAN: a 2560×1351 remote desktop with a Python 3.6 sender, shown 1:1 in a TigerVNC viewer with default settings; random (incompressible) data, `--fps 20`, default 3 px cells (849×439 cells, 127 KB per screen):

| Transfer | Time from starting `recv` | Rate shown by `recv` |
|---|---|---|
| 2 MB, five runs | 4–5 s | 0.9–1.4 MB/s |
| 20 MB, two runs | 24–25 s | 1.3–1.4 MB/s throughout |
| 20 MB, `recv` started 35 s after `send` | 51 s | 0.39 MB/s (catches up from the repeated frames) |
| For comparison: QR codes, `--codec qr --terminal`, 2 MB | 57–58 s | 37–38 KB/s |

- The time includes about 9 s in which the Python 3.6 sender compresses and encrypts 20 MB before it shows the grid; `recv` measures its rate from the first frame.
- The sender reached about 8 screens per second in the first pass and about 4 later, when it also computes repair frames. Most of a frame's time goes to the X server, which encodes the screen for VNC; painting takes 38 ms in Python 3.6.
- In a channel test before this design, cells of up to 64 colours arrived bit-exact over the same link, even with the viewer forced to the lowest JPEG quality: VNC encoders use JPEG only for smooth, photo-like areas. The 8 cube-corner colours keep the largest possible distance, so they also survive VNC servers that do compress more colours.
- Keep the VNC view at 100 % scale and the whole remote screen visible. A scaled view still works (the timing rings give every cell boundary), but blends colours at cell edges.

### QR codes

Measured on 2026-10-12 (local Python 3.13, 1.1 MB of random data, one run each; zbar receiver unless noted):

| Setup | Early prototype | 1.0.0 |
|---|---|---|
| Terminal mode through VNC: xfce4-terminal with 508×125 cells (font size 6, 5 px per module) on Xvnc, lossless TigerVNC viewer | 5×2 codes, `--fps 8`: 46 KB/s; `--fps 20`: 47 KB/s (reached only 8.8 fps) | 6×3 codes, `--fps 12`: 80 KB/s; `--fps 20`: 111–168 KB/s; `--fps 30`: 138–159 KB/s |
| Same, `--grid 6x2` (6 on top, 6 below), `--fps 20` | — | 110 KB/s |
| Tk window, Xvfb 1280×800 (3×2 codes) | `--fps 6`: 21 KB/s; `--fps 20`: 60 KB/s | `--fps 12` (default): 42 KB/s; `--fps 20`: 71 KB/s |
| Tk window, Xvfb 2560×1440 | 4×3 codes, `--fps 12`: 64 KB/s | 6×4 codes, `--fps 12`: 155 KB/s |

In terminal mode, `--grid 6x2` shows only 12 v15 codes, while the automatic layout fits 18 in the same terminal, which is why it is faster. On a real VNC link (Python 3.6 remote, 5×2 codes) the early prototype reached only about 4 fps, held back mainly by choosing masks in pure Python; 1.0.0 removes that bottleneck.

Text files are compressed with xz first, so the effective file throughput is higher. Setting `fps` faster than the terminal or VNC can refresh only drops more frames (which the fountain code tolerates); it does not cause errors. In the terminal setup above, throughput kept rising from 8 fps to 30 fps. When xfce4-terminal (VTE) redraws a screen full of block characters, it shows part of the new frame over part of the old one and sometimes skips frames entirely; this is the main limit of terminal mode. Recommendations:

- In terminal mode, enlarge the terminal and shrink the font **before** starting `send`, so each module is about 4–5 pixels (for example DejaVu Sans Mono at size 6, with 5×10 px cells). The code size is chosen at start from the terminal size at that moment.
- Watch actual/target fps on the status line: while the actual value keeps up, press `+` to go faster; when it falls behind, the limit has been reached.
- With zxing-cpp on the receiver (`python3-zxing-cpp` or `pip install zxing-cpp`), decoding is about 8× faster than zbar. At high fps it catches more frames: 140–166 KB/s under the same conditions.
- Show the remote desktop at **100 % scale** in the VNC viewer if possible. Simulations show that codes are still read at 50 % scale (2 pixels per module) with JPEG quality 30, but with less margin.
- Keep the VNC window visible during the transfer: do not cover it with other windows or minimize it.
- If reading is unreliable, use `--density low` or `--min-px 6` (bigger modules). To go faster, press `+` or raise `--fps`.

## Security

- **Encryption**: scrypt (N=2¹⁷, r=8, p=1, about 128 MiB of memory; PBKDF2-SHA256 with 2²⁰ iterations on old OpenSSL builds without scrypt) derives a 64-byte key from the passphrase and a 16-byte random salt. Half of it keys a SHAKE-256 keystream for encryption, the other half keys HMAC-SHA256 for authentication (encrypt-then-MAC). Every transfer gets a new salt and new keys. Everything uses the Python standard library (hashlib/hmac, backed by OpenSSL or CPython's built-in implementations), so the remote machine needs no crypto library.
- **Integrity**: every frame carries a CRC32 and the whole payload an HMAC; a wrong passphrase or tampered data is rejected and nothing is written.
- **What it protects against**: if the screen is photographed, recorded or seen by someone else, the file contents cannot be recovered without the passphrase.
- **What to watch out for**: the passphrase is typed on the remote machine (through the VNC keyboard). **If the VNC connection itself is not encrypted** (no TLS or SSH tunnel), an eavesdropper on the network sees both the screen and the keystrokes, and the encryption is pointless. In that case:
  - tunnel VNC through SSH or enable TLS; or
  - put the same passphrase file on both machines in advance through another channel (such as a USB stick) and use `-p PASSPHRASE_FILE` on both sides, so the passphrase never crosses VNC.
- If you choose your own passphrase, make it long enough; prefer `qrxfer genpass` or pressing Enter on the receiver to generate one.

## Limitations

- The screen channel is one-way: files can only go from **the side that shows the screen (VNC server / remote)** to **the side that watches it (local)**. Local → remote is not possible this way.
- Screen capture on the receiver needs X11 (XFCE, MATE and other X11 sessions all work); in a pure Wayland session the window contents may not be capturable. The grid window on the sender likewise needs an X11 display with 24-bit colour; otherwise use QR codes (`--terminal`).
- Suited to files from KB to tens of MB (measured up to 20 MB with the grid); time grows linearly with size (see the tables above).

## Development and testing

```sh
python3 -m unittest discover -s tests   # unit tests + receive pipeline tests (cases needing numpy or a decoder are skipped when absent)
python3 qrxfer.py selftest              # environment check + offline end-to-end test
```

When `segno` is installed, the tests also compare the built-in QR encoder's output with segno bit for bit. The grid window itself is tested end to end on a virtual display (Xvfb); see `AGENTS.md`.

## Files

| File | Description |
|---|---|
| `qrxfer.py` | The program (a single file that can be copied to any machine) |
| `install.sh` | Installs to `~/.local/bin` and runs the self-test |
| `tests/test_qrxfer.py` | Tests |
| `skills/qrxfer/SKILL.md` | VNC download skill: the argument convention and the procedure |
| `skills/qrxfer/scripts/transfer.py` | Run preparation, targeted keyboard input, bundle manifest, verified install |
| `tests/test_skill_transfer.py` | Offline workflow and failure-path tests for the skill |
| `LICENSE` | MIT license |

## License

MIT, see [LICENSE](LICENSE).
