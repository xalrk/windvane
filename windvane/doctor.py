"""`windvane doctor`: what this machine has and lacks for windvane, each gap with the command that fixes it.

Free and quick: it runs only local commands (`claude auth status`, `gh auth status`), never a Claude session."""
import json
import math
import os
import re
import shutil
import subprocess
import sys

from . import heavy, install, phone, runner
from .config import CHECKOUT

# The triggers that run a workflow on its own for every autopilot ticket: its PR and the merge push to main.
AUTO_TRIGGERS = ("push", "pull_request", "pull_request_target")


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


def claude_lacks():
    """The session flags this `claude` doesn't list in its help: an older CLI that fails every session at launch."""
    r = run_quiet(["claude", "--help"])
    text = r.stdout if r else ""
    return [f for f in runner.CLI_FLAGS if not re.search(rf"(?<![\w-]){f}(?![\w-])", text)]


def _indent(line):
    return len(line) - len(line.lstrip())


def _block(lines, i):
    """The lines indented under lines[i] (a `key:` line), up to the next line at its indent or less."""
    out = []
    for line in lines[i + 1:]:
        if line.strip() and not line.lstrip().startswith("#"):
            if _indent(line) <= _indent(lines[i]):
                break
            out.append(line)
    return out


def _top(block):
    """A block's lines at its own (least) indent: its keys, or its `- item`s."""
    least = min(map(_indent, block), default=0)
    return [line.strip() for line in block if _indent(line) == least]


def _value(lines, key, indent=None):
    """The first `key:` line's inline value (comment cut) and the block under it; ("", []) when there's none."""
    for i, line in enumerate(lines):
        m = re.match(rf"\s*['\"]?{key}['\"]?\s*:(.*)$", line)
        if m and indent in (None, _indent(line)):
            return m.group(1).split(" #")[0].strip(), _block(lines, i)
    return "", []


def _list_len(inline, block):
    """How many items a YAML list has, inline (`[a, b]`) or as `- item` lines."""
    if inline.startswith("["):
        return len([x for x in inline.strip("[]").split(",") if x.strip()])
    return 1 if inline else sum(line.startswith("-") for line in _top(block))


def _matrix_jobs(job):
    """How many jobs one job of a workflow starts: its matrix's axes multiplied, plus `include`, minus `exclude`
    (an `include` that only extends a combination is counted as one more job: this errs high)."""
    _, matrix = _value(job, "matrix")
    axes, extra = [], 0
    for key in [m.group(1) for line in _top(matrix) if (m := re.match(r"['\"]?([\w-]+)['\"]?\s*:", line))]:
        n = _list_len(*_value(matrix, key, _indent(matrix[0])))
        if key in ("include", "exclude"):
            extra += n if key == "include" else -n
        else:
            axes.append(n)
    return max(1, math.prod(axes) + extra if axes else extra)


def workflow_runs(text):
    """(the triggers in AUTO_TRIGGERS a workflow has, the jobs one run of it starts), from the plain YAML GitHub's
    own examples use: a reading good enough to warn on, without a YAML library."""
    lines = [line.rstrip() for line in text.splitlines()]
    inline, block = _value(lines, "on", 0)
    names = re.findall(r"[\w-]+", inline) if inline else [re.match(r"-?\s*['\"]?([\w-]*)", line).group(1)
                                                           for line in _top(block)]
    _, jobs = _value(lines, "jobs", 0)
    starts = [i for i, line in enumerate(jobs) if _indent(line) == _indent(jobs[0])] + [len(jobs)]
    return [t for t in AUTO_TRIGGERS if t in names], sum(_matrix_jobs(jobs[a:b]) for a, b in zip(starts, starts[1:]))


def ci_warnings(root):
    """(what, fix) per workflow in root that runs by itself on every push or PR: on an autopilot project that is a
    hosted run for each ticket's PR and again for its merge, which can use up a month's Actions minutes in days."""
    out = []
    folder = root / ".github/workflows"
    for f in sorted(folder.glob("*.y*ml")) if folder.is_dir() else []:
        try:
            triggers, jobs = workflow_runs(f.read_text())
        except (OSError, UnicodeDecodeError):
            continue
        if triggers:
            per_ticket = jobs * (("push" in triggers) + any(t.startswith("pull_request") for t in triggers))
            jobs_said = (f"about {per_ticket} hosted jobs a ticket ({jobs} a run, for its PR and again for its merge)"
                         if jobs else "hosted jobs for each ticket's PR and again for its merge (how many, doctor "
                         "can't read from this YAML)")  # flow-style `jobs: {...}`, say
            out.append((f"{f.relative_to(root)} runs on {' and '.join(triggers)}: {jobs_said}",
                        f"keep only `on: workflow_dispatch` and run it by hand (`gh workflow run {f.name}`); "
                        "test every version on this machine in the check instead"))
    return out


def checks(p=None):
    """(level, what, fix) per check: level "ok", "need" (windvane can't run without it), "optional" or "warn"
    (it runs, but at a cost the user should know of)."""
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
        lacks = claude_lacks()
        add(not lacks, True, f"Claude Code new enough ({version('claude') or '?'}"
            + (f" lacks {', '.join(lacks)})" if lacks else ")"), "claude update")
    if p and p.exists:
        bad = p.perm_problems
        add(not bad, True, "[permissions] rules in config.toml" + (f": {'; '.join(bad)}" if bad else ""),
            "fix them: Tool or Tool(specifier), in lists under <role>_allow / <role>_deny")
    if p and getattr(p, "root", None):
        out += [("warn", what, fix) for what, fix in ci_warnings(p.root)]
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
        "windvane install" if CHECKOUT else "add ~/.local/bin to PATH (pipx ensurepath does it)")
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
    width = max(len(what) for level, what, _ in rows if level != "warn")  # a long warning mustn't push every fix out
    label = {"ok": "ok      ", "need": "MISSING ", "optional": "optional", "warn": "WARNING "}
    for level, what, fix in rows:
        print(f"{label[level]}  {what.ljust(width) if fix else what}" + (f"  -> {fix}" if fix else ""))
    # Where new projects go is the user's own setting; the windvane skill reads this line rather than guess the path.
    folder = heavy.machine().get("projects_dir")
    print(f"machine settings: {heavy.machine_file()} (projects_dir: {folder or 'not set'})")
    missing = sum(level == "need" for level, _, _ in rows)
    print(f"{missing} to fix before windvane can run" if missing else "all set: windvane can run here")
    return 1 if missing else 0
