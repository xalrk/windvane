"""Temp space. Each session and check gets a TMPDIR of its own in the project's state dir (or under `tmp_root`, on a
roomier disk), dropped when its ticket merges or fails, so what a session or a leaky test leaves behind can't fill the
machine's temp dir (a tmpfs runs out of inodes long before space, and then no shell command runs). A cleaner removes
what leaked anyway: `windvane-*` dirs in the temp dir and the session dirs of tickets no longer running, once they're
an hour old and in use by no process; and the `tmpXXXXXXXX` dirs tests leaked before windvane named them (old leaks),
once they're a day old and hold only what windvane makes."""
import filecmp
import os
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path

from . import util

PREFIX = "windvane-"  # what the tests and tools/check_pythons.sh name their temp dirs, so the cleaner knows them
AGE = 3600  # seconds a leftover must have sat untouched: a running check writes into its dirs all the time
FULL = 90  # percent of the temp dir's inodes or space at which doctor warns and the driver pushes
FLOOR = 2 * 1024 ** 3  # bytes free under which a filesystem is nearly full whatever its percent: 98% of a large disk
# still holds a check's clones, 98% of a small one doesn't
DAY = 86400  # seconds an old leak must have sat untouched: a `tmpXXXXXXXX` dir might be anyone's work in progress
LEAK = re.compile(r"tmp[a-z0-9_]{8}")  # tempfile.mkdtemp's names, which the tests used before they named theirs
OURS = {"runs", "settings.json", "windvane-mentioned", "Critic", "critic.jsonl", "windvane-heavy.lock",
        "windvane-heavy.queue", "state.json", "answers.json"}  # all an old leak holds when it was a session's state dir
# What Claude Code gives a sandboxed Bash command as its TMPDIR, the first one set: it sets CLAUDE_TMPDIR itself, to
# its own dir under TMPDIR (`claude-<uid>`), and every command it runs inherits it, in the sandbox or outside it
SANDBOX_VARS = ("CLAUDE_CODE_TMPDIR", "CLAUDE_TMPDIR")
TMP_DIR = "windvane-tmp"  # the folder of windvane's own under `tmp_root`, a dir per project in it
MARK = ".windvane-tmp"  # in a project's dir there: windvane made it, so its ticket dirs are the cleaner's to remove
ORIGIN = re.compile(r'^\[remote "origin"\]\s*$((?:\n[^\[].*$)*)', re.M)


def root():
    """The temp dir, read fresh: tempfile caches its first answer, and a test or session changes TMPDIR."""
    return Path(os.environ.get("TMPDIR") or tempfile.gettempdir())


def sandbox_dir():
    """The TMPDIR a sandboxed Bash command of the Claude Code session we run in gets, when it is a dir other than ours,
    else None. A `windvane` command runs outside the sandbox, where TMPDIR is its parent (the session's TMPDIR), so a
    `$TMPDIR/x` a worker wrote in the sandbox is another file out here."""
    d = next((os.environ[k] for k in SANDBOX_VARS if os.environ.get(k)), None)
    if d is None and os.environ.get("CLAUDECODE"):  # a Claude Code that doesn't export it: where it puts its dir
        d = str(root() / f"claude-{os.getuid()}")
    if d and os.path.isdir(d) and os.path.realpath(d) != os.path.realpath(root()):
        return Path(d)
    return None


def sandbox_twin(arg, inner=None):
    """The file in the sandbox's TMPDIR that `arg` (a path, or `--opt=path`) may have been meant to name, when `arg` is
    under our TMPDIR and either isn't there (nor its folder) while that file (or its folder) is, or both are files
    with different bytes: the shell running a `windvane` command expanded `$TMPDIR` outside the sandbox, and an
    outer file of that name is a stale one some earlier command wrote. None for anything else: the same bytes on both
    sides, a directory, a path outside TMPDIR."""
    inner = inner or sandbox_dir()
    if inner is None:
        return None
    path = arg.split("=", 1)[1] if arg.startswith("-") and "=" in arg else arg
    if not os.path.isabs(path):
        return None
    here = os.path.normpath(path)
    rels = [os.path.relpath(here, d) for d in {os.path.normpath(root()), os.path.realpath(root())}]  # a symlinked one
    rel = next((r for r in rels if r != "." and not r.startswith("..")), None)
    if rel is None:
        return None
    twin = inner / rel
    if inner.resolve() in (Path(here).resolve(), *Path(here).resolve().parents):  # the sandbox's own dir, spelled out
        return None
    if os.path.lexists(here):
        return twin if os.path.isfile(here) and twin.is_file() and not same(here, twin) else None
    if twin.exists() or (twin.parent.is_dir() and not os.path.isdir(os.path.dirname(here))):
        return twin
    return None


def same(a, b):
    """Whether two files hold the same bytes; one we can't read counts as the same, so the command's own read of it
    reports the error as it would without a sandbox."""
    try:
        return filecmp.cmp(a, b, shallow=False)
    except OSError:
        return True


def refuse_twins(args, who, inner=None):
    """Exit before a command does anything when one of `args` is the outside twin of a file in the sandbox's dir
    (`sandbox_twin`), naming the path to pass: reading the outer one would quietly use another file's bytes."""
    inner = inner or sandbox_dir()
    if inner is None:
        return
    for a in args:
        if not (twin := sandbox_twin(a, inner)):
            continue
        here = a.split("=", 1)[1] if a.startswith("-") and "=" in a else a
        if not os.path.lexists(here):
            sys.exit(f"{who}: `{a}` isn't there outside the sandbox; the file is `{twin}`: pass that path")
        sys.exit(f"{who}: `{here}` and `{twin}` differ (`$TMPDIR` names the first out here, the second in the "
                 f"sandbox): pass `{twin}` for the sandbox's file, or rename `{here}` and pass the new name for this one")


def job_env(cmd, who):
    """What a `bg` or `heavy` job started from a session changes in its environment: the sandbox's TMPDIR, so the job
    and the worker's sandboxed commands mean the same files by `$TMPDIR`. Refuses an argument that names the outside
    twin of a file in the sandbox's dir (`refuse_twins`)."""
    inner = sandbox_dir()
    if inner is None:
        return {}
    refuse_twins(cmd, who, inner)
    return {"TMPDIR": str(inner)}


def tmp_root(p):
    """`tmp_root` as set (the project's config over machine.toml's), expanded, or None when neither sets it."""
    from . import heavy  # (heavy imports the runner, which imports this module)
    v = getattr(p, "cfg", {}).get("tmp_root") or heavy.machine().get("tmp_root")  # (a stand-in project may have none)
    return Path(os.path.expanduser(str(v))) if v else None


def base(p):
    """Where `p`'s per-ticket TMPDIRs go: `<tmp_root>/windvane-tmp/<project>`, else the state dir's tmp/. A check's
    clones and builds land there, so a small disk under the state dir fills while the project's own disk has room.
    A folder of windvane's own under `tmp_root`: the cleaner removes what's in it, and `tmp_root` may well be the
    folder that holds the user's projects."""
    r = tmp_root(p)
    return r / TMP_DIR / p.name if r else p.state / "tmp"


def bases(p):
    """The dirs `p`'s ticket TMPDIRs may be in, that the cleaner may empty: `base`, when it's the state dir's or
    windvane made it under `tmp_root` (its MARK file says so), and the state dir's too once `tmp_root` is set, which
    still holds what tickets left there before."""
    if tmp_root(p) is None:
        return [base(p)]
    return [b for b in (base(p), p.state / "tmp") if b == p.state / "tmp" or (b / MARK).is_file()]


def tmp_root_problem(p):
    """Why `p`'s `tmp_root` can't hold its TMPDIRs (missing, not a directory, not writable), or None. Checked at a
    run's start: a session can't make its TMPDIR there, and every command in it would fail."""
    r = tmp_root(p)
    if r is None:
        return None
    where = ".windvane/config.toml" if getattr(p, "cfg", {}).get("tmp_root") else "machine.toml"
    if not r.is_absolute():
        return f"tmp_root ({where}) is {r}: give an absolute path"
    if not r.is_dir():
        return f"tmp_root ({where}) is {r}, which isn't a directory: create it or unset tmp_root"
    if not os.access(r, os.W_OK | os.X_OK):
        return f"tmp_root ({where}) is {r}, which you can't write to: fix its permissions or unset tmp_root"
    b, top = base(p).resolve(), Path(p.main_root()).resolve()
    if top == b or top in b.parents or b in top.parents:
        return f"tmp_root ({where}) puts the tickets' TMPDIRs at {b}, overlapping the checkout {top}: pick a folder " \
               "outside it"
    if b.exists() and not (b / MARK).is_file():
        return f"tmp_root ({where}) puts the tickets' TMPDIRs at {b}, which already holds files windvane didn't make " \
               "(the cleaner would remove them): pick another tmp_root"
    return None


def session_dir(p, key):
    """The TMPDIR of a ticket's sessions and checks (or of a ticketless session, by its label)."""
    b = base(p)
    if tmp_root(p) is not None and not b.exists():
        b.mkdir(parents=True, exist_ok=True)
        (b / MARK).touch()
    d = b / key
    d.mkdir(parents=True, exist_ok=True)
    return d


def drop(p, key):
    """A ticket's leftovers once it ends: its sessions' TMPDIR and its `windvane bg` jobs (jobs.job_base)."""
    for b in {base(p), p.state / "tmp"}:  # a TMPDIR made before tmp_root was set, or since it was unset
        shutil.rmtree(b / key, ignore_errors=True)
    shutil.rmtree(p.state / "bg" / key, ignore_errors=True)


def busy():
    """The cwd and TMPDIR of every process we can see (/proc on Linux; `lsof` and `ps` on macOS)."""
    return set(util.cwds().values()) | {d.rstrip("/") for d in util.tmpdirs().values()}


class Busy:
    """`busy()`, read at the first look and then kept. Without /proc (macOS) it is an `lsof` and a `ps` over every
    process, up to seconds on a loaded machine, and the driver cleans after every ticket: a clean whose dirs are all
    in recent use (the usual case) never asks."""

    def __init__(self):
        self.seen = None

    def __iter__(self):
        if self.seen is None:
            self.seen = busy()
        return iter(self.seen)


def _recent(path, since):
    """Whether anything in the tree at `path` changed since `since`; stops at the first that did."""
    stack = [str(path)]
    while stack:
        d = stack.pop()
        try:
            if os.lstat(d).st_mtime >= since:
                return True
            with os.scandir(d) as it:
                for e in it:
                    if e.stat(follow_symlinks=False).st_mtime >= since:
                        return True
                    if e.is_dir(follow_symlinks=False):
                        stack.append(e.path)
        except OSError:
            continue
    return False


def size(path):
    """(entries, bytes) in the tree at `path`: entries are what a tmpfs runs out of."""
    n = b = 0
    for d, dirs, files in os.walk(path):
        n += len(dirs) + len(files)
        for f in files:
            try:
                b += os.lstat(os.path.join(d, f)).st_size
            except OSError:
                pass
    return n + 1, b


def stale(dirs, age=AGE, procs=None):
    """The dirs among `dirs` that are ours, untouched for `age` seconds, and no process's cwd or TMPDIR is in."""
    since, procs = time.time() - age, Busy() if procs is None else procs
    out = []
    for d in dirs:
        try:
            if not d.is_dir() or d.is_symlink() or d.stat().st_uid != os.getuid():
                continue
        except OSError:
            continue
        names = {str(d), os.path.realpath(d)}  # lsof gives macOS's /var as /private/var; a TMPDIR is as it was set
        if _recent(d, since) or any(b == n or b.startswith(n + os.sep) for b in procs for n in names):
            continue
        out.append(d)
    return out


def candidates(p=None, running=()):
    """windvane's temp dirs: `windvane-*` in the temp dir, and `p`'s session dirs of tickets not in `running`."""
    try:
        out = [d for d in root().iterdir() if d.name.startswith(PREFIX)]
    except OSError:
        out = []
    for b in bases(p) if p is not None else ():
        if b.is_dir():
            out += [d for d in b.iterdir() if d.name not in running]
    return out


def orphan_clone(d):
    """Whether `d` is a git clone of a test Sandbox's remote that's gone: what a test's clone leaked."""
    try:
        m = ORIGIN.search((d / ".git" / "config").read_text(errors="replace"))
    except OSError:
        return False
    url = re.search(r"^\s*url\s*=\s*(.+?)\s*$", m.group(1), re.M) if m else None
    return bool(url) and re.search(r"/windvane-test-[^/]+/remote\.git/?$", url.group(1)) is not None \
        and not Path(url.group(1)).exists()


def old_leak(d):
    """Whether a `tmpXXXXXXXX` dir is one a test leaked: empty, windvane state files only, or an orphan clone.
    Anything else (another program's files) isn't ours, whatever its age."""
    try:
        names = set(os.listdir(d))
    except OSError:
        return False
    return names <= OURS or (".git" in names and orphan_clone(d))


def old_leaks():
    """`tmpXXXXXXXX` dirs in the temp dir shaped like a test's leak (their age is for `stale`)."""
    try:
        dirs = [d for d in root().iterdir() if LEAK.fullmatch(d.name)]
    except OSError:
        return []
    return [d for d in dirs if d.is_dir() and not d.is_symlink() and old_leak(d)]


def removable(p=None, running=(), age=AGE):
    """The leftovers `clean` removes: stale windvane dirs, and old leaks untouched for a day."""
    procs = Busy()
    return stale(candidates(p, running), age, procs) + stale(old_leaks(), max(age, DAY), procs)


def leftovers(p=None, running=(), age=AGE):
    """[(dir, entries, bytes)] of what `clean` would remove, the biggest first."""
    return sorted(((d, *size(d)) for d in removable(p, running, age)), key=lambda x: -x[1])


def clean(p=None, running=(), age=AGE):
    """Remove the stale leftovers; (dirs, entries) removed."""
    dirs = n = 0
    for d in removable(p, running, age):
        n += size(d)[0]
        shutil.rmtree(d, ignore_errors=True)
        dirs += 1
    return dirs, n


def fullness(path=None):
    """(percent of inodes used, percent of space used) of the filesystem holding `path` (the temp dir)."""
    st = os.statvfs(path or root())
    inodes = 100 * (st.f_files - st.f_ffree) / st.f_files if st.f_files else 0  # some filesystems report none
    space = 100 * (st.f_blocks - st.f_bfree) / st.f_blocks if st.f_blocks else 0
    return inodes, space


def free(path=None):
    """Bytes free to us on the filesystem holding `path` (the temp dir)."""
    st = os.statvfs(path or root())
    return st.f_bavail * st.f_frsize


def existing(path):
    """`path`, or its nearest parent that exists: a ticket dir not made yet is on its parent's filesystem."""
    path = Path(path)
    while not path.exists() and path != path.parent:
        path = path.parent
    return path


def device(path):
    return os.stat(path).st_dev


def places(p=None):
    """[(path, what it is, named with its path)] of the filesystems temp files land on: the temp dir, and `p`'s
    per-ticket TMPDIRs (`base`), each filesystem once."""
    out, seen = [], {}
    ticket = [(base(p), f"the tickets' TMPDIRs under {base(p)}")] if p is not None else []
    for path, what in [(root(), f"the temp dir {root()}")] + ticket:
        try:
            dev = device(existing(path))
        except OSError:
            continue
        if dev in seen:
            i = seen[dev]
            out[i] = (out[i][0], f"{out[i][1]} and {what}")
            continue
        seen[dev] = len(out)
        out.append((path, what))
    return out


def nearly_full(p=None):
    """[(path, what it holds, % inodes used, % space used, bytes free)] of the filesystems in `places` over FULL,
    or with under FLOOR free."""
    out = []
    for path, what in places(p):
        try:
            inodes, space = fullness(existing(path))
            left = free(existing(path))
        except OSError:
            continue
        if max(inodes, space) > FULL or left < FLOOR:
            out.append((path, what, inodes, space, left))
    return out


def full_line(path, what, inodes, space, left, when=""):
    """How doctor and the driver's push say a nearly full filesystem (`places`, `nearly_full`)."""
    return f"{what} is {inodes:.0f}% full of inodes and {space:.0f}% of space ({mb(left)} MB free){when}"


def way_out(what):
    """`tmp_root`, when tickets' TMPDIRs are on the filesystem: free space isn't the only fix then."""
    return "set tmp_root (machine.toml) to a roomier disk" if "TMPDIR" in what else ""


def mb(n):
    return f"{n / 1024 ** 2:,.0f}"
