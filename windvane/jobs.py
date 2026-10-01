"""The project's check (CI) with a pass cache, and background jobs for commands that outlast a tool call.

A pass is recorded under the git tree it ran on (plus the check command), only when the checkout was clean before
and after, so the driver and the reviewer reuse a worker's green check instead of running it again. Failures are
never recorded.
"""
import hashlib
import os
import subprocess
import sys
import time
from pathlib import Path

from .util import gout, now


def tree_key(p, cwd):
    """The key of a clean checkout's tree for the configured check, or None if it has uncommitted changes."""
    if gout(cwd, "status", "--porcelain", "--untracked-files=normal"):
        return None
    tree = gout(cwd, "rev-parse", "HEAD^{tree}")
    return hashlib.sha1(f"{tree}\n{p.cfg['check']}".encode()).hexdigest() if tree else None


def check(p, cwd, quiet=False):
    """Run the project's check in `cwd`. Returns (passed, summary line, log path)."""
    cmd = p.cfg["check"]
    if not cmd:
        return True, "CHECK PASSED (no check configured)", None
    passes = p.state / "passes"
    key = tree_key(p, cwd)
    if key and (passes / key).exists():
        return True, f"CHECK PASSED (reused the pass from {(passes / key).read_text().strip()} for this tree)", None
    logs = p.state / "checks"
    logs.mkdir(parents=True, exist_ok=True)
    log = logs / f"{now():%Y%m%d-%H%M%S}.log"
    start = time.time()
    with log.open("w") as f:
        if p.cfg["heavy_check"]:  # one machine-heavy job at a time, across slots and projects
            code = subprocess.run([sys.executable, "-c", "import sys; from windvane.heavy import run; "
                                   "sys.exit(run(sys.argv[1], cwd=sys.argv[2]))", cmd, str(cwd)], cwd=cwd, stdout=f,
                                  stderr=subprocess.STDOUT, env={**os.environ, "PYTHONPATH": str(Path(__file__).parents[1])}).returncode
        else:
            code = subprocess.run(cmd, shell=True, cwd=cwd, stdout=f, stderr=subprocess.STDOUT).returncode
    mins = (time.time() - start) / 60
    if code == 0:
        if key and tree_key(p, cwd) == key:
            passes.mkdir(parents=True, exist_ok=True)
            (passes / key).write_text(f"{now():%Y-%m-%d %H:%M} ({gout(cwd, 'rev-parse', '--short', 'HEAD')})")
        return True, f"CHECK PASSED in {mins:.1f} min" + ("" if key else " (uncommitted changes: not recorded)"), log
    return False, f"CHECK FAILED (exit {code}) in {mins:.1f} min; log {log}", log


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
        cmd = [str(Path(__file__).parents[1] / "bin/windvane"), "heavy", "--", *cmd]
    proc = subprocess.Popen(["bash", "-c", '"$@" > "$0.log" 2>&1; echo $? > "$0.exit"', str(base), *cmd],
                            cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, start_new_session=True)
    pid.write_text(str(proc.pid))
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
