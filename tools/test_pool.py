"""Run a test directory's modules side by side: K workers each take the next `test_*.py` off a shared list, longest
first when an earlier run's timings are known, each in its own unittest process with its own TMPDIR. Most of a test
run is waiting on subprocesses and sleeps, not CPU, so this cuts the wall time to about that of the slowest modules.

Prints one unittest-style summary for the lot (`Ran N tests in Xs`, then `OK` or `FAILED (...)` with the counts
summed), then `slowest: ...` with the five slowest modules; the failed modules' output tails go to --tails.
tools/check_pythons.sh runs it once per Python, under that Python. Standard library only.
Usage: test_pool.py --jobs K --tails FILE [--times FILE] [DIR]   (DIR defaults to tests)
"""
import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

TAIL = 40  # lines of a failed module's output, as the unsharded check shows of the whole run
SHOWN = 5  # failed modules whose tails are shown; the rest are named, so a broken import can't flood the log
KEYS = ("failures", "errors", "skipped", "expected failures", "unexpected successes")  # unittest's order

running = {}  # module -> Popen, so a signal can stop every module's process group
lock = threading.Lock()


def order(modules, times):
    """Longest first, so a slow module doesn't start last and set the wall time alone; unknown ones by name after."""
    return sorted(modules, key=lambda m: (-times.get(m, -1), m))


def parse(out):
    """(tests run, counts, passed) from a unittest run's output; None for tests run when it printed no summary."""
    ran = re.findall(r"^Ran (\d+) tests? in ", out, re.M)
    end = re.findall(r"^(OK|FAILED)(?: \((.*)\))?$", out, re.M)
    if not ran or not end:
        return None, {}, False
    word, detail = end[-1]
    counts = {}
    for part in filter(None, (detail or "").split(", ")):
        key, _, n = part.rpartition("=")
        if key in KEYS and n.isdigit():
            counts[key] = int(n)
    return int(ran[-1]), counts, word == "OK"


def run_module(module, tests, logdir, tmpbase):
    """Runs one module as `discover -s tests` would (the tests dir on sys.path, so `import helpers` works)."""
    tmp = tmpbase / module
    tmp.mkdir(parents=True, exist_ok=True)
    log = logdir / f"{module}.log"
    start = time.monotonic()
    with open(log, "wb") as f:
        proc = subprocess.Popen([sys.executable, "-m", "unittest", "discover", "-s", str(tests), "-p", f"{module}.py",
                                 "-q"], stdout=f, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                env={**os.environ, "TMPDIR": str(tmp)}, start_new_session=True)
        with lock:
            running[module] = proc
        code = proc.wait()
        with lock:
            running.pop(module, None)
    shutil.rmtree(tmp, ignore_errors=True)
    return time.monotonic() - start, code


def stop(signum, _frame):
    """Takes every module's process group down with the pool, so nothing it started outlives the check."""
    with lock:
        for proc in running.values():
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except OSError:
                pass
    sys.exit(128 + signum)


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("--jobs", type=int, default=2)
    p.add_argument("--tails", type=Path, required=True)
    p.add_argument("--times", type=Path)
    p.add_argument("dir", nargs="?", default="tests")
    a = p.parse_args(argv)
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    tests = Path(a.dir).resolve()
    times = {}
    if a.times:
        try:
            times = {k: float(v) for k, v in json.loads(a.times.read_text()).items()}
        except (OSError, ValueError, AttributeError, TypeError):
            pass
    todo = order([f.stem for f in tests.glob("test_*.py")], times)
    tmpbase = Path(os.environ.get("TMPDIR") or "/tmp")
    logdir = tmpbase / "logs"
    logdir.mkdir(parents=True, exist_ok=True)
    results, queue = {}, list(todo)
    start = time.monotonic()

    def worker():
        while True:
            with lock:
                if not queue:
                    return
                module = queue.pop(0)
            results[module] = run_module(module, tests, logdir, tmpbase)

    threads = [threading.Thread(target=worker, daemon=True) for _ in range(max(1, min(a.jobs, len(todo))))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    wall = time.monotonic() - start

    total, counts, failed = 0, dict.fromkeys(KEYS, 0), []
    for module in todo:
        out = (logdir / f"{module}.log").read_text(errors="replace")
        ran, c, ok = parse(out)
        if ran is None:  # killed or crashed before unittest's summary: count it as an error
            c = {"errors": 1}
        total += ran or 0
        for k, n in c.items():
            counts[k] += n
        if not ok or results[module][1] != 0:
            failed.append((module, out))
    with open(a.tails, "w") as f:
        for module, out in failed[:SHOWN]:
            f.write(f"--- {module}:\n" + "".join(out.splitlines(True)[-TAIL:]))
        if len(failed) > SHOWN:
            f.write(f"--- and {len(failed) - SHOWN} more failed: {', '.join(m for m, _ in failed[SHOWN:])}\n")
    detail = ", ".join(f"{k}={counts[k]}" for k in KEYS if counts[k])
    word = "FAILED" if failed else "OK"
    print(f"Ran {total} test{'' if total == 1 else 's'} in {wall:.3f}s\n\n{word}{f' ({detail})' if detail else ''}")
    slow = sorted(results, key=lambda m: -results[m][0])[:5]
    print("slowest: " + ", ".join(f"{m} {results[m][0]:.0f}s" for m in slow))
    if a.times:
        try:
            a.times.parent.mkdir(parents=True, exist_ok=True)
            new = a.times.with_name(f"{a.times.name}.{os.getpid()}")
            new.write_text(json.dumps({m: round(results[m][0], 1) for m in results}, indent=0, sort_keys=True))
            os.replace(new, a.times)
        except OSError:
            pass
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
