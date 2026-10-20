"""`windvane stats` counts worker launches that stopped at once (PARTIAL within a few turns) and what they cost, the
quick questions (BLOCKED) apart, from the `result` each session's ledger row now keeps."""
import io
import os
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from windvane import cli, runner, stats  # noqa: E402
from windvane.config import Project  # noqa: E402
from windvane.util import append_jsonl, read_jsonl  # noqa: E402
from helpers import Sandbox  # noqa: E402

# Two launches that stopped at once (both for one ticket), a quick question on the cut's edge, and three that worked: a READY, a
# PARTIAL one turn past the cut, a short READY. Weighted = input + 1.25 cache_write + 5 output
IDLE = [dict(ticket="T-007", turns=3, input=8000, cache_write=4000, output=400, result="PARTIAL"),  # 15k
        dict(ticket="T-009", turns=stats.IDLE_TURNS, input=9000, output=200, result="BLOCKED"),  # 10k
        dict(ticket="T-007", turns=2, input=5000, output=0, result="PARTIAL")]  # 5k
WORKED = [dict(turns=40, input=50000, output=10000, result="READY"),  # 100k
          dict(turns=stats.IDLE_TURNS + 1, input=30000, output=4000, result="PARTIAL"),  # 50k
          dict(turns=4, input=10000, output=2000, result="READY")]  # 20k


class Wasted(unittest.TestCase):
    def setUp(self):
        s = self.s = Sandbox()
        self.addCleanup(s.close)
        self.p = Project(s.root)

    def ledger(self, rows):
        for i, kw in enumerate(rows):
            append_jsonl(self.p.state / "ledger.jsonl", {"role": "worker", "ticket": f"T-00{i}", **kw})
        append_jsonl(self.p.state / "ledger.jsonl", dict(role="planner", turns=2, input=7000, result=None))

    def stats(self):
        out, here = io.StringIO(), os.getcwd()
        self.addCleanup(os.chdir, here)
        with redirect_stdout(out):
            cli.main(["-C", str(self.s.root), "stats"])
        return out.getvalue()

    def test_idle_launches_are_counted_with_their_tokens_and_share(self):
        self.ledger(IDLE + WORKED)
        out = " ".join(self.stats().split())  # the line wraps at 100 columns
        # 20k of the workers' 200k weighted; fresh in 8k + 4k + 5k, out 400; the BLOCKED only counted apart
        self.assertIn(f"idle worker launches: 2 (PARTIAL within {stats.IDLE_TURNS} turns), 17k fresh in, <1k out, "
                      "10% of worker weighted tokens; most: T-007 ×2. Not counted: 1 quick question (BLOCKED)", out)
        self.assertEqual(out.count("idle worker launches"), 1)

    def test_the_line_names_the_grep_that_finds_the_top_tickets_partials(self):
        self.ledger(IDLE + WORKED)
        log = self.p.state / "autopilot.log"
        log.write_text("01:00 T-007: partial (1): started past its wrap-up\n")
        why = [ln for ln in self.stats().splitlines() if ln.startswith(" why: ")]
        self.assertEqual(why, [f' why: grep "T-007: partial" {stats.shell_path(log)}'])

    def test_the_logs_path_is_a_shell_word(self):
        with mock.patch.object(Path, "home", return_value=Path("/home/u")):
            self.assertEqual(stats.shell_path(Path("/home/u/.local/state/p/autopilot.log")),
                             "~/.local/state/p/autopilot.log")
            self.assertEqual(stats.shell_path(Path("/home/u/my state/autopilot.log")), "'/home/u/my state/autopilot.log'")
            self.assertEqual(stats.shell_path(Path("/srv/state/autopilot.log")), "/srv/state/autopilot.log")

    def test_no_grep_for_launches_with_no_ticket(self):
        self.ledger([{**r, "ticket": None} for r in IDLE[::2]] + WORKED)
        out = self.stats()
        self.assertIn("most: no ticket ×2", " ".join(out.split()))
        self.assertNotIn(" why: grep", out)

    def test_the_grep_matches_the_drivers_partial_line(self):
        """The command finds a line shaped like the one the driver logs when a worker ends PARTIAL."""
        from windvane.util import log
        log(self.p, "T-007: partial (1): started past its wrap-up", echo=False)
        self.ledger(IDLE)
        cmd = next(ln for ln in self.stats().splitlines() if ln.startswith(" why: "))
        pattern = cmd.split('"')[1]
        self.assertTrue(any(pattern in ln for ln in (self.p.state / "autopilot.log").read_text().splitlines()))

    def test_a_quick_blocked_alone_is_no_idle_launch(self):
        self.ledger([IDLE[1]] + WORKED)
        out = self.stats()
        self.assertNotIn("idle worker launches", out)
        self.assertNotIn(" why: grep", out)

    def test_a_quick_partial_alone_counts_with_no_questions_count(self):
        self.ledger([IDLE[2]] + WORKED)
        out = " ".join(self.stats().split())
        self.assertIn(f"idle worker launches: 1 (PARTIAL within {stats.IDLE_TURNS} turns), 5k fresh in", out)
        self.assertIn("most: T-007 ", out)
        self.assertNotIn("quick question", out)

    def test_no_line_without_idle_launches(self):
        self.ledger(WORKED)
        out = self.stats()
        self.assertIn("worker", out)
        self.assertNotIn("idle worker launches", out)

    def test_an_old_ledger_without_result_reads(self):
        self.ledger([{k: v for k, v in r.items() if k != "result"} for r in IDLE + WORKED])
        out = self.stats()
        self.assertIn("worker", out)
        self.assertNotIn("idle worker launches", out)


class LedgerResult(unittest.TestCase):
    def setUp(self):
        self.s = Sandbox()
        self.addCleanup(self.s.close)
        self.p = Project(self.s.root)

    def run_worker(self, behaviour):
        with mock.patch.dict(os.environ, FAKE_WORKER=behaviour):
            runner.run_claude(self.p, "worker", "T-001", "Ticket T-001. Go.", "# windvane worker run", self.s.root)
        return read_jsonl(self.p.state / "ledger.jsonl")[-1]

    def test_a_partial_worker_keeps_its_result(self):
        self.assertEqual(self.run_worker("partial")["result"], "PARTIAL")

    def test_a_session_with_no_result_line_keeps_none(self):
        runner.run_claude(self.p, "planner", "plan", "Plan.", "# windvane planner", self.s.root)
        self.assertIsNone(read_jsonl(self.p.state / "ledger.jsonl")[-1]["result"])

    def test_the_last_result_line_counts(self):
        self.assertEqual(runner.result_word("RESULT: READY T-1 abc\nthen\nRESULT: BLOCKED T-1 which?"), "BLOCKED")
        self.assertIsNone(runner.result_word("no result line"))


if __name__ == "__main__":
    unittest.main()
