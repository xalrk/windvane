"""Limit pushes and the summary (T-057): a ticket the run went on without is listed in the last push with why, a
limit push says where the limit lives, and a setup cut short or too long to quote reads well on the phone."""
import os
import sys
import threading
import time
import unittest
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from windvane import driver, runner  # noqa: E402
from windvane.config import Project  # noqa: E402
from windvane.util import now  # noqa: E402
from test_hangs import alive  # noqa: E402
from test_set_aside import Base, args  # noqa: E402


class SummaryAside(Base):
    tickets = ("T-001",)

    def summary(self):
        [msg] = [m for t, m in self.pushes if t == "windvane stopped"]
        return msg

    def test_a_budget_kill_says_its_dollar_budget_and_the_summary_lists_it(self):
        os.environ["FAKE_MAX_TURNS"] = "# windvane worker"
        os.environ["FAKE_LIMIT"] = "error_max_budget_usd"
        self.pilot().run(now() + timedelta(hours=2))
        self.assertEqual(self.aside(), [("Set aside: do T-001 (T-001)",
                                         "Hit its dollar budget. Split the ticket. Branch kept.")])
        self.assertIn("\nSet aside: do T-001 (T-001) (hit its dollar budget).\n", self.summary())

    def test_a_max_turns_kill_is_in_the_summary(self):
        os.environ["FAKE_MAX_TURNS"] = "# windvane worker"
        self.pilot().run(now() + timedelta(hours=2))
        self.assertIn("Set aside: do T-001 (T-001) (hit its max turns).", self.summary())

    def test_a_kill_at_the_deadline_is_in_the_summary_and_the_ticket_is_not_skipped(self):
        os.environ["FAKE_HANG"] = str(self.s.tmp / "streamed")
        os.environ["FAKE_HANG_ROLE"] = "# windvane worker run"
        poll, runner.POLL = runner.POLL, 0.2
        self.addCleanup(setattr, runner, "POLL", poll)
        pilot = self.pilot()
        pilot.run(now() + timedelta(seconds=4))  # well inside the last 5 minutes
        self.assertNotIn("T-001", pilot.skip)  # no change to what is set aside: the next run resumes it
        self.assertEqual(self.aside(), [])
        self.assertIn("\nStopped at the deadline (resumes next run): do T-001 (T-001).\n", self.summary())
        self.assertNotIn("Set aside", self.summary())

    def test_no_set_aside_line_when_nothing_was(self):
        self.pilot().run(now() + timedelta(hours=2))
        self.assertNotIn("Set aside", self.summary())


class SetupText(unittest.TestCase):
    def setUp(self):
        from helpers import Sandbox
        self.s = Sandbox()
        self.addCleanup(self.s.close)
        self.pushes = []
        orig, driver.push = driver.push, lambda title, msg, *a, **k: self.pushes.append((title, msg))
        self.addCleanup(setattr, driver, "push", orig)

    def setup(self, command, *tickets):
        cfg = self.s.root / ".windvane/config.toml"
        cfg.write_text(f'setup = "{command}"\nsetup_minutes = 20\n' + cfg.read_text())
        for t, touches in tickets:
            self.s.ticket(t, touches=touches)
        self.s.publish("setup")
        return Project(self.s.root)

    def test_a_long_setup_command_is_quoted_whole_in_the_halt_push(self):
        long = "echo one && echo two && echo three && echo four && echo five && echo six; exit 3"
        driver.Pilot(self.setup(long), args()).run(now() + timedelta(minutes=40))
        [(title, msg)] = self.pushes
        self.assertEqual(title, "windvane couldn't start")
        self.assertEqual(msg.split("`")[1], long)
        self.assertNotIn("…", msg)

    def test_a_second_slots_setup_cut_short_mid_run_lets_the_first_finish(self):
        pid = self.s.tmp / "setup.pid"
        os.environ["FAKE_WORK_S"] = "3"  # T-001 is still running when the second slot sets up
        p = self.setup(f'case $PWD in */work-1) echo $$ > {pid}; sleep 600;; esac', ("T-001", "a.py"),
                       ("T-002", "b.py"))

        def stop():
            while not (pid.exists() and pid.read_text().strip()):
                time.sleep(0.05)
            (p.state / "STOP").touch()
        threading.Thread(target=stop, daemon=True).start()
        pilot = driver.Pilot(p, args(parallel=2))
        t = time.monotonic()
        self.assertTrue(pilot.run(now() + timedelta(minutes=40)))
        self.assertLess(time.monotonic() - t, 60)  # not the 10 min the setup sleeps
        self.addCleanup(lambda: alive(int(pid.read_text())) and os.killpg(int(pid.read_text()), 9))
        self.assertFalse(alive(int(pid.read_text())))
        self.assertEqual(pilot.merged, ["T-001"])
        self.assertIsNone(pilot.halt)
        self.assertEqual(pilot.stop_reason, "asked to stop")
        self.assertFalse((p.state / "work-1").exists())  # half-made: the next start sets it up afresh
        titles = [t for t, _ in self.pushes]
        self.assertEqual(titles[-1], "windvane stopped")
        self.assertNotIn("couldn't start", " ".join(titles))
        self.assertTrue(self.pushes[-1][1].startswith("Why: asked to stop.\nMerged: do T-001"), self.pushes[-1][1])


if __name__ == "__main__":
    unittest.main()
