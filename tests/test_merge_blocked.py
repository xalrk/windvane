"""An approved ticket whose merge GitHub refuses as BLOCKED: held only by required checks still running, it is merged
again on later passes with no session, and fails at the run's end with its approval kept if they never end; a failed
check or a missing review fails it at once, saying what to look at. A draft hold the user lets go between runs merges
the kept approval with no session, and a `gh pr ready` refused with a 403 says what the gh login lacks."""
import json
import re
import sys
import time
import unittest
from argparse import Namespace
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from windvane import driver, held  # noqa: E402
from windvane.config import Project  # noqa: E402
from windvane.util import now  # noqa: E402
from helpers import Sandbox  # noqa: E402

# A fake `gh` over the sandbox's bare remote: every branch on it has PR #7 open. The state dir's files set the PR up:
# `checks` (its statusCheckRollup, JSON) and `review` (its reviewDecision) hold the merge as BLOCKED while a check
# isn't a success or the review is required; `clear` (a count of refused merges) makes the checks pass after that
# many; `stop` (a count too) writes the run's STOP file after that many; `draft`, `timeline` and `stuck` (the error
# `pr ready` fails with) as in test_merge_draft. `pr merge` merges --match-head-commit into main.
GH = """#!{python}
import json, subprocess, sys
from pathlib import Path
st, args = Path("{state}"), sys.argv[1:]
with open(st / "log", "a") as f:
    f.write(" ".join(args) + "\\n")
def git(*a):
    return subprocess.run(["git", "--git-dir", "{remote}", *a], capture_output=True, text=True)
def read(name, default=""):
    return (st / name).read_text() if (st / name).exists() else default
draft, checks, review = (st / "draft").exists(), json.loads(read("checks", "[]")), read("review")
ok = ("SUCCESS", "")
blocked = review in ("REVIEW_REQUIRED", "CHANGES_REQUESTED") or any(
    (c.get("conclusion") or c.get("state") or "") not in ok or c.get("status", "COMPLETED") != "COMPLETED"
    for c in checks)
if args[:2] == ["pr", "list"]:
    head = args[args.index("--head") + 1]
    there = git("rev-parse", "--verify", "-q", "refs/heads/" + head).returncode == 0
    print(json.dumps([{{"number": 7, "state": "OPEN", "headRefName": head}}] if there else []))
elif args[:2] == ["pr", "view"] and args[args.index("--json") + 1] == "isDraft":
    print("true" if draft else "false")
elif args[:2] == ["pr", "view"]:
    print(json.dumps({{"state": "OPEN", "mergeable": "MERGEABLE", "mergeStateStatus": "BLOCKED" if blocked else "CLEAN",
                      "isDraft": draft, "statusCheckRollup": checks, "reviewDecision": review}}))
elif args[:2] == ["pr", "ready"]:
    if (st / "stuck").exists():
        sys.exit(read("stuck"))
    (st / "draft").unlink()
elif args[0] == "api" and "timeline" in args[2]:
    print(read("timeline", "committed\\nreviewed\\n"))
elif args[:2] == ["pr", "merge"]:
    if draft:
        sys.exit("GraphQL: Pull Request is still a draft (mergePullRequest)")
    if blocked:
        for name, then in (("clear", lambda: (st / "checks").write_text(json.dumps(
                [dict(c, status="COMPLETED", conclusion="SUCCESS") for c in checks]))),
                           ("stop", lambda: (Path("{run_state}") / "STOP").write_text(""))):
            if (st / name).exists():
                n = int(read(name)) - 1
                (st / name).write_text(str(n))
                if n == 0:
                    then()
        sys.exit("GraphQL: Required status checks are expected. (mergePullRequest)")
    sha = args[args.index("--match-head-commit") + 1]
    tree = git("merge-tree", "--write-tree", "refs/heads/main", sha).stdout.split()[0]
    merge = git("commit-tree", tree, "-p", "refs/heads/main", "-p", sha, "-m", args[args.index("--subject") + 1])
    git("update-ref", "refs/heads/main", merge.stdout.strip())
"""

RUNNING = json.dumps([{"__typename": "CheckRun", "name": "build", "status": "IN_PROGRESS", "conclusion": ""}])
FAILED = json.dumps([{"__typename": "CheckRun", "name": "build", "status": "COMPLETED", "conclusion": "FAILURE"}])


def args(**kw):
    a = dict(cap=75, week_cap=95, ticket_hours=1, min_slot=0, answer_wait=10, replan=0, goal="", parallel=1)
    a.update(kw)
    return Namespace(**a)


class MergeBlocked(unittest.TestCase):
    def setUp(self):
        self.s = Sandbox()
        self.addCleanup(self.s.close)
        self.s.ticket("T-001")
        cfg = self.s.root / ".windvane/config.toml"
        cfg.write_text(cfg.read_text().replace('forge = "git"', 'forge = "github"'))
        self.s.publish()
        self.p = Project(self.s.root)
        self.state = self.s.tmp / "gh"
        self.state.mkdir()
        gh = self.s.bin / "gh"
        gh.write_text(GH.format(python=sys.executable, state=self.state, remote=self.s.remote, run_state=self.p.state))
        gh.chmod(0o755)
        self.pushes = []
        orig, driver.push = driver.push, lambda title, msg, *a, **k: self.pushes.append((title, msg))
        self.addCleanup(setattr, driver, "push", orig)
        for name in ("CHECKS_EVERY", "CHECKS_MOST"):
            self.addCleanup(setattr, driver, name, getattr(driver, name))
        driver.CHECKS_EVERY = 0  # a test can't wait minutes between tries

    def run_pilot(self, replan=0, goal="", **files):
        for name, text in files.items():
            (self.state / name).write_text(text)
        pilot = driver.Pilot(self.p, args(replan=replan, goal=goal))
        pilot.sleep = lambda s: None
        pilot.pause = lambda seconds=30: time.sleep(0.05)
        pilot.run(now() + timedelta(hours=1))
        return pilot

    def log(self):
        return re.sub(r"^\S+ \S+ ", "", (self.p.state / "autopilot.log").read_text(), flags=re.M)

    def merges(self):
        return sum(c.startswith("pr merge 7") for c in (self.state / "log").read_text().splitlines())

    def sessions(self):
        """Worker, critic and review sessions launched, in that order."""
        calls = [json.loads(line) for line in (self.s.tmp / "calls.jsonl").read_text().splitlines()]
        return tuple(sum(c["system"].startswith(f"# windvane {role}") for c in calls)
                     for role in ("worker", "critic", "review"))

    def planners(self):
        calls = [json.loads(line) for line in (self.s.tmp / "calls.jsonl").read_text().splitlines()]
        return sum(c["system"].startswith("# windvane planner") for c in calls)

    def landing(self):
        return json.loads((self.p.state / "state.json").read_text())["landing"]

    def test_checks_still_running_are_waited_for_and_the_merge_lands_with_no_new_session(self):
        pilot = self.run_pilot(checks=RUNNING, clear="2")
        self.assertEqual((pilot.merged, pilot.failed), (["T-001"], []))
        self.assertEqual(self.merges(), 3)
        self.assertEqual(self.sessions(), (1, 0, 1))
        self.assertEqual(self.log().count("T-001: merge waits: PR #7's required checks are still running (build); "
                                          "trying again every 0 min"), 1)
        self.assertNotIn("FAILED", self.log())
        self.assertEqual([t for t, _ in self.pushes if not t.startswith(("Done", "windvane "))], [])

    def test_checks_running_at_the_stop_fail_it_with_the_approval_kept_and_the_next_run_merges_it(self):
        pilot = self.run_pilot(checks=RUNNING, stop="2")
        self.assertEqual((pilot.merged, pilot.failed), ([], ["T-001"]))
        self.assertEqual(self.merges(), 2)
        self.assertIn("T-001: FAILED: the merge was refused: required checks are still running when the run ended; "
                      "the next run merges the kept approval",
                      self.log())
        [msg] = [m for t, m in self.pushes if t.startswith("Failed")]
        self.assertEqual([t for t, _ in self.pushes if not t.startswith("windvane ")], ["Failed: do T-001 (T-001)"])
        self.assertTrue(msg.endswith("the next run merges the kept approval. Branch kept."), msg)
        self.assertIn("T-001", self.landing())
        (self.state / "checks").write_text("[]")  # they ended while no run was going
        pilot = self.run_pilot()
        self.assertEqual(pilot.merged, ["T-001"])
        self.assertEqual(self.sessions(), (1, 0, 1))
        self.assertIn("straight to the merge", self.log())

    def test_a_check_that_never_ends_fails_it_after_the_most_wait_while_the_run_goes_on_planning(self):
        driver.CHECKS_EVERY, driver.CHECKS_MOST = 0.002, 0.005  # tries 0.12 s apart for 0.3 s: a plan between them
        started = time.monotonic()
        never = json.dumps([{"__typename": "StatusContext", "context": "ci/build", "state": "EXPECTED"}])
        pilot = self.run_pilot(replan=1, goal="more", checks=never)
        self.assertLess(time.monotonic() - started, 60)  # not the hour to its deadline
        self.assertEqual((pilot.merged, pilot.failed), ([], ["T-001"]))
        self.assertGreaterEqual(self.merges(), 2)
        self.assertEqual(self.sessions(), (1, 0, 1))
        self.assertEqual(self.planners(), 1)  # the replan ran while the merge waited
        self.assertIn("T-001: merge waits: PR #7's required checks are still running (ci/build)", self.log())
        self.assertIn("T-001: FAILED: the merge was refused: required checks are still running after 0.005 min; see "
                      "`gh pr checks 7`; once they pass, `windvane retry T-001` merges the kept approval", self.log())
        self.assertIn("T-001", self.landing())

    def test_a_retry_in_the_same_run_waits_for_its_checks_afresh(self):
        driver.CHECKS_EVERY, driver.CHECKS_MOST = 0.002, 0.005
        orig, fails = driver.Pilot.fail, []

        def fail(pilot, tid, why, head=None):  # the user asks `windvane retry` after the first fail, checks still running
            fails.append(self.merges())
            if len(fails) == 1:
                (self.p.state / driver.RETRY).write_text(tid + "\n")
            return orig(pilot, tid, why, head)
        driver.Pilot.fail = fail
        self.addCleanup(setattr, driver.Pilot, "fail", orig)
        pilot = self.run_pilot(checks=RUNNING)
        self.assertEqual((pilot.merged, pilot.failed), ([], ["T-001"]))
        self.assertEqual(len(fails), 2)
        self.assertGreaterEqual(fails[1] - fails[0], 2)  # tried again, not failed at its first refusal on the old clock
        self.assertEqual(self.log().count("T-001: merge waits: PR #7's required checks are still running"), 2)
        self.assertEqual(self.sessions(), (1, 0, 1))

    def test_a_failed_check_fails_it_at_once_naming_gh_pr_checks(self):
        pilot = self.run_pilot(checks=FAILED)
        self.assertEqual((pilot.failed, self.merges()), (["T-001"], 1))
        self.assertIn("T-001: FAILED: the merge was refused: a check failed; see `gh pr checks 7`", self.log())
        self.assertIn("T-001", self.landing())

    def test_a_required_review_fails_it_at_once_saying_retry_merges_the_kept_approval(self):
        pilot = self.run_pilot(review="REVIEW_REQUIRED", checks=RUNNING)
        self.assertEqual((pilot.failed, self.merges()), (["T-001"], 1))
        self.assertIn("T-001: FAILED: the merge was refused: a required review hasn't approved it; once it's given, "
                      "`windvane retry T-001` merges the kept approval", self.log())
        self.assertNotIn("merge waits", self.log())

    def test_a_draft_hold_let_go_between_runs_merges_the_kept_approval_with_no_session(self):
        pilot = self.run_pilot(draft="", timeline="committed\nconvert_to_draft\nreviewed\n")
        self.assertEqual((pilot.merged, pilot.failed, self.merges()), ([], [], 0))
        [msg] = [m for t, m in self.pushes if t.startswith("Held")]
        self.assertIn("`gh pr ready 7`; the next run merges the kept approval", msg)
        (self.state / "draft").unlink()  # the user ran `gh pr ready 7`
        pilot = self.run_pilot()
        self.assertEqual(pilot.merged, ["T-001"])
        self.assertEqual(self.sessions(), (1, 0, 1))

    def test_a_ready_refused_with_a_403_says_the_login_lacks_write_access(self):
        pilot = self.run_pilot(draft="", stuck="gh: Resource not accessible by integration (HTTP 403)")
        self.assertEqual(pilot.failed, ["T-001"])
        self.assertIn("T-001: FAILED: PR #7 is a draft and GitHub refused to mark it ready: the gh login lacks write "
                      "access to pull requests (gh: Resource not accessible by integration (HTTP 403))", self.log())


class BlockedBy(unittest.TestCase):
    def pr(self, checks=(), review=""):
        return {"mergeable": "MERGEABLE", "mergeStateStatus": "BLOCKED", "statusCheckRollup": list(checks),
                "reviewDecision": review}

    def test_running_and_expected_checks_are_pending(self):
        for c in ({"status": "QUEUED", "conclusion": ""}, {"__typename": "StatusContext", "state": "PENDING"},
                  {"__typename": "StatusContext", "state": "EXPECTED"}):
            self.assertEqual(held.blocked_by(self.pr([c])), ["PENDING"])
        self.assertEqual(held.merge_state(self.pr([c]), ""), "required checks are still running")

    def test_failures_and_reviews_come_first_and_finished_checks_are_left_out(self):
        checks = [{"status": "COMPLETED", "conclusion": "SUCCESS"}, {"status": "COMPLETED", "conclusion": "SKIPPED"},
                  {"state": "ERROR"}, {"status": "IN_PROGRESS"}]
        self.assertEqual(held.blocked_by(self.pr(checks, "CHANGES_REQUESTED")), ["FAILED", "REVIEW", "PENDING"])

    def test_a_block_neither_shows_reads_as_before(self):
        self.assertEqual(held.blocked_by(self.pr(review="APPROVED")), [])
        self.assertEqual(held.merge_state(self.pr(), ""), "a required check or review blocks it")
        self.assertEqual(held.blocked_by(dict(self.pr([{"status": "QUEUED"}]), mergeStateStatus="CLEAN")), [])


if __name__ == "__main__":
    unittest.main()
