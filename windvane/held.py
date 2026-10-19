"""The driver's own branches on their way to the remote: pushing them, holding back what a refused push kept
locally (held.json) and pushing it again at a later run's start, and landing the driver's own changes on main."""
import fcntl
import json
import re
import subprocess
import time

from . import actions, net, privacy
from .config import DIR, author
from .phone import cut, whole
from .util import git, gout, load_json, locked, log, push, save_json
from .worktree import clean_detach, ensure_worktree


def author_ident(p):
    """`-c user.name/email` for the driver's own commits when the config names an author."""
    name, email = author(p)
    return ["-c", f"user.name={name}", "-c", f"user.email={email}"] if email else []


def push_branch(p, cd, branch, tries=2):
    """Push `cd`'s HEAD to `branch`, trying twice (or `tries` times). When all fail, the commit stays on a local branch of that name, so
    the next run's reset of `cd` can't lose paid-for work. Returns where it is: `branch`, or "the local branch ...
    (why the push was refused and what to do)", so every push that names the place also says the next step."""
    for _ in range(tries):
        res = net.git(cd, "push", "-q", p.remote, f"HEAD:refs/heads/{branch}")
        if res.returncode == 0:
            return branch
    git(cd, "branch", "-f", branch, "HEAD")
    log(p, f"push to {branch} refused ({cut(' '.join(res.stderr.split()))}); kept only locally on {branch}")
    why = push_refusal(res.stderr, p.cfg["forge"], branch, p.remote, p.main)
    hold(p, branch, "push", why)
    return f"the local branch {branch} ({why})"


def hold(p, branch, how, why=""):
    """Note a branch a failed push kept locally, for `push_held`: "push" it, or "land" it (a merge stays a merge).
    Only branches noted here are ever pushed again, so a user's own copy of a closed plan never comes back. The
    refusal's reason goes beside it (held_why.json, the newest wins) with when it was first held, for `status` to show
    while it waits."""
    def edit(held, reasons):
        if held.get(branch) != "land":
            held[branch] = how
        old = held_note(reasons.get(branch))
        reasons[branch] = {"why": why or old.get("why", ""), "at": old.get("at") or int(time.time())}
    edit_held(p, edit)


def held_note(note):
    """A held_why.json entry as {"why", "at"}: one written before T-072 is the reason alone, with no time."""
    return note if isinstance(note, dict) else {"why": note or ""}


def unhold(p, branches):
    """Forget `branches` (pushed, landed, or no longer waiting) and their reasons, keeping any held meanwhile."""
    edit_held(p, lambda held, _: [held.pop(b, None) for b in branches])


def edit_held(p, change):
    """Let `change(held, reasons)` edit held.json and held_why.json as they are now, under one lock for the pair: the
    driver, `windvane plan` and `windvane held` all write them, and saving a copy loaded earlier would save over
    another's hold (that branch never pushed again, nor listed by `status`). In the same step the reasons of branches
    no longer held go, so none outlives its branch."""
    with locked(p.state / "held.json"):
        held, reasons = load_json(p.state / "held.json", {}), load_json(p.state / "held_why.json", {})
        change(held, reasons)
        save_json(p.state / "held.json", held)
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
    ("the remote's ssh host key isn't trusted yet; connect once by hand (`ssh -T` to its host) to check and accept it, "
     "and the next run pushes it", re.compile(r"host key verification failed", re.I)),
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


def local_push(where, kept, log):
    """A push's body for a branch `push_branch` kept locally: why the push was refused and the next step first, then
    `kept` with the branch's name on its own line."""
    m = re.fullmatch(r"the local branch (\S+) \((.*)\)", where, re.S)
    branch, why = m.groups() if m else (where, "")
    return whole("", why[:1].upper() + why[1:], ".", log=log) + f"\n{kept} {branch}."


def land(p, cd, branch, title, body, commit=True, tries=2, close=False):
    """Put the driver's own change in `cd` (staged, or committed when `commit` is off) on main: through a merged PR on
    github, a push (replayed onto a main that moved) on plain git. Returns (landed, where it is). One kept on a local
    branch, or whose PR can't merge, is noted as one to land; with `close`, a PR that can't merge is closed instead
    and the result is (None, why)."""
    title, body = actions.tag(p, privacy.censor(title), cd), privacy.censor(body)  # a periodic or plan session's reply
    if commit:
        git(cd, *author_ident(p), "commit", "-q", "-m", title, "-m", body[:12000])  # a periodic reply's 80 lines
    found = privacy.outgoing(cd, p.upstream, "HEAD", DIR, privacy.guarded(p))
    ok, where = (False, kept_secret(p, cd, branch, title, found)) if found \
        else _land(p, cd, branch, title, body, tries, close)
    if is_local(where):
        hold(p, branch, "land")
    return ok, where


def kept_secret(p, cd, branch, title, found):
    """Keep a change whose new `.windvane/` lines hold a secret on its local branch, held with the reason, and tell the
    phone once (the next run's `push_held` scans it again, and holds it again until it's taken out). The reason names
    kinds and places only: it goes to a log and a phone."""
    why = (f"its {DIR}/ lines hold {privacy.said(found)}; take it out of the branch's commit "
           f"(`git switch {branch}`, edit, `git commit -a --amend`), then the next run pushes it")
    told = branch in load_json(p.state / "held.json", {})
    git(cd, "branch", "-f", branch, "HEAD")
    hold(p, branch, "land", why)
    log(p, f"{branch}: {why}")
    if not told:
        push(f"Not pushed: {title}", whole("", why[:1].upper() + why[1:], ".", log=lambda m: log(p, m)), 4, "warning")
    return f"the local branch {branch} ({why})"


def _land(p, cd, branch, title, body, tries, close):
    if p.cfg["forge"] == "github":
        where = push_branch(p, cd, branch, tries)
        if is_local(where):
            return False, where
        pr = net.sh(cd, "gh", "pr", "create", "--head", branch, "--base", p.main, "--title", title, "--body", body)
        pr_n = pr.rstrip("/").rsplit("/", 1)[-1]
        merged, state = merge_pr(p, cd, branch, pr_n, title) if pr_n.isdigit() else (False, "")
        if merged:
            net.git(p.main_root(), "push", "-q", p.remote, "--delete", branch)
            return True, pr
        return not_merged(p, cd, branch, pr, pr_n, state, close)
    res = net.git(cd, "push", "-q", p.remote, f"HEAD:refs/heads/{p.main}")
    if res.returncode:  # main moved meanwhile: the change only adds to .windvane/, so replay it
        if replayed(p, cd):
            res = net.git(cd, "push", "-q", p.remote, f"HEAD:refs/heads/{p.main}")
        if res.returncode:  # keep the paid-for work on its branch rather than drop it
            return False, push_branch(p, cd, branch, tries)
    return True, p.main


CANT_MERGE = "couldn't merge"  # in a held change's reason: it has a PR, so `status` shows why, not "open a PR"
MERGE_TRIES, MERGE_WAIT = 6, 5  # a new PR's mergeability takes GitHub a few seconds: half a minute of waits at most


def merge_pr(p, cd, branch, pr_n, title):
    """Merge PR `pr_n`, retrying what clears without the user: tried before GitHub knew whether the new PR can merge
    (UNKNOWN), it waits and tries again; with main moved under it (CONFLICTING, BEHIND), the change (only additions to
    .windvane/) is replayed onto the fetched main, force-pushed and tried once more. Returns (merged, why not, in a
    few words for the user)."""
    state, tries, replays = MERGE_STATES["UNKNOWN"], MERGE_TRIES, 1
    while tries:
        tries -= 1
        res = net.call(["gh", "pr", "merge", pr_n, "--merge", *(["--subject", title] * (actions.SKIP in title))],
                       p.main_root())
        if res.returncode == 0:
            return True, ""
        try:
            pr = json.loads(net.sh(p.main_root(), "gh", "pr", "view", pr_n, "--json",
                                   "state,mergeable,mergeStateStatus," + CHECK_FIELDS) or "{}")
        except ValueError:
            pr = {}
        if pr.get("state") == "MERGED":
            return True, ""
        marks, state = {pr.get("mergeable"), pr.get("mergeStateStatus")}, merge_state(pr, res.stderr)
        if "UNKNOWN" in marks or not pr:
            time.sleep(MERGE_WAIT * bool(tries))
        elif marks & {"CONFLICTING", "BEHIND", "DIRTY"} and replays and replayed(p, cd, branch):
            replays, tries = 0, max(tries, 1)  # a replay is always tried
        else:
            break
    return False, state


# GitHub's merge states, in the user's words: the conflict or wait first, then what blocks a mergeable PR.
MERGE_STATES = {"UNKNOWN": "GitHub hadn't worked out whether it can merge", "CONFLICTING": "it conflicts with main",
                "DIRTY": "it conflicts with main", "BEHIND": "main moved on", "DRAFT": "it is a draft",
                "BLOCKED": "a required check or review blocks it",
                # what blocks a BLOCKED PR, when its checks and review decision say (`blocked_by`)
                "FAILED": "a check failed", "REVIEW": "a required review hasn't approved it",
                "PENDING": "required checks are still running"}
CHECK_FIELDS = "statusCheckRollup,reviewDecision"  # what `blocked_by` reads, in the same `gh pr view` call
FAILED_CHECKS = {"FAILURE", "ERROR", "CANCELLED", "TIMED_OUT", "ACTION_REQUIRED", "STARTUP_FAILURE"}


def check_end(c):
    """One rollup entry's outcome, PENDING while it runs: a CheckRun has status and conclusion, a StatusContext a
    state."""
    end = (c.get("conclusion") or "") if c.get("status", "COMPLETED") == "COMPLETED" else "PENDING"
    end = c.get("state") or end
    return "PENDING" if end in ("PENDING", "EXPECTED", "") else end


def running_checks(pr):
    """The names of the PR's checks still running, as `blocked_by` counts them."""
    return [c.get("name") or c.get("context") or "?" for c in pr.get("statusCheckRollup") or []
            if check_end(c) == "PENDING"]


def blocked_by(pr):
    """What blocks a BLOCKED PR, from its checks and review decision: some of FAILED, REVIEW and PENDING, in that
    order. Empty when they show nothing (a rule they don't cover, as a required signature), or the PR isn't BLOCKED.
    The rollup doesn't say which checks are required: a check still running on a PR that's otherwise clear is taken
    for the one that holds it, since a non-required check can't."""
    if pr.get("mergeStateStatus") != "BLOCKED" or pr.get("isDraft"):
        return []
    out = set()
    for c in pr.get("statusCheckRollup") or []:
        end = check_end(c)
        out |= {"FAILED"} if end in FAILED_CHECKS else {"PENDING"} if end == "PENDING" else set()
    if pr.get("reviewDecision") in ("REVIEW_REQUIRED", "CHANGES_REQUESTED"):
        out.add("REVIEW")
    return [k for k in ("FAILED", "REVIEW", "PENDING") if k in out]


def merge_state(pr, stderr):
    """Why a PR didn't merge, in a few words: its conflict or wait, else the merge state that blocks it (and, when
    BLOCKED, what blocks it), else gh's own error when GitHub saw nothing in the way."""
    mergeable, status = pr.get("mergeable", ""), pr.get("mergeStateStatus", "")
    if mergeable in ("CONFLICTING", "UNKNOWN"):
        return MERGE_STATES[mergeable]
    if pr.get("isDraft"):  # its state can still read CLEAN
        status = "DRAFT"
    by = blocked_by(pr)
    if by:  # running checks matter only once nothing else blocks it
        return " and ".join(MERGE_STATES[k] for k in by if k != "PENDING" or by == ["PENDING"])
    return MERGE_STATES.get(status) or cut(" ".join(stderr.split())) or MERGE_STATES["UNKNOWN"]


def replayed(p, cd, branch=""):
    """Replay `cd`'s change onto the fetched main (and force-push it to `branch`, leased on what was pushed there):
    it only adds to .windvane/, so it replays unless main changed the same lines."""
    pushed = gout(cd, "rev-parse", "HEAD")
    net.git(p.main_root(), "fetch", "-q", p.remote)
    if git(cd, *author_ident(p), "rebase", "-q", p.upstream).returncode:
        git(cd, "rebase", "--abort")
        return False
    return not branch or net.git(cd, "push", "-q", f"--force-with-lease=refs/heads/{branch}:{pushed}", p.remote,
                                 f"HEAD:refs/heads/{branch}").returncode == 0


def not_merged(p, cd, branch, pr, pr_n, state, close):
    """A change whose PR still can't merge. With `close` (desk answers, cheap to give again) its PR is closed and its
    branch deleted, so it can't sit conflicting with every later change: (None, why). Any other (a paid plan, a held
    change) keeps its PR and is held as one to land, so `status` lists it: (False, the PR)."""
    why = f"PR #{pr_n} {CANT_MERGE}: {state}" if pr_n.isdigit() else "its PR couldn't be opened"
    log(p, f"{branch}: {why}")
    root = p.main_root()
    if close and (net.call(["gh", "pr", "close", pr_n, "--delete-branch"], root) if pr_n.isdigit()
                  else net.git(root, "push", "-q", p.remote, "--delete", branch)).returncode == 0:
        return None, why
    git(cd, "branch", "-f", branch, "HEAD")
    hold(p, branch, "land", f"{why}; merge it by hand" + f" (gh pr merge {pr_n} --merge)" * pr_n.isdigit())
    return False, pr or branch


def held_branches(p):
    """The branches `hold` noted that still wait to be pushed: unmerged and on no remote branch (pushing one by hand
    under another name counts as pushed), with no PR on github (one pushed by hand, its PR closed and branch deleted,
    stays closed). A change to land pushed by hand stays noted, not pushed again, until main has it or its PR is
    closed. Notes and reasons for any other branch are dropped."""
    held = load_json(p.state / "held.json", {})
    here, tracked = held_here(p, held), {}
    for b, (how, on) in here.items():
        states = p.cfg["forge"] == "github" and {pr["state"] for pr in net.own_prs(
            p, "--head", on.removeprefix(f"{p.remote}/") if on else b, "--state", "all") or []}
        if not states or (on and "OPEN" in states):
            tracked[b] = how
    if tracked != held or set(load_json(p.state / "held_why.json", {})) - set(held):
        unhold(p, set(held) - set(tracked))  # what `gh` was asked about: one held meanwhile stays
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
    2.38 (no `merge-tree --write-tree`), `git cherry` finds a rebased or one-commit squashed change and `squashed_on`
    a squash of several. A squashed change main later reverted counts as landed too (as `git cherry` counts it): the
    user undid it on purpose. When none can tell, it isn't landed: a change kept listed costs a line, a dropped one is
    lost."""
    r = git(root, "merge-tree", "--write-tree", upstream, branch)
    if r.returncode in (0, 1):  # 1: the merge conflicts, so main lacks some of it
        return r.returncode == 0 and r.stdout.split()[:1] == [gout(root, "rev-parse", f"{upstream}^{{tree}}")]
    marks = [line[:1] for line in gout(root, "cherry", upstream, branch).splitlines()]
    return (bool(marks) and "+" not in marks) or squashed_on(root, upstream, branch)


SQUASH_WALK = 200  # upstream commits `squashed_on` reads at most, so a long-lived repo doesn't make `status` slow


def squashed_on(root, upstream, branch):
    """Whether one of the last SQUASH_WALK upstream commits since the merge-base is `branch`'s whole change: a squash
    of several commits, which `git cherry` misses as it compares them one by one. Old `git patch-id` leaves a binary
    file's content out, so binary files are matched by blob instead: the commit has the change's patch-id without
    them (or, for binary files alone, touches them) and holds each at the branch's blob."""
    base = gout(root, "merge-base", upstream, branch)
    if not base:
        return False
    binary = binary_files(root, base, branch)
    literal = [f":(literal){b}" for b in binary]  # a name such as `:(icase)logo.png` is a path, not pathspec magic
    rest = ["--", ".", *(f":(exclude,literal){b}" for b in binary)]
    change = patch_ids(root, raw(root, "diff-tree", "-p", base, branch, *rest))
    # raw, so it ends in a newline: `diff-tree --stdin` drops a last line without one
    recent = raw(root, "rev-list", "--no-merges", f"-{SQUASH_WALK}", f"{base}..{upstream}")
    if change:
        hits = [c for pid, c in patch_ids(root, raw(root, "diff-tree", "--stdin", "-p", *rest, stdin=recent))
                if pid == change[0][0]]
    elif binary:
        hits = raw(root, "diff-tree", "--stdin", "-s", "--format=%H", "--", *literal, stdin=recent).decode().split()
    else:
        return False
    if not binary:
        return bool(hits)
    blobs = tree_entries(root, branch, literal)
    return blobs is not None and any(tree_entries(root, c, literal) == blobs for c in hits)


def tree_entries(root, rev, pathspecs):
    """`rev`'s tree entries (mode, blob, path) for `pathspecs`, or None when ls-tree fails: two failures both print
    nothing, and must not pass for the same blobs."""
    r = subprocess.run(["git", "ls-tree", "--full-tree", "-z", rev, "--", *pathspecs], cwd=root, capture_output=True)
    return r.stdout if r.returncode == 0 else None


def binary_files(root, base, branch):
    """The paths of binary files `branch` changes since `base` (numstat's "-" counts). `--no-renames` splits a renamed
    binary file into a delete and an add, so both paths get their blob checked; `-z` keeps a name that isn't UTF-8
    intact."""
    out = raw(root, "diff-tree", "-r", "--numstat", "--no-renames", "-z", base, branch)
    return [line[4:] for line in out.decode(errors="surrogateescape").split("\0") if line.startswith("-\t-\t")]


def raw(root, *args, stdin=b""):
    """git `args`' output as bytes: a diff or a path needn't be UTF-8."""
    return subprocess.run(["git", *args], cwd=root, input=stdin, capture_output=True).stdout


def patch_ids(root, diff):
    """(stable patch-id, commit) for each patch in `diff`. Diffs come from plumbing (`diff-tree`), so the user's diff
    settings (renames, colour, prefixes) can't change a patch-id."""
    ids = subprocess.run(["git", "patch-id", "--stable"], cwd=root, input=diff, capture_output=True).stdout
    return [tuple(line.decode().split()[:2]) for line in ids.splitlines()]


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
            + (merge if on and CANT_MERGE not in why else why)
            for b, why, at, on in (held_now(p) if now is None else now)]


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
    if not to_land or landed:  # one whose PR can't merge stays held (`land` noted it), for `status` to list
        git(p.main_root(), "branch", "-D", branch)
        unhold(p, [branch])
    no_pr = not to_land and p.cfg["forge"] == "github" and where == branch
    how = (("merged" if landed else "not merged; merge it by hand") if to_land
           else "pushed; no PR opened, open one by hand" if no_pr else "pushed")
    log(p, f"{branch}: held back, now on {where}, {how}")
    first = whole("", title, f": on {where}, {how}.", log=lambda m: log(p, m))
    return first, no_pr or (to_land and not landed)
