"""Experiment tickets: `Kind: experiment` in a ticket's header marks a change meant to make the projects windvane builds
better (what sessions are told, the steps they take). The critic and the reviewer can't see whether it does, so once
they and the check have passed it, its head is benchmarked against current main before the merge:
`experiment_repeat` runs of the head, main's runs at its sha reused from the recorded results (one is made when none
is), each a `windvane bench --ref` in the background (`windvane await experiment-<id>` follows it).

Kept (`bench.experiment_verdict`), it merges as usual, its numbers in the log and its PR body. Rejected, it isn't
merged: its branch stays, QUEUE.md gets `- rejected: <id> (numbers)` and EXPERIMENTS.md a record of the idea, its files
and both sides' numbers, which the planner and the periodic sessions get, so the idea isn't planned again unchanged.
Main moving on during the bench: the head is compared with the newest main a bench ran, benching main's side again
(only that side) when the results for its new sha are missing.
"""
from datetime import timedelta

from . import bench, jobs, net, privacy
from .config import DIR
from .held import land
from .periodic import bench_command, bench_runs
from .planner import EXPERIMENTS, branch_name
from .util import append_jsonl, git, gout, hhmm, now, push
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
        which, said = self.bench_size()
        repeat = max(1, int(p.ap["experiment_repeat"]))
        for _ in range(MOVES):
            self.fetch()
            base = gout(p.main_root(), "rev-parse", p.upstream)
            if not bench.planned(p, base, which):
                self.log(f"{tid}: Kind: experiment, but {p.main} has no bench seeds to measure it with; merged as "
                         "usual")
                return self.settled(tid)
            if not self.bench_for(tid, todo(p, base, sha, which, repeat), which, said):
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
        if self.bench_fits(tid, sha) and self.experiment(tid, branch, sha):
            self.integrate(tid, branch, sha)
        return True

    def bench_size(self):
        """("full" or "small", why): by the weekly usage, as `windvane bench` sizes itself."""
        week = self.usage.week
        return bench.size(False, False, bench.week_now() if week is None else week, bench.week_limit())

    def run_minutes(self, sha, which):
        """The most one bench run at `sha` takes: its seeds build for `experiment_minutes` side by side, then each
        seed's judge has up to JUDGE_MINUTES, one after another."""
        return int(self.p.ap["experiment_minutes"]) + len(bench.planned(self.p, sha, which)) * bench.JUDGE_MINUTES

    def bench_fits(self, tid, sha):
        """Whether the experiment's next bench run fits before the deadline (or none is owed); if not, the ticket is
        set aside for the rest of the run. Asked before the critic too, so no verdict is paid for on a head this run
        can't bench."""
        p = self.p
        which, _ = self.bench_size()
        base = gout(p.main_root(), "rev-parse", p.upstream)
        left = todo(p, base, sha, which, max(1, int(p.ap["experiment_repeat"])))
        need = left and self.run_minutes(left[0][1], which)
        if not need or now() + timedelta(minutes=need) <= self.end():
            return True
        held = self.put_aside(tid, f"its next bench run ({need} min) didn't fit before the deadline",
                              f"a run with {need} min left before its deadline benches it")
        self.log(f"{tid}: the experiment's next bench run (up to {need} min) doesn't fit before the deadline "
                 f"({hhmm(self.end())}); set aside, the next run benches it{held}")
        return False

    def bench_for(self, tid, runs, which, said):
        """One `windvane bench --ref <sha>` for each (side, sha) of `runs`, one after another, each only once it fits
        before the deadline. True once all ran (each records itself; a judge failure shows as a run missing), else
        None: a run that doesn't fit sets the ticket aside, and a stop or the deadline ends the job."""
        p = self.p
        name = job_name(tid)
        base = jobs.job_base(p, name)
        hours = int(p.ap["experiment_minutes"]) / 60
        for i, (side, sha) in enumerate(runs, 1):
            if self.stopping_now() or (p.state / "STOP").exists():
                return self.log(f"{tid}: the run stops before the experiment's bench ended; the next run benches it")
            if not self.bench_fits(tid, sha):
                return None
            args = ["--ref", sha, f"--{which}", "--hours", f"{hours:.3g}", "--cap", f"{self.a.cap:g}", "--week-cap",
                    f"{self.a.week_cap:g}"]
            try:
                jobs.bg(p, name, bench_command(p, args), p.main_root(), quiet=True)
            except SystemExit as err:  # `bg` refuses with an exit; the driver goes on
                return self.fail(tid, f"the experiment's bench didn't start: {err}")
            self.log(f"{tid}: experiment bench {i} of {len(runs)}, {side} at {sha[:7]} ({which}: {said}; up to "
                     f"{self.run_minutes(sha, which)} min); `windvane await {name}` follows it")
            while jobs.alive(base):
                if self.stopping_now() or (p.state / "STOP").exists() or now() >= self.end():
                    jobs.end_job(base, "windvane: the experiment's bench ended with the run")
                    return self.log(f"{tid}: the experiment's bench ended with the run before it finished; the next "
                                    "run benches it again")
                self.nap(now() + timedelta(seconds=POLL))
        return True

    def bench_body(self, tid, branch, numbers, why, table):
        """A kept experiment's numbers, added to its PR's body (forge github) before the merge."""
        from .driver import pr_for
        p = self.p
        pr = p.cfg["forge"] == "github" and pr_for(p, branch)
        if not pr:
            return
        body = net.sh(p.main_root(), "gh", "pr", "view", pr, "--json", "body", "-q", ".body") or ""
        more = (f"## Experiment: kept by the bench\n{why}: {numbers}\n\n```\n{table}\n```")
        net.sh(p.main_root(), "gh", "pr", "edit", pr, "--body",
               privacy.censor(f"{body.rstrip()}\n\n{more}".lstrip(), privacy.own_names(p)))

    def reject(self, tid, branch, sha, both, numbers, why):
        """Not merged: `- rejected:` in QUEUE.md and a record in EXPERIMENTS.md, landed on main the way the driver
        lands its own changes; the branch stays. One push."""
        p, cd = self.p, self.cd
        self.skip.add(tid)
        self.approved.pop(tid, None)
        self.settled(tid)
        append_jsonl(p.state / "ledger.jsonl", {"event": "outcome", "ticket": tid, "outcome": "rejected"})
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
                               + record(tid, self.queue.titles.get(tid, tid), branch, files, both, why))
                git(cd, "add", "-A", DIR)
                with self.critical():  # a signal waits until the record is pushed
                    ok, where = land(p, cd, branch_name(p, cd, "experiment"), f"Experiment {tid} rejected",
                                     f"{why}: {numbers}. Branch {branch} kept, not merged.", fresh=True)
            finally:
                clean_detach(p, cd, p.upstream)
        if not ok:
            self.log(f"{tid}: its rejection record isn't on {p.main} ({where}): the next run benches it again")
        push(f"Rejected: {self.name(tid)}", f"The bench found it no better than {p.main} ({why}: {numbers}). Not "
             f"merged; branch {branch} kept" + ("." if ok else f"; the record waits on {where}."), 3, "test_tube")


def todo(p, base, sha, which, repeat):
    """The bench runs still owed, [(side, sha)]: main's when none is recorded at `base`, then the head's up to
    `repeat`."""
    return [(p.main, base)] * (not bench_runs(p, base, which)) + [("the head", sha)] * max(
        0, repeat - len(bench_runs(p, sha, which)))


def job_name(tid):
    """The background job of a ticket's experiment bench, as `windvane await` names it."""
    return f"experiment-{tid}"


def record(tid, title, branch, files, both, why):
    """A rejected experiment's entry in EXPERIMENTS.md: the idea, its files, both sides' numbers."""
    a, b = both
    shown = ", ".join(files[:12]) + (f" and {len(files) - 12} more" if len(files) > 12 else "")
    return "\n".join([
        "", f"### {tid}: {title} ({now():%Y-%m-%d})",
        f"- Idea: {title}", f"- Files: {shown or '(none)'}",
        f"- {a['name']} {a['sha'][:7]} ({bench.runs_n(len(a['runs']))}) → {b['name']} {b['sha'][:7]} "
        f"({bench.runs_n(len(b['runs']))}): {bench.headline(both)}",
        f"- Verdict: {why}; branch `{branch}` kept", ""])
