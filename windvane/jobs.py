"""The project's check (CI) with a pass cache, and background jobs for commands that outlast a tool call.

A pass is recorded under the git tree it ran on (plus the check command), only when the checkout was clean before
and after, so the driver and the reviewer reuse a worker's green check instead of running it again. Failures are
never recorded.
"""
import fcntl
import hashlib
import itertools
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

from .config import exe
from .runner import NEST, TICKET
from .util import append_jsonl, gout, now, read_jsonl

_LOGS = itertools.count(1)  # a check log's number in this process
# A running check touches its log this often (seconds): `stats` times a killed check by its log's last write, so a
# check that hung without output would otherwise count as nearly free; this bounds the error to one interval.
HEARTBEAT = 60
# How long check logs are kept (seconds), pruned at each check's start, since every check writes its own log: a
# passed check's log is rarely opened once its ticket moves on, so it goes after a few days; any log after a few
# weeks, when no ticket still running needs it. The newest failed or cut-off logs stay whatever their age: the
# last failure is the log someone opens.
KEEP_PASSED = 3 * 86400
KEEP_ANY = 21 * 86400
KEEP_FAILED = 5


def tree_key(p, cwd):
    """The key of a clean checkout's tree for the configured check, or None if it has uncommitted changes."""
    if gout(cwd, "status", "--porcelain", "--untracked-files=normal"):
        return None
    tree = gout(cwd, "rev-parse", "HEAD^{tree}")
    return hashlib.sha1(f"{tree}\n{p.cfg['check']}".encode()).hexdigest() if tree else None


def ledger(p, ticket, row, event="check"):
    """A check run for a ticket (the driver's, or a session's `windvane check`) as a ledger row, so `stats` can say
    what checks cost; one at the desk, for no ticket, isn't counted."""
    ticket = ticket or os.environ.get(TICKET)
    if ticket:
        append_jsonl(p.state / "ledger.jsonl", {"event": event, "ticket": ticket, **row})


def check(p, cwd, quiet=False, ticket=None):
    """Run the project's check in `cwd`. Returns (passed, summary line, log path)."""
    cmd = p.cfg["check"]
    if not cmd:
        return True, "CHECK PASSED (no check configured)", None
    passes = p.state / "passes"
    key = tree_key(p, cwd)
    if key and (passes / key).exists():
        ledger(p, ticket, {"minutes": 0, "passed": True, "reused": True})
        return True, f"CHECK PASSED (reused the pass from {(passes / key).read_text().strip()} for this tree)", None
    logs = p.state / "checks"
    logs.mkdir(parents=True, exist_ok=True)
    prune(p, logs)
    log = logs / log_name()
    code, mins = run_logged(p, ticket, cmd, cwd, log)
    if code == 0:
        if key and tree_key(p, cwd) == key:
            passes.mkdir(parents=True, exist_ok=True)
            (passes / key).write_text(f"{now():%Y-%m-%d %H:%M} ({gout(cwd, 'rev-parse', '--short', 'HEAD')})")
        return True, f"CHECK PASSED in {mins:.1f} min" + ("" if key else " (uncommitted changes: not recorded)"), log
    return False, f"CHECK FAILED (exit {code}) in {mins:.1f} min; log {log}", log


def log_name():
    """A check log's name, sortable by time and its own even for checks started in the same second (parallel slots,
    a session's check beside the driver's): a shared log would be truncated by the second check, and its lock would
    make a killed check look still running."""
    return f"{now():%Y%m%d-%H%M%S}-{os.getpid()}-{next(_LOGS)}.log"


def run_logged(p, ticket, cmd, cwd, log):
    """Run the check into `log`, with a ledger row on each side, so a check that's cut off still counts: a
    `check-start` row first (all a SIGKILL or a killed driver leaves; `stats` calls it cut off), then the end row
    with the same `start`, written on the way out even for an exception or Ctrl-C (`finished: false`, the minutes
    so far). Both rows, because only the start row survives a kill and only the end row knows the minutes. The log
    stays locked while the check runs (the kernel drops the lock on a kill), so `running` tells a check still going
    from one cut off; the start row is written only once it's locked, or `stats` could catch it unlocked and call a
    check that just started cut off. A heartbeat touches the log while the check runs, so a kill is timed within
    `HEARTBEAT` even when the check went quiet."""
    start = time.time()
    code = None
    try:
        with log.open("w") as f:
            fcntl.flock(f, fcntl.LOCK_EX)  # blocking: `running` may hold a momentary shared probe
            ledger(p, ticket, {"start": round(start, 3), "log": log.name}, event="check-start")
            beat = heartbeat(log)
            try:
                if p.cfg["heavy_check"]:  # one machine-heavy job at a time, across slots and projects
                    code = subprocess.run([sys.executable, "-c", "import sys; from windvane.heavy import run; "
                                           "sys.exit(run(sys.argv[1], cwd=sys.argv[2]))", cmd, str(cwd)], cwd=cwd,
                                          stdout=f, stderr=subprocess.STDOUT,
                                          env={**os.environ, "PYTHONPATH": str(Path(__file__).parents[1])}).returncode
                else:
                    code = subprocess.run(cmd, shell=True, cwd=cwd, stdout=f, stderr=subprocess.STDOUT).returncode
            finally:
                beat.set()
    finally:
        mins = (time.time() - start) / 60
        ledger(p, ticket, {"minutes": round(mins, 2), "passed": code == 0, "reused": False, "start": round(start, 3),
                           **({} if code is not None else {"finished": False})})
    return code, mins


def heartbeat(log):
    """Touch `log` every `HEARTBEAT` seconds from a daemon thread (it dies with the process, as a kill should leave
    the log's time) until the returned event is set."""
    stop = threading.Event()

    def beat():
        while not stop.wait(HEARTBEAT):
            try:
                os.utime(log)
            except OSError:  # the log was removed: nothing to time
                pass

    threading.Thread(target=beat, daemon=True).start()
    return stop


def locked(path):
    """Whether a check still holds `path`'s lock; a log that can't be opened isn't (OSError is the caller's)."""
    with path.open("rb") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
    return False


def running(p, start_row):
    """Whether the check a `check-start` row names is still running: its log is still locked."""
    try:
        return locked(p.state / "checks" / start_row.get("log", ""))
    except OSError:  # no log: a row from before logs were named
        return False


def outcomes(p):
    """(the logs of checks that passed, the start rows of killed or running checks by log name), from the ledger: a
    start row names the log, its end row (same ticket and start) says how it went. A log no row names (a check at
    the desk, for no ticket) is in neither, and counts as not passed."""
    rows = read_jsonl(p.state / "ledger.jsonl")
    ends = {(r.get("ticket"), r["start"]): r.get("passed") for r in rows if r.get("event") == "check" and "start" in r}
    starts = [r for r in rows if r.get("event") == "check-start" and "log" in r]
    return ({r["log"] for r in starts if ends.get((r.get("ticket"), r.get("start")))},
            {r["log"]: r for r in starts if (r.get("ticket"), r.get("start")) not in ends})


def log_minutes(log, start):
    """A killed check's minutes: from its start to its log's last write (its output's or the heartbeat's)."""
    return round(max(0.0, log.stat().st_mtime - start) / 60, 2)


def prune(p, logs, clock=time.time):
    """Delete check logs past their keep (`KEEP_PASSED`, `KEEP_ANY`), but never a locked one (its check runs) or
    one of the `KEEP_FAILED` newest that didn't pass. A killed check's log gets the end row it never wrote first, so
    `stats` doesn't time its hang at 0 once the log is gone. Never fails the check it runs for: a file it can't read
    or delete stays, and anything else wrong skips the prune. One prune at a time (another slot's check may start
    alongside): two would both close the same killed check, counting its minutes twice."""
    try:
        with (logs / ".prune.lock").open("w") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:  # another check is pruning right now
                return
            prune_logs(p, logs, clock())
    except Exception as e:  # noqa: BLE001 - a prune is housekeeping: the check must still run
        print(f"check logs not pruned (the check runs anyway): {e}", file=sys.stderr)


def prune_logs(p, logs, t):
    """`prune`'s work, under its lock."""
    passed, killed = outcomes(p)
    files = sorted(logs.glob("*.log"))  # names sort by start time
    keep = set([f for f in files if f.name not in passed][-KEEP_FAILED:])
    for f in files:
        try:
            if f in keep or t - f.stat().st_mtime < (KEEP_PASSED if f.name in passed else KEEP_ANY) or locked(f):
                continue
            if f.name in killed:
                close(p, killed[f.name], f)
            f.unlink()
        except OSError:
            pass


def close(p, start_row, log):
    """The end row a killed check never wrote (cut off, timed to its log's last write), dated like its start row so
    `stats --days` counts it where it happened."""
    row = {"event": "check", "ticket": start_row["ticket"], "minutes": log_minutes(log, start_row["start"]),
           "passed": False, "reused": False, "start": start_row["start"], "finished": False}
    append_jsonl(p.state / "ledger.jsonl", {**({"at": start_row["at"]} if "at" in start_row else {}), **row})


def tail(path, n=40):
    try:
        return "\n".join(Path(path).read_text(errors="replace").splitlines()[-n:])
    except OSError:
        return ""


def bg(p, name, cmd, cwd, heavy=False):
    d = p.state / "bg"
    d.mkdir(parents=True, exist_ok=True)
    base = d / name
    pid = Path(f"{base}.pid")
    if pid.exists() and not Path(f"{base}.exit").exists():
        try:
            os.kill(int(pid.read_text()), 0)
            sys.exit(f"bg: '{name}' is still running; wait with: windvane await {name}")
        except (OSError, ValueError):
            pass
    Path(f"{base}.exit").unlink(missing_ok=True)
    Path(f"{base}.start").write_text(str(int(time.time())))
    if heavy:
        cmd = [*exe(), "heavy", "--", *cmd]
    proc = subprocess.Popen(["bash", "-c", '"$@" > "$0.log" 2>&1; echo $? > "$0.exit"', str(base), *cmd],
                            cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, start_new_session=True)
    pid.write_text(str(proc.pid))
    session = os.environ.get(NEST)
    if session:  # killed with the session that started it (runner.kill_nested), not left behind by a stop
        mark = p.state / "nested" / session / str(proc.pid)
        mark.parent.mkdir(parents=True, exist_ok=True)
        mark.touch()
    print(f"started '{name}' (log {base}.log); wait with: windvane await {name}")


def await_(p, name, lines=40, limit=570):
    """Block until a bg job ends, at most `limit` seconds (under one tool call's 10 minutes)."""
    base = p.state / "bg" / name
    if not Path(f"{base}.start").exists():
        sys.exit(f"await: nothing started as '{name}'")
    end = time.time() + limit
    exit_f = Path(f"{base}.exit")
    while not exit_f.exists() and time.time() < end:
        try:
            os.kill(int(Path(f"{base}.pid").read_text()), 0)
        except (OSError, ValueError):
            time.sleep(1)
            if not exit_f.exists():
                exit_f.write_text("1")
            break
        time.sleep(3)
    mins = (time.time() - int(Path(f"{base}.start").read_text())) / 60
    if exit_f.exists():
        print(f"DONE {name} exit {exit_f.read_text().strip()} after {mins:.0f} min")
        print(tail(f"{base}.log", lines))
    else:
        print(f"RUNNING {name} ({mins:.0f} min so far); call windvane await {name} again")
        print(tail(f"{base}.log", 3))
