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

HEADER = ["date", "category", "amount", "note"]


class Filter(Base):
    def setUp(self):
        super().setUp()
        self.write(SPENDING)

    def notes(self, condition):
        rows = self.rows("filter", "data.csv", condition)
        self.assertEqual(rows[0], HEADER)
        return [r[3] for r in rows[1:]]

    def test_equal(self):
        """T-002: `filter data.csv category=food` prints the header row, then every row whose `category` is exactly `food`, in file order, as CSV."""
        out = self.ok("filter", "data.csv", "category=food").splitlines()
        self.assertEqual(out[:3], ["date,category,amount,note", "2026-10-01,food,12.50,groceries",
                                   "2026-10-03,food,9,lunch"])
        self.assertEqual(self.notes("category=food"), ["groceries", "lunch", 'the "good" bread'])
        self.assertEqual(self.notes("category=foo"), [])
        self.assertEqual(self.notes("category=Food"), [])

    def test_ops(self):
        """T-002: `amount>10`, `amount<=10`, `amount>=10`, `amount<10`, `category!=food` each give the rows picked by hand; `amount>9.5` is numeric."""
        self.assertEqual(self.notes("amount>10"), ["groceries", "october", "concert"])
        self.assertEqual(self.notes("amount<=10"), ["lunch", "cinema, with Ana", 'the "good" bread'])
        self.assertEqual(self.notes("amount>=10"), ["groceries", "october", "cinema, with Ana",
                                                    'the "good" bread', "concert"])
        self.assertEqual(self.notes("amount<10"), ["lunch"])
        self.assertEqual(self.notes("category!=food"), ["october", "cinema, with Ana", "concert"])
        self.assertEqual(self.notes("amount>9.5"), ["groceries", "october", "cinema, with Ana",
                                                    'the "good" bread', "concert"])
        self.assertEqual(self.notes("amount=10"), ["cinema, with Ana", 'the "good" bread'])

    def test_not_a_number_cell(self):
        """T-002: a row whose cell isn't a number doesn't match the numeric ops."""
        self.write("item,amount\na,5\nb,n/a\nc,20\nd,\n")
        rows = self.rows("filter", "data.csv", "amount>1")
        self.assertEqual(rows, [["item", "amount"], ["a", "5"], ["c", "20"]])
        self.assertEqual(self.rows("filter", "data.csv", "amount<100"), [["item", "amount"], ["a", "5"], ["c", "20"]])

    def test_quoting(self):
        """T-002: cells holding commas or quotes come out as valid CSV: reading the output back gives the same cells."""
        self.assertEqual(self.rows("filter", "data.csv", "category!=rent"),
                         [r for r in csv.reader(io.StringIO(SPENDING)) if r and r[1] != "rent"])
        self.write("name,city\n\"Doe, Jane\",Zürich\n\"say \"\"hi\"\"\",Oslo\n")
        self.assertEqual(self.rows("filter", "data.csv", "city=Zürich"), [["name", "city"], ["Doe, Jane", "Zürich"]])

    def test_no_match(self):
        """T-002: with no matching row, only the header row is printed, exit 0."""
        self.assertEqual(self.ok("filter", "data.csv", "amount>10000"), "date,category,amount,note")

    def test_bad_condition(self):
        """T-002: no operator, or a numeric op with a value that isn't a number, prints `error: bad condition: <condition>` and exits 2."""
        for cond in ("category", "amount>abc", "amount<=1,5"):
            r = self.csvtool("filter", "data.csv", cond)
            self.assertEqual((r.returncode, r.stderr.strip()), (2, f"error: bad condition: {cond}"))

    def test_no_column_and_unreadable(self):
        """T-002: an unknown column prints `error: no column: <column>`, an unreadable file `error: cannot read <file>`, both exit 1."""
        self.fails(1, "error: no column: price", "filter", "data.csv", "price>1")
        self.fails(1, "error: cannot read nope.csv", "filter", "nope.csv", "category=food")
