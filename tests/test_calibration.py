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
from windvane.util import append_jsonl  # noqa: E402
from helpers import Sandbox  # noqa: E402

# (ticket, score, step), oldest first: T-001 revised up to the bar, T-002 passed at once above it, T-003 ran out of
# revisions, T-004 wasn't user-facing, a plan marker, and T-005 critiqued again by a later run.
FIXTURE = [("T-001", 4, "rethink"), ("T-001", 6, "revise"), ("T-001", 8, "pass"), ("T-002", 9, "pass"),
           ("T-003", 5, "revise"), ("T-003", 6, "revise"), ("T-003", 7, "accept"), ("T-004", None, "skip"),
           ("PLAN", None, "plan"), ("T-005", 8, "pass"), ("T-005", 8, "pass")]


class Calibration(unittest.TestCase):
    def setUp(self):
        s = Sandbox("pass = 8\n")
        self.addCleanup(s.close)
        self.p = Project(s.root)
        for t, score, step in FIXTURE:
            append_jsonl(self.p.state / "critic.jsonl", {"ticket": t, "score": score, "step": step, "fixes": 0})

    def run_cli(self, fn, **kw):
        out = io.StringIO()
        with redirect_stdout(out):
            fn(argparse.Namespace(**kw), self.p)
        return out.getvalue()

    def test_stats_critic_prints_the_spread(self):
        out = self.run_cli(cli.cmd_stats, critic=True, last=10).splitlines()
        self.assertEqual(out[:6], [
            "critic: last 4 tickets, 9 verdicts, pass mark 8/10",
            "first scores: 4-9 (median 6.5)",
            "final scores: 7-9 (median 8)",
            "needed revisions: 2 of 4",
            "exactly on the pass mark: 2 of 4 final scores, 3 of 9 verdicts",
            "passed only by running out of revisions (accept): 1 of 4",
        ])
        self.assertRegex(out[6], r"^  T-001 +4 → 6 → 8 +pass$")
        self.assertRegex(out[-1], r"^  T-005 +8 → 8 +pass$")

    def test_last_n_keeps_the_latest_tickets(self):
        out = self.run_cli(cli.cmd_stats, critic=True, last=2)
        self.assertIn("critic: last 2 tickets, 5 verdicts", out)
        self.assertIn("first scores: 5-8 (median 6.5)", out)
        self.assertNotIn("T-001", out)

    def test_last_below_one_is_refused(self):
        with self.assertRaises(SystemExit):
            self.run_cli(cli.cmd_stats, critic=True, last=0)

    def test_overview_line(self):
        out = self.run_cli(cli.cmd_overview)
        self.assertIn("Critic, last 4 tickets: first scores 4-9 (median 6.5); 2 of 4 ended exactly on the pass mark "
                      "(8). 1 passed only by running out of revisions.", out)

    def test_no_verdicts(self):
        (self.p.state / "critic.jsonl").write_text('{"ticket": "T-004", "score": null, "step": "skip"}\n')
        self.assertEqual(self.run_cli(cli.cmd_stats, critic=True, last=10), "no critic verdicts yet\n")
        self.assertNotIn("Critic", self.run_cli(cli.cmd_overview))


if __name__ == "__main__":
    unittest.main()
