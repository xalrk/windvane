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


# Each prompt's size in bytes when it was capped (T-295): every session re-reads its prompt on every turn, so new
# text replaces old. Bytes, not lines, so a longer line can't dodge the cap. A prompt that shrinks lowers its cap here.
CAPS = {"CRITIC.md": 3824, "CRITIC-numeric.md": 2164, "CRITIC-project.md": 842, "FIX.md": 987, "JUDGE.md": 844,
        "PERIODIC.md": 1089, "PLAN.md": 3090, "PREFLIGHT.md": 1299, "REVIEW.md": 2157, "SCOUT.md": 1108, "SEED.md": 1991, "TRIAGE-REQUESTS.md": 1499, "WORKER.md": 7880}


def over_cap(sizes):
    """The prompts in `sizes` (name: bytes) past their cap, and any without one."""
    return sorted(n for n, size in sizes.items() if size > CAPS.get(n, -1))


class Caps(unittest.TestCase):
    def test_no_prompt_grew(self):
        self.assertEqual(over_cap({f.name: len(f.read_bytes()) for f in PROMPTS.glob("*.md")}), [])

    def test_one_byte_more_is_caught(self):
        for name, cap in CAPS.items():
            self.assertEqual(over_cap({name: cap + 1}), [name])
            self.assertEqual(over_cap({name: cap}), [])


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
        self.assertIn("spend the review on the code, the tests and the untried paths", review)

    def test_only_a_passing_verdict_settles_the_trial(self):
        """A critic session that died on a revised head leaves its older `revise` verdict as the last one: the
        reviewer mustn't trust a trial of another head, nor run the change itself without the critic's guards."""
        self.assertIn('Any other verdict, or none, settles nothing: judge "Done when" from the code and tests, running '
                      'nothing new', self.text("REVIEW.md"))

    def test_the_reviewer_sends_back_what_users_meet(self):
        """T-318: a correctness bar of "every input there is" bounced tickets over input no user types; that goes
        under nits, while security and data loss count whatever the input."""
        review = self.text("REVIEW.md")
        self.assertIn("Correctness on what users would meet (input they'd type or paste, files and output the program "
                      "makes, errors the ticket names)", review)
        self.assertIn("security (injection, secrets, unsafe shell) or data loss on any input", review)
        self.assertIn("data loss on any input. Other freak input (thousand-digit numbers, undecodable bytes, its own "
                      "files hand-edited or nested deep, odd env or locale, closed std streams) is a nit unless the "
                      "ticket or docs name it", review)
        self.assertIn("list nits under `Nits:` and approve", review)

    def test_the_planner_drops_test_only_critic_notes(self):
        self.assertIn("A critic note asking only for a test or a pack measurement is dropped, not ticketed, unless it "
                      "names a fault a user would meet", self.text("PLAN.md"))

    def test_the_rules_reach_the_sessions(self):
        """The sessions get these rules through `rules()`, not only the files on disk."""
        s = Sandbox()
        self.addCleanup(s.close)
        p = Project(s.root)
        for name, words in (("CRITIC.md", "is never a point"), ("REVIEW.md", "don't redo that trial"),
                            ("REVIEW.md", "is a nit unless the ticket or docs name it"),
                            ("PLAN.md", "dropped, not ticketed")):
            self.assertIn(words, " ".join(rules(p, name).split()), name)


if __name__ == "__main__":
    unittest.main()
