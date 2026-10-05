"""No hangs and no orphans: a time limit on `setup`, nested sessions that ignore SIGTERM killed anyway, and what a dead
driver's sessions left running swept at the next run's start."""
import os
import signal
import subprocess
import sys
import threading
import time
import unittest
import unittest.mock
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from windvane import driver, runner, util  # noqa: E402
from windvane.config import Project  # noqa: E402
from windvane.util import now  # noqa: E402
from helpers import Sandbox  # noqa: E402
from test_pipeline import args  # noqa: E402

DEAF = "trap '' TERM; sleep 600"  # a whole group that ignores SIGTERM (sleep inherits the ignored signal)


def alive(pgid):
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False


def dies(pgid, within=5):
    """Whether the group is gone within a few seconds: a killed child stays a zombie, still in its group, until
    whoever adopted it reaps it, which on a busy CI runner can take a moment."""
    end = time.monotonic() + within
    while alive(pgid):
        if time.monotonic() > end:
            return False
        time.sleep(0.05)
    return True


class Hangs(unittest.TestCase):
    def setUp(self):
        self.s = Sandbox()
        self.s.ticket("T-001", touches="a.py")
        self.pushes = []
        orig, driver.push = driver.push, lambda title, msg, *a, **k: self.pushes.append((title, msg))
        self.addCleanup(setattr, driver, "push", orig)
        self.addCleanup(setattr, runner, "GRACE", runner.GRACE)
        runner.GRACE = 0.5
        self.procs = []

    def tearDown(self):
        for proc in self.procs:  # never leave a test's sleeps behind, whatever failed
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.wait()
        self.s.close()

    def group(self, cmd="sleep 600"):
        proc = subprocess.Popen(["bash", "-c", cmd], start_new_session=True)
        self.procs.append(proc)
        threading.Thread(target=proc.wait, daemon=True).start()  # reaped as it ends, as its real parent would
        time.sleep(0.2)  # the trap is set before the test signals it
        return proc

    def mark(self, p, session, pid, name=None):
        f = p.state / "nested" / session / (name or str(pid))
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(str(pid))
        return f

    def gone(self, proc, within=5):
        try:
            proc.wait(within)
            return True
        except subprocess.TimeoutExpired:
            return False

    def test_a_hung_setup_is_killed_and_fails_the_start_loudly(self):
        pidfile = self.s.tmp / "setup.pid"
        cfg = self.s.root / ".windvane/config.toml"
        cfg.write_text(f'setup = "echo installing; echo $$ > {pidfile}; sleep 600"\nsetup_minutes = 0.02\n'
                       + cfg.read_text())
        self.s.publish("setup")
        p = Project(self.s.root)
        pilot = driver.Pilot(p, args())
        t = time.time()
        self.assertFalse(pilot.run(now() + timedelta(minutes=15)))
        self.assertLess(time.time() - t, 30)
        self.assertTrue(dies(int(pidfile.read_text())))  # its whole group, the sleep too
        self.assertIn("timed out after 0.02 min", pilot.stop_reason)
        self.assertEqual([t for t, _ in self.pushes], ["windvane couldn't start"])
        self.assertIn("timed out after 0.02 min", self.pushes[0][1])
        log = (p.state / "autopilot.log").read_text()
        self.assertIn(f"setting up {p.work_dir}: echo installing;", log)
        self.assertIn("timed out after 0.02 min", log)
        self.assertIn("installing", log)  # the command's last lines
        self.assertFalse(p.work_dir.exists())  # the next start sets it up afresh

    def test_a_ctrl_c_during_setup_kills_it_and_returns_at_once(self):
        pidfile = self.s.tmp / "group.pid"

        def ctrl_c():
            if pidfile.exists() and pidfile.read_text().strip():
                raise KeyboardInterrupt
            return ""
        t = time.monotonic()
        with self.assertRaises(KeyboardInterrupt):
            util.run_limited(f"echo $$ > {pidfile}; sleep 600", self.s.tmp, now() + timedelta(minutes=5), stop=ctrl_c)
        self.assertLess(time.monotonic() - t, 3)
        self.assertTrue(dies(int(pidfile.read_text())))

    def test_setup_minutes_must_be_a_positive_number(self):
        cfg = self.s.root / ".windvane/config.toml"
        cfg.write_text('setup_minutes = "soon"\n' + cfg.read_text())
        with self.assertRaises(SystemExit) as e:
            Project(self.s.root)
        self.assertIn("setup_minutes", str(e.exception.code))

    def test_a_nested_session_that_ignores_sigterm_is_killed(self):
        p = Project(self.s.root)
        deaf, gone_already = self.group(DEAF), self.group("true")
        gone_already.wait()
        self.mark(p, "T-001-worker", deaf.pid)
        self.mark(p, "T-001-worker", gone_already.pid)  # ended on its own: fine
        t = time.time()
        self.assertEqual(runner.kill_nested(p, "T-001-worker"), 1)
        self.assertLess(time.time() - t, 3)  # the driver isn't held up for long
        self.assertTrue(self.gone(deaf))
        self.assertEqual(deaf.returncode, -signal.SIGKILL)
        self.assertFalse((p.state / "nested" / "T-001-worker").exists())

    def test_a_quick_nested_session_isnt_waited_on(self):
        p = Project(self.s.root)
        polite = self.group()
        self.mark(p, "T-001-worker", polite.pid)
        t = time.time()
        runner.kill_nested(p, "T-001-worker")
        self.assertLess(time.time() - t, 1.5)  # well under the grace: not waited out
        self.assertTrue(self.gone(polite))
        self.assertEqual(polite.returncode, -signal.SIGTERM)

    @unittest.skipUnless(Path("/proc/self/stat").exists(), "needs /proc to tell a process's start time")
    def test_a_number_reused_after_the_mark_is_left_alone(self):
        p = Project(self.s.root)
        stranger = self.group()
        f = self.mark(p, "T-001-worker", stranger.pid)
        os.utime(f, (time.time() - 3600, time.time() - 3600))  # marked before this process existed: a reboot since
        self.assertEqual(runner.kill_nested(p, "T-001-worker"), 0)
        self.assertTrue(alive(stranger.pid))

    def test_the_sweep_spares_a_group_whose_start_it_cant_read(self):
        p = Project(self.s.root)
        stranger = self.group()
        self.mark(p, "T-007-worker", stranger.pid)
        with unittest.mock.patch.object(runner, "started_at", return_value=None):  # no /proc (macOS)
            self.assertEqual(runner.sweep_nested(p), 0)
        self.assertTrue(alive(stranger.pid))
        self.assertFalse((p.state / "nested" / "T-007-worker").exists())

    def test_the_start_sweeps_what_a_dead_drivers_sessions_left(self):
        p = Project(self.s.root)
        dead_owner = self.group("true")
        dead_owner.wait()
        left, deaf, desk_session, desk_job = self.group(), self.group(DEAF), self.group(), self.group()
        self.mark(p, "T-007-worker", dead_owner.pid, "session")
        self.mark(p, "T-007-worker", left.pid)
        self.mark(p, "T-007-critic", deaf.pid)  # a dir with no owner recorded
        self.mark(p, "plan-desk", desk_session.pid, "session")  # a desk `plan` still running beside the run
        self.mark(p, "plan-desk", desk_job.pid)
        pilot = driver.Pilot(p, args())
        pilot.run(now() + timedelta(minutes=15))
        self.assertTrue(self.gone(left) and self.gone(deaf))
        self.assertFalse((p.state / "nested" / "T-007-worker").exists())
        self.assertFalse((p.state / "nested" / "T-007-critic").exists())
        self.assertTrue(alive(desk_job.pid))  # a live session's own are its business
        self.assertTrue((p.state / "nested" / "plan-desk" / str(desk_job.pid)).exists())
        log = (p.state / "autopilot.log").read_text()
        self.assertIn("killed 2 process groups left running by an earlier run's sessions", log)
        self.assertEqual(log.count("left running by an earlier run"), 1)

    def test_a_clean_start_says_nothing(self):
        p = Project(self.s.root)
        driver.Pilot(p, args()).run(now() + timedelta(minutes=15))
        self.assertNotIn("left running", (p.state / "autopilot.log").read_text())
        self.assertEqual(list((p.state / "nested").glob("*")) if (p.state / "nested").exists() else [], [])


if __name__ == "__main__":
    unittest.main()
