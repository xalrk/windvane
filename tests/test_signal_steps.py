"""A SIGTERM or SIGHUP never cuts a main-thread git, merge or push step in half: inside `Pilot.critical` it kills the
sessions at once but stops the run only once the step is done. Outside one it stops the run at once, as before. A
run that died without its teardown (SIGKILL, the OOM killer) is torn down by the next run before its setup, and a
teardown that fails at a run's end gets a push that says what to run by hand."""
import contextlib
import io
import os
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
from windvane import driver, selfupdate  # noqa: E402
from windvane.config import Project  # noqa: E402
from windvane.util import now  # noqa: E402
from helpers import Sandbox  # noqa: E402
from test_pipeline import args  # noqa: E402

HOOK = 2  # seconds the merge's hook takes: the signal lands while git is at work


class Base(unittest.TestCase):
    def setUp(self):
        self.addCleanup(os.chdir, os.getcwd())
        self.s = Sandbox()
        self.addCleanup(self.s.close)
        self.s.ticket("T-001")
        self.s.publish()
        self.p = Project(self.s.root)
        self.pushes = []
        orig, driver.push = driver.push, lambda title, msg="", *a, **k: self.pushes.append((title, msg))
        self.addCleanup(setattr, driver, "push", orig)

    def config(self, text):
        cfg = self.s.root / ".windvane/config.toml"
        cfg.write_text(text + cfg.read_text())
        self.s.publish("config")
        self.p = Project(self.s.root)

    def log(self):
        return (self.p.state / "autopilot.log").read_text()


class Merging(Base):
    """A repo with a branch to merge whose pre-merge-commit hook is slow and says when it starts."""

    def setUp(self):
        super().setUp()
        self.repo, self.started = self.s.tmp / "merge-repo", self.s.tmp / "hook-started"
        git = lambda *a: subprocess.run(["git", "-C", str(self.repo), *a], check=True, capture_output=True)  # noqa
        subprocess.run(["git", "init", "-q", "-b", "main", str(self.repo)], check=True, capture_output=True)
        for name, branch in (("a", "main"), ("b", "side"), ("c", "main")):
            if branch == "side":
                git("switch", "-q", "-c", "side")
            elif name == "c":
                git("switch", "-q", "main")
            (self.repo / name).write_text(name)
            git("add", name)
            git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", name)
        hook = self.repo / ".git/hooks/pre-merge-commit"
        hook.write_text(f"#!/bin/sh\n: > {self.started}\nsleep {HOOK}\n")
        hook.chmod(0o755)

    def merge(self):
        """The step: a real `git merge`, SIGTERMed (from another thread, as a shutdown would) while its hook runs."""
        pid = os.getpid()
        threading.Thread(target=lambda: wait_for(self.started) and os.kill(pid, signal.SIGTERM), daemon=True).start()
        subprocess.run(["git", "-C", str(self.repo), "-c", "user.name=t", "-c", "user.email=t@t", "merge", "-q",
                        "--no-ff", "-m", "merge side", "side"], capture_output=True)

    def merged(self):
        parents = subprocess.run(["git", "-C", str(self.repo), "log", "-1", "--format=%P"], capture_output=True,
                                 text=True).stdout.split()
        return len(parents) == 2

    def assertClean(self):
        git_dir = self.repo / ".git"
        self.assertFalse((git_dir / "index.lock").exists())
        self.assertFalse((git_dir / "MERGE_HEAD").exists())
        status = subprocess.run(["git", "-C", str(self.repo), "status", "--porcelain"], capture_output=True, text=True)
        self.assertEqual(status.stdout, "")


class Guard(Merging):
    def setUp(self):
        super().setUp()
        self.pilot = driver.Pilot(self.p, args())
        self.addCleanup(signal.signal, signal.SIGTERM, signal.signal(signal.SIGTERM, self.pilot.on_signal))

    def test_a_signal_inside_a_guarded_merge_lets_it_finish_then_stops(self):
        after = []
        with self.assertRaises(driver.Signalled):
            with self.pilot.critical():
                self.merge()
                after.append("recorded")  # the step's own bookkeeping runs too
        self.assertEqual(after, ["recorded"])
        self.assertTrue(self.merged())
        self.assertClean()
        self.assertTrue(self.pilot.kill_all)  # the sessions were told at once
        self.assertEqual(self.pilot.stop_reason, f"{driver.SIGNALLED[signal.SIGTERM]}; the running sessions were "
                                                 "killed")
        self.assertIn("the step under way (a merge, a push, a pull) finishes first", self.log())

    def test_nested_guards_stop_only_at_the_outermost_end(self):
        with self.assertRaises(driver.Signalled):
            with self.pilot.critical():
                with self.pilot.critical():
                    self.merge()
                self.assertTrue(self.merged())  # still here: the inner end didn't raise
        self.assertClean()

    def test_outside_a_guard_the_signal_stops_at_once(self):
        t = time.monotonic()
        with self.assertRaises(driver.Signalled):
            self.merge()
        self.assertLess(time.monotonic() - t, HOOK)  # git's hook was still sleeping
        self.assertFalse(self.merged())


class Run(Merging):
    def test_a_run_signalled_during_its_self_update_finishes_the_pull_then_stops(self):
        """The guard as the run uses it: the loop's self-update, on the main thread with the run's own handler."""
        done = []

        def pull(home, said):
            self.merge()
            done.append(self.merged())
            return False

        pilot = driver.Pilot(self.p, args())
        pilot.home = self.repo
        with mock.patch.object(selfupdate, "pull", pull), contextlib.redirect_stdout(io.StringIO()):
            pilot.run(now() + timedelta(hours=1))
        self.assertEqual(done, [True])
        self.assertClean()
        stopped = [m for t, m in self.pushes if t == "windvane stopped"]
        self.assertEqual(len(stopped), 1)
        self.assertIn("Why: the driver got SIGTERM (a kill or a shutdown); the running sessions were killed.",
                      stopped[0])
        self.assertIn("Merged: none.", stopped[0])  # no ticket started after the signal
        self.assertNotIn("T-001: worker", self.log())


class Teardown(Base):
    def setUp(self):
        super().setUp()
        self.marker = self.s.tmp / "order"

    def run_once(self):
        with contextlib.redirect_stdout(io.StringIO()):
            driver.Pilot(self.p, args()).run(now() + timedelta(hours=1))

    def dead_pid(self):
        proc = subprocess.Popen(["true"])
        proc.wait()
        return proc.pid

    def lines(self):
        return self.marker.read_text().splitlines() if self.marker.exists() else []

    def test_a_killed_runs_teardown_runs_once_before_the_next_setup(self):
        self.config(f'teardown = "echo teardown >> {self.marker}"\nsetup = "echo setup >> {self.marker}"\n')
        self.p.state.mkdir(parents=True, exist_ok=True)
        dead = self.dead_pid()
        (self.p.state / "lock").write_text(str(dead))  # a driver SIGKILLed mid-run left its pid
        self.run_once()
        got = self.lines()
        self.assertEqual(got[:2], ["teardown", "setup"])
        self.assertEqual(got.count("teardown"), 2)  # that one, and this run's own at its end
        self.assertIn(f"the last run (pid {dead}) ended without its teardown (killed?): running it before setup",
                      self.log())
        self.assertEqual((self.p.state / "lock").read_text(), "")  # this run ended well: nothing left to do
        self.marker.unlink()
        self.run_once()
        self.assertEqual(self.lines().count("teardown"), 1)  # only at its end

    def test_a_failed_teardown_at_the_start_is_in_the_started_push(self):
        self.config('teardown = "echo cannot reach adb; exit 1"\n')
        self.p.state.mkdir(parents=True, exist_ok=True)
        (self.p.state / "lock").write_text(str(self.dead_pid()))
        self.run_once()
        started = next(m for t, m in self.pushes if t == "windvane started")
        self.assertIn("Last run's teardown failed: echo cannot reach adb; exit 1 (exit 1)", started)

    def test_supervise_tearing_down_a_dead_driver_empties_its_pid(self):
        self.config(f'teardown = "echo teardown >> {self.marker}"\n')
        self.p.state.mkdir(parents=True, exist_ok=True)
        (self.p.state / "lock").write_text(str(self.dead_pid()))
        with contextlib.redirect_stdout(io.StringIO()):
            driver.tear_down(self.p, lock=True)
        self.assertEqual(self.lines(), ["teardown"])
        self.assertEqual((self.p.state / "lock").read_text(), "")

    def test_a_failing_teardown_at_the_end_pushes_the_command_to_run(self):
        self.config('teardown = "adb kill-server; exit 3"\n')
        self.run_once()
        titles = [t for t, _ in self.pushes if t.startswith("windvane ")]
        self.assertEqual(titles[-2:], ["windvane stopped", "windvane teardown failed"])  # after the summary
        self.assertEqual(self.pushes[-1][1], "Its helpers may still be up (exit 3). Run it by hand: adb kill-server; "
                                             "exit 3")

    def test_a_runs_lock_holds_its_pid_only_while_it_runs(self):
        """For the next run to read if this one dies: a run that ends, torn down or not, empties it."""
        seen = []
        orig = driver.Pilot.loop
        with mock.patch.object(driver.Pilot, "loop", lambda self: seen.append(
                (self.p.state / "lock").read_text()) or orig(self)), contextlib.redirect_stdout(io.StringIO()):
            driver.Pilot(self.p, args()).run(now() + timedelta(hours=1))
        self.assertEqual(seen, [str(os.getpid())])
        self.assertEqual((self.p.state / "lock").read_text(), "")


def wait_for(path, seconds=20):
    until = time.monotonic() + seconds
    while not path.exists() and time.monotonic() < until:
        time.sleep(0.02)
    return path.exists()


if __name__ == "__main__":
    unittest.main()
