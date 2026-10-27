import subprocess
import sys
import unittest
from pathlib import Path

CSVTOOL = Path(__file__).resolve().parents[1] / "csvtool.py"


class Smoke(unittest.TestCase):
    def test_version(self):
        r = subprocess.run([sys.executable, str(CSVTOOL), "--version"], capture_output=True, text=True)
        self.assertEqual((r.returncode, r.stdout.strip()), (0, "csvtool 0.1"))
