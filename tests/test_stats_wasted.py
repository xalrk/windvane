"""`windvane stats` counts worker launches that stopped at once (PARTIAL or BLOCKED within a few turns) and what they
cost, from the `result` each session's ledger row now keeps."""
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

# Three launches that stopped at once (one on the cut's edge, two for one ticket) and three that worked: a READY, a
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
        # 30k of the workers' 200k weighted; fresh in 8k + 4k + 9k + 5k, out 400 + 200
        self.assertIn(f"idle worker launches: 3 (PARTIAL or BLOCKED within {stats.IDLE_TURNS} turns), 26k fresh in, "
                      "1k out, 15% of worker weighted tokens; most: T-007 ×2, T-009 (autopilot.log says why)", out)
        self.assertEqual(out.count("idle worker launches"), 1)

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
