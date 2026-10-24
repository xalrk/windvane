"""T-131: `windvane heavy --status` tells a holder whose pid was reused from the real one, marks a paused holder, and
without /proc doesn't run `ps` on every poll. T-268: it and `await` (`standing`) read each waiter the way its own wait
does, and a wait line's estimate that ran out reads as overdue."""
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from windvane import heavy, util  # noqa: E402
from test_heavy_check_wait import machine  # noqa: E402

ROOT = Path(__file__).parents[1]
RUN = "import sys; from windvane.heavy import run; sys.exit(run(sys.argv[1], wait_s=60))"
# Counts its runs in $PS_COUNT and says the process sleeps.
FAKE_PS = "#!/bin/sh\necho x >> \"$PS_COUNT\"\necho S\n"


class HeavyStatus(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="windvane-")
        self.addCleanup(tmp.cleanup)
        self.d = Path(tmp.name)
        self.env = {**os.environ, **machine(self.d, 60), "PYTHONPATH": str(ROOT), "XDG_STATE_HOME": str(self.d / "st")}
        self.env.pop("WINDVANE_HEAVY_HELD", None)
        self.env.pop("WINDVANE_STOP_AT", None)  # a session running the tests has one
        patch = mock.patch.dict(os.environ, self.env)
        patch.start()
        self.addCleanup(patch.stop)
        heavy._ps_seen.clear()
        self.addCleanup(heavy._ps_seen.clear)

    def write_lock(self, **info):
        heavy.lock_path().write_text(json.dumps({"since": time.time(), "cmd": "make", "cwd": "a/b", **info}))

    def until(self, ok, timeout=10):
        end = time.time() + timeout
        while not ok():
            self.assertLess(time.time(), end, "timed out")
            time.sleep(0.05)
            heavy._ps_seen.clear()  # without /proc (macOS) `paused` keeps an answer PS_KEEP s: a wait asks afresh

    def test_a_reused_pid_shows_the_lock_free(self):
        me = os.getpid()
        start = util.started_at(me)
        self.assertIsNotNone(start)
        self.write_lock(pid=me, start=start - 1000)  # a holder killed long ago, its pid now this process's
        self.assertEqual(heavy.status(), ["free"])
        info = json.loads(heavy.lock_path().read_text())
        self.assertIn("heavy: the machine lock is free; 1 ahead", heavy.waiting_line(info, 1))
        self.write_lock(pid=me, start=start)
        self.assertTrue(heavy.status()[0].startswith(f"held by pid {me} (make in a/b, since "))
        self.write_lock(pid=me)  # an older windvane's line, without a start: the pid alone says
        self.assertTrue(heavy.status()[0].startswith(f"held by pid {me} "))

    def test_a_paused_holder_is_marked(self):
        job = subprocess.Popen([sys.executable, "-c", RUN, "touch running; sleep 30"], env=self.env, cwd=self.d,
                               stderr=subprocess.DEVNULL)

        def end():
            job.send_signal(signal.SIGCONT)
            job.kill()
            job.wait()
        self.addCleanup(end)
        self.until((self.d / "running").exists)
        self.assertIn("start", json.loads(heavy.lock_path().read_text()))
        line = heavy.status()[0]
        self.assertTrue(line.startswith(f"held by pid {job.pid} "), line)
        self.assertNotIn("paused", line)
        job.send_signal(signal.SIGSTOP)
        self.until(lambda: heavy.paused(job.pid))
        line = heavy.status()[0]
        self.assertTrue(line.startswith(f"held by pid {job.pid} "), line)
        self.assertTrue(line.endswith(f"; paused, so everyone waits: resume it with kill -CONT -{os.getpgid(job.pid)})"),
                        line)

    @unittest.skipUnless(shutil.which("ps"), "no ps on this system")
    def test_a_paused_holder_is_marked_through_ps(self):
        with mock.patch.object(util, "PROC", self.d / "no-proc"):  # macOS's branch: its answers kept PS_KEEP s
            self.test_a_paused_holder_is_marked()

    @unittest.skipUnless(Path("/proc/self/stat").exists(), "reads a child's state from /proc")
    def test_the_paused_holders_way_out_resumes_its_command_too(self):
        """Ctrl-Z stops the whole group: waking only `windvane heavy`'s pid would leave its command stopped. The way
        out is typed into whatever shell the person has, so it runs in each one here (dash is Debian's and Ubuntu's
        `sh`, with a stricter `kill` than bash's)."""
        job = subprocess.Popen([sys.executable, "-c", RUN, "echo $$ > child; exec sleep 30"], env=self.env,
                               cwd=self.d, stderr=subprocess.DEVNULL, start_new_session=True)

        def end():
            os.killpg(job.pid, signal.SIGCONT)
            os.killpg(job.pid, signal.SIGKILL)
            job.wait()
        self.addCleanup(end)
        self.until(lambda: (self.d / "child").exists() and (self.d / "child").read_text().strip())
        child = int((self.d / "child").read_text())
        for shell in ("sh", "dash", "bash", "zsh"):
            with self.subTest(shell=shell):
                if not shutil.which(shell):
                    self.skipTest(f"no {shell} here")
                os.killpg(job.pid, signal.SIGSTOP)  # what Ctrl-Z does
                self.until(lambda: heavy.paused(job.pid) and heavy.paused(child))
                way_out = heavy.status()[0].rsplit("resume it with ", 1)[1].rstrip(")")
                ran = subprocess.run([shell, "-c", way_out], capture_output=True, text=True)
                self.assertEqual(ran.returncode, 0, ran.stderr)
                self.until(lambda: not heavy.paused(child))
                self.assertNotIn("paused", heavy.status()[0])

    def test_without_proc_ps_runs_once_per_pid_in_ten_seconds(self):
        bin_ = self.d / "bin"
        bin_.mkdir()
        (bin_ / "ps").write_text(FAKE_PS)
        (bin_ / "ps").chmod(0o755)
        count = self.d / "ps-count"
        os.environ.update(PATH=f"{bin_}{os.pathsep}{os.environ['PATH']}", PS_COUNT=str(count))
        with mock.patch.object(util, "PROC", self.d / "no-proc"):
            self.assertEqual([heavy.paused(4242) for _ in range(20)], [False] * 20)
            self.assertEqual(len(count.read_text().splitlines()), 1)
            heavy.paused(4243)  # another pid asks again
            self.assertEqual(len(count.read_text().splitlines()), 2)
            with mock.patch.object(heavy.time, "monotonic", return_value=time.monotonic() + heavy.PS_KEEP):
                heavy.paused(4242)  # kept no longer than PS_KEEP
            self.assertEqual(len(count.read_text().splitlines()), 3)


class LiveStanding(unittest.TestCase):
    """What `status` and `await` say of a waiter, from one `queue_view`, and the wait loop's own lines (T-268)."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="windvane-")
        self.addCleanup(tmp.cleanup)
        self.d = Path(tmp.name)
        env = {**machine(self.d, 60), "XDG_STATE_HOME": str(self.d / "st")}
        patch = mock.patch.dict(os.environ, env)
        patch.start()
        self.addCleanup(patch.stop)
        for k in ("WINDVANE_HEAVY_HELD", "WINDVANE_STOP_AT", heavy.JOB_ENV):  # a session running the tests has them
            os.environ.pop(k, None)
        self.mem = [9.0]
        for patch in (mock.patch.object(heavy, "free_gb", lambda: self.mem[0]),
                      mock.patch.object(heavy, "load_avg", lambda: None),
                      mock.patch.object(heavy, "holders", lambda n=3: [])):
            patch.start()
            self.addCleanup(patch.stop)

    def proc(self):
        """A live pid, for a holder."""
        p = subprocess.Popen(["sleep", "60"])
        self.addCleanup(lambda: (p.kill(), p.wait()))
        return p

    def hold(self, pid, cmd="make", **info):
        heavy.lock_path().write_text(json.dumps({"pid": pid, "since": time.time(), "cmd": cmd, "cwd": "a/b", **info}))

    def queue(self, need, job=None, key=None):
        with mock.patch.dict(os.environ, {heavy.JOB_ENV: job} if job else {}):
            entry = heavy.join_queue(need, key)
        self.addCleanup(heavy.leave_queue, entry)
        time.sleep(0.001)  # entries are named by arrival time
        return entry

    def test_await_and_status_say_the_same_of_a_job_short_of_memory(self):
        holder = self.proc()
        self.hold(holder.pid)
        self.queue(8, "/st/bg/T-1/eval")
        self.mem[0] = 2.4
        now = heavy.standing("/st/bg/T-1/eval")
        self.assertEqual(len(now), 2, now)
        self.assertRegex(now[0], rf"^now: the machine lock is held by pid {holder.pid} \(make in a/b, since \d\d:\d\d; "
                                 r"running under 1 min, never timed\)$")
        tail = "(waiting for 8 GB, 2.4 free; letting smaller jobs by); work ahead unknown (never timed)"
        self.assertRegex(now[1], rf"^now: this job \(pid {os.getpid()}, queued at \d\d:\d\d\) is next in line ")
        self.assertTrue(now[1].endswith(tail), now[1])
        self.assertTrue(heavy.status()[1].endswith(tail), heavy.status())
        self.assertEqual(heavy.standing("/st/bg/T-1/other"), [])  # no waiter of that job's

    def test_a_job_behind_one_short_of_memory_is_next_and_waits_on_the_holder_alone(self):
        """As `take` counts it: the head short of memory lets it by, so its work ahead is the holder's, not theirs."""
        holder = self.proc()
        self.hold(holder.pid, key="k-make")
        heavy.record("k-make", 10)
        heavy.record("k-big", 50)
        self.queue(8, key="k-big")
        self.queue(1, "/st/bg/T-1/small")
        self.mem[0] = 2.4
        now = heavy.standing("/st/bg/T-1/small")
        self.assertIn(") is next in line; about 10 min of work ahead (until about ", now[1])
        self.mem[0] = 9.0  # the head can go: this one waits behind it, on both
        now = heavy.standing("/st/bg/T-1/small")
        self.assertIn(") is 1 ahead of it; about 60 min of work ahead (until about ", now[1])

    def test_this_projects_helpers_count_as_free_for_its_own_jobs(self):
        holder = self.proc()
        self.hold(holder.pid)
        reuse = (["emu"], [str(self.d / "proj")])
        self.queue(8, "/st/bg/T-1/eval", key=f"{self.d / 'proj'} make")
        self.mem[0] = 2.4
        with mock.patch.object(heavy, "helpers_gb", lambda r: 6.0):
            self.assertIn(") is next in line; work ahead unknown", heavy.standing("/st/bg/T-1/eval", reuse)[1])
        with mock.patch.object(heavy, "helpers_gb", lambda r: 3.0):
            self.assertIn("(waiting for 8 GB, 2.4 free + 3.0 in this project's helpers; letting smaller jobs by)",
                          heavy.standing("/st/bg/T-1/eval", reuse)[1])
            self.assertIn("(waiting for 8 GB, 2.4 free; letting", heavy.standing("/st/bg/T-1/eval")[1])  # not its own

    def test_an_estimate_that_ran_out_reads_as_overdue(self):
        at = time.time()
        past, soon = heavy.clock(at - 50 * 60), heavy.clock(at + 10 * 60)
        self.assertEqual(heavy.overdue(f"next in line; about 10 min of work ahead (until about {past})", at),
                         f"next in line; about 10 min of work ahead (overdue since {past})")
        self.assertEqual(heavy.overdue(f"at least 5 min of work ahead (until {past} or later; 1 never timed)", at),
                         f"at least 5 min of work ahead (overdue since {past}; 1 never timed)")
        line = f"about 10 min of work ahead (until about {soon})"
        self.assertEqual(heavy.overdue(line, at), line)

    def run_take(self, steps, need=8):
        """`take`'s lines for a waiter needing `need` GB with the lock held throughout, `steps` {sleep count: what
        changes then} run as it waits, and its wait given up after the last."""
        entry = self.queue(need)
        lock = os.open(heavy.lock_path(), os.O_CREAT | os.O_RDWR, 0o600)
        self.addCleanup(os.close, lock)
        n, lines = [0], []

        def tick(_):
            n[0] += 1
            steps.get(n[0], lambda: None)()
        with mock.patch.object(heavy.fcntl, "flock", side_effect=OSError), \
                mock.patch.object(heavy.time, "sleep", tick):
            heavy.take(lock, entry, need, {"busy": []}, lambda: 0 if n[0] > max(steps) else float("inf"), lines.append)
        return lines

    def test_the_wait_loop_says_each_change_of_r049s_sequence(self):
        """R-049: a holder exits, another job takes the lock, then the waiter is short of memory. The wait's log says
        each (a killed holder leaves its line in the lock, one that ended empties it): the hour-old line `await`
        showed was its tail, not a line the loop missed."""
        for clean in (False, True):
            with self.subTest(holder_ended_cleanly=clean):
                first, second = self.proc(), self.proc()
                self.hold(first.pid, cmd="tools/check.sh")

                def exits():
                    first.kill()
                    first.wait()
                    if clean:
                        heavy.lock_path().write_text("")
                lines = self.run_take({3: exits, 6: lambda: self.hold(second.pid), 9: lambda: self.mem.__setitem__(0, 2.4)})
                self.mem[0] = 9.0
                waits = [ln for ln in lines if ln.startswith("heavy: waiting")]
                self.assertEqual(len(waits), 3, lines)
                self.assertIn(f"held by pid {first.pid} (tools/check.sh", waits[0])
                self.assertIn(f"held by pid {second.pid} (make", waits[1])
                self.assertTrue(waits[2].startswith("heavy: waiting: only 2.4 GB free (want 8)"), waits[2])


if __name__ == "__main__":
    unittest.main()
