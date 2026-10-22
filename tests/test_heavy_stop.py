"""T-123: a heavy job's wait hears the project's STOP, a waiter paused at the head of the line doesn't hold up the
jobs behind it, and `windvane heavy --status` shows the holder and the line without waiting."""
import os
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from windvane import heavy, jobs  # noqa: E402
from windvane.config import Project  # noqa: E402
from windvane.runner import TICKET  # noqa: E402
from helpers import Sandbox  # noqa: E402
from test_heavy_check_wait import hold, machine  # noqa: E402

ROOT = Path(__file__).parents[1]
RUN = "import sys; from windvane.heavy import run; sys.exit(run(sys.argv[1], wait_s=float(sys.argv[2])))"
# A job short of free memory for good, polling its 10 s naps as they really run (a second at a time).
MEMWAIT = "import sys; import windvane.heavy as h; h.free_gb=lambda: 0; sys.exit(h.run(sys.argv[1], wait_s=60))"


class HeavyStop(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="windvane-")
        self.addCleanup(tmp.cleanup)
        self.d = d = Path(tmp.name)
        self.stop = d / "STOP"
        self.env = {**os.environ, **machine(d, 60), "PYTHONPATH": str(ROOT), heavy.STOP_ENV: str(self.stop),
                    "XDG_STATE_HOME": str(d / "st")}
        self.env.pop("WINDVANE_HEAVY_HELD", None)
        self.env.pop("WINDVANE_STOP_AT", None)  # a session running the tests has one
        self.order = d / "order"

    def job(self, cmd, code=RUN, env=None, **kw):
        proc = subprocess.Popen([sys.executable, "-c", code, cmd, "60"], env=env or self.env, cwd=self.d,
                                **({"stderr": subprocess.PIPE, "text": True} | kw))
        self.addCleanup(self.end, proc)
        return proc

    @staticmethod
    def end(proc):
        if proc.poll() is None:
            proc.kill()
        proc.wait()
        if proc.stderr:
            proc.stderr.close()

    def until(self, ok, timeout=10):
        end = time.time() + timeout
        while not ok():
            self.assertLess(time.time(), end, "timed out")
            time.sleep(0.05)

    def waiters(self):
        q = self.d / "windvane-heavy.queue"
        return [n for n in sorted(os.listdir(q)) if not n.startswith(".")] if q.exists() else []

    def gives_up_on_stop(self, proc):
        """`proc` exits 75 within a few seconds of the STOP file appearing; its last line."""
        time.sleep(1.5)
        self.assertIsNone(proc.poll(), "gave up before the stop")
        self.stop.touch()
        start = time.time()
        err = proc.communicate(timeout=10)[1]
        self.assertLess(time.time() - start, 3)
        self.assertEqual(proc.returncode, heavy.TIMED_OUT)
        self.assertNotIn("ran", self.order.read_text() if self.order.exists() else "")
        return err.splitlines()[-1]

    def test_a_stop_ends_the_wait_for_the_lock(self):
        holder = hold(self, self.d)
        waiter = self.job(f"echo ran >> {self.order}")
        self.until(lambda: len(self.waiters()) == 1)
        last = self.gives_up_on_stop(waiter)
        self.assertEqual(last, f"{heavy.STOPPED} lock: held by pid {holder.pid} "
                               f"(render.sh in game/work, since {last[-6:-1]})")
        self.assertEqual(self.waiters(), [])

    def test_a_stop_ends_the_wait_for_memory(self):
        (self.d / "cfg/windvane/machine.toml").write_text("busy = []\nmin_free_gb = 1\n")
        waiter = self.job(f"echo ran >> {self.order}", MEMWAIT)
        self.assertEqual(self.gives_up_on_stop(waiter), f"{heavy.STOPPED}: only 0.0 GB free (want 1)")

    def test_a_free_machine_still_runs_and_the_command_doesnt_see_the_stop(self):
        self.stop.touch()
        done = self.job(f'echo "ran $WINDVANE_HEAVY_STOP." >> {self.order}')
        self.assertEqual(done.wait(10), 0)
        self.assertEqual(self.order.read_text(), "ran .\n")

    def test_without_a_stop_path_the_wait_goes_on(self):
        hold(self, self.d)
        self.stop.touch()
        env = {k: v for k, v in self.env.items() if k != heavy.STOP_ENV}
        waiter = self.job("true", env=env)
        time.sleep(2)
        self.assertIsNone(waiter.poll())

    def test_a_check_stopped_while_waiting_is_not_run(self):
        with mock.patch.dict(os.environ, machine(self.d, 1)):
            os.environ.pop("WINDVANE_HEAVY_HELD", None)
            os.environ.pop("WINDVANE_STOP_AT", None)  # a session running the tests has one
            os.environ.pop(TICKET, None)
            holder = hold(self, self.d)
            p = SimpleNamespace(root=self.d, state=self.d / "state", cfg={"check": "echo checked", "heavy_check": True})
            threading.Timer(1.5, lambda: (p.state / "STOP").touch()).start()
            start = time.time()
            ok, summary, log = jobs.check(p, self.d, ticket="T-1")
        self.assertLess(time.time() - start, 5)
        self.assertIsNone(ok)
        self.assertRegex(summary, rf"^CHECK NOT RUN: stopped waiting for the machine lock \(held by pid {holder.pid} "
                                  r"\(render.sh in game/work, since \d\d:\d\d\)\) after 0 min; the run is stopping, "
                                  "so don't run it again now$")
        self.assertNotIn("checked", log.read_text())
        self.assertEqual(jobs.waited(summary), "not run, " + summary[15:summary.index("; the run")])

    def test_a_sessions_check_during_a_stop_says_not_to_retry(self):
        """A worker's `windvane check` after `windvane stop`, the machine busy: not run at once, exit 75, and no
        "run it again" (each retry would be cut the same way)."""
        sb = Sandbox()
        self.addCleanup(sb.close)
        sb.write(".windvane/config.toml", 'name = "sandbox"\nforge = "git"\ncheck = "echo checked"\nheavy_check = true\n')
        sb.git("commit", "-qam", "heavy check")
        hold(self, self.d)
        p = Project(sb.root)
        p.state.mkdir(parents=True, exist_ok=True)
        (p.state / "STOP").touch()
        start = time.time()
        out = subprocess.run(["windvane", "check"], cwd=sb.root, capture_output=True, text=True,
                             env={**os.environ, **machine(self.d, 60)}, timeout=30)
        self.assertLess(time.time() - start, 10)
        self.assertEqual(out.returncode, heavy.TIMED_OUT, out.stderr)
        self.assertIn("CHECK NOT RUN: stopped waiting for the machine lock (held by pid", out.stdout)
        self.assertIn("the run is stopping, so don't run it again now", out.stdout)
        self.assertNotIn("run it again\n", out.stdout)

    @unittest.skipUnless(Path("/proc/self/stat").exists(), "needs /proc")
    def test_a_paused_waiter_at_the_head_lets_the_next_one_in(self):
        holder = hold(self, self.d)
        first = self.job(f"echo first >> {self.order}")
        self.until(lambda: len(self.waiters()) == 1)
        second = self.job(f"echo second >> {self.order}")
        self.until(lambda: len(self.waiters()) == 2)
        first.send_signal(signal.SIGSTOP)
        self.until(lambda: heavy.paused(first.pid))
        (self.d / "release").touch(), holder.wait()
        self.assertEqual(second.wait(10), 0)
        self.assertEqual(self.order.read_text().split(), ["second"])
        self.assertEqual(len(self.waiters()), 1)  # the paused one keeps its place
        first.send_signal(signal.SIGCONT)
        self.assertEqual(first.wait(10), 0)
        self.assertEqual(self.order.read_text().split(), ["second", "first"])
        self.assertEqual(self.waiters(), [])

    def status(self):
        start = time.time()
        out = subprocess.run([sys.executable, "-m", "windvane", "heavy", "--status"], env=self.env, cwd=self.d,
                             capture_output=True, text=True, timeout=10)
        self.assertLess(time.time() - start, 3)  # never waits
        self.assertEqual(out.returncode, 0, out.stderr)
        return out.stdout.splitlines()

    def test_status_lists_the_holder_and_the_line_in_order(self):
        self.assertEqual(self.status(), ["free"])
        held = self.job("touch running; while [ ! -e release ]; do sleep 0.05; done")
        self.until((self.d / "running").exists)
        a = self.job("true")
        self.until(lambda: len(self.waiters()) == 1)
        b = self.job("true")
        self.until(lambda: len(self.waiters()) == 2)
        lines = self.status()
        where = "/".join(self.d.resolve().parts[-2:])
        self.assertEqual(len(lines), 3, lines)
        self.assertRegex(lines[0], re.escape(f"held by pid {held.pid} (touch running; while [ ! -e release ]; do "
                                             f"sleep 0.05; done in {where}, since ")
                         + r"\d\d:\d\d; running under 1 min, never timed\)$")
        self.assertRegex(lines[1], rf"^waiting: pid {a.pid}, since \d\d:\d\d; work ahead unknown \(never timed\)$")
        self.assertRegex(lines[2], rf"^waiting: pid {b.pid}, since \d\d:\d\d; work ahead unknown \(never timed\)$")
        self.assertEqual(len(self.waiters()), 2)  # it took no place in line
        if Path("/proc/self/stat").exists():
            a.send_signal(signal.SIGSTOP)
            self.until(lambda: heavy.paused(a.pid))
            self.assertTrue(self.status()[1].endswith(f" (paused, so skipped: resume it with kill -CONT -{os.getpgid(a.pid)})"))
            a.send_signal(signal.SIGCONT)
        (self.d / "release").touch()
        self.assertEqual([held.wait(10), a.wait(10), b.wait(10)], [0, 0, 0])
        self.assertEqual(self.status(), ["free"])


if __name__ == "__main__":
    unittest.main()
