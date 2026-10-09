"""Between tickets the driver fast-forwards a clean windvane checkout on main and restarts itself, with the same
deadline, when its own code changed; any other install is left alone, with one log line."""
import io
import subprocess
import sys
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from windvane import driver, selfupdate  # noqa: E402
from windvane.config import Project  # noqa: E402
from windvane.util import now  # noqa: E402
from helpers import Sandbox  # noqa: E402
from test_parallel import args  # noqa: E402


class Restarted(Exception):
    pass


class Install:
    """A fake windvane install: a checkout of its own bare remote, and a second clone that moves the remote's main."""

    def __init__(self, s):
        self.s, self.home, self.up = s, s.tmp / "install", s.tmp / "upstream"
        bare = s.tmp / "install.git"
        s.git("init", "-q", "--bare", "-b", "main", str(bare), cwd=s.tmp)
        s.git("clone", "-q", str(bare), str(self.up), cwd=s.tmp)
        self.commit("windvane/driver.py", "v1\n")
        s.git("clone", "-q", str(bare), str(self.home), cwd=s.tmp)

    def commit(self, rel, text):
        f = self.up / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text)
        self.s.git("add", "-A", cwd=self.up)
        self.s.git("commit", "-qm", rel, cwd=self.up)
        self.s.git("push", "-q", "origin", "HEAD:main", cwd=self.up)
        return self.s.git("rev-parse", "HEAD", cwd=self.up)

    def head(self):
        return self.s.git("rev-parse", "HEAD", cwd=self.home)


class Pull(unittest.TestCase):
    def setUp(self):
        self.s = Sandbox()
        self.i = Install(self.s)

    def tearDown(self):
        self.s.close()

    def test_code_change_fast_forwards_and_asks_for_a_restart(self):
        new = self.i.commit("windvane/driver.py", "v2\n")
        said = []
        self.assertEqual(selfupdate.pull(self.i.home, said), "restart")
        self.assertEqual(self.i.head(), new)
        self.assertEqual(said, [f"windvane updated to {new[:8]}"])

    def test_docs_change_fast_forwards_without_a_restart(self):
        new = self.i.commit("README.md", "docs\n")
        self.assertIsNone(selfupdate.pull(self.i.home, []))
        self.assertEqual(self.i.head(), new)

    def test_up_to_date_says_nothing(self):
        said = []
        self.assertIsNone(selfupdate.pull(self.i.home, said))
        self.assertEqual(said, [])

    def test_dirty_install_is_left_alone(self):
        old = self.i.head()
        self.i.commit("windvane/driver.py", "v2\n")
        (self.i.home / "windvane/driver.py").write_text("my edit\n")
        said = []
        self.assertIsNone(selfupdate.pull(self.i.home, said))
        self.assertEqual(self.i.head(), old)
        self.assertEqual(said, ["windvane not updated: it has local changes"])
        self.assertEqual((self.i.home / "windvane/driver.py").read_text(), "my edit\n")

    def test_other_branch_is_left_alone(self):
        self.s.git("switch", "-q", "-c", "desk-parallel", cwd=self.i.home)
        old = self.i.head()
        self.i.commit("windvane/driver.py", "v2\n")
        said = []
        self.assertIsNone(selfupdate.pull(self.i.home, said))
        self.assertEqual(self.i.head(), old)
        self.assertEqual(said, ["windvane not updated: on branch desk-parallel, not main"])

    def test_local_commits_are_left_alone(self):
        (self.i.home / "mine.txt").write_text("x\n")
        self.s.git("add", "-A", cwd=self.i.home)
        self.s.git("commit", "-qm", "mine", cwd=self.i.home)
        old = self.i.head()
        self.i.commit("windvane/driver.py", "v2\n")
        said = []
        self.assertIsNone(selfupdate.pull(self.i.home, said))
        self.assertEqual(self.i.head(), old)
        self.assertIn("commits origin/main doesn't", said[0])


class Units(unittest.TestCase):
    def test_restart_args_keep_the_deadline_and_flags(self):
        d = datetime(2026, 10, 1, 18, 45)
        self.assertEqual(selfupdate.restart_args(["-C", "/p", "run", "--for", "6h", "--detach", "--parallel", "2"], d),
                         ["-C", "/p", "run", "--parallel", "2", "--until", "2026-10-01 18:45", "--no-preflight"])
        self.assertEqual(selfupdate.restart_args(["run", "--until=17:00", "--no-preflight"], d),
                         ["run", "--until", "2026-10-01 18:45", "--no-preflight"])

    def test_newer_release(self):
        tags = "a1\trefs/tags/v0.2.0\nb2\trefs/tags/v0.10.0\nc3\trefs/tags/v0.10.0^{}\nd4\trefs/tags/nightly\n"
        self.assertEqual(selfupdate.newer_release("0.1.0", tags), "0.10.0")
        self.assertIsNone(selfupdate.newer_release("0.10.0", tags))
        self.assertIsNone(selfupdate.newer_release("0.1.0", ""))  # no tags, or the repo unreachable
        self.assertIsNone(selfupdate.newer_release("0", tags))  # a checkout: no package to compare


class Driver(unittest.TestCase):
    """The driver pulls between tickets: a restart carries the run's deadline; a skipped install logs once."""

    def setUp(self):
        self.s = Sandbox()
        self.i = Install(self.s)
        self.s.ticket("T-001")
        self.s.ticket("T-002")
        self.s.publish()
        self.p = Project(self.s.root)
        self.pilot = driver.Pilot(self.p, args(parallel=1))
        self.pilot.home, self.pilot.argv = self.i.home, ["run", "--for", "6h", "--detach"]
        self.calls = []

        def execv(path, argv):
            self.calls.append(argv)
            raise Restarted()
        self.pilot.execv = execv

    def tearDown(self):
        self.s.close()

    def log(self):
        return (self.p.state / "autopilot.log").read_text()

    def test_clean_install_is_updated_and_the_driver_restarts_with_the_same_deadline(self):
        new = self.i.commit("windvane/driver.py", "v2\n")
        deadline = (now() + timedelta(hours=2)).replace(second=0, microsecond=0)
        with redirect_stdout(io.StringIO()), self.assertRaises(Restarted):
            self.pilot.run(deadline)
        self.assertEqual(self.i.head(), new)
        argv = self.calls[0]
        self.assertEqual(argv[1:], ["run", "--until", f"{deadline:%Y-%m-%d %H:%M}", "--no-preflight"])
        self.assertTrue(argv[0].endswith("windvane"))
        self.assertIn("windvane restarting with its new code", self.log())
        self.assertNotIn("T-001", self.pilot.running)  # restarted between tickets, none was running

    def test_a_failed_restart_ends_the_run_with_its_own_error(self):
        """An exec that fails (a moved or broken interpreter) ends the run with that error in the log and the last
        push, and the teardown still runs under the lock it kept."""
        pushes = []
        orig, driver.push = driver.push, lambda title, msg, *a, **k: pushes.append((title, msg))
        self.addCleanup(setattr, driver, "push", orig)

        def execv(path, argv):
            self.calls.append(argv)
            raise FileNotFoundError(2, "No such file or directory")  # as a real exec's: no file named
        self.pilot.execv = execv
        self.i.commit("windvane/driver.py", "v2\n")
        with redirect_stdout(io.StringIO()), self.assertRaises(FileNotFoundError):
            self.pilot.run(now() + timedelta(hours=2))
        why = f"driver error: [Errno 2] couldn't restart on the new code: No such file or directory: '{self.calls[0][0]}'"
        self.assertIn(why, self.log())
        self.assertNotIn("closed file", self.log())
        [msg] = [m for t, m in pushes if t == "windvane stopped"]
        self.assertIn(why, msg)
        self.assertEqual((self.p.state / "lock").read_text(), "")  # torn down: emptied, then let go
        self.assertNotIn("WINDVANE_RESTARTED", driver.os.environ)
        again = driver.Pilot(self.p, args(parallel=1))
        again.take_lock()
        again.lockfile.close()

    def test_dirty_install_is_left_alone_with_one_log_line(self):
        self.i.commit("windvane/driver.py", "v2\n")
        old = self.i.head()
        (self.i.home / "windvane/driver.py").write_text("my edit\n")
        with redirect_stdout(io.StringIO()):
            self.pilot.run(now() + timedelta(hours=2))
        self.assertEqual(self.pilot.merged, ["T-001", "T-002"])  # it pulled between both tickets, and went on
        self.assertEqual(self.i.head(), old)
        self.assertEqual(self.log().count("windvane not updated: it has local changes"), 1)
        self.assertEqual(self.calls, [])

    def test_the_restarted_driver_goes_on_with_the_run(self):
        """What the run did before the restart counts after it: a failed ticket isn't picked again, the replan
        budget isn't reset, and the final summary lists the merges from before."""
        self.s.ticket("T-003")
        self.s.write(".windvane/QUEUE.md", "# Queue\n- done: T-001 (merged before the restart)\n")
        self.s.publish("one more")
        self.pilot.merged, self.pilot.failed, self.pilot.skip, self.pilot.plans = ["T-001"], ["T-002"], {"T-002"}, 1
        self.i.commit("windvane/driver.py", "v2\n")
        self.pilot.take_lock()  # as run() would have
        self.assertFalse(driver.os.get_inheritable(self.pilot.lockfile.fileno()))  # a real exec lets it go
        self.pilot.deadline = now() + timedelta(hours=2)
        with redirect_stdout(io.StringIO()), self.assertRaises(Restarted):
            self.pilot.self_update()
        self.pilot.lockfile.close()  # as the exec would have
        again = driver.Pilot(self.p, args(parallel=1))
        again.home = self.i.home
        with redirect_stdout(io.StringIO()):
            again.run(now() + timedelta(hours=2))  # the restart set WINDVANE_RESTARTED, as the exec'd driver sees it
        self.assertEqual(again.merged, ["T-001", "T-003"])
        self.assertEqual((again.failed, again.plans), (["T-002"], 1))
        self.assertNotIn("T-002: worker run", self.log())
        self.assertIn("Merged: do T-001 (T-001); do T-003 (T-003). Waiting on you: none. Failed: do T-002 (T-002)", self.log())
        self.assertFalse((self.p.state / "restart.json").exists())

    def test_a_fresh_start_forgets_an_old_restart_file(self):
        self.p.state.mkdir(parents=True, exist_ok=True)
        (self.p.state / "restart.json").write_text('{"skip": ["T-001"], "merged": ["T-000"]}')
        self.pilot.home = None
        with redirect_stdout(io.StringIO()):
            self.pilot.run(now() + timedelta(hours=2))
        self.assertEqual(self.pilot.merged, ["T-001", "T-002"])

    def test_no_merge_means_no_pull_for_a_while(self):
        self.pilot.self_update()
        new = self.i.commit("README.md", "docs\n")
        self.pilot.self_update()  # nothing merged since, and just now: no fetch
        self.assertNotEqual(self.i.head(), new)
        self.pilot.merged.append("T-009")
        self.pilot.self_update()
        self.assertEqual(self.i.head(), new)

    def test_off_in_tests_by_default(self):
        self.assertIsNone(driver.Pilot(self.p, args(parallel=1)).home)


if __name__ == "__main__":
    unittest.main()
