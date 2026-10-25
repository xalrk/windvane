"""A short heavy job goes before a long one ahead of it in the queue, and a long one is passed over only so long."""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT))
from windvane import heavy  # noqa: E402

HOLD = "import fcntl,os,sys,time; f=open(sys.argv[1],'w'); fcntl.flock(f,fcntl.LOCK_EX); open(sys.argv[2],'w')\n" \
       "while not os.path.exists(sys.argv[3]): time.sleep(0.05)"
# A heavy job that looks at the queue every 0.1 s instead of every second, with `PASS_CAP` (minutes) as given.
RUN = ("import sys,time; import windvane.heavy as h; s=time.sleep; h.time.sleep=lambda t: s(min(t,0.1)); "
       "h.PASS_CAP=float(sys.argv[2]); sys.exit(h.run(sys.argv[1], wait_s=30, cwd=sys.argv[3]))")


class Rule(unittest.TestCase):
    """`passes` and `short_first` on queue entries written by hand."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="windvane-")
        self.addCleanup(tmp.cleanup)
        self.q = Path(tmp.name)
        self.times = {"long": [25, 24, 26], "short": [1, 1.2], "mid": [4]}
        self.n = 0

    def entry(self, key, **fields):
        self.n += 1
        path = self.q / f"{self.n:020d}-{1000 + self.n}"
        path.write_text(json.dumps({"mem": 0, "key": key, "expect": None, "load": None, **fields}))
        return path

    def test_a_short_job_passes_a_long_one(self):
        long, short = self.entry("long"), self.entry("short")
        self.assertTrue(heavy.passes(long, short, self.times))
        self.assertTrue(heavy.lets_by(short, [long], 100, 0, self.times))
        self.assertEqual(heavy.short_first(long, [], [short], 100, 0, self.times), short)

    def test_a_job_not_much_shorter_or_not_short_keeps_its_place(self):
        long, mid = self.entry("long"), self.entry("mid")
        self.assertTrue(heavy.passes(long, mid, self.times))  # 4 min is short, 25 is over 3 times it
        self.assertFalse(heavy.passes(mid, self.entry("short", expect=None), {**self.times, "short": [2]}))
        self.assertFalse(heavy.passes(long, self.entry("big"), {**self.times, "big": [8]}))  # 8 min isn't short

    def test_unknown_times_keep_arrival_order(self):
        never, short = self.entry("never"), self.entry("short")
        self.assertFalse(heavy.passes(never, short, self.times))
        self.assertFalse(heavy.passes(self.entry("long"), self.entry("unseen"), self.times))
        self.assertIsNone(heavy.short_first(never, [], [short], 100, 0, self.times))

    def test_an_expected_time_counts_until_one_is_recorded(self):
        self.assertTrue(heavy.passes(self.entry("new engine", expect=30), self.entry("short"), self.times))

    def test_a_long_job_passed_over_to_its_cap_keeps_its_turn(self):
        short = self.entry("short")
        self.assertTrue(heavy.passes(self.entry("long", passed=13 * 60), short, self.times))  # 13 + 1 <= 15
        self.assertFalse(heavy.passes(self.entry("long", passed=14.5 * 60), short, self.times))
        # A job shorter than the cap is passed over for its own time at most.
        self.assertFalse(heavy.passes(self.entry("mid", passed=3.5 * 60), self.entry("tiny"), {**self.times, "tiny": [1]}))

    def test_a_measurement_is_never_passed(self):
        short = self.entry("short")
        for m in (self.entry("long", load=4), self.entry("long", load=4, hold=True)):
            self.assertFalse(heavy.passes(m, short, self.times))
        holding = self.entry("long", load=4, hold=True)
        self.assertFalse(heavy.lets_by(self.entry("short"), [holding], 100, 99, self.times))

    def test_a_short_job_short_of_memory_isnt_let_by(self):
        long, short = self.entry("long"), self.entry("short", mem=8)
        self.assertIsNone(heavy.short_first(long, [], [short], 4, 0, self.times))

    def test_short_jobs_go_in_their_own_arrival_order(self):
        long, one, two = self.entry("long"), self.entry("short"), self.entry("short")
        self.assertEqual(heavy.short_first(long, [], [one, two], 100, 0, self.times), one)
        self.assertFalse(heavy.lets_by(two, [long, one], 100, 0, self.times))


class Queue(unittest.TestCase):
    """Real waiters: a holder keeps the lock while a long and short jobs queue, then lets go."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="windvane-")
        self.addCleanup(tmp.cleanup)
        self.d = d = Path(tmp.name)
        (d / "cfg/windvane").mkdir(parents=True)
        (d / "cfg/windvane/machine.toml").write_text("busy = []\nmin_free_gb = 0\n")
        self.env = {**os.environ, "XDG_RUNTIME_DIR": str(d), "XDG_CONFIG_HOME": str(d / "cfg"),
                    "XDG_STATE_HOME": str(d / "st"), "PYTHONPATH": str(ROOT)}
        for k in ("WINDVANE_HEAVY_HELD", "WINDVANE_STOP_AT", "WINDVANE_BG_BASE"):
            self.env.pop(k, None)
        self.order = d / "order"

    def cmd(self, name, then=""):
        return f"{then}echo {name} >> {self.order}"

    def timed(self, **minutes):
        """Record run times for the jobs `cmd` makes, as earlier runs would have."""
        f = self.d / "st/windvane/heavy-times.json"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps({heavy.job_key(self.cmd(n, then), str(self.d)): [m]
                                 for n, (m, then) in minutes.items()}))

    def job(self, cmd, cap=15):
        p = subprocess.Popen([sys.executable, "-c", RUN, cmd, str(cap), str(self.d)], env=self.env,
                             stderr=subprocess.PIPE, text=True)
        n = len(self.queued())
        self.until(lambda: len(self.queued()) > n)  # in line before the next one comes
        return p

    def queued(self):
        q = self.d / "windvane-heavy.queue"
        return [n for n in os.listdir(q) if not n.startswith(".")] if q.exists() else []

    def until(self, ok, timeout=15):
        end = time.time() + timeout
        while not ok():
            self.assertLess(time.time(), end, "timed out")
            time.sleep(0.05)

    def hold(self):
        go = self.d / "go"
        holder = subprocess.Popen([sys.executable, "-c", HOLD, str(self.d / "windvane-heavy.lock"),
                                   str(self.d / "held"), str(go)])
        self.until((self.d / "held").exists)
        return holder, go

    def finish(self, *jobs):
        return [p.communicate(timeout=30)[1] for p in jobs]

    def test_a_one_minute_job_runs_before_a_25_minute_one_queued_ahead_of_it(self):
        self.timed(long=(25, ""), short=(1, ""))
        holder, go = self.hold()
        long, short = self.job(self.cmd("long")), self.job(self.cmd("short"))
        time.sleep(0.5)
        go.touch(), holder.wait()
        err_long, err_short = self.finish(long, short)
        self.assertEqual(self.order.read_text().split(), ["short", "long"])
        self.assertIn(f"letting pid {short.pid} by, a short job (1 min; this job 25 min)", err_long)
        self.assertIn(f"going ahead of pid {long.pid} (25 min) as a short job (1 min)", err_short)

    def test_jobs_with_no_recorded_time_keep_arrival_order(self):
        holder, go = self.hold()
        long, short = self.job(self.cmd("long")), self.job(self.cmd("short"))
        time.sleep(0.5)
        go.touch(), holder.wait()
        err_long, err_short = self.finish(long, short)
        self.assertEqual(self.order.read_text().split(), ["long", "short"])
        self.assertNotIn("short job", err_long + err_short)

    def test_a_long_job_passed_over_past_its_cap_keeps_its_turn(self):
        """With a cap of 0.03 min (1.8 s), the first 0.02-min job goes by and runs 1.5 s: a second one would take
        the long job past its cap, so the long job runs next."""
        self.timed(long=(25, ""), one=(0.02, "sleep 1.5; "), two=(0.02, ""))
        holder, go = self.hold()
        jobs = [self.job(self.cmd("long"), cap=0.03), self.job(self.cmd("one", "sleep 1.5; "), cap=0.03),
                self.job(self.cmd("two"), cap=0.03)]
        time.sleep(0.5)
        go.touch(), holder.wait()
        self.finish(*jobs)
        self.assertEqual(self.order.read_text().split(), ["one", "long", "two"])


if __name__ == "__main__":
    unittest.main()
