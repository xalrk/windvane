"""T-051: a refused plan push tells the phone why (the login, a hook, the network), so the user knows whether pushing
by hand will work; a closed plan PR frees its ids however many PRs the repo has; one ticket reads "1 ticket"."""
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from windvane import driver  # noqa: E402
from windvane.config import Project  # noqa: E402
from helpers import Sandbox  # noqa: E402
from test_plan_safety import REFUSE_PLAN  # noqa: E402

AUTH = ("remote: Invalid username or token. Password authentication is not supported for Git operations.\n"
        "fatal: Authentication failed for 'https://github.com/o/r.git/'\n")


class PlanPushes(unittest.TestCase):
    def setUp(self):
        self.s = Sandbox()
        self.addCleanup(self.s.close)
        self.s.ticket("T-001")
        self.s.publish()
        self.p = Project(self.s.root)
        self.pushes = []
        orig = driver.push
        driver.push = lambda title, msg="", *a, **k: self.pushes.append((title, msg))
        self.addCleanup(setattr, driver, "push", orig)

    def log(self):
        return (self.p.state / "autopilot.log").read_text()

    def refuse_with(self, stderr):
        """Every `git push` fails with `stderr`, as a remote that refuses the login would."""
        orig = driver.net.git

        def fake(cwd, *args):
            if args[0] == "push":
                return subprocess.CompletedProcess(["git", *args], 128, "", stderr)
            return orig(cwd, *args)
        driver.net.git = fake
        self.addCleanup(setattr, driver.net, "git", orig)

    def test_an_auth_refusal_names_the_login(self):
        self.refuse_with(AUTH)
        self.assertEqual(driver.plan(self.p, "make it great"), 0)
        title, msg = self.pushes[-1]
        self.assertEqual(title, "plan not pushed")
        self.assertRegex(msg, r"^1 ticket kept only locally on plan-\S+ \(refused: the login or its permission"
                              r"; log in again first\): push it by hand\.$")
        self.assertIn("plan: 1 ticket kept only locally on plan-", self.log())

    def test_a_hook_refusal_names_the_hook(self):
        hook = self.s.remote / "hooks" / "pre-receive"
        hook.write_text(REFUSE_PLAN)
        hook.chmod(0o755)
        self.assertEqual(driver.plan(self.p, "make it great"), 0)
        self.assertEqual(self.pushes[-1][0], "plan not pushed")
        self.assertIn("(refused by a hook or branch protection on the remote): push it by hand.", self.pushes[-1][1])

    def test_a_refused_merge_says_why_too(self):
        self.refuse_with("ssh: connect to host github.com port 22: Connection timed out\n"
                         "fatal: Could not read from remote repository.\n")
        self.assertEqual(driver.plan(self.p, "make it great", merge=True), 0)
        self.assertEqual(self.pushes[-1][0], "plan not merged")
        self.assertRegex(self.pushes[-1][1], r"^1 ticket waits on the local branch plan-\S+ \(the network failed; "
                                             r"try again later\): push it by hand\.$")

    def test_one_ticket_is_singular_in_the_push_and_the_log(self):
        self.assertEqual(driver.plan(self.p, "make it great"), 1)
        self.assertEqual(self.pushes[-1][0], "planned 1 ticket")
        self.assertIn("plan: 1 ticket pushed to plan-", self.log())
        self.assertIn("Plan: 1 ticket (", self.s.git("log", "-1", "--format=%s", "origin/" + self.s.git(
            "ls-remote", "--heads", "origin", "plan-*").split("refs/heads/")[1]))
        self.assertNotIn("1 tickets", self.log())


class Refusals(unittest.TestCase):
    """What git and github print, word for word, and the reason each gets."""
    CASES = {
        AUTH: "login",
        "git@github.com: Permission denied (publickey).\nfatal: Could not read from remote repository.\n": "login",
        "remote: Permission to o/r.git denied to someone.\nfatal: unable to access 'https://github.com/o/r.git/': "
        "The requested URL returned error: 403\n": "login",
        "fatal: could not read Username for 'https://github.com': terminal prompts disabled\n": "login",
        "remote: error: GH006: Protected branch update failed for refs/heads/plan-1.\nTo github.com:o/r.git\n"
        " ! [remote rejected] HEAD -> plan-1 (protected branch hook declined)\n": "hook",
        "To /tmp/r.git\n ! [remote rejected] HEAD -> plan-1 (pre-receive hook declined)\n"
        "error: failed to push some refs to '/tmp/r.git'\n": "hook",
        "fatal: unable to access 'https://github.com/o/r.git/': Could not resolve host: github.com\n": "network",
        "timed out after 120s": "network",
    }

    def test_each_stderr_gets_its_reason(self):
        for stderr, word in self.CASES.items():
            with self.subTest(stderr=stderr[:40]):
                self.assertIn(word, driver.push_refusal(stderr))

    def test_anything_else_gives_its_first_line(self):
        self.assertEqual(driver.push_refusal("To /tmp/r.git\nerror: src refspec x does not match any\n"
                                             "error: failed to push some refs to '/tmp/r.git'\n"),
                         "push failed: error: src refspec x does not match any")
        self.assertEqual(driver.push_refusal(""), "push failed")


# A repo with 301 PRs: the old closed plan PR is past any `--limit 300` list, but `--head` finds it.
BUSY_GH = """#!/bin/sh
case "$*" in
  *"--head plan-20250101-000000 "*) echo CLOSED;;
  *"--head "*) ;;
  *"--limit"*) i=0; while [ $i -lt 300 ]; do echo "feature-$i MERGED"; i=$((i+1)); done;;
esac
exit 0
"""


class BusyRepo(unittest.TestCase):
    def setUp(self):
        self.s = Sandbox()
        self.addCleanup(self.s.close)
        self.s.ticket("T-001")
        self.s.publish()
        (self.s.bin / "gh").write_text(BUSY_GH)
        (self.s.bin / "gh").chmod(0o755)
        cfg = self.s.root / ".windvane/config.toml"
        cfg.write_text(cfg.read_text().replace('forge = "git"', 'forge = "github"'))
        self.s.publish("forge")
        self.p = Project(self.s.root)

    def plan_branch(self, name, tid):
        self.s.git("switch", "-q", "-c", name)
        self.s.ticket(tid)
        self.s.git("add", "-A")
        self.s.git("commit", "-qm", "plan")
        self.s.git("push", "-q", "origin", "HEAD")
        self.s.git("switch", "-q", "main")

    def test_an_old_closed_plan_pr_frees_its_ids_past_300_prs(self):
        self.plan_branch("plan-20250101-000000", "T-002")  # closed long ago
        self.plan_branch("plan-20261001-000000", "T-003")  # no PR found: still holds its id
        self.s.git("fetch", "-q", "origin")
        self.assertEqual(driver.pending_ids(self.p) - {"T-001"}, {"T-003"})


if __name__ == "__main__":
    unittest.main()
