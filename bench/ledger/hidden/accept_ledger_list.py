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


class List(Base):
    def test_list(self):
        """T-002: prints one line per entry, oldest first: `#<id> <amount> <note>`, e.g. `#1 12.50 coffee beans`."""
        self.ok("add", "12.5", "coffee", "beans")
        self.ok("add", "-3", "refund")
        self.assertEqual(self.ok("list").splitlines(), ["#1 12.50 coffee beans", "#2 -3.00 refund"])

    def test_balance(self):
        """T-002: prints `balance: <sum>` with two decimals, summed exactly: `0.10` and `0.20` make `balance: 0.30`."""
        self.ok("add", "0.10", "a")
        self.ok("add", "0.20", "b")
        self.assertEqual(self.ok("balance"), "balance: 0.30")
        self.ok("add", "-1", "c")
        self.assertEqual(self.ok("balance"), "balance: -0.70")

    def test_empty(self):
        """T-002: with no ledger file yet, `list` prints nothing and `balance` prints `balance: 0.00`, both exiting 0."""
        self.assertEqual(self.ok("list"), "")
        self.assertEqual(self.ok("balance"), "balance: 0.00")
