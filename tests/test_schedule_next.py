"""A daily window's stopped push names the window that really comes next, even after `deadline` moved this one's end
past the next start; and a stop that lands as a window starts (`stop --now` too) spends nothing: no `claude` call,
no session in the ledger."""
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from windvane import driver, phone, schedule  # noqa: E402
from windvane.util import read_jsonl  # noqa: E402
from test_parallel import args  # noqa: E402
import test_run_window as rw  # noqa: E402


class ScheduleNext(unittest.TestCase):
    W = rw.RunWindow
    setUp, tearDown, sleep, schedule, cli = W.setUp, W.tearDown, W.sleep, W.schedule, W.cli

    def start(self, argv, env):
        """The window's real driver (fake sessions); `on_start` runs once it has come up (its lock taken)."""
        self.runs.append(self.clock())
        self.during_run(len(self.runs))
        pilot = driver.Pilot(self.p, args(parallel=1))
        until = schedule.datetime.fromisoformat(argv[argv.index("--until") + 1])
        start_run = phone.start_run

        def came_up(p):
            start_run(p)
            self.on_start(len(self.runs))
        with mock.patch.dict(os.environ, env), mock.patch.object(driver, "push", self.push), \
                mock.patch.object(driver.phone, "start_run", came_up):
            pilot.run(until)
        self.clock.t = max(self.clock.t, until)  # the window ran to its end, as the driver's deadline left it
        return 0

    def push(self, title, body="", *a, **k):
        self.pushes.append((title, body))
        if title == "windvane started":
            self.on_started(len(self.runs))

    def tickets(self):
        self.pushes = []
        self.on_start = self.on_started = lambda n: None
        for t in ("T-001", "T-002", "T-003"):
            self.s.ticket(t)
        self.s.publish()

    def stopped_pushes(self):
        return [b for t, b in self.pushes if t == "windvane stopped"]

    def sessions(self):
        return [r for r in read_jsonl(self.p.state / "ledger.jsonl") if "turns" in r]

    def claude_calls(self):
        f = self.s.tmp / "calls.jsonl"
        return f.read_text().splitlines() if f.exists() else []

    # ---------- the next window

    def move_end(self, when, now):
        """In the first window: `windvane deadline <when>`, then the clock at `now` (past it): the driver stops for
        the deadline there, before a ticket."""
        def move(n):
            if n == 1:
                self.cli("cmd_deadline", when=when.split())
                self.clock.t = now
        self.on_started = move

    def test_an_end_moved_into_the_next_window_names_that_window_from_the_moved_end(self):
        self.tickets()
        self.move_end("2026-10-06 13:00", rw.at(1, 13))
        self.during_run = lambda n: n == 2 and self.cli("cmd_stop", if_running=False, now=False)
        self.schedule("--daily", "12:00-20:00")
        self.assertEqual(self.runs, [rw.at(0, 12), rw.at(1, 13)])  # Tuesday's window starts at the moved end
        first = self.stopped_pushes()[0]
        self.assertIn("Why: deadline", first)
        self.assertTrue(first.endswith("\nNext window: today 13:00-20:00."), first)

    def test_an_end_moved_past_the_next_window_names_the_window_after_it(self):
        self.tickets()
        self.move_end("2026-10-06 21:00", rw.at(1, 21))
        self.during_run = lambda n: n == 2 and self.cli("cmd_stop", if_running=False, now=False)
        self.schedule("--daily", "12:00-20:00")
        self.assertEqual(self.runs, [rw.at(0, 12), rw.at(2, 12)])  # Tuesday's window is skipped
        first = self.stopped_pushes()[0]
        self.assertTrue(first.endswith("\nNext window: tomorrow 12:00-20:00."), first)

    # ---------- a stop as the window starts spends nothing

    def test_stop_now_as_a_window_starts_calls_no_claude_and_ends_the_schedule(self):
        self.tickets()
        said = []
        self.during_run = lambda n: said.append(self.cli("cmd_stop", if_running=False, now=True))
        self.schedule("--daily", "12:00-20:00")
        self.assertIn("the schedule (daily 12:00-20:00) ends: no later window starts", said[0])
        self.assertEqual(self.runs, [rw.at(0, 12)])  # no second window
        self.assertEqual(self.claude_calls(), [])
        self.assertEqual(self.sessions(), [])
        self.assertIsNone(schedule.state(self.p))
        self.assertNotIn("Next window", self.stopped_pushes()[-1])

    def test_a_stop_landing_as_the_windows_driver_comes_up_spends_no_session(self):
        self.tickets()
        self.on_start = lambda n: self.cli("cmd_stop", if_running=False, now=False)
        self.schedule("--daily", "12:00-20:00")
        self.assertEqual(self.runs, [rw.at(0, 12)])
        self.assertEqual(self.sessions(), [])
        self.assertEqual(self.claude_calls(), [])
        self.assertIsNone(schedule.state(self.p))


if __name__ == "__main__":
    unittest.main()
