#!/usr/bin/env bash
# Install qrxfer for the current user, then report what this machine can do.
#   ./install.sh                      -> ~/.local/bin/qrxfer
#   QRXFER_BIN_DIR=/some/dir ./install.sh
set -euo pipefail

src="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/qrxfer.py"
bin_dir="${QRXFER_BIN_DIR:-$HOME/.local/bin}"

if ! command -v python3 >/dev/null 2>&1; then
    echo "error: python3 (3.6 or newer) is required" >&2
    exit 1
fi
mkdir -p "$bin_dir"
install -m 755 "$src" "$bin_dir/qrxfer"
echo "Installed $bin_dir/qrxfer"
case ":$PATH:" in
    *":$bin_dir:"*) ;;
    *) echo "Note: $bin_dir is not on PATH; add to ~/.zshrc or ~/.bashrc:  export PATH=\"$bin_dir:\$PATH\"" ;;
esac
echo
python3 "$bin_dir/qrxfer" selftest || true
