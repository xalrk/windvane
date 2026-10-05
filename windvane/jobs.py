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
import time
from pathlib import Path

from .config import exe
from .runner import NEST, TICKET
from .util import append_jsonl, gout, now

_LOGS = itertools.count(1)  # a check log's number in this process


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
    from one cut off."""
    start = time.time()
    ledger(p, ticket, {"start": round(start, 3), "log": log.name}, event="check-start")
    code = None
    try:
        with log.open("w") as f:
            fcntl.flock(f, fcntl.LOCK_EX)  # blocking: `running` may hold a momentary shared probe
            if p.cfg["heavy_check"]:  # one machine-heavy job at a time, across slots and projects
                code = subprocess.run([sys.executable, "-c", "import sys; from windvane.heavy import run; "
                                       "sys.exit(run(sys.argv[1], cwd=sys.argv[2]))", cmd, str(cwd)], cwd=cwd, stdout=f,
                                      stderr=subprocess.STDOUT, env={**os.environ, "PYTHONPATH": str(Path(__file__).parents[1])}).returncode
            else:
                code = subprocess.run(cmd, shell=True, cwd=cwd, stdout=f, stderr=subprocess.STDOUT).returncode
    finally:
        mins = (time.time() - start) / 60
        ledger(p, ticket, {"minutes": round(mins, 2), "passed": code == 0, "reused": False, "start": round(start, 3),
                           **({} if code is not None else {"finished": False})})
    return code, mins


def running(p, start_row):
    """Whether the check a `check-start` row names is still running: its log is still locked."""
    try:
        with (p.state / "checks" / start_row.get("log", "")).open("rb") as f:
            fcntl.flock(f, fcntl.LOCK_SH | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    except OSError:  # no log: a row from before logs were named
        pass
    return False


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
