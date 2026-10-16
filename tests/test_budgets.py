"""Each kind of send-back spends its own budget: red checks `check_retries`, merge conflicts `conflict_retries`, the
critic `[critic] revisions`, review changes `review_rounds`. A flaky check mustn't set aside a ticket the reviewer
never asked anything of."""
import os
import sys
import unittest
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from windvane import driver  # noqa: E402
from windvane.config import Project  # noqa: E402
from windvane.util import now  # noqa: E402
from helpers import Sandbox  # noqa: E402
from test_parallel import args  # noqa: E402

RED_TWICE = 'check = "test ! -f T-001.txt || test $(grep -c built T-001.txt) -ge 3"'  # green from the third worker run


class Budgets(unittest.TestCase):
    def setUp(self):
        self.s = Sandbox()
        self.s.ticket("T-001")

    def tearDown(self):
        self.s.close()

    def config(self, check=None, critic=None, **autopilot):
        text = (self.s.root / ".windvane/config.toml").read_text()
        if check:
            text = text.replace('check = "test -f README"', check)
        if critic is not None:
            text = text.replace("enabled = false", f"enabled = true\nrevisions = {critic}")
        text += "[autopilot]\n" + "".join(f"{k} = {v}\n" for k, v in autopilot.items())
        self.s.write(".windvane/config.toml", text)
        self.s.publish()

    def run_pilot(self, review=None):
        pilot = driver.Pilot(Project(self.s.root), args(parallel=1))
        self.lines, log = [], pilot.log
        pilot.log = lambda msg: (self.lines.append(msg), log(msg))
        if review:
            pilot.review = review(pilot.review)
        pilot.run(now() + timedelta(minutes=40))
        return pilot

    def failure(self):
        return next(l for l in self.lines if "T-001: FAILED" in l)

    def test_two_red_checks_then_a_pass_merges_with_one_review_round(self):
        self.config(check=RED_TWICE, review_rounds=1)
        pilot = self.run_pilot()
        self.assertEqual((pilot.merged, pilot.failed), (["T-001"], []))
        self.assertEqual(pilot.bounces["T-001"], {"check": 2})

    def test_a_third_red_check_sets_it_aside_naming_check_retries(self):
        self.config(check=RED_TWICE, check_retries=1)
        pilot = self.run_pilot()
        self.assertEqual(pilot.failed, ["T-001"])
        self.assertIn("the check sent it back 2 times; `[autopilot] check_retries` = 1 allows 1:", self.failure())

    def test_a_third_conflict_sets_it_aside_naming_conflict_retries(self):
        os.environ["FAKE_CONFLICT"] = "1"  # the worker rewrites shared.txt, and merges main when told it conflicts
        self.config(conflict_retries=2, review_rounds=1)  # the old shared count set it aside at the second

        def desk_lands(review):
            def wrapped(*a, **kw):
                out = review(*a, **kw)
                self.s.write("shared.txt", f"desk {len(self.s.git('log', '--oneline').splitlines())}\n")
                self.s.publish("desk change")
                return out
            return wrapped
        pilot = self.run_pilot(desk_lands)
        self.assertEqual(pilot.failed, ["T-001"])
        self.assertIn("a merge conflict sent it back 3 times; `[autopilot] conflict_retries` = 2 allows 2:",
                      self.failure())

    def test_a_missing_critic_pack_spends_the_critics_budget(self):
        self.config(critic=0, review_rounds=5, check_retries=5)
        pilot = self.run_pilot()
        self.assertEqual(pilot.failed, ["T-001"])
        self.assertIn("the critic sent it back 2 times; `[critic] revisions` = 0 allows 1 (one more for a READY "
                      "without the evidence pack):", self.failure())

    def test_review_changes_name_review_rounds(self):
        os.environ["FAKE_REVIEW"] = "CHANGES"
        self.config(review_rounds=1)
        pilot = self.run_pilot()
        self.assertEqual(pilot.failed, ["T-001"])
        self.assertIn("the reviewer asked for changes 1 time, the most `[autopilot] review_rounds` = 1 allows",
                      self.failure())


if __name__ == "__main__":
    unittest.main()
