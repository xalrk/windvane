"""R-012, R-014: a heavy job waits for its own memory need (`--mem`, a project's `heavy_mem`), and the head of the line
waits for it before taking the lock, so a job behind it whose need is met may go first instead of waiting too."""
import io
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
from test_heavy_check_wait import hold  # noqa: E402

ROOT = Path(__file__).parents[1]
# A job whose free memory is the number in a file, polling fast instead of every second. Its need: argv[2] ("-": the
# default, machine.toml's min_free_gb).
MEM = ("import sys,time; import windvane.heavy as h; s=time.sleep; h.time.sleep=lambda t: s(min(t,0.1)); "
       "h.free_gb=lambda: float(open(sys.argv[3]).read()); "
       "sys.exit(h.run(sys.argv[1], wait_s=float(sys.argv[4]), mem=None if sys.argv[2]=='-' else float(sys.argv[2])))")


class HeavyMem(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="windvane-")
        self.addCleanup(tmp.cleanup)
        self.d = d = Path(tmp.name)
        (d / "cfg/windvane").mkdir(parents=True)
        (d / "cfg/windvane/machine.toml").write_text("busy = []\nmin_free_gb = 6\n")
        self.env = {**os.environ, "XDG_RUNTIME_DIR": str(d), "XDG_CONFIG_HOME": str(d / "cfg"),
                    "PYTHONPATH": str(ROOT)}
        for k in ("WINDVANE_HEAVY_HELD", "WINDVANE_STOP_AT", heavy.STOP_ENV, heavy.DEADLINE_ENV):
            self.env.pop(k, None)  # a session running the tests has some
        self.order, self.mem = d / "order", d / "free"
        self.free(4)

    def free(self, gb):
        (self.d / "free.new").write_text(str(gb))
        (self.d / "free.new").replace(self.mem)  # never read half-written

    def job(self, tag, need="-", wait_s=30, **kw):
        proc = subprocess.Popen([sys.executable, "-c", MEM, f"echo {tag} >> {self.order}", str(need), str(self.mem),
                                 str(wait_s)], env=self.env, **({"stderr": subprocess.DEVNULL} | kw))
        self.addCleanup(lambda: proc.poll() is None and (proc.kill(), proc.wait()))
        return proc

    def until(self, ok, timeout=10):
        end = time.time() + timeout
        while not ok():
            self.assertLess(time.time(), end, "timed out")
            time.sleep(0.05)

    def waiters(self):
        q = self.d / "windvane-heavy.queue"
        return [n for n in sorted(os.listdir(q)) if not n.startswith(".")] if q.exists() else []

    def ran(self):
        return self.order.read_text().split() if self.order.exists() else []

    def test_a_small_job_runs_where_a_default_one_waits(self):
        self.assertEqual(self.job("small", 2).wait(30), 0)
        big = self.job("big", wait_s=1, stderr=subprocess.PIPE, text=True)
        err = big.communicate(timeout=30)[1]
        self.assertEqual(big.returncode, heavy.TIMED_OUT)
        self.assertEqual(err.splitlines(), ["heavy: waiting: only 4.0 GB free (want 6)",
                                            f"{heavy.GAVE_UP}: only 4.0 GB free (want 6)"])
        self.assertEqual(self.ran(), ["small"])

    def test_a_small_job_goes_by_a_head_short_of_memory_which_runs_next(self):
        head = self.job("head")
        self.until(lambda: len(self.waiters()) == 1)
        small = self.job("small", 2)
        self.assertEqual(small.wait(30), 0)
        self.assertEqual(self.ran(), ["small"])
        self.assertIsNone(head.poll())
        self.assertEqual(len(self.waiters()), 1)  # still the head, still in line
        with mock.patch.dict(os.environ, {k: self.env[k] for k in ("XDG_RUNTIME_DIR", "XDG_CONFIG_HOME")}), \
                mock.patch.object(heavy, "free_gb", lambda: 4.1):
            lines = heavy.status()
        self.assertEqual(lines[0], "free")  # it waits without the lock
        self.assertRegex(lines[1], rf"^waiting: pid {head.pid}, since \d\d:\d\d "
                                   r"\(waiting for 6 GB, 4\.1 free; letting smaller jobs by\)$")
        self.free(8)
        self.assertEqual(head.wait(30), 0)
        self.assertEqual(self.ran(), ["small", "head"])

    def test_with_memory_for_both_the_line_stays_in_order(self):
        self.free(8)
        (self.d / "windvane-heavy.lock").touch()
        holder = hold(self, self.d)
        head = self.job("head")
        self.until(lambda: len(self.waiters()) == 1)
        small = self.job("small", 2)
        self.until(lambda: len(self.waiters()) == 2)
        (self.d / "release").touch()
        holder.wait(10)
        self.assertEqual([head.wait(30), small.wait(30)], [0, 0])
        self.assertEqual(self.ran(), ["head", "small"])

    def test_two_small_jobs_go_by_one_at_a_time_in_their_order(self):
        head = self.job("head")
        self.until(lambda: len(self.waiters()) == 1)
        hold = self.d / "go"
        first = self.job(f"first >> {self.order}; while [ ! -e {hold} ]; do sleep 0.05; done; echo first-done", 2)
        self.until(lambda: self.ran() == ["first"])
        second = self.job("second", 2)
        self.until(lambda: len(self.waiters()) == 2)
        time.sleep(0.5)
        self.assertEqual(self.ran(), ["first"])  # the lock is first's: second waits for it, not beside it
        hold.touch()
        self.assertEqual([first.wait(30), second.wait(30)], [0, 0])
        self.assertIsNone(head.poll())
        self.free(8)
        self.assertEqual(head.wait(30), 0)
        self.assertEqual(self.ran(), ["first", "first-done", "second", "head"])

    def test_a_job_that_does_not_fit_either_does_not_go_by(self):
        head = self.job("head")
        self.until(lambda: len(self.waiters()) == 1)
        five = self.job("five", 5)
        self.until(lambda: len(self.waiters()) == 2)
        time.sleep(1)
        self.assertEqual(self.ran(), [])
        self.free(8)
        self.assertEqual([head.wait(30), five.wait(30)], [0, 0])
        self.assertEqual(self.ran(), ["head", "five"])


class MemSetting(unittest.TestCase):
    """Where a job's need comes from: `--mem`, else its project's `heavy_mem`, else machine.toml (None here)."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="windvane-")
        self.addCleanup(tmp.cleanup)
        self.d = d = Path(tmp.name)
        subprocess.run(["git", "init", "-q", str(d)], check=True)
        (d / ".windvane").mkdir()
        cwd = os.getcwd()
        os.chdir(d)
        self.addCleanup(os.chdir, cwd)

    def heavy_mem(self, *args):
        with mock.patch.object(heavy, "run", return_value=0) as run, self.assertRaises(SystemExit):
            cli.main(["heavy", *args, "--", "true"])
        return run.call_args.kwargs["mem"]

    def test_heavy_mem_sets_the_default_and_mem_overrides_it(self):
        self.assertIsNone(self.heavy_mem())
        (self.d / ".windvane/config.toml").write_text("heavy_mem = 2\n")
        self.assertEqual(self.heavy_mem(), 2)
        self.assertEqual(self.heavy_mem("--mem", "3.5"), 3.5)

    def test_a_bad_heavy_mem_is_refused(self):
        (self.d / ".windvane/config.toml").write_text('heavy_mem = "lots"\n')
        with mock.patch.object(heavy, "run") as run, self.assertRaisesRegex(SystemExit, "heavy_mem = 'lots'"):
            cli.main(["heavy", "--", "true"])
        run.assert_not_called()

    def test_bg_passes_its_mem_to_heavy(self):
        p = SimpleNamespace(state=self.d / "state")
        with mock.patch.object(jobs.subprocess, "Popen") as popen, redirect_stdout(io.StringIO()), \
                mock.patch.dict(os.environ, {TICKET: "T-1"}):
            jobs.bg(p, "shots", ["make", "shots"], self.d, heavy=True, mem=2.0)
        argv = popen.call_args.args[0]
        self.assertEqual(argv[argv.index("heavy"):], ["heavy", "--mem", "2", "--", "make", "shots"])
        with self.assertRaisesRegex(SystemExit, "--mem goes with --heavy"):
            cli.main(["bg", "--mem", "2", "shots", "make"])

    def test_a_bad_mem_is_refused_as_heavy_mem_is(self):
        for bad in ("-1", "nan", "inf", "lots"):
            with self.subTest(bad), mock.patch("sys.stderr", io.StringIO()) as err, self.assertRaises(SystemExit):
                cli.main(["heavy", "--mem", bad, "--", "true"])
            self.assertIn("use a number of GB (0 or more)", err.getvalue())

    @unittest.skipUnless(heavy.free_gb(), "free memory is only known on Linux")
    def test_a_heavy_check_waits_for_the_projects_heavy_mem(self):
        (self.d / "cfg/windvane").mkdir(parents=True)
        (self.d / "cfg/windvane/machine.toml").write_text("busy = []\nmin_free_gb = 0\nheavy_wait = 0.02\n")
        env = {"XDG_RUNTIME_DIR": str(self.d), "XDG_CONFIG_HOME": str(self.d / "cfg")}
        with mock.patch.dict(os.environ, env):
            for k in ("WINDVANE_HEAVY_HELD", "WINDVANE_STOP_AT", TICKET):
                os.environ.pop(k, None)
            p = SimpleNamespace(state=self.d / "state", cfg={"check": "echo checked", "heavy_check": True,
                                                              "heavy_mem": 10 ** 6})
            ok, summary, log = jobs.check(p, self.d)
            self.assertIsNone(ok)
            self.assertIn("(want 1000000)", summary)
            p.cfg["heavy_mem"] = 0
            ok, _, log = jobs.check(p, self.d)
            self.assertTrue(ok)
            self.assertIn("checked", log.read_text())


if __name__ == "__main__":
    unittest.main()
