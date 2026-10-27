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


class Where(Base):
    def setUp(self):
        super().setUp()
        self.write(SPENDING)

    def test_where(self):
        """T-003: `sum data.csv amount --where category=food` prints the sum over the rows whose `category` is `food`."""
        self.assertEqual(self.ok("sum", "data.csv", "amount", "--where", "category=food"), "31.50")
        self.assertEqual(self.ok("sum", "data.csv", "amount", "--where", "amount>10"), "837.75")

    def test_two_conditions(self):
        """T-003: two `--where` conditions sum only the rows matching both; with none matching, the sum is `0`."""
        self.assertEqual(self.ok("sum", "data.csv", "amount", "--where", "category=food", "--where", "amount>=10"),
                         "22.50")
        self.assertEqual(self.ok("sum", "data.csv", "amount", "--where", "category=fun", "--where", "amount<5"), "0")
        self.assertEqual(self.ok("sum", "data.csv", "amount", "--where", "category=car"), "0")

    def test_skipped_rows(self):
        """T-003: a non-number cell in a skipped row is no error; one in a kept row prints T-001's error."""
        self.write("kind,amount\nreal,1.5\nnote,see below\nreal,2\nreal,oops\n")
        self.assertEqual(self.ok("sum", "data.csv", "amount", "--where", "kind=note", "--where", "kind!=note"), "0")
        self.fails(1, "error: line 5: not a number: oops", "sum", "data.csv", "amount", "--where", "kind=real")
        self.write("kind,amount\nreal,1.5\nnote,see below\nreal,2\n")
        self.assertEqual(self.ok("sum", "data.csv", "amount", "--where", "kind=real"), "3.5")

    def test_errors(self):
        """T-003: a bad condition prints `error: bad condition: <condition>` and exits 2; an unknown column `error: no column: <column>`, exit 1."""
        r = self.csvtool("sum", "data.csv", "amount", "--where", "category=food", "--where", "amount>x")
        self.assertEqual((r.returncode, r.stderr.strip()), (2, "error: bad condition: amount>x"))
        self.assertEqual(r.stdout, "")
        self.fails(1, "error: no column: kind", "sum", "data.csv", "amount", "--where", "kind=food")
