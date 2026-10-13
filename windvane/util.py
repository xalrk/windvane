"""Small shared helpers: git, the log, phone pushes, JSON state files."""
import calendar
import fcntl
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path


# Set while the driver's main thread is in a guarded step (`Pilot.critical`): the commands it runs then get a process
# group of their own and no terminal input, so a Ctrl-C at the terminal (sent to the whole foreground group) reaches
# only the driver, which lets the step finish, and never git in the middle of a merge.
shielded = False


def held(fh):
    """Whether another open file holds a lock on `fh`'s file. The probe's own shared lock is let go at once: a close
    alone leaves it held while any thread's fork (a git or a session starting) still has the fd, until that child
    execs, and a run taking the lock then would think another one has it."""
    try:
        fcntl.flock(fh, fcntl.LOCK_SH | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    fcntl.flock(fh, fcntl.LOCK_UN)
    return False


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


# Linux's process table. Every reader of it goes through this name, and takes its `ps` (macOS) branch when it isn't a
# dir, so the tests point it at a path that isn't there to run that branch on Linux (test_no_proc).
PROC = Path("/proc")
PS_WAIT = 10  # seconds a `ps` may take: a hung one counts as no answer


def cwds(pid=None):
    """{pid: working directory} of every process we can see, or of `pid` alone. /proc on Linux; elsewhere (macOS)
    `lsof`, which macOS keeps in /usr/sbin, often off PATH. Empty when neither answers."""
    if PROC.is_dir():
        out = {}
        for d in [str(pid)] if pid else [d for d in _listdir(PROC) if d.isdigit()]:
            try:
                out[int(d)] = os.readlink(PROC / d / "cwd")
            except OSError:
                pass
        return out
    exe = shutil.which("lsof") or next((x for x in ("/usr/sbin/lsof", "/usr/bin/lsof") if os.access(x, os.X_OK)), None)
    if not exe:
        return {}
    try:
        text = subprocess.run([exe, "-a", "-d", "cwd", "-w", "-Fn", *(["-p", str(pid)] if pid else [])],
                              capture_output=True, text=True, timeout=PS_WAIT).stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    out, at = {}, None
    for line in text.splitlines():  # -F: a `p<pid>` line, then its `n<path>`
        if line.startswith("p"):
            at = int(line[1:])
        elif line.startswith("n") and at is not None:
            out[at] = line[1:]
    return out


def tmpdirs():
    """{pid: its TMPDIR} for every process we can see that sets one. /proc on Linux; elsewhere `ps` with the
    environment, which shows only our own processes' (paths with spaces in them are cut at the space)."""
    out = {}
    if PROC.is_dir():
        for d in [d for d in _listdir(PROC) if d.isdigit()]:
            try:
                env = (PROC / d / "environ").read_bytes()
            except OSError:
                continue
            for var in env.split(b"\0"):
                if var.startswith(b"TMPDIR="):
                    out[int(d)] = os.fsdecode(var[7:])
        return out
    flags = "-axwwE" if sys.platform == "darwin" else "axeww"  # macOS's ps, or procps (a test without /proc)
    try:
        text = subprocess.run(["ps", flags, "-o", "pid=,command="], capture_output=True, text=True,
                              timeout=PS_WAIT).stdout
    except (OSError, subprocess.SubprocessError):
        return out
    for line in text.splitlines():
        pid, _, rest = line.strip().partition(" ")
        m = re.search(r"(?:^|\s)TMPDIR=(\S+)", rest)
        if m and pid.isdigit():
            out[int(pid)] = m.group(1)
    return out


def _listdir(d):
    try:
        return os.listdir(d)
    except OSError:
        return []


def started_at(pid):
    """When process `pid` started (epoch seconds), or None when it's gone. /proc on Linux; elsewhere (macOS) `ps`,
    whose `lstart` has a fixed format under LC_ALL=C, in UTC so the hour clocks go back can't read an hour off."""
    if not PROC.is_dir():
        try:
            out = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True, text=True,
                                 timeout=PS_WAIT, env={**os.environ, "LC_ALL": "C", "TZ": "UTC"}).stdout
            return calendar.timegm(time.strptime(" ".join(out.split()), "%a %b %d %H:%M:%S %Y"))
        except (OSError, ValueError, subprocess.SubprocessError):
            return None
    try:
        ticks = int((PROC / str(pid) / "stat").read_text().rsplit(")", 1)[1].split()[19])
        boot = next(int(l.split()[1]) for l in (PROC / "stat").read_text().splitlines() if l.startswith("btime"))
        return boot + ticks / os.sysconf("SC_CLK_TCK")
    except (OSError, ValueError, IndexError, StopIteration):
        return None
