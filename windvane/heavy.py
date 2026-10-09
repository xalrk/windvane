"""`windvane heavy -- <cmd>`: run a machine-heavy command (engine, render, big build) one at a time across every
project and autopilot slot on this machine, and only when nothing else heavy is running and enough RAM is free.

It holds one machine-wide lock of its own. It never takes another tool's lock (that deadlocks when the command is
that tool, waiting on its own lock); instead it waits until no process matching a `busy` pattern runs. Settings, in
`~/.config/windvane/machine.toml`:

    busy = ["UnrealEditor", "ShaderCompile", "blender"]   # process-name regexes (pgrep) of heavy work started elsewhere
    min_free_gb = 6                                           # wait for this much MemAvailable before starting

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
from pathlib import Path

from .config import user_dir

# Process names, not command lines: `pgrep -f blender` would match any Claude session whose prompt says "blender".
DEFAULTS = {"busy": ["UnrealEditor", "ShaderCompile", "blender"], "min_free_gb": 6}


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
        m = re.search(r"^MemAvailable:\s+(\d+) kB", Path("/proc/meminfo").read_text(), re.M)
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


def wait_until_clear(cfg, deadline, say):
    said = None
    while True:
        b = busy(cfg["busy"])
        mem = free_gb()
        why = (f"'{b}' is running" if b else
               f"only {mem:.1f} GB free (want {cfg['min_free_gb']})" if mem is not None and mem < cfg["min_free_gb"]
               else None)
        if not why:
            return True
        if time.time() >= deadline:
            say(f"heavy: gave up waiting: {why}")
            return False
        if why != said:
            say(f"heavy: waiting: {why}")
            said = why
        time.sleep(10)


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


def ahead(entry):
    """How many live waiters arrived before this one. Clears dead entries on the way (and a waiter's hidden one
    left by a kill between creating and renaming it)."""
    q, mine, n = entry[1].parent, entry[1].name, 0
    for name in sorted(os.listdir(q)):
        path = q / name
        if name.startswith("."):
            try:
                if time.time() - path.stat().st_mtime > 60:
                    alive(path)
            except OSError:
                pass
        elif name < mine and alive(path):
            n += 1
    return n


def holder(lock):
    """Who holds the lock, as the holder wrote it in: {pid, since, cmd, cwd}, or {} (an older windvane)."""
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
    info = {"pid": os.getpid(), "since": time.time(), "cmd": text if len(text) <= 60 else text[:59] + "…",
            "cwd": "/".join(where)}
    os.ftruncate(lock, 0)
    os.lseek(lock, 0, os.SEEK_SET)
    os.write(lock, json.dumps(info).encode())


def waiting_line(info, n):
    if info.get("pid"):
        mins = int((time.time() - info.get("since", time.time())) // 60)
        since = time.strftime("%H:%M", time.localtime(info.get("since", time.time())))
        who = f"held by pid {info['pid']} ({info.get('cmd', '?')} in {info.get('cwd', '?')}, since {since}, {mins} min)"
    else:
        who = "held by another heavy job"
    return f"heavy: waiting for the machine lock: {who}; " + (f"{n} ahead of this job" if n else "next in line")


def take(lock, entry, deadline, say):
    """Wait for this job's turn, then take the lock: only the oldest live waiter tries it, so a job that arrives
    just as the lock frees can't overtake one that has been waiting (R-007). True once held, False at the deadline."""
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
        if time.time() >= deadline:
            say("heavy: gave up waiting for the machine lock")
            return False
        time.sleep(1)


def run(cmd, wait_s=3600, cwd=None):
    """Run `cmd` under the machine-wide heavy lock. Returns its exit code (75 if the wait timed out).

    Jobs run in arrival order: each joins a queue when it asks and waits there for the lock, then holds the lock
    while it waits for the machine to clear (busy processes, free memory), so it keeps its place throughout."""
    say = lambda m: print(m, file=sys.stderr, flush=True)
    cfg = machine()
    deadline = time.time() + wait_s
    path = lock_path()
    env = {**os.environ, "WINDVANE_HEAVY_HELD": str(path)}
    if os.environ.get("WINDVANE_HEAVY_HELD") == str(path):  # nested: the outer one already holds it
        return subprocess.run(cmd, cwd=cwd, env=env, shell=isinstance(cmd, str)).returncode
    entry = join_queue()
    lock = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)  # not truncated on open: the holder's line is in it
    try:
        if not take(lock, entry, deadline, say):
            return 75
        leave_queue(entry)  # the lock keeps this job's place now; the next waiter becomes the head and waits on it
        entry = None
        try:
            claim(lock, cmd, cwd)
            if not wait_until_clear(cfg, deadline, say):
                return 75
            return subprocess.run(cmd, cwd=cwd, env=env, shell=isinstance(cmd, str)).returncode
        finally:
            os.ftruncate(lock, 0)
    finally:
        if entry:
            leave_queue(entry)
        os.close(lock)  # frees the lock
