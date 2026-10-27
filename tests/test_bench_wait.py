"""A periodic bench round waits for another bench before it starts: while one holds the machine's lock the round
isn't due (said once), it starts on a later pass once the lock is free, sized by the week as it is then, and a deadline
reached meanwhile starts nothing. `windvane bench` is never left to block in the lock, a wait no deadline counts."""
import re
import sys
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_bench_one_at_a_time import hold  # noqa: E402
import test_bench_periodic as tbp  # noqa: E402

from windvane import jobs, periodic  # noqa: E402
from windvane.util import now  # noqa: E402

WAITING = (r"^[\d: -]+bench: bench: waiting for other T-009's bench, running since \d\d:\d\d "
           r"\(windvane bench --status to check; windvane stop or Ctrl-C ends it\)$")


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
        self.assertLess(log.index("bench: bench: waiting"), log.index("bench: bench started in the background"))

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


if __name__ == "__main__":
    unittest.main()
