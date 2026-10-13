"""A helper a ticket's tools start to outlive them (an emulator, a dev server) and no `teardown` stops is named at the
run's end, in the log and one push, and left running: the driver can't tell it from something the user started."""
import contextlib
import io
import os
import signal
import subprocess
import sys
import time
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from windvane import driver, util  # noqa: E402
from windvane.config import Project  # noqa: E402
from windvane.util import now  # noqa: E402
from helpers import Sandbox  # noqa: E402
from test_pipeline import args  # noqa: E402

TITLE = "windvane left helpers running"


class LeftoverHelpers(unittest.TestCase):
    def setUp(self):
        self.addCleanup(os.chdir, os.getcwd())
        self.s = Sandbox()
        self.addCleanup(self.s.close)
        self.s.ticket("T-001")
        self.pidfile = self.s.tmp / "helper.pid"
        env = mock.patch.dict(os.environ, {"FAKE_HELPER": str(self.pidfile)})
        env.start()
        self.addCleanup(env.stop)
        self.pushes = []
        orig, driver.push = driver.push, lambda title, msg="", *a, **k: self.pushes.append((title, msg))
        self.addCleanup(setattr, driver, "push", orig)

    def config(self, extra=""):
        cfg = self.s.root / ".windvane/config.toml"
        cfg.write_text(extra + cfg.read_text())
        self.s.publish("config")
        self.p = Project(self.s.root)

    def run_once(self):
        with contextlib.redirect_stdout(io.StringIO()):
            driver.Pilot(self.p, args()).run(now() + timedelta(hours=1))
        if self.pidfile.exists():
            self.addCleanup(kill, self.helper())
        return [m for t, m in self.pushes if t == TITLE]

    def helper(self):
        return int(self.pidfile.read_text())

    def log(self):
        return (self.p.state / "autopilot.log").read_text()

    def test_a_helper_the_session_left_is_named_and_left_running(self):
        self.config()
        [msg] = self.run_once()
        pid = self.helper()
        self.assertIn(f"Still running in the autopilot's worktrees: sleep 120 (pid {pid}). Set `teardown` in "
                      ".windvane/config.toml to stop them at a run's end.", msg)
        self.assertIn(f"left running after the run: pid {pid}: sleep 120", self.log())
        self.assertTrue(alive(pid))  # reported, never killed

    def test_a_teardown_that_missed_it_says_so(self):
        self.config('teardown = "true"\n')
        [msg] = self.run_once()
        self.assertIn("Its `teardown` in .windvane/config.toml missed them.", msg)
        self.assertTrue(alive(self.helper()))

    def test_a_teardown_that_stops_it_gives_no_line(self):
        self.config(f'teardown = "kill $(cat {self.pidfile})"\n')
        self.assertEqual(self.run_once(), [])
        self.assertNotIn("left running", self.log())

    def test_none_left_gives_no_line(self):
        del os.environ["FAKE_HELPER"]
        self.config()
        self.assertEqual(self.run_once(), [])
        self.assertNotIn("left running", self.log())

    def test_with_isolate_off_only_what_the_run_started_counts(self):
        self.config("isolate = false\n")
        older = subprocess.Popen(["sleep", "121"], cwd=self.s.root, start_new_session=True)  # the user's, before it
        self.addCleanup(kill, older.pid)
        time.sleep(0.1)
        [msg] = self.run_once()
        self.assertIn(f"Still running in the checkout, started by the run: sleep 120 (pid {self.helper()}).", msg)
        self.assertNotIn("sleep 121", msg)
        self.assertNotIn("sleep 121", self.log())
        self.assertIsNone(older.poll())


class Command(unittest.TestCase):
    def test_its_command_and_no_terminal_from_proc_and_from_ps(self):
        c = subprocess.Popen(["sleep", "30"], start_new_session=True)
        self.addCleanup(kill, c.pid)
        time.sleep(0.1)  # exec'd
        self.assertEqual(util.command(c.pid), ("sleep 30", False))
        with mock.patch.object(util, "PROC", Path("/nonexistent/proc")):  # macOS's branch, through this `ps`
            self.assertEqual(util.command(c.pid), ("sleep 30", False))
        kill(c.pid)
        self.assertIsNone(util.command(c.pid))


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    with contextlib.suppress(ChildProcessError):  # a zombie of ours is dead
        return os.waitpid(pid, os.WNOHANG) == (0, 0)
    return True


def kill(pid):
    with contextlib.suppress(OSError):
        os.kill(pid, signal.SIGKILL)
    with contextlib.suppress(ChildProcessError):
        os.waitpid(pid, 0)


if __name__ == "__main__":
    unittest.main()
