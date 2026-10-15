"""The empty stubs Claude Code leaves in a session's cwd under CLAUDE_CODE_SUBPROCESS_ENV_SCRUB: cleared from the
autopilot's own worktrees (only those, and only untracked, empty, named ones), so they're never committed and never
pin a slot to its last ticket."""
import sys
import unittest
from unittest import mock
from argparse import Namespace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from windvane import driver  # noqa: E402
from windvane.config import Project  # noqa: E402
from windvane.util import gout  # noqa: E402
from windvane.worktree import SCRUB_DIRS, SCRUB_STUBS, clear_scrub_stubs, ensure_worktree  # noqa: E402
from helpers import Sandbox  # noqa: E402


def make_stubs(path):
    for name in SCRUB_STUBS:
        (path / name).touch()
    for name in SCRUB_DIRS:
        (path / name).mkdir(parents=True, exist_ok=True)


class ScrubStubs(unittest.TestCase):
    def setUp(self):
        self.s = Sandbox()
        self.addCleanup(self.s.close)

    def test_only_untracked_empty_stubs_go(self):
        self.s.write("package.json", "")  # a project's own empty package.json is tracked: it stays
        self.s.publish("an empty package.json")
        p = Project(self.s.root)
        ensure_worktree(p, p.work_dir, setup=False)
        wd = p.work_dir
        make_stubs(wd)
        self.assertEqual(len(SCRUB_STUBS), 17)
        (wd / ".env").write_text("TOKEN=keep\n")  # non-empty: someone's real settings
        (wd / "notes.txt").touch()  # empty, but not a stub's name
        (wd / "node_modules/.bin/tool").touch()  # a dir with something in it stays
        gone = clear_scrub_stubs(p, wd)
        self.assertEqual(sorted(n for n in gone if not n.endswith("/")),
                         sorted(set(SCRUB_STUBS) - {".env", "package.json"}))
        self.assertEqual(gout(wd, "status", "--porcelain"), "?? .env\n?? node_modules/\n?? notes.txt")
        self.assertEqual((wd / ".env").read_text(), "TOKEN=keep\n")
        self.assertTrue((wd / "package.json").exists() and (wd / "node_modules/.bin/tool").exists())
        self.assertFalse((wd / ".claude").exists())  # its stub dirs went, then the folder they left empty
        self.assertIn("removed the empty stubs a session under CLAUDE_CODE_SUBPROCESS_ENV_SCRUB left: .env.local, ",
                      (p.state / "autopilot.log").read_text())

    def test_the_users_checkout_is_left_alone(self):
        p = Project(self.s.root)
        make_stubs(self.s.root)
        self.assertEqual(clear_scrub_stubs(p, self.s.root), [])
        self.assertTrue((self.s.root / ".env").exists() and (self.s.root / ".claude/agents").is_dir())

    def slot_after(self, leftover=""):
        """Slot 0's owner after a ticket whose worker left the stubs (and `leftover`, a file of real work)."""
        p = Project(self.s.root)
        a = Namespace(cap=75, week_cap=95, ticket_hours=1, min_slot=0, answer_wait=10, replan=0, goal="", parallel=1)
        pilot = driver.Pilot(p, a)
        self.assertTrue(pilot.prepare(0))

        def work(tid, kind, branches):
            make_stubs(pilot.wd)
            if leftover:
                (pilot.wd / leftover).write_text("half\n")
        pilot.work = work
        pilot.running["T-001"] = 0
        pilot.slot_run(0, "T-001", "new", [])
        return pilot.owner[0]

    def test_a_slot_holding_only_stubs_is_free(self):
        self.assertIsNone(self.slot_after())

    def test_real_leftovers_still_keep_the_slot(self):
        self.assertEqual(self.slot_after("half-done.txt"), "T-001")

    def test_a_kept_job_still_writing_there_keeps_its_files(self):
        with mock.patch.object(driver.jobs, "kept_jobs", return_value=["build"]):
            self.assertEqual(self.slot_after(), "T-001")
        self.assertTrue((Project(self.s.root).work_dir / "package.json").exists())

    def test_stubs_are_gone_before_the_worker_starts(self):
        p = Project(self.s.root)
        ensure_worktree(p, p.work_dir, setup=False)
        make_stubs(p.work_dir)
        a = Namespace(cap=75, week_cap=95, ticket_hours=1, min_slot=0, answer_wait=10, replan=0, goal="", parallel=1)
        self.assertTrue(driver.Pilot(p, a).prepare(0))
        self.assertEqual(gout(p.work_dir, "status", "--porcelain"), "")
        log = (p.state / "autopilot.log").read_text()
        self.assertEqual(log.count("removed the empty stubs"), 1)
        self.assertTrue(driver.Pilot(p, a).prepare(0))  # nothing left to clear: nothing said
        self.assertEqual((p.state / "autopilot.log").read_text(), log)


if __name__ == "__main__":
    unittest.main()
