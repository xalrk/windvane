"""A periodic bench round waits for another bench before it starts: while one holds the machine's lock the round
isn't due (said once), it starts on a later pass once the lock is free, sized by the week as it is then, and a deadline
reached meanwhile starts nothing. `windvane bench` is never left to block in the lock, a wait no deadline counts."""
import os
import re
import sys
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_bench_one_at_a_time import hold  # noqa: E402
import test_bench_periodic as tbp  # noqa: E402

from windvane import bench, jobs, periodic  # noqa: E402
from windvane.heavy import JOB_ENV  # noqa: E402
from windvane.util import now  # noqa: E402

WAITING = (r"^[\d: -]+bench: waiting for other T-009's bench, running since \d\d:\d\d before its bench starts "
           r"\(windvane bench --status to check; windvane stop ends the wait\)$")


class RoundWaits(tbp.Base):
    setUp = tbp.Caps.setUp  # main's change since the last bench: a round is due

    def held(self):
        """Another project's bench holding the machine's lock until `free()`."""
        self.holder = hold(600)
        self.addCleanup(self.free)

    def free(self):
        if self.holder.poll() is None:
            self.holder.kill()
        self.holder.wait()

    def passes(self, pilot, n=3):
        for _ in range(n):
            if due := pilot.periodic_due():
                pilot.periodic(due)

    def test_it_waits_saying_so_once_then_starts_sized_from_then(self):
        pilot = self.pilot(hours=3, week=10)
        self.held()
        with self.fake([{}], sleep=30):
            self.passes(pilot)
            self.assertEqual(self.calls, [])
            self.assertFalse(periodic.bench_file(self.p, "bench").exists())
            self.assertEqual(len(re.findall(WAITING, self.log(), re.M)), 1, self.log())
            pilot.usage.week = 80  # the week moved while it waited: the round is sized as it starts
            self.free()
            self.passes(pilot, 1)
        self.addCleanup(jobs.end_job, periodic.bench_job(self.p, "bench"), "test over")
        self.assertEqual(len(self.calls), 1)
        a = self.calls[0]
        self.assertIn("--small", a)
        self.assertEqual(a[a.index("--hours") + 1], "0.5")
        log = self.log()
        self.assertLess(log.index("bench: waiting for other"), log.index("bench: bench started in the background"))

    def test_a_restart_or_a_second_wait_says_so_once_more(self):
        """Who has said it waits is the run's memory only: a restarted run says it once again, and so does a new
        wait after the lock was free for a pass."""
        self.held()
        with self.fake([{}]):
            self.passes(self.pilot())
            pilot = self.pilot()  # the driver restarted mid-wait
            self.passes(pilot)
            self.assertEqual(len(re.findall(WAITING, self.log(), re.M)), 2, self.log())
            self.free()
            self.assertIsNone(pilot.bench_held("bench"))
            self.held()
            self.passes(pilot)
        self.assertEqual(self.calls, [])
        self.assertEqual(len(re.findall(WAITING, self.log(), re.M)), 3, self.log())

    def test_a_deadline_reached_while_waiting_starts_nothing(self):
        pilot = self.pilot(hours=3)
        self.held()
        with self.fake([{}]):
            self.passes(pilot)
            pilot.deadline = now() + timedelta(hours=1)  # 120 min no longer fit
            self.free()
            self.passes(pilot)
        self.assertEqual(self.calls, [])
        self.assertIn("bench: skipped: 120 min don't fit before the deadline", self.log())

    def test_an_asked_round_waits_still_asked_then_starts(self):
        pilot = self.pilot(hours=3)
        periodic.ask(self.p, "bench")
        self.held()
        with self.fake([{}], sleep=30), mock.patch.object(periodic, "push") as pushed:
            self.passes(pilot)
            self.assertEqual((self.calls, periodic.asked(self.p)), ([], ["bench"]))
            self.free()
            self.passes(pilot, 1)
        self.addCleanup(jobs.end_job, periodic.bench_job(self.p, "bench"), "test over")
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(pushed.call_count, 0)
        self.assertEqual(periodic.asked(self.p), [])
        self.assertEqual(len(re.findall(WAITING, self.log(), re.M)), 1, self.log())


class WaitLine(unittest.TestCase):
    """`windvane bench`'s wait line names what ends that wait: Ctrl-C by hand, `bg --cancel <its job>` in the
    background, where no one has its terminal and `windvane stop` ends a run, not a bench."""
    def hint(self, job=None):
        env = {k: v for k, v in os.environ.items() if k != JOB_ENV}
        with mock.patch.dict("os.environ", env if job is None else {**env, JOB_ENV: job}, clear=True):
            return bench.wait_hint()

    def test_by_hand(self):
        self.assertEqual(self.hint(), "windvane bench --status to check; Ctrl-C ends the wait, nothing run")

    def test_in_a_background_job(self):
        self.assertEqual(self.hint("/s/bg/periodic-weekly"),
                         "windvane bench --status to check; windvane bg --cancel periodic-weekly ends it, nothing run")


if __name__ == "__main__":
    unittest.main()
