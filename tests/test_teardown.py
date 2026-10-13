"""The project's `teardown` runs once when a run really ends, whatever ended it, in the driver's own ci worktree:
after the "stopped" push, never holding it up, and a hung one is killed with its children."""
import fcntl
import io
import os
import sys
import threading
import time
import unittest
from argparse import Namespace
from contextlib import redirect_stdout
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from windvane import cli, driver, runner  # noqa: E402
from windvane.config import Project  # noqa: E402
from windvane.util import now  # noqa: E402
from helpers import Sandbox  # noqa: E402
from test_hangs import alive  # noqa: E402
from test_pipeline import args  # noqa: E402
import test_robust  # noqa: E402


class Teardown(unittest.TestCase):
    def setUp(self):
        self.s = Sandbox()
        self.addCleanup(self.s.close)
        self.s.ticket("T-001")
        self.marker = self.s.tmp / "torn"
        self.pushes = []
        orig, driver.push = driver.push, self.push
        self.addCleanup(setattr, driver, "push", orig)

    def push(self, title, msg="", *a, **k):
        if title.startswith("windvane "):  # not the ticket pushes
            self.pushes.append((title, msg, self.marker.exists()))  # whether the teardown had run by then

    def config(self, text):
        cfg = self.s.root / ".windvane/config.toml"
        cfg.write_text(text + cfg.read_text())
        self.s.publish("config")
        self.p = Project(self.s.root)

    def tears(self, extra=""):
        self.config(f'teardown = "echo $PWD >> {self.marker}"\n' + extra)

    def run_until(self, deadline=timedelta(hours=1)):
        self.pilot = driver.Pilot(self.p, args())
        t = time.monotonic()
        with redirect_stdout(io.StringIO()):
            ready = self.pilot.run(now() + deadline)
        return ready, time.monotonic() - t

    def runs(self):
        return self.marker.read_text().splitlines() if self.marker.exists() else []

    def log(self):
        return (self.p.state / "autopilot.log").read_text()

    def test_it_runs_once_in_the_ci_worktree_after_a_merge_and_after_the_push(self):
        self.tears()
        self.run_until()
        self.assertEqual(self.pilot.merged, ["T-001"])
        self.assertEqual(self.runs(), [str(self.p.state / "ci")])
        self.assertEqual([(t, ran) for t, _, ran in self.pushes], [("windvane started", False),
                                                                    ("windvane stopped", False)])
        self.assertIn(f"teardown `echo $PWD >> {self.marker}` done", self.log())
        self.assertEqual(self.s.git("status", "--porcelain"), "")  # the user's checkout is untouched
        self.assertEqual(self.s.git("rev-parse", "--abbrev-ref", "HEAD"), "main")
        self.assertFalse((self.s.root / "T-001.txt").exists())

    def test_it_runs_once_at_the_deadline(self):
        self.tears()
        os.environ["FAKE_HANG"] = str(self.s.tmp / "streamed")  # the worker works on until the deadline kills it
        os.environ["FAKE_HANG_ROLE"] = "# windvane worker run"
        self.addCleanup(runner.__setattr__, "POLL", runner.POLL)
        runner.POLL = 0.2
        _, took = self.run_until(timedelta(seconds=4))
        self.assertEqual(self.pilot.stop_reason, "deadline")
        self.assertEqual(self.pilot.stopped, ["T-001"])
        self.assertEqual(len(self.runs()), 1)
        self.assertLess(took, 30)

    def test_it_runs_once_after_stop_now(self):
        self.tears()
        os.environ["FAKE_HANG"] = str(self.s.tmp / "streamed")
        os.environ["FAKE_HANG_ROLE"] = "# windvane worker run"
        self.addCleanup(runner.__setattr__, "POLL", runner.POLL)
        runner.POLL = 0.2
        self.pilot = driver.Pilot(self.p, args())
        thread = threading.Thread(target=self.pilot.run, args=(now() + timedelta(hours=1),))
        with redirect_stdout(io.StringIO()):
            thread.start()
            for _ in range(200):
                if (self.s.tmp / "streamed").exists():
                    break
                time.sleep(0.05)
            cli.cmd_stop(Namespace(if_running=False, now=True), self.p)  # returns once the run has ended
            thread.join(30)
        self.assertTrue(self.pilot.stop_reason.startswith("stopped now"))
        self.assertEqual(len(self.runs()), 1)

    def test_it_runs_once_after_a_failed_setup_in_a_throwaway_worktree(self):
        self.tears('setup = "echo nope; exit 3"\n')
        ready, _ = self.run_until()
        self.assertFalse(ready)
        self.assertEqual([t for t, *_ in self.pushes], ["windvane couldn't start"])
        self.assertEqual(self.runs(), [str(self.p.state / "teardown")])  # no ci tree was made: one just for it
        self.assertFalse((self.p.state / "teardown").exists())
        self.assertNotIn("teardown", self.s.git("worktree", "list"))
        self.assertEqual(self.s.git("status", "--porcelain"), "")

    def test_a_failing_one_is_logged_and_the_push_still_goes(self):
        self.config(f'teardown = "touch {self.marker}; echo first; echo cannot reach adb; exit 4"\n')
        self.run_until()
        self.assertEqual([t for t, *_ in self.pushes], ["windvane started", "windvane stopped",
                                                         "windvane teardown failed"])  # after the summary went out
        self.assertIn("` failed (exit 4) in ci: cannot reach adb", self.log())
        self.assertIn("its last lines:\nfirst\ncannot reach adb", self.log())
        self.assertFalse(driver.locked(self.p))

    def test_a_hung_one_is_killed_at_teardown_minutes_with_its_children(self):
        child = self.s.tmp / "child.pid"
        self.config(f'teardown = "sleep 600 & echo $! > {child}; echo stopping; sleep 600"\n'
                    "teardown_minutes = 0.05\n")
        _, took = self.run_until()
        self.assertLess(took, 30)
        self.assertEqual([t for t, *_ in self.pushes], ["windvane started", "windvane stopped",
                                                         "windvane teardown failed"])
        self.assertIn("Its helpers may still be up (timed out after 0.05 min). Run it by hand: sleep 600 &",
                      self.pushes[-1][1])
        self.assertIn("` timed out after 0.05 min (killed) in ci: stopping", self.log())
        pid = int(child.read_text())
        for _ in range(50):  # the killed child is reaped by init
            if not alive(pid):
                break
            time.sleep(0.05)
        self.assertFalse(alive(pid))

    def helper_after_run(self, teardown):
        """The pid of a helper the setup starts outside every process group (as a shared emulator is), after a run."""
        pids, helper = self.s.tmp / "helper.pids", self.s.tmp / "helper.py"
        helper.write_text("import os, time\nos.setsid()\ntime.sleep(600)\n")  # setsid(1) is Linux's, not macOS's
        self.config(f'setup = "python3 {helper} >/dev/null 2>&1 & echo $! >> {pids}"\n' + teardown)
        self.run_until()
        pid = int(pids.read_text().split()[0])
        self.addCleanup(lambda: [os.kill(int(q), 9) for q in pids.read_text().split() if alive_pid(int(q))])
        for _ in range(50):
            if not alive_pid(pid):
                break
            time.sleep(0.05)
        return pid

    def test_a_helper_started_outside_the_sessions_outlives_a_run_without_teardown(self):
        self.assertTrue(alive_pid(self.helper_after_run("")))  # the old failure: up for hours after the run

    def test_the_teardown_stops_that_helper(self):
        pids = self.s.tmp / "helper.pids"
        self.assertFalse(alive_pid(self.helper_after_run(f'teardown = "kill $(cat {pids})"\n')))
        self.assertIn("` done", self.log())

    def test_nothing_runs_without_a_teardown(self):
        self.config(f'setup = "touch {self.marker}.setup"\n')
        self.run_until()
        self.assertEqual(self.pilot.merged, ["T-001"])
        self.assertNotIn("teardown", self.log())
        self.assertFalse(self.marker.exists())

    def test_teardown_minutes_is_checked_like_setup_minutes(self):
        cfg = self.s.root / ".windvane/config.toml"
        cfg.write_text("teardown_minutes = 0\n" + cfg.read_text())
        with self.assertRaises(SystemExit) as e:
            Project(self.s.root)
        self.assertIn("teardown_minutes = 0 in config.toml: use a number of minutes above 0", str(e.exception))


def alive_pid(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


class SupervisedTeardown(unittest.TestCase):
    """A crash `supervise` restarts goes on with the run's helpers; only the run's real end tears them down."""
    R = test_robust.Supervisor  # its helpers, not its tests
    tearDown, crash_when, start, supervise, log = R.tearDown, R.crash_when, R.start, R.supervise, R.log

    def setUp(self):
        self.R.setUp(self)
        self.marker = self.s.tmp / "torn"
        self.cfg = self.s.root / ".windvane/config.toml"
        self.base = self.cfg.read_text()
        self.config(f'teardown = "echo x >> {self.marker}"\n')

    def config(self, teardown):
        self.cfg.write_text(teardown + self.base)
        self.s.publish("teardown")
        self.p = Project(self.s.root)

    def give_up(self, teardown, on_stopped=lambda: None):
        """Four crashes in a row with `teardown`; (seconds from the "stopped" push to supervise's return, the log)."""
        if teardown:
            self.config(teardown)
        self.crash_when(lambda pilot: True)
        at = []

        def push(title, msg="", *a, **k):
            self.pushes.append((title, msg))
            if title == "windvane stopped":
                at.append(time.monotonic())
                on_stopped()
        driver.push = push
        self.supervise("--for", "2h")
        return time.monotonic() - at[0], self.log()

    def test_not_between_restarts_only_at_the_end(self):
        crashed = []
        self.crash_when(lambda pilot: pilot.merged == ["T-001"] and not crashed and not crashed.append(1))
        rc, _ = self.supervise("--for", "2h")
        self.assertEqual((rc, len(self.calls)), (0, 2))
        self.assertEqual(self.marker.read_text(), "x\n")

    def test_once_when_supervise_gives_up(self):
        self.crash_when(lambda pilot: True)
        rc, _ = self.supervise("--for", "2h")
        self.assertEqual((rc, len(self.calls)), (1, 4))
        self.assertEqual(self.marker.read_text(), "x\n")

    def test_when_it_gives_up_a_hung_teardown_doesnt_hold_up_the_push(self):
        after, log = self.give_up('teardown = "sleep 600"\nteardown_minutes = 0.05\n')
        self.assertGreater(after, 2.5)  # the push went out before the 3 s teardown was killed
        self.assertIn("teardown `sleep 600` timed out after 0.05 min (killed)", log)

    def test_when_it_gives_up_a_run_started_meanwhile_keeps_its_helpers(self):
        held = []

        def new_run():  # takes the lock in the gap between the dead driver and the teardown
            held.append(open(self.p.state / "lock", "a"))
            fcntl.flock(held[0], fcntl.LOCK_EX | fcntl.LOCK_NB)
        _, log = self.give_up(None, new_run)
        held[0].close()
        self.assertFalse(self.marker.exists())
        self.assertIn("teardown not run: a new run has started and uses the helpers", log)


if __name__ == "__main__":
    unittest.main()
