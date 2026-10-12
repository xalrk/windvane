"""T-142: `User-facing: no` means no user will ever see or feel the change, not just "nothing calls it yet". Logic a
planned ticket will show (the numbers a later page displays) gets the critic now, and the reviewer checks the claim
by the same test."""
import unittest
from pathlib import Path

PROMPTS = Path(__file__).resolve().parents[1] / "prompts"


def flat(name):
    """The prompt as one line, so a rule reads the same however it's wrapped."""
    return " ".join((PROMPTS / name).read_text().split())


def rule(name):
    """The words around a prompt's `User-facing: no`: where the test for the claim has to sit."""
    text = flat(name)
    at = text.index("`User-facing: no")
    return text[at:at + 200]


class UserFacing(unittest.TestCase):
    def test_the_worker_counts_what_a_later_ticket_will_show(self):
        self.assertIn("if no user will ever see or feel it, even via a later ticket", rule("WORKER.md"))
        self.assertIn("logic a planned ticket shows is `yes`", rule("WORKER.md"))

    def test_the_reviewer_checks_the_claim_by_the_same_test(self):
        self.assertIn("no user will ever see or feel the diff, even via a later ticket", rule("REVIEW.md"))

    def test_the_prompts_did_not_grow(self):
        self.assertLessEqual(len((PROMPTS / "WORKER.md").read_text().splitlines()), 93)
        self.assertLessEqual(len((PROMPTS / "REVIEW.md").read_text().splitlines()), 29)


if __name__ == "__main__":
    unittest.main()
