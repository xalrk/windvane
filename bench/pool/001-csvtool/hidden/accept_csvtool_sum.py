import csv
import io
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

CSVTOOL = Path("csvtool.py").resolve()


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, True)

    def write(self, text, name="data.csv", bom=False):
        (self.dir / name).write_bytes((b"\xef\xbb\xbf" if bom else b"") + text.encode("utf-8"))
        return name

    def csvtool(self, *args):
        return subprocess.run([sys.executable, str(CSVTOOL), *args], cwd=self.dir, capture_output=True, text=True,
                              timeout=30)

    def ok(self, *args):
        r = self.csvtool(*args)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout.strip()

    def fails(self, code, message, *args):
        r = self.csvtool(*args)
        self.assertEqual((r.returncode, r.stderr.strip()), (code, message), r.stdout)
        self.assertEqual(r.stdout, "")

    def rows(self, *args):
        return list(csv.reader(io.StringIO(self.ok(*args))))


SPENDING = """date,category,amount,note
2026-10-01,food,12.50,groceries
2026-10-02,rent,800,october
2026-10-03,food,9,lunch
2026-10-04,fun,10,"cinema, with Ana"
2026-10-05,food,10,"the ""good"" bread"
2026-10-06,fun,25.25,concert
"""


class Sum(Base):
    def test_exact(self):
        """T-001: prints the column's exact sum and nothing else: `0.10` and `0.20` make `0.30`, `12.5` and `3.25` make `15.75`."""
        self.write("name,amount\na,0.10\nb,0.20\n")
        self.assertEqual(self.csvtool("sum", "data.csv", "amount").stdout, "0.30\n")
        self.write("name,amount\na,12.5\nb,3.25\n")
        self.assertEqual(self.ok("sum", "data.csv", "amount"), "15.75")
        self.write(SPENDING)
        self.assertEqual(self.ok("sum", "data.csv", "amount"), "866.75")

    def test_decimals(self):
        """T-001: the sum keeps as many decimals as the most precise cell; whole numbers sum to a whole number; a header and no rows sums to `0`."""
        self.write("x,amount\na,1.10\nb,2\n")
        self.assertEqual(self.ok("sum", "data.csv", "amount"), "3.10")
        self.write("x,amount\na,3\nb,4\nc,-2\nd,2\n")
        self.assertEqual(self.ok("sum", "data.csv", "amount"), "7")
        self.write("x,amount\n")
        self.assertEqual(self.ok("sum", "data.csv", "amount"), "0")

    def test_blank_and_bom(self):
        """T-001: blank cells are skipped, and a file saved with a byte-order mark works the same, its first column included."""
        self.write("amount,note\n5,a\n,b\n  ,c\n -1.5 ,d\n", bom=True)
        self.assertEqual(self.ok("sum", "data.csv", "amount"), "3.5")
        self.write("amount,note\r\n2,a\r\n3,b\r\n", bom=True)
        self.assertEqual(self.ok("sum", "data.csv", "amount"), "5")

    def test_no_column(self):
        """T-001: a column the header doesn't have prints `error: no column: <column>` to stderr and exits 1."""
        self.write(SPENDING)
        self.fails(1, "error: no column: price", "sum", "data.csv", "price")

    def test_not_a_number(self):
        """T-001: a cell that isn't a number prints `error: line <n>: not a number: <cell>` (the header is line 1) and exits 1."""
        self.write("x,amount\na,1\nb,2\nc,1e3\nd,4\n")
        self.fails(1, "error: line 4: not a number: 1e3", "sum", "data.csv", "amount")
        self.write("x,amount\na,1\nb,\"1,000\"\n")
        self.fails(1, "error: line 3: not a number: 1,000", "sum", "data.csv", "amount")

    def test_cannot_read(self):
        """T-001: a file that can't be read prints `error: cannot read <file>` and exits 1."""
        self.fails(1, "error: cannot read missing.csv", "sum", "missing.csv", "amount")
        (self.dir / "folder.csv").mkdir()
        self.fails(1, "error: cannot read folder.csv", "sum", "folder.csv", "amount")

    def test_file_untouched(self):
        """T-001: csvtool only reads: the file is byte for byte as it was."""
        self.write(SPENDING, bom=True)
        before = (self.dir / "data.csv").read_bytes()
        self.ok("sum", "data.csv", "amount")
        self.csvtool("sum", "data.csv", "note")
        self.assertEqual((self.dir / "data.csv").read_bytes(), before)
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), ["data.csv"])
