"""The plan run: a planner session writes tickets for a goal (and the critic's follow-ups) on a branch the driver
commits, then pushes or lands it."""
import fcntl
import re
from pathlib import Path

from . import actions, critic, net, requests
from .held import author_ident, is_local, land, local_push, n_tickets, push_branch
from .phone import whole
from .queue import Queue
from .runner import NO_REQUEST, rules, run_claude
from .util import git, gout, last_match, load_json, log, now, push, save_json
from .worktree import clean_detach, ensure_worktree

PLAN_RE = re.compile(r"PLAN: (?:new=(\d+) noted=(\d+)|NONE)[ \t]*(.*)")


def changed_paths(cwd):
    """Every path with uncommitted changes, both sides of a rename. -z: no quoting, no stripped leading spaces."""
    out = git(cwd, "status", "--porcelain", "-z", "--untracked-files=all").stdout.split("\0")
    paths, i = [], 0
    while i < len(out):
        entry = out[i]
        if len(entry) > 3:
            paths.append(entry[3:])
            if entry[0] in "RC":  # a rename's source follows as its own field
                i += 1
                paths.append(out[i])
        i += 1
    return paths


class PlanBusy(Exception):
    """Another plan holds the lock: raised for the desk's `windvane plan`, which must say so."""


def plan(p, goal, use_followups=True, usage=None, stop_if=None, merge=False, max_new=6, busy_raises=False):
    """A planner run writes tickets for `goal` (and the critic's follow-ups) on a branch the driver commits;
    `merge` merges it at once. Returns how many tickets it merged (with `merge`) or wrote; while another plan runs,
    0 (or PlanBusy with `busy_raises`)."""
    p.state.mkdir(parents=True, exist_ok=True)
    with open(p.state / "plan.lock", "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            log(p, "plan: another plan is running")
            if busy_raises:
                raise PlanBusy("another plan is running")
            return 0
        return _plan(p, goal, use_followups, usage, stop_if, merge, max_new)


def pending_ids(p):
    """Ticket ids on unmerged remote `plan-*` and `periodic-*` branches: tickets whose PR is still open have claimed
    them, and so do local ones (a plan whose push was refused, waiting to be pushed by hand). A branch whose PR was
    closed unmerged frees its ids (its branch is left alone)."""
    return set(pending_branches(p))


def pending_branches(p):
    """`pending_ids` as {ticket id: the branch it waits on}: a request accepted into a held ticket names its branch."""
    ids = {}
    remote = gout(p.main_root(), "branch", "-r", "--no-merged", p.upstream, "--format=%(refname:short)").split()
    local = gout(p.main_root(), "branch", "--no-merged", p.upstream, "--format=%(refname:short)").split()
    heads = {b: b.removeprefix(f"{p.remote}/") if b in remote else b for b in remote + local}
    heads = {b: name for b, name in heads.items() if name.startswith(("plan-", "periodic-"))}
    closed = closed_prs(p, set(heads.values()))
    for b, name in heads.items():
        if name not in closed:
            names = gout(p.main_root(), "ls-tree", "--name-only", b, p.tickets_rel + "/").split()
            ids.update((t, name) for n in names if n.endswith(".md") for t in [p.norm(Path(n).stem)] if t)
    return ids


def closed_prs(p, branches):
    """Which of `branches` have PRs that were all closed unmerged; none off github or without `gh` (every branch
    counts). One query per branch rather than a list of recent PRs, so no limit drops an old closed plan; a branch
    found closed is remembered in closed_plans.json and not asked about again (each query takes ~0.8 s, and the
    closed ones pile up, since their paid-for branches are kept). One list of the open PRs per call notices a
    reopened one: its branch leaves the file and claims its ids again, or a new plan would reuse them; when that
    list fails, no branch counts as closed and the file stays. Requests `requests.reopen` sent back to the inbox
    from a closed plan are held on it again once it reopens."""
    if p.cfg["forge"] != "github":
        return set()
    f = p.state / "closed_plans.json"
    known = set(load_json(f, []))
    reopened = set()
    if known & set(branches):
        r = net.call(["gh", "pr", "list", "--state", "open", "--limit", "1000", "--json", "headRefName", "-q",
                      ".[].headRefName"], p.main_root())
        if r.returncode:  # can't tell which reopened: all keep their ids this time (a skipped id costs nothing)
            return set()
        reopened = set(r.stdout.split())
    closed = (known - reopened) & set(branches)
    for b in sorted(set(branches) - known - reopened):
        states = set(net.sh(p.main_root(), "gh", "pr", "list", "--head", b, "--state", "all", "--json", "state",
                            "-q", ".[].state").split())
        if states == {"CLOSED"}:
            closed.add(b)
    keep = closed
    if any(e["requests"] for e in p.periodic.values()):  # only the triaging project's branches hold accepts
        try:  # every closed one, each time: a plan closed before this existed, or a move cut off, is caught next time
            for rid, b in requests.reopen(closed).items():
                log(p, f"{rid}: back in the request inbox: {b}, which held its ticket, was closed unmerged")
        except OSError as e:
            log(p, f"requests held on closed plans not reopened yet ({e}): tried again next time")
        try:  # once per reopened plan: its line leaves the file only when this went through
            for rid, b in requests.rehold(reopened & known).items():
                log(p, f"{rid}: held on {b} again: its PR was reopened")
        except OSError as e:
            keep = closed | (reopened & known)
            log(p, f"requests on reopened plans not held again yet ({e}): tried again next time")
    if keep != known:  # a deleted branch's line goes too
        save_json(f, sorted(keep))
    return closed


def plan_branch(p, cd):
    """`plan-<date>-<time to the second>`, with a suffix should that name be taken here or on the remote."""
    base = name = f"plan-{now():%Y%m%d-%H%M%S}"
    n = 1
    while any(git(cd, "rev-parse", "-q", "--verify", ref).returncode == 0
              for ref in (f"refs/heads/{name}", f"refs/remotes/{p.remote}/{name}")):
        n += 1
        name = f"{base}-{n}"
    return name


def unmerged(p, n, where):
    """A plan that couldn't merge counts as none (so `run --replan` doesn't take it for new work), but the user
    hears where it waits: it was a paid planner session."""
    clean_detach(p, p.plan_dir, p.upstream)
    how = "" if is_local(where) else ": merge it by hand"  # a local branch's reason says what to do
    log(p, f"plan: {n_tickets(n)} on {where}, not merged{how}")
    waits = f"{n_tickets(n)} {'waits' if n == 1 else 'wait'} on"
    body = (whole(f"{waits} ", where, f"{how}.", log=lambda m: log(p, m)) if how
            else local_push(where, f"{waits} the local branch", lambda m: log(p, m)))
    push("plan not merged", body, 4, "warning")
    return 0


def _plan(p, goal, use_followups, usage, stop_if, merge, max_new):
    cd = p.plan_dir
    net.git(p.main_root(), "fetch", "-q", "--prune", p.remote)
    ensure_worktree(p, cd, setup=False)
    if not clean_detach(p, cd, p.upstream):
        log(p, "plan: can't check out main")
        return 0
    q = Queue(p)
    first = int(re.search(r"\d+", q.next_id(pending_ids(p))).group())
    ids = [f"{p.prefix}-{n:03d}" for n in range(first, first + max_new)]
    notes = critic.followups(p) if use_followups else []
    if not goal and not notes:
        log(p, "plan: no goal and no critic follow-ups")
        return 0
    prompt = "\n\n".join(filter(None, [
        f"Plan tickets for this project. Today is {now():%Y-%m-%d}. Follow the planner rules in your system prompt "
        "and end with the PLAN line.",
        f"## The goal\n{goal}" if goal else "",
        "## The critic's follow-up notes since the last plan (ticket, point)\n"
        + "\n".join(f"- ({t}) {b}" for t, b in notes[:60]) if notes else "",
        f"## Existing tickets (id, status, title)\n{q.listing() or '(none yet)'}",
        f"## Ids for new tickets, in order (at most {max_new})\n{', '.join(ids)}",
    ]))
    log(p, f"plan run ({len(notes)} critic notes)")
    deny = ["Agent", "Bash(git commit *)", "Bash(git push *)", "Bash(git switch *)", "Bash(git checkout *)",
            "Bash(gh *)", "NotebookEdit", NO_REQUEST]
    while True:
        text, killed, error = run_claude(p, "planner", "plan", prompt, rules(p, "PLAN.md"), cd,
                                         ["Bash(git ls-files *)"], deny, usage, stop_if)
        m = last_match(PLAN_RE, text)
        if killed or m or not net.down_wait():  # cut off by an outage: once it's back, plan again from scratch
            break
        log(p, "plan: the run was cut off with the network down; running it again")
        clean_detach(p, cd, p.upstream)
    changed = changed_paths(cd)
    if killed or not m or not changed:
        clean_detach(p, cd, p.upstream)
        log(p, f"plan: nothing written ({killed or error or (m.group(0) if m else 'no PLAN line')})")
        return 0
    outside = [c for c in changed if not c.startswith(".windvane/")]
    if outside:
        clean_detach(p, cd, p.upstream)
        log(p, f"plan: wrote outside .windvane/ ({outside[0]}); discarded")
        return 0
    n = int(m.group(1) or 0)
    branch = plan_branch(p, cd)
    git(cd, "add", "-A", ".windvane")
    title = actions.tag(p, f"Plan: {n_tickets(n)} ({m.group(3)[:60]})", cd)
    git(cd, *author_ident(p), "commit", "-q", "-m", title, "-m", text.strip()[-3000:])
    if notes:
        from .util import append_jsonl
        append_jsonl(p.state / "critic.jsonl", {"ticket": "PLAN", "step": "plan", "notes": len(notes)})
    if merge:
        ok, where = land(p, cd, branch, f"Plan: {n_tickets(n, 'new ')}", text.strip()[-6000:], commit=False)
        if not ok:
            return unmerged(p, n, where)
        log(p, f"plan: {n_tickets(n)} on {where} (merged)")
    else:
        where = push_branch(p, cd, branch)
        if is_local(where):
            clean_detach(p, cd, p.upstream)
            kept = f"{n_tickets(n)} kept only locally on {where.removeprefix('the local branch ')}"
            log(p, f"plan: {kept}")
            push("plan not pushed", local_push(where, f"{n_tickets(n)} kept only locally on", lambda m: log(p, m)),
                 4, "warning")
            return 0
        if p.cfg["forge"] == "github":
            pr = net.sh(cd, "gh", "pr", "create", "--head", branch, "--base", p.main, "--title",
                        f"Plan: {n_tickets(n, 'new ')}", "--body", text.strip()[-6000:])
            log(p, f"plan: {n_tickets(n)} on {pr or branch}")
        else:
            log(p, f"plan: {n_tickets(n)} pushed to {branch}")
    push(f"planned {n_tickets(n)}", whole("", m.group(3) or "See the queue.", log=lambda m: log(p, m)), 3, "memo")
    clean_detach(p, cd, p.upstream)
    return n
