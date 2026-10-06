"""`run --from/--until` and `run --daily`: a detached run waits for its start, each window is a supervisor run of its
own with its own deadline, and `stop` ends the whole schedule. On a fake clock that the schedule's sleeps move."""
import contextlib
import io
import os
import sys
import unittest
from argparse import Namespace
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from windvane import cli, driver, schedule, util  # noqa: E402
from windvane.config import Project  # noqa: E402
from helpers import Sandbox  # noqa: E402

MON = datetime(2026, 10, 5)


def at(day, hh, mm=0):
    return MON + timedelta(days=day, hours=hh, minutes=mm)


class Clock:
    def __init__(self, t):
        self.t, self.slept = t, timedelta()

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += timedelta(seconds=s)
        self.slept += timedelta(seconds=s)


class RunWindow(unittest.TestCase):
    def setUp(self):
        self.addCleanup(os.chdir, os.getcwd())  # `run -C` moves this process into the sandbox
        self.s = Sandbox()
        self.p = Project(self.s.root)
        self.p.state.mkdir(parents=True, exist_ok=True)
        self.clock = Clock(at(0, 9))
        for mod in (schedule, util, cli, driver):
            patch = mock.patch.object(mod, "now", self.clock)
            patch.start()
            self.addCleanup(patch.stop)
        patch = mock.patch.object(cli, "in_tmux", lambda p: False)  # never the real tmux server
        patch.start()
        self.addCleanup(patch.stop)
        self.runs = []  # (when the window's driver started, its --until)
        self.during_run = lambda n: None  # what happens inside the n-th window (from 1)
        self.during_wait = lambda: None  # what happens on each sleep while waiting
        self.said = []

    def tearDown(self):
        self.s.close()

    def start(self, args, env):
        self.assertNotIn("--from", args)
        self.assertNotIn("--daily", args)
        self.runs.append((self.clock(), args[args.index("--until") + 1]))
        self.during_run(len(self.runs))
        return 0

    def sleep(self, s):
        self.during_wait()
        self.clock.sleep(s)

    def schedule(self, *run_args):
        args = ["-C", str(self.s.root), "run", *run_args, "--no-preflight"]
        ns = Namespace(from_=None, until=None, for_=None, daily=None)
        it = iter(run_args)
        for x in it:
            setattr(ns, {"--from": "from_", "--until": "until", "--for": "for_", "--daily": "daily"}[x], next(it))
        window = schedule.first(ns.from_, ns.until, ns.for_, ns.daily)
        with contextlib.redirect_stdout(io.StringIO()):
            return schedule.run(self.p, args, window, ns.daily, start=self.start, sleep=self.sleep)

    def cli(self, fn, **kw):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            getattr(cli, fn)(Namespace(**kw), self.p)
        return out.getvalue()

    def stop(self):
        self.said.append(self.cli("cmd_stop", if_running=False, now=False))

    # ---------- a single scheduled run

    def test_a_future_from_starts_no_session_before_it(self):
        self.schedule("--from", "12:00", "--until", "20:00")
        self.assertEqual(self.runs, [(at(0, 12), "2026-10-05 20:00")])
        self.assertEqual(self.clock.slept, timedelta(hours=3))

    def test_a_past_from_starts_at_once(self):
        self.clock.t = at(0, 14)
        self.schedule("--from", "12:00", "--until", "20:00")
        self.assertEqual(self.runs, [(at(0, 14), "2026-10-05 20:00")])
        self.assertEqual(self.clock.slept, timedelta())
        self.assertEqual(schedule.first("2026-10-05 13:00", None, "2h", None), (at(0, 14), at(0, 15)))
        with self.assertRaisesRegex(ValueError, "ended"):
            schedule.first("2026-10-04 23:00", None, "2h", None)  # over: refused at the desk

    def test_an_end_before_the_start_is_the_next_morning(self):
        self.clock.t = at(0, 21)
        self.schedule("--from", "22:00", "--until", "06:00")
        self.assertEqual(self.runs, [(at(0, 22), "2026-10-06 06:00")])
        self.clock.t = at(0, 23)  # already under way: starts now, still ends in the morning
        self.assertEqual(schedule.first("22:00", "06:00", None, None), (at(0, 23), at(1, 6)))
        self.assertEqual(schedule.first("01:00", "06:00", None, None), (at(1, 1), at(1, 6)))  # today's is over
        self.assertEqual(schedule.first("01:00", None, "5h", None), (at(1, 1), at(1, 6)))

    # ---------- a daily schedule

    def test_daily_runs_a_window_each_day_and_sleeps_between(self):
        def run(n):
            self.clock.t += timedelta(hours=3)  # the queue ran dry early: the next window is still tomorrow's
            if n == 2:
                self.stop()
        self.during_run = run
        self.schedule("--daily", "12:00-20:00")
        self.assertEqual(self.runs, [(at(0, 12), "2026-10-05 20:00"), (at(1, 12), "2026-10-06 20:00")])
        self.assertEqual(self.clock.slept, timedelta(hours=3 + 21))
        self.assertIn("no later window starts", self.said[0])

    def test_typed_inside_a_window_daily_starts_now_and_keeps_the_windows(self):
        self.clock.t = at(0, 14)
        self.during_run = lambda n: n == 2 and self.stop()
        self.schedule("--daily", "12:00-20:00")
        self.assertEqual(self.runs, [(at(0, 14), "2026-10-05 20:00"), (at(1, 12), "2026-10-06 20:00")])

    def test_a_daily_window_may_cross_midnight(self):
        self.clock.t = at(0, 3)  # inside the window that began yesterday
        self.during_run = lambda n: n == 2 and self.stop()
        self.schedule("--daily", "22:00-06:00")
        self.assertEqual(self.runs, [(at(0, 3), "2026-10-05 06:00"), (at(0, 22), "2026-10-06 06:00")])

    # ---------- stop, status, deadline

    def test_stop_mid_window_ends_the_schedule(self):
        self.during_run = lambda n: self.stop()
        self.schedule("--daily", "12:00-20:00")
        self.assertEqual(len(self.runs), 1)
        self.assertIn("stops after the current ticket, and the schedule (daily 12:00-20:00) ends", self.said[0])
        self.assertTrue((self.p.state / "STOP").exists())  # for the window's driver
        self.assertIsNone(schedule.state(self.p))

    def test_stop_while_waiting_ends_the_schedule(self):
        self.during_wait = lambda: self.said or self.stop()
        self.schedule("--daily", "12:00-20:00")
        self.assertEqual(self.runs, [])
        self.assertEqual(self.said, ["Sandbox: schedule (daily 12:00-20:00) called off: nothing starts at 12:00\n"])
        self.assertLess(self.clock.slept, timedelta(minutes=1))
        self.assertIn("isn't running", self.cli("cmd_stop", if_running=True, now=False))

    def test_status_shows_the_waiting_line_and_deadline_moves_the_next_window(self):
        seen = []

        def wait():
            if not seen:
                seen.append(self.cli("cmd_status", line=True))
                seen.append(self.cli("cmd_status", line=False))
                seen.append(self.cli("cmd_deadline", when=["18:00"]))
                seen.append(self.cli("cmd_status", line=True))
        self.during_wait = wait
        self.during_run = lambda n: n == 2 and self.stop()
        self.schedule("--daily", "12:00-20:00")
        self.assertEqual("Sandbox: autopilot waiting: starts 12:00 (daily 12:00-20:00)\n", seen[0])
        self.assertIn("autopilot: waiting: starts 12:00 (daily 12:00-20:00)", seen[1])
        self.assertIn("the run starting 12:00 now ends at 2026-10-05 18:00 (this window only", seen[2])
        self.assertIn("waiting: starts 12:00 (daily 12:00-20:00, this one until 18:00)", seen[3])
        self.assertEqual(self.runs, [(at(0, 12), "2026-10-05 18:00"), (at(1, 12), "2026-10-06 20:00")])

    def test_deadline_before_the_start_is_refused_while_waiting(self):
        said = []

        def wait():
            if not said:
                with self.assertRaises(SystemExit) as e:
                    self.cli("cmd_deadline", when=["2026-10-05 11:00"])
                said.append(str(e.exception.code))
                self.stop()
        self.during_wait = wait
        self.schedule("--from", "12:00", "--until", "20:00")
        self.assertIn("the run starts 12:00; give an end after that", said[0])

    # ---------- at the desk

    def run_cli(self, *args):
        argv = ["-C", str(self.s.root), "run", *args, "--no-preflight"]
        with mock.patch.object(sys, "argv", ["windvane", *argv]), self.assertRaises(SystemExit) as e:
            cli.main(argv)
        return str(e.exception.code)

    def test_from_or_daily_without_detach_refuses_with_the_command(self):
        self.assertIn(f"run: windvane -C {self.s.root} run --from 12:00 --until 20:00 --no-preflight --detach",
                      self.run_cli("--from", "12:00", "--until", "20:00"))
        self.assertIn("--detach", self.run_cli("--daily", "12:00-20:00"))
        self.assertIn("say when to stop", self.run_cli("--from", "12:00"))
        self.assertIn("leave out --from, --until and --for", self.run_cli("--daily", "12:00-20:00", "--for", "2h"))
        self.assertIn("--daily takes HH:MM-HH:MM", self.run_cli("--daily", "noon"))
        self.assertEqual(self.runs, [])

    def test_each_window_gets_the_run_arguments_without_the_schedule(self):
        argv = ["-C", "/x", "run", "--daily", "12:00-20:00", "--parallel", "2", "--from=1", "--no-preflight"]
        self.assertEqual(schedule.window_args(argv, at(0, 20)),
                         ["-C", "/x", "run", "--parallel", "2", "--until", "2026-10-05 20:00", "--no-preflight"])


if __name__ == "__main__":
    unittest.main()
