#!/usr/bin/env bash
# The test suite on the supported Pythons found on PATH: the local stand-in for a hosted CI matrix, whose minutes are
# limited. By default it runs the whole suite on the oldest one installed, where syntax and APIs newer than our oldest
# supported version fail, and on the newest a pass that byte-compiles and imports every windvane/ and test module, in
# seconds, where a stdlib module or name a newer Python removed fails; both must pass. Failures on only some versions
# of the full matrix were all timing tests missing their windows under its load, so running every version for every
# check bought four times the CPU and little else. `--all` runs the whole suite on every version, side by side, for
# releases and by hand; unittest arguments run on every version too (they're short).
# Within a version tools/test_pool.py runs the test modules side by side, K at a time (longest first by the last run's
# timings, kept in the user's cache dir), so a suite takes about as long as its slowest modules. Each run gets its
# version as `python3` too, as CI's setup-python does, so the `windvane` and tools the tests start run on it, and a
# TMPDIR inside this script's own temp dir, so even a killed or leaky run leaves nothing in /tmp (a windvane-check-*
# dir a SIGKILL leaves, the cleaner removes). Bash and the Pythons only: nothing to install.
# Usage: tools/check_pythons.sh [--all | --quick] [unittest args]   (--all or --quick may come anywhere)
#   (none: every tests/test_*.py pooled on the oldest, imports on the newest; --all: pooled on every version;
#    --quick: on the oldest only, the modules likely to fail for this change, stopping at the first that fails
#    alone too: windvane's `check_quick`; with args: one unittest run per version)
# CHECK_JOBS sets K; by default the CPUs (shared out among the versions with --all), at least 2 each: the tests' own
# subprocesses fill the rest, and more only slows every module down until the ones that time their steps miss their
# windows. For the same reason checks running at once on one machine split K among them (markers in the cache dir;
# `shared:` says so), unless CHECK_JOBS is set. A module that fails beside the others is rerun alone once, and
# `rerun alone:` names it, with the Pythons it was rerun on, what its outcome means and the seconds the reruns added;
# each pooled check adds a line to the cache's reruns.log (its newest 500 kept), so whether reruns get rarer shows
# over weeks.
cd "$(dirname "$0")/.." || exit 2
all="" quick="" args=()
for a in "$@"; do
    case $a in --all) all=1 ;; --quick) quick=1 ;; *) args+=("$a") ;; esac
done
if [ -n "$quick" ] && [ ${#args[@]} -gt 0 ]; then
    echo "--quick picks its own tests: no unittest arguments with it"
    exit 2
fi
[ ${#args[@]} -eq 0 ] || all=1
VERSIONS="3.11 3.12 3.13 3.14"
tmp=$(mktemp -d "${TMPDIR:-/tmp}/windvane-check-XXXXXX") || exit 2
trap 'rm -rf "$tmp"' EXIT
trap 'kill $(jobs -p) 2>/dev/null; exit 130' INT TERM
cache="${XDG_CACHE_HOME:-$HOME/.cache}/windvane-check"

found="" missing=""
for v in $VERSIONS; do
    if command -v "python$v" >/dev/null; then found="$found $v"; else missing="$missing $v"; fi
done
[ -n "$found" ] || { echo "no supported Python on PATH (looked for$(printf ' python%s' $VERSIONS))"; exit 1; }
set -- $found
oldest=$1
for newest; do :; done
# A run is a version, the whole suite on it, a version and `i`, the import pass on it, or a version and `q`, the
# quick pass on it.
if [ -n "$quick" ]; then runs="${oldest}q" suites=""
elif [ -n "$all" ]; then runs=$found suites=$found; else runs="$oldest ${newest}i" suites=$oldest; fi
set -- ${suites:-$oldest}
cpus=$(nproc 2>/dev/null || getconf _NPROCESSORS_ONLN 2>/dev/null || echo 4)
k=${CHECK_JOBS:-$(( cpus / $# ))}
[ "$k" -ge 2 ] 2>/dev/null || k=2
py=$(command -v "python$oldest")

name() {
    case $1 in *i) echo "python${1%i} imports" ;; *q) echo "python${1%q} quick pass" ;; *) echo "python$1" ;; esac
}
what() {
    case $1 in *i) echo "imports on python${1%i}" ;; *q) echo "the quick pass on python${1%q}" ;;
        *) echo "the suite on python$1" ;; esac
}

pids=""
for r in $runs; do
    v=${r%[iq]}
    exe=$(command -v "python$v")
    mkdir "$tmp/bin$r" "$tmp/tmp$r" && ln -s "$exe" "$tmp/bin$r/python3"
    if [ "$r" = "${v}i" ]; then
        cmd=(tools/test_pool.py --imports windvane tests)
    elif [ "$r" = "${v}q" ]; then  # the reruns log and the times are read, not written: they're the full check's
        cmd=(tools/test_pool.py --quick --jobs "$k" --tails "$tmp/tails$r" --times "$cache/times$v.json"
             --reruns "$cache/reruns.log" tests)
        [ -n "$CHECK_JOBS" ] || cmd+=(--share "$cache/running" --check "${tmp##*/}")
    elif [ ${#args[@]} -eq 0 ]; then
        cmd=(tools/test_pool.py --jobs "$k" --tails "$tmp/tails$r" --times "$cache/times$v.json" tests)
        [ -n "$CHECK_JOBS" ] || cmd+=(--share "$cache/running" --check "${tmp##*/}")
    else
        cmd=(-m unittest "${args[@]}")
    fi
    TMPDIR="$tmp/tmp$r" PATH="$tmp/bin$r:$PATH" "$exe" "${cmd[@]}" >"$tmp/log$r" 2>&1 &
    pids="$pids $!"
done

set -- $pids
failed=""
for r in $runs; do
    wait "$1" || failed="$failed $r"
    shift
    summary=$(grep -E '^(Ran |OK|FAILED)' "$tmp/log$r" | tr '\n' ' ')
    summary=${summary% }
    echo "$(name "$r"): ${summary:-no summary (it crashed?)}"
done
for v in $suites; do  # the first version's five slowest modules: what to speed up next
    slowest=$(grep -m1 '^slowest: ' "$tmp/log$v") && { echo "${slowest/slowest:/slowest on python$v:}"; break; }
done
for v in $suites; do
    grep -m1 '^shared: ' "$tmp/log$v" && break
done
if [ -n "$quick" ]; then  # what it picked and why, its reruns, and where it stopped
    grep -E '^(quick|rerun alone|shared): ' "$tmp/log$runs"
    grep -m1 '^slowest: ' "$tmp/log$runs"
elif [ ${#args[@]} -eq 0 ]; then  # the versions' reruns, as one line, and the check's line in reruns.log
    "$py" tools/test_pool.py --fold $(for v in $suites; do echo "$v=$tmp/log$v"; done) \
        --log "$cache/reruns.log" --check "${tmp##*/}"
fi
for r in $failed; do
    echo "--- $(name "$r"), the end of its output:"
    if [ -s "$tmp/tails$r" ]; then cat "$tmp/tails$r"; else tail -n 40 "$tmp/log$r"; fi
done
[ -z "$missing" ] || echo "not installed:$(printf ' python%s' $missing)"
if [ -n "$all" ]; then
    [ -z "$failed" ] || { echo "FAILED on:$(printf ' python%s' $failed)"; exit 1; }
    echo "passed on:$(printf ' python%s' $runs)"
    exit 0
fi
list() { local out="" r; for r in "$@"; do out="$out, $(what "$r")"; done; echo "${out#, }"; }
[ -z "$failed" ] || { echo "FAILED: $(list $failed)"; exit 1; }
echo "passed: $(list $runs)"
