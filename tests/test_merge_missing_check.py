"""A merge blocked by a required check that never started on the PR (a path filter, a workflow off the default
branch, Actions off) fails at once naming it, from the base's rulesets or branch protection, instead of the generic
"a required check or review blocks it", once no check it may wait on (a `needs:` job) is still running; a required
check still running waits as before, and a gh too old to tell required checks apart says which version does."""
import json
import re
import sys
import time
import unittest
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from windvane import driver  # noqa: E402
from windvane.config import Project  # noqa: E402
from windvane.util import now  # noqa: E402
from test_merge_blocked import args  # noqa: E402
from test_merge_wait_restart import GH, PREVIEW  # noqa: E402
from helpers import Sandbox  # noqa: E402

# test_merge_wait_restart's fake gh, with the base's required checks read through `gh api`: the state dir's `rules` (a
# JSON list of names) answers the rulesets' call and `protection` the branch's; with no such file the call fails, as
# one GitHub refuses does. `unknown` (a count) refuses that many merges with GitHub not yet sure it can merge, as it
# is just after a push. `later` ({"in": n, "checks": [...]}) gives the PR that rollup after n refused merges, and lifts
# `held`: a job that only starts once another ends.
GH = GH.replace('''"mergeable": "MERGEABLE", "mergeStateStatus": "BLOCKED" if blocked else "CLEAN",''', '''**unknown_view(),''').replace(
    """if args[:2] == ["pr", "list"]:""", """def unknown_view():
    n = int(read("unknown", "0"))
    if n > 0:
        (st / "unknown").write_text(str(n - 1))
        return {{"mergeable": "UNKNOWN", "mergeStateStatus": "UNKNOWN"}}
    return {{"mergeable": "MERGEABLE", "mergeStateStatus": "BLOCKED" if blocked else "CLEAN"}}
if int(read("unknown", "0")) > 0 and args[:2] == ["pr", "merge"]:
    sys.exit("GraphQL: Pull Request is not mergeable (mergePullRequest)")
if args[:2] == ["pr", "list"]:""", 1).replace("""elif args[0] == "api" and "timeline" in args[2]:""", """elif args[0] == "api" and "/rules/branches/" in args[1]:
    if not (st / "rules").exists():
        sys.exit("gh: Not Found (HTTP 404)")
    print(json.dumps([{{"type": "deletion"}}] + [{{"type": "required_status_checks", "parameters": {{
        "required_status_checks": [{{"context": n, "integration_id": 15368}}]}}}} for n in json.loads(read("rules"))]))
elif args[0] == "api" and "/branches/" in args[1]:
    if not (st / "protection").exists():
        sys.exit("gh: Branch not protected (HTTP 404)")
    print(json.dumps({{"name": "main", "protection": {{"enabled": True, "required_status_checks": {{
        "enforcement_level": "non_admins", "contexts": json.loads(read("protection"))}}}}}}))
elif args[0] == "api" and "timeline" in args[2]:""").replace("""    if blocked:
        for name, then in""", """    if blocked and (st / "later").exists():
        later = json.loads(read("later"))
        later["in"] -= 1
        (st / "later").write_text(json.dumps(later))
        if later["in"] == 0:
            (st / "checks").write_text(json.dumps(later["checks"]))
            (st / "held").unlink(missing_ok=True)
    if blocked:
        for name, then in""", 1)

CI = {"__typename": "CheckRun", "name": "ci", "status": "IN_PROGRESS", "conclusion": ""}
CI_PASSED = dict(CI, status="COMPLETED", conclusion="SUCCESS")
TEST_PASSED = dict(CI_PASSED, name="test")
LINT = {"__typename": "CheckRun", "name": "lint", "status": "COMPLETED", "conclusion": "SUCCESS"}


class MergeMissingCheck(unittest.TestCase):
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
        orig, driver.push = driver.push, lambda *a, **k: None
        self.addCleanup(setattr, driver, "push", orig)
        for name in ("CHECKS_EVERY", "CHECKS_MOST"):
            self.addCleanup(setattr, driver, name, getattr(driver, name))
        driver.CHECKS_EVERY = 0  # a test can't wait minutes between tries

    def run_pilot(self, **files):
        for name, text in files.items():
            (self.state / name).write_text(text)
        pilot = driver.Pilot(self.p, args())
        pilot.sleep = lambda s: None
        pilot.pause = lambda seconds=30: time.sleep(0.05)
        pilot.run(now() + timedelta(hours=1))
        return pilot

    def log(self):
        return re.sub(r"^\S+ \S+ ", "", (self.p.state / "autopilot.log").read_text(), flags=re.M)

    def renumbered_head(self):
        """A ticket branch whose merge with main conflicts only in an appended entry: the driver resolves it and
        pushes the resolution to the branch just before the merge, so GitHub has had no time to queue its checks. A
        pilot ready to integrate it, and the branch's approved head."""
        cfg = self.s.root / ".windvane/config.toml"
        cfg.write_text(cfg.read_text() + "[[append_only]]\nfile = \"LOG.md\"\nentry = '^- L(\\d+):'\nref = 'L{n}'\n")
        self.commit("LOG.md", "- L1: start\n")
        self.s.git("switch", "-q", "-c", "t-001-work")
        self.commit("LOG.md", "- L1: start\n- L2: ticket\n")
        self.s.git("push", "-q", "origin", "t-001-work")
        sha = self.s.git("rev-parse", "HEAD")
        self.s.git("switch", "-q", "main")
        self.commit("LOG.md", "- L1: start\n- L2: main\n")
        self.p = Project(self.s.root)
        pilot = driver.Pilot(self.p, args())
        pilot.deadline = now() + timedelta(hours=1)
        self.assertTrue(pilot.prepare(0))
        pilot.queue = driver.Queue(self.p)
        return pilot, sha

    def commit(self, path, text):
        self.s.write(path, text)
        self.s.git("add", "-A")
        self.s.git("commit", "-qm", "change")
        if self.s.git("branch", "--show-current") == "main":
            self.s.git("push", "-q", "origin", "main")

    def gh_calls(self, start):
        return [c for c in (self.state / "log").read_text().splitlines() if c.startswith(start)]

    def test_a_required_check_missing_from_the_rollup_fails_at_once_by_name(self):
        pilot = self.run_pilot(checks=json.dumps([LINT]), rules='["ci"]', required="[]", held="")
        self.assertEqual((pilot.failed, len(self.gh_calls("pr merge 7"))), (["T-001"], 1))
        self.assertIn("T-001: FAILED: the merge was refused: required check `ci` never started on this PR; once the "
                      "check has run on the PR, `windvane retry T-001` merges the kept approval", self.log())
        self.assertNotIn("merge waits", self.log())

    def test_a_pr_with_no_checks_at_all_names_the_missing_ones_from_branch_protection(self):
        pilot = self.run_pilot(protection='["test (macos)", "build"]', held="")
        self.assertEqual(pilot.failed, ["T-001"])
        self.assertIn("the merge was refused: required checks `build`, `test (macos)` never started on this PR",
                      self.log())

    def test_a_required_check_still_running_waits_and_the_rules_are_asked_once(self):
        pilot = self.run_pilot(checks=json.dumps([CI]), rules='["ci"]', required='["ci"]', clear="3")
        self.assertEqual(pilot.merged, ["T-001"])
        self.assertIn("T-001: merge waits: PR #7's required checks are still running (ci)", self.log())
        self.assertNotIn("never started", self.log())
        self.assertEqual(len(self.gh_calls("api repos/{owner}/{repo}/rules/branches/")), 1)  # once a run per base

    def test_a_required_job_not_started_behind_a_running_check_waits_and_merges_once_it_passes(self):
        later = json.dumps({"in": 2, "checks": [CI_PASSED, TEST_PASSED]})  # `test` needs `ci`: it starts once ci ends
        pilot = self.run_pilot(checks=json.dumps([CI]), rules='["ci", "test"]', required='["ci", "test"]', held="",
                               later=later)
        self.assertEqual((pilot.failed, pilot.merged), ([], ["T-001"]))
        self.assertIn("T-001: merge waits: PR #7's required checks are still running (ci, test, not started yet)",
                      self.log())
        self.assertNotIn("never started", self.log())

    def test_rules_github_wont_give_fall_back_to_the_generic_refusal(self):
        pilot = self.run_pilot(checks=json.dumps([LINT]), required="[]", held="")
        self.assertEqual((pilot.failed, len(self.gh_calls("pr merge 7"))), (["T-001"], 1))
        self.assertIn("T-001: FAILED: the merge was refused: a required check or review blocks it", self.log())
        self.assertNotIn("never started", self.log())

    def test_an_old_gh_says_which_version_tells_required_checks_apart_and_a_missing_one_fails_once_all_end(self):
        pilot = self.run_pilot(checks=json.dumps([PREVIEW]), rules='["ci"]', held="", clear="2")
        self.assertEqual((pilot.failed, len(self.gh_calls("pr merge 7"))), (["T-001"], 2))
        self.assertIn("T-001: merge waits: PR #7's required checks are still running (preview, ci, not started yet)", self.log())
        self.assertIn("T-001: gh couldn't say which of PR #7's checks are required (`gh pr checks --required`); any "
                      "check still running counts as one; gh 2.50.0 or later (`gh pr checks --required --json`) "
                      "tells them apart: `gh --version` shows yours", self.log())
        self.assertIn("the merge was refused: required check `ci` never started on this PR", self.log())

    def test_a_head_pushed_just_before_the_merge_waits_a_pass_for_a_missing_check_and_merges_once_it_shows(self):
        pilot, sha = self.renumbered_head()
        (self.state / "rules").write_text('["ci"]')
        (self.state / "required").write_text("[]")
        (self.state / "held").write_text("")
        pilot.integrate("T-001", "t-001-work", sha)
        self.assertEqual((pilot.failed, pilot.merged), ([], []))
        self.assertIn("T-001: merge waits: PR #7's required checks are still running (ci, not started yet)", self.log())
        head = pilot.landing["T-001"]
        self.assertNotEqual(head["sha"], sha)  # the pushed resolution
        (self.state / "checks").write_text(json.dumps([CI_PASSED]))  # Actions queued it, and it passed
        (self.state / "held").unlink()
        pilot.integrate("T-001", head["branch"], head["sha"])
        self.assertEqual(pilot.merged, ["T-001"])
        self.assertNotIn("never started", self.log())

    def test_a_check_still_missing_on_the_pass_after_that_push_never_started(self):
        pilot, sha = self.renumbered_head()
        (self.state / "rules").write_text('["ci"]')
        (self.state / "required").write_text("[]")
        (self.state / "held").write_text("")
        pilot.integrate("T-001", "t-001-work", sha)
        head = pilot.landing["T-001"]
        pilot.integrate("T-001", head["branch"], head["sha"])
        self.assertEqual(pilot.failed, ["T-001"])
        self.assertIn("T-001: FAILED: the merge was refused: required check `ci` never started on this PR", self.log())

    def test_a_pushed_head_github_cant_judge_yet_tries_again_at_once_instead_of_waiting_on_checks(self):
        pilot, sha = self.renumbered_head()
        (self.state / "unknown").write_text("1")
        pilot.integrate("T-001", "t-001-work", sha)
        self.assertEqual((pilot.failed, pilot.merged), ([], ["T-001"]))
        self.assertIn("T-001: merge refused (GitHub hadn't worked out whether it can merge); trying again", self.log())
        self.assertNotIn("merge waits", self.log())
        self.assertEqual(len(self.gh_calls("api ")), 0)  # not BLOCKED: no rules asked for

    def test_rules_github_didnt_give_are_asked_again_on_the_next_refusal(self):
        pilot, blocked = driver.Pilot(self.p, args()), {"mergeStateStatus": "BLOCKED", "baseRefName": "main"}
        self.assertEqual(pilot.expected_of(blocked), set())
        (self.state / "rules").write_text('["ci"]')
        self.assertEqual((pilot.expected_of(blocked), pilot.expected_of(blocked)), ({"ci"}, {"ci"}))
        self.assertEqual(len(self.gh_calls("api repos/{owner}/{repo}/rules/branches/")), 2)


if __name__ == "__main__":
    unittest.main()
