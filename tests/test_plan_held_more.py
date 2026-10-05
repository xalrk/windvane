"""T-065: a reopened plan PR claims its ids again; `status` and `overview` show held-back branches (the phone's
status counts them); `push_held`'s push puts what still needs the user first, under a title that says so."""
import json
import re
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from windvane import driver  # noqa: E402
from test_plan_retry import NETWORK, Base  # noqa: E402
from test_plan_safety import REFUSE_PLAN  # noqa: E402

OLD = "plan-20250101-000000"
# Logs its calls; the old plan's PR is open again, `pr create` gives a URL.
REOPENED = """#!/bin/sh
echo "$*" >> "{log}"
case "$*" in
  "pr list --state open"*) echo {old};;
  "pr create"*) echo https://github.com/o/r/pull/7;;
esac
exit 0
"""
HOOK_WHY = "refused by a hook or branch rule; push it by hand with `git push origin {b}:{b}-b`, or change the rule"


class ReopenedPlans(Base):
    def setUp(self):
        super().setUp()
        self.github()
        (self.s.bin / "gh").write_text(REOPENED.format(log=self.s.tmp / "gh.log", old=OLD))
        self.s.git("switch", "-q", "-c", OLD)
        self.s.ticket("T-009")
        self.s.git("add", "-A")
        self.s.git("commit", "-qm", "Plan: 1 ticket (old)")
        self.s.git("push", "-q", "origin", "HEAD")
        self.s.git("switch", "-q", "main")
        self.closed = self.p.state / "closed_plans.json"

    def test_a_reopened_plan_claims_its_ids_again(self):
        self.p.state.mkdir(parents=True, exist_ok=True)
        self.closed.write_text(json.dumps([OLD]))  # found closed by an earlier plan, then reopened
        self.assertEqual(driver.plan(self.p, "make it great"), 1)
        self.assertEqual(json.loads(self.closed.read_text()), [])
        self.s.git("fetch", "-q", "origin")
        new = [b for b in self.s.git("branch", "-r", "--format=%(refname:short)").split()
               if b.startswith("origin/plan-") and b != f"origin/{OLD}"]
        names = self.s.git("ls-tree", "--name-only", new[0], ".windvane/tickets/").split()
        self.assertIn(".windvane/tickets/T-010.md", names)  # not T-002..T-009: the old plan keeps T-009
        calls = self.gh_calls()
        self.assertEqual(len([c for c in calls if c.startswith("pr list --state open")]), 1)  # one query, once
        self.assertFalse([c for c in calls if f"--head {OLD} " in c])  # an open one isn't asked about again

    def test_a_failed_open_list_frees_nothing(self):
        self.p.state.mkdir(parents=True, exist_ok=True)
        self.closed.write_text(json.dumps([OLD]))
        (self.s.bin / "gh").write_text("#!/bin/sh\ncase \"$*\" in \"pr list --state open\"*) exit 1;; esac\nexit 0\n")
        self.assertIn("T-009", driver.pending_ids(self.p))  # maybe reopened: a skipped id beats a collision
        self.assertEqual(json.loads(self.closed.read_text()), [OLD])  # asked again next time

    def test_no_remembered_branch_no_extra_query(self):
        driver.pending_ids(self.p)
        self.assertFalse([c for c in self.gh_calls() if c.startswith("pr list --state open")])


class HeldInStatus(Base):
    def windvane(self, *args):
        r = subprocess.run(["windvane", *args], cwd=self.s.root, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr[-500:])
        return r.stdout

    def test_status_and_overview_show_a_held_branch_and_why(self):
        hook = self.s.remote / "hooks" / "pre-receive"
        hook.write_text(REFUSE_PLAN)
        hook.chmod(0o755)
        driver.plan(self.p, "make it great")
        branch = self.local()
        for cmd in (["status"], ["overview"]):
            with self.subTest(cmd=cmd):
                out = self.windvane(*cmd)
                self.assertRegex(out, rf"(?i)held back for under an hour, not pushed: {branch} \(Plan: 1 ticket.*\): "
                                      rf"{re.escape(HOOK_WHY.format(b=branch))}\n"
                                      r"  \(each run's start tries these again\)")
        self.assertIn("1 held-back change not pushed, for under an hour", self.windvane("status", "--line"))

    def test_nothing_when_none_is_held(self):
        driver.plan(self.p, "make it great")  # pushed fine
        for cmd in (["status"], ["overview"], ["status", "--line"]):
            with self.subTest(cmd=cmd):
                self.assertNotIn("held", self.windvane(*cmd))

    def test_the_newest_refusal_wins(self):
        self.refusal = NETWORK
        driver.plan(self.p, "make it great")
        self.assertIn("network down", driver.held_lines(self.p)[0])
        hook = self.s.remote / "hooks" / "pre-receive"
        hook.write_text(REFUSE_PLAN)
        hook.chmod(0o755)
        self.refusal = None
        self.assertEqual(driver.push_held(self.p), 0)
        self.assertEqual(len(driver.held_lines(self.p)), 1)
        self.assertIn(HOOK_WHY.format(b=self.local()), driver.held_lines(self.p)[0])


class HeldPush(Base):
    def test_not_merged_comes_first_under_its_own_title(self):
        self.github()
        # the first plan's merge goes through, the second's is refused
        (self.s.bin / "gh").write_text("#!/bin/sh\ncase \"$*\" in\n  \"pr create\"*) echo https://github.com/o/r/pull/"
                                       "$(cat \"$0.n\" 2>/dev/null || echo 7);;\n  \"pr merge 7\"*) exit 0;;\n"
                                       "  \"pr merge\"*|\"pr view\"*) exit 1;;\nesac\nexit 0\n")
        self.refusal = NETWORK
        driver.plan(self.p, "first", merge=True)
        driver.plan(self.p, "second", merge=True)
        first = json.loads((self.p.state / "held.json").read_text())
        self.assertEqual(len(first), 2)
        first = next(iter(first))
        self.refusal = None
        n = self.s.bin / "gh.n"
        orig = driver.land

        def land(p, cd, branch, *a, **k):  # the first PR is #7, the second #8
            n.write_text("7" if branch == first else "8")
            return orig(p, cd, branch, *a, **k)
        driver.land = land
        self.addCleanup(setattr, driver, "land", orig)
        self.assertEqual(driver.push_held(self.p), 2)
        title, body = self.pushes[-1]
        self.assertEqual(title, "1 held-back change pushed, not merged; 1 more pushed")
        lines = body.splitlines()
        self.assertRegex(lines[0], r"^Plan: 1 ticket.*: on https://github.com/o/r/pull/8, not merged; merge it by hand\.$")
        self.assertEqual(lines[1:3], ["", "Pushed:"])
        self.assertRegex(lines[3], r"^Plan: 1 ticket.*: on https://github.com/o/r/pull/7, merged\.$")

    def test_all_landed_keeps_todays_title(self):
        self.assertEqual(driver.held_push([("a: on main, merged.", False), ("b: on main, merged.", False)]),
                         ("2 held-back changes pushed", "a: on main, merged.\nb: on main, merged."))
        self.assertEqual(driver.held_push([("a: on plan-1, pushed; no PR opened, open one by hand.", True)]),
                         ("1 held-back change pushed, not merged", "a: on plan-1, pushed; no PR opened, open one by "
                                                                    "hand."))


if __name__ == "__main__":
    unittest.main()
