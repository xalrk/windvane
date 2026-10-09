"""R-007: the heavy lock goes to whoever waited longest, not to a job that arrives just as it frees."""
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).parents[1]
HOLD = "import fcntl,sys,time; f=open(sys.argv[1],'w'); fcntl.flock(f,fcntl.LOCK_EX); open(sys.argv[2],'w'); time.sleep(2)"
# A job whose free memory is short until a flag file appears, polling fast instead of every 10 s.
MEMWAIT = ("import os,sys,time; import windvane.heavy as h; s=time.sleep; h.time.sleep=lambda t: s(min(t,0.1)); "
           "h.free_gb=lambda: 100 if os.path.exists(sys.argv[3]) else 0; sys.exit(h.run(sys.argv[1], wait_s=30))")
RUN = "import sys; from windvane.heavy import run; sys.exit(run(sys.argv[1], wait_s=float(sys.argv[2])))"


class HeavyFifo(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="windvane-")
        self.addCleanup(tmp.cleanup)
        self.d = d = Path(tmp.name)
        (d / "cfg/windvane").mkdir(parents=True)
        (d / "cfg/windvane/machine.toml").write_text("busy = []\nmin_free_gb = 0\n")
        self.env = {**os.environ, "XDG_RUNTIME_DIR": str(d), "XDG_CONFIG_HOME": str(d / "cfg"),
                    "PYTHONPATH": str(ROOT)}
        self.env.pop("WINDVANE_HEAVY_HELD", None)
        self.order = d / "order"

    def job(self, cmd, wait_s=30, **kw):
        return subprocess.Popen([sys.executable, "-c", RUN, cmd, str(wait_s)], env=self.env,
                                **({"stderr": subprocess.DEVNULL} | kw))

    def hold(self, seconds=2):
        """Another process holds the machine lock for a while (as an older windvane would: no holder line)."""
        hold = HOLD.replace("time.sleep(2)", f"time.sleep({seconds})")
        holder = subprocess.Popen([sys.executable, "-c", hold, str(self.d / "windvane-heavy.lock"), str(self.d / "held")])
        self.until(lambda: (self.d / "held").exists())
        return holder

    def until(self, ok, timeout=10):
        end = time.time() + timeout
        while not ok():
            self.assertLess(time.time(), end, "timed out")
            time.sleep(0.05)

    def queue(self):
        q = self.d / "windvane-heavy.queue"
        return sorted(os.listdir(q)) if q.exists() else []

    def test_the_longest_waiter_runs_first(self):
        holder = self.hold()
        early = self.job(f"echo early >> {self.order}")  # waits on the holder
        holder.wait()
        time.sleep(0.5)
        late = self.job(f"echo late >> {self.order}")    # arrives after the lock freed, while `early` still waits
        early.wait(30), late.wait(30)
        self.assertEqual(self.order.read_text().split(), ["early", "late"])
        self.assertEqual(self.queue(), [])  # nothing left behind

    def test_a_killed_waiter_at_the_head_does_not_block_the_next(self):
        holder = self.hold(seconds=60)
        first = self.job(f"echo first >> {self.order}")
        self.until(lambda: len(self.queue()) == 1 and not self.queue()[0].startswith("."))
        second = self.job(f"echo second >> {self.order}")
        self.until(lambda: len([n for n in self.queue() if not n.startswith(".")]) == 2)
        first.send_signal(signal.SIGKILL)
        first.wait()
        holder.kill(), holder.wait()
        self.assertEqual(second.wait(30), 0)
        self.assertEqual(self.order.read_text().split(), ["second"])
        self.assertEqual(self.queue(), [])

    def test_the_waiting_line_names_the_holder_and_who_is_ahead(self):
        running = self.d / "running"
        held = self.job("touch running; sleep 3; echo held >> order", cwd=self.d)
        self.until(running.exists)
        ahead = self.job(f"echo ahead >> {self.order}")
        self.until(lambda: len(self.queue()) == 1)
        waiter = self.job(f"echo waiter >> {self.order}", stderr=subprocess.PIPE, text=True)
        err = waiter.communicate(timeout=30)[1]
        held.wait(30), ahead.wait(30)
        where = "/".join(self.d.resolve().parts[-2:])
        self.assertRegex(err.splitlines()[0], re.escape(f"heavy: waiting for the machine lock: held by pid {held.pid} "
                         f"(touch running; sleep 3; echo held >> order in {where}, since ") + r"\d\d:\d\d\); "
                         "1 ahead of this job$")
        self.assertEqual(err.count("1 ahead of this job"), 1)  # reprinted only when it changes
        self.assertEqual(self.order.read_text().split(), ["held", "ahead", "waiter"])
        self.assertEqual((self.d / "windvane-heavy.lock").read_text(), "")  # the holder's line goes with it

    def test_giving_up_drops_the_queue_entry(self):
        holder = self.hold(seconds=60)
        try:
            gave_up = self.job("true", wait_s=1, stderr=subprocess.PIPE, text=True)
            err = gave_up.communicate(timeout=30)[1]
            self.assertEqual(gave_up.returncode, 75)
            self.assertIn("held by another heavy job; next in line", err)
            self.assertEqual(self.queue(), [])
        finally:
            holder.kill(), holder.wait()

    def test_a_holder_waiting_on_memory_keeps_its_place(self):
        (self.d / "cfg/windvane/machine.toml").write_text("busy = []\nmin_free_gb = 1\n")
        mem = lambda tag: subprocess.Popen([sys.executable, "-c", MEMWAIT, f"echo {tag} >> {self.order}", "30",
                                            str(self.d / "free")], env=self.env, stderr=subprocess.DEVNULL)
        first = mem("first")
        lock = self.d / "windvane-heavy.lock"
        self.until(lambda: lock.exists() and b'"pid"' in lock.read_bytes())  # holds it, waiting on memory
        later = mem("later")
        self.until(lambda: len(self.queue()) == 1)
        time.sleep(0.5)
        (self.d / "free").touch()
        first.wait(30), later.wait(30)
        self.assertEqual(self.order.read_text().split(), ["first", "later"])

    def test_a_nested_run_skips_the_lock_and_the_queue(self):
        holder = self.hold(seconds=60)
        try:
            self.env["WINDVANE_HEAVY_HELD"] = str(self.d / "windvane-heavy.lock")  # as the holder's own command sees it
            started = time.time()
            self.assertEqual(self.job(f"echo nested >> {self.order}", wait_s=5).wait(30), 0)
            self.assertLess(time.time() - started, 4)
            self.assertEqual(self.order.read_text().split(), ["nested"])
            self.assertFalse((self.d / "windvane-heavy.queue").exists())
        finally:
            holder.kill(), holder.wait()


if __name__ == "__main__":
    unittest.main()
