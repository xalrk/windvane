"""The training pool and the frozen holdout: a `seed` round's session writes a new seed to `bench/pool/`, which joins
only when its hidden tests fail on the bare seed and pass on the session's reference (never committed), the oldest
leaving past `pool_size`; only the seed round's own branch may write there. The automatic benches (an experiment's
gate, the bench round) run on the pool, `windvane bench` by hand on the holdout, and the pool rising while the
holdout falls stops experiment planning with one push."""
import io
import json
import os
import subprocess
import sys
import unittest
from argparse import Namespace
from contextlib import redirect_stdout
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from windvane import bench, driver, experiment, held, periodic, planner, pool  # noqa: E402
from windvane.config import Project, periodic as entries  # noqa: E402
from windvane.util import append_jsonl, git, now  # noqa: E402
from windvane.worktree import clean_detach, ensure_worktree  # noqa: E402
from helpers import Sandbox  # noqa: E402
from test_bench_periodic import rec  # noqa: E402
from test_pipeline import args  # noqa: E402

SEED = '[[periodic]]\nname = "seed"\nbuiltin = "seed"\npool_size = 2\nminutes = 30\n'
PROGRAM = "import sys\nprint({out!r})\n"
ACCEPT = ("import subprocess, sys, unittest\n\n\nclass Done(unittest.TestCase):\n"
          "    def test_done(self):\n"
          "        r = subprocess.run([sys.executable, 'tool.py'], capture_output=True, text=True)\n"
          "        self.assertEqual(r.stdout.strip(), 'done')\n")


def write(root, files):
    for rel, text in files.items():
        f = Path(root) / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text)


def seed_folder(where, kind, reference="done"):
    """A session's seed as it leaves it: `new-<kind>/` with seed/, hidden/ and reference/ (its tool prints
    `reference`; the bare seed's prints `todo`)."""
    d = Path(where) / f"new-{kind}"
    write(d, {
        "seed/CLAUDE.md": f"# {kind}\n", "seed/tool.py": PROGRAM.format(out="todo"),
        "seed/tests/test_smoke.py": "import unittest\n",
        "seed/.windvane/config.toml": f'name = "{kind}"\nforge = "git"\ngoal = "a {kind}"\n',
        "seed/.windvane/QUEUE.md": "# Queue\n\n- T-001 Finish it\n",
        "seed/.windvane/tickets/T-001.md": "# T-001: Finish it\nDepends on: nothing.\n",
        "hidden/bench.toml": "command = \"python3 -m unittest discover -p 'accept_*.py' -v\"\n\n[tickets]\n"
                             "\"T-001\" = [\"accept_done\"]\n",
        "hidden/accept_done.py": ACCEPT, "hidden/JUDGE.md": "- J1: `python3 tool.py` says done.\n",
        "reference/tool.py": PROGRAM.format(out=reference)})
    return d


class Base(unittest.TestCase):
    def make(self, config=SEED):
        self.s = Sandbox(config)
        self.addCleanup(self.s.close)
        self.s.ticket("T-001")
        self.s.write("bench/ledger/seed/.windvane/config.toml", 'forge = "git"\n')
        self.s.write("bench/ledger/hidden/bench.toml", "")
        self.s.publish()
        self.p = Project(self.s.root)
        self.pushes = []
        patch = mock.patch.object(periodic, "push", lambda title, msg="", *a, **k: self.pushes.append((title, msg)))
        patch.start()
        self.addCleanup(patch.stop)
        os.environ.pop("FAKE_SEED", None)
        self.addCleanup(os.environ.pop, "FAKE_SEED", None)

    def pilot(self):
        pilot = driver.Pilot(self.p, args())
        pilot.deadline = now() + timedelta(hours=3)
        pilot.usage.week = 10
        self.assertTrue(pilot.prepare(0))
        return pilot

    def round(self, pilot, kind, reference="done"):
        """One seed round, its session writing the seed `kind`: due, then run."""
        os.environ["FAKE_SEED"] = str(seed_folder(self.s.tmp / kind, kind, reference))
        due = pilot.periodic_due()
        self.assertEqual(due and due["name"], "seed")
        pilot.periodic(due)

    def main_files(self, path):
        self.s.git("fetch", "-q", "origin")
        return self.s.git("ls-tree", "-r", "--name-only", "origin/main", path).split()

    def log(self):
        return (self.p.state / "autopilot.log").read_text()

    def ledger(self):
        f = self.p.state / "ledger.jsonl"
        return [r for r in map(json.loads, f.read_text().splitlines()) if r.get("event") == "seed"] if f.exists() \
            else []


class SeedRound(Base):
    def test_a_seed_whose_hidden_tests_fail_on_its_reference_is_refused(self):
        self.make()
        pilot = self.pilot()
        self.round(pilot, "todo", reference="still todo")
        self.assertEqual(self.main_files("bench/pool"), [])
        self.assertNotIn("stray", (self.p.ci_dir / "README").read_text() if (self.p.ci_dir / "README").exists()
                         else "")
        [(title, msg)] = self.pushes
        self.assertEqual(title, "Bench seed refused")
        self.assertIn("the hidden tests fail on the reference solution: T-001 (0/1)", msg)
        self.assertEqual([r["verdict"] for r in self.ledger()], ["refused"])
        self.assertFalse(pilot.periodic_due())  # once a run
        for _ in range(2):  # the next runs (or restarts) try again, three times in all at one head of main
            self.round(self.pilot(), "todo", reference="still todo")
        self.assertEqual(len(self.pushes), 3)
        self.assertTrue(self.pushes[-1][1].endswith(
            f"That was try 3 at main {self.s.git('rev-parse', '--short=7', 'HEAD')}: no more until main moves on, or "
            "`windvane periodic seed`. Meanwhile experiments wait: the training pool has 0 of its 2 seeds."),
            self.pushes[-1][1])
        self.assertFalse(self.pilot().periodic_due())  # no fourth session at this head
        self.s.write("docs/x.md", "a merge\n")
        self.s.publish("Merge T-009: something")
        self.assertEqual(self.pilot().periodic_due()["name"], "seed")

    def test_a_seed_whose_tests_already_pass_on_the_bare_seed_is_refused(self):
        self.make()
        d = seed_folder(self.s.tmp, "todo")
        write(d, {"seed/tool.py": PROGRAM.format(out="done")})
        self.assertIn("T-001 already pass on the bare seed", pool.check(d))

    def test_a_good_seed_joins_and_the_oldest_leaves_past_pool_size(self):
        self.make(SEED + "seed_every = 1\n")
        pilot = self.pilot()
        for i, kind in enumerate(("todo", "timer", "quiz")):
            pilot.merged.append(f"T-01{i}")  # one round a merge
            if i == 2:  # the pool is full: the next seed comes after a kept experiment
                self.assertIsNone(pilot.periodic_due())
                append_jsonl(self.p.state / "ledger.jsonl", {"event": "experiment", "verdict": "kept"})
            self.round(pilot, kind)
        files = self.main_files("bench/pool")
        self.assertEqual(sorted({f.split("/")[2] for f in files}), ["002-timer", "003-quiz"])
        self.assertIn("bench/pool/003-quiz/hidden/accept_done.py", files)
        self.assertFalse([f for f in files if "/reference/" in f or f.endswith("README")])  # nothing else lands
        self.assertNotIn("stray", self.s.git("show", "origin/main:README"))  # the session wrote it
        self.assertEqual([r.get("seed") for r in self.ledger()], ["001-todo", "002-timer", "003-quiz"])
        self.assertEqual(self.ledger()[-1]["dropped"], ["001-todo"])
        self.assertEqual(self.pushes[-1], ("Bench seed added", "Seed: 003-quiz joins the bench pool, 001-todo leaves. "
                                                               "new-quiz: a seed for the test."))
        self.assertEqual(bench.planned(self.p, "origin/main", "full", True), ["002-timer", "003-quiz"])
        self.assertEqual(bench.planned(self.p, "origin/main", "full"), ["ledger"])  # the holdout is untouched
        self.assertEqual(self.s.git("log", "-1", "--format=%s", "origin/main"),
                         "Seed: 003-quiz joins the bench pool, 001-todo leaves")

    def test_a_full_pool_is_due_again_after_seed_every_kept_experiments(self):
        self.make(SEED + "seed_every = 2\n")
        pilot = self.pilot()
        for i, kind in enumerate(("todo", "timer")):
            pilot.merged.append(f"T-01{i}")
            self.round(pilot, kind)
        pilot.merged.append("T-020")
        self.assertIsNone(pilot.periodic_due())  # full, and nothing kept since
        append_jsonl(self.p.state / "ledger.jsonl", {"event": "experiment", "ticket": "T-020", "verdict": "kept"})
        self.assertIsNone(pilot.periodic_due())
        append_jsonl(self.p.state / "ledger.jsonl", {"event": "experiment", "ticket": "T-021", "verdict": "kept"})
        self.assertEqual(pilot.periodic_due()["name"], "seed")

    def test_a_kind_taken_by_the_holdout_is_refused(self):
        self.make()
        pilot = self.pilot()
        self.round(pilot, "ledger")
        self.assertIn("ledger is in the pool or the holdout already", self.pushes[0][1])
        self.assertEqual(self.main_files("bench/pool"), [])

    def test_config(self):
        e = entries([{"name": "s", "builtin": "seed"}])["s"]
        self.assertEqual((e["seed_every"], e["pool_size"]), (5, 4))
        for bad in ({"name": "x", "builtin": "seed", "pool_size": 0}, {"name": "x", "prompt": "a.md", "seed_every": 3},
                    {"name": "x", "builtin": "seed", "seed_every": True}):
            with self.assertRaises(SystemExit):
                entries([bad])


class Gate(Base):
    def test_an_experiment_waits_for_a_full_pool_rather_than_merging_unmeasured(self):
        import test_experiments  # the module, not its class: a class imported here would run all its tests again
        e = test_experiments.Experiment()
        e.make()
        self.addCleanup(e.doCleanups)
        with open(e.s.root / ".windvane/config.toml", "a") as f:
            f.write(SEED)
        e.s.publish("a seed round")
        e.p = Project(e.s.root)
        self.assertEqual(bench.pool_short(e.p), "the training pool has 0 of its 2 seeds")
        pilot = e.run_pilot([test_experiments.MAIN])
        self.assertEqual((pilot.merged, list(pilot.aside), e.calls), ([], ["T-001"], []))  # no bench on the holdout
        self.assertIn("T-001: the training pool has 0 of its 2 seeds: the experiment waits, not failed", e.log())
        self.assertEqual((e.sessions("worker"), e.sessions("critic")), (0, 0))  # set aside before it's paid for
        self.assertEqual(json.loads((e.p.state / "seed-seed.json").read_text())["tries"], 1)  # its seed was tried

    def test_experiments_resume_by_hand(self):
        self.make()
        experiment.stop(self.p, "the training pool rose while the frozen holdout fell this week")
        run = lambda *a: subprocess.run(["windvane", "-C", str(self.s.root), "experiments", *a], capture_output=True,
                                        text=True).stdout
        self.assertIn("experiments stopped: the training pool rose while the frozen holdout fell this week; "
                      "`windvane experiments resume` plans them again", run())
        self.assertEqual(run("resume"), "experiments: planned again\n")
        self.assertEqual(run("resume"), "experiments: weren't stopped\n")
        self.assertEqual(experiment.stopped(self.p), "")


class OnlyTheSeedBranch(Base):
    def land(self, branch):
        cd = self.p.plan_dir
        ensure_worktree(self.p, cd, setup=False)
        clean_detach(self.p, cd, self.p.upstream)
        write(cd, {"bench/pool/001-todo/seed/tool.py": "print('x')\n"})
        git(cd, "add", "-A")
        with mock.patch.object(held, "push"):
            return held.land(self.p, cd, branch, "Seed: 001-todo joins the bench pool", "body")

    def test_the_seed_branch_lands_and_a_plan_branch_is_held(self):
        self.make()
        self.assertFalse(self.land("plan-x")[0])
        self.assertTrue(self.land("seed-x")[0])
        self.assertIn("bench/pool/001-todo/seed/tool.py", self.main_files("bench/pool"))

    def test_the_seed_branch_may_not_write_the_holdout(self):
        self.make()
        cd = self.p.plan_dir
        ensure_worktree(self.p, cd, setup=False)
        clean_detach(self.p, cd, self.p.upstream)
        write(cd, {"bench/ledger/hidden/bench.toml": "command = 'true'\n"})
        git(cd, "add", "-A")
        with mock.patch.object(held, "push"):
            self.assertFalse(held.land(self.p, cd, "seed-x", "Seed: sneaky", "body")[0])

    def test_a_worker_branch_writing_under_the_pool_is_held(self):
        self.make(config="")
        self.s.write("bench/pool/001-todo/seed/tool.py", "print('todo')\n")
        self.s.publish("a pool seed")
        pushes = []
        with mock.patch.object(driver, "push", lambda title, msg="", *a, **k: pushes.append((title, msg))):
            os.environ["FAKE_LEAK"] = "bench/pool/001-todo/seed/tool.py=print('done')"
            self.addCleanup(os.environ.pop, "FAKE_LEAK", None)
            pilot = driver.Pilot(self.p, args())
            pilot.run(now() + timedelta(hours=1))
        self.assertEqual((pilot.merged, list(pilot.aside)), ([], ["T-001"]))
        self.assertIn("changes protected paths: bench/pool/001-todo/seed/tool.py",
                      "\n".join(m for t, m in pushes if t.startswith("Held")))


class Sets(Base):
    def seeds_on_main(self):
        self.s.write("bench/pool/001-todo/seed/.windvane/config.toml", 'forge = "git"\n')
        self.s.write("bench/pool/001-todo/hidden/bench.toml", "")
        self.s.publish("a pool seed")
        return self.s.git("rev-parse", "HEAD")

    def test_the_gate_and_the_bench_round_run_on_the_pool(self):
        self.make()
        head = self.seeds_on_main()
        self.assertTrue(bench.training(self.p))
        self.assertEqual(periodic.bench_command(self.p, ["--ref", head])[-1], "--pool")
        append_jsonl(bench.results(self.p), rec(head, seed="ledger", run="20261001-000000"))
        self.assertEqual(periodic.bench_runs(self.p, head, "full"), [])  # a holdout run is never the gate's
        append_jsonl(bench.results(self.p), {**rec(head, seed="001-todo", run="20261002-000000"), "pool": True})
        self.assertEqual([rs[0]["run"] for rs in periodic.bench_runs(self.p, head, "full")], ["20261002-000000"])
        self.assertEqual(experiment.todo(self.p, head, head, "full", 2), [("the head", head)])
        self.assertEqual(periodic.last_benched(self.p, head), head)

    def test_without_a_seed_round_they_run_on_bench_as_before(self):
        self.make(config="")
        head = self.seeds_on_main()
        self.assertFalse(bench.training(self.p))
        self.assertNotIn("--pool", periodic.bench_command(self.p, ["--ref", head]))
        append_jsonl(bench.results(self.p), rec(head, seed="ledger"))
        self.assertEqual(len(periodic.bench_runs(self.p, head, "full")), 1)

    def test_bench_by_hand_runs_the_holdout_and_pool_runs_the_pool(self):
        self.make()
        self.seeds_on_main()
        for pool_, want in ((False, "  ledger: "), (True, "  001-todo: ")):
            a = Namespace(what=[], repeat=1, last=False, full=True, small=False, ref="HEAD", hours=1, dry_run=True,
                          pool=pool_, cap=None, week_cap=None)
            out = io.StringIO()
            with redirect_stdout(out):
                bench.main(a, self.p)
            self.assertIn(want, out.getvalue())
            self.assertIn("the training pool" if pool_ else "the holdout", out.getvalue())


class Overfit(Base):
    def runs(self, pool_scores, holdout_scores):
        """A run a day this week on each set, after any made before, quality `passed` as given."""
        start = datetime.now() - timedelta(days=5)
        for a, b in zip(pool_scores, holdout_scores):
            self.day = getattr(self, "day", -1) + 1
            at = f"{start + timedelta(days=self.day):%Y%m%d-%H%M%S}"
            append_jsonl(bench.results(self.p), {**rec("abc", seed="001-todo", passed=a, run=at + "-p"), "pool": True})
            append_jsonl(bench.results(self.p), rec("abc", seed="ledger", passed=b, run=at + "-h"))

    def test_pool_up_and_holdout_down_stops_experiment_planning_with_one_push(self):
        self.make()
        pilot = self.pilot()
        self.runs([5, 8], [8, 5])
        pilot.overfit_watch()
        [(title, msg)] = self.pushes
        self.assertEqual(title, "Experiments stopped: overfit")
        self.assertIn("pool up: hidden tests 5→8, judge yes 5→5", msg)
        self.assertIn("holdout down: hidden tests 8→5, judge yes 5→5", msg)
        self.assertIn("the training pool rose while the frozen holdout fell", experiment.stopped(self.p))
        self.assertIn("## Experiments are stopped", planner.stopped_part(self.p))
        self.runs([9], [4])  # a later holdout run: read again, but said once
        pilot.overfit_watch()
        pilot.overfit_watch()
        self.assertEqual(len(self.pushes), 1)
        self.assertTrue(experiment.resume(self.p))
        self.assertEqual(planner.stopped_part(self.p), "")

    def test_both_up_or_a_change_within_the_noise_is_no_alarm(self):
        self.make()
        pilot = self.pilot()
        self.runs([5, 8], [5, 8])
        pilot.overfit_watch()
        self.runs([8], [4])  # the holdout down by one from the week's first run: within the noise
        pilot.overfit_watch()
        self.assertEqual((self.pushes, experiment.stopped(self.p)), ([], ""))
        self.assertIn("pool up: hidden tests 5→8, judge yes 5→5; holdout flat: hidden tests 5→4", self.log())

    def test_runs_older_than_the_week_dont_count(self):
        self.make()
        old = f"{datetime.now() - timedelta(days=9):%Y%m%d-%H%M%S}"
        append_jsonl(bench.results(self.p), {**rec("abc", seed="001-todo", passed=2, run=old + "-p"), "pool": True})
        append_jsonl(bench.results(self.p), rec("abc", seed="ledger", passed=9, run=old + "-h"))
        self.runs([3], [8])
        alarmed, pool_line, hold_line = bench.overfit(self.p)
        self.assertFalse(alarmed)
        self.assertEqual((pool_line, hold_line), ("pool ?: 1 pool run this week", "holdout ?: 1 holdout run this week"))


if __name__ == "__main__":
    unittest.main()
