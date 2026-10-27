import subprocess
import sys
import unittest
from pathlib import Path

HABITS = Path(__file__).resolve().parents[1] / "habits.py"


class Smoke(unittest.TestCase):
    def test_version(self):
        r = subprocess.run([sys.executable, str(HABITS), "--version"], capture_output=True, text=True)
        self.assertEqual((r.returncode, r.stdout.strip()), (0, "habits 0.1"))
