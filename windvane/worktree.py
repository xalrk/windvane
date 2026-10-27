"""The autopilot's own worktrees: made (and set up) when missing, reset to a commit between uses. Shared by the
driver, the plan run and held-back pushes, so neither of those imports the driver."""
import contextlib
import os
import re
import shlex
import shutil
import tempfile
from datetime import timedelta
from pathlib import Path

from . import net, privacy
from .util import git, groups_dir, load_json, log, now, run_limited, same_process, save_json, stamp


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
                                       keep=15, groups=groups_dir(p))
        if code or why:  # a half-made tree fails every check: remove it, so the next start sets it up afresh
            git(root, "worktree", "remove", "--force", str(path))
            shutil.rmtree(path, ignore_errors=True)
            git(root, "worktree", "prune")
            how = (f"timed out after {limit:g} min" if why == TIMED_OUT else f"cut short ({why})" if why
                   else f"failed (exit {code})")
            cmd, where = f"`{p.cfg['setup']}`", short_path(p, path)
            raise SetupFailed(f"setup {cmd} {how} in {where}", lines, why, how, f"{cmd} in {where}")


# What Claude Code makes in a session's cwd under CLAUDE_CODE_SUBPROCESS_ENV_SCRUB, for its sandbox to bind over: empty
# files and dirs. It lists them in .git/info/exclude, which fails in a linked worktree (.git is a file), so git shows
# them as untracked: a worker's `git add -A` commits them, and a slot holding them looks like a ticket's leftovers.
SCRUB_STUBS = (".env", ".env.local",
               *(f".env.{e}{s}" for e in ("development", "production", "test") for s in ("", ".local")),
               "bunfig.toml", "package.json", "package-lock.json", "yarn.lock", "pnpm-lock.yaml", ".npmrc", ".yarnrc",
               ".yarnrc.yml", ".gitmodules")
SCRUB_DIRS = (".claude/commands", ".claude/agents", "node_modules/.bin")


def clear_scrub_stubs(p, path):
    """Removes the stubs above from the autopilot's worktree `path`: only files untracked, empty and at its root, and
    the dirs when empty (then their parent, when that's empty too). Anything else stays: windvane never throws away
    work it didn't make. Never in the user's checkout. Returns the names removed, and logs them."""
    path = Path(path)
    if path.resolve() == p.main_root().resolve() or not (path / ".git").exists():
        return []
    r = git(path, "ls-files", "-z", "--", *SCRUB_STUBS)
    if r.returncode:  # can't tell what's tracked: remove nothing
        return []
    tracked = set(r.stdout.split("\0"))
    gone = []
    for name in SCRUB_STUBS:
        f = path / name
        if name not in tracked and f.is_file() and not f.is_symlink() and f.stat().st_size == 0:
            f.unlink()
            gone.append(name)
    for name in SCRUB_DIRS:
        d = path / name
        try:
            d.rmdir()  # only when empty: a tracked file in it, or anything else, keeps it
        except OSError:
            continue
        gone.append(name + "/")
        with contextlib.suppress(OSError):
            d.parent.rmdir()
    if gone:
        log(p, f"{short_path(p, path)}: removed the empty stubs a session under CLAUDE_CODE_SUBPROCESS_ENV_SCRUB left: "
               + ", ".join(gone))
    return gone


HOOKS = "hooks"  # under the state dir: one hooks folder per worktree a session pushes from


def guard_pushes(p, path):
    """Environment for a session that pushes from `path`: git's core.hooksPath for `path` only (`hooks_env`), given in
    its environment config (never the repo's: the user's checkout shares it), on a hooks folder of windvane's own. Its
    pre-push scans what each push makes public (`privacy.pre_push`), then runs the repo's own pre-push; the repo's other
    hooks are linked in, so they run as before. Rebuilt for every session: the repo's hooks may have changed. Sessions
    push their own branches, so a scan in the driver would come too late: on a public repository a branch is public the
    moment it's pushed."""
    folder = hooks_folder(p, path)
    theirs = their_hooks(path)
    own = theirs / "pre-push"
    with rebuild(folder) as new:
        for hook in theirs.iterdir() if theirs.is_dir() else ():
            if hook.suffix != ".sample" and hook.name != "pre-push" and os.access(hook, os.X_OK):
                (new / hook.name).symlink_to(hook)
        script = new / "pre-push"
        script.write_text(privacy.hook_script(p.root, p.upstream, str(own) if os.access(own, os.X_OK) else "",
                                              record=str(refusal_file(p, path))))
        script.chmod(0o755)
    return hooks_env(path, folder, submodule_hooks(p, path))


SUBMODULE_HOOKS = "submodule-hooks"  # under the state dir: the hooks of the submodules in a worktree a session uses
# The client-side hooks a submodule's own hooks folder may hold, which windvane's core.hooksPath there would hide. Not
# reference-transaction and the like, which git runs on every ref update.
CLIENT_HOOKS = ("applypatch-msg", "pre-applypatch", "post-applypatch", "pre-commit", "pre-merge-commit",
                "prepare-commit-msg", "commit-msg", "post-commit", "pre-rebase", "post-checkout", "post-merge",
                "post-rewrite", "pre-auto-gc")
# A stand-in for the submodule's own hook (argv 1): found as `their_hooks` finds it, in sh, as a python start on each
# commit and checkout would make them ten times slower.
OWN_HOOK = """d=$(git config --show-scope --get-all core.hooksPath |
    awk -F '\t' '$1 != "command" && $2 != "" { d = $2 } END { print d }')
case $d in "") d=$(git rev-parse --git-common-dir)/hooks ;; "~/"*) d=$HOME/${d#"~/"} ;; esac
h=$d/$1
shift
[ -f "$h" ] && [ -x "$h" ] || exit 0
exec "$h" "$@"
"""


def submodule_hooks(p, path):
    """The hooks folder of the submodules in `path`, (re)built: a submodule is a repo of its own, and its remote may
    be public too, so a session's push from one is scanned as well, against that remote (`privacy.pre_push`'s
    `own_remote`). One folder serves them all, so each hook finds the submodule's own when it runs (OWN_HOOK), even
    for a submodule the session itself adds."""
    folder = submodule_folder(p, path)
    own = shlex.quote(str(folder / "own-hook"))
    scripts = {name: f'#!/bin/sh\n# windvane: runs this submodule\'s own {name} hook\nexec sh {own} {name} "$@"\n'
               for name in CLIENT_HOOKS}
    scripts["pre-push"] = privacy.hook_script(p.root, "", "", own_remote=True)
    with rebuild(folder) as new:
        (new / "own-hook").write_text(OWN_HOOK)
        for name, text in scripts.items():
            (new / name).write_text(text)
            (new / name).chmod(0o755)
    return folder


def submodule_folder(p, path):
    return p.state / SUBMODULE_HOOKS / Path(path).resolve().name


HOOKS_CONFIG = ".gitconfig"  # in a hooks folder: the core.hooksPath its session's worktree includes


def hooks_env(path, folder, modules=None):
    """Environment config giving `path`'s repo `folder` as its core.hooksPath, and no other repo: a session's git
    environment reaches every git it runs, its check's included, so a plain core.hooksPath would also be the hooks of
    every repo a test clones or inits (`git lfs install` there refuses windvane's pre-push, a test's own hooks never
    run). The setting sits in a file in `folder`, included only where the git dir is `path`'s; no repo config is
    written. An included file reads as scope `command`, so `their_hooks` still skips it. `modules`: the hooks folder
    of `path`'s submodules, whose git dirs git keeps under `path`'s own (`modules/<name>`, nested ones further down),
    or in their own checkout (`in_place_submodules`), each named on its own: never a pattern for all of `path`."""
    gitdir = git(path, "rev-parse", "--absolute-git-dir").stdout.strip()
    places = [(glob_escape(gitdir), folder)]
    if modules:
        for where in [gitdir, *(str(sub / ".git") for sub in in_place_submodules(path))]:
            places += [(f"{glob_escape(where)}/modules/**", modules)]
            if where != gitdir:
                places += [(glob_escape(where), modules)]
    env, n = {}, int(os.environ.get("GIT_CONFIG_COUNT") or 0)
    for where, hooks in places:
        write_config(hooks)
        env |= {f"GIT_CONFIG_KEY_{n}": f"includeIf.gitdir:{where}.path",
                f"GIT_CONFIG_VALUE_{n}": str(Path(hooks, HOOKS_CONFIG))}
        n += 1
    return {"GIT_CONFIG_COUNT": str(n), **env}


def glob_escape(path):
    """`path` as a glob to git, where a `[` or `*` in it matches only itself."""
    return re.sub(r"([][*?\\])", r"\\\1", path)


def in_place_submodules(path):
    """The submodules `path`'s `.gitmodules` names whose git dir is a `.git` folder in their own checkout (a clone
    registered with `git submodule add <url> <path>`, a checkout from before git kept them in its own git dir), as
    resolved paths inside `path`. Read as the session starts: one cloned in place later isn't among them."""
    top = Path(path).resolve()
    if not (top / ".gitmodules").is_file():
        return []
    out = git(top, "config", "-z", "-f", ".gitmodules", "--get-regexp", r"^submodule\..*\.path$").stdout
    subs = []
    for entry in filter(None, out.split("\0")):
        sub = (top / entry.partition("\n")[2]).resolve()
        if sub != top and sub.is_relative_to(top) and (sub / ".git").is_dir() and sub not in subs:
            subs.append(sub)
    return subs


def write_config(folder, into=None):
    """`folder`'s HOOKS_CONFIG, giving it as core.hooksPath, in `into` (a build of it, `rebuild`) or in `folder`: only
    when its text differs, and by a rename, so a git reading it meanwhile never finds half of it."""
    conf = Path(into or folder, HOOKS_CONFIG)
    text = f"[core]\n\thooksPath = {quote_config(str(folder))}\n"
    with contextlib.suppress(OSError):
        if conf.read_text() == text:
            return
    part = conf.with_name(f"{HOOKS_CONFIG}.{os.getpid()}")
    part.write_text(text)
    os.replace(part, conf)


@contextlib.contextmanager
def rebuild(folder):
    """A new `folder`, filled in the `with` block in a build folder beside it, then swapped in (`swap`): a session
    already running in the worktree pushes through it meanwhile, and a driver killed halfway must leave a working
    pre-push, so `folder` is always the old build or the new one, never missing or half-written. The builds a killed
    driver left are cleared first."""
    folder = Path(folder)
    folder.parent.mkdir(parents=True, exist_ok=True)
    clear_leftovers(folder)
    new = Path(tempfile.mkdtemp(prefix=f".{folder.name}.{os.getpid()}.", dir=folder.parent))
    try:
        yield new
        write_config(folder, new)
        swap(folder, new)
    except BaseException:
        shutil.rmtree(new, ignore_errors=True)  # swap raises only before `folder` links to it
        raise


def swap(folder, new):
    """Makes `folder` a link to the build `new` with one rename, then removes the build it linked to. A plain folder
    (from before builds were swapped in) is moved aside first: the one moment it's missing, once. A failure leaves
    `folder` as it was."""
    link, aside = new.with_name(new.name + "_link"), None
    try:
        link.symlink_to(new.name)
        old = folder.parent / os.readlink(folder) if folder.is_symlink() else None
        if old is None and folder.is_dir():
            old = aside = new.with_name(new.name + "_old")
            os.rename(folder, aside)
        os.replace(link, folder)
    except BaseException:
        if aside and not folder.exists():
            os.rename(aside, folder)
        link.unlink(missing_ok=True)
        raise
    if old:
        shutil.rmtree(old, ignore_errors=True)


def clear_leftovers(folder):
    """Removes the builds and links beside `folder` of a process that has ended (a driver killed in `rebuild`), never
    the build `folder` links to, nor one a live driver is making."""
    current = os.readlink(folder) if folder.is_symlink() else None
    pattern = re.compile(rf"\.{re.escape(folder.name)}\.(\d+)\.[a-z0-9_]+")
    for entry in folder.parent.iterdir():
        m = pattern.fullmatch(entry.name)
        if not m or entry.name == current or alive(int(m[1])):
            continue
        if entry.is_dir() and not entry.is_symlink():
            shutil.rmtree(entry, ignore_errors=True)
        else:
            entry.unlink(missing_ok=True)


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    return True


def quote_config(value):
    """`value` as a git config file's quoted string."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def hooks_folder(p, path):
    return p.state / HOOKS / Path(path).resolve().name


REFUSALS = "push-refusals"  # under the state dir: the pre-push scan's last refusal in each worktree, `<tree>.json`


def refusal_file(p, path):
    return p.state / REFUSALS / f"{Path(path).resolve().name}.json"


def last_refusal(p, mine):
    """The newest refusal the pre-push scan noted in any of windvane's worktrees ({at, branch, line, base},
    `privacy.note_refusal`) for a branch `mine(branch)` accepts, or None. Any tree: a ticket may run in another slot
    than the one its push was refused in."""
    found = [r for f in (p.state / REFUSALS).glob("*.json") for r in [load_json(f, None)]
             if isinstance(r, dict) and r.get("line") and mine(r.get("branch", ""))]
    return max(found, key=lambda r: r.get("at", ""), default=None)


def forget_refusals(p, mine):
    """Drop the notes `last_refusal` would find for branches `mine(branch)` accepts: once the user fixed a branch by
    hand (their checkout's pushes skip windvane's hook, so nothing clears them) and sent its ticket back."""
    for f in (p.state / REFUSALS).glob("*.json"):
        r = load_json(f, None)
        if isinstance(r, dict) and mine(r.get("branch", "")):
            f.unlink(missing_ok=True)


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
        code, why, lines = run_limited(cmd, path, now() + timedelta(minutes=limit), keep=15, groups=groups_dir(p))
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


TRIALS = "trials"  # under the state dir: the critic's throwaway copies of a ticket's head, `<ticket>.<pid>`
TRIAL_HOOK = "#!/bin/sh\necho \"windvane: this is the critic's throwaway copy: no commits or pushes\" >&2\nexit 1\n"


@contextlib.contextmanager
def trial(p, cwd, rev, tid, stop=lambda: ""):
    """A throwaway detached worktree of `cwd`'s repo at `rev`, for the critic to run the change in, with the project's
    `setup` run there: yields (its path, git environment, a note for the critic's prompt), or (None, {}, why) when it
    can't be made, so the critic judges read-only instead. The worker's tree and the user's checkout are never
    touched. Its hooks refuse commits and pushes (behind the critic's permission rules: a second wall). It's removed
    however the critic ends; one a killed process left behind is removed by the next trial."""
    root = p.state / TRIALS
    root.mkdir(parents=True, exist_ok=True)
    sweep_trials(cwd, root)
    path = root / f"{tid}.{os.getpid()}"
    drop_trial(cwd, path)  # an earlier process's with this pid and no owner mark: this one makes one copy per ticket
    hooks = Path(f"{path}.hooks")
    hooks.mkdir()
    (hooks / "owner").write_text(stamp(os.getpid()) or "")  # before the copy, so a kill midway leaves it marked
    r = git(cwd, "worktree", "add", "--detach", str(path), rev)
    if r.returncode:
        drop_trial(cwd, path)
        yield None, {}, f"no copy to run it in ({r.stderr.strip() or 'git worktree add failed'}): judge read-only"
        return
    try:
        for name in ("pre-commit", "pre-merge-commit", "pre-push"):
            (hooks / name).write_text(TRIAL_HOOK)
            (hooks / name).chmod(0o755)
        note = ""
        if p.cfg["setup"]:
            limit = p.cfg["setup_minutes"]
            code, why, lines = run_limited(p.cfg["setup"], path, now() + timedelta(minutes=limit), stop, keep=3,
                                           groups=groups_dir(p))
            if code or why:
                note = (f"its setup `{p.cfg['setup']}` {'timed out' if why == TIMED_OUT else why or f'failed (exit {code})'}"
                        f"{': ' + lines[-1] if lines else ''}")
        yield path, hooks_env(path, hooks), note
    finally:
        drop_trial(cwd, path)


def drop_trial(cwd, path):
    """Removes a trial worktree, its hooks and git's record of it; a process killed inside `git worktree add` leaves
    it locked, which prune alone would keep, even once its folder is gone."""
    git(cwd, "worktree", "unlock", str(path))
    if path.exists():
        git(cwd, "worktree", "remove", "--force", "--force", str(path))
    shutil.rmtree(path, ignore_errors=True)
    shutil.rmtree(f"{path}.hooks", ignore_errors=True)
    git(cwd, "worktree", "prune")


def sweep_trials(cwd, root):
    """Removes the trials of processes that have ended (a driver or a `windvane critic` killed outright), and of a pid
    reused since: its `owner` mark names another process. By `stamp`, not wall-clock start times, which a step of
    the clock moves: a live critic's copy must never go mid-trial. Without a mark (killed before writing it) it
    goes by whether the pid is alive."""
    for entry in list(root.iterdir()):
        m = re.fullmatch(r"(.+\.(\d+))(?:\.hooks)?", entry.name)
        if not m:
            continue
        pid = int(m[2])
        try:
            recorded = Path(root, f"{m[1]}.hooks", "owner").read_text().strip() or None
        except OSError:
            recorded = None
        if not alive(pid) or recorded and same_process(pid, recorded) is False:
            drop_trial(cwd, root / m[1])
