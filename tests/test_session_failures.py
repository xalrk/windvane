"""Dead sessions (T-042): a ticket whose own sessions keep dying is set aside while the run goes on, a dead critic is
retried and then said to be unavailable, and a session out of its own turns is the ticket's outcome, not a crash."""
import argparse
import io
import json
import os
import sys
import unittest
from argparse import Namespace
from contextlib import redirect_stdout
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from windvane import cli, critic, driver, runner  # noqa: E402
from windvane.config import Project  # noqa: E402
from windvane.util import now, read_jsonl  # noqa: E402
from helpers import Sandbox  # noqa: E402

OVERLOADED = "API Error: 529 Overloaded"


def args(**kw):
    a = dict(cap=75, week_cap=95, ticket_hours=1, min_slot=0, answer_wait=10, replan=0, goal="", parallel=1)
    a.update(kw)
    return Namespace(**a)


class Base(unittest.TestCase):
    def setUp(self):
        self.s = Sandbox()
        self.s.ticket("T-001")
        self.s.ticket("T-002")  # independent: it can run while T-001 is in trouble
        self.s.publish()
        self.p = Project(self.s.root)

    def tearDown(self):
        self.s.close()

    def pilot(self, **env):
        os.environ.update(env)
        pilot = driver.Pilot(self.p, args())
        self.waits, self.pushes = [], []
        pilot.sleep, pilot.deadline = self.waits.append, now() + timedelta(hours=2)
        orig, driver.push = driver.push, lambda title, *a, **k: self.pushes.append(title)
        self.addCleanup(setattr, driver, "push", orig)
        return pilot

    def roles(self):
        f = self.s.tmp / "calls.jsonl"
        calls = [json.loads(l) for l in f.read_text().splitlines()] if f.exists() else []
        return [(c["system"].split()[2].rstrip(":"), c["prompt"].split()[1].rstrip(".")) for c in calls
                if c["system"].startswith("# windvane")]


class TicketCrashes(Base):
    def test_a_ticket_whose_sessions_keep_dying_is_set_aside_and_the_rest_merges(self):
        pilot = self.pilot(FAKE_ERROR_FOR=r"\bT-001\b")
        pilot.run(now() + timedelta(hours=2))
        self.assertEqual((pilot.merged, pilot.failed), (["T-002"], []))
        self.assertIn("T-001", pilot.skip)
        self.assertNotIn("died", pilot.stop_reason)
        self.assertEqual(self.roles()[:5], [("worker", "T-001")] * 4 + [("worker", "T-002")])
        self.assertEqual(self.waits, [60] * 21)  # 3, 6, 12 minutes for T-001; none for T-002
        aside = [t for t in self.pushes if t.startswith("Set aside")]
        self.assertEqual(aside, ["Set aside: do T-001 (T-001)"])  # one push, once T-002's session worked
        self.assertLess(self.pushes.index(aside[0]), self.pushes.index("Done: do T-002 (T-002)"))

    def test_every_session_dying_still_stops_the_run(self):
        pilot = self.pilot(FAKE_FAIL="error*")
        pilot.run(now() + timedelta(hours=2))
        self.assertEqual(pilot.stop_reason, f"5 sessions in a row died ({OVERLOADED})")
        self.assertEqual((pilot.merged, pilot.failed), ([], []))
        self.assertEqual(self.roles(), [("worker", "T-001")] * 4 + [("worker", "T-002")])  # one try on T-002
        self.assertEqual(self.pushes, ["windvane started", "windvane stopped"])  # no set-aside on a broken API

    def test_with_other_sessions_working_since_its_first_crash_it_is_set_aside_at_once(self):
        """Side by side: other tickets' sessions end well between T-001's crashes, so its spent retries set it aside
        without a trial, and the run-wide streak starts over."""
        pilot = self.pilot()
        pilot.queue = driver.Queue(self.p)
        self.assertTrue(pilot.crashed("T-001", "too long", "T-001"))
        pilot.session_ok("T-002")
        for _ in range(3):
            pilot.crashed("T-001", "too long", "T-001")
        self.assertEqual((pilot.halt, pilot.crashes, pilot.suspect), (None, 0, None))
        self.assertIn("T-001", pilot.skip)
        self.assertEqual(self.pushes, ["Set aside: do T-001 (T-001)"])
        self.assertEqual(self.waits, [60] * 21)

    def test_a_good_session_of_its_own_clears_a_tickets_count(self):
        pilot = self.pilot()
        pilot.queue = driver.Queue(self.p)
        for _ in range(3):
            pilot.crashed("T-001", "boom", "T-001")
        pilot.session_ok("T-001")
        self.assertTrue(pilot.crashed("T-001-review", "boom", "T-001"))
        self.assertEqual(pilot.ticket_crashes["T-001"][0], 1)


class CriticCrashes(Base):
    def setUp(self):
        super().setUp()
        self.p.critic["enabled"] = True
        d = critic.pack_dir(self.p, "T-001")
        d.mkdir(parents=True)
        (d / "notes.md").write_text("User-facing: yes\nIt works.\n")
        os.environ["FAKE_ERROR_FOR"] = r"^Critique T-001\."

    def test_the_worker_is_told_the_critic_is_unavailable_after_its_retries(self):
        self.p.ap["retry_minutes"] = 0
        out, cwd = io.StringIO(), os.getcwd()
        os.chdir(self.s.root)
        try:
            with redirect_stdout(out):
                cli.cmd_critic(argparse.Namespace(ticket="T-001"), self.p)
        finally:
            os.chdir(cwd)
        text = out.getvalue()
        self.assertEqual(self.roles(), [("critic", "T-001")] * 4)  # retried `retries` times
        self.assertIn(f"Critic unavailable ({OVERLOADED}).", text)
        self.assertIn("CRITIC STEP: unavailable. No critique this time: go on to the check", text)
        recs = read_jsonl(self.p.state / "critic.jsonl")
        self.assertEqual([(r["step"], r["score"]) for r in recs], [("unavailable", None)])
        self.assertIsNone(critic.calibration(recs, 8))  # `stats --critic` counts no score

    def test_the_commands_pauses_fit_in_one_bash_call(self):
        waits = []
        retry = cli.critic_retry(self.p, waits.append)
        self.assertEqual([retry("x") for _ in range(4)], [True, False, False, False])
        self.assertEqual(waits, [180])  # 3 minutes; 3 + 6 would pass CRITIC_WAIT

    def test_the_drivers_own_critic_run_retries_without_counting_a_crash(self):
        pilot = self.pilot()
        entry, text = critic.judge(self.p, "T-001", str(self.s.root), retry=pilot.critic_retry("T-001"))
        self.assertEqual((entry["step"], entry["score"]), ("unavailable", None))
        self.assertEqual(len(self.roles()), 4)
        self.assertEqual(self.waits, [60] * 21)
        self.assertEqual((pilot.crashes, pilot.ticket_crashes, pilot.halt), (0, {}, None))
        self.assertNotIn(entry["step"], critic.DONE)  # the gate asks the critic again on the next head


class OwnBudget(Base):
    def test_a_session_out_of_turns_is_no_crash(self):
        pilot = self.pilot(FAKE_MAX_TURNS="# windvane worker")
        pilot.run(now() + timedelta(hours=2))
        self.assertEqual((pilot.crashes, pilot.ticket_crashes, self.waits), (0, {}, []))
        self.assertEqual({"T-001", "T-002"} - pilot.skip, set())  # each set aside like a run out of its time box
        self.assertNotIn("died", pilot.stop_reason)
        ends = [(r["end"], r["killed"], r["error"]) for r in read_jsonl(self.p.state / "ledger.jsonl")]
        self.assertEqual(ends, [("budget", "max turns", None)] * 2)

    def test_ending_tells_its_own_budget_from_an_api_error(self):
        self.assertEqual(runner.ending({"subtype": "error_max_turns", "is_error": True}, 1, []),
                         ("budget", "max turns"))
        self.assertEqual(runner.ending({"subtype": "error_max_budget_usd"}, 1, []), ("budget", "dollar budget"))
        self.assertEqual(runner.ending({"subtype": "error_during_execution", "result": "boom"}, 1, [])[0], "error")


if __name__ == "__main__":
    unittest.main()
