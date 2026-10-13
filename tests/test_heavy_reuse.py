"""R-010, R-015: a project's heavy jobs count the memory of its own long-lived helpers (`heavy_reuses`: an emulator,
a build daemon) as free, since they use those very processes; another project's helpers don't count."""
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from windvane import config, heavy, util  # noqa: E402

GB = 1048576  # kB


class HeavyReuse(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="windvane-")
        self.addCleanup(tmp.cleanup)
        self.d = d = Path(tmp.name)
        self.proc = d / "proc"
        self.proc.mkdir()
        (self.proc / "meminfo").write_text(f"MemTotal: {32 * GB} kB\nMemAvailable: {int(4.2 * GB)} kB\n")
        # This project: its main checkout, two slot worktrees and its state dir; another project's checkout; an SDK.
        for sub in ("proj", "state/slot-1", "state/slot-2", "other", "sdk"):
            (d / sub).mkdir(parents=True)
        for sub in ("proj", "state/slot-1", "state/slot-2", "other"):
            (d / sub / ".git").write_text("gitdir: x\n")
        self.reuse = (["qemu-system", "GradleDaemon"],
                      [str(d / "proj"), str(d / "state"), str(d / "state/slot-1"), str(d / "state/slot-2")])
        patch = mock.patch.object(util, "PROC", self.proc)
        patch.start()
        self.addCleanup(patch.stop)

    def ps(self, pid, comm, cwd, rss_gb=0.0, ppid=1, cmdline=None):
        p = self.proc / str(pid)
        p.mkdir()
        (p / "comm").write_text(comm + "\n")
        (p / "cmdline").write_bytes(b"\0".join(w.encode() for w in (cmdline or [comm])) + b"\0")
        (p / "status").write_text(f"Name:\t{comm}\nPPid:\t{ppid}\nVmRSS:\t{int(rss_gb * GB)} kB\n")
        os.symlink(self.d / cwd, p / "cwd")

    def helpers(self, reuse="default"):
        return round(heavy.helpers_gb(self.reuse if reuse == "default" else reuse), 1)

    def test_own_helper_lifts_the_job_over_its_bar(self):
        self.ps(100, "qemu-system-x86", "state/slot-1", 3.4)
        self.assertEqual(self.helpers(), 3.4)
        self.assertIsNone(heavy.short(6, heavy.free_gb(), self.helpers()))
        self.assertTrue(self.take(self.reuse))

    def test_without_heavy_reuses_nothing_changes(self):
        self.ps(100, "qemu-system-x86", "state/slot-1", 3.4)
        self.assertEqual(self.helpers(None), 0)
        self.assertEqual(self.helpers(([], self.reuse[1])), 0)
        self.assertFalse(self.take(None))
        self.assertIn("only 4.2 GB free (want 6)", self.said)

    def test_a_helper_in_another_slot_or_the_main_checkout_counts(self):
        self.ps(100, "qemu-system-x86", "state/slot-2", 3.4)  # booted by a job from another slot
        self.ps(101, "java", "proj", 2.5, cmdline=["java", "-Xmx2g", "org.gradle.launcher.daemon.bootstrap.GradleDaemon",
                                                   "8.9"])
        self.assertEqual(self.helpers(), 5.9)

    def test_another_projects_helper_or_a_non_matching_process_doesnt(self):
        self.ps(100, "qemu-system-x86", "other", 3.4)
        self.ps(101, "java", "proj", 2.0)  # a JVM that isn't a Gradle daemon
        self.ps(102, "claude", "state/slot-1", 0.5, cmdline=["claude", "-p", "why is GradleDaemon slow"])
        self.assertEqual(self.helpers(), 0)
        self.assertFalse(self.take(self.reuse))
        self.assertIn("only 4.2 GB free (want 6)", self.said)

    def test_a_launcher_that_cds_elsewhere_counts_by_its_ancestor(self):
        self.ps(50, "bash", "state/slot-1", 0.01)
        self.ps(51, "emulator", "sdk", 0.1, ppid=50)
        self.ps(52, "qemu-system-x86", "sdk", 3.4, ppid=51)
        self.assertEqual(self.helpers(), 3.4)
        self.ps(60, "bash", "other", 0.01)  # the same launch from another project's checkout
        self.ps(61, "qemu-system-x86", "sdk", 3.0, ppid=60)
        self.assertEqual(self.helpers(), 3.4)

    def test_a_shared_ancestor_doesnt_claim_another_projects_helper(self):
        self.ps(10, "tmux: server", "proj", 0.01)  # one tmux server, started in this project
        self.ps(11, "bash", "other", 0.01, ppid=10)
        self.ps(12, "qemu-system-x86", "sdk", 3.0, ppid=11)
        self.assertEqual(self.helpers(), 0)

    def test_a_reparented_helper_goes_by_its_own_cwd(self):
        self.ps(70, "qemu-system-x86", "sdk", 3.0, ppid=1)  # its launcher left: reparented to init
        self.ps(71, "qemu-system-x86", "sdk", 2.0, ppid=999)  # its parent is gone
        self.assertEqual(self.helpers(), 0)
        self.ps(72, "qemu-system-x86", "state/slot-2", 3.4, ppid=1)
        self.assertEqual(self.helpers(), 3.4)

    def test_a_cwd_it_cant_enter_doesnt_crash_the_wait(self):
        self.ps(80, "bash", "state/slot-1", 0.01)
        self.ps(81, "qemu-system-x86", "sdk", 3.4, ppid=80)
        real = Path.exists
        def exists(p, *a, **kw):
            if "sdk" in str(p):
                raise PermissionError(13, "Permission denied", str(p))
            return real(p, *a, **kw)
        with mock.patch.object(Path, "exists", exists):
            self.assertEqual(self.helpers(), 3.4)
            self.assertTrue(self.take(self.reuse))

    def test_the_waiting_line_says_what_it_counted(self):
        self.assertEqual(heavy.short(6, 1.0, 3.4), "1.0 GB free + 3.4 GB in this project's helpers (want 6)")
        self.assertEqual(heavy.short(6, 1.0), "only 1.0 GB free (want 6)")

    def test_config_checks_heavy_reuses(self):
        self.assertEqual(config.DEFAULTS["heavy_reuses"], [])
        self.assertEqual(config.heavy_reuses(["qemu-system"]), ["qemu-system"])
        for bad in ("qemu-system", [""], ["("], [3]):
            with self.assertRaises(SystemExit):
                config.heavy_reuses(bad)

    def test_reuse_of_names_every_checkout_of_the_project_and_its_state(self):
        root = self.d / "repo"
        root.mkdir()
        git = lambda *a: subprocess.run(["git", "-C", str(root), *a], check=True, capture_output=True)
        git("init", "-q", "-b", "main")
        git("-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "--allow-empty", "-m", "x")
        git("worktree", "add", "-q", "--detach", str(self.d / "wt"))
        (root / ".windvane").mkdir()
        with mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(self.d / "st")}):
            p = config.Project(root)
            self.assertIsNone(heavy.reuse_of(p))
            (root / ".windvane/config.toml").write_text('heavy_reuses = ["qemu-system"]\n')
            p = config.Project(root)
            pats, dirs = heavy.reuse_of(p)
        self.assertEqual(pats, ["qemu-system"])
        real = lambda q: os.path.realpath(q)
        self.assertLessEqual({real(root), real(self.d / "wt"), real(p.state)}, set(dirs))

    def take(self, reuse):
        """Whether a job needing 6 GB gets the machine within a moment, with `reuse`; what it said is in self.said."""
        lines = []
        with mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": str(self.d)}):
            entry = heavy.join_queue(6)
            lock = os.open(heavy.lock_path(), os.O_CREAT | os.O_RDWR, 0o600)
            try:
                end = time.time() + 1.5
                got = heavy.take(lock, entry, 6, {"busy": []}, lambda: end, lines.append, reuse=reuse)
            finally:
                heavy.leave_queue(entry)
                os.close(lock)
        self.said = "\n".join(lines)
        return got


if __name__ == "__main__":
    unittest.main()
