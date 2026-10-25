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
        isn't (rejected, failed, or cut off by a stop or the deadline: the next run benches it again, reusing every
        run recorded meanwhile)."""
        p = self.p
        week = self.usage.week
        which, said = bench.size(False, False, bench.week_now() if week is None else week, bench.week_limit())
        repeat = max(1, int(p.ap["experiment_repeat"]))
        for _ in range(MOVES):
            self.fetch()
            base = gout(p.main_root(), "rev-parse", p.upstream)
            if not bench.planned(p, base, which):
                self.log(f"{tid}: Kind: experiment, but {p.main} has no bench seeds to measure it with; merged as "
                         "usual")
                return True
            todo = [(p.main, base)] * (not bench_runs(p, base, which)) + [("the head", sha)] * max(
                0, repeat - len(bench_runs(p, sha, which)))
            if todo and not self.bench_for(tid, todo, which, said):
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
        table = bench.compare_table(both, which)
        if kept is None:
            return self.fail(tid, f"the experiment's bench counted no seed on both sides; log {log}")
        if not kept:
            return self.reject(tid, branch, sha, both, numbers, why)
        self.log(f"{tid}: experiment kept by the bench ({why}): {numbers}")
        self.bench_body(tid, branch, numbers, why, table)
        return True

    def bench_for(self, tid, todo, which, said):
        """One `windvane bench --ref <sha>` for each (side, sha) of `todo`, one after another, `experiment_minutes`
        shared out among them. True once all ran (each records itself, or a judge failure shows as a run missing), else None:
        a stop or the deadline ends the job, and a bench that doesn't fit before the deadline doesn't start."""
        p = self.p
        minutes = int(p.ap["experiment_minutes"])
        hours = (minutes - len(todo) * bench.JUDGE_MINUTES) / 60 / len(todo)
        if hours < 0.25:
            return self.fail(tid, f"`experiment_minutes` = {minutes} leaves under 15 min for each of the bench's "
                                  f"{len(todo)} runs")
        if now() + timedelta(minutes=minutes) > self.end():  # not worked again this run: its head waits as it is
            held = self.put_aside(tid, f"its bench ({minutes} min) didn't fit before the deadline",
                                  "a run with `experiment_minutes` left before its deadline benches it")
            return self.log(f"{tid}: the experiment's bench ({minutes} min) doesn't fit before the deadline "
                            f"({hhmm(self.end())}); set aside, the next run benches it{held}")
        name = job_name(tid)
        base = jobs.job_base(p, name)
        for i, (side, sha) in enumerate(todo, 1):
            args = ["--ref", sha, f"--{which}", "--hours", f"{hours:.3g}", "--cap", f"{self.a.cap:g}", "--week-cap",
                    f"{self.a.week_cap:g}"]
            try:
                jobs.bg(p, name, bench_command(p, args), p.main_root(), quiet=True)
            except SystemExit as err:  # `bg` refuses with an exit; the driver goes on
                return self.fail(tid, f"the experiment's bench didn't start: {err}")
            self.log(f"{tid}: experiment bench {i} of {len(todo)}, {side} at {sha[:7]} ({which}: {said}; "
                     f"{hours:.3g} h a seed); `windvane await {name}` follows it")
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
