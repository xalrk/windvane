"""A run's edges: a guarded step that fails while a signal waits keeps its error in the log and the stopped push; Ctrl-C
inside a guarded step lets it finish (outside one it stops at once); a failed teardown is told once, not again by the
next run's "started" push; and a scheduled window called off as it starts still pushes a held plan."""
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
from windvane import driver, held, selfupdate  # noqa: E402
from windvane.util import now  # noqa: E402
from test_pipeline import args  # noqa: E402
from test_signal_steps import HOOK, Base, Merging, wait_for  # noqa: E402
import test_schedule_next as sn  # noqa: E402

CTRL_C = f"Why: {driver.SIGNALLED[signal.SIGINT]}; the running sessions were killed."


class Edges(Merging):
    def signal_merge(self, sig, times=1):
        """The fixture's slow merge, with `sig` sent `times` times (a second Ctrl-C half a second after the first)."""
        pid = os.getpid()

        def send():
            if wait_for(self.started):
                for _ in range(times):
                    os.kill(pid, sig)
                    time.sleep(0.5)
        threading.Thread(target=send, daemon=True).start()
        subprocess.run(["git", "-C", str(self.repo), "-c", "user.name=t", "-c", "user.email=t@t", "merge", "-q",
                        "--no-ff", "-m", "merge side", "side"], capture_output=True)

    def run_pilot(self, pull):
        pilot = driver.Pilot(self.p, args())
        pilot.home = self.repo
        with mock.patch.object(selfupdate, "pull", pull), contextlib.redirect_stdout(io.StringIO()):
            pilot.run(now() + timedelta(hours=1))
        return [m for t, m in self.pushes if t == "windvane stopped"]


class StepError(Edges):
    def test_a_guarded_step_that_fails_while_a_sigterm_waits_names_its_error(self):
        def pull(home, said):
            os.kill(os.getpid(), signal.SIGTERM)  # handled at once: waiting when the step then fails
            raise RuntimeError("boom")

        stopped = self.run_pilot(pull)
        self.assertIn("  the step under way failed: RuntimeError: boom", self.log())
        self.assertEqual(len(stopped), 1)
        self.assertIn(f"Why: {driver.SIGNALLED[signal.SIGTERM]}; the running sessions were killed; the step under way "
                      "failed: RuntimeError: boom.", stopped[0])

    def test_a_guarded_step_that_ends_well_adds_nothing(self):
        pilot = driver.Pilot(self.p, args())
        self.addCleanup(signal.signal, signal.SIGTERM, signal.signal(signal.SIGTERM, pilot.on_signal))
        with self.assertRaises(driver.Signalled):
            with pilot.critical():
                os.kill(os.getpid(), signal.SIGTERM)
        self.assertNotIn("failed", pilot.stop_reason)


class CtrlC(Edges):
    def setUp(self):
        super().setUp()
        self.pilot = driver.Pilot(self.p, args())
        self.addCleanup(signal.signal, signal.SIGINT, signal.signal(signal.SIGINT, self.pilot.on_signal))

    def test_ctrl_c_twice_inside_a_guarded_merge_lets_it_finish_then_stops(self):
        with self.assertRaises(driver.Signalled):
            with self.pilot.critical():
                self.signal_merge(signal.SIGINT, times=2)
                self.assertTrue(self.merged())  # still here after both
        self.assertClean()  # no index.lock, no MERGE_HEAD
        self.assertTrue(self.pilot.kill_all)
        self.assertEqual(self.log().count("stopping now"), 1)  # the second one changed nothing

    def test_ctrl_c_outside_a_guard_stops_at_once(self):
        t = time.monotonic()
        with self.assertRaises(KeyboardInterrupt):
            self.signal_merge(signal.SIGINT)
        self.assertLess(time.monotonic() - t, HOOK)  # git's hook was still sleeping
        self.assertFalse(self.merged())
        self.assertIsNone(self.pilot.signalled)  # Python's own Ctrl-C: nothing of on_signal's

    def test_a_run_ctrl_ced_during_its_self_update_finishes_the_pull_then_stops(self):
        done = []

        def pull(home, said):
            self.signal_merge(signal.SIGINT)
            done.append(self.merged())
            return False

        stopped = self.run_pilot(pull)
        self.assertEqual(done, [True])
        self.assertClean()
        self.assertEqual(len(stopped), 1)
        self.assertIn(CTRL_C, stopped[0])
        self.assertNotIn("T-001: worker", self.log())


class TeardownTold(Base):
    def test_a_failed_teardown_is_pushed_once_and_not_again_at_the_next_start(self):
        self.config('teardown = "adb kill-server; exit 3"\n')
        for _ in range(2):
            with contextlib.redirect_stdout(io.StringIO()):
                driver.Pilot(self.p, args()).run(now() + timedelta(hours=1))
        titles = [t for t, _ in self.pushes]
        first = titles.index("windvane started", 1)  # the second run's
        self.assertEqual(titles[:first].count("windvane teardown failed"), 1)  # the first run's end
        self.assertNotIn("teardown", self.pushes[first][1])
        self.assertTrue(driver.load_json(self.p.state / driver.TEARDOWN_FAILED, {}).get("told"))


class CalledOff(unittest.TestCase):
    """ScheduleNext's window driver, without its tests."""
    N, W = sn.ScheduleNext, sn.rw.RunWindow
    setUp, tearDown, sleep, schedule, cli = W.setUp, W.tearDown, W.sleep, W.schedule, W.cli
    start, push, tickets, stopped_pushes, sessions = N.start, N.push, N.tickets, N.stopped_pushes, N.sessions

    def test_a_window_called_off_as_it_starts_pushes_the_held_plan_and_says_why(self):
        self.tickets()
        self.s.git("switch", "-q", "-c", "plan-1")
        self.s.write("plan.txt", "a plan")
        self.s.git("add", "-A")
        self.s.git("commit", "-qm", "a plan the network kept local")
        self.s.git("switch", "-q", "main")
        held.hold(self.p, "plan-1", "push", "network down")
        self.on_start = lambda n: self.cli("cmd_stop", if_running=False, now=False)
        with mock.patch.object(held, "push", self.push):
            self.schedule("--daily", "12:00-20:00")
        self.assertIn("refs/heads/plan-1", self.s.git("ls-remote", "--heads", "origin", "plan-1"))
        self.assertEqual(self.sessions(), [])
        self.assertEqual(self.stopped_pushes(), [
            "Why: asked to stop as this window (daily 12:00-20:00) started; nothing ran; the schedule ends: no later "
            "window starts.\nMerged: none.\nWaiting on you: none.\nFailed: none.\n"])


if __name__ == "__main__":
    unittest.main()
