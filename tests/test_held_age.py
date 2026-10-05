"""T-072: a hook's refusal names the `git push` to run by hand; `held_branches` drops a dropped branch's reason in the
same step; `status --line` says how long the oldest held-back change has waited (an entry from before T-072, with no
time noted, is aged by its branch's last commit)."""
import json
import os
import re
import shlex
import subprocess
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from windvane import driver  # noqa: E402
from test_plan_retry import Base  # noqa: E402

HOOK = "To /tmp/r.git\n ! [remote rejected] HEAD -> plan-x (pre-receive hook declined)\n"
DAY = 86400


class HeldAge(Base):
    def windvane(self, *args):
        r = subprocess.run(["windvane", *args], cwd=self.s.root, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr[-500:])
        return r.stdout

    def hold_plans(self, n):
        """`n` plans a hook refused, held on local branches: their names, oldest first."""
        self.refusal = HOOK
        for _ in range(n):
            driver.plan(self.p, "make it great")
        self.refusal = None
        return self.local().split()

    def notes(self):
        return json.loads((self.p.state / "held_why.json").read_text())

    def test_a_hook_refusal_names_the_push_with_branch_and_remote(self):
        [branch] = self.hold_plans(1)
        why = self.notes()[branch]["why"]
        self.assertIn(f"change the rule, or push it by hand with `git push origin {branch}:{branch}-b` and merge it "
                      "into main", why)
        self.assertIn(why, driver.held_lines(self.p)[0])
        self.assertIn("`git push up x:x-b` and open a PR from it", driver.push_refusal(HOOK, "github", "x", "up"))
        self.assertIn("`git push up x:x-b` and merge it into trunk", driver.push_refusal(HOOK, "git", "x", "up", "trunk"))

    def test_the_hints_push_gets_past_a_rule_on_the_branchs_name(self):
        hook = self.s.remote / "hooks" / "pre-receive"  # refuses plan-* except a hand push's -b name
        hook.write_text("#!/bin/sh\nwhile read old new ref; do case \"$ref\" in refs/heads/plan-*-b) ;; "
                        "refs/heads/plan-*) exit 1;; esac; done\nexit 0\n")
        hook.chmod(0o755)
        driver.plan(self.p, "make it great")
        branch = self.local()
        self.assertIn("1 held-back change not pushed", self.windvane("status", "--line"))
        cmd = re.search(r"`(git push [^`]+)`", self.notes()[branch]["why"]).group(1)
        subprocess.run(shlex.split(cmd), cwd=self.s.root, check=True, capture_output=True)
        self.assertIn(f"refs/heads/{branch}-b", self.remote())
        self.assertNotIn("held", self.windvane("status", "--line"))  # pushed: the user lands it, as the hint said
        self.assertEqual(driver.held_branches(self.p), {})
        self.assertEqual(self.notes(), {})

    def test_held_branches_drops_a_merged_branchs_reason(self):
        merged, waiting = self.hold_plans(2)
        self.s.git("merge", "-q", merged)
        self.s.git("push", "-q", "origin", "main")
        self.assertEqual(list(driver.held_branches(self.p)), [waiting])
        self.assertEqual(list(self.notes()), [waiting])
        self.assertEqual(list(json.loads((self.p.state / "held.json").read_text())), [waiting])

    def test_a_pushed_branchs_reason_goes_with_it(self):
        self.hold_plans(1)
        self.assertEqual(driver.push_held(self.p), 1)
        self.assertEqual(self.notes(), {})

    def test_status_line_names_the_oldest_ones_age(self):
        old, new = self.hold_plans(2)
        notes = self.notes()
        notes[old]["at"] = int(time.time()) - 3 * DAY - 60
        (self.p.state / "held_why.json").write_text(json.dumps(notes))
        self.assertTrue(self.windvane("status", "--line").endswith(". 2 held-back changes not pushed, oldest 3 days\n"))
        status = self.windvane("status")
        self.assertIn(f"held back for 3 days, not pushed: {old} ", status)
        self.assertIn(f"held back for under an hour, not pushed: {new} ", status)
        self.assertIn(f"Held back for 3 days, not pushed: {old} ", self.windvane("overview"))

    def test_a_retry_keeps_when_it_was_first_held(self):
        [branch] = self.hold_plans(1)
        notes = self.notes()
        notes[branch]["at"] = 1000
        (self.p.state / "held_why.json").write_text(json.dumps(notes))
        self.refusal = HOOK
        self.assertEqual(driver.push_held(self.p), 0)
        self.assertEqual(self.notes()[branch]["at"], 1000)

    def test_an_old_entry_without_a_time_is_aged_by_its_commit(self):
        self.s.git("switch", "-q", "-c", "plan-old")
        self.s.write(".windvane/tickets/T-009.md", "# T-009: old\n")
        self.s.git("add", "-A")
        env = {**os.environ, "GIT_COMMITTER_DATE": f"{int(time.time()) - 5 * DAY - 60} +0000"}
        subprocess.run(["git", "commit", "-qm", "Plan: 1 ticket (old)"], cwd=self.s.root, env=env, check=True)
        self.s.git("switch", "-q", "main")
        self.p.state.mkdir(parents=True, exist_ok=True)
        (self.p.state / "held.json").write_text(json.dumps({"plan-old": "push"}))
        (self.p.state / "held_why.json").write_text(json.dumps({"plan-old": "network down; the next run pushes it again"}))
        self.assertEqual(driver.held_lines(self.p), ["held back for 5 days, not pushed: plan-old (Plan: 1 ticket (old)): "
                                                     "network down; the next run pushes it again"])
        self.assertIn("1 held-back change not pushed, for 5 days", self.windvane("status", "--line"))

    def test_age_words(self):
        for seconds, words in ((0, "under an hour"), (3600, "1 hour"), (23 * 3600, "23 hours"), (DAY, "1 day"),
                               (10 * DAY, "10 days")):
            self.assertEqual(driver.age(seconds), words)


if __name__ == "__main__":
    unittest.main()
