"""The test sandbox itself: it must behave the same on Linux and macOS, or a test hangs or passes without checking."""
import os
import shutil
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

import helpers
from helpers import Sandbox, running, tmpdir


class SandboxPython(unittest.TestCase):
    def test_python3_on_its_path_is_the_one_running_the_tests(self):
        """bin/windvane runs `python3` from PATH; /usr/bin's is 3.9 on macOS, too old for windvane."""
        s = Sandbox()
        self.addCleanup(s.close)
        self.assertEqual(Path(shutil.which("python3")).resolve(), Path(sys.executable).resolve())
        out = subprocess.run(["windvane", "--help"], capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stderr)


class Running(unittest.TestCase):
    def check(self):
        self.assertTrue(running(os.getpid()))
        done = subprocess.Popen([sys.executable, "-c", "pass"])
        done.wait()
        self.assertFalse(running(done.pid))  # gone
        zombie = subprocess.Popen([sys.executable, "-c", "pass"])
        self.addCleanup(zombie.wait)
        for _ in range(100):  # ended, not yet reaped
            if not running(zombie.pid):
                break
            time.sleep(0.05)
        self.assertFalse(running(zombie.pid))

    def test_with_proc(self):
        if not Path("/proc/self/stat").exists():
            self.skipTest("no /proc")
        self.check()

    def test_with_ps_as_on_macos(self):
        with mock.patch.object(helpers, "PROC", tmpdir(self)):
            self.check()
