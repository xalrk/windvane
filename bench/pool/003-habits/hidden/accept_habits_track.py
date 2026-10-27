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


class Track(Base):
    def test_add(self):
        """T-001: `add read` adds the line `habit read` to `habits.txt` (making the file when there is none) and prints `added read`."""
        self.assertEqual(self.ok("add", "read"), ["added read"])
        self.assertEqual(self.text(), "habit read\n")
        self.assertEqual(self.ok("add", "no-sugar"), ["added no-sugar"])
        self.assertEqual(self.ok("add", "run5k"), ["added run5k"])
        self.assertEqual(self.text(), "habit read\nhabit no-sugar\nhabit run5k\n")

    def test_add_errors(self):
        """T-001: a bad habit name prints `error: bad habit name: <name>` and exits 2; a habit already in the file prints `error: habit exists: <name>` and exits 1, the file unchanged."""
        for bad in ("Read", "5k", "no sugar", "run!"):
            self.fails(2, f"error: bad habit name: {bad}", "add", bad)
        self.assertIsNone(self.text())
        self.write("# my habits", "habit read")
        self.fails(1, "error: habit exists: read", "add", "read")
        self.assertEqual(self.text(), "# my habits\nhabit read\n")

    def test_done(self):
        """T-001: `done read` marks today and prints `read: done on <today>`; `--on` marks that day instead; a day already done prints `read: already done on <date>`, exits 0 and adds nothing."""
        self.write("habit read")
        self.assertEqual(self.ok("done", "read"), [f"read: done on {ago(0)}"])
        self.assertEqual(self.ok("done", "read", "--on", "2026-10-01"), ["read: done on 2026-10-01"])
        before = self.text()
        self.assertEqual(self.ok("done", "read", "--on", "2026-10-01"), ["read: already done on 2026-10-01"])
        self.assertEqual(self.ok("done", "read"), [f"read: already done on {ago(0)}"])
        self.assertEqual(self.text(), before)
        self.assertEqual(self.ok("list"), ["- read: 2 days"])

    def test_done_errors(self):
        """T-001: an unknown habit prints `error: no such habit: <name>` (exit 1); a `--on` that isn't a real `YYYY-MM-DD` date prints `error: bad date: <value>` (exit 2); a date after today prints `error: date in the future: <date>` (exit 1); none changes the file."""
        self.write("habit read")
        self.fails(1, "error: no such habit: run", "done", "run")
        for bad in ("2026-13-01", "2026-02-30", "yesterday", "2025-02-29"):
            self.fails(2, f"error: bad date: {bad}", "done", "read", "--on", bad)
        tomorrow = (date.today() + timedelta(days=1)).isoformat()
        self.fails(1, f"error: date in the future: {tomorrow}", "done", "read", "--on", tomorrow)
        self.assertEqual(self.text(), "habit read\n")

    def test_list(self):
        """T-001: `list` prints `- <name>: <N> days` per habit in the order added, N the number of different days (`1 day`, `0 days`); with no habits or no file it prints `no habits yet`."""
        self.assertEqual(self.ok("list"), ["no habits yet"])
        self.write("# nothing yet", "")
        self.assertEqual(self.ok("list"), ["no habits yet"])
        self.write("habit walk", "2026-01-01 walk", "habit read", "", "2026-01-01 read", "2026-01-03 walk",
                   "habit tea", "2026-01-05 walk", "2026-01-03 walk")
        self.assertEqual(self.ok("list"), ["- walk: 3 days", "- read: 1 day", "- tea: 0 days"])

    def test_bad_line(self):
        """T-001: a line that isn't blank, a comment, a good new `habit <name>` or `<date> <name>` of a declared habit makes every command print `error: habits.txt:<line>: bad line` and exit 1, counting every line from 1, writing nothing."""
        for bad in ("hello", "habit Read", "habit read", "2026-02-30 read", "2026-01-01 run", "26-01-01 read"):
            self.write("habit read", "", "# a note", bad, "2026-01-01 read")
            before = self.text()
            for args in (("list",), ("add", "tea"), ("done", "read")):
                self.fails(1, "error: habits.txt:4: bad line", *args)
            self.assertEqual(self.text(), before, bad)

    def test_appends_only(self):
        """T-001 / CLAUDE.md: commands only add lines at the end of `habits.txt`, keeping a user's comments and blank lines."""
        self.write("# morning", "habit read", "", "2026-01-01 read")
        self.ok("add", "tea")
        self.ok("done", "tea", "--on", "2026-01-02")
        self.assertEqual(self.text(), "# morning\nhabit read\n\n2026-01-01 read\nhabit tea\n2026-01-02 tea\n")
