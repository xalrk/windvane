"""Locks are let go of explicitly, not by closing their file: a thread's fork (a git or a session starting) shares the
open file until the child execs, so a close alone can leave the lock held a moment after the run or the probe is done.
A dup of the fd stands in for that child here."""
import contextlib
import fcntl
import io
import os
import sys
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from windvane import driver, jobs, schedule, util  # noqa: E402
from windvane.config import Project  # noqa: E402
from windvane.util import now  # noqa: E402
from helpers import Sandbox  # noqa: E402
from test_pipeline import args  # noqa: E402


def free(path):
    """Whether a new open file can take `path`'s lock outright, as a starting run does."""
    with open(path, "a") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        fcntl.flock(fh, fcntl.LOCK_UN)
        return True


class Release(unittest.TestCase):
    def setUp(self):
        self.addCleanup(os.chdir, os.getcwd())
        self.s = Sandbox()
        self.addCleanup(self.s.close)
        self.p = Project(self.s.root)
        self.p.state.mkdir(parents=True, exist_ok=True)

    def forked(self, fh):
        """A child forked while `fh` was open, not yet at its exec."""
        fd = os.dup(fh.fileno())
        self.addCleanup(os.close, fd)

    def test_a_probe_leaves_no_lock_behind(self):
        lock = self.p.state / "lock"
        lock.touch()
        with open(lock) as fh:
            self.forked(fh)
            self.assertFalse(util.held(fh))
        self.assertTrue(free(lock))
        self.assertFalse(driver.locked(self.p))
        self.assertFalse(jobs.locked(lock))
        (self.p.state / schedule.LOCK).touch()
        self.assertIsNone(schedule.state(self.p))
        self.assertTrue(free(lock) and free(self.p.state / schedule.LOCK))

    def test_a_run_ends_with_its_lock_free(self):
        self.s.ticket("T-001")
        take = driver.Pilot.take_lock

        def take_lock(pilot):
            take(pilot)
            self.forked(pilot.lockfile)

        with mock.patch.object(driver.Pilot, "take_lock", take_lock), mock.patch.object(driver, "push"), \
                contextlib.redirect_stdout(io.StringIO()):
            driver.Pilot(self.p, args()).run(now() + timedelta(hours=1))
        self.assertFalse(driver.locked(self.p))
        self.assertTrue(free(self.p.state / "lock"))


if __name__ == "__main__":
    unittest.main()
