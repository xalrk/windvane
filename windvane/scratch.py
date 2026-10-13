"""Temp space. Each session and check gets a TMPDIR of its own in the project's state dir, dropped when its ticket
merges or fails, so what a session or a leaky test leaves behind can't fill the machine's temp dir (a tmpfs runs out of
inodes long before space, and then no shell command runs). A cleaner removes what leaked anyway: `windvane-*` dirs in
the temp dir and the session dirs of tickets no longer running, once they're an hour old and in use by no process; and
the `tmpXXXXXXXX` dirs tests leaked before windvane named them (old leaks), once they're a day old and hold only what
windvane makes."""
import os
import re
import shutil
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
ORIGIN = re.compile(r'^\[remote "origin"\]\s*$((?:\n[^\[].*$)*)', re.M)


def root():
    """The temp dir, read fresh: tempfile caches its first answer, and a test or session changes TMPDIR."""
    return Path(os.environ.get("TMPDIR") or tempfile.gettempdir())


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
    since, procs = time.time() - age, busy() if procs is None else procs
    out = []
    for d in dirs:
        try:
            if not d.is_dir() or d.is_symlink() or d.stat().st_uid != os.getuid():
                continue
        except OSError:
            continue
        names = {str(d), os.path.realpath(d)}  # lsof gives macOS's /var as /private/var; a TMPDIR is as it was set
        if any(b == n or b.startswith(n + os.sep) for b in procs for n in names) or _recent(d, since):
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
    return stale(candidates(p, running), age) + stale(old_leaks(), max(age, DAY))


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
