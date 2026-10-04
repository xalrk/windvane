"""`windvane stats` shows what a merged ticket really costs: a cost-weighted token figure (cache reads included), `?`
for an unknown API $, the runs that died, overhead spread over merged tickets, failed tickets, and `--critic --since`."""
import io
import os
import subprocess
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from windvane import cli  # noqa: E402
from windvane.config import Project  # noqa: E402
from windvane.util import append_jsonl, log  # noqa: E402
from helpers import Sandbox  # noqa: E402

# (role, ticket, fields): weighted = input + 1.25 cache_write + 0.1 cache_read + 5 output
LEDGER = [
    ("worker", "T-001", dict(input=10000, cache_read=100000, output=2000, usd=1.0)),  # 30k
    ("worker", "T-001", dict(input=4000, usd=0.2, end="error", error="API Error: overloaded")),  # 4k, died
    ("worker", "T-002", dict(input=5000, output=1000, usd=None, estimated=True, killed=True, end="killed")),  # 10k
    ("worker", "T-003", dict(input=20000, usd=5.0)),  # 20k, the dearest in API $ (a pricier model)
    ("worker", "T-004", dict(input=1000, usd=0.01)),  # 1k
    ("planner", "", dict(input=6000, cache_write=4000, usd=0.3)),  # 11k
]


class StatsCost(unittest.TestCase):
    def setUp(self):
        s = self.s = Sandbox()
        self.addCleanup(s.close)
        for t in ("T-001", "T-002", "T-003", "T-004"):
            s.ticket(t)
        s.publish()
        s.git("switch", "-q", "-c", "t-001-x")
        s.write("x", "1")
        s.git("add", "-A")
        s.git("commit", "-qm", "x")
        s.git("switch", "-q", "main")
        s.git("merge", "-q", "--no-ff", "-m", "Merge T-001: x (branch t-001-x)", "t-001-x")
        s.git("push", "-q", "origin", "main")
        self.p = Project(s.root)
        for role, t, kw in LEDGER:
            append_jsonl(self.p.state / "ledger.jsonl", dict(role=role, ticket=t, turns=3, **kw))
        for line in ("T-003: worker run (todo)", "T-003: FAILED: tests fail", "T-004: worker run (todo)",
                     "T-004: FAILED: no RESULT line", "T-004: worker run (todo)"):  # T-004 was tried again
            log(self.p, line)

    def cli(self, *argv):
        out, here = io.StringIO(), os.getcwd()
        self.addCleanup(os.chdir, here)
        with redirect_stdout(out):
            try:
                cli.main(["-C", str(self.s.root), *argv])
            except SystemExit as e:
                return f"{out.getvalue()}{e.code}"
        return out.getvalue()

    def rows(self, out):
        return {l.split()[0]: l.split() for l in out.splitlines()[1:] if l.startswith(("T-", "planner", "worker"))}

    def test_weights_price_each_kind_of_token(self):
        self.assertEqual(cli.weighted(dict(input=100, cache_write=100, cache_read=1000, output=10)),
                         100 + 125 + 100 + 50)

    def test_by_ticket_sorts_by_weighted_cost_and_shows_outcomes(self):
        out = self.cli("stats", "--by", "ticket")
        head = out.splitlines()[0].split()
        self.assertEqual(head[:4], ["ticket", "outcome", "runs", "died"])
        self.assertIn("weighted", head)
        order = [l.split()[0] for l in out.splitlines()[1:6]]
        self.assertEqual(order, ["T-001", "T-003", "planner", "T-002", "T-004"])  # not by API $: T-003 would lead
        r = self.rows(out)
        w = -5  # weighted, API $, min, base, cached/turn end each row
        self.assertEqual([r[t][w] for t in order], ["34k", "20k", "11k", "10k", "1k"])
        self.assertEqual({t: r[t][1] for t in order}, {"T-001": "merged", "T-003": "failed", "planner": "-",
                                                      "T-002": "open", "T-004": "open"})
        self.assertEqual(r["T-001"][3], "1")  # died
        self.assertEqual(r["T-002"][w + 1], "?")  # killed-only: no API $ at all, not ≥0.00
        self.assertEqual(r["T-001"][w + 1], "1.20")
        self.assertNotIn("≥0.00", out)

    def test_by_ticket_summary_lines(self):
        out = self.cli("stats", "--by", "ticket")
        self.assertIn("per merged ticket (1): 2.0 runs, 34k weighted tokens, 1.20 API $\n", out)
        self.assertIn("with overhead (planner, preflight, periodic): 45k per merged ticket\n", out)
        self.assertIn("spent on tickets not merged (open, parked, failed): 31k weighted tokens, 48% of ticket", out)
        self.assertIn("died or retried: 1 runs, 4k weighted tokens, 5% of all\n", out)

    def test_by_role_shows_the_died_column(self):
        out = self.cli("stats")
        r = self.rows(out)
        self.assertEqual(r["worker"][1:3], ["5", "1"])  # runs, died
        self.assertEqual(r["planner"][1:3], ["1", "0"])
        self.assertIn("died or retried: 1 runs", out)

    def critic_fixture(self):
        for t, at, score in (("T-001", "2026-09-01 10:00", 6), ("T-002", "2026-09-20 10:00", 9)):
            append_jsonl(self.p.state / "critic.jsonl", {"ticket": t, "score": score, "step": "pass", "at": at})

    def test_critic_since_a_date(self):
        self.critic_fixture()
        self.assertIn("2 tickets", self.cli("stats", "--critic"))
        out = self.cli("stats", "--critic", "--since", "2026-09-10")
        self.assertIn("critic: last 1 tickets, 1 verdicts since 2026-09-10,", out)
        self.assertIn("T-002", out)
        self.assertNotIn("T-001", out)
        self.assertIn("no critic verdicts since 2026-10-01", self.cli("stats", "--critic", "--since", "2026-10-01"))

    def test_critic_since_a_commit_uses_its_commit_time(self):
        self.critic_fixture()
        self.s.write("CALIBRATION.md", "stricter\n")
        self.s.git("add", "-A")
        env = {**os.environ, "GIT_COMMITTER_DATE": "2026-09-10T12:00:00"}
        subprocess.run(["git", "commit", "-qm", "calibrate"], cwd=self.s.root, env=env, check=True)
        sha = self.s.git("rev-parse", "--short", "HEAD")
        out = self.cli("stats", "--critic", "--since", sha)
        self.assertIn("1 verdicts since 2026-09-10 12:00,", out)
        self.assertNotIn("T-001", out)
        self.assertIn("neither a date", self.cli("stats", "--critic", "--since", "nosuchcommit"))


if __name__ == "__main__":
    unittest.main()
