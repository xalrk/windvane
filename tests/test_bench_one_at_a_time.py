"""One bench at a time on a machine: `windvane bench` holds a flock in the state root for its whole run, whoever
started it; a second waits (saying so once), a stop during the wait ends it with nothing run, and a lock whose process
died is free. An experiment waits for it before its fit is reckoned."""
import fcntl
import os
import re
import signal
import subprocess
import sys
import threading
import time
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_bench  # noqa: E402
import test_experiments as te  # noqa: E402
from helpers import REPO  # noqa: E402

from windvane import bench, driver, experiment  # noqa: E402
from windvane.config import Project  # noqa: E402
from windvane.heavy import JOB_ENV  # noqa: E402
from windvane.util import now  # noqa: E402

# A bench of another project, for an experiment's ticket: takes the lock, says so, holds it for argv[1] seconds
HOLD = ("import sys, time, types\nfrom windvane import bench\nbench.take_lock(types.SimpleNamespace(name='other'))\n"
        "print('held', flush=True)\ntime.sleep(float(sys.argv[1]))\n")


def hold(seconds, job="experiment-T-009"):
    """A process holding the bench lock, once it does."""
    env = {**os.environ, "PYTHONPATH": str(REPO), JOB_ENV: f"/x/bg/{job}"}
    proc = subprocess.Popen([sys.executable, "-c", HOLD, str(seconds)], env=env, stdout=subprocess.PIPE, text=True)
    assert proc.stdout.readline() == "held\n"
    proc.stdout.close()
    return proc


class Lines(threading.Thread):
    """A process's stdout, each line with the time it came."""
    def __init__(self, proc):
        super().__init__(daemon=True)
        self.proc, self.lines = proc, []
        self.start()

    def run(self):
        for line in self.proc.stdout:
            self.lines.append((time.time(), line))

    def first(self, pattern):
        return next((t for t, line in self.lines if re.search(pattern, line)), None)

    def text(self):
        return "".join(line for _, line in self.lines)


class OneAtATime(unittest.TestCase):
    setUp = test_bench.Bench.setUp

    def start(self, *args, **env):
        mine = {k: v for k, v in os.environ.items() if k != JOB_ENV}  # by hand, even in a suite run as a job
        proc = subprocess.Popen(["windvane", "-C", str(self.s.root), "bench", *args], env={**mine, **env},
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(lambda: (proc.poll() is None and (proc.kill(), proc.wait()), proc.stdout.close(),
                                 proc.stderr.close()))
        return proc

    def wait_for(self, check, timeout=60):
        end = time.time() + timeout
        while not check() and time.time() < end:
            time.sleep(0.05)
        self.assertTrue(check())

    def test_two_benches_started_together_run_one_after_the_other(self):
        holder = hold(600, job="periodic-weekly")
        procs = [self.start("--hours", "1") for _ in range(2)]
        outs = [Lines(p) for p in procs]
        for out in outs:
            self.wait_for(lambda: out.first("waiting"))
        holder.kill()  # both are waiting when it ends, and race for the lock
        holder.wait()
        for proc, out in zip(procs, outs):
            proc.wait(120)
            out.join(10)
            self.assertEqual(proc.returncode, 0, out.text() + proc.stderr.read())
        for out in outs:
            self.assertEqual(len(re.findall(r"^bench: waiting for other weekly round's bench, running since "
                                            r"\d\d:\d\d \(windvane bench --status to check; Ctrl-C ends the wait, "
                                            r"nothing run\)$",
                                            out.text(), re.M)), 1, out.text())
            self.assertTrue(out.text().startswith("bench: waiting"))  # the week is read, the size picked, after it
        a, b = sorted(outs, key=lambda o: o.first(r": running, log in"))
        self.assertGreater(b.first(r": running, log in"), a.lines[-1][0])  # B's seeds start after A printed its table
        self.assertEqual(len(bench.runs_of(Project(self.s.root))), 2)
        self.assertIsNone(bench.holder())

    def test_a_stop_while_waiting_ends_the_wait_with_nothing_run(self):
        hang = self.s.tmp / "hang"
        a = self.start("--hours", "1", FAKE_HANG=str(hang), FAKE_HANG_ROLE="# windvane worker run",
                       **{JOB_ENV: str(self.s.tmp / "bg/experiment-T-009")})
        self.wait_for(hang.exists)  # A's seed is mid-session
        runs = set((bench.state_root() / "_bench").iterdir())
        status = subprocess.run(["windvane", "-C", str(self.s.root), "bench", "--status"], capture_output=True,
                                text=True, timeout=60).stdout
        self.assertRegex(status, rf"^bench: sandbox T-009's bench, running since \d\d:\d\d \(pid {a.pid}\)\n$")
        b = self.start("--hours", "1")
        out = Lines(b)
        self.wait_for(lambda: out.first("waiting"))
        self.assertRegex(out.text(), r"^bench: waiting for sandbox T-009's bench, running since \d\d:\d\d \(windvane "
                                     r"bench --status to check; Ctrl-C ends the wait, nothing run\)\n$")
        b.send_signal(signal.SIGTERM)
        b.wait(60)
        err = b.stderr.read()
        self.assertEqual(b.returncode, 130, err)
        self.assertEqual(err, "bench: stopped (SIGTERM) while waiting for the machine's bench: nothing ran\n")
        self.assertEqual(set((bench.state_root() / "_bench").iterdir()), runs)  # B made no run dir
        self.assertEqual(bench.holder()["pid"], a.pid)  # A's line is left as it was
        a.send_signal(signal.SIGTERM)
        a.wait(120)
        self.assertIsNone(bench.holder())

    def test_a_lock_left_by_a_dead_process_is_taken_over(self):
        holder = hold(600)
        self.assertEqual(bench.holder()["pid"], holder.pid)
        holder.kill()  # SIGKILL: no cleanup, its line stays in the file
        holder.wait()
        self.assertIn(str(holder.pid), bench.lock_file().read_text())
        self.assertIsNone(bench.holder())
        out = self.start("--hours", "1")
        stdout, err = out.communicate(timeout=120)
        self.assertEqual(out.returncode, 0, stdout + err)
        self.assertNotIn("waiting", stdout)

    def test_a_garbled_lock_file_still_names_a_holder(self):
        bench.lock_file().parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(bench.lock_file(), os.O_CREAT | os.O_RDWR, 0o600)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX)
        for text in ("not json", "[1]", ""):
            with self.subTest(text=text):
                os.ftruncate(fd, 0)
                os.pwrite(fd, text.encode(), 0)
                self.assertEqual(bench.describe(bench.holder()), "another project's bench")

    def test_a_dry_run_doesnt_wait(self):
        holder = hold(600)
        self.addCleanup(lambda: (holder.kill(), holder.wait()))
        out = self.start("--dry-run")
        stdout, err = out.communicate(timeout=60)
        self.assertEqual(out.returncode, 0, stdout + err)
        self.assertNotIn("waiting", stdout)


class ExperimentWaits(unittest.TestCase):
    """An experiment's bench starts only once the machine's bench is free; a stop during that wait isn't a failure."""
    make, run_pilot, log, head, sessions = (te.Experiment.make, te.Experiment.run_pilot, te.Experiment.log,
                                            te.Experiment.head, te.Experiment.sessions)

    def test_it_waits_for_another_projects_bench_then_benches(self):
        self.make()
        holder = hold(2)
        better = {**te.MAIN, "passed": 6}
        pilot = self.run_pilot([better, better])
        holder.wait()
        self.assertEqual(pilot.merged, ["T-001"])
        log = self.log()
        self.assertEqual(len(re.findall(r"^T-001: waiting for other T-009's bench, running since \d\d:\d\d before its "
                                        r"bench starts \(windvane bench --status to check; windvane stop ends the "
                                        r"wait\)$",
                                        log, re.M)), 1, log)
        self.assertLess(log.index("T-001: waiting for other"), log.index("T-001: experiment bench 1 of 2"))

    def test_a_stop_during_the_wait_leaves_it_for_the_next_run(self):
        self.make()
        holder = hold(600)
        self.addCleanup(lambda: (holder.kill(), holder.wait()))
        logf = self.p.state / "autopilot.log"

        def stop():
            end = time.time() + 120
            while time.time() < end and not (logf.exists() and "bench: waiting" in logf.read_text()):
                time.sleep(0.05)
            (self.p.state / "STOP").write_text("")
        threading.Thread(target=stop, daemon=True).start()
        with mock.patch.object(experiment, "bench_command", lambda p, a: self.fail("no bench should start")):
            pilot = driver.Pilot(self.p, te.args())
            pilot.run(now() + timedelta(hours=6))
        self.assertEqual(pilot.merged, [])
        self.assertIn("T-001: the run ended while its bench waited for other T-009's bench, running since",
                      self.log())
        self.assertNotIn("T-001", pilot.failed)
        self.assertIn("T-001", pilot.benching)  # owed: the next run goes straight to its bench


if __name__ == "__main__":
    unittest.main()
