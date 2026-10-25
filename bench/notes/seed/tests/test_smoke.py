import json
import subprocess
import sys
import tempfile
import unittest
import urllib.request
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / "app.py"


class Smoke(unittest.TestCase):
    def test_health(self):
        data = Path(tempfile.mkdtemp()) / "notes.json"
        p = subprocess.Popen([sys.executable, str(APP), "--port", "0", "--data", str(data)], stdout=subprocess.PIPE,
                             text=True)
        self.addCleanup(p.wait)
        self.addCleanup(p.kill)
        url = p.stdout.readline().split()[-1]
        with urllib.request.urlopen(url + "health", timeout=10) as r:
            self.assertEqual(json.load(r), {"ok": True})
