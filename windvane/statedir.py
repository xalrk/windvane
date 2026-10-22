"""A project's state dir, kept lean. What runs leave there and nothing reads again grows until the disk is full (gigabytes
within days of steady runs): slot worktrees past their run, each with its own build output; session
transcripts (`runs/*.jsonl`: `stats` reads the ledger, `watch` only the newest stream); critic packs and `bg` job
logs of merged tickets. `reclaimable` lists what may go, with sizes, as `scratch.leftovers` does for temp dirs, and `clean` removes it.

Work is never lost: a worktree with a commit not on main, uncommitted changes, or a running session (the driver's
`running` slots, a process inside it) stays, and so does anything touched in the last hour (`scratch.AGE`). Slot 0's
`work`/`ci` and `plan/` stay too (the next run reuses them); with `trim_build` their ignored build output goes."""
import contextlib
import fcntl
import os
import re
import shutil
import time
from collections import namedtuple
from pathlib import Path

from . import heavy, jobs, scratch
from .config import state_settings
from .util import git, gout, load_json

DAY = 86400
SLOT = re.compile(r"(?:work|ci)-([1-9]\d*)")  # slot N's worktrees; slot 0's are `work` and `ci`
RUN = re.compile(r"\d{8}-\d{6}-(.+)\.jsonl")  # runner.py names a session's stream `<when>-<label>.jsonl`
CLEANING = "cleaning"  # in the state dir while `doctor --clean` holds a project's run lock: a run starting then waits
WAIT = 120  # seconds a starting run waits for such a clean (a few seconds a project) before it gives up
# One line of the listing: `paths` go together (a worktree, a trim, or every old transcript); `kind` says how.
Item = namedtuple("Item", "what size paths kind")


def settings(p):
    """`[state]` from the machine settings file, then the project's config.toml over it."""
    return state_settings(heavy.machine().get("state", {}), p.cfg.get("state", {}))


def human(n):
    """A size as `du -h` says it; a few bytes are 1K, never "freed 0K"."""
    return f"{n / 2 ** 30:.1f}G" if n >= 2 ** 30 else f"{n / 2 ** 20:.0f}M" if n >= 2 ** 20 else f"{-(-n // 1024)}K"


def merged_tickets(p):
    """The tickets merged on main (or marked done), or None when the repo can't be read: then nothing is known merged,
    and only the age rule applies."""
    from .queue import Queue
    try:
        return Queue(p).done
    except Exception:  # a deleted checkout, a broken config: keep everything that depends on it
        return None


def worktrees(root):
    """The worktrees git has registered for the checkout at `root` (resolved paths)."""
    out = gout(root, "worktree", "list", "--porcelain")
    return {Path(line[9:]).resolve() for line in out.splitlines() if line.startswith("worktree ")}


def in_use(d, procs):
    inside = str(d) + "/"
    return any(b == str(d) or b.startswith(inside) for b in procs)


def why_kept(p, wt, since, procs):
    """Why the slot worktree `wt` must stay, or "" when it can go."""
    if in_use(wt, procs):
        return "a process is working in it"
    if scratch._recent(wt, since):
        return "used in the last hour"
    st = git(wt, "--no-optional-locks", "status", "--porcelain")  # no index refresh: that would make it look used
    if st.returncode:
        return "git can't read it"
    if st.stdout.strip():
        return "uncommitted changes"
    if wt.name.startswith("ci-"):  # the driver's trial merges (GitHub makes its own): only a merged ticket's commits
        own = gout(wt, "rev-list", "--no-merges", "--count", "HEAD", "--not", p.upstream)
        return "" if own == "0" else f"commits not on {p.upstream}"
    if git(wt, "merge-base", "--is-ancestor", "HEAD", p.upstream).returncode:  # a worker's merge may hold a resolution
        head = gout(wt, "rev-parse", "--abbrev-ref", "HEAD")
        return f"commits not on {p.upstream}" + (f" ({head})" if head not in ("HEAD", "") else "")
    return ""


def busy_slots(p):
    """The slots the driver's last saved state has running: right while it runs, and kept safe after a crash."""
    return set(load_json(p.state / "state.json", {}).get("running", {}).values())


def slot_items(p, busy, keep_slots, since, procs):
    """(items, kept): idle slot worktrees with nothing unmerged, and (name, why) for each one that stays."""
    items, kept = [], []
    root = p.main_root()
    registered = worktrees(root) if (root / ".git").exists() else set()
    for d in sorted(p.state.iterdir()) if p.state.is_dir() else ():
        m = SLOT.fullmatch(d.name)
        if not m or not d.is_dir() or d.resolve() not in registered:
            continue  # not a slot, or a tree git doesn't know: never removed by hand
        slot = int(m.group(1))
        why = ("its ticket is running" if slot in busy else "this run may use it" if slot < keep_slots
               else why_kept(p, d, since, procs))
        if why:
            kept.append((d.name, why))
        else:
            items.append(Item(f"{d.name} (idle worktree, nothing unmerged)", scratch.size(d)[1], [d], "worktree"))
    return items, kept


def ignored(d):
    """The ignored files and dirs in worktree `d` that `git clean -fdX` would remove."""
    out = gout(d, "clean", "-ndX")
    return [d / line[len("Would remove "):] for line in out.splitlines() if line.startswith("Would remove ")]


def trim_items(p, busy, since, procs):
    """Slot 0's worktrees and the planner's: kept, but their build output can go while nothing uses them."""
    root, out = p.main_root(), []
    trees = ([] if 0 in busy else [p.work_dir, p.ci_dir]) + [p.plan_dir]
    for d in trees:
        if d == root or not (d / ".git").exists() or scratch._recent(d, since) or in_use(d, procs):
            continue
        paths = ignored(d)
        size = sum(scratch.size(x)[1] if x.is_dir() else x.lstat().st_size for x in paths if x.exists())
        if size:
            out.append(Item(f"{d.name}/ build output (ignored files)", size, [d], "trim"))
    return out


def run_items(p, done, keep_days, keep_runs, now):
    """Transcripts older than the window, except each unmerged ticket's newest `keep_runs`."""
    files = sorted(p.state.glob("runs/*.jsonl"), key=lambda f: f.stat().st_mtime, reverse=True)
    old, mine = now - max(keep_days * DAY, scratch.AGE), {}
    out = []
    for f in files:
        m = RUN.fullmatch(f.name)
        tid = m and re.match(rf"({p.id_re})(?:-|$)", m.group(1), re.I)
        tid = p.norm(tid.group(1)) if tid else None
        if tid and (done is None or tid not in done):
            mine[tid] = mine.get(tid, 0) + 1
            if mine[tid] <= keep_runs:
                continue
        if f.stat().st_mtime < old:
            out.append(f)
    if not out:
        return []
    size = sum(f.stat().st_size for f in out)
    return [Item(f"runs/ ({len(out)} transcript{'s' * (len(out) > 1)} over {keep_days:g} days old)", size, out,
                 "files")]


def critic_items(p, done, keep_days, now):
    """Critic evidence packs of merged tickets, untouched for the window."""
    if not done or not (p.state / "critic").is_dir():
        return []
    since = now - max(keep_days * DAY, scratch.AGE)
    dirs = [d for d in sorted((p.state / "critic").iterdir())
            if d.is_dir() and p.norm(d.name) in done and not scratch._recent(d, since)]
    if not dirs:
        return []
    size = sum(scratch.size(d)[1] for d in dirs)
    whose = f"{len(dirs)} merged " + ("tickets'" if len(dirs) > 1 else "ticket's")
    span = f"{dirs[0].name} to {dirs[-1].name}" if len(dirs) > 1 else dirs[0].name
    return [Item(f"critic/ ({whose} packs: {span})", size, dirs, "files")]


def bg_items(p, done, keep_days, now):
    """Merged tickets' `windvane bg` job dirs (logs, pids, locks), untouched for the window. The driver drops a
    ticket's as it merges; one merged elsewhere, or by a driver that died first, would stay for good. A dir with a job
    still running, or kept (`bg --keep`) with time still to run, stays."""
    if not done or not (p.state / "bg").is_dir():
        return []
    since = now - max(keep_days * DAY, scratch.AGE)
    dirs = [d for d in sorted((p.state / "bg").iterdir())
            if d.is_dir() and p.norm(d.name) in done and not scratch._recent(d, since) and not jobs_live(d, now)]
    if not dirs:
        return []
    size = sum(scratch.size(d)[1] for d in dirs)
    whose = f"{len(dirs)} merged " + ("tickets'" if len(dirs) > 1 else "ticket's")
    return [Item(f"bg/ ({whose} job logs)", size, dirs, "files")]


def desk_job_items(p, keep_days, now):
    """The files of jobs right under bg/ (the desk's, and tickets' from before they had a dir of their own) that ended
    and were untouched for the window: no ticket's merge drops them."""
    since = now - max(keep_days * DAY, scratch.AGE)
    paths = []
    for start in sorted((p.state / "bg").glob("*.start")):
        base = start.with_suffix("")
        files = jobs.job_files(base)
        if not any(f.stat().st_mtime >= since for f in files) and not job_live(base, now):
            paths += files
    if not paths:
        return []
    n = sum(f.suffix == ".start" for f in paths)
    return [Item(f"bg/ ({n} desk job{'s' * (n > 1)} ended over {keep_days:g} days ago)",
                 sum(f.stat().st_size for f in paths), paths, "files")]


def jobs_live(d, now):
    """Whether a job in bg dir `d` runs, or is kept until a time still ahead."""
    bases = {f.with_suffix("") for f in d.glob("*.start")} | {f.with_suffix("") for f in d.glob("*.keep")}
    return any(job_live(b, now) for b in bases)


def job_live(base, now):
    """Whether the bg job at `base` runs, or is kept until a time still ahead."""
    return jobs.alive(base) or Path(f"{base}.keep").exists() and (jobs.kept_until(base) or 0) > now


def survey(p, busy=None, keep_slots=0, trim=None, procs=None, now=None):
    """(items biggest first, kept worktrees as (name, why)). `busy`: the slots running sessions use (default: the
    driver's saved `running`); `keep_slots`: slots below it stay whatever their state (a run that may use them again);
    `trim`: whether build output counts (default: the `trim_build` setting)."""
    if not p.state.is_dir():
        return [], []
    s, now = settings(p), time.time() if now is None else now
    busy = busy_slots(p) if busy is None else set(busy)
    procs = scratch.Busy() if procs is None else procs
    since = now - scratch.AGE
    items, kept = slot_items(p, busy, keep_slots, since, procs)
    if s["trim_build"] if trim is None else trim:
        items += trim_items(p, busy, since, procs)
    done = merged_tickets(p)
    items += run_items(p, done, s["keep_days"], s["keep_runs"], now)
    items += critic_items(p, done, s["keep_days"], now)
    items += bg_items(p, done, s["keep_days"], now)
    if (p.state / "bg").is_dir():
        items += desk_job_items(p, s["keep_days"], now)
    return sorted(items, key=lambda i: -i.size), kept


def reclaimable(p, **kw):
    return survey(p, **kw)[0]


def remove(p, item):
    """Remove one item; whether it went. A worktree goes through git (never a bare rmtree on a registered tree), and
    git refuses one that changed since the survey."""
    root = p.main_root()
    if item.kind == "worktree":
        ok = git(root, "worktree", "remove", str(item.paths[0])).returncode == 0
        git(root, "worktree", "prune")
        return ok
    if item.kind == "trim":
        d = item.paths[0]
        if d == p.plan_dir:  # the planner, a preflight or a held push may start in it meanwhile
            with open(p.state / "plan.lock", "w") as lock:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError:
                    return False
                return git(d, "clean", "-qfdX").returncode == 0
        return git(d, "clean", "-qfdX").returncode == 0
    for x in item.paths:
        shutil.rmtree(x, ignore_errors=True) if x.is_dir() else x.unlink(missing_ok=True)
    return True


@contextlib.contextmanager
def lock(p):
    """The project's run lock, held while `doctor --clean` cleans a project no run is using: a run starting meanwhile
    waits (it sees CLEANING) rather than prepare a slot tree the clean is removing. Yields False when a run holds it."""
    with open(p.state / "lock", "a") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield False
            return
        mark = p.state / CLEANING
        mark.write_text(f"{os.getpid()}\n")
        try:
            yield True
        finally:  # let go first: a run waiting on the mark would otherwise see the lock held and no mark
            fcntl.flock(f, fcntl.LOCK_UN)
            mark.unlink(missing_ok=True)


def clean(p, **kw):
    """Remove what `reclaimable` lists; (items removed, bytes freed)."""
    n = freed = 0
    for item in reclaimable(p, **kw):
        if remove(p, item):
            n, freed = n + 1, freed + item.size
    return n, freed
