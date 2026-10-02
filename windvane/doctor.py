"""`windvane doctor`: what this machine has and lacks for windvane, each gap with the command that fixes it.

Free and quick: it runs only local commands (`claude auth status`, `gh auth status`), never a Claude session."""
import json
import os
import shutil
import subprocess
import sys

from . import install, phone
from .config import CHECKOUT


def run_quiet(cmd, timeout=20):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None


def version(tool):
    r = run_quiet([tool, "--version"])
    return r.stdout.strip().splitlines()[0] if r and r.returncode == 0 and r.stdout.strip() else ""


def claude_logged_in():
    r = run_quiet(["claude", "auth", "status"])
    if not r:
        return False
    try:
        return bool(json.loads(r.stdout).get("loggedIn"))
    except (ValueError, AttributeError):
        return r.returncode == 0


def checks(p=None):
    """(level, what, fix) per check: level "ok", "need" (windvane can't run without it) or "optional"."""
    out = []

    def add(good, need, what, fix):
        out.append(("ok" if good else "need" if need else "optional", what, "" if good else fix))

    add(sys.version_info >= (3, 11), True, f"Python {sys.version.split()[0]} (3.11 or newer)",
        "install Python 3.11+ and reinstall windvane with it")
    if not sys.platform.startswith("linux"):
        add(False, False, f"Linux (this is {sys.platform}): `windvane heavy` can't see free memory here, so it waits "
            "only for busy processes", "nothing to run: the rest works; tell the windvane repo what breaks")
    add(shutil.which("git"), True, f"git {version('git').removeprefix('git version ')}".strip(),
        "install git (e.g. sudo apt install git)")
    has_claude = bool(shutil.which("claude"))
    add(has_claude, True, "Claude Code (`claude`)", "curl -fsSL https://claude.ai/install.sh | bash")
    if has_claude:
        add(claude_logged_in(), True, "Claude Code logged in", "claude auth login")
    github = bool(p and p.exists and p.cfg["forge"] == "github")
    why = "this project's forge is github" if github else "for projects with forge = github"
    has_gh = bool(shutil.which("gh"))
    add(has_gh, github, f"GitHub CLI `gh` ({why})", "install gh: https://cli.github.com, then gh auth login")
    if has_gh:
        r = run_quiet(["gh", "auth", "status"])
        add(r and r.returncode == 0, github, "gh logged in", "gh auth login")
    add(shutil.which("tmux"), False, "tmux (for `windvane run --detach`)", "install tmux (e.g. sudo apt install tmux)")
    add(shutil.which("notify") and shutil.which("notify-ask") or phone.on(phone.conf()), False,
        "phone pushes and questions (ntfy)", "windvane phone setup; without it, questions wait for `windvane answer`")
    add(shutil.which("windvane"), True, "`windvane` on PATH",
        "windvane install" if CHECKOUT else "pipx ensurepath (or add ~/.local/bin to PATH)")
    skill = install.claude_dir() / "skills/windvane"
    if skill.is_symlink() and not skill.exists():  # e.g. pipx reinstalled windvane on a newer Python
        add(False, False, f"the windvane skill in {skill.parent}: a broken link to {os.readlink(skill)}",
            "windvane install")
    else:
        add(skill.exists(), False, f"the windvane skill in {skill.parent}", "windvane install")
    settings = install.claude_dir() / "settings.json"
    add(install.has_keyword_hook(settings, any_copy=True), False, f"the mention hook in {settings}",
        "windvane install")
    return out


def main(p=None):
    rows = checks(p)
    width = max(len(what) for _, what, _ in rows)
    label = {"ok": "ok      ", "need": "MISSING ", "optional": "optional"}
    for level, what, fix in rows:
        print(f"{label[level]}  {what.ljust(width) if fix else what}" + (f"  -> {fix}" if fix else ""))
    missing = sum(level == "need" for level, _, _ in rows)
    print(f"{missing} to fix before windvane can run" if missing else "all set: windvane can run here")
    return 1 if missing else 0
