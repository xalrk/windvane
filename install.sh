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
python3 -c 'import ensurepip' 2>/dev/null || {
    echo "python3 can't make a venv here: install its venv module (e.g. sudo apt install python3-venv), then rerun" >&2
    exit 1
}
python3 -m venv "$venv"
"$venv/bin/python" -m pip install -q --disable-pip-version-check --upgrade "$src"
mkdir -p "$bin"
ln -sf "$venv/bin/windvane" "$bin/windvane"
echo "installed: $bin/windvane (package in $venv)"
cmd=windvane
case ":$PATH:" in
    *":$bin:"*) ;;
    *)  # the next steps work either way: by full path now, by name once PATH has it
        cmd="$bin/windvane"
        echo "$bin isn't on your PATH: to run windvane by name, run this and add it to your shell's startup file:"
        echo '  export PATH="$HOME/.local/bin:$PATH"' ;;
esac
echo "next: $cmd doctor (what this machine still lacks), then $cmd install (the skill and the mention hook)"
