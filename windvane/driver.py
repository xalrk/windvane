"""The autopilot driver: works a project's tickets unattended until a deadline, one fresh headless Claude session
per ticket, in its own worktree so the user's checkout is never touched.

Per ticket: a worker run builds it (and has the critic judge it before the check), then the driver itself runs the
project's check on the exact head (reusing the worker's pass for the same tree), a fresh read-only reviewer reads the
change, and the driver merges it after checking the merged tree too. Anything that fails a stage goes back to the
ticket's next worker run as written feedback. Design questions go to the user's phone; in their quiet times runs
build their recommendation and record it instead.

Stops at the deadline, `windvane stop`, two failed tickets in a row, an empty queue, the weekly cap, or sessions that
keep dying (API errors, crashes; each is retried after a growing pause first). At the 5-hour cap it stops the running
session and pauses until the window resets.
"""
import fcntl
import threading
import re
import shutil
import subprocess
import time
from datetime import datetime, timedelta

from . import critic
from .config import HOME
from .jobs import check, tail
from .questions import (desk_answers, give_answer, mode, parts, phone_answer, provisional, quiet_now, section)
from .queue import Queue
from .runner import Usage, run_claude
from .util import git, gout, hhmm, last_match, load_json, log, now, push, save_json, sh

RESULT_RE = re.compile(r"RESULT: (READY|PARTIAL|BLOCKED|FAILED) (\S+)[ \t]*(.*)")
REVIEW_RE = re.compile(r"REVIEW: (APPROVE|CHANGES) (\S+)[ \t]*(.*)")
PLAN_RE = re.compile(r"PLAN: (?:new=(\d+) noted=(\d+)|NONE)[ \t]*(.*)")


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


def overlaps(paths, others):
    """Whether two Touches lists share a file, or one names a directory holding the other's file."""
    for a in paths:
        for b in others:
            a_, b_ = a.rstrip("/"), b.rstrip("/")
            if a_ == b_ or a_.startswith(b_ + "/") or b_.startswith(a_ + "/"):
                return True
    return False


def ensure_worktree(p, path):
    """The autopilot's own worktree at `path`, created detached at upstream (and set up) when missing."""
    if not p.cfg["isolate"] or (path / ".git").exists():
        return
    root = p.main_root()
    git(root, "fetch", "-q", p.remote)
    git(root, "worktree", "prune")
    r = git(root, "worktree", "add", "--detach", str(path), p.upstream)
    if r.returncode:
        raise SystemExit(f"windvane: can't create the worktree {path}: {r.stderr.strip()}")
    if p.cfg["setup"]:
        subprocess.run(p.cfg["setup"], shell=True, cwd=path)


def clean_detach(p, cwd, rev):
    """Put the driver's own worktree on `rev`, discarding anything left there. Never used on the worker's tree."""
    git(cwd, "merge", "--abort")
    git(cwd, "reset", "-q", "--hard")
    git(cwd, "clean", "-qfd")
    return git(cwd, "switch", "-q", "--detach", rev).returncode == 0


def pr_for(p, branch):
    if p.cfg["forge"] != "github" or not branch:
        return ""
    return sh(p.main_root(), "gh", "pr", "list", "--head", branch, "--state", "open", "--json", "number",
              "-q", ".[0].number")


class Pilot:
    def __init__(self, p, a):
        self.p, self.a = p, a
        self.usage = Usage(p)
        self.state_file = p.state / "state.json"
        s = load_json(self.state_file, {})
        self.feedback = s.get("feedback", {})  # ticket -> what its next worker run must act on
        self.questions = s.get("questions", {})  # ticket -> its open question for the user
        self.costs = s.get("costs", [])  # 5-hour-window points each ticket took
        self.skip, self.merged, self.failed, self.held = set(), [], [], []
        self.partials, self.reviews, self.scores = {}, {}, {}
        self.fail_streak, self.plans, self.was_quiet, self.idle = 0, 0, False, False
        self.crashes, self.halt, self.unfinished = 0, None, set()  # sessions dead in a row; why the run must stop
        self.sleep = time.sleep  # the retry backoff's; tests make it instant
        self.stop_reason = "unknown"
        self.worker_rules, self.review_rules = rules(p, "WORKER.md"), rules(p, "REVIEW.md")
        # Parallel slots: each has its own work and ci worktrees. Merges and fetches are serialized.
        self.n = max(1, getattr(a, "parallel", None) or p.ap["parallel"]) if p.cfg["isolate"] else 1
        self.tl = threading.local()
        self.lock, self.merge_lock, self.git_lock = threading.RLock(), threading.Lock(), threading.Lock()
        self.running = {}  # ticket -> slot
        self.owner = {i: None for i in range(self.n)}  # slot -> the ticket whose uncommitted work its tree holds
        self.threads, self.wake = [], threading.Event()

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
            git(self.p.main_root(), "fetch", "-q", "--prune", self.p.remote)

    def save(self):
        with self.lock:
                save_json(self.state_file, {"feedback": dict(self.feedback), "questions": dict(self.questions),
                                        "costs": self.costs[-10:]})

    def log(self, msg):
        log(self.p, msg)

    def name(self, tid):
        t = self.queue.titles.get(tid, "") if hasattr(self, "queue") else ""
        return f"{t[:45]} ({tid})" if t else tid

    def stopper(self, kill_at):
        def stop_if():
            if now() >= kill_at:
                return "time"
            u = self.usage
            if u.five is not None and u.five >= self.a.cap:
                return "5-hour cap"
            if u.week is not None and u.week >= self.a.week_cap:
                return "weekly cap"
            return "usage limit" if u.rejected else None
        return stop_if

    def claude(self, role, label, prompt, system, cwd, kill_at, allow=(), deny=(), ticket=""):
        text, killed, error = run_claude(self.p, role, label, prompt, system, cwd, allow, deny, self.usage,
                                         self.stopper(kill_at), ticket)
        if killed:
            self.log(f"  stopped {label}: {killed} ({self.usage.text()})")
        elif not error:
            self.crashes = 0
        return text, killed, error

    def crashed(self, label, error, deadline):
        """A session died for a reason outside its ticket (API overload, network, not logged in): pause, longer each
        time, and return True to run it again. Too many in a row stop the run instead of failing ticket after ticket
        on a broken API; nothing is pushed until then."""
        self.crashes += 1
        n, wait = self.p.ap["retries"], self.p.ap["retry_minutes"] * 2 ** (self.crashes - 1)
        if self.crashes > n:
            self.halt = f"{self.crashes} sessions in a row died ({error})"
        elif now() + timedelta(minutes=wait) >= deadline - timedelta(minutes=self.a.min_slot):
            self.halt = f"a session died ({error}), and there's no time left to retry it"
        if self.halt:
            self.log(f"  {label}: {self.halt}")
            return False
        self.log(f"  {label} died ({error}); retry {self.crashes}/{n} in {wait:g} min")
        for _ in range(round(wait)):
            if (self.p.state / "STOP").exists():
                return False
            self.sleep(60)
        return True

    # ---------- one ticket

    def work(self, tid, kind, branches, deadline):
        p, wd = self.p, self.wd
        start_five = self.usage.five
        kill_at = min(deadline, now() + timedelta(hours=self.a.ticket_hours))
        wrap = kill_at - timedelta(minutes=20)
        ref = f"{p.remote}/{branches[0]}" if branches else p.upstream
        ticket = gout(wd, "show", f"{ref}:{p.tickets_rel}/{tid}.md") or self.queue.text.get(tid, "")
        dirty = gout(wd, "status", "--porcelain")
        here = gout(wd, "rev-parse", "--abbrev-ref", "HEAD")
        fb = self.feedback.get(tid)
        pr = pr_for(p, branches[0]) if branches else ""
        prompt = "\n\n".join(filter(None, [
            f"Ticket {tid}. Follow the worker rules in your system prompt and end with the RESULT line.",
            f"## The ticket\n{ticket[:8000]}",
            f"## Act on this first\n{fb}" if fb else "",
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
        text, killed, error = self.claude("worker", tid, prompt, self.worker_rules, wd, kill_at, allow, deny, tid)
        if error:
            self.crashed(tid, error, deadline)  # the loop picks the ticket again, with its branch as it now is
        else:
            self.handle(tid, text, killed, deadline)
        if start_five is not None and self.usage.five is not None and self.usage.five >= start_five:
            self.costs.append(round(self.usage.five - start_five, 1))
        self.save()

    def handle(self, tid, text, killed, deadline):
        if killed:
            if killed == "time" and now() < deadline - timedelta(minutes=5):
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
        self.after(tid, branch, sha, deadline)

    def unfinished_run(self, tid, branch):
        """A run that ended normally but without its RESULT line may have done all the work: one more run, told
        where its branch is, can finish it. Twice in a row fails the ticket."""
        if tid in self.unfinished:
            return self.fail(tid, "the run ended without a result line, twice")
        self.unfinished.add(tid)
        head = gout(self.wd, "rev-parse", "--short=12", "HEAD")
        old = self.feedback.get(tid, "")
        self.feedback[tid] = (f"Your last run ended without the RESULT line for {tid}. The worktree is on "
                              f"`{branch or 'a detached HEAD'}` at {head}: finish the rules' steps still left from "
                              "there, then end with the RESULT line."
                              + (f"\n\nWhat that run had to act on:\n{old}" if old and "RESULT line" not in old[:80]
                                 else ""))
        self.log(f"{tid}: the run ended without a result line; one more run to finish it")

    def after(self, tid, branch, sha, deadline):
        """A READY head: critic gate, check, review, merge. Each failed stage writes the next run's feedback."""
        p = self.p
        self.fetch()
        if gout(p.main_root(), "rev-parse", "--verify", "-q", f"{sha}^{{commit}}") == "" or \
                (branch and gout(p.main_root(), "rev-parse", f"{p.remote}/{branch}") != sha):
            self.feedback[tid] = (f"Your RESULT line named {sha[:12]}, but `{p.remote}/{branch or '?'}` isn't at it. "
                                  "Push the final head, then finish READY with its full sha.")
            return self.log(f"{tid}: READY head {sha[:12]} isn't pushed; back to the worker")
        if not self.critic_gate(tid, sha, deadline):
            return
        ok, summary, logf = self.check_at(sha)
        self.log(f"{tid}: check on {sha[:7]}: {summary}")
        if not ok:
            self.feedback[tid] = f"The check fails on {sha[:12]}: {summary}\n```\n{tail(logf, 40)}\n```"
            return self.bounce(tid)
        verdict, review = self.review(tid, branch, sha, deadline)
        if verdict is None:
            return
        if verdict == "CHANGES":
            self.feedback[tid] = f"The reviewer asks for changes on {sha[:12]}:\n{review[-4000:]}"
            self.reviews[tid] = self.reviews.get(tid, 0) + 1
            if self.reviews[tid] >= p.ap["review_rounds"]:
                return self.fail(tid, f"still not through review after {self.reviews[tid]} rounds")
            return
        self.integrate(tid, branch, sha)

    def bounce(self, tid):
        """A stage sent the ticket back: count it like a review round, so a ticket can't loop forever."""
        self.reviews[tid] = self.reviews.get(tid, 0) + 1
        if self.reviews[tid] >= self.p.ap["review_rounds"] + 1:
            self.fail(tid, f"sent back {self.reviews[tid]} times: {self.feedback.get(tid, '')[:200]}")

    def critic_gate(self, tid, sha, deadline):
        p = self.p
        if not p.critic["enabled"]:
            return True
        past = critic.history(p, tid)
        if past and past[-1]["step"] in ("revise", "rethink") and past[-1].get("sha") and \
                not sha.startswith(past[-1]["sha"]):
            past = []  # judged an older head: this one needs its own verdict
        if not past or past[-1]["step"] not in critic.DONE:
            if critic.pack(p, tid)[0] is None:
                self.feedback[tid] = ("You finished READY without the critic: write the evidence pack and run "
                                      f"`windvane critic {tid}` (rules step 6) before the check.")
                self.log(f"{tid}: READY without a critic pack; back to the worker")
                self.bounce(tid)
                return False
            self.log(f"{tid}: critic run (the worker didn't finish it)")
            entry, text = critic.judge(p, tid, self.wd, sha, self.usage,
                                       self.stopper(min(deadline, now() + timedelta(minutes=30))))
            if entry is None:
                self.log(f"{tid}: the critic gave no verdict; on to the check")  # a help, never a gate on its own failure
                return True
            past = [entry]
        last = past[-1]
        score = last.get("score")
        self.log(f"{tid}: critic {'-' if score is None else f'{score:g}'}/10 ({last['step']}): {last.get('gist', '')}")
        if last["step"] in ("revise", "rethink"):
            self.feedback[tid] = (f"The critic scored your head {score:g}/10 ({last['step']}). Fix its \"Fix in this "
                                  f"ticket\" points, refresh the pack and run `windvane critic {tid}` again:\n"
                                  f"{last.get('critique', '')}")
            self.bounce(tid)
            return False
        if score is not None:
            self.scores[tid] = score
        return True

    def check_at(self, rev):
        cd = self.cd
        if not clean_detach(self.p, cd, rev):
            return False, f"couldn't check out {rev[:12]}", None
        return check(self.p, cd)

    def review(self, tid, branch, sha, deadline):
        p, cd = self.p, self.cd
        pr = pr_for(p, branch)
        body = sh(p.main_root(), "gh", "pr", "view", pr, "--json", "body", "-q", ".body") if pr else \
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
        self.log(f"{tid}: review run")
        while True:
            text, killed, error = self.claude("reviewer", f"{tid}-review", prompt, self.review_rules, cd,
                                              min(deadline, now() + timedelta(hours=1)),
                                              ["Bash(git diff *)", "Bash(git show *)", "Bash(git log *)"],
                                              REVIEW_DENY, tid)
            if not error or not self.crashed(f"{tid}-review", error, deadline):
                break
        if killed or error:
            return None, ""
        m = last_match(REVIEW_RE, text)
        if not m:
            self.fail(tid, "the review run ended without a verdict")
            return None, ""
        self.log(f"{tid}: review {m.group(1).lower()}: {m.group(3)}")
        if pr:
            sh(p.main_root(), "gh", "pr", "comment", pr, "--body",
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
            self.fetch()
            clean_detach(p, cd, p.upstream)
            author = p.cfg["author"]
            ident = (["-c", f"user.name={author.split('<')[0].strip()}", "-c",
                      f"user.email={author.split('<')[1].rstrip('>').strip()}"] if "<" in author else [])
            msg = f"Merge {tid}: {title} (branch {branch})"
            r = git(cd, *ident, "merge", "--no-ff", "-m", msg, sha)
            if r.returncode:
                git(cd, "merge", "--abort")
                self.feedback[tid] = (f"Your approved head {sha[:12]} conflicts with {p.upstream}: `git merge "
                                      f"{p.upstream}` into your branch, resolve the conflicts keeping both sides' "
                                      "intent, rerun the check, push, and finish READY.")
                self.log(f"{tid}: conflicts with {p.main}; back to the worker")
                return self.bounce(tid)
            ok, summary, logf = check(p, cd)
            if not ok:
                self.feedback[tid] = (f"Merged with the current {p.upstream}, your head {sha[:12]} fails the check: "
                                      f"{summary}\n```\n{tail(logf, 40)}\n```\nMerge {p.upstream} into your branch "
                                      "and fix it.")
                self.log(f"{tid}: the merged tree fails the check; back to the worker")
                return self.bounce(tid)
            if p.cfg["forge"] == "github":
                pr = pr_for(p, branch)
                if not pr:
                    self.feedback[tid] = "No open PR for your branch: open it (`gh pr create`) and finish READY."
                    return self.bounce(tid)
                res = subprocess.run(["gh", "pr", "merge", pr, "--merge", "--match-head-commit", sha, "--subject",
                                      msg], cwd=p.main_root(), capture_output=True, text=True)
                merged = res.returncode == 0 or sh(p.main_root(), "gh", "pr", "view", pr, "--json", "state",
                                                   "-q", ".state") == "MERGED"
            else:
                res = git(cd, "push", "-q", p.remote, f"HEAD:{p.main}")
                merged = res.returncode == 0
            if merged:
                return self.merged_ok(tid, branch, sha)
            self.log(f"{tid}: merge refused ({' '.join((res.stderr or res.stdout).split())[:200]}); trying again")
        self.fail(tid, "the merge was refused three times")

    def merged_ok(self, tid, branch, sha):
        p = self.p
        if branch:
            git(p.main_root(), "push", "-q", p.remote, "--delete", branch)
        self.fetch()
        wd = self.wd
        if not gout(wd, "status", "--porcelain"):
            git(wd, "switch", "-q", "--detach", p.upstream)
            if branch:
                git(wd, "branch", "-q", "-D", branch)
        self.merged.append(tid)
        self.fail_streak = 0
        self.feedback.pop(tid, None)
        self.save()
        score = self.scores.get(tid)
        self.log(f"{tid}: merged {sha[:7]} · {self.usage.text()}")
        push(f"Done: {self.name(tid)}", "Built, checked, reviewed and merged."
             + (f" Critic {score:g}/10." if score is not None else "") + f" {self.usage.text()}", 2, "white_check_mark")
        picks = [q for q in provisional(gout(p.main_root(), "show", f"{p.upstream}:{p.questions_rel}"))
                 if p.norm(q["ticket"]) == tid]
        if picks:
            push(f"Provisional: {self.name(tid)}", "\n".join(f"{q['question']} Built {q['built']}: {q['option']}"
                                                             for q in picks)
                 + f"\nTo change one: windvane answer {tid} <letter>, or tell a Claude session.", 3, "memo")

    def fail(self, tid, why):
        self.skip.add(tid)
        self.failed.append(tid)
        self.fail_streak += 1
        self.log(f"{tid}: FAILED: {why}")
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
        if quiet_now(p)[0]:
            self.held.append(tid)  # only money, licence and merged-work questions ask in a quiet time
        else:
            self.send_question(tid)
        self.save()

    def send_question(self, tid):
        q = self.questions[tid]
        q["asked"] = time.time()
        wait = "" if q["must_wait"] or not q["rec"] else (
            f" No answer in {self.a.answer_wait:g} min: I go with {q['rec']} and mark it to revisit.")
        if shutil.which("notify-ask"):
            subprocess.run(["notify-ask", self.p.qid(tid), f"{self.name(tid)}: {q['q']}{wait}",
                            *[q["opts"][k] for k in sorted(q["opts"])]], capture_output=True)
        else:
            self.log(f"{tid}: no notify-ask; answer at the desk with `windvane answer {tid} <letter>`")

    def poll_questions(self):
        p = self.p
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
        if err and not getattr(self, "quiet_err_sent", False):
            self.quiet_err_sent = True
            self.log(f"quiet times: {err}; asking as usual")
        if self.was_quiet and not end:
            self.log("quiet time over")
            for tid in self.held:
                if tid in self.questions:
                    self.send_question(tid)
            self.held = []
            picks = provisional(gout(self.p.main_root(), "show", f"{self.p.upstream}:{self.p.questions_rel}"))
            if picks:
                push(f"Review: {len(picks)} provisional answer{'s' * (len(picks) > 1)}",
                     "\n".join(f"{q['ticket']}: {q['question']} Built {q['built']}." for q in picks[:8]), 3, "memo")
        self.was_quiet = bool(end)

    # ---------- the loop

    def est(self):
        recent = self.costs[-3:]
        return max(3.0, sum(recent) / len(recent)) if recent else 8.0

    def wait_for_reset(self, deadline):
        until = datetime.fromtimestamp(self.usage.five_reset or time.time() + 3600) + timedelta(minutes=2)
        if until >= deadline - timedelta(minutes=self.a.min_slot):
            self.stop_reason = f"5-hour window at {self.usage.five:g}%, and it resets too close to the deadline"
            return False
        self.log(f"pausing at {self.usage.text()} until the window resets at {hhmm(until)}")
        push(f"{self.p.name}: paused", f"{self.usage.text()}. Resuming at {hhmm(until)}.", 2, "pause_button",
             ("Usage=usage", "Stop=stop"))
        while now() < until and not (self.p.state / "STOP").exists():
            time.sleep(60)
        self.usage.probe(self.p)
        return True

    def stop_check(self, deadline):
        """Why no new ticket may start (the run winds down), or None."""
        p = self.p
        if (p.state / "STOP").exists():
            (p.state / "STOP").unlink()
            return "asked to stop"
        if self.halt:
            return self.halt
        if now() >= deadline - timedelta(minutes=self.a.min_slot):
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

    def pick(self, free):
        """(ticket, kind, branches, slot) to start in one of the `free` slots, or (None, why, None, None). Tickets
        run side by side only when their `Touches:` lists don't overlap; one without the line runs alone."""
        q = self.queue
        busy = set(self.running)
        avoid = set(self.skip) | busy
        if busy and any(not q.touches.get(t) for t in busy):
            avoid |= set(q.order)  # something running may touch anything: nothing starts beside it
        touching = set().union(*(q.touches.get(t, set()) for t in busy)) if busy else set()
        for t in q.order:
            if busy and (not q.touches.get(t) or overlaps(q.touches[t], touching)):
                avoid.add(t)
        while True:
            tid, *rest = q.pick(avoid, set(self.questions))
            if tid is None:
                return None, rest[0], None, None
            own = next((i for i, o in self.owner.items() if o == tid), None)
            slot = own if own is not None else next((i for i in free if self.owner[i] is None), None)
            if slot is not None and slot in free:
                return tid, rest[0], rest[1], slot
            avoid.add(tid)

    def start(self, slot, tid, kind, branches, deadline):
        self.running[tid] = slot
        ensure_worktree(self.p, self.slot_dirs(slot)[0])
        ensure_worktree(self.p, self.slot_dirs(slot)[1])
        t = threading.Thread(target=self.slot_run, args=(slot, tid, kind, branches, deadline), daemon=True)
        self.threads.append(t)
        t.start()

    def slot_run(self, slot, tid, kind, branches, deadline):
        self.tl.wd, self.tl.cd = self.slot_dirs(slot)
        try:
            self.work(tid, kind, branches, deadline)
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
            self.save()
            self.wake.set()

    def loop(self, deadline):
        p = self.p
        while True:
            self.quiet_check()
            why = self.stop_check(deadline)
            if why:
                self.stop_reason = why
                return
            free = [i for i in range(self.n) if i not in self.running.values()]
            if not free:
                self.pause()
                continue
            need = self.est() * (len(self.running) + 1)
            if self.usage.rejected or self.usage.five + need > self.a.cap:
                if self.running:
                    self.pause(60)  # no more slots until the window allows; the running ones go on
                    continue
                if not self.wait_for_reset(deadline):
                    return
                continue
            self.fetch()
            self.poll_questions()
            self.queue = Queue(p)
            tid, kind, branches, slot = self.pick(free)
            if tid is None:
                if self.running:
                    self.pause()
                    continue
                if self.questions:
                    if not self.idle:
                        self.log(f"waiting for answers on {', '.join(self.questions)}")
                        self.idle = True
                    time.sleep(60)
                    continue
                if self.a.replan and self.plans < self.a.replan:
                    self.plans += 1
                    n = plan(p, self.a.goal or p.cfg["goal"], True, self.usage,
                             self.stopper(min(deadline, now() + timedelta(minutes=40))), merge=True)
                    if n:
                        continue
                self.stop_reason = kind
                return
            self.idle = False
            if self.n > 1:
                self.log(f"{tid}: slot {slot}")
            self.start(slot, tid, kind, branches, deadline)
            if self.n == 1:
                self.threads[-1].join()

    def run(self, deadline):
        p = self.p
        p.state.mkdir(parents=True, exist_ok=True)
        self.lockfile = open(p.state / "lock", "w")
        try:
            fcntl.flock(self.lockfile, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.lockfile.close()
            raise SystemExit("windvane: the autopilot is already running for this project")
        (p.state / "STOP").unlink(missing_ok=True)
        self.log(f"autopilot started, until {hhmm(deadline)} (5h cap {self.a.cap:g}%, week cap {self.a.week_cap:g}%"
                 + (f", {self.n} tickets at a time" if self.n > 1 else "") + ")")
        self.usage.probe(p)
        push(f"{p.name}: autopilot started", f"Until {hhmm(deadline)}. {self.usage.text()}.", 2, "rocket",
             ("Status=status", "Stop=stop"))
        try:
            self.loop(deadline)
        except Exception as e:
            self.stop_reason = f"driver error: {e}"
            raise
        finally:
            for t in self.threads:  # the running tickets finish (their runs stop at the deadline at the latest)
                t.join()
            names = lambda ids: "; ".join(map(self.name, ids)) or "none"
            crit = ", ".join(f"{t} {s:g}" for t, s in self.scores.items())
            summary = (f"Why: {self.stop_reason}.\nMerged: {names(self.merged)}.\n"
                       + (f"Critic (/10): {crit}.\n" if crit else "")
                       + f"Waiting on you: {names(self.questions)}.\nFailed: {names(self.failed)}.\n"
                       f"{self.usage.text()}.")
            self.log(summary.replace("\n", " "))
            push(f"{p.name}: autopilot stopped", summary, 4 if self.failed or self.questions or self.halt else 3,
                 "checkered_flag", ("Usage=usage",))
            self.save()
            self.lockfile.close()


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


def plan(p, goal, use_followups=True, usage=None, stop_if=None, merge=False, max_new=6):
    """A planner run writes tickets for `goal` (and the critic's follow-ups) on a branch the driver commits;
    `merge` merges it at once. Returns how many tickets it wrote."""
    cd = p.ci_dir
    ensure_worktree(p, cd)
    git(p.main_root(), "fetch", "-q", "--prune", p.remote)
    if not clean_detach(p, cd, p.upstream):
        log(p, "plan: can't check out main")
        return 0
    q = Queue(p)
    first = int(re.search(r"\d+", q.next_id()).group())
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
    text, killed, error = run_claude(p, "planner", "plan", prompt, rules(p, "PLAN.md"), cd,
                                     ["Bash(git ls-files *)"], deny, usage, stop_if)
    m = last_match(PLAN_RE, text)
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
    author = p.cfg["author"]
    ident = (["-c", f"user.name={author.split('<')[0].strip()}", "-c",
              f"user.email={author.split('<')[1].rstrip('>').strip()}"] if "<" in author else [])
    git(cd, "add", "-A", ".windvane")
    git(cd, *ident, "commit", "-q", "-m", f"Plan: {n} tickets ({m.group(3)[:60]})", "-m", text.strip()[-3000:])
    if notes:
        from .util import append_jsonl
        append_jsonl(p.state / "critic.jsonl", {"ticket": "PLAN", "step": "plan", "notes": len(notes)})
    if p.cfg["forge"] == "github":
        git(cd, "push", "-q", p.remote, f"HEAD:refs/heads/{branch}")
        pr = sh(cd, "gh", "pr", "create", "--head", branch, "--base", p.main, "--title",
                f"Plan: {n} new tickets", "--body", text.strip()[-6000:])
        pr_n = pr.rstrip("/").rsplit("/", 1)[-1]
        if merge and pr_n.isdigit():
            subprocess.run(["gh", "pr", "merge", pr_n, "--merge"], cwd=p.main_root(),
                           capture_output=True)
            git(p.main_root(), "push", "-q", p.remote, "--delete", branch)
        log(p, f"plan: {n} tickets on {pr or branch}{' (merged)' if merge else ''}")
    else:
        target = p.main if merge else branch
        git(cd, "push", "-q", p.remote, f"HEAD:refs/heads/{target}")
        log(p, f"plan: {n} tickets pushed to {target}")
    push(f"{p.name}: planned {n} tickets", m.group(3)[:200] or "See the queue.", 3, "memo")
    clean_detach(p, cd, p.upstream)
    return n
