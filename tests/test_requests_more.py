"""`windvane request`'s gaps (T-115): an accept into a ticket held on a branch is done, not ticketed again; the role
comes from where the command runs, not only from the env a worker's shell sets; a project's verdicts are matched by
its root, not its name; and a question's push says which request asks."""
import os
import subprocess
import sys
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from windvane import driver, requests  # noqa: E402
from windvane.config import Project  # noqa: E402
import test_requests  # noqa: E402
from test_requests import TRIAGE  # noqa: E402


class RequestsMore(unittest.TestCase):
    R = test_requests.Requests  # its helpers, not its tests (a subclass would run them twice)
    make, cli, file, inbox, triage = R.make, R.cli, R.file, R.inbox, R.triage

    def pilot(self):
        return driver.Pilot(self.p, __import__("test_pipeline").args())

    def plan_branch(self, tid, branch):
        """A plan that didn't merge: `branch` adds ticket `tid`, main doesn't have it."""
        self.s.git("checkout", "-q", "-b", branch)
        self.s.ticket(tid)
        self.s.git("add", "-A")
        self.s.git("commit", "-qm", f"Plan: {tid}")
        self.s.git("checkout", "-q", "main")

    def test_an_accept_into_a_ticket_held_on_a_plan_branch_is_done_not_offered_again(self):
        self.make(TRIAGE)
        self.plan_branch("T-009", "plan-20261008-120000")
        self.plan_branch("T-010", "plan-20261008-130000")
        self.s.git("push", "-q", "origin", "plan-20261008-130000")  # one whose PR waits on the forge
        self.s.git("branch", "-q", "-D", "plan-20261008-130000")
        self.file("Retry the check on a flaky network")
        self.file("A stats column for cost")
        self.file("Quiet hours on Sunday")
        pilot = self.pilot()
        self.assertEqual(pilot.periodic_due()["name"], "requests")
        pilot.verdicts(requests.files(False), "REQUEST: R-001 accept T-009\nREQUEST: R-002 accept T-010\n"
                       "REQUEST: R-003 accept T-011", [], {}, "requests")
        done = self.inbox(done=True)
        self.assertIn("- Verdict: accepted → T-009 (held on plan-20261008-120000)", done["R-001"])
        self.assertIn("- Verdict: accepted → T-010 (held on plan-20261008-130000)", done["R-002"])
        self.assertEqual(list(self.inbox()), ["R-003"])  # T-011 is nowhere: still open
        self.assertEqual([f.stem for f in requests.pending()], ["R-003"])
        pilot.requests_tried.add("R-003")
        self.assertIsNone(pilot.periodic_due())  # nothing held is offered to the next round
        _, out = self.cli("status")
        self.assertIn("windvane request R-001 accepted → windvane's T-009 (held on plan-20261008-120000)", out)

    def slot(self, name, branch=None):
        """A tree in the project's state dir, as the driver makes them, and the cwd moved into it."""
        where = self.p.state / name
        self.s.git("worktree", "add", "-q", *(["-b", branch] if branch else ["--detach"]), str(where))
        os.chdir(where)
        return where

    def test_a_desk_role_set_in_a_slot_worktree_is_still_a_workers(self):
        self.make(TRIAGE)
        self.triage([("Rename the critic", "WANT=question")])
        self.slot("work-2", "t-009-x")
        desk = {requests.ROLE: "desk"}  # and no WINDVANE_SESSION: the run is counted by its tree and branch
        self.assertEqual(self.file("Retry the check on a flaky network", **desk)[0], 0)
        self.assertEqual(self.file("A stats column for cost", **desk)[0], 0)
        code, out = self.file("Quiet hours on Sunday", **desk)
        self.assertEqual(code, 1)
        self.assertIn("refused: a worker run files at most 2 requests", out)
        code, out = self.file("Quiet hours on Sunday", **{requests.ROLE: "desk:x"})  # any desk role, not just "desk"
        self.assertEqual(code, 1)
        self.assertIn("refused: a worker run files at most 2 requests", out)
        self.assertIn("- Role: worker", self.inbox()["R-002"])
        with unittest.mock.patch.dict(os.environ, desk):
            code, out = self.cli("request", "--answer", "R-001", "A")
        self.assertEqual((code, out.strip()), (1, "windvane request: only the user answers, at the desk "
                                                  "(this is a worker session)"))
        self.assertIn("- Verdict: question: A or B?", self.inbox()["R-001"])
        self.slot("ci-2")
        code, out = self.file("Quiet hours on Sunday", **desk)
        self.assertEqual(code, 1)
        self.assertIn("only desk sessions and workers file requests (this is a ci session)", out)
        os.chdir(self.s.root)  # the user's checkout: the desk again
        self.assertEqual(self.cli("request", "--answer", "R-001", "A")[0], 0)

    def app(self, parent):
        root = self.s.tmp / parent / "app"
        root.mkdir(parents=True)
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=root, check=True)
        subprocess.run(["git", "commit", "-q", "--allow-empty", "-m", "init"], cwd=root, check=True)
        return root

    def test_two_projects_named_alike_each_see_only_their_own_verdict(self):
        self.make()
        roots = [self.app("a"), self.app("b")]
        for root, title in zip(roots, ("Retry the check on a flaky network", "A stats column for cost")):
            os.chdir(root)
            self.assertEqual(self.file(title)[0], 0)
        os.chdir(self.s.root)
        requests.apply(requests.files(False), "REQUEST: R-001 reject not now\nREQUEST: R-002 reject later",
                       set(), lambda t: False, lambda m: None)
        a, b = (Project(r) for r in roots)
        self.assertEqual((a.name, b.name), ("app", "app"))
        self.assertEqual(requests.unseen(a), ["R-001 rejected: not now  (Retry the check on a flaky network)"])
        self.assertEqual(requests.unseen(b), ["R-002 rejected: later  (A stats column for cost)"])
        # an R-file from before roots were noted is still matched, by name
        old = requests.inbox() / "done" / "R-003.md"
        old.write_text("# R-003: Old one\n- From: app\n- Verdict: rejected: old\n")
        self.assertEqual(requests.unseen(a), ["R-003 rejected: old  (Old one)"])

    def test_a_question_push_carries_the_requests_title(self):
        self.make(TRIAGE)
        self.file("Rename the critic")
        pushes = []
        with unittest.mock.patch("windvane.periodic.push", lambda *a: pushes.append(a[:2])):
            self.pilot().verdicts(requests.files(False), "REQUEST: R-001 question A or B?", [], {}, "requests")
        self.assertEqual(pushes, [("windvane request R-001 asks", "Request: Rename the critic\nQuestion: A or B?\n"
                                   'Not answerable from the phone: answer at the desk: windvane request --answer '
                                   'R-001 "<your answer>"')])


if __name__ == "__main__":
    unittest.main()
