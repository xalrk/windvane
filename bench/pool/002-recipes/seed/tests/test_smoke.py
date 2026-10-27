import subprocess
import sys
import unittest
from pathlib import Path

RECIPES = Path(__file__).resolve().parents[1] / "recipes.py"


class Smoke(unittest.TestCase):
    def test_version(self):
        r = subprocess.run([sys.executable, str(RECIPES), "--version"], capture_output=True, text=True)
        self.assertEqual((r.returncode, r.stdout.strip()), (0, "recipes 0.1"))
