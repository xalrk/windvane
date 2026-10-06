"""A running ticket's files are its Touches line plus what its branch has changed already: a Touches line written
before the work can miss a file, and a ticket beside it on that file pays for a conflict bounce."""
import os
import subprocess
import sys
import unittest
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from windvane import driver  # noqa: E402
from windvane.config import Project  # noqa: E402
from windvane.queue import Queue  # noqa: E402
from windvane.util import now  # noqa: E402
from windvane.worktree import ensure_worktree  # noqa: E402
from helpers import Sandbox  # noqa: E402
from test_parallel import args  # noqa: E402

WAIT = "overlaps T-001 on b.py (changed, not on its Touches line)"


class Overlap(unittest.TestCase):
    def setUp(self):
        self.s = Sandbox()

    def tearDown(self):
        self.s.close()

    def pilot(self, **touches):
        for tid, paths in touches.items():
            self.s.ticket(tid.replace("_", "-"), touches=paths)
        self.s.publish()
        self.p = Project(self.s.root)
        pilot = driver.Pilot(self.p, args())
        pilot.queue = Queue(self.p)
        return pilot

    def wd(self, *cmd):
        subprocess.run(cmd, cwd=self.p.work_dir, check=True, capture_output=True)

    def running_t001(self, branch="t-001-work"):
        """T-001 running in slot 0, on `branch` off main, with the Touches line `a.py`."""
        pilot = self.pilot(T_001="a.py", T_002="b.py", T_003="c.py")
        ensure_worktree(self.p, self.p.work_dir, setup=False)
        self.wd("git", "switch", "-q", "-c", branch, self.p.upstream)
        pilot.running = {"T-001": 0}
        return pilot

    def commit(self, *files):
        for f in files:
            (self.p.work_dir / f).write_text("x\n")
        self.wd("git", "add", "-A")
        self.wd("git", "commit", "-qm", "build")

    def test_a_file_committed_outside_touches_holds_the_ticket_on_it(self):
        pilot = self.running_t001()
        self.commit("a.py", "b.py", ".windvane/QUESTIONS.md")
        self.assertEqual(pilot.clash("T-002", ["T-001"]), WAIT)
        self.assertEqual(pilot.clash("T-003", ["T-001"]), "")  # c.py: nothing in common
        self.assertEqual(pilot.outside(["T-001"]), {"T-001": ["b.py"]})  # the queue's own files don't count
        pilot.pick([1])
        self.assertIn(f"T-002 waits: {WAIT}", pilot.waits)

    def test_an_uncommitted_change_holds_it_too(self):
        pilot = self.running_t001()
        (self.p.work_dir / "b.py").write_text("x\n")  # untracked
        self.assertEqual(pilot.clash("T-002", ["T-001"]), WAIT)
        self.wd("git", "add", "b.py")  # staged
        self.assertEqual(pilot.clash("T-002", ["T-001"]), WAIT)

    def test_the_probe_leaves_a_live_workers_index_alone(self):
        pilot = self.running_t001()
        self.commit("a.py")
        os.utime(self.p.work_dir / "a.py", (1, 1))  # stale stat data: a plain `git status` would rewrite the index
        index = Path(subprocess.run(["git", "rev-parse", "--path-format=absolute", "--git-path", "index"],
                                    cwd=self.p.work_dir, capture_output=True, text=True).stdout.strip())
        before = index.stat().st_ino
        self.assertEqual(pilot.outside(["T-001"]), {"T-001": []})
        self.assertEqual(index.stat().st_ino, before)  # not taken under index.lock and replaced

    def test_a_branch_that_kept_to_its_touches_runs_beside(self):
        pilot = self.running_t001()
        self.commit("a.py")
        self.assertEqual(pilot.clash("T-002", ["T-001"]), "")
        pilot.pick([1])
        self.assertEqual(pilot.waits, [])

    def test_another_tickets_head_left_in_the_slot_doesnt_count(self):
        pilot = self.running_t001(branch="t-009-old")  # a bounced ticket's, before T-001's worker switches
        self.commit("b.py")
        self.wd("git", "switch", "-q", "--detach")
        self.assertEqual(pilot.clash("T-002", ["T-001"]), "")

    def test_without_git_it_goes_by_touches_alone(self):
        pilot = self.pilot(T_001="a.py", T_002="b.py")
        pilot.running = {"T-001": 3}  # its worktree isn't there
        self.assertEqual(pilot.outside(["T-001"]), {"T-001": []})
        self.assertEqual(pilot.clash("T-002", ["T-001"]), "")
        self.assertEqual(pilot.clash("T-002", ["T-001"], {"T-001": ["b.py"]}), WAIT)

    def test_in_a_run_the_ticket_waits_until_the_running_one_merges(self):
        os.environ["FAKE_WORK_S"] = "1.5"
        self.s.ticket("T-001", touches="a.py")
        self.s.ticket("T-002", touches="T-001.txt")  # the fake worker writes <ID>.txt: off T-001's line
        self.s.publish()
        self.p = Project(self.s.root)
        ensure_worktree(self.p, self.p.work_dir, setup=False)
        (self.p.work_dir / "T-001.txt").write_text("from a run that was cut off\n")  # T-001 resumes in slot 0
        pilot = driver.Pilot(self.p, args())
        pilot.run(now() + timedelta(minutes=15))
        self.assertEqual(sorted(pilot.merged), ["T-001", "T-002"])
        log = (self.p.state / "autopilot.log").read_text()
        self.assertIn("T-001: slot 0", log)
        self.assertEqual(log.count("T-002 waits: overlaps T-001 on T-001.txt (changed, not on its Touches line)"), 1)
        self.assertLess(log.index("T-001: merged"), log.index("T-002: slot"))

    def test_the_merge_logs_what_was_changed_outside_touches(self):
        pilot = self.pilot(T_001="a.py", T_002="T-002.txt")  # the fake worker writes <ID>.txt
        pilot.run(now() + timedelta(minutes=15))
        self.assertEqual(sorted(pilot.merged), ["T-001", "T-002"])
        log = (self.p.state / "autopilot.log").read_text()
        self.assertEqual(log.count("changed outside its Touches line"), 1)
        self.assertIn("T-001: changed outside its Touches line: T-001.txt", log)


if __name__ == "__main__":
    unittest.main()
