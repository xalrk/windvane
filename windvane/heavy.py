"""`windvane heavy -- <cmd>`: run a machine-heavy command (engine, render, big build) one at a time across every
project and autopilot slot on this machine, and only when nothing else heavy is running and enough RAM is free.

It holds one machine-wide lock of its own. It never takes another tool's lock (that deadlocks when the command is
that tool, waiting on its own lock); instead it waits until no process matching a `busy` pattern runs. Settings, in
`~/.config/windvane/machine.toml`:

    busy = ["UnrealEditor", "ShaderCompile", "blender"]   # process-name regexes (pgrep) of heavy work started elsewhere
    min_free_gb = 6                                           # wait for this much MemAvailable before starting
    heavy_wait = 60                                           # minutes a command run by hand waits before giving up

A job's own `--mem`, or its project's `heavy_mem`, replaces `min_free_gb` for it. Its project's `heavy_reuses` names
helpers its heavy jobs reuse (an emulator, a build daemon): the memory theirs hold counts as free for it. Free memory
comes from Linux's /proc; elsewhere (macOS) it isn't known, and only `busy` is waited on.

Each job's run time is kept (`times_file`), so a waiter can say how much work is ahead of it and whether it likely
starts after its session's wrap-up; one waiting on memory names the processes that hold the most.
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
# A session's wrap-up is this long before its stop (as the worker prompt tells it): a job likely to start after it is
# one the session should plan around.
WRAP_UP_S = 20 * 60
WRAP_UP = "heavy: likely to start after your wrap-up ({}); still waiting: plan the ticket without it"
# Run times kept: the last few runs of each command, of the commands run most recently.
KEEP_RUNS, KEEP_CMDS = 5, 100


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


def largest(n=3):
    """The `n` processes holding the most memory, as "pid 412 qemu-system-x86 3.4 GB in emu/slot-1" (the last two
    parts of the working directory): a job waiting on memory says who holds it, an idle helper of another project
    plainly among them. Empty without /proc."""
    if not util.PROC.is_dir():
        return []
    rss = [(status_field(int(d), "VmRSS") or 0, int(d)) for d in os.listdir(util.PROC) if d.isdigit()]
    out = []
    for kb, pid in sorted(rss, reverse=True)[:n]:
        try:
            name = (util.PROC / str(pid) / "comm").read_text().strip()
        except OSError:
            name = "?"
        try:
            where = "/".join(Path(os.readlink(util.PROC / str(pid) / "cwd")).parts[-2:])
        except OSError:  # someone else's: its folder can't be read
            where = None
        if kb:
            out.append(f"pid {pid} {name} {kb / 1048576:.1f} GB " + (f"in {where}" if where else "(folder unreadable)"))
    return out


def in_dirs(path, dirs):
    return any(path == d or path.startswith(d.rstrip("/") + "/") for d in dirs)


def in_checkout(path):
    """Whether `path` is inside some git checkout (a `.git` in it or above): a process working there works for that
    project, whichever it is. A dir it can't enter (a container's /root) isn't one it can tell, so isn't one."""
    p = Path(path)
    for d in (p, *p.parents):
        try:
            if d != Path(d.anchor) and (d / ".git").exists():
                return True
        except OSError:
            pass
    return False


def owned(pid, dirs, cwds):
    """Whether process `pid` works for the project in `dirs`: the nearest process up its tree (itself, its parent,
    ...) whose working directory is in the project or in any git checkout decides. A launcher that `cd`s to an SDK
    dir leaves its emulator's cwd outside the project, but the shell that started it is inside; the walk stops at a
    checkout so a shared ancestor (one tmux server for every project) can't claim another project's helper. A helper
    reparented to init when its launcher left is decided by its own cwd alone."""
    seen = set()
    while pid > 1 and pid not in seen:
        seen.add(pid)
        cwd = cwds.get(pid)
        if cwd is not None:
            if in_dirs(cwd, dirs):
                return True
            if in_checkout(cwd):
                return False
        pid = status_field(pid, "PPid") or 0
    return False


def status_field(pid, key):
    """A number from /proc/<pid>/status (`VmRSS` in kB, `PPid`), or None."""
    try:
        m = re.search(rf"^{key}:\s+(\d+)", (util.PROC / str(pid) / "status").read_text(), re.M)
    except OSError:
        return None
    return int(m.group(1)) if m else None


def matches(pid, patterns):
    """Whether process `pid`'s name (as pgrep sees it) or one of its command-line words matches a pattern. A word, not
    the whole line: a JVM's name is `java`, its main class a word of its line; a prompt with spaces in it isn't."""
    try:
        name = (util.PROC / str(pid) / "comm").read_text().strip()
        words = [w for w in os.fsdecode((util.PROC / str(pid) / "cmdline").read_bytes()).split("\0")
                 if w and not any(c.isspace() for c in w)]
    except OSError:
        return False
    return any(re.search(p, w) for p in patterns for w in (name, *words))


def helpers_gb(reuse):
    """GB resident in the long-lived helpers a project's heavy jobs reuse: processes matching its `heavy_reuses`
    patterns that work for it (`owned`). Its heavy jobs count it as free, since they use those very processes
    rather than start their own. 0 when it has none, or without /proc."""
    if not reuse or not reuse[0] or not util.PROC.is_dir():
        return 0
    patterns, dirs = reuse
    mine = {os.getpid(), os.getppid()}
    cwds = util.cwds()
    kb = sum(status_field(pid, "VmRSS") or 0 for pid in cwds
             if pid not in mine and matches(pid, patterns) and owned(pid, dirs, cwds))
    return kb / 1048576


def reuse_of(p):
    """A project's (`heavy_reuses` patterns, the dirs whose processes are its own: every checkout git knows of it, its
    main one and the slots, and its state dir), or None when it names none."""
    if not p.cfg.get("heavy_reuses"):
        return None
    out = util.gout(p.root, "worktree", "list", "--porcelain")
    dirs = {line[9:] for line in out.splitlines() if line.startswith("worktree ")} | {str(p.root), str(p.state)}
    return list(p.cfg["heavy_reuses"]), sorted({os.path.realpath(d) for d in dirs})


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


def short(need, mem, helpers=0):
    """Why a job needing `need` GB can't start with `mem` GB free (and `helpers` GB more in its project's own
    helpers), or None: also when either is unknown (no /proc, or a waiter from an older windvane that didn't say)."""
    if mem is not None and need is not None and mem + helpers < need:
        if helpers:
            return f"{mem:.1f} GB free + {helpers:.1f} GB in this project's helpers (want {need:.10g})"
        return f"only {mem:.1f} GB free (want {need:.10g})"
    return None


def queue_dir():
    return lock_path().with_name("windvane-heavy.queue")


def join_queue(need, key=None, expect=None):
    """An entry in the queue, named by arrival time so they sort in order, and flocked while this job lives. It holds
    the job's memory need, for the waiters behind it to see whether it lets them by, and its `job_key` and own guess
    of its run time (`expect`, minutes), for them to tell how long it runs.

    It's flocked and written under a hidden name first and only then renamed into place, so a scan never sees a live
    entry unlocked or empty. The open file keeps the entry; closing it (or dying) frees it."""
    q = queue_dir()
    q.mkdir(parents=True, exist_ok=True)
    name = f"{time.time_ns():020d}-{os.getpid()}"
    fd = os.open(q / f".{name}", os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    os.write(fd, json.dumps({"mem": need, "key": key, "expect": expect}).encode())
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


def need_of(path):
    """A waiter's memory need in GB, from its queue entry; None when it can't be read (an older windvane's entry)."""
    try:
        need = json.loads(Path(path).read_text() or "{}").get("mem")
    except (OSError, ValueError, AttributeError):
        return None
    return need if isinstance(need, (int, float)) and not isinstance(need, bool) else None


def job_of(path):
    """A waiter's (`job_key`, expected minutes), from its queue entry; Nones when it can't be read."""
    try:
        entry = json.loads(Path(path).read_text() or "{}")
        return entry.get("key"), entry.get("expect")
    except (OSError, ValueError, AttributeError):
        return None, None


def times_file():
    return user_dir("XDG_STATE_HOME", ".local/state") / "heavy-times.json"


def project_dir(cwd):
    """The project a job runs for: the main checkout of the git repo `cwd` is in, which every slot's worktree
    shares, so a job times the same from any slot; else `cwd` itself."""
    common = util.sh(cwd, "git", "rev-parse", "--path-format=absolute", "--git-common-dir")
    if common:
        return str(Path(common).parent) if Path(common).name == ".git" else common
    return str(Path(cwd).resolve())


def job_key(cmd, cwd):
    """What a job's run time is kept under: its project and its command, spaces folded."""
    text = cmd if isinstance(cmd, str) else shlex.join(cmd)
    return f"{project_dir(cwd or os.getcwd())} {' '.join(text.split())}"


def load_times():
    """{job key: its last few run times in minutes, oldest first}; {} when there are none yet."""
    try:
        times = json.loads(times_file().read_text())
    except (OSError, ValueError):
        return {}
    return times if isinstance(times, dict) else {}


def record(key, minutes):
    """Keep a job's run time: the last KEEP_RUNS of each command, for the KEEP_CMDS commands run most recently. Its
    writer holds the heavy lock, so no two write at once; written whole and renamed, so a reader never sees half."""
    times = load_times()
    runs = runs_of(times, key)
    times.pop(key, None)
    times[key] = (runs + [round(minutes, 2)])[-KEEP_RUNS:]
    times = dict(list(times.items())[-KEEP_CMDS:])
    f = times_file()
    try:
        f.parent.mkdir(parents=True, exist_ok=True)
        tmp = f.with_name(f".{f.name}.{os.getpid()}")
        tmp.write_text(json.dumps(times))
        os.replace(tmp, f)
    except OSError:
        pass  # an estimate the next waiter can't make, not a reason to fail the job


def runs_of(times, key):
    """A job's kept run times, skipping anything that isn't one: a hand-edited file isn't a reason to fail a job."""
    runs = times.get(key) if key else None
    if not isinstance(runs, list):
        return []
    return [r for r in runs if isinstance(r, (int, float)) and not isinstance(r, bool)]


def usual(key, times):
    """A job's usual run time in minutes (the median of its last runs), or None for a command never seen."""
    runs = sorted(runs_of(times, key))
    return runs[len(runs) // 2] if runs else None


def run_time(key, times, expect=None):
    """A job's run time in minutes and where it comes from: (its `usual`, "usual"), else the `expect` it was started
    with, (minutes, "expected"); (None, None) for a command never timed and never guessed. Recorded runs win: they
    measured this machine, a guess didn't."""
    if (u := usual(key, times)) is not None:
        return u, "usual"
    if isinstance(expect, (int, float)) and not isinstance(expect, bool) and expect > 0:
        return expect, "expected"
    return None, None


def work_ahead(info, jobs, times, now=None):
    """The heavy work before a job can start, behind the holder `info` and the waiters ahead of it (`jobs`, their
    (key, expect)): (minutes known, how many jobs have no known time, how many jobs, the holder's (minutes, "usual"
    or "expected") when it ran past them). The holder counts its time less what it ran so far; one past it has no
    known end."""
    now = time.time() if now is None else now
    held = info.get("pid") and still_held(info)
    every = ([(info.get("key"), info.get("expect"), now - info.get("since", now))] if held else []) \
        + [(k, e, 0) for k, e in jobs]
    mins, unknown, over = 0, 0, None
    for key, expect, ran in every:
        u, kind = run_time(key, times, expect)
        if u is None or u < ran / 60:
            unknown += 1
            over = over if u is None else (u, kind)
        else:
            mins += u - ran / 60
    return mins, unknown, len(every), over


def minutes(m):
    return f"{m:.0f} min" if m >= 1 else "under 1 min"


def estimate(work, now=None):
    """`work_ahead` as words, "about 25 min of work ahead (until about 01:30)", a lower bound when some job's end
    isn't known (never timed, or a holder past its usual time), "work ahead unknown" when none is; None with nothing
    ahead. A clock time too, since the line is read later."""
    mins, unknown, jobs, over = work
    if not jobs:
        return None
    never = unknown - (over is not None)
    why = []
    if over is not None:
        why.append(f"the holder is past its {over[1]} " + (f"{over[0]:.0f} min" if over[0] >= 1 else "time"))
    if never:
        why.append("never timed" if never == jobs else f"{never} never timed")
    why = "; ".join(why)
    if unknown == jobs or (unknown and mins < 1):
        return f"work ahead unknown ({why})"
    until = time.strftime("%H:%M", time.localtime((time.time() if now is None else now) + mins * 60))
    if unknown:
        return f"at least {minutes(mins)} of work ahead (until {until} or later; {why})"
    return ("under 1 min" if mins < 1 else f"about {minutes(mins)}") + f" of work ahead (until about {until})"


def ahead(entry):
    """The queue entries of the live waiters that arrived before this one, oldest first. Clears dead entries on the
    way (and a waiter's hidden one left by a kill between creating and renaming it). A paused waiter (Ctrl-Z) isn't
    in it: it keeps its entry, but would hold up the whole line until its wait times out; resumed, it gets in line
    again."""
    q, mine, needs = entry[1].parent, entry[1].name, []
    for name in sorted(os.listdir(q)):
        path = q / name
        if name.startswith("."):
            try:
                if time.time() - path.stat().st_mtime > 60:
                    alive(path)
            except OSError:
                pass
        elif name < mine and alive(path) and not paused(entry_pid(name)):
            needs.append(path)
    return needs


def holder(lock):
    """Who holds the lock, as the holder wrote it in: {pid, start, since, cmd, cwd}, or {} (an older windvane)."""
    try:
        os.lseek(lock, 0, os.SEEK_SET)
        return json.loads(os.read(lock, 4096) or b"{}")
    except (OSError, ValueError):
        return {}


def claim(lock, cmd, cwd, key=None, expect=None):
    """Write this job into the lock it now holds, for the waiters' log line."""
    text = cmd if isinstance(cmd, str) else shlex.join(cmd)
    text = " ".join(text.split())
    where = Path(cwd or os.getcwd()).resolve().parts[-2:]
    info = {"pid": os.getpid(), "start": started_at(os.getpid()), "since": time.time(), "cmd": text if len(text) <= 60 else text[:59] + "…",
            "cwd": "/".join(where), "key": key, "expect": expect}
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


def held_by(info, times=None, note=""):
    """The holder, "held by pid 12 (render.sh in game/work, since 01:56)". A clock time, not minutes held: the waiting
    line is printed once and read later, when minutes would be stale. Given `times` (for `status`, read live) it adds
    how long it has run and its usual or expected time, "; running 72 min, usually 40"; a `note` goes inside the same
    parentheses."""
    if not info.get("pid"):
        return "held by another heavy job"
    if not still_held(info):
        return "free"
    at = info.get("since", time.time())
    since = time.strftime("%H:%M", time.localtime(at))
    if times is not None:
        u, kind = run_time(info.get("key"), times, info.get("expect"))
        word = "usually" if kind == "usual" else "expected"
        since += f"; running {minutes((time.time() - at) / 60)}, " + (
            "never timed" if u is None else f"{word} {u:.0f}" if u >= 1 else f"{word} under 1 min")
    return f"held by pid {info['pid']} ({info.get('cmd', '?')} in {info.get('cwd', '?')}, since {since}{note})"


def paused_note(pid, who):
    """Why a stopped (Ctrl-Z) job matters and how to go on, for `status`'s parentheses. Ctrl-Z stops the whole
    process group and the pid is only `windvane heavy`'s own, its command a child in that group, so the way out
    wakes the group (under `bg --heavy` led by the shell around it); the bare pid only if the group can't be read."""
    try:
        target = f"-- -{os.getpgid(pid)}"
    except OSError:
        target = str(pid)
    return f"paused, so {who}: resume it with kill -CONT {target}"


def waiting_line(info, n, est=None):
    """A job about to wait, as said once. With the holder gone it waits only on the jobs ahead, so it says the lock
    is free rather than "waiting for the machine lock: free"."""
    holder = held_by(info)
    lock = "the machine lock is free" if holder == "free" else f"waiting for the machine lock: {holder}"
    return f"heavy: {lock}; " + (f"{n} ahead of this job" if n else "next in line") + (f"; {est}" if est else "")


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


def take(lock, entry, need, cfg, deadline, say, stopped=lambda: False, reuse=None):
    """Wait for this job's turn, its memory and a machine clear of `busy` work, then take the lock. True once held,
    False at the deadline (a function: a moved deadline counts at once) or a stop.

    Only the head of the line (the oldest live waiter) tries the lock, so a job that arrives just as it frees can't
    overtake one that has been waiting (R-007). A head short of memory doesn't take it, and every job behind it would
    wait on that memory too: so the first waiter behind it whose own need is met may go first, while the head stays
    the head and gets the first try once its memory is free. Only memory lets a job by: a waiter ahead whose need is
    met, or unknown, keeps its turn. This job's own project's helpers (`reuse`) count as free for it alone: a waiter
    ahead is judged on what's free for anyone.

    Each time what it waits on changes, the line says so: the work ahead (`estimate`), or for a wait on memory the
    processes holding most. With a session's stop known it says once that the job likely starts after the wrap-up:
    when the known work ahead runs past it, or the wrap-up came with the job still waiting."""
    said, busy_why, retry, warned, looked = None, None, 0, False, 0
    while True:
        line, mem = ahead(entry), free_gb()
        turn = all(short(need_of(w), mem) for w in line)  # the head, or all ahead wait on memory
        mine = short(need, mem, helpers_gb(reuse) if mem is not None else 0) if turn else None
        if turn and not mine and time.time() >= retry:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                busy_why = None
            else:
                b = busy(cfg["busy"])
                if not b:
                    return True
                fcntl.flock(lock, fcntl.LOCK_UN)  # no one runs beside it anyway; it looks again in 10 s
                busy_why, retry = f"'{b}' is running", time.time() + 10
        why = mine or (busy_why if turn else None)
        info = holder(lock)
        n = 0 if turn else len(line)
        key = "memory" if mine else why or (info.get("pid"), info.get("since"), n)
        if key != said or time.time() >= looked + 60:  # the wrap-up is looked at again each minute as time passes
            work, looked = work_ahead(info, [job_of(w) for w in line[:n]], load_times()), time.time()
            if key != said and mine:
                say(f"heavy: waiting: {why}" + (f"; largest: {', '.join(big)}" if (big := largest()) else ""))
            elif key != said:  # reprinted when what it waits on changes, not every second (nor as free memory drifts)
                say(f"heavy: waiting: {why}" if why else waiting_line(info, n, estimate(work)))
            wrap, now = wrap_up(), time.time()
            late = wrap and (now >= wrap or work[1] < work[2] and now + work[0] * 60 > wrap)
            if late and not warned:
                say(WRAP_UP.format(time.strftime("%H:%M", time.localtime(wrap))))
                warned = True
            said = key
        gone = f": {why}" if why else f" lock: {held_by(info)}"
        if stopped():
            say(STOPPED + gone)
            return False
        if time.time() >= deadline():
            say(GAVE_UP + gone)
            return False
        time.sleep(1)


def wrap_up():
    """A session's wrap-up time (`WRAP_UP_S` before its stop, `STOP_AT`), or None outside a session."""
    try:
        return float(os.environ[STOP_AT]) - WRAP_UP_S
    except (KeyError, ValueError):
        return None


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


def run(cmd, wait_s=None, cwd=None, mem=None, reuse=None, expect=None):
    """Run `cmd` under the machine-wide heavy lock, once `mem` GB (default: machine.toml's `min_free_gb`) are free,
    counting the memory of its project's own reused helpers (`reuse_of`) as free. `expect` (minutes) is the job's own
    guess of its run time, for the waiters' estimate until it has recorded times.
    Returns its exit code (TIMED_OUT if the wait timed out, at `deadline`, or as soon as the STOP file named in
    `STOP_ENV` appears while it waits).

    Jobs run in arrival order: each joins a queue when it asks and waits there, keeping its place, until the lock
    is free and the machine is clear for it (`take`)."""
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
    need = cfg["min_free_gb"] if mem is None else mem
    key = job_key(cmd, cwd)
    entry = join_queue(need, key, expect)
    lock = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)  # not truncated on open: the holder's line is in it
    try:
        if not take(lock, entry, need, cfg, until, say, stopped, reuse):
            return TIMED_OUT
        leave_queue(entry)  # the next waiter becomes the head and waits on the lock
        entry = None
        try:
            claim(lock, cmd, cwd, key, expect)
            if (s := round(time.time() - t0)) >= 1:
                say(WAITED.format(s))
            t1 = time.time()
            code = subprocess.run(cmd, cwd=cwd, env=env, shell=isinstance(cmd, str)).returncode
            record(key, (time.time() - t1) / 60)  # still holding the lock: the next job's estimate counts it
            return code
        finally:
            os.ftruncate(lock, 0)
    finally:
        if entry:
            leave_queue(entry)
        os.close(lock)  # frees the lock


def status():
    """Who holds the lock and who waits, in order, without waiting or taking either: the holder's line (it writes it
    in when it takes the lock, and empties it on the way out), then a line per live waiter with the work ahead of it,
    and for a head short of memory the processes holding most."""
    try:
        info = json.loads(lock_path().read_text() or "{}")
    except (OSError, ValueError):
        info = {}
    times = load_times()
    pid = info.get("pid")
    note = "; " + paused_note(pid, "everyone waits") if pid and paused(pid) else ""
    line = held_by(info, times, note) if pid else "free"
    lines, head, jobs = [line], True, []
    q = queue_dir()
    for name in sorted(os.listdir(q)) if q.is_dir() else []:
        pid = entry_pid(name)
        if name.startswith(".") or not pid or not alive(q / name):
            continue
        since = time.strftime("%H:%M", time.localtime(int(name.split("-")[0]) / 1e9))
        line = f"waiting: pid {pid}, since {since}"
        big = []
        if paused(pid):
            line += f" ({paused_note(pid, 'skipped')})"
        else:
            est = estimate(work_ahead(info, jobs, times))
            jobs.append(job_of(q / name))
            if head:
                head, need, mem = False, need_of(q / name), free_gb()
                if short(need, mem):
                    line += f" (waiting for {need:.10g} GB, {mem:.1f} free; letting smaller jobs by)"
                    big = largest()
            line += f"; {est}" if est else ""
        lines.append(line)
        lines += [f"  holds memory: {b}" for b in big]
    return lines


def lives(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:  # someone else's
        pass
    return True
