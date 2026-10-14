"""T-056: a plan a failed push kept locally is pushed by the next run (once a run, without a new phone push while it
still fails); each refusal's push names its own next step; a closed plan PR is looked up with `gh` only once."""
import json
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from windvane import held, net, planner  # noqa: E402
from windvane.config import Project  # noqa: E402
from helpers import Sandbox  # noqa: E402
from test_plan_pushes import AUTH  # noqa: E402
from test_plan_safety import REFUSE_PLAN  # noqa: E402

NETWORK = "fatal: unable to access 'https://github.com/o/r.git/': Could not resolve host: github.com\n"
HOOK = "To /tmp/r.git\n ! [remote rejected] HEAD -> plan-1 (pre-receive hook declined)\n"
OTHER = "To /tmp/r.git\nerror: src refspec x does not match any\n"

# Logs its calls; an old plan's PR was closed, `pr create` gives a URL.
GH = """#!/bin/sh
echo "$*" >> "{log}"
case "$*" in
  *"--head plan-20250101-000000 "*) echo '[{{"number": 5, "state": "CLOSED"}}]';;
  "pr create"*) echo https://github.com/o/r/pull/7;;
esac
exit 0
"""


class Base(unittest.TestCase):
    def setUp(self):
        self.s = Sandbox()
        self.addCleanup(self.s.close)
        self.s.ticket("T-001")
        self.s.publish()
        self.p = Project(self.s.root)
        self.pushes, self.git_pushes = [], 0
        for module in (held, planner):
            self.addCleanup(setattr, module, "push", module.push)
            module.push = lambda title, msg="", *a, **k: self.pushes.append((title, msg))
        self.refusal = None
        orig_git = net.git

        def fake(cwd, *args):
            if args[0] == "push":
                self.git_pushes += 1
                if self.refusal:
                    return subprocess.CompletedProcess(["git", *args], 128, "", self.refusal)
            return orig_git(cwd, *args)
        net.git = fake
        self.addCleanup(setattr, net, "git", orig_git)

    def log(self):
        return (self.p.state / "autopilot.log").read_text()

    def local(self, pattern="plan-*"):
        return self.s.git("branch", "--list", pattern, "--format=%(refname:short)")

    def remote(self, pattern="plan-*"):
        return self.s.git("ls-remote", "--heads", "origin", pattern)

    def github(self):
        (self.s.bin / "gh").write_text(GH.format(log=self.s.tmp / "gh.log"))
        (self.s.bin / "gh").chmod(0o755)
        cfg = self.s.root / ".windvane/config.toml"
        cfg.write_text(cfg.read_text().replace('forge = "git"', 'forge = "github"'))
        self.s.publish("forge")
        self.p = Project(self.s.root)

    def gh_calls(self):
        f = self.s.tmp / "gh.log"
        return f.read_text().splitlines() if f.exists() else []


class HeldPlans(Base):
    def test_a_plan_held_by_the_network_is_pushed_by_the_next_run(self):
        self.refusal = NETWORK
        self.assertEqual(planner.plan(self.p, "make it great"), 0)
        branch = self.local()
        self.assertTrue(branch)
        self.assertEqual(self.remote(), "")
        self.assertTrue(self.pushes[-1][1].startswith("Network down; the next run pushes it again.\n"))
        r = subprocess.run(["windvane", "-C", str(self.s.root), "run", "--for", "1h", "--parallel", "1",
                            "--no-preflight"], capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr[-500:])
        self.assertIn(f"refs/heads/{branch}", self.remote())
        self.assertIn(f"{branch}: held back, now on {branch}, pushed", self.log())
        self.assertEqual(self.local(), "")  # the remote has it: a closed, deleted branch isn't pushed back

    def test_a_plan_refused_by_a_hook_is_tried_once_a_run_and_told_once(self):
        hook = self.s.remote / "hooks" / "pre-receive"
        hook.write_text(REFUSE_PLAN)
        hook.chmod(0o755)
        planner.plan(self.p, "make it great")
        branch, told = self.local(), len(self.pushes)
        self.assertEqual(self.git_pushes, 2)  # the plan's own push tries twice
        for run in (1, 2):  # two runs' starts
            self.assertEqual(held.push_held(self.p), 0)
            self.assertEqual(self.git_pushes, 2 + run)
        self.assertEqual(len(self.pushes), told)  # no phone push for a refusal the user already heard
        self.assertEqual(self.local(), branch)
        self.assertEqual(self.log().count(f"{branch}: still not pushed (refused by a hook or branch rule; change the "
                                          f"rule, or push it by hand with `git push origin {branch}:{branch}-b` and "
                                          "merge it into main)"), 2)

    def test_a_held_merge_plan_is_landed(self):
        self.refusal = NETWORK
        planner.plan(self.p, "make it great", merge=True)
        branch = self.local()
        self.refusal = None
        self.assertEqual(held.push_held(self.p), 1)
        self.s.git("fetch", "-q", "origin")
        self.assertIn(".windvane/tickets/T-002.md", self.s.git("ls-tree", "-r", "--name-only", "origin/main"))
        self.assertEqual(self.pushes[-1][0], "1 held-back change pushed")
        self.assertRegex(self.pushes[-1][1], r"^Plan: 1 ticket \(.*\): on main, merged\.$")
        self.assertNotIn(branch, json.loads((self.p.state / "held.json").read_text()))

    def test_a_held_plan_on_github_gets_its_pr(self):
        self.github()
        self.refusal = NETWORK
        planner.plan(self.p, "make it great")
        self.refusal = None
        self.assertEqual(held.push_held(self.p), 1)
        self.assertTrue([c for c in self.gh_calls() if c.startswith("pr create --head plan-")])
        self.assertIn(", pushed", self.log())
        self.assertIn("on https://github.com/o/r/pull/7, pushed.", self.pushes[-1][1])

    def test_a_pr_that_wont_open_says_so(self):
        self.github()
        (self.s.bin / "gh").write_text("#!/bin/sh\nexit 1\n")
        self.refusal = NETWORK
        planner.plan(self.p, "make it great")
        self.refusal = None
        self.assertEqual(held.push_held(self.p), 1)
        self.assertRegex(self.pushes[-1][1], r"on plan-\S+, pushed; no PR opened, open one by hand\.$")

    def test_only_the_drivers_own_unpushed_branches(self):
        self.refusal = NETWORK
        planner.plan(self.p, "make it great")
        self.refusal = None
        kept = self.local()
        self.s.git("push", "-q", "origin", f"{kept}:refs/heads/by-hand")  # pushed by hand under another name
        self.s.git("switch", "-q", "-c", "plan-mine")  # the user's own branch
        self.s.write("x", "x\n")
        self.s.git("add", "-A")
        self.s.git("commit", "-qm", "my plan")
        self.s.git("switch", "-q", "main")
        # a copy of a driver plan the user checked out, its PR closed and its remote branch deleted
        self.s.git("switch", "-q", "-c", "plan-20261001-000000")
        self.s.ticket("T-009")
        self.s.git("add", "-A")
        self.s.git("commit", "-qm", "Plan: 1 ticket (rejected)")
        self.s.git("switch", "-q", "main")
        self.assertEqual(held.push_held(self.p), 0)
        self.assertEqual(self.remote(), "")
        self.assertEqual(json.loads((self.p.state / "held.json").read_text()), {})  # its note goes too

    def test_one_pushed_by_hand_whose_pr_was_closed_stays_closed(self):
        self.github()
        (self.s.bin / "gh").write_text("#!/bin/sh\ncase \"$*\" in *\"pr list --head plan-\"*) echo '[{\"number\": 12, \"state\": \"CLOSED\"}]';; esac\n")
        self.refusal = NETWORK
        planner.plan(self.p, "make it great")
        self.refusal = None  # the user pushed it, closed its PR #12 and deleted the branch
        self.assertEqual(held.push_held(self.p), 0)
        self.assertEqual(self.remote(), "")


class Refusals(Base):
    def test_each_refusal_names_its_own_next_step(self):
        for stderr, step in ((AUTH, "check your login to the remote and the next run pushes it"),
                             (HOOK, "-b` and merge it into main"),
                             (NETWORK, "the next run pushes it again"), (OTHER, "push failed; see the windvane log")):
            with self.subTest(step=step):
                self.refusal = stderr
                self.assertEqual(planner.plan(self.p, "make it great"), 0)
                self.assertIn(f"{step[1:]}.\n1 ticket kept only locally on ", self.pushes[-1][1])  # capitalized
                self.assertEqual("by hand" in self.pushes[-1][1], stderr == HOOK)  # only a hook needs a hand push
        self.assertIn("src refspec x does not match any", self.log())  # git's own words stay in the log

    def test_on_github_the_login_step_is_gh(self):
        self.github()
        self.refusal = AUTH
        planner.plan(self.p, "make it great")
        self.assertTrue(self.pushes[-1][1].startswith("Login refused; run `gh auth login` and the next run pushes "
                                                      "it.\n"))


class ClosedPlans(Base):
    def test_a_closed_plan_branch_is_looked_up_once_across_two_plans(self):
        self.github()
        self.s.git("switch", "-q", "-c", "plan-20250101-000000")
        self.s.ticket("T-009")
        self.s.git("add", "-A")
        self.s.git("commit", "-qm", "Plan: 1 ticket (old)")
        self.s.git("push", "-q", "origin", "HEAD")
        self.s.git("switch", "-q", "main")
        self.assertEqual(planner.plan(self.p, "make it great"), 1)
        self.assertEqual(planner.plan(self.p, "make it greater"), 1)
        old = [c for c in self.gh_calls() if "--head plan-20250101-000000 " in c]
        self.assertEqual(len(old), 1)
        self.assertEqual(json.loads((self.p.state / "closed_plans.json").read_text()), ["plan-20250101-000000"])
        self.assertEqual(self.local(), "plan-20250101-000000")  # paid work: never deleted


if __name__ == "__main__":
    unittest.main()
