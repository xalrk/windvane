#!/usr/bin/env bash
# The test suite on every supported Python found on PATH: the local stand-in for a hosted CI matrix, whose minutes are
# limited. Most of a run is waiting on subprocesses, so the versions run side by side and the
# whole check takes about as long as one. Each run gets its version as `python3` too, as CI's setup-python does, so
# the `windvane` and tools the tests start run on it. Bash and the Pythons only: nothing to install.
# Usage: tools/check_pythons.sh [unittest args]   (default: discover -s tests -q)
cd "$(dirname "$0")/.." || exit 2
[ $# -gt 0 ] || set -- discover -s tests -q
VERSIONS="3.11 3.12 3.13 3.14"
tmp=$(mktemp -d) || exit 2
trap 'rm -rf "$tmp"' EXIT
trap 'kill $(jobs -p) 2>/dev/null; exit 130' INT TERM

ran="" skipped="" pids=""
for v in $VERSIONS; do
    exe=$(command -v "python$v") || { skipped="$skipped $v"; continue; }
    mkdir "$tmp/bin$v" && ln -s "$exe" "$tmp/bin$v/python3"
    PATH="$tmp/bin$v:$PATH" "$exe" -m unittest "$@" >"$tmp/log$v" 2>&1 &
    ran="$ran $v" pids="$pids $!"
done
[ -n "$ran" ] || { echo "no supported Python on PATH (looked for$(printf ' python%s' $VERSIONS))"; exit 1; }

set -- $pids
failed=""
for v in $ran; do
    wait "$1" || failed="$failed $v"
    shift
    summary=$(grep -E '^(Ran |OK|FAILED)' "$tmp/log$v" | tr '\n' ' ')
    summary=${summary% }
    echo "python$v: ${summary:-no summary (it crashed?)}"
done
for v in $failed; do
    echo "--- python$v, the end of its output:"
    tail -n 40 "$tmp/log$v"
done
[ -z "$skipped" ] || echo "skipped (not on PATH):$(printf ' python%s' $skipped)"
if [ -n "$failed" ]; then
    echo "FAILED on:$(printf ' python%s' $failed)"
    exit 1
fi
echo "passed on:$(printf ' python%s' $ran)"
