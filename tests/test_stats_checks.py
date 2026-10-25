"""`stats` tells a check's wait for the machine from its run: a heavy check's `waited_s` isn't run time, a check
killed while it still waited in the heavy queue (its log holds only the queue's lines) never ran, so it's cancelled
rather than cut off and all its minutes are wait, and a killed check that got the machine is cut off with its run
minutes only. The prune's end row for a killed check keeps the same split once its log is gone."""
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from windvane import jobs, stats  # noqa: E402
from windvane.util import read_jsonl  # noqa: E402
from test_stats_check_more import Base  # noqa: E402

QUEUED = "heavy: waiting for the machine lock: pid 9 (render.sh in game/work, since 09:00); 2 ahead of this job\n"


class WaitApart(Base):
    def setUp(self):
        super().setUp()
        self.t0 = time.time() - 7200
        self.add(event="check", ticket="T-001", role="worker", start=1.0, minutes=8, passed=True, reused=False,
                 waited_s=180)
        self.killed("T-002", "ran.log", "heavy: waited 60 s for the machine\nheavy: load 1.0 at start\ntests...\n", 10)
        self.killed("T-003", "queued.log", QUEUED * 3, 20)

    def killed(self, ticket, log, text, minutes):
        """A start row with no end row, its log last written `minutes` after its start."""
        f = self.p.state / "checks" / log
        f.parent.mkdir(exist_ok=True)
        f.write_text(text)
        self.add(event="check-start", ticket=ticket, role="worker", start=self.t0, log=log)
        jobs.os.utime(f, (self.t0 + 60 * minutes,) * 2)

    def test_the_checks_line_shows_run_and_waited_minutes_apart(self):
        # T-001 ran 5 of its 8 min, T-002 9 of its 10; T-003 only waited: 3 + 1 + 20 min in the queue
        self.assertEqual(self.lines(), ["checks: 2 runs, 14 min running (7.0 min a ticket checked), 24 min waiting "
                                        "for the machine, 1 cut off, 1 cancelled in the queue"])

    def test_the_costliest_wasted_check_is_timed_by_its_run(self):
        self.assertIn("costliest wasted check: T-002, 9.0 min, cut off", self.cli("stats"))

    def test_the_digest_counts_only_checks_that_ran_as_cut_off(self):
        d = stats.checks_digest(self.p, read_jsonl(self.ledger))
        self.assertEqual({k: d[k] for k in ("runs", "tickets", "per_ticket", "minutes", "waited", "failed", "cut_off",
                                            "cancelled_queued")},
                         {"runs": 2, "tickets": 2, "per_ticket": 7.0, "minutes": 14.0, "waited": 24.0, "failed": 0,
                          "cut_off": 1, "cancelled_queued": 1})

    def test_a_pruned_killed_checks_end_row_keeps_the_split(self):
        starts = {r["log"]: r for r in read_jsonl(self.ledger) if r["event"] == "check-start"}
        for log, row in starts.items():
            jobs.close(self.p, row, self.p.state / "checks" / log)
            (self.p.state / "checks" / log).unlink()
        ends = {r["ticket"]: r for r in read_jsonl(self.ledger) if r["event"] == "check" and r["ticket"] != "T-001"}
        self.assertEqual((ends["T-003"]["waited_s"], ends["T-003"]["cancelled_queued"]), (1200, True))
        self.assertEqual(ends["T-002"]["waited_s"], 60)
        self.assertNotIn("cancelled_queued", ends["T-002"])
        self.assertEqual(self.lines(), ["checks: 2 runs, 14 min running (7.0 min a ticket checked), 24 min waiting "
                                        "for the machine, 1 cut off, 1 cancelled in the queue"])

    def test_a_wait_past_the_logs_head_that_then_ran_is_cut_off(self):
        self.ledger.write_text("")
        self.killed("T-004", "long.log", QUEUED * 1000 + "heavy: waited 36000 s for the machine\ntests...\n", 660)
        self.assertGreater((self.p.state / "checks/long.log").stat().st_size, 65536)
        self.assertEqual(self.lines(), ["checks: 1 run, 60 min running (60.0 min a ticket checked), 600 min waiting "
                                        "for the machine, 1 cut off"])

    def test_without_a_wait_the_line_reads_as_before(self):
        self.ledger.write_text("")
        self.add(event="check", ticket="T-001", role="worker", start=1.0, minutes=8, passed=True, reused=False)
        self.assertEqual(self.lines(), ["checks: 1 run, 8.0 min (8.0 min a ticket checked)"])


if __name__ == "__main__":
    unittest.main()
