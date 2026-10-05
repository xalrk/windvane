"""The autopilot's own worktrees: made (and set up) when missing, reset to a commit between uses. Shared by the
driver, the plan run and held-back pushes, so neither of those imports the driver."""
import shutil
from datetime import timedelta
from pathlib import Path

from . import net
from .util import git, log, now, run_limited


def ensure_worktree(p, path, setup=True, stop=lambda: ""):
    """The autopilot's own worktree at `path`, created detached at upstream (and set up) when missing. With
    isolate off, work_dir and ci_dir are the checkout itself, which is left alone. `stop()` gives a reason to end
    the setup early (the run's stop or deadline), raised as SetupFailed's `killed`."""
    if path == p.main_root() or (path / ".git").exists():
        return
    root = p.main_root()
    net.git(root, "fetch", "-q", p.remote)
    git(root, "worktree", "prune")
    r = git(root, "worktree", "add", "--detach", str(path), p.upstream)
    if r.returncode:
        raise SystemExit(f"windvane: can't create the worktree {path}: {r.stderr.strip()}")
    if setup and p.cfg["setup"]:
        log(p, f"setting up {path}: {p.cfg['setup']}…")
        limit = p.cfg["setup_minutes"]
        code, why, lines = run_limited(p.cfg["setup"], path, now() + timedelta(minutes=limit), stop, echo=True,
                                       keep=15)
        if code or why:  # a half-made tree fails every check: remove it, so the next start sets it up afresh
            git(root, "worktree", "remove", "--force", str(path))
            shutil.rmtree(path, ignore_errors=True)
            git(root, "worktree", "prune")
            how = (f"timed out after {limit:g} min" if why == TIMED_OUT else f"cut short ({why})" if why
                   else f"failed (exit {code})")
            cmd, where = f"`{p.cfg['setup']}`", short_path(p, path)
            raise SetupFailed(f"setup {cmd} {how} in {where}", lines, why, how, f"{cmd} in {where}")


TIMED_OUT = "out of time"  # run_limited's reason when `until` passes


def short_path(p, path):
    """`path` as the user knows it: relative to the state dir (`work-1`), else its last two parts."""
    try:
        return str(Path(path).relative_to(p.state))
    except ValueError:
        return str(Path(*Path(path).parts[-2:]))


class SetupFailed(Exception):
    """The project's `setup` command failed in a fresh worktree; `lines` are the end of its output, `killed` why it
    was killed (TIMED_OUT at `setup_minutes`, or the stop rule's reason), "" when it failed on its own."""

    def __init__(self, why, lines, killed="", how="", ran=""):
        super().__init__(why)
        self.lines, self.killed, self.how, self.ran = lines, killed, how, ran

    @property
    def cut(self):
        """Ended by the run's stop or deadline: the run ends as a stop, with nothing to fix."""
        return self.killed not in ("", TIMED_OUT)


def clean_detach(p, cwd, rev):
    """Put the driver's own worktree on `rev`, discarding anything left there. Never used on the worker's tree."""
    git(cwd, "merge", "--abort")
    git(cwd, "reset", "-q", "--hard")
    git(cwd, "clean", "-qfd")
    return git(cwd, "switch", "-q", "--detach", rev).returncode == 0
