"""Small shared helpers: git, the log, phone pushes, JSON state files."""
import calendar
import json
import os
import signal
import subprocess
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path


# Set while the driver's main thread is in a guarded step (`Pilot.critical`): the commands it runs then get a process
# group of their own and no terminal input, so a Ctrl-C at the terminal (sent to the whole foreground group) reaches
# only the driver, which lets the step finish, and never git in the middle of a merge.
shielded = False


def run(cmd, cwd, timeout=None, env=None):
    own = shielded and threading.current_thread() is threading.main_thread()
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout, env=env,
                          **({"process_group": 0, "stdin": subprocess.DEVNULL} if own else {}))


def run_limited(cmd, cwd, until, stop=lambda: "", echo=False, keep=1):
    """Run shell `cmd` with no input in a process group of its own until it ends, `until` passes or `stop()` gives a
    reason; then the whole group is killed (a build's own children too). (exit code, why it was stopped or "", its
    last `keep` lines); `echo` shows its output as it comes, so a long `npm ci` doesn't look stuck."""
    lines = deque(maxlen=keep)

    def read(out):
        for line in out:
            if echo:
                print(line, end="", flush=True)
            lines.append(line.rstrip())
    with subprocess.Popen(cmd, shell=True, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, text=True, errors="replace", start_new_session=True) as proc:
        reader = threading.Thread(target=read, args=(proc.stdout,), daemon=True)
        reader.start()
        why = ""
        try:
            while proc.poll() is None and not why:
                why = stop() or ("out of time" if now() >= until else "")
                if not why:
                    time.sleep(0.2)
        except BaseException:  # a Ctrl-C reaches only us: kill the group, or closing its pipe waits for it to end
            _kill_group(proc)
            raise
        if why:
            _kill_group(proc)
        reader.join(5)
    return proc.returncode, why, list(lines)


def _kill_group(proc):
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:  # it ended (and was reaped) as the stop came
        pass
    proc.wait()


def sh(cwd, *cmd):
    """stdout of a command, stripped; "" when it fails."""
    r = run(list(cmd), cwd)
    return r.stdout.strip() if r.returncode == 0 else ""


def git(cwd, *args):
    return run(["git", *args], cwd)


def gout(cwd, *args):
    return sh(cwd, "git", *args)


def now():
    return datetime.now()


def hhmm(t):
    return t.strftime("%H:%M") if t.date() == now().date() else t.strftime("%a %H:%M")


def log(p, msg, echo=True):
    line = f"{now():%Y-%m-%d %H:%M} {msg}"
    if echo:
        print(line, flush=True)
    p.state.mkdir(parents=True, exist_ok=True)
    with (p.state / "autopilot.log").open("a") as f:
        f.write(line + "\n")


def last_log(p, n=1):
    """The log's last `n` lines (None: all of them)."""
    f = p.state / "autopilot.log"
    return f.read_text().splitlines()[-n if n else 0:] if f.exists() else []


def push(title, msg, prio=3, tags="", buttons=()):
    """A phone push (censored first), when there's a phone: see phone.py. Silently nothing without one; True when it
    went out."""
    from . import phone
    return phone.push(title, msg, prio, tags, buttons)


def load_json(path, default):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return default


def save_json(path, data):
    """Atomic, and safe beside other writers: each process and thread has its own temp file, since parallel slots
    save usage.json from their own streams (a shared one was renamed away by the first, failing the second)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f"{path.suffix}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(data, indent=1))
    tmp.replace(path)


def append_jsonl(path, entry):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(json.dumps({"at": f"{now():%Y-%m-%d %H:%M}", **entry}) + "\n")


def read_jsonl(path):
    out = []
    try:
        lines = Path(path).read_text().splitlines()
    except OSError:
        return out
    for line in lines:
        try:
            out.append(json.loads(line))
        except ValueError:
            pass
    return out


def last_match(regex, text):
    m = None
    for m in regex.finditer(text or ""):
        pass
    return m


PROC = Path("/proc")


def started_at(pid):
    """When process `pid` started (epoch seconds), or None when it's gone. /proc on Linux; elsewhere (macOS) `ps`,
    whose `lstart` has a fixed format under LC_ALL=C, in UTC so the hour clocks go back can't read an hour off."""
    if not PROC.is_dir():
        try:
            out = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True, text=True, timeout=10,
                                 env={**os.environ, "LC_ALL": "C", "TZ": "UTC"}).stdout
            return calendar.timegm(time.strptime(" ".join(out.split()), "%a %b %d %H:%M:%S %Y"))
        except (OSError, ValueError, subprocess.SubprocessError):
            return None
    try:
        ticks = int((PROC / str(pid) / "stat").read_text().rsplit(")", 1)[1].split()[19])
        boot = next(int(l.split()[1]) for l in (PROC / "stat").read_text().splitlines() if l.startswith("btime"))
        return boot + ticks / os.sysconf("SC_CLK_TCK")
    except (OSError, ValueError, IndexError, StopIteration):
        return None
