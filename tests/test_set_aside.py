"""Set-aside pushes and the trial across a restart (T-050): a ticket the run gives up on says why on the phone and
what to change, and a ticket on trial for its own dead sessions stays on trial when the driver restarts."""
import os
import sys
import unittest
from argparse import Namespace
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from windvane import driver, runner  # noqa: E402
from windvane.config import Project  # noqa: E402
from windvane.util import now  # noqa: E402
from helpers import Sandbox, no_wrap_up  # noqa: E402

TOO_LONG = "Prompt is too long"


def args(**kw):
    a = dict(cap=75, week_cap=95, ticket_hours=1, min_slot=0, answer_wait=10, replan=0, goal="", parallel=1)
    a.update(kw)
    return Namespace(**a)


class Base(unittest.TestCase):
    tickets = ("T-001", "T-002")

    def setUp(self):
        self.s = Sandbox()
        for t in self.tickets:
            self.s.ticket(t)
        self.s.publish()
        self.p = Project(self.s.root)
        self.pushes = []
        orig, driver.push = driver.push, lambda title, msg, *a, **k: self.pushes.append((title, msg))
        self.addCleanup(setattr, driver, "push", orig)

    def tearDown(self):
        self.s.close()

    def pilot(self, **kw):
        pilot = driver.Pilot(self.p, args(**kw))
        self.waits = []
        pilot.sleep, pilot.deadline = self.waits.append, now() + timedelta(hours=2)
        return pilot

    def aside(self):
        return [(t, m) for t, m in self.pushes if t.startswith("Set aside")]


class LimitPushes(Base):
    tickets = ("T-001",)

    def test_a_run_out_of_turns_pushes_the_limit_and_the_fix(self):
        os.environ["FAKE_MAX_TURNS"] = "# windvane worker"
        pilot = self.pilot()
        pilot.run(now() + timedelta(hours=2))
        self.assertIn("T-001", pilot.skip)
        self.assertEqual(self.aside(), [("Set aside: do T-001 (T-001)",
                                         "Hit its max turns. Split it, or raise CLAUDE_CODE_MAX_TURNS. Branch kept.")])

    def test_a_run_out_of_its_time_box_pushes_the_limit_and_the_knob(self):
        os.environ["FAKE_HANG"] = str(self.s.tmp / "streamed")
        os.environ["FAKE_HANG_ROLE"] = "# windvane worker run"
        poll, runner.POLL = runner.POLL, 0.2
        self.addCleanup(setattr, runner, "POLL", poll)
        no_wrap_up(self)
        pilot = self.pilot(ticket_hours=1 / 1800)  # two seconds
        pilot.run(now() + timedelta(hours=2))
        self.assertIn("T-001", pilot.skip)
        [(title, msg)] = self.aside()
        self.assertEqual(title, "Set aside: do T-001 (T-001)")
        self.assertIn("hour limit for one run. Split it, or raise ticket_hours.", msg)


class CrashPushes(Base):
    def test_a_prompt_too_long_death_pushes_the_split_advice(self):
        os.environ.update(FAKE_ERROR_FOR=r"\bT-001\b", FAKE_ERROR_TEXT=TOO_LONG)
        pilot = self.pilot()
        pilot.run(now() + timedelta(hours=2))
        self.assertEqual(pilot.merged, ["T-002"])
        [(title, msg)] = self.aside()
        self.assertEqual(title, "Set aside: do T-001 (T-001)")
        self.assertEqual(msg, "Sessions died 4×: prompt is too long. Split the ticket, or trim what it reads.\n"
                              f"The error: {TOO_LONG}.\nBranch kept, run goes on. Other sessions worked, so it's this "
                              "ticket's doing.")

    def test_other_errors_keep_the_plain_text(self):
        pilot = self.pilot()
        pilot.set_aside("T-001", "API Error: 529 Overloaded")
        [(_, msg)] = self.aside()
        self.assertNotIn("Split", msg)
        self.assertTrue(msg.startswith("Sessions died 4×: API Error: 529 Overloaded. Branch kept, run goes on.\n"), msg)


class TrialAcrossRestart(Base):
    def on_trial(self):
        """T-001's sessions die retries + 1 times with no good session yet: aside on trial; the driver restarts."""
        pilot = self.pilot()
        pilot.queue = driver.Queue(self.p)
        for _ in range(4):
            pilot.crashed("T-001", TOO_LONG, "T-001")
        self.assertEqual((pilot.suspect, pilot.halt), (["T-001", TOO_LONG], None))
        pilot.hand_over()
        again = self.pilot()
        again.carry_over(True)
        again.queue = driver.Queue(self.p)
        return again

    def test_a_good_session_after_the_restart_keeps_it_aside_and_says_so(self):
        pilot = self.on_trial()
        self.assertIn("T-001", pilot.skip)
        pilot.session_ok("T-002")
        self.assertEqual(pilot.suspect, None)
        self.assertEqual([t for t, _ in self.aside()], ["Set aside: do T-001 (T-001)"])

    def test_a_dead_session_after_the_restart_stops_the_run(self):
        pilot = self.on_trial()
        self.assertFalse(pilot.crashed("T-002", TOO_LONG, "T-002"))
        self.assertEqual(pilot.halt, f"5 sessions in a row died ({TOO_LONG})")
        self.assertEqual(self.aside(), [])


class LoneTicket(Base):
    tickets = ("T-001",)

    def test_a_lone_ticket_whose_sessions_keep_dying_halts_the_run(self):
        os.environ["FAKE_ERROR_FOR"] = r"\bT-001\b"
        pilot = self.pilot()
        pilot.run(now() + timedelta(hours=2))
        calls = [l for l in (self.s.tmp / "calls.jsonl").read_text().splitlines() if "windvane worker" in l]
        self.assertEqual(len(calls), self.p.ap["retries"] + 1)
        self.assertEqual(pilot.stop_reason, "4 sessions in a row died (API Error 529: Overloaded)")
        self.assertEqual(self.aside(), [])  # nothing to tell it from a broken API: no set-aside, a stop


if __name__ == "__main__":
    unittest.main()
