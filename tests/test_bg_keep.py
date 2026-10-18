"""A kept heavy job (`windvane bg --heavy --keep`) outlives the session that queued it: the driver holds its ticket,
and its slot, until it ends, then resumes the ticket with the job's exit code. The run's stop kills it unless
`heavy_keep_hours` lets it go on, and `windvane stop` always does."""
import io
import json
import os
import subprocess
import sys
import threading
import time
import unittest
from argparse import Namespace
from contextlib import redirect_stdout
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from windvane import cli, driver, jobs, runner  # noqa: E402
from windvane.config import Project  # noqa: E402
from windvane.runner import NEST, TICKET  # noqa: E402
from windvane.util import now  # noqa: E402
from helpers import Sandbox, no_wrap_up  # noqa: E402
from test_heavy_check_wait import hold  # noqa: E402
from test_parallel import args  # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        self.s = Sandbox()
        self.addCleanup(self.s.close)
        self.addCleanup(lambda: jobs.end_kept(self.p, why="the test's end"))  # before the sandbox goes
        self.run_dir = self.s.tmp / "run"  # the machine's heavy lock and line, this test's own
        self.run_dir.mkdir()
        (self.s.tmp / "config/windvane").mkdir(parents=True, exist_ok=True)
        (self.s.tmp / "config/windvane/machine.toml").write_text("busy = []\nmin_free_gb = 0\nheavy_wait = 60\n")
        os.environ["XDG_RUNTIME_DIR"] = str(self.run_dir)
        for k in ("WINDVANE_HEAVY_HELD", jobs.RUN_STOP):
            os.environ.pop(k, None)
        self.p = Project(self.s.root)
        self.job = self.p.state / "bg" / "T-001" / "job"

    def until(self, ok, timeout=30, what="the condition"):
        end = time.time() + timeout
        while not ok():
            self.assertLess(time.time(), end, f"timed out waiting for {what}")
            time.sleep(0.05)

    def workers(self, tid="T-001"):
        calls = [json.loads(x) for x in (self.s.tmp / "calls.jsonl").read_text().splitlines()]
        return [c["prompt"] for c in calls if c["system"].startswith("# windvane worker run")
                and c["prompt"].startswith(f"Ticket {tid}.")]

    def starts(self, tid):
        """When each worker run of `tid` started (the fake logs a span row first)."""
        calls = [json.loads(x) for x in (self.s.tmp / "calls.jsonl").read_text().splitlines()]
        return [float(c["prompt"].split()[-1]) for c in calls
                if c["system"] == "span" and c["prompt"].startswith(f"{tid} start")]

    def pilot(self, **kw):
        pilot = driver.Pilot(self.p, args(**{"parallel": 1, **kw}))
        pause = pilot.pause
        pilot.pause = lambda seconds=30: pause(0.2)  # a test can't wait half a minute for each look at the job
        return pilot

    def start(self, pilot, minutes=60):
        t = threading.Thread(target=pilot.run, args=(now() + timedelta(minutes=minutes),))
        t.start()
        self.addCleanup(t.join, 60)
        return t

    def queued(self):
        """Once the first worker has ended PARTIAL with its job waiting behind the holder."""
        self.until(lambda: self.job.with_suffix(".keep").exists() and jobs.alive(self.job), what="the kept job")
        self.until(lambda: any("held until its kept job" in line for line in self.log()), what="the PARTIAL")

    def log(self):
        f = self.p.state / "autopilot.log"
        return f.read_text().splitlines() if f.exists() else []


class KeptJobResumesItsTicket(Base):
    def setUp(self):
        super().setUp()
        for t in ("T-001", "T-002"):
            self.s.ticket(t)
        self.s.publish()
        os.environ["FAKE_KEEP"] = "echo the kept job ran; exit 3"
        os.environ["FAKE_KEEP_FOR"] = "T-001"

    def test_a_kept_job_outlives_its_session_holds_its_ticket_and_slot_and_resumes_it(self):
        holder = hold(self, self.run_dir)
        pilot = self.pilot()
        slots = []
        start = pilot.start

        def record(slot, tid, kind, branches):
            slots.append((slot, tid, jobs.alive(self.job)))
            start(slot, tid, kind, branches)
        pilot.start = record
        t = self.start(pilot)
        self.queued()
        time.sleep(1.5)  # a few of the loop's looks: none may start a session for it
        self.assertTrue(jobs.alive(self.job), "the job died with its session")
        self.assertFalse(list((self.p.state / "nested").glob("*/*[0-9]")), "the job is registered with a session")
        self.assertEqual(len(self.workers()), 1)
        self.assertEqual(self.workers("T-002"), [])  # its only slot holds the job's tree
        self.assertNotIn("T-001", pilot.partials)  # a PARTIAL that waits for its job didn't run out of time
        self.assertIn("T-001 waits: its kept job 'job' runs", pilot.waits)

        (self.run_dir / "release").touch()  # the holder ends: the job gets the machine, runs and exits 3
        holder.wait(10)
        t.join(60)
        self.assertFalse(t.is_alive())
        self.assertEqual(sorted(pilot.merged), ["T-001", "T-002"])
        first, resumed = self.workers()
        self.assertIn("## Your kept jobs (`bg --keep`) ended\n- 'job' exit 3", resumed)
        self.assertIn("`windvane await job`", resumed)
        self.assertEqual(slots[0][1:], ("T-001", False))
        self.assertFalse([s for s in slots if s[2]], "a session started while the kept job ran")
        self.assertFalse(self.job.parent.exists())  # its files went with its merged ticket


class KeptSlotBesideOthers(Base):
    def setUp(self):
        super().setUp()
        for t in ("T-001", "T-002", "T-003"):
            self.s.ticket(t, touches=f"{t}.txt")
        self.s.publish()
        os.environ["FAKE_KEEP"] = "echo kept; exit 0"
        os.environ["FAKE_KEEP_FOR"] = "T-001"

    def test_other_tickets_run_in_other_slots_while_a_kept_job_holds_its_tree(self):
        holder = hold(self, self.run_dir)
        pilot = self.pilot(parallel=2)
        pilot.serial_fits = lambda: None  # side by side whatever the time estimate says
        seen = []
        start = pilot.start

        def record(slot, tid, kind, branches):
            seen.append((slot, tid, jobs.alive(self.job)))
            start(slot, tid, kind, branches)
        pilot.start = record
        t = self.start(pilot)
        self.queued()
        self.until(lambda: {"T-002", "T-003"} <= set(pilot.merged), what="the other tickets to merge")
        t001 = next(slot for slot, tid, _ in seen if tid == "T-001")
        beside = [(slot, tid) for slot, tid, kept in seen if kept and tid != "T-001"]
        self.assertTrue(beside, "no other ticket ran while the job did")
        self.assertNotIn(t001, [slot for slot, _ in beside])  # never in the tree the job writes into
        (self.run_dir / "release").touch()
        holder.wait(10)
        t.join(60)
        self.assertIn("T-001", pilot.merged)
        self.assertEqual(next(slot for slot, tid, _ in reversed(seen) if tid == "T-001"), t001)


class RunStop(Base):
    def setUp(self):
        super().setUp()
        self.s.ticket("T-001")
        self.s.publish()
        os.environ["FAKE_KEEP"] = "echo the kept job ran; exit 0"
        self.holder = hold(self, self.run_dir)

    def run_to_stop(self, seconds=8):
        """A run that ends `seconds` from now, its kept job still in line."""
        no_wrap_up(self)
        pilot = self.pilot()
        pilot.run(now() + timedelta(seconds=seconds))
        return pilot

    def test_the_runs_stop_kills_a_kept_job_and_the_next_run_resumes_its_ticket(self):
        pilot = self.run_to_stop()
        self.assertEqual(pilot.merged, [])
        self.until(lambda: not jobs.alive(self.job), 10, "the kill")
        self.assertEqual(self.job.with_suffix(".exit").read_text().strip(), str(jobs.KILLED))
        self.assertIn("killed: the run's stop", self.job.with_suffix(".log").read_text())
        self.assertIn("Kept jobs killed at the stop", pilot.summary())
        self.assertNotIn("T-001", pilot.partials)

        (self.run_dir / "release").touch()
        self.holder.wait(10)
        os.environ.pop("FAKE_KEEP")
        nxt = self.pilot()
        nxt.run(now() + timedelta(minutes=40))
        self.assertEqual(nxt.merged, ["T-001"])
        self.assertIn(f"- 'job' exit {jobs.KILLED}", self.workers()[-1])
        self.assertNotIn("the kept job ran", self.workers()[-1])


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
        pilot.clean_state = lambda between: (time.sleep(5) if not between else None, clean(between))
        pilot.run(now() + timedelta(seconds=8))
        self.assertTrue(jobs.alive(self.job), "killed during the wind-down despite heavy_keep_hours")
        self.assertAlmostEqual(jobs.kept_until(self.job), time.time() + 2 * 3600, delta=60)

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


class StopsAndSessions(Base):
    def bg(self, keep, session="worker-session", run_stop=None):
        os.environ[TICKET], os.environ[NEST] = "T-001", session
        if run_stop is not None:
            os.environ[jobs.RUN_STOP] = str(run_stop)
        with redirect_stdout(io.StringIO()) as out:
            jobs.bg(self.p, "job", ["sleep", "60"], str(self.s.root), heavy=True, keep=keep)
        for k in (TICKET, NEST, jobs.RUN_STOP):
            os.environ.pop(k, None)
        self.until(lambda: self.job.with_suffix(".pid").exists())
        return out.getvalue()

    def test_without_keep_the_job_still_dies_with_its_session(self):
        self.bg(keep=False)
        self.assertTrue(jobs.alive(self.job))
        runner.kill_nested(self.p, "worker-session")
        pid = int(self.job.with_suffix(".pid").read_text())
        self.until(lambda: os.waitpid(pid, os.WNOHANG)[0], 10, "the job to die with its session")

    def test_a_kept_job_survives_its_session_and_ends_at_its_runs_stop(self):
        said = self.bg(keep=True, run_stop=time.time() + 3)
        self.assertIn("kept past this session", said)
        runner.kill_nested(self.p, "worker-session")
        time.sleep(0.5)
        self.assertTrue(jobs.alive(self.job))
        self.until(lambda: not jobs.alive(self.job), 15, "the job to end at the run's stop")
        self.assertEqual(self.job.with_suffix(".exit").read_text().strip(), str(jobs.KILLED))
        self.assertIn("stopped at the run's stop", self.job.with_suffix(".log").read_text())

    def test_windvane_stop_kills_a_kept_job_with_no_run_going(self):
        self.bg(keep=True, run_stop=time.time() + 3600)
        child = int(self.job.with_suffix(".child").read_text()) if self.job.with_suffix(".child").exists() else None
        with redirect_stdout(io.StringIO()) as out:
            cli.cmd_stop(Namespace(if_running=False, now=False), self.p)
        self.assertIn("killed kept jobs: T-001's 'job'", out.getvalue())
        self.assertFalse(jobs.alive(self.job))
        if child:
            self.until(lambda: not os.path.exists(f"/proc/{child}") or "Z" in Path(f"/proc/{child}/stat").read_text()
                       .split(") ")[-1][:1], 10, "the command's own group")

    def test_a_pid_reused_after_a_reboot_holds_nothing(self):
        """A kept job's leader gone with a reboot, its number now a stranger's: no hold, and the stranger lives."""
        stranger = subprocess.Popen(["sleep", "30"])
        self.addCleanup(lambda: (stranger.kill(), stranger.wait()))
        self.job.parent.mkdir(parents=True)
        for end, text in (("start", "1"), ("cwd", str(self.s.root)), ("keep", ""), ("pid", str(stranger.pid))):
            self.job.with_suffix(f".{end}").write_text(text)
        before = time.time() - 3600  # bg wrote it an hour before the stranger started
        os.utime(self.job.with_suffix(".pid"), (before, before))
        self.assertEqual(jobs.running_kept(self.p), [])
        self.assertEqual(jobs.end_kept(self.p), [])
        self.assertIsNone(stranger.poll())
        os.utime(self.job.with_suffix(".pid"))  # written as it started: its own job, running
        self.assertEqual(jobs.running_kept(self.p), [self.job])

    def test_keep_goes_with_heavy(self):
        self.addCleanup(os.chdir, os.getcwd())  # -C moves into the sandbox, which goes at the test's end
        with self.assertRaisesRegex(SystemExit, "--keep goes with --heavy"):
            cli.main(["-C", str(self.s.root), "bg", "--keep", "job", "true"])
        self.assertFalse(self.job.parent.exists())


if __name__ == "__main__":
    unittest.main()
