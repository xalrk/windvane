"""With isolate off, the run's end names only what started in the checkout after the run began: a run's start read on
the processes' own clock (their start ticks), so a container that fakes /proc/uptime (lxcfs) while the ticks stay the
host's can't make the user's own processes look like the run's helpers."""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from windvane import driver, util  # noqa: E402

TCK = os.sysconf("SC_CLK_TCK")
USERS, HELPER = 4000001, 4000002  # pids no real process here has


class LeftoverClock(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.proc = Path(tmp.name) / "proc"
        self.root = Path(os.path.realpath(tmp.name)) / "checkout"
        self.root.mkdir()
        self.p = SimpleNamespace(cfg={"isolate": False}, main_root=lambda: self.root)
        patch = mock.patch.object(util, "PROC", self.proc)
        patch.start()
        self.addCleanup(patch.stop)
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        for k in (driver.SUPERVISED, driver.BEGAN):
            os.environ.pop(k, None)
        # the host's clock: booted 10 000 s ago; the container's uptime says 50 s
        self.proc.mkdir()
        (self.proc / "uptime").write_text("50.00 40.00\n")
        self.fake(os.getpid(), 9000, None)  # the driver, started at 9000 s
        self.fake(USERS, 8000, self.root, "vim notes")  # the user's, older than the run
        self.fake(HELPER, 9500, self.root, "sleep 120")  # what the run's tools started

    def fake(self, pid, started, cwd, cmd="windvane run", tty=0):
        d = self.proc / str(pid)
        d.mkdir()
        fields = ["S", "1", str(pid), str(pid), str(tty)] + ["0"] * 14 + [str(int(started * TCK))] + ["0"] * 5
        (d / "stat").write_text(f"{pid} ({cmd.split()[0]}) " + " ".join(fields) + "\n")
        (d / "cmdline").write_bytes(cmd.replace(" ", "\0").encode() + b"\0")
        if cwd:
            os.symlink(cwd, d / "cwd")

    def left(self):
        return driver.leftover_helpers(self.p, driver.run_began())

    def test_a_faked_uptime_names_only_what_the_run_started(self):
        self.assertEqual(self.left(), [(HELPER, "sleep 120")])

    def test_an_unreadable_uptime_changes_nothing(self):
        (self.proc / "uptime").unlink()
        self.assertEqual(self.left(), [(HELPER, "sleep 120")])

    def test_no_start_of_its_own_reports_nothing_rather_than_everything(self):
        (self.proc / str(os.getpid()) / "stat").unlink()
        self.assertIsNone(driver.run_began())
        self.assertEqual(self.left(), [])

    def test_a_restarted_driver_goes_by_the_supervisors_start(self):
        """Its own start is after the crashed driver's helpers: the supervisor's, before them, still counts them."""
        os.environ.update({driver.SUPERVISED: "1", driver.BEGAN: "8500.0"})
        self.fake(HELPER + 1, 8700, self.root, "sleep 121")  # the crashed driver's tools' helper
        self.assertEqual(self.left(), [(HELPER, "sleep 120"), (HELPER + 1, "sleep 121")])

    def test_a_process_with_a_terminal_is_never_named(self):
        self.fake(HELPER + 1, 9600, self.root, "bash", tty=34816)
        self.assertEqual(self.left(), [(HELPER, "sleep 120")])


class SupervisorPassesItsStart(unittest.TestCase):
    def test_the_driver_it_starts_gets_its_start(self):
        seen = {}

        def start(args, env):
            seen.update(env)
            raise SystemExit  # one look is enough
        with mock.patch.dict(os.environ), self.assertRaises(SystemExit):
            driver.supervise(SimpleNamespace(state=Path("/nonexistent")), ["run"], None, start=start)
        self.assertEqual(float(seen[driver.BEGAN]), util.boot_started(os.getpid()))


if __name__ == "__main__":
    unittest.main()
