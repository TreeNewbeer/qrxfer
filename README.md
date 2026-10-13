# qrxfer — encrypted file transfer through on-screen QR codes

`qrxfer` lets two Linux machines exchange files **through nothing but the screen**. The sender packs, compresses and encrypts the files, cuts them into fountain-code frames and shows them as an animated grid of QR codes. The receiver captures its own screen (for example a VNC window showing the remote desktop), and once it has enough data it decrypts and unpacks the files automatically. No network port is opened and no camera is needed.

```
 Remote machine (VNC server)                   Local machine (VNC viewer)
┌──────────────────────────┐   VNC screen  ┌───────────────────────────┐
│ qrxfer send FILES/DIRS   │ ───────────▶  │ qrxfer recv               │
│  tar.xz → encrypt →      │               │  capture → read QR codes  │
│  fountain code →         │               │  → fountain decode →      │
│  full-screen QR grid     │               │  verify, decrypt, unpack  │
└──────────────────────────┘               └───────────────────────────┘
```

## Features

- **No dependencies on the sender**: only the Python ≥ 3.6 standard library (with a built-in pure-Python QR encoder, verified bit for bit against segno). With tkinter it shows a full-screen window; without it, `--terminal` draws the codes directly in the terminal (it detects whether the terminal supports UTF-8 and falls back to plain ASCII background colours when it does not).
- **Few dependencies on the receiver**: numpy + libzbar (usually already present on a Debian desktop), optionally zxing-cpp (faster). The screen is captured directly through libX11; OpenCV is not needed.
- **Fountain code**: the receiver can start at any time; lost, torn or skipped frames do not matter. About k frames (k = number of data blocks) are enough to rebuild the data, with no need to wait for the next cycle.
- **Many codes per screen, code size chosen automatically**: when sending starts, the QR code size (versions 10–17) and the layout are chosen together from the window or terminal size so that each screen carries the most data. For example, a 508×125-cell terminal gets 6×3 = 18 codes and a 2560×1440 screen gets 6×4 = 24 codes. You can also force a layout such as `--grid 6x2` (6 on top, 6 below); the code size then shrinks to fit exactly.
- **Fast encoding**: the sender is pure Python, with a fixed mask, shift-register Reed-Solomon encoding and whole-row module placement. Each code takes about 0.4 ms (v17, Python 3.13), about 19× faster than the early prototype, so slow machines are not held back by code generation.
- **Automatic region tracking**: the receiver first searches the whole screen for codes, then captures only the area that holds them, and rescans the full screen periodically (so it keeps up if the VNC window moves).
- **Multithreaded, tear-tolerant decoding**: screen capture and decoding run as a parallel pipeline (up to 4 decoding threads by default). When a terminal or VNC redraw is only half done, zbar returns no code at all for the whole image, so the receiver cuts the image into bands at the blank rows between code rows and reads each band separately; a tear only costs the row of codes it hits.
- **Safe extraction**: absolute paths, `..` and device files are rejected; existing files are never overwritten (a name clash becomes `name (1).ext`).

## Installation

Copy `qrxfer.py` to both machines (it is a single file). Alternatively, run the install script, which copies it to `~/.local/bin/qrxfer` and runs the self-test:

```sh
./install.sh                          # install to ~/.local/bin/qrxfer
QRXFER_BIN_DIR=/opt/bin ./install.sh  # install to another directory
```

| Role | Needs | Debian/Ubuntu install |
|---|---|---|
| Sender (remote) | Python ≥ 3.6; optionally tkinter | `sudo apt install python3-tk` (without it, use `--terminal`) |
| Receiver (local) | numpy, libzbar0 or zxing-cpp, an X11 session | `sudo apt install python3-numpy libzbar0`; optionally `python3-zxing-cpp` |

`qrxfer selftest` checks what this machine can do and runs the whole pipeline once offline (pack → encrypt → QR encode → QR read → fountain decode → decrypt → unpack).

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

   Enter the same passphrase twice; the screen then shows a full-screen grid of QR codes.

3. When the local progress bar reaches 100 %, the receiver decrypts, unpacks and lists the saved files. Then press `q` in the remote QR window to quit.

Either side may start first.

### Sender window keys

| Key | Action |
|---|---|
| `+` / `-` | Faster / slower (×1.25 per press) |
| Space | Pause / resume |
| `f` | Toggle full screen (Tk window) |
| `q` / `Esc` / Ctrl+C | Quit |

## Agent skill: download with four arguments

The repository ships an agent skill whose text lives in `skills/qrxfer/SKILL.md`. Once installed, type this in Claude Code:

```text
/qrxfer <server> <password> <source path> <dest path>
```

Quote arguments that contain spaces. A single colon in the server is a VNC display number, e.g. `192.0.2.10:69` means port 5969; write an explicit port as `192.0.2.10::5969`. The source is an absolute path on the remote machine and the destination is the local parent directory: a source directory `project` is saved as `project` under dest, and an existing item with the same name makes the download land under a new name with a numeric suffix. The password is the VNC login password; the transfer itself uses a separately generated random passphrase, and the skill stores no credentials.

The skill reuses or opens a VNC session, receives the files through QR codes, verifies SHA-256 digests, the directory structure and symlinks item by item, and saves the result only after verification passes. The remote side needs no Tk; the skill uses the terminal QR mode. An external symlink is kept as a link; its target is not copied. The local helper's install step needs Python ≥ 3.12; packing on the remote side still needs only the Python ≥ 3.6 standard library.

To install it, symlink this repository directory into your personal skill directories, so both clients share one copy of the text:

```sh
ln -s "$PWD/skills/qrxfer" ~/.claude/skills/qrxfer   # Claude Code
ln -s "$PWD/skills/qrxfer" ~/.agents/skills/qrxfer   # Codex
```

Claude Code uses the directory name as the command name ([documentation](https://code.claude.com/docs/en/skills#how-a-skill-gets-its-command-name)); in Codex, use `$qrxfer` with the same four arguments, or ask explicitly for the skill ([skill documentation](https://learn.chatgpt.com/docs/build-skills)). If a new session does not list the entry yet, reopen the session.

This is a skill invocation run by an agent, not a shell command. It requires the local machine to have an X11 desktop and to be able to reach the VNC server.

## Command reference

```
qrxfer send FILE_OR_DIR...  [-p FILE] [--fps 12] [--density auto|low|medium|high|BYTES]
                            [--grid auto|COLSxROWS] [--max-codes 24] [--min-px 4]
                            [--ecc L|M|Q|H] [--terminal] [--windowed]
qrxfer recv    [-o DIR] [-p FILE] [--decoder auto|zxing|zbar] [--region X,Y,W,H] [--timeout SECONDS]
               [--threads N]
qrxfer decrypt FILE.qxf [-o DIR] [-p FILE]
qrxfer genpass
qrxfer selftest
```

- `-p/--passphrase-file FILE`: read the passphrase from a file instead of prompting; the `QRXFER_PASSPHRASE` environment variable also works.
- `--fps`: screens of codes per second, 12 by default. The status line shows actual/target fps; when the actual value falls behind the target, the sender's CPU or the terminal has reached its limit.
- `--density`: data carried by each QR code. The default `auto` picks, when sending starts, the code size (versions 10–17) that puts the most data on one screen for the display area. The code size stays fixed for the whole transfer, so in terminal mode **set the window size and font size before starting**. It can also be fixed as `low` = 300 B, `medium` = 600 B (version 17-L), `high` = 1000 B, or a byte count.
- `--grid`: layout of codes on each screen. The default `auto` fits as many codes as possible (at most `--max-codes`) while keeping modules at least `--min-px` pixels. `COLSxROWS` fixes the layout; for example `--grid 6x2` means 6 on top and 6 below. With `--density auto`, the largest code that fits that layout is chosen.
- `--region`: only watch one part of the screen (rarely needed; the codes are located automatically).
- `--threads N`: number of captures decoded at the same time; by default the CPU count minus 1, at most 4.
- When decryption fails (wrong passphrase) you get 3 retries; after that the encrypted data is saved as `qrxfer-YYYYMMDD-HHMMSS.qxf`, so you can retry later with `qrxfer decrypt` without sending again.

## Speed and tuning

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
- Screen capture on the receiver needs X11 (XFCE, MATE and other X11 sessions all work); in a pure Wayland session the window contents may not be capturable.
- Suited to files from KB to tens of MB; larger payloads take proportionally longer (see the table above).

## Development and testing

```sh
python3 -m unittest discover -s tests   # unit tests + receive pipeline tests (cases needing numpy or a decoder are skipped when absent)
python3 qrxfer.py selftest              # environment check + offline end-to-end test
```

When `segno` is installed, the tests also compare the built-in QR encoder's output with segno bit for bit.

## Files

| File | Description |
|---|---|
| `qrxfer.py` | The program (a single file that can be copied to any machine) |
| `install.sh` | Installs to `~/.local/bin` and runs the self-test |
| `tests/test_qrxfer.py` | Tests |
| `skills/qrxfer/SKILL.md` | VNC QR download skill: the four-argument convention and the procedure |
| `skills/qrxfer/scripts/transfer.py` | Run preparation, targeted keyboard input, bundle manifest, verified install |
| `tests/test_skill_transfer.py` | Offline workflow and failure-path tests for the skill |
| `LICENSE` | MIT license |

## License

MIT, see [LICENSE](LICENSE).
