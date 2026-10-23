"""What a Sandbox costs each test, where the machine makes it dear: git that fsyncs (macOS flushes the whole disk each
time) or starts its auto maintenance, and no RAM-backed temp dir to put the repos in (macOS has none)."""
import json
import os
import shutil
import subprocess
import sys
import textwrap
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from windvane import runner, worktree  # noqa: E402
from windvane.config import Project  # noqa: E402
import helpers  # noqa: E402
from helpers import IN_TMPDIR, REPO, Sandbox, tmpdir  # noqa: E402


class Git(unittest.TestCase):
    def setUp(self):
        self.s = Sandbox()
        self.addCleanup(self.s.close)

    def config(self, key, cwd, env=None):
        return subprocess.run(["git", "config", key], cwd=cwd, env=env, capture_output=True, text=True).stdout.strip()

    def test_the_tests_and_the_drivers_git_dont_fsync_nor_start_maintenance(self):
        p = Project(self.s.root)
        session = runner.env(worktree.guard_pushes(p, self.s.root), p)  # its own GIT_CONFIG_* too: the hooks' path
        self.assertIn("core.hooksPath", runner.git_config(session)[-1])
        for where, env in (("the tests'", None), ("a session's", session)):
            self.assertEqual(self.config("core.fsync", self.s.root, env), "none", where)
            self.assertEqual(self.config("maintenance.auto", self.s.root, env), "false", where)
        # a push's receive-pack runs without GIT_CONFIG_*: the remote has them in its own config
        self.assertEqual(self.config("core.fsync", self.s.remote, {"PATH": os.environ["PATH"]}), "none")

    def test_a_commit_and_its_push_flush_nothing_to_the_disk(self):
        trace = self.s.tmp / "trace.json"
        os.environ["GIT_TRACE2_EVENT"] = str(trace)  # git's own counters: one per fsync kind a process used
        self.s.write("a", "a\n")
        self.s.publish("a")
        events = [json.loads(line) for line in trace.read_text().splitlines()]
        self.assertTrue(any(e["event"] == "start" for e in events))
        flushes = [e for e in events if e["event"] == "counter" and e.get("name") == "hardware-flush"]
        self.assertEqual(flushes, [])
        self.assertFalse([e for e in events if e["event"] == "start" and "maintenance" in e["argv"]])


class CallersGit(unittest.TestCase):
    """macOS: the Sandbox's PATH reaches the caller's git (Homebrew's, say), not the developer tools' stub."""

    def test_a_git_outside_the_system_dirs_is_linked_in_a_dir_of_its_own(self):
        git = tmpdir(self) / "brew/bin/git"
        git.parent.mkdir(parents=True)
        git.write_text("#!/bin/sh\nexec /usr/bin/git \"$@\"\n")
        git.chmod(0o755)
        with mock.patch.object(helpers.shutil, "which", lambda name: str(git)):
            d = Path(helpers.git_dir())
        self.assertEqual(os.listdir(d), ["git"])
        self.assertEqual((d / "git").resolve(), git.resolve())
        with mock.patch.object(helpers, "GIT_DIR", str(d)):
            s = Sandbox()
            self.addCleanup(s.close)
            self.assertEqual(shutil.which("git"), str(d / "git"))
            self.assertIn("init", s.git("log", "--format=%s"))
            (s.bin / "git").write_text("#!/bin/sh\n")  # a test's own git, first on the PATH, over nobody's
        self.assertIn("exec /usr/bin/git", git.read_text())

    def test_the_systems_own_git_needs_no_link(self):
        with mock.patch.object(helpers.shutil, "which", lambda name: "/usr/bin/git"):
            self.assertIsNone(helpers.git_dir())


class NoRam(unittest.TestCase):
    """macOS: no /dev/shm, so the tests' files stay in the temp dir the system gives."""

    def test_no_ram_dir_means_the_system_temp_dir(self):
        with mock.patch.object(helpers, "RAM", tmpdir(self) / "no-shm"):
            self.assertIsNone(helpers.ram_tmp())

    def test_a_sandbox_works_and_goes_in_the_system_temp_dir(self):
        disk = tmpdir(self)
        script = textwrap.dedent("""\
            import sys, tempfile
            sys.path.insert(0, sys.argv[1] + "/tests")
            from helpers import Sandbox
            s = Sandbox()
            assert s.tmp.parent == __import__("pathlib").Path(sys.argv[2]).resolve(), (s.tmp, sys.argv[2])
            s.write("a", "a\\n")
            s.publish("a")
            assert s.git("log", "--format=%s", "-1", "origin/main") == "a"
            s.close()
            assert not s.tmp.exists()
            """)
        env = {**os.environ, "TMPDIR": str(disk), IN_TMPDIR: "1", "PYTHONPATH": str(REPO)}
        r = subprocess.run([sys.executable, "-c", script, str(REPO), str(disk)], capture_output=True, text=True,
                           env=env, timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr[-2000:])
        self.assertEqual(list(disk.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
