"""New windvane code the driver can't take up, because its restart command is gone or its exec fails: the phone hears
once a run, and the run goes on on its old code instead of ending as a crash."""
import errno
import io
import os
import sys
import unittest
from contextlib import redirect_stdout
from datetime import timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from windvane import driver  # noqa: E402
from windvane.config import Project  # noqa: E402
from windvane.util import now  # noqa: E402
from helpers import Sandbox  # noqa: E402
from test_parallel import args  # noqa: E402
from test_restart_missing import script  # noqa: E402
from test_selfupdate import Install  # noqa: E402

TITLE = "windvane update not taken up"
REINSTALL = "Reinstall windvane (pipx reinstall windvane, or sh install.sh in its clone)."


class RestartPush(unittest.TestCase):
    def setUp(self):
        self.s = Sandbox()
        self.addCleanup(self.s.close)
        self.i = Install(self.s)
        self.s.ticket("T-001")
        self.s.ticket("T-002")
        self.s.publish()
        self.p = Project(self.s.root)
        self.pilot = driver.Pilot(self.p, args(parallel=1))
        self.pilot.home, self.pilot.argv = self.i.home, ["run", "--for", "6h"]
        self.exe = self.s.tmp / "venv/bin/windvane"
        patch = mock.patch.object(driver, "exe", lambda: [str(self.exe)])
        patch.start()
        self.addCleanup(patch.stop)
        self.pushes, self.calls = [], []
        orig, driver.push = driver.push, lambda title, msg="", *a, **k: self.pushes.append((title, msg))
        self.addCleanup(setattr, driver, "push", orig)
        self.addCleanup(os.environ.pop, "WINDVANE_RESTARTED", None)

    def mine(self):
        return [m for t, m in self.pushes if t == TITLE]

    def log(self):
        return (self.p.state / "autopilot.log").read_text()

    def test_a_missing_install_pushes_once_over_two_pulls(self):
        for n in (2, 3):
            self.i.commit("windvane/driver.py", f"v{n}\n")
            self.pilot.pull()
            self.assertIsNone(self.pilot.behind)
        self.assertEqual(self.mine(), [f"{self.exe} is gone. {REINSTALL} Until then the run goes on on its "
                                       "current code."])

    def run_with(self, execv):
        """T-001 merges, the pull after it finds new code, the restart's exec fails; T-002 must still merge."""
        self.exe.parent.mkdir(parents=True)
        script(self.exe, "/bin/sh")

        def wrapped(path, argv):
            self.calls.append(argv)
            self.assertTrue((self.p.state / "restart.json").exists())  # handed over before the exec
            self.assertEqual(os.environ.get("WINDVANE_RESTARTED"), "1")
            execv(path, argv)
        self.pilot.execv = wrapped
        self.i.commit("windvane/driver.py", "v2\n")
        with redirect_stdout(io.StringIO()):
            self.pilot.run(now() + timedelta(hours=2))
        self.assertEqual(len(self.calls), 1)  # the same install code isn't tried again after the second merge
        self.assertEqual(self.pilot.merged, ["T-001", "T-002"])
        self.assertFalse((self.p.state / "crash.json").exists())
        self.assertFalse((self.p.state / "restart.json").exists())
        self.assertNotIn("WINDVANE_RESTARTED", os.environ)
        self.assertEqual((self.pilot.behind, self.pilot.drain), (None, None))
        self.assertNotIn("crash", " ".join(t + m for t, m in self.pushes).lower())

    def test_an_exec_refused_keeps_the_run_going_and_pushes_once(self):
        def execv(path, argv):
            raise PermissionError(errno.EACCES, os.strerror(errno.EACCES))
        self.run_with(execv)
        self.assertEqual(self.mine(), [f"{self.exe} can't be run (permission denied). {REINSTALL} Until then the "
                                       "run goes on on its current code."])
        self.assertIn(f"new windvane code not taken up: {self.exe} can't be run (permission denied)", self.log())

    def test_an_install_removed_after_the_check_behaves_the_same(self):
        def execv(path, argv):
            self.exe.unlink()
            self.assertFalse(Path(path).exists())  # so the real exec below can only fail
            os.execv(path, argv)
        self.run_with(execv)
        self.assertEqual(self.mine(), [f"{self.exe} can't be run (no such file or directory). {REINSTALL} Until "
                                       "then the run goes on on its current code."])

    def test_newer_code_after_a_failed_exec_is_tried_again_without_a_second_push(self):
        self.exe.parent.mkdir(parents=True)
        script(self.exe, "/bin/sh")

        def execv(path, argv):
            self.calls.append(argv)
            raise PermissionError(errno.EACCES, "Permission denied")
        self.pilot.execv, self.pilot.end = execv, lambda: now() + timedelta(hours=1)
        self.i.commit("windvane/driver.py", "v2\n")
        self.pilot.pull()
        self.pilot.restart()
        self.pilot.pull()
        self.assertIsNone(self.pilot.behind)  # the code that failed: not again
        self.i.commit("windvane/driver.py", "v3\n")
        self.pilot.pull()
        self.assertTrue(self.pilot.behind)
        self.pilot.restart()
        self.assertEqual((len(self.calls), len(self.mine())), (2, 1))


if __name__ == "__main__":
    unittest.main()
