"""A driver started by `run --detach` lives in a tmux pane, with TMUX and TMUX_PANE set. Without -L or -S, tmux takes
$TMUX's socket over TMUX_TMPDIR, so a session (or a check, or a setup) that made a scratch server with TMUX_TMPDIR and
ended it with `tmux kill-server` ended the driver's server instead: the user's own, with every detached run on it.
Nothing the driver starts gets those two variables; the driver itself keeps them."""
import os
import shutil
import subprocess
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock

from helpers import Sandbox, tmpdir

from windvane import jobs, runner, util  # noqa: E402
from windvane.config import Project  # noqa: E402
from windvane.util import now  # noqa: E402

PANE = {"TMUX": "/tmp/tmux-1000/default,4169,0", "TMUX_PANE": "%3"}
SHOW = 'echo "TMUX=${TMUX-unset} TMUX_PANE=${TMUX_PANE-unset}" > seen'
UNSET = "TMUX=unset TMUX_PANE=unset"


class NothingStartedGetsThePane(unittest.TestCase):
    def setUp(self):
        self.s = Sandbox()
        self.addCleanup(self.s.close)
        self.p = Project(self.s.root)
        patch = mock.patch.dict(os.environ, PANE)
        patch.start()
        self.addCleanup(patch.stop)

    def tearDown(self):
        """The driver's own environment keeps them: `supervise`, its re-exec and `run --detach` go by them."""
        self.assertEqual({k: os.environ.get(k) for k in PANE}, PANE)

    def test_a_session(self):
        for out in (runner.env({"TMPDIR": "/x"}), runner.env({"TMPDIR": "/x"}, self.p)):
            self.assertEqual(set(PANE) & set(out), set())  # (names only: the rest may hold secrets)

    def test_the_drivers_own_check(self):
        jobs.run_logged(self.p, "T-001", SHOW, self.s.root, self.s.tmp / "check.log")
        self.assertEqual((self.s.root / "seen").read_text().strip(), UNSET)

    def test_setup_and_teardown(self):
        """The config's commands are the user's, but a setup's install scripts come from the repo, and a teardown
        stopping a helper in tmux finds it on the default server, where a command without the pane looks."""
        code, why, _ = util.run_limited(SHOW, self.s.root, now() + timedelta(minutes=1))
        self.assertEqual((code, why), (0, ""))
        self.assertEqual((self.s.root / "seen").read_text().strip(), UNSET)


@unittest.skipUnless(shutil.which("tmux"), "needs tmux")
class ScratchServers(unittest.TestCase):
    """On two throwaway servers, never the default socket: A stands for the user's (the driver's pane is on it),
    B is the TMUX_TMPDIR a session picks for its scratch server."""

    def setUp(self):
        self.base = tmpdir(self, "wvtm-")
        sock = f"/a/tmux-{os.getuid()}/default"
        if len(str(self.base) + sock) > 100:  # a Unix socket's path is cut at 104-108 bytes
            self.base = Path(tempfile.mkdtemp(prefix="wvtm-", dir="/tmp")).resolve()
            self.addCleanup(shutil.rmtree, self.base, ignore_errors=True)
        # never the caller's own server: these tests may run inside a tmux pane themselves
        self.clean = {k: v for k, v in os.environ.items() if k not in ("TMUX", "TMUX_PANE", "TMUX_TMPDIR")}
        self.a, self.b = self.base / "a", self.base / "b"
        for d in (self.a, self.b):
            d.mkdir(mode=0o700)
            self.addCleanup(self.tmux, d, "kill-server")
        self.tmux(self.a, "new-session", "-d", "-s", "user", "sleep 600", check=True)
        self.sock_a = self.tmux(self.a, "display-message", "-p", "-t", "user", "#{socket_path}").stdout.strip()
        self.assertTrue(self.sock_a.startswith(str(self.a)), self.sock_a)

    def tmux(self, tmpdir, *args, check=False):
        return subprocess.run(["tmux", *args], env={**self.clean, "TMUX_TMPDIR": str(tmpdir)}, capture_output=True,
                              text=True, timeout=30, check=check)

    def alive(self, d):
        return self.tmux(d, "has-session", "-t", "=user").returncode == 0

    def test_a_sessions_kill_server_leaves_the_drivers_server_alive(self):
        pane = {"TMUX": f"{self.sock_a},1,0", "TMUX_PANE": "%0"}
        with mock.patch.dict(os.environ, self.clean, clear=True), mock.patch.dict(os.environ, pane):
            env = {**runner.env({"TMPDIR": str(self.base)}), "TMUX_TMPDIR": str(self.b)}
        r = subprocess.run("tmux new-session -d -s scratch 'sleep 600' && tmux ls && tmux kill-server", shell=True,
                           env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(r.stdout.startswith("scratch:"), r.stdout)  # it saw its own server, not A's "user"
        self.assertTrue(self.alive(self.a), "the driver's server was killed")


if __name__ == "__main__":
    unittest.main()
