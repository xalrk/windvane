"""A worktree's setup keeps to the run's stop rule: a stop or the deadline ends it as a stop, and a timeout says what to
do about it. `[[periodic]] setup_minutes` takes the same numbers as the top-level key."""
import os
import sys
import threading
import time
import unittest
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from windvane import driver, worktree  # noqa: E402
from windvane.config import Project  # noqa: E402
from windvane.util import now  # noqa: E402
from helpers import Sandbox  # noqa: E402
from test_hangs import alive  # noqa: E402
from test_pipeline import args  # noqa: E402

NOTHING_RAN = "deadline reached during setup; nothing ran"
FIX = "raise `setup_minutes` in config.toml or fix the setup"


class SetupLimits(unittest.TestCase):
    def setUp(self):
        self.s = Sandbox()
        self.addCleanup(self.s.close)
        self.s.ticket("T-001", touches="a.py")
        self.pushes = []
        orig, driver.push = driver.push, lambda title, msg, *a, **k: self.pushes.append((title, msg))
        self.addCleanup(setattr, driver, "push", orig)
        self.pidfile = self.s.tmp / "setup.pid"

    def config(self, text):
        cfg = self.s.root / ".windvane/config.toml"
        cfg.write_text(text + cfg.read_text())

    def run_setup(self, minutes=20, deadline=timedelta(minutes=15), during=None):
        """A run whose setup sleeps; `during(p)` runs once the setup has started. (the run's result, seconds taken)"""
        self.config(f'setup = "echo installing; echo $$ > {self.pidfile}; sleep 600"\nsetup_minutes = {minutes}\n')
        self.s.publish("setup")
        p = Project(self.s.root)
        pilot = driver.Pilot(p, args())
        if during:
            def later():
                while not (self.pidfile.exists() and self.pidfile.read_text().strip()):
                    time.sleep(0.05)
                during(p)
            threading.Thread(target=later, daemon=True).start()
        t = time.monotonic()
        ready = pilot.run(now() + deadline)
        took = time.monotonic() - t
        self.addCleanup(lambda: alive(int(self.pidfile.read_text())) and os.killpg(int(self.pidfile.read_text()), 9))
        return p, pilot, ready, took

    def assert_stopped_not_failed(self, p, pilot, why):
        self.assertEqual(pilot.stop_reason, why)
        self.assertIsNone(pilot.halt)
        self.assertEqual([t for t, _ in self.pushes], ["windvane stopped"])  # no "couldn't start", no started
        self.assertTrue(self.pushes[0][1].startswith(f"Why: {why}."))
        self.assertNotIn("setup `", self.pushes[0][1])  # a stop, not a failed setup
        self.assertFalse(alive(int(self.pidfile.read_text())))
        self.assertFalse(p.work_dir.exists())  # half-made: the next start sets it up afresh
        self.assertIn("cut short", (p.state / "autopilot.log").read_text())

    def test_a_stop_during_setup_ends_it_at_once_as_a_stop(self):
        p, pilot, ready, took = self.run_setup(during=lambda p: (p.state / "STOP").touch())
        self.assertFalse(ready)
        self.assertLess(took, 5)  # not the 20 min setup_minutes allows
        self.assert_stopped_not_failed(p, pilot, "asked to stop")
        self.assertFalse((p.state / "STOP").exists())  # used up, as any stop

    def test_a_stop_now_during_setup_ends_it_too(self):
        p, pilot, ready, took = self.run_setup(during=lambda p: (p.state / "STOP").write_text("now"))
        self.assertLess(took, 5)
        self.assert_stopped_not_failed(p, pilot, "stopped now: the running sessions were killed")

    def test_the_deadline_ends_setup_before_setup_minutes(self):
        p, pilot, ready, took = self.run_setup(deadline=timedelta(seconds=2))
        self.assertFalse(ready)
        self.assertGreater(took, 1.5)
        self.assertLess(took, 6)
        self.assert_stopped_not_failed(p, pilot, NOTHING_RAN)

    def test_a_deadline_moved_during_setup_ends_it_too(self):
        def sooner(p):
            (p.state / "DEADLINE").write_text((now() - timedelta(seconds=1)).isoformat())
        p, pilot, ready, took = self.run_setup(during=sooner)
        self.assertLess(took, 5)
        self.assert_stopped_not_failed(p, pilot, NOTHING_RAN)

    def test_a_timed_out_setup_says_how_to_fix_it_once(self):
        p, pilot, ready, took = self.run_setup(minutes=0.02)
        self.assertFalse(ready)
        self.assertEqual([t for t, _ in self.pushes], ["windvane couldn't start"])
        msg = self.pushes[0][1]
        self.assertTrue(msg.startswith("Setup timed out after 0.02 min; " + FIX + ": installing (it ran `echo installing;"),
                        msg)  # the fix first, then the setup's last line, in reading order
        self.assertEqual(msg.count(FIX), 1)
        self.assertNotIn("Why:", msg)
        self.assertNotIn("stopped", msg)
        self.assertNotIn(str(p.state), msg)  # the worktree by its short name
        self.assertIn(FIX, pilot.stop_reason)

    def test_a_failed_setup_has_no_timeout_fix(self):
        self.config('setup = "echo nope; exit 3"\n')
        self.s.publish("setup")
        pilot = driver.Pilot(Project(self.s.root), args())
        pilot.run(now() + timedelta(minutes=15))
        self.assertEqual(self.pushes, [("windvane couldn't start",
                                        "Setup failed (exit 3): nope (it ran `echo nope; exit 3` in work).")])

    def test_short_path(self):
        p = Project(self.s.root)
        self.assertEqual(worktree.short_path(p, p.state / "work-1"), "work-1")
        self.assertEqual(worktree.short_path(p, Path("/elsewhere/deep/proj-ci")), "deep/proj-ci")


class PeriodicSetupMinutes(unittest.TestCase):
    def setUp(self):
        self.s = Sandbox()
        self.addCleanup(self.s.close)
        self.cfg = self.s.root / ".windvane/config.toml"
        self.base = self.cfg.read_text()

    def load(self, top="", periodic=""):
        self.cfg.write_text(top + self.base + f'\n[[periodic]]\nname = "build"\nprompt = "P.md"\n{periodic}')
        return Project(self.s.root)

    def refused(self, **kw):
        with self.assertRaises(SystemExit) as e:
            self.load(**kw)
        return str(e.exception.code)

    def test_a_fraction_loads(self):
        self.assertEqual(self.load(periodic="setup_minutes = 0.5\n").periodic["build"]["setup_minutes"], 0.5)
        self.assertEqual(self.load().periodic["build"]["setup_minutes"], 60)

    def test_zero_and_text_are_refused_like_the_top_level_key(self):
        for bad in ("0", '"x"', "-1", "true"):
            mine = self.refused(periodic=f"setup_minutes = {bad}\n")
            top = self.refused(top=f"setup_minutes = {bad}\n")
            self.assertEqual(mine, top.replace("windvane: ", "windvane: [[periodic]] build: "))
            self.assertIn("use a number of minutes above 0", mine)

    def test_every_and_minutes_stay_whole_numbers(self):
        self.assertIn("every and minutes are whole numbers", self.refused(periodic='every = "x"\n'))


if __name__ == "__main__":
    unittest.main()
