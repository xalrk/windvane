"""`windvane heavy` and `heavy --status` stop inside the Bash sandbox: there the runtime folder holding the lock is
read-only (`heavy -- <command>` crashed opening it) and the pid namespace hides the holder and waiters (`--status`
said `free` while another job held the machine)."""
import os
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import Sandbox  # noqa: E402
from test_heavy_check_wait import machine  # noqa: E402
from windvane.runner import IN_SANDBOX  # noqa: E402

MESSAGE = "windvane: the heavy lock can't run inside the Bash sandbox: run the `windvane` command on its own"


class HeavySandbox(unittest.TestCase):
    def setUp(self):
        self.sb = Sandbox()
        self.addCleanup(self.sb.close)
        self.run_dir = self.sb.tmp / "run"
        self.run_dir.mkdir()
        self.env = {**os.environ, **machine(self.run_dir, 1)}
        for k in (IN_SANDBOX, "WINDVANE_HEAVY_HELD"):
            self.env.pop(k, None)
        self.lock = self.run_dir / "windvane-heavy.lock"
        self.ran = self.sb.tmp / "ran"

    def windvane(self, *args, sandboxed=False, **extra):
        env = {**self.env, **extra, **({IN_SANDBOX: "1"} if sandboxed else {})}
        return subprocess.run(["windvane", *args], cwd=self.sb.root, env=env, capture_output=True, text=True,
                              timeout=60)

    def assert_refused(self, r):
        self.assertNotEqual(r.returncode, 0)
        self.assertIn(MESSAGE, r.stderr)
        self.assertNotIn("Traceback", r.stderr)
        self.assertFalse(self.ran.exists(), "the command ran")
        self.assertFalse(self.lock.exists(), "a lock file was made")
        self.assertFalse((self.run_dir / "windvane-heavy.queue").exists(), "a queue entry was made")

    def test_heavy_refuses_in_the_sandbox(self):
        self.assert_refused(self.windvane("heavy", "--", "touch", str(self.ran), sandboxed=True))

    def test_a_read_only_runtime_folder_gets_the_message_not_a_traceback(self):
        """R-027's repro: the sandbox mounts $XDG_RUNTIME_DIR read-only; `run` used to raise OSError opening the lock."""
        self.run_dir.chmod(0o555)
        self.addCleanup(self.run_dir.chmod, 0o755)
        self.assert_refused(self.windvane("heavy", "--", "touch", str(self.ran), sandboxed=True))

    def test_status_refuses_in_the_sandbox(self):
        r = self.windvane("heavy", "--status", sandboxed=True)
        self.assert_refused(r)
        self.assertNotIn("free", r.stdout)

    def test_outside_the_sandbox_both_work_as_before(self):
        r = self.windvane("heavy", "--", "touch", str(self.ran))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(self.ran.exists())
        r = self.windvane("heavy", "--status")
        self.assertEqual((r.returncode, r.stdout), (0, "free\n"), r.stderr)

    def test_a_nested_heavy_is_unchanged(self):
        """Inside a heavy job the outer one holds the lock: the nested call only runs its command."""
        r = self.windvane("heavy", "--", "touch", str(self.ran), WINDVANE_HEAVY_HELD=str(self.lock))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(self.ran.exists())
        self.assertFalse(self.lock.exists())


if __name__ == "__main__":
    unittest.main()
