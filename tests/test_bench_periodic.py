"""The bench as a [[periodic]] entry: due once tickets touching its `after_touch` paths merge, started in the
background (the run goes on with tickets), and a regression against the windvane last benchmarked written up as one
ticket and one push. The bench itself is a fake job that records results, as `windvane bench` would."""
import io
import json
import sys
import time
import unittest
from datetime import timedelta
from pathlib import Path
from contextlib import redirect_stdout
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from windvane import bench, driver, jobs, periodic  # noqa: E402
from windvane.config import Project, periodic as entries  # noqa: E402
from windvane.queue import Queue  # noqa: E402
from windvane.util import append_jsonl, now  # noqa: E402
from helpers import Sandbox  # noqa: E402
from test_pipeline import args  # noqa: E402

BENCH = '[[periodic]]\nname = "bench"\nbuiltin = "bench"\nafter_touch = ["prompts/", "windvane/driver.py"]\n' \
        'minutes = 120\n'
# The fake bench: appends the records it is given, with its own run id, as `windvane bench` does at a run's end
FAKE = ("import json, sys, time; from datetime import datetime\n"
        "time.sleep(float(sys.argv[3]))\n"
        "run = f'{datetime.now():%Y%m%d-%H%M%S}-fake'\n"
        "with open(sys.argv[2], 'a') as f:\n"
        "    for r in json.loads(sys.argv[1]):\n"
        "        f.write(json.dumps({**r, 'run': run}) + '\\n')\n")


def rec(sha, seed="alpha", passed=8, yes=5, weighted=300_000, merged=3, run="20261001-000000"):
    """One seed's bench record, as `windvane bench` writes it."""
    ts = [f"S-{i}" for i in range(merged)]
    return {"run": run, "ref": sha, "sha": sha, "size": "full", "seed": seed,
            "tickets": {"merged": ts, "failed": [], "parked": [], "open": []},
            "hidden": {"S-0": {"passed": passed, "total": 10}}, "passed": passed, "total": 10,
            "judge": {"yes": yes, "total": 6, "answers": [], "weighted": 1000, "usd": 0, "stopped": None, "error": None},
            "weighted": weighted, "usd": 1.0, "per_merged": {}, "minutes": 30, "week_before": 10, "week_after": 12,
            "contaminated": False, "hits": [], "exit": 0}


class Base(unittest.TestCase):
    def make(self, config=BENCH, tickets=0):
        self.s = Sandbox(config)
        self.addCleanup(self.s.close)
        for i in range(1, tickets + 1):
            self.s.ticket(f"T-00{i}")
        self.s.write("prompts/WORKER.md", "the worker's rules\n")
        self.s.publish()
        self.p = Project(self.s.root)
        self.first = self.s.git("rev-parse", "HEAD")
        self.calls = []

    def record(self, *recs):
        for r in recs:
            append_jsonl(bench.results(self.p), r)

    def merge(self, subject, path):
        """A change on main, as a ticket's merge leaves it."""
        self.s.write(path, f"{subject}\n")
        self.s.publish(subject)
        return self.s.git("rev-parse", "HEAD")

    def fake(self, recs, sleep=0.0):
        """bench_command's stand-in: records `recs` (made for the head it's given) after `sleep` seconds."""
        def command(p, a):
            self.calls.append(a)
            head = a[1] if a[0] == "--ref" else a[2]
            return [sys.executable, "-c", FAKE, json.dumps([rec(head, **r) for r in recs]), str(bench.results(p)),
                    str(sleep)]
        return mock.patch.object(periodic, "bench_command", command)

    def pilot(self, hours=3, week=10):
        pilot = driver.Pilot(self.p, args())
        pilot.deadline = now() + timedelta(hours=hours)
        pilot.usage.week = week
        self.assertTrue(pilot.prepare(0))
        return pilot

    def ended(self):
        job = periodic.bench_job(self.p, "bench")
        until = time.time() + 30
        while jobs.alive(job) or not Path(f"{job}.exit").exists():
            self.assertLess(time.time(), until, "the fake bench never ended")
            time.sleep(0.05)

    def log(self):
        f = self.p.state / "autopilot.log"
        return f.read_text() if f.exists() else ""

    def tickets_on_main(self):
        self.s.git("fetch", "-q", "origin")
        return self.s.git("ls-tree", "--name-only", "origin/main", ".windvane/tickets/").split()


class Due(Base):
    def test_a_merge_touching_prompts_makes_it_due_and_one_touching_only_docs_doesnt(self):
        self.make()
        self.record(rec(self.first))
        self.merge("Merge T-005: reword the guide", "docs/guide.md")
        pilot = self.pilot()
        self.assertIsNone(pilot.periodic_due())
        head = self.merge("Merge T-006: a shorter worker prompt (branch t-006-short) [skip ci]", "prompts/WORKER.md")
        due = pilot.periodic_due()
        self.assertEqual((due["name"], due["bench"]), ("bench", "start"))
        self.assertEqual(periodic.touching(self.p, self.first, head, ["prompts/"]),
                         [["T-006", "a shorter worker prompt", ["prompts/WORKER.md"]]])

    def test_a_round_started_at_main_is_not_due_again_until_a_new_change(self):
        self.make()
        self.record(rec(self.first))
        self.merge("Merge T-006: a shorter worker prompt", "prompts/WORKER.md")
        pilot = self.pilot()
        with self.fake([{}], sleep=30):
            pilot.periodic(pilot.periodic_due())
        self.addCleanup(jobs.end_job, periodic.bench_job(self.p, "bench"), "test over")
        self.assertIsNone(pilot.periodic_due())  # running
        self.assertIn("bench: periodic bench running since", "\n".join(periodic.bench_status(self.p)))
        self.assertIn("`windvane await periodic-bench` follows it", periodic.bench_status(self.p)[0])

    def test_config(self):
        e = entries([{"name": "b", "builtin": "bench", "after_touch": "prompts/"}])["b"]
        self.assertEqual((e["after_touch"], e["bench_cost_up"], e["minutes"]), (["prompts/"], 25, 300))
        for bad in ({"name": "x", "prompt": "a.md", "after_touch": ["prompts/"]},
                    {"name": "x", "builtin": "bench", "after_touch": [""]},
                    {"name": "x", "builtin": "bench", "bench_cost_up": -1}):
            with self.assertRaises(SystemExit):
                entries([bad])


class Rounds(Base):
    def start(self, recs, push=None):
        """A round started on main, ended, and its verdict read at the next gap: (pilot, the push mock)."""
        pilot = self.pilot()
        with self.fake(recs), mock.patch.object(periodic, "push") as pushed:
            due = pilot.periodic_due()
            self.assertEqual(due["bench"], "start")
            pilot.periodic(due)
            self.ended()
            due = pilot.periodic_due()
            if due:
                pilot.periodic(due)
            self.assertIsNone(pilot.periodic_due())  # said once
        return pilot, pushed

    def test_the_first_run_records_a_baseline_without_a_ticket(self):
        self.make()
        before = self.tickets_on_main()
        pilot, pushed = self.start([{}])
        self.assertEqual(self.calls[0][:2], ["--ref", self.first])
        self.assertIn(f"bench: bench baseline recorded at {self.first[:7]}: hidden tests 8/10, judge yes 5/6, "
                      "100k tokens/merged", self.log())
        self.assertEqual(self.tickets_on_main(), before)
        pushed.assert_not_called()

    def test_a_regression_writes_one_ticket_with_both_sides_numbers_and_one_push(self):
        self.make()
        self.record(rec(self.first), rec(self.first, seed="beta"))
        self.merge("Merge T-006: a shorter worker prompt", "prompts/WORKER.md")
        head = self.merge("Merge T-007: a docs fix", "docs/x.md")
        pilot, pushed = self.start([{"passed": 5}, {"seed": "beta"}])
        self.assertEqual(self.calls[0][:3], ["compare", self.first, head])
        new = [t for t in self.tickets_on_main() if t.endswith("T-001.md")]
        self.assertEqual(len(new), 1)
        text = self.s.git("show", f"origin/main:{new[0]}")
        self.assertIn("# T-001: Bench regression after T-006\n", text)
        self.assertIn("hidden tests down 3", text)
        self.assertIn("- T-006: a shorter worker prompt (prompts/WORKER.md)", text)
        self.assertNotIn("T-007", text)  # it touched none of the paths
        self.assertIn("alpha: hidden tests 8/10 → 5/10", text)  # the seed that moved; beta didn't
        self.assertNotIn("beta:", text.split("## Seeds that moved")[1])
        self.assertIn(f"A = last benchmarked ({self.first[:7]})", text)
        self.assertIn("16/20", text)  # both sides' totals, in the compare table
        self.assertIn("13/20", text)
        self.assertEqual(Queue(self.p).order, ["T-001"])
        self.assertEqual(pushed.call_count, 1)
        self.assertIn("hidden tests down 3 after T-006. T-001 written (queued); nothing was reverted.",
                      pushed.call_args[0][1])

    def test_cost_up_past_bench_cost_up_is_a_regression(self):
        self.make()
        self.record(rec(self.first))
        self.merge("Merge T-006: more context for the reviewer", "windvane/driver.py")
        _, pushed = self.start([{"weighted": 400_000}])
        self.assertIn("weighted tokens per merged ticket up 33% (more than 25%)", self.log())
        self.assertEqual(pushed.call_count, 1)

    def test_a_flat_result_writes_neither_a_ticket_nor_a_push(self):
        self.make()
        self.record(rec(self.first))
        self.merge("Merge T-006: a shorter worker prompt", "prompts/WORKER.md")
        before = self.tickets_on_main()
        _, pushed = self.start([{"passed": 7, "weighted": 320_000}])  # one test is noise; +7% is under 25%
        self.assertIn(f"bench: bench {self.first[:7]} → ", self.log())
        self.assertIn("no regression (hidden tests 8/10 → 7/10, judge yes 5/6 → 5/6, tokens/merged 100k → 107k)",
                      self.log())
        self.assertEqual(self.tickets_on_main(), before)
        pushed.assert_not_called()

    def test_a_job_that_records_nothing_is_one_push(self):
        self.make()
        self.record(rec(self.first))
        self.merge("Merge T-006: a shorter worker prompt", "prompts/WORKER.md")
        _, pushed = self.start([])
        self.assertEqual(pushed.call_count, 1)
        self.assertIn("bench round failed", pushed.call_args[0][0])


class Caps(Base):
    def setUp(self):
        self.make()
        self.record(rec(self.first))
        self.merge("Merge T-006: a shorter worker prompt", "prompts/WORKER.md")

    def not_started(self, pilot):
        with self.fake([{}]):
            pilot.periodic(pilot.periodic_due())
        self.assertEqual(self.calls, [])
        self.assertFalse(periodic.bench_file(self.p, "bench").exists())
        self.assertIsNone(pilot.periodic_due())  # not again at this head

    def test_a_stop_keeps_it_from_starting(self):
        pilot = self.pilot()
        (self.p.state / "STOP").write_text("now")
        self.not_started(pilot)
        self.assertIn("bench: bench not started: stopped", self.log())

    def test_a_near_deadline_keeps_it_from_starting(self):
        self.not_started(self.pilot(hours=1))
        self.assertIn("bench: skipped: 120 min don't fit before the deadline", self.log())

    def test_over_the_weekly_cap_it_doesnt_start(self):
        self.not_started(self.pilot(week=96))
        self.assertIn("bench: bench not started: weekly usage at 96% (cap 95%)", self.log())

    def test_it_runs_with_the_runs_caps_sized_by_the_weekly_usage(self):
        pilot = self.pilot(hours=3, week=80)
        with self.fake([{}], sleep=30):
            pilot.periodic(pilot.periodic_due())
        self.addCleanup(jobs.end_job, periodic.bench_job(self.p, "bench"), "test over")
        a = self.calls[0]
        self.assertIn("--small", a)  # over bench_week_small's 75%
        self.assertEqual(a[a.index("--cap") + 1:a.index("--cap") + 2] + a[a.index("--week-cap") + 1:][:1],
                         ["75", "95"])
        self.assertEqual(a[a.index("--hours") + 1], "0.5")  # 120 min, two runs (A isn't recorded small), a judge each


class Run(Base):
    def test_the_run_works_tickets_while_the_bench_runs_then_waits_for_its_verdict(self):
        self.make(tickets=2)
        self.record(rec(self.first))
        self.merge("Merge T-009: a shorter worker prompt", "prompts/WORKER.md")
        pilot = driver.Pilot(self.p, args())
        with self.fake([{}], sleep=4), mock.patch.object(driver.Pilot, "pause", lambda self, seconds=30: time.sleep(.2)):
            pilot.run(now() + timedelta(hours=3))
        self.assertEqual(pilot.merged, ["T-001", "T-002"])
        log = self.log()
        started = log.index("bench: bench started in the background")
        self.assertIn("after T-009", log)
        self.assertGreater(log.index("T-002", started), started)  # worked after it started
        waited = log.index("waiting for the bench bench round to end")  # the queue was done before the bench
        self.assertGreater(log.index("no regression", waited), waited)  # read before the run ended
        self.assertNotIn("ended with the run", log)


    def test_a_bench_that_ends_during_the_runs_last_pass_is_read_before_the_run_ends(self):
        self.make()
        self.record(rec(self.first))
        self.merge("Merge T-009: a shorter worker prompt", "prompts/WORKER.md")
        first = self.pilot()
        with self.fake([{"passed": 4}]):
            first.periodic(first.periodic_due())
        self.ended()
        real, looks = driver.Pilot.periodic_due, []

        def due(pilot):  # the pass's look came just before the bench ended: it saw nothing to do
            looks.append(1)
            return None if len(looks) == 1 else real(pilot)
        pilot = driver.Pilot(self.p, args())
        with mock.patch.object(driver.Pilot, "periodic_due", due), mock.patch.object(periodic, "push") as pushed:
            pilot.run(now() + timedelta(hours=3))
        self.assertIn("bench regression", self.log())
        self.assertEqual(pushed.call_count, 1)
        self.assertIn("T-001", pilot.merged)  # its ticket landed, and the run worked it


class Unfinished(Base):
    def setUp(self):
        self.make()
        self.record(rec(self.first))
        self.merge("Merge T-006: a shorter worker prompt", "prompts/WORKER.md")

    def started(self):
        pilot = self.pilot()
        with self.fake([{}], sleep=600):
            pilot.periodic(pilot.periodic_due())
        self.assertTrue(jobs.alive(periodic.bench_job(self.p, "bench")))
        return pilot

    def test_a_stop_ends_it_with_one_push_and_the_round_stays_due(self):
        pilot = self.started()
        pilot.stop_reason = "stopped now: the running sessions were killed"
        with mock.patch.object(periodic, "push") as pushed:
            pilot.bench_at_end()
        self.assertFalse(jobs.alive(periodic.bench_job(self.p, "bench")))
        self.assertEqual(pushed.call_count, 1)
        self.assertIn("was ended with the run (stopped now", pushed.call_args[0][1])
        self.assertIn("The next run starts it again.", pushed.call_args[0][1])
        self.assertIn("bench: bench ended with the run (stopped now", self.log())
        again = self.pilot()  # the next run
        self.assertEqual(again.periodic_due()["bench"], "start")

    def test_a_restarted_run_waits_for_a_round_it_did_not_start_and_ends_it_at_its_end(self):
        self.started()
        again = self.pilot()  # a restart: a fresh Pilot, the job still running
        self.assertIsNone(again.periodic_due())
        self.assertEqual(len(again.bench_waits()), 1)
        out = io.StringIO()
        with redirect_stdout(out):
            jobs.await_(self.p, "periodic-bench", limit=0)  # the desk can follow it as the status line says
        self.assertIn("RUNNING periodic-bench", out.getvalue())
        again.stop_reason = "deadline"
        with mock.patch.object(periodic, "push"):
            again.bench_at_end()
        self.assertFalse(jobs.alive(periodic.bench_job(self.p, "bench")))
        self.assertEqual(periodic.bench_status(self.p), [])

    def test_a_round_that_ran_to_its_end_with_nothing_recorded_waits_for_a_new_change(self):
        pilot = self.pilot()
        with self.fake([]), mock.patch.object(periodic, "push"):
            pilot.periodic(pilot.periodic_due())
            self.ended()
            self.assertIsNone(pilot.periodic_due())
            self.assertIsNone(self.pilot().periodic_due())  # nor in the next run
            self.merge("Merge T-007: another prompt change", "prompts/CRITIC.md")
            self.assertEqual(pilot.periodic_due()["bench"], "start")


class Regression(unittest.TestCase):
    """bench.regression's rule, on its own."""
    def side(self, *runs):
        return {"name": "x", "sha": "x", "runs": [[rec("x", **r)] for r in runs]}

    def test_rules(self):
        cases = [
            ([{}], [{"passed": 7}], []),  # one hidden test, one run a side: noise
            ([{}], [{"passed": 6}], ["hidden tests down 2"]),
            ([{}], [{"yes": 3}], ["judge yes down 2"]),
            ([{}], [{"weighted": 380_000}], ["weighted tokens per merged ticket up 27% (more than 25%)"]),
            ([{}], [{"weighted": 380_000, "passed": 10}], []),  # dearer, but better
            ([{}, {"passed": 6}], [{"passed": 5}, {"passed": 5}], []),  # within A's own spread
            ([{}, {}], [{"passed": 7}, {"passed": 7}], ["hidden tests down 1"]),  # past a spread of 0
        ]
        for a, b, want in cases:
            with self.subTest(a=a, b=b):
                self.assertEqual(bench.regression([self.side(*a), self.side(*b)])[0], want)
        self.assertEqual(bench.regression([self.side({}), self.side({"weighted": 380_000})], cost_up=30)[0], [])


if __name__ == "__main__":
    unittest.main()
