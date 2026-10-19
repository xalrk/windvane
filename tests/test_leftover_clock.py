"""With isolate off, the run's end names only what started in the checkout after the run began: a run's start read on
the processes' own clock (their start ticks), so a container that fakes /proc/uptime (lxcfs) while the ticks stay the
host's can't make the user's own processes look like the run's helpers."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from windvane import driver, util  # noqa: E402
from windvane.config import Project  # noqa: E402
from helpers import Sandbox  # noqa: E402
from test_leftover_helpers import TITLE, kill  # noqa: E402

TCK = os.sysconf("SC_CLK_TCK")
USERS, HELPER = 4000001, 4000002  # pids no real process here has


class LeftoverClock(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.proc = Path(tmp.name) / "proc"
        self.root = Path(os.path.realpath(tmp.name)) / "checkout"
        self.root.mkdir()
        self.p = SimpleNamespace(cfg={"isolate": False}, main_root=lambda: self.root)
        self.now = util.clock()  # the host's clock, which the run reads off a thread of its own (the real one's ticks)
        patch = mock.patch.object(util, "PROC", self.proc)
        patch.start()
        self.addCleanup(patch.stop)
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        for k in (driver.SUPERVISED, driver.BEGAN):
            os.environ.pop(k, None)
        # the container's uptime says 50 s, far below every process's start ticks, the host's
        self.proc.mkdir()
        (self.proc / "uptime").write_text("50.00 40.00\n")
        (self.proc / str(os.getpid())).mkdir()
        os.symlink(f"/proc/{os.getpid()}/task", self.proc / str(os.getpid()) / "task")
        self.fake(USERS, self.now / 2, self.root, "vim notes")  # the user's, older than the run
        self.fake(HELPER, self.now + 1000, self.root, "sleep 120")  # what the run's tools started

    def fake(self, pid, started, cwd, cmd="windvane run", tty=0):
        d = self.proc / str(pid)
        d.mkdir()
        fields = ["S", "1", str(pid), str(pid), str(tty)] + ["0"] * 14 + [str(int(started * TCK))] + ["0"] * 5
        (d / "stat").write_text(f"{pid} ({cmd.split()[0]}) " + " ".join(fields) + "\n")
        (d / "cmdline").write_bytes(cmd.replace(" ", "\0").encode() + b"\0")
        if cwd:
            os.symlink(cwd, d / "cwd")

    def left(self):
        return driver.leftover_helpers(self.p, driver.run_began())

    def test_a_faked_uptime_names_only_what_the_run_started(self):
        self.assertEqual(self.left(), [(HELPER, "sleep 120")])

    def test_an_unreadable_uptime_changes_nothing(self):
        (self.proc / "uptime").unlink()
        self.assertEqual(self.left(), [(HELPER, "sleep 120")])

    def test_no_clock_reports_nothing_rather_than_everything(self):
        (self.proc / str(os.getpid()) / "task").unlink()
        self.assertIsNone(driver.run_began())
        self.assertEqual(self.left(), [])

    def test_a_restarted_driver_goes_by_the_supervisors_start(self):
        """It began after the crashed driver's helpers: the supervisor's start, before them, still counts them."""
        os.environ.update({driver.SUPERVISED: "1", driver.BEGAN: repr(self.now - 50)})
        self.fake(HELPER + 1, self.now - 30, self.root, "sleep 121")  # the crashed driver's tools' helper
        self.assertEqual(self.left(), [(HELPER, "sleep 120"), (HELPER + 1, "sleep 121")])

    def test_a_process_with_a_terminal_is_never_named(self):
        self.fake(HELPER + 1, self.now + 1100, self.root, "bash", tty=34816)
        self.assertEqual(self.left(), [(HELPER, "sleep 120")])


# A run's driver as its own process, started after the user's: the sandbox's in-process patches, its pushes printed.
DRIVER = """import json, sys
from datetime import timedelta
sys.path[:0] = sys.argv[1:3]
from windvane import driver, login, net, scratch
from windvane.config import Project
from windvane.util import now
from test_pipeline import args
net.probe, net.waiter, login.waiter, login.back_at = (lambda: True), None, None, 0.0
scratch.fullness = lambda path=None: (10, 10)
pushes = []
driver.push = lambda title, msg="", *a, **k: pushes.append((title, msg))
driver.Pilot(Project(sys.argv[3]), args()).run(now() + timedelta(hours=1))
print(json.dumps(pushes))
"""


def can_fake_uptime():
    """Whether this machine lets an unprivileged user bind a file over /proc/uptime in a namespace of its own, as
    lxcfs does in an LXC container."""
    if not shutil.which("unshare") or not Path("/proc/uptime").exists():
        return False
    return subprocess.run(["unshare", "-rm", "sh", "-c", "mount --bind /dev/null /proc/uptime"],
                          capture_output=True).returncode == 0


class ThroughARun(unittest.TestCase):
    """The run's end-of-run push, from a real driver process started after the user's own process in the checkout."""

    def setUp(self):
        self.addCleanup(os.chdir, os.getcwd())
        self.s = Sandbox()
        self.addCleanup(self.s.close)
        self.s.ticket("T-001")
        cfg = self.s.root / ".windvane/config.toml"
        cfg.write_text("isolate = false\n" + cfg.read_text())
        self.s.publish("config")
        self.pidfile = self.s.tmp / "helper.pid"
        os.environ["FAKE_HELPER"] = str(self.pidfile)

    def run_driver(self, setup=""):
        """The driver's pushes. A shell (after `setup`) starts the user's process in the checkout, then the driver:
        one namespace for both, as a process can't read the cwd of one outside its own user namespace."""
        tests, users = Path(__file__).resolve().parent, self.s.tmp / "users.pid"
        start = ('(cd "$0" && exec sleep 121 >/dev/null 2>&1 </dev/null) & echo $! > "$1"; sleep 0.1; shift; '
                 '"$@"; exit $?')  # the `exit`: the driver a child of the shell, so started after the user's process
        driver_cmd = [sys.executable, "-c", DRIVER, str(tests.parent), str(tests), str(self.s.root)]
        out = subprocess.run([*(["unshare", "-rm"] if setup else []), "sh", "-c", setup + start, str(self.s.root),
                              str(users), *driver_cmd], capture_output=True, text=True, timeout=60)
        for f in (users, self.pidfile):
            if f.exists():
                self.addCleanup(kill, int(f.read_text()))
        self.assertEqual(out.returncode, 0, out.stderr[-2000:])
        return [m for t, m in json.loads(out.stdout.splitlines()[-1]) if t == TITLE]

    def helper_named(self, msgs):
        self.assertEqual(len(msgs), 1, msgs)
        self.assertIn(f"Still running in the checkout, started by the run: sleep 120 (pid {self.pidfile.read_text()})",
                      msgs[0])
        self.assertNotIn("sleep 121", msgs[0])

    def test_the_push_names_the_runs_helper_and_not_the_users_process(self):
        self.helper_named(self.run_driver())

    @unittest.skipUnless(can_fake_uptime(), "no unprivileged mount namespace to fake /proc/uptime in")
    def test_a_faked_uptime_as_lxcfs_gives_still_names_only_the_helper(self):
        fake = self.s.tmp / "uptime"
        fake.write_text("50.00 40.00\n")  # the container's: far below every process's start ticks, the host's
        self.helper_named(self.run_driver(f"mount --bind {fake} /proc/uptime || exit 9; "))


class SupervisorPassesItsStart(unittest.TestCase):
    def test_the_driver_gets_when_the_supervisor_started_it_not_when_its_process_did(self):
        """A schedule's long-lived process supervises each window in turn: the run's start is the window's."""
        seen = {}

        def start(args, env):
            seen.update(env)
            raise SystemExit  # one look is enough
        before = util.clock()
        with mock.patch.dict(os.environ), self.assertRaises(SystemExit):
            driver.supervise(SimpleNamespace(state=Path("/nonexistent")), ["run"], None, start=start)
        self.assertGreater(before, util.boot_started(os.getpid()))
        self.assertLessEqual(before, float(seen[driver.BEGAN]))
        self.assertLessEqual(float(seen[driver.BEGAN]), util.clock())

if __name__ == "__main__":
    unittest.main()
