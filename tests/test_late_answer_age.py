"""T-222: a late `windvane answer` kept while its push fails says how old it is once it's over an hour old, and one
kept past phone.LATE_KEEP is dropped with a log line instead of told as new; a question settled by a typed answer is
named by the start of its words; and the failed push is logged once per failure spell, across restarts too."""
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
import test_late_answers_kept as kept_tests  # noqa: E402
from test_phone_late import driver_args  # noqa: E402

DAY = 86400


class Kept(kept_tests.Folded):
    """Kept's helpers without its tests, which run in their own file."""
    kept, failed_late_answer = kept_tests.Kept.kept, kept_tests.Kept.failed_late_answer


class Age(Kept):
    def log(self):
        f = self.p.state / "autopilot.log"
        return f.read_text() if f.exists() else ""

    def age_kept(self, pl, seconds):
        """The kept late answers as given `seconds` ago, as after that long of failed pushes."""
        for e in pl.late:
            e[2] -= seconds
        pl.save()

    def test_an_answer_kept_two_days_says_its_age(self):
        pl, before = self.failed_late_answer()
        self.age_kept(pl, 2 * DAY + 60)
        pl.poll_questions()
        told = self.not_taken()[before:]
        self.assertEqual(len(told), 1, told)
        self.assertIn("T-001: your answer from 2 days ago came after it was settled", told[0][-1])
        self.assertEqual(self.kept(), [])

    def test_in_a_push_of_several_each_old_line_says_its_age(self):
        pl, _ = self.failed_late_answer()
        self.age_kept(pl, 5 * 3600)
        with mock.patch.dict(os.environ, FAKE_EXIT="1"):
            self.settled(pl, "T-002")
        before = len(self.not_taken())
        questions.give_answer(self.p, "T-002", "B")
        pl.poll_questions()
        told = self.not_taken()[before:]
        self.assertEqual(told[0][-1].split("\n")[1:],
                         ["T-001: building A (New); you answered 5 hours ago", "T-002: building A (New)"])

    def test_one_past_the_cap_is_dropped_and_logged(self):
        pl, before = self.failed_late_answer()
        self.age_kept(pl, phone.LATE_KEEP + 60)
        pl.poll_questions()
        self.assertEqual(self.not_taken()[before:], [], "a week-old answer isn't told as new")
        self.assertEqual(self.kept(), [])
        said = ("T-001: your answer from 7 days ago wasn't taken (building A (New)), and it was kept 7 days without its "
                "push going out, so it's dropped; `windvane status` names it once. Once its ticket merges, change it "
                "with `windvane answer T-001 <letter>`.")
        self.assertEqual(self.log().count(said), 1, self.log()[-500:])
        pl.poll_questions()
        self.assertEqual(self.log().count("so it's dropped"), 1)

    def test_a_restart_while_the_push_keeps_failing_logs_it_once(self):
        self.failed_late_answer()
        with mock.patch.dict(os.environ, FAKE_EXIT="1"):
            for _ in range(2):
                driver.Pilot(self.p, driver_args(answer_wait=0)).poll_questions()
        self.assertEqual(self.log().count("late answer failed"), 1, self.log()[-500:])
        pl = driver.Pilot(self.p, driver_args(answer_wait=0))
        pl.poll_questions()  # it went out: the spell is over
        self.assertEqual(self.kept(), [])
        with mock.patch.dict(os.environ, FAKE_EXIT="1"):
            self.settled(pl, "T-002")
            questions.give_answer(self.p, "T-002", "B")
            driver.Pilot(self.p, driver_args(answer_wait=0)).poll_questions()
        self.assertEqual(self.log().count("late answer failed"), 2, "a new spell is logged again")

    def test_an_answer_kept_from_before_times_were_kept_is_told_as_new(self):
        pl, before = self.failed_late_answer()
        pl.late = [e[:2] for e in pl.late]
        pl.save()
        driver.Pilot(self.p, driver_args(answer_wait=0)).poll_questions()
        told = self.not_taken()[before:]
        self.assertIn("T-001: your answer came after it was settled", told[0][-1])


class Typed(Kept):
    def test_a_typed_answers_line_quotes_its_start(self):
        pl, _ = self.timed_out_pilot()
        pl.questions["T-002"] = {"ticket": "T-002", "q": "Which menu?", "opts": {"A": "New", "B": "Old"},
                                 "rec": "A", "must_wait": False, "asked": time.time()}
        words = "use the old menu,  the new one\nhides the export button from most of the people who use it daily"
        questions.give_answer(self.p, "T-002", None, words)
        pl.poll_questions()
        before = len(self.not_taken())
        questions.give_answer(self.p, "T-001", "B")
        questions.give_answer(self.p, "T-002", "B")
        pl.poll_questions()
        lines = self.not_taken()[before][-1].split("\n")[1:]
        self.assertNotIn("typed answer", lines[1])
        self.assertTrue(lines[1].startswith('T-002: building your answer "use the old menu, the new one hides the'),
                        lines[1])
        self.assertTrue(lines[1].endswith('…"'), lines[1])
        quoted = lines[1].split('"')[1]
        self.assertLessEqual(len(quoted.encode()), 60)
        self.assertIn(quoted.removesuffix("…"), " ".join(words.split()), "cut at a word")

    def test_ages_read_by_the_hour_then_the_day(self):
        self.assertEqual([phone.ago(s) for s in (59 * 60, 3600, 7200, DAY - 1, DAY, 3 * DAY + 5)],
                         ["", "1 hour ago", "2 hours ago", "23 hours ago", "1 day ago", "3 days ago"])


if __name__ == "__main__":
    unittest.main()
