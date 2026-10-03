"""The autopilot driver: works a project's tickets unattended until a deadline, one fresh headless Claude session
per ticket, in its own worktree so the user's checkout is never touched.

Per ticket: a worker run builds it (and has the critic judge it before the check), then the driver itself runs the
project's check on the exact head (reusing the worker's pass for the same tree), a fresh read-only reviewer reads the
change, and the driver merges it after checking the merged tree too. Anything that fails a stage goes back to the
ticket's next worker run as written feedback. Design questions go to the user's phone; in their quiet times runs
build their recommendation and record it instead.

Stops at the deadline, `windvane stop`, two failed tickets in a row, an empty queue, the weekly cap, or sessions that
keep dying (API errors, crashes; each is retried after a growing pause first). A network outage stops nothing: it is
waited out (net.py), and what it broke is resumed with no retry spent. At the 5-hour cap it stops the running
session and pauses until the window resets. A detached run's driver is itself restarted after a crash (`supervise`).
"""
import fcntl
import os
import signal
import sys
import threading
import re
import shutil
import subprocess
import time
import traceback
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path

from . import critic, net, phone, renumber, selfupdate
from .renumber import OLD_NUMBERS, said
from .desk import Desk
from .periodic import Periodic
from .config import CHECKOUT, DIR, HOME, exe, slots
from .jobs import check, tail
from .questions import (answered, desk_answers, give_answer, mode, parts, phone_answer, provisional, quiet_now, section)
from .queue import Queue
from .runner import Usage, over_caps, run_claude
from .util import git, gout, hhmm, last_match, load_json, log, now, push, read_jsonl, save_json

RESULT_RE = re.compile(r"RESULT: (READY|PARTIAL|BLOCKED|FAILED) (\S+)[ \t]*(.*)")
REVIEW_RE = re.compile(r"REVIEW: (APPROVE|CHANGES) (\S+)[ \t]*(.*)")
PLAN_RE = re.compile(r"PLAN: (?:new=(\d+) noted=(\d+)|NONE)[ \t]*(.*)")
CARRIED = ("periodic_at", "groups_seen", "held", "skip", "merged", "failed", "partials", "reviews", "bounces", "scores", "plans", "fail_streak", "peak",
           "conflicts", "spent", "update_said", "scale_said", "desk_merged")  # the run's own memory, handed to the driver a self-update restarts
# Each kind of send-back spends its own budget: (who sent it back, config table, key, send-backs allowed over the key
# and why). The critic gives its last verdict after `revisions` itself; the spare one covers a READY without a pack.
BUDGETS = {"review": ("a missing PR", "autopilot", "review_rounds", 0, ""),
           "critic": ("the critic", "critic", "revisions", 1, "one more for a READY without the evidence pack"),
           "check": ("the check", "autopilot", "check_retries", 0, ""),
           "conflict": ("a merge conflict", "autopilot", "conflict_retries", 0, "")}
PULL_EVERY = 600  # seconds between looks for a newer windvane while no ticket merges (a wait for answers)
NAP = 5  # seconds between a waiting driver's looks at STOP and the deadline
RESTARTS = 3  # crashes in an hour a detached run's driver is restarted from; the next one ends the run
RESTART_PAUSE = 30  # seconds before such a restart, so a driver that dies at once doesn't spin
SUPERVISED = "WINDVANE_SUPERVISED"  # set for a driver under `supervise`: it hands a crash over instead of pushing it


def said_times(n):
    return f"{n} time{'s' * (n != 1)}"


def rules(p, name):
    text = (HOME / "prompts" / name).read_text()
    for k, v in {"{upstream}": p.upstream, "{remote}": p.remote, "{main}": p.main}.items():
        text = text.replace(k, v)
    return text


def worker_perms(p):
    m = p.main
    deny = [f"Bash(git push * {m})", f"Bash(git push * HEAD:{m})", f"Bash(git push * {m}:{m})",
            "Bash(git push --force *)", "Bash(git push -f *)", "Bash(git reset --hard *)", "Bash(gh pr merge *)",
            f"Bash(git switch {m})", f"Bash(git checkout {m})", "Bash(git worktree *)"]
    if not p.ap["subagents"]:
        deny.append("Agent")
    allow = ["Bash(windvane *)", "Bash(git *)", "Bash(gh pr create *)", "Bash(gh pr view *)", "Bash(gh pr edit *)",
             "Bash(gh pr comment *)", "Bash(gh pr list *)"]
    return allow, deny


REVIEW_DENY = ["Edit", "Write", "NotebookEdit", "Agent", "Bash(git push *)", "Bash(git commit *)",
               "Bash(git switch *)", "Bash(git checkout *)", "Bash(git reset *)", "Bash(gh pr merge *)",
               "Bash(gh pr edit *)", "Bash(gh pr comment *)", "Bash(gh pr close *)"]


def shared(paths, others):
    """The paths two Touches lists have in common: a file both name, or the file in a directory the other names."""
    out = set()
    for a in paths:
        for b in others:
            a_, b_ = a.rstrip("/"), b.rstrip("/")
            if a_ == b_ or a_.startswith(b_ + "/") or b_.startswith(a_ + "/"):
                out.add(max(a, b, key=len))
    return sorted(out)


def names(text, path):
    """Whether a ticket's text names `path` as a path of its own (`a.py` doesn't match `data.py` or `a.py.bak`); a
    directory (`docs/`) matches anything under it too."""
    end = "" if path.endswith("/") else r"(?![\w/-]|\.\w)"
    return re.search(rf"(?<![\w/.-]){re.escape(path)}{end}", text) is not None


def overlaps(paths, others):
    return bool(shared(paths, others))


def nth(n):
    return {1: "1st", 2: "2nd", 3: "3rd"}.get(n, f"{n}th")


def ensure_worktree(p, path, setup=True):
    """The autopilot's own worktree at `path`, created detached at upstream (and set up) when missing. With
    isolate off, work_dir and ci_dir are the checkout itself, which is left alone."""
    if path == p.main_root() or (path / ".git").exists():
        return
    root = p.main_root()
    net.git(root, "fetch", "-q", p.remote)
    git(root, "worktree", "prune")
    r = git(root, "worktree", "add", "--detach", str(path), p.upstream)
    if r.returncode:
        raise SystemExit(f"windvane: can't create the worktree {path}: {r.stderr.strip()}")
    if setup and p.cfg["setup"]:
        log(p, f"setting up {path}: {p.cfg['setup']}")
        lines = deque(maxlen=15)
        with subprocess.Popen(p.cfg["setup"], shell=True, cwd=path, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              text=True, errors="replace") as r:
            for line in r.stdout:  # shown as it comes (a long `npm ci` doesn't look stuck), the end kept for the log
                print(line, end="", flush=True)
                lines.append(line.rstrip())
        if r.returncode:  # a half-made tree fails every check: remove it, so the next start sets it up afresh
            git(root, "worktree", "remove", "--force", str(path))
            shutil.rmtree(path, ignore_errors=True)
            git(root, "worktree", "prune")
            raise SetupFailed(f"setup `{p.cfg['setup']}` failed (exit {r.returncode}) in {path}", list(lines))


class SetupFailed(Exception):
    """The project's `setup` command failed in a fresh worktree; `lines` are the end of its output."""

    def __init__(self, why, lines):
        super().__init__(why)
        self.lines = lines


def clean_detach(p, cwd, rev):
    """Put the driver's own worktree on `rev`, discarding anything left there. Never used on the worker's tree."""
    git(cwd, "merge", "--abort")
    git(cwd, "reset", "-q", "--hard")
    git(cwd, "clean", "-qfd")
    return git(cwd, "switch", "-q", "--detach", rev).returncode == 0


def pr_for(p, branch):
    if p.cfg["forge"] != "github" or not branch:
        return ""
    return net.sh(p.main_root(), "gh", "pr", "list", "--head", branch, "--state", "open", "--json", "number",
                  "-q", ".[0].number")


class Pilot(Desk, Periodic):
    def __init__(self, p, a):
        self.p, self.a = p, a
        phone.use(p)
        self.usage = Usage(p)
        self.state_file = p.state / "state.json"
        s = load_json(self.state_file, {})
        self.feedback = s.get("feedback", {})  # ticket -> what its next worker run must act on
        self.questions = s.get("questions", {})  # ticket -> its open question for the user
        self.costs = s.get("costs", [])  # 5-hour-window points each ticket took
        self.approved = s.get("approved", {})  # ticket -> its approved head bounced off main: {"sha", "why"}
        self.skip, self.merged, self.failed = set(), [], []
        self.partials, self.reviews, self.scores, self.bounces = {}, {}, {}, {}  # bounces: ticket -> {kind: n}
        self.fail_streak, self.plans, self.was_quiet, self.idle = 0, 0, False, False
        self.crashes, self.halt, self.unfinished = 0, None, set()  # sessions dead in a row; why the run must stop
        self.sleep = time.sleep  # the retry backoff's; tests make it instant
        self.stop_reason = "unknown"
        self.deadline, self.kill_all = None, False  # set by run(); `windvane stop --now` sets kill_all
        self.worker_rules, self.review_rules = rules(p, "WORKER.md"), rules(p, "REVIEW.md")
        # Parallel slots: each has its own work and ci worktrees, made when first used. Merges and fetches are
        # serialized. `n` is the most at once; the queue's Touches and the usage window decide how many really run.
        self.n = slots(p, getattr(a, "parallel", None))
        self.tl = threading.local()
        self.lock, self.merge_lock, self.git_lock = threading.RLock(), threading.Lock(), threading.Lock()
        self.running = {}  # ticket -> slot
        self.owner = {i: None for i in range(self.n)}  # slot -> the ticket whose uncommitted work its tree holds
        self.threads, self.wake = [], threading.Event()
        self.waits, self.said = [], set()  # why ready tickets can't start beside the running ones; what the log has
        # Side by side only while it pays: `limit` drops a slot for every 2 conflict bounces between tickets that ran
        # beside another (`beside`); `peak` is the most that ran at once; `minutes` is recent tickets' running time:
        # `began` is when each running attempt started, `spent` the seconds a ticket's finished attempts ran.
        self.limit, self.peak, self.conflicts, self.beside = self.n, 1, 0, set()
        self.began, self.spent = {}, {}
        self.minutes = s.get("minutes", [])
        # Self-update between tickets (`selfupdate`): the install it pulls (None: off, as in the tests) and how the
        # driver restarts itself when its own code changed.
        self.home = HOME if CHECKOUT and os.environ.get("WINDVANE_SELF_UPDATE") != "0" else None
        self.argv, self.execv, self.update_said, self.newer = sys.argv[1:], os.execv, [], None
        self.pulled = (0, -1)  # (when, merges then): pull after each merge, else every PULL_EVERY seconds at most
        self.scale_said = False  # the collapsed-scale warning: logged and pushed once a run
        self.stash_failed = False  # a failed stash of the checkout's own changes: pushed once a run
        self.periodic_at, self.held = {}, []  # [[periodic]] (periodic.py): name -> merges when it last ran; tickets held
        self.groups_seen, self.groups_read = {}, (None, None)  # name -> groups finished then; (main's sha, its queue)
        self.groups_wait = {}  # name -> merges when a round was skipped: its finished groups wait for the next merge
        self.desk_init(s.get("desk", {}))  # the user's labelled PRs, merged between tickets (desk.py)

    def slot_dirs(self, i):
        p = self.p
        return (p.work_dir, p.ci_dir) if i == 0 else (p.state / f"work-{i}", p.state / f"ci-{i}")

    @property
    def wd(self):
        return getattr(self.tl, "wd", self.p.work_dir)

    @property
    def cd(self):
        return getattr(self.tl, "cd", self.p.ci_dir)

    def fetch(self):
        with self.git_lock:
            net.git(self.p.main_root(), "fetch", "-q", "--prune", self.p.remote)

    def save(self):
        with self.lock:
                save_json(self.state_file, {"feedback": dict(self.feedback), "questions": dict(self.questions),
                                        "approved": dict(self.approved), "desk": dict(self.desk),
                                        "costs": self.costs[-10:], "minutes": self.minutes[-10:],
                                        "running": dict(self.running)})

    def log(self, msg):
        log(self.p, msg)

    def name(self, tid):
        t = self.queue.titles.get(tid, "") if hasattr(self, "queue") else ""
        return f"{t[:45]} ({tid})" if t else tid

    def end(self):
        """The run's deadline, live: `windvane deadline` moves it by writing DEADLINE in the state dir, and every
        session's stop rule reads it at each poll, so new tickets and running ones keep to the new time."""
        try:
            t = datetime.fromisoformat((self.p.state / "DEADLINE").read_text().strip())
        except (OSError, ValueError):
            return self.deadline
        with self.lock:
            if t != self.deadline:
                self.deadline = t
                self.log(f"deadline moved to {hhmm(t)}")
        return t

    def stopping_now(self):
        """`windvane stop --now`: every session of the run is killed at its next poll."""
        if not self.kill_all:
            try:
                self.kill_all = (self.p.state / "STOP").read_text().strip() == "now"
            except OSError:
                pass
        return self.kill_all

    def stopper(self, limit):
        """A session's stop rule: `limit` (its own time box), the run's deadline as it now is, the caps, a stop now."""
        def stop_if():
            if self.stopping_now():
                return "stop now"
            return "time" if now() >= min(limit, self.end()) else over_caps(self.usage, self.a.cap, self.a.week_cap)
        return stop_if

    def claude(self, role, label, prompt, system, cwd, limit, allow=(), deny=(), ticket=""):
        text, killed, error = run_claude(self.p, role, label, prompt, system, cwd, allow, deny, self.usage,
                                         self.stopper(limit), ticket,
                                         (min(limit, self.end()), self.a.cap, self.a.week_cap))
        if killed:
            self.log(f"  stopped {label}: {killed} ({self.usage.text()})")
        elif not error:
            self.crashes = 0
        return text, killed, error

    def wait_online(self):
        """Wait out a network outage; False when a stop or the deadline ended the wait."""
        return net.wait(self.p, self.end, lambda: (self.p.state / "STOP").exists(), self.sleep)

    def crashed(self, label, error):
        """A session died for a reason outside its ticket (API overload, network, not logged in): pause, longer each
        time, and return True to run it again. Too many in a row stop the run instead of failing ticket after ticket
        on a broken API; nothing is pushed until then. With the network down it waits for it instead, and the rerun
        spends no retry."""
        if not net.online():
            self.log(f"  {label} died ({error}) with the network down; rerun when it's back, no retry spent")
            return self.wait_online()
        self.crashes += 1
        n, wait = self.p.ap["retries"], self.p.ap["retry_minutes"] * 2 ** (self.crashes - 1)
        if self.crashes > n:
            self.halt = f"{self.crashes} sessions in a row died ({error})"
        elif now() + timedelta(minutes=wait) >= self.end() - timedelta(minutes=self.a.min_slot):
            self.halt = f"a session died ({error}), and there's no time left to retry it"
        if self.halt:
            self.log(f"  {label}: {self.halt}")
            return False
        self.log(f"  {label} died ({error}); retry {self.crashes}/{n} in {wait:g} min")
        for _ in range(round(wait)):
            if (self.p.state / "STOP").exists() or now() >= self.end():
                return False
            self.sleep(60)
        return True

    # ---------- one ticket

    def work(self, tid, kind, branches):
        p, wd = self.p, self.wd
        start_five = self.usage.five
        limit = now() + timedelta(hours=self.a.ticket_hours)
        kill_at = min(self.end(), limit)
        wrap = kill_at - timedelta(minutes=20)
        ref = f"{p.remote}/{branches[0]}" if branches else p.upstream
        ticket = gout(wd, "show", f"{ref}:{p.tickets_rel}/{tid}.md") or self.queue.text.get(tid, "")
        dirty = gout(wd, "status", "--porcelain")
        here = gout(wd, "rev-parse", "--abbrev-ref", "HEAD")
        fb = self.feedback.get(tid)
        pr = pr_for(p, branches[0]) if branches else ""
        known = self.answered(tid, ref)
        prompt = "\n\n".join(filter(None, [
            f"Ticket {tid}. Follow the worker rules in your system prompt and end with the RESULT line.",
            f"## The ticket\n{ticket[:8000]}",
            f"## Act on this first\n{fb}" if fb else "",
            "## The user already answered these (under ## Answered in QUESTIONS.md): build them, don't ask again\n"
            + "\n\n".join(known) if known else "",
            (f"In progress on branch(es) {', '.join(branches)}" + (f", PR #{pr}" if pr else "") + ". Resume it.")
            if branches else "A new ticket: branch it off " + p.upstream + ".",
            f"The worktree is on `{here}` with uncommitted changes from an earlier run:\n{dirty[:1500]}" if dirty else
            f"The worktree is on `{here}`, left by an earlier run." if here not in ("HEAD", *branches) else "",
            (f"The critic is on: your evidence pack goes in {critic.pack_dir(p, tid)}/ (pass mark "
             f"{p.critic['pass']}/10)." if p.critic["enabled"] else "The critic is off for this run: skip rules step 6."),
            f"Check command (`windvane check` runs it): `{p.cfg['check']}`." if p.cfg["check"] else
            "No check is configured: `windvane check` passes at once; run the tests you wrote yourself.",
            f"Forge: {p.cfg['forge']}. Design questions now: {mode(p)}.",
            f"Wrap-up time: {hhmm(wrap)} (the run is stopped at {hhmm(kill_at)}); check with `date`.",
        ]))
        self.log(f"{tid}: worker run ({kind}{', ' + ' '.join(branches) if branches else ''})")
        allow, deny = worker_perms(p)
        text, killed, error = self.claude("worker", tid, prompt, self.worker_rules, wd, limit, allow, deny, tid)
        if error:
            self.crashed(tid, error)  # the loop picks the ticket again, with its branch as it now is
        else:
            self.handle(tid, text, killed)
        if start_five is not None and self.usage.five is not None and self.usage.five >= start_five:
            self.costs.append(round(self.usage.five - start_five, 1))
        self.save()

    def answered(self, tid, ref):
        """The ticket's answered questions on main (preflight puts them there) and on its own branch."""
        out = []
        for r in dict.fromkeys((self.p.upstream, ref)):
            for b in answered(gout(self.wd, "show", f"{r}:{self.p.questions_rel}"), tid):
                if b not in out:
                    out.append(b)
        return out[:10]

    def handle(self, tid, text, killed):
        if killed:
            if killed == "time" and now() < self.end() - timedelta(minutes=5):
                self.log(f"{tid}: hit the {self.a.ticket_hours:g}-hour limit for one run; set aside")
                self.skip.add(tid)
            return
        m = last_match(RESULT_RE, text)
        branch = gout(self.wd, "rev-parse", "--abbrev-ref", "HEAD")
        branch = "" if branch == "HEAD" else branch
        if not m or self.p.norm(m.group(2)) != tid:
            return self.unfinished_run(tid, branch)
        self.unfinished.discard(tid)
        status, rest = m.group(1), m.group(3).strip()
        if status == "PARTIAL":
            self.partials[tid] = self.partials.get(tid, 0) + 1
            self.log(f"{tid}: partial ({self.partials[tid]}): {rest}")
            if self.partials[tid] >= self.p.ap["partials"]:
                self.skip.add(tid)
                push(f"Set aside: {self.name(tid)}", f"{self.partials[tid]} runs in a row ran out of time. Left: {rest}",
                     3, "hourglass")
            return
        if status == "FAILED":
            return self.fail(tid, rest)
        if status == "BLOCKED":
            return self.ask(tid, branch, rest)
        sha = next(iter(re.findall(r"\b[0-9a-f]{40}\b", rest)), "") or gout(self.wd, "rev-parse", "HEAD")
        self.feedback.pop(tid, None)
        self.after(tid, branch, sha)

    def unfinished_run(self, tid, branch):
        """A run that ended normally but without its RESULT line may have done all the work: one more run, told
        where its branch is, can finish it. Twice in a row fails the ticket. A run the network cut off ("API Error:
        No response from API") is resumed when it's back, and doesn't count."""
        cut = not net.online()
        if cut:
            self.log(f"{tid}: the run ended without a result line with the network down")
            if not self.wait_online():
                return  # a stop or the deadline: the ticket stays as it is, for the next run
        elif tid in self.unfinished:
            return self.fail(tid, "the run ended without a result line, twice")
        else:
            self.unfinished.add(tid)
        head = gout(self.wd, "rev-parse", "--short=12", "HEAD")
        old = self.feedback.get(tid, "")
        why = "was cut off by a network outage" if cut else f"ended without the RESULT line for {tid}"
        self.feedback[tid] = (f"Your last run {why}. The worktree is on "
                              f"`{branch or 'a detached HEAD'}` at {head}: finish the rules' steps still left from "
                              "there, then end with the RESULT line."
                              + (f"\n\nWhat that run had to act on:\n{old}" if old and "RESULT line" not in old[:80]
                                 else ""))
        self.log(f"{tid}: " + ("the network is back; resuming the run" if cut else
                               "the run ended without a result line; one more run to finish it"))

    def after(self, tid, branch, sha):
        """A READY head: critic gate, check, review, merge. Each failed stage writes the next run's feedback."""
        p = self.p
        if self.stopping_now():  # the next run's worker finds the head pushed and finishes READY again
            return
        self.fetch()
        if gout(p.main_root(), "rev-parse", "--verify", "-q", f"{sha}^{{commit}}") == "" or \
                (branch and gout(p.main_root(), "rev-parse", f"{p.remote}/{branch}") != sha):
            self.feedback[tid] = (f"Your RESULT line named {sha[:12]}, but `{p.remote}/{branch or '?'}` isn't at it. "
                                  "Push the final head, then finish READY with its full sha.")
            return self.log(f"{tid}: READY head {sha[:12]} isn't pushed; back to the worker")
        ap = self.approved.get(tid)
        res = ap and critic.resolution(self.cd, ap["sha"], sha, p.upstream, ap.get("why"))
        if ap and not res:
            self.approved.pop(tid)  # more than a resolution: the full critic and review again
        if res:
            self.log(f"{tid}: only merges {p.main} into the approved {ap['sha'][:7]} and resolves it "
                     f"({len(res[1])} file{'s' * (len(res[1]) != 1)}); the critic's verdict stands")
        elif not self.critic_gate(tid, sha, stale=ap and ap["sha"], branch=branch) or self.stopping_now():
            return
        if not self.keep_changes(f"checking {tid}"):
            return self.fail(tid, "couldn't stash the uncommitted changes in your checkout before its check")
        ok, summary, logf = self.check_at(sha)
        self.log(f"{tid}: check on {sha[:7]}: {summary}")
        if not ok:
            self.feedback[tid] = f"The check fails on {sha[:12]}: {summary}\n```\n{tail(logf, 40)}\n```"
            return self.bounce(tid, "check")
        if res and not res[0]:  # git merged it cleanly: like any approved head that merges, nothing new to review
            self.log(f"{tid}: a clean merge of {p.main}; no review needed")
            return self.integrate(tid, branch, sha)
        verdict, review = self.review(tid, branch, sha, res and (ap["sha"], *res))
        if verdict is None:
            return
        if verdict == "CHANGES":
            self.feedback[tid] = f"The reviewer asks for changes on {sha[:12]}:\n{review[-4000:]}"
            self.reviews[tid] = self.reviews.get(tid, 0) + 1
            if self.reviews[tid] >= p.ap["review_rounds"]:
                return self.fail(tid, f"the reviewer asked for changes {said_times(self.reviews[tid])}, the most "
                                      f"`[autopilot] review_rounds` = {p.ap['review_rounds']} allows")
            return
        self.integrate(tid, branch, sha)

    def bounce(self, tid, kind="review"):
        """A stage sent the ticket back: count it against that stage's own budget (BUDGETS), so a ticket can't loop
        forever and a slow or flaky check can't spend the reviewer's rounds."""
        what, table, key, spare, why = BUDGETS[kind]
        if kind == "review":  # shares the count review CHANGES make
            n = self.reviews[tid] = self.reviews.get(tid, 0) + 1
        else:
            n = self.bounces.setdefault(tid, {})[kind] = self.bounces.get(tid, {}).get(kind, 0) + 1
        limit = (self.p.critic if table == "critic" else self.p.ap)[key]
        if n > limit + spare:
            self.fail(tid, f"{what} sent it back {said_times(n)}; `[{table}] {key}` = {limit} allows {limit + spare}"
                           f"{f' ({why})' if why else ''}: {self.feedback.get(tid, '')[:200]}")

    def critic_gate(self, tid, sha, stale=None, branch=""):
        """`stale`: an approved head whose verdict no longer covers this one (it changed more than a resolution)."""
        p = self.p
        if not p.critic["enabled"]:
            return True
        past = critic.history(p, tid)
        if past and past[-1].get("sha") and not sha.startswith(past[-1]["sha"]) and \
                (past[-1]["step"] in ("revise", "rethink") or (stale or "").startswith(past[-1]["sha"])):
            past = []  # judged an older head: this one needs its own verdict
        if not past or past[-1]["step"] not in critic.DONE:
            if critic.pack(p, tid)[0] is None:
                self.feedback[tid] = ("You finished READY without the critic: write the evidence pack and run "
                                      f"`windvane critic {tid}` (rules step 6) before the check.")
                self.log(f"{tid}: READY without a critic pack; back to the worker")
                self.bounce(tid, "critic")
                return False
            self.log(f"{tid}: critic run (the worker didn't finish it)")
            entry, text = critic.judge(p, tid, self.wd, sha, self.usage,
                                       self.stopper(now() + timedelta(minutes=30)))
            if entry is None:
                self.log(f"{tid}: the critic gave no verdict; on to the check")  # a help, never a gate on its own failure
                return True
            past = [entry]
        last = past[-1]
        score = last.get("score")
        dims = " ".join(f"{k}={v:g}" for k, v in last.get("dims", {}).items() if k != "score")
        self.log(f"{tid}: critic {'-' if score is None else f'{score:g}'}/10{f' ({dims})' if dims else ''} "
                 f"({last['step']}): {last.get('gist', '')}")
        self.watch_scale()
        if last["step"] in ("revise", "rethink"):
            self.feedback[tid] = (f"The critic scored your head {score:g}/10 ({last['step']}). Fix its \"Fix in this "
                                  f"ticket\" points, refresh the pack and run `windvane critic {tid}` again:\n"
                                  f"{last.get('critique', '')}")
            pr = pr_for(p, branch)
            if not critic.comment(p, last, pr):
                self.log(f"{tid}: couldn't post the critique on PR #{pr}")
            self.bounce(tid, "critic")
            return False
        if score is not None:
            self.scores[tid] = score
        return True

    def watch_scale(self):
        """Say once a run when the critic's scale stopped measuring anything (`critic.collapsed`): in the first
        self-run the user only saw it in a status report, after a dozen tickets had passed on it."""
        line = None if self.scale_said else critic.collapsed(read_jsonl(self.p.state / "critic.jsonl"))
        if line:
            self.scale_said = True
            self.log(line)
            push("critic scale collapsed", line.split(": ", 1)[1] + ". The run goes on.", 3,
                 "warning")

    def keep_changes(self, why):
        """With isolate off the clean before a ticket's check or merge, a periodic round or a desk PR runs in the checkout
        itself: whatever is uncommitted there (a ticket's work is pushed by then, so someone at the desk made it) is
        stashed first, never thrown away. False when the stash failed: the caller skips its step instead of cleaning."""
        p, cd = self.p, self.cd
        if cd != p.main_root() or not gout(cd, "status", "--porcelain"):
            return True
        msg = f"windvane kept: {now():%Y-%m-%d %H:%M} ({why})"
        r = git(cd, "stash", "push", "-u", "-m", msg)
        if r.returncode == 0 and not gout(cd, "status", "--porcelain"):
            self.log(f"{why}: stashed the checkout's uncommitted changes as \"{msg}\"")
            push("your changes were stashed", f"Uncommitted changes in your checkout were stashed before "
                 f"{why}: git stash list\n{msg}", 3, "package")
            return True
        err = (r.stderr.strip() or "?").splitlines()[0][:200] if r.returncode else "changes were left after it"
        self.log(f"{why}: skipped: couldn't stash the checkout's uncommitted changes ({err})")
        if not self.stash_failed:
            self.stash_failed = True
            push("checkout not cleaned", f"Couldn't stash the uncommitted changes in your checkout, so "
                 f"{why} was skipped and nothing was deleted: commit or stash them. git said: {err}", 4, "warning")
        return False

    def check_at(self, rev):
        cd = self.cd
        if not clean_detach(self.p, cd, rev):
            return False, f"couldn't check out {rev[:12]}", None
        return check(self.p, cd)

    def review(self, tid, branch, sha, res=None):
        """`res`: (approved head, diff, paths, tree) when the head only resolves a bounce off main: the reviewer
        gets just the resolution."""
        p, cd = self.p, self.cd
        pr = pr_for(p, branch)
        if res:
            return self.verdict(tid, branch, sha, pr, self.resolution_prompt(tid, branch, sha, pr, *res))
        body = net.sh(p.main_root(), "gh", "pr", "view", pr, "--json", "body", "-q", ".body") if pr else \
            gout(cd, "log", "--format=%B", f"{p.upstream}..{sha}")
        stat = gout(cd, "diff", "--stat", f"{p.upstream}...{sha}")
        diff = gout(cd, "diff", f"{p.upstream}...{sha}")
        inline = len(diff.splitlines()) <= p.ap["inline_diff_lines"]
        last = (critic.history(p, tid) or [{}])[-1]
        prompt = "\n\n".join(filter(None, [
            f"Review {tid} at head {sha} (branch {branch}{', PR #' + pr if pr else ''}). Follow the review rules in "
            "your system prompt and end with the REVIEW line.",
            f"## The ticket\n{self.queue.text.get(tid, '')[:6000]}",
            f"## {'PR body' if pr else 'Commit messages'}\n{body[:4000]}",
            f"## The critic's last verdict\n{last.get('critique') or last.get('gist') or last.get('step')}"
            if last else "",
            f"## Diff stat\n{stat[-3000:]}",
            f"## The diff\n```diff\n{diff}\n```" if inline else
            f"The diff is {len(diff.splitlines())} lines: read the files that matter with `git diff {p.upstream}...HEAD "
            "-- <path>`.",
        ]))
        return self.verdict(tid, branch, sha, pr, prompt)

    def resolution_prompt(self, tid, branch, sha, pr, approved, diff, paths, tree):
        p = self.p
        inline = len(diff.splitlines()) <= p.ap["inline_diff_lines"]
        return "\n\n".join(filter(None, [
            f"Review {tid} at head {sha} (branch {branch}{', PR #' + pr if pr else ''}): only its merge resolution. "
            "Follow the review rules in your system prompt and end with the REVIEW line.",
            f"## Only the resolution\nThe critic and the reviewer approved head {approved[:12]}; merged with "
            f"{p.upstream} it then conflicted or failed the check, and the worker merged {p.upstream} in. The diff "
            f"below runs from git's own merge of the two (conflict markers and all) to the new head, so {p.upstream}'s "
            "changes are left out. Check only that it keeps both sides' intent and breaks nothing: the rest was "
            "approved.",
            f"## The ticket\n{self.queue.text.get(tid, '')[:6000]}",
            f"## The resolution ({', '.join(paths)})\n```diff\n{diff}\n```" if inline else
            f"The resolution is {len(diff.splitlines())} lines in {', '.join(paths)}: read it with "
            f"`git diff {tree} HEAD -- <path>`.",
        ]))

    def verdict(self, tid, branch, sha, pr, prompt):
        p, cd = self.p, self.cd
        self.log(f"{tid}: review run")
        while True:
            text, killed, error = self.claude("reviewer", f"{tid}-review", prompt, self.review_rules, cd,
                                              now() + timedelta(hours=1),
                                              ["Bash(git diff *)", "Bash(git show *)", "Bash(git log *)"],
                                              REVIEW_DENY, tid)
            if error and self.crashed(f"{tid}-review", error):
                continue
            m = None if killed or error else last_match(REVIEW_RE, text)
            if m or killed or error or net.online():
                break
            self.log(f"{tid}: the review run ended without a verdict with the network down")
            if not self.wait_online():
                return None, ""
        if killed or error:
            return None, ""
        if not m:
            self.fail(tid, "the review run ended without a verdict")
            return None, ""
        self.log(f"{tid}: review {m.group(1).lower()}: {m.group(3)}")
        if pr:
            net.sh(p.main_root(), "gh", "pr", "comment", pr, "--body",
                   f"**windvane review: {m.group(1)}** at {sha[:7]}\n\n{text.strip()[-6000:]}")
        return m.group(1), text

    def integrate(self, tid, branch, sha):
        with self.merge_lock:  # one merge at a time: each checks the tree main will really have
            return self._integrate(tid, branch, sha)

    def _integrate(self, tid, branch, sha, tries=3):
        """Merge the approved head into main: test the merge, check the merged tree, then merge (PR or push)."""
        p, cd = self.p, self.cd
        title = self.queue.titles.get(tid, tid)
        for _ in range(tries):
            if not self.keep_changes(f"merging {tid}"):  # isolate off: someone edited the checkout during the review
                return self.fail(tid, "couldn't stash the uncommitted changes in your checkout before its merge")
            self.fetch()
            clean_detach(p, cd, p.upstream)
            msg = f"Merge {tid}: {title} (branch {branch})"
            ok, renumbered = self.merge_main(sha, msg)
            if not ok:
                self.feedback[tid] = (f"Your approved head {sha[:12]} conflicts with {p.upstream}: `git merge "
                                      f"{p.upstream}` into your branch, resolve the conflicts keeping both sides' "
                                      "intent, rerun the check, push, and finish READY.")
                self.log(f"{tid}: conflicts with {p.main}; back to the worker")
                self.approve_bounce(tid, sha, "conflict")
                self.conflicted(tid)
                return self.bounce(tid, "conflict")
            merge = gout(cd, "rev-parse", "HEAD")
            if renumbered is not None:
                self.log(f"{tid}: conflicted with {p.main} only in appended entries: {said(renumbered)}; resolved "
                         "without a worker run")
            ok, summary, logf = check(p, cd)
            if not ok:
                self.feedback[tid] = (f"Merged with the current {p.upstream}, your head {sha[:12]} fails the check: "
                                      f"{summary}\n```\n{tail(logf, 40)}\n```\nMerge {p.upstream} into your branch "
                                      "and fix it.")
                self.log(f"{tid}: the merged tree fails the check; back to the worker")
                self.approve_bounce(tid, sha, "red")
                return self.bounce(tid, "check")
            if p.cfg["forge"] == "github":
                pr = pr_for(p, branch)
                if not pr:
                    self.feedback[tid] = "No open PR for your branch: open it (`gh pr create`) and finish READY."
                    return self.bounce(tid)
                if renumbered is not None:  # GitHub merges the branch: it has to hold the resolution
                    if net.git(cd, "push", "-q", f"--force-with-lease=refs/heads/{branch}:{sha}", p.remote,
                               f"{merge}:refs/heads/{branch}").returncode:
                        self.log(f"{tid}: couldn't push the resolution to {branch}; trying again")
                        continue
                    sha = merge  # the head to merge now, and on a retry the one to merge main into
                    net.sh(p.main_root(), "gh", "pr", "comment", pr, "--body",
                           f"**windvane: resolved the conflict with {p.main}** in appended entries: {said(renumbered)}."
                           + OLD_NUMBERS * bool(renumbered))
                res = net.call(["gh", "pr", "merge", pr, "--merge", "--match-head-commit", sha, "--subject", msg],
                               p.main_root())
                merged = res.returncode == 0 or net.sh(p.main_root(), "gh", "pr", "view", pr, "--json", "state",
                                                       "-q", ".state") == "MERGED"
            else:  # by sha, so nothing that moves the ci worktree's HEAD meanwhile can turn it into a no-op
                res = net.git(cd, "push", "-q", p.remote, f"{merge}:refs/heads/{p.main}")
                merged = res.returncode == 0
            if merged:
                return self.merged_ok(tid, branch, sha, renumbered) if self.landed(tid, branch, sha) else None
            self.log(f"{tid}: merge refused ({' '.join((res.stderr or res.stdout).split())[:200]}); trying again")
        self.fail(tid, "the merge was refused three times")

    def merge_main(self, rev, msg):
        """Merge `rev` into main in the ci worktree (left on the merge). A conflict only in appended entries of
        `[[append_only]]` files is resolved and committed with the renumbering in its message. Returns (merged,
        the renumbering: None for a clean merge)."""
        cd, ident = self.cd, author_ident(self.p)
        if git(cd, *ident, "merge", "--no-ff", "-m", msg, rev).returncode == 0:
            return True, None
        done = renumber.resolve(cd, self.p.append_only) if self.p.append_only else None
        if done is not None and git(cd, *ident, "commit", "-q", "--no-verify", "-m", msg, "-m",
                                    "Appended entries kept from both sides: " + said(done)).returncode == 0:
            return True, done
        git(cd, "merge", "--abort")
        return False, None

    def approve_bounce(self, tid, sha, why):
        """An approved head sent back only for main's sake: if the worker just merges main and fixes that, the critic's
        verdict stands and the reviewer sees only the fix (the earlier head keeps it, when this is a second bounce)."""
        ap = self.approved.setdefault(tid, {"sha": sha, "why": why})
        ap["why"] = "red" if "red" in (why, ap["why"]) else why
        self.feedback[tid] += (" If that's all you change, the critic's verdict stands: don't run `windvane critic` "
                               "again; the reviewer sees only your resolution.")

    def landed(self, tid, branch, sha):
        """A merge counts only once main holds the head: a push or `gh pr merge` can report success without it
        (a no-op push, a hook that rejects late), and the branches about to be deleted may be the only copies."""
        p = self.p
        with self.git_lock:  # parallel slots fetch too: one at a time, or one fails on a locked ref
            fetched = any(net.git(p.main_root(), "fetch", "-q", p.remote).returncode == 0 for _ in range(2))
        if git(p.main_root(), "merge-base", "--is-ancestor", sha, p.upstream).returncode == 0:
            return True
        why = f"{p.upstream} doesn't contain {sha[:12]}" if fetched else f"couldn't fetch {p.remote} to confirm it"
        self.fail(tid, f"the merge was reported done but {why}; the work is kept on branch {branch or '(none)'} "
                       f"at {sha[:12]}")
        return False

    def merged_ok(self, tid, branch, sha, renumbered=None):
        """`renumbered`: the merge's renumbering, said in the push: on forge git it's the only place but the log."""
        p = self.p
        if branch:  # only while it still points at the merged head: a later push there is unmerged work
            if net.git(p.main_root(), "push", "-q", f"--force-with-lease={branch}:{sha}", p.remote, "--delete",
                       branch).returncode and net.sh(p.main_root(), "git", "ls-remote", "--heads", p.remote, branch):
                self.log(f"{tid}: kept {p.remote}/{branch}: it moved past the merged head {sha[:7]}")
        self.fetch()
        wd = self.wd
        if not gout(wd, "status", "--porcelain"):
            git(wd, "switch", "-q", "--detach", p.upstream)
            if branch and git(wd, "merge-base", "--is-ancestor", branch, p.upstream).returncode == 0:
                git(wd, "branch", "-q", "-D", branch)  # -d's own test would use the deleted remote branch
        self.merged.append(tid)
        with self.lock:  # this attempt is still running: its time so far counts too
            ran = self.spent.pop(tid, 0) + time.time() - self.began.get(tid, time.time())
        self.minutes.append(round(ran / 60, 2))
        self.fail_streak = 0
        self.feedback.pop(tid, None)
        self.approved.pop(tid, None)
        self.save()
        score = self.scores.get(tid)
        self.log(f"{tid}: merged {sha[:7]} · {self.usage.text()}")
        push(f"Done: {self.name(tid)}", "Built, checked, reviewed and merged."
             + (f" Critic {score:g}/10." if score is not None else "") + f" {self.usage.text()}"
             + (f"\nMerged with {p.main}: {said(renumbered)}." if renumbered else ""), 2, "white_check_mark")
        picks = [q for q in provisional(gout(p.main_root(), "show", f"{p.upstream}:{p.questions_rel}"))
                 if p.norm(q["ticket"]) == tid]
        if picks:
            push(f"Provisional: {self.name(tid)}", "\n".join(f"{q['question']} Built {q['built']}: {q['option']}"
                                                             for q in picks)
                 + f"\nTo change one: windvane answer {tid} <letter>, or tell a Claude session.", 3, "memo")

    def fail(self, tid, why):
        self.skip.add(tid)
        self.failed.append(tid)
        self.approved.pop(tid, None)
        self.log(f"{tid}: FAILED: {why}")
        if net.online():
            self.fail_streak += 1
        else:  # the outage's fault, not the tickets': it mustn't stop the run
            self.log(f"{tid}: failed with the network down: not counted toward two failures in a row")
        push(f"Failed: {self.name(tid)}", f"{why}. The work so far is on its branch; autopilot moved on.", 4, "x")

    # ---------- questions

    def ask(self, tid, branch, gist):
        p = self.p
        self.fetch()
        text = gout(p.main_root(), "show", f"{p.remote}/{branch}:{p.questions_rel}") if branch else ""
        sec = section(text, tid)
        opts, rec, must_wait, q = parts(sec)
        self.skip.add(tid)
        self.fail_streak = 0
        if len(opts) < 2:
            return self.fail(tid, f"BLOCKED, but no question with options under ## Open on {branch}: {gist}")
        self.questions[tid] = {"q": q or gist, "opts": opts, "rec": rec, "must_wait": must_wait, "branch": branch,
                               "asked": None}
        self.log(f"{tid}: question for the user: {q or gist}")
        self.save()  # the loop's next pass sends it (state.json keeps it unsent across a restart)

    def send_unsent(self):
        """Send every stored question not yet sent: held through a quiet time, or left by an earlier run."""
        for tid, q in list(self.questions.items()):
            if q["asked"] is None:
                self.send_question(tid)
                self.save()

    def send_question(self, tid):
        q = self.questions[tid]
        q["asked"] = time.time()
        wait = "" if q["must_wait"] or not q["rec"] else (
            f" No answer in {self.a.answer_wait:g} min: I go with {q['rec']} and mark it to revisit.")
        opts = [q["opts"][k] for k in sorted(q["opts"])]
        sent = phone.ask(self.p.qid(tid), f"{self.name(tid)}: {q['q']}{wait}", opts)
        if sent is None:
            self.log(f"{tid}: no phone; answer at the desk with `windvane answer {tid} <letter>`")
        elif not sent:  # the next pass sends it again; the wait for an answer starts once it's out
            q["asked"] = None
            if not q.get("send_failed"):
                self.log(f"{tid}: the phone push failed; trying again on the next pass")
            q["send_failed"] = True

    def poll_questions(self):
        p = self.p
        phone.pump()
        desk = desk_answers(p)
        for tid, q in list(self.questions.items()):
            if q["asked"] is None:
                continue
            got, letter, text = desk.get(tid), None, None
            if got:
                letter, text = got.get("option"), got.get("text")
            else:
                ph = phone_answer(p, tid)
                if ph.startswith("ANSWER"):
                    letter = ph.split()[1]
                elif ph == "LATER":
                    q["later"] = True
            if letter or text:
                opt = q["opts"].get(letter or "", "")
                self.feedback[tid] = (f"The user answered your question \"{q['q']}\": "
                                      + (f"option {letter} ({opt})" if letter else "")
                                      + (f" They added: {text}" if text else "")
                                      + ". Build that, and move the question to ## Answered with the answer.")
                self.log(f"{tid}: answered {letter or ''} {text or ''}".rstrip())
            elif (not q.get("later") and not q["must_wait"] and q["rec"]
                  and time.time() - q["asked"] > self.a.answer_wait * 60):
                self.feedback[tid] = (f"The user didn't answer your question \"{q['q']}\" within "
                                      f"{self.a.answer_wait:g} minutes: build your recommendation ({q['rec']}), and "
                                      "move the question to ## Provisional with its `Built:` line.")
                self.log(f"{tid}: no answer in {self.a.answer_wait:g} min; building {q['rec']}")
                push(f"No answer: {self.name(tid)}", f"Going with {q['rec']}: {q['opts'].get(q['rec'], '')[:150]}\n"
                     f"To change it later: windvane answer {tid} <letter>.", 3, "hourglass")
            else:
                continue
            del self.questions[tid]
            desk.pop(tid, None)
            self.skip.discard(tid)
        save_json(p.state / "answers.json", desk)
        self.save()

    def quiet_check(self):
        end, err = quiet_now(self.p)
        if err and getattr(self, "quiet_err_sent", None) != err:  # once, not on every pass; again after a fix
            self.log(f"quiet times ignored: {err}; asking as usual")
            push("quiet times ignored", f"{err}. Questions come to the phone as usual until it's "
                 "fixed (`windvane quiet` shows the format).", 3, "warning")
        self.quiet_err_sent = err
        if not end:  # only money, licence and merged-work questions ask in a quiet time
            self.send_unsent()
        if self.was_quiet and not end:
            self.log("quiet time over")
            picks = provisional(gout(self.p.main_root(), "show", f"{self.p.upstream}:{self.p.questions_rel}"))
            if picks:
                push(f"Review: {len(picks)} provisional answer{'s' * (len(picks) > 1)}",
                     "\n".join(f"{q['ticket']}: {q['question']} Built {q['built']}." for q in picks[:8]), 3, "memo")
        self.was_quiet = bool(end)

    # ---------- the loop

    def est(self):
        recent = self.costs[-3:]
        return max(3.0, sum(recent) / len(recent)) if recent else 8.0

    def wait_for_reset(self):
        until = datetime.fromtimestamp(self.usage.five_reset or time.time() + 3600) + timedelta(minutes=2)
        if until >= self.end() - timedelta(minutes=self.a.min_slot):
            self.stop_reason = f"5-hour window at {self.usage.five:g}%, and it resets too close to the deadline"
            return False
        self.log(f"pausing at {self.usage.text()} until the window resets at {hhmm(until)}")
        push("paused", f"{self.usage.text()}. Resuming at {hhmm(until)}.", 2, "pause_button",
             ("Usage=usage", "Stop=stop"))
        self.nap(until)
        self.usage.probe(self.p)
        return True

    def nap(self, until, answers=False):
        """Sleep until `until`, waking within seconds for a stop or the run's deadline (which may have moved).
        Phone messages are handled throughout, but only a wait for `answers` ends early on one: a tap on Usage
        during a usage pause must not cut the pause short."""
        while now() < min(until, self.end()) and not (self.p.state / "STOP").exists():
            time.sleep(NAP)
            if phone.pump() and answers:
                return

    def stop_check(self):
        """Why no new ticket may start (the run winds down), or None."""
        p = self.p
        if (p.state / "STOP").exists():
            now_too = self.stopping_now()
            (p.state / "STOP").unlink()
            return "stopped now: the running sessions were killed" if now_too else "asked to stop"
        if self.halt:
            return self.halt
        if now() >= self.end() - timedelta(minutes=self.a.min_slot):
            return "deadline"
        if self.fail_streak >= 2:
            return "two failed tickets in a row"
        if self.usage.five is None or (time.time() - self.usage.at > 300 and not self.idle):
            self.usage.probe(p)
        if self.usage.five is None:
            return "couldn't read usage (is `claude` logged in?)"
        if self.usage.week is not None and self.usage.week >= self.a.week_cap:
            return f"weekly usage at {self.usage.week:g}% (cap {self.a.week_cap:g}%)"
        return None

    def pause(self, seconds=30):
        """Wait until a slot finishes or `seconds` pass."""
        self.wake.wait(seconds)
        self.wake.clear()

    def conflicted(self, tid):
        """A merge conflict: when the ticket ran beside another, parallel cost a worker run. Every second one drops a
        slot for the rest of the run."""
        with self.lock:
            if tid not in self.beside:
                return
            self.conflicts += 1
            drop = self.conflicts % 2 == 0 and self.limit > 1
            if drop:
                self.limit = max(1, min(self.limit, self.peak) - 1)
        if drop:
            self.log(f"{self.conflicts} conflict bounces between side-by-side tickets: at most {self.limit} at a "
                     "time for the rest of the run")

    def ticket_minutes(self):
        """Recent merged tickets' running minutes, all attempts (10 before any is measured)."""
        recent = self.minutes[-5:]
        return sum(recent) / len(recent) if recent else 10.0

    def serial_fits(self):
        """Why another slot isn't worth opening, or "". Side by side risks conflicts and reading code that's about to
        change, so a slot opens when the 5-hour window is under half the cap (headroom the user chose to spend on
        speed) or when the tickets left wouldn't all finish one at a time before the deadline. A running ticket
        counts as half one: it's under way."""
        if self.usage.five is not None and self.usage.five < self.a.cap / 2:
            return ""
        q, blocked = self.queue, set(self.questions)
        left = sum(t not in self.skip and q.status(t, blocked) in ("wip", "todo", "waiting") for t in q.order)
        m = self.ticket_minutes()
        if (left - len(self.running) / 2) * m > (self.end() - now()).total_seconds() / 60 - self.a.min_slot:
            return ""
        return f"{left} ticket{'s' * (left != 1)} x {m:.0f} min fit before {hhmm(self.end())} one at a time"

    def clash(self, tid, busy):
        """Why `tid` can't start beside the `busy` tickets, or "". Tickets run side by side only when their
        `Touches:` lists don't overlap and they aren't related: a planner's `Related:` line, or a ticket naming a
        running one or a file it changes (it builds on unmerged work). One without Touches may touch anything."""
        q = self.queue
        touches = q.touches
        alone = next((b for b in busy if not touches.get(b)), None)
        if alone:
            return f"{alone} runs alone (no Touches line)"
        if busy and not touches.get(tid):
            return "no Touches line, so it runs alone"
        for b in busy:
            both = shared(touches[tid], touches[b])
            if both:
                return f"overlaps {b} on {', '.join(both)}"
        text = q.text.get(tid, "")
        for b in busy:
            if b in q.related.get(tid, []) or tid in q.related.get(b, []):
                return f"related to {b}"
            if re.search(rf"\b{re.escape(b)}\b", text):
                return f"names {b}, which is running"
            path = next((x for x in sorted(touches[b]) if names(text, x)), None)
            if path:
                return f"names {path}, which {b} changes"
        return ""

    def pick(self, free):
        """(ticket, kind, branches, slot) to start in one of the `free` slots, or (None, why, None, None). Notes in
        `waits` why each ready ticket must wait for the running ones."""
        q = self.queue
        busy = sorted(self.running)
        avoid = set(self.skip) | set(busy)
        self.waits = []
        for t in q.order:
            why = "" if t in avoid or q.status(t, set(self.questions)) not in ("wip", "todo") else self.clash(t, busy)
            if why:
                avoid.add(t)
                self.waits.append(f"{t} waits: {why}")
        while True:
            tid, *rest = q.pick(avoid, set(self.questions))
            if tid is None:
                return None, rest[0], None, None
            own = next((i for i, o in self.owner.items() if o == tid), None)
            slot = own if own is not None else next((i for i in free if self.owner[i] is None), None)
            if slot is not None and slot in free:
                return tid, rest[0], rest[1], slot
            avoid.add(tid)

    def prepare(self, slot):
        """Both of a slot's worktrees. A failed setup halts the run: every check in that tree would fail, and each
        ticket would bounce back to its worker with a red check it can't fix."""
        try:
            for d in self.slot_dirs(slot):
                ensure_worktree(self.p, d)
            return True
        except SetupFailed as e:
            last = e.lines[-1][:200] if e.lines else "no output"
            self.halt = f"{e}: {last}"
            self.log(str(e))
            log(self.p, "\n".join(["its last lines:", *e.lines]), echo=False)  # the terminal showed them already
            return False

    def rules_ok(self):
        """The project's `[permissions]` rules all parse. The CLI drops a rule it can't read without a word, so a
        session would run without the tool its project granted: the run stops at its start instead."""
        bad = self.p.perm_problems
        if bad:
            more = f"; and {len(bad) - 2} more (windvane doctor lists them)" if len(bad) > 2 else ""
            self.halt = f"{len(bad)} bad permission rule{'s' * (len(bad) > 1)} in config.toml: {'; '.join(bad[:2])}{more}"
            self.log(self.halt)
        return not bad

    def idle_note(self, notes):
        """Log why a slot sits idle, once per change rather than every pass of the loop."""
        for n in notes:
            if n not in self.said:
                self.log(n)
        self.said = set(notes)

    def start(self, slot, tid, kind, branches):
        if not self.prepare(slot):
            return
        with self.lock:
            if self.running:
                self.beside |= {tid, *self.running}
            self.running[tid] = slot
            self.peak = max(self.peak, len(self.running))
            self.began[tid] = time.time()
        self.save()  # `windvane status` shows the running tickets
        t = threading.Thread(target=self.slot_run, args=(slot, tid, kind, branches), daemon=True)
        self.threads.append(t)
        t.start()

    def slot_run(self, slot, tid, kind, branches):
        self.tl.wd, self.tl.cd = self.slot_dirs(slot)
        try:
            self.work(tid, kind, branches)
        except Exception as e:  # one ticket's driver error must not take the other slots down
            self.log(f"{tid}: driver error: {e!r}")
            self.fail(tid, f"driver error: {e}")
        finally:
            wd = self.wd
            if gout(wd, "status", "--porcelain"):
                self.owner[slot] = tid  # its leftovers stay here: only this ticket resumes in this slot
            else:
                self.owner[slot] = None
                if gout(wd, "rev-parse", "--abbrev-ref", "HEAD") not in ("HEAD", ""):
                    git(wd, "switch", "-q", "--detach")  # free the branch for whichever slot takes it next
            with self.lock:
                self.running.pop(tid, None)
                ran = time.time() - self.began.pop(tid, time.time())
                if tid not in self.merged:  # waiting on an answer or in the queue after a bounce doesn't count
                    self.spent[tid] = self.spent.get(tid, 0) + ran
                self.beside.discard(tid)  # a later attempt alone doesn't count its conflicts against parallel
            self.save()
            self.wake.set()

    def loop(self):
        p = self.p
        while True:
            self.quiet_check()
            why = self.stop_check()
            if why:
                self.stop_reason = why
                return
            if not self.running:
                self.self_update()
            free = [i for i in range(self.n) if i not in self.running.values()]
            if not free or len(self.running) >= self.limit:
                self.pause()
                continue
            k = len(self.running) + 1
            over = self.usage.rejected or self.usage.five + self.est() * k > self.a.cap
            if over and not self.running:
                if not self.wait_for_reset():
                    return
                continue
            due = self.periodic_due()
            if due and self.running:  # it runs on a quiet tree: no new ticket until the running ones finish
                self.idle_note([f"{due['name']} is due: waiting for {', '.join(sorted(self.running))} to finish"])
                self.pause()
                continue
            if due:
                self.periodic(due)
                continue
            desk = self.desk_due()
            if desk and self.running:  # checked and merged between tickets, like a periodic session
                self.idle_note([f"desk PR #{desk[0]['number']} waits for {', '.join(sorted(self.running))} to finish"])
                self.pause()
                continue
            if desk:
                self.desk_one(desk[0])
                continue
            self.fetch()
            self.poll_questions()
            self.queue = Queue(p)
            tid, kind, branches, slot = self.pick(free)
            if tid is not None and over:  # no more slots until the window allows; the running ones go on
                why = "usage limit reached" if self.usage.rejected else (
                    f"usage estimate ({self.usage.five:g}% + {k} x {self.est():.0f}% > cap {self.a.cap:g}%)")
                self.idle_note([f"no {nth(k)} slot: {why}"])
                self.pause(60)
                continue
            no_need = tid is not None and self.running and self.serial_fits()
            if no_need:
                self.idle_note([f"no {nth(k)} slot: {no_need}"])
                self.pause()
                continue
            if tid is None:
                if self.running:
                    self.idle_note(self.waits)
                    self.pause()
                    continue
                if self.questions:
                    if not self.idle:
                        self.log(f"waiting for answers on {', '.join(self.questions)}")
                        self.idle = True
                    self.nap(now() + timedelta(minutes=1), answers=True)
                    continue
                if self.a.replan and self.plans < self.a.replan:
                    self.plans += 1
                    n = plan(p, self.a.goal or p.cfg["goal"], True, self.usage,
                             self.stopper(now() + timedelta(minutes=40)), merge=True)
                    if n:
                        continue
                self.stop_reason = kind
                return
            self.idle, self.said = False, set()
            if self.n > 1:
                beside = f", beside {', '.join(sorted(self.running))}" if self.running else ""
                self.log(f"{tid}: slot {slot}{beside}")
            self.start(slot, tid, kind, branches)
            if self.n == 1:
                self.threads[-1].join()

    def self_update(self):
        """Pulls the windvane install between tickets (nothing running) and, when the driver's own code changed,
        restarts it in place with the deadline as it now is. Each reason to leave the install alone is logged once."""
        if self.home is None or (time.time() - self.pulled[0] < PULL_EVERY and self.pulled[1] == len(self.merged)):
            return
        self.pulled = (time.time(), len(self.merged))
        said = []
        again = selfupdate.pull(self.home, said)
        for line in said:
            if line not in self.update_said:
                self.log(line)
                self.update_said.append(line)
        if again:
            names = lambda ids: ", ".join(ids) or "none"
            self.log(f"windvane restarting with its new code (so far merged: {names(self.merged)}; "
                     f"failed: {names(self.failed)})")
            self.hand_over()
            self.lockfile.close()  # the restarted driver takes it again
            os.environ["WINDVANE_RESTARTED"] = "1"  # no second "windvane started" push; it reads restart.json
            cmd = [*exe(), *selfupdate.restart_args(self.argv, self.end())]
            self.execv(cmd[0], cmd)

    def hand_over(self):
        """Leaves the run's memory for the driver that replaces this one (`carry_over`)."""
        self.save()
        save_json(self.p.state / "restart.json", {k: sorted(v) if isinstance(v, set) else v
                                                  for k, v in vars(self).items() if k in CARRIED})

    def carry_over(self, restarted):
        """A restart (a self-update, or `supervise` after a crash) goes on with the run it replaced: the same tickets set aside or failed, the same
        replan budget, and the same merges in the final summary. Any other start begins afresh."""
        f = self.p.state / "restart.json"
        if restarted:
            for k, v in load_json(f, {}).items():
                if k in CARRIED:
                    setattr(self, k, set(v) if k == "skip" else v)
        f.unlink(missing_ok=True)

    def take_lock(self):
        """One driver per project; `locked` reads this lock to tell the phone a run is on."""
        self.p.state.mkdir(parents=True, exist_ok=True)
        self.lockfile = open(self.p.state / "lock", "w")
        try:
            fcntl.flock(self.lockfile, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.lockfile.close()
            raise SystemExit("windvane: the autopilot is already running for this project")

    def run(self, deadline):
        """Works tickets until a stop; False when it couldn't start (a failed setup)."""
        p = self.p
        self.take_lock()
        phone.start_run(p)
        restarted = os.environ.pop("WINDVANE_RESTARTED", None)
        if not restarted:  # a stop asked for while the driver restarted still holds
            (p.state / "STOP").unlink(missing_ok=True)
        (p.state / "DEADLINE").unlink(missing_ok=True)  # an earlier run's
        self.deadline = deadline
        # Network calls wait out an outage (net.py) with this run's stop rule; the probe tries the remote's host too.
        net.hosts = list(filter(None, [net.remote_host(gout(p.main_root(), "remote", "get-url", p.remote))]))
        net.waiter = self.wait_online
        net.learn()
        self.log(f"autopilot {'restarted' if restarted else 'started'}, until {hhmm(deadline)} (5h cap "
                 f"{self.a.cap:g}%, week cap {self.a.week_cap:g}%"
                 + (f", up to {self.n} tickets at a time" if self.n > 1 else "") + ")")
        ready, crashed = False, None
        try:
            ready = self.rules_ok() and self.prepare(0)  # before any session or push: a bad rule or a failed setup
            # ends the run with a single push
            self.carry_over(restarted)
            if not CHECKOUT:
                self.newer = selfupdate.newer_release(selfupdate.version())
                if self.newer:
                    self.log(f"windvane {self.newer} is out: pipx upgrade windvane")
            if ready and not restarted:
                self.usage.probe(p)
                push("windvane started", f"Until {hhmm(deadline)}. {self.usage.text()}.", 2, "rocket",
                     ("Status=status", "Stop=stop"))
            else:
                self.stop_reason = self.halt
            if ready:
                self.loop()
        except Exception as e:
            self.stop_reason = f"driver error: {e}"
            crashed = traceback.format_exception_only(e)[-1].strip()
            raise
        finally:
            for t in self.threads:  # the running tickets finish (their runs stop at the deadline at the latest)
                t.join()
            if not quiet_now(p)[0]:
                self.send_unsent()  # a question from a ticket that finished after the loop's last pass
            summary = self.summary()
            self.log(summary.replace("\n", " "))
            if crashed and os.environ.get(SUPERVISED):  # `supervise` restarts it, or pushes why it doesn't
                self.hand_over()
                save_json(p.state / "crash.json", {"error": crashed, "summary": summary.split("\n", 1)[1]})
            else:
                push("windvane stopped", summary, 4 if self.failed or self.questions or self.halt else 3,
                     "checkered_flag", ("Usage=usage",))
            self.save()
            self.lockfile.close()
        return ready


    def summary(self):
        """The run's last push: what merged and what still needs the user (questions, provisional answers, merges
        the critic only let through because its revisions ran out)."""
        p = self.p
        names = lambda ids: "; ".join(map(self.name, ids)) or "none"
        crit = ", ".join(f"{t} {s:g}" for t, s in self.scores.items())
        under = [t for t in self.merged if (h := critic.history(p, t)) and h[-1].get("step") == "accept"]
        picks = [(q["ticket"], q["question"], f"{q['built'] or '?'}: {q['option'][:60]}" if q["option"] else q["built"] or "?")
                 for q in provisional(gout(p.main_root(), "show", f"{p.upstream}:{p.questions_rel}"))]
        side = (f"Side by side: up to {self.peak} at once, {self.conflicts} conflict bounce"
                f"{'s' * (self.conflicts != 1)}.\n" if self.n > 1 else "")
        return (f"Why: {self.stop_reason}.\nMerged: {names(self.merged)}.\n"
                + (f"Merged under the critic's bar (out of revisions): {names(under)}.\n" if under else "")
                + (f"Desk PRs merged: {', '.join('#' + n for n in self.desk_merged)}.\n" if self.desk_merged else "")
                + (f"Critic (/10): {crit}.\n" if crit else "") + side
                + (f"Held for you (parked in QUEUE.md): {names(self.held)}.\n" if self.held else "")
                + f"Waiting on you: {names(self.questions)}.\n"
                + (f"Provisional answers to check: {'; '.join(f'{q[0]} {q[1]} (built {q[2]})' for q in picks[:6])}"
                   + (f" and {len(picks) - 6} more" if len(picks) > 6 else "") + ". "
                   "To change one: windvane answer <ID> <letter>.\n" if picks else "")
                + f"Failed: {names(self.failed)}.\n"
                + (f"{self.usage.text()}." if self.usage.five is not None else "")
                + (f"\nwindvane {self.newer} is out: pipx upgrade windvane." if self.newer else ""))


def supervise(p, argv, deadline, start=None, sleep=time.sleep):
    """`run --detach`'s loop around the driver (`windvane <argv>`): a driver that dies of an exception is restarted
    with the run's deadline as it now is and what it had done (`carry_over`), up to RESTARTS times an hour; each
    restart is logged and pushed with the error. A stop, the deadline, an empty queue or a failed setup end the run
    as they end the driver. A driver killed by the system (OOM) is only pushed: its sessions, in process groups of
    their own, may still be at work in the worktrees a restart would resume. Returns the driver's last exit code."""
    start = start or (lambda args, env: subprocess.run([*exe(), *args], env=env).returncode)
    env = {**os.environ, SUPERVISED: "1"}
    env.pop("WINDVANE_RESTARTED", None)
    crashes, args = [], list(argv)
    while True:
        crash = p.state / "crash.json"
        crash.unlink(missing_ok=True)
        rc = start(args, env)
        info = load_json(crash, None)
        if info is None:
            if rc < 0 and -rc not in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):  # not someone's stop: the system's
                why = f"the driver was killed ({signal.Signals(-rc).name})"
                log(p, f"{why}; not restarted: its sessions may still be running in the worktrees")
                push("windvane stopped", f"Why: {why}. Not restarted: its sessions may still be running "
                     "in the worktrees.", 4, "rotating_light", ("Status=status",))
            return rc  # its own end (a stop, the deadline, the queue, a failed setup), or a kill
        why = info["error"]
        try:
            deadline = datetime.fromisoformat((p.state / "DEADLINE").read_text().strip())  # moved meanwhile
        except (OSError, ValueError):
            pass
        crashes = [t for t in crashes if t > time.time() - 3600] + [time.time()]
        asked = lambda: "asked to stop" if (p.state / "STOP").exists() else None
        stop = (f"crashed {len(crashes)} times in an hour" if len(crashes) > RESTARTS
                else asked() or ("deadline" if now() >= deadline else None))
        if not stop:
            log(p, f"driver crashed ({why}); restart {len(crashes)} of {RESTARTS} this hour")
            push("windvane restarted", f"The driver crashed: {why}. Restart {len(crashes)} of "
                 f"{RESTARTS} this hour; it goes on until {hhmm(deadline)}.", 3, "repeat", ("Status=status",))
            sleep(RESTART_PAUSE)
            stop = asked()
        if stop:
            log(p, f"driver crashed ({why}); not restarted: {stop}")
            push("windvane stopped", f"Why: the driver crashed ({why}); not restarted: {stop}.\n"
                 + info["summary"], 4, "rotating_light", ("Usage=usage",))
            return rc
        args = selfupdate.restart_args(args, deadline)
        env["WINDVANE_RESTARTED"] = "1"


def locked(p):
    """Whether a driver holds this project's lock, foreground or detached: the lock goes when its process does."""
    f = p.state / "lock"
    if not f.exists():
        return False
    with open(f) as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except OSError:
            return True
    return False


# ---------- planning

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


def author_ident(p):
    """`-c user.name/email` for the driver's own commits when the config names an author."""
    a = p.cfg["author"]
    return (["-c", f"user.name={a.split('<')[0].strip()}", "-c", f"user.email={a.split('<')[1].rstrip('>').strip()}"]
            if "<" in a else [])


def land(p, cd, branch, title, body, commit=True):
    """Put the driver's own change in `cd` (staged, or committed when `commit` is off) on main: through a merged PR on
    github, a push (replayed onto a main that moved) on plain git. Returns (landed, where it is)."""
    if commit:
        git(cd, *author_ident(p), "commit", "-q", "-m", title, "-m", body[:12000])  # a periodic reply's 80 lines
    if p.cfg["forge"] == "github":
        net.git(cd, "push", "-q", p.remote, f"HEAD:refs/heads/{branch}")
        pr = net.sh(cd, "gh", "pr", "create", "--head", branch, "--base", p.main, "--title", title, "--body", body)
        pr_n = pr.rstrip("/").rsplit("/", 1)[-1]
        merged = pr_n.isdigit() and (
            net.call(["gh", "pr", "merge", pr_n, "--merge"], p.main_root()).returncode
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
            net.git(cd, "push", "-q", p.remote, f"HEAD:refs/heads/{branch}")
            return False, branch
    return True, p.main


def plan(p, goal, use_followups=True, usage=None, stop_if=None, merge=False, max_new=6):
    """A planner run writes tickets for `goal` (and the critic's follow-ups) on a branch the driver commits;
    `merge` merges it at once. Returns how many tickets it merged (with `merge`) or wrote."""
    p.state.mkdir(parents=True, exist_ok=True)
    with open(p.state / "plan.lock", "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            log(p, "plan: another plan is running")
            return 0
        return _plan(p, goal, use_followups, usage, stop_if, merge, max_new)


def pending_ids(p):
    """Ticket ids on unmerged remote `plan-*` and `periodic-*` branches: tickets whose PR is still open have claimed
    them."""
    ids = set()
    branches = gout(p.main_root(), "branch", "-r", "--no-merged", p.upstream, "--format=%(refname:short)")
    for b in branches.split():
        if b.startswith((f"{p.remote}/plan-", f"{p.remote}/periodic-")):
            names = gout(p.main_root(), "ls-tree", "--name-only", b, p.tickets_rel + "/").split()
            ids |= {p.norm(Path(n).stem) for n in names if n.endswith(".md")} - {None}
    return ids


def unmerged(p, n, where):
    """A plan that couldn't merge counts as none (so `run --replan` doesn't take it for new work), but the user
    hears where it waits: it was a paid planner session."""
    clean_detach(p, p.plan_dir, p.upstream)
    log(p, f"plan: {n} tickets on {where}, not merged; merge it by hand")
    push("plan not merged", f"{n} tickets wait on {where}: merge it by hand.", 4, "warning")
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
            "Bash(gh *)", "NotebookEdit"]
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
    branch = f"plan-{now():%Y%m%d-%H%M}"
    git(cd, "add", "-A", ".windvane")
    git(cd, *author_ident(p), "commit", "-q", "-m", f"Plan: {n} tickets ({m.group(3)[:60]})", "-m",
        text.strip()[-3000:])
    if notes:
        from .util import append_jsonl
        append_jsonl(p.state / "critic.jsonl", {"ticket": "PLAN", "step": "plan", "notes": len(notes)})
    if merge:
        ok, where = land(p, cd, branch, f"Plan: {n} new tickets", text.strip()[-6000:], commit=False)
        if not ok:
            return unmerged(p, n, where)
        log(p, f"plan: {n} tickets on {where} (merged)")
    elif p.cfg["forge"] == "github":
        net.git(cd, "push", "-q", p.remote, f"HEAD:refs/heads/{branch}")
        pr = net.sh(cd, "gh", "pr", "create", "--head", branch, "--base", p.main, "--title",
                    f"Plan: {n} new tickets", "--body", text.strip()[-6000:])
        log(p, f"plan: {n} tickets on {pr or branch}")
    else:
        res = net.git(cd, "push", "-q", p.remote, f"HEAD:refs/heads/{branch}")
        if res.returncode:
            clean_detach(p, cd, p.upstream)
            log(p, f"plan: push to {branch} refused ({' '.join(res.stderr.split())[:200]}); plan dropped")
            return 0
        log(p, f"plan: {n} tickets pushed to {branch}")
    push(f"planned {n} tickets", m.group(3)[:200] or "See the queue.", 3, "memo")
    clean_detach(p, cd, p.upstream)
    return n
