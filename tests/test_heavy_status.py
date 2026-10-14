"""T-131: `windvane heavy --status` tells a holder whose pid was reused from the real one, marks a paused holder, and
without /proc doesn't run `ps` on every poll."""
import json
import os
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

    def test_a_reused_pid_shows_the_lock_free(self):
        me = os.getpid()
        start = util.started_at(me)
        self.assertIsNotNone(start)
        self.write_lock(pid=me, start=start - 1000)  # a holder killed long ago, its pid now this process's
        self.assertEqual(heavy.status(), ["free"])
        info = json.loads(heavy.lock_path().read_text())
        self.assertIn("lock: free; 1 ahead", heavy.waiting_line(info, 1))
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
        self.assertFalse(line.endswith("(paused: everyone waits on it)"), line)
        job.send_signal(signal.SIGSTOP)
        self.until(lambda: heavy.paused(job.pid))
        line = heavy.status()[0]
        self.assertTrue(line.startswith(f"held by pid {job.pid} "), line)
        self.assertTrue(line.endswith(") (paused: everyone waits on it)"), line)

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


if __name__ == "__main__":
    unittest.main()
