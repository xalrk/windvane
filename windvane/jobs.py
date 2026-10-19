"""The project's check (CI) with a pass cache, and background jobs for commands that outlast a tool call.

A pass is recorded under the git tree it ran on (plus the check command), only when the checkout was clean before
and after, so the driver and the reviewer reuse a worker's green check instead of running it again. Failures are
never recorded.
"""
import fcntl
import hashlib
import itertools
import json
import os
import signal
import stat
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from . import heavy, scratch, util
from .config import exe
from .heavy import STOP_AT
from .runner import GRACE, IN_JOB, NEST, ROLE, TICKET, outside_sandbox, signal_group
from .util import append_jsonl, gout, now, read_jsonl

_LOGS = itertools.count(1)  # a check log's number in this process
# A running check touches its log this often (seconds): `stats` times a killed check by its log's last write, so a
# check that hung without output would otherwise count as nearly free; this bounds the error to one interval.
HEARTBEAT = 60
# How long check logs are kept (seconds), pruned at each check's start, since every check writes its own log: a
# passed check's log is rarely opened once its ticket moves on, so it goes after a few days; any log after a few
# weeks, when no ticket still running needs it. The newest failed or cut-off logs stay whatever their age: the
# last failure is the log someone opens.
KEEP_PASSED = 3 * 86400
KEEP_ANY = 21 * 86400
KEEP_FAILED = 5
NOT_RUN, AGAIN = "CHECK NOT RUN: ", "; run it again"  # a heavy check that never got the machine
STOPPING = "; the run is stopping, so don't run it again now"  # ...because a stop ended its wait: a retry would too
# A kept job (`bg --heavy --keep`) outlives its session: the run's stop (a Unix time), which the driver gives its
# workers, is when it ends unless the driver moves it (`<job>.keep` holds the time it ends, empty for never).
RUN_STOP = "WINDVANE_RUN_STOP"
KILLED = 143  # a kept job's exit when the run's stop or a kill ended it (as SIGTERM's)
CANCELLED = "windvane: cancelled"  # a job's last log line after `bg --cancel`
WATCH_S = 2  # seconds between a kept job's looks at its end time
AWAIT_S = 570  # how long `await` blocks: under one tool call's 10 minutes
SESSION_CHECK = "check"  # the bg job a session's heavy check runs as
CHECK_LOG = "check: log "  # a check job's first line names the check's log, where its wait for the machine is said
# A bg job's shell: it links its own pid in first, should `bg` be killed before writing it (a link: never half-written,
# and never over the one `bg` wrote, which its stamp must not predate), then runs the command and writes its exit.
JOB = ('echo $$ > "$0.pid.$$"; ln "$0.pid.$$" "$0.pid" 2>/dev/null; rm -f "$0.pid.$$"; '
       '"$@" > "$0.log" 2>&1; echo $? > "$0.exit"')
# A kept job's group leader: `watch` from this same windvane (its package dir is the first argument), outside PATH.
WATCH = ("import sys; sys.path.insert(0, sys.argv[1]); from windvane.jobs import watch; "
         "sys.exit(watch(sys.argv[2], sys.argv[3:]))")


def waited(summary):
    """A not-run check's summary as the driver logs it: it runs the check again itself, so no "run it again"."""
    return f"not run, {summary.removeprefix(NOT_RUN).removesuffix(AGAIN).removesuffix(STOPPING)}"


def placeholder(path, rel=""):
    """Whether an untracked path is a sandbox's placeholder: a character device (/dev/null bound over a path the
    sandbox guards). Git can't track one, so it was never part of the tree; the sandbox picks the paths, so they're
    told apart by what they are, not by name. Outside the sandbox (a check run while a sandboxed command lives) the
    bind is in another namespace and the host sees only the mount point made for it: an empty regular file. All the
    guarded paths are dotfiles at the top or under a dot dir, so an empty file whose first component `rel` (relative
    to the top level) starts with "." counts too; an empty `src/x.py` or a dotfile with content still doesn't."""
    try:
        st = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISCHR(st.st_mode) or (stat.S_ISREG(st.st_mode) and not st.st_size and rel.startswith("."))


def top(cwd):
    """The top level of the checkout `cwd` is in (`cwd` itself outside one). Git status's paths are relative to it
    whatever the cwd, and the check runs there as the driver's gate does, so a pass from any subdirectory is the
    gate's."""
    return gout(cwd, "rev-parse", "--show-toplevel") or str(cwd)


def blockers(cwd):
    """The paths that keep a checkout's tree from being clean: every change to a tracked file, every untracked file
    but placeholders, seen from inside a sandbox or out. Untracked dirs are listed file by file (`all`), so a dir
    holding only placeholders is clean; -z: no quoting. None when git fails."""
    cwd = top(cwd)
    r = util.git(cwd, "status", "--porcelain", "-z", "--untracked-files=all")
    if r.returncode:
        return None
    out, paths, i = r.stdout.split("\0"), [], 0
    while i < len(out):
        entry = out[i]
        if len(entry) > 3 and not (entry[:2] == "??" and placeholder(Path(cwd) / entry[3:], entry[3:])):
            paths.append(entry[3:])
        i += 1 + (entry[:1] in ("R", "C"))  # a rename's source follows as its own field
    return paths


def tree_key(p, cwd):
    """The key of a clean checkout's tree (`blockers`) for the configured check, or None if it has uncommitted
    changes. Only HEAD's tree and the command: a pass recorded in a sandbox is reused outside it, and back."""
    if blockers(cwd) != []:
        return None
    tree = gout(cwd, "rev-parse", "HEAD^{tree}")
    return hashlib.sha1(f"{tree}\n{p.cfg['check']}".encode()).hexdigest() if tree else None


def ledger(p, ticket, row, event="check"):
    """A check run for a ticket (the driver's, or a session's `windvane check`) as a ledger row, so `stats` can say
    what checks cost; one at the desk, for no ticket, isn't counted. The role is the session's that ran it, or the
    driver's (its gate), so `stats` can say who waited."""
    ticket = ticket or os.environ.get(TICKET)
    if ticket:
        append_jsonl(p.state / "ledger.jsonl", {"event": event, "ticket": ticket,
                                                "role": os.environ.get(ROLE) or "driver", **row})


def check(p, cwd, quiet=False, ticket=None, stop_at=None, mem=None, announce=False):
    """Run the project's check at the top level of `cwd`'s checkout (`top`). Returns (passed, summary line, log
    path); passed is None when a heavy check never ran: its wait for the machine timed out, which says nothing about the tree, so callers try it again.
    `stop_at` (a datetime): when a heavy check stops waiting for the machine, or earlier if the run's deadline is
    moved meanwhile (the driver's); a session's check has its own stop in the environment already. `mem` (GB): the
    free memory a heavy check waits for, this once, instead of `heavy_mem`. `announce`: say the log first (in a
    bg job, for `await` to read its wait from)."""
    cmd = p.cfg["check"]
    if not cmd:
        return True, "CHECK PASSED (no check configured)", None
    cwd = top(cwd)
    passes = p.state / "passes"
    key = tree_key(p, cwd)
    if key and (passes / key).exists():
        ledger(p, ticket, {"minutes": 0, "passed": True, "reused": True})
        return True, f"CHECK PASSED (reused the pass from {(passes / key).read_text().strip()} for this tree)", None
    logs = p.state / "checks"
    logs.mkdir(parents=True, exist_ok=True)
    prune(p, logs)
    log = logs / log_name()
    if announce:
        print(f"{CHECK_LOG}{log}", flush=True)
    code, mins, waited = run_logged(p, ticket, cmd, cwd, log, stop_at, mem)
    if waited:
        what, stopped = waited
        if stopped:
            return None, f"{NOT_RUN}stopped waiting for {what} after {mins:.0f} min{STOPPING}", log
        return None, f"{NOT_RUN}waited {mins:.0f} min for {what}{AGAIN}", log
    if code == 0:
        if key and tree_key(p, cwd) == key:
            passes.mkdir(parents=True, exist_ok=True)
            (passes / key).write_text(f"{now():%Y-%m-%d %H:%M} ({gout(cwd, 'rev-parse', '--short', 'HEAD')})")
        return True, f"CHECK PASSED in {mins:.1f} min" + ("" if key else unrecorded(cwd)), log
    return False, f"CHECK FAILED (exit {code}) in {mins:.1f} min; log {log}", log


def unrecorded(cwd):
    """Why a pass wasn't recorded, naming the first paths in the way, so a worker tells its own leftovers from
    something else's."""
    paths = blockers(cwd) or []
    named = ", ".join(paths[:2]) + (", …" if len(paths) > 2 else "")
    return f" (uncommitted changes: not recorded{': ' + named if named else ''})"


def log_name():
    """A check log's name, sortable by time and its own even for checks started in the same second (parallel slots,
    a session's check beside the driver's): a shared log would be truncated by the second check, and its lock would
    make a killed check look still running."""
    return f"{now():%Y%m%d-%H%M%S}-{os.getpid()}-{next(_LOGS)}.log"


def run_logged(p, ticket, cmd, cwd, log, stop_at=None, mem=None):
    """Run the check into `log`, with a ledger row on each side, so a check that's cut off still counts: a
    `check-start` row first (all a SIGKILL or a killed driver leaves; `stats` calls it cut off), then the end row
    with the same `start`, written on the way out even for an exception or Ctrl-C (`finished: false`, the minutes
    so far). Both rows, because only the start row survives a kill and only the end row knows the minutes. The log
    stays locked while the check runs (the kernel drops the lock on a kill), so `running` tells a check still going
    from one cut off; the start row is written only once it's locked, or `stats` could catch it unlocked and call a
    check that just started cut off. A heartbeat touches the log while the check runs, so a kill is timed within
    `HEARTBEAT` even when the check went quiet. Returns (exit code, minutes, what a heavy check waited for and never
    got, or None): such a row says `not_run`, and `stats` doesn't count it a failure. A heavy check's end row also
    says how many of its seconds went to waiting for the machine (`waited_s`: all of them when it never ran)."""
    start = time.time()
    code = waited = None
    t = ticket or os.environ.get(TICKET)  # a ticket's check gets its scratch dir as TMPDIR, as its sessions do
    env = {**os.environ, **({"TMPDIR": str(scratch.session_dir(p, t))} if t else {})}
    try:
        with log.open("w") as f:
            fcntl.flock(f, fcntl.LOCK_EX)  # blocking: `running` may hold a momentary shared probe
            ledger(p, ticket, {"start": round(start, 3), "log": log.name}, event="check-start")
            beat = heartbeat(log)
            try:
                if p.cfg["heavy_check"]:  # one machine-heavy job at a time, across slots and projects
                    mem = p.cfg.get("heavy_mem") if mem is None else mem  # unset: machine.toml's min_free_gb
                    code = subprocess.run([sys.executable, "-c", "import json, sys; from windvane.heavy import run; "
                                           "sys.exit(run(sys.argv[1], cwd=sys.argv[2], "
                                           "mem=float(sys.argv[3]) if sys.argv[3] else None, "
                                           "reuse=json.loads(sys.argv[4])))",
                                           cmd, str(cwd), "" if mem is None else str(mem),
                                           json.dumps(heavy.reuse_of(p))], cwd=cwd,
                                          stdout=f, stderr=subprocess.STDOUT,
                                          env={**env, "PYTHONPATH": str(Path(__file__).parents[1]),
                                               heavy.STOP_ENV: str(p.state / "STOP"),  # a stop ends the wait
                                               **({heavy.STOP_AT: str(stop_at.timestamp()),
                                                   heavy.DEADLINE_ENV: str(p.state / "DEADLINE")} if stop_at else {})}
                                          ).returncode
                else:
                    code = subprocess.run(cmd, shell=True, cwd=cwd, stdout=f, stderr=subprocess.STDOUT,
                                          env=env).returncode
            finally:
                beat.set()
        if p.cfg["heavy_check"] and code == heavy.TIMED_OUT:
            waited = heavy.gave_up(log)
        for hint in heavy.hints_in(log) if p.cfg["heavy_check"] else []:
            util.log(p, f"heavy check: {hint}")
    finally:
        mins = (time.time() - start) / 60
        wait_s = round(mins * 60) if waited else heavy.waited(log) if p.cfg["heavy_check"] else 0
        ledger(p, ticket, {"minutes": round(mins, 2), "passed": code == 0, "reused": False, "start": round(start, 3),
                           **({} if code is not None else {"finished": False}), **({"not_run": True} if waited else {}),
                           **({"waited_s": wait_s} if wait_s else {})})
    return code, mins, waited


def heavy_waits(p, since, ticket=None):
    """{log: (when it began waiting, seconds waited, heavy_reuses words, default need's GB or None)} for each heavy
    job (a check, a ticket's `bg --heavy` job) last at work at `since` or later that waited on memory and was told
    which settings would have spared it (`heavy.memory_hints`). Read from the logs where they are: a check's in
    checks/ (its heartbeat keeps its time), a bg job's until its ticket's scratch is dropped; with a `ticket`, only
    that ticket's bg jobs. A job that never got the machine waited all its time. A log removed as it's read (a prune,
    another slot's ticket ending) is skipped."""
    out = {}
    logs = [*(p.state / "checks").glob("*.log"), *(p.state / "bg").glob("*/*.log")] if ticket is None else \
        list((p.state / "bg" / ticket).glob("*.log"))
    for log in logs:
        try:
            if found := heavy_wait(log, log.parent.name != "checks", since):
                out[log] = found
        except OSError:
            continue
    return out


def heavy_wait(log, bg, since):
    """`heavy_waits`' row for one log, or None."""
    exit_ = log.with_suffix(".exit")
    end = log.stat().st_mtime if not bg else exit_.stat().st_mtime if exit_.exists() else time.time()  # bg: running
    if end < since:
        return None
    words, need = heavy.memory_hints(log)
    if not words and need is None:
        return None
    try:
        start = (float(log.with_suffix(".start").read_text()) if bg
                 else datetime.strptime(log.name[:15], "%Y%m%d-%H%M%S").timestamp())  # `log_name`'s
    except ValueError:
        start = end
    secs = heavy.waited(log)
    if not secs and (heavy.waiting(log) or heavy.gave_up(log)):
        secs = max(0, round(end - start))
    return start, secs, words, need


def heartbeat(log):
    """Touch `log` every `HEARTBEAT` seconds from a daemon thread (it dies with the process, as a kill should leave
    the log's time) until the returned event is set."""
    stop = threading.Event()

    def beat():
        while not stop.wait(HEARTBEAT):
            try:
                os.utime(log)
            except OSError:  # the log was removed: nothing to time
                pass

    threading.Thread(target=beat, daemon=True).start()
    return stop


def locked(path):
    """Whether a check still holds `path`'s lock; a log that can't be opened isn't (OSError is the caller's)."""
    with path.open("rb") as f:
        return util.held(f)


def running(p, start_row):
    """Whether the check a `check-start` row names is still running: its log is still locked."""
    try:
        return locked(p.state / "checks" / start_row.get("log", ""))
    except OSError:  # no log: a row from before logs were named
        return False


def outcomes(p):
    """(the logs of checks that passed, the start rows of killed or running checks by log name), from the ledger: a
    start row names the log, its end row (same ticket and start) says how it went. A log no row names (a check at
    the desk, for no ticket) is in neither, and counts as not passed."""
    rows = read_jsonl(p.state / "ledger.jsonl")
    ends = {(r.get("ticket"), r["start"]): r.get("passed") or r.get("not_run")  # a check that never ran: no failure
            for r in rows if r.get("event") == "check" and "start" in r}
    starts = [r for r in rows if r.get("event") == "check-start" and "log" in r]
    return ({r["log"] for r in starts if ends.get((r.get("ticket"), r.get("start")))},
            {r["log"]: r for r in starts if (r.get("ticket"), r.get("start")) not in ends})


def log_minutes(log, start):
    """A killed check's minutes: from its start to its log's last write (its output's or the heartbeat's)."""
    return round(max(0.0, log.stat().st_mtime - start) / 60, 2)


def prune(p, logs, clock=time.time):
    """Delete check logs past their keep (`KEEP_PASSED`, `KEEP_ANY`), but never a locked one (its check runs) or
    one of the `KEEP_FAILED` newest that didn't pass. A killed check's log gets the end row it never wrote first, so
    `stats` doesn't time its hang at 0 once the log is gone. Never fails the check it runs for: a file it can't read
    or delete stays, and anything else wrong skips the prune. One prune at a time (another slot's check may start
    alongside): two would both close the same killed check, counting its minutes twice."""
    try:
        with logs.with_name("prune.lock").open("w") as lock:  # beside the logs: `checks/` holds only logs
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:  # another check is pruning right now
                return
            prune_logs(p, logs, clock())
    except Exception as e:  # noqa: BLE001 - a prune is housekeeping: the check must still run
        print(f"check logs not pruned (the check runs anyway): {e}", file=sys.stderr)


def prune_logs(p, logs, t):
    """`prune`'s work, under its lock."""
    passed, killed = outcomes(p)
    files = sorted(logs.glob("*.log"))  # names sort by start time
    keep = set([f for f in files if f.name not in passed][-KEEP_FAILED:])
    for f in files:
        try:
            if f in keep or t - f.stat().st_mtime < (KEEP_PASSED if f.name in passed else KEEP_ANY) or locked(f):
                continue
            if f.name in killed:
                close(p, killed[f.name], f)
            f.unlink()
        except OSError:
            pass


def close(p, start_row, log):
    """The end row a killed check never wrote (cut off, timed to its log's last write), dated like its start row so
    `stats --days` counts it where it happened."""
    row = {"event": "check", "ticket": start_row["ticket"], "minutes": log_minutes(log, start_row["start"]),
           "passed": False, "reused": False, "start": start_row["start"], "finished": False}
    append_jsonl(p.state / "ledger.jsonl", {**({"at": start_row["at"]} if "at" in start_row else {}), **row})


def tail(path, n=40):
    try:
        return "\n".join(Path(path).read_text(errors="replace").splitlines()[-n:])
    except OSError:
        return ""


def job_base(p, name):
    """A bg job's files, minus their suffix. A ticket's sessions keep theirs under the ticket's own dir, so tickets
    running side by side that pick the same name (`check`) each start and await their own job; the desk's (no
    ticket) stay right under bg/. Dropped with the ticket's scratch (scratch.drop)."""
    ticket = os.environ.get(TICKET)
    return p.state / "bg" / (ticket or "") / name


def find_job(p, name):
    """(the base of the job `name` the caller owns, an unscoped job of that name it can't tie to itself or None).

    A ticket's job started by an older windvane (before job_base scoped them) sits at the unscoped path, and the
    installed windvane updates under a running session: that job is the caller's when it runs in the caller's
    worktree, by the cwd `bg` recorded or, for an older job, its live pid's. Any other stays another's (T-118)."""
    base, old = job_base(p, name), p.state / "bg" / name
    if base == old or Path(f"{base}.start").exists() or not Path(f"{old}.start").exists():
        return base, None
    where = job_cwd(old)
    if where and worktree(where) == worktree(os.getcwd()):
        return old, None
    return base, old


def job_cwd(base):
    """Where a job runs: as `bg` recorded it, or for an older windvane's job, its live pid's working directory."""
    try:
        return Path(f"{base}.cwd").read_text().strip()
    except OSError:
        pass
    try:
        pid = int(Path(f"{base}.pid").read_text())
    except (OSError, ValueError):
        return None
    return util.cwds(pid).get(pid)  # None when it's dead: nothing ties it to anyone


def worktree(path):
    """The git worktree `path` is in (`path` itself outside one), or None when it's gone."""
    try:
        path = Path(path).resolve(strict=True)
    except OSError:
        return None
    return Path(gout(path, "rev-parse", "--show-toplevel") or path).resolve()


def alive(base):
    """Whether a job runs: no exit written, and its pid is alive and started by the time `bg` wrote it. A number
    reused after a reboot is a stranger's, which mustn't hold a kept job's ticket and slot (or block the name)."""
    return not Path(f"{base}.exit").exists() and pid_lives(base)


def pid_lives(base):
    """Whether the job's recorded pid is still the job's process. Only ESRCH (or no pid to look at) is a death: EPERM
    means a process holds the number, and the `stamp` `bg` wrote beside it tells whether it is the job or a stranger
    reusing it, through a step of the wall clock too. A job from an older windvane, with no stamp (or one older than
    its pid, left by an older `bg` that started it again), goes by its start time against the pid file's mtime."""
    pid_f, stamp_f = Path(f"{base}.pid"), Path(f"{base}.stamp")
    try:
        pid = int(pid_f.read_text())
        try:
            os.kill(pid, 0)
        except PermissionError:
            pass
        recorded = util.recorded_stamp(stamp_f)
        if recorded and stamp_f.stat().st_mtime >= pid_f.stat().st_mtime:
            return util.same_process(pid, recorded) is not False  # None: it can't tell, and the number is held
        t = util.started_at(pid)
        return t is None or t <= pid_f.stat().st_mtime + 2  # /proc's boot time and ps's are in whole seconds
    except (OSError, ValueError):
        return False


def stray_line(name, base):
    """One line on an unscoped job the caller can't tie to itself, so a worker isn't left guessing where it went."""
    exit_f = Path(f"{base}.exit")
    how = (f"exited {exit_f.read_text().strip()}" if exit_f.exists() else "running" if alive(base) else
           "not running, no exit recorded")
    return (f"a '{name}' job outside this ticket (another worktree's, or one an older windvane started that can't be "
            f"tied to it): {how}; log {base}.log")


def bg(p, name, cmd, cwd, heavy=False, mem=None, keep=False, expect=None, quiet=False):
    """Start `cmd` in the background. A session's job dies with it; a kept one (`keep`) goes on past it, until the
    run's stop, so a heavy job that can't both queue and run in one session still runs: the driver holds its ticket
    until it ends."""
    outside_sandbox("a background job")  # it would end with the sandboxed call that started it
    tmp = scratch.job_env(cmd, "bg")  # before anything is written: a refused job leaves no trace
    if alive(find_job(p, name)[0]):  # refused before writing anything; the check under the lock catches a racer
        sys.exit(f"bg: '{name}' is still running; wait with: windvane await {name}")
    base = job_base(p, name)
    base.parent.mkdir(parents=True, exist_ok=True)
    with open(f"{base}.lock", "a") as lock:  # a session's parallel calls for one name: the second waits, then sees it
        fcntl.flock(lock, fcntl.LOCK_EX)
        if alive(find_job(p, name)[0]):
            sys.exit(f"bg: '{name}' is still running; wait with: windvane await {name}")
        proc, who = start_job(name, cmd, cwd, base, tmp, heavy, mem, keep, expect)
    session = os.environ.get(NEST)
    if session and not keep:  # killed with the session that started it (runner.kill_nested), not left by a stop
        mark = p.state / "nested" / session / str(proc.pid)
        mark.parent.mkdir(parents=True, exist_ok=True)
        mark.write_text(who or "")
    if quiet:
        pass
    elif keep:
        print(f"started '{name}', kept past this session (log {base}.log): commit, push and end your run "
              f"PARTIAL naming it; the ticket resumes when it ends")
    else:
        print(f"started '{name}' (log {base}.log); wait with: windvane await {name}")


def start_job(name, cmd, cwd, base, tmp, heavy, mem, keep, expect):
    """`bg`'s start, under its lock: the job's files and process. (the process, its `util.stamp` or None)."""
    pid = Path(f"{base}.pid")
    for end in ("pid", "exit", "keep", "child", "stamp"):
        Path(f"{base}.{end}").unlink(missing_ok=True)
    Path(f"{base}.start").write_text(str(int(time.time())))
    Path(f"{base}.cwd").write_text(str(Path(cwd).resolve()))  # ties it to its worktree, should job_base move again
    if heavy:
        cmd = [*exe(), "heavy", *(["--mem", f"{mem:.10g}"] if mem is not None else []),
               *(["--expect", f"{expect:.10g}"] if expect is not None else []), "--", *cmd]
    env = {**os.environ, **tmp, IN_JOB: name}
    if keep:  # it waits for the machine as long as it's kept (`watch` ends it), and belongs to no session
        Path(f"{base}.keep").write_text(os.environ.get(RUN_STOP, ""))
        cmd = [sys.executable, "-c", WATCH, str(Path(__file__).parents[1]), str(base), *cmd]
        env = {**{k: v for k, v in env.items() if k != NEST}, STOP_AT: "inf"}
    proc = subprocess.Popen(["bash", "-c", JOB, str(base), *cmd], cwd=cwd, stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True, env=env)
    util.replace_text(pid, str(proc.pid))  # the job writes it too: a caller killed before this leaves no lost job
    if who := util.stamp(proc.pid):  # after the pid, which `pid_lives` checks it against; gone already: none
        Path(f"{base}.stamp").write_text(who)
    return proc, who


def watch(base, cmd):
    """A kept job's group leader: runs `cmd` in a group of its own (its pid in `<job>.child`, for `end_kept`) and
    ends it at the time in `<job>.keep`, read afresh each look, since no session's end does. Its exit code."""
    child = subprocess.Popen(cmd, start_new_session=True)
    Path(f"{base}.child").write_text(str(child.pid))
    while True:
        try:
            return child.wait(WATCH_S)
        except subprocess.TimeoutExpired:
            pass
        until = kept_until(base)
        if until is not None and time.time() >= until:
            print("windvane: kept job stopped at the run's stop", file=sys.stderr, flush=True)
            stop_groups([(child.pid, Path(f"{base}.child"))])
            child.wait()
            return KILLED


def kept_until(base):
    """When a kept job ends (a Unix time), or None for never: a desk job, or one whose ticket's run gave no stop."""
    try:
        return float(Path(f"{base}.keep").read_text())
    except (OSError, ValueError):
        return None


def kept_jobs(p, ticket=None):
    """The kept jobs (`bg --keep`) of one ticket, or of every ticket, that its next session hasn't been told of yet:
    running, or ended since. Their bases, as job_base gives them."""
    return [f.with_suffix("") for f in sorted((p.state / "bg").glob(f"{ticket or '*'}/*.keep"))]


def running_kept(p, ticket=None):
    """The kept jobs still waiting or running."""
    return [b for b in kept_jobs(p, ticket) if alive(b)]


def follow(p, until, was=0):
    """Let every running kept job go on until at least `until` (a Unix time or a datetime), and bring those told a
    stop after it, up to `was` (the latest a run gave out before a deadline moved earlier), back to it."""
    until = until.timestamp() if isinstance(until, datetime) else until
    for b in running_kept(p):
        t = kept_until(b)
        if t is not None and (t < until or until < t <= was):
            Path(f"{b}.keep").write_text(str(until))


def stop_groups(groups):
    """SIGTERM each (process group, the file written as it started), then SIGKILL those alive after GRACE."""
    alive_ = [g for g, mark in groups if signal_group(g, signal.SIGTERM, mark)]
    end = time.time() + GRACE
    while alive_ and time.time() < end:
        time.sleep(0.1)
        alive_ = [g for g in alive_ if signal_group(g, 0)]
    for g in alive_:
        signal_group(g, signal.SIGKILL)


def end_job(base, line):
    """Stop a job's process groups (its leader's, and a kept job's command's: see `watch`), then give its log `line`
    last, after whatever the job said as it died, and exit `KILLED` if it wrote none, so `await` and the session
    that resumes a kept job's ticket are told. False, touching nothing, when it has just ended on its own."""
    if Path(f"{base}.exit").exists():
        return False
    groups = []
    for end in ("pid", "child"):
        try:
            groups.append((int(Path(f"{base}.{end}").read_text()), Path(f"{base}.{end}")))
        except (OSError, ValueError):
            pass
    stop_groups(groups)
    with open(f"{base}.log", "a") as f:
        f.write(f"{line}\n")
    exit_f = Path(f"{base}.exit")
    if not exit_f.exists() or not exit_f.read_text().strip():  # killed between creating the file and writing it
        exit_f.write_text(str(KILLED))
    return True


def end_kept(p, ticket=None, why="the run's stop"):
    """Kill the kept jobs still waiting or running (a ticket's, or every one), each with a last log line saying why.
    Their names."""
    out = []
    for b in running_kept(p, ticket):
        end_job(b, f"windvane: kept job killed: {why}")
        out.append(f"{b.parent.name}'s '{b.name}'")
    return out


def cancel(p, name):
    """End the caller's own job `name` (`bg --cancel`), waiting for the machine or running, kept or not: its heavy
    queue entry goes with its process, so the jobs behind it move up and the name can start again at once."""
    outside_sandbox("cancelling a background job")  # its own pid namespace hides the job: it would look dead
    (base, stray), ticket = find_job(p, name), os.environ.get(TICKET)
    if not Path(f"{base}.start").exists():
        sys.exit(f"bg --cancel: nothing started as '{name}'" + (f" for {ticket}" if ticket else "")
                 + (f"\n{stray_line(name, stray)}" if stray else ""))
    if not alive(base):
        exit_f = Path(f"{base}.exit")
        how = f"exit {exit_code(exit_f, 0)}" if exit_f.exists() else "it died without an exit"
        sys.exit(f"bg --cancel: '{name}' already ended ({how}); log {base}.log")
    log, mins = f"{base}.log", (time.time() - int(Path(f"{base}.start").read_text())) / 60
    queued, wait_m = heavy.waiting(watched_log(base)), heavy.waited(watched_log(base)) / 60
    if not end_job(base, CANCELLED):
        sys.exit(f"bg --cancel: '{name}' ended on its own just now (exit {exit_code(Path(f'{base}.exit'))}); log {log}")
    if queued:
        was = f"waiting for the machine for {mins:.0f} min"
    else:  # the minutes it ran, not counting its wait in the queue
        was = f"running for {mins - wait_m:.0f} min" + (f", after {wait_m:.0f} min in the heavy queue"
                                                        if round(wait_m) else "")
    print(f"cancelled '{name}' (was {was})")


def ended_kept(p, ticket):
    """Lines on the ticket's kept jobs that have ended, for its next session's prompt; it's told of each once."""
    lines = []
    for b in kept_jobs(p, ticket):
        if alive(b):
            continue
        exit_f = Path(f"{b}.exit")
        code = exit_code(exit_f, 0) if exit_f.exists() else "? (it died without one)"
        how = "cancelled (`bg --cancel`)" if tail(f"{b}.log", 1) == CANCELLED else f"exit {code}"
        try:
            mins = f" after {(exit_f.stat().st_mtime - int(Path(f'{b}.start').read_text())) / 60:.0f} min"
        except (OSError, ValueError):
            mins = ""
        lines.append(f"- '{b.name}' {how}{mins}; log {b}.log (`windvane await {b.name}` shows it)")
        for end in ("keep", "child"):
            Path(f"{b}.{end}").unlink(missing_ok=True)
    return lines


def watched_log(base):
    """The log that tells whether a job still waits for the machine: its own, or the check log a session's check
    job names on its first line, since the check writes its heavy wait there."""
    first = first_line(f"{base}.log")
    return first.removeprefix(CHECK_LOG) if first.startswith(CHECK_LOG) else f"{base}.log"


def first_line(path):
    """A file's first line, or "" when it can't be read."""
    try:
        with open(path, errors="replace") as f:
            return f.readline().rstrip("\n")
    except OSError:
        return ""


def exit_code(exit_f, patience=2.0):
    """A job's exit code from its exit file. The shell creates the file before it writes the code into it, so a
    file seen the moment it appears can still be empty: wait a moment for the code rather than print none."""
    end = time.time() + patience
    while not (code := exit_f.read_text().strip()) and time.time() < end:
        time.sleep(0.05)
    return code or "?"


def await_(p, name, lines=40, limit=None):
    """Block until a bg job ends, at most `limit` seconds (default AWAIT_S). The seconds it blocked go to the ledger
    as an `await` row (a ticket's only), so `stats` shows the time each role spends waiting. Its exit code (a
    string), or None while it still waits or runs."""
    outside_sandbox("waiting for a background job")  # its own pid namespace hides the job: it would look dead
    (base, stray), ticket = find_job(p, name), os.environ.get(TICKET)
    if not Path(f"{base}.start").exists():
        sys.exit(f"await: nothing started as '{name}'" + (f" for {ticket}" if ticket else "")
                 + (f"\n{stray_line(name, stray)}" if stray else ""))
    t0 = time.time()
    end = t0 + (AWAIT_S if limit is None else limit)
    exit_f = Path(f"{base}.exit")
    while not exit_f.exists() and time.time() < end:
        if not pid_lives(base):
            time.sleep(1)
            if exit_f.exists() or pid_lives(base):  # it wrote its exit as it died, or its pid was being written
                continue
            exit_f.write_text("1")
            break
        time.sleep(3)
    if (blocked := round(time.time() - t0)) >= 1:
        ledger(p, None, {"name": name, "seconds": blocked}, event="await")
    mins = (time.time() - int(Path(f"{base}.start").read_text())) / 60
    if exit_f.exists():
        code = exit_code(exit_f)
        print(f"DONE {name} exit {code} after {mins:.0f} min")
        out = tail(f"{base}.log", lines)  # a check job's log line was for the wait: its summary names a log it needs
        print(out[out.index("\n") + 1:] if out.startswith(CHECK_LOG) and "\n" in out else out)
        return code
    log = watched_log(base)
    state = "WAITING" if heavy.waiting(log) else "RUNNING"
    where = "in the heavy queue, " if state == "WAITING" else ""
    print(f"{state} {name} ({where}{mins:.0f} min so far); call windvane await {name} again")
    print(tail(log, 3))
    return None


def check_exit(ok):
    """`windvane check`'s exit for `check`'s `passed`: a check that never ran (None) isn't a failure."""
    return 0 if ok else 1 if ok is False else heavy.TIMED_OUT


def session_check(p, cwd, mem=None, lines=60):
    """A session's check (`windvane check` with `STOP_AT` set). A check can outlast one tool call's 10 minutes (a
    heavy one's wait for the machine lasts until the session's stop), and a killed call loses the run and its pass:
    so it runs as the session's bg job `check` (the running one when there is one, never a second) and is awaited
    as `await` does. Its exit code: the check's own once it ended, else TIMED_OUT, `await check` going on."""
    cwd = top(cwd)  # the job runs where the gate does
    key = tree_key(p, cwd)
    if not p.cfg["check"] or key and (p.state / "passes" / key).exists():  # nothing to wait for, piped or not
        ok, summary, log = check(p, cwd)  # a pass pruned since the look above runs the check here, and may fail
        if ok is False and log:
            print(tail(log, lines))
        print(summary)
        return check_exit(ok)
    outside_sandbox("a session's check")  # the job would die with the call (and the heavy lock's folder is read-only)
    base, head = find_job(p, SESSION_CHECK)[0], gout(cwd, "rev-parse", "--short", "HEAD")
    if not alive(base):
        bg(p, SESSION_CHECK, [*exe(), "check", "--lines", str(lines),
                              *(["--mem", f"{mem:.10g}"] if mem is not None else [])], cwd, quiet=True)
        Path(f"{base}.head").write_text(head)
    else:
        if mem is not None:
            print(f"check: the running check keeps its own need; --mem {mem:g} goes to the next one")
        was = tail(f"{base}.head", 1)
        if was and was != head:  # a check passed on another head records no pass for this one
            print(f"check: the running check started on {was}, not {head}: once it ends, run `windvane check` again")
    code = await_(p, SESSION_CHECK, lines + 1)
    if code is None:  # await's line said to call it again: the job keeps its place in line meanwhile
        return heavy.TIMED_OUT
    return int(code) if code.isdigit() else 1
