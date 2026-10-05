"""T-086: `lead` says so when a push's fixed words alone pass the lock screen (a long ticket name or fix in a real
run would otherwise slip by), and a set-aside push for an error the ticket brought on itself leads with the fix."""
import io
import sys
import unittest
from contextlib import redirect_stderr
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from windvane import driver  # noqa: E402
from test_set_aside import Base  # noqa: E402

HEAD, TAIL = "Hit its max turns while working on " + "a very long ticket name " * 2, ", so split the ticket and trim it."


class Overflow(unittest.TestCase):
    def test_a_head_and_tail_past_the_lock_screen_log_one_line_and_still_return_the_text(self):
        self.assertGreater(len(HEAD + TAIL), driver.LOCK_SCREEN)
        lines = []
        out = driver.lead(HEAD, "x", TAIL, log=lines.append)
        self.assertTrue(out.startswith(HEAD), out)
        self.assertIn(TAIL, out)
        self.assertEqual(len(lines), 1, lines)
        self.assertIn(HEAD.strip(), lines[0])

    def test_without_a_hook_the_line_goes_to_stderr(self):
        err = io.StringIO()
        with redirect_stderr(err):
            driver.lead(HEAD, "x", TAIL)
        self.assertEqual(len(err.getvalue().splitlines()), 1)

    def test_a_line_that_fits_logs_nothing(self):
        lines = []
        driver.lead("Hit ", "max turns " * 20, ". Branch kept.", log=lines.append)
        self.assertEqual(lines, [])


class SetAsideFix(Base):
    tickets = ("T-001",)
    ERROR = "API Error: 400 {\"type\":\"error\",\"error\":{\"message\":\"Prompt is too long: 212000 tokens > 200000\"}}"

    def test_a_prompt_too_long_push_leads_with_the_fix_and_keeps_the_error_below(self):
        self.pilot().set_aside("T-001", self.ERROR)
        [(_, msg)] = self.aside()
        first, *rest = msg.split("\n")
        self.assertEqual(first, "Sessions died 4×: prompt is too long. Split the ticket, or trim what it reads.")
        self.assertLessEqual(len(first), driver.LOCK_SCREEN)
        self.assertIn(self.ERROR, "\n".join(rest))
        self.assertIn("Branch kept, run goes on.", "\n".join(rest))

    def test_an_unmatched_error_keeps_todays_push(self):
        self.pilot().set_aside("T-001", "API Error: 529 Overloaded")
        [(_, msg)] = self.aside()
        self.assertEqual(msg, "Sessions died 4×: API Error: 529 Overloaded. Branch kept, run goes on.\n"
                              "Other sessions worked, so it's this ticket's doing.")


if __name__ == "__main__":
    unittest.main()
