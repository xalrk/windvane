"""T-142: `User-facing: no` means no user will ever see or feel the change, not just "nothing calls it yet". Logic a
planned ticket will show (the numbers a later page displays) gets the critic now, and the reviewer checks the claim
by the same test."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from windvane.config import Project  # noqa: E402
from windvane.runner import rules  # noqa: E402
from helpers import Sandbox  # noqa: E402

PROMPTS = Path(__file__).resolve().parents[1] / "prompts"


class UserFacing(unittest.TestCase):
    def setUp(self):
        self.s = Sandbox()
        self.addCleanup(self.s.close)
        self.p = Project(self.s.root)

    def rule(self, name):
        """The words around `User-facing: no` in the rules a session is started with, unwrapped."""
        text = " ".join(rules(self.p, name).split())
        at = text.index("`User-facing: no")
        return text[at:at + 200]

    def test_the_worker_counts_what_a_later_ticket_will_show(self):
        self.assertIn("if no user will ever see or feel it, even via a later ticket", self.rule("WORKER.md"))
        self.assertIn("logic a planned ticket shows is `yes`", self.rule("WORKER.md"))

    def test_the_reviewer_checks_the_claim_by_the_same_test(self):
        self.assertIn("no user will ever see or feel the diff, even via a later ticket", self.rule("REVIEW.md"))

    def test_the_prompts_did_not_grow(self):
        self.assertLessEqual(len((PROMPTS / "WORKER.md").read_text().splitlines()), 93)
        self.assertLessEqual(len((PROMPTS / "REVIEW.md").read_text().splitlines()), 29)
        self.assertLessEqual(len((PROMPTS / "CRITIC.md").read_text().splitlines()), 54)
        self.assertLessEqual(len((PROMPTS / "PLAN.md").read_text().splitlines()), 42)


class WhoJudgesWhat(unittest.TestCase):
    """T-263: the critic's points name what a user meets, never a test or a pack measurement; the reviewer owns the
    code and doesn't redo the critic's trial; the planner drops a critic note that only asks for a test."""

    def text(self, name):
        return " ".join((PROMPTS / name).read_text().split())

    def lists(self):
        """The critic's rules for its two lists, unwrapped."""
        text = self.text("CRITIC.md")
        return text[text.index("## Judge only"):text.index("## Evidence")]

    def test_every_critic_point_names_what_a_user_meets(self):
        self.assertIn("Every point names what a user meets (a command's output, a push, a lost file, a wait) and what "
                      "fixed looks like", self.lists())
        self.assertIn("- <what a user meets> -> <the follow-up>", self.text("CRITIC.md"))

    def test_tests_and_pack_measurements_are_never_a_critic_point(self):
        rule = self.lists()
        self.assertIn("A missing test (the reviewer judges tests), a pack that doesn't show something or a count to add "
                      "is never a point", rule)
        self.assertIn("a fault you suspect but couldn't try is a `no` on its check, with the reason", rule)
        self.assertIn("A code reviewer comes after you for the code and its tests", self.text("CRITIC.md"))

    def test_the_reviewer_skips_what_the_critic_tried(self):
        review = self.text("REVIEW.md")
        self.assertIn("A critic verdict ending `fixes=0` says what it ran and saw: don't redo that trial or re-raise a "
                      "point it settled", review)
        self.assertIn("spend the review on the code, the tests and the paths no trial reached", review)

    def test_only_a_passing_verdict_settles_the_trial(self):
        """A critic session that died on a revised head leaves its older `revise` verdict as the last one: the
        reviewer must try that head itself rather than trust a trial of another head."""
        self.assertIn("Any other verdict, or none, settles nothing: try the main promise yourself", self.text("REVIEW.md"))

    def test_the_planner_drops_test_only_critic_notes(self):
        self.assertIn("A critic note asking only for a test or a pack measurement is dropped, not ticketed, unless it "
                      "names a fault a user would meet", self.text("PLAN.md"))

    def test_the_rules_reach_the_sessions(self):
        """The sessions get these rules through `rules()`, not only the files on disk."""
        s = Sandbox()
        self.addCleanup(s.close)
        p = Project(s.root)
        for name, words in (("CRITIC.md", "is never a point"), ("REVIEW.md", "don't redo that trial"),
                            ("PLAN.md", "dropped, not ticketed")):
            self.assertIn(words, " ".join(rules(p, name).split()), name)


if __name__ == "__main__":
    unittest.main()
