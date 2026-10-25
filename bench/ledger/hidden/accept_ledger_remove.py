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


class Remove(Base):
    def test_remove(self):
        """T-003: `python3 ledger.py remove 2` prints `removed #2`, and `list` no longer shows it."""
        for note in ("a", "b", "c"):
            self.ok("add", "1", note)
        self.assertEqual(self.ok("remove", "2"), "removed #2")
        self.assertEqual(self.ok("list").splitlines(), ["#1 1.00 a", "#3 1.00 c"])

    def test_unknown(self):
        """T-003: an id that isn't in the ledger prints `error: no entry #9` (for 9) to stderr and exits 1."""
        self.ok("add", "1", "a")
        before = (self.dir / "ledger.json").read_bytes()
        r = self.ledger("remove", "9")
        self.assertEqual((r.returncode, r.stderr.strip()), (1, "error: no entry #9"))
        self.assertEqual((self.dir / "ledger.json").read_bytes(), before)

    def test_never_reused(self):
        """T-003: ids are never reused: with entries #1 and #2, after `remove 2` the next add prints `added #3`."""
        self.ok("add", "1", "a")
        self.ok("add", "2", "b")
        self.ok("remove", "2")
        self.assertEqual(self.ok("add", "3", "c"), "added #3")
