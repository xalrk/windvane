"""`stop` as a scheduled window starts: nothing starts, or the window's driver stops at once, as `stop` said. A plain
`run` refused for a waiting schedule says so, and a daily window's stopped push names the next window."""
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from windvane import driver, schedule  # noqa: E402
from test_parallel import args  # noqa: E402
import test_run_window as rw  # noqa: E402


class ScheduleStop(unittest.TestCase):
    W = rw.RunWindow
    setUp, tearDown, sleep, schedule, cli, stop, run_cli = W.setUp, W.tearDown, W.sleep, W.schedule, W.cli, W.stop, \
        W.run_cli

    def start(self, argv, env):
        """The window's real driver (fake sessions), after what the test does as it starts; its pushes recorded."""
        self.runs.append(self.clock())
        self.during_run(len(self.runs))
        pilot = driver.Pilot(self.p, args(parallel=1))
        with mock.patch.dict(os.environ, env), mock.patch.object(driver, "push", self.push):
            pilot.run(schedule.datetime.fromisoformat(argv[argv.index("--until") + 1]))
        self.merged.append(list(pilot.merged))
        self.clock.t = rw.at(len(self.runs) - 1, 20)  # the window's end
        return 0

    def push(self, title, body="", *a, **k):
        self.pushes.append((title, body))

    def go(self, *run_args):
        self.merged, self.pushes = [], []
        self.s.ticket("T-001")
        self.s.ticket("T-002")
        self.s.publish()
        self.schedule(*run_args)

    def test_a_stop_landing_after_the_wait_ends_starts_nothing_and_says_so(self):
        real = schedule.wait

        def wait(p, start, sleep):  # it has returned: the window is about to start
            ok = real(p, start, sleep)
            self.stop()
            return ok
        with mock.patch.object(schedule, "wait", wait):
            self.go("--daily", "12:00-20:00")
        self.assertEqual(self.runs, [])
        self.assertEqual(self.said, ["Sandbox: schedule (daily 12:00-20:00) called off: nothing starts at 12:00\n"])

    def test_a_stop_landing_as_the_window_starts_stops_its_driver_before_a_ticket(self):
        self.during_run = lambda n: self.stop()  # marked running, its driver not started yet (it clears old STOPs)
        self.go("--daily", "12:00-20:00")
        self.assertEqual(self.runs, [rw.at(0, 12)])
        self.assertEqual(self.merged, [[]])
        self.assertIn("stops after the current ticket, and the schedule (daily 12:00-20:00) ends", self.said[0])
        self.assertNotIn("Next window", self.pushes[-1][1])

    def test_a_plain_run_while_a_schedule_waits_says_so(self):
        said = []
        def wait():
            if not said:
                with mock.patch.dict(os.environ):
                    os.environ.pop(driver.SUPERVISED, None)  # set when the tests run under a windvane run
                    said.append(self.run_cli("--until", "20:00"))
                self.stop()
        self.during_wait = wait
        self.go("--from", "12:00", "--until", "20:00")
        self.assertEqual(said, ["a schedule is waiting: starts 12:00 (until 20:00); change it with windvane stop, "
                                "then run again"])

    def test_a_daily_windows_stopped_push_names_the_next_window_and_the_last_one_doesnt(self):
        self.during_run = lambda n: n == 2 and self.stop()
        self.go("--daily", "12:00-20:00")
        self.assertEqual(self.runs, [rw.at(0, 12), rw.at(1, 12)])
        self.assertEqual(sorted(self.merged[0]), ["T-001", "T-002"])
        first, last = [b for t, b in self.pushes if t == "windvane stopped"]
        self.assertTrue(first.endswith("\nNext window: tomorrow 12:00-20:00."), first)
        self.assertNotIn("Next window", last)


if __name__ == "__main__":
    unittest.main()
