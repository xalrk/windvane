"""T-196: the driver reads answers.json at the start of its pass, settles questions (an answer, or a timeout going with
the recommendation), then drops the settled ones' answers. Dropping by key deleted, unread, an answer the user gave in
between to a question just settled by its timeout."""
import json
import sys
import time
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from windvane import driver, phone, questions  # noqa: E402
from windvane.config import Project  # noqa: E402
from windvane.queue import Queue  # noqa: E402
from windvane.util import now  # noqa: E402
from helpers import Sandbox  # noqa: E402
from test_pipeline import args  # noqa: E402
from test_state_edits import waiting  # noqa: E402


class Settle(unittest.TestCase):
    def setUp(self):
        self.s = Sandbox()
        self.addCleanup(self.s.close)
        self.p = Project(self.s.root)
        self.p.state.mkdir(parents=True, exist_ok=True)
        self.pushes = []
        sent = lambda *a, **k: self.pushes.append(a[:2]) or True  # noqa: E731
        for target, name in ((driver, "push"), (phone, "push"), (phone, "ask"), (driver, "quiet_now")):
            patch = mock.patch.object(target, name, (lambda p: (None, "")) if name == "quiet_now" else sent)
            patch.start()
            self.addCleanup(patch.stop)
        self.pl = driver.Pilot(self.p, args(answer_wait=10))
        self.pl.deadline = now() + timedelta(hours=2)
        self.pl.queue = Queue(self.p)
        self.logged = []
        self.pl.log = lambda m, *a, **k: self.logged.append(m)
        self.pl.questions = {"T-001": waiting("T-001", 1, time.time() - 3600)}  # its 10 minutes ran out long ago

    def answers(self):
        return json.loads((self.p.state / "answers.json").read_text()) if (self.p.state / "answers.json").exists() else {}

    def test_an_answer_given_after_the_pass_read_survives_its_drop_and_the_next_pass_reads_it(self):
        read = questions.desk_answers

        def then_the_user_answers(p):
            got = read(p)
            questions.give_answer(p, "T-001", "B")  # `windvane answer T-001 B`, just after the pass read the file
            return got
        with mock.patch.object(driver, "desk_answers", then_the_user_answers):
            self.pl.poll_questions()
        self.assertIn("didn't answer", self.pl.feedback["T-001"])  # settled by its timeout: the recommendation
        self.assertEqual(self.answers()["T-001"]["option"], "B", "an answer given during the pass was dropped unread")

        seen = []
        with mock.patch.object(driver, "desk_answers", lambda p: seen.append(read(p)) or seen[-1]):
            self.pl.poll_questions()
        self.assertEqual(seen[0]["T-001"]["option"], "B")
        self.assertTrue(any(t == "Answer not taken" and "T-001" in m for t, m in self.pushes), self.pushes)
        self.assertNotIn("T-001", self.answers(), "told once, it would answer a later T-001 question")
        self.pushes.clear()
        self.pl.poll_questions()
        self.assertEqual(self.pushes, [])

    def test_an_unchanged_settled_answer_is_dropped(self):
        questions.give_answer(self.p, "T-001", "B")
        questions.give_answer(self.p, "T-002", "A")  # T-002's question isn't the driver's yet: its answer stays
        self.pl.questions["T-002"] = waiting("T-002", 1, None)
        self.pl.poll_questions()
        self.assertIn("option B", self.pl.feedback["T-001"])
        self.assertEqual(list(self.answers()), ["T-002"])
        self.assertFalse(any(t == "Answer not taken" for t, _ in self.pushes))

    def test_a_newer_answer_to_a_question_the_pass_settled_by_answer_stays(self):
        """The same holds when the pass settled on the answer it read: a second, different one given meanwhile is
        kept for the next pass, which says it wasn't taken."""
        questions.give_answer(self.p, "T-001", "A")
        read = questions.desk_answers

        def then_the_user_changes_it(p):
            got = read(p)
            questions.give_answer(p, "T-001", "B", "on second thought")
            return got
        with mock.patch.object(driver, "desk_answers", then_the_user_changes_it):
            self.pl.poll_questions()
        self.assertIn("option A", self.pl.feedback["T-001"])
        self.assertEqual(self.answers()["T-001"]["text"], "on second thought")


if __name__ == "__main__":
    unittest.main()
