import subprocess
import sys
import unittest
from pathlib import Path

LEDGER = Path(__file__).resolve().parents[1] / "ledger.py"


class Smoke(unittest.TestCase):
    def test_version(self):
        r = subprocess.run([sys.executable, str(LEDGER), "--version"], capture_output=True, text=True)
        self.assertEqual((r.returncode, r.stdout.strip()), (0, "ledger 0.1"))
