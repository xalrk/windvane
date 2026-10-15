"""Keeps the running windvane current between tickets: what a run merges into windvane (or what lands upstream) is
used from the next ticket on, instead of after someone pulls and restarts the driver by hand."""
import os
import re

from . import net
from .util import gout, run

CODE = ("windvane/", "bin/", "prompts/")  # read once by the driver: a change here needs a restart
SOURCE = "https://github.com/xalrk/windvane"  # where `pipx install git+...` took it from
# README's two ways in, for an install gone from under a run
REINSTALL = "pipx reinstall windvane, or sh install.sh in its clone"


_loaded = {}  # install -> the commit this process's code came from


def loaded(home):
    """The install commit this process runs: its HEAD the first time it is asked (the driver asks as it starts).
    Another driver, or a `git pull` by hand, may move the shared install later; this process keeps the old code."""
    key = str(home)
    if key not in _loaded:
        _loaded[key] = gout(home, "rev-parse", "HEAD")
    return _loaded[key]


def pull(home, said, ran=None):
    """Fast-forwards a clean checkout of `main` to its remote's; "restart" when driver code differs between `ran`
    (the commit the caller runs, by default `loaded`) and the install's HEAD, whoever moved it, else None.
    Anything else (local changes, another branch, local commits) is left alone: the reason goes into `said`, which
    the caller logs once."""
    ran = ran or loaded(home)
    branch = gout(home, "rev-parse", "--abbrev-ref", "HEAD")
    if branch != "main":
        return skip(said, f"on branch {branch or '?'}, not main")
    if gout(home, "status", "--porcelain", "--untracked-files=no"):
        return skip(said, "it has local changes")
    r = net.call(["git", "fetch", "-q", "origin", "main"], home, timeout=60)  # waits out an outage in a run
    if r.returncode:
        return skip(said, "git fetch timed out" if r.returncode == 124 else "git fetch failed")
    old, new = gout(home, "rev-parse", "HEAD"), gout(home, "rev-parse", "FETCH_HEAD")
    if new and old != new:
        if run(["git", "merge-base", "--is-ancestor", "HEAD", new], home).returncode:
            return skip(said, "its main has commits origin/main doesn't")
        if run(["git", "merge", "-q", "--ff-only", new], home).returncode:
            return skip(said, "the fast-forward failed")
        said.append(f"windvane updated to {new[:8]}")
    return "restart" if code_changed(home, ran) else None


def code_changed(home, ran):
    """Whether the driver's code at the install's HEAD differs from `ran`'s. A commit git can't compare (gone from
    the install) counts as changed: a restart loads what is there, and then runs it."""
    head = gout(home, "rev-parse", "HEAD")
    if not ran or ran == head:
        return False
    r = run(["git", "diff", "--name-only", ran, head], home)
    return bool(r.returncode) or any(f.startswith(CODE) for f in r.stdout.splitlines())


def skip(said, why):
    said.append(f"windvane not updated: {why}")
    return None


def newer_release(current, tags=None):
    """For a packaged install: the newest release tag (vX.Y.Z) above `current`, or None (also when the repo can't
    be reached). The tags, not PyPI: the package is installed from git."""
    if current == "0":  # not installed as a package: nothing to compare with
        return None
    if tags is None:
        r = net.call(["git", "ls-remote", "--tags", SOURCE], None, timeout=15,
                env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})  # never a password prompt in an unattended run
        tags = r.stdout if r.returncode == 0 else ""
    key = lambda v: tuple(int(x) for x in v.split("."))
    found = re.findall(r"refs/tags/v(\d+(?:\.\d+)*)$", tags, re.M)
    best = max(found, key=key, default=None)
    try:
        return best if best and key(best) > key(current) else None
    except ValueError:  # a local version like 0.1.0.dev1+g...: not comparable, say nothing
        return None


def gone(cmd):
    """The file a command `config.exe()` built can't start without, when it is missing or not executable: the program,
    or the interpreter a script's #! line names (a Python upgrade removes it from under a pipx venv); else None.
    Asked before an exec, whose own error names no file, and before the run hands over to a driver that can't start."""
    if not (os.path.isfile(cmd[0]) and os.access(cmd[0], os.X_OK)):
        return cmd[0]
    try:
        with open(cmd[0], "rb") as f:
            line = f.readline(256)
    except OSError:
        return cmd[0]
    words = line[2:].split() if line.startswith(b"#!") else []
    if words and not (os.path.isfile(words[0]) and os.access(words[0], os.X_OK)):
        return os.fsdecode(words[0])
    return None


def restart_args(argv, deadline):
    """`windvane run`'s own arguments for the restarted driver: the deadline as it is now (a `--for 6h` would start
    over), no tmux (it's in one already) and no preflight (nobody is at the terminal)."""
    out, skip_next = [], False
    for x in argv:
        if skip_next:
            skip_next = False
            continue
        if x in ("--until", "--for"):
            skip_next = True
            continue
        if x.startswith(("--until=", "--for=")) or x in ("--detach", "--no-preflight"):
            continue
        out.append(x)
    return [*out, "--until", deadline.strftime("%Y-%m-%d %H:%M"), "--no-preflight"]


def version():
    from importlib.metadata import PackageNotFoundError, version as v
    try:
        return v("windvane")
    except PackageNotFoundError:
        return "0"
