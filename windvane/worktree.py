"""The autopilot's own worktrees: made (and set up) when missing, reset to a commit between uses. Shared by the
driver, the plan run and held-back pushes, so neither of those imports the driver."""
import os
import shutil
from datetime import timedelta
from pathlib import Path

from . import net, privacy
from .util import git, log, now, run_limited, save_json


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


HOOKS = "hooks"  # under the state dir: one hooks folder per worktree a session pushes from


def guard_pushes(p, path):
    """Environment for a session that pushes from `path`: git's core.hooksPath, given in its environment config (never
    the repo's: the user's checkout shares it), on a hooks folder of windvane's own. Its pre-push scans what each push
    makes public (`privacy.pre_push`), then runs the repo's own pre-push; the repo's other hooks are linked in, so they
    run as before. Rebuilt for every session: the repo's hooks may have changed. Sessions push their own branches, so
    a scan in the driver would come too late: on a public repository a branch is public the moment it's pushed."""
    folder = hooks_folder(p, path)
    theirs = their_hooks(path)
    shutil.rmtree(folder, ignore_errors=True)
    folder.mkdir(parents=True)
    for hook in theirs.iterdir() if theirs.is_dir() else ():
        if hook.suffix != ".sample" and hook.name != "pre-push" and os.access(hook, os.X_OK):
            (folder / hook.name).symlink_to(hook)
    script = folder / "pre-push"
    own = theirs / "pre-push"
    script.write_text(privacy.hook_script(p.root, p.upstream, str(own) if os.access(own, os.X_OK) else ""))
    script.chmod(0o755)
    n = int(os.environ.get("GIT_CONFIG_COUNT") or 0)
    return {"GIT_CONFIG_COUNT": str(n + 1), f"GIT_CONFIG_KEY_{n}": "core.hooksPath",
            f"GIT_CONFIG_VALUE_{n}": str(folder)}


def hooks_folder(p, path):
    return p.state / HOOKS / Path(path).resolve().name


def their_hooks(path):
    """The repo's own hooks folder, as git finds it: the last core.hooksPath in its config files (relative to the
    worktree, as git reads it; not git's command line, where a session's own may be), else the common `.git/hooks`."""
    out = git(path, "config", "--show-scope", "--get-all", "core.hooksPath").stdout.splitlines()
    vals = [v for scope, _, v in (line.partition("\t") for line in out) if scope != "command" and v]
    if vals:
        return Path(path, Path(vals[-1]).expanduser())
    common = git(path, "rev-parse", "--git-common-dir").stdout.strip()
    return Path(path, common, "hooks").resolve()


TIMED_OUT = "out of time"  # run_limited's reason when `until` passes
# The last teardown's failure, for the next run's "started" push unless "told" (its own push said it)
TEARDOWN_FAILED = "teardown.json"


def teardown(p):
    """The project's `teardown`, once at a run's real end: it stops the helpers the project's tools started on purpose
    outside the sessions' process groups (a shared emulator, an adb server), which no session kill reaches. It runs in
    the ci worktree (the checkout with isolate off), or in a throwaway one when a failed setup left none, so the
    user's checkout is never touched. A failure or a timeout is logged and recorded (TEARDOWN_FAILED) for the next
    run's "started" push, since its helpers may still be up; one that ends well clears that record."""
    cmd = p.cfg["teardown"]
    if not cmd:
        return
    path, temp = p.ci_dir, None
    if path != p.main_root() and not (path / ".git").exists():
        temp = path = p.state / "teardown"
        shutil.rmtree(temp, ignore_errors=True)
        git(p.main_root(), "worktree", "prune")
        r = git(p.main_root(), "worktree", "add", "--detach", str(temp), p.upstream)  # no fetch: offline it'd hang
        if r.returncode:
            log(p, f"teardown `{cmd}` not run: can't create a worktree for it: {r.stderr.strip()}")
            return failed(p, cmd, "not run", r.stderr.strip())
    limit = p.cfg["teardown_minutes"]
    try:
        code, why, lines = run_limited(cmd, path, now() + timedelta(minutes=limit), keep=15)
    finally:
        if temp:
            git(p.main_root(), "worktree", "remove", "--force", str(temp))
            shutil.rmtree(temp, ignore_errors=True)
    if code or why:
        how = f"timed out after {limit:g} min (killed)" if why else f"failed (exit {code})"
        log(p, f"teardown `{cmd}` {how} in {short_path(p, path)}: {lines[-1] if lines else 'no output'}")
        if len(lines) > 1:
            log(p, "\n".join(["its last lines:", *lines]), echo=False)
        failed(p, cmd, f"timed out after {limit:g} min" if why else f"exit {code}", lines[-1] if lines else "")
    else:
        log(p, f"teardown `{cmd}` done")
        (p.state / TEARDOWN_FAILED).unlink(missing_ok=True)


def failed(p, cmd, how, last):
    """Records a failed teardown: when, the command, how it failed (exit, timeout) and its last line."""
    save_json(p.state / TEARDOWN_FAILED, {"at": now().isoformat(timespec="seconds"), "cmd": cmd, "how": how,
                                          "last": last})


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
