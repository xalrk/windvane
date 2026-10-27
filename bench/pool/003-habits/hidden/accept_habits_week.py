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


class Week(Base):
    def test_grid(self):
        """T-003: `week` prints `<first day> to <today>`, then per habit its padded name, seven marks oldest first (`#` done, `.` not) and `<N>/7`, and nothing else."""
        self.write("habit read", "habit stretch", "habit no-sugar",
                   f"{ago(6)} read", f"{ago(5)} read", f"{ago(3)} read", f"{ago(0)} read", f"{ago(9)} read",
                   f"{ago(1)} stretch", f"{ago(0)} stretch", f"{ago(7)} stretch")
        self.assertEqual(self.ok("week"), [f"{ago(6)} to {ago(0)}", "read     ##.#..# 4/7",
                                           "stretch  .....## 2/7", "no-sugar ....... 0/7"])

    def test_ending(self):
        """T-003: `week --ending 2026-03-01` shows the seven days ending that day, across a month's end."""
        self.write("habit run", "habit yoga", "2026-02-23 run", "2026-03-01 run", "2026-02-28 run",
                   "2026-02-22 run", "2026-03-02 run", "2026-02-25 yoga")
        self.assertEqual(self.ok("week", "--ending", "2026-03-01"),
                         ["2026-02-23 to 2026-03-01", "run  #....## 3/7", "yoga ..#.... 1/7"])
        self.assertEqual(self.ok("week", "--ending", "2025-01-02")[0], "2024-12-27 to 2025-01-02")

    def test_ending_errors(self):
        """T-003: a `--ending` that isn't a real date prints `error: bad date: <value>` (exit 2); one after today prints `error: date in the future: <date>` (exit 1)."""
        self.write("habit run")
        for bad in ("2026-02-29", "today", "2026-3-1"):
            self.fails(2, f"error: bad date: {bad}", "week", "--ending", bad)
        tomorrow = (date.today() + timedelta(days=1)).isoformat()
        self.fails(1, f"error: date in the future: {tomorrow}", "week", "--ending", tomorrow)

    def test_empty_and_broken(self):
        """T-003: with no habits or no file `week` prints `no habits yet`; a bad line gives T-001's error."""
        self.assertEqual(self.ok("week"), ["no habits yet"])
        self.write("habit run", "run today")
        self.fails(1, "error: habits.txt:2: bad line", "week")
