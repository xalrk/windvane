"""Picking a ticket id and reserving it is one step under a lock in the state dir, and every picker counts the main
checkout's ticket files: a `windvane new` leaves its ticket there uncommitted, where a plan's own worktree doesn't
show it, and a `new` while a plan is between its pick and its claim (or the other way round) would take the same id."""
import contextlib
import fcntl
import io
import os
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from windvane import cli, planner  # noqa: E402
from windvane.config import Project  # noqa: E402
from helpers import Sandbox  # noqa: E402

REPO = Path(__file__).resolve().parents[1]


class IdPickLock(unittest.TestCase):
    def setUp(self):
        self.s = Sandbox()
        self.addCleanup(self.s.close)
        self.addCleanup(os.chdir, os.getcwd())  # `-C` changes into the sandbox, which close() removes
        self.s.ticket("T-001")
        self.s.publish()
        self.p = Project(self.s.root)
        self.tree = self.s.tmp / "plan-tree"  # a plan's own worktree: main as pushed, none of the checkout's files
        self.s.git("worktree", "add", "-q", "--detach", str(self.tree), "origin/main")

    def new(self, title="next"):
        """`windvane new`'s ticket id."""
        with contextlib.redirect_stdout(io.StringIO()) as out:
            cli.main(["-C", str(self.s.root), "new", title])
        return Path(out.getvalue().strip()).stem

    def test_the_plan_skips_a_ticket_new_left_uncommitted_in_the_checkout(self):
        self.assertEqual(self.new(), "T-002")
        prompts = []
        with mock.patch.object(planner, "run_claude", side_effect=lambda *a, **k: prompts.append(a[3]) or ("", None, "")):
            planner.plan(self.p, "more", use_followups=False, max_new=3)  # in its own worktree, p.plan_dir
        self.assertFalse((self.p.plan_dir / ".windvane/tickets/T-002.md").exists())
        self.assertIn("## Ids for new tickets, in order (at most 3)\nT-003, T-004, T-005", prompts[0])

    def test_free_id_counts_the_main_checkout_from_any_tree(self):
        self.s.ticket("T-007")  # uncommitted in the main checkout
        self.assertEqual(planner.free_id(self.p, self.tree, ask_forge=False), "T-008")

    def slowed(self, *names):
        """Each of planner's `names` waits a moment after it runs: the window between a pick and its reservation,
        wide enough that a picker that doesn't hold the lock across both loses the race every time."""
        def slow(f):
            return lambda *a, **k: (f(*a, **k), time.sleep(0.05))[0]
        return contextlib.ExitStack(), [mock.patch.object(planner, n, slow(getattr(planner, n))) for n in names]

    def test_a_plan_and_a_new_started_at_once_get_disjoint_ids(self):
        stack, patches = self.slowed("free_id", "known_ids")
        with stack:
            for x in patches:
                stack.enter_context(x)
            for round in range(15):
                got, go = {}, threading.Barrier(2)
                plan = threading.Thread(target=lambda: (go.wait(), got.update(
                    plan=planner.pick_ids(self.p, "plan", 3, self.tree, ask_forge=False))))
                new = threading.Thread(target=lambda: (go.wait(), got.update(new=self.new(f"round {round}"))))
                for t in (plan, new):
                    t.start()
                for t in (plan, new):
                    t.join(10)
                self.assertNotIn(got["new"], got["plan"], f"round {round}: {got}")
                planner.release_ids(self.p, "plan")
                (self.s.root / f".windvane/tickets/{got['new']}.md").unlink()

    def test_a_picker_killed_holding_the_lock_blocks_no_one(self):
        holder = subprocess.Popen(
            [sys.executable, "-c", "import sys, time; sys.path.insert(0, sys.argv[1]);"
             "from windvane import planner; from windvane.config import Project;"
             "cm = planner.id_lock(Project(sys.argv[2])); cm.__enter__(); print('held', flush=True); time.sleep(60)",
             str(REPO), str(self.s.root)], stdout=subprocess.PIPE, text=True)
        self.addCleanup(holder.kill)
        self.assertEqual(holder.stdout.readline().strip(), "held")
        with open(self.p.state / "ids.lock", "w") as f, self.assertRaises(BlockingIOError):
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)  # it really holds it
        holder.kill()
        holder.wait()
        holder.stdout.close()
        start = time.monotonic()
        self.assertEqual(planner.pick_ids(self.p, "plan", 2, self.tree, ask_forge=False), ["T-002", "T-003"])
        self.assertLess(time.monotonic() - start, 5)
        self.assertEqual(self.new(), "T-004")  # the plan's claim counts

    def test_a_periodic_pick_keeps_what_the_chain_was_handed(self):
        first = planner.pick_ids(self.p, "periodic", 2, self.tree, own="periodic", ask_forge=False)
        again = planner.pick_ids(self.p, "periodic", 2, self.tree, own="periodic", ask_forge=False, keep=first)
        self.assertEqual((first, again), (["T-002", "T-003"], ["T-002", "T-003"]))  # its own claim: not skipped
        self.assertEqual(planner.claimed_ids(self.p), {"T-002", "T-003"})
        self.assertEqual(self.new(), "T-004")


if __name__ == "__main__":
    unittest.main()
