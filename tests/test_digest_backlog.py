"""T-100: the Review digest names every provisional answer no push has shown in one push. It spelled out eight and
left the rest "with the next one", so a backlog of 20 took three quiet-time ends to reach the user."""
import os
import sys
import unittest
from contextlib import ExitStack
from datetime import timedelta
from pathlib import Path
from unittest import mock
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from windvane import driver, phone  # noqa: E402
from windvane.config import Project  # noqa: E402
from windvane.queue import Queue  # noqa: E402
from windvane.util import now  # noqa: E402
from windvane.worktree import ensure_worktree  # noqa: E402
from helpers import Sandbox  # noqa: E402
from test_pipeline import args  # noqa: E402


def answers(n, long=""):
    return "".join(f"### T-{100 + i}: Question {i}?{long}\n- **A**: yes\n- **B**: no\nRecommendation: A, because.\n"
                   "Built: A; to change it: x.txt\n" for i in range(n))


class Backlog(unittest.TestCase):
    def setUp(self):
        self.s = Sandbox()
        self.addCleanup(self.s.close)
        self.s.ticket("T-001")
        self.s.publish()
        self.p = Project(self.s.root)
        self.sent = []
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(mock.patch.object(driver, "push", lambda title, msg="", *a, **k:
                                              self.sent.append((title, msg))))
        stack.enter_context(mock.patch.object(driver, "quiet_now", lambda p: (None, None)))
        self.addCleanup(os.chdir, os.getcwd())

    def morning(self, n, long=""):
        self.s.write(".windvane/QUESTIONS.md", f"# Q\n\n## Open\n\n## Provisional\n{answers(n, long)}\n## Answered\n")
        self.s.publish("questions")
        ensure_worktree(self.p, self.p.work_dir, setup=False)
        pl = driver.Pilot(self.p, args())
        pl.deadline = now() + timedelta(hours=2)
        pl.queue = Queue(self.p)
        pl.was_quiet = True
        pl.quiet_check()
        return pl

    def test_twenty_come_in_one_push_and_the_next_morning_is_quiet(self):
        pl = self.morning(20)
        [(title, msg)] = self.sent
        self.assertEqual(title, "Review: 20 provisional answers")
        self.assertEqual(sum(f"Question {i}? Built A" in msg for i in range(20)), 8, msg)
        rest = ", ".join(f"T-{100 + i}" for i in range(8, 20))
        self.assertTrue(msg.endswith(f"\nAlso: {rest}; windvane status lists them."), msg)
        pl.was_quiet = True
        pl.quiet_check()
        self.assertEqual(len(self.sent), 1, self.sent)

    def test_long_questions_and_a_long_backlog_stay_under_the_phones_bound(self):
        self.morning(300, " Why" * 30)
        [(_, msg)] = self.sent
        self.assertLessEqual(len(msg), phone.MOST)
        self.assertRegex(msg.splitlines()[-1], r"^Also: T-108, .* and \d+ more; windvane status lists them\.$")

    def test_three_have_no_also_line(self):
        self.morning(3)
        [(_, msg)] = self.sent
        self.assertNotIn("Also", msg)
        self.assertEqual(msg.count("\n"), 3, msg)


class AlsoLine(unittest.TestCase):
    def test_a_ticket_with_two_answers_is_named_once(self):
        self.assertEqual(driver.also(["T-1", "T-2", "T-1"]), "\nAlso: T-1, T-2; windvane status lists them.")

    def test_a_backlog_too_long_for_the_phone_ends_with_how_many_more(self):
        line = driver.also([f"T-{i}" for i in range(1000)])
        self.assertLessEqual(len(line), phone.MOST)
        shown = line.split(": ", 1)[1].split(" and ")[0].split(", ")
        self.assertTrue(line.endswith(f" and {1000 - len(shown)} more; windvane status lists them."), line)
        self.assertEqual(shown[:2], ["T-0", "T-1"])


if __name__ == "__main__":
    unittest.main()
