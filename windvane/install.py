"""`windvane install` / `uninstall`: bring windvane into Claude Code (the skill, the mention hook) and onto PATH,
asking before each change, and take back exactly what it added. What it added is listed in
`~/.local/state/windvane/install.json`, so uninstall never touches anything the user made or changed themselves, and
puts `settings.json` back byte for byte when nothing else in it changed meanwhile."""
import json
import os
import re
import shlex
import shutil
import sys
from pathlib import Path

from .config import CHECKOUT, HOME, exe, user_dir


def manifest_path():
    return user_dir("XDG_STATE_HOME", ".local/state") / "install.json"


def load():
    try:
        return json.loads(manifest_path().read_text())
    except (OSError, ValueError):
        return {"paths": [], "settings": []}


def save(m):
    f = manifest_path()
    if not (m["paths"] or m["settings"]):
        f.unlink(missing_ok=True)
        return
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(m, indent=2) + "\n")


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
    m = load()
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
    save(m)
    tidy_phone_hooks()
    if not shutil.which("windvane"):
        print("windvane isn't on PATH yet: add ~/.local/bin to PATH (pipx users: pipx ensurepath)")
    print("next: windvane doctor (what this machine still lacks); windvane uninstall takes all of this back")


def uninstall(yes=False):
    m = load()
    items = [e["path"] for e in m["paths"] if e["kind"] != "dir"] + [f"the mention hook in {s['path']}"
                                                                       for s in m["settings"]]
    if not items:
        print("nothing to remove: windvane install hasn't added anything (or it was removed already)")
        return
    print("windvane added:\n  " + "\n  ".join(items))
    if not ask("remove all of it?", yes):
        return
    for s in reversed(m["settings"]):
        remove_keyword_hook(Path(s["path"]), s["command"], s["before"])
    for e in reversed(m["paths"]):
        remove_path(e)
    save({"paths": [], "settings": []})
    print(f"removed. {removal_hint()}")


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
    m, cmd = load(), shlex.join(exe())
    for kind, args in (("stop.d", "stop --if-running"), ("overview.d", "overview")):
        write_hook(base / kind / f"windvane-{p.name}", f"{p.name}: `{args.split()[0]}`",
                   f"{cmd} -C {shlex.quote(str(p.main_root()))} {args}", m)
    write_status_hook(m)
    tidy_hooks(m)
    save(m)
    return [str(base / k) for k in ("status.d", "stop.d", "overview.d")]


def ntfy_dir():
    return Path.home() / ".config/ntfy"


def write_hook(f, what, command, m):
    """Write a hook script windvane owns; a file there that install.json doesn't list is the user's, left alone."""
    ours = any(e["path"] == str(f) for e in m["paths"])
    if f.exists() and not ours:
        print(f"{f} isn't windvane's, so it's left alone")
        return
    make_dirs(f.parent, m)
    if not ours:
        m["paths"].append({"path": str(f), "kind": "file"})
    f.write_text(f"#!/usr/bin/env bash\n# windvane {what} from the phone (ntfy-listen)\nexec {command}\n")
    f.chmod(0o755)


def write_status_hook(m):
    write_hook(ntfy_dir() / "status.d/windvane", "every project: `status`, only the autopilots that are on",
               f"{shlex.join(exe())} status --line --running", m)


# A per-project hook as windvane writes it: hooks written before install.json listed them are known by this shape.
OURS = re.compile(r"#!/usr/bin/env bash\n# windvane [a-z0-9-]+: `(status|stop|overview)` from the phone "
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
                t.unlink()
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


def tidy_phone_hooks():
    """At a run's start: an install made before the shared `status` hook gets it now, and hooks of deleted repos go,
    without waiting for an `init` (which is what writes the hooks)."""
    m = load()
    before = json.dumps(m)
    if tidy_hooks(m):
        write_status_hook(m)
    if json.dumps(m) != before:
        save(m)


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
