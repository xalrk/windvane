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


if __name__ == "__main__":
    unittest.main()
