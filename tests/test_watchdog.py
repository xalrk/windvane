"""The sandbox's hang watchdog: a stuck test names where it is stuck and ends the run, instead of sitting silent until
a CI job's time limit cancels it with no clue which test it was."""
import os
import subprocess
import sys
import unittest
from pathlib import Path

from helpers import tmpdir

TESTS = Path(__file__).resolve().parent


class Watchdog(unittest.TestCase):
    def run_sandboxed(self, body):
        """`body` run in a fresh Python after making a Sandbox whose watchdog fires after 1 second. Its temp dirs go
        in one removed with this test: a run the watchdog ends never cleans up after itself."""
        code = (f"import sys, time\nsys.path.insert(0, {str(TESTS)!r})\nimport helpers\nhelpers.HANG_S = 1\n"
                f"s = helpers.Sandbox()\n{body}\n")
        return subprocess.run([sys.executable, "-c", code], cwd=TESTS.parent, capture_output=True, text=True,
                              timeout=60, env={**os.environ, "TMPDIR": str(tmpdir(self))})

    def test_a_stuck_test_prints_where_it_is_and_ends_the_run(self):
        out = self.run_sandboxed("def stuck_here():\n    time.sleep(30)\nstuck_here()")
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("Timeout", out.stderr)
        self.assertIn("stuck_here", out.stderr)

    def test_a_closed_sandbox_leaves_the_rest_of_the_run_alone(self):
        out = self.run_sandboxed("s.close()\ntime.sleep(2)\nprint('done')")
        self.assertEqual((out.returncode, out.stdout.strip()), (0, "done"), out.stderr)
