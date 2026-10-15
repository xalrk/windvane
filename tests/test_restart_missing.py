"""An install that moves from under a run (a reinstall, a Python upgrade that removes the old interpreter): the
self-update keeps the run going on its old code, `supervise` ends the run cleanly with the reinstall step, and doctor
names what went."""
import fcntl
import io
import os
import shutil
import subprocess
import sys
import unittest
from contextlib import redirect_stdout
from datetime import timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from windvane import doctor, driver, selfupdate  # noqa: E402
from windvane.config import Project  # noqa: E402
from windvane.util import now  # noqa: E402
from helpers import Sandbox  # noqa: E402
from test_parallel import args  # noqa: E402
from test_selfupdate import Install, Restarted  # noqa: E402

GONE = "new windvane code not taken up"


def script(path, interpreter):
    path.write_text(f"#!{interpreter}\nsleep 60\n")
    path.chmod(0o755)
    return path


class Gone(unittest.TestCase):
    def setUp(self):
        self.s = Sandbox()
        self.addCleanup(self.s.close)

    def test_a_script_needs_its_interpreter_too(self):
        ok = script(self.s.tmp / "ok", "/bin/sh")
        self.assertIsNone(selfupdate.gone([str(ok)]))
        self.assertIsNone(selfupdate.gone([sys.executable, "-m", "windvane"]))
        moved = script(self.s.tmp / "moved", f"{self.s.tmp}/old-venv/bin/python")
        self.assertEqual(selfupdate.gone([str(moved)]), f"{self.s.tmp}/old-venv/bin/python")
        self.assertEqual(selfupdate.gone([str(self.s.tmp / "nothing")]), str(self.s.tmp / "nothing"))
        ok.chmod(0o644)
        self.assertEqual(selfupdate.gone([str(ok)]), str(ok))


class SelfUpdate(unittest.TestCase):
    """New code in the install, but the command a restart would exec is gone."""

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
        self.calls = []

        def execv(path, argv):
            self.calls.append(argv)
            raise Restarted()
        self.pilot.execv = execv
        self.exe = self.s.tmp / "venv/bin/windvane"
        patch = mock.patch.object(driver, "exe", lambda: [str(self.exe)])
        patch.start()
        self.addCleanup(patch.stop)

    def log(self):
        return (self.p.state / "autopilot.log").read_text()

    def test_the_run_goes_on_on_its_old_code_and_says_so_once(self):
        self.i.commit("windvane/driver.py", "v2\n")
        with redirect_stdout(io.StringIO()):
            self.pilot.run(now() + timedelta(hours=2))
        self.assertEqual(self.pilot.merged, ["T-001", "T-002"])  # pulled after each merge, and went on
        self.assertEqual(self.calls, [])
        self.assertFalse((self.p.state / "restart.json").exists())  # never handed over
        self.assertEqual(self.log().count(GONE), 1)
        self.assertIn(f"{GONE}: {self.exe} is gone; reinstall windvane (pipx reinstall windvane, or sh "
                      "install.sh in its clone) and the run restarts on it", self.log())
        self.assertIsNone(self.pilot.behind)
        # Reinstalled: the next pull takes the new code up.
        self.exe.parent.mkdir(parents=True)
        script(self.exe, "/bin/sh")
        self.pilot.pull()
        self.assertTrue(self.pilot.behind)

    def test_a_command_gone_after_the_pull_stops_the_drain_not_the_run(self):
        self.pilot.behind, self.pilot.drain = 1.0, [1.0, 1.0, 1, 0.0]
        self.pilot.restart()
        self.assertEqual((self.pilot.behind, self.pilot.drain, self.calls), (None, None, []))
        self.assertIn(GONE, self.log())
        self.assertFalse((self.p.state / "restart.json").exists())


class Supervise(unittest.TestCase):
    """`run --detach`'s supervisor whose driver command is gone, at the start or at a crash's restart."""

    def setUp(self):
        self.s = Sandbox()
        self.addCleanup(self.s.close)
        self.mark = self.s.tmp / "torn-down"
        cfg = self.s.root / ".windvane/config.toml"
        cfg.write_text(f'teardown = "touch {self.mark}"\n' + cfg.read_text())
        self.s.publish("config")
        self.p = Project(self.s.root)
        self.p.state.mkdir(parents=True, exist_ok=True)
        self.pushes = []
        orig, driver.push = driver.push, lambda title, msg="", *a, **k: self.pushes.append((title, msg))
        self.addCleanup(setattr, driver, "push", orig)

    def supervise(self, start=None):
        with redirect_stdout(io.StringIO()):
            return driver.supervise(self.p, ["run"], now() + timedelta(hours=1), start=start, sleep=lambda s: None)

    def test_a_start_that_cant_find_its_command_tears_down_and_says_how_to_reinstall(self):
        def start(a, env):
            raise FileNotFoundError(2, "No such file or directory", "/old/venv/bin/windvane")
        self.assertEqual(self.supervise(start), 1)
        self.assertTrue(self.mark.exists())
        self.assertEqual(self.pushes, [("windvane stopped", "Why: windvane couldn't start its driver "
                                        "(/old/venv/bin/windvane: no such file or directory).\nReinstall windvane "
                                        "(pipx reinstall windvane, or sh install.sh in its clone), then start the "
                                        "run again.")])
        self.assertIn("couldn't start its driver (/old/venv/bin/windvane", (self.p.state / "autopilot.log").read_text())
        self.assertEqual((self.p.state / "lock").read_text(), "")

    def test_the_default_start_names_the_interpreter_that_went(self):
        """A script whose #! interpreter is gone: subprocess's own error would name the script."""
        old = f"{self.s.tmp}/old-python/bin/python3"
        cmd = script(self.s.tmp / "windvane", old)
        with mock.patch.object(driver, "exe", lambda: [str(cmd)]):
            self.assertEqual(self.supervise(), 1)
        self.assertIn(f"({old}: no such file or directory)", self.pushes[0][1])
        self.assertTrue(self.mark.exists())


@unittest.skipUnless(Path("/proc/self/exe").exists(), "needs /proc")
class Doctor(unittest.TestCase):
    """The running driver's interpreter or install, gone since it started."""

    def setUp(self):
        self.s = Sandbox()
        self.addCleanup(self.s.close)
        self.p = Project(self.s.root)
        self.p.state.mkdir(parents=True, exist_ok=True)

    def running(self, cmd):
        """`cmd` as the run's driver: its pid in the project's lock, held."""
        proc = subprocess.Popen(cmd)
        self.addCleanup(proc.wait)
        self.addCleanup(proc.kill)
        fh = open(self.p.state / "lock", "w")
        self.addCleanup(fh.close)
        fcntl.flock(fh, fcntl.LOCK_EX)
        fh.write(str(proc.pid))
        fh.flush()
        self.pid = proc.pid
        return proc.pid

    def test_a_moved_interpreter_is_named_with_the_reinstall_step(self):
        python = self.s.tmp / "old-python3"
        shutil.copy(shutil.which("sleep"), python)  # any program does: it stands for the driver's interpreter
        pid = self.running([str(python), "60"])
        self.assertEqual(doctor.moved_warnings(self.p), [])  # still there
        python.unlink()
        self.assertEqual(doctor.moved_warnings(self.p), [
            ("warn", f"the running driver (pid {pid}) started from {python}, which is gone: it can't restart on new "
             "code or after a crash", "reinstall windvane (pipx reinstall windvane, or sh install.sh in its clone)")])

    def test_an_interpreter_replaced_in_place_is_no_warning(self):
        """A distro upgrade or a reinstall puts a new file at the same path: a restart execs that, so all is well."""
        python = self.s.tmp / "python3"
        shutil.copy(shutil.which("sleep"), python)
        self.running([str(python), "60"])
        python.unlink()
        shutil.copy(shutil.which("sleep"), python)
        self.assertTrue(os.readlink(f"/proc/{self.pid}/exe").endswith(" (deleted)"))
        self.assertEqual(doctor.moved_warnings(self.p), [])

    def test_a_moved_install_is_named(self):
        cmd = script(self.s.tmp / "windvane", "/bin/sh")
        self.running([str(cmd)])
        cmd.unlink()
        [(level, what, fix)] = doctor.moved_warnings(self.p)
        self.assertIn(f"started from {cmd}, which is gone", what)

    def test_quiet_when_it_cant_tell(self):
        self.assertEqual(doctor.moved_warnings(self.p), [])  # no run
        (self.p.state / "lock").write_text("12345")  # a pid left behind, but no lock held: no run either
        self.assertEqual(doctor.moved_warnings(self.p), [])
        self.assertIsNone(doctor.gone_from(2 ** 22 + 1))  # no such process


if __name__ == "__main__":
    unittest.main()
