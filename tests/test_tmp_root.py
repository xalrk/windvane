"""`tmp_root` moves the tickets' TMPDIRs to a roomier disk; a nearly full disk under them is warned about (by percent
or by a free-space floor), and a check that ran out of disk says so instead of reading as a bug in the change."""
import contextlib
import io
import json
import os
import sys
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock

from helpers import Sandbox
from test_install import REPO
from test_pipeline import args

sys.path.insert(0, str(REPO))
from windvane import cli, doctor, driver, heavy, jobs, scratch  # noqa: E402
from windvane.config import Project  # noqa: E402
from windvane.util import now  # noqa: E402

GB = 1024 ** 3


def statvfs(free_gb, total_gb=22, files=1000, ffree=900):
    """A stand-in for os.statvfs: a `total_gb` disk with `free_gb` free (in 4K blocks)."""
    blocks, avail = total_gb * GB // 4096, int(free_gb * GB) // 4096
    return lambda path: os.statvfs_result((4096, 4096, blocks, avail, avail, files, ffree, ffree, 0, 255))


class TmpRoot(unittest.TestCase):
    def setUp(self):
        self.s = Sandbox()
        self.addCleanup(self.s.close)
        self.root = self.s.tmp / "roomy"
        self.root.mkdir()

    def machine(self, text):
        heavy.machine_file().parent.mkdir(parents=True, exist_ok=True)
        heavy.machine_file().write_text(text)

    def test_unset_keeps_the_state_dir(self):
        p = Project(self.s.root)
        self.assertEqual(scratch.session_dir(p, "T-001"), p.state / "tmp" / "T-001")
        self.assertIsNone(scratch.tmp_root_problem(p))

    def test_machine_toml_moves_sessions_and_checks_and_drop_and_clean_find_them_there(self):
        self.machine(f'tmp_root = "{self.root}"\n')
        p = Project(self.s.root)
        d = self.root / p.name / "T-007"
        self.assertEqual(scratch.session_dir(p, "T-007"), d)
        jobs.run_logged(p, "T-007", 'echo "$TMPDIR" > seen', self.s.root, self.s.tmp / "check.log")
        self.assertEqual((self.s.root / "seen").read_text().strip(), str(d))
        self.assertFalse((p.state / "tmp" / "T-007").exists())
        scratch.drop(p, "T-007")
        self.assertFalse(d.exists())
        old = scratch.session_dir(p, "T-008")
        os.utime(old, (0, 0))
        self.assertIn(old, scratch.candidates(p))
        self.assertNotIn(old, scratch.candidates(p, {"T-008"}))  # a running ticket's stays
        self.assertEqual(scratch.clean(p)[0], 1)
        self.assertFalse(old.exists())

    def test_the_project_overrides_the_machine(self):
        self.machine(f'tmp_root = "{self.s.tmp / "elsewhere"}"\n')
        self.s.write(".windvane/config.toml", f'tmp_root = "{self.root}"\n'
                     + (self.s.root / ".windvane/config.toml").read_text())  # before any table
        p = Project(self.s.root)
        self.assertEqual(scratch.session_dir(p, "T-001"), self.root / p.name / "T-001")

    def test_a_missing_or_relative_root_refuses_the_run_and_doctor_says_why(self):
        self.machine(f'tmp_root = "{self.s.tmp / "gone"}"\n')
        p = Project(self.s.root)
        self.assertRegex(scratch.tmp_root_problem(p), r"^tmp_root \(machine.toml\) is .*/gone, which isn't a directory")
        rows = doctor.tmp_warnings(p)
        self.assertEqual(rows[0][0], "need")
        self.assertIn("isn't a directory", rows[0][1])
        with mock.patch.object(cli.shutil, "which", return_value="/bin/claude"), \
                self.assertRaises(SystemExit) as e:
            cli.main(["-C", str(self.s.root), "run", "--for", "2h", "--no-preflight"])
        self.assertIn("tmp_root (machine.toml)", str(e.exception.code))
        self.machine('tmp_root = "rel/dir"\n')
        self.assertIn("give an absolute path", scratch.tmp_root_problem(Project(self.s.root)))


class NearlyFull(unittest.TestCase):
    def setUp(self):
        self.s = Sandbox()
        self.addCleanup(self.s.close)
        self.p = Project(self.s.root)
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(mock.patch.object(scratch, "fullness", self.s.fullness))  # the real ones, on a fake disk
        stack.enter_context(mock.patch.object(scratch, "free", self.s.free))

    def test_a_disk_under_the_floor_warns_in_doctor_and_pushes_once(self):
        with mock.patch("os.statvfs", statvfs(30)):
            self.assertEqual(scratch.nearly_full(self.p), [])
        with mock.patch("os.statvfs", statvfs(0.56)):  # 98% of 22G: 573 MB free
            full = scratch.nearly_full(self.p)
            rows = doctor.tmp_warnings(self.p)
        self.assertEqual(len(full), 1)  # the temp dir and the TMPDIRs share a filesystem here: said once
        self.assertEqual(rows[0][0], "warn")
        self.assertRegex(rows[0][1], r"each ticket's TMPDIR .* is 10% full of inodes and 97% of space \(573 MB free\)")
        self.assertIn("set tmp_root", rows[0][2])
        sent = []
        pilot = driver.Pilot(self.p, args())
        with mock.patch.object(driver, "push", side_effect=lambda *a: sent.append(a)):
            with mock.patch("os.statvfs", statvfs(0.56)):
                pilot.warn_tmp_full(), pilot.warn_tmp_full()
            self.assertEqual(len(sent), 1)
            self.assertIn("573 MB free", sent[0][1])
            self.assertIn("set tmp_root", sent[0][1])
            with mock.patch("os.statvfs", statvfs(30)):
                pilot.warn_tmp_full()
            with mock.patch("os.statvfs", statvfs(1)):
                pilot.warn_tmp_full()  # filled again after going under: pushed again
        self.assertEqual(len(sent), 2)

    def test_two_filesystems_are_each_measured_and_named(self):
        root = self.s.tmp / "roomy"
        root.mkdir()
        heavy.machine_file().parent.mkdir(parents=True, exist_ok=True)
        heavy.machine_file().write_text(f'tmp_root = "{root}"\n')
        p = Project(self.s.root)
        dev = lambda path: 2 if str(path).startswith(str(root)) else 1
        seen = []
        with mock.patch.object(scratch, "device", dev), mock.patch("os.statvfs", lambda path: seen.append(path)
                                                                     or statvfs(0.5)(path)):
            full = scratch.nearly_full(p)
        self.assertEqual([(f[0], f[1]) for f in full], [(scratch.root(), "the temp dir"),
                                                        (root / p.name, "each ticket's TMPDIR")])
        self.assertIn(root, seen)  # the ticket dir isn't made yet: its nearest parent is measured


class DiskFullCheck(unittest.TestCase):
    def setUp(self):
        self.s = Sandbox()
        self.addCleanup(self.s.close)
        self.p = Project(self.s.root)

    def check(self, script):
        self.p.cfg["check"] = script
        with mock.patch.object(jobs, "tree_key", return_value=None):
            return jobs.check(self.p, self.s.root, ticket="T-009")

    def test_a_check_whose_log_says_enospc_gets_the_note(self):
        ok, summary, _ = self.check("echo 'OSError: [Errno 28] No space left on device'; exit 1")
        self.assertFalse(ok)
        self.assertRegex(summary, rf"; the disk holding {self.p.state}/tmp/T-009 ran out of space \([\d,]+ MB free "
                                  r"now\): not the change's fault, free space or set tmp_root; log ")
        self.assertNotIn("ran out of space", self.check("echo 'AssertionError'; exit 1")[1])

    def test_the_gate_runs_it_again_instead_of_bouncing_and_fails_after_three(self):
        pilot = driver.Pilot(self.p, args())
        pilot.deadline = now() + timedelta(hours=2)
        full = (False, "CHECK FAILED (exit 1) in 1.0 min; the disk holding /x ran out of space (12 MB free now)"
                + jobs.DISK_FULL + "; log /l", None)
        results = [full, (True, "CHECK PASSED in 1.0 min", None)]
        sent, naps = [], []
        with mock.patch.object(driver, "push", side_effect=lambda *a: sent.append(a)), \
                mock.patch.object(pilot, "nap", side_effect=naps.append), \
                mock.patch.object(pilot, "clean_tmp") as clean:
            self.assertTrue(pilot.ran("T-009", lambda: results.pop(0), "check")[0])
            self.assertEqual((len(naps), clean.call_count), (1, 1))
            self.assertEqual([a[0].split(":")[0] for a in sent], ["Check ran out of disk"])
            with mock.patch.object(pilot, "fail") as fail:
                self.assertIsNone(pilot.ran("T-009", lambda: full, "check")[0])
        self.assertIn("ran out of disk 3 times in a row", fail.call_args[0][1])
        self.assertEqual(len(naps), 3)  # two more waits, none after the third


if __name__ == "__main__":
    unittest.main()
