"""The autopilot driver: works a project's tickets unattended until a deadline, one fresh headless Claude session
per ticket, in its own worktree so the user's checkout is never touched.

Per ticket: a worker run builds it (and has the critic judge it before the check), then the driver itself runs the
project's check on the exact head (reusing the worker's pass for the same tree), a fresh read-only reviewer reads the
change, and the driver merges it after checking the merged tree too. Anything that fails a stage goes back to the
ticket's next worker run as written feedback. Design questions go to the user's phone; in their quiet times runs
build their recommendation and record it instead.

Stops at the deadline, `windvane stop`, two failed tickets in a row, an empty queue, the weekly cap, or sessions that
keep dying across tickets (API errors, crashes; each is retried after a growing pause first; one ticket's alone set
it aside). A network outage stops nothing: it is
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
from datetime import datetime, timedelta
from pathlib import Path

from . import actions, critic, net, phone, renumber, selfupdate
from .renumber import OLD_NUMBERS, said
from .desk import Desk
from .periodic import Periodic
from .config import CHECKOUT, DIR, HOME, exe, slots
from .jobs import check, tail
from .questions import (answered, desk_answers, give_answer, mode, parts, phone_answer, provisional, quiet_now, section)
from .queue import Queue
from .runner import Usage, over_caps, run_claude, sweep_nested
from .util import append_jsonl, git, gout, hhmm, last_match, load_json, log, now, push, read_jsonl, run_limited, save_json

RESULT_RE = re.compile(r"RESULT: (READY|PARTIAL|BLOCKED|FAILED) (\S+)[ \t]*(.*)")
REVIEW_RE = re.compile(r"REVIEW: (APPROVE|CHANGES) (\S+)[ \t]*(.*)")
PLAN_RE = re.compile(r"PLAN: (?:new=(\d+) noted=(\d+)|NONE)[ \t]*(.*)")
CARRIED = ("periodic_at", "groups_seen", "held", "skip", "aside", "stopped", "merged", "failed", "partials", "reviews", "bounces", "scores", "plans", "fail_streak", "peak",
           "conflicts", "spent", "update_said", "desk_merged", "suspect", "ticket_crashes", "oks", "crashes")  # the run's own memory, handed to the driver a restart starts
# Each kind of send-back spends its own budget: (who sent it back, config table, key, send-backs allowed over the key
# and why). The critic gives its last verdict after `revisions` itself; the spare one covers a READY without a pack.
BUDGETS = {"review": ("a missing PR", "autopilot", "review_rounds", 0, ""),
           "critic": ("the critic", "critic", "revisions", 1, "one more for a READY without the evidence pack"),
           "check": ("the check", "autopilot", "check_retries", 0, ""),
           "conflict": ("a merge conflict", "autopilot", "conflict_retries", 0, "")}
PULL_EVERY = 600  # seconds between looks for a newer windvane while no ticket merges (a wait for answers)
NAP = 5  # seconds between a waiting driver's looks at STOP and the deadline
STASH_WAIT = 30  # seconds between tries to stash the checkout's changes before an approved ticket's check or merge
RESTARTS = 3  # crashes in an hour a detached run's driver is restarted from; the next one ends the run
RESTART_PAUSE = 30  # seconds before such a restart, so a driver that dies at once doesn't spin
# The environment variable set for a driver under `supervise`: it hands a crash over instead of pushing it
SUPERVISED = "WINDVANE_SUPERVISED"
# Errors a ticket brings on itself, as (text in the error, lowercase; what to do), for its set-aside push
SELF_INFLICTED = (("prompt is too long", "Split the ticket, or trim what it reads."),
                  ("context limit", "Split the ticket, or trim what it reads."),
                  ("request too large", "Split the ticket, or trim what it reads."))


LOCK_SCREEN = 90  # characters of a push's body a phone's lock-screen preview shows


def lead(head, free, tail="", whole=True, log=None):
    """A push's first line: `head`, the free text `free` (an error, a reason, an option) cut with … so the line fits
    LOCK_SCREEN, then `tail`, which carries the next step. The cut-off rest follows on the next line (`whole`), so
    the point is on the lock screen and nothing is lost once the push is opened. A `head` and `tail` too long on their
    own can't fit: the push still goes out, and `log` (stderr without one) gets a line so it doesn't pass unseen."""
    if len(head) + len(tail) > LOCK_SCREEN:
        msg = f"push's first line over {LOCK_SCREEN} characters before its free text: {head + '…' + tail!r}"
        (log or (lambda m: print(m, file=sys.stderr)))(msg)
    free = " ".join(str(free).split())
    free = free.rstrip(".") if tail.startswith(".") else free
    room = LOCK_SCREEN - len(head) - len(tail)
    if len(free) <= room:
        return head + free + tail
    cut = free[:max(room - 1, 0)]
    if " " in cut[-15:]:  # at a word's end, not inside one
        cut = cut.rsplit(" ", 1)[0]
    if cut.count("`") % 2:  # nor inside a command, which the user may copy
        cut = cut[:cut.rindex("`")]
    stop = "." * tail.startswith(".")  # "…" ends the line's sentence; the cut-off rest gets the full stop
    rest = f"\n…{free[len(cut):].lstrip()}{stop}" if whole else ""
    return head + cut.rstrip(" ,;:(") + "…" + tail.removeprefix(stop) + rest


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
            f"Bash(git switch {m})", f"Bash(git checkout {m})", "Bash(git worktree *)",
            *actions.DENY]
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
                                       keep=15)
        if code or why:  # a half-made tree fails every check: remove it, so the next start sets it up afresh
            git(root, "worktree", "remove", "--force", str(path))
            shutil.rmtree(path, ignore_errors=True)
            git(root, "worktree", "prune")
            how = (f"timed out after {limit:g} min" if why == TIMED_OUT else f"cut short ({why})" if why
                   else f"failed (exit {code})")
            cmd, where = f"`{clip(p.cfg['setup'], 60)}`", short_path(p, path)
            raise SetupFailed(f"setup {cmd} {how} in {where}", lines, why, how, f"{cmd} in {where}")


def clip(text, n):
    """`text` cut to `n` characters with "…": a long chained `setup` would fill the phone's first line."""
    return text if len(text) <= n else text[:n - 1].rstrip() + "…"


TIMED_OUT = "out of time"  # run_limited's reason when `until` passes


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
        self.aside = {}  # ticket -> why the run went on without it, for the summary's "Set aside" line
        self.stopped = []  # tickets whose worker the deadline cut off: not set aside, the next run resumes them
        self.partials, self.reviews, self.scores, self.bounces = {}, {}, {}, {}  # bounces: ticket -> {kind: n}
        self.fail_streak, self.plans, self.was_quiet, self.idle = 0, 0, False, False
        self.crashes, self.halt, self.unfinished = 0, None, set()  # sessions dead in a row; why the run must stop
        # Sessions dead per ticket: ticket -> [crashes, self.oks at its first], and the sessions that ended well, so a
        # ticket whose own sessions keep dying can be told from a broken API. `suspect`: [ticket, error] set aside
        # on its own crashes alone, until another ticket's session shows the API works.
        self.ticket_crashes, self.oks, self.suspect = {}, 0, None
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
        self.stash_failed = set()  # what waited, or was skipped, on a failed stash of the checkout: pushed once a run
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

    def claude(self, role, label, prompt, system, cwd, limit, allow=(), deny=(), ticket="", env=None):
        text, killed, error = run_claude(self.p, role, label, prompt, system, cwd, allow, deny, self.usage,
                                         self.stopper(limit), ticket,
                                         (min(limit, self.end()), self.a.cap, self.a.week_cap), env)
        if killed:
            self.log(f"  stopped {label}: {killed} ({self.usage.text()})")
        elif not error:
            self.session_ok(ticket)
        return text, killed, error

    def session_ok(self, tid):
        """A session ended well: the API works, so a ticket set aside on its own crashes alone stays aside, said now."""
        with self.lock:
            self.crashes, self.oks = 0, self.oks + 1
            self.ticket_crashes.pop(tid, None)
            suspect, self.suspect = self.suspect, None
        if suspect:
            self.set_aside(*suspect)

    def wait_online(self):
        """Wait out a network outage; False when a stop or the deadline ended the wait."""
        return net.wait(self.p, self.end, lambda: (self.p.state / "STOP").exists(), self.sleep)

    def crashed(self, label, error, tid=""):
        """A session died for a reason outside its ticket (API overload, network, not logged in): pause, longer each
        time, and return True to run it again. Nothing is pushed for a retry. With the network down it waits for it
        instead, and the rerun spends no retry.
        When one ticket's sessions have died more than `retries` times, it is set aside for the run if other
        sessions ended well since its first crash; with none since, it is set aside on trial while other tickets
        can run, and the next session decides: a good one keeps it aside, a dead one stops the run. Sessions dying
        across tickets stop the run instead of failing ticket after ticket on a broken API."""
        if not net.online():
            self.log(f"  {label} died ({error}) with the network down; rerun when it's back, no retry spent")
            return self.wait_online()
        n = self.p.ap["retries"]
        with self.lock:
            self.crashes += 1
            own = self.ticket_crashes.setdefault(tid, [0, self.oks]) if tid else None
            if own:
                own[0] += 1
            if own and own[0] > n and self.oks > own[1]:
                self.crashes = 0  # the API works: this ticket is what fails
                del self.ticket_crashes[tid]
                aside = "now"
            elif own and own[0] > n and not self.suspect and self.others_left(tid):
                self.suspect = [tid, error]
                aside = "on trial"
            else:
                aside = None
        if aside == "now":
            self.set_aside(tid, error)
            return False
        if aside:
            self.skip.add(tid)
            self.aside[tid] = f"on trial: its sessions died ({error})"
            self.log(f"  {label}: {own[0]} sessions died ({error}); set aside while another ticket runs: if its "
                     "session dies too, the run stops")
            return False
        wait = self.p.ap["retry_minutes"] * 2 ** (max(self.crashes, own[0] if own else 0) - 1)
        if self.crashes > n:
            self.halt = f"{self.crashes} sessions in a row died ({error})"
        elif now() + timedelta(minutes=wait) >= self.end() - timedelta(minutes=self.a.min_slot):
            self.halt = f"a session died ({error}), and there's no time left to retry it"
        if self.halt:
            self.log(f"  {label}: {self.halt}")
            return False
        self.log(f"  {label} died ({error}); retry {own[0] if own else self.crashes}/{n} in {wait:g} min")
        return self.backoff(wait)

    def backoff(self, minutes):
        """A retry's backoff; False when a stop or the deadline came first."""
        for _ in range(round(minutes)):
            if (self.p.state / "STOP").exists() or now() >= self.end():
                return False
            self.sleep(60)
        return True

    def others_left(self, tid):
        """Whether a ticket other than `tid` is running or could start: the session that tells a ticket's own
        failure from a broken API."""
        q, blocked = self.queue, set(self.questions)
        return any(t != tid for t in self.running) or any(
            t != tid and t not in self.skip and q.status(t, blocked) in ("wip", "todo") for t in q.order)

    def set_aside(self, tid, error):
        """A ticket whose own sessions keep dying while others' don't: the rest of the run goes on without it."""
        self.skip.add(tid)
        self.aside[tid] = f"its sessions kept dying: {error}"
        self.log(f"{tid}: set aside: its sessions keep dying ({error}) while other sessions work")
        died = f"Sessions died {self.p.ap['retries'] + 1}×: "
        why = "Other sessions worked, so it's this ticket's doing."
        hit = next(((text, fix) for text, fix in SELF_INFLICTED if text in error.lower()), None)
        if hit:  # the fix leads: the error's own words rarely say what to do
            body = (lead(died, hit[0], f". {hit[1]}", log=self.log)
                    + f"\nThe error: {' '.join(error.split())}\nBranch kept, run goes on. {why}")
        else:
            body = lead(died, error, ". Branch kept, run goes on.") + f"\n{why}"
        push(f"Set aside: {self.name(tid)}", body, 4, "warning")

    def set_aside_limit(self, tid, limit, fix):
        """A worker run stopped at a limit of its own (the time box, max turns or budget): the ticket is too big for
        one run as it stands, so the run goes on without it and the phone says which limit and what to change."""
        self.skip.add(tid)
        self.aside[tid] = f"hit {limit}"
        self.log(f"{tid}: the run hit {limit}; set aside")
        push(f"Set aside: {self.name(tid)}", lead("Hit ", limit, f". {fix} Branch kept.", log=self.log), 3, "hourglass")

    def critic_retry(self, tid):
        """`critic.judge`'s retry for the driver's own critic run: the same backoff as any dead session, but spent
        retries only make the critic unavailable for this head (it never gates a ticket on its own failure), and
        they count toward neither the ticket's crashes nor the run's."""
        tries = []

        def retry(error):
            if not net.online():
                self.log(f"  {tid}-critic died ({error}) with the network down; rerun when it's back")
                return self.wait_online()
            tries.append(error)
            n, wait = self.p.ap["retries"], self.p.ap["retry_minutes"] * 2 ** (len(tries) - 1)
            if len(tries) > n or now() + timedelta(minutes=wait) >= self.end() - timedelta(minutes=self.a.min_slot):
                return False
            self.log(f"  {tid}-critic died ({error}); retry {len(tries)}/{n} in {wait:g} min")
            return self.backoff(wait)
        return retry

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
        git_env, skip_line = actions.worker_git(p, wd)
        session_env = {**git_env, **actions.gh_guard(p, wd)}
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
            skip_line,
        ]))
        self.log(f"{tid}: worker run ({kind}{', ' + ' '.join(branches) if branches else ''})")
        allow, deny = worker_perms(p)
        text, killed, error = self.claude("worker", tid, prompt, self.worker_rules, wd, limit, allow, deny, tid,
                                          session_env)
        if error:
            self.crashed(tid, error, tid)  # the loop picks the ticket again, with its branch as it now is
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
                self.set_aside_limit(tid, f"the {self.a.ticket_hours:g}-hour limit for one run",
                                     "Split it, or raise ticket_hours.")
            elif killed == "time":  # the run ends in minutes: its summary says so, and the next run resumes it
                self.stopped += [tid] * (tid not in self.stopped)
                self.log(f"{tid}: the worker run was stopped at the deadline")
            elif killed == "max turns":  # out of its own turns or budget: the ticket's doing, like the time box
                self.set_aside_limit(tid, "its max turns", "Raise max turns in .claude/ settings or split it.")
            elif killed.startswith("max ") or killed == "dollar budget":  # windvane sets no --max-budget-usd: only
                self.set_aside_limit(tid, f"its {killed}", "Split the ticket.")  # splitting is left to the user
            return
        m = last_match(RESULT_RE, text)
        branch = gout(self.wd, "rev-parse", "--abbrev-ref", "HEAD")
        branch = "" if branch == "HEAD" else branch
        if not m or self.p.norm(m.group(2)) != tid:
            return self.unfinished_run(tid, branch)
        self.unfinished.discard(tid)
        self.stopped = [t for t in self.stopped if t != tid]  # a deadline moved later let it finish after all
        status, rest = m.group(1), m.group(3).strip()
        if status == "PARTIAL":
            self.partials[tid] = self.partials.get(tid, 0) + 1
            self.log(f"{tid}: partial ({self.partials[tid]}): {rest}")
            if self.partials[tid] >= self.p.ap["partials"]:
                self.skip.add(tid)
                self.aside[tid] = f"{self.partials[tid]} runs in a row ran out of time"
                push(f"Set aside: {self.name(tid)}", f"{self.partials[tid]} runs in a row ran out of time. Split it. "
                     f"Branch kept.\nLeft: {rest}", 3, "hourglass")
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
        if not self.clean_wait(tid, f"checking {tid}"):
            return
        ok, summary, logf = self.check_at(sha, tid)
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
                                       self.stopper(now() + timedelta(minutes=30)), self.critic_retry(tid))
            if entry is None:
                self.log(f"{tid}: the critic gave no verdict; on to the check")  # a help, never a gate on its own failure
                return True
            past = [entry]
        last = past[-1]
        score = last.get("score")
        dims = " ".join(f"{k}={v:g}" for k, v in last.get("dims", {}).items() if k != "score")
        self.log(f"{tid}: critic {'-' if score is None else f'{score:g}'}/10{f' ({dims})' if dims else ''} "
                 f"({last['step']}): {last.get('gist', '')}")
        if last.get("unanswered"):
            self.log(f"{tid}: the critic left {', '.join(last['unanswered'])} unanswered (counted as no)")
        if last["step"] in ("revise", "rethink"):
            self.feedback[tid] = (f"The critic scored your head {score:g}/10 ({last['step']}). Fix its failed checks "
                                  f"and \"Fix in this ticket\" points, refresh the pack and run `windvane critic {tid}` again:\n"
                                  f"{last.get('critique', '')}")
            pr = pr_for(p, branch)
            if not critic.comment(p, last, pr):
                self.log(f"{tid}: couldn't post the critique on PR #{pr}")
            self.bounce(tid, "critic")
            return False
        if score is not None:
            self.scores[tid] = score
        return True

    def clean_wait(self, tid, why):
        """keep_changes() before an approved ticket's check or merge, tried again every STASH_WAIT seconds while it
        fails: a stale index.lock or a merge left half-done at the desk is brief, and failing the ticket would drop
        its approval (and two such in a row stop the run). False when a stop or the deadline ends the wait: the next
        run's worker finds the head pushed and finishes READY again."""
        then = f"{tid} waits (its approval stands) and is tried again every {STASH_WAIT} s"
        while not self.keep_changes(why, then):
            self.nap(now() + timedelta(seconds=STASH_WAIT))
            if (self.p.state / "STOP").exists() or now() >= self.end():
                return False
        return True

    def keep_changes(self, why, then=""):
        """With isolate off the clean before a ticket's check or merge, a periodic round or a desk PR runs in the
        checkout itself: whatever is uncommitted there (a ticket's work is pushed by then, so someone at the desk made it) is
        stashed first, never thrown away. False when the stash failed: the caller skips its step (or, `then`, says
        what it does instead) rather than clean."""
        p, cd = self.p, self.cd
        if cd != p.main_root() or not gout(cd, "status", "--porcelain"):
            self.stash_failed.discard(why)  # cleared: a later failure for it is news again
            return True
        msg = f"windvane kept: {now():%Y-%m-%d %H:%M} ({why})"
        r = git(cd, "stash", "push", "-u", "-m", msg)
        if r.returncode == 0 and not gout(cd, "status", "--porcelain"):
            self.stash_failed.discard(why)
            self.log(f"{why}: stashed the checkout's uncommitted changes as \"{msg}\"")
            push("your changes were stashed", lead("Your uncommitted changes were stashed before ", why, ".")
                 + f"\ngit stash list: {msg}", 3, "package")
            return True
        err = (r.stderr.strip() or "?").splitlines()[0][:200] if r.returncode else "changes were left after it"
        first = why not in self.stash_failed
        if first or not then:  # a wait tries again every STASH_WAIT seconds: logged once
            self.log(f"{why}: {'waits' if then else 'skipped'}: couldn't stash the checkout's changes ({err})")
        if first:
            self.stash_failed.add(why)
            push("checkout not cleaned", lead("Commit or stash your checkout's changes: ",
                                              then or why + " was skipped", ".")
                 + f"\nwindvane couldn't stash them, and deleted nothing. git said: {err}", 4, "warning")
        return False

    def check_at(self, rev, ticket=None):
        cd = self.cd
        if not clean_detach(self.p, cd, rev):
            return False, f"couldn't check out {rev[:12]}", None
        return check(self.p, cd, ticket=ticket)

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
            if error and self.crashed(f"{tid}-review", error, tid):
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
            if not self.clean_wait(tid, f"merging {tid}"):  # isolate off: the checkout was edited during the review
                return None
            self.fetch()
            clean_detach(p, cd, p.upstream)
            msg = actions.tag(p, f"Merge {tid}: {title} (branch {branch})", cd)
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
            ok, summary, logf = check(p, cd, ticket=tid)
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
            push(f"Provisional: {self.name(tid)}", f"To change one: windvane answer {tid} <letter>, or tell a Claude "
                 "session."
                 + "".join(f"\n{q['question']} Built {q['built']}: {q['option']}" for q in picks), 3, "memo")

    def fail(self, tid, why):
        self.skip.add(tid)
        self.failed.append(tid)
        self.approved.pop(tid, None)
        self.log(f"{tid}: FAILED: {why}")
        # what `windvane stats` reads as the ticket's outcome, so a reworded log line can't hide a failure
        append_jsonl(self.p.state / "ledger.jsonl", {"event": "outcome", "ticket": tid, "outcome": "failed"})
        if net.online():
            self.fail_streak += 1
        else:  # the outage's fault, not the tickets': it mustn't stop the run
            self.log(f"{tid}: failed with the network down: not counted toward two failures in a row")
        push(f"Failed: {self.name(tid)}", lead("", why, ". Branch kept, run goes on."), 4, "x")

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
                opt = q["opts"].get(q["rec"], "")[:300]
                push(f"No answer: {self.name(tid)}", lead(f"Going with {q['rec']}: ", opt, ".")
                     + f"\nTo change it later: windvane answer {tid} <letter>.", 3, "hourglass")
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
            push("quiet times ignored", lead("", err, "; asking as usual till it's fixed.")
                 + "\n`windvane quiet` shows the format.", 3, "warning")
        self.quiet_err_sent = err
        if not end:  # only money, licence and merged-work questions ask in a quiet time
            self.send_unsent()
        if self.was_quiet and not end:
            self.log("quiet time over")
            picks = provisional(gout(self.p.main_root(), "show", f"{self.p.upstream}:{self.p.questions_rel}"))
            if picks:
                push(f"Review: {len(picks)} provisional answer{'s' * (len(picks) > 1)}",
                     "To change one: windvane answer <ID> <letter>."
                     + "".join(f"\n{q['ticket']}: {q['question']} Built {q['built']}." for q in picks[:8]), 3, "memo")
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
        ticket would bounce back to its worker with a red check it can't fix. A stop or the deadline ends the setup as
        it ends the run."""
        try:
            for d in self.slot_dirs(slot):
                ensure_worktree(self.p, d, stop=self.setup_stop)
            return True
        except SetupFailed as e:
            if e.cut:  # stop_check gives the stop or the deadline as the run's reason
                self.log(str(e))
                return False
            last = e.lines[-1][:200] if e.lines else "no output"
            fix = "; raise `setup_minutes` in config.toml or fix the setup" if e.killed == TIMED_OUT else ""
            self.halt = f"setup {e.how}{fix}: {last} (it ran {e.ran})"  # the fix first: the push's line is cut
            self.log(str(e))
            log(self.p, "\n".join(["its last lines:", *e.lines]), echo=False)  # the terminal showed them already
            return False

    def setup_stop(self):
        """A setup's stop rule: any stop (it's nothing paid for yet), or the run's deadline as it now is."""
        return "stopped" if (self.p.state / "STOP").exists() else "deadline" if now() >= self.end() else ""

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
        replan budget, the same dead-session counts (so a ticket on trial is still decided by the next session), and
        the same merges in the final summary. Any other start begins afresh."""
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
        swept = sweep_nested(p)  # under the lock: no live run owns them
        if swept:
            self.log(f"killed {swept} process group{'s' * (swept > 1)} left running by an earlier run's sessions")
        ready, crashed = False, None
        try:
            ready = self.rules_ok() and self.prepare(0)  # before any session or push: a bad rule or a failed setup
            # ends the run with a single push
            self.carry_over(restarted)
            if not CHECKOUT:
                self.newer = selfupdate.newer_release(selfupdate.version())
                if self.newer:
                    self.log(f"windvane {self.newer} is out: pipx upgrade windvane")
            if ready:
                actions.refresh(p)  # status and the phone read only the cached count
            if ready and not restarted:
                self.usage.probe(p)
                push("windvane started", f"Until {hhmm(deadline)}. {self.usage.text()}.", 2, "rocket",
                     ("Status=status", "Stop=stop"))
            elif not ready:
                self.stop_reason = self.halt or self.stop_check()
                if self.stop_reason == "deadline" and not restarted:  # cut short during setup
                    self.stop_reason = "deadline reached during setup; nothing ran"
            if ready:
                push_held(p)  # a plan a failed push kept locally, once a run: the network may be back
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
                rest = "Merged:" + summary.split("\nMerged:", 1)[1]  # past the why, which may run on to a 2nd line
                save_json(p.state / "crash.json", {"error": crashed, "summary": rest})
            elif not ready and self.halt:  # just why, once: nothing ran, so the summary's other lines say nothing
                push("windvane couldn't start", lead("", self.halt[0].upper() + self.halt[1:], "."), 4, "warning")
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
        return (lead("Why: ", self.stop_reason, ".") + f"\nMerged: {names(self.merged)}.\n"
                + (f"Merged under the critic's bar (out of revisions): {names(under)}.\n" if under else "")
                + (f"Desk PRs merged: {', '.join('#' + n for n in self.desk_merged)}.\n" if self.desk_merged else "")
                + (f"Critic (/10): {crit}.\n" if crit else "") + side
                + (f"Held for you (parked in QUEUE.md): {names(self.held)}.\n" if self.held else "")
                + f"Waiting on you: {names(self.questions)}.\n"
                + (f"Provisional answers to check: {'; '.join(f'{q[0]} {q[1]} (built {q[2]})' for q in picks[:6])}"
                   + (f" and {len(picks) - 6} more" if len(picks) > 6 else "") + ". "
                   "To change one: windvane answer <ID> <letter>.\n" if picks else "")
                + f"Failed: {names(self.failed)}.\n"
                + (f"Stopped at the deadline (resumes next run): {names(self.stopped)}.\n" if self.stopped else "")
                + "".join(f"Set aside: {self.name(t)} ({why}).\n" for t, why in self.aside.items())
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
                push("windvane stopped", lead("Why: ", why, ". Not restarted.") + "\nIts sessions may still be "
                     "running in the worktrees.", 4, "rotating_light", ("Status=status",))
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
            push("windvane restarted", lead("The driver crashed: ", why,
                                            f". Restart {len(crashes)} of {RESTARTS} this hour.")
                 + f"\nIt goes on until {hhmm(deadline)}.", 3, "repeat", ("Status=status",))
            sleep(RESTART_PAUSE)
            stop = asked()
        if stop:
            log(p, f"driver crashed ({why}); not restarted: {stop}")
            push("windvane stopped", lead("Why: the driver crashed (", why, f"); not restarted: {stop}.",
                                          log=lambda m: log(p, m)) + "\n"
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
    whole (a cut one doesn't open): `lead` moves what passes the lock screen to the next line."""
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
    return lead("", why[:1].upper() + why[1:], ".") + f"\n{kept} {branch}."


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
    first = lead("", title, f": on {where}, {how}.", whole=False, log=lambda m: log(p, m))
    return first, no_pr or (to_land and not landed)


def pending_ids(p):
    """Ticket ids on unmerged remote `plan-*` and `periodic-*` branches: tickets whose PR is still open have claimed
    them, and so do local ones (a plan whose push was refused, waiting to be pushed by hand). A branch whose PR was
    closed unmerged frees its ids (its branch is left alone)."""
    ids = set()
    remote = gout(p.main_root(), "branch", "-r", "--no-merged", p.upstream, "--format=%(refname:short)").split()
    local = gout(p.main_root(), "branch", "--no-merged", p.upstream, "--format=%(refname:short)").split()
    heads = {b: b.removeprefix(f"{p.remote}/") if b in remote else b for b in remote + local}
    heads = {b: name for b, name in heads.items() if name.startswith(("plan-", "periodic-"))}
    closed = closed_prs(p, set(heads.values()))
    for b, name in heads.items():
        if name not in closed:
            names = gout(p.main_root(), "ls-tree", "--name-only", b, p.tickets_rel + "/").split()
            ids |= {p.norm(Path(n).stem) for n in names if n.endswith(".md")} - {None}
    return ids


def closed_prs(p, branches):
    """Which of `branches` have PRs that were all closed unmerged; none off github or without `gh` (every branch
    counts). One query per branch rather than a list of recent PRs, so no limit drops an old closed plan; a branch
    found closed is remembered in closed_plans.json and not asked about again (each query takes ~0.8 s, and the
    closed ones pile up, since their paid-for branches are kept). One list of the open PRs per call notices a
    reopened one: its branch leaves the file and claims its ids again, or a new plan would reuse them; when that
    list fails, no branch counts as closed and the file stays."""
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
    if closed != known:  # a deleted branch's line goes too
        save_json(f, sorted(closed))
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
    body = (lead(f"{waits} ", where, f"{how}.", log=lambda m: log(p, m)) if how
            else local_push(where, f"{waits} the local branch"))
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
            push("plan not pushed", local_push(where, f"{n_tickets(n)} kept only locally on"), 4, "warning")
            return 0
        if p.cfg["forge"] == "github":
            pr = net.sh(cd, "gh", "pr", "create", "--head", branch, "--base", p.main, "--title",
                        f"Plan: {n_tickets(n, 'new ')}", "--body", text.strip()[-6000:])
            log(p, f"plan: {n_tickets(n)} on {pr or branch}")
        else:
            log(p, f"plan: {n_tickets(n)} pushed to {branch}")
    push(f"planned {n_tickets(n)}", lead("", m.group(3)[:300] or "See the queue."), 3, "memo")
    clean_detach(p, cd, p.upstream)
    return n
