"""R-017: a heavy job can say how long it takes (`--expect`), so a holder or waiter never timed still gives the line
an estimate; recorded times win over it, and `heavy --status` says how long the holder has run and is expected to."""
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from windvane import cli, heavy, jobs  # noqa: E402
from windvane.runner import TICKET  # noqa: E402
from test_heavy_check_wait import hold, machine  # noqa: E402


class HeavyExpect(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="windvane-")
        self.addCleanup(tmp.cleanup)
        self.d = Path(tmp.name)
        patch = mock.patch.dict(os.environ, {**machine(self.d, 60), "XDG_STATE_HOME": str(self.d / "st")})
        patch.start()
        self.addCleanup(patch.stop)
        for k in ("WINDVANE_HEAVY_HELD", heavy.STOP_AT, heavy.STOP_ENV, heavy.DEADLINE_ENV):
            os.environ.pop(k, None)  # a session running the tests has some

    def held(self, ran_min, key="render", expect=None):
        """Another job holds the lock: `key`, started with `expect`, `ran_min` minutes run so far."""
        proc = hold(self, self.d)
        heavy.lock_path().write_text(json.dumps({"pid": proc.pid, "since": time.time() - ran_min * 60,
                                                 "cmd": "render.sh", "cwd": "game/work", "key": key,
                                                 "expect": expect}))

    def line(self, waiters=()):
        """The first line a waiter behind `waiters` ((key, expect) already in line) says."""
        entries = [heavy.join_queue(0, k, e) for k, e in waiters]
        entry = heavy.join_queue(0, "me")
        lock = os.open(heavy.lock_path(), os.O_CREAT | os.O_RDWR, 0o600)
        lines = []
        try:
            end = time.time() + 1.5
            self.assertFalse(heavy.take(lock, entry, 0, {"busy": []}, lambda: end, lines.append))
        finally:
            for e in [*entries, entry]:
                heavy.leave_queue(e)
            os.close(lock)
        return lines[0]

    def test_a_never_timed_holder_with_expect_gives_an_estimate(self):
        self.held(10, expect=30)
        self.assertRegex(self.line(), r"; next in line; about 20 min of work ahead \(until about \d\d:\d\d\)$")
        self.held(10)  # without it, as before
        self.assertTrue(self.line().endswith("next in line; work ahead unknown (never timed)"))

    def test_a_waiter_ahead_with_expect_counts(self):
        self.held(10, expect=30)
        self.assertRegex(self.line([("bake", 15), ("test", None)]),
                         r"; 2 ahead of this job; at least 35 min of work ahead \(until \d\d:\d\d or later; "
                         r"1 never timed\)$")

    def test_a_recorded_time_wins_over_expect(self):
        heavy.record("render", 40)
        self.held(10, expect=30)
        self.assertRegex(self.line([("render", 5)]), r"; 1 ahead of this job; about 70 min of work ahead")  # 30 + 40

    def test_a_holder_past_its_expected_time_says_so(self):
        self.held(45, expect=30)
        self.assertTrue(self.line().endswith("next in line; work ahead unknown (the holder is past its expected "
                                             "30 min)"))
        heavy.record("render", 40)
        self.assertTrue(self.line().endswith("(the holder is past its usual 40 min)"))

    def test_status_shows_the_holders_run_time_and_its_usual_or_expected_time(self):
        self.held(72, expect=40)
        self.assertRegex(heavy.status()[0], r"^held by pid \d+ \(render.sh in game/work, since \d\d:\d\d; "
                                            r"running 72 min, expected 40\)$")
        heavy.record("render", 50)
        self.assertTrue(heavy.status()[0].endswith("; running 72 min, usually 50)"))
        self.held(3, key="never-run")
        self.assertTrue(heavy.status()[0].endswith("; running 3 min, never timed)"))

    def test_run_writes_expect_into_its_queue_entry_and_the_lock(self):
        out = self.d / "lock.json"
        with mock.patch.object(heavy, "join_queue", wraps=heavy.join_queue) as join:
            code = heavy.run(f"cat {heavy.lock_path()} > {out}", wait_s=10, cwd=self.d, mem=0, expect=25)
        self.assertEqual(code, 0)
        self.assertEqual(join.call_args.args[2], 25)
        self.assertEqual(json.loads(out.read_text())["expect"], 25)

    def test_an_older_lock_or_entry_without_expect_reads_as_never_timed(self):
        proc = hold(self, self.d)
        heavy.lock_path().write_text(json.dumps({"pid": proc.pid, "since": time.time() - 60, "cmd": "render.sh",
                                                 "cwd": "game/work", "key": "render"}))
        self.assertTrue(self.line().endswith("next in line; work ahead unknown (never timed)"))
        self.assertTrue(heavy.status()[0].endswith("; running 1 min, never timed)"))
        old = self.d / "entry"
        old.write_text(json.dumps({"mem": 0, "key": "bake"}))
        self.assertEqual(heavy.job_of(old), ("bake", None))

    def test_a_dead_holders_expect_isnt_counted(self):
        proc = hold(self, self.d)
        heavy.lock_path().write_text(json.dumps({"pid": proc.pid, "since": time.time() - 60, "cmd": "render.sh",
                                                 "cwd": "game/work", "key": "render", "expect": 30}))
        proc.kill()
        proc.wait()
        self.assertEqual(heavy.status()[0], "free")
        self.assertRegex(self.line([("bake", 5)]), r"^heavy: the machine lock is free; 1 ahead of this job; about 5 min of work ahead")

    def test_a_queue_entry_keeps_its_expect(self):
        entry = heavy.join_queue(0, "bake", 12.5)
        self.addCleanup(heavy.leave_queue, entry)
        self.assertEqual(heavy.job_of(entry[1]), ("bake", 12.5))
        self.assertEqual(heavy.job_of(self.d / "missing"), (None, None))


class ExpectOption(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="windvane-")
        self.addCleanup(tmp.cleanup)
        self.d = d = Path(tmp.name)
        subprocess.run(["git", "init", "-q", str(d)], check=True)
        (d / ".windvane").mkdir()
        cwd = os.getcwd()
        os.chdir(d)
        self.addCleanup(os.chdir, cwd)

    def test_heavy_passes_expect_to_the_job(self):
        with mock.patch.object(heavy, "run", return_value=0) as run, self.assertRaises(SystemExit):
            cli.main(["heavy", "--expect", "30", "--", "true"])
        self.assertEqual(run.call_args.kwargs["expect"], 30)

    def test_a_bad_expect_is_refused(self):
        for bad in ("0", "-5", "nan", "inf", "soon"):
            with self.subTest(bad), mock.patch("sys.stderr", io.StringIO()) as err, self.assertRaises(SystemExit):
                cli.main(["heavy", "--expect", bad, "--", "true"])
            self.assertIn("use a number of minutes (more than 0)", err.getvalue())

    def test_bg_heavy_expect_passes_it_on_to_the_job(self):
        p = SimpleNamespace(state=self.d / "state")
        with mock.patch.object(jobs.subprocess, "Popen") as popen, redirect_stdout(io.StringIO()), \
                mock.patch.dict(os.environ, {TICKET: "T-1"}):
            jobs.bg(p, "shots", ["make", "shots"], self.d, heavy=True, mem=2.0, expect=45)
        argv = popen.call_args.args[0]
        self.assertEqual(argv[argv.index("heavy"):], ["heavy", "--mem", "2", "--expect", "45", "--", "make", "shots"])
        with self.assertRaisesRegex(SystemExit, "--expect goes with --heavy"):
            cli.main(["bg", "--expect", "45", "shots", "make"])


if __name__ == "__main__":
    unittest.main()
