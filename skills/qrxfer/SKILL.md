---
name: qrxfer
description: Download files or folders from a remote Linux VNC desktop using qrxfer's animated colour grid (QR codes as a fallback). Use for /qrxfer server password source-path dest-path (or labelled: script:remote-qrxfer source:path dest:path), or an explicit request to copy from a VNC server through qrxfer.
metadata:
  version: "1.1.1"
---

# qrxfer

Download a file or folder from a remote Linux machine to this machine through an animated colour grid (or QR codes) shown in a VNC session. Once the user has given the arguments, carry out the connection, sending, receiving, verification and saving directly; ask only when required information is missing or a real blocker appears.

## Invocation and arguments

```text
/qrxfer <server> <password> <source path> <dest path>
/qrxfer <server> <password> [script:<remote qrxfer path>] source:<source path> dest:<dest path>
```

- Split the arguments by shell quoting rules, but never hand the argument string to a shell for execution; paths or passwords containing spaces are quoted.
- The first two arguments are always server and password, taken as they are, even when they contain a colon or look like a label.
- After them, an argument starting with `script:`, `source:` or `dest:` names that value. The value follows the colon directly, or is the next argument when nothing follows the colon, so `dest: /path` equals `dest:/path`. Arguments without a label fill source and then dest; script is only accepted with its label. A repeated label, a missing source or dest, or an extra argument is missing information: ask instead of guessing.
- script is optional: the absolute path of a qrxfer copy already on the remote machine (`qrxfer.py`, or a copy installed as `qrxfer`). With it, only the small helper is typed in through VNC instead of qrxfer as well (about 12 instead of 79 thousand characters), and the remote copy is read and run, never changed or deleted. Without it, this project's qrxfer is installed in the run's temporary remote directory.
- server is the VNC address: host defaults to port 5900; host:69 is display 69, port 5969; host::5969 is an explicit port. IPv4 addresses and host names are supported. The password is used only for VNC authentication; the qrxfer transfer uses a separate passphrase generated at random for this run.
- source path is an absolute path on the remote machine; dest path is the local parent directory that receives it. For example, downloading the source directory project to ~/Downloads gives ~/Downloads/project. When an item with the same name exists, the download is saved as a new item with a numeric suffix and the existing item stays intact.
- Copy only the given source; symlinks keep their link text and are not followed to outside directories. The original files, remote repositories and system configuration are not changed by the transfer.
- The local machine needs X11, Python ≥ 3.12, numpy, the TigerVNC viewer and xdotool, plus libzbar or zxing-cpp for the QR fallback; the remote machine needs only Python ≥ 3.6 and libX11 (any X11 desktop has it): the grid is drawn through ctypes, so no Tk is needed.

Below, `SKILL_DIR` is the real path (realpath, symlinks resolved) of the directory holding this SKILL.md, and `REPO` is the repository root two levels above it. helper means `SKILL_DIR/scripts/transfer.py`; qrxfer means `REPO/qrxfer.py`. Replace both with actual absolute paths before running commands.

## Preparation and connection

1. Check the local dependencies, the current contents of the destination directory and the free space. Run qrxfer selftest if needed; do not change its protocol or extraction safety rules for this transfer.
2. Run helper prepare with server, source and dest, and --script only when the user gave script (**never pass the VNC password**). It prints this run's work, remote_work, viewer_server and qrxfer_sha256 (this project's qrxfer), and generates a random transfer passphrase and the command files: bootstrap, bundle, send (colour grid), send_qr (terminal QR fallback), quit (the key q) and cleanup. Record these exact paths; never pick another run by "newest directory".

   ```text
   python3 SKILL_DIR/scripts/transfer.py prepare --server SERVER --source SOURCE --dest DEST [--script SCRIPT]
   ```

3. Prefer reusing an existing VNC session to the target server: confirm it from process arguments, the connected address/port in ss, the window title and screenshots together. A user name in the title alone is not enough to identify the server. When a new connection is needed, start vncviewer with the viewer_server returned by prepare, with Shared on, RemoteResize off and a lossless or high-quality picture; record the PID of anything you start. The grid covers the whole remote screen, so the viewer must show all of it, preferably unscaled; a remote desktop larger than the viewer window, with scroll bars, cuts the grid. Do not disconnect other sessions or change the remote desktop size.
4. Complete VNC authentication with the password the user gave. If already logged in, no authentication is needed. Never write the password into the skill, README, logs, command-line arguments or a repository; when file input is required, use a 0600 temporary file inside this run's work and delete it after authentication. If the connection fails, check the address and authentication method against the actual output; do not silently turn a single-colon display number into a raw port, and do not resubmit credentials that were rejected.
5. Save screenshots in `~/Pictures/agents/` and inspect them in chronological order. Determine the VNC window ID, its visible area and the remote state. Open a terminal on the remote desktop dedicated to this run, and verify the host, the Python version, and that the given source exists and its size; never type shell commands into an existing program whose state you cannot confirm.

## Remote preparation and sending

helper type sends one line of text from a file to **the verified VNC window ID**. It focuses that window and types with xdotool's explicit window argument: xdotool uses real (XTEST) key events only while that window has the focus, and VNC viewers ignore the synthetic events it falls back to otherwise. So if local focus moves away mid-way, the rest of the line is dropped instead of landing in another application; always check the result on the remote screen. Neither password files nor command files may contain a newline; add --enter to submit.

```text
python3 SKILL_DIR/scripts/transfer.py type --window WINDOW_ID --file INPUT_FILE --enter
```

1. Once the remote shell is confirmed, turn terminal echo off for the moment and send bootstrap.txt from work. It compresses the helper, and the current project's qrxfer unless script was given, places them in the unique remote_work through the VNC keyboard and checks their SHA-256. Confirm that the screen shows QRXFER_READY with this run's path, then turn echo back on. The clipboard may be tried first for short commands; if the result is not seen on screen, use the targeted keyboard input above, and never treat a successful paste as fact.
   - With script, the bootstrap first reads that file without running it and prints `QRXFER_SCRIPT version=… grid=yes|no sha256=…`; a wrong path stops it with an error before it creates remote_work.
   - The same sha256 as qrxfer_sha256 means the same qrxfer as this project. Otherwise go by grid: yes → send.txt; no means a qrxfer from before the colour grid → send_qr.txt (QR codes in the terminal, see step 4).
   - If the script is missing, unreadable or not qrxfer (version ?), tell the user and continue with a fresh prepare without --script, which installs this project's qrxfer. If the abandoned run's bootstrap printed QRXFER_READY, send that run's cleanup.txt first; from then on use only the new run's paths.
2. Send bundle.txt, wait for the command to exit, and check the file count, total bytes, compressed size, manifest_sha256 and the symlink summary. Keep the manifest_sha256 from the screenshot to check against the manifest once received. Permission errors, a source that changes while being read, or special files are real failures; after fixing the cause, bundle again into a fresh remote temporary directory, and never skip files and then claim a complete success.
3. bundle packs the source into payload.tar.xz and writes manifest.json. qrxfer then sends these two regular files; this is how external symlinks are preserved. The receiver still uses qrxfer's own safe extraction, without loosening its tar filter.
4. send.txt shows the colour grid: a full-screen window of 3 px cells in 8 colours, with a status line at the bottom.
   - Nothing needs adjusting first: the grid sizes itself to the remote screen at start. Do not change the remote resolution or the viewer scaling while it runs.
   - The grid window grabs the remote keyboard, so keys typed into the VNC viewer go to it: q or Esc stops it, space pauses. Type commands into the remote terminal only after it has stopped.
   - send.txt uses --fps 20; the sender usually reaches fewer. Watch the actual/target fps on the status line and the local receive rate, and adjust only according to the actual decode rate.
   - If the remote has no usable X11 display (the sender reports an error), or the receiver finds no grid although the whole remote screen is visible, stop the sender and use send_qr.txt instead: QR codes in the terminal, about 30 times slower. Before starting it, maximize the terminal and shrink its font so each module is about 4–5 pixels (character cells of about 5×10 pixels); --density auto chooses the code size from the terminal size at start. Record every font zoom step and undo them in reverse when done; never hard-code the zoom count from some earlier window.
5. Start qrxfer recv locally: output to received under this run's work, use passphrase.txt from work, capture the verified visible area of the VNC window, and keep stdout/stderr in this run's recv.log. The visible area comes from this run's window geometry, never from historical coordinates; keep the window visible and uncovered. Set a reasonable timeout, keep watching the receive progress, and report the status of long transfers to the user. The receiver detects the grid or QR codes by itself.
6. Type send.txt (or send_qr.txt) on the remote side. When the Passphrase prompt appears, type this run's passphrase.txt with helper type; type it again only **after the Repeat passphrase prompt appears**. getpass flushes keystrokes sent in advance, so the passphrase cannot be typed twice in one go. The transfer passphrase is not echoed; do not capture or print the passphrase file.
7. Wait until the local receiver exits with 0 and shows that both files were saved. Once recv.log shows `All … blocks arrived`, the sender is no longer needed; reassembling and decrypting hundreds of MB can take a few more minutes. If progress stalls, check first for a screensaver on either side (screenshot the VNC window; check that keepawake is still running), covered windows, a cut or scaled remote screen, the code size, VNC screen updates and the decoding log; the fountain code makes up for lost frames, so there is no need to start over at once. If a script whose sha256 differs from qrxfer_sha256 shows codes but the receiver never decodes a frame, its format may be incompatible: stop it and fall back as in step 1. On interruption or decryption failure, keep the raw receive log and the recoverable encrypted file, and never deliver a partial result as the complete directory.

The local receive command looks like this; WORK and the region coordinates must be replaced with this run's actual values, and the timeout adjusted to the compressed size and receive rate for long transfers. The timeout counts from the start of recv, so it also covers the time the sender spends compressing and encrypting before the grid appears; for hundreds of MB, allow at least 5 minutes per 100 MB of compressed size on top of that:

```text
python3 REPO/qrxfer.py recv -o WORK/received -p WORK/passphrase.txt --region X,Y,W,H --timeout 900 > WORK/recv.log 2>&1
```

Right after starting recv, start helper keepawake in the background with the verified VNC window ID and the receiver's PID, and keep its output in this run's keepawake.log. Screensavers on both sides count idle time from the last key typed through the viewer, so in a transfer longer than their timeout (often 10–30 minutes) they cover the grid and the receiver stalls. Every 4 minutes keepawake tells a local xscreensaver to deactivate and moves the pointer into the corner of the VNC window and back; the viewer forwards that motion to the remote. It never types keys, because q stops the grid sender, and it exits by itself once the receiver has exited:

```text
python3 SKILL_DIR/scripts/transfer.py keepawake --window WINDOW_ID --pid RECV_PID > WORK/keepawake.log 2>&1
```

## Verification, saving and cleanup

1. Check the SHA-256 of the received manifest.json against the remote bundle digest. Then run helper install:

   ```text
   python3 SKILL_DIR/scripts/transfer.py install --work WORK
   ```

2. install checks the requested source, the payload SHA-256, tar paths, member types, duplicate entries and symlink ancestors; it extracts regular data first, then restores symlinks, and checks directories, file sizes, SHA-256 digests and link texts item by item. The staging directory is created inside the destination parent to avoid cross-filesystem rename failures; after verification passes, it is published with an atomic rename that refuses to overwrite. An existing item with the same name makes it pick a numeric suffix automatically.
3. Report the download as complete only when install exits with 0 and verification.json shows PASS. On failure, keep this run's work and the exact error and fix the actual cause; never move an unverified directory into place to make up a result.
4. Type quit.txt (without --enter) into the VNC window to stop the sender that is still running, and confirm in a screenshot that the grid is gone and the terminal is back at the shell; if it is not, the sender's status line says how to stop it (Ctrl+C in the terminal). Undo any terminal zoom of this run, send cleanup.txt, which deletes only the remote_work created in this run, never the script given with script:. If a dedicated terminal was used, close it after cleanup. Leave a reused VNC session connected; a viewer you started yourself may be closed by its recorded PID. Report cleanup failures separately, and do not call a verified download a failure.
5. A successful install deletes this run's random passphrase file automatically. Delete other temporary authentication files too; on failure, keep the key needed for recovery and tell the user its path without printing its contents. recv.log, manifest.json and verification.json stay in this run's work for later tracing.
6. Finally, report to the user, in their language, the saved path, the file count and the integrity check result; if there are external or dangling symlinks, say that the links were kept but their target files were not copied. Never put numbers or passwords from an old run into generic examples.

## Installation and invocation

The only copy of this text lives in the repository's skills/qrxfer. Personal entry points are symlinks:

- `~/.claude/skills/qrxfer` points to this directory; Claude Code invokes it as `/qrxfer`.
- `~/.agents/skills/qrxfer` points to this directory for Codex to discover; Codex uses `$qrxfer` with the same arguments, or a direct request to use this skill.

Claude Code uses the skill directory name as the command name; see the [Claude Code documentation](https://code.claude.com/docs/en/skills#how-a-skill-gets-its-command-name). For Codex's personal skill discovery directory, see the [OpenAI documentation](https://learn.chatgpt.com/docs/build-skills). If a new session does not show the entry, reopen that client's session and invoke it again.
