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


class Target(Base):
    def test_add(self):
        """T-004: `add gym --per-week 3` adds the line `habit gym 3/week` and prints `added gym (3 a week)`; a `--per-week` not a whole number from 1 to 7 prints `error: bad target: <value>` and exits 2."""
        self.write("habit read")
        self.assertEqual(self.ok("add", "gym", "--per-week", "3"), ["added gym (3 a week)"])
        self.assertEqual(self.ok("add", "swim", "--per-week", "7"), ["added swim (7 a week)"])
        self.assertEqual(self.text(), "habit read\nhabit gym 3/week\nhabit swim 7/week\n")
        for bad in ("0", "8", "two", "2.5", "-1"):
            self.fails(2, f"error: bad target: {bad}", "add", "yoga", "--per-week", bad)
        self.assertEqual(self.text(), "habit read\nhabit gym 3/week\nhabit swim 7/week\n")

    def test_list(self):
        """T-004: `list` prints a weekly habit as `- gym: <N> days (3 a week)`, and a hand-written `habit gym 3/week` is read the same way; a target outside 1 to 7 is a bad line."""
        self.write("habit read", "habit gym 3/week", "2026-01-05 gym", "2026-01-06 gym", "2026-01-05 read")
        self.assertEqual(self.ok("list"), ["- read: 1 day", "- gym: 2 days (3 a week)"])
        for bad in ("habit gym 0/week", "habit gym 8/week"):
            self.write("habit read", bad)
            self.fails(1, "error: habits.txt:2: bad line", "list")

    def test_streak(self):
        """T-004: a weekly habit's week counts when at least N of its Monday-to-Sunday days are done; the current run ends this week, or last week when this week hasn't reached N; printed `gym: <N> weeks, best <M>`."""
        lines = ["habit read", "habit gym 2/week", "habit tea 1/week", "habit swim 3/week", f"{ago(0)} read"]
        for w in (1, 2, 4, 5, 6):
            lines += [f"{monday(w) + timedelta(days=5)} gym", f"{monday(w) + timedelta(days=6)} gym"]
        lines += [f"{monday(3) + timedelta(days=2)} gym"]
        lines += [f"{date.today()} tea", f"{monday(1) + timedelta(days=6)} tea", f"{monday(2)} tea"]
        lines += [f"{monday(1) + timedelta(days=d)} swim" for d in range(7)]
        self.write(*lines)
        self.assertEqual(self.ok("streak"), ["read: 1 day, best 1", "gym: 2 weeks, best 3", "tea: 3 weeks, best 3",
                                             "swim: 1 week, best 1"])
        self.assertEqual(self.ok("streak", "gym"), ["gym: 2 weeks, best 3"])

    def test_streak_none(self):
        """T-004: a weekly habit with no counted week before this one prints `0 weeks`; runs are counted across year ends."""
        self.write("habit gym 1/week", "2025-12-24 gym", "2025-12-31 gym", "2026-01-05 gym", "2026-01-14 gym",
                   f"{monday(2)} gym")
        self.assertEqual(self.ok("streak"), ["gym: 0 weeks, best 4"])
