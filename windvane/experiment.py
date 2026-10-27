"""Experiment tickets: `Kind: experiment` in a ticket's header marks a change meant to make the projects windvane builds
better (what sessions are told, the steps they take). The critic and the reviewer can't see whether it does, so once
they and the check have passed it, its head is benchmarked against current main before the merge:
`experiment_repeat` runs of the head, main's runs at its sha reused from the recorded results (one is made when none
is), each a `windvane bench --ref` in the background (`windvane await experiment-<id>` follows it).

Kept (`bench.experiment_verdict`), it merges as usual, its numbers in the log and its PR body. Rejected, it isn't
merged: its branch stays, QUEUE.md gets `- rejected: <id> (numbers)` and EXPERIMENTS.md a record of the idea, its files
and both sides' numbers, which the planner and the periodic sessions get, so the idea isn't planned again unchanged.
Main moving on during the bench: the head is compared with the newest main a bench ran, benching main's side again
(only that side) when the results for its new sha are missing. With a `seed` round, the bench is the training pool's
(`bench.training`), never the holdout's.

`stop` halts experiment planning (the overfit alarm, `periodic.Periodic.overfit_watch`): the planner and the periodic
sessions are told to write no experiment until `windvane experiments resume`. A plateau stops it too
(`plateau_watch`): `plateau` rejections in a row first get one exploration round (only bold, ablation and scout
ideas, `mix.EXPLORE`); that round all rejected too means the ideas stopped paying, so the bench's share of the week
goes unspent until something changes what an experiment could find: a kept one, a new goal, a new seed, or a resume.
A rejected `Idea: bold` experiment gets one revision first (`revise`): its worker sees which hidden tests and judge
checks moved on each seed, since a big change is rarely right first time.
An endless run (`run --replan forever`) also pushes the week's `stats.trend` once a week, at the usage window's reset
(`trend_watch`), so whether the loop works shows without a look at the desk.
"""
import math
import time
from datetime import datetime, timedelta

from . import bench, budget, jobs, mix, net, phone, privacy, stats
from .bench_score import counted, judged
from .config import DIR
from .held import land
from .periodic import bench_command, bench_runs
from .phone import whole
from .planner import EXPERIMENTS, branch_name
from .questions import quiet_now
from .util import append_jsonl, git, gout, hhmm, load_json, now, push, read_jsonl, save_json
from .worktree import clean_detach

POLL = 30  # seconds between looks at a running bench job
MOVES = 3  # main moving on during the bench: the most times its side is benched again before the head is compared
HEADER = ("# Experiments the bench rejected\n\nEach passed its critic, check and review, then measured no better than "
          "main on the bench\n(`windvane bench`), so it wasn't merged; its branch is kept. A ticket planning one again "
          "says what's different.\n")


class Experiments:
    """The Pilot's part for experiment tickets: `experiment` between an approved review and the merge."""

    def experiment(self, tid, branch, sha):
        """Bench an approved experiment's head against main and keep or reject it: True to merge it now, None when it
        isn't (rejected, failed, or cut off by a stop or the deadline). Until it settles the head is saved
        (`benching`), so the next run, or this one after a restart, benches it with no worker, critic or review run
        again, reusing every bench run recorded meanwhile."""
        p = self.p
        self.benching[tid] = {"branch": branch, "sha": sha}
        self.save()
        if not self.pool_fits(tid):
            return None
        which, said = self.bench_size()
        repeat = max(1, int(p.ap["experiment_repeat"]))
        for _ in range(MOVES):
            self.fetch()
            base = gout(p.main_root(), "rev-parse", p.upstream)
            if not bench.planned(p, base, which, bench.training(p)):
                self.log(f"{tid}: Kind: experiment, but {p.main} has no bench seeds to measure it with; merged as "
                         "usual")
                return self.settled(tid)
            if not self.bench_for(tid, base, sha, which, said, repeat):
                return None
            self.fetch()
            if gout(p.main_root(), "rev-parse", p.upstream) == base:
                break
            self.log(f"{tid}: {p.main} moved on during the experiment's bench")
        both = [{"name": p.main, "sha": base, "runs": bench_runs(p, base, which)[-repeat:]},
                {"name": tid, "sha": sha, "runs": bench_runs(p, sha, which)[-repeat:]}]
        log = f"{jobs.job_base(p, job_name(tid))}.log"
        lack = [f"{s['name']} ({s['sha'][:7]})" for s in both if not s["runs"]]
        if lack:
            return self.fail(tid, f"the experiment's bench left no run of {' or '.join(lack)} to compare; log {log}")
        kept, numbers, why = bench.experiment_verdict(both, float(p.ap["experiment_saving"]))
        if kept is None:
            return self.fail(tid, f"the experiment's bench counted no seed on both sides; log {log}")
        if not kept:
            return self.reject(tid, branch, sha, both, numbers, why)
        self.log(f"{tid}: experiment kept by the bench ({why}): {numbers}")
        append_jsonl(p.state / "ledger.jsonl", {"event": "experiment", "ticket": tid, "verdict": "kept",  # pool.py
                                                "idea": self.idea(tid)})
        self.bench_body(tid, branch, numbers, why, bench.compare_table(both, which))
        return self.settled(tid)

    def settled(self, tid):
        """The experiment's bench is over: nothing is owed on its head any more. True, for `experiment` to return."""
        self.benching.pop(tid, None)
        self.save()
        return True

    def bench_again(self, tid):
        """An approved experiment whose bench didn't end (a stop, the deadline, a restart): while its branch still has
        that head, straight to the bench and on to the merge if it's kept. True when it went there."""
        p, owed = self.p, self.benching.get(tid)
        if not owed:
            return False
        branch, sha = owed["branch"], owed["sha"]
        if gout(p.main_root(), "rev-parse", "-q", "--verify", f"{p.remote}/{branch}") != sha:
            self.benching.pop(tid)
            self.save()
            self.log(f"{tid}: {branch} moved off its approved head {sha[:7]}: the worker takes it up again")
            return False
        self.log(f"{tid}: {branch} is still at its approved head {sha[:7]}: straight to its bench")
        if self.experiment(tid, branch, sha):  # a job left running is waited for before any fit is asked
            self.integrate(tid, branch, sha)
        return True

    def bench_size(self):
        """("full" or "small", why): by the weekly usage, as `windvane bench` sizes itself."""
        week = self.usage.week
        return bench.size(False, False, bench.week_now() if week is None else week, bench.week_limit())

    def run_minutes(self, sha, which):
        """The most one bench run at `sha` takes: its seeds build for `experiment_minutes` side by side, then each
        seed's judge has up to JUDGE_MINUTES, one after another."""
        return int(self.p.ap["experiment_minutes"]) + len(bench.planned(self.p, sha, which, bench.training(self.p))) \
            * bench.JUDGE_MINUTES

    def pool_fits(self, tid):
        """Whether the training pool can decide the experiment (`bench.pool_short`); if not, it is set aside for the
        rest of the run, not merged unmeasured, before its worker when it's picked: the seed round fills the pool."""
        short = bench.pool_short(self.p)
        if not short:
            return True
        how = "the seed round filling it (`windvane periodic seed` asks for a round now)"
        held = self.put_aside(tid, short, how)
        self.log(f"{tid}: {short}: the experiment waits, not failed, for {how}{held}")
        return False

    def bench_fits(self, tid, sha):
        """Whether the experiment's next bench run fits before the deadline (or none is owed); if not, the ticket is
        set aside for the rest of the run. Asked before the critic too, so no verdict is paid for on a head this run
        can't bench."""
        p = self.p
        if not self.pool_fits(tid):
            return False
        which, _ = self.bench_size()
        base = gout(p.main_root(), "rev-parse", p.upstream)
        left = todo(p, base, sha, which, max(1, int(p.ap["experiment_repeat"])))
        need = left and self.run_minutes(left[0][1], which)
        if not need:
            return True
        if now() + timedelta(minutes=need) > self.end():
            held = self.put_aside(tid, f"its next bench run ({need} min) didn't fit before the deadline",
                                  f"a run with {need} min left before its deadline benches it")
            self.log(f"{tid}: the experiment's next bench run (up to {need} min) doesn't fit before the deadline "
                     f"({hhmm(self.end())}); set aside, the next run benches it{held}")
            return False
        why, b = self.budget_holds(tid, which, 1)
        if not why:
            return True
        held = self.put_aside(tid, f"{budget.MARK}: {why}", "the week's reset, or `windvane experiments allow +10`")
        self.log(f"{tid}: {budget.MARK} holds its next bench run ({why}); it waits, not failed: the week resets "
                 f"{b.ends()}, or `windvane experiments allow +10`{held}")
        return False

    def plateau_watch(self):
        """Between tickets: the last `plateau` experiments all rejected (since experiments last started again) start
        one exploration round (`mix.EXPLORE` kinds only, planned next); once all of that round's experiments are
        rejected too, experiment planning stops with one push holding the week's trend, and the
        run goes on with other tickets. A kept experiment ends the round; a plateau's stop ends by itself on a kept
        experiment, a changed goal or a new seed. Read only when the ledger, main or the goal moved."""
        p, goal = self.p, self.goal()
        try:
            key = ((p.state / "ledger.jsonl").stat().st_mtime, gout(p.main_root(), "rev-parse", p.upstream), goal)
        except OSError:
            return
        if key == self.plateau_read:
            return
        self.plateau_read = key
        verdicts, st = decisions(p), load_json(stopped_file(p), {})
        if st.get("kind") == PLATEAU:
            why = restart_why(p, st, verdicts, goal)
            if why:
                resume(p)
                self.log(f"experiments: planned again after the plateau: {why}")
            return
        if st:  # stopped already (the overfit alarm): that stop stands
            return
        n, pf = int(p.cfg["experiments"]["plateau"]), load_json(plateau_file(p), {})
        explore = pf.get("explore")
        if explore:
            return self.explore_watch(pf, explore, verdicts, n, goal)
        last = verdicts[pf.get("from", 0):][-n:]
        if len(last) < n or "kept" in last:
            return
        save_json(plateau_file(p), {**pf, "explore": {"at": len(verdicts), "ids": None}})
        self.log(f"plateau: the last {n} experiments were all rejected; one exploration round (bold, ablation and "
                 "scout ideas only) comes before experiment planning stops")

    def explore_watch(self, pf, explore, verdicts, n, goal):
        """The exploration round's end: a kept experiment since it began goes back to the usual mix; each of its own
        experiments closed unkept (rejected, failed, parked, done or gone) stops experiment planning, its push
        saying how each ended."""
        p = self.p
        if "kept" in verdicts[explore["at"]:]:
            save_json(plateau_file(p), {k: v for k, v in pf.items() if k != "explore"})
            return self.log("plateau: an experiment was kept during the exploration round; the usual mix again")
        ids = explore.get("ids")
        if ids is None:
            return
        got = fates(p, ids)
        if "kept" in got.values() or None in got.values():  # a kept one: seen above once the ledger has it
            return
        what = tally(got)
        stop(p, f"the last {n} experiments were all rejected and the exploration round after them kept none ({what}; "
             "a plateau); a kept experiment, a new goal or seed, or `windvane experiments resume` starts them again",
             kind=PLATEAU, goal=goal, seeds=seed_names(p), verdicts=len(verdicts))
        self.log(f"plateau: the exploration round ({', '.join(ids)}) kept none ({what}); "
                 "experiment planning stopped, other tickets go on")
        push("Experiments plateaued", phone.cut(
            f"The last {n} experiments were all rejected, and an exploration round after them kept none "
            f"({what}), so no more are planned until one is kept, the goal or a seed changes, or `windvane "
            "experiments resume`. Other tickets go on. This week: " + "; ".join(stats.trend(p)), phone.MOST,
            log=self.log), 3, "chart_with_downwards_trend")

    def trend_watch(self):
        """An endless run's weekly push: once the usage window's week has reset, the week that ended in
        `stats.trend`'s lines. The first look only notes when the week resets; a run that wasn't going at a reset
        pushes at its first pass after it, for the last week that ended."""
        if self.a.replan != math.inf:
            return
        p, f = self.p, self.p.state / "trend.json"
        b = budget.Budget(p.cfg["experiments"])
        due, t = load_json(f, {}).get("next"), time.time()
        if due and t < due:
            return
        end = due
        while end and end + budget.WEEK <= t:
            end += budget.WEEK
        save_json(f, {"next": b.reset if b.reset and b.reset > t else (end or t) + budget.WEEK})
        if end:
            push("Weekly trend", phone.cut("\n".join(stats.trend(p, end)), phone.MOST, log=self.log), 3,
                 "chart_with_upwards_trend")
            self.log(f"weekly trend pushed for the week to {datetime.fromtimestamp(end):%a %H:%M}")

    def budget_holds(self, who, which, runs):
        """(why, budget): why the experiment budget holds the next `runs` bench runs of this size ("" when they may
        start). At the share, the week's one phone question per cap may raise it first."""
        cfg = self.p.cfg["experiments"]
        b = budget.Budget(cfg)
        why = b.blocks(which, runs)
        if why and why[0] == "share" and self.ask_budget(who, b, which, runs):
            b = budget.Budget(cfg)
            why = b.blocks(which, runs)
        return (why[1] if why else ""), b

    def ask_budget(self, who, b, which, runs):
        """The phone question at the cap, once a week per cap (machine-wide): +10 or +20 points this week, or wait
        for the reset. Waits up to `answer_wait` minutes, never past the deadline or a stop; no answer yet, "later",
        no phone or a quiet time: no for now, and an answer that comes later is read the next time a bench asks for
        room. A push that failed is sent again then. True when the user allowed more."""
        p = self.p
        q = b.question_at(b.cap)
        if q:
            return not q.get("taken") and self.budget_answer(who, q["qid"], b.cap) is True
        if quiet_now(p)[0]:
            self.log(f"{who}: quiet time, so no question on {budget.MARK}")
            return False
        qid = p.qid(f"budget-{datetime.fromtimestamp(b.reset or b.at):%m%d}-{b.cap:g}")
        text, options = budget.question(b, which, runs)
        sent = phone.ask(qid, f"{text} No answer in {self.a.answer_wait:g} min: no for now.", options)
        if not sent:
            self.log(f"{who}: {'no phone' if sent is None else 'the budget question did not send'}; "
                     "`windvane experiments allow +10` raises the cap")
            return False
        budget.Budget(p.cfg["experiments"]).asking(b.cap, qid)
        until = min(now() + timedelta(minutes=self.a.answer_wait), self.end())
        while now() < until and not (p.state / "STOP").exists() and not (self.signalled or self.kill_all):
            got = self.budget_answer(who, qid, b.cap)
            if got is not None:
                return got
            self.sleep(min(5, max(0.0, (until - now()).total_seconds())))
        self.log(f"{who}: no answer on {budget.MARK} yet: no for now; a later answer is still taken")
        return False

    def budget_answer(self, who, qid, cap):
        """The phone's answer to the budget question at `cap`: True (A or B: more allowed, applied once), False (no,
        or "later"), None (none yet)."""
        got = phone.answer(qid)
        if got == "LATER":
            return False
        if not got.startswith("ANSWER"):
            return None
        letter = (got.split()[1:] or [""])[0][:1].upper()
        more = budget.STEPS["AB".index(letter)] if letter and letter in "AB" else 0
        budget.Budget(self.p.cfg["experiments"]).allow(more, cap)  # a no is taken too: not read again
        self.log(f"{who}: answered {letter}: " + (f"experiments may use {more}% more of this week" if more
                                                   else "no more this week"))
        return bool(more)

    def bench_for(self, tid, base, sha, which, said, repeat):
        """The bench runs owed (`todo`), one `windvane bench --ref <sha>` after another, each only once it fits before
        the deadline; first, a job of the ticket's still running from before a restart is waited for, not doubled.
        True once all ran (each records itself; a judge failure shows as a run missing), else None: a run that
        doesn't fit sets the ticket aside, and a stop or the deadline ends the job."""
        p = self.p
        name = job_name(tid)
        job = jobs.job_base(p, name)
        if jobs.alive(job):  # the driver restarted (a crash, a self-update) while its bench ran on
            self.log(f"{tid}: its bench job from before a restart still runs; waiting for it")
            if not self.bench_ended(tid, job):
                return None
        runs = todo(p, base, sha, which, repeat)
        if runs:
            self.log(f"{tid}: experiment bench, {len(runs)} run{'s' * (len(runs) != 1)} ({which}: {said}); "
                     f"`windvane await {name}` follows each")
        hours = int(p.ap["experiment_minutes"]) / 60
        for i, (side, at) in enumerate(runs, 1):
            if self.stopping_now() or (p.state / "STOP").exists():
                return self.log(f"{tid}: the run stops before the experiment's bench ended; the next run benches it")
            if not self.bench_free(tid) or not self.bench_fits(tid, at):
                return None
            args = ["--ref", at, f"--{which}", "--hours", f"{hours:.3g}", "--cap", f"{self.a.cap:g}", "--week-cap",
                    f"{self.a.week_cap:g}"]
            try:
                jobs.bg(p, name, bench_command(p, args), p.main_root(), quiet=True)
            except SystemExit as err:  # `bg` refuses with an exit; the driver goes on
                return self.fail(tid, f"the experiment's bench didn't start: {err}")
            self.log(f"{tid}: experiment bench {i} of {len(runs)}: {side} at {at[:7]}, up to "
                     f"{self.run_minutes(at, which)} min")
            if not self.bench_ended(tid, job):
                return None
        return True

    def bench_free(self, tid):
        """Wait while another bench holds the machine's (one runs at a time), so this one's fit before the deadline
        is reckoned from when it can start. True once free; None once a stop or the deadline ends the wait, which
        isn't a failure: the next run benches it."""
        said = False
        while (h := bench.holder()) is not None:
            if self.stopping_now() or (self.p.state / "STOP").exists() or now() >= self.end():
                return self.log(f"{tid}: the run ended while its bench waited for {bench.describe(h)}; the next run "
                                "benches it")
            if not said:
                self.log(f"{tid}: bench: waiting for {bench.describe(h)}")
                said = True
            self.nap(now() + timedelta(seconds=POLL))
        return True

    def bench_ended(self, tid, job):
        """Wait for the bench job to end: True, or None once a stop or the deadline has ended it."""
        p = self.p
        while jobs.alive(job):
            if self.stopping_now() or (p.state / "STOP").exists() or now() >= self.end():
                jobs.end_job(job, "windvane: the experiment's bench ended with the run")
                return self.log(f"{tid}: the experiment's bench ended with the run before it finished; the next run "
                                "benches it again")
            self.nap(now() + timedelta(seconds=POLL))
        return True

    def bench_body(self, tid, branch, numbers, why, table):
        """A kept experiment's numbers, added to its PR's body (forge github) before the merge."""
        from .driver import pr_for
        p = self.p
        pr = p.cfg["forge"] == "github" and pr_for(p, branch)
        if not pr:
            return
        r = net.call(["gh", "pr", "view", pr, "--json", "body", "-q", ".body"], p.main_root())
        if r.returncode:  # an edit now would replace the worker's whole body with only this section
            return self.log(f"{tid}: its PR #{pr} couldn't be read, so the bench's numbers stay out of its body")
        body = r.stdout.strip()
        more = (f"## Experiment: kept by the bench\n{why}: {numbers}\n\n```\n{table}\n```")
        net.sh(p.main_root(), "gh", "pr", "edit", pr, "--body",
               privacy.censor(f"{body.rstrip()}\n\n{more}".lstrip(), privacy.own_names(p)))

    def reject(self, tid, branch, sha, both, numbers, why):
        """Not merged: `- rejected:` in QUEUE.md and a record in EXPERIMENTS.md, landed on main the way the driver
        lands its own changes; the branch stays. One push. Until it lands, the head stays owed (`benching`), so the next run
        goes straight back here with no worker, critic, review or bench run again."""
        p, cd = self.p, self.cd
        if self.idea(tid) == "bold" and not revised(p, tid, sha):
            return self.revise(tid, sha, both, numbers, why)
        self.skip.add(tid)
        self.log(f"{tid}: experiment rejected by the bench ({why}): {numbers}; branch {branch} kept, not merged")
        files = gout(p.main_root(), "diff", "--name-only", f"{p.upstream}...{sha}").split()
        with self.merge_lock:
            self.fetch()
            clean_detach(p, cd, p.upstream)
            try:
                q, rec = cd / p.queue_rel, cd / EXPERIMENTS
                q.write_text(q.read_text().rstrip("\n") + f"\n- rejected: {tid} ({numbers})\n" if q.is_file()
                             else f"- rejected: {tid} ({numbers})\n")
                rec.write_text((rec.read_text().rstrip("\n") + "\n" if rec.is_file() else HEADER)
                               + record(tid, self.queue.titles.get(tid, tid), branch, files, both, why,
                                        self.idea(tid), mix.mechanism(self.head_of(tid))))
                git(cd, "add", "-A", DIR)
                with self.critical():  # a signal waits until the record is pushed
                    ok, where = land(p, cd, branch_name(p, cd, "experiment"), f"Experiment {tid} rejected",
                                     f"{why}: {numbers}. Branch {branch} kept, not merged.", fresh=True)
            finally:
                clean_detach(p, cd, p.upstream)
        if ok:
            self.approved.pop(tid, None)
            self.settled(tid)
            append_jsonl(p.state / "ledger.jsonl", {"event": "outcome", "ticket": tid, "outcome": "rejected",
                                                    "idea": self.idea(tid)})
        else:  # its head stays owed (`benching`): the next run reuses the recorded runs and lands the record again
            self.log(f"{tid}: its rejection record isn't on {p.main} ({where}): the next run lands it with no worker, "
                     "critic or review run again")
        push(f"Rejected: {self.name(tid)}", whole(f"The bench found it no better than {p.main}: ", f"{why} ({numbers})",
                                                  f". Not merged; branch {branch} kept"
                                                  + ("." if ok else f"; the record waits on {where}."), log=self.log),
             3, "test_tube")

    def revise(self, tid, sha, both, numbers, why):
        """A bold experiment's first rejection: not closed but sent back to its worker once, with which hidden tests
        and judge checks moved on each seed, to fix what broke; its next head is critiqued, checked, reviewed and
        benched again, and a second rejection closes it. The ledger row names the head first: a restart before the
        feedback is saved benches that same head again and sends it back again (`revised`). None: nothing to merge."""
        append_jsonl(self.p.state / "ledger.jsonl", {"event": "experiment", "ticket": tid, "verdict": "revise",
                                                     "idea": "bold", "sha": sha})
        self.feedback[tid] = (
            f"The bench rejected your head {sha[:12]} ({why}: {numbers}). A bold experiment gets one revision: find "
            "what broke from what moved below (main → your head), fix it, keep the idea, and finish READY again; a "
            "second rejection closes it.\n" + "\n".join(seed_diff(both)))
        self.approved.pop(tid, None)
        self.settled(tid)
        self.log(f"{tid}: bold experiment rejected by the bench ({why}): {numbers}; back to its worker for its one "
                 "revision")
        return None

    def idea(self, tid):
        """The ticket's `Idea:` kind ("" when it names none)."""
        return getattr(self.queue, "ideas", {}).get(tid, "")

    def head_of(self, tid):
        """The ticket's header, its first 8 lines."""
        return "\n".join(self.queue.text.get(tid, "").splitlines()[:8])


def revised(p, tid, sha):
    """Whether the bold experiment had its one revision: a head other than `sha` was sent back, or this one twice
    (a revision that pushed no change can't loop)."""
    sent = [r.get("sha") for r in read_jsonl(p.state / "ledger.jsonl")
            if r.get("event") == "experiment" and r.get("verdict") == "revise" and r.get("ticket") == tid]
    return any(x != sha for x in sent) or len(sent) >= 2


def seed_diff(both):
    """What moved between main's newest run (A) and the head's (B), per seed counted on both: each ticket's hidden
    tests that changed, and each judge check whose answer did (B's reason with it)."""
    per, common = counted(both)
    out = []
    for seed in common:
        a, b = per[0][-1][seed], per[1][-1][seed]
        for t in sorted(set(a.get("hidden", {})) | set(b.get("hidden", {}))):
            ha, hb = a.get("hidden", {}).get(t), b.get("hidden", {}).get(t)
            if (ha or {}).get("passed") != (hb or {}).get("passed"):
                score = lambda h: f"{h['passed']}/{h['total']}" if h else "none"  # noqa: E731
                out.append(f"- {seed} {t}: hidden tests {score(ha)} → {score(hb)}")
        ja, jb = ({x["id"]: x for x in (judged(r) or {}).get("answers", [])} for r in (a, b))
        for c in sorted(set(ja) | set(jb)):
            sa, sb = (j.get(c, {}).get("answer", "none") for j in (ja, jb))
            if sa != sb:
                reason = jb.get(c, {}).get("reason", "")
                out.append(f"- {seed} judge {c}: {sa} → {sb}" + (f" ({reason})" if reason else ""))
    return out or ["- no hidden test or judge check changed on a seed counted on both sides: the totals moved"]


def fates(p, ids):
    """{ticket: "kept", "rejected", "failed", "parked", "done" (without a kept verdict), "gone" (no longer a ticket
    anywhere), or None while it's open}: so an exploration round's experiment that never reaches the bench still
    ends it. A ticket not on main yet but on an unmerged plan branch is open: its plan hasn't merged."""
    from .planner import pending_ids
    from .queue import Queue
    last = {}
    for r in read_jsonl(p.state / "ledger.jsonl"):
        t = r.get("ticket")
        if r.get("event") == "experiment" and r.get("verdict") == "kept":
            last[t] = "kept"
        elif r.get("event") == "outcome" and r.get("outcome") in ("rejected", "failed"):
            last[t] = r["outcome"]
        elif r.get("event") == "outcome" and r.get("outcome") == "retried":  # a failed ticket worked again
            last.pop(t, None)
    q = Queue(p)
    missing = [t for t in ids if t not in q.files and t not in last]
    waiting = pending_ids(p, ask_forge=False) if missing else set()

    def fate(t):
        if t in last:
            return last[t]
        if t not in q.files:
            return None if t in waiting else "gone"
        return q.status(t) if q.status(t) in ("done", "parked") else None
    return {t: fate(t) for t in ids}


def tally(got):
    """`2 rejected, 1 failed`: an exploration round's fates, as its push says them."""
    n = {}
    for v in got.values():
        n[v] = n.get(v, 0) + 1
    return ", ".join(f"{c} {v}" for v, c in sorted(n.items(), key=lambda x: -x[1])) or "no experiment planned"


def todo(p, base, sha, which, repeat):
    """The bench runs still owed, [(side, sha)]: main's when none is recorded at `base`, then the head's up to
    `repeat`."""
    return [(p.main, base)] * (not bench_runs(p, base, which)) + [("the head", sha)] * max(
        0, repeat - len(bench_runs(p, sha, which)))


def stopped_file(p):
    return p.state / "experiments-stopped.json"


def stopped(p):
    """Why experiment planning is stopped ("" when it isn't)."""
    return load_json(stopped_file(p), {}).get("why", "")


def stop(p, why, **more):
    """Stop experiment planning; `more` (a plateau's kind, goal, seeds and verdict count) tells when it ends itself."""
    save_json(stopped_file(p), {"why": why, "at": f"{now():%Y-%m-%d %H:%M}", **more})


def resume(p):
    """Start experiment planning again; whether it was stopped. The verdicts so far no longer count towards a
    plateau, or the next look would stop it again at once."""
    was = bool(stopped(p))
    stopped_file(p).unlink(missing_ok=True)
    save_json(plateau_file(p), {"from": len(decisions(p))})
    return was


PLATEAU = "plateau"  # a stop's kind when it came of `plateau` rejections in a row


def plateau_file(p):
    return p.state / "plateau.json"


def decisions(p):
    """The bench's verdicts on experiments, oldest first ("kept" or "rejected"), from the ledger."""
    out = []
    for r in read_jsonl(p.state / "ledger.jsonl"):
        if r.get("event") == "experiment" and r.get("verdict") == "kept":
            out.append("kept")
        elif r.get("event") == "outcome" and r.get("outcome") == "rejected":
            out.append("rejected")
    return out


def seed_names(p):
    """The seeds on main, both sets (`pool/<name>` for the training pool's): a new one may find what the old didn't."""
    return [*bench.planned(p, p.upstream, "full"), *(f"pool/{n}" for n in bench.planned(p, p.upstream, "full", True))]


def restart_why(p, st, verdicts, goal):
    """Why a plateau's stop `st` is over ("" while it isn't): an experiment kept since, the goal changed, a new seed."""
    if "kept" in verdicts[st.get("verdicts", 0):]:
        return "an experiment was kept"
    if goal != st.get("goal", goal):
        return "the goal changed"
    new = sorted(set(seed_names(p)) - set(st.get("seeds", [])))
    return f"a new seed ({', '.join(new)})" if new else ""


def job_name(tid):
    """The background job of a ticket's experiment bench, as `windvane await` names it."""
    return f"experiment-{tid}"


def record(tid, title, branch, files, both, why, kind="", mechanism=""):
    """A rejected experiment's entry in EXPERIMENTS.md: the idea, its kind and mechanism, its files, both sides'
    numbers."""
    a, b = both
    shown = ", ".join(files[:12]) + (f" and {len(files) - 12} more" if len(files) > 12 else "")
    return "\n".join([
        "", f"### {tid}: {title} ({now():%Y-%m-%d})",
        f"- Idea: {title}", *([f"- Kind: {kind}"] if kind else []),
        *([f"- Mechanism: {mechanism}"] if mechanism else []), f"- Files: {shown or '(none)'}",
        f"- {a['name']} {a['sha'][:7]} ({bench.runs_n(len(a['runs']))}) → {b['name']} {b['sha'][:7]} "
        f"({bench.runs_n(len(b['runs']))}): {bench.headline(both)}",
        f"- Verdict: {why}; branch `{branch}` kept", ""])
