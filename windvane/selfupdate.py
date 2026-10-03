"""Keeps the running windvane current between tickets: what a run merges into windvane (or what lands upstream) is
used from the next ticket on, instead of after someone pulls and restarts the driver by hand."""
import os
import re

from . import net
from .util import gout, run

CODE = ("windvane/", "bin/", "prompts/")  # read once by the driver: a change here needs a restart
SOURCE = "https://github.com/xalrk/windvane"  # where `pipx install git+...` took it from


def pull(home, said):
    """Fast-forwards a clean checkout of `main` to its remote's; "restart" when driver code changed, else None.
    Anything else (local changes, another branch, local commits) is left alone: the reason goes into `said`, which
    the caller logs once."""
    branch = gout(home, "rev-parse", "--abbrev-ref", "HEAD")
    if branch != "main":
        return skip(said, f"on branch {branch or '?'}, not main")
    if gout(home, "status", "--porcelain", "--untracked-files=no"):
        return skip(said, "it has local changes")
    r = net.call(["git", "fetch", "-q", "origin", "main"], home, timeout=60)  # waits out an outage in a run
    if r.returncode:
        return skip(said, "git fetch timed out" if r.returncode == 124 else "git fetch failed")
    old, new = gout(home, "rev-parse", "HEAD"), gout(home, "rev-parse", "FETCH_HEAD")
    if old == new or not new:
        return None
    if run(["git", "merge-base", "--is-ancestor", "HEAD", new], home).returncode:
        return skip(said, "its main has commits origin/main doesn't")
    if run(["git", "merge", "-q", "--ff-only", new], home).returncode:
        return skip(said, "the fast-forward failed")
    said.append(f"windvane updated to {new[:8]}")
    changed = gout(home, "diff", "--name-only", old, new).splitlines()
    return "restart" if any(f.startswith(CODE) for f in changed) else None


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
