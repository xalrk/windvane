"""Small shared helpers: git, the log, phone pushes, JSON state files."""
import calendar
import contextlib
import fcntl
import json
import math
import os
import re
import shlex
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
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout,
                          **({"process_group": 0, "stdin": subprocess.DEVNULL, "env": no_prompts(cwd, env)} if own
                             else {"env": env}))


def no_prompts(cwd, env=None):
    """`env` (or ours) for a command out of the terminal's reach: git and ssh asking for a password or a passphrase
    there would stop (SIGTTIN) until the network timeout, with nothing said; this way they fail at once, in words
    `held.push_refusal` reads as a login problem. The user's own ssh command (`GIT_SSH_COMMAND`, `GIT_SSH`,
    `core.sshCommand`) is kept: ours would override it, and a key it picks would never be used."""
    env = dict(os.environ if env is None else env, GIT_TERMINAL_PROMPT="0")
    if not (env.get("GIT_SSH_COMMAND") or env.get("GIT_SSH") or subprocess.run(
            ["git", "config", "--get", "core.sshCommand"], cwd=cwd, env=env, capture_output=True,
            stdin=subprocess.DEVNULL).stdout.strip()):
        env["GIT_SSH_COMMAND"] = "ssh -o BatchMode=yes"
    return env


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


# Past this size the log moves to `autopilot.log.1` (replacing the one before) and a fresh one starts: every `status`
# reads its tail, and `stats` all of it, so it can't grow for good.
LOG_CAP = 4 * 1024 * 1024


def log_paths(p):
    """The rotated log and the current one, oldest first, whether or not they exist."""
    f = p.state / "autopilot.log"
    return [f.with_name(f.name + ".1"), f]


def log(p, msg, echo=True):
    line = f"{now():%Y-%m-%d %H:%M} {msg}"
    if echo:
        print(line, flush=True)
    p.state.mkdir(parents=True, exist_ok=True)
    old, f = log_paths(p)
    with f.open("a") as fh:
        fh.write(line + "\n")
        fh.flush()
        if os.fstat(fh.fileno()).st_size > LOG_CAP:
            rotate_log(f, old, fh)


def rotate_log(f, old, fh):
    """Moves the full log `f` (open as `fh`) to `old`. Locked on the full file, and only while it's still the one at
    `f`: parallel slots past the cap at once move it once, never the fresh log over the full one."""
    fcntl.flock(fh, fcntl.LOCK_EX)
    try:
        if os.stat(f).st_ino == os.fstat(fh.fileno()).st_ino:
            os.replace(f, old)
    except OSError:  # another writer moved it first
        pass


def shell_path(f):
    """`f` as a shell word: under home as `~/...`, for width; quoted in full when it holds a space or the like."""
    home, f = str(Path.home()), str(f)
    if f.startswith(home + "/") and shlex.quote(f[len(home):]) == f[len(home):]:
        return "~" + f[len(home):]
    return shlex.quote(f)


# The driver's log line for a worker run that ended PARTIAL: `windvane stats` greps for it, so the two stay one text.
PARTIAL_LOG = "{tid}: partial"


def last_log(p, n=1):
    """The log's last `n` lines (None: all of them), from the rotated one too when the current one is short of them."""
    lines = []
    for f in reversed(log_paths(p)):
        lines = tail_lines(f, n - len(lines) if n else None) + lines
        if n and len(lines) >= n:
            break
    return lines


def log_has(f, needle):
    """Whether a line of `f` holds `needle`, read a line at a time and stopped at the first: a log runs to megabytes."""
    try:
        with open(f, errors="replace") as fh:
            return any(needle in line for line in fh)
    except OSError:
        return False


def tail_lines(f, n):
    """`f`'s last `n` lines (None: all of them), read from the end: a status needs a dozen lines of a long log."""
    try:
        with open(f, "rb") as fh:
            if not n:
                return fh.read().decode(errors="replace").splitlines()
            end = pos = fh.seek(0, 2)
            data = b""
            while pos and data.count(b"\n") <= n:
                pos = max(0, pos - 65536)
                fh.seek(pos)
                data = fh.read(end - pos)
            return data.decode(errors="replace").splitlines()[-n:]
    except OSError:
        return []


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
    replace_text(path, json.dumps(data, indent=1))


def replace_text(path, text):
    """Write `path` whole in one rename, so a reader never sees it empty or half-written."""
    path = Path(path)
    tmp = path.with_suffix(f"{path.suffix}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(text)
    tmp.replace(path)


def edit_json(path, change, default):
    """Load, let `change` edit the data in place, save, all under an exclusive flock on a sibling `.lock` file, so two
    writers (the driver and `windvane answer`, two `windvane answer`s) can't each save over the other's edit: save_json
    alone is atomic but last-writer-wins. The lock goes with its process, so a crash leaves none behind. A reader that
    only reads needs no lock. Returns what `change` returned."""
    with locked(path):
        data = load_json(path, default)
        out = change(data)
        save_json(path, data)
    return out


@contextlib.contextmanager
def locked(path):
    """`edit_json`'s lock on `path` alone, for an edit of more than one file under one lock (held.json and its
    reasons)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path.with_name(f"{path.name}.lock"), "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


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
PS_GRAIN = 1  # seconds `ps lstart` (macOS's start times) rounds down to: within one, it can't say which came first


def wsl():
    """1 or 2 inside Windows' WSL (its kernel names Microsoft in /proc/version), else 0: there the Windows drives and
    the Windows PATH are in reach, and both trip windvane up."""
    try:
        version = (PROC / "version").read_text().lower()
    except OSError:
        return 0
    if "microsoft" not in version:
        return 0
    return 2 if "wsl2" in version or "microsoft-standard" in version else 1


def windows_drive(path):
    """The drive letter when `path` is on a Windows drive as WSL mounts them (/mnt/<letter>/...), else "": git and the
    checks crawl there, and its fcntl locks (slots, the heavy line, the phone's) aren't reliable."""
    m = re.match(r"/mnt/([a-zA-Z])(/|$)", str(path))
    return m.group(1).lower() if m else ""


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
    exe = cwds_exe()
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


def cwds_exe():
    """The `lsof` that `cwds` asks without /proc, or None."""
    return shutil.which("lsof") or next((x for x in ("/usr/sbin/lsof", "/usr/bin/lsof") if os.access(x, os.X_OK)), None)


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


def stamp(pid):
    """Which process `pid` is, as text to record now and hand `same_process` later, or None when it's gone. On Linux
    "boot:<boot id>:<start ticks since boot>" (/proc/<pid>/stat field 22): a step of the wall clock (NTP, a clock
    set after resume) moves /proc's btime and so every `started_at`, but not this, and a reboot changes the boot id.
    Elsewhere (macOS: `ps lstart`, no /proc) "wall:<its wall-clock start>", which such a step does move."""
    if not PROC.is_dir():
        t = started_at(pid)
        return None if t is None else f"wall:{t}"
    try:
        ticks = int((PROC / str(pid) / "stat").read_text().rsplit(")", 1)[1].split()[19])
        return f"boot:{(PROC / 'sys/kernel/random/boot_id').read_text().strip()}:{ticks}"
    except (OSError, ValueError, IndexError):
        return None


def same_process(pid, recorded):
    """Whether process `pid` is the one `recorded` (a `stamp`) names, so a number reused since is someone else's;
    None when it can't tell (gone, unreadable). A wall-clock start (macOS's, or written by an older windvane) is
    compared by wall clock, 2 s apart at most: /proc's boot time and ps's are in whole seconds."""
    if isinstance(recorded, str) and recorded.startswith("boot:"):
        now = stamp(pid)
        return None if now is None or not now.startswith("boot:") else now == recorded
    try:
        then = float(recorded.removeprefix("wall:") if isinstance(recorded, str) else recorded)
    except (TypeError, ValueError):
        return None
    now = started_at(pid)
    return None if now is None else abs(now - then) <= 2


def recorded_stamp(mark):
    """The `stamp` a mark file holds: all of a group's mark, the last of a `session` file's five fields (its pid, then
    its owner's pid, start and stamp, then its own). None for an older windvane's (empty, or without it): those go by
    mtime."""
    try:
        fields = mark.read_text(errors="replace").split()
    except OSError:
        return None
    field = (fields[4:5] if mark.name == "session" else fields[:1]) or [""]
    return field[0] if field[0].startswith(("boot:", "wall:")) else None


def clock():
    """Now, on `boot_started`'s clock, or None when /proc can't say: on Linux the start ticks of a thread started for
    the reading, the clock every process's start is on (/proc/uptime isn't: a container's lxcfs fakes it while the
    ticks stay the host's); elsewhere (macOS) the wall clock, as `ps` gives no other."""
    if not PROC.is_dir():
        return time.time()
    out = []
    t = threading.Thread(target=lambda: out.append(task_started(os.getpid(), threading.get_native_id())))
    t.start()
    t.join()
    return out[0] if out else None


def task_started(pid, tid):
    """When thread `tid` of process `pid` started, in seconds of start ticks since boot, or None when it's gone."""
    try:
        stat = (PROC / str(pid) / "task" / str(tid) / "stat").read_text()
        return int(stat.rsplit(")", 1)[1].split()[19]) / os.sysconf("SC_CLK_TCK")
    except (OSError, ValueError, IndexError):
        return None


def boot_started(pid):
    """When process `pid` started, or None when it's gone: on Linux its start ticks since boot (/proc/<pid>/stat field
    22), in seconds, which a step of the wall clock (NTP, a clock set after resume) doesn't move, as it moves /proc's
    btime and so every `started_at`; elsewhere (macOS) its `started_at`, as `ps` gives no other."""
    if not PROC.is_dir():
        return started_at(pid)
    try:
        return int((PROC / str(pid) / "stat").read_text().rsplit(")", 1)[1].split()[19]) / os.sysconf("SC_CLK_TCK")
    except (OSError, ValueError, IndexError):
        return None


def started_after(pid, since):
    """Whether process `pid` started after `since` (on `boot_started`'s clock), False when it's gone. `ps` gives whole
    seconds, rounded down, so there it must have started in a later second than `since`'s: one started in `since`'s
    own second may be older. A whole PS_GRAIN past `since` itself would also pass over one started early in the next
    second when `since` was read mid-second, as a run's start is."""
    t = boot_started(pid)
    return t is not None and t >= (since if PROC.is_dir() else math.floor(since) + PS_GRAIN)


def command(pid):
    """(the command line of process `pid`, whether it has a terminal), or None when it's gone or a zombie. /proc on
    Linux; elsewhere (macOS) `ps`, whose `??` is no terminal."""
    if not PROC.is_dir():
        try:
            out = subprocess.run(["ps", "-o", "tty=,command=", "-p", str(pid)], capture_output=True, text=True,
                                 timeout=PS_WAIT).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            return None
        tty, _, cmd = out.partition(" ")
        return (cmd.strip(), tty not in ("??", "?", "-")) if cmd.strip() else None
    try:
        cmd = (PROC / str(pid) / "cmdline").read_bytes().rstrip(b"\0").replace(b"\0", b" ")
        tty = int((PROC / str(pid) / "stat").read_text().rsplit(")", 1)[1].split()[4])
    except (OSError, ValueError, IndexError):
        return None
    return (os.fsdecode(cmd), tty != 0) if cmd else None
