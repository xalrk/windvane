"""`windvane heavy -- <cmd>`: run a machine-heavy command (engine, render, big build) one at a time across every
project and autopilot slot on this machine, and only when nothing else heavy is running and enough RAM is free.

It holds one machine-wide lock of its own. It never takes another tool's lock (that deadlocks when the command is
that tool, waiting on its own lock); instead it waits until no process matching a `busy` pattern runs. Settings, in
`~/.config/windvane/machine.toml`:

    busy = ["UnrealEditor", "ShaderCompile", "blender"]   # process-name regexes (pgrep) of heavy work started elsewhere
    min_free_gb = 6                                           # wait for this much MemAvailable before starting
    heavy_wait = 60                                           # minutes a command run by hand waits before giving up
    measure_load = 4                                          # a --measure job waits for a 1-minute load this low
    measure_hold = 20                                         # minutes it lets other jobs by before holding the line

A job's own `--mem`, or its project's `heavy_mem`, replaces `min_free_gb` for it. Its project's `heavy_reuses` names
helpers its heavy jobs reuse (an emulator, a build daemon): the memory theirs hold counts as free for it. Free memory
comes from Linux's /proc; elsewhere (macOS) it isn't known, and only `busy` is waited on.

Each job's run time is kept (`times_file`), so a waiter can say how much work is ahead of it and whether it likely
starts after its session's wrap-up; one waiting on memory names the processes that hold the most.

A job that measures timing (`--measure`) also waits for a quiet CPU: a 1-minute load average at or under
`measure_load` (default a quarter of the CPU count), since another project's ordinary work (an emulator, a build
daemon) slows its CPU-side times without being heavy. It lets the jobs behind it by for `measure_hold` minutes, then
holds the line, since each job it lets by adds load of its own; until the load has stayed up that long again with no
heavy job running, when the load isn't the queue's and holding would stall every heavy job for nothing. Measurements
queued together share one hold: one behind a measurement that holds the line holds it at once. Every job's log says the load at its start, end and peak, so a
contended run shows in it. windvane never pauses or signals processes it didn't start to get a quiet CPU.
"""
import fcntl
import json
import math
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
from .runner import outside_sandbox
from .util import same_process, stamp, started_at

# Process names, not command lines: `pgrep -f blender` would match any Claude session whose prompt says "blender".
DEFAULTS = {"busy": ["UnrealEditor", "ShaderCompile", "blender"], "min_free_gb": 6, "heavy_wait": 60, "measure_hold": 20}
# A wait that timed out: the exit code, and the start of the line said just before it (`gave_up` reads it back).
TIMED_OUT = 75
GAVE_UP = "heavy: gave up waiting for the machine"
# A wait cut short by the project's STOP file (its path in this env var, set by the check's runner): its last line.
STOP_ENV, STOPPED = "WINDVANE_HEAVY_STOP", "heavy: stopped while waiting for the machine"
# Said once the job got the machine after waiting, before its own output: a check reads it back (`waited`) for its
# ledger row, so `stats` can tell the minutes a check queued from the minutes it ran.
WAITED = "heavy: waited {} s for the machine"
WAITED_RE = re.compile("^" + re.escape(WAITED).replace(r"\{\}", r"(\d+)") + "$", re.M)
# Said by every job once it has the machine (after WAITED), and after its command ends: how busy the machine was, so
# a timing run made on a contended machine shows as one. The peak is sampled every LOAD_EVERY seconds while it runs.
LOAD_START, LOAD_END, LOAD_EVERY = "heavy: load {} at start", "heavy: load {} at end, {} peak", 5
LOAD_START_RE = re.compile("^" + re.escape(LOAD_START).replace(r"\{\}", r"\S+") + "$", re.M)
NO_LOAD = "heavy: no load average on this machine: --measure doesn't wait for a quiet CPU"
# Said once in a measurement's wait on load, as DEFAULT_NEED is for memory: where its limit comes from.
LOAD_LIMIT = ("the {} is measure_load in {} (default: a quarter of the CPU count): if this machine is never that "
              "quiet, raise it there with a line measure_load = <n>")
# Said once by a measurement that has waited on load for machine.toml's `measure_hold` minutes: the jobs it let by
# kept the load up, so from now on they wait behind it.
HOLDING = "heavy: holding the line for a quiet CPU after {} min"
# Said once by a measurement that holds the line at once because the one ahead of it did (in line, or as the lock's
# holder): measurements queued together let the jobs behind them by for one `measure_hold` in all, not one each.
HOLDING_TOO = "holding the line too, as pid {} did: measurements queued together share one measure_hold"
# Said once by a measurement that held the line for `measure_hold` minutes more with no heavy job running and the load
# still up: the load isn't the queue's, so holding it up would only stall every heavy job on the machine.
LET_GO = "heavy: the load stayed up {} min with no heavy job running: letting other jobs by again"
BAD_LIMIT = "heavy: machine.toml's measure_load isn't a number: waiting for the default, {}"
# A wait on memory with no need of its own (no `--mem`, no `heavy_mem`): the line says once where the need comes from,
# since a small job may sit for an hour behind a default sized for a big build.
DEFAULT_NEED = ("the {} GB is min_free_gb in {}: if this job needs less, give it --mem <GB> "
                "(or set heavy_mem in .windvane/config.toml)")
# A session's stop, as a Unix time: the driver sets it for each session (its `bg` jobs and checks inherit it) and for
# its own checks. A job waits for the machine until then, not `heavy_wait`.
STOP_AT = "WINDVANE_STOP_AT"
# The driver's check also follows the run's deadline file (its path in this env var), read as it waits: a deadline the
# user moved earlier ends the wait then, not at the end it had when the check started.
DEADLINE_ENV = "WINDVANE_HEAVY_DEADLINE"
# A `bg` job's files (their base path), which `bg` sets for its command: a wait for the machine writes it into its queue
# entry, so `await` on the job finds that entry and says where it stands in line now (`standing`).
JOB_ENV = "WINDVANE_BG_BASE"
# A session's wrap-up is this long before its stop (as the worker prompt tells it): a job likely to start after it is
# one the session should plan around.
WRAP_UP_S = 20 * 60
WRAP_UP = "heavy: likely to start after your wrap-up ({}); still waiting: plan the ticket without it"
# Run times kept: the last few runs of each command, of the commands run most recently.
KEEP_RUNS, KEEP_CMDS = 5, 100


def machine_file():
    return user_dir("XDG_CONFIG_HOME", ".config") / "machine.toml"


def default_load():
    """`measure_load` when machine.toml doesn't set it: a quarter of the CPUs busy leaves a measurement its own."""
    return 0.25 * (os.cpu_count() or 1)


def machine():
    base = {**DEFAULTS, "measure_load": default_load()}
    try:
        return {**base, **tomllib.loads(machine_file().read_text())}
    except (OSError, ValueError):
        return base


def lock_path():
    return Path(os.environ.get("XDG_RUNTIME_DIR") or "/tmp") / "windvane-heavy.lock"


def free_gb():
    try:
        m = re.search(r"^MemAvailable:\s+(\d+) kB", (util.PROC / "meminfo").read_text(), re.M)
        return int(m.group(1)) / 1048576 if m else None
    except OSError:
        return None


def load_avg():
    """The 1-minute load average: from /proc (`util.PROC`), else `os.getloadavg`, else None (no way to tell)."""
    try:
        return float((util.PROC / "loadavg").read_text().split()[0])
    except (OSError, ValueError, IndexError):
        pass
    try:
        return os.getloadavg()[0]
    except (OSError, AttributeError):
        return None


def loadf(x):
    """A load average as said: two decimals at most, "9.5" or "4.04", so one just over the limit never reads as it."""
    return f"{x:.2f}".rstrip("0").rstrip(".")


def too_busy(limit, load):
    """Why a measurement wanting a load of at most `limit` can't start at `load`, or None: also when either is unknown
    (no load source, a waiter that isn't a measurement or is from an older windvane)."""
    if isinstance(limit, (int, float)) and not isinstance(limit, bool) and load is not None and load > limit:
        return f"load {loadf(load)} (want ≤ {limit:.10g})"
    return None


def cpu_times():
    """{pid: CPU seconds used so far (user + system)} from /proc's stat files; {} without /proc."""
    out, tick = {}, os.sysconf("SC_CLK_TCK")
    for d in os.listdir(util.PROC) if util.PROC.is_dir() else []:
        if d.isdigit():
            try:
                f = (util.PROC / d / "stat").read_text().rsplit(")", 1)[1].split()
                out[int(d)] = (int(f[11]) + int(f[12])) / tick
            except (OSError, IndexError, ValueError):
                pass
    return out


def busiest(n=3, gap=1):
    """The `n` processes using the most CPU over `gap` seconds (two samples), as "pid 412 qemu-system-x86 310% in
    emu/slot-1" (% of one CPU, as `top` says it): a measurement waiting on load says who makes it. Empty without
    /proc."""
    first = cpu_times()
    if not first:
        return []
    t0 = time.monotonic()
    time.sleep(gap)
    took = max(time.monotonic() - t0, 0.01)
    used = sorted(((t - first[pid], pid) for pid, t in cpu_times().items() if pid in first), reverse=True)
    return [f"pid {pid} {comm(pid)} {s / took * 100:.0f}% " + (f"in {w}" if (w := folder(pid)) else "(folder unreadable)")
            for s, pid in used[:n] if s > 0]


def comm(pid):
    try:
        return (util.PROC / str(pid) / "comm").read_text().strip()
    except OSError:
        return "?"


def folder(pid):
    """The last two parts of process `pid`'s working directory, or None when it can't be read (someone else's)."""
    try:
        return "/".join(Path(os.readlink(util.PROC / str(pid) / "cwd")).parts[-2:])
    except OSError:
        return None


def holders(n=3):
    """The `n` processes holding the most memory, as (pid, name, GB, the last two parts of the working directory, or
    None when it can't be read). Empty without /proc."""
    if not util.PROC.is_dir():
        return []
    rss = [(status_field(int(d), "VmRSS") or 0, int(d)) for d in os.listdir(util.PROC) if d.isdigit()]
    return [(pid, comm(pid), kb / 1048576, folder(pid)) for kb, pid in sorted(rss, reverse=True)[:n] if kb]


def largest(n=3, big=None):
    """The `n` processes holding the most memory (or `big`, from `holders`), as "pid 412 qemu-system-x86 3.4 GB in
    emu/slot-1": a job waiting on memory says who holds it, an idle helper of another project plainly among them."""
    return [f"pid {pid} {name} {gb:.1f} GB " + (f"in {where}" if where else "(folder unreadable)")
            for pid, name, gb, where in (holders(n) if big is None else big)]


HINT_MIN_GB = 1  # a holder smaller than this frees too little to be worth a config line


def unreused(reuse, big, skip=()):
    """The holders in `big` (from `holders`) that work for the project in `reuse` (`owned`) but match none of its
    `heavy_reuses` patterns: a helper its heavy jobs may reuse yet count as taken. Not one this job runs under (its
    session), one under a pid in `skip` (the lock's holder: that job's memory is in use), or one under `HINT_MIN_GB`."""
    if not reuse or not util.PROC.is_dir():
        return []
    patterns, dirs = reuse
    cwds = util.cwds()
    mine = set(ancestors(os.getpid()))
    return [h for h in big if h[2] >= HINT_MIN_GB and h[0] not in mine
            and not set(skip) & set(ancestors(h[0])) and owned(h[0], dirs, cwds) and not matches(h[0], patterns)]


def ancestors(pid):
    """`pid` and the processes up its tree, to init."""
    out = []
    while pid > 1 and pid not in out:
        out.append(pid)
        pid = status_field(pid, "PPid") or 0
    return out


OWN = "is this project's own: if its heavy jobs reuse it, add"


def reuse_hint(proc):
    """The waiting line's clause for a holder from `unreused`."""
    pid, name, gb, _ = proc
    word = reuse_word(pid, name)
    label = name if word == name else f"{name} {word}"
    return f'{label} ({gb:.1f} GB) {OWN} "{word}" to heavy_reuses in .windvane/config.toml'


MAIN_CLASS = re.compile(r"(?:[A-Za-z_][\w$]*\.)+([A-Z][\w$]*)")


def reuse_word(pid, name):
    """The word to put in `heavy_reuses` for process `pid` named `name`: its name, but for a JVM the last part of its
    main class (`GradleDaemon`, `KotlinCompileDaemon`), since every JVM is named `java` and "java" would count each of
    the project's JVMs as a helper."""
    if name != "java":
        return name
    try:
        words = os.fsdecode((util.PROC / str(pid) / "cmdline").read_bytes()).split("\0")
    except OSError:
        return name
    return next((m.group(1) for w in words[1:] if (m := MAIN_CLASS.fullmatch(w))), name)


def hints_in(log, head=65536):
    """The `reuse_hint` clauses a heavy job whose output went to `log` said while it waited (from the log's head, as
    `waited`), for the project log: the check's own log is gone by the next run."""
    return [h for line in re.findall(r"^heavy: waiting: .*$", log_head(log, head), re.M)
            for h in re.findall(r"; ([^;]* " + re.escape(OWN) + r" [^;]*)", line)]


NEED_RE = re.compile(re.escape(DEFAULT_NEED).replace(r"\{\}", r"(\S+)", 1).replace(r"\{\}", ".+?"))  # its figure
WORD_RE = re.compile(r'"([^"]+)" to heavy_reuses')


def memory_hints(log, head=65536):
    """(the `heavy_reuses` words, in order, the GB of the default need it waited for or None) that a heavy job whose
    output went to `log` was told while it waited on memory: settings only the user can make, so the run's summary
    names them."""
    lines = re.findall(r"^heavy: waiting: .*$", log_head(log, head), re.M)
    words = [w for line in lines for w in WORD_RE.findall(line)]
    need = next((m.group(1) for line in lines if (m := NEED_RE.search(line))), None)
    return list(dict.fromkeys(words)), need


def span(seconds):
    """A wait as words, "under 1 min", "40 min" or "2 h 10 min": a run's waits add up to hours."""
    m = round(seconds / 60)
    return minutes(seconds / 60) if m < 60 else f"{m // 60} h" + (f" {m % 60} min" if m % 60 else "")


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
    checkout so a shared ancestor (one tmux server for every project) can't claim another project's helper. A walk
    that reaches no checkout (a build daemon reparented to init, working in its own dir under the home) is decided by
    the dir the process was started from (`started_in`)."""
    seen, first = set(), pid
    while pid > 1 and pid not in seen:
        seen.add(pid)
        cwd = cwds.get(pid)
        if cwd is not None:
            if in_dirs(cwd, dirs):
                return True
            if in_checkout(cwd):
                return False
        pid = status_field(pid, "PPid") or 0
    start = started_in(first)
    return start is not None and in_dirs(start, dirs)


def started_in(pid):
    """The `PWD` in process `pid`'s environment, or None when it has none or it can't be read (another user's). A
    daemon keeps the environment of the build that started it, so this is the checkout where `./gradlew` ran, though
    the daemon itself works in `~/.gradle/daemon/<version>`."""
    try:
        env = (util.PROC / str(pid) / "environ").read_bytes()
    except OSError:
        return None
    for var in env.split(b"\0"):
        if var.startswith(b"PWD="):
            return os.path.realpath(var[4:].decode(errors="replace"))
    return None


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
    main one and the slots, and its state dir). The dirs even with no patterns: a job waiting on memory its own
    helper holds says to name it (`unreused`)."""
    out = util.gout(p.root, "worktree", "list", "--porcelain")
    dirs = {line[9:] for line in out.splitlines() if line.startswith("worktree ")} | {str(p.root), str(p.state)}
    return list(p.cfg.get("heavy_reuses") or []), sorted({os.path.realpath(d) for d in dirs})


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
            return f"{down(mem)} GB free + {down(helpers)} GB in this project's helpers (want {need:.10g})"
        return f"only {down(mem)} GB free (want {need:.10g})"
    return None


def down(gb):
    """`gb` to one decimal, rounded down: a job short of memory never reads as having enough ("5.9 GB free (want 6)",
    not "6.0"). Within half a kB (meminfo's unit) of a tenth is that tenth: 4.2 GB in whole kB is a hair under it."""
    return f"{math.floor(gb * 10 + 5 / 1048576) / 10:.1f}"


def queue_dir():
    return lock_path().with_name("windvane-heavy.queue")


def join_queue(need, key=None, expect=None, load=None):
    """An entry in the queue, named by arrival time so they sort in order, and flocked while this job lives. It holds
    the job's memory need and a measurement's load limit (`load`), for the waiters behind it to see whether it lets
    them by, and its `job_key` and own guess of its run time (`expect`, minutes), for them to tell how long it runs.

    It's flocked and written under a hidden name first and only then renamed into place, so a scan never sees a live
    entry unlocked or empty. The open file keeps the entry; closing it (or dying) frees it."""
    q = queue_dir()
    q.mkdir(parents=True, exist_ok=True)
    name = f"{time.time_ns():020d}-{os.getpid()}"
    fd = os.open(q / f".{name}", os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    job = os.environ.get(JOB_ENV)
    os.write(fd, json.dumps({"mem": need, "key": key, "expect": expect, "load": load, "job": job}).encode())
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
    return entry_number(path, "mem")


def limit_of(path):
    """A measurement's load limit, from its queue entry; None for a job that isn't one."""
    return entry_number(path, "load")


def entry_number(path, field):
    try:
        n = json.loads(Path(path).read_text() or "{}").get(field)
    except (OSError, ValueError, AttributeError):
        return None
    return n if isinstance(n, (int, float)) and not isinstance(n, bool) else None


def held_up(path, mem, load):
    """Why the waiter whose queue entry is `path` lets the jobs behind it by, with `mem` GB free and a load of `load`:
    short of memory (`short`) or, for a measurement not yet holding the line (`holds`), of a quiet CPU (`too_busy`);
    None when it can start, or holds the line."""
    return short(need_of(path), mem) or (None if holds(path) else too_busy(limit_of(path), load))


def holds(path):
    """Whether the measurement whose queue entry is `path` has waited on load long enough to hold the line."""
    try:
        return json.loads(Path(path).read_text() or "{}").get("hold") is True
    except (OSError, ValueError, AttributeError):
        return False


def hold_line(entry, on=True):
    """Mark this waiter's queue entry as holding the line, or (`on` false) no longer. Written over the old text in one
    go, never shorter than it ("false" is longer than "true"), so a scan reads the old entry or the new one (or, at
    worst, nothing: a waiter that can't be read is waited on)."""
    fd, path = entry
    os.pwrite(fd, json.dumps({**json.loads(path.read_text()), "hold": on}).encode(), 0)


def holding(path, load, hold):
    """Why the jobs behind the measurement whose entry is `path` wait on it, and for how long at most: it holds the
    line and the load is still too high for it; else None."""
    hot = holds(path) and too_busy(limit_of(path), load)
    return hot and (f"pid {entry_pid(path.name)}, a --measure job, holds the line for a quiet CPU: {hot}; "
                    f"it lets jobs by again once the load stays up {hold:.10g} min with no heavy job running "
                    f"(measure_hold in {machine_file()})")


def hold_minutes(cfg):
    """machine.toml's `measure_hold`, or its default when it isn't a number."""
    m = cfg.get("measure_hold")
    return m if isinstance(m, (int, float)) and not isinstance(m, bool) and m >= 0 else DEFAULTS["measure_hold"]


def bg_job_of(path):
    """The `bg` job a waiter waits for (its base path, `JOB_ENV`), from its queue entry; None for one run by hand."""
    try:
        return json.loads(Path(path).read_text() or "{}").get("job")
    except (OSError, ValueError, AttributeError):
        return None


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
    if (end := until_of(work, now)) is None:
        return f"work ahead unknown ({why})"
    until = clock(end)
    if unknown:
        return f"at least {minutes(mins)} of work ahead (until {until} or later; {why})"
    return ("under 1 min" if mins < 1 else f"about {minutes(mins)}") + f" of work ahead (until about {until})"


def until_of(work, now=None):
    """When the `work_ahead` should be done, as a Unix time: None when `estimate` gives no clock time for it."""
    mins, unknown, jobs, _ = work
    if not jobs or unknown == jobs or (unknown and mins < 1):
        return None
    return (time.time() if now is None else now) + mins * 60


def clock(t):
    return time.strftime("%H:%M", time.localtime(t))


OVERDUE = "overdue since {}"
UNTIL_RE = re.compile(r"\(until (?:about )?(\d\d):(\d\d)(?: or later)?(;[^)]*)?\)")


def overdue(text, now=None):
    """`text` (a wait line said earlier) with each estimate's "until" that has passed by `now` said as overdue,
    "(until about 03:57)" as "(overdue since 03:57)": a line read an hour later mustn't read as a time to come. Its
    clock time is taken within 12 hours of now, the way an estimate's end is read."""
    now = time.time() if now is None else now

    def mark(m):
        at = datetime.fromtimestamp(now).replace(hour=int(m.group(1)), minute=int(m.group(2)), second=0).timestamp()
        at += 86400 * ((now - at > 43200) - (at - now > 43200))
        return f"({OVERDUE.format(m.group(1) + ':' + m.group(2))}{m.group(3) or ''})" if now >= at + 60 else m.group(0)
    return UNTIL_RE.sub(mark, text)


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


def claim(lock, cmd, cwd, key=None, expect=None, measure=None, held=False):
    """Write this job into the lock it now holds, for the waiters' log line, and whether it held the line as it
    waited (`held`), for a measurement that then becomes the head to hold it at once (`take`)."""
    text = cmd if isinstance(cmd, str) else shlex.join(cmd)
    text = " ".join(text.split())
    where = Path(cwd or os.getcwd()).resolve().parts[-2:]
    info = {"pid": os.getpid(), "start": started_at(os.getpid()), "stamp": stamp(os.getpid()), "since": time.time(), "cmd": text if len(text) <= 60 else text[:59] + "…",
            "cwd": "/".join(where), "key": key, "expect": expect, "measure": measure, "held": held}
    os.ftruncate(lock, 0)
    os.lseek(lock, 0, os.SEEK_SET)
    os.write(lock, json.dumps(info).encode())


def still_held(info):
    """Whether the holder named in the lock file still runs. A killed one leaves its line, and its pid may be reused
    by some other process later, which `same_process` tells from it: by its `stamp`, or by the wall-clock `start`
    in a line an older windvane wrote (which also reads `start`, so it stays). A line without either, or a start
    that can't be read, goes by the pid alone."""
    if not lives(info["pid"]):
        return False
    known = info.get("stamp") or info.get("start")
    return known is None or same_process(info["pid"], known) is not False


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
    wakes the group (under `bg --heavy` led by the shell around it); the bare pid only if the group can't be read.
    No `--` before the group: dash's and busybox's `kill` reject it, and every shell's `kill` (and fish's
    /usr/bin/kill) reads a negative number after the signal as a group."""
    try:
        target = f"-{os.getpgid(pid)}"
    except OSError:
        target = str(pid)
    return f"paused, so {who}: resume it with kill -CONT {target}"


def waiting_line(info, n, est=None, past=None):
    """A job about to wait, as said once. With the holder gone it waits only on the jobs ahead, so it says the lock
    is free rather than "waiting for the machine lock: free". Said again once the last line's estimate ran out
    (`past`, its end), it says that estimate is overdue."""
    holder = held_by(info)
    lock = "the machine lock is free" if holder == "free" else f"waiting for the machine lock: {holder}"
    return f"heavy: {lock}; " + (f"{n} ahead of this job" if n else "next in line") + (f"; {est}" if est else "") \
        + (f"; the last estimate is {OVERDUE.format(clock(past))}" if past else "")


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


def log_head(log, head=65536):
    """The first `head` bytes of a heavy job's log ("" when it can't be read): `take`'s lines and `run`'s WAITED line
    come before the command's own output."""
    try:
        with open(log, "rb") as f:
            return f.read(head).decode(errors="replace")
    except OSError:
        return ""


def waited(log, head=65536):
    """The seconds a heavy job whose output went to `log` waited before it ran, from `run`'s WAITED line (0 when it
    didn't wait)."""
    m = WAITED_RE.search(log_head(log, head))
    return int(m.group(1)) if m else 0


def waiting(log, head=65536):
    """Whether a heavy job whose output went to `log` is still waiting for the machine: `take` said why (always its
    first line) and `run` hasn't said WAITED or the load at its start yet. A job that got the machine at once says
    only the latter."""
    text = log_head(log, head)
    return text.startswith("heavy: ") and not WAITED_RE.search(text) and not LOAD_START_RE.search(text)


def lock_free(lock):
    """Whether no heavy job holds the lock now: tried and let go at once, as the head does when `busy` work runs."""
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    fcntl.flock(lock, fcntl.LOCK_UN)
    return True


def take(lock, entry, need, cfg, deadline, say, stopped=lambda: False, reuse=None, default=False, measure=None):
    """Wait for this job's turn, its memory, a machine clear of `busy` work and, for a measurement, a load average at
    or under `measure`, then take the lock. True once held, False at the deadline (a function: a moved deadline counts
    at once) or a stop.

    Only the head of the line (the oldest live waiter) tries the lock, so a job that arrives just as it frees can't
    overtake one that has been waiting (R-007). A head short of memory doesn't take it, and every job behind it would
    wait on that memory too: so the first waiter behind it whose own need is met may go first, while the head stays
    the head and gets the first try once its memory is free. A measurement short of a quiet CPU lets them by the same
    way, so it doesn't hold the whole line for hours, and gets the first try once the load is down; but only for
    machine.toml's `measure_hold` minutes of waiting on load: each job it lets by adds load, so on a busy day the
    load would never drop. Then it holds the line (`hold_line`): the jobs behind it wait, saying who holds them up,
    and it starts once the running job ends and the load is down. If the load stays up `measure_hold` minutes more
    with the lock free, no heavy job keeps it up: it lets them by again (`LET_GO`), for the rest of its wait.
    Measurements queued together share that one hold: a mark the first holder leaves (its entry's `hold`, then the
    lock's `held` once it runs) makes the measurement behind it hold the line at once (`HOLDING_TOO`) rather than
    start a `measure_hold` of its own, so the jobs behind them are let by for one `measure_hold` in all; `LET_GO` still
    applies to each. A mark dies with its job: a killed one's entry is gone and its lock free. Only these let a job by: a waiter ahead whose need is met, or unknown, keeps its turn. This job's own project's helpers (`reuse`) count as free for it alone: a waiter
    ahead is judged on what's free for anyone.

    Each time what it waits on changes, the line says so: the work ahead (`estimate`), or for a wait on memory the
    processes holding most, and once a wait, which of those are this project's own helpers not in its
    `heavy_reuses` (`unreused`); for a wait on load the processes using the most CPU (`busiest`). With a session's stop known it says once that the job likely starts after the wrap-up:
    when the known work ahead runs past it, or the wrap-up came with the job still waiting. A `default` need (none of
    its own) is named as such once, with how to lower it (`DEFAULT_NEED`)."""
    said, busy_why, retry, warned, looked, hinted, told = None, None, 0, False, 0, False, not default
    limit_told, hot_since, free_since, hold = False, None, None, hold_minutes(cfg)
    holding_it = let_go = False
    too = due = None
    while True:
        line, mem, load = ahead(entry), free_gb(), load_avg()
        turn = all(held_up(w, mem, load) for w in line)  # the head, or all ahead wait on memory or a quiet CPU
        low = short(need, mem, helpers_gb(reuse) if mem is not None else 0) if turn else None
        hot = too_busy(measure, load) if turn and not low else None
        mine = low or hot
        info = holder(lock)
        lead = None if turn else next((w for w in line if not held_up(w, mem, load)), None)
        held = lead and holding(lead, load, hold)
        hot_since = hot_since or (time.time() if hot else None)  # its wait on load, from its first moment
        if hot_since and not holding_it and not let_go and time.time() - hot_since >= hold * 60:
            hold_line(entry)
            say(HOLDING.format(f"{hold:.10g}"))
            holding_it = True
        # The pid of a measurement ahead that holds the line, in it or (`held`, from `claim`) as the lock's holder: this
        # one holds it at once too, so the jobs behind a group of measurements are let by for one measure_hold in all.
        by = measure is not None and not holding_it and not let_go and (
            entry_pid(lead.name) if lead and holds(lead) else
            turn and info.get("held") and info.get("pid") and still_held(info) and info["pid"])
        if by:
            hold_line(entry)
            holding_it, too = True, HOLDING_TOO.format(by)
        # Holding the line, it times the load staying up with the lock free: no heavy job is what keeps the CPU busy.
        # Not only as the head: one holding behind a holder lets go about when that one does.
        cpu = hot if turn else holding_it and too_busy(measure, load)
        free_since = (free_since or time.time()) if holding_it and cpu and lock_free(lock) else None
        if free_since and time.time() - free_since >= hold * 60:
            hold_line(entry, False)
            say(LET_GO.format(f"{hold:.10g}"))
            holding_it, let_go = False, True
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
        why = mine or (busy_why if turn else held)
        n = 0 if turn else len(line)
        key = "memory" if low else "load" if hot else f"held by {lead.name}" if held else why or (
            info.get("pid"), info.get("since"), n)
        # A holder gone, the next yet to claim it (it looks once a second), or the measurement it waited behind taking
        # the lock and yet to write itself in: an empty line for that moment says nothing new.
        if key[:1] == (None,) and (isinstance(said, tuple) and said[2] == n or str(said).startswith("held by ")):
            key = said
        if too and key == said:
            say("heavy: " + too)
            too = None
        if key != said or time.time() >= looked + 60:  # the wrap-up is looked at again each minute as time passes
            work, looked = work_ahead(info, [job_of(w) for w in line[:n]], load_times()), time.time()
            past = due if key == said and due is not None and looked >= due + 60 else None  # its end came and went
            extra, too = f"; {too}" if too else "", None  # said on the wait line it changes, not a line of its own
            if key != said and low:
                big = holders()
                own = [] if hinted else unreused(reuse, big, [info["pid"]] if info.get("pid") else [])
                hinted = hinted or bool(own)
                say(f"heavy: waiting: {why}" + (f"; largest: {', '.join(largest(big=big))}" if big else "")
                    + "".join(f"; {reuse_hint(h)}" for i, h in enumerate(own)  # a word once: the largest of it
                            if reuse_word(h[0], h[1]) not in {reuse_word(g[0], g[1]) for g in own[:i]})
                    + ("" if told else "; " + DEFAULT_NEED.format(f"{need:.10g}", machine_file())) + extra)
                told = True
            elif key != said and hot:  # not reprinted as the load drifts: it says what it waits on, not how much
                cpu = busiest()
                say(f"heavy: waiting: {why}" + (f"; busiest: {', '.join(cpu)}" if cpu else "")
                    + ("" if limit_told else "; " + LOAD_LIMIT.format(f"≤ {measure:.10g}", machine_file())) + extra)
                limit_told = True
            elif key != said or past:  # reprinted when what it waits on changes or its estimate ran out, not every
                # second (nor as free memory drifts)
                say((f"heavy: waiting: {why}" if why else waiting_line(info, n, estimate(work), past)) + extra)
            if key != said or past:
                due = None if why else until_of(work)
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


def run(cmd, wait_s=None, cwd=None, mem=None, reuse=None, expect=None, measure=False):
    """Run `cmd` under the machine-wide heavy lock, once `mem` GB (default: machine.toml's `min_free_gb`) are free,
    counting the memory of its project's own reused helpers (`reuse_of`) as free, and for a `measure` job once the
    load is at or under machine.toml's `measure_load`. `expect` (minutes) is the job's own guess of its run time, for
    the waiters' estimate until it has recorded times.
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
    outside_sandbox("the heavy lock")  # its $XDG_RUNTIME_DIR is read-only, and its pid namespace hides the holder
    need = cfg["min_free_gb"] if mem is None else mem
    limit = cfg["measure_load"] if measure else None
    if limit is not None and (not isinstance(limit, (int, float)) or isinstance(limit, bool)):
        limit = default_load()
        say(BAD_LIMIT.format(f"{limit:.10g}"))
    if limit is not None and load_avg() is None:
        say(NO_LOAD)
        limit = None
    key = job_key(cmd, cwd)
    entry = join_queue(need, key, expect, limit)
    lock = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)  # not truncated on open: the holder's line is in it
    try:
        if not take(lock, entry, need, cfg, until, say, stopped, reuse, default=mem is None, measure=limit):
            return TIMED_OUT
        held = holds(entry[1])
        leave_queue(entry)  # the next waiter becomes the head and waits on the lock
        entry = None
        try:
            claim(lock, cmd, cwd, key, expect, limit, held)
            if (s := round(time.time() - t0)) >= 1:
                say(WAITED.format(s))
            t1 = time.time()
            code = watch_load(lambda: subprocess.Popen(cmd, cwd=cwd, env=env, shell=isinstance(cmd, str)), say)
            record(key, (time.time() - t1) / 60)  # still holding the lock: the next job's estimate counts it
            return code
        finally:
            os.ftruncate(lock, 0)
    finally:
        if entry:
            leave_queue(entry)
        os.close(lock)  # frees the lock


def watch_load(start_job, say):
    """Start the job (`start_job` gives its process) and wait for it, saying the load at its start, end and peak
    (sampled every LOAD_EVERY seconds from here, not by a thread in the command). The start is said before the job
    runs, so it comes before the job's own output. Its exit code."""
    start = peak = load_avg()
    say(LOAD_START.format("unknown" if start is None else loadf(start)))
    proc = start_job()
    try:
        while True:
            try:
                code = proc.wait(timeout=LOAD_EVERY)
                break
            except subprocess.TimeoutExpired:
                if (now := load_avg()) is not None:
                    peak = now if peak is None else max(peak, now)
    except BaseException:  # as subprocess.run: an interrupted wait doesn't leave the command running
        proc.kill()
        proc.wait()
        raise
    if (end := load_avg()) is not None:
        say(LOAD_END.format(loadf(end), loadf(end if peak is None else max(peak, end))))
    return code


def status(reuse=None):
    """Who holds the lock and who waits, in order, without waiting or taking either: the holder's line (it writes it
    in when it takes the lock, and empties it on the way out), then a line per live waiter with what it waits on and
    the work ahead of it (`queue_view`), and for the first one short of memory or a quiet CPU the processes holding
    most: run from a project (`reuse`), its own helpers not in its `heavy_reuses` are marked."""
    outside_sandbox("the heavy lock")  # its own pid namespace hides the holder and waiters: it would say `free`
    info, times = lock_info(), load_times()
    lines = [holder_line(info, times)]
    for w in queue_view(info, times, reuse):
        lines.append(f"waiting: pid {w['pid']}, since {w['since']}" + (", measuring" if w["measuring"] else "")
                     + (f" ({w['why']})" if w["why"] else "") + (f"; {w['est']}" if w["est"] else ""))
        lines += [f"  {b}" for b in w["big"]]
    return lines


def standing(job, reuse=None):
    """Where the `bg` job `job` (its base path) stands in the queue now, as `status` reads it, for `await`: the
    holder's line and this job's (its place in line, what it waits on, the work ahead), or [] when no waiter in the
    queue is that job's (it just took the machine, or an older windvane queued it)."""
    info, times = lock_info(), load_times()
    w = next((w for w in queue_view(info, times, reuse, big=False) if w["job"] == job), None)
    if w is None:
        return []
    holder = holder_line(info, times)
    where = w["why"] if w["place"] is None else w["place"] + (f" ({w['why']})" if w["why"] else "")  # paused
    return ["now: the machine lock is " + holder,
            f"now: this job (pid {w['pid']}, queued at {w['since']}) is {where}" + (f"; {w['est']}" if w["est"] else "")]


def lock_info():
    """The holder's line in the lock, read without opening it for writing: {} when free or unreadable."""
    try:
        return json.loads(lock_path().read_text() or "{}")
    except (OSError, ValueError):
        return {}


def holder_line(info, times):
    """The holder, read live (`held_by` with its run time), marked when it's paused or measuring; "free"."""
    pid = info.get("pid")
    if not pid:
        return "free"
    note = "; " + paused_note(pid, "everyone waits") if paused(pid) else ""
    note += "; measuring" if info.get("measure") is not None else ""
    return held_by(info, times, note)


def own_job(path, reuse):
    """Whether the waiter whose queue entry is `path` runs for the project `reuse` is of: its `job_key` starts with
    one of that project's dirs."""
    key = job_of(path)[0]
    return bool(reuse and isinstance(key, str) and any(key.startswith(d + " ") for d in reuse[1]))


def queue_view(info, times, reuse=None, big=True):
    """Each live waiter, oldest first, as {pid, job, since, measuring, place, why, est, big}, judged the way its own
    wait (`take`) judges it, so what `status` and `await` say is what the waiter does: it's next in line when every
    live waiter ahead lets it by (`held_up`), and then waits on its own memory (this project's helpers, `reuse`,
    counting as free for this project's jobs alone) or a quiet CPU; else on the jobs ahead, or a measurement ahead
    holding the line. Its work ahead counts the jobs it waits behind. The first waiter short of memory or a quiet
    CPU lists the processes holding most (`big`: false skips that look, which takes a second)."""
    q, out, line, listed = queue_dir(), [], [], not big
    mem, load, hold = free_gb(), load_avg(), hold_minutes(machine())
    helpers = None
    for name in sorted(os.listdir(q)) if q.is_dir() else []:
        path, pid = q / name, entry_pid(name)
        if name.startswith(".") or not pid or not alive(path):
            continue
        w = {"pid": pid, "job": bg_job_of(path), "since": clock(int(name.split("-")[0]) / 1e9),
             "measuring": limit_of(path) is not None, "place": None, "why": None, "est": None, "big": []}
        out.append(w)
        if paused(pid):
            w["why"] = paused_note(pid, "skipped")
            continue
        turn = all(held_up(a, mem, load) for a in line)
        n = 0 if turn else len(line)
        w["place"] = f"{n} ahead of it" if n else "next in line"
        w["est"] = estimate(work_ahead(info, [job_of(a) for a in line[:n]], times))
        need, limit = need_of(path), limit_of(path)
        if turn:
            mine = 0
            if own_job(path, reuse):  # looked up once, and only for this project's own jobs
                helpers = (helpers_gb(reuse) if mem is not None else 0) if helpers is None else helpers
                mine = helpers
            if short(need, mem, mine):
                w["why"] = f"waiting for {need:.10g} GB, {down(mem)} free" + (
                    f" + {down(mine)} in this project's helpers" if mine else "") + "; letting smaller jobs by"
                if not listed:
                    procs = holders()
                    own = {h[0] for h in unreused(reuse, procs, [info["pid"]] if info.get("pid") else [])}
                    w["big"] = ["holds memory: " + b + (" (this project's)" if h[0] in own else "")
                                for h, b in zip(procs, largest(big=procs))]
            elif too_busy(limit, load):
                by = "holding the line" if holds(path) else "letting other jobs by"
                w["why"] = f"waiting for load ≤ {limit:.10g}, now {loadf(load)}; {by}"
                w["big"] = [] if listed else [f"uses CPU: {b}" for b in busiest()]
            listed = listed or bool(w["why"])
        else:
            lead = next((a for a in line if not held_up(a, mem, load)), None)
            if lead and holding(lead, load, hold):
                w["why"] = f"behind pid {entry_pid(lead.name)}, a --measure job holding the line for a quiet CPU"
        line.append(path)
    return out


def lives(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:  # someone else's
        pass
    return True
