"""Keeps the running windvane current between tickets: what a run merges into windvane (or what lands upstream) is
used from the next ticket on, instead of after someone pulls and restarts the driver by hand."""
import os
import re
import time

from . import net
from .util import git, gout, load_json, run, save_json

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


# ---------- trial: new driver code that breaks the run is reverted

TRIAL_S = 3600  # new driver code is on trial until it merges a ticket or has run this long
TRIAL, REVERTED = "trial.json", "reverted.json"  # in the project's state dir


class Unreverted(Exception):
    """Why a bad update couldn't be reverted on main: the run stops instead."""


def put_on_trial(state, good, new, merged):
    """Called by a driver about to restart on `new` code: the restarted driver (and `supervise`, which outlives it)
    read it. `good` is the last code that passed its trial; `merged` the run's merge count now."""
    save_json(state / TRIAL, {"good": good, "new": new, "since": time.time(), "merged": merged, "seen": {},
                              "crashes": 0})


def on_trial(state):
    """The trial under way, or None (none, or it ran its hour: the code is kept)."""
    t = load_json(state / TRIAL, None)
    if t and time.time() - t["since"] >= TRIAL_S:
        end_trial(state)
        return None
    return t


def end_trial(state):
    (state / TRIAL).unlink(missing_ok=True)


def signature(why, id_re):
    """A setback's words with what differs between tickets taken out (ids, branch names, shas, numbers), so two
    tickets that failed the same way read the same."""
    s = re.sub(rf"\b{id_re}[\w.-]*", "<id>", why, flags=re.I)
    s = re.sub(r"\b[0-9a-f]{7,40}\b", "<sha>", s)
    return re.sub(r"\d+(\.\d+)?", "<n>", s)


def setback(state, tid, step, why, id_re):
    """Records ticket `tid`'s first setback on trial (a failed check, a failed ticket); True when the update is bad:
    two tickets or more have had one, and all at the same step with the same error."""
    t = on_trial(state)
    if not t:
        return False
    t["seen"].setdefault(tid, [step, signature(why, id_re), why])
    save_json(state / TRIAL, t)
    sigs = {tuple(v[:2]) for v in t["seen"].values()}
    return len(t["seen"]) >= 2 and len(sigs) == 1


def to_revert(root, upstream, good, new):
    """The commits on `upstream`'s first-parent line between `good` and `new` that changed driver code, newest first,
    as (sha, is a merge, subject); None when `new` isn't in `upstream`'s history (the update came from elsewhere)."""
    if run(["git", "merge-base", "--is-ancestor", new, upstream], root).returncode or \
            run(["git", "merge-base", "--is-ancestor", good, new], root).returncode:
        return None
    out = []
    for line in gout(root, "log", "--first-parent", "--format=%H %P%x1f%s", f"{good}..{new}").splitlines():
        shas, _, subject = line.partition("\x1f")
        sha, *parents = shas.split()
        files = gout(root, "diff", "--name-only", parents[0] if parents else f"{sha}^", sha).splitlines()
        if any(f.startswith(CODE) for f in files):
            out.append((sha, len(parents) > 1, subject))
    return out


def reverted_ids(commits, p):
    """The ticket ids the reverted commits brought: from a merge's branch name, or a squashed commit's `T-1:` subject."""
    from .queue import merged_branches
    bp = re.escape(p.prefix.lower())
    ids = []
    for _, merge, subject in commits:
        found = merged_branches(subject, bp) if merge else re.findall(rf"^({p.id_re}):", subject, re.I)
        ids += [t for t in map(p.norm, found) if t and t not in ids]
    return ids


def revert_update(p, cd, trial, how, log):
    """Lands on main, through the driver's own landing path (a PR on github, a push on git: never a force push), a
    revert of the commits that brought the code on trial, and a ticket to find and fix what broke. The reverted
    tickets go back in the queue after it (the revert's subject names them: `Queue` reads them as not done) and wait
    for it. Returns (where it landed, the reverted ids, the new ticket's id, the revert's sha); raises Unreverted."""
    from . import held, planner, worktree
    root, good, new = p.main_root(), trial["good"], trial["new"]
    net.git(root, "fetch", "-q", p.remote)
    commits = to_revert(root, p.upstream, good, new)
    if not commits:
        raise Unreverted(f"{new[:8]} isn't on {p.upstream}, so its change isn't this project's to revert"
                         if commits is None else f"no commit since {good[:8]} changed the driver's code")
    worktree.ensure_worktree(p, cd)
    worktree.clean_detach(p, cd, p.upstream)
    try:
        for sha, merge, _ in commits:
            if git(cd, "revert", "--no-commit", *(["-m", "1"] if merge else []), sha).returncode:
                raise Unreverted(f"reverting {sha[:8]} conflicts with {p.main} since")
        ids = reverted_ids(commits, p)
        title = f"Revert {', '.join(ids)}: " if ids else "Revert: "
        title += f"windvane {new[:8]} {how}"[:200]
        body = "Reverted, newest first:\n" + "\n".join(f"- {s[:8]} {subj}" for s, _, subj in commits)
        if git(cd, *held.author_ident(p), "commit", "-q", "-m", title, "-m", body).returncode:
            raise Unreverted("the revert commit failed")
        revert = gout(cd, "rev-parse", "HEAD")
        with planner.id_lock(p):
            tid = planner.free_id(p, cd)
            fix_ticket(p, cd, tid, trial, how, commits, ids, revert)
        git(cd, "add", "-A", p.tickets_rel, p.queue_rel)
        git(cd, *held.author_ident(p), "commit", "-q", "-m", f"{tid}: find and fix what broke windvane {new[:8]}")
        landed = held.land(p, cd, planner.branch_name(p, cd, "revert"), title, body, commit=False, fresh=True)
    finally:
        worktree.clean_detach(p, cd, p.upstream)
    if not landed.ok:
        raise Unreverted(f"the revert waits on {landed.where}")
    log(f"windvane {new[:8]} reverted on {p.main} ({', '.join(ids) or 'no ticket'}); {tid} is to fix it")
    return landed.where, ids, tid, revert


def fix_ticket(p, cd, tid, trial, how, commits, ids, revert):
    """Writes `tid` (what broke, and what was reverted) in checkout `cd`, puts it at the queue's top with the
    reverted tickets after it, and makes each of them wait for it."""
    good, new = trial["good"], trial["new"]
    listed = "\n".join(f"- `{s[:8]}` {subj}" for s, _, subj in commits)
    text = (f"# {tid}: Find and fix what broke windvane {new[:8]}\nDepends on: nothing.\n\n## Goal\nOn trial after a "
            f"self-update, windvane {new[:8]} {how}. The run reverted these on {p.main} in `{revert[:8]}` and went on "
            f"on {good[:8]}:\n{listed}\n\n## Scope\n- Reproduce it (the reverted change is `git show {revert[:8]}`, "
            "inverted), find the cause, and fix it with a test that fails without the fix.\n"
            + (f"- {', '.join(ids)} wait for this ticket, then redo their change on top of the fix.\n" if ids else "")
            + "\n## Done when\n- A test shows the error above can't happen again.\n")
    (cd / p.tickets_rel).mkdir(parents=True, exist_ok=True)
    (cd / p.tickets_rel / f"{tid}.md").write_text(text)
    for t in ids:
        f = cd / p.tickets_rel / f"{t}.md"
        if f.is_file():
            f.write_text(waits_for(f.read_text(), tid) + f"\n## Reverted\nIts merge was reverted in `{revert[:8]}` "
                         f"({how}); it waits for {tid}, then redoes its change.\n")
    q = cd / p.queue_rel
    text = q.read_text() if q.is_file() else "# Queue\n"
    lines = "".join(f"- {t}\n" for t in [tid, *ids])
    m = re.search(rf"^\s*[-*]\s+(?:\*\*)?{p.id_re}\b", text, re.M | re.I)
    q.write_text(text[:m.start()] + lines + text[m.start():] if m else text.rstrip("\n") + "\n\n" + lines)


def waits_for(text, tid):
    """A ticket's text with `tid` added to its `Depends on:` line (one put under its title when it has none)."""
    m = re.search(r"^((?:\*\*)?Depends on:\**)(.*)$", text, re.M | re.I)
    if not m:
        title, _, rest = text.partition("\n")
        return f"{title}\nDepends on: {tid}.\n{rest}"
    rest = m.group(2).strip()
    new = f" {tid}." if not rest or rest.lower().rstrip(". ").split()[0] in ("nothing", "none") else \
        f" {tid}, {rest.lstrip()}"
    return text[:m.start(2)] + new + text[m.end(2):]
