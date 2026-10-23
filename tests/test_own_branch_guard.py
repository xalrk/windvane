"""Only the driver's own branches (`held.OWN_BRANCHES`) are ever landed or pushed: a branch named any other way would
carry tickets whose ids no picker counts. Their names skip one another machine pushed since the last fetch, and the
desk answers that didn't merge say how to merge them."""
import contextlib
import io
import os
import sys
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from windvane import cli, held, planner, preflight  # noqa: E402
from windvane.config import Project  # noqa: E402
from helpers import Sandbox  # noqa: E402

AT = datetime(2026, 10, 20, 12, 0, 0)
PROVISIONAL = ("# Q\n\n## Open\n\n## Provisional\n### T-001: Big or small?\n- **A**: big\n- **B**: small\n"
               "Recommendation: A, because.\nBuilt: A; to change it: SIZE in x.py\n\n## Answered\n")


class Guard(unittest.TestCase):
    def setUp(self):
        self.s = Sandbox()
        self.addCleanup(self.s.close)
        self.addCleanup(os.chdir, os.getcwd())
        self.s.ticket("T-001")
        self.s.publish()
        self.p = Project(self.s.root)

    def heads(self):
        return {line.split("refs/heads/")[1] for line in self.s.git("ls-remote", "--heads", "origin").splitlines()}

    def test_land_and_push_branch_refuse_a_name_not_their_own(self):
        """Its ticket would be on the remote with its id free: the next ticket written takes it again."""
        self.s.git("switch", "-q", "-c", "t-999-x")
        self.s.ticket("T-002")
        self.s.git("add", "-A")
        self.s.git("commit", "-qm", "a ticket on a branch of its own")
        self.assertNotIn("T-002", planner.pending_ids(self.p))  # what pushing it would leak
        for call in (lambda: held.land(self.p, self.s.root, "t-999-x", "Plan", "body", commit=False),
                     lambda: held.push_branch(self.p, self.s.root, "t-999-x")):
            with self.assertRaises(ValueError):
                call()
        self.assertEqual(self.heads(), {"main"})
        self.assertFalse((self.p.state / "held.json").exists())

    def test_a_name_another_clone_pushed_gets_a_suffix(self):
        other = self.s.tmp / "other"
        self.s.git("clone", "-q", str(self.s.remote), str(other), cwd=self.s.tmp)
        self.s.git("push", "-q", "origin", "HEAD:refs/heads/plan-20261020-120000", cwd=other)
        self.assertEqual(self.s.git("for-each-ref", "refs/remotes/origin/plan-*"), "")  # not fetched
        with mock.patch.object(planner, "now", lambda: AT):
            self.assertEqual(planner.branch_name(self.p, self.s.root, "plan"), "plan-20261020-120000-2")
            self.assertEqual(planner.branch_name(self.p, self.s.root, "answers"), "answers-20261020-120000")

    def test_a_plan_whose_name_another_clone_pushed_lands_on_a_suffix(self):
        """The whole plan run (fake `claude`), the other clone pushing while the planner session runs (after the
        plan's fetch): its push isn't refused as non-fast-forward, so the paid plan isn't held back; the other
        clone's branch is left as it was."""
        other = self.s.tmp / "other"
        self.s.git("clone", "-q", str(self.s.remote), str(other), cwd=self.s.tmp)
        self.s.git("commit", "-q", "--allow-empty", "-m", "theirs", cwd=other)
        theirs = self.s.git("rev-parse", "HEAD", cwd=other)

        def session(*a, **k):
            self.s.git("push", "-q", "origin", "HEAD:refs/heads/plan-20261020-120000", cwd=other)
            return real(*a, **k)

        real = planner.run_claude
        with mock.patch.object(planner, "now", lambda: AT), mock.patch.object(planner, "run_claude", session):
            self.assertGreater(planner.plan(self.p, "make it great"), 0)
        self.s.git("fetch", "-q", "origin", "plan-20261020-120000-2")
        self.assertIn(".windvane/tickets/T-002.md", self.s.git("ls-tree", "-r", "--name-only", "FETCH_HEAD"))
        self.assertEqual(self.s.git("ls-remote", "origin", "plan-20261020-120000").split()[0], theirs)
        self.assertEqual(held.load_json(self.p.state / "held.json", {}), {})
        self.assertIn("plan: 1 ticket pushed to plan-20261020-120000-2", (self.p.state / "autopilot.log").read_text())

    def test_a_held_branch_not_its_own_is_left_alone(self):
        """held.json edited by hand, or written by something else: the run's start skips it rather than raising."""
        self.s.git("branch", "t-999-x")
        held.hold(self.p, "t-999-x", "push", "network down")
        self.assertEqual(held.push_held(self.p), 0)
        self.assertEqual(self.heads(), {"main"})

    def test_a_remote_that_cant_be_asked_still_gives_a_name(self):
        self.s.git("branch", "plan-20261020-120000")  # the local check still counts
        self.s.git("remote", "set-url", "origin", str(self.s.tmp / "gone.git"))
        with mock.patch.object(planner, "now", lambda: AT):
            self.assertEqual(planner.branch_name(self.p, self.s.root, "plan"), "plan-20261020-120000-2")
            self.assertEqual(planner.branch_name(self.p, self.s.root, "periodic"), "periodic-20261020-120000")


class AnswersStep(unittest.TestCase):
    def setUp(self):
        self.s = Sandbox()
        self.addCleanup(self.s.close)
        self.addCleanup(os.chdir, os.getcwd())
        self.s.ticket("T-001")
        self.s.write(".windvane/QUESTIONS.md", PROVISIONAL)
        self.s.publish()

    def answer(self, where):
        with mock.patch.object(preflight, "land", lambda *a, **k: (False, where)), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            cli.main(["-C", str(self.s.root), "answer", "T-001", "B"])
        return out.getvalue()

    def test_an_unmerged_pr_names_its_merge_command(self):
        out = self.answer("https://github.com/o/r/pull/7")
        self.assertIn("they wait on https://github.com/o/r/pull/7 until you merge it: `gh pr merge 7 --merge`", out)

    def test_a_pushed_branch_names_the_merge_status_gives(self):
        self.assertIn("they wait on answers-1 until you merge it into main", self.answer("answers-1"))

    def test_a_pushed_branch_without_a_pr_names_the_pr_to_open(self):
        cfg = self.s.root / ".windvane/config.toml"
        cfg.write_text(cfg.read_text().replace('forge = "git"', 'forge = "github"'))
        self.s.publish("forge")
        self.assertIn("they wait on answers-1 until you open a PR from it and merge it", self.answer("answers-1"))

    def test_a_local_branch_keeps_its_own_step(self):
        where = "the local branch answers-1 (network down; the next run pushes it again)"
        out = self.answer(where)
        self.assertIn(f"they wait on {where}", out)
        self.assertNotIn("until you", out)


if __name__ == "__main__":
    unittest.main()
