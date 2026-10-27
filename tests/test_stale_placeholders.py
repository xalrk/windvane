"""The empty read-only files a killed sandboxed command leaves: doctor names them and --clean removes them, the driver
clears its own places at a run's start and between tickets, and one a live sandbox mounts over is never touched."""
import contextlib
import io
import os
import subprocess
import sys
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock

from helpers import Sandbox
from test_install import REPO
from test_pipeline import args

sys.path.insert(0, str(REPO))
from windvane import doctor, driver, jobs  # noqa: E402
from windvane.config import Project  # noqa: E402
from windvane.util import now  # noqa: E402


def leave(path, mode=0o444):
    """An empty file at `path` with `mode`: a killed sandbox's placeholder at 0444, git's own crash lock at 0644."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("")
    path.chmod(mode)
    return path


def mountinfo(proc, *points):
    """A fake /proc with one process whose mount table lists `points` (a running sandboxed command's binds)."""
    (proc / "4242").mkdir(parents=True, exist_ok=True)
    lines = [f"{i} 1 0:{i} / {pt} ro,relatime - none none ro" for i, pt in enumerate(["/", *points], 30)]
    (proc / "4242/mountinfo").write_text("\n".join(lines) + "\n")


@unittest.skipUnless(sys.platform.startswith("linux"), "only Linux's sandbox makes them")
class StalePlaceholders(unittest.TestCase):
    def setUp(self):
        self.s = Sandbox()
        self.addCleanup(self.s.close)
        self.proc = self.s.tmp / "proc"
        mountinfo(self.proc)
        for patch in (mock.patch.object(jobs, "PROC", str(self.proc)), mock.patch.dict(os.environ)):
            patch.start()
            self.addCleanup(patch.stop)
        os.environ.pop("SANDBOX_RUNTIME", None)  # these tests' own commands may run in one
        self.p = Project(self.s.root)
        self.lock = self.s.root / ".git/config.lock"

    def doctor(self, **kw):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), mock.patch.object(doctor, "checks", return_value=[("ok", "x", "")]):
            if kw:
                doctor.main(self.p, **kw)
        return out.getvalue()

    def warnings(self):
        return [what for _, what, _ in doctor.placeholder_warnings(self.p)]

    def test_doctor_names_a_stale_config_lock_and_clean_removes_it_so_push_u_sets_the_upstream(self):
        leave(self.lock)
        self.s.git("switch", "-q", "-c", "t-001-x")
        push = subprocess.run(["git", "push", "-q", "-u", "origin", "t-001-x"], cwd=self.s.root, capture_output=True,
                              text=True)
        self.assertIn("could not lock config file", push.stderr)  # the failure this is about
        self.assertEqual(self.warnings(), ["1 empty read-only file a killed sandboxed command left in the repo's git "
                                           "dir (.git/config.lock)"])
        self.assertIn("-> windvane doctor --clean", "  -> ".join(next(
            (what, fix) for _, what, fix in doctor.checks(self.p) if "read-only file" in what)))
        out = self.doctor(clean=True)
        self.assertIn("removed 1 stale sandbox placeholder from the repo's git dir: .git/config.lock", out)
        self.assertFalse(self.lock.exists())
        self.s.git("push", "-q", "-u", "origin", "t-001-x")
        self.assertEqual(self.s.git("rev-parse", "--abbrev-ref", "@{upstream}"), "origin/t-001-x")

    def test_gits_own_writable_lock_and_an_empty_readonly_source_file_are_left_alone(self):
        leave(self.lock, 0o644)
        src = leave(self.s.root / "src/x.py")
        self.assertEqual(self.warnings(), [])
        self.doctor(clean=True)
        self.assertTrue(self.lock.exists() and src.exists())

    def test_one_a_live_sandbox_mounts_over_stays(self):
        leave(self.lock)
        mountinfo(self.proc, str(self.lock))
        self.assertEqual(self.warnings(), [])
        out = self.doctor(clean=True)
        self.assertTrue(self.lock.exists())
        self.assertNotIn("placeholder", out)

    def test_the_checkout_and_the_state_dir_are_named_per_place(self):
        leave(self.s.root / ".mcp.json")
        leave(self.s.root / ".claude/launch.json")
        leave(self.p.state / "trials/.claude/settings.json")
        self.assertEqual(self.warnings(), [
            "2 empty read-only files a killed sandboxed command left in the checkout (.mcp.json, .claude/launch.json)",
            "1 empty read-only file a killed sandboxed command left in windvane's state dir "
            "(sandbox/trials/.claude/settings.json)"])
        self.assertIn("would remove 2 stale sandbox placeholders from the checkout", self.doctor(dry_run=True))
        self.assertTrue((self.s.root / ".mcp.json").exists())
        self.doctor(clean=True)
        self.assertEqual(self.warnings(), [])

    def test_a_run_start_clears_the_lock_and_only_names_the_checkouts_own(self):
        leave(self.lock)
        mcp = leave(self.s.root / ".mcp.json")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            driver.Pilot(self.p, args()).run(now() + timedelta(minutes=5))
        log = out.getvalue()
        self.assertIn("removed 1 stale sandbox placeholder from the repo's git dir: .git/config.lock", log)
        self.assertIn("1 stale sandbox placeholder in the checkout (.mcp.json): left there -> windvane doctor "
                      "--clean", log)
        self.assertFalse(self.lock.exists())
        self.assertTrue(mcp.exists())  # the checkout is the user's

    def test_a_lock_held_at_one_look_goes_at_the_next(self):
        leave(self.lock)
        pilot = driver.Pilot(self.p, args())
        mountinfo(self.proc, str(self.lock))
        with contextlib.redirect_stdout(io.StringIO()):
            pilot.clear_placeholders(between=True)
        self.assertTrue(self.lock.exists())
        mountinfo(self.proc)  # that sandboxed command ended
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            pilot.clear_placeholders(between=True)
        self.assertFalse(self.lock.exists())
        self.assertIn("removed 1 stale sandbox placeholder from the repo's git dir", out.getvalue())

    def test_inside_a_sandbox_nothing_goes_as_it_cant_see_the_others(self):
        leave(self.lock)
        os.environ["SANDBOX_RUNTIME"] = "1"
        self.assertIn("can't be told from live ones inside the Bash sandbox", self.warnings()[0])
        self.assertIn("left the empty read-only files", self.doctor(clean=True))
        self.assertTrue(self.lock.exists())

    def test_only_locks_count_in_the_git_dir(self):
        leave(self.s.root / ".git/description2")
        self.assertEqual(self.warnings(), [])

    def test_mount_points_are_unescaped(self):
        mountinfo(self.proc, "/a\\040b/.mcp.json")
        self.assertEqual(jobs.mount_points(self.proc), {"/", "/a b/.mcp.json"})
        self.assertIsNone(jobs.mount_points(self.s.tmp / "none"))

    def test_an_unreadable_proc_tells_none_stale(self):
        leave(self.lock)
        with mock.patch.object(jobs, "PROC", str(self.s.tmp / "none")):
            self.assertEqual(self.warnings(), [])
            self.assertEqual(jobs.remove_placeholders([self.lock]), [])
        self.assertTrue(self.lock.exists())


if __name__ == "__main__":
    unittest.main()
