"""The teardown also runs when a signal ends the run: a SIGTERM or SIGHUP unwinds the driver to its own end, and a
driver the system kills (SIGKILL) is torn down by `supervise`. A failed teardown is named in the next run's "started"
push, once. And `run --daily` tears down at the end of each window."""
import contextlib
import io
import os
import pty
import signal
import subprocess
import sys
import threading
import time
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from windvane import cli, driver, runner, schedule, util  # noqa: E402
from windvane.config import Project  # noqa: E402
from windvane.util import now  # noqa: E402
from helpers import Sandbox  # noqa: E402
from test_pipeline import args  # noqa: E402
from test_run_window import Clock, at  # noqa: E402

REPO = Path(__file__).resolve().parents[1]


class Base(unittest.TestCase):
    def setUp(self):
        self.addCleanup(os.chdir, os.getcwd())
        self.s = Sandbox()
        self.addCleanup(self.s.close)
        self.s.ticket("T-001")
        self.marker = self.s.tmp / "torn"
        self.cfg = self.s.root / ".windvane/config.toml"
        self.base = self.cfg.read_text()
        self.pushes = []
        orig, driver.push = driver.push, self.push
        self.addCleanup(setattr, driver, "push", orig)

    def push(self, title, msg="", *a, **k):
        if title.startswith("windvane "):
            self.pushes.append((title, msg, len(self.runs())))  # how many teardowns had run by then
        return title != "windvane teardown failed"  # that one didn't go out: the next "started" push says it

    def config(self, teardown=None):
        teardown = f'teardown = "echo x >> {self.marker}"\n' if teardown is None else teardown
        self.s.git("pull", "-q", "origin", "main")  # a run before merged into it
        self.cfg.write_text(teardown + self.base)
        self.s.publish("config")
        self.p = Project(self.s.root)

    def runs(self):
        return self.marker.read_text().splitlines() if self.marker.exists() else []

    def log(self):
        return (self.p.state / "autopilot.log").read_text()

    def hang_worker(self):
        """The worker works on until it is killed; `streamed` appears once it has started."""
        streamed = self.s.tmp / "streamed"
        os.environ["FAKE_HANG"] = str(streamed)
        os.environ["FAKE_HANG_ROLE"] = "# windvane worker run"
        self.addCleanup(runner.__setattr__, "POLL", runner.POLL)
        runner.POLL = 0.2
        return streamed


def wait_for(path, seconds=20):
    until = time.monotonic() + seconds
    while not path.exists() and time.monotonic() < until:
        time.sleep(0.05)
    return path.exists()


class Supervised(Base):
    def supervise(self, rc):
        self.config()
        self.calls = []
        start = lambda a, env: self.calls.append(a) or rc  # noqa: E731 (the driver, dead of `rc`)
        with contextlib.redirect_stdout(io.StringIO()):
            return driver.supervise(self.p, ["run"], now() + timedelta(hours=1), start=start, sleep=lambda s: None)

    def test_a_driver_killed_by_the_system_is_torn_down_once_after_the_push(self):
        self.assertEqual(self.supervise(-9), -9)
        self.assertEqual(len(self.calls), 1)  # not restarted
        self.assertEqual(self.runs(), ["x"])
        self.assertEqual([(t, n) for t, _, n in self.pushes], [("windvane stopped", 0)])
        self.assertIn("killed (SIGKILL)", self.pushes[0][1])

    def test_a_driver_ended_by_sigterm_tore_down_itself(self):
        self.assertEqual(self.supervise(-signal.SIGTERM), -signal.SIGTERM)
        self.assertEqual(self.runs(), [])  # its own `finally` did; a second one could stop a new run's helpers
        self.assertEqual(self.pushes, [])  # and pushed its own "stopped"


class Foreground(Base):
    def kill_mid_run(self, sig):
        """A foreground run, sent `sig` once its worker is at work; the Pilot."""
        self.config()
        streamed = self.hang_worker()
        pid = os.getpid()
        threading.Thread(target=lambda: wait_for(streamed) and os.kill(pid, sig), daemon=True).start()
        before = signal.getsignal(sig)
        pilot = driver.Pilot(self.p, args())
        t = time.monotonic()
        with contextlib.redirect_stdout(io.StringIO()):
            pilot.run(now() + timedelta(hours=1))
        self.assertLess(time.monotonic() - t, 60)
        self.assertIs(signal.getsignal(sig), before)  # Python's handler is back
        return pilot

    def test_sigterm_ends_the_run_with_its_push_and_teardown(self):
        pilot = self.kill_mid_run(signal.SIGTERM)
        self.assertEqual(pilot.signalled, signal.SIGTERM)
        self.assertEqual([(t, n) for t, _, n in self.pushes], [("windvane started", 0), ("windvane stopped", 0)])
        self.assertIn("Why: the driver got SIGTERM (a kill or a shutdown); the running sessions were killed.",
                      self.pushes[1][1])
        self.assertIn("Failed: none.", self.pushes[1][1])  # the killed ticket isn't failed: the next run resumes it
        self.assertEqual(self.runs(), ["x"])
        self.assertFalse(driver.locked(self.p))

    def test_sighup_too(self):
        pilot = self.kill_mid_run(signal.SIGHUP)
        self.assertEqual(pilot.signalled, signal.SIGHUP)
        self.assertIn("tmux session closed (SIGHUP)", self.pushes[-1][1])
        self.assertEqual(self.runs(), ["x"])

    def cli_run(self, sig, tty=False):
        """`windvane run` as the shell or `supervise` starts it, sent `sig` mid-ticket: (its exit code, its pushes).
        `tty`: it runs on a terminal that is closed first, as a closed terminal window or tmux pane leaves it."""
        self.config()
        streamed = self.hang_worker()
        bin_, sent = self.s.tmp / "notify-bin", self.s.tmp / "notified"
        bin_.mkdir()
        (bin_ / "notify").write_text(f'#!/bin/sh\necho "$2" >> {sent}\n')  # a phone: each push's title
        (bin_ / "notify").chmod(0o755)
        master, slave = pty.openpty() if tty else (None, subprocess.DEVNULL)
        proc = subprocess.Popen([sys.executable, "-m", "windvane", "-C", str(self.s.root), "run", "--for", "1h",
                                 "--parallel", "1", "--no-preflight"], cwd=REPO, stdin=subprocess.DEVNULL,
                                stdout=slave, stderr=slave,
                                env={**os.environ, "PYTHONPATH": str(REPO), "PATH": f"{bin_}:{os.environ['PATH']}"})
        self.addCleanup(lambda: proc.poll() is None and proc.kill())
        if tty:
            os.close(slave)
        self.assertTrue(wait_for(streamed))
        if tty:
            os.close(master)  # a write to the terminal now fails with EIO
        proc.send_signal(sig)
        rc = proc.wait(60)
        return rc, sent.read_text().splitlines() if sent.exists() else []

    def test_the_process_then_dies_of_the_signal(self):
        rc, pushes = self.cli_run(signal.SIGTERM)
        self.assertEqual(rc, -signal.SIGTERM)
        self.assertEqual(pushes, ["Sandbox windvane started", "Sandbox windvane stopped"])
        self.assertEqual(self.runs(), ["x"])
        self.assertIn("the driver got SIGTERM (a kill or a shutdown): stopping now", self.log())

    def test_a_closed_terminal_still_ends_well(self):
        rc, pushes = self.cli_run(signal.SIGHUP, tty=True)
        self.assertEqual(rc, -signal.SIGHUP)
        self.assertEqual(pushes, ["Sandbox windvane started", "Sandbox windvane stopped"])
        self.assertEqual(self.runs(), ["x"])
        self.assertIn("Why: the driver's terminal or tmux session closed (SIGHUP); the running sessions were killed.",
                      self.log())


class FailedTeardown(Base):
    def run_once(self):
        with contextlib.redirect_stdout(io.StringIO()):
            driver.Pilot(self.p, args()).run(now() + timedelta(hours=1))
        return next(m for t, m, _ in self.pushes[::-1] if t == "windvane started")

    def test_the_next_runs_started_push_names_it_once(self):
        self.config('teardown = "echo cannot reach adb; exit 1"\n')
        self.assertNotIn("teardown", self.run_once())
        self.assertIn("` failed (exit 1) in ci: cannot reach adb", self.log())
        self.config("")  # no teardown now: only the push clears the record
        self.assertIn("\nLast run's teardown failed: echo cannot reach adb; exit 1 (exit 1); its helpers may still "
                      "be up.", self.run_once())
        self.assertNotIn("teardown", self.run_once())

    def test_a_timeout_is_named_and_a_good_teardown_clears_it(self):
        self.config('teardown = "sleep 600"\nteardown_minutes = 0.02\n')
        self.run_once()
        self.config()
        self.assertIn("Last run's teardown failed: sleep 600 (timed out after 0.02 min)", self.run_once())
        self.assertFalse((self.p.state / "teardown.json").exists())
        self.config('teardown = "exit 2"\n')
        self.run_once()
        self.assertTrue((self.p.state / "teardown.json").exists())
        self.config()
        self.run_once()  # names it, and its own teardown ends well
        self.assertFalse((self.p.state / "teardown.json").exists())
        self.assertNotIn("teardown", self.run_once())


class Daily(Base):
    """`run --daily`: each window's driver is a run of its own, so each one tears down at its end."""

    def setUp(self):
        super().setUp()
        self.config()
        self.p.state.mkdir(parents=True, exist_ok=True)
        self.clock = Clock(at(0, 12))
        for mod in (schedule, util, cli, driver):
            patch = mock.patch.object(mod, "now", self.clock)
            patch.start()
            self.addCleanup(patch.stop)
        patch = mock.patch.object(cli, "in_tmux", lambda p: False)
        patch.start()
        self.addCleanup(patch.stop)
        self.windows = []

    def start(self, a, env):
        """A window's driver, run here as `supervise` would start it."""
        self.windows.append(len(self.runs()))  # teardowns before it
        os.environ.clear()
        os.environ.update(env)
        if len(self.windows) == 2:  # `windvane stop` during the second: the schedule ends with it
            with contextlib.redirect_stdout(io.StringIO()):
                cli.cmd_stop(argparse_ns(), self.p)
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                cli.main(a)
        except SystemExit as e:
            return e.code if isinstance(e.code, int) else 1
        return 0

    def test_two_windows_tear_down_once_each(self):
        argv = ["-C", str(self.s.root), "run", "--daily", "12:00-20:00", "--parallel", "1", "--no-preflight"]
        window = schedule.first(None, None, None, "12:00-20:00")
        with contextlib.redirect_stdout(io.StringIO()):
            schedule.run(self.p, argv, window, "12:00-20:00", start=self.start, sleep=self.clock.sleep)
        self.assertEqual(self.windows, [0, 1])
        self.assertEqual(self.runs(), ["x", "x"])
        self.assertEqual(self.clock.t.date(), at(1, 12).date())  # the second window was the next day's


def argparse_ns():
    from argparse import Namespace
    return Namespace(if_running=False, now=False)


if __name__ == "__main__":
    unittest.main()
