"""Run a test directory's modules side by side: K workers each take the next `test_*.py` off a shared list, longest
first when an earlier run's timings are known, each in its own unittest process with its own TMPDIR. Most of a test
run is waiting on subprocesses and sleeps, not CPU, so this cuts the wall time to about that of the slowest modules.
A module that fails beside the others is run once more alone after the pool, since a test that times its own steps
can miss its window on a busy machine; it counts by that second run, and the `rerun alone:` line names it and the
seconds the reruns added (they run one after another). With --share, pools of other checks running on the machine at
the same time are counted, and K is split among the checks (at least 2 each), as modules start.

Prints one unittest-style summary for the lot (`Ran N tests in Xs`, then `OK` or `FAILED (...)` with the counts
summed), then `slowest: ...` with the five slowest modules; the failed modules' output tails go to --tails.
tools/check_pythons.sh runs it once per Python, under that Python, then `--fold` to merge the versions' rerun lines.
Standard library only.
Usage: test_pool.py --jobs K --tails FILE [--times FILE] [--share DIR --check ID] [DIR]   (DIR defaults to tests)
       test_pool.py --fold VERSION=LOG...
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

try:
    import fcntl
except ImportError:  # no flock (Windows): every check keeps the jobs it was given
    fcntl = None

TAIL = 40  # lines of a failed module's output, as the unsharded check shows of the whole run
SHOWN = 5  # failed modules whose tails are shown; the rest are named, so a broken import can't flood the log
RERUN = 10  # more failed modules than this is a real breakage, not a busy machine: no reruns, they'd only add time
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


def run_module(module, n, tests, log, tmpbase):
    """Runs one module as `discover -s tests` would (the tests dir on sys.path, so `import helpers` works). Its
    TMPDIR is numbered, not named after it: a test may look for a word in its paths that a module name holds."""
    tmp = tmpbase / str(n)
    tmp.mkdir(parents=True, exist_ok=True)
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


def held(path):
    """Whether a live pool holds this marker's lock. One nobody holds is a check that was killed: it doesn't count, and
    whoever finds it first removes it."""
    try:
        with open(path) as f:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                return True
            path.unlink(missing_ok=True)
    except OSError:
        pass
    return False


class Share:
    """The checks running on this machine, so two at once split the CPUs instead of each taking them all and pushing
    the tests that time their own steps past their windows. Each pool holds a shared lock on a marker named after its
    check (a check runs one pool per Python). Locks, not pids: checks in separate sandboxes can't always see each
    other's pids, but they see the same cache dir, and a killed check's lock goes with it."""

    def __init__(self, folder, check, jobs):
        self.folder, self.jobs, self.least, self.most = folder, jobs, jobs, 1
        folder.mkdir(parents=True, exist_ok=True)
        self.name = f"{check}.{os.getpid()}"
        new = folder / f".{self.name}"
        self.mine = open(new, "w")
        fcntl.flock(self.mine, fcntl.LOCK_SH)
        os.replace(new, folder / self.name)  # visible only once locked, so no one takes it for a dead check

    def checks(self):
        live = {f.name.rpartition(".")[0] for f in self.folder.iterdir()
                if not f.name.startswith(".") and (f.name == self.name or held(f))}
        return max(1, len(live))

    def jobs_now(self):
        """K divided among the running checks, at least 2: asked before each module starts, so a check that began
        alone gives up workers when another starts, and takes them back when it ends."""
        n = self.checks()
        k = min(self.jobs, max(2, self.jobs // n))
        with lock:
            self.least, self.most = min(self.least, k), max(self.most, n)
        return k

    def close(self):
        (self.folder / self.name).unlink(missing_ok=True)
        self.mine.close()


def fold(logs):
    """One `rerun alone:` line for every version's pool: each module and outcome once, with the versions it happened
    on, and the most seconds any version's reruns added. The versions run side by side, so that's about what the
    check's wall time grew by."""
    seen, added = {}, 0
    for version, log in logs:
        try:
            line = next((l for l in Path(log).read_text(errors="replace").splitlines()
                         if l.startswith("rerun alone: ")), None)
        except OSError:
            line = None
        if not line:
            continue
        reruns, _, secs = line[len("rerun alone: "):].partition("; added ")
        secs = secs.removesuffix("s")
        added = max(added, int(secs) if secs.isdigit() else 0)
        for item in filter(None, reruns.split(", ")):
            seen.setdefault(item, []).append(version)
    if not seen:
        return None
    return ("rerun alone: " + ", ".join(f"{item} ({', '.join(vs)})" for item, vs in seen.items())
            + f"; added {added}s")


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
    p.add_argument("--tails", type=Path)
    p.add_argument("--times", type=Path)
    p.add_argument("--share", type=Path, help="the folder where running checks leave their markers")
    p.add_argument("--check", default=str(os.getpid()), help="this check's id: one per check, not per Python")
    p.add_argument("--fold", nargs="+", metavar="VERSION=LOG", help="print one rerun line for these pools' logs")
    p.add_argument("dir", nargs="?", default="tests")
    a = p.parse_args(argv)
    if a.fold:
        line = fold(arg.partition("=")[::2] for arg in a.fold)
        if line:
            print(line)
        return 0
    if not a.tails:
        p.error("--tails is required")
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
    share = Share(a.share, a.check, a.jobs) if a.share and fcntl else None
    try:
        return pool(a, tests, todo, logdir, tmpbase, share)
    finally:
        if share:
            share.close()


def pool(a, tests, todo, logdir, tmpbase, share):
    """Runs the modules K at a time, reruns the ones that failed alone, and prints the summary; the exit code."""
    results, queue = {}, list(todo)
    start = time.monotonic()

    def worker(i):
        while True:
            while share and queue and i >= share.jobs_now():  # another check is running: leave it its share
                time.sleep(1)
            with lock:
                if not queue:
                    return
                module = queue.pop(0)
                n = len(todo) - len(queue)
            results[module] = run_module(module, n, tests, logdir / f"{module}.log", tmpbase)

    threads = [threading.Thread(target=worker, args=(i,), daemon=True)
               for i in range(max(1, min(a.jobs, len(todo))))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    outs = {m: (logdir / f"{m}.log").read_text(errors="replace") for m in todo}
    first = [m for m in todo if not parse(outs[m])[2] or results[m][1] != 0]
    reruns, alone = [], time.monotonic()
    for module in first if len(first) <= RERUN else []:
        log = logdir / f"{module}.rerun.log"
        code = run_module(module, len(todo) + 1, tests, log, tmpbase)[1]
        outs[module] = log.read_text(errors="replace")
        results[module] = results[module][0], code
        reruns.append(f"{module} {'passed' if parse(outs[module])[2] and code == 0 else 'failed again'}")
    wall = time.monotonic() - start

    total, counts, failed = 0, dict.fromkeys(KEYS, 0), []
    for module in todo:
        out = outs[module]
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
    if reruns:
        print(f"rerun alone: {', '.join(reruns)}; added {time.monotonic() - alone:.0f}s")
    if share and share.least < a.jobs:
        print(f"shared: {share.least} of {a.jobs} jobs at once while {share.most} checks ran")
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
