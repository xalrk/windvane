"""T-093: a push names its ticket by the whole title and carries its reason whole, in reading order, with no "…".
The phone showed `Done: The critic answers yes/no checks and the driv (T-054)` and bodies split by "…" around the
next step; each push here gets a 120-character title and a 300-character reason, and must keep both."""
import sys
import time
import unittest
from contextlib import ExitStack
from datetime import timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from windvane import driver, net, phone, planner  # noqa: E402
from windvane.periodic import ticket_list  # noqa: E402
from windvane.config import Project  # noqa: E402
from windvane.util import now  # noqa: E402
from windvane.worktree import ensure_worktree  # noqa: E402
from helpers import Sandbox  # noqa: E402
from test_set_aside import args  # noqa: E402

TITLE = ("Settings screens: graphics, audio, controls, and the split-screen layout each player picks before a match "
         "starts, online")
REASON = (("The worker's check failed because the fixture database was missing its seed rows, so every "
           "integration test timed out. ") * 3)[:300]
NAME = f"{TITLE} (T-001)"


class PushWhole(unittest.TestCase):
    def setUp(self):
        self.assertEqual((len(TITLE), len(REASON)), (120, 300))
        self.s = Sandbox()
        self.addCleanup(self.s.close)
        self.s.write(".windvane/tickets/T-001.md", f"# T-001: {TITLE}\nDepends on: nothing.\n")
        self.s.publish()
        self.p = Project(self.s.root)
        self.pushes, self.asked = [], []
        stack = ExitStack()
        self.addCleanup(stack.close)
        self.patch = lambda obj, name, value: stack.enter_context(mock.patch.object(obj, name, value))
        for module in (driver, planner):
            self.patch(module, "push", lambda title, msg="", *a, **k: self.pushes.append((title, msg)))
        self.patch(phone, "ask", lambda qid, question, opts: self.asked.append((question, opts)) or True)

    def pilot(self, worktree=False):
        if worktree:
            ensure_worktree(self.p, self.p.work_dir, setup=False)
        pl = driver.Pilot(self.p, args())
        pl.sleep, pl.deadline = (lambda s: None), now() + timedelta(hours=2)
        pl.queue = driver.Queue(self.p)
        return pl

    def question(self, pl, asked):
        pl.questions["T-001"] = {"q": REASON, "opts": {"A": REASON, "B": "b"}, "rec": "A", "must_wait": False,
                                 "branch": "", "asked": asked}

    def sent(self, prefix):
        [msg] = [m for t, m in self.pushes if t.startswith(prefix)] or [None]
        self.assertIsNotNone(msg, (prefix, self.pushes))
        self.assertNotIn("…", msg)
        return msg

    def assert_whole(self, prefix, reason=True):
        [title] = [t for t, _ in self.pushes if t.startswith(prefix)]
        self.assertEqual(title, f"{prefix}: {NAME}")
        msg = self.sent(prefix)
        if reason:
            self.assertIn(REASON, msg)
        return msg

    def test_done_and_provisional(self):
        picks = [{"ticket": "T-001", "question": REASON, "built": "A", "option": REASON}]
        self.patch(driver, "provisional", lambda text: picks)
        self.pilot(worktree=True).merged_ok("T-001", "", self.s.git("rev-parse", "HEAD"))
        self.assert_whole("Done", reason=False)
        self.assert_whole("Provisional")

    def test_failed_keeps_the_reason_then_the_next_step(self):
        self.patch(net, "online", lambda: True)
        self.pilot().fail("T-001", REASON)
        self.assertEqual(self.assert_whole("Failed"), f"{REASON}. Branch kept, run goes on.")

    def test_set_aside(self):
        self.pilot().set_aside("T-001", REASON)
        msg = self.assert_whole("Set aside")
        self.assertTrue(msg.startswith(f"Sessions died 4×: {REASON}. Branch kept, run goes on.\n"), msg)

    def test_a_5000_character_error_with_a_known_cause_stays_under_ntfys_limit_and_is_logged_whole(self):
        error = "API Error: 400 Prompt is too long. " + " ".join(f"Part {i} of the prompt." for i in range(250))
        self.assertGreater(len(error), 5000)
        self.pilot().set_aside("T-001", error)
        msg = self.sent("Set aside")
        self.assertLess(len(msg.encode()), 4096)
        self.assertIn(" (the rest is in the log).\nBranch kept, run goes on.", msg)
        self.assertIn(f"the whole text: {error}\n", (self.p.state / "autopilot.log").read_text())

    def test_no_answer(self):
        pl = self.pilot()
        self.question(pl, time.time() - 3600)
        pl.poll_questions()
        msg = self.assert_whole("No answer")
        self.assertTrue(msg.startswith(f"Going with A: {REASON}.\nTo change it later:"), msg)

    def test_question(self):
        pl = self.pilot()
        self.question(pl, None)
        pl.send_question("T-001")
        [(question, opts)] = self.asked
        self.assertTrue(question.startswith(f"{NAME}: {REASON}"), question)
        self.assertEqual(opts[0], REASON)
        self.assertNotIn("…", question)

    def test_planned(self):
        orig = planner.run_claude

        def run_claude(*a, **k):
            text, killed, error = orig(*a, **k)
            return text.replace("one ticket", REASON), killed, error
        self.patch(planner, "run_claude", run_claude)
        planner.plan(self.p, "make it great")
        self.assertIn(REASON, self.sent("planned"))


class Bound(unittest.TestCase):
    def test_a_5000_character_error_is_cut_at_a_sentence_end_and_logged_whole(self):
        error = " ".join(f"Step {i} failed." for i in range(500))
        lines = []
        out = phone.whole("", error, ". Branch kept.", log=lines.append)
        self.assertLessEqual(len(out), phone.MOST + 50)
        self.assertRegex(out, r"^Step 0 failed\. .* Step \d+ failed \(the rest is in the log\)\. Branch kept\.$")
        self.assertIn(error, lines[0])

    def test_one_long_line_without_sentences_is_cut_at_a_word_end(self):
        out = phone.whole("", "word " * 1000, log=[].append)
        self.assertTrue(out.endswith("word (the rest is in the log)"), out[-60:])

    def test_a_periodic_question_lists_whole_titles_and_counts_the_rest(self):
        self.assertEqual(ticket_list(["T-004", "T-005"], {"T-004": TITLE}), f"T-004 {TITLE}; T-005")
        many = [f"T-{i:03}" for i in range(100, 120)]
        out = ticket_list(many, dict.fromkeys(many, TITLE))
        self.assertRegex(out, rf"^(T-\d+ {TITLE}; )+and \d+ more$")
        self.assertLess(len(out), phone.MOST)

    def test_one_token_without_spaces_is_cut_at_the_bound(self):
        out = phone.whole("", "x" * 5000, ".", log=[].append)
        self.assertEqual(out, "x" * phone.MOST + " (the rest is in the log).")

    def test_text_that_fits_logs_nothing_and_keeps_one_full_stop(self):
        lines = []
        self.assertEqual(phone.whole("Why: ", "it  broke.\n", ".", log=lines.append), "Why: it broke.")
        self.assertEqual(lines, [])

    def test_text_with_no_tail_keeps_its_own_full_stop(self):
        self.assertEqual(phone.whole("", "It broke.", log=[].append), "It broke.")
        self.assertEqual(phone.whole("git said: ", "fatal: bad thing.", "", log=[].append), "git said: fatal: bad thing.")


if __name__ == "__main__":
    unittest.main()
