"""An expired Claude login is waited out like a network outage: the session it killed spends no crash retry and sets
no ticket aside, the expiry is pushed once per machine, the session reruns once `claude auth status` says logged in
again, and a deadline during the wait names the login as the run's reason to stop. Other deaths retry as before."""
import io
import json
import os
import sys
import time
import unittest
from contextlib import redirect_stdout
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from windvane import driver, login  # noqa: E402
from windvane.config import Project  # noqa: E402
from windvane.util import now  # noqa: E402
from helpers import Sandbox  # noqa: E402
from test_pipeline import args  # noqa: E402

EXPIRED = "Failed to authenticate: OAuth session expired and could not be refreshed"


class Base(unittest.TestCase):
    def setUp(self):
        self.s = Sandbox()
        self.s.ticket("T-001")
        self.s.ticket("T-002", "T-001")
        self.s.publish()
        self.p = Project(self.s.root)
        self.login = self.s.tmp / "login"
        os.environ["FAKE_LOGIN"] = str(self.login)
        self.waits, self.pushes = [], []
        for mod in (driver, login):
            orig = mod.push
            mod.push = lambda title, *a, **k: self.pushes.append(title)
            self.addCleanup(setattr, mod, "push", orig)

    def tearDown(self):
        self.s.close()

    def pilot(self, back_after=None, fail="", **kw):
        """A run whose login is gone from the start (the usage probe still works) when `back_after` is set: the
        fake's `auth status` says logged in again after that many minutes of waiting."""
        os.environ["FAKE_FAIL"] = fail
        if back_after is not None:
            self.login.write_text("out")
        pilot = driver.Pilot(self.p, args(**kw))

        def sleep(s):
            self.waits.append(s)
            if len(self.waits) == back_after:
                self.login.write_text("in")
        pilot.sleep = sleep
        return pilot

    def run_pilot(self, pilot, hours=1):
        with redirect_stdout(io.StringIO()):
            pilot.run(now() + timedelta(hours=hours))

    def log(self):
        return (self.p.state / "autopilot.log").read_text()

    def roles(self):
        calls = [json.loads(line) for line in (self.s.tmp / "calls.jsonl").read_text().splitlines()]
        return [c["system"].split(" run")[0][11:] for c in calls if c["system"].startswith("# windvane")]

    def sessions(self):
        """Sessions started so far, dead or not."""
        calls = self.s.tmp / "calls.jsonl"
        return self.dead() + (len(calls.read_text().splitlines()) if calls.exists() else 0)

    def dead(self):
        f = Path(f"{self.login}.dead")
        return len(f.read_text().splitlines()) if f.exists() else 0


class Waits(Base):
    def test_an_expired_login_waits_and_reruns_the_same_ticket_with_no_retry_spent(self):
        pilot = self.pilot(back_after=3)
        self.run_pilot(pilot)
        self.assertEqual((pilot.merged, pilot.failed, pilot.aside), (["T-001", "T-002"], [], {}))
        self.assertEqual(self.dead(), 1)  # one worker session died of the login, and nothing started while waiting
        self.assertEqual(self.roles(), ["worker", "review", "worker", "review"])  # T-001's rerun, then T-002
        self.assertEqual(self.waits, [60, 60, 60])  # a minute between looks, no 3-minute crash backoff
        self.assertEqual((pilot.crashes, pilot.ticket_crashes, pilot.suspect, pilot.halt), (0, {}, None, None))
        self.assertEqual(self.pushes.count("Claude login expired"), 1)
        self.assertEqual(self.pushes.count("Claude login back"), 1)
        self.assertNotIn("retry 1/", self.log())
        self.assertIn("T-001 died of the Claude login", self.log())
        self.assertIn("the Claude login looks back: rerun T-001, no retry spent", self.log())
        self.assertFalse(login.marker().exists())

    def test_a_login_still_gone_on_the_rerun_waits_again(self):
        pilot = self.pilot(back_after=10 ** 6)
        login.BLIND, blind = 2, login.BLIND  # no sign of it after 2 minutes: tried again, still dead, waits again
        self.addCleanup(setattr, login, "BLIND", blind)
        login.status = lambda: True  # `auth status` says logged in all along, as with a stale token: no signal
        self.addCleanup(setattr, login, "status", None)

        def sleep(s):
            self.waits.append(s)
            if len(self.waits) == 5:
                self.login.write_text("in")
        pilot.sleep = sleep
        self.run_pilot(pilot)
        self.assertEqual((pilot.merged, pilot.aside, pilot.crashes, pilot.ticket_crashes),
                         (["T-001", "T-002"], {}, 0, {}))
        self.assertEqual(self.dead(), 3)
        self.assertEqual(self.pushes.count("Claude login expired"), 1)  # the machine's marker: said once

    def test_a_deadline_during_the_wait_ends_the_run_with_the_login_reason(self):
        pilot = self.pilot(back_after=10 ** 6)

        def sleep(s):
            self.waits.append(s)
            if len(self.waits) == 2:  # `windvane deadline` moves it to now
                (self.p.state / "DEADLINE").write_text((now() - timedelta(minutes=1)).isoformat())
        pilot.sleep = sleep
        self.run_pilot(pilot)
        self.assertEqual((pilot.merged, pilot.failed, pilot.aside), ([], [], {}))
        self.assertRegex(pilot.stop_reason, r"^the Claude login expired at \d\d:\d\d$")
        self.assertNotIn("Set aside", self.log())
        self.assertEqual(self.pushes.count("Claude login expired"), 1)

    def test_a_stop_ends_the_wait_and_fails_nothing(self):
        pilot = self.pilot(back_after=10 ** 6)

        def sleep(s):
            self.waits.append(s)
            if len(self.waits) == 2:
                (self.p.state / "STOP").write_text("")
        pilot.sleep = sleep
        self.run_pilot(pilot)
        self.assertEqual((pilot.merged, pilot.failed, pilot.aside, pilot.crashes), ([], [], {}, 0))
        self.assertEqual(pilot.stop_reason, "asked to stop")

    def test_two_runs_on_one_machine_push_the_expiry_once(self):
        """The first run's wait ends at its deadline with the login still gone; a second run, sharing the state
        root, finds it gone too: no second push. Once the login is back, one push says the runs go on."""
        first = self.pilot(back_after=10 ** 6)
        first.sleep = lambda s: (self.p.state / "DEADLINE").write_text((now() - timedelta(minutes=1)).isoformat())
        self.run_pilot(first)
        self.assertTrue(login.marker().exists())
        (self.p.state / "DEADLINE").unlink()
        second = self.pilot(back_after=2)
        self.run_pilot(second)
        self.assertEqual(second.merged, ["T-001", "T-002"])
        self.assertEqual(self.pushes.count("Claude login expired"), 1)
        self.assertEqual(self.pushes.count("Claude login back"), 1)
        self.assertFalse(login.marker().exists())


    def test_parallel_slots_wait_together_and_start_nothing_meanwhile(self):
        self.s.ticket("T-001", touches="a.py")
        self.s.ticket("T-002", touches="b.py")
        self.s.publish("independent")
        os.environ["FAKE_WORK_S"] = "0.5"
        pilot = self.pilot(back_after=3, parallel=2)
        seen = []

        def sleep(s):  # the wait lasts until the other slot is about to start its session, and a moment more
            until = time.time() + 10
            while "T-002: worker run" not in self.log() and time.time() < until:
                time.sleep(0.1)
            time.sleep(0.5)
            seen.append(self.sessions())
            self.waits.append(s)
            if len(self.waits) == 3:
                self.login.write_text("in")
        pilot.sleep = sleep
        self.run_pilot(pilot)
        self.assertEqual(len(set(seen)), 1, seen)  # no session started during the wait
        self.assertEqual((sorted(pilot.merged), pilot.aside, pilot.crashes), (["T-001", "T-002"], {}, 0))
        self.assertEqual(self.dead(), 1)  # T-002's session waited for the login instead of dying of it
        self.assertEqual(self.waits, [60, 60, 60])  # one wait for both slots
        self.assertEqual(self.pushes.count("Claude login expired"), 1)


    def test_auth_status_saying_logged_out_spares_the_blind_retries(self):
        pilot = self.pilot(back_after=2 * login.BLIND + 1)
        self.run_pilot(pilot)
        self.assertEqual((pilot.merged, self.dead()), (["T-001", "T-002"], 1))
        self.assertNotIn("trying again", self.log())


    def test_a_plan_run_that_dies_of_the_login_waits_and_plans_again(self):
        self.s.write(".windvane/QUEUE.md", "# Queue\n- done: T-001 (x)\n- done: T-002 (x)\n")
        self.s.publish("both done")
        pilot = self.pilot(back_after=2, replan=1, goal="more")
        self.run_pilot(pilot)
        self.assertEqual((pilot.merged, pilot.aside, self.dead()), (["T-003"], {}, 1))
        self.assertIn("plan died of the Claude login", self.log())
        self.assertIn("the Claude login looks back: rerun plan, no retry spent", self.log())
        self.assertEqual(self.pushes.count("Claude login expired"), 1)


class Unit(Base):
    def test_the_login_errors_claude_prints_are_told_from_other_deaths(self):
        for error in (EXPIRED, "Invalid API key · Please run /login", "Not logged in · Please run /login",
                      'API Error: 401 {"type":"error","error":{"type":"authentication_error","message":"x"}}',
                      "OAuth token has expired. Please obtain a new token or refresh your existing token."):
            self.assertTrue(login.expired(error), error)
        for error in ('API Error: 529 {"type":"overloaded_error","message":"Overloaded"}', "no result event (exit 1)",
                      "API Error: 4010 tokens", None, ""):
            self.assertFalse(login.expired(error), error)

    def test_an_old_marker_is_pushed_again(self):
        self.assertFalse(login.said_once(now()))
        self.assertTrue(login.said_once(now()))
        old = login.marker().stat().st_mtime - login.STALE - 60
        os.utime(login.marker(), (old, old))
        self.assertFalse(login.said_once(now()))

    def test_auth_status_reads_the_exit_code_and_logged_in(self):
        self.login.write_text("out")
        self.assertIs(login.logged_in(self.s.tmp), False)
        self.login.write_text("in")
        self.assertIs(login.logged_in(self.s.tmp), True)


class Crashes(Base):
    def test_an_ordinary_crash_still_retries_and_halts(self):
        pilot = self.pilot(fail="crash*")
        self.run_pilot(pilot)
        self.assertEqual(pilot.merged, [])
        self.assertIn("sessions in a row died", pilot.stop_reason)
        self.assertIn("retry 1/", self.log())
        self.assertNotIn("Claude login", self.log())
        self.assertNotIn("Claude login expired", self.pushes)
        self.assertFalse(login.marker().exists())


if __name__ == "__main__":
    unittest.main()
