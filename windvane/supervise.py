"""What keeps a detached run alive when memory runs out. On Linux the kernel's out-of-memory killer ends one process,
but systemd's default `OOMPolicy=stop` then stops the whole unit or scope it lived in: a session's runaway test would
take the driver with it. `--detach` therefore starts the run in a scope of its own with `OOMPolicy=continue`
(`scoped`), the driver's children are the kernel's first pick (`util.doomed`), and a run that a kill ends anyway is
told by its cgroup's `oom_kill` count (`oom_kills`) and restarted by `driver.supervise`, which first ends what the dead
driver left running in its process group (`end_orphans`)."""
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import phone

SCOPE = ["systemd-run", "--user", "--scope", "--quiet", "-p", "OOMPolicy=continue", "--"]
OOM_LOOK = 5  # seconds between a run's looks at its cgroup's OOM-kill count
PROBE_S = 10  # seconds the scope's probe may take: a user manager that doesn't answer means no scope
PROC = Path("/proc")
CGROUP = Path("/sys/fs/cgroup")
RESTARTED = "restarts.json"  # the supervisor's restarts in this run, for `windvane status`


def scope_ok():
    """Whether `systemd-run --user --scope` works here (Linux, a systemd user manager it can reach, a systemd that
    knows OOMPolicy on a scope), tried with `true` so a run is never started in one that fails."""
    if not (sys.platform.startswith("linux") and phone.has_systemd() and shutil.which("systemd-run")):
        return False
    try:
        return subprocess.run([*SCOPE, "true"], stdin=subprocess.DEVNULL, capture_output=True,
                              timeout=PROBE_S).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def scoped(cmd):
    """`cmd` (an argv list) in a scope where an OOM kill takes only the process that ran away, when there is one."""
    return [*SCOPE, *cmd] if scope_ok() else list(cmd)


def default_policy():
    """The user manager's DefaultOOMPolicy ("stop" on most machines), or "" when it can't be read."""
    try:
        r = subprocess.run(["systemctl", "--user", "show", "-p", "DefaultOOMPolicy", "--value"],
                           stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=PROBE_S)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return r.stdout.strip() if r.returncode == 0 else ""


def events_file():
    """This process's cgroup's `memory.events` (cgroup v2), or None: not Linux, cgroup v1, or no memory controller."""
    try:
        for line in (PROC / "self" / "cgroup").read_text().splitlines():
            if line.startswith("0::"):
                f = CGROUP / line[3:].strip().lstrip("/") / "memory.events"
                return f if f.is_file() else None
    except OSError:
        pass
    return None


def oom_kills():
    """How many processes the kernel's out-of-memory killer has ended in this process's cgroup (and below it), or
    None when that can't be read. Read at a run's start and when it is stopped, so a rise names the cause."""
    f = events_file()
    try:
        for line in f.read_text().splitlines() if f else ():
            key, _, n = line.partition(" ")
            if key == "oom_kill":
                return int(n)
    except (OSError, ValueError):
        pass
    return None


def rose(before):
    """Whether an OOM kill happened in this cgroup since `before` (an `oom_kills()`)."""
    after = oom_kills()
    return before is not None and after is not None and after > before


class OomWatch:
    """The cgroup's OOM-kill count as it was a few seconds ago (`base`), looked at every OOM_LOOK seconds while a run
    goes: systemd's OOMPolicy=stop sends its SIGTERM right after the kill, while in a scope with OOMPolicy=continue a
    session's kill hours earlier is no cause of a later shutdown."""

    def __init__(self):
        self.base = self.last = oom_kills()
        self.done = threading.Event()

    def start(self):
        if self.last is not None:
            threading.Thread(target=self.watch, daemon=True).start()

    def watch(self):
        while not self.done.wait(OOM_LOOK):
            self.look()

    def look(self):
        self.base, self.last = self.last, oom_kills()

    def stop(self):
        self.done.set()

    def rose(self):
        """Whether an OOM kill came in the last OOM_LOOK to 2 * OOM_LOOK seconds."""
        return rose(self.base)


def ancestors():
    pids, pid = set(), os.getpid()
    while pid > 1:
        pids.add(pid)
        try:
            pid = int((PROC / str(pid) / "stat").read_text().rpartition(")")[2].split()[1])
        except (OSError, ValueError, IndexError):
            break
    return pids


def end_orphans(grace=3):
    """End what a killed driver left in this supervisor's process group (a check, a git command: a driver's children
    that have no group of their own; its sessions do, and the restarted driver sweeps those), so a restart never runs
    beside them. Only as the group's leader, which `run --detach` makes it: anywhere else the group is someone else's.
    How many it signalled; nothing off Linux (no /proc)."""
    me = os.getpid()
    if os.getpgrp() != me or not (PROC / "self" / "stat").exists():
        return 0
    keep = ancestors()
    left = []
    for d in PROC.iterdir():
        if not d.name.isdigit() or int(d.name) in keep:
            continue
        try:
            if int((d / "stat").read_text().rpartition(")")[2].split()[2]) == me:
                left.append(int(d.name))
        except (OSError, ValueError, IndexError):
            continue
    n = len(left)
    for sig in (signal.SIGTERM, signal.SIGKILL):
        alive = []
        for pid in left:
            try:
                os.kill(pid, sig)
                alive.append(pid)
            except (ProcessLookupError, PermissionError):
                pass
        end = time.time() + grace
        while sig == signal.SIGTERM and alive and time.time() < end:
            time.sleep(0.1)
            alive = [pid for pid in alive if lives(pid)]
        left = alive
    return n


def lives(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:  # an orphan this supervisor doesn't reap stays a zombie: dead
        return (PROC / str(pid) / "stat").read_text().rpartition(")")[2].split()[0] != "Z"
    except (OSError, IndexError):
        return False
