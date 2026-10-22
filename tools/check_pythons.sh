#!/usr/bin/env bash
# The test suite on every supported Python found on PATH: the local stand-in for a hosted CI matrix, whose minutes are
# limited. Most of a run is waiting on subprocesses, so the versions run side by side, and within each version
# tools/test_pool.py runs the test modules side by side too, K at a time (longest first by the last run's timings, kept
# in the user's cache dir), so the whole check takes about as long as its slowest modules. Each run gets its version as
# `python3` too, as CI's setup-python does, so the `windvane` and tools the tests start run on it, and a TMPDIR inside
# this script's own temp dir, so even a killed or leaky run leaves nothing in /tmp (a windvane-check-* dir a SIGKILL
# leaves, the cleaner removes). Bash and the Pythons only: nothing to install.
# Usage: tools/check_pythons.sh [unittest args]   (none: every tests/test_*.py, pooled; with args: one unittest run)
# CHECK_JOBS sets K; by default the CPUs shared out among the versions, at least 2 each: the tests' own subprocesses
# fill the rest, and more only slows every module down until the ones that time their steps miss their windows. For the
# same reason checks running at once on one machine split K among them (markers in the cache dir; `shared:` says so),
# unless CHECK_JOBS is set. A module that fails beside the others is rerun alone once, and `rerun alone:` names it,
# with the Pythons it was rerun on, what its outcome means and the seconds the reruns added; each pooled check adds a
# line to the cache's reruns.log (its newest 500 kept), so whether reruns get rarer shows over weeks.
cd "$(dirname "$0")/.." || exit 2
args=("$@")
VERSIONS="3.11 3.12 3.13 3.14"
tmp=$(mktemp -d "${TMPDIR:-/tmp}/windvane-check-XXXXXX") || exit 2
trap 'rm -rf "$tmp"' EXIT
trap 'kill $(jobs -p) 2>/dev/null; exit 130' INT TERM
cache="${XDG_CACHE_HOME:-$HOME/.cache}/windvane-check"

ran="" missing="" exes="" py=""
for v in $VERSIONS; do
    exe=$(command -v "python$v") || { missing="$missing $v"; continue; }
    ran="$ran $v" exes="$exes $exe" py=${py:-$exe}
done
[ -n "$ran" ] || { echo "no supported Python on PATH (looked for$(printf ' python%s' $VERSIONS))"; exit 1; }
set -- $ran
n=$#
cpus=$(nproc 2>/dev/null || getconf _NPROCESSORS_ONLN 2>/dev/null || echo 4)
k=${CHECK_JOBS:-$(( cpus / n ))}
[ "$k" -ge 2 ] 2>/dev/null || k=2

pids=""
set -- $exes
for v in $ran; do
    exe=$1
    shift
    mkdir "$tmp/bin$v" "$tmp/tmp$v" && ln -s "$exe" "$tmp/bin$v/python3"
    if [ ${#args[@]} -eq 0 ]; then
        cmd=(tools/test_pool.py --jobs "$k" --tails "$tmp/tails$v" --times "$cache/times$v.json" tests)
        [ -n "$CHECK_JOBS" ] || cmd+=(--share "$cache/running" --check "${tmp##*/}")
    else
        cmd=(-m unittest "${args[@]}")
    fi
    TMPDIR="$tmp/tmp$v" PATH="$tmp/bin$v:$PATH" "$exe" "${cmd[@]}" >"$tmp/log$v" 2>&1 &
    pids="$pids $!"
done

set -- $pids
failed=""
for v in $ran; do
    wait "$1" || failed="$failed $v"
    shift
    summary=$(grep -E '^(Ran |OK|FAILED)' "$tmp/log$v" | tr '\n' ' ')
    summary=${summary% }
    echo "python$v: ${summary:-no summary (it crashed?)}"
done
for v in $ran; do  # the first version's five slowest modules: what to speed up next
    slowest=$(grep -m1 '^slowest: ' "$tmp/log$v") && { echo "${slowest/slowest:/slowest on python$v:}"; break; }
done
for v in $ran; do
    grep -m1 '^shared: ' "$tmp/log$v" && break
done
if [ ${#args[@]} -eq 0 ]; then  # the versions' reruns, as one line, and the check's line in reruns.log
    "$py" tools/test_pool.py --fold $(for v in $ran; do echo "$v=$tmp/log$v"; done) \
        --log "$cache/reruns.log" --check "${tmp##*/}"
fi
for v in $failed; do
    echo "--- python$v, the end of its output:"
    if [ -s "$tmp/tails$v" ]; then cat "$tmp/tails$v"; else tail -n 40 "$tmp/log$v"; fi
done
[ -z "$missing" ] || echo "not installed:$(printf ' python%s' $missing)"
if [ -n "$failed" ]; then
    echo "FAILED on:$(printf ' python%s' $failed)"
    exit 1
fi
echo "passed on:$(printf ' python%s' $ran)"
