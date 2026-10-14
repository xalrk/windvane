"""`windvane doctor`: what this machine has and lacks for windvane, each gap with the command that fixes it.

Free and quick: it runs only local commands (`claude auth status`, `gh auth status`), never a Claude session."""
import json
import math
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from . import actions, heavy, install, phone, privacy, runner, scratch, selfupdate, statedir
from .config import CHECKOUT, DIR, HOME, TOKEN_FIX, Project, author, github_token
from .util import gout, load_json

# How gh says GitHub turned a token down (expired, revoked, or not given this repo), as opposed to not reaching it.
REFUSED = re.compile(r"HTTP 40[134]|Bad credentials|Could not resolve to a Repository")
# The triggers that run a workflow on its own for every autopilot ticket: its PR and the merge push to main.
AUTO_TRIGGERS = ("push", "pull_request", "pull_request_target")


def run_quiet(cmd, timeout=20, cwd=None, env=None):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=cwd, env=env)
    except (OSError, subprocess.TimeoutExpired):
        return None


def own_version():
    """`windvane --version`'s line. A clone has no package metadata (or a stale one, for an editable install), so
    its commit says which code runs; the toplevel must be the checkout itself, not a repo it happens to sit in. No git
    (a downloaded zip) reads as no checkout: doctor must get on to saying git is missing."""
    top = gout(HOME, "rev-parse", "--show-toplevel") if CHECKOUT and shutil.which("git") else ""
    if top and Path(top).resolve() == HOME:
        dirty = gout(HOME, "status", "--porcelain", "--untracked-files=no")
        return f"windvane {gout(HOME, 'rev-parse', '--short', 'HEAD')} (clone{', modified' if dirty else ''})"
    v = selfupdate.version()
    return f"windvane {v}" if v != "0" else "windvane (unknown version: not installed, and no git commit to read)"


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


def _split(flow):
    """A flow collection's (`[..]` or `{..}`) items, split at its own commas, not those nested or quoted; None when
    it isn't one closed on this line."""
    if not flow or flow[0] + flow[-1] not in ("[]", "{}"):
        return None
    items, depth, quote, item = [], 0, "", ""
    for ch in flow[1:-1]:
        if quote:
            quote = "" if ch == quote else quote
        elif ch in "'\"":
            quote = ch
        elif ch in "[{":
            depth += 1
        elif ch in "]}":
            depth -= 1
        elif ch == "," and not depth:
            items, item = items + [item], ""
            continue
        item += ch
    return [x.strip() for x in items + [item] if x.strip()]


def _list_len(inline, block):
    """How many items a YAML list has, inline (`[a, b]`) or as `- item` lines; None when it's neither (an
    expression like `${{ fromJSON(..) }}`, or a list spread over lines)."""
    if inline:
        items = _split(inline) if inline.startswith("[") else None
        return None if items is None else len(items)
    return sum(line.startswith("-") for line in _top(block))


def _matrix_jobs(job):
    """How many jobs one job of a workflow starts: its matrix's axes multiplied, plus `include`, minus `exclude`
    (an `include` that only extends a combination is counted as one more job: this errs high). None when the matrix
    can't be read here."""
    inline, matrix = _value(job, "matrix")
    if inline:
        pairs = _split(inline) if inline.startswith("{") else None
        if pairs is None:
            return None
        lists = [(k.strip().strip("'\""), v.strip()) for k, _, v in (x.partition(":") for x in pairs)]
    else:
        lists = [(key, _value(matrix, key, _indent(matrix[0]))) for line in _top(matrix)
                 if (key := (m := re.match(r"['\"]?([\w-]+)['\"]?\s*:", line)) and m.group(1))]
    axes, extra = [], 0
    for key, value in lists:
        n = _list_len(*value) if isinstance(value, tuple) else _list_len(value, [])
        if n is None:
            return None
        if key in ("include", "exclude"):
            extra += n if key == "include" else -n
        else:
            axes.append(n)
    return max(1, math.prod(axes) + extra if axes else extra)


def workflow_runs(text):
    """(the triggers in AUTO_TRIGGERS a workflow has, the jobs one run of it starts or None when they can't be
    counted), from the plain YAML GitHub's own examples use: a reading good enough to warn on, without a YAML
    library."""
    lines = [line.rstrip() for line in text.splitlines()]
    inline, block = _value(lines, "on", 0)
    names = re.findall(r"[\w-]+", inline) if inline else [re.match(r"-?\s*['\"]?([\w-]*)", line).group(1)
                                                           for line in _top(block)]
    _, jobs = _value(lines, "jobs", 0)
    starts = [i for i, line in enumerate(jobs) if _indent(line) == _indent(jobs[0])] + [len(jobs)]
    counts = [_matrix_jobs(jobs[a:b]) for a, b in zip(starts, starts[1:])]
    return [t for t in AUTO_TRIGGERS if t in names], sum(counts) if counts and None not in counts else None


def ci_warnings(root, p=None):
    """(what, fix) per workflow in root that runs by itself on every push or PR: on an autopilot project that is a
    hosted run for each ticket's PR and again for its merge, which can use up a month's Actions minutes in days. With
    an Actions budget set (project `p`), the warning says those are the capped minutes."""
    out, b = [], actions.budget(p) if p else None
    spends = ""
    if b:
        total = actions.cached()
        spends = (f"; it spends the capped minutes of the Actions budget ({actions.fmt(b[0])} min, cap "
                  f"{actions.cap_minutes(p):,}; " + (f"{actions.fmt(total['used'])} counted this month)" if total
                                                     else "not counted yet this month)"))
    folder = root / ".github/workflows"
    for f in sorted(folder.glob("*.y*ml")) if folder.is_dir() else []:
        try:
            triggers, jobs = workflow_runs(f.read_text())
        except (OSError, UnicodeDecodeError):
            continue
        if triggers:
            on_pr, on_push = any(t.startswith("pull_request") for t in triggers), "push" in triggers
            runs = ("for its PR and again for its merge" if on_pr and on_push else "for its PR" if on_pr
                    else "for its merge")
            jobs_said = (f"about {jobs * (on_pr + on_push)} hosted jobs a ticket ({jobs} a run, {runs})" if jobs
                         else f"some hosted jobs on every ticket, {runs} (doctor can't count them in this YAML)")
            out.append((f"{f.relative_to(root)} runs on {' and '.join(triggers)}: {jobs_said}{spends}",
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
        out += [("warn", what, fix) for what, fix in ci_warnings(p.root, p)]
    github = bool(p and p.exists and p.cfg["forge"] == "github")
    why = "this project's forge is github" if github else "for projects with forge = github"
    has_gh = bool(shutil.which("gh"))
    add(has_gh, github, f"GitHub CLI `gh` ({why})", "install gh: https://cli.github.com, then gh auth login")
    if has_gh:
        r = run_quiet(["gh", "auth", "status"])
        add(r and r.returncode == 0, github, "gh logged in", "gh auth login")
        if github and getattr(p, "root", None):
            out += public_warnings(p) + token_warnings(p)
    if github and getattr(p, "root", None):
        out += identity_warnings(p)
    add(shutil.which("tmux"), False, "tmux (for `windvane run --detach`)", "install tmux (e.g. sudo apt install tmux)")
    add(shutil.which("notify") and shutil.which("notify-ask") or phone.on(phone.conf()), False,
        "phone pushes and questions (ntfy)", "windvane phone setup; without it, questions wait for `windvane answer`")
    add(shutil.which("windvane"), True, "`windvane` on PATH",
        "windvane install" if CHECKOUT else f"{install.PATH_LINE} (and in your shell's startup file)")
    skill = install.claude_dir() / "skills/windvane"
    if skill.is_symlink() and not skill.exists():  # e.g. pipx reinstalled windvane on a newer Python
        add(False, False, f"the windvane skill in {skill.parent}: a broken link to {os.readlink(skill)}",
            "windvane install")
    else:
        add(skill.exists(), False, f"the windvane skill in {skill.parent}", "windvane install")
    settings = install.claude_dir() / "settings.json"
    add(install.has_keyword_hook(settings, any_copy=True), False, f"the mention hook in {settings}",
        "windvane install")
    return out + tmp_warnings(p) + state_warnings(p)


def public_warnings(p):
    """A warning when the project's GitHub repo is public: `.windvane/` is pushed with it, answers and all. Quiet when
    `gh` can't tell (offline, logged out): this is a reminder, never a failure."""
    r = run_quiet(["gh", "repo", "view", "--json", "visibility", "-q", ".visibility"], timeout=10, cwd=p.root)
    if not r or r.returncode or r.stdout.strip().upper() != "PUBLIC":
        return []
    return [("warn", f"this repo is public on GitHub, and so is {DIR}/ (tickets, your answers, the critic's notes)",
             f"keep secrets and personal details out of them; `windvane doctor --leaks` checks its history "
             f"(docs/safety.md, \"What {DIR}/ holds\")")]


def identity_warnings(p):
    """A warning when the email windvane's commits carry (`author`, else git's own) isn't a GitHub noreply address:
    every commit pushed to GitHub shows it, and on a public repo anyone can read it."""
    email = author(p)[1] or gout(p.root, "config", "user.email")
    if email.endswith("@users.noreply.github.com"):
        return []
    return [("warn", f"commits here carry {'no email' if not email else 'an email that is not a GitHub noreply one'}",
             f'set author = "Name <id+login@users.noreply.github.com>" in {DIR}/config.toml '
             "(GitHub: Settings, Emails)")]


def token_warnings(p):
    """`[github] token_file`, when set: it can reach this repo and none of the user's other private ones. Checked
    through gh with the token in its environment; the token itself is never printed."""
    try:
        token = github_token(p)
    except SystemExit as e:
        return [("warn", str(e).removeprefix("windvane: ").removesuffix(TOKEN_FIX),
                 "fix the file, or take token_file out of config.toml")]
    if not token:
        return []
    env = {**os.environ, "GH_TOKEN": token}
    r = run_quiet(["gh", "repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"], 15, p.root, env)
    if not r or r.returncode and not REFUSED.search(r.stderr):
        return []  # offline, or no gh: nothing to tell yet
    repo = r.stdout.strip() if r.returncode == 0 else ""
    if not repo:
        return [("warn", "[github] token_file: GitHub refused its token for this repo",
                 "make a fine-grained token for it with Contents, Pull requests and Metadata (docs/configuration.md)")]
    r = run_quiet(["gh", "api", "user/repos?visibility=private&per_page=100", "-q", ".[].full_name"], 15, p.root, env)
    others = sorted(set(r.stdout.split()) - {repo}) if r and r.returncode == 0 else []
    if others:
        return [("warn", f"[github] token_file: its token also reaches {len(others)} other private repo(s)",
                 f"limit it to {repo} alone (Repository access: Only select repositories)")]
    return []


def leaks(p):
    """`doctor --leaks`: every leak kind in the lines any commit adds, by kind with its places, never the text.
    Exit status 1 when there are any, for a check before a repo goes public."""
    found = {}
    for kind, where in privacy.history(p.root):
        found.setdefault(kind, []).append(where)
    for kind, places in sorted(found.items()):
        more = f" (+{len(places) - 20} more)" if len(places) > 20 else ""
        print(f"{privacy.NAMES[kind]}: {len(places)} place(s): {', '.join(places[:20])}{more}")
    print(f"{sum(map(len, found.values()))} place(s) to check in {p.root}'s history; a deleted file's lines stay in it"
          if found else f"nothing found in {p.root}'s history")
    return 1 if found else 0


def tmp_scope(p):
    """(the project whose session dirs count too, or None; its running tickets, which keep theirs)."""
    if p is None or not getattr(p, "exists", False) or not getattr(p, "state", None):
        return None, set()
    return p, set(load_json(p.state / "state.json", {}).get("running", {}))


def tmp_warnings(p=None):
    """The temp dir nearly full (a full tmpfs stops every command), and windvane's leftovers in it, biggest first."""
    out, tmp = [], scratch.root()
    try:
        inodes, space = scratch.fullness(tmp)
    except OSError:
        inodes = space = 0
    if max(inodes, space) > scratch.FULL:
        out.append(("warn", f"the temp dir {tmp} is {inodes:.0f}% full of inodes and {space:.0f}% of space: when "
                    "either is full, no command runs", "remove what fills it; windvane doctor --clean removes "
                    "windvane's own leftovers"))
    left = scratch.leftovers(*tmp_scope(p))
    if left:
        files = lambda n: f"{n} file{'s' * (n != 1)}"
        big = ", ".join(f"{d.name} ({files(n)})" for d, n, _ in left[:3])
        out.append(("warn", f"{len(left)} leftover windvane temp dir{'s' * (len(left) > 1)}, "
                    f"{files(sum(n for _, n, _ in left))} (biggest: {big})", "windvane doctor --clean"))
    return out


def state_warnings(p=None):
    """What of the project's state dir could go (statedir.py): one line, the biggest item named."""
    if p is None or not getattr(p, "exists", False) or not getattr(p, "state", None):
        return []
    try:
        items = statedir.reclaimable(p)
    except (OSError, SystemExit) as e:
        return [("warn", f"the state dir {p.state} wasn't surveyed: {e}", "fix the [state] settings")]
    if not items:
        return [("ok", f"state dir {p.state}: nothing to reclaim", "")]
    total, big = sum(i.size for i in items), items[0]
    what = f"{statedir.human(total)} of {p.state} can go (biggest: {big.what}, {statedir.human(big.size)})"
    return [("warn" if total >= BIG else "ok", what, "windvane doctor --clean" if total >= BIG else "")]


BIG = 100 * 2 ** 20  # reclaimable state worth a warning


def clean_tmp(p, dry_run):
    scope = tmp_scope(p)
    where = f"{scratch.root()}" + (f" and {p.state / 'tmp'}" if scope[0] else "")
    if not dry_run:
        dirs, n = scratch.clean(*scope)
        print(f"removed {dirs} leftover windvane temp dir{'s' * (dirs != 1)} ({n} files) from {where}")
        return
    left = scratch.leftovers(*scope)
    print(f"would remove {len(left)} leftover windvane temp dir{'s' * (len(left) != 1)} from {where}")
    for d, n, b in left:
        print(f"  {statedir.human(b):>6}  {d} ({n} files)")


def clean_states(p=None, dry_run=False):
    """Every project's state dir on this machine (the ones its runs registered, and `p`), but not one whose
    autopilot is running: its slots and transcripts are in use."""
    roots = dict(phone.projects())
    if p is not None and getattr(p, "exists", False):
        roots.setdefault(p.name, str(p.main_root()))
    total = 0
    for name, root in sorted(roots.items()):
        try:
            q = Project(root)
            if not q.exists or not q.state.is_dir():
                continue
            with statedir.lock(q) as free:
                if not free:
                    print(f"{q.name}: skipped, its autopilot is running")
                    continue
                items, kept = statedir.survey(q)
                done = [(item, dry_run or statedir.remove(q, item)) for item in items]
        except (OSError, SystemExit) as e:
            print(f"{name}: skipped ({e})")
            continue
        freed = sum(item.size for item, ok in done if ok)
        verb = "would free" if dry_run else "freed"
        print(f"{q.name} ({q.state}): " + (f"{verb} {statedir.human(freed)}" if items else "nothing to reclaim"))
        for item, ok in done:
            print(f"  {statedir.human(item.size):>6}  {item.what}" + ("" if ok else "  (kept: it changed meanwhile)"))
        for wt, why in kept:
            print(f"  kept    {wt}: {why}")
        total += freed
    print(f"{'would free' if dry_run else 'freed'} {statedir.human(total)} of state in all")


def main(p=None, clean=False, dry_run=False):
    print(own_version())  # a bug report pastes this output: it names the windvane it describes
    if clean or dry_run:
        clean_tmp(p, dry_run)
        clean_states(p, dry_run)
        if dry_run:
            return 0
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
