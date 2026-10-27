import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

HABITS = Path("habits.py").resolve()


def ago(n):
    return (date.today() - timedelta(days=n)).isoformat()


def monday(weeks_ago):
    today = date.today()
    return today - timedelta(days=today.weekday() + 7 * weeks_ago)


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.file = self.dir / "habits.txt"

    def write(self, *lines):
        self.file.write_text("".join(line + "\n" for line in lines), encoding="utf-8")

    def text(self):
        return self.file.read_text(encoding="utf-8") if self.file.exists() else None

    def habits(self, *args):
        return subprocess.run([sys.executable, str(HABITS), *args], cwd=self.dir, capture_output=True, text=True,
                              encoding="utf-8", timeout=30)

    def ok(self, *args):
        r = self.habits(*args)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout.strip().splitlines()

    def fails(self, code, error, *args):
        r = self.habits(*args)
        self.assertEqual((r.returncode, r.stderr.strip()), (code, error), args)
        self.assertNotIn("Traceback", r.stderr)


class Streak(Base):
    def test_lines(self):
        """T-002: `streak` prints `<name>: <N> days, best <M>` per habit in the order added (`1 day`, `0 days`), and nothing else."""
        self.write("habit walk", "habit read", "habit tea", f"{ago(0)} walk", f"{ago(1)} walk", f"{ago(0)} read")
        self.assertEqual(self.ok("streak"), ["walk: 2 days, best 2", "read: 1 day, best 1", "tea: 0 days, best 0"])

    def test_current(self):
        """T-002: the current streak ends today, or yesterday when today isn't done yet; it is 0 when neither is done."""
        self.write("habit a", "habit b", "habit c",
                   f"{ago(0)} a", f"{ago(1)} a", f"{ago(2)} a", f"{ago(4)} a",
                   f"{ago(1)} b", f"{ago(2)} b", f"{ago(3)} b", f"{ago(4)} b",
                   f"{ago(2)} c", f"{ago(3)} c")
        self.assertEqual(self.ok("streak"), ["a: 3 days, best 3", "b: 4 days, best 4", "c: 0 days, best 2"])

    def test_best(self):
        """T-002: the best streak is the longest run ever, the current one included, across leap days and year ends."""
        self.write("habit read", "2024-02-28 read", "2024-02-29 read", "2024-03-01 read",
                   "2025-12-30 read", "2025-12-31 read", "2026-01-01 read", "2026-01-02 read", f"{ago(1)} read")
        self.assertEqual(self.ok("streak"), ["read: 1 day, best 4"])
        self.write("habit read", "2023-02-27 read", "2023-02-28 read", "2023-03-01 read", "2023-03-02 read",
                   "2023-12-31 read", "2024-01-01 read", f"{ago(0)} read", f"{ago(1)} read")
        self.assertEqual(self.ok("streak"), ["read: 2 days, best 4"])
        lines = ["habit read"] + [f"{ago(n)} read" for n in range(6)] + ["2024-02-29 read"]
        self.write(*lines)
        self.assertEqual(self.ok("streak"), ["read: 6 days, best 6"])

    def test_twice(self):
        """T-001 / T-002: the same day written twice by hand counts once."""
        self.write("habit read", f"{ago(0)} read", f"{ago(0)} read", f"{ago(1)} read")
        self.assertEqual(self.ok("streak"), ["read: 2 days, best 2"])

    def test_one(self):
        """T-002: `streak read` prints only that habit's line; an unknown habit prints `error: no such habit: <name>` and exits 1."""
        self.write("habit walk", "habit read", f"{ago(0)} read")
        self.assertEqual(self.ok("streak", "read"), ["read: 1 day, best 1"])
        self.fails(1, "error: no such habit: run", "streak", "run")

    def test_empty_and_broken(self):
        """T-002: with no habits or no file `streak` prints `no habits yet`; a bad line gives T-001's error."""
        self.assertEqual(self.ok("streak"), ["no habits yet"])
        self.write("habit read", "", "read yesterday")
        self.fails(1, "error: habits.txt:3: bad line", "streak")
