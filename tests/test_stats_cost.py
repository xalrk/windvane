"""`windvane stats` shows what a merged ticket really costs: a cost-weighted token figure (cache reads included), `?`
for an unknown API $, the runs that died or were killed for time, overhead spread over merged tickets and named, failed
tickets from the ledger's outcome rows, swept orphans, a table that fits 100 columns, and `--critic --since`."""
import io
import os
import subprocess
import sys
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest import mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from windvane import cli, driver, stats  # noqa: E402
from windvane.config import Project  # noqa: E402
from windvane.util import append_jsonl, log, read_jsonl  # noqa: E402
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
        self.assertEqual(stats.weighted(dict(input=100, cache_write=100, cache_read=1000, output=10)),
                         100 + 125 + 100 + 50)

    def test_by_ticket_sorts_by_weighted_cost_and_shows_outcomes(self):
        out = self.cli("stats", "--by", "ticket")
        head = out.splitlines()[0].split()
        self.assertEqual(head[:4], ["ticket", "outcome", "runs", "died/killed"])
        self.assertIn("weighted", head)
        self.assertNotIn("base", head)  # behind --wide
        order = [l.split()[0] for l in out.splitlines()[1:6]]
        self.assertEqual(order, ["T-001", "T-003", "planner", "T-002", "T-004"])  # not by API $: T-003 would lead
        r = self.rows(out)
        w = -3  # weighted, API $, min end each row
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
        self.assertIn("with overhead: 45k weighted tokens per merged ticket\n  overhead: planner: 11k\n", out)
        self.assertIn("spent on tickets not merged (open, parked, failed): 31k weighted tokens, 48% of ticket", out)
        self.assertIn("died or killed: 1 run (died), 4k weighted tokens, 5% of all\n", out)

    def test_by_role_shows_the_died_column(self):
        out = self.cli("stats")
        r = self.rows(out)
        self.assertEqual(r["worker"][1:3], ["5", "1"])  # runs, died
        self.assertEqual(r["planner"][1:3], ["1", "·"])
        self.assertIn("died or killed: 1 run (", out)

    def critic_fixture(self):
        for t, at, score in (("T-001", "2026-09-01 10:00", 6), ("T-002", "2026-09-20 10:00", 9)):
            append_jsonl(self.p.state / "critic.jsonl", {"ticket": t, "score": score, "step": "pass", "at": at})

    def test_critic_since_a_date(self):
        self.critic_fixture()
        self.assertIn("2 tickets", self.cli("stats", "--critic"))
        out = self.cli("stats", "--critic", "--since", "2026-09-10")
        self.assertIn("critic: last 1 ticket, 1 verdict since 2026-09-10,", out)
        self.assertIn("T-002", out)
        self.assertNotIn("T-001", out)
        self.assertIn("no critic verdicts since 2026-10-01", self.cli("stats", "--critic", "--since", "2026-10-01"))

    def test_critic_since_a_commit_uses_its_author_time(self):
        """A rebase moves the commit time past verdicts given after the change was written: they still count."""
        self.critic_fixture()
        self.s.write("CALIBRATION.md", "stricter\n")
        self.s.git("add", "-A")
        env = {**os.environ, "GIT_AUTHOR_DATE": "2026-09-10T12:00:00", "GIT_COMMITTER_DATE": "2026-09-25T12:00:00"}
        subprocess.run(["git", "commit", "-qm", "calibrate"], cwd=self.s.root, env=env, check=True)
        sha = self.s.git("rev-parse", "--short", "HEAD")
        out = self.cli("stats", "--critic", "--since", sha)
        self.assertIn("1 verdict since 2026-09-10 12:00,", out)
        self.assertIn("T-002", out)  # 2026-09-20: between the author and the commit time
        self.assertNotIn("T-001", out)
        self.assertIn("neither a date", self.cli("stats", "--critic", "--since", "nosuchcommit"))

    def test_a_run_killed_for_time_and_retried_counts_as_lost(self):
        ledger = self.p.state / "ledger.jsonl"
        append_jsonl(ledger, dict(role="worker", ticket="T-002", input=8000, usd=0.1, killed="time", end="killed"))
        append_jsonl(ledger, dict(role="worker", ticket="T-002", input=1000, usd=0.1, killed="stop now", end="killed"))
        append_jsonl(ledger, dict(role="worker", ticket="T-002", input=1000, usd=0.1))  # the retry
        out = self.cli("stats", "--by", "ticket")
        self.assertEqual(self.rows(out)["T-002"][2:4], ["4", "1"])  # runs, died/killed: not the deliberate stop
        self.assertIn("died or killed: 2 runs (1 died, 1 killed for time), 12k weighted tokens, 14% of all\n", out)
        self.assertEqual(self.rows(self.cli("stats"))["worker"][1:3], ["8", "2"])

    def test_an_outcome_row_fails_a_ticket_whatever_the_log_says(self):
        append_jsonl(self.p.state / "ledger.jsonl", dict(event="outcome", ticket="T-002", outcome="failed"))
        log(self.p, "T-002: gave up: reworded")
        r = self.rows(self.cli("stats", "--by", "ticket"))
        self.assertEqual(r["T-002"][1], "failed")
        self.assertEqual(r["T-002"][2], "1")  # the outcome row is no run
        self.assertEqual(r["T-003"][1], "failed")  # no outcome row: the log, as before

    def test_a_later_worker_run_reopens_a_failed_ticket(self):
        ledger = self.p.state / "ledger.jsonl"
        append_jsonl(ledger, dict(event="outcome", ticket="T-002", outcome="failed"))
        append_jsonl(ledger, dict(role="worker", ticket="T-002", input=1000, usd=0.1))
        self.assertEqual(self.rows(self.cli("stats", "--by", "ticket"))["T-002"][1], "open")

    def test_runner_fail_writes_the_outcome_row(self):
        pilot = SimpleNamespace(p=self.p, skip=set(), failed=[], approved={}, fail_streak=0, log=lambda m: None,
                                name=lambda t: t, note_heavy=lambda tid=None: None)
        with mock.patch.object(driver, "push"), mock.patch.object(driver.net, "online", return_value=True):
            driver.Pilot.fail(pilot, "T-004", "tests fail")
        row = [r for r in read_jsonl(self.p.state / "ledger.jsonl") if r.get("event")][-1]
        self.assertEqual({k: row[k] for k in ("event", "ticket", "outcome")},
                         {"event": "outcome", "ticket": "T-004", "outcome": "failed"})
        self.assertTrue(row["at"])
        self.assertEqual(self.rows(self.cli("stats", "--by", "ticket"))["T-004"][1], "failed")

    def test_singular_run(self):
        out = self.cli("stats", "--by", "ticket")
        self.assertIn("(≥: 1 run ended before its result,", out)
        self.assertNotIn("1 runs", out)

    def test_every_table_line_fits_100_columns(self):
        append_jsonl(self.p.state / "ledger.jsonl", dict(role="periodic:a-long-name", ticket="", turns=99999,
                                                        input=10 ** 8, cache_read=10 ** 9, output=10 ** 7,
                                                        usd=9999.99, minutes=99999, base=10 ** 6))
        for by in ("role", "ticket"):
            out = self.cli("stats", "--by", by)
            self.assertEqual([l for l in out.splitlines() if len(l) > 100], [])
        self.assertIn("cached/turn", self.cli("stats", "--wide").splitlines()[0])

    def test_tickets_no_longer_in_the_queue_are_named_in_the_overhead(self):
        append_jsonl(self.p.state / "ledger.jsonl", dict(role="worker", ticket="T-009", input=5000, usd=0.1))
        out = self.cli("stats", "--by", "ticket")
        self.assertEqual(self.rows(out)["T-009"][1], "gone")
        self.assertIn("with overhead: 50k weighted tokens per merged ticket\n"
                      "  overhead: planner: 11k; 1 ticket no longer in the queue (T-009): 5k\n", out)

    def test_swept_rows_sum_into_the_orphan_line(self):
        self.assertNotIn("orphaned", self.cli("stats"))
        for n in (2, 3):
            append_jsonl(self.p.state / "ledger.jsonl", {"event": "swept", "role": "driver", "groups": n})
        out = self.cli("stats")
        self.assertIn("5% of all\norphaned process groups killed: 5\n", out)  # with the summary, before the notes
        self.assertEqual(self.rows(out)["worker"][1], "5")  # a sweep is no run

if __name__ == "__main__":
    unittest.main()
