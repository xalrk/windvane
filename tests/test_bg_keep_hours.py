"""`heavy_keep_hours`: a kept heavy job runs on past the run's stop, its stop moving with the deadline, and the next
run resumes its ticket once it ends; `windvane stop` or a signal still kills it. The rest of the kept jobs' tests are
in test_bg_keep; apart, so the check's pool runs the two at once."""
import io
import os
import signal
import sys
import time
import unittest
from argparse import Namespace
from contextlib import redirect_stdout
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from windvane import cli, jobs  # noqa: E402
from windvane.config import Project  # noqa: E402
from windvane.util import now  # noqa: E402
from helpers import no_wrap_up  # noqa: E402
from test_bg_keep import Base  # noqa: E402
from test_heavy_check_wait import hold  # noqa: E402


class KeepHours(Base):
    def setUp(self):
        super().setUp()
        cfg = self.s.root / ".windvane/config.toml"
        cfg.write_text("heavy_keep_hours = 2\n" + cfg.read_text())  # top level: the sandbox's own ends in a table
        self.p = Project(self.s.root)
        self.s.ticket("T-001")
        self.s.ticket("T-002")
        self.s.publish()
        self.ended = self.s.tmp / "ended"
        os.environ["FAKE_KEEP"] = f"echo the kept job ran; date +%s > {self.ended}; exit 0"
        os.environ["FAKE_KEEP_FOR"] = "T-001"

    def test_heavy_keep_hours_lets_it_run_past_the_stop_and_the_next_run_resumes_it_when_it_ends(self):
        holder = hold(self, self.run_dir)
        no_wrap_up(self)
        pilot = self.pilot()
        pilot.run(now() + timedelta(seconds=8))
        self.assertTrue(jobs.alive(self.job), "killed at the stop despite heavy_keep_hours")
        until = jobs.kept_until(self.job)
        self.assertAlmostEqual(until, time.time() + 2 * 3600, delta=60)
        self.assertIn("Kept jobs run on until", pilot.summary())

        nxt = self.pilot()
        t = self.start(nxt)
        self.until(lambda: "T-001 waits: its kept job 'job' runs" in nxt.waits, what="the next run to hold it")
        self.assertEqual(len(self.workers()), 1)
        self.assertEqual(self.workers("T-002"), [])  # the job's slot is still T-001's
        (self.run_dir / "release").touch()
        holder.wait(10)
        t.join(60)
        self.assertEqual(sorted(nxt.merged), ["T-001", "T-002"])
        self.assertIn("- 'job' exit 0", self.workers()[-1])
        self.assertGreaterEqual(self.starts("T-001")[-1], float(self.ended.read_text()))  # resumed once it ended

    def test_a_wind_down_slower_than_the_watch_doesnt_kill_it(self):
        """The job's own watch looks at `.keep` every few seconds while the run winds down: told only the stop, it
        killed the job before the run's end could move it on by the hours."""
        hold(self, self.run_dir)
        no_wrap_up(self)
        pilot = self.pilot()
        clean = pilot.clean_state
        pilot.clean_state = lambda between: (time.sleep(5) if pilot.ending else None, clean(between))
        pilot.run(now() + timedelta(seconds=8))
        self.assertTrue(jobs.alive(self.job), "killed during the wind-down despite heavy_keep_hours")
        self.assertAlmostEqual(jobs.kept_until(self.job), time.time() + 2 * 3600, delta=60)

    def test_windvane_stop_during_the_run_kills_it_at_once(self):
        hold(self, self.run_dir)
        no_wrap_up(self)
        pilot = self.pilot()
        t = self.start(pilot)
        self.until(lambda: self.job.with_suffix(".keep").exists() and jobs.alive(self.job), what="the kept job")
        self.assertGreater(jobs.kept_until(self.job), time.time() + 3600)  # told the hours: only the stop ends it
        with redirect_stdout(io.StringIO()) as out:
            cli.cmd_stop(Namespace(if_running=False, now=False), self.p)
        self.assertIn("killed kept jobs: T-001's 'job'", out.getvalue())
        self.assertFalse(jobs.alive(self.job))
        self.assertIn("killed: windvane stop", self.job.with_suffix(".log").read_text())
        t.join(60)
        self.assertNotIn("Kept jobs run on", pilot.summary())

    def test_a_signal_during_the_wind_down_kills_it(self):
        hold(self, self.run_dir)
        no_wrap_up(self)
        pilot = self.pilot()
        clean = pilot.clean_state
        pilot.clean_state = lambda between: (pilot.winding_down(signal.SIGTERM) if pilot.ending else None,
                                             clean(between))
        pilot.run(now() + timedelta(seconds=8))
        self.assertFalse(jobs.alive(self.job), "a signalled run let its kept job run on")
        self.assertIn("killed: the run's stop", self.job.with_suffix(".log").read_text())
        self.assertIn("Kept jobs killed at the stop", pilot.summary())

    def test_a_deadline_moved_earlier_brings_the_kept_jobs_stop_with_it(self):
        pilot = self.pilot()
        pilot.deadline = now() + timedelta(hours=1)
        self.job.parent.mkdir(parents=True)
        self.job.with_suffix(".keep").write_text(str(pilot.keep_until().timestamp()))  # as bg writes it
        other = self.p.state / "bg" / "T-002" / "job"  # a desk job, or one an earlier run let go on longer
        other.parent.mkdir(parents=True)
        far = (now() + timedelta(hours=9)).timestamp()
        other.with_suffix(".keep").write_text(str(far))
        jobs_alive = jobs.alive
        self.addCleanup(setattr, jobs, "alive", jobs_alive)
        jobs.alive = lambda b: True
        pilot.follow_kept()
        (self.p.state / "DEADLINE").write_text((now() + timedelta(minutes=10)).isoformat())
        pilot.follow_kept()
        self.assertAlmostEqual(jobs.kept_until(self.job), time.time() + 600 + 2 * 3600, delta=60)
        self.assertEqual(jobs.kept_until(other), far)
        (self.p.state / "DEADLINE").write_text((now() + timedelta(hours=3)).isoformat())
        pilot.follow_kept()
        self.assertAlmostEqual(jobs.kept_until(self.job), time.time() + 5 * 3600, delta=60)



if __name__ == "__main__":
    unittest.main()
