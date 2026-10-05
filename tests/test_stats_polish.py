"""`windvane stats` names only the kinds of lost run that happened ("1 run (died)", not "1 died, 0 killed for time"),
and its `--wide` help says the table then goes past 100 columns."""
import argparse
import io
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from windvane import cli  # noqa: E402
from windvane.config import Project  # noqa: E402
from windvane.util import append_jsonl, now  # noqa: E402
from helpers import Sandbox  # noqa: E402

DIED = dict(input=4000, end="error", error="API Error: overloaded")
KILLED = dict(input=8000, killed="time", end="killed")
FINE = dict(input=10000, output=2000, usd=1.0)


class StatsPolish(unittest.TestCase):
    def setUp(self):
        self.s = Sandbox()
        self.addCleanup(self.s.close)
        self.p = Project(self.s.root)

    def stats(self, *runs):
        for kw in runs:
            append_jsonl(self.p.state / "ledger.jsonl",
                         dict(role="worker", ticket="T-001", turns=3, at=f"{now():%Y-%m-%d %H:%M}", **kw))
        out = io.StringIO()
        with redirect_stdout(out):
            cli.cmd_stats(argparse.Namespace(days=7, by="role", critic=False, wide=False), self.p)
        return next((l for l in out.getvalue().splitlines() if l.startswith("died or killed")), "")

    def test_only_deaths_name_no_zero_kills(self):
        line = self.stats(FINE, DIED)
        self.assertTrue(line.startswith("died or killed: 1 run (died), 4k weighted tokens"), line)
        self.assertNotIn("0 killed", line)

    def test_only_kills_name_no_zero_deaths(self):
        line = self.stats(FINE, KILLED, KILLED)
        self.assertTrue(line.startswith("died or killed: 2 runs (killed for time), 16k"), line)
        self.assertNotIn("0 died", line)

    def test_both_kinds_are_counted(self):
        line = self.stats(FINE, DIED, DIED, KILLED)
        self.assertTrue(line.startswith("died or killed: 3 runs (2 died, 1 killed for time), "), line)

    def test_no_lost_runs_no_line(self):
        self.assertEqual(self.stats(FINE), "")

    def test_wide_help_says_it_passes_100_columns(self):
        out = io.StringIO()
        with redirect_stdout(out), self.assertRaises(SystemExit):
            cli.main(["-C", str(self.s.root), "stats", "--help"])
        wide = " ".join(out.getvalue().split()).split("--wide", 1)[1]
        self.assertIn("wider than 100 columns", wide)


if __name__ == "__main__":
    unittest.main()
