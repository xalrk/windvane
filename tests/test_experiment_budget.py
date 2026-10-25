"""The experiment budget: benches start only under `week_below` weekly usage and while this week's bench spend plus
the next run's estimate stays within `week_share` (plus what the user allowed this week); at the cap, one phone
question a week per cap. The usage replies are the fake claude's (FAKE_WEEK) or usage.json files; the spend rows and
ledgers are written by hand."""
import json
import os
import subprocess
import sys
import time
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from windvane import budget  # noqa: E402
from windvane.config import Project  # noqa: E402
from windvane.runner import Usage  # noqa: E402
from windvane.stats import weighted  # noqa: E402
from windvane.util import append_jsonl, load_json  # noqa: E402
from helpers import Sandbox  # noqa: E402
import test_bench_periodic  # noqa: E402
import test_experiments  # noqa: E402  (by module: its TestCase in this namespace would run its tests here too)
from test_merge_draft import args  # noqa: E402
from windvane import driver  # noqa: E402

CFG = {"week_below": 75, "week_share": 30}
DAY = 86400


def minute(t):
    return f"{datetime.fromtimestamp(t):%Y-%m-%d %H:%M}"


def spend(spent, size="full", ended=None):
    """A bench run's spend row, as `budget.record` writes it."""
    append_jsonl(budget.spend_file(), {"run": "r", "size": size, "ended": ended or time.time(), "spent": spent})


def usage(name, week, at, reset=None):
    """A project's last usage reply, as runner.Usage saves it."""
    f = budget.root() / name / "usage.json"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps({"week": week, "week_reset": reset, "at": at}))
    return f


class Gate(unittest.TestCase):
    """The driver's side: an approved experiment's bench, held or let through by the budget."""
    E = test_experiments.Experiment
    make, run_pilot, log = E.make, E.run_pilot, E.log

    def setUp(self):
        self.make()
        self.asks, self.answer = self.s.tmp / "asks.txt", self.s.tmp / "answer.txt"
        fake = self.s.bin / "notify-ask"  # records each question; `--get` prints answer.txt when there is one
        fake.write_text(f'#!/bin/sh\nif [ "$1" = --get ]; then cat {self.answer} 2>/dev/null; exit 0; fi\n'
                        f'echo "$@" >> {self.asks}\n')
        fake.chmod(0o755)
        os.environ["FAKE_WEEK"] = "0.2"
        self.better = {**test_experiments.MAIN, "passed": 6}

    def run_with(self, **kw):
        old = driver.Pilot.__init__

        def init(pilot, p, a):
            old(pilot, p, args(answer_wait=0.02, **kw))
        try:
            driver.Pilot.__init__ = init
            return self.run_pilot([self.better, self.better])
        finally:
            driver.Pilot.__init__ = old

    def asked(self):
        return self.asks.read_text().splitlines() if self.asks.exists() else []

    def assert_waits(self, pilot):
        st = load_json(self.p.state / "state.json", {})
        self.assertTrue(st["aside"]["T-001"].startswith(budget.MARK), st["aside"])
        self.assertNotIn("T-001", st.get("failed", []))
        self.assertEqual((self.calls, pilot.merged), ([], []))

    def test_a_run_at_76_percent_weekly_starts_no_bench(self):
        os.environ["FAKE_WEEK"] = "0.76"
        pilot = self.run_with()
        self.assert_waits(pilot)
        self.assertIn("weekly usage at 76% (benches start below 75%)", self.log())
        self.assertEqual(self.asked(), [])  # nothing to allow: the weekly usage itself is too high

    def test_at_the_share_it_asks_once_and_with_no_answer_it_waits(self):
        for _ in range(3):
            spend(5, ended=time.time() - 8 * DAY)  # last week's runs: a full run takes about 5%
        spend(27, size="small")
        pilot = self.run_with()
        self.assert_waits(pilot)
        self.assertEqual(len(self.asked()), 1)
        self.assertIn("windvane experiments used 27% of this week (cap 30%), and the next bench run takes about 5%. "
                      "Allow more this week?", self.asked()[0])
        self.assertIn("No, wait for the reset", self.asked()[0])
        self.assertIn("no answer on the experiment budget: no more this week", self.log())
        self.run_with()  # the next run: the same cap was asked about this week, so no second question
        self.assertEqual(len(self.asked()), 1)

    def test_answer_a_lets_the_bench_run_and_past_40_percent_it_asks_again(self):
        for _ in range(3):
            spend(5, ended=time.time() - 8 * DAY)
        spend(27, size="small")
        self.answer.write_text("ANSWER A\t+10% this week\n")
        pilot = self.run_with()
        self.assertEqual(pilot.merged, ["T-001"])
        self.assertEqual(len(self.calls), 2)  # both of the head's runs fit within 40%
        self.assertEqual(budget.Budget(CFG).cap, 40)
        spend(8, size="small")  # 35% spent: 35 + 5 is still within 40
        self.assertEqual(pilot.budget_holds("T-002", "full", 1)[0], "")
        spend(2, size="small")  # 37 + 5 > 40: asked again, at the new cap; no answer this time
        self.answer.unlink()
        why, b = pilot.budget_holds("T-002", "full", 1)
        self.assertEqual(why, "benches used 37% of this week, and the next run takes about 5% (cap 40%)")
        self.assertEqual(len(self.asked()), 2)
        self.assertIn("used 37% of this week (cap 40%)", self.asked()[1])


class Periodic(unittest.TestCase):
    """The periodic bench (`builtin = "bench"`) counts and waits on the same budget."""
    C = test_bench_periodic.Caps
    setUp, not_started, make, record, merge, fake, pilot = C.setUp, C.not_started, C.make, C.record, C.merge, \
        C.fake, C.pilot

    def test_at_the_weeks_bench_share_the_periodic_bench_doesnt_start(self):
        spend(31, size="small")
        self.not_started(self.pilot())  # a phone-less run: nothing to allow more with
        log = (self.p.state / "autopilot.log").read_text()
        self.assertIn("bench: bench not started: the experiment budget: benches used 31% of this week", log)


class Week(unittest.TestCase):
    def setUp(self):
        self.s = Sandbox()
        self.addCleanup(self.s.close)

    def test_the_weeks_reset_clears_the_spend_and_the_allowance(self):
        t = time.time()
        reset = t + DAY
        usage("one", 50, t, reset)
        spend(25, ended=t - 3600)
        b = budget.Budget(CFG, at=t)
        b.allow(10)
        b.save(asked=[30])
        b = budget.Budget(CFG, at=t)
        self.assertEqual((b.spent, b.allowed, b.cap, b.asked, b.week), (25, 10, 40, [30], 50))
        after = budget.Budget(CFG, at=reset + 60)  # no reply since: last week's reading isn't this week's
        self.assertEqual((after.spent, after.allowed, after.cap, after.asked, after.week), (0, 0, 30, [], None))
        usage("one", 2, reset + 60, reset + 7 * DAY)
        after = budget.Budget(CFG, at=reset + 120)
        self.assertEqual((after.spent, after.allowed, after.cap, after.week), (0, 0, 30, 2))
        self.assertEqual(after.estimate("full"), 25)  # earlier weeks' runs still say what a run takes

    def test_the_spend_of_a_span_shared_with_another_projects_run_is_its_share_of_the_rise(self):
        started = time.time() - 600
        before = {"week": 40.0, "reset": None, "at": started}
        base = budget.root() / "_bench" / "20261024-120000"
        row = lambda at, **tok: {"at": minute(at), "role": "worker", **tok}
        mine = [row(started + 60, input=1000, output=2000, cache_read=50_000),
                row(started + 300, input=500, cache_write=20_000)]
        judge = [row(started + 500, input=200, output=800, cache_read=10_000)]
        other = [row(started + 200, input=3000, output=1000, cache_read=90_000)]
        for r in mine:
            append_jsonl(base / "notes/state/windvane/notes/ledger.jsonl", r)
        for r in judge:
            append_jsonl(base / "judge/notes/state/ledger.jsonl", r)
        for r in other:
            append_jsonl(budget.root() / "other/ledger.jsonl", r)
        append_jsonl(budget.root() / "other/ledger.jsonl", row(started - 2 * DAY, input=10**7))  # before the span
        usage("other", 44.0, time.time())
        after = base / "notes/state/windvane/notes/usage.json"  # the bench's own last reply: the newest
        after.write_text(json.dumps({"week": 46.5, "at": time.time() + 1}))
        rec = budget.record(base, started, before, "small")
        ours, theirs = sum(map(weighted, mine + judge)), sum(map(weighted, other))
        self.assertEqual(rec["spent"], round(6.5 * ours / (ours + theirs), 2))
        self.assertEqual((rec["bench"], rec["all"], rec["rise"]), (round(ours), round(ours + theirs), 6.5))
        self.assertEqual(budget.Budget(CFG).spent, rec["spent"])

    def test_a_reset_during_a_run_counts_the_new_weeks_reading_whole(self):
        self.assertEqual(budget.rise({"week": 90, "reset": 100}, {"week": 3, "reset": 100 + 7 * DAY}), 3)
        self.assertEqual(budget.rise({"week": 40, "reset": 100}, {"week": 43.5, "reset": 100}), 3.5)

    def test_the_usage_reply_keeps_the_weeks_reset(self):
        p = Project(self.s.root)
        Usage(p).update({"unifiedWindows": {"seven_day": {"utilization": 0.5, "resetsAt": 1_800_000_000}}})
        self.assertEqual(budget.reading(), {"week": 50.0, "reset": 1_800_000_000, "at": load_json(
            p.state / "usage.json", {})["at"]})

    def test_the_desk_allows_more_this_week_and_sees_the_budget(self):
        usage("one", 60, time.time(), time.time() + 2 * DAY)
        spend(31)
        run = lambda *a: subprocess.run(["windvane", "experiments", *a], cwd=self.s.root, capture_output=True,
                                        text=True)
        out = run().stdout
        self.assertIn("experiments: 31% of this week spent on benches (1 run), cap 30%", out)
        self.assertIn("weekly usage 60%; benches start below 75%; the week resets", out)
        self.assertIn("benches wait: benches used 31% of this week", out)
        self.assertIn("cap 40% until the week resets", run("allow", "+10").stdout)
        out = run().stdout
        self.assertIn("cap 40% (share 30% + 10% you allowed this week)", out)
        self.assertIn("benches may start", out)
        self.assertEqual(run("allow", "lots").returncode, 1)


if __name__ == "__main__":
    unittest.main()
