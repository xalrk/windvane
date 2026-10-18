"""T-196: held.json and held_why.json have three writers (the driver, `windvane plan` and `windvane held`). Each wrote
them with a load, change, save of its own copy, so a branch one of them held while another pruned was saved over:
never pushed again, and no longer listed by `status`."""
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from windvane import held, planner  # noqa: E402


def waiting_on_lock(path):
    """Whether some writer is blocked on `path`'s flock, from the kernel's table of locks: a blocked request is listed
    with `->` and the file's inode."""
    try:
        ino = os.stat(path).st_ino
    except FileNotFoundError:  # no writer ever took it
        return False
    lines = Path("/proc/locks").read_text().splitlines()
    return any("->" in line and line.split()[-3].endswith(f":{ino}") for line in lines)


class Writers(unittest.TestCase):
    def setUp(self):
        d = tempfile.TemporaryDirectory(prefix="windvane-test-")
        self.addCleanup(d.cleanup)
        self.p = SimpleNamespace(state=Path(d.name), cfg={"forge": "git"})

    def read(self, name="held.json"):
        return json.loads((self.p.state / name).read_text())

    def test_a_second_writer_waits_for_the_first_and_both_holds_survive(self):
        """The first writer stops between reading the files and saving them (in `held_note`, which every hold calls
        there). The second must wait for it, not save first and then be saved over."""
        inside, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        note, first = held.held_note, []

        def paused(n):
            if threading.current_thread() in first:
                inside.set()
                release.wait(30)
            return note(n)

        with mock.patch.object(held, "held_note", paused):
            t1 = threading.Thread(target=held.hold, args=(self.p, "plan-a", "push", "network down"))
            first.append(t1)
            t1.start()
            self.assertTrue(inside.wait(10))
            t2 = threading.Thread(target=held.hold, args=(self.p, "plan-b", "land", "PR can't merge"))
            t2.start()
            if Path("/proc/locks").exists():
                end = time.monotonic() + 10
                while not waiting_on_lock(self.p.state / "held.json.lock") and time.monotonic() < end:
                    time.sleep(0.01)  # until the kernel lists the second writer as blocked on the lock
                self.assertTrue(waiting_on_lock(self.p.state / "held.json.lock"),
                                "the second writer didn't wait for the first one's lock")
            else:
                t2.join(0.5)
                self.assertTrue(t2.is_alive(), "the second writer didn't wait for the first one's lock")
            self.assertTrue(t2.is_alive())
            release.set()
            t1.join(10)
            t2.join(10)
        self.assertEqual(self.read(), {"plan-a": "push", "plan-b": "land"})
        self.assertEqual({b: n["why"] for b, n in self.read("held_why.json").items()},
                         {"plan-a": "network down", "plan-b": "PR can't merge"})

    def test_a_prune_keeps_a_branch_held_while_it_looked(self):
        """`held_branches` (the CLI's `windvane held`, a run's start) asks git and gh about each branch, then drops
        the ones done. A branch the driver holds meanwhile wasn't asked about: it stays."""
        held.hold(self.p, "plan-done", "push", "network down")

        def looked(p, h):
            held.hold(p, "plan-new", "push", "network down")  # the driver, while the prune looked
            return {}  # plan-done is done
        with mock.patch.object(held, "held_here", looked):
            self.assertEqual(held.held_branches(self.p), {})
        self.assertEqual(self.read(), {"plan-new": "push"})
        self.assertEqual(list(self.read("held_why.json")), ["plan-new"])

    def test_unhold_drops_the_branch_and_its_reason_only(self):
        for b in ("plan-a", "plan-b"):
            held.hold(self.p, b, "push", f"{b} refused")
        held.unhold(self.p, ["plan-a", "plan-gone"])
        self.assertEqual(self.read(), {"plan-b": "push"})
        self.assertEqual(list(self.read("held_why.json")), ["plan-b"])

    def test_a_land_stays_a_land_and_the_first_time_held_stays(self):
        held.hold(self.p, "plan-a", "land", "PR can't merge")
        at = self.read("held_why.json")["plan-a"]["at"]
        held.hold(self.p, "plan-a", "push")
        self.assertEqual(self.read(), {"plan-a": "land"})
        self.assertEqual(self.read("held_why.json")["plan-a"], {"why": "PR can't merge", "at": at})


class ClosedPlans(unittest.TestCase):
    """closed_plans.json: `windvane plan` and the driver's replan both note the plans closed unmerged in it."""
    def test_a_plan_noted_while_gh_was_asked_stays(self):
        d = tempfile.TemporaryDirectory(prefix="windvane-test-")
        self.addCleanup(d.cleanup)
        p = SimpleNamespace(state=Path(d.name), cfg={"forge": "github"}, periodic={})
        f = p.state / "closed_plans.json"
        f.write_text(json.dumps(["plan-old"]))

        def asked(p, *a):
            if "--head" in a:
                f.write_text(json.dumps(["plan-old", "plan-other"]))  # the other writer, while gh answered
                return [{"state": "CLOSED"}]
            return []  # no open PR: plan-old stays closed
        with mock.patch.object(planner.net, "own_prs", asked):
            self.assertEqual(planner.closed_prs(p, {"plan-old", "plan-new"}), {"plan-old", "plan-new"})
        self.assertEqual(json.loads(f.read_text()), ["plan-new", "plan-old", "plan-other"])


if __name__ == "__main__":
    unittest.main()
