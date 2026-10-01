#!/bin/sh
# Install windvane without pipx or uv: a venv of its own under ~/.local/share/windvane and the `windvane` command in
# ~/.local/bin. Run it from a clone (`sh install.sh`) or with WINDVANE_SOURCE set to a pip source. Rerun to update.
set -eu
here=$(cd "$(dirname "$0")" && pwd)
if [ -n "${WINDVANE_SOURCE:-}" ]; then
    src=$WINDVANE_SOURCE
elif [ -f "$here/pyproject.toml" ]; then
    src=$here
else
    src="git+https://github.com/xalrk/windvane"
fi
python3 -c 'import sys; sys.exit(sys.version_info < (3, 11))' || {
    echo "windvane needs Python 3.11 or newer (python3 here: $(python3 --version 2>&1))" >&2
    exit 1
}
venv="${XDG_DATA_HOME:-$HOME/.local/share}/windvane/venv"
bin="$HOME/.local/bin"
python3 -m venv "$venv"
"$venv/bin/python" -m pip install -q --disable-pip-version-check --upgrade "$src"
mkdir -p "$bin"
ln -sf "$venv/bin/windvane" "$bin/windvane"
echo "installed: $bin/windvane (package in $venv)"
case ":$PATH:" in *":$bin:"*) ;; *) echo "add $bin to your PATH to run windvane by name" ;; esac
echo "next: windvane doctor (what this machine still lacks), then windvane install (the skill and the mention hook)"
