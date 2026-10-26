"""The endless loop (`run --replan forever`): the planner runs each time the queue runs dry, with no count to reach;
its prompt holds where the machine's projects hurt (`stats --projects --json`, projects by letter), the rejected
experiments and the newest bench runs; `plateau` rejected experiments in a row stop experiment planning with one
push until a kept one, a new goal or seed, or `experiments resume`; and a weekly push and `stats --bench --trend`
give the week's before → after."""
import contextlib
import io
import json
import math
import sys
import time
import unittest
from argparse import Namespace
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from windvane import budget, config, driver, experiment, planner, stats  # noqa: E402
from windvane.config import Project  # noqa: E402
from windvane.util import append_jsonl, now  # noqa: E402
from helpers import Sandbox  # noqa: E402
from test_bench_periodic import rec  # noqa: E402
from test_merge_draft import args  # noqa: E402

DAY = 86400


def at(t):
    return f"{datetime.fromtimestamp(t):%Y-%m-%d %H:%M}"


def run_id(t):
    return f"{datetime.fromtimestamp(t):%Y%m%d-%H%M%S}"


class Base(unittest.TestCase):
    def make(self, extra=""):
        self.s = Sandbox(extra)
        self.addCleanup(self.s.close)
        self.s.write("bench/tiny/seed/README.md", "tiny\n")
        self.s.write("bench/tiny/hidden/bench.toml", "")
        self.s.publish()
        self.p = Project(self.s.root)
        self.p.state.mkdir(parents=True, exist_ok=True)
        self.addCleanup(setattr, driver, "NAP", driver.NAP)
        driver.NAP = 0.05

    def pilot(self, **kw):
        pilot = driver.Pilot(self.p, args(**{"replan": math.inf, "goal": "make it better", **kw}))
        pilot.sleep = lambda s: None
        return pilot

    def prompts(self, role="planner"):
        calls = [json.loads(x) for x in (self.s.tmp / "calls.jsonl").read_text().splitlines()]
        return [c["prompt"] for c in calls if c["system"].startswith(f"# windvane {role} run")]


class Replan(Base):
    def run_counting(self, stop_at, **kw):
        """A run whose plans are counted; the `stop_at`th leaves a stop, as `windvane stop` does."""
        plans, real = [], driver.plan

        def counted(*a, **k):
            plans.append(k.get("endless"))
            n = real(*a, **k)
            if len(plans) == stop_at:
                (self.p.state / "STOP").touch()
            return n
        with mock.patch.object(driver, "plan", counted):
            pilot = self.pilot(**kw)
            pilot.run(now() + timedelta(minutes=30))
        return pilot, plans

    def test_forever_plans_each_time_the_queue_runs_dry_past_any_count(self):
        self.make()
        pilot, plans = self.run_counting(4)
        self.assertEqual(plans, [True] * 4)
        self.assertGreaterEqual(len(pilot.merged), 3)  # each plan's ticket was worked before the next plan
        self.assertEqual(len(self.prompts()), 4)

    def test_a_count_still_ends_the_run_once_it_is_used(self):
        self.make()
        pilot, plans = self.run_counting(99, replan=2)
        self.assertEqual(plans, [False, False])
        self.assertEqual(len(pilot.merged), 2)

    def test_a_plan_that_writes_nothing_waits_for_the_next_plan_until_the_deadline(self):
        self.make()
        with mock.patch.dict("os.environ", {"FAKE_END_LINE": "PLAN: NONE nothing worth a ticket"}):
            pilot = self.pilot()
            started = time.time()
            pilot.run(now() + timedelta(seconds=4))
        self.assertEqual(len(self.prompts()), 1)
        self.assertLess(time.time() - started, 60)
        self.assertIn("nothing merged since the last plan: planning again at",
                      self.p.state.joinpath("autopilot.log").read_text())

    def test_the_next_plan_waits_for_a_merge_or_an_hour_and_the_wait_survives_a_restart(self):
        self.make()
        pilot = self.pilot()
        self.assertTrue(pilot.replan_due())  # no plan yet
        pilot.replan_wait = {"until": time.time() + 3600, "merged": 0}
        self.assertFalse(pilot.replan_due())  # it wrote nothing, or its tickets all failed
        pilot.merged.append("T-001")
        self.assertTrue(pilot.replan_due())
        pilot.merged.clear()
        pilot.hand_over()
        again = self.pilot()
        again.carry_over(True)
        self.assertEqual(again.replan_wait, pilot.replan_wait)
        self.assertFalse(again.replan_due())
        again.replan_wait["until"] = time.time() - 1
        self.assertTrue(again.replan_due())

    def test_the_config_and_the_flag_take_a_count_or_forever(self):
        self.assertEqual(config.replan("forever"), math.inf)
        self.assertEqual((config.replan(3), config.replan("3")), (3, 3))
        for bad in (-1, "always", True, 1.5):
            with self.assertRaises(SystemExit):
                config.replan(bad)
        with self.assertRaises(SystemExit):
            config.plateau(0)
        self.make("[experiments]\nplateau = 3\n")
        cfg = self.s.root / ".windvane/config.toml"
        cfg.write_text('replan = "forever"\n' + cfg.read_text())
        self.p = Project(self.s.root)
        self.assertEqual((config.replan(self.p.cfg["replan"]), self.p.cfg["experiments"]["plateau"]), (math.inf, 3))
        self.assertIn(".windvane/config.toml:plateau", self.p.protected)


class PlannerPrompt(Base):
    def test_an_endless_plan_holds_the_digest_by_letter_the_experiments_and_the_bench(self):
        self.make()
        self.s.write(".windvane/EXPERIMENTS.md", experiment.HEADER + "\n### T-005: a shorter worker prompt\n")
        self.s.publish()
        append_jsonl(self.p.state / "ledger.jsonl", {"role": "worker", "ticket": "T-001", "at": at(time.time()),
                                                     "input": 5000})
        append_jsonl(self.p.state / "bench.jsonl", rec("a" * 40, seed="tiny", run="20261020-100000"))
        other = self.p.state.parent / "secretproj"
        other.mkdir()
        append_jsonl(other / "ledger.jsonl", {"event": "outcome", "ticket": "X-001", "outcome": "failed",
                                              "at": at(time.time())})
        (other / "autopilot.log").write_text(f"{at(time.time())} X-001: FAILED: the secretproj build broke\n")
        planner.plan(self.p, "make it better", endless=True)
        prompt = self.prompts()[-1]
        self.assertIn("## Where projects hurt (`windvane stats --projects --json`, last 30 days, numbers only, "
                      "projects by letter, costliest first)", prompt)
        self.assertIn("Rank ideas by what this says costs projects most", prompt)
        part = prompt.split("## Where projects hurt")[1].split("\n## ")[0]
        digest = json.loads(part.splitlines()[-1])
        self.assertEqual(list(digest)[:3], ["since", "across", "projects"])
        self.assertEqual([d["name"] for d in digest["projects"]], ["project A", "project B"])
        self.assertEqual(digest["projects"][0]["tokens"]["weighted"], 5000)  # the costliest first
        self.assertEqual(digest["projects"][1]["tickets"]["failed"], 1)
        for leak in ("secretproj", "X-001", "build broke", self.p.name, str(self.s.root)):
            self.assertNotIn(leak, part)
        self.assertNotIn(str(self.s.root), prompt)
        self.assertIn("## Experiments the bench rejected (.windvane/EXPERIMENTS.md)", prompt)
        self.assertIn("### T-005: a shorter worker prompt", prompt)
        self.assertIn("## The newest bench runs (`windvane stats --bench`)\nrun ", prompt)
        self.assertIn("20261020-100000", prompt)
        planner.plan(self.p, "make it better")  # a plan by count, or at the desk: none of it
        self.assertNotIn("## Where projects hurt", self.prompts()[-1])

    def test_a_digest_too_long_leaves_whole_cheaper_projects_out(self):
        row = lambda i: {"name": f"p{i}", "tokens": {"weighted": i, "per_merged": None, "roles": {}},
                         "tickets": {"merged": i}, "critic": {}, "checks": {}, "check_quick": None,
                         "calibration_lines": 0, "refixes": [["T-1", "T-2"]], "failed": {"T-1": "why"}}
        digest = {"since": "2026-10-01", "projects": [row(i) for i in range(60)],
                  "across": [{"measure": "merged tickets", "low": 0, "low_projects": ["p0"], "high": 59,
                              "high_projects": ["p59"]}]}
        text = planner.numbers_only(digest, 2000)
        self.assertLessEqual(len(text), 2000)
        out = json.loads(text)
        self.assertEqual(out["across"][0]["high_projects"], ["project A"])  # p59, the costliest
        self.assertEqual(out["projects"][0], {
            "name": "project A", "tickets": {"merged": 59}, "tokens": row(59)["tokens"], "critic": {}, "checks": {},
            "check_quick": None, "calibration_lines": 0, "refixes": 1})
        self.assertEqual(out["left_out"], f"{60 - len(out['projects'])} cheaper projects")

    def test_a_project_without_a_bench_gets_no_digest(self):
        s = Sandbox()
        self.addCleanup(s.close)
        self.assertEqual(planner.endless_parts(Project(s.root)), [])


class Plateau(Base):
    def verdicts(self, *kinds):
        for v in kinds:
            row = {"event": "experiment", "verdict": "kept"} if v == "kept" else \
                {"event": "outcome", "outcome": "rejected"}
            append_jsonl(self.p.state / "ledger.jsonl", {**row, "ticket": "T-001", "at": at(time.time())})

    def watch(self, pilot):
        with mock.patch.object(experiment, "push") as push:
            pilot.plateau_watch()
        return [c.args for c in push.call_args_list]

    def test_eight_rejected_in_a_row_stop_experiment_planning_with_one_push(self):
        self.make()
        pilot = self.pilot(replan=2)  # any run, not only an endless one
        self.verdicts("kept", *["rejected"] * 7)
        self.assertEqual(self.watch(pilot), [])
        self.assertEqual(experiment.stopped(self.p), "")
        self.verdicts("rejected")
        pushes = self.watch(pilot)
        self.assertEqual(len(pushes), 1)
        self.assertEqual(pushes[0][0], "Experiments plateaued")
        self.assertIn("The last 8 experiments were all rejected", pushes[0][1])
        self.assertIn("frozen seeds: no run this week", pushes[0][1])  # the week's trend
        self.assertIn("a plateau", experiment.stopped(self.p))
        self.assertIn("## Experiments are stopped\nthe last 8 experiments were all rejected",
                      planner.stopped_part(self.p))
        self.verdicts("rejected")
        self.assertEqual(self.watch(pilot), [])  # one push, not one per rejection
        self.assertIn("Other tickets go on", pushes[0][1])

    def test_a_kept_experiment_starts_them_again(self):
        self.make()
        pilot = self.pilot()
        self.verdicts(*["rejected"] * 8)
        self.watch(pilot)
        self.assertTrue(experiment.stopped(self.p))
        self.verdicts("kept")
        self.assertEqual(self.watch(pilot), [])
        self.assertEqual(experiment.stopped(self.p), "")
        self.assertIn("planned again after the plateau: an experiment was kept",
                      self.p.state.joinpath("autopilot.log").read_text())

    def test_resume_starts_them_again_and_the_old_rejections_no_longer_count(self):
        self.make()
        pilot = self.pilot()
        self.verdicts(*["rejected"] * 8)
        self.watch(pilot)
        self.assertTrue(experiment.resume(self.p))
        self.assertEqual(self.watch(pilot), [])
        self.assertEqual(experiment.stopped(self.p), "")
        self.verdicts(*["rejected"] * 7)
        self.assertEqual(self.watch(pilot), [])
        self.verdicts("rejected")
        self.assertEqual(len(self.watch(pilot)), 1)

    def test_a_new_goal_or_a_new_seed_starts_them_again(self):
        self.make()
        pilot = self.pilot()
        self.verdicts(*["rejected"] * 8)
        self.watch(pilot)
        pilot.a.goal = "something else"
        self.watch(pilot)
        self.assertEqual(experiment.stopped(self.p), "")
        self.assertIn("the goal changed", self.p.state.joinpath("autopilot.log").read_text())
        experiment.save_json(experiment.plateau_file(self.p), {"from": 0})  # the same 8 rejections, read again
        pilot.plateau_read = None
        self.watch(pilot)
        self.assertTrue(experiment.stopped(self.p))
        self.s.write("bench/other/seed/README.md", "other\n")
        self.s.write("bench/other/hidden/bench.toml", "")
        self.s.publish()
        self.watch(pilot)
        self.assertEqual(experiment.stopped(self.p), "")
        self.assertIn("a new seed (other)", self.p.state.joinpath("autopilot.log").read_text())

    def test_an_overfit_stop_is_left_to_the_user(self):
        self.make()
        pilot = self.pilot()
        experiment.stop(self.p, "the training pool rose while the frozen holdout fell")
        self.verdicts(*["rejected"] * 8, "kept")
        self.assertEqual(self.watch(pilot), [])
        self.assertIn("frozen holdout fell", experiment.stopped(self.p))


class Trend(Base):
    def setUp(self):
        self.make()
        t = time.time()
        self.end = int(t + 2 * DAY) // 60 * 60  # the week resets in two days
        self.start = self.end - budget.WEEK
        (self.p.state / "usage.json").write_text(json.dumps({"week": 20, "week_reset": self.end, "at": t}))
        s, led = self.start, self.p.state / "ledger.jsonl"
        for row in ({"role": "worker", "at": at(s - 2 * DAY), "input": 300_000},
                    {"event": "outcome", "outcome": "rejected", "at": at(s - 2 * DAY)},
                    {"role": "worker", "at": at(s + DAY), "input": 200_000},
                    {"role": "critic", "at": at(s + DAY), "output": 20_000},
                    {"event": "experiment", "verdict": "kept", "at": at(s + DAY)},
                    {"event": "outcome", "outcome": "rejected", "at": at(s + DAY)},
                    {"event": "outcome", "outcome": "rejected", "at": at(s + DAY + 60)}):
            append_jsonl(led, {"ticket": "T-001", **row})
        (self.p.state / "autopilot.log").write_text(
            f"{at(s - 2 * DAY)} T-001: merged abcdef1 (branch t-001)\n{at(s + DAY)} T-002: merged abcdef2 (b)\n"
            f"{at(s + DAY)} T-003: merged abcdef3 (b)\n")
        b = self.p.state / "bench.jsonl"
        append_jsonl(b, rec("a" * 40, "tiny", passed=4, yes=3, weighted=100_000, merged=1, run=run_id(s - DAY)))
        append_jsonl(b, rec("b" * 40, "tiny", passed=5, yes=4, weighted=100_000, merged=1, run=run_id(s + DAY)))
        append_jsonl(b, {**rec("c" * 40, "tiny", passed=9, yes=6, run=run_id(s + DAY + 60)), "pool": True})
        append_jsonl(b, rec("d" * 40, "tiny", passed=6, yes=5, weighted=160_000, merged=2,
                            run=run_id(s + DAY + 120)))
        for run, ended, spent in (("x", s - DAY, 4), ("y", s + DAY, 7.5)):
            append_jsonl(budget.spend_file(), {"run": run, "started": ended - 600, "ended": ended, "spent": spent})

    THIS_WEEK = ["frozen seeds: hidden tests 4/10 → 6/10, judge 3/6 → 5/6, tokens/merged 100k → 80k",
                 "weighted tokens per merged ticket here: 300k → 150k (1 → 2 merged)",
                 "experiments: run 1 → 3, kept 0 → 1, rejected 1 → 2",
                 "bench budget used: 4% → 7.5% of the weekly limit (cap 30%)"]

    def test_stats_bench_trend_prints_this_weeks_numbers(self):
        self.assertEqual(stats.trend(self.p), self.THIS_WEEK)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            stats.cmd_stats(Namespace(critic=False, bench=True, trend=True), self.p)
        self.assertEqual(out.getvalue().splitlines(), self.THIS_WEEK)
        self.assertLess(len(self.THIS_WEEK), 5)

    def test_the_weekly_push_comes_once_at_the_reset_with_the_week_that_ended(self):
        pilot = self.pilot()
        with mock.patch.object(experiment, "push") as push:
            pilot.trend_watch()  # the first look only notes the reset
            self.assertEqual(push.call_count, 0)
            self.assertEqual(json.loads((self.p.state / "trend.json").read_text()), {"next": self.end})
            experiment.save_json(self.p.state / "trend.json", {"next": self.start})  # the last reset was missed
            pilot.trend_watch()
            pilot.trend_watch()
        self.assertEqual(push.call_count, 1)
        self.assertEqual(push.call_args.args[:2], ("Weekly trend", "\n".join([
            "frozen seeds: hidden tests 4/10, judge 3/6, tokens/merged 100k (one run)",
            "weighted tokens per merged ticket here: - → 300k (0 → 1 merged)",
            "experiments: run 0 → 1, kept 0 → 0, rejected 0 → 1",
            "bench budget used: 0% → 4% of the weekly limit (cap 30%)"])))
        self.assertEqual(json.loads((self.p.state / "trend.json").read_text()), {"next": self.end})

    def test_a_run_by_count_sends_no_weekly_push(self):
        experiment.save_json(self.p.state / "trend.json", {"next": self.start})
        with mock.patch.object(experiment, "push") as push:
            self.pilot(replan=3).trend_watch()
        self.assertEqual(push.call_count, 0)


if __name__ == "__main__":
    unittest.main()
