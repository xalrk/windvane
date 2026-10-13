"""R-010, R-011, R-013: a heavy job waits for the machine until its session's stop (`WINDVANE_STOP_AT`), not
machine.toml's `heavy_wait`, so it never gives up its place in line and requeues at the back; `--wait` still wins,
and a command run by hand (no stop in its environment) waits `heavy_wait` as before. The driver's heavy check waits
once, in place, until the ticket's stop."""
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from windvane import driver, heavy  # noqa: E402
from windvane.config import Project  # noqa: E402
from windvane.util import now, read_jsonl  # noqa: E402
from helpers import Sandbox  # noqa: E402
from test_heavy_check_wait import hold, machine  # noqa: E402
from test_parallel import args  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
RUN = ("import sys; from windvane.heavy import run; "
       "sys.exit(run(sys.argv[1], wait_s=None if sys.argv[2] == '-' else float(sys.argv[2])))")
HEAVY_WAIT = 0.02  # minutes: 1.2 s


class Deadline(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="windvane-")
        self.addCleanup(tmp.cleanup)
        self.d = Path(tmp.name)
        self.env = {**os.environ, **machine(self.d, HEAVY_WAIT), "PYTHONPATH": str(ROOT)}
        for k in ("WINDVANE_HEAVY_HELD", heavy.STOP_AT, heavy.STOP_ENV):
            self.env.pop(k, None)

    def job(self, stop_at=None, wait_s=None):
        """A heavy job behind the holder, writing `ran` once it gets the machine: (exit code, stderr, seconds)."""
        env = {**self.env, **({heavy.STOP_AT: str(stop_at)} if stop_at is not None else {})}
        t = time.time()
        r = subprocess.run([sys.executable, "-c", RUN, f"touch {self.d / 'ran'}", "-" if wait_s is None else str(wait_s)],
                           env=env, capture_output=True, text=True, timeout=60)
        return r.returncode, r.stderr, time.time() - t

    def release_in(self, seconds):
        timer = threading.Timer(seconds, (self.d / "release").touch)
        timer.start()
        self.addCleanup(timer.cancel)

    def test_a_session_job_outlives_heavy_wait_and_runs_when_the_holder_ends(self):
        hold(self, self.d)
        self.release_in(4)  # > heavy_wait's 1.2 s
        code, err, secs = self.job(stop_at=time.time() + 30)
        self.assertEqual(code, 0, err)
        self.assertTrue((self.d / "ran").exists())
        self.assertGreater(secs, 3)
        self.assertNotIn(heavy.GAVE_UP, err)
        self.assertRegex(err, r"heavy: waited \d+ s for the machine")

    def test_a_passed_stop_gives_up(self):
        hold(self, self.d)
        code, err, secs = self.job(stop_at=time.time() - 1)
        self.assertEqual(code, heavy.TIMED_OUT)
        self.assertIn(f"{heavy.GAVE_UP} lock: held by pid", err)
        self.assertFalse((self.d / "ran").exists())

    def test_the_stop_ends_the_wait_at_its_time(self):
        hold(self, self.d)
        code, err, secs = self.job(stop_at=time.time() + 3)
        self.assertEqual(code, heavy.TIMED_OUT)
        self.assertGreater(secs, 2.5)
        self.assertLess(secs, 10)

    def test_wait_beats_the_stop(self):
        hold(self, self.d)
        code, err, secs = self.job(stop_at=time.time() + 60, wait_s=1)
        self.assertEqual(code, heavy.TIMED_OUT)
        self.assertLess(secs, 10)
        self.assertIn(heavy.GAVE_UP, err)

    def test_without_either_heavy_wait_applies(self):
        hold(self, self.d)
        code, err, secs = self.job()
        self.assertEqual(code, heavy.TIMED_OUT)
        self.assertLess(secs, 10)
        self.assertIn(heavy.GAVE_UP, err)

    def test_a_bad_stop_falls_back_to_heavy_wait(self):
        hold(self, self.d)
        code, _, secs = self.job(stop_at="soon")
        self.assertEqual(code, heavy.TIMED_OUT)
        self.assertLess(secs, 10)


class DriverCheckKeepsItsPlace(unittest.TestCase):
    def setUp(self):
        self.s = Sandbox()
        self.addCleanup(self.s.close)
        self.d = Path(tempfile.mkdtemp(dir=self.s.tmp))
        os.environ.update(machine(self.d, HEAVY_WAIT))
        self.s.ticket("T-001")
        self.s.write(".windvane/config.toml", (self.s.root / ".windvane/config.toml").read_text()
                     .replace('check = "test -f README"', 'check = "test -f README"\nheavy_check = true'))
        self.s.publish()
        for k, v in {"NAP": 0.1, "STASH_WAIT": 0.5}.items():
            patch = mock.patch.object(driver, k, v)
            patch.start()
            self.addCleanup(patch.stop)

    def test_the_check_waits_once_in_place_behind_a_long_holder(self):
        hold(self, self.d)
        pilot = driver.Pilot(Project(self.s.root), args(parallel=1))
        lines, log = [], pilot.log
        pilot.log = lambda msg: (lines.append(msg), log(msg))
        entries, done = set(), threading.Event()

        def watch():  # every queue entry the waiting check ever has; the holder lets go after 4 s of it
            q, first = heavy.queue_dir(), None
            while not done.is_set():
                names = {n for n in (os.listdir(q) if q.exists() else []) if not n.startswith(".")}
                if not (self.d / "release").exists():  # the merged tree's check after it queues too
                    entries.update(names)
                first = first or (names and time.time())
                if first and time.time() - first > 4:
                    (self.d / "release").touch()
                time.sleep(0.05)
        watcher = threading.Thread(target=watch, daemon=True)
        watcher.start()
        try:
            pilot.run(now() + timedelta(minutes=10))
        finally:
            done.set()
            watcher.join()
        self.assertEqual(pilot.merged, ["T-001"])
        self.assertEqual([m for m in lines if "not run" in m], [])
        self.assertEqual(len(entries), 1, entries)  # one place in line for the whole wait, kept past heavy_wait
        checks = [r for r in read_jsonl(pilot.p.state / "ledger.jsonl") if r.get("event") == "check"]
        self.assertEqual([r.get("not_run") for r in checks], [None, None])  # the head, then the merged tree
        self.assertGreater(checks[0].get("waited_s", 0), HEAVY_WAIT * 60)


if __name__ == "__main__":
    unittest.main()
