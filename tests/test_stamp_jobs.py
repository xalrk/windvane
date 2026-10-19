"""A bg job and the leftover check through a step of the wall clock: a job is named by the `stamp` `bg` wrote beside
its pid, and a run's helpers by their start ticks since boot against the run's own `clock`, so a clock set an hour
either way neither loses a running job nor mixes the user's processes up with the run's."""
import io
import os
import subprocess
import sys
import time
import types
import unittest
import unittest.mock
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from windvane import driver, jobs, util  # noqa: E402
from windvane.config import Project  # noqa: E402
from windvane.runner import IN_SANDBOX, NEST, TICKET  # noqa: E402
from helpers import Sandbox  # noqa: E402
from test_start_clock import HOUR, REAL, FakeProc  # noqa: E402


class Proc(FakeProc):
    """FakeProc with each copied pid's `cwd` and `cmdline` too, for the leftover check, and `stamp` reading a pid
    `bg` just started (copied into the fake as it's asked about)."""

    def setUp(self):
        super().setUp()
        stamp = util.stamp

        def synced(pid):
            self.sync(pid)
            return stamp(pid)
        patch = unittest.mock.patch.object(util, "stamp", synced)
        patch.start()
        self.addCleanup(patch.stop)

    def sync(self, *pids, **kw):
        super().sync(*pids, **kw)
        for pid in self.pids:
            d = self.proc / str(pid)
            if not d.is_dir():
                continue
            try:
                cwd, cmd = os.readlink(REAL / str(pid) / "cwd"), (REAL / str(pid) / "cmdline").read_bytes()
            except OSError:
                continue
            (d / "cwd").unlink(missing_ok=True)
            (d / "cwd").symlink_to(cwd)
            (d / "cmdline").write_bytes(cmd)


class Jobs(Proc, unittest.TestCase):
    def setUp(self):
        self.s = Sandbox()
        self.addCleanup(self.s.close)
        for k in (TICKET, NEST, IN_SANDBOX, jobs.RUN_STOP):
            os.environ.pop(k, None)
        self.p = Project(self.s.root)
        super().setUp()
        self.addCleanup(self.end)

    def end(self):
        if Path(f"{(base := jobs.job_base(self.p, 'j'))}.pid").exists():
            jobs.end_job(base, "the test's end")

    def started(self):
        with redirect_stdout(io.StringIO()):
            jobs.bg(self.p, "j", ["sleep", "30"], self.s.root)
        base = jobs.job_base(self.p, "j")
        self.assertTrue(Path(f"{base}.stamp").read_text().startswith("boot:"))
        return base

    def test_a_running_job_lives_through_a_clock_step(self):
        base = self.started()
        for step in (-HOUR, 2 * HOUR):
            with self.subTest(step=step):
                self.sync(step=step)
                self.assertTrue(jobs.alive(base))

    def test_bg_refuses_a_second_copy_after_a_step(self):
        self.started()
        self.sync(step=HOUR)
        with self.assertRaisesRegex(SystemExit, "still running"):
            jobs.bg(self.p, "j", ["sleep", "30"], self.s.root)

    def test_a_pid_reused_after_a_reboot_is_dead(self):
        base = self.started()
        self.reboot()
        self.assertFalse(jobs.alive(base))

    def test_an_older_jobs_pid_keeps_the_mtime_rule(self):
        base = self.started()
        Path(f"{base}.stamp").unlink()  # as an older windvane's bg left it
        self.assertTrue(jobs.alive(base))
        before = time.time() - HOUR
        os.utime(f"{base}.pid", (before, before))
        self.assertFalse(jobs.alive(base))

    def test_a_stamp_older_than_its_pid_is_ignored(self):
        """An older windvane started the name again: its pid is newer than the stamp a newer one left."""
        base = self.started()
        before = time.time() - HOUR
        os.utime(f"{base}.stamp", (before, before))
        self.sync(step=HOUR)  # a clock set forward makes its start look later than bg's write
        self.assertFalse(jobs.alive(base))  # the mtime rule, which the step fools, as it did


class Leftovers(Proc, unittest.TestCase):
    """With isolate off, what the run started in the checkout (after `since`) is a helper; the rest is the user's."""

    def setUp(self):
        super().setUp()
        self.root = Path(self.proc.parent / f"{self.proc.name}-checkout")
        self.root.mkdir()
        self.addCleanup(lambda: self.root.rmdir())
        self.p = types.SimpleNamespace(cfg={"isolate": False}, main_root=lambda: self.root)

    def spawn(self):
        c = subprocess.Popen(["sleep", "30"], cwd=self.root, start_new_session=True, stdin=subprocess.DEVNULL)
        self.addCleanup(lambda: (c.kill(), c.wait()))
        self.sync(c.pid)
        return c.pid

    def began(self):
        """A run's start as `driver.run_began` reads it: the driver's start ticks (here a stand-in's, outside the
        checkout)."""
        c = subprocess.Popen(["sleep", "30"], start_new_session=True, stdin=subprocess.DEVNULL)
        self.addCleanup(lambda: (c.kill(), c.wait()))
        self.sync(c.pid)
        return util.boot_started(c.pid)

    def test_a_process_older_than_the_run_stays_out_after_a_step_forward(self):
        users = self.spawn()
        time.sleep(0.05)
        since = self.began()
        self.assertNotIn(users, [pid for pid, _ in driver.leftover_helpers(self.p, since)])
        self.sync(step=HOUR)
        self.assertNotIn(users, [pid for pid, _ in driver.leftover_helpers(self.p, since)])

    def test_a_helper_the_run_started_is_listed_after_a_step_back(self):
        since = self.began()
        time.sleep(0.05)
        helper = self.spawn()
        self.sync(step=-HOUR)
        self.assertIn(helper, [pid for pid, _ in driver.leftover_helpers(self.p, since)])

    def test_without_a_clock_nothing_counts(self):
        self.spawn()
        self.assertEqual(driver.leftover_helpers(self.p, None), [])


if __name__ == "__main__":
    unittest.main()
