"""The driver's own branches on their way to the remote: pushing them, holding back what a refused push kept
locally (held.json) and pushing it again at a later run's start, and landing the driver's own changes on main."""
import fcntl
import re
import time

from . import actions, net
from .phone import whole
from .util import git, gout, load_json, log, push, save_json
from .worktree import clean_detach, ensure_worktree


def author_ident(p):
    """`-c user.name/email` for the driver's own commits when the config names an author."""
    a = p.cfg["author"]
    return (["-c", f"user.name={a.split('<')[0].strip()}", "-c", f"user.email={a.split('<')[1].rstrip('>').strip()}"]
            if "<" in a else [])


def push_branch(p, cd, branch, tries=2):
    """Push `cd`'s HEAD to `branch`, trying twice (or `tries` times). When all fail, the commit stays on a local branch of that name, so
    the next run's reset of `cd` can't lose paid-for work. Returns where it is: `branch`, or "the local branch ...
    (why the push was refused and what to do)", so every push that names the place also says the next step."""
    for _ in range(tries):
        res = net.git(cd, "push", "-q", p.remote, f"HEAD:refs/heads/{branch}")
        if res.returncode == 0:
            return branch
    git(cd, "branch", "-f", branch, "HEAD")
    log(p, f"push to {branch} refused ({' '.join(res.stderr.split())[:200]}); kept only locally on {branch}")
    why = push_refusal(res.stderr, p.cfg["forge"], branch, p.remote, p.main)
    hold(p, branch, "push", why)
    return f"the local branch {branch} ({why})"


def hold(p, branch, how, why=""):
    """Note a branch a failed push kept locally, for `push_held`: "push" it, or "land" it (a merge stays a merge).
    Only branches noted here are ever pushed again, so a user's own copy of a closed plan never comes back. The
    refusal's reason goes beside it (held_why.json, the newest wins) with when it was first held, for `status` to show
    while it waits."""
    held = load_json(p.state / "held.json", {})
    if held.get(branch) != "land":
        held = {**held, branch: how}
    old = held_note(load_json(p.state / "held_why.json", {}).get(branch))
    save_held(p, held, {branch: {"why": why or old.get("why", ""), "at": old.get("at") or int(time.time())}})


def held_note(note):
    """A held_why.json entry as {"why", "at"}: one written before T-072 is the reason alone, with no time."""
    return note if isinstance(note, dict) else {"why": note or ""}


def save_held(p, held, notes=None):
    """Write held.json and, in the same step, drop the reasons of branches no longer in it (adding `notes`), so no
    stale reason outlives its branch."""
    save_json(p.state / "held.json", held)
    reasons = {**load_json(p.state / "held_why.json", {}), **(notes or {})}
    save_json(p.state / "held_why.json", {b: w for b, w in reasons.items() if b in held})


# What a refused push's stderr says, first match wins, with its next step: a secret by taking it out of the commit
# (GitHub's push protection is a GH013 rule too, but no rule change or other name gets past it), a login problem by
# logging in, a hook or branch protection by changing the rule or the branch (pushing again alone fails the same way),
# the network by waiting: the next run pushes what was held back (`push_held`).
REFUSALS = (
    ("refused: it holds a secret{where} (GitHub push protection); remove it from the branch's commit "
     "(`git commit --amend` on `{branch}`), then the next run pushes it{allow}",
     re.compile(r"push protection|cannot contain secrets|secret scanning", re.I)),
    ("login refused; {login} and the next run pushes it",
     re.compile(r"authentication failed|permission denied|permission to .* denied|could not read username|"
                r"terminal prompts disabled|invalid username or password|\b403\b", re.I)),
    ("refused by a hook or branch rule; change the rule, or push it by hand with `git push {remote} {branch}:{branch}-b` "
     "and {then}",
     re.compile(r"hook declined|protected branch|GH00\d|GH013|rule violation|remote rejected", re.I)),
    ("network down; the next run pushes it again",
     re.compile(r"could not resolve|timed out|connection (refused|reset)|network is unreachable|unable to access|"
                r"early eof|could not read from remote", re.I)),
)


LOGIN = {"github": "run `gh auth login`", "git": "check your login to the remote"}
# After a hand push: windvane counts the branch as pushed and stops pushing it, so the user lands it (a change to land
# stays listed until main has it).
THEN = {"github": "open a PR from it", "git": "merge it into {main}"}


def push_refusal(stderr, forge="git", branch="BRANCH", remote="origin", main="main"):
    """A short reason for a refused push and its next step, from git's stderr: auth, hook, network; anything else
    sends the user to the log, which `push_branch` gave git's own words (a raw git line reads badly on a phone).
    A hook's step names the command for `branch` on `remote`: under another name it counts as pushed (`held_here`)."""
    for why, pattern in REFUSALS:
        if pattern.search(stderr):
            return why.format(login=LOGIN.get(forge, LOGIN["git"]), branch=branch, remote=remote,
                              then=THEN.get(forge, THEN["git"]).format(main=main), **secret_at(stderr))
    return "push failed; see the windvane log"


def secret_at(stderr):
    """Where GitHub's push protection found the secret (its `path:` lines) and, when it isn't one, the page that
    allows it (its unblock URL), for the secret's step; empty parts when the stderr names neither. The URL stays
    whole: a cut one doesn't open."""
    paths = list(dict.fromkeys(re.findall(r"^remote:\s+path:\s*(\S+)", stderr, re.M)))
    url = re.search(r"https://\S+/unblock-secret/\S+", stderr)
    return {"where": " in " + ", ".join(f"`{x}`" for x in paths[:3]) + " and more" * (len(paths) > 3) if paths else "",
            "allow": f"; or allow it on GitHub if it isn't a secret: {url.group()}" if url else ""}


def n_tickets(n, kind=""):
    """ "1 ticket", "2 tickets"; `kind` goes between ("1 new ticket")."""
    return f"{n} {kind}ticket" + ("" if n == 1 else "s")


def is_local(where):
    return where.startswith("the local branch ")


def local_push(where, kept):
    """A push's body for a branch `push_branch` kept locally: why the push was refused and the next step first (the
    branch's name would push them past the lock screen), then `kept` with the branch's name."""
    m = re.fullmatch(r"the local branch (\S+) \((.*)\)", where, re.S)
    branch, why = m.groups() if m else (where, "")
    return whole("", why[:1].upper() + why[1:], ".") + f"\n{kept} {branch}."


def land(p, cd, branch, title, body, commit=True, tries=2):
    """Put the driver's own change in `cd` (staged, or committed when `commit` is off) on main: through a merged PR on
    github, a push (replayed onto a main that moved) on plain git. Returns (landed, where it is). One kept on a local
    branch is noted as one to land when pushed again."""
    title = actions.tag(p, title, cd)
    if commit:
        git(cd, *author_ident(p), "commit", "-q", "-m", title, "-m", body[:12000])  # a periodic reply's 80 lines
    ok, where = _land(p, cd, branch, title, body, tries)
    if is_local(where):
        hold(p, branch, "land")
    return ok, where


def _land(p, cd, branch, title, body, tries):
    if p.cfg["forge"] == "github":
        where = push_branch(p, cd, branch, tries)
        if is_local(where):
            return False, where
        pr = net.sh(cd, "gh", "pr", "create", "--head", branch, "--base", p.main, "--title", title, "--body", body)
        pr_n = pr.rstrip("/").rsplit("/", 1)[-1]
        merged = pr_n.isdigit() and (
            net.call(["gh", "pr", "merge", pr_n, "--merge", *(["--subject", title] * (actions.SKIP in title))],
                     p.main_root()).returncode
            == 0 or net.sh(p.main_root(), "gh", "pr", "view", pr_n, "--json", "state", "-q", ".state") == "MERGED")
        if merged:
            net.git(p.main_root(), "push", "-q", p.remote, "--delete", branch)
        return merged, pr or branch
    res = net.git(cd, "push", "-q", p.remote, f"HEAD:refs/heads/{p.main}")
    if res.returncode:  # main moved meanwhile: the change only adds to .windvane/, so replay it
        net.git(p.main_root(), "fetch", "-q", p.remote)
        if git(cd, *author_ident(p), "rebase", "-q", p.upstream).returncode == 0:
            res = net.git(cd, "push", "-q", p.remote, f"HEAD:refs/heads/{p.main}")
        else:
            git(cd, "rebase", "--abort")
        if res.returncode:  # keep the paid-for work on its branch rather than drop it
            return False, push_branch(p, cd, branch, tries)
    return True, p.main


def held_branches(p):
    """The branches `hold` noted that still wait to be pushed: unmerged and on no remote branch (pushing one by hand
    under another name counts as pushed), with no PR on github (one pushed by hand, its PR closed and branch deleted,
    stays closed). A change to land pushed by hand stays noted, not pushed again, until main has it or its PR is
    closed. Notes and reasons for any other branch are dropped."""
    held, root = load_json(p.state / "held.json", {}), p.main_root()
    here, tracked = held_here(p, held), {}
    for b, (how, on) in here.items():
        states = p.cfg["forge"] == "github" and net.sh(
            root, "gh", "pr", "list", "--head", on.removeprefix(f"{p.remote}/") if on else b, "--state", "all",
            "--json", "state", "-q", ".[].state")
        if not states or (on and "OPEN" in states.split()):
            tracked[b] = how
    if tracked != held or set(load_json(p.state / "held_why.json", {})) - set(tracked):
        save_held(p, tracked)
    return {b: how for b, how in tracked.items() if not here[b][1]}


def held_here(p, held):
    """`held`'s branches still tracked, as branch -> (how, the remote branch that has it or ""): unmerged ones on no
    remote branch, and a change to land on one too (pushed by hand, it isn't done until main has it). Git alone, no
    network."""
    if not held:  # the usual case: no git call for `status`
        return {}
    root, out = p.main_root(), {}
    unmerged = set(gout(root, "branch", "--no-merged", p.upstream, "--format=%(refname:short)").split())
    for b, how in held.items():
        if b not in unmerged or main_has(root, p.upstream, b):
            continue
        on = gout(root, "branch", "-r", "--contains", b, "--format=%(refname:short)").split()
        if not on or how == "land":
            out[b] = how, (on or [""])[0]
    return out


def main_has(root, upstream, branch):
    """Whether `upstream` already holds `branch`'s change though git doesn't count it merged: a squash or rebase merge
    (GitHub's, or a `git merge --squash`) makes new commits. Merging the branch in would change nothing; before git
    2.38 (no `merge-tree --write-tree`), `git cherry` finds a rebased or one-commit squashed change. When neither can
    tell, it isn't landed: a change kept listed costs a line, a dropped one is lost."""
    r = git(root, "merge-tree", "--write-tree", upstream, branch)
    if r.returncode in (0, 1):  # 1: the merge conflicts, so main lacks some of it
        return r.returncode == 0 and r.stdout.split()[:1] == [gout(root, "rev-parse", f"{upstream}^{{tree}}")]
    marks = [line[:1] for line in gout(root, "cherry", upstream, branch).splitlines()]
    return bool(marks) and "+" not in marks


def held_now(p):
    """(branch, the last refusal's reason and next step, when it was held, the remote branch a hand push put it on or
    "") for each held branch still tracked; read only (no `gh`, no write: `held_branches` prunes at the next run's
    start). One held before T-072 has no time noted: its last commit's date stands in."""
    reasons, out = load_json(p.state / "held_why.json", {}), []
    for b, (_, on) in held_here(p, load_json(p.state / "held.json", {})).items():  # no git call when none is held
        note = held_note(reasons.get(b))
        at = note.get("at") or int(gout(p.main_root(), "log", "-1", "--format=%ct", b) or time.time())
        out.append((b, note["why"] or "push failed; see the windvane log", at, on))
    return out


def held_lines(p, now=None, subject=60):
    """One line per held branch (of `now`, `held_now`'s list) with how long it has waited and where it is: the last
    refusal's reason and next step, or, pushed by hand, the merge still to do (on github, through a PR); for `status`
    (`subject` None: the commit subject whole) and `overview`."""
    forge = p.cfg["forge"]
    merge = THEN.get(forge, THEN["git"]).format(main=p.main) + " and merge it" * (forge == "github")
    return [f"held back for {age(time.time() - at)}, " + (f"pushed to {on}, not merged" if on else "not pushed")
            + f": {b} ({gout(p.main_root(), 'log', '-1', '--format=%s', b)[:subject]}): "
            + (merge if on else why) for b, why, at, on in (held_now(p) if now is None else now)]


def age(seconds):
    """ "under an hour", "5 hours", "3 days": how long something has waited, in a phone line's words."""
    hours = int(seconds // 3600)
    if hours < 1:
        return "under an hour"
    n, unit = (hours, "hour") if hours < 24 else (hours // 24, "day")
    return f"{n} {unit}{'s' * (n > 1)}"


def push_held(p):
    """At a run's start and a desk plan's: push what a failed push kept on a local branch, one try each, landing
    what was to land (held.json) and opening a plan's PR on github. A push still failing is logged only: the phone
    heard why the first time. A pushed branch's local copy goes (the remote has it), or this would push it again
    after its PR was closed and deleted. Returns how many were pushed; 0 while a plan runs (it uses the worktree)."""
    p.state.mkdir(parents=True, exist_ok=True)
    with open(p.state / "plan.lock", "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return 0
        net.git(p.main_root(), "fetch", "-q", "--prune", p.remote)
        branches = held_branches(p)
        if branches:
            ensure_worktree(p, p.plan_dir, setup=False)
        pushed = list(filter(None, (push_one_held(p, p.plan_dir, b, how) for b, how in branches.items())))
        if branches:
            clean_detach(p, p.plan_dir, p.upstream)
        if pushed:  # one push for them all
            push(*held_push(pushed), 3, "memo")
        return len(pushed)


def held_push(pushed):
    """The title and body of `push_held`'s one push, from (line, needs the user) pairs: the ones that need the user
    (not merged, no PR) come first, under a title that says so, so "pushed" never hides them."""
    todo, done = [l for l, need in pushed if need], [l for l, need in pushed if not need]
    if not todo:
        return f"{len(done)} held-back change{'s' * (len(done) > 1)} pushed", "\n".join(done)
    title = f"{len(todo)} held-back change{'s' * (len(todo) > 1)} pushed, not merged"
    return title + (f"; {len(done)} more pushed" if done else ""), "\n".join(todo + ["", "Pushed:"] * bool(done) + done)


def push_one_held(p, cd, branch, how):
    """Where `branch` went, as a line for the phone and whether it needs the user; "" when it stays local."""
    if not clean_detach(p, cd, branch):
        return ""
    title, body = gout(cd, "log", "-1", "--format=%s"), gout(cd, "log", "-1", "--format=%b")
    to_land = how == "land"  # a desk plan without --merge only pushes
    if to_land:
        landed, where = land(p, cd, branch, title, body, commit=False, tries=1)
    else:
        where = push_branch(p, cd, branch, tries=1)
        if not is_local(where) and p.cfg["forge"] == "github":
            where = net.sh(cd, "gh", "pr", "create", "--head", branch, "--base", p.main, "--title", title,
                           "--body", body[-6000:]) or where
    if is_local(where):
        log(p, f"{branch}: still not pushed{where.removeprefix(f'the local branch {branch}')}")
        return ""
    git(p.main_root(), "branch", "-D", branch)
    save_held(p, {b: h for b, h in load_json(p.state / "held.json", {}).items() if b != branch})
    no_pr = not to_land and p.cfg["forge"] == "github" and where == branch
    how = (("merged" if landed else "not merged; merge it by hand") if to_land
           else "pushed; no PR opened, open one by hand" if no_pr else "pushed")
    log(p, f"{branch}: held back, now on {where}, {how}")
    first = whole("", title, f": on {where}, {how}.", log=lambda m: log(p, m))
    return first, no_pr or (to_land and not landed)
