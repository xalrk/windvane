"""Every check gets its own log, even two started in the same second, so a killed one can't pass for running or
overwrite another's log; a killed check is timed to its log's last write; and `--by ticket` names the costliest
check that bought nothing."""
import os
import signal
import sys
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from windvane import jobs  # noqa: E402
from windvane.util import read_jsonl  # noqa: E402
from test_stats_check_more import Base  # noqa: E402

SECOND = datetime(2026, 10, 5, 6, 0, 0)


class SameSecond(Base):
    def setUp(self):
        super().setUp()
        frozen = mock.patch.object(jobs, "now", lambda: SECOND)
        frozen.start()
        self.addCleanup(frozen.stop)
        self.real = jobs.subprocess.run

    def starts(self):
        return [r for r in read_jsonl(self.ledger) if r["event"] == "check-start"]

    def test_two_checks_in_the_same_second_write_two_logs(self):
        self.s.write("new", "x")  # dirty, so neither reuses a pass
        jobs.check(self.p, self.s.root, ticket="T-001")
        jobs.check(self.p, self.s.root, ticket="T-002")
        a, b = (r["log"] for r in self.starts())
        self.assertNotEqual(a, b)
        self.assertTrue(a.startswith("20261005-060000-") and b.startswith("20261005-060000-"))
        self.assertEqual(sorted(p.name for p in (self.p.state / "checks").iterdir()), sorted([a, b]))

    def test_killing_one_leaves_the_others_lock_and_log_alone(self):
        parent, seen = os.getpid(), []

        def run(cmd, *args, **kw):
            if isinstance(cmd, list) and cmd[0] == "git":
                return self.real(cmd, *args, **kw)
            out = kw["stdout"]
            if os.getpid() != parent:  # the second check, killed while it runs
                out.write("second\n")
                out.flush()
                time.sleep(60)
            out.write("first\n")
            out.flush()
            child = os.fork()
            if not child:
                try:
                    jobs.check(self.p, self.s.root, ticket="T-002")
                finally:
                    os._exit(0)
            deadline = time.time() + 10
            while len(self.starts()) < 2 or not (self.p.state / "checks" / self.starts()[1]["log"]).stat().st_size:
                self.assertLess(time.time(), deadline, "the second check never started")
                time.sleep(0.02)
            os.kill(child, signal.SIGKILL)
            os.waitpid(child, 0)
            first, second = self.starts()
            seen.extend([jobs.running(self.p, first), jobs.running(self.p, second), self.lines()])
            out.write("first failed\n")
            return mock.Mock(returncode=1)

        self.s.write("new", "x")
        with mock.patch.object(jobs.subprocess, "run", run):
            passed, _, log = jobs.check(self.p, self.s.root, ticket="T-001")
        self.assertFalse(passed)
        self.assertEqual(seen[:2], [True, False])  # the first still holds its lock; the killed one is cut off
        self.assertRegex(seen[2][0], r"^checks: 1 run, .*, 1 cut off, 1 still running$")
        self.assertEqual(log.read_text(), "first\nfirst failed\n")
        second = self.p.state / "checks" / self.starts()[1]["log"]
        self.assertNotEqual(second, log)
        self.assertEqual(second.read_text(), "second\n")


class KilledMinutes(Base):
    def killed(self, ticket, start, mins, log=True):
        name = f"{ticket}-{start}.log"
        self.add(event="check-start", ticket=ticket, start=start, log=name)
        if log:
            path = self.p.state / "checks" / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("out\n")
            os.utime(path, (start + mins * 60, start + mins * 60))

    def test_a_killed_check_is_timed_to_its_logs_last_write(self):
        self.killed("T-001", 1000.0, 12)
        self.assertEqual(self.lines(), ["checks: 1 run, 12 min (12.0 min a ticket checked), 1 cut off"])

    def test_a_killed_check_with_no_log_counts_no_minutes(self):
        self.killed("T-001", 1000.0, 12, log=False)
        self.assertEqual(self.lines(), ["checks: 1 run, 0.0 min (0.0 min a ticket checked), 1 cut off"])

    def test_by_ticket_names_the_costliest_wasted_check(self):
        self.add(event="check", ticket="T-001", start=1.0, minutes=30.0, passed=True, reused=False)  # longest, passed
        self.add(event="check", ticket="T-002", start=2.0, minutes=9.0, passed=False, reused=False)
        self.killed("T-060", 1000.0, 18)
        self.add(event="check", ticket="T-003", start=3.0, minutes=4.0, passed=False, reused=False, finished=False)
        out = self.lines("--by", "ticket", start=("most check time:", "costliest wasted check:"))
        self.assertEqual(out, ["most check time: T-001, 1 run, 30 min", "costliest wasted check: T-060, 18 min, cut off"])
        self.add(event="check", ticket="T-004", start=4.0, minutes=25.0, passed=False, reused=False)
        self.assertEqual(self.lines("--by", "ticket", start="costliest wasted check:"),
                         ["costliest wasted check: T-004, 25 min, failed"])
        self.assertEqual(self.lines("--by", "role", start="costliest wasted check:"), [])  # by ticket only

    def test_no_wasted_check_no_line(self):
        self.add(event="check", ticket="T-001", start=1.0, minutes=30.0, passed=True, reused=False)
        self.add(event="check", ticket="T-002", minutes=0, passed=True, reused=True)
        self.assertEqual(self.lines("--by", "ticket", start="costliest wasted check:"), [])


if __name__ == "__main__":
    unittest.main()
