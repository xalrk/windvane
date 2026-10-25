import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

LEDGER = Path("ledger.py").resolve()


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, True)

    def ledger(self, *args):
        return subprocess.run([sys.executable, str(LEDGER), *args], cwd=self.dir, capture_output=True, text=True,
                              timeout=30)

    def ok(self, *args):
        r = self.ledger(*args)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout.strip()


class Add(Base):
    def test_ids(self):
        """T-001: prints `added #1`, and the next add prints `added #2`"""
        self.assertEqual(self.ok("add", "12.50", "coffee", "beans"), "added #1")
        self.assertEqual(self.ok("add", "3", "bus"), "added #2")

    def test_file(self):
        """T-001: the file `--file` names (default `ledger.json` in the current directory) is created and holds every entry as JSON."""
        self.ok("add", "12.50", "coffee", "beans")
        self.assertIn("coffee beans", json.dumps(json.loads((self.dir / "ledger.json").read_text())))
        self.ok("--file", "other.json", "add", "1", "tea")
        text = json.dumps(json.loads((self.dir / "other.json").read_text()))
        self.assertIn("tea", text)
        self.assertNotIn("coffee", text)

    def test_negative(self):
        """T-001: a negative amount is an entry too: `add -3.20 refund` prints its `added #<id>` line."""
        self.ok("add", "5", "lunch")
        self.assertEqual(self.ok("add", "-3.20", "refund"), "added #2")

    def test_bad_amount(self):
        """T-001: an amount that isn't a number with at most two decimals (`12.555`, `abc`) prints `error: bad amount: <amount>` to stderr, exits 2 and leaves the file unchanged."""
        self.ok("add", "1", "first")
        before = (self.dir / "ledger.json").read_bytes()
        for bad in ("12.555", "abc"):
            r = self.ledger("add", bad, "x")
            self.assertEqual((r.returncode, r.stderr.strip()), (2, f"error: bad amount: {bad}"))
            self.assertNotIn("Traceback", r.stderr)
        self.assertEqual((self.dir / "ledger.json").read_bytes(), before)
