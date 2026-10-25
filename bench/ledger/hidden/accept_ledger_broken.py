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


class Broken(Base):
    def test_not_a_ledger(self):
        """T-004: a file that isn't a ledger written by this program makes every command print `error: <file> is not a ledger file` to stderr and exit 1, leaving the file byte for byte as it was."""
        for text in ("{not json", '["a list"]', ""):
            f = self.dir / "ledger.json"
            f.write_text(text)
            for args in (["list"], ["balance"], ["add", "1", "x"], ["remove", "1"]):
                r = self.ledger(*args)
                self.assertEqual((r.returncode, r.stderr.strip()), (1, "error: ledger.json is not a ledger file"),
                                 (text, args))
                self.assertEqual(f.read_text(), text)

    def test_missing_folder(self):
        """T-004: `add` with a `--file` in a folder that doesn't exist prints one line starting `error: ` and exits 1, with no traceback."""
        r = self.ledger("--file", "nowhere/ledger.json", "add", "1", "x")
        self.assertEqual(r.returncode, 1)
        self.assertTrue(r.stderr.startswith("error: "), r.stderr)
        self.assertEqual(len(r.stderr.strip().splitlines()), 1, r.stderr)

    def test_leftover_tmp(self):
        """T-004: a leftover `<file>.tmp` from a crashed write is ignored: `list` shows the entries of `<file>`, and the next `add` works."""
        self.ok("add", "1", "kept")
        (self.dir / "ledger.json.tmp").write_text('{"half": ')
        self.assertEqual(self.ok("list"), "#1 1.00 kept")
        self.assertEqual(self.ok("add", "2", "next"), "added #2")
        self.assertEqual(self.ok("list").splitlines(), ["#1 1.00 kept", "#2 2.00 next"])
