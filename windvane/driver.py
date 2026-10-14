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
import subprocess
import time
import traceback
from contextlib import contextmanager, nullcontext
from datetime import datetime, timedelta

from . import (actions, critic, install, jobs, net, phone, privacy, renumber, requests, scratch, selfupdate, statedir,
               util, worktree)
from .renumber import noted, old_numbers, said
from .desk import Desk
from .held import author_ident, push_held
from .periodic import Periodic
from .phone import NTFY, REASON, cut, whole
from .planner import plan
from .config import CHECKOUT, DIR, HOME, exe, slots
from .jobs import check, tail, waited
from .questions import (answered, desk_answers, give_answer, keys, mode, note, parts, phone_answer, provisional, qkey,
                        quiet_note, quiet_now, sections, shown, ticket_of, tickets, unseen, waiting_on, with_ids)
from .queue import Queue, branch_depends
from .runner import NO_REQUEST, Usage, over_caps, plain_error, rules, run_claude, sweep_nested
from .util import append_jsonl, git, gout, hhmm, last_match, load_json, log, now, push, read_jsonl, save_json
from .worktree import TEARDOWN_FAILED, TIMED_OUT, SetupFailed, clean_detach, ensure_worktree, teardown

RESULT_RE = re.compile(r"RESULT: (READY|PARTIAL|BLOCKED|FAILED) (\S+)[ \t]*(.*)")
REVIEW_RE = re.compile(r"REVIEW: (APPROVE|CHANGES) (\S+)[ \t]*(.*)")
CARRIED = ("periodic_at", "groups_seen", "held", "skip", "aside", "stopped", "merged", "failed", "partials", "reviews", "bounces", "scores", "plans", "fail_streak", "peak",
           "conflicts", "spent", "update_said", "desk_merged", "suspect", "ticket_crashes", "oks", "crashes")  # the run's own memory, handed to the driver a restart starts
# Each kind of send-back spends its own budget: (who sent it back, config table, key, send-backs allowed over the key
# and why). The critic gives its last verdict after `revisions` itself; the spare one covers a READY without a pack.
BUDGETS = {"review": ("a missing PR", "autopilot", "review_rounds", 0, ""),
           "critic": ("the critic", "critic", "revisions", 1, "one more for a READY without the evidence pack"),
           "check": ("the check", "autopilot", "check_retries", 0, ""),
           "conflict": ("a merge conflict", "autopilot", "conflict_retries", 0, "")}
DIGEST_RETRY = 600  # seconds between tries to push provisional answers the phone missed, outside a quiet time
PULL_EVERY = 600  # seconds between looks for a newer windvane while no ticket merges (a wait for answers)
NAP = 5  # seconds between a waiting driver's looks at STOP and the deadline
STASH_WAIT = 30  # seconds between tries to stash the checkout's changes before an approved ticket's check or merge
RESTARTS = 3  # crashes in an hour a detached run's driver is restarted from; the next one ends the run
RESTART_PAUSE = 30  # seconds before such a restart, so a driver that dies at once doesn't spin
# The environment variable set for a driver under `supervise`: it hands a crash over instead of pushing it
SUPERVISED = "WINDVANE_SUPERVISED"
SIGNALLED = {signal.SIGTERM: "the driver got SIGTERM (a kill or a shutdown)",
             signal.SIGHUP: "the driver's terminal or tmux session closed (SIGHUP)",
             signal.SIGINT: "Ctrl-C during a merge, a push or a pull"}  # outside such a step it's KeyboardInterrupt


class Signalled(BaseException):
    """SIGTERM or SIGHUP (or a Ctrl-C a guarded step held back), raised in the driver's main thread as Ctrl-C raises
    KeyboardInterrupt, so `run`'s `finally`
    (the push, the save, the teardown) runs: Python's own handlers for these end the process on the spot. Not an
    Exception, so it's no crash `supervise` would restart."""
# Errors a ticket brings on itself, as (text in the error, lowercase; what to do), for its set-aside push
SELF_INFLICTED = (("prompt is too long", "Split the ticket, or trim what it reads."),
                  ("context limit", "Split the ticket, or trim what it reads."),
                  ("request too large", "Split the ticket, or trim what it reads."))



def said_times(n):
    return f"{n} time{'s' * (n != 1)}"


def worker_perms(p):
    m = p.main
    deny = [f"Bash(git push * {m})", f"Bash(git push * HEAD:{m})", f"Bash(git push * {m}:{m})",
            "Bash(git push --force *)", "Bash(git push -f *)", "Bash(git reset --hard *)", "Bash(gh pr merge *)",
            f"Bash(git switch {m})", f"Bash(git checkout {m})", "Bash(git worktree *)",
            *actions.DENY, *requests.worker_deny(p)]
    if not p.ap["subagents"]:
        deny.append("Agent")
    allow = ["Bash(windvane *)", "Bash(git *)", "Bash(gh pr create *)", "Bash(gh pr view *)", "Bash(gh pr edit *)",
             "Bash(gh pr comment *)", "Bash(gh pr list *)"]
    return allow, deny


REVIEW_DENY = ["Edit", "Write", "NotebookEdit", "Agent", "Bash(git push *)", "Bash(git commit *)",
               "Bash(git switch *)", "Bash(git checkout *)", "Bash(git reset *)", "Bash(gh pr merge *)",
               "Bash(gh pr edit *)", "Bash(gh pr comment *)", "Bash(gh pr close *)", NO_REQUEST]


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


def changed(wd, upstream, prefix):
    """The files a running ticket has changed in its worktree `wd`: uncommitted, and committed on its branch against
    `upstream` (only while the worktree is on a branch starting with `prefix`: a slot a bounced ticket left holds
    that ticket's head until the next worker switches). None when git can't tell. No optional locks: a plain status
    rewrites the index under index.lock, and the worker's own `git add` or `commit` at that moment would fail."""
    try:
        st = git(wd, "--no-optional-locks", "status", "--porcelain", "-z", "-b", "-uall")
        if st.returncode:
            return None
        head, *rest = st.stdout.split("\0")
        out, entries = set(), iter(rest)
        for e in entries:
            if len(e) > 3:
                out.add(e[3:])
                if "R" in e[:2] or "C" in e[:2]:
                    out.add(next(entries, ""))  # a rename's old path follows it
        branch = head[3:].split("...")[0]
        if branch.startswith(prefix + "-") or branch == prefix:
            d = git(wd, "diff", "--name-only", "-z", f"{upstream}...HEAD")
            if d.returncode:
                return None
            out |= set(d.stdout.split("\0"))
    except OSError:  # its worktree is gone
        return None
    return out


def apart(paths, notes):
    """`paths` without the `shared_notes` files: every ticket appends to them and the merge resolves that, so they
    never make one ticket wait for another."""
    return {x for x in paths if not noted(x, notes)} if notes else paths


def beyond(files, touches):
    """The `files` a Touches list doesn't cover, leaving out the queue's own under .windvane/."""
    return sorted(f for f in files if f and not f.startswith(DIR + "/") and not overlaps({f}, touches))


def nth(n):
    return {1: "1st", 2: "2nd", 3: "3rd"}.get(n, f"{n}th")


def pr_for(p, branch):
    if p.cfg["forge"] != "github" or not branch:
        return ""
    return net.sh(p.main_root(), "gh", "pr", "list", "--head", branch, "--state", "open", "--json", "number",
                  "-q", ".[0].number")


def also(tickets, most=NTFY):
    """The digest's line naming the answers past its first eight by ticket, each ticket once, or "". Kept under
    `most` bytes (what the push has left): a backlog too long for that ends with how many more."""
    ids = list(dict.fromkeys(tickets))
    if not ids:
        return ""
    end = "; windvane status lists them."
    for n in range(len(ids), 0, -1):
        line = "\nAlso: " + ", ".join(ids[:n]) + (f" and {len(ids) - n} more" if n < len(ids) else "") + end
        if len(line.encode()) <= most:
            return line
    return f"\nAlso {len(ids)} more{end}"


class Pilot(Desk, Periodic):
    def __init__(self, p, a):
        self.p, self.a = p, a
        phone.use(p)
        self.usage = Usage(p)
        self.state_file = p.state / "state.json"
        s = load_json(self.state_file, {})
        self.feedback = s.get("feedback", {})  # ticket -> what its next worker run must act on
        self.questions = s.get("questions", {})  # question id (questions.keys) -> a question for the user
        self.costs = s.get("costs", [])  # 5-hour-window points each ticket took
        self.approved = s.get("approved", {})  # ticket -> its approved head bounced off main: {"sha", "why"}
        self.secrets = s.get("secrets", {})  # ticket -> {"branch", "sha"}: a head set aside for a secret (secret_aside)
        self.skip, self.merged, self.failed = set(), [], []
        self.aside = {}  # ticket -> why the run went on without it, for the summary's "Set aside" line
        self.stopped = []  # tickets whose worker the deadline cut off: not set aside, the next run resumes them
        self.partials, self.reviews, self.scores, self.bounces = {}, {}, {}, {}  # bounces: ticket -> {kind: n}
        self.fail_streak, self.plans, self.was_quiet, self.idle = 0, 0, False, False
        self.digest_tried = 0.0  # when a push last tried the phone's missed provisional answers (see quiet_check)
        self.crashes, self.halt, self.unfinished = 0, None, set()  # sessions dead in a row; why the run must stop
        # Sessions dead per ticket: ticket -> [crashes, self.oks at its first], and the sessions that ended well, so a
        # ticket whose own sessions keep dying can be told from a broken API. `suspect`: [ticket, error] set aside
        # on its own crashes alone, until another ticket's session shows the API works.
        self.ticket_crashes, self.oks, self.suspect = {}, 0, None
        self.sleep = time.sleep  # the retry backoff's; tests make it instant
        self.stop_reason = "unknown"
        self.deadline, self.kill_all = None, False  # set by run(); `windvane stop --now` sets kill_all
        self.signalled, self.ending = None, False  # the SIGTERM or SIGHUP that ended the run; past its `finally`
        self.guarded = 0  # how deep the main thread is in `critical` steps: a signal waits for them to end
        self.left = ""  # the pid in the lock file a run that ended without its teardown left (`take_lock`)
        self.kept_note = ""  # the summary's line on kept jobs (`bg --keep`) the run's end killed or let go on
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
        self.tmp_full = False  # pushed that the temp dir is nearly full, and it hasn't dropped back under the bar
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
        self.requests_tried = set()  # windvane requests a [[periodic]] round was given this run: never twice
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
                                        "secrets": dict(self.secrets),
                                        "costs": self.costs[-10:], "minutes": self.minutes[-10:],
                                        "running": dict(self.running)})

    def log(self, msg):
        log(self.p, msg)

    def name(self, tid, key=None):
        """The ticket's title and id; `key` shows instead of the id (one of its several questions: `T-003.q2`)."""
        t = self.queue.titles.get(tid, "") if hasattr(self, "queue") else ""
        return f"{t} ({key or tid})" if t else key or tid

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
        return net.wait(self.p, self.end, lambda: self.signalled or (self.p.state / "STOP").exists(), self.sleep)

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
            self.aside[tid] = f"on trial: its sessions died ({self.said_error(error)})"
            self.log(f"  {label}: {own[0]} sessions died ({error}); set aside while another ticket runs: if its "
                     "session dies too, the run stops")
            return False
        wait = self.p.ap["retry_minutes"] * 2 ** (max(self.crashes, own[0] if own else 0) - 1)
        if self.crashes > n:
            self.halt = f"{self.crashes} sessions in a row died ({self.said_error(error)})"
        elif now() + timedelta(minutes=wait) >= self.end() - timedelta(minutes=self.a.min_slot):
            self.halt = f"a session died ({self.said_error(error)}), and there's no time left to retry it"
        if self.halt:
            self.log(f"  {label}: {self.halt}" + (f"; the error: {error}" if error not in self.halt else ""))
            return False
        self.log(f"  {label} died ({error}); retry {own[0] if own else self.crashes}/{n} in {wait:g} min")
        return self.backoff(wait)

    def said_error(self, error, log=None):
        """A session's error as a reason says it (the run summary's aside line, the stop reason): its API message, at
        most REASON bytes, since a summary carries a few of them in one push. The log keeps the whole text (`log`, a
        no-op where the caller logs it itself)."""
        return whole("", plain_error(error), log=log or self.log, most=REASON)

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
        q, blocked = self.queue, tickets(self.questions)
        return any(t != tid for t in self.running) or any(
            t != tid and t not in self.skip and q.status(t, blocked) in ("wip", "todo") for t in q.order)

    def set_aside(self, tid, error):
        """A ticket whose own sessions keep dying while others' don't: the rest of the run goes on without it."""
        self.skip.add(tid)
        self.log(f"{tid}: set aside: its sessions keep dying ({error}) while other sessions work")
        logged = lambda m: None  # noqa: E731  the line above has the whole error: the cuts below don't log it again
        self.aside[tid] = f"its sessions kept dying: {self.said_error(error, logged)}"
        error = plain_error(error)
        died = f"Sessions died {self.p.ap['retries'] + 1}×: "
        why = "Other sessions worked, so it's this ticket's doing."
        hit = next(((text, fix) for text, fix in SELF_INFLICTED if text in error.lower()), None)
        if hit:  # the fix leads: the error's own words rarely say what to do
            body = (whole(died, hit[0], f". {hit[1]}", log=logged)
                    + "\n" + whole("The error: ", error, ".", log=logged) + f"\nBranch kept, run goes on. {why}")
        else:
            body = whole(died, error, ". Branch kept, run goes on.", log=logged) + f"\n{why}"
        push(f"Set aside: {self.name(tid)}", body, 4, "warning")

    def set_aside_limit(self, tid, limit, fix):
        """A worker run stopped at a limit of its own (the time box, max turns or budget): the ticket is too big for
        one run as it stands, so the run goes on without it and the phone says which limit and what to change."""
        self.skip.add(tid)
        self.aside[tid] = f"hit {limit}"
        self.log(f"{tid}: the run hit {limit}; set aside")
        push(f"Set aside: {self.name(tid)}", whole("Hit ", limit, f". {fix} Branch kept.", log=self.log), 3,
             "hourglass")

    def secret_aside(self, tid, branch, sha, found):
        """A ticket whose commits add a secret under `.windvane/` isn't merged: main would keep it in its history for
        good. Only the user can rewrite the branch (sessions never force-push), so the run goes on without it, and
        later runs too while its head stays `sha` (`secrets_unchanged`): no session is paid to find it again. The
        phone hears kind and place once, never the text."""
        p = self.p
        self.skip.add(tid)
        self.aside[tid] = f"its {DIR}/ lines hold {privacy.said(found, 1)}"
        with self.lock:
            self.secrets[tid] = {"branch": branch, "sha": sha}
        self.save()
        self.log(f"{tid}: not merged: the lines its commits add under {DIR}/ hold {privacy.said(found)}; set aside")
        push(f"Set aside: {self.name(tid)}",
             whole("Not merged: its commits add ", privacy.said(found), f", which would stay in {p.main}'s history.",
                   log=self.log)
             + f"\nRewrite the branch without it: `git fetch {p.remote} && git switch {branch} && git rebase -i "
             f"{p.upstream}`, then `git push --force-with-lease {p.remote} {branch}`. Change the secret if it's real: "
             "the branch is pushed already. Until it changes, runs leave it be; then the next one takes it up again.\n"
             f"Not a secret? Let its kind through with `scan_allow` in {DIR}/config.toml.", 4, "warning")

    def secrets_unchanged(self):
        """Set aside, without a session, each ticket `secret_aside` stopped whose branch still has that head; one the
        user rewrote (or deleted) goes back to the queue as usual."""
        root = self.p.main_root()
        for tid, s in list(self.secrets.items()):
            if tid in self.skip:
                continue
            if gout(root, "rev-parse", "-q", "--verify", f"{self.p.remote}/{s['branch']}") == s["sha"]:
                self.skip.add(tid)
                self.aside[tid] = f"its {DIR}/ lines still hold a secret: {s['branch']} unchanged since it was set aside"
                self.log(f"{tid}: {self.aside[tid]}; left be")
            else:
                with self.lock:
                    self.secrets.pop(tid, None)
                self.log(f"{tid}: {s['branch']} changed since it was set aside for a secret; back in the queue")

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
        session_env = {**git_env, **actions.gh_guard(p, wd), jobs.RUN_STOP: str(self.end().timestamp())}
        kept = jobs.ended_kept(p, tid)
        prompt = "\n\n".join(filter(None, [
            f"Ticket {tid}. Follow the worker rules in your system prompt and end with the RESULT line.",
            f"## The ticket\n{ticket[:8000]}",
            f"## Act on this first\n{fb}" if fb else "",
            "## Your kept jobs (`bg --keep`) ended\n" + "\n".join(kept) if kept else "",
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
            f"Shared notes ({', '.join(p.shared_notes)}): add to them as a new section at the end; don't rewrite their "
            "existing text, or your merge conflicts." if p.shared_notes else "",
            skip_line,
        ]))
        self.log(f"{tid}: worker run ({kind}{', ' + ' '.join(branches) if branches else ''})")
        allow, deny = worker_perms(p)
        # in the system prompt, as a rule: a quiet time now, or one starting before the session can end
        text, killed, error = self.claude("worker", tid, prompt, self.worker_rules + quiet_note(p, kill_at), wd, limit,
                                          allow, deny, tid, session_env)
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
            waits = self.waits_on_merge(tid, branch)
            if waits:  # not out of time: the queue holds it until they merge, then resumes it
                self.log(f"{tid}: waits for {', '.join(waits)} to merge (its branch says so)")
                return self.held_for_good(tid, branch, waits)
            kept = jobs.kept_jobs(self.p, tid)
            if kept:  # not out of time either: the queue holds it until its job ends, then resumes it
                return self.log(f"{tid}: held until its kept job {', '.join(repr(b.name) for b in kept)} ends: {rest}")
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

    def waits_on_merge(self, tid, branch):
        """The unmerged tickets the `Depends on:` line on the ticket's pushed branch names: a worker that can't go on
        until another ticket merges adds it there, and its PARTIAL isn't a run that ran out of time."""
        q = getattr(self, "queue", None)
        if not (q and branch and tid in q.files):
            return []
        return [d for d in branch_depends(self.wd, f"{self.p.remote}/{branch}", q.files[tid], self.p)
                if d != tid and q.known(d) and not q.is_done(d)]

    def held_for_good(self, tid, branch, waits):
        """A ticket its branch makes wait for one that can't merge (parked, set aside, or itself waiting for this
        one) would wait silently forever: say so once, with how to release it."""
        q = self.queue
        why = {d: "parked" if d in q.parked else "set aside" if d in self.skip else
               f"waits for {tid} too" if q.leads_to(d, tid) else "" for d in waits}
        stuck = [f"{d} ({w})" for d, w in why.items() if w]
        if stuck:
            push(f"Held: {self.name(tid)}", f"Its branch waits for {', '.join(stuck)}, which can't merge first. To "
                 f"release it, take {'them' if len(stuck) > 1 else 'it'} out of `Depends on:` in {q.files[tid]} on "
                 f"{branch}.", 3, "hourglass")

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
        ok, summary, logf = self.ran(tid, lambda: self.check_at(sha, tid, self.end()), f"check on {sha[:7]}")
        if ok is None:  # stopped while the machine was busy: the next run's worker finds the head and is READY again
            return
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
                           f"{f' ({why})' if why else ''}: {cut(self.feedback.get(tid, ''), log=self.log)}")

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
        with self.critical():  # half a stash -u loses the user's untracked files from view
            r = git(cd, "stash", "push", "-u", "-m", msg)
        if r.returncode == 0 and not gout(cd, "status", "--porcelain"):
            self.stash_failed.discard(why)
            self.log(f"{why}: stashed the checkout's uncommitted changes as \"{msg}\"")
            push("your changes were stashed",
                 whole("Your uncommitted changes were stashed before ", why, ".", log=self.log)
                 + f"\ngit stash list: {msg}", 3, "package")
            return True
        err = (r.stderr.strip() or "?").splitlines()[0] if r.returncode else "changes were left after it"
        first = why not in self.stash_failed
        if first or not then:  # a wait tries again every STASH_WAIT seconds: logged once
            self.log(f"{why}: {'waits' if then else 'skipped'}: couldn't stash the checkout's changes ({err})")
        if first:
            self.stash_failed.add(why)
            push("checkout not cleaned", whole("Commit or stash your checkout's changes: ",
                                              then or why + " was skipped", ".", log=self.log)
                 + "\nwindvane couldn't stash them, and deleted nothing. " + whole("git said: ", err, "", log=self.log),
                 4, "warning")
        return False

    def ran(self, tid, run, what):
        """`run()` (a check) again until it runs: a heavy check whose wait for the machine timed out says nothing
        about the ticket, so it costs no worker run and no bounce, only a line in the log for each wait. (None, ...)
        when a stop or the deadline comes first. A ticket's check waits for the machine until the run's end, keeping
        its place in line (not its worker's time box: that started with the worker and may be spent), so it runs
        again only when the deadline moved later while it waited."""
        while True:
            ok, summary, logf = run()
            if ok is not None:
                return ok, summary, logf
            if self.stopping_now() or (self.p.state / "STOP").exists() or now() >= self.end():
                self.log(f"{tid}: {what}: {waited(summary)}; the next run checks it again, nothing to do")
                return ok, summary, logf
            self.log(f"{tid}: {what}: {waited(summary)}; the autopilot runs it again, nothing to do")
            self.nap(now() + timedelta(seconds=STASH_WAIT))  # a short heavy_wait mustn't spin

    def check_at(self, rev, ticket=None, stop_at=None):
        cd = self.cd
        if not clean_detach(self.p, cd, rev):
            return False, f"couldn't check out {rev[:12]}", None
        return check(self.p, cd, ticket=ticket, stop_at=stop_at)

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
            found = privacy.outgoing(cd, p.upstream, merge, DIR, privacy.guarded(p))
            if found:  # before the check: no paid-for check on a merge that can't go out
                return self.secret_aside(tid, branch, sha, found)
            if renumbered is not None:
                self.log(f"{tid}: conflicted with {p.main} only in appended entries: {said(renumbered)}; resolved "
                         "without a worker run")
            ok, summary, logf = self.ran(tid, lambda: check(p, cd, ticket=tid, stop_at=self.end()),
                                         "check on the merged tree")
            if ok is None:
                return None
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
                           + old_numbers(renumbered))
                res = net.call(["gh", "pr", "merge", pr, "--merge", "--match-head-commit", sha, "--subject", msg],
                               p.main_root())
                merged = res.returncode == 0 or net.sh(p.main_root(), "gh", "pr", "view", pr, "--json", "state",
                                                       "-q", ".state") == "MERGED"
            else:  # by sha, so nothing that moves the ci worktree's HEAD meanwhile can turn it into a no-op
                res = net.git(cd, "push", "-q", p.remote, f"{merge}:refs/heads/{p.main}")
                merged = res.returncode == 0
            if merged:
                if self.landed(tid, branch, sha):  # the ci worktree is still on the merge
                    return self.merged_ok(tid, branch, sha, renumbered, self.off_touches(tid))
                return None
            self.log(f"{tid}: merge refused ({cut(' '.join((res.stderr or res.stdout).split()))}); trying again")
        self.fail(tid, "the merge was refused three times")

    def merge_main(self, rev, msg):
        """Merge `rev` into main in the ci worktree (left on the merge). A conflict only in appended entries of
        `[[append_only]]` files and sections of `shared_notes` files is resolved and committed with what it did in its
        message. Returns (merged, what it did, as renumber.resolve() says it: None for a clean merge)."""
        cd, ident = self.cd, author_ident(self.p)
        if git(cd, *ident, "merge", "--no-ff", "-m", msg, rev).returncode == 0:
            return True, None
        p = self.p
        done = renumber.resolve(cd, p.append_only, p.shared_notes) if p.append_only or p.shared_notes else None
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

    def off_touches(self, tid):
        """The files the merge in the ci worktree brings to main that `tid`'s Touches line didn't name: where planners'
        Touches lines fall short, and so where side-by-side tickets meet without the driver knowing."""
        touches = self.queue.touches.get(tid)
        d = git(self.cd, "diff", "--name-only", "-z", "HEAD^1", "HEAD")
        return (beyond(apart(d.stdout.split("\0"), self.p.shared_notes), touches) if touches and d.returncode == 0
                else [])

    def merged_ok(self, tid, branch, sha, renumbered=None, outside=()):
        """`renumbered`: the merge's renumbering, said in the push: on forge git it's the only place but the log.
        `outside`: the files it changed off its Touches line, logged for planners."""
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
        jobs.end_kept(p, tid, "its ticket merged")
        scratch.drop(p, tid)
        with self.lock:  # this attempt is still running: its time so far counts too
            ran = self.spent.pop(tid, 0) + time.time() - self.began.get(tid, time.time())
        self.minutes.append(round(ran / 60, 2))
        self.fail_streak = 0
        self.feedback.pop(tid, None)
        self.approved.pop(tid, None)
        self.save()
        score, first = self.scores.get(tid), critic.first_score(p, tid)
        self.log(f"{tid}: merged {sha[:7]} · {self.usage.text()}")
        if outside:
            self.log(f"{tid}: changed outside its Touches line: {', '.join(outside)}")
        lines, picks = self.built_lines(tid)
        sent = push(f"Done: {self.name(tid)}", "Built, checked, reviewed and merged."
                    + (f" Critic {score:g}/10" + (f" (first try {first:g})" if first not in (None, score) else "") + "."
                       if score is not None else "") + f" {self.usage.text()}"
                    + (f"\nMerged with {p.main}: {said(renumbered)}." if renumbered else "") + lines,
                    2, "white_check_mark")
        if picks:
            with self.lock:  # slots merge side by side
                def told(s):
                    if sent:  # a failed push leaves them to a later digest
                        s["pushed"] += [qkey(q) for q in picks if qkey(q) not in s["pushed"]]
                    s["asked"].pop(tid, None)
                note(p, told)
            self.digest_tried = time.time()

    def built_lines(self, tid):
        """The Done push's lines for the ticket's provisional answers, and those answers. A question the phone already
        showed gets one line without its text; only one built without the user ever seeing it (a quiet time) is
        spelled out. Once the push went out, either way it counts as pushed: the digest leaves it out."""
        p = self.p
        picks = [q for q in with_ids(provisional(gout(p.main_root(), "show", f"{p.upstream}:{p.questions_rel}")))
                 if p.norm(q["ticket"]) == tid]
        if not picks:
            return "", []
        with self.lock:
            new = unseen(picks, shown(p)["asked"].get(tid, []))
        how = lambda q: f"to change: windvane answer {q['id']} <letter>."  # noqa: E731
        return "".join(f"\n{q['question']} Built {q['built']} without asking you: {q['option']}; {how(q)}" if q in new
                       else f"\nBuilt {q['built']} for {tid}: {q['option']}; {how(q)}" for q in picks), picks

    def fail(self, tid, why):
        self.skip.add(tid)
        jobs.end_kept(self.p, tid, "its ticket failed")
        scratch.drop(self.p, tid)
        self.failed.append(tid)
        self.approved.pop(tid, None)
        self.log(f"{tid}: FAILED: {why}")
        # what `windvane stats` reads as the ticket's outcome, so a reworded log line can't hide a failure
        append_jsonl(self.p.state / "ledger.jsonl", {"event": "outcome", "ticket": tid, "outcome": "failed"})
        if net.online():
            self.fail_streak += 1
        else:  # the outage's fault, not the tickets': it mustn't stop the run
            self.log(f"{tid}: failed with the network down: not counted toward two failures in a row")
        push(f"Failed: {self.name(tid)}", whole("", plain_error(why), ". Branch kept, run goes on.", log=self.log), 4,
             "x")

    # ---------- questions

    def ask(self, tid, branch, gist):
        p = self.p
        self.fetch()
        text = gout(p.main_root(), "show", f"{p.remote}/{branch}:{p.questions_rel}") if branch else ""
        found = [x for x in map(parts, sections(text, tid)) if len(x[0]) >= 2]
        self.skip.add(tid)
        self.fail_streak = 0
        if not found:
            return self.fail(tid, f"BLOCKED, but no question with options under ## Open on {branch}: {gist}")
        for key, (opts, rec, must_wait, q) in zip(keys(tid, len(found)), found):
            self.questions[key] = {"ticket": tid, "q": q or gist, "opts": opts, "rec": rec, "must_wait": must_wait,
                                   "branch": branch, "asked": None}
            self.log(f"{key}: question for the user: {q or gist}")
        self.save()  # the loop's next pass sends them (state.json keeps them unsent across a restart)

    def send_unsent(self):
        """Send every stored question not yet sent: held through a quiet time, or left by an earlier run."""
        for key, q in list(self.questions.items()):
            if q["asked"] is None:
                self.send_question(key)
                self.save()

    def send_question(self, key):
        q = self.questions[key]
        tid = ticket_of(key, q)
        q["asked"] = time.time()
        wait = "" if q["must_wait"] or not q["rec"] else (
            f" No answer in {self.a.answer_wait:g} min: I go with {q['rec']} and mark it to revisit.")
        opts = [q["opts"][k] for k in sorted(q["opts"])]
        sent = phone.ask(self.p.qid(key), f"{self.name(tid, key)}: {q['q']}{wait}", opts)
        if sent is None:
            self.log(f"{key}: no phone; answer at the desk with `windvane answer {key} <letter>`")
        elif sent:  # the Done push then names it in one line, without the question
            with self.lock:
                note(self.p, lambda s: s["asked"].setdefault(tid, []).append(q["q"]))
        elif not sent:  # the next pass sends it again; the wait for an answer starts once it's out
            q["asked"] = None
            if not q.get("send_failed"):
                self.log(f"{key}: the phone push failed; trying again on the next pass")
            q["send_failed"] = True

    def poll_questions(self):
        """Settle each sent question on its own (an answer, or its recommendation once its wait runs out); a ticket
        runs again once all of its questions are settled, with one feedback naming each. In a quiet time the user
        can't answer, so a question with a recommendation (sent or held) gets it built at once, never left waiting
        all night; one that must wait for the user, or was put off with "later", still waits."""
        p = self.p
        phone.pump()
        desk, settled = desk_answers(p), []
        quiet = quiet_now(p)[0]
        for key, q in list(self.questions.items()):
            if "result" in q or (q["asked"] is None and not quiet):
                continue
            tid = ticket_of(key, q)
            got, letter, text = desk.get(key), None, None
            if got:
                letter, text = got.get("option"), got.get("text")
            elif q["asked"] is not None:
                ph = phone_answer(p, key)
                if ph.startswith("ANSWER"):
                    letter = ph.split()[1]
                elif ph == "LATER":
                    q["later"] = True
            if letter or text:
                opt = q["opts"].get(letter or "", "")
                q["result"] = (f"The user answered your question \"{q['q']}\": "
                               + (f"option {letter} ({opt})" if letter else "")
                               + (f" They added: {text}" if text else "")
                               + ". Build that, and move the question to ## Answered with the answer.")
                self.log(f"{key}: answered {letter or ''} {text or ''}".rstrip())
                with self.lock:  # answered, so never mistaken for a reworded copy of a later provisional answer
                    note(p, lambda s: s["asked"].update({tid: [a for a in s["asked"].get(tid, []) if a != q["q"]]}))
            elif q.get("later") or q["must_wait"] or not q["rec"]:
                continue
            elif quiet:
                self.go_with_rec(key, q, f"The user is in a quiet time until {quiet:%a %H:%M} and can't answer your "
                                 f"question \"{q['q']}\"", f"quiet time till {quiet:%H:%M}", "Quiet time")
            elif q["asked"] is not None and time.time() - q["asked"] > self.a.answer_wait * 60:
                wait = f"{self.a.answer_wait:g}"
                self.go_with_rec(key, q, f"The user didn't answer your question \"{q['q']}\" within {wait} minutes",
                                 f"no answer in {wait} min", "No answer")
            else:
                continue
            settled.append(key)
        for tid in tickets(self.questions):
            mine = {k: q for k, q in self.questions.items() if ticket_of(k, q) == tid}
            if all("result" in q for q in mine.values()):
                self.feedback[tid] = "\n".join(q["result"] for q in mine.values())
                for k in mine:
                    del self.questions[k]
                self.skip.discard(tid)
        if settled:  # read again: an answer `windvane answer` gave meanwhile (during a push) must not be lost
            save_json(p.state / "answers.json", {k: v for k, v in desk_answers(p).items() if k not in settled})
        self.save()

    def go_with_rec(self, key, q, why, said, title):
        """Settle question `key` with its recommendation: the worker's feedback, the record `windvane answer` needs
        to change it, and one push. A question the phone never showed (held for a quiet time) is spelled out, and
        then counts as asked, so the ticket's Done push names it in one line instead of twice in full."""
        p, tid = self.p, ticket_of(key, q)
        q["result"] = (f"{why}: build your recommendation ({q['rec']}), and move the question to ## Provisional with "
                       "its `Built:` line.")
        self.log(f"{key}: {said}; building {q['rec']}")
        if key != tid:  # one of several: `windvane answer <key>` finds its provisional answer once merged
            with self.lock:
                note(p, lambda s: s["built"].update({key: q["q"]}))
        unseen_q = q["asked"] is None
        if not unseen_q:
            phone.timed_out(p.qid(key), key, q["rec"])
        sent = push(f"{title}: {self.name(tid, key)}", (f"{cut(q['q'], log=self.log)} " if unseen_q else "")
                    + whole(f"Going with {q['rec']}: ", q["opts"].get(q["rec"], ""), ".", log=self.log)
                    + f"\nTo change it later: windvane answer {key} <letter>.", 3, "hourglass")
        if sent and unseen_q:
            with self.lock:
                note(p, lambda s: s["asked"].setdefault(tid, []).append(q["q"]))

    def quiet_check(self):
        end, err = quiet_now(self.p)
        if err and getattr(self, "quiet_err_sent", None) != err:  # once, not on every pass; again after a fix
            self.log(f"quiet times ignored: {err}; that file is ignored till it's fixed")
            push("quiet times ignored", whole("", err, "; that file is ignored till it's fixed.", log=self.log)
                 + "\n`windvane quiet` shows the format.", 3, "warning")
        self.quiet_err_sent = err
        if not end:  # only money, licence and merged-work questions ask in a quiet time
            self.send_unsent()
        if self.was_quiet and not end:
            self.log("quiet time over")
            self.digest()
        elif not end and time.time() - self.digest_tried >= DIGEST_RETRY:  # a failed Done push or digest, again
            self.digest()
        self.was_quiet = bool(end)

    def digest(self):
        """At a quiet time's end, and every DIGEST_RETRY outside one: the provisional answers no push has shown yet (a
        Done push or an earlier digest has the rest), or nothing. Past eight, the rest are named by ticket on one
        line, so a long backlog reaches the user in one push, not one quiet time's end per eight. The record keeps
        only those still provisional."""
        p = self.p
        self.digest_tried = time.time()
        picks = provisional(gout(p.main_root(), "show", f"{p.upstream}:{p.questions_rel}"))
        with self.lock:
            new = [q for q in picks if qkey(q) not in set(shown(p)["pushed"])]
        sent = False
        if new:
            body = ("To change one: windvane answer <ID> <letter>."
                    + "".join(f"\n{q['ticket']}: {cut(q['question'], log=self.log)} Built {q['built']}."
                              for q in new[:8]))
            sent = push(f"Review: {len(new)} provisional answer{'s' * (len(new) > 1)}",
                        body + also([q["ticket"] for q in new[8:]], NTFY - len(body.encode())), 3, "memo")
        with self.lock:  # marked only once the phone has them: a failed push leaves them for the next digest
            told = {qkey(q) for q in new} if sent else set()
            note(p, lambda s: s.update(pushed=[qkey(q) for q in picks if qkey(q) in told | set(s["pushed"])]))

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
        q, blocked = self.queue, tickets(self.questions)
        left = sum(t not in self.skip and q.status(t, blocked) in ("wip", "todo", "waiting") for t in q.order)
        m = self.ticket_minutes()
        if (left - len(self.running) / 2) * m > (self.end() - now()).total_seconds() / 60 - self.a.min_slot:
            return ""
        return f"{left} ticket{'s' * (left != 1)} x {m:.0f} min fit before {hhmm(self.end())} one at a time"

    def clash(self, tid, busy, extra=None):
        """Why `tid` can't start beside the `busy` tickets, or "". Tickets run side by side only when their
        `Touches:` lists don't overlap and they aren't related: a planner's `Related:` line, or a ticket naming a
        running one or a file it changes (it builds on unmerged work). One without Touches may touch anything. A
        running ticket's files are also those its branch has changed already: Touches lines are written before the
        work and miss some. `shared_notes` files count for neither. `extra`: running ticket -> those files outside its
        Touches line (computed when None)."""
        q = self.queue
        touches = q.touches
        alone = next((b for b in busy if not touches.get(b)), None)
        if alone:
            return f"{alone} runs alone (no Touches line)"
        if busy and not touches.get(tid):
            return "no Touches line, so it runs alone"
        extra = self.outside(busy) if extra is None else extra
        notes = self.p.shared_notes
        mine = apart(touches[tid], notes)
        for b in busy:
            both = shared(mine, apart(touches[b], notes))
            if both:
                return f"overlaps {b} on {', '.join(both)}"
            both = shared(mine, extra.get(b, ()))
            if both:
                return f"overlaps {b} on {', '.join(both)} (changed, not on its Touches line)"
        text = q.text.get(tid, "")
        for b in busy:
            if b in q.related.get(tid, []) or tid in q.related.get(b, []):
                return f"related to {b}"
            if re.search(rf"\b{re.escape(b)}\b", text):
                return f"names {b}, which is running"
            path = next((x for x in sorted(apart(touches[b], notes)) if names(text, x)), None)
            if path:
                return f"names {path}, which {b} changes"
        return ""

    def outside(self, busy):
        """Running ticket -> the files its branch changed that its Touches line doesn't cover (none when git can't
        tell). The queue's own files under .windvane/ and `shared_notes` don't count: any ticket may note a question,
        a done line or its findings."""
        out, touches = {}, self.queue.touches
        for b in busy:
            slot = self.running.get(b)
            files = None if slot is None else changed(self.slot_dirs(slot)[0], self.p.upstream, self.p.branch_prefix(b))
            out[b] = beyond(apart(files or (), self.p.shared_notes), touches.get(b, ()))
        return out

    def pick(self, free):
        """(ticket, kind, branches, slot) to start in one of the `free` slots, or (None, why, None, None). Notes in
        `waits` why each ready ticket must wait for the running ones. A last ticket also waits while a ticket not
        marked last could still start, even one held only by a clash: last means once the rest has run out."""
        q = self.queue
        busy = sorted(self.running)
        # merged this run: a slot can merge and free itself after this pass's fetch, so the queue may not show it yet
        self.secrets_unchanged()
        avoid = set(self.skip) | set(busy) | set(self.merged)
        self.waits = []
        extra = self.outside(busy)  # once a pass: two git calls a running ticket
        kept, held = {}, self.kept_slots()
        for b in jobs.running_kept(self.p):
            kept.setdefault(b.parent.name, []).append(b.name)
        ahead = q.to_run(avoid, tickets(self.questions))  # those waiting on a clash too: a free slot isn't "run out"
        for t in q.order:
            why = ("" if t in avoid or q.status(t, tickets(self.questions)) not in ("wip", "todo")
                   else f"its kept job {', '.join(map(repr, kept[t]))} runs" if t in kept
                   else f"last, {ahead[0]} still to run" if ahead and t in q.last else self.clash(t, busy, extra))
            if why:
                avoid.add(t)
                self.waits.append(f"{t} waits: {why}")
        while True:
            tid, *rest = q.pick(avoid, tickets(self.questions))
            if tid is None:
                return None, rest[0], None, None
            own = next((i for i, o in [*self.owner.items(), *held.items()] if o == tid), None)
            slot = own if own is not None else next((i for i in free if self.owner[i] is None and i not in held), None)
            if slot is not None and slot in free:
                return tid, rest[0], rest[1], slot
            avoid.add(tid)

    def kept_slots(self):
        """slot -> the ticket a kept job runs for (or ended for, its session not yet told) in that slot's work tree: no
        other ticket may take the tree it writes into, in this run or the next."""
        kept = jobs.kept_jobs(self.p)
        trees = {t: i for i in range(self.n) if (t := jobs.worktree(self.slot_dirs(i)[0]))} if kept else {}
        out = {}
        for b in kept:
            where = jobs.job_cwd(b)
            i = trees.get(jobs.worktree(where)) if where else None
            if i is not None:
                out[i] = b.parent.name
        return out

    def kept_at_end(self):
        """A kept job still waiting or running at the run's end is killed, as a session's job is, unless
        `heavy_keep_hours` lets it go on that long past the stop (not past a stop the user asked for): the next run
        keeps its slot and resumes its ticket when it ends."""
        hours = self.p.cfg["heavy_keep_hours"]
        asked = self.signalled or self.stop_reason.startswith(("asked to stop", "stopped now"))
        if hours and not asked:
            until = now() + timedelta(hours=hours)
            jobs.follow(self.p, until)
            on = [f"{b.parent.name}'s '{b.name}'" for b in jobs.running_kept(self.p)]
            if on:
                self.kept_note = (f"Kept jobs run on until {hhmm(until)} (`windvane stop` ends them sooner); "
                                  f"the next run resumes their tickets when they end: {', '.join(on)}.\n")
        else:
            killed = jobs.end_kept(self.p)
            if killed:
                self.kept_note = f"Kept jobs killed at the stop (their tickets resume next run): {', '.join(killed)}.\n"
        if self.kept_note:
            self.log(self.kept_note.strip())

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
            last = e.lines[-1] if e.lines else "no output"
            fix = "; raise `setup_minutes` in config.toml or fix the setup" if e.killed == TIMED_OUT else ""
            self.halt = f"setup {e.how}{fix}: {last} (it ran {e.ran})"  # the fix first: the error may run long
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
            if gout(wd, "status", "--porcelain") or jobs.kept_jobs(self.p, tid):
                self.owner[slot] = tid  # its leftovers stay here, or its kept job writes here: only it resumes here
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
            self.clean_tmp()
            self.wake.set()

    def clean_tmp(self, between=True):
        """Between tickets: what tests and sessions leaked into the temp dir, before it fills (tmpfs inodes)."""
        with self.lock:
            running = set(self.running)
        dirs, n = scratch.clean(self.p, running)
        if dirs:
            self.log(f"removed {dirs} leftover temp dir{'s' * (dirs > 1)} ({n} files)")
        self.warn_tmp_full()
        self.clean_state(between)

    def clean_state(self, between):
        """What the state dir no longer needs (statedir.py). Between tickets the loop may prepare any slot at any
        moment, so only the transcripts and critic packs go then; idle slot worktrees and build output go at the
        run's start and end, when no slot is being prepared."""
        with self.lock:
            busy = set(self.running.values())
        try:
            n, freed = statedir.clean(self.p, busy=busy, keep_slots=self.n if between else 0,
                                      trim=False if between else None)
        except (OSError, SystemExit) as e:  # a bad [state] setting, a vanished file: the run goes on
            self.log(f"state dir: not cleaned ({e})")
            return
        if n:
            self.log(f"state dir: freed {statedir.human(freed)} ({n} item{'s' * (n > 1)})")

    def warn_tmp_full(self):
        """One push when the temp dir is nearly full even after cleaning (nobody runs doctor at 3am), and another
        only once it has dropped back under the bar and filled again."""
        try:
            inodes, space = scratch.fullness()
        except OSError:
            return
        full = max(inodes, space) > scratch.FULL
        if full and not self.tmp_full:
            tmp, leaks = scratch.root(), scratch.old_leaks()
            young = len(leaks) - len(scratch.stale(leaks, scratch.DAY))
            mine = f"{young} dirs older windvane tests leaked go once a day old; the rest" if young else "the rest"
            msg = (f"{tmp} is {inodes:.0f}% full of inodes and {space:.0f}% of space after cleaning: {mine} isn't "
                   f"windvane's. See what fills it: du --inodes -s {tmp}/* | sort -n | tail")
            self.log(msg)
            push("Temp dir nearly full", msg, 4, "warning")
        self.tmp_full = full

    def loop(self):
        p = self.p
        while True:
            self.quiet_check()
            self.poll_questions()  # every pass, busy slots or not: a question's wait runs out on time
            jobs.follow(p, self.end())  # a deadline moved later keeps the kept jobs going as long
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
            self.queue = Queue(p)
            busy = bool(self.running)  # a slot that ends during the pick still had its ticket held back: pick again
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
                if busy or self.running or jobs.running_kept(p):  # a kept job's ticket resumes when it ends
                    self.idle_note(self.waits)
                    self.pause()
                    continue
                if self.questions:
                    if not self.idle:
                        self.log(f"waiting for answers on {', '.join(waiting_on(self.questions))}")
                        self.idle = True
                    self.nap(now() + timedelta(minutes=1), answers=True)
                    continue
                if self.a.replan and self.plans < self.a.replan:
                    self.plans += 1
                    with self.critical():  # its session dies at its next poll on a signal; the merge finishes
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
            while self.n == 1 and self.threads[-1].is_alive():
                self.threads[-1].join(NAP)
                self.poll_questions()

    def self_update(self):
        """Pulls the windvane install between tickets (nothing running) and, when the driver's own code changed,
        restarts it in place with the deadline as it now is. Each reason to leave the install alone is logged once."""
        if self.home is None or (time.time() - self.pulled[0] < PULL_EVERY and self.pulled[1] == len(self.merged)):
            return
        self.pulled = (time.time(), len(self.merged))
        said = []
        with self.critical():
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
            # The lock stays open: its fd closes on exec (Python's are non-inheritable), so the restarted driver takes
            # it again, and an exec that fails leaves the lock held for this run's own teardown.
            os.environ["WINDVANE_RESTARTED"] = "1"  # no second "windvane started" push; it reads restart.json
            cmd = [*exe(), *selfupdate.restart_args(self.argv, self.end())]
            try:
                self.execv(cmd[0], cmd)
            except OSError as e:  # a moved or broken interpreter: the run ends with why (exec's own error names no file)
                os.environ.pop("WINDVANE_RESTARTED", None)  # the teardown's helpers aren't a restarted driver's
                raise RuntimeError(f"windvane couldn't restart on its new code ({cmd[0]}: {e.strerror.lower()}); "
                                   "run `windvane doctor`, then start the run again") from e

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
        self.lockfile = open(self.p.state / "lock", "a+")  # not "w": that would empty a live run's pid
        until = time.time() + statedir.WAIT
        while True:
            try:
                fcntl.flock(self.lockfile, fcntl.LOCK_EX | fcntl.LOCK_NB)
                (self.p.state / statedir.CLEANING).unlink(missing_ok=True)  # one a killed doctor left
                self.lockfile.seek(0)
                left = self.lockfile.read().strip()  # a run's end empties it: a pid here ended without its teardown
                self.left = left if left != str(os.getpid()) else ""  # (a self-update's restart keeps the pid)
                self.lockfile.truncate(0)
                self.lockfile.write(str(os.getpid()))
                self.lockfile.flush()
                return
            except OSError:  # a run, or `doctor --clean` for a few seconds (it may be removing a slot's tree)
                if not (self.p.state / statedir.CLEANING).exists() or time.time() > until:
                    self.lockfile.close()
                    raise SystemExit("windvane: the autopilot is already running for this project")
                time.sleep(0.2)

    def run(self, deadline):
        """Works tickets until a stop; False when it couldn't start (a failed setup)."""
        p = self.p
        began = util.clock()  # with isolate off, a process in the checkout older than this is the user's
        self.take_lock()
        phone.start_run(p)
        try:  # an older install's per-project `status` hooks, a deleted repo's, and this one's back if they went
            install.tidy_phone_hooks(p)
        except (OSError, install.Unreadable) as e:
            self.log(f"phone hooks: not tidied ({e})")
        restarted = os.environ.pop("WINDVANE_RESTARTED", None)
        from . import schedule  # (it imports this module)
        if not (restarted or schedule.called_off(p)):  # a stop asked for while it restarted, or started, holds
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
        if self.left and not restarted and p.cfg["teardown"]:  # we hold its lock now, so that driver is gone
            self.log(f"the last run (pid {self.left}) ended without its teardown (killed?): running it before setup")
            tear_down(p)  # a failure is named in the "started" push
        self.clean_tmp(between=False)
        ready, crashed = False, None
        handlers, streams = self.catch_signals(), (sys.stdout, sys.stderr)
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
            stopped_first = ready and not restarted and (p.state / "STOP").exists()
            if stopped_first:  # a stop as it came up (a schedule's window called off as it starts): nothing is spent
                with self.critical():  # but a held plan, paid for already, still goes out; with STOP still there
                    push_held(p)  # an outage wait ends at once
                self.stop_reason = self.stop_check()
                if st := schedule.state(p):  # which window, and that no other follows
                    ends = "; the schedule ends: no later window starts" if st["daily"] else ""
                    self.stop_reason = (f"asked to stop as this window ({schedule.what(st['daily'], st['end'])}) "
                                        f"started; nothing ran{ends}")
            elif ready and not restarted:
                self.usage.probe(p)
                push("windvane started", f"Until {hhmm(deadline)}. {self.usage.text()}.{failed_teardown(p)}", 2,
                     "rocket", ("Status=status", "Stop=stop"))
                (p.state / TEARDOWN_FAILED).unlink(missing_ok=True)  # said once
            elif not ready:
                self.stop_reason = self.halt or self.stop_check()
                if self.stop_reason == "deadline" and not restarted:  # cut short during setup
                    self.stop_reason = "deadline reached during setup; nothing ran"
            if ready and not stopped_first:
                with self.critical():
                    push_held(p)  # a plan a failed push kept locally, once a run: the network may be back
                self.loop()
        except Signalled:
            pass  # on_signal set the stop reason
        except Exception as e:
            self.stop_reason = f"driver error: {e}"
            crashed = traceback.format_exception_only(e)[-1].strip()
            raise
        finally:
            self.ending = True
            for t in self.threads:  # the running tickets finish (their runs stop at the deadline at the latest)
                join(t)
            self.clean_state(between=False)  # every slot's thread is done: its trees are idle
            if not (crashed and os.environ.get(SUPERVISED)):  # a restart goes on with them
                self.kept_at_end()
            if not quiet_now(p)[0]:
                self.send_unsent()  # a question from a ticket that finished after the loop's last pass
            summary = self.summary()
            self.log(summary.replace("\n", " "))
            if crashed and os.environ.get(SUPERVISED):  # `supervise` restarts it, or pushes why it doesn't
                self.hand_over()
                rest = "Merged:" + summary.split("\nMerged:", 1)[1]  # past the why, which may run on to a 2nd line
                save_json(p.state / "crash.json", {"error": crashed, "summary": rest})
            elif not ready and self.halt:  # just why, once: nothing ran, so the summary's other lines say nothing
                push("windvane couldn't start", whole("", self.halt[0].upper() + self.halt[1:], ".", log=self.log), 4,
                     "warning")
            else:
                push("windvane stopped", summary, 4 if self.failed or self.questions or self.halt else 3,
                     "checkered_flag", ("Usage=usage",))
            self.save()
            # After the push (a hung teardown can't hold it up) and under the lock (the next run's helpers are safe).
            # A crash `supervise` restarts goes on with this run's helpers; it tears down when it gives up.
            if not (crashed and os.environ.get(SUPERVISED)):
                tear_down(p, tell=True)
                self.lockfile.truncate(0)  # torn down: the next run has nothing left to tear down
                report_leftovers(p, began)
            fcntl.flock(self.lockfile, fcntl.LOCK_UN)  # now, not at the close: a thread's fork may still have the fd
            self.lockfile.close()
            for sig, h in handlers.items():
                signal.signal(sig, h)
            if sys.stdout is not streams[0]:  # on_signal's /dev/null
                sys.stdout.close()
            sys.stdout, sys.stderr = streams
        return ready

    def catch_signals(self):
        """SIGTERM and SIGHUP end the run as `stop --now` does, and so does Ctrl-C inside a guarded step (main thread
        only: a run in a test's thread keeps Python's handlers). Returns the handlers they replace."""
        if threading.current_thread() is not threading.main_thread():
            return {}
        return {sig: signal.signal(sig, self.on_signal) for sig in SIGNALLED}

    def on_signal(self, sig, frame):
        """The first SIGTERM or SIGHUP: every session is killed at its next poll, and the main thread unwinds to
        `run`'s `finally`, unless it's there already. A second one changes nothing: the wind-down is under way.
        Ctrl-C outside a guarded step is Python's own KeyboardInterrupt, at once, as without this handler."""
        if sig == signal.SIGINT and not self.guarded:
            raise KeyboardInterrupt
        if self.signalled:
            return
        self.signalled, self.kill_all = sig, True
        if sig == signal.SIGHUP:  # its terminal is gone: a print there fails (EIO) and would cost the run its end
            sys.stdout = sys.stderr = open(os.devnull, "w")
        log(self.p, f"{SIGNALLED[sig]}: stopping now")
        if not self.ending:
            self.stop_reason = f"{SIGNALLED[sig]}; the running sessions were killed"
            if self.guarded:
                log(self.p, "  the step under way (a merge, a push, a pull) finishes first")
                return
            raise Signalled(sig)

    @contextmanager
    def critical(self):
        """A main-thread step that changes git state or the remote (a merge, a push, a pull, recording what it did):
        a SIGTERM or SIGHUP inside it kills the sessions at once (`on_signal`) but unwinds the run only when the step
        ends, so it never leaves a half-done merge, an index.lock or a pushed branch with nothing recorded (Ctrl-C too:
        its commands get a process group of their own, `util.shielded`, out of the terminal's reach). Bounded by
        the steps' own network timeouts; an outage wait ends on the signal (`wait_online`). Off the main thread (a
        slot's merge) it's no guard: signals never land there, and `kill_all` stops those threads."""
        if threading.current_thread() is not threading.main_thread():
            yield
            return
        self.guarded += 1
        util.shielded, error = True, None
        try:
            yield
        except Exception as e:
            error = e
            raise
        finally:
            self.guarded -= 1
            util.shielded = bool(self.guarded)
            if not self.guarded and self.signalled and not self.ending:
                if error:  # Signalled replaces it: its text goes in the log and the stopped push's why
                    said = traceback.format_exception_only(error)[-1].strip()
                    self.log(f"  the step under way failed: {said}")
                    self.stop_reason += f"; the step under way failed: {said}"
                raise Signalled(self.signalled)


    def summary(self):
        """The run's last push: what merged and what still needs the user (questions, provisional answers, merges
        the critic only let through because its revisions ran out)."""
        from . import schedule  # (it imports this module)
        p = self.p
        names = lambda ids: "; ".join(map(self.name, ids)) or "none"
        crit = ", ".join(f"{t} {critic.trail(critic.first_score(p, t), s)}" for t, s in self.scores.items())
        under = [t for t in self.merged if (h := critic.history(p, t)) and h[-1].get("step") == "accept"]
        picks = [(q["ticket"], f"{q['built'] or '?'}: {q['option']}" if q["option"] else q["built"] or "?")
                 for q in provisional(gout(p.main_root(), "show", f"{p.upstream}:{p.questions_rel}"))]
        side = (f"Side by side: up to {self.peak} at once, {self.conflicts} conflict bounce"
                f"{'s' * (self.conflicts != 1)}.\n" if self.n > 1 else "")
        return (whole("Why: ", self.stop_reason, ".", log=self.log) + f"\nMerged: {names(self.merged)}.\n"
                + (f"Merged under the critic's bar (out of revisions): {names(under)}.\n" if under else "")
                + (f"Desk PRs merged: {', '.join('#' + n for n in self.desk_merged)}.\n" if self.desk_merged else "")
                + (f"Critic (/10, first try→final): {crit}.\n" if crit else "") + side
                + (f"Held for you (parked in QUEUE.md): {names(self.held)}.\n" if self.held else "")
                + f"Waiting on you: {names(sorted(tickets(self.questions)))}.\n"
                + (f"Provisional answers to check: {'; '.join(f'{q[0]} (built {q[1]})' for q in picks[:6])}"
                   + (f" and {len(picks) - 6} more" if len(picks) > 6 else "") + ". "
                   "To change one: windvane answer <ID> <letter>.\n" if picks else "")
                + f"Failed: {names(self.failed)}.\n"
                + (f"Stopped at the deadline (resumes next run): {names(self.stopped)}.\n" if self.stopped else "")
                + self.kept_note
                + "".join(f"Set aside: {self.name(t)} ({why}).\n" for t, why in self.aside.items())
                + (f"{self.usage.text()}." if self.usage.five is not None else "")
                + (f"\nwindvane {self.newer} is out: pipx upgrade windvane." if self.newer else "")
                + schedule.next_line(p))


def join(t):
    """`t.join()`, also after a signal interrupted an earlier join of it (the main loop's): Python 3.11 and 3.12 then
    mark the thread ended while it still runs, so join returns at once and the teardown would run beside its session.
    A thread is in `threading.enumerate()` until it really ends."""
    t.join()
    while t in threading.enumerate():
        time.sleep(0.05)


def supervise(p, argv, deadline, start=None, sleep=time.sleep):
    """`run --detach`'s loop around the driver (`windvane <argv>`): a driver that dies of an exception is restarted
    with the run's deadline as it now is and what it had done (`carry_over`), up to RESTARTS times an hour; each
    restart is logged and pushed with the error. A stop, the deadline, an empty queue or a failed setup end the run
    as they end the driver. A driver killed by the system (OOM) is only pushed: its sessions, in process groups of
    their own, may still be at work in the worktrees a restart would resume. Returns the driver's last exit code."""
    start = start or (lambda args, env: subprocess.run([*exe(), *args], env=env).returncode)
    began = util.clock()
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
                push("windvane stopped", whole("Why: ", why, ". Not restarted.", log=lambda m: log(p, m))
                     + "\nIts sessions may still be running in the worktrees.", 4, "rotating_light", ("Status=status",))
                tear_down(p, lock=True, tell=True)  # its `finally` never ran (a SIGTERM's or SIGHUP's did: it tore down itself)
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
            push("windvane restarted", whole("The driver crashed: ", why,
                                            f". Restart {len(crashes)} of {RESTARTS} this hour.",
                                            log=lambda m: log(p, m))
                 + f"\nIt goes on until {hhmm(deadline)}.", 3, "repeat", ("Status=status",))
            sleep(RESTART_PAUSE)
            stop = asked()
        if stop:
            log(p, f"driver crashed ({why}); not restarted: {stop}")
            push("windvane stopped", whole("Why: the driver crashed (", why, f"); not restarted: {stop}.",
                                          log=lambda m: log(p, m)) + "\n"
                 + info["summary"], 4, "rotating_light", ("Usage=usage",))
            tear_down(p, lock=True, tell=True)  # after the push, as the driver's own end does
            if not locked(p):  # its sessions ended with it (a killed one's may not have: not checked there); a run
                report_leftovers(p, began)  # started since has sessions of its own there
            return rc
        args = selfupdate.restart_args(args, deadline)
        env["WINDVANE_RESTARTED"] = "1"


def tear_down(p, lock=False, tell=False):
    """The project's `teardown` at a run's real end; nothing in it may cost the run its end (the lock, the state).
    `lock`: `supervise` takes the run's lock for it, as the dead driver no longer holds it, so a run started meanwhile
    keeps its helpers, and empties the dead driver's pid there once done (`take_lock`). `tell`: a failure gets a push
    of its own, as the run's "stopped" one has gone out already (a hung teardown mustn't hold it up)."""
    if not p.cfg["teardown"]:
        return
    try:
        with open(p.state / "lock", "a") if lock else nullcontext() as fh:
            if lock:
                try:
                    fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError:
                    return log(p, "teardown not run: a new run has started and uses the helpers")
            teardown(p)
            if lock:
                fh.truncate(0)
    except Exception as e:  # noqa: BLE001 (a git or OS error: the run is over either way)
        log(p, f"teardown `{p.cfg['teardown']}` not run: {e}")
        worktree.failed(p, p.cfg["teardown"], "not run", str(e))
    if tell and (f := load_json(p.state / TEARDOWN_FAILED, None)):
        if f.get("told"):
            return
        if push("windvane teardown failed", whole(f"Its helpers may still be up ({f['how']}). Run it by hand: ",
                                                  f["cmd"], log=lambda m: log(p, m)), 4, "warning"):
            save_json(p.state / TEARDOWN_FAILED, {**f, "told": True})  # the next "started" push needn't say it again


def leftover_helpers(p, since):
    """[(pid, command)] of the processes still at work in the autopilot's trees once a run is over: the helpers its
    tools started (an emulator, a dev server, a container) that its `teardown` doesn't stop. With isolate off the tree
    is the checkout, where the user works too: only what started after `since` with no terminal counts, so their
    editor and shells never do."""
    if p.cfg["isolate"]:
        roots = [os.path.realpath(p.state / d) for d in util._listdir(p.state) if re.fullmatch(r"(work|ci)(-\d+)?", d)]
    else:
        roots = [os.path.realpath(p.main_root())]
    out = []
    for pid, cwd in sorted(util.cwds().items()):
        if pid in (os.getpid(), os.getppid()) or not any(cwd == r or cwd.startswith(r + os.sep) for r in roots):
            continue
        if not (info := util.command(pid)):
            continue
        if not p.cfg["isolate"] and (info[1] or (util.started_at(pid) or 0) < since):
            continue
        out.append((pid, info[0]))
    return out


def report_leftovers(p, since):
    """Names what a run's end left running (`leftover_helpers`) in the log and one push, never kills it: the driver
    can't tell a helper from something the user started there. The push says how to stop them next time."""
    try:
        left = leftover_helpers(p, since)
    except Exception as e:  # noqa: BLE001 (a report: the run is over either way)
        return log(p, f"left running: not checked ({e})")
    if not left:
        return
    for pid, cmd in left:
        log(p, f"left running after the run: pid {pid}: {cmd[:80]}")
    say = lambda m: log(p, m)  # noqa: E731
    names = (", ".join(f"{cut(cmd, 120, log=say)} (pid {pid})" for pid, cmd in left[:3])  # a JVM's is pages long
             + f", {len(left) - 3} more" * (len(left) > 3))
    where = "the autopilot's worktrees" if p.cfg["isolate"] else "the checkout, started by the run"
    fix = ("Its `teardown` in .windvane/config.toml missed them." if p.cfg["teardown"]
           else "Set `teardown` in .windvane/config.toml to stop them at a run's end.")
    push("windvane left helpers running", whole(f"Still running in {where}: ", names, ". " + fix, log=say), 3,
         "warning")


def failed_teardown(p):
    """The "started" push's line for the last run's failed teardown (worktree.teardown records it), or "" when there
    was none or its own push told it already."""
    f = load_json(p.state / TEARDOWN_FAILED, None)
    if not f or f.get("told"):
        return ""
    return whole("\nLast run's teardown failed: ", f"{f['cmd']} ({f['how']})", "; its helpers may still be up.",
                 log=lambda m: log(p, m))


def locked(p):
    """Whether a driver holds this project's lock, foreground or detached: the lock goes when its process does.
    `doctor --clean` holds it for a few seconds too (with CLEANING): that's no run, and a run starting then waits."""
    f = p.state / "lock"
    if not f.exists():
        return False
    with open(f) as fh:
        return util.held(fh) and not (p.state / statedir.CLEANING).exists()
