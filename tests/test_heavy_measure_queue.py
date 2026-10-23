"""A measurement waiting for a quiet CPU lets the jobs behind it by only for machine.toml's `measure_hold` minutes:
each job it lets by adds load of its own, so on a busy day it would wait until its deadline. Then it holds the line
until the load is down; one killed while it waits holds no one up."""
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from windvane import heavy  # noqa: E402
from test_heavy_measure import Fixture  # noqa: E402


class HoldTheLine(Fixture):
    def holding(self, tag):
        return heavy.HOLDING.format("0.04") in self.said(tag)

    def test_a_job_behind_a_measurement_goes_first_before_the_hold_and_waits_after_it(self):
        self.machine("measure_load = 4\nmeasure_hold = 0.04\n")  # 2.4 s
        timing = self.job("timing", measure=True)
        self.until(lambda: self.said("timing"))
        self.assertEqual(self.job("early", measure=False).wait(15), 0)
        self.assertEqual(self.ran(), ["early"])
        self.assertFalse(self.holding("timing"))
        self.until(lambda: self.holding("timing"))
        late = self.job("late", measure=False)
        held = (f"heavy: waiting: pid {timing.pid}, a --measure job, holds the line for a quiet CPU: "
                "load 9.5 (want ≤ 4)")
        self.until(lambda: held in self.said("late"))
        time.sleep(0.5)
        self.assertEqual(self.ran(), ["early"])
        self.assertEqual(self.said("late").count(held), 1, "said once, not as the load drifts")
        self.in_process(busiest=lambda: [])
        self.assertRegex(heavy.status()[1], rf"^waiting: pid {timing.pid}, since \d\d:\d\d, measuring "
                                            r"\(waiting for load ≤ 4, now 9\.5; holding the line\)")
        self.load(3)
        self.assertEqual(timing.wait(15), 0)
        self.assertEqual(late.wait(15), 0, self.said("late"))
        self.assertEqual(self.ran(), ["early", "timing", "late"])
        self.assertEqual(sum(l.startswith("heavy: holding the line") for l in self.said("timing")), 1)

    def test_a_measurement_killed_while_it_holds_the_line_holds_no_one_up(self):
        self.machine("measure_load = 4\nmeasure_hold = 0\n")
        timing = self.job("timing", measure=True)
        self.until(lambda: heavy.HOLDING.format(0) in self.said("timing"))
        build = self.job("build", measure=False)
        self.until(lambda: any("holds the line" in l for l in self.said("build")))
        timing.kill()
        timing.wait()
        self.assertEqual(build.wait(15), 0)
        self.assertEqual(self.ran(), ["build"])
        self.assertEqual(self.waiters(), [])

    def test_two_measurements_start_in_arrival_order_once_the_load_drops(self):
        for hold in ("20", "0"):
            with self.subTest(measure_hold=hold):
                self.order.unlink(missing_ok=True)
                self.load(9.5)
                self.machine(f"measure_load = 4\nmeasure_hold = {hold}\n")
                first = self.job(f"first{hold}", measure=True, cmd=f"sleep 0.3; echo first >> {self.order}")
                self.until(lambda: self.said(f"first{hold}"))
                second = self.job(f"second{hold}", measure=True, cmd=f"echo second >> {self.order}")
                self.until(lambda: self.said(f"second{hold}"))
                time.sleep(0.3)
                self.load(1)
                self.assertEqual(first.wait(15), 0)
                self.assertEqual(second.wait(15), 0)
                self.assertEqual(self.ran(), ["first", "second"])

    def test_the_waiting_line_says_where_measure_load_is_set_and_the_line_to_add(self):
        self.in_process(busiest=lambda: [])
        line = self.run_here()[1][0]
        self.assertIn(str(self.d / "cfg/windvane/machine.toml"), line)
        self.assertIn("measure_load = <n>", line)

    def test_a_measure_hold_that_is_not_a_number_is_the_default(self):
        for bad in ('"5"', "true", "-1"):
            self.assertEqual(heavy.hold_minutes({"measure_hold": bad}), 20)
        self.assertEqual(heavy.hold_minutes({}), 20)
        self.assertEqual(heavy.hold_minutes({"measure_hold": 0.5}), 0.5)


if __name__ == "__main__":
    unittest.main()
