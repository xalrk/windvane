"""T-210: a late `windvane answer` (given after the run settled its question) is kept in the driver's state until its
"Answer not taken" push goes out, so a failed push (ntfy down) is tried again on a later pass, after a restart too;
and what was built for a question is saved with the questions, so a restarted driver's push still names it."""
import os
import sys
import time
import unittest
from pathlib import Path
from unittest import mock
TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS.parent))
sys.path.insert(0, str(TESTS))
from windvane import driver, phone, questions  # noqa: E402
from windvane.util import load_json  # noqa: E402
from test_late_fold import Folded  # noqa: E402
from test_phone_late import driver_args  # noqa: E402


class Kept(Folded):
    def kept(self):
        return load_json(self.p.state / "state.json", {}).get("late", [])

    def failed_late_answer(self):
        """T-001 settled on its timeout, then a desk answer to it whose push fails: (the pilot, pushes before)."""
        pl, _ = self.timed_out_pilot()
        before = len(self.not_taken())
        questions.give_answer(self.p, "T-001", "B")
        with mock.patch.dict(os.environ, FAKE_EXIT="1"):
            pl.poll_questions()
        self.assertEqual(len(self.not_taken()), before + 1, "the push was tried")
        self.assertEqual(questions.desk_answers(self.p), {}, "the answer left answers.json")
        self.assertEqual(len(self.kept()), 1, "but is kept in state.json")
        return pl, before + 1

    def test_a_failed_push_is_sent_on_the_next_pass_and_only_once(self):
        pl, before = self.failed_late_answer()
        pl.poll_questions()
        told = self.not_taken()[before:]
        self.assertEqual(len(told), 1, told)
        self.assertTrue(told[0][told[0].index("-t") + 1].endswith("Answer not taken"), told)
        self.assertIn("T-001: your answer came after it was settled", told[0][-1])
        self.assertEqual(self.kept(), [])
        pl.poll_questions()
        self.assertEqual(self.not_taken()[before + 1:], [], "told once")

    def test_a_driver_restarted_between_the_passes_still_sends_it(self):
        self.failed_late_answer()
        before = len(self.not_taken())
        pl = driver.Pilot(self.p, driver_args(answer_wait=0))
        pl.poll_questions()
        told = self.not_taken()[before:]
        self.assertEqual(len(told), 1, told)
        self.assertIn("T-001: your answer came after it was settled", told[0][-1])
        pl.poll_questions()
        self.assertEqual(len(self.not_taken()), before + 1, "told once")

    def test_a_kept_answer_goes_in_one_push_with_the_next_passes_late_answers(self):
        pl, before = self.failed_late_answer()
        with mock.patch.dict(os.environ, FAKE_EXIT="1"):  # still down: T-001's retry fails again
            self.settled(pl, "T-002")
        before = len(self.not_taken())
        questions.give_answer(self.p, "T-002", "B")
        pl.poll_questions()
        told = self.not_taken()[before:]
        self.assertEqual(len(told), 1, told)
        self.assertEqual(told[0][-1].split("\n")[1:], ["T-001: building A (New)", "T-002: building A (New)"])

    def test_a_failed_push_is_logged_once(self):
        pl, _ = self.failed_late_answer()
        with mock.patch.dict(os.environ, FAKE_EXIT="1"):
            pl.poll_questions()
        log = (self.p.state / "autopilot.log").read_text() if (self.p.state / "autopilot.log").exists() else ""
        self.assertEqual(log.count("late answer(s) failed"), 1, log[-500:])
        self.assertEqual(len(self.kept()), 1)

    def test_with_no_phone_at_all_nothing_is_kept(self):
        with mock.patch.object(phone.shutil, "which", lambda name: None):
            self.assertTrue(phone.late_pass([("T-001: building A", "T-001: not taken")]))
        with mock.patch.dict(os.environ, FAKE_EXIT="1"):
            self.assertFalse(phone.late_pass([("T-001: building A", "T-001: not taken")]))
            self.assertTrue(phone.late_pass([]), "no desk answers: nothing to keep")


class SettledAs(Folded):
    def test_after_a_restart_the_line_names_what_is_built(self):
        pl, _ = self.timed_out_pilot(rec="B")
        self.settled(pl, "T-002")
        before = len(self.not_taken())
        questions.give_answer(self.p, "T-001", "A")
        questions.give_answer(self.p, "T-002", "B")
        driver.Pilot(self.p, driver_args(answer_wait=0)).poll_questions()  # a crash restart or self-update
        told = self.not_taken()[before:]
        self.assertEqual(len(told), 1, told)
        self.assertEqual(told[0][-1].split("\n")[1:], ["T-001: building B (Old)", "T-002: building A (New)"])

    def test_a_day_old_record_of_a_gone_question_is_pruned(self):
        pl, _ = self.timed_out_pilot()
        pl.settled_as["T-009"] = ["A (New)", time.time() - phone.LATE_FOR - 1]
        pl.questions["T-008"] = {"ticket": "T-008", "q": "Still open?", "opts": {"A": "x", "B": "y"}, "rec": "A",
                                 "must_wait": True, "asked": time.time(), "result": "settled"}
        pl.settled_as["T-008"] = ["A (x)", time.time() - phone.LATE_FOR - 1]
        pl.save()
        kept = load_json(self.p.state / "state.json", {})["settled_as"]
        self.assertEqual(sorted(kept), ["T-001", "T-008"], "a day old and its question gone: no late answer comes")


if __name__ == "__main__":
    unittest.main()
