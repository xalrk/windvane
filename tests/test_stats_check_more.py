"""`windvane stats`' checks line counts a check that was cut off (Ctrl-C, an exception, or a kill that left only its
start row), averages over the tickets that ran a check (not those that only reused a pass), and, by ticket, names
the ticket whose checks took longest."""
import io
import os
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from windvane import cli, jobs  # noqa: E402
from windvane.config import Project  # noqa: E402
from windvane.util import append_jsonl, read_jsonl  # noqa: E402
from helpers import Sandbox  # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        self.s = Sandbox()
        self.addCleanup(self.s.close)
        self.p = Project(self.s.root)
        self.ledger = self.p.state / "ledger.jsonl"

    def add(self, **row):
        append_jsonl(self.ledger, row)

    def cli(self, *argv):
        out, here = io.StringIO(), os.getcwd()
        self.addCleanup(os.chdir, here)
        with redirect_stdout(out):
            cli.main(["-C", str(self.s.root), *argv])
        return out.getvalue()

    def lines(self, *argv, start=("checks:", "most check time:")):
        return [l for l in self.cli("stats", *argv).splitlines() if l.startswith(start)]


class Unfinished(Base):
    def test_a_check_interrupted_leaves_a_start_row_and_an_unfinished_end_row(self):
        seen, real = [], jobs.subprocess.run

        def interrupted(cmd, *args, **kw):
            if cmd[0] == "git":  # the tree key's git status
                return real(cmd, *args, **kw)
            seen.extend(read_jsonl(self.ledger))  # what a SIGKILL right now would leave
            raise KeyboardInterrupt

        with mock.patch.object(jobs.subprocess, "run", interrupted), self.assertRaises(KeyboardInterrupt):
            jobs.check(self.p, self.s.root, ticket="T-009")
        self.assertEqual([(r["event"], r["ticket"]) for r in seen], [("check-start", "T-009")])
        start, end = read_jsonl(self.ledger)
        self.assertEqual((end["event"], end["finished"], end["passed"], end["reused"]), ("check", False, False, False))
        self.assertEqual(end["start"], start["start"])
        self.assertIsInstance(end["minutes"], float)
        self.assertEqual(self.lines(), ["checks: 1 run, 0.0 min (0.0 min a ticket checked), 1 cut off"])

    def test_a_check_still_running_isnt_cut_off(self):
        seen, real = [], jobs.subprocess.run

        def slow(cmd, *args, **kw):
            if cmd[0] != "git":
                seen.extend(self.lines())  # `stats` while the check runs
            return real(cmd, *args, **kw)

        with mock.patch.object(jobs.subprocess, "run", slow):
            self.assertTrue(jobs.check(self.p, self.s.root, ticket="T-011")[0])
        self.assertEqual(seen, ["checks: 0 runs, 0.0 min, 1 still running"])
        self.assertEqual(self.lines(), ["checks: 1 run, 0.0 min (0.0 min a ticket checked)"])
        self.add(event="check-start", ticket="T-011", start=1.0, log=read_jsonl(self.ledger)[0]["log"])
        self.assertIn("1 cut off", self.lines()[0])  # its log is there but no longer locked: killed

    def test_a_finished_check_has_no_finished_flag_and_matches_its_start(self):
        os.environ["WINDVANE_TICKET"] = "T-010"
        self.addCleanup(os.environ.pop, "WINDVANE_TICKET", None)
        self.s.write("new", "x")  # dirty, so it runs
        self.assertTrue(jobs.check(self.p, self.s.root)[0])
        start, end = read_jsonl(self.ledger)
        self.assertEqual((start["event"], end["event"], end["passed"]), ("check-start", "check", True))
        self.assertEqual(start["start"], end["start"])
        self.assertNotIn("finished", end)
        self.assertEqual(self.lines(), ["checks: 1 run, 0.0 min (0.0 min a ticket checked)"])

    def test_a_start_row_with_no_end_counts_as_cut_off_without_minutes(self):
        self.add(event="check-start", ticket="T-001", start=100.0)
        self.add(event="check", ticket="T-001", start=100.0, minutes=4.0, passed=False, reused=False)
        self.add(event="check-start", ticket="T-001", start=200.0)  # killed: no end row
        self.add(event="check-start", ticket="T-002", start=100.0)  # same start, other ticket: killed too
        self.add(event="check", ticket="T-001", minutes=2.0, passed=True, reused=False)  # a row from before starts
        self.assertEqual(self.lines(), ["checks: 4 runs, 6.0 min (3.0 min a ticket checked), 1 failed, 2 cut off"])


class Average(Base):
    def test_a_ticket_with_only_a_reused_pass_is_left_out_of_the_average(self):
        self.add(event="check", ticket="T-001", minutes=6.0, passed=True, reused=False)
        self.add(event="check", ticket="T-002", minutes=0, passed=True, reused=True)
        self.assertEqual(self.lines(), ["checks: 1 run, 6.0 min (6.0 min a ticket checked), 1 reused a pass"])

    def test_only_reused_passes_say_no_average(self):
        self.add(event="check", ticket="T-002", minutes=0, passed=True, reused=True)
        self.assertEqual(self.lines(), ["checks: 0 runs, 0.0 min, 1 reused a pass"])


class Costliest(Base):
    SESSION = dict(role="worker", ticket="T-001", turns=3, input=10000, output=2000, usd=1.0, minutes=12)

    def add_checks(self):
        self.add(event="check", ticket="T-001", start=1.0, minutes=4.0, passed=True, reused=False)
        for start, mins in ((2.0, 9.0), (3.0, 12.5)):
            self.add(event="check-start", ticket="T-060", start=start)
            self.add(event="check", ticket="T-060", start=start, minutes=mins, passed=False, reused=False)
        self.add(event="check-start", ticket="T-060", start=4.0)  # cut off: a run, no minutes
        self.add(event="check", ticket="T-061", minutes=0, passed=True, reused=True)

    def test_by_ticket_names_the_ticket_whose_checks_took_longest(self):
        self.add(**self.SESSION)
        self.add_checks()
        self.assertEqual(self.lines("--by", "ticket"),
                         ["checks: 4 runs, 26 min (12.8 min a ticket checked), 2 failed, 1 cut off, 1 reused a pass",
                          "most check time: T-060, 3 runs, 22 min"])
        self.assertEqual(self.lines("--by", "role", start="most check time:"), [])  # by role: the checks line only

    def test_no_check_rows_no_costliest_line(self):
        self.add(**self.SESSION)
        self.assertEqual(self.lines("--by", "ticket"), [])
        self.add(event="check", ticket="T-061", minutes=0, passed=True, reused=True)  # nothing ran either
        self.assertEqual(self.lines("--by", "ticket"), ["checks: 0 runs, 0.0 min, 1 reused a pass"])

    def test_start_and_cut_off_rows_leave_the_session_totals_alone(self):
        self.add(**self.SESSION)
        before = self.cli("stats", "--by", "ticket")
        self.add_checks()
        self.add(event="check", ticket="T-001", start=5.0, minutes=1.0, passed=False, reused=False, finished=False)
        after = self.cli("stats", "--by", "ticket")
        self.assertEqual([l for l in after.splitlines() if not l.startswith(("checks:", "most check time:"))],
                         before.splitlines())


if __name__ == "__main__":
    unittest.main()
