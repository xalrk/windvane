"""Temp space. Each session and check gets a TMPDIR of its own in the project's state dir, dropped when its ticket
merges or fails, so what a session or a leaky test leaves behind can't fill the machine's temp dir (a tmpfs runs out of
inodes long before space, and then no shell command runs). A cleaner removes what leaked anyway: `windvane-*` dirs in
the temp dir and the session dirs of tickets no longer running, once they're an hour old and in use by no process; and
the `tmpXXXXXXXX` dirs tests leaked before windvane named them (old leaks), once they're a day old and hold only what
windvane makes."""
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
DAY = 86400  # seconds an old leak must have sat untouched: a `tmpXXXXXXXX` dir might be anyone's work in progress
LEAK = re.compile(r"tmp[a-z0-9_]{8}")  # tempfile.mkdtemp's names, which the tests used before they named theirs
OURS = {"runs", "settings.json", "windvane-mentioned", "Critic", "critic.jsonl", "windvane-heavy.lock",
        "windvane-heavy.queue", "state.json", "answers.json"}  # all an old leak holds when it was a session's state dir
# What Claude Code gives a sandboxed Bash command as its TMPDIR, the first one set: it sets CLAUDE_TMPDIR itself, to
# its own dir under TMPDIR (`claude-<uid>`), and every command it runs inherits it, in the sandbox or outside it
SANDBOX_VARS = ("CLAUDE_CODE_TMPDIR", "CLAUDE_TMPDIR")
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


def session_dir(p, key):
    """The TMPDIR of a ticket's sessions and checks (or of a ticketless session, by its label)."""
    d = p.state / "tmp" / key
    d.mkdir(parents=True, exist_ok=True)
    return d


def drop(p, key):
    """A ticket's leftovers once it ends: its sessions' TMPDIR and its `windvane bg` jobs (jobs.job_base)."""
    shutil.rmtree(p.state / "tmp" / key, ignore_errors=True)
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
    if p is not None and (p.state / "tmp").is_dir():
        out += [d for d in (p.state / "tmp").iterdir() if d.name not in running]
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
