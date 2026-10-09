"""The sweep's `ps` path against the real `ps` (test_sweep's uses a fake one): a start time read wrong lets the strict
owner check kill a stranger that reused a pid, so it must agree with /proc and with when a child really started."""
import os
import shutil
import subprocess
import sys
import time
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from windvane import runner, util  # noqa: E402


@unittest.skipUnless(shutil.which("ps"), "no ps on this system")
class RealPs(unittest.TestCase):
    def setUp(self):
        self.proc = runner.started_at(os.getpid()) if util.PROC.is_dir() else None
        patch = unittest.mock.patch.object(util, "PROC", Path(__file__).parent / "no-such-proc")
        patch.start()
        self.addCleanup(patch.stop)

    def child(self, *cmd):
        before = time.time()
        c = subprocess.Popen(cmd or ["sleep", "30"])
        self.addCleanup(lambda: (c.kill(), c.wait()))
        return c, before, time.time()

    def test_own_start_agrees_with_proc(self):
        t = runner.started_at(os.getpid())
        self.assertIsNotNone(t)
        self.assertLessEqual(t, time.time())
        if self.proc is None:
            self.skipTest("no /proc to compare with")
        self.assertLessEqual(abs(t - self.proc), 2)  # ps prints whole seconds

    def test_a_childs_start_is_when_it_started(self):
        c, before, after = self.child()
        t = runner.started_at(c.pid)
        self.assertIsNotNone(t)
        self.assertGreaterEqual(t, int(before) - 1)
        self.assertLessEqual(t, after + 1)

    def test_a_dead_pid_gives_none(self):
        c, _, _ = self.child("true")
        c.wait()
        self.assertIsNone(runner.started_at(c.pid))


if __name__ == "__main__":
    unittest.main()
