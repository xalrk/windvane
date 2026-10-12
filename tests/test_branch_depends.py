"""A worker that finds its ticket can't go on until another ticket merges adds that id to the `Depends on:` line on
its own branch: the queue reads the line there too, so the ticket waits instead of being resumed run after run, and
its PARTIAL run doesn't count toward being set aside."""
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from windvane import driver  # noqa: E402
from windvane.config import Project  # noqa: E402
from windvane.queue import Queue  # noqa: E402
from windvane.util import last_log  # noqa: E402
from windvane.worktree import ensure_worktree  # noqa: E402
from helpers import Sandbox  # noqa: E402
from test_parallel import args  # noqa: E402


class BranchDepends(unittest.TestCase):
    def setUp(self):
        self.s = Sandbox()
        self.addCleanup(self.s.close)
        self.s.ticket("T-001")
        self.s.ticket("T-002")
        self.s.ticket("T-003")
        self.s.publish()
        self.p = Project(self.s.root)

    def branch(self, name, tid, deps, cwd=None):
        """`name` off main, pushed, with `tid`'s header saying `Depends on: <deps>.`"""
        s, cwd = self.s, cwd or self.s.root
        s.git("switch", "-q", "-c", name, "origin/main", cwd=cwd)
        f = Path(cwd) / f".windvane/tickets/{tid}.md"
        f.write_text(f"# {tid}: do {tid}\nDepends on: {deps}.\n")
        s.git("commit", "-q", "--allow-empty", "-am", f"{tid}: waits", cwd=cwd)
        s.git("push", "-q", "-u", "origin", name, cwd=cwd)
        s.git("switch", "-q", "main", cwd=cwd) if cwd == s.root else None

    def merge(self, name, tid):
        s = self.s
        s.git("switch", "-q", "-c", name, "main")
        s.write(f"{tid}.txt", "x\n")
        s.git("add", "-A")
        s.git("commit", "-qm", tid)
        s.git("switch", "-q", "main")
        s.git("merge", "-q", "--no-ff", name, "-m", f"Merge {tid}: do {tid} (branch {name})")
        s.git("push", "-q", "origin", "main")

    def test_a_branch_dependency_makes_the_ticket_wait(self):
        self.branch("t-001-x", "T-001", "T-002")
        q = Queue(self.p)
        self.assertEqual(q.status("T-001"), "waiting")
        row = next(r for r in q.listing().splitlines() if r.startswith("T-001"))
        self.assertIn("waiting on: T-002 (from its branch)", row)
        self.assertEqual(q.pick()[:2], ("T-002", "todo"))

    def test_once_it_merges_the_ticket_is_resumed_first(self):
        self.branch("t-001-x", "T-001", "T-002")
        self.merge("t-002-y", "T-002")
        q = Queue(self.p)
        self.assertEqual(q.status("T-001"), "wip")
        self.assertEqual(q.pick(), ("T-001", "wip", ["t-001-x"]))

    def test_the_user_on_a_branch_changes_nothing(self):
        self.branch("t-001-x", "T-001", "the user")
        q = Queue(self.p)
        self.assertEqual(q.status("T-001"), "wip")
        self.assertEqual(q.pick()[0], "T-001")

    def test_a_branch_cant_drop_mains_dependency(self):
        self.s.ticket("T-001", "T-002")
        self.s.publish()
        self.branch("t-001-x", "T-001", "nothing")
        q = Queue(self.p)
        self.assertEqual(q.status("T-001"), "waiting")
        self.assertIn("(waiting on: T-002)", q.listing())

    def test_two_tickets_waiting_on_each_others_branches_both_wait(self):
        self.branch("t-001-x", "T-001", "T-002")
        self.branch("t-002-y", "T-002", "T-001")
        q = Queue(self.p)
        self.assertEqual((q.status("T-001"), q.status("T-002")), ("waiting", "waiting"))
        self.assertEqual(q.pick()[:2], ("T-003", "todo"))

    def partial(self, deps, skip=()):
        """A worker run on T-001 that pushes its branch with `deps` and ends PARTIAL, one run short of set aside."""
        ensure_worktree(self.p, self.p.work_dir, setup=False)
        self.branch("t-001-x", "T-001", deps, cwd=self.p.work_dir)
        pl = driver.Pilot(self.p, args())
        pl.queue = Queue(self.p)
        pl.skip |= set(skip)
        before = self.p.ap["partials"] - 1
        pl.partials["T-001"] = before
        with mock.patch.object(driver, "push") as pushed:
            pl.handle("T-001", "RESULT: PARTIAL T-001 waits for T-002", None)
        return pl, before, pushed

    def test_a_partial_run_waiting_on_a_merge_isnt_set_aside(self):
        pl, before, pushed = self.partial("T-002")
        self.assertEqual(pl.partials["T-001"], before)
        self.assertNotIn("T-001", pl.skip)
        pushed.assert_not_called()
        self.assertIn("T-001: waits for T-002 to merge (its branch says so)", last_log(self.p)[0])

    def test_a_partial_run_with_no_new_dependency_still_counts(self):
        pl, before, pushed = self.partial("nothing")
        self.assertEqual(pl.partials["T-001"], before + 1)
        self.assertIn("T-001", pl.skip)
        self.assertIn("Set aside", pushed.call_args[0][0])

    def test_an_unknown_id_on_a_branch_is_no_wait(self):
        pl, before, _ = self.partial("T-099")
        self.assertEqual(pl.queue.status("T-001"), "wip")
        self.assertEqual(pl.partials["T-001"], before + 1)

    def test_a_line_left_unpushed_is_no_wait(self):
        ensure_worktree(self.p, self.p.work_dir, setup=False)
        w = self.p.work_dir
        self.s.git("switch", "-q", "-c", "t-001-x", "origin/main", cwd=w)
        (w / ".windvane/tickets/T-001.md").write_text("# T-001: do T-001\nDepends on: T-002.\n")
        self.s.git("commit", "-qam", "waits", cwd=w)
        pl = driver.Pilot(self.p, args())
        pl.queue = Queue(self.p)
        pl.handle("T-001", "RESULT: PARTIAL T-001 waits for T-002", None)
        self.assertEqual(pl.partials["T-001"], 1)  # the pushed branch is what the queue reads: it counts

    def held(self, pushed):
        self.assertEqual(pushed.call_count, 1)
        title, body = pushed.call_args[0][:2]
        self.assertEqual(title, "Held: do T-001 (T-001)")
        self.assertIn("take it out of `Depends on:` in .windvane/tickets/T-001.md on t-001-x", body)
        return body

    def test_waiting_on_a_parked_ticket_says_so_once(self):
        self.s.write(".windvane/QUEUE.md", "# Queue\n- parked: T-002 (later)\n")
        self.s.publish()
        pl, before, pushed = self.partial("T-002")
        self.assertIn("Its branch waits for T-002 (parked), which can't merge first", self.held(pushed))
        self.assertEqual(pl.partials["T-001"], before)

    def test_waiting_on_a_set_aside_ticket_says_so(self):
        _, _, pushed = self.partial("T-002", skip=["T-002"])
        self.assertIn("T-002 (set aside)", self.held(pushed))

    def test_waiting_on_a_ticket_that_waits_back_says_so(self):
        self.branch("t-002-y", "T-002", "T-003")
        self.branch("t-003-z", "T-003", "T-001")
        _, _, pushed = self.partial("T-002")
        self.assertIn("T-002 (waits for T-001 too)", self.held(pushed))


if __name__ == "__main__":
    unittest.main()
