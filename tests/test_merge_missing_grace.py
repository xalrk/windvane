"""A required check missing from the PR once nothing runs gets one more merge pass before it reads as never started:
GitHub can take a few seconds to queue a `needs:` job after the job it waits on ends. The pass given is saved with
the merge wait, so a restarted driver neither skips nor repeats it; before failing, the base's rule is asked again,
so a check dropped from it since lets the merge through; the refusal names the `gh workflow run` that starts it."""
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
from windvane.doctor import dispatched_checks  # noqa: E402
from windvane.util import now  # noqa: E402
from test_merge_blocked import args  # noqa: E402
from test_merge_missing_check import CI, CI_PASSED, GH, TEST_PASSED  # noqa: E402
from helpers import Sandbox  # noqa: E402

# `test` needs `ci`, and the workflow can be started by hand.
WORKFLOW = """name: tests
on:
  workflow_dispatch:
jobs:
  ci:
    runs-on: ubuntu-latest
    steps:
      - run: make
  unit:
    name: test
    needs: ci
    runs-on: ubuntu-latest
    steps:
      - run: make test
"""


class MergeMissingGrace(unittest.TestCase):
    """test_merge_missing_check's fake gh: `later` gives the PR a new rollup after n refused merges and lifts `held`."""
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
        self.addCleanup(setattr, driver, "CHECKS_EVERY", driver.CHECKS_EVERY)
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

    def commit(self, path, text):
        self.s.write(path, text)
        self.s.git("add", "-A")
        self.s.git("commit", "-qm", "change")
        if self.s.git("branch", "--show-current") == "main":
            self.s.git("push", "-q", "origin", "main")

    def gh_calls(self, start):
        return [c for c in (self.state / "log").read_text().splitlines() if c.startswith(start)]

    def ready_head(self, workflow=WORKFLOW):
        """A ticket branch pushed with the workflow on it, and a pilot ready to merge it pass by pass: its head."""
        self.s.git("switch", "-q", "-c", "t-001-work")
        self.commit(".github/workflows/tests.yml", workflow)
        self.s.git("push", "-q", "origin", "t-001-work")
        sha = self.s.git("rev-parse", "HEAD")
        self.s.git("switch", "-q", "main")
        return self.pilot(), sha

    def pilot(self, restarted=False):
        pilot = driver.Pilot(Project(self.s.root), args())
        pilot.deadline = now() + timedelta(hours=1)
        self.assertTrue(pilot.prepare(0))
        pilot.carry_over(restarted)
        pilot.queue = driver.Queue(pilot.p)
        return pilot

    def blocked_on_test(self):
        """`ci` has passed and `test` (required) isn't on the PR."""
        for name, text in (("checks", json.dumps([CI_PASSED])), ("rules", '["ci", "test"]'),
                           ("required", '["ci", "test"]'), ("held", "")):
            (self.state / name).write_text(text)

    def test_a_check_that_shows_on_the_pass_after_the_last_one_ended_merges(self):
        self.commit(".github/workflows/tests.yml", WORKFLOW)
        later = json.dumps({"in": 2, "checks": [CI_PASSED, TEST_PASSED]})  # `test` is queued once ci has ended
        pilot = self.run_pilot(checks=json.dumps([CI]), rules='["ci", "test"]', required='["ci", "test"]', held="",
                               clear="1", later=later)
        self.assertEqual((pilot.failed, pilot.merged), ([], ["T-001"]))
        self.assertNotIn("never started", self.log())

    def test_one_that_never_shows_fails_after_exactly_one_more_pass_naming_the_command_that_starts_it(self):
        self.commit(".github/workflows/tests.yml", WORKFLOW)
        pilot = self.run_pilot(checks=json.dumps([CI]), rules='["ci", "test"]', required='["ci", "test"]', held="",
                               clear="1")
        # the first pass ends ci; the second finds nothing running and gives the one more; the third fails
        self.assertEqual((pilot.failed, len(self.gh_calls("pr merge 7"))), (["T-001"], 2))
        self.assertIn("T-001: FAILED: the merge was refused: required check `test` never started on this PR; start "
                      "it: `gh workflow run tests.yml --ref t-001-work`; once the check has run on the PR, `windvane "
                      "retry T-001` merges the kept approval", self.log())

    def test_a_restart_between_the_passes_neither_repeats_the_one_more_nor_skips_it(self):
        pilot, sha = self.ready_head()
        self.blocked_on_test()
        pilot.integrate("T-001", "t-001-work", sha)
        self.assertEqual((pilot.failed, pilot.check_wait["T-001"]["quiet"]), ([], sha))  # saved with the wait
        again = self.pilot(restarted=True)
        again.integrate("T-001", "t-001-work", sha)
        self.assertEqual(again.failed, ["T-001"])
        self.assertIn("required check `test` never started on this PR", self.log())

    def test_a_check_that_shows_after_a_restart_merges(self):
        pilot, sha = self.ready_head()
        self.blocked_on_test()
        pilot.integrate("T-001", "t-001-work", sha)
        self.assertEqual(pilot.failed, [])
        (self.state / "checks").write_text(json.dumps([CI_PASSED, TEST_PASSED]))
        (self.state / "held").unlink()
        again = self.pilot(restarted=True)
        again.integrate("T-001", "t-001-work", sha)
        self.assertEqual((again.failed, again.merged), ([], ["T-001"]))

    def test_a_new_run_gives_the_pass_again(self):
        pilot, sha = self.ready_head()
        self.blocked_on_test()
        pilot.integrate("T-001", "t-001-work", sha)
        fresh = self.pilot()
        fresh.integrate("T-001", "t-001-work", sha)
        self.assertEqual(fresh.failed, [])

    def test_a_check_dropped_from_the_rule_mid_run_lets_the_merge_through(self):
        pilot, sha = self.ready_head()
        self.blocked_on_test()
        pilot.integrate("T-001", "t-001-work", sha)
        (self.state / "rules").write_text('["ci"]')  # the user drops `test`; GitHub still says BLOCKED on this pass
        pilot.integrate("T-001", "t-001-work", sha)
        self.assertEqual(pilot.failed, [])
        (self.state / "held").unlink()
        pilot.integrate("T-001", "t-001-work", sha)
        self.assertEqual((pilot.failed, pilot.merged), ([], ["T-001"]))
        self.assertEqual(len(self.gh_calls("api repos/{owner}/{repo}/rules/branches/")), 2)
        self.assertNotIn("never started", self.log())

    def test_a_workflow_that_cant_be_started_by_hand_leaves_the_name_to_the_user(self):
        pilot, sha = self.ready_head(WORKFLOW.replace("workflow_dispatch:", "push:"))
        self.blocked_on_test()
        pilot.integrate("T-001", "t-001-work", sha)
        pilot.integrate("T-001", "t-001-work", sha)
        self.assertIn("start it: `gh workflow run <workflow> --ref t-001-work`", self.log())

    def test_the_checks_a_workflow_started_by_hand_reports(self):
        self.assertEqual(dispatched_checks(WORKFLOW), {"ci", "test"})
        self.assertEqual(dispatched_checks("on: [push, workflow_dispatch]\njobs:\n  'lint':\n    runs-on: x\n"),
                         {"lint"})
        self.assertEqual(dispatched_checks(WORKFLOW.replace("workflow_dispatch:", "pull_request:")), set())


if __name__ == "__main__":
    unittest.main()
