"""A check's log and its cost accounting: a heartbeat times a silent hang to its kill, the start row waits for the
log's lock so `stats` never calls a starting check cut off, old logs are pruned at a check's start (never one still
running or the newest failures, and never failing the check), and `stats` names the costliest wasted check without
`--by` too."""
import fcntl
import io
import os
import signal
import sys
import time
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from windvane import cli, jobs  # noqa: E402
from windvane.util import read_jsonl  # noqa: E402
from test_stats_check_more import Base  # noqa: E402

DAY = 86400


class Check(Base):
    def setUp(self):
        super().setUp()
        self.real = jobs.subprocess.run
        self.logs = self.p.state / "checks"
        self.logs.mkdir(parents=True, exist_ok=True)
        self.s.write("new", "x")  # dirty, so no check reuses a pass

    def starts(self):
        return [r for r in read_jsonl(self.ledger) if r["event"] == "check-start"]

    def run_check(self, check):
        """jobs.check with the check command replaced by `check(stdout)` (git still runs)."""
        def run(cmd, *args, **kw):
            if isinstance(cmd, list) and cmd[0] == "git":
                return self.real(cmd, *args, **kw)
            return mock.Mock(returncode=check(kw["stdout"]))

        with mock.patch.object(jobs.subprocess, "run", run):
            return jobs.check(self.p, self.s.root, ticket="T-001")


class SilentHang(Check):
    def test_a_silent_check_killed_counts_about_its_real_length(self):
        """The ticket's minute-long hang, scaled down: a 0.25 s heartbeat and a 3 s silent check. Without the
        heartbeat the log's last write is its creation, and the check counts as 0."""
        beat, hang = 0.25, 3.0
        with mock.patch.object(jobs, "HEARTBEAT", beat):
            child = os.fork()
            if not child:
                try:
                    self.run_check(lambda out: time.sleep(60))
                finally:
                    os._exit(0)
        deadline = time.time() + 10
        while not self.starts():
            self.assertLess(time.time(), deadline, "the check never started")
            time.sleep(0.02)
        time.sleep(hang)
        os.kill(child, signal.SIGKILL)
        os.waitpid(child, 0)
        real = time.time() - self.starts()[0]["start"]
        self.assertEqual((self.logs / self.starts()[0]["log"]).stat().st_size, 0)  # it printed nothing
        ran, _, live = cli.check_runs(self.p, read_jsonl(self.ledger))
        self.assertEqual((len(ran), ran[0]["finished"], live), (1, False, 0))
        self.assertGreater(real, hang)
        self.assertLess(abs(ran[0]["minutes"] * 60 - real), beat + 0.6)  # one beat, and the 0.01 min rounding


class StartRow(Check):
    def test_stats_never_sees_a_starting_check_unlocked(self):
        """At the moment the start row is written, the log is already locked: `stats` then says still running."""
        seen, write = [], jobs.ledger

        def spy(p, ticket, row, event="check"):
            write(p, ticket, row, event)
            if event == "check-start":
                seen.append((jobs.running(self.p, row), self.lines()))

        with mock.patch.object(jobs, "ledger", spy):
            passed, _, _ = self.run_check(lambda out: 0)
        self.assertTrue(passed)
        self.assertEqual(seen, [(True, ["checks: 0 runs, 0.0 min, 1 still running"])])
        self.assertEqual(self.lines(), ["checks: 1 run, 0.0 min (0.0 min a ticket checked)"])


class Prune(Check):
    def log(self, name, days, passed=None):
        """A check log `days` old; `passed` adds its ledger rows (True or False), None leaves it unnamed (a desk
        check)."""
        path = self.logs / f"{name}.log"
        path.write_text("out\n")
        os.utime(path, (time.time() - days * DAY,) * 2)
        if passed is not None:
            start = float(len(read_jsonl(self.ledger)))
            self.add(event="check-start", ticket="T-009", start=start, log=path.name)
            self.add(event="check", ticket="T-009", start=start, minutes=1.0, passed=passed, reused=False)
        return path

    def left(self):
        return sorted(p.name[:-4] for p in self.logs.glob("*.log") if p.name not in {r["log"] for r in self.starts()
                                                                                    if r["ticket"] == "T-001"})

    def test_old_passed_logs_go_a_locked_one_and_the_newest_failures_stay(self):
        self.log("20260901-old-passed", 4, True)
        self.log("20260902-new-passed", 1, True)
        held = self.log("20260903-old-passed-locked", 4, True)
        failed = [self.log(f"202608{10 + i}-ancient-failed-{i}", 40, False if i % 2 else None)  # failed, or a desk's
                  for i in range(jobs.KEEP_FAILED + 1)]
        self.log("20260801-ancient-passed", 40, True)
        with held.open("rb") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            passed, _, _ = self.run_check(lambda out: 0)
        self.assertTrue(passed)
        self.assertEqual(self.left(), sorted(["20260902-new-passed", "20260903-old-passed-locked",
                                              *(p.name[:-4] for p in failed[1:])]))

    def test_a_killed_checks_minutes_outlive_its_pruned_log(self):
        WASTED = "costliest wasted check: T-060, 18 min, cut off"  # noqa: N806
        start = time.time() - 40 * DAY
        self.add(event="check-start", ticket="T-060", start=start, log="20260801-killed.log")
        killed = self.logs / "20260801-killed.log"
        killed.write_text("out\n")
        os.utime(killed, (start + 18 * 60,) * 2)
        for i in range(jobs.KEEP_FAILED):  # newer failures, so the killed one isn't kept as one of the newest
            self.log(f"2026090{i}-newer-failed", 1, False)
        self.assertEqual(self.lines("--by", "role", start="costliest"), [WASTED])
        passed, _, _ = self.run_check(lambda out: 0)
        self.assertTrue(passed)
        self.assertFalse(killed.exists())
        self.assertEqual(self.lines("--by", "role", start="costliest"), [WASTED])
        end = [r for r in read_jsonl(self.ledger) if r["event"] == "check" and r["ticket"] == "T-060"]
        self.assertEqual([(r["finished"], r["passed"], r["start"]) for r in end], [(False, False, start)])
        self.assertEqual(end[0]["at"], self.starts()[0]["at"])  # dated as it happened

    def test_one_prune_at_a_time(self):
        """A check starting while another slot's check prunes skips its prune, so a killed log is closed once."""
        old = self.log("20260901-old-passed", 4, True)
        with (self.p.state / "prune.lock").open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            self.assertTrue(self.run_check(lambda out: 0)[0])
            self.assertTrue(old.exists())
        self.assertTrue(self.run_check(lambda out: 0)[0])
        self.assertFalse(old.exists())

    def test_a_file_it_cannot_read_or_delete_does_not_fail_the_check(self):
        """Both are old passed logs, so the prune tries them (first: names sort by time) and must go on past them."""
        self.log("20260801-a-directory", 4, True).unlink()
        (self.logs / "20260801-a-directory.log").mkdir()
        os.utime(self.logs / "20260801-a-directory.log", (time.time() - 4 * DAY,) * 2)
        unreadable = self.log("20260802-unreadable", 4, True)
        unreadable.chmod(0)
        self.addCleanup(unreadable.chmod, 0o644)
        self.log("20260901-old-passed", 4, True)
        passed, _, _ = self.run_check(lambda out: 0)
        self.assertTrue(passed)
        self.assertIn("20260801-a-directory", self.left())
        self.assertNotIn("20260901-old-passed", self.left())
        if os.geteuid():  # root reads it anyway, and deletes it as an old passed log
            self.assertIn("20260802-unreadable", self.left())

    def test_a_prune_that_breaks_skips_itself_not_the_check(self):
        old = self.log("20260901-old-passed", 4, True)
        err = io.StringIO()
        with mock.patch.object(jobs, "read_jsonl", side_effect=ValueError("bad ledger")), redirect_stderr(err):
            passed, _, _ = self.run_check(lambda out: 0)
        self.assertTrue(passed)
        self.assertTrue(old.exists())
        self.assertEqual(err.getvalue(), "check logs not pruned (the check runs anyway): bad ledger\n")


class Costliest(Base):
    def test_stats_without_by_names_the_costliest_wasted_check(self):
        self.add(event="check", ticket="T-001", start=1.0, minutes=30.0, passed=True, reused=False)
        self.add(event="check", ticket="T-002", start=2.0, minutes=9.0, passed=False, reused=False)
        self.assertEqual(self.lines(start=("most check time:", "costliest wasted check:")),
                         ["costliest wasted check: T-002, 9.0 min, failed"])


if __name__ == "__main__":
    unittest.main()
