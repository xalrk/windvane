"""T-043: a paid plan is never lost (a refused push keeps a local branch and says so; two plans in one minute both
land on their own branches), a plan branch whose PR was closed frees its ids, the desk's `plan` says when another
plan holds the lock, and the planner gets each ticket's follow-ups from its latest critique only."""
import contextlib
import fcntl
import io
import sys
import unittest
from argparse import Namespace
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from windvane import cli, critic, driver  # noqa: E402
from windvane.config import Project  # noqa: E402
from windvane.util import append_jsonl  # noqa: E402
from helpers import Sandbox  # noqa: E402

REFUSE_PLAN = "#!/bin/sh\nwhile read old new ref; do case \"$ref\" in refs/heads/plan-*) exit 1;; esac; done\nexit 0\n"


class PlanSafety(unittest.TestCase):
    def setUp(self):
        self.s = Sandbox()
        self.addCleanup(self.s.close)
        self.s.ticket("T-001")
        self.s.ticket("T-002")
        self.s.publish()
        self.p = Project(self.s.root)
        self.pushes = []
        orig = driver.push
        driver.push = lambda title, msg="", *a, **k: self.pushes.append((title, msg))
        self.addCleanup(setattr, driver, "push", orig)

    def forge(self, name, gh=None):
        if gh:
            (self.s.bin / "gh").write_text(gh)
            (self.s.bin / "gh").chmod(0o755)
        cfg = self.s.root / ".windvane/config.toml"
        cfg.write_text(cfg.read_text().replace('forge = "git"', f'forge = "{name}"'))
        self.s.publish("forge")
        self.p = Project(self.s.root)

    def refuse_plan_pushes(self):
        hook = self.s.remote / "hooks" / "pre-receive"
        hook.write_text(REFUSE_PLAN)
        hook.chmod(0o755)

    def log(self):
        return (self.p.state / "autopilot.log").read_text()

    def local_plan(self):
        """The plan branch kept in the repo, and the ticket files on it."""
        branch = self.s.git("branch", "--list", "plan-*", "--format=%(refname:short)")
        return branch, self.s.git("ls-tree", "-r", "--name-only", branch, ".windvane/tickets/").split()

    def test_refused_plan_push_keeps_a_local_branch_and_says_so(self):
        self.refuse_plan_pushes()
        self.assertEqual(driver.plan(self.p, "make it great"), 0)
        branch, files = self.local_plan()
        self.assertIn(".windvane/tickets/T-003.md", files)
        self.assertEqual(self.s.git("ls-remote", "--heads", "origin", "plan-*"), "")
        self.assertIn(f"kept only locally on {branch}: push it by hand", self.log())
        self.assertEqual(self.pushes[-1][0], "plan not pushed")
        self.assertIn(f"kept only locally on {branch}", self.pushes[-1][1])
        driver.plan(self.p, "more")  # the next plan resets the plan tree; the kept branch stays and holds its id
        self.assertIn(".windvane/tickets/T-003.md", self.s.git("ls-tree", "-r", "--name-only", branch).split())
        second = [b for b in self.s.git("branch", "--list", "plan-*", "--format=%(refname:short)").split()
                  if b != branch]
        self.assertEqual(self.s.git("ls-tree", "--name-only", second[0], ".windvane/tickets/").split()[-1],
                         ".windvane/tickets/T-004.md")

    def test_refused_push_on_github_opens_no_pr_and_never_claims_a_remote_branch(self):
        self.forge("github", "#!/bin/sh\necho \"$@\" >> \"$FAKE_LOG.gh\"\n")
        self.refuse_plan_pushes()
        self.assertEqual(driver.plan(self.p, "make it great"), 0)
        self.assertIn(".windvane/tickets/T-003.md", self.local_plan()[1])
        gh = (Path(str(self.s.tmp / "calls.jsonl") + ".gh"))
        self.assertNotIn("pr create", gh.read_text() if gh.exists() else "")

    def test_refused_push_with_merge_tells_the_phone_its_local(self):
        self.forge("github", "#!/bin/sh\nexit 0\n")
        self.refuse_plan_pushes()
        self.assertEqual(driver.plan(self.p, "make it great", merge=True), 0)
        branch, files = self.local_plan()
        self.assertIn(".windvane/tickets/T-003.md", files)
        self.assertEqual(self.pushes, [("plan not merged", f"1 tickets wait on the local branch {branch}: "
                                                           "push it by hand.")])

    def test_two_plans_in_the_same_minute_both_survive(self):
        orig = driver.now
        self.addCleanup(setattr, driver, "now", orig)
        for t in (datetime(2026, 10, 4, 9, 30, 5), datetime(2026, 10, 4, 9, 30, 5), datetime(2026, 10, 4, 9, 30, 41)):
            driver.now = lambda t=t: t
            self.assertEqual(driver.plan(self.p, "make it great"), 1)
        heads = self.s.git("ls-remote", "--heads", "origin", "plan-*")
        names = sorted(l.split("refs/heads/")[1] for l in heads.splitlines())
        self.assertEqual(names, ["plan-20261004-093005", "plan-20261004-093005-2", "plan-20261004-093041"])
        tickets = [self.s.git("ls-tree", "--name-only", f"origin/{b}", ".windvane/tickets/").split() for b in names]
        self.assertEqual([t[-1] for t in tickets], [".windvane/tickets/T-003.md", ".windvane/tickets/T-004.md",
                                                     ".windvane/tickets/T-005.md"])

    def open_plan_branch(self, name, tid):
        self.s.git("switch", "-q", "-c", name)
        self.s.ticket(tid)
        self.s.git("add", "-A")
        self.s.git("commit", "-qm", "plan")
        self.s.git("push", "-q", "origin", "HEAD")
        self.s.git("switch", "-q", "main")

    def test_closed_pr_plan_branch_no_longer_reserves_ids(self):
        self.open_plan_branch("plan-20260101-000000", "T-003")
        self.open_plan_branch("plan-20260102-000000", "T-004")
        self.forge("github", "#!/bin/sh\n[ \"$2\" = list ] && printf '%s\\n' 'plan-20260101-000000 CLOSED' "
                             "'plan-20260102-000000 OPEN' 'plan-20260102-000000 CLOSED'\nexit 0\n")
        self.s.git("fetch", "-q", "origin")
        self.assertEqual(driver.pending_ids(self.p) - {"T-001", "T-002"}, {"T-004"})  # the open one holds its id
        self.assertEqual(self.s.git("ls-remote", "--heads", "origin", "plan-20260101-000000").count("plan-"), 1)

    def test_without_gh_every_unmerged_plan_branch_reserves_ids(self):
        self.open_plan_branch("plan-20260101-000000", "T-003")
        self.forge("github")  # no `gh` on the PATH
        self.s.git("fetch", "-q", "origin")
        self.assertEqual(driver.pending_ids(self.p) - {"T-001", "T-002"}, {"T-003"})

    def cmd_plan(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.cmd_plan(Namespace(goal=["x"], no_followups=True, merge=False, max=6), self.p)
        return out.getvalue()

    def test_desk_plan_while_locked_exits_non_zero_with_the_message(self):
        self.p.state.mkdir(parents=True, exist_ok=True)
        with open(self.p.state / "plan.lock", "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            with self.assertRaises(SystemExit) as e:
                self.cmd_plan()
            self.assertEqual(driver.plan(self.p, "x", merge=True), 0)  # the driver's --replan path: as before
        self.assertIn("another plan is running", str(e.exception.code))
        self.assertNotEqual(e.exception.code, 0)

    def test_desk_plan_unlocked_runs(self):
        self.assertIn("1 tickets planned", self.cmd_plan())


class Followups(unittest.TestCase):
    def setUp(self):
        self.s = Sandbox()
        self.addCleanup(self.s.close)
        self.p = Project(self.s.root)
        self.p.state.mkdir(parents=True, exist_ok=True)

    def critique(self, tid, *points):
        text = "Score: 7/10\nBeyond this ticket:\n" + "".join(f"- {b}\n" for b in points) + f"CRITIC: 7/10 {tid}"
        append_jsonl(self.p.state / "critic.jsonl", {"ticket": tid, "score": 7, "critique": text})

    def test_three_critiques_of_one_ticket_give_only_the_last_ones_points(self):
        self.critique("T-001", "make it faster", "log the retries")
        self.critique("T-002", "a doc page")
        self.critique("T-001", "speed it up", "log each retry")
        append_jsonl(self.p.state / "critic.jsonl", {"ticket": "T-001", "step": "skip"})  # no critique: no verdict
        self.critique("T-001", "faster, please", "retries in the log", "a new point")
        self.assertEqual(critic.followups(self.p), [
            ("T-001", "faster, please"), ("T-001", "retries in the log"), ("T-001", "a new point"),
            ("T-002", "a doc page")])

    def test_a_plan_marker_still_starts_afresh(self):
        self.critique("T-001", "old")
        append_jsonl(self.p.state / "critic.jsonl", {"ticket": "PLAN", "step": "plan", "notes": 1})
        self.critique("T-002", "new")
        self.assertEqual(critic.followups(self.p), [("T-002", "new")])


if __name__ == "__main__":
    unittest.main()
