"""`windvane heavy -- <cmd>`: run a machine-heavy command (engine, render, big build) one at a time across every
project and autopilot slot on this machine, and only when nothing else heavy is running and enough RAM is free.

It holds one machine-wide lock of its own. It never takes another tool's lock (that deadlocks when the command is
that tool, waiting on its own lock); instead it waits until no process matching a `busy` pattern runs. Settings, in
`~/.config/windvane/machine.toml`:

    busy = ["UnrealEditor", "ShaderCompile", "blender"]   # process-name regexes (pgrep) of heavy work started elsewhere
    min_free_gb = 6                                           # wait for this much MemAvailable before starting
    heavy_wait = 60                                           # minutes a command run by hand waits before giving up

Free memory comes from Linux's /proc/meminfo; elsewhere (macOS) it isn't known, and only `busy` is waited on.
"""
import fcntl
import json
import os
import re
import shlex
import subprocess
import sys
import time
import tomllib
from datetime import datetime
from pathlib import Path

from . import util
from .config import user_dir
from .util import started_at

# Process names, not command lines: `pgrep -f blender` would match any Claude session whose prompt says "blender".
DEFAULTS = {"busy": ["UnrealEditor", "ShaderCompile", "blender"], "min_free_gb": 6, "heavy_wait": 60}
# A wait that timed out: the exit code, and the start of the line said just before it (`gave_up` reads it back).
TIMED_OUT = 75
GAVE_UP = "heavy: gave up waiting for the machine"
# A wait cut short by the project's STOP file (its path in this env var, set by the check's runner): its last line.
STOP_ENV, STOPPED = "WINDVANE_HEAVY_STOP", "heavy: stopped while waiting for the machine"
# Said once the job got the machine after waiting, before its own output: a check reads it back (`waited`) for its
# ledger row, so `stats` can tell the minutes a check queued from the minutes it ran.
WAITED = "heavy: waited {} s for the machine"
# A session's stop, as a Unix time: the driver sets it for each session (its `bg` jobs and checks inherit it) and for
# its own checks. A job waits for the machine until then, not `heavy_wait`.
STOP_AT = "WINDVANE_STOP_AT"
# The driver's check also follows the run's deadline file (its path in this env var), read as it waits: a deadline the
# user moved earlier ends the wait then, not at the end it had when the check started.
DEADLINE_ENV = "WINDVANE_HEAVY_DEADLINE"


def machine_file():
    return user_dir("XDG_CONFIG_HOME", ".config") / "machine.toml"


def machine():
    try:
        return {**DEFAULTS, **tomllib.loads(machine_file().read_text())}
    except (OSError, ValueError):
        return dict(DEFAULTS)


def lock_path():
    return Path(os.environ.get("XDG_RUNTIME_DIR") or "/tmp") / "windvane-heavy.lock"


def free_gb():
    try:
        m = re.search(r"^MemAvailable:\s+(\d+) kB", (util.PROC / "meminfo").read_text(), re.M)
        return int(m.group(1)) / 1048576 if m else None
    except OSError:
        return None


def busy(patterns):
    """The first busy pattern with a running process (other than this one's own tree), or None."""
    for pat in patterns:
        try:
            out = subprocess.run(["pgrep", pat], capture_output=True, text=True).stdout.split()
        except OSError:  # no pgrep (a minimal system): nothing can be seen running
            return None
        if [p for p in out if int(p) not in (os.getpid(), os.getppid())]:
            return pat
    return None


def nap(seconds, stopped):
    """Sleep up to `seconds`, waking each second to see a stop."""
    end = time.time() + seconds
    while time.time() < end and not stopped():
        time.sleep(min(1, end - time.time()))


def wait_until_clear(cfg, deadline, say, stopped=lambda: False):
    said = None
    while True:
        b = busy(cfg["busy"])
        mem = free_gb()
        why = (f"'{b}' is running" if b else
               f"only {mem:.1f} GB free (want {cfg['min_free_gb']})" if mem is not None and mem < cfg["min_free_gb"]
               else None)
        if not why:
            return True
        if stopped():  # only once it has to wait: a clear machine still runs the job
            say(f"{STOPPED}: {why}")
            return False
        if time.time() >= deadline():
            say(f"{GAVE_UP}: {why}")
            return False
        if why != said:
            say(f"heavy: waiting: {why}")
            said = why
        nap(10, stopped)


def queue_dir():
    return lock_path().with_name("windvane-heavy.queue")


def join_queue():
    """An entry in the queue, named by arrival time so they sort in order, and flocked while this job lives.

    It's flocked under a hidden name first and only then renamed into place, so a scan never sees a live entry
    unlocked. The open file keeps the entry; closing it (or dying) frees it."""
    q = queue_dir()
    q.mkdir(parents=True, exist_ok=True)
    name = f"{time.time_ns():020d}-{os.getpid()}"
    fd = os.open(q / f".{name}", os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    os.rename(q / f".{name}", q / name)
    return fd, q / name


def leave_queue(entry):
    fd, path = entry
    path.unlink(missing_ok=True)
    os.close(fd)


def alive(path):
    """Whether a queue entry's waiter still lives (holds its flock). A dead one's entry is removed."""
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return True
    else:
        path.unlink(missing_ok=True)
        return False
    finally:
        os.close(fd)


# Without /proc (macOS) `paused` runs `ps`, and every waiter asks about each one ahead every second: a pid's answer
# is kept this long, {pid: (when asked, paused)}.
PS_KEEP = 10
_ps_seen = {}


def paused(pid):
    """Whether process `pid` is stopped (Ctrl-Z, SIGSTOP): from /proc on Linux, else `ps` (kept PS_KEEP seconds);
    False when unknown."""
    if util.PROC.is_dir():
        try:
            return (util.PROC / str(pid) / "stat").read_text().rsplit(")", 1)[1].split()[0] in ("T", "t")
        except (OSError, IndexError):  # gone
            return False
    now = time.monotonic()
    asked, was = _ps_seen.get(pid, (None, False))
    if asked is not None and now - asked < PS_KEEP:
        return was
    try:
        out = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True,
                             timeout=util.PS_WAIT).stdout
    except (OSError, subprocess.SubprocessError):  # a hung `ps` mustn't stall the wait
        out = ""
    for p in [p for p, (at, _) in _ps_seen.items() if now - at >= PS_KEEP]:
        del _ps_seen[p]
    _ps_seen[pid] = (now, out.strip()[:1] in ("T", "t"))
    return _ps_seen[pid][1]


def entry_pid(name):
    try:
        return int(name.rsplit("-", 1)[1])
    except (IndexError, ValueError):
        return None


def ahead(entry):
    """How many live waiters arrived before this one. Clears dead entries on the way (and a waiter's hidden one
    left by a kill between creating and renaming it). A paused waiter (Ctrl-Z) doesn't count: it keeps its entry,
    but would hold up the whole line until its wait times out; resumed, it gets in line again."""
    q, mine, n = entry[1].parent, entry[1].name, 0
    for name in sorted(os.listdir(q)):
        path = q / name
        if name.startswith("."):
            try:
                if time.time() - path.stat().st_mtime > 60:
                    alive(path)
            except OSError:
                pass
        elif name < mine and alive(path) and not paused(entry_pid(name)):
            n += 1
    return n


def holder(lock):
    """Who holds the lock, as the holder wrote it in: {pid, start, since, cmd, cwd}, or {} (an older windvane)."""
    try:
        os.lseek(lock, 0, os.SEEK_SET)
        return json.loads(os.read(lock, 4096) or b"{}")
    except (OSError, ValueError):
        return {}


def claim(lock, cmd, cwd):
    """Write this job into the lock it now holds, for the waiters' log line."""
    text = cmd if isinstance(cmd, str) else shlex.join(cmd)
    text = " ".join(text.split())
    where = Path(cwd or os.getcwd()).resolve().parts[-2:]
    info = {"pid": os.getpid(), "start": started_at(os.getpid()), "since": time.time(), "cmd": text if len(text) <= 60 else text[:59] + "…",
            "cwd": "/".join(where)}
    os.ftruncate(lock, 0)
    os.lseek(lock, 0, os.SEEK_SET)
    os.write(lock, json.dumps(info).encode())


def still_held(info):
    """Whether the holder named in the lock file still runs. A killed one leaves its line, and its pid may be reused
    by some other process later: one that started at another time (2 s apart: /proc's boot time and ps's are in
    whole seconds). A line without a start (an older windvane) or a start that can't be read goes by the pid alone."""
    if not lives(info["pid"]):
        return False
    now = started_at(info["pid"]) if info.get("start") is not None else None
    return now is None or abs(now - info["start"]) <= 2


def held_by(info):
    if not info.get("pid"):
        return "held by another heavy job"
    if not still_held(info):
        return "free"
    # A clock time, not minutes held: the line is printed once and read later, when minutes would be stale.
    since = time.strftime("%H:%M", time.localtime(info.get("since", time.time())))
    return f"held by pid {info['pid']} ({info.get('cmd', '?')} in {info.get('cwd', '?')}, since {since})"


def waiting_line(info, n):
    return f"heavy: waiting for the machine lock: {held_by(info)}; " + (f"{n} ahead of this job" if n else "next in line")


def gave_up(log):
    """What a heavy job whose output went to `log` waited for and never got, "the machine lock (held by ...)", and
    whether a stop rather than the timeout ended the wait, from the line `run` said then (its last); None when it
    ran: the exit code can't tell, a command may exit 75 itself."""
    try:
        lines = Path(log).read_text(errors="replace").splitlines()
    except OSError:
        return None
    m = re.match(f"({re.escape(GAVE_UP)}|{re.escape(STOPPED)})" + r"( lock)?: (.*)", lines[-1] if lines else "")
    return m and (f"the machine{m.group(2) or ''} ({m.group(3)})", m.group(1) == STOPPED)


def waited(log, head=65536):
    """The seconds a heavy job whose output went to `log` waited before it ran, from `run`'s WAITED line (0 when it
    didn't wait). Only the log's head is read: the line comes before the command's own output, after a few waiting
    lines."""
    try:
        with open(log, "rb") as f:
            text = f.read(head).decode(errors="replace")
    except OSError:
        return 0
    m = re.search("^" + re.escape(WAITED).replace(r"\{\}", r"(\d+)") + "$", text, re.M)
    return int(m.group(1)) if m else 0


def take(lock, entry, deadline, say, stopped=lambda: False):
    """Wait for this job's turn, then take the lock: only the oldest live waiter tries it, so a job that arrives
    just as the lock frees can't overtake one that has been waiting (R-007). True once held, False at the deadline
    (a function: a moved deadline counts at once) or a stop."""
    said = None
    while True:
        n = ahead(entry)
        if not n:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return True
            except OSError:
                pass
        info = holder(lock)
        key = (info.get("pid"), info.get("since"), n)
        if key != said:  # reprinted only when the holder or the line ahead changes, not every minute
            say(waiting_line(info, n))
            said = key
        if stopped():
            say(f"{STOPPED} lock: {held_by(info)}")
            return False
        if time.time() >= deadline():
            say(f"{GAVE_UP} lock: {held_by(info)}")
            return False
        time.sleep(1)


def moved(path):
    """The time in a deadline file (an ISO time, as `windvane deadline` writes it); never when it can't be read."""
    try:
        return datetime.fromisoformat(Path(path).read_text().strip()).timestamp()
    except (OSError, ValueError):
        return float("inf")


def deadline(cfg, wait_s):
    """When a wait for the machine gives up: after `wait_s` seconds when given; else at the session's stop
    (`STOP_AT`), since a job still waiting then is killed with its session anyway, and one that gave up earlier
    would lose its place in line and requeue at the back; else after machine.toml's `heavy_wait` (a command run by
    hand)."""
    if wait_s is not None:
        return time.time() + wait_s
    try:
        return float(os.environ[STOP_AT])
    except (KeyError, ValueError):
        return time.time() + cfg["heavy_wait"] * 60


def run(cmd, wait_s=None, cwd=None):
    """Run `cmd` under the machine-wide heavy lock. Returns its exit code (TIMED_OUT if the wait timed out, at
    `deadline`, or as soon as the STOP file named in `STOP_ENV` appears while it waits).

    Jobs run in arrival order: each joins a queue when it asks and waits there for the lock, then holds the lock
    while it waits for the machine to clear (busy processes, free memory), so it keeps its place throughout."""
    say = lambda m: print(m, file=sys.stderr, flush=True)
    t0 = time.time()
    cfg = machine()
    end, moves = deadline(cfg, wait_s), os.environ.get(DEADLINE_ENV)
    until = lambda: min(end, moved(moves)) if moves else end
    path = lock_path()
    stop = os.environ.get(STOP_ENV)
    stopped = lambda: bool(stop) and os.path.exists(stop)
    env = {k: v for k, v in os.environ.items() if k not in (STOP_ENV, DEADLINE_ENV)}  # for this wait, not the command
    env["WINDVANE_HEAVY_HELD"] = str(path)
    if os.environ.get("WINDVANE_HEAVY_HELD") == str(path):  # nested: the outer one already holds it
        return subprocess.run(cmd, cwd=cwd, env=env, shell=isinstance(cmd, str)).returncode
    entry = join_queue()
    lock = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)  # not truncated on open: the holder's line is in it
    try:
        if not take(lock, entry, until, say, stopped):
            return TIMED_OUT
        leave_queue(entry)  # the lock keeps this job's place now; the next waiter becomes the head and waits on it
        entry = None
        try:
            claim(lock, cmd, cwd)
            if not wait_until_clear(cfg, until, say, stopped):
                return TIMED_OUT
            if (s := round(time.time() - t0)) >= 1:
                say(WAITED.format(s))
            return subprocess.run(cmd, cwd=cwd, env=env, shell=isinstance(cmd, str)).returncode
        finally:
            os.ftruncate(lock, 0)
    finally:
        if entry:
            leave_queue(entry)
        os.close(lock)  # frees the lock


def status():
    """Who holds the lock and who waits, in order, without waiting or taking either: the holder's line (it writes it
    in when it takes the lock, and empties it on the way out), then a line per live waiter."""
    try:
        info = json.loads(lock_path().read_text() or "{}")
    except (OSError, ValueError):
        info = {}
    line = held_by(info) if info.get("pid") else "free"
    if line != "free" and paused(info["pid"]):
        line += " (paused: everyone waits on it)"
    lines = [line]
    q = queue_dir()
    for name in sorted(os.listdir(q)) if q.is_dir() else []:
        pid = entry_pid(name)
        if name.startswith(".") or not pid or not alive(q / name):
            continue
        since = time.strftime("%H:%M", time.localtime(int(name.split("-")[0]) / 1e9))
        lines.append(f"waiting: pid {pid}, since {since}" + (" (paused: skipped)" if paused(pid) else ""))
    return lines


def lives(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:  # someone else's
        pass
    return True
