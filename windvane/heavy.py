"""`windvane heavy -- <cmd>`: run a machine-heavy command (engine, render, big build) one at a time across every
project and autopilot slot on this machine, and only when nothing else heavy is running and enough RAM is free.

It holds one machine-wide lock of its own. It never takes another tool's lock (that deadlocks when the command is
that tool, waiting on its own lock); instead it waits until no process matching a `busy` pattern runs. Settings, in
`~/.config/windvane/machine.toml`:

    busy = ["UnrealEditor", "ShaderCompile", "blender"]   # process-name regexes (pgrep) of heavy work started elsewhere
    min_free_gb = 6                                           # wait for this much MemAvailable before starting
"""
import fcntl
import os
import re
import subprocess
import sys
import time
import tomllib
from pathlib import Path

# Process names, not command lines: `pgrep -f blender` would match any Claude session whose prompt says "blender".
DEFAULTS = {"busy": ["UnrealEditor", "ShaderCompile", "blender"], "min_free_gb": 6}


def machine():
    f = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "windvane/machine.toml"
    try:
        return {**DEFAULTS, **tomllib.loads(f.read_text())}
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
        out = subprocess.run(["pgrep", pat], capture_output=True, text=True).stdout.split()
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


def run(cmd, wait_s=3600, cwd=None):
    """Run `cmd` under the machine-wide heavy lock. Returns its exit code (75 if the wait timed out)."""
    say = lambda m: print(m, file=sys.stderr, flush=True)
    cfg = machine()
    deadline = time.time() + wait_s
    path = lock_path()
    with open(path, "w") as lock:
        if os.environ.get("WINDVANE_HEAVY_HELD") != str(path):  # nested: the outer one already holds it
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                say("heavy: another heavy job holds the machine lock; waiting")
                while True:
                    try:
                        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except OSError:
                        if time.time() >= deadline:
                            say("heavy: gave up waiting for the machine lock")
                            return 75
                        time.sleep(5)
            if not wait_until_clear(cfg, deadline, say):
                return 75
        env = {**os.environ, "WINDVANE_HEAVY_HELD": str(path)}
        return subprocess.run(cmd, cwd=cwd, env=env, shell=isinstance(cmd, str)).returncode
