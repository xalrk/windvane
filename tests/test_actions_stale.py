"""A cached Actions total over a day old counts as unknown, so recounts that keep failing can't leave workers'
commits untagged all month; and a recount that raises is one log line, not a thread's traceback (T-076)."""
import contextlib
import io
import os
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from windvane import actions  # noqa: E402
from windvane.config import Project  # noqa: E402
from windvane.util import save_json  # noqa: E402
from helpers import Sandbox  # noqa: E402


class Stale(unittest.TestCase):
    def setUp(self):
        self.s = Sandbox()
        cfg = self.s.root / ".windvane/config.toml"
        cfg.write_text("actions_minutes = 100\n" + cfg.read_text())  # cap 90
        self.s.publish("budget")
        self.p = Project(self.s.root)
        self.wt = self.s.tmp / "slot-1"
        self.s.git("worktree", "add", "-q", "--detach", str(self.wt))
        self.out = io.StringIO()  # util.log echoes to stdout

    def tearDown(self):
        self.assertTrue(actions.counting.acquire(timeout=10))
        actions.counting.release()
        actions.asked.clear()
        actions.stale_logged.clear()
        self.s.close()

    def cache(self, used, age):
        save_json(actions.cache_file(), {"month": actions.month(), "runs": {},
                                         "total": {"used": used, "at": time.time() - age}})

    def log(self):
        f = self.p.state / "autopilot.log"
        return f.read_text().splitlines() if f.exists() else []

    def commit(self, msg):
        """A commit in the worker's worktree under the environment a worker session gets; its subject."""
        with contextlib.redirect_stdout(self.out):
            env, line = actions.worker_git(self.p, self.wt)
        self.assertEqual(line, "")
        (self.wt / "a.txt").write_text(msg)
        self.s.git("add", "a.txt", cwd=self.wt)
        os.environ.update(env)
        try:
            self.s.git("commit", "-qm", msg, cwd=self.wt)
        finally:
            for k in env:
                os.environ.pop(k)
        return self.s.git("log", "-1", "--format=%s", cwd=self.wt)

    def test_an_under_cap_total_two_days_old_tags(self):
        self.cache(10, 2 * 86400)
        self.assertTrue(actions.cached_over_cap(self.p))
        self.assertEqual(self.commit("one"), f"one {actions.SKIP}")
        self.assertEqual(self.commit("two"), f"two {actions.SKIP}")
        said = [x for x in self.log() if "Actions count is 2 days old" in x]
        self.assertEqual(len(said), 1, self.log())  # once a run, not once a session
        self.assertIn("gh auth status", said[0])

    def test_no_stale_line_while_a_recount_is_in_flight(self):
        self.cache(10, 2 * 86400)
        self.assertTrue(actions.counting.acquire(blocking=False))  # the driver's start-up recount, still running
        try:
            self.assertEqual(self.commit("one"), f"one {actions.SKIP}")  # still tags: the count is unknown
        finally:
            actions.counting.release()
        self.assertEqual(self.log(), [])  # but doesn't call it failing yet
        self.commit("two")  # the recount ended and the count is still old: now it is failing
        self.assertEqual(len([x for x in self.log() if "2 days old" in x]), 1, self.log())

    def test_a_fresh_under_cap_total_does_not_tag(self):
        self.cache(10, 60)
        self.assertFalse(actions.cached_over_cap(self.p))
        self.assertEqual(self.commit("one"), "one")
        self.assertEqual(self.log(), [])

    def test_the_boundary_is_the_constant(self):
        self.cache(10, actions.STALE_S - 120)
        self.assertFalse(actions.cached_over_cap(self.p))
        self.assertEqual(self.commit("inside"), "inside")
        self.cache(10, actions.STALE_S + 120)
        self.assertTrue(actions.cached_over_cap(self.p))
        self.assertEqual(self.commit("outside"), f"outside {actions.SKIP}")

    def test_status_shows_the_date_of_another_days_count(self):
        self.cache(10, 2 * 86400)
        day = time.localtime(time.time() - 2 * 86400)
        self.assertIn(f"(counted {time.strftime('%b', day)} {day.tm_mday} ", actions.line(self.p))
        self.cache(10, 2 * 3600)
        if time.localtime(time.time() - 2 * 3600)[:3] == time.localtime()[:3]:  # not just past midnight
            self.assertRegex(actions.line(self.p), r"\(counted \d\d:\d\d\)$")

    def test_a_recount_that_raises_logs_one_line_and_frees_the_lock(self):
        made, real = [], threading.Thread

        def thread(**kw):
            made.append(real(**kw))
            return made[-1]

        err = io.StringIO()
        boom = mock.Mock(side_effect=RuntimeError("gh exploded\nsecond line"))
        with mock.patch.object(actions, "count", boom), mock.patch.object(threading, "Thread", thread), \
                contextlib.redirect_stderr(err), contextlib.redirect_stdout(self.out):
            actions.refresh(self.p)
            made[0].join(10)  # past the thread's end, where an uncaught exception would be printed
        self.assertEqual(err.getvalue(), "")
        failed = [x for x in self.log() if "recount failed" in x]
        self.assertEqual(len(failed), 1, self.log())
        self.assertTrue(failed[0].endswith("actions: recount failed: RuntimeError: gh exploded"), failed[0])
        again = mock.Mock(return_value=None)
        with mock.patch.object(actions, "count", again):
            actions.refresh(self.p)  # the lock was released, so this one runs
            self.assertTrue(actions.counting.acquire(timeout=10))
            actions.counting.release()
        again.assert_called_once_with(self.s.root)


if __name__ == "__main__":
    unittest.main()
