"""R-011..R-014: a heavy job keeps its run time, a waiter says how much work is ahead of it and, in a session, when
that likely puts its start past the wrap-up; one waiting on memory names the processes holding the most."""
import json
import os
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
from test_heavy_check_wait import hold, machine  # noqa: E402

GB = 1048576  # kB


class HeavyEstimate(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="windvane-")
        self.addCleanup(tmp.cleanup)
        self.d = Path(tmp.name)
        env = {**machine(self.d, 60), "XDG_STATE_HOME": str(self.d / "st")}
        patch = mock.patch.dict(os.environ, env)
        patch.start()
        self.addCleanup(patch.stop)
        for k in ("WINDVANE_HEAVY_HELD", heavy.STOP_AT, heavy.STOP_ENV, heavy.DEADLINE_ENV):
            os.environ.pop(k, None)  # a session running the tests has some

    def times(self, **usual):
        """Record each job's run times: key=[minutes, ...]."""
        for key, runs in usual.items():
            for m in runs:
                heavy.record(key, m)

    def held(self, key, ran_min):
        """Another job holds the lock, its line naming `key` and `ran_min` minutes run so far."""
        proc = hold(self, self.d)
        heavy.lock_path().write_text(json.dumps({"pid": proc.pid, "since": time.time() - ran_min * 60,
                                                 "cmd": "render.sh", "cwd": "game/work", "key": key}))
        return proc

    def take(self, need=0, key="me", seconds=1.5, waiters=()):
        """What a waiter behind `waiters` (their keys, already in line) says in `seconds`; whether it got the lock."""
        entries = [heavy.join_queue(0, k) for k in waiters]
        entry = heavy.join_queue(need, key)
        lock = os.open(heavy.lock_path(), os.O_CREAT | os.O_RDWR, 0o600)
        lines = []
        try:
            end = time.time() + seconds
            got = heavy.take(lock, entry, need, {"busy": []}, lambda: end, lines.append)
        finally:
            for e in [*entries, entry]:
                heavy.leave_queue(e)
            os.close(lock)
        return lines, got

    def test_recorded_times_give_the_work_ahead_holder_and_waiters_counted(self):
        self.times(render=[9, 10, 30], bake=[5], test=[7])  # the holder's usual: the median, 10
        self.held("render", ran_min=2)
        lines, got = self.take(waiters=["bake", "test"])
        self.assertFalse(got)
        self.assertRegex(lines[0], r"held by pid \d+ \(render.sh in game/work, since \d\d:\d\d\); 2 ahead of this job; "
                                   r"about 20 min of work ahead \(until about \d\d:\d\d\)$")  # 8 + 5 + 7

    def test_a_holder_past_its_usual_time_counts_none_left(self):
        self.times(render=[10])
        self.held("render", ran_min=15)
        lines, _ = self.take()
        self.assertIn("next in line; under 1 min of work ahead", lines[0])

    def test_an_unseen_command_says_unknown(self):
        self.held("never-run", ran_min=1)
        self.assertTrue(self.take()[0][0].endswith("next in line; work ahead unknown (never timed)"))
        self.times(bake=[5])
        lines, _ = self.take(waiters=["bake"])
        self.assertRegex(lines[0], r"at least 5 min of work ahead \(until \d\d:\d\d or later; 1 never timed\)$")

    def test_a_late_start_says_so_once_in_a_session(self):
        self.times(render=[40])
        self.held("render", ran_min=5)  # about 35 min left; the wrap-up is in 10
        os.environ[heavy.STOP_AT] = str(time.time() + 30 * 60)
        wrap = time.strftime("%H:%M", time.localtime(time.time() + 10 * 60))
        lines, got = self.take(seconds=2.5)
        self.assertFalse(got)
        self.assertEqual([x for x in lines if x.startswith("heavy: likely")],
                         [f"heavy: likely to start after your wrap-up ({wrap})"])
        self.assertTrue(lines[0].startswith("heavy: waiting for the machine lock:"))  # the estimate comes first

    def test_an_early_start_or_no_session_says_nothing_of_the_wrap_up(self):
        self.times(render=[12])
        self.held("render", ran_min=5)  # 7 min left
        os.environ[heavy.STOP_AT] = str(time.time() + 60 * 60)
        self.assertFalse([x for x in self.take()[0] if "wrap-up" in x])
        os.environ.pop(heavy.STOP_AT)
        self.times(render=[200, 200])
        self.assertFalse([x for x in self.take()[0] if "wrap-up" in x])

    def test_an_unknown_wait_says_nothing_of_the_wrap_up(self):
        self.held("never-run", ran_min=1)
        os.environ[heavy.STOP_AT] = str(time.time() + 21 * 60)
        self.assertFalse([x for x in self.take()[0] if "wrap-up" in x])

    def fake_proc(self):
        proc = self.d / "proc"
        proc.mkdir()
        (proc / "meminfo").write_text(f"MemTotal: {32 * GB} kB\nMemAvailable: {int(3.2 * GB)} kB\n")
        for pid, comm, cwd, gb in [(100, "qemu-system-x86", "other-app/emu", 3.6), (101, "java", "proj/slot-1", 1.5),
                                   (102, "node", "proj/web", 0.4), (103, "bash", "home/me", 0.01),
                                   (104, "firefox", None, 2.2)]:
            p = proc / str(pid)
            p.mkdir()
            (p / "comm").write_text(comm + "\n")
            (p / "status").write_text(f"Name:\t{comm}\nPPid:\t1\nVmRSS:\t{int(gb * GB)} kB\n")
            if cwd:  # 104 is someone else's: its folder can't be read
                (self.d / "fs" / cwd).mkdir(parents=True)
                os.symlink(self.d / "fs" / cwd, p / "cwd")
        patch = mock.patch.object(util, "PROC", proc)
        patch.start()
        self.addCleanup(patch.stop)

    def test_a_memory_wait_names_the_largest_processes(self):
        self.fake_proc()
        lines, got = self.take(need=6)
        self.assertFalse(got)
        self.assertEqual(lines[0], "heavy: waiting: only 3.2 GB free (want 6); largest: "
                                   "pid 100 qemu-system-x86 3.6 GB in other-app/emu, pid 104 firefox 2.2 GB in ?, "
                                   "pid 101 java 1.5 GB in proj/slot-1")
        self.assertEqual(lines[1:], [f"{heavy.GAVE_UP}: only 3.2 GB free (want 6)"])

    def test_status_shows_the_estimate_and_who_holds_the_memory(self):
        self.times(render=[10])
        self.held("render", ran_min=4)
        mine = heavy.join_queue(0, "bake")
        self.addCleanup(heavy.leave_queue, mine)
        lines = heavy.status()
        self.assertRegex(lines[1], r"^waiting: pid \d+, since \d\d:\d\d; about 6 min of work ahead")
        self.fake_proc()  # the same waiter, short of memory now
        with mock.patch.object(heavy, "need_of", lambda path: 6):
            lines = heavy.status()
        self.assertIn("(waiting for 6 GB, 3.2 free; letting smaller jobs by)", lines[1])
        self.assertEqual(lines[2:], ["  holds memory: pid 100 qemu-system-x86 3.6 GB in other-app/emu",
                                     "  holds memory: pid 104 firefox 2.2 GB in ?",
                                     "  holds memory: pid 101 java 1.5 GB in proj/slot-1"])

    def test_a_run_keeps_its_time_and_the_history_stays_bounded(self):
        self.assertEqual(heavy.run(["sh", "-c", "exit 3"], wait_s=5, cwd=str(self.d)), 3)
        key = heavy.job_key(["sh", "-c", "exit 3"], str(self.d))
        self.assertEqual(list(heavy.load_times()), [key])
        for m in range(1, 9):
            heavy.record("render", m)
        self.assertEqual(heavy.load_times()["render"], [4, 5, 6, 7, 8])  # the last KEEP_RUNS
        for i in range(heavy.KEEP_CMDS + 10):
            heavy.record(f"job {i}", 1)
        heavy.record("job 50", 2)  # run again: now the most recent
        times = heavy.load_times()
        self.assertEqual(len(times), heavy.KEEP_CMDS)
        self.assertNotIn("render", times)
        self.assertEqual(times["job 50"], [1, 2])
        self.assertNotIn("job 3", times)  # among the oldest
        self.assertEqual(list(times)[-1], "job 50")

    def test_every_slot_of_a_project_times_the_same_job(self):
        git = lambda *a: subprocess.run(["git", "-C", str(self.d / "repo"), *a], check=True, capture_output=True)
        (self.d / "repo").mkdir()
        git("init", "-q", "-b", "main")
        git("-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "--allow-empty", "-m", "x")
        git("worktree", "add", "-q", "--detach", str(self.d / "slot"))
        self.assertEqual(heavy.job_key("make  all", str(self.d / "repo")),
                         heavy.job_key("make all", str(self.d / "slot")))


if __name__ == "__main__":
    unittest.main()
