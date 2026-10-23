"""Measurements queued together share one hold: one behind a measurement that holds the line (in line, or running
as the lock's holder) holds it at once, so the jobs behind them are let by for one `measure_hold` in all, not one each.
A hold dies with its job: a killed holder holds no one up, and its job started again earns its own."""
import io
import sys
import time
import unittest
from contextlib import redirect_stderr
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from windvane import heavy  # noqa: E402
from test_heavy_measure import Fixture  # noqa: E402

HOLD = "0.05"  # 3 s


class ManyMeasurements(Fixture):
    def setUp(self):
        super().setUp()
        self.machine(f"measure_load = 4\nmeasure_hold = {HOLD}\n")

    def holding(self, tag):
        return heavy.HOLDING.format(HOLD) in self.said(tag)

    def too(self, tag, pid):
        return any(line.endswith("; " + heavy.HOLDING_TOO.format(pid)) for line in self.said(tag))

    def held(self, pid):
        return f"heavy: waiting: pid {pid}, a --measure job, holds the line for a quiet CPU: load 9.5 (want ≤ 4)"

    def measuring(self, tag):
        """A measurement that stays running until its `go` file appears: its own load keeps the CPU busy meanwhile."""
        go = self.d / f"go-{tag}"
        self.addCleanup(go.touch)
        job = self.job(tag, measure=True, cmd=f"echo {tag} >> {self.order}; while [ ! -e {go} ]; do sleep 0.05; done")
        return job, go

    def runs_next(self, tag, go, ran):
        """The load drops, `tag` starts, the load comes back up as it runs, and it ends."""
        self.load(1)
        self.until(lambda: self.ran() == ran)
        self.load(9.5)
        go.touch()

    def test_three_measurements_let_a_job_by_for_one_hold_in_all(self):
        first, go1 = self.measuring("first")
        t0 = time.time()
        self.until(lambda: self.said("first"))
        self.assertEqual(self.job("early", measure=False).wait(15), 0)  # let by within the hold
        self.until(lambda: self.holding("first"))
        self.assertGreaterEqual(time.time() - t0, 2.9)
        second, go2 = self.measuring("second")
        self.until(lambda: self.said("second"))  # in line in this order
        third = self.job("third", measure=True)
        self.until(lambda: self.said("third"))
        plain = self.job("plain", measure=False)
        self.until(lambda: self.held(first.pid) in "\n".join(self.said("plain")))
        self.until(lambda: self.too("second", first.pid) and self.too("third", first.pid))
        self.assertTrue(self.said("second")[0].startswith(self.held(first.pid)), "folded into its wait line")
        self.runs_next("first", go1, ["early", "first"])
        self.assertEqual(first.wait(15), 0)
        time.sleep(1.5)  # the second, now the head, holds at once: the plain job isn't let by with the load up
        self.assertEqual(self.ran(), ["early", "first"])
        self.runs_next("second", go2, ["early", "first", "second"])
        self.assertEqual(second.wait(15), 0)
        time.sleep(1.5)
        self.assertEqual(self.ran(), ["early", "first", "second"])
        self.load(1)
        for job in (third, plain):
            self.assertEqual(job.wait(15), 0)
        self.assertEqual(self.ran(), ["early", "first", "second", "third", "plain"])
        for tag in ("second", "third"):
            self.assertFalse(self.holding(tag), "no hold of its own")
            self.assertEqual(sum(heavy.HOLDING_TOO.split(",")[0] in line for line in self.said(tag)), 1)

    def test_one_that_comes_while_a_holder_runs_holds_at_once(self):
        first, go1 = self.measuring("first")
        self.until(lambda: self.holding("first"))
        self.load(1)
        self.until(lambda: self.ran() == ["first"])
        self.load(9.5)
        late = self.job("late", measure=True)  # the head while the first runs: the lock says it held the line
        self.until(lambda: any(heavy.HOLDING_TOO.format(first.pid) in line for line in self.said("late")))
        plain = self.job("plain", measure=False)
        self.until(lambda: self.said("plain"))
        go1.touch()
        self.assertEqual(first.wait(15), 0)
        time.sleep(1.5)
        self.assertEqual(self.ran(), ["first"])
        self.load(1)
        self.assertEqual(late.wait(15), 0)
        self.assertEqual(plain.wait(15), 0)
        self.assertEqual(self.ran(), ["first", "late", "plain"])

    def test_a_holder_killed_holds_no_one_up_and_its_job_again_earns_its_own_hold(self):
        for running in (False, True):
            with self.subTest(killed="running" if running else "waiting"):
                self.order.unlink(missing_ok=True)
                self.load(9.5)
                timing, go = self.measuring(f"timing{running}")
                self.until(lambda: self.holding(f"timing{running}"))
                if running:  # it held the line, then got the machine: the lock keeps its mark
                    self.load(1)
                    self.until(lambda: self.ran() == [f"timing{running}"])
                    self.load(9.5)
                else:
                    build = self.job("build", measure=False)
                    self.until(lambda: self.held(timing.pid) in "\n".join(self.said("build")))
                timing.kill()
                timing.wait()
                go.touch()
                if not running:
                    self.assertEqual(build.wait(15), 0)
                t0 = time.time()
                again = self.job(f"again{running}", measure=True)
                self.until(lambda: self.said(f"again{running}"))
                self.assertEqual(self.job(f"by{running}", measure=False).wait(15), 0, "let by: no hold inherited")
                self.until(lambda: heavy.HOLDING.format(HOLD) in self.said(f"again{running}"))
                self.assertGreaterEqual(time.time() - t0, 2.9)
                self.assertFalse(any(heavy.HOLDING_TOO.split(",")[0] in line for line in self.said(f"again{running}")))
                self.load(1)
                self.assertEqual(again.wait(15), 0)
                self.assertEqual(self.waiters(), [])

    def test_a_default_need_wait_names_machine_toml(self):
        toml = self.d / "cfg/windvane/machine.toml"
        toml.write_text("busy = []\nmin_free_gb = 64\n")
        self.in_process(holders=lambda: [])
        err = io.StringIO()
        with redirect_stderr(err):
            self.assertEqual(heavy.run(["true"], wait_s=1.2), heavy.TIMED_OUT)
        line = err.getvalue().splitlines()[0]
        self.assertIn(f"the 64 GB is min_free_gb in {toml}: if this job needs less", line)
        self.assertEqual(heavy.NEED_RE.search(line).group(1), "64")


if __name__ == "__main__":
    unittest.main()
