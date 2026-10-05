"""T-080: a push GitHub's push protection refused says to take the secret out (no hand push gets past it); a held
change to land, pushed by hand, stays listed as not merged until main has it; `status --line` drops its `Last:` part
while a held count shows, and a stale one about a branch no longer held."""
import json
import shlex
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from windvane import driver  # noqa: E402
from test_held_age import HOOK  # noqa: E402
from test_plan_retry import Base  # noqa: E402

# The shape of GitHub's push protection refusal, from its docs ("Working with push protection from the command line").
SECRET = """remote: error: GH013: Repository rule violations found for refs/heads/plan-x.
remote:
remote: - GITHUB PUSH PROTECTION
remote:   —————————————————————————————————————————
remote:     Resolve the following violations before pushing again
remote:
remote:     - Push cannot contain secrets
remote:
remote:      (?) Learn how to resolve a blocked push
remote:      https://docs.github.com/code-security/secret-scanning/working-with-secret-scanning-and-push-protection/working-with-push-protection-from-the-command-line#resolving-a-blocked-push
remote:
remote:       —— GitHub Personal Access Token ——————————————————————
remote:        locations:
remote:          - commit: 8728dbe67
remote:            path: README.md:4
remote:
remote:        (?) To push, remove secret from commit(s) or follow this URL to allow the secret.
remote:        https://github.com/octo-org/octo-repo/security/secret-scanning/unblock-secret/2Y0Ylt0cdWmPddTuWFFh0KK7PBZ
remote:
To https://github.com/octo-org/octo-repo
 ! [remote rejected] plan-x -> plan-x (push declined due to repository rule violations)
error: failed to push some refs to 'https://github.com/octo-org/octo-repo'
"""
# A plain repository rule (a required signed commit): pushing it under another name gets past a rule on the name.
RULE = """remote: error: GH013: Repository rule violations found for refs/heads/plan-x.
remote: Review all repository rules at https://github.com/octo-org/octo-repo/rules?ref=refs%2Fheads%2Fplan-x
remote:
remote: - Commits must have verified signatures.
remote:   Found 1 violation:
remote:
remote:   8728dbe67
remote:
To https://github.com/octo-org/octo-repo
 ! [remote rejected] plan-x -> plan-x (push declined due to repository rule violations)
"""
GH = """#!/bin/sh
case "$*" in
  *"--head {head} "*) echo {state};;
esac
exit 0
"""


class HeldSecret(Base):
    def windvane(self, *args):
        r = subprocess.run(["windvane", *args], cwd=self.s.root, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr[-500:])
        return r.stdout

    def test_push_protection_says_remove_the_secret(self):
        why = driver.push_refusal(SECRET, "github", "plan-x", "origin")
        self.assertEqual(why, "refused: it holds a secret (GitHub push protection); remove it from the branch's commit "
                              "(`git commit --amend` on `plan-x`), then the next run pushes it")
        self.assertNotIn("-b", why)
        self.assertIn("remove it", driver.push_refusal("remote: error: Secret scanning found a token", "git", "x"))

    def test_a_plain_rule_violation_keeps_the_hand_push(self):
        why = driver.push_refusal(RULE, "github", "plan-x", "origin")
        self.assertIn("refused by a hook or branch rule; change the rule, or push it by hand with "
                      "`git push origin plan-x:plan-x-b` and open a PR from it", why)
        self.assertNotIn("secret", why)

    def hand_pushed_land(self):
        """A plan to merge a hook refused, then pushed by hand with the hint's command: its branch."""
        self.refusal = HOOK
        driver.plan(self.p, "make it great", merge=True)
        branch = self.local()
        self.assertEqual(json.loads((self.p.state / "held.json").read_text()), {branch: "land"})
        why = json.loads((self.p.state / "held_why.json").read_text())[branch]["why"]
        cmd = shlex.split(next(part for part in why.split("`") if part.startswith("git push")))
        subprocess.run(cmd, cwd=self.s.root, check=True, capture_output=True)
        return branch

    def test_a_hand_pushed_land_change_is_listed_until_it_merges(self):
        branch = self.hand_pushed_land()
        status = self.windvane("status")
        self.assertIn(f"held back for under an hour, pushed to origin/{branch}-b, not merged: {branch} (Plan: ", status)
        self.assertIn(": merge it into main", status)
        self.assertIn(f"Held back for under an hour, pushed to origin/{branch}-b, not merged", self.windvane("overview"))
        self.assertTrue(self.windvane("status", "--line").endswith(
            "autopilot off. 1 held-back change pushed, not merged, for under an hour\n"))
        pushes = self.git_pushes
        self.assertEqual(driver.push_held(self.p), 0)  # the user has it: not pushed again, still noted
        self.assertEqual(self.git_pushes, pushes)
        self.assertEqual(list(json.loads((self.p.state / "held.json").read_text())), [branch])
        self.assertIn("pushed, not merged", self.windvane("status", "--line"))

        self.s.git("merge", "-q", f"origin/{branch}-b")
        self.s.git("push", "-q", "origin", "main")
        self.assertNotIn("held", self.windvane("status"))
        self.assertIn("on the local branch", self.log().splitlines()[-1])  # the plan's own line, stale once merged
        self.assertEqual(self.windvane("status", "--line"), "Sandbox: autopilot off\n")
        self.assertEqual(driver.held_branches(self.p), {})
        self.assertEqual(json.loads((self.p.state / "held.json").read_text()), {})
        self.assertEqual(json.loads((self.p.state / "held_why.json").read_text()), {})

    def test_a_hand_pushed_push_change_is_still_dropped(self):
        self.refusal = HOOK
        driver.plan(self.p, "make it great")
        branch = self.local()
        self.s.git("push", "-q", "origin", f"{branch}:{branch}-b")
        self.assertEqual(driver.held_now(self.p), [])

    def test_on_github_a_closed_pr_drops_it_and_an_open_one_keeps_it(self):
        self.github()
        branch = self.hand_pushed_land()
        for state, kept in (("OPEN", [branch]), ("CLOSED", [])):
            (self.s.bin / "gh").write_text(GH.format(head=f"{branch}-b", state=state))
            self.assertEqual(driver.held_branches(self.p), {})  # never pushed again either way
            self.assertEqual(list(json.loads((self.p.state / "held.json").read_text())), kept)

    def test_status_line_leaves_out_last_while_held_and_once_it_is_gone(self):
        self.refusal = HOOK
        driver.plan(self.p, "make it great")
        branch = self.local()
        line = self.windvane("status", "--line")
        self.assertIn("1 held-back change not pushed", line)
        self.assertNotIn("Last:", line)
        self.s.git("push", "-q", "origin", f"{branch}:{branch}-b")  # no longer held: "kept only locally" is stale
        self.assertIn("kept only locally", self.log().splitlines()[-1])
        self.assertEqual(self.windvane("status", "--line"), "Sandbox: autopilot off\n")
        driver.log(self.p, "T-001: worker run")
        self.assertRegex(self.windvane("status", "--line"), r"\. Last: \S+ T-001: worker run$")


if __name__ == "__main__":
    unittest.main()
