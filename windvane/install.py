"""`windvane install` / `uninstall`: bring windvane into Claude Code (the skill, the mention hook) and onto PATH,
asking before each change, and take back exactly what it added. What it added is listed in
`~/.local/state/windvane/install.json`, so uninstall never touches anything the user made or changed themselves, and
puts `settings.json` back byte for byte when nothing else in it changed meanwhile."""
import contextlib
import fcntl
import json
import os
import re
import shlex
import shutil
import sys
import time
from pathlib import Path

from .config import CHECKOUT, HOME, NAME_CHARS, exe, user_dir

# How to put ~/.local/bin (where install.sh, pipx and uv put the command) on PATH: in sh, bash, zsh and fish alike
PATH_LINE = 'export PATH="$HOME/.local/bin:$PATH"'
LOCK_WAIT = 10  # seconds: an `install` waiting on its questions holds the lock that long at most for the others


class Unreadable(Exception):
    """install.json is there but can't be read: saving over it would lose the list of what windvane placed, so
    uninstall couldn't take it back."""


def manifest_path():
    return user_dir("XDG_STATE_HOME", ".local/state") / "install.json"


def load():
    """The record of what windvane placed: empty when there's none yet; Unreadable when there is one it can't read."""
    f = manifest_path()
    try:
        m = json.loads(f.read_text())
    except FileNotFoundError:
        return {"paths": [], "settings": []}
    except (OSError, ValueError) as e:
        raise Unreadable(f"{f} can't be read ({e}), so it's left as it is: fix or delete it") from None
    if not (isinstance(m, dict) and isinstance(m.get("paths"), list) and isinstance(m.get("settings"), list)):
        raise Unreadable(f"{f} isn't a windvane install record, so it's left as it is: fix or delete it")
    return m


def save(m):
    f = manifest_path()
    if not (m["paths"] or m["settings"]):
        f.unlink(missing_ok=True)
        return
    f.parent.mkdir(parents=True, exist_ok=True)
    write_json(f, m)


@contextlib.contextmanager
def changing():
    """install.json to change, loaded under a lock held until the change is saved, so an `init`, an `install` and
    runs starting at the same moment don't lose each other's entries. Saved only when the block changed it and
    didn't raise. Raises Unreadable (nothing changed) for a record it can't read, TimeoutError when it stays busy."""
    lock = manifest_path().with_name("install.json.lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    with open(lock, "a") as fh:
        until = time.time() + LOCK_WAIT
        while True:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.time() > until:
                    raise TimeoutError(f"{manifest_path()} is busy: another windvane install, init or run is "
                                       "changing it; try again in a moment") from None
                time.sleep(0.1)
        m = load()
        before = json.dumps(m)
        yield m
        if json.dumps(m) != before:
            save(m)


def claude_dir():
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")


def hook_command():
    return shlex.quote(str(HOME / "hooks/mention_hook.py"))


def ask(question, yes):
    """Yes with --yes; otherwise the user's answer, and no when there's no terminal to ask in."""
    if yes:
        print(f"{question} yes (--yes)")
        return True
    if not sys.stdin.isatty():
        print(f"{question} skipped: no terminal to ask in (`windvane install --yes` makes every change)")
        return False
    return input(f"{question} [Y/n] ").strip().lower() in ("", "y", "yes")


def make_dirs(d, m):
    """mkdir -p, recording each directory it creates so uninstall can remove it again (when empty)."""
    missing = []
    while not d.exists():
        missing.append(d)
        d = d.parent
    for x in reversed(missing):
        x.mkdir()
        m["paths"].append({"path": str(x), "kind": "dir"})


def place_link(target, source, what, yes, m):
    """Link `target` to `source` (a copy where links fail), replacing only an older link, never a real file."""
    if target.is_symlink() and target.resolve() == source.resolve():
        print(f"{what}: already in place ({target})")
        return
    if target.exists() and not target.is_symlink():
        print(f"{what}: {target} exists and isn't a link, so it's left alone")
        return
    if not ask(f"{what}: link {target} -> {source}?", yes):
        return
    was = os.readlink(target) if target.is_symlink() else None
    make_dirs(target.parent, m)
    target.unlink(missing_ok=True)
    try:
        target.symlink_to(source)
        kind = "link"
    except OSError:  # no symlinks here (some Windows setups): a copy, refreshed by the next install
        shutil.copytree(source, target) if source.is_dir() else shutil.copy2(source, target)
        kind = "copy"
    m["paths"].append({"path": str(target), "kind": kind, "source": str(source), "was": was})


def install(yes=False):
    try:
        with changing() as m:
            install_into(m, yes)
    except (Unreadable, TimeoutError) as e:
        print(f"windvane install: nothing changed: {e}")
        return
    tidy_phone_hooks()
    if not shutil.which("windvane"):
        print(f"windvane isn't on PATH yet: run `{PATH_LINE}`, and add it to your shell's startup file")
    print("next: windvane doctor (what this machine still lacks); windvane uninstall takes all of this back")


def install_into(m, yes):
    if CHECKOUT:  # pipx, uv and install.sh put the installed command on PATH themselves
        place_link(Path.home() / ".local/bin/windvane", HOME / "bin/windvane", "the windvane command", yes, m)
    skill = claude_dir() / "skills/windvane"
    place_link(skill, HOME / "skills/windvane", "the windvane skill (the playbook sessions load)", yes, m)
    settings = claude_dir() / "settings.json"
    if has_keyword_hook(settings):
        print(f"the mention hook: already in {settings}")
    elif ask(f"the mention hook (brings windvane in when a prompt names it or autopilot): add it to {settings}?",
             yes):
        before = settings.read_text() if settings.exists() else None
        make_dirs(settings.parent, m)
        if add_keyword_hook(settings):
            m["settings"].append({"path": str(settings), "command": hook_command(), "before": before})


def uninstall(yes=False):
    try:
        with changing() as m:
            removed = uninstall_from(m, yes)
    except (Unreadable, TimeoutError) as e:
        print(f"windvane uninstall: nothing removed: {e}")
        return
    if removed:  # install.json.lock stays: removing it could let two later writers lock different files
        print(f"removed. {removal_hint()}")


def uninstall_from(m, yes):
    """Take back what `m` lists, emptying it; whether it did."""
    items = [e["path"] for e in m["paths"] if e["kind"] != "dir"] + [f"the mention hook in {s['path']}"
                                                                       for s in m["settings"]]
    if not items:
        print("nothing to remove: windvane install hasn't added anything (or it was removed already)")
        return False
    print("windvane added:\n  " + "\n  ".join(items))
    if not ask("remove all of it?", yes):
        return False
    for s in reversed(m["settings"]):
        remove_keyword_hook(Path(s["path"]), s["command"], s["before"])
    for e in reversed(m["paths"]):
        remove_path(e)
    m["paths"].clear()
    m["settings"].clear()
    return True


def removal_hint():
    """How to remove windvane itself, which depends on how it was installed: a checkout, pipx, uv or install.sh."""
    if CHECKOUT:
        return f"The checkout itself stays: delete {HOME} when you no longer want it."
    prefix = Path(sys.prefix).resolve()
    if prefix == (user_dir("XDG_DATA_HOME", ".local/share") / "venv").resolve():
        return f"The package itself (install.sh's): rm -r {prefix.parent} ~/.local/bin/windvane"
    if "pipx" in prefix.parts:
        return "The package itself: pipx uninstall windvane"
    if "uv" in prefix.parts:
        return "The package itself: uv tool uninstall windvane"
    return f"The package itself: {Path(sys.executable).name} -m pip uninstall windvane (in {prefix})"


def remove_path(e):
    t = Path(e["path"])
    kind = e["kind"]
    if kind == "dir":
        try:
            t.rmdir()
        except OSError:  # gone, or something else lives there now
            pass
    elif kind == "link":
        if t.is_symlink() and os.readlink(t) == e["source"]:
            t.unlink()
            if e.get("was") and (t.parent / e["was"]).exists():  # the link it replaced, unless that was broken
                t.symlink_to(e["was"])
        elif t.exists() or t.is_symlink():
            print(f"{t} changed since install, so it's left alone")
    elif kind == "copy":
        shutil.rmtree(t, ignore_errors=True) if t.is_dir() else t.unlink(missing_ok=True)
    elif kind == "file":
        t.unlink(missing_ok=True)


def phone_hooks(p):
    """The phone's hooks for a project (ntfy-listen runs them), when ntfy is set up here: `stop` and `overview` per
    project, and one `status` for them all that lists only the autopilots that are on."""
    base = ntfy_dir()
    if not base.exists():
        return []
    try:
        with changing() as m:
            write_project_hooks(p, m)
            if other := write_status_hook(m):
                print(f"  phone status: kept with the windvane that set it up ({other})")
            tidy_hooks(m)
    except (Unreadable, TimeoutError) as e:
        print(f"  phone hooks: not written: {e}")
        return []
    return [str(base / k) for k in ("status.d", "stop.d", "overview.d")]


def write_project_hooks(p, m, missing_only=False):
    """A project's `stop` and `overview` hooks (with `missing_only`, only those that aren't there)."""
    cmd = shlex.join(exe())
    for kind, args in (("stop.d", "stop --if-running"), ("overview.d", "overview")):
        f = ntfy_dir() / kind / f"windvane-{p.name}"
        if not (missing_only and (f.exists() or f.is_symlink())):
            write_hook(f, f"{p.name}: `{args.split()[0]}`", f"{cmd} -C {shlex.quote(str(p.main_root()))} {args}", m)


def ntfy_dir():
    return Path.home() / ".config/ntfy"


def write_hook(f, what, command, m):
    """Write a hook script windvane owns. A file there is the user's, left alone, unless install.json lists it or it
    has the form windvane writes (hooks from before install.json listed them, which get listed now)."""
    listed = any(e["path"] == str(f) for e in m["paths"])
    if f.exists() and not listed and not ours(f):
        print(f"{f} isn't windvane's, so it's left alone")
        return
    make_dirs(f.parent, m)
    if not listed:
        m["paths"].append({"path": str(f), "kind": "file"})
    f.write_text(f"#!/usr/bin/env bash\n# windvane {what} from the phone (ntfy-listen)\nexec {command}\n")
    f.chmod(0o755)


def write_status_hook(m):
    """The shared `status` hook, unless another windvane install on this machine wrote it and its command is still
    there: then it stays that install's, so an `init` from a second copy doesn't move every project's `status` to it
    (that command is returned)."""
    f = ntfy_dir() / "status.d/windvane"
    if other := other_install(f):
        return other
    write_hook(f, "every project: `status`, only the autopilots that are on",
               f"{shlex.join(exe())} status --line --running", m)


def other_install(f):
    """The command of another windvane install that the hook `f` runs, when that command still exists."""
    cmd = status_command(f)
    return shlex.join(cmd) if cmd and cmd != exe() and shutil.which(cmd[0]) else None


def status_command(f):
    """The windvane command a `status` hook runs (the words between `exec` and `status`), or None."""
    try:
        words = shlex.split(f.read_text().splitlines()[-1])
        return words[1:words.index("status")] if words[0] == "exec" else None
    except (OSError, ValueError, IndexError):
        return None


# A per-project hook as windvane writes it: hooks written before install.json listed them are known by this shape.
OURS = re.compile(rf"#!/usr/bin/env bash\n# windvane [{NAME_CHARS}]+: `(status|stop|overview)` from the phone "
                  r"\(ntfy-listen\)\nexec [^\n]+ -C [^\n]+\n")


def tidy_hooks(m):
    """Take out the phone hooks windvane wrote that no longer belong: the per-project `status` hooks of installs
    before the shared one, and any hook whose project's repo is gone (it would answer with an error). Only hooks
    install.json lists or that have exactly the form windvane writes are touched. Whether it removed an old `status`
    hook, so the shared one replaces it."""
    base, old_status = ntfy_dir(), False
    listed = {e["path"] for e in m["paths"] if e["kind"] == "file"}
    for kind in ("status.d", "stop.d", "overview.d"):
        for t in sorted((base / kind).glob("windvane-*")):
            if str(t) not in listed and not ours(t):
                continue
            if kind == "status.d" or not hook_root_exists(t):
                old_status |= kind == "status.d"
                t.unlink(missing_ok=True)  # another run's start may sweep it at the same moment
    m["paths"] = [e for e in m["paths"]  # and what install.json lists there that's gone now
                  if not (e["kind"] == "file" and Path(e["path"]).parent.parent == base and not Path(e["path"]).exists())]
    return old_status


def ours(f):
    try:
        return bool(OURS.fullmatch(f.read_text()))
    except (OSError, ValueError):
        return False


def hook_root_exists(f):
    """Whether the repo a per-project hook runs in (its `-C` directory) is still there; True when it can't tell."""
    try:
        words = shlex.split(f.read_text().splitlines()[-1])
        return Path(words[words.index("-C") + 1]).is_dir()
    except (OSError, ValueError, IndexError):
        return True


def tidy_phone_hooks(p=None):
    """At a run's start: an install made before the shared `status` hook gets it now (and a run gets it back when
    it's missing or its install is gone), hooks of deleted repos go, and `p`'s own `stop` and `overview` come back
    when they're missing (tidied while its repo was away), without waiting for an `init`. Raises Unreadable or TimeoutError, changing nothing, as `changing` does."""
    with changing() as m:
        if tidy_hooks(m) or p and ntfy_dir().exists() and status_hook_gone():
            write_status_hook(m)
        if p and ntfy_dir().exists():
            write_project_hooks(p, m, missing_only=True)


def status_hook_gone():
    """Whether the shared `status` hook is missing or runs a command that's no longer there (an uninstalled copy)."""
    f = ntfy_dir() / "status.d/windvane"
    cmd = status_command(f)
    return not (f.exists() or f.is_symlink()) or bool(cmd) and not shutil.which(cmd[0])


def _groups(data):
    return data.get("hooks", {}).get("UserPromptSubmit", []) if isinstance(data, dict) else []


def has_keyword_hook(settings, cmd=None, any_copy=False):
    """Whether this windvane's mention hook is in `settings` (with `any_copy`: any windvane install's)."""
    try:
        data = json.loads(settings.read_text())
    except (OSError, ValueError):
        return False
    cmd = cmd or hook_command()
    return any(h.get("command") == cmd or any_copy and str(h.get("command", "")).endswith("/mention_hook.py")
               for g in _groups(data) for h in g.get("hooks", []))


def add_keyword_hook(settings):
    """Add the UserPromptSubmit keyword hook to Claude Code's user settings, once, keeping everything else."""
    cmd = hook_command()
    try:
        data = json.loads(settings.read_text()) if settings.exists() else {}
    except ValueError:
        print(f"{settings} isn't valid JSON: add the hook yourself (UserPromptSubmit -> {cmd})")
        return False
    if has_keyword_hook(settings, cmd):
        return False
    data.setdefault("hooks", {}).setdefault("UserPromptSubmit", []).append(
        {"hooks": [{"type": "command", "command": cmd, "timeout": 5}]})
    write_json(settings, data)
    return True


def remove_keyword_hook(settings, cmd, before):
    """Take out the hook `add_keyword_hook` put in, and the empty lists it made; when the rest is unchanged, the file
    goes back to `before` exactly (its own formatting), or away when there was none."""
    try:
        data = json.loads(settings.read_text())
    except (OSError, ValueError):
        return
    old = json.loads(before) if before else {}
    groups = _groups(data)
    for g in groups:
        if "hooks" in g:
            g["hooks"] = [h for h in g["hooks"] if h.get("command") != cmd]
    if groups:  # drop the groups this left empty (not ones the user had empty already)
        data["hooks"]["UserPromptSubmit"] = [g for g in groups if g.get("hooks") != [] or g in _groups(old)]
    if isinstance(data, dict) and isinstance(data.get("hooks"), dict):
        if data["hooks"].get("UserPromptSubmit") == [] and "UserPromptSubmit" not in old.get("hooks", {}):
            del data["hooks"]["UserPromptSubmit"]
        if not data["hooks"] and "hooks" not in old:
            del data["hooks"]
    if before is None and data == {}:
        settings.unlink()
    elif before is not None and data == old:
        settings.write_text(before)
    else:
        write_json(settings, data)


def write_json(f, data):
    tmp = f.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    tmp.replace(f)
