"""Experiment tickets (`Kind: experiment`): after the review approves one, its head is benchmarked against main (main's
recorded runs reused) and merged only when it measures better, or as good for fewer tokens; otherwise it's rejected,
its branch kept, with a `rejected:` line in QUEUE.md and a record in EXPERIMENTS.md that the planner gets. The bench
is a fake job that records results for the commit it's given, as `windvane bench --ref` does."""
import json
import re
import sys
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from windvane import bench, driver, experiment, jobs, planner  # noqa: E402
from windvane.config import Project  # noqa: E402
from windvane.queue import Queue  # noqa: E402
from windvane.stats import ticket_outcomes  # noqa: E402
from windvane.util import append_jsonl, now  # noqa: E402
from helpers import REPO, Sandbox  # noqa: E402
from test_bench_periodic import rec  # noqa: E402
from test_merge_draft import GH, args  # noqa: E402

# The fake bench: appends the records it is given under a run id of its own, as `windvane bench` does at a run's end
FAKE = ("import json, sys, time\n"
        "with open(sys.argv[2], 'a') as f:\n"
        "    for r in json.loads(sys.argv[1]):\n"
        "        f.write(json.dumps({**r, 'run': f'{time.time_ns()}-fake'}) + '\\n')\n"
        "if sys.argv[3:]:\n"  # then a stop, as `windvane stop` leaves it
        "    open(sys.argv[3], 'w').close()\n")
# main's recorded run: 4 of 10 hidden tests, 3 of 6 judge yeses, 100k weighted tokens over one merged ticket
MAIN = {"passed": 4, "yes": 3, "weighted": 100_000, "merged": 1}
# `pr view --json body` and `pr edit --body` on top of the fake gh of test_merge_draft
DRAFT = '''elif args[:2] == ["pr", "view"] and args[args.index("--json") + 1] == "isDraft":'''
BODY = GH.replace(DRAFT, '''elif args[:2] == ["pr", "view"] and args[args.index("--json") + 1] == "body":
    print((st / "body").read_text() if (st / "body").exists() else "What changed and why.")
elif args[:2] == ["pr", "edit"]:
    (st / "body").write_text(args[args.index("--body") + 1])
''' + DRAFT)


class Experiment(unittest.TestCase):
    def make(self, kind="Kind: experiment.\n", forge="git"):
        self.s = Sandbox()
        self.addCleanup(self.s.close)
        self.s.write(".windvane/tickets/T-001.md", f"# T-001: a shorter worker prompt\nDepends on: nothing.\n{kind}")
        self.s.write("bench/tiny/seed/README.md", "tiny\n")  # what a recorded run must have worked (`bench.planned`)
        self.s.write("bench/tiny/hidden/bench.toml", "")
        if forge == "github":
            cfg = self.s.root / ".windvane/config.toml"
            cfg.write_text(cfg.read_text().replace('forge = "git"', 'forge = "github"'))
            self.gh = self.s.tmp / "gh"
            self.gh.mkdir()
            gh = self.s.bin / "gh"
            gh.write_text(BODY.format(python=sys.executable, state=self.gh, remote=self.s.remote))
            gh.chmod(0o755)
        self.s.publish()
        self.p = Project(self.s.root)
        self.main = self.s.git("rev-parse", "HEAD")
        append_jsonl(bench.results(self.p), rec(self.main, seed="tiny", **MAIN))
        self.calls, self.moved, self.stop_after_first = [], None, False
        self.addCleanup(setattr, experiment, "POLL", experiment.POLL)
        experiment.POLL = 0.05
        self.addCleanup(setattr, driver, "NAP", driver.NAP)
        driver.NAP = 0.05

    def run_pilot(self, head, moved=None):
        """A run with the bench faked: each head run records `head`'s numbers (one dict a run); `moved`: numbers of a
        commit that lands on main while the head's first run goes, recorded when main's side is benched again."""
        def command(p, a):
            sha = a[a.index("--ref") + 1]
            self.calls.append(sha)
            if sha in (self.main, self.moved):
                numbers = MAIN if sha == self.main else moved
            else:
                numbers = head[min(self.calls.count(sha), len(head)) - 1]
                if moved and len(self.calls) == 1:
                    self.s.write("other.txt", "merged meanwhile\n")
                    self.s.publish("Merge T-009: something else")
                    self.moved = self.s.git("rev-parse", "HEAD")
            return [sys.executable, "-c", FAKE, json.dumps([rec(sha, seed="tiny", **numbers)]),
                    str(bench.results(p)), *[str(p.state / "STOP")] * self.stop_after_first]
        with mock.patch.object(experiment, "bench_command", command):
            pilot = driver.Pilot(self.p, args())
            pilot.sleep = lambda s: None
            pilot.run(now() + timedelta(hours=6))  # the bench's 240 min fit
        return pilot

    def sessions(self, role):
        calls = [json.loads(x) for x in (self.s.tmp / "calls.jsonl").read_text().splitlines()]
        return sum(c["system"].startswith(f"# windvane {role} run") for c in calls)

    def head(self):
        """The experiment's head, as the bench was given it (a merged branch is gone from the remote)."""
        return next(c for c in self.calls if c not in (self.main, self.moved))

    def on_main(self, rel):
        self.s.git("fetch", "-q", "origin")
        return self.s.git("show", f"origin/main:{rel}")

    def log(self):
        return re.sub(r"^\S+ \S+ ", "", (self.p.state / "autopilot.log").read_text(), flags=re.M)

    def assert_rejected(self, pilot, line):
        self.assertEqual(pilot.merged, [])
        self.assertIn("t-001-work", self.s.git("branch", "-r"))  # the branch is kept
        self.assertNotIn("T-001.txt", self.s.git("ls-tree", "--name-only", "origin/main"))
        self.assertIn(line, self.on_main(".windvane/QUEUE.md").splitlines())
        record = self.on_main(".windvane/EXPERIMENTS.md")
        self.assertIn("### T-001: a shorter worker prompt", record)
        self.assertIn("- Idea: a shorter worker prompt", record)
        self.assertIn("- Files: T-001.txt", record)
        self.assertIn(f"main {self.main[:7]} (1 run) → T-001 {self.head()[:7]} (2 runs): hidden tests 4/10 →", record)
        self.assertIn("branch `t-001-work` kept", record)
        q = Queue(self.p)
        self.assertEqual((q.status("T-001"), q.rejected), ("done", {"T-001"}))  # never picked again
        self.assertEqual(ticket_outcomes(self.p, {"T-001": {"ticket": True}}), {"T-001": "rejected"})
        self.assertIn("T-001: experiment rejected by the bench", self.log())
        return record

    def test_a_better_experiment_merges_with_its_numbers_in_the_pr_body(self):
        self.make(forge="github")
        better = {**MAIN, "passed": 6}
        pilot = self.run_pilot([better, better])
        self.assertEqual(pilot.merged, ["T-001"])
        self.assertEqual(self.calls, [self.head(), self.head()])  # main's recorded run is reused; the head runs twice
        body = (self.gh / "body").read_text()
        self.assertTrue(body.startswith("What changed and why.\n\n## Experiment: kept by the bench\n"), body)
        self.assertIn("hidden tests up: hidden 4→6, judge 3→3, tokens/ticket +0%", body)
        self.assertIn(f"A = main ({self.main[:7]}), 1 run · B = T-001 ({self.head()[:7]}), 2 runs", body)
        self.assertIn("T-001: experiment kept by the bench (hidden tests up): hidden 4→6", self.log())

    def test_a_flat_experiment_with_tokens_down_12_percent_merges(self):
        self.make()
        cheaper = {**MAIN, "weighted": 88_000}
        pilot = self.run_pilot([cheaper, cheaper])
        self.assertEqual(pilot.merged, ["T-001"])
        self.assertIn("T-001: experiment kept by the bench (quality held, weighted tokens per merged ticket down "
                      "12%): hidden 4→4, judge 3→3, tokens/ticket -12%", self.log())

    def test_a_flat_experiment_is_rejected_and_its_branch_kept(self):
        self.make()
        pilot = self.run_pilot([{**MAIN, "weighted": 96_000}])
        self.assert_rejected(pilot, "- rejected: T-001 (hidden 4→4, judge 3→3, tokens/ticket -4%)")
        self.assertIn("quality held, tokens per merged ticket not down 10%", self.log())

    def test_a_worse_experiment_is_rejected_and_its_branch_kept(self):
        self.make()
        worse = {**MAIN, "passed": 3, "weighted": 50_000}
        pilot = self.run_pilot([worse, worse])
        record = self.assert_rejected(pilot, "- rejected: T-001 (hidden 4→3, judge 3→3, tokens/ticket -50%)")
        self.assertIn("- Verdict: hidden tests down", record)  # cheaper doesn't save a drop in quality

    def test_main_moving_during_the_bench_benches_only_mains_side_again(self):
        self.make()
        better = {**MAIN, "passed": 6}
        pilot = self.run_pilot([better, better], moved={**MAIN, "passed": 7})
        self.assertEqual(self.calls, [self.head(), self.head(), self.moved])
        self.assertIn("T-001: main moved on during the experiment's bench", self.log())
        self.assertEqual(pilot.merged, [])  # against the new main (7 of 10), the head's 6 is worse
        self.assertIn("- rejected: T-001 (hidden 7→6, judge 3→3, tokens/ticket +0%)",
                      self.on_main(".windvane/QUEUE.md").splitlines())

    def test_a_bench_that_doesnt_fit_before_the_deadline_sets_the_ticket_aside_before_its_review(self):
        self.make()
        with mock.patch.object(experiment, "bench_command", lambda p, a: self.fail("no bench should start")):
            pilot = driver.Pilot(self.p, args())
            pilot.run(now() + timedelta(hours=1))  # a run is 60 min of seed runs and 30 for its one seed's judge
        self.assertEqual(pilot.merged, [])
        self.assertIn("T-001: the experiment's next bench run (up to 90 min) doesn't fit before the deadline",
                      self.log())
        self.assertEqual((self.sessions("worker"), self.sessions("review")), (1, 0))  # no review paid for, no rework
        self.assertIn("T-001", pilot.aside)

    def test_a_bench_cut_off_by_a_stop_goes_on_in_the_next_run_without_a_worker_or_review(self):
        self.make()
        better = {**MAIN, "passed": 6}
        self.stop_after_first = True
        self.run_pilot([better, better])
        self.assertEqual((len(self.calls), self.sessions("worker"), self.sessions("review")), (1, 1, 1))
        self.assertIn("the next run benches it", self.log())  # a stop ends its job, or comes between two runs
        (self.p.state / "STOP").unlink(missing_ok=True)  # the run that stopped may have cleared it
        self.stop_after_first = False
        pilot = self.run_pilot([better, better])
        self.assertEqual(pilot.merged, ["T-001"])
        self.assertIn("T-001: t-001-work is still at its approved head", self.log())
        self.assertEqual((self.calls, self.sessions("worker"), self.sessions("review")),
                         ([self.head(), self.head()], 1, 1))  # the recorded run reused; nothing paid for twice
        self.assertEqual(json.loads((self.p.state / "state.json").read_text()).get("benching"), {})

    def test_a_restart_while_its_bench_job_runs_waits_for_that_job(self):
        self.make()
        better = {**MAIN, "passed": 6}
        self.stop_after_first = True
        self.run_pilot([better, better])
        (self.p.state / "STOP").unlink(missing_ok=True)
        self.stop_after_first = False
        # the driver died while its second bench run went on: the job is still there when the next one starts
        script = "import time; time.sleep(1)\n" + FAKE
        jobs.bg(self.p, "experiment-T-001", [sys.executable, "-c", script, json.dumps([rec(
            self.head(), seed="tiny", **better)]), str(bench.results(self.p))], self.p.root, quiet=True)
        pilot = self.run_pilot([better, better])
        self.assertEqual(pilot.merged, ["T-001"])
        self.assertIn("T-001: its bench job from before a restart still runs; waiting for it", self.log())
        self.assertEqual((self.calls, self.sessions("worker"), self.sessions("review")), ([self.head()], 1, 1))

    def test_a_ticket_without_kind_experiment_merges_without_a_bench(self):
        self.make(kind="")
        pilot = self.run_pilot([{**MAIN, "passed": 0}])
        self.assertEqual((pilot.merged, self.calls), (["T-001"], []))
        self.assertNotIn("experiment", self.log())


class Planner(unittest.TestCase):
    def test_the_planners_prompt_holds_the_rejected_experiments(self):
        s = Sandbox()
        self.addCleanup(s.close)
        s.write(".windvane/EXPERIMENTS.md", experiment.HEADER + experiment.record(
            "T-005", "a shorter worker prompt", "t-005-short", ["prompts/WORKER.md"],
            [{"name": "main", "sha": "a" * 40, "runs": [[]]}, {"name": "T-005", "sha": "b" * 40, "runs": [[], []]}],
            "hidden tests down"))
        s.publish()
        planner.plan(Project(s.root), "make it better")
        calls = [json.loads(x) for x in (s.tmp / "calls.jsonl").read_text().splitlines()]
        prompt = next(c["prompt"] for c in calls if c["system"].startswith("# windvane planner run"))
        self.assertIn("## Experiments the bench rejected (.windvane/EXPERIMENTS.md)\n# Experiments the bench rejected",
                      prompt)
        self.assertIn("### T-005: a shorter worker prompt", prompt)
        self.assertIn("- Files: prompts/WORKER.md", prompt)
        self.assertIn("`Kind: experiment`", (REPO / "prompts/PLAN.md").read_text())

    def test_a_long_record_shows_its_newest_entries_whole(self):
        s = Sandbox()
        self.addCleanup(s.close)
        entries = "".join(f"\n### T-{i:03}: idea {i}\n- Idea: idea {i}\n" + "- Files: x\n" * 40 for i in range(30))
        s.write(".windvane/EXPERIMENTS.md", experiment.HEADER + entries)
        part = planner.rejected_part(s.root)
        self.assertLessEqual(len(part), planner.EXPERIMENTS_SHOWN + 200)
        self.assertIn("(older records left out)\n### T-", part)
        self.assertTrue(part.rstrip().endswith("- Files: x"))
        self.assertIn("### T-029: idea 29", part)
        self.assertNotIn("### T-000", part)
        self.assertEqual(planner.rejected_part(s.tmp), "")  # none rejected: no section


class Verdict(unittest.TestCase):
    def side(self, sha, *runs):
        return {"name": sha, "sha": sha * 7, "runs": [[rec(sha * 7, **r)] for r in runs]}

    def test_the_noise_rule_decides(self):
        main = self.side("a", MAIN)
        cases = [
            ([{**MAIN, "passed": 5}, {**MAIN, "passed": 5}], True),  # one more hidden test, both runs: past the spread
            ([{**MAIN, "passed": 5}, {**MAIN, "passed": 4}], False),  # 4.5 within the head's own spread of 1
            ([{**MAIN, "yes": 4}, {**MAIN, "yes": 4}], True),  # a judge yes more
            ([{**MAIN, "passed": 6, "yes": 2}, {**MAIN, "passed": 6, "yes": 2}], False),  # a rise doesn't buy a drop
            ([{**MAIN, "weighted": 90_000}, {**MAIN, "weighted": 90_000}], True),  # exactly 10% fewer tokens
            ([{**MAIN, "weighted": 85_000}, {**MAIN, "weighted": 95_000}], False),  # 10% down, inside a 10k spread
        ]
        for runs, kept in cases:
            with self.subTest(runs=runs):
                self.assertEqual(bench.experiment_verdict([main, self.side("b", *runs)], 10)[0], kept)
        self.assertEqual(bench.experiment_verdict([main, {"name": "b", "sha": "b" * 7, "runs": []}], 10)[0], None)


if __name__ == "__main__":
    unittest.main()
