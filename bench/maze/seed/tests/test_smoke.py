import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class Smoke(unittest.TestCase):
    def test_start_and_quit(self):
        r = subprocess.run([sys.executable, str(ROOT / "game.py"), str(ROOT / "maps/first.txt")], input="quit\n",
                           capture_output=True, text=True, timeout=30)
        self.assertEqual((r.returncode, r.stdout.splitlines()), (0, ["You are at (1, 1).", "Bye."]))
