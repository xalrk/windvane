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


class By(Base):
    def setUp(self):
        super().setUp()
        self.write(SPENDING)

    def test_by(self):
        """T-004: `sum data.csv amount --by category` prints `category,amount`, then `<group>,<sum>` per category in first-seen order."""
        self.assertEqual(self.ok("sum", "data.csv", "amount", "--by", "category").splitlines(),
                         ["category,amount", "food,31.50", "rent,800", "fun,35.25"])

    def test_exact(self):
        """T-004: each group's sum is exact and keeps its decimals as T-001's sums do."""
        self.write("who,paid\nana,0.10\nbo,1\nana,0.20\nbo,2\n")
        self.assertEqual(self.ok("sum", "data.csv", "paid", "--by", "who").splitlines(),
                         ["who,paid", "ana,0.30", "bo,3"])

    def test_quoting(self):
        """T-004: group values holding commas or quotes come out as valid CSV, read back as typed."""
        self.write('shop,amount\n"Smith, Jones",2\n"the ""corner""",3\n"Smith, Jones",4.5\nCafé,1\n')
        self.assertEqual(self.rows("sum", "data.csv", "amount", "--by", "shop"),
                         [["shop", "amount"], ["Smith, Jones", "6.5"], ['the "corner"', "3"], ["Café", "1"]])

    def test_with_where(self):
        """T-004: with `--where`, only matching rows are grouped; a group with no matching row isn't printed."""
        self.assertEqual(self.ok("sum", "data.csv", "amount", "--by", "category", "--where", "amount<=10").splitlines(),
                         ["category,amount", "food,19", "fun,10"])
        self.assertEqual(self.ok("sum", "data.csv", "amount", "--where", "amount>1000", "--by", "category"),
                         "category,amount")

    def test_no_column(self):
        """T-004: a `--by` column the header doesn't have prints `error: no column: <column>` and exits 1."""
        self.fails(1, "error: no column: shop", "sum", "data.csv", "amount", "--by", "shop")
