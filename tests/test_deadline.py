"""A running autopilot's deadline moves with `windvane deadline`, and `windvane stop --now` ends the run at once
without leaving a session behind: the worktree keeps its work, and the next run resumes it."""
import io
import json
import os
import sys
import threading
import time
import unittest
from argparse import Namespace
from contextlib import redirect_stdout
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from windvane import cli, driver, jobs, runner  # noqa: E402
from windvane.config import Project  # noqa: E402
from windvane.util import now  # noqa: E402
from helpers import Sandbox  # noqa: E402
from test_parallel import args  # noqa: E402


class LiveRun(unittest.TestCase):
    def setUp(self):
        self.s = Sandbox()
        self.s.ticket("T-001")
        self.s.ticket("T-002")
        self.s.publish()
        self.p = Project(self.s.root)
        self.marker = self.s.tmp / "streamed"
        os.environ["FAKE_HANG"] = str(self.marker)  # the worker streams, then works on until it is killed
        os.environ["FAKE_HANG_ROLE"] = "# windvane worker run"  # not the usage probe
        self.poll, runner.POLL = runner.POLL, 0.2

    def tearDown(self):
        runner.POLL = self.poll
        self.s.close()

    def start(self):
        """A one-slot run until an hour from now, in a thread; returns once its first worker is busy."""
        self.pilot = driver.Pilot(self.p, args(parallel=1))
        self.thread = threading.Thread(target=self.pilot.run, args=(now() + timedelta(hours=1),))
        self.began = time.time()
        self.thread.start()
        for _ in range(200):
            if self.marker.exists():
                return int(Path(f"{self.marker}.pid").read_text())
            time.sleep(0.05)
        self.fail("the worker never started")

    def workers(self):
        calls = [json.loads(x) for x in (self.s.tmp / "calls.jsonl").read_text().splitlines()]
        return [c["prompt"] for c in calls if c["system"].startswith("# windvane worker run")]

    def alive(self, pid):
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False

    def test_a_deadline_moved_earlier_stops_new_tickets_and_the_running_one(self):
        pid = self.start()
        cli.cmd_deadline(Namespace(when=["+0.05m"]), self.p)  # 3 seconds from now
        self.thread.join(30)
        self.assertFalse(self.thread.is_alive())
        self.assertLess(time.time() - self.began, 20)  # not the hour the run was started with
        self.assertEqual(self.pilot.stop_reason, "deadline")
        self.assertFalse(self.alive(pid))
        self.assertEqual(len(self.workers()), 1)  # T-002 never started
        self.assertIn("deadline moved to", (self.p.state / "autopilot.log").read_text())

    def test_a_paused_run_ends_at_a_deadline_moved_earlier(self):
        """The 5-hour window is over the cap, so the run sleeps until it resets, an hour away; the deadline moves to
        now and the run ends within seconds, not at the reset."""
        os.environ.pop("FAKE_HANG")
        self.pilot = driver.Pilot(self.p, args(parallel=1, cap=5))  # the fake reports 10%
        thread = threading.Thread(target=self.pilot.run, args=(now() + timedelta(hours=3),))
        thread.start()
        for _ in range(200):
            log = self.p.state / "autopilot.log"
            if log.exists() and "pausing at" in log.read_text():
                break
            time.sleep(0.05)
        began = time.time()
        cli.cmd_deadline(Namespace(when=["+0.02m"]), self.p)
        thread.join(30)
        self.assertFalse(thread.is_alive())
        self.assertLess(time.time() - began, 15)
        self.assertEqual(self.pilot.stop_reason, "deadline")
        self.assertEqual(self.workers(), [])

    def test_a_deadline_moved_later_keeps_the_run_going(self):
        self.start()
        cli.cmd_deadline(Namespace(when=["+3h"]), self.p)
        time.sleep(1)
        self.assertGreater(self.pilot.end(), now() + timedelta(hours=2))
        self.assertTrue(self.thread.is_alive())
        cli.cmd_stop(Namespace(if_running=False, now=True), self.p)
        self.thread.join(30)

    def test_the_deadline_needs_a_running_autopilot_and_a_time(self):
        with self.assertRaises(SystemExit) as e:
            cli.cmd_deadline(Namespace(when=["+90m"]), self.p)
        self.assertIn("isn't running", str(e.exception))
        with self.assertRaises(SystemExit) as e:
            cli.cmd_deadline(Namespace(when=["soon"]), self.p)
        self.assertIn("HH:MM", str(e.exception))

    def test_stop_now_kills_every_session_and_the_next_run_resumes(self):
        os.environ["FAKE_HANG_LEFTOVER"] = "1"
        pid = self.start()
        cli.cmd_stop(Namespace(if_running=False, now=True), self.p)  # returns once the run has ended
        self.assertFalse(driver.locked(self.p))
        self.assertLess(time.time() - self.began, 20)
        self.assertFalse(self.alive(pid))  # the fake `claude` is gone, not orphaned in its own process group
        self.assertTrue(self.pilot.stop_reason.startswith("stopped now"))
        self.assertIn("Why: stopped now", (self.p.state / "autopilot.log").read_text())  # the summary push's text
        leftover = self.p.state / "work" / "half-done.txt"
        self.assertTrue(leftover.exists())  # the worktree kept the killed run's work

        os.environ.pop("FAKE_HANG")
        self.thread.join(30)
        pilot = driver.Pilot(self.p, args(parallel=1))
        pilot.run(now() + timedelta(minutes=15))
        resumed = [w for w in self.workers() if w.startswith("Ticket T-001.")][1]
        self.assertIn("uncommitted changes from an earlier run", resumed)
        self.assertIn("half-done.txt", resumed)
        self.assertIn("T-001", pilot.merged)

    def test_a_bg_job_dies_with_the_session_that_started_it(self):
        os.environ[runner.NEST] = "worker-session"
        with redirect_stdout(io.StringIO()):
            jobs.bg(self.p, "slow", ["sleep", "60"], str(self.s.root))
        pid = int((self.p.state / "bg" / "slow.pid").read_text())
        self.assertTrue(self.alive(pid))
        runner.kill_nested(self.p, "worker-session")
        for _ in range(100):
            if os.waitpid(pid, os.WNOHANG)[0]:
                break
            time.sleep(0.05)
        self.assertFalse(self.alive(pid))


if __name__ == "__main__":
    unittest.main()
