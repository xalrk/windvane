"""`windvane stats` shows what the check costs: every check run for a ticket (the driver's, or a session's
`windvane check`) is a ledger row, summed on one line after the table without touching the token totals; a zero
died/killed cell is `·`; and the `--wide` note says how wide the wide table is."""
import io
import json
import os
import sys
import unittest
from argparse import Namespace
from contextlib import redirect_stdout
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from windvane import cli, driver, jobs  # noqa: E402
from windvane.config import Project  # noqa: E402
from windvane.util import append_jsonl, now, read_jsonl  # noqa: E402
from helpers import Sandbox  # noqa: E402

SESSIONS = [("worker", "T-001", dict(input=10000, output=2000, usd=1.0, minutes=12)),
            ("worker", "T-002", dict(input=4000, usd=0.2, minutes=3, end="error", error="API Error: overloaded")),
            ("planner", "", dict(input=6000, usd=0.3, minutes=2))]
CHECKS = [("T-001", 4.0, False, False), ("T-001", 5.0, True, False), ("T-001", 0, True, True),
          ("T-002", 6.0, True, False)]


class CheckRows(unittest.TestCase):
    def setUp(self):
        self.s = Sandbox()
        self.addCleanup(self.s.close)
        self.p = Project(self.s.root)

    def checks(self):
        return [r for r in read_jsonl(self.p.state / "ledger.jsonl") if r.get("event") == "check"]

    def test_a_ticket_checked_twice_leaves_two_rows_and_a_reused_pass(self):
        # red until the worker has built twice: the first check fails and bounces, the second passes
        self.s.write(".windvane/config.toml", (self.s.root / ".windvane/config.toml").read_text().replace(
            'check = "test -f README"', 'check = "test $(cat T-001.txt 2>/dev/null | wc -l) -ge 2"'))
        self.s.ticket("T-001")
        self.s.publish()
        p = Project(self.s.root)
        pilot = driver.Pilot(p, Namespace(cap=75, week_cap=95, ticket_hours=1, min_slot=0, answer_wait=10, replan=0,
                                          goal="", parallel=1))
        pilot.run(now() + timedelta(hours=1))
        self.assertEqual(pilot.merged, ["T-001"])
        rows = self.checks()
        ran = [r for r in rows if not r["reused"]]
        self.assertEqual([(r["ticket"], r["passed"]) for r in ran], [("T-001", False), ("T-001", True)])
        self.assertTrue(all(isinstance(r["minutes"], float) for r in ran))
        # merged with an unmoved main, the tree is the branch's: its pass is reused, and recorded as such
        self.assertEqual([(r["ticket"], r["passed"]) for r in rows if r["reused"]], [("T-001", True)])
        calls = [json.loads(l) for l in (self.s.tmp / "calls.jsonl").read_text().splitlines()]
        self.assertEqual({c["ticket_env"] for c in calls if c["system"].startswith("# windvane worker")}, {"T-001"})

    def test_a_sessions_check_counts_for_its_ticket_and_a_desk_check_doesnt(self):
        jobs.check(self.p, self.s.root)  # at the desk: no ticket
        self.assertEqual(self.checks(), [])
        os.environ["WINDVANE_TICKET"] = "T-007"  # what run_claude gives a worker session
        self.addCleanup(os.environ.pop, "WINDVANE_TICKET", None)
        self.assertIn("reused", jobs.check(self.p, self.s.root)[1])
        self.s.write("new", "x")
        self.assertIn("not recorded", jobs.check(self.p, self.s.root)[1])  # dirty: runs, counted all the same
        self.assertEqual([(r["ticket"], r["passed"], r["reused"]) for r in self.checks()],
                         [("T-007", True, True), ("T-007", True, False)])


class CheckStats(unittest.TestCase):
    def setUp(self):
        self.s = Sandbox()
        self.addCleanup(self.s.close)
        self.p = Project(self.s.root)
        for role, t, kw in SESSIONS:
            append_jsonl(self.p.state / "ledger.jsonl", dict(role=role, ticket=t, turns=3, **kw))

    def cli(self, *argv):
        out, here = io.StringIO(), os.getcwd()
        self.addCleanup(os.chdir, here)
        with redirect_stdout(out):
            cli.main(["-C", str(self.s.root), *argv])
        return out.getvalue()

    def add_checks(self):
        for t, mins, passed, reused in CHECKS:
            append_jsonl(self.p.state / "ledger.jsonl", dict(event="check", ticket=t, minutes=mins, passed=passed,
                                                             reused=reused))

    def test_the_checks_line_sums_runs_minutes_failures_and_reuses(self):
        self.assertNotIn("checks:", self.cli("stats"))
        self.add_checks()
        line = "checks: 3 runs, 15 min (7.5 min a ticket checked), 1 failed, 1 reused a pass\n"
        self.assertIn(line, self.cli("stats"))
        self.assertIn(line, self.cli("stats", "--by", "ticket"))

    def test_check_rows_leave_the_session_totals_alone(self):
        before = self.cli("stats", "--by", "ticket")
        self.add_checks()
        after = self.cli("stats", "--by", "ticket")
        skip = ("checks:", "most check time:", "costliest wasted check:")
        self.assertEqual([l for l in after.splitlines() if not l.startswith(skip)],
                         before.splitlines())

    def test_a_zero_died_killed_cell_is_a_dot(self):
        rows = {l.split()[0]: l.split() for l in self.cli("stats", "--by", "ticket").splitlines()[1:]
                if l.startswith(("T-", "planner"))}
        self.assertEqual(rows["T-001"][3], "·")
        self.assertEqual(rows["T-002"][3], "1")

    def test_the_wide_note_names_the_wide_tables_width(self):
        for by in ("role", "ticket"):
            wide = self.cli("stats", "--by", by, "--wide").splitlines()[0]
            note = [l for l in self.cli("stats", "--by", by).splitlines() if l.startswith("(--wide")]
            self.assertEqual(note, [f"(--wide, {len(wide)} columns, adds base and cached/turn: what a run starts with "
                                    "and each turn re-reads.)"])  # one line: it fits the default table's 100
            self.assertGreater(len(wide), 100)


if __name__ == "__main__":
    unittest.main()
