"""T-086: a set-aside push for an error the ticket brought on itself leads with the fix."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_set_aside import Base  # noqa: E402


class SetAsideFix(Base):
    tickets = ("T-001",)
    ERROR = "API Error: 400 {\"type\":\"error\",\"error\":{\"message\":\"Prompt is too long: 212000 tokens > 200000\"}}"

    def test_a_prompt_too_long_push_leads_with_the_fix_and_keeps_the_error_below(self):
        self.pilot().set_aside("T-001", self.ERROR)
        [(_, msg)] = self.aside()
        first, *rest = msg.split("\n")
        self.assertEqual(first, "Sessions died 4×: prompt is too long. Split the ticket, or trim what it reads.")
        self.assertIn(self.ERROR, "\n".join(rest))
        self.assertIn("Branch kept, run goes on.", "\n".join(rest))

    def test_an_unmatched_error_keeps_todays_push(self):
        self.pilot().set_aside("T-001", "API Error: 529 Overloaded")
        [(_, msg)] = self.aside()
        self.assertEqual(msg, "Sessions died 4×: API Error: 529 Overloaded. Branch kept, run goes on.\n"
                              "Other sessions worked, so it's this ticket's doing.")


if __name__ == "__main__":
    unittest.main()
