"""T-233: a late `windvane answer` dropped after phone.LATE_KEEP is kept in state.json for `windvane status` to name
once, so the user learns their answer didn't count without reading the log; the drop line says it was kept that long
(the answer may be older because the driver was stopped); and the failed push's line counts its answers right."""
import contextlib
import io
import os
import sys
import unittest
from pathlib import Path
from unittest import mock
TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS.parent))
sys.path.insert(0, str(TESTS))
from windvane import cli, driver, phone, questions  # noqa: E402
from windvane.util import load_json  # noqa: E402
import test_late_answer_age as age_tests  # noqa: E402
from test_phone_late import driver_args  # noqa: E402

DAY = 86400


class Dropped(age_tests.Kept):
    log, age_kept = age_tests.Age.log, age_tests.Age.age_kept

    def status(self):
        """What `windvane status` printed, in the sandbox project."""
        old = os.getcwd()
        os.chdir(self.s.root)
        self.addCleanup(os.chdir, old)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.main(["status"])
        return out.getvalue()

    def dropped(self):
        """A week-old kept answer to T-001 dropped by the next pass: the pilot."""
        pl, before = self.failed_late_answer()
        self.age_kept(pl, phone.LATE_KEEP + 60)
        pl.poll_questions()
        self.assertEqual(self.not_taken()[before:], [], "not told as new")
        return pl

    def test_status_names_a_dropped_answer_once(self):
        pl = self.dropped()
        out = [line for line in self.status().splitlines() if line.startswith("dropped answer:")]
        self.assertEqual(out, ["dropped answer: T-001: your answer from 7 days ago came after it was settled, so it "
                               "wasn't taken; T-001 is building A (New). Once its ticket merges, change it with "
                               "windvane answer T-001 <letter>"])
        self.assertNotIn("dropped answer:", self.status(), "shown once")
        pl.poll_questions()  # the driver forgets it once shown
        self.assertEqual(load_json(self.p.state / "state.json", {})["dropped"], [])
        self.assertNotIn("dropped answer:", self.status())
        self.assertEqual(load_json(self.p.state / driver.DROPPED_SHOWN, None), [], "nothing left to remember")

    def test_one_settled_with_nothing_built_says_only_that_it_came_late(self):
        pl = self.dropped()
        pl.dropped = [["T-001", "settled before your answer came", pl.dropped[0][2]]]
        pl.save()
        out = [line for line in self.status().splitlines() if line.startswith("dropped answer:")]
        self.assertEqual(out, ["dropped answer: T-001: your answer from 7 days ago came after it was settled, so it "
                               "wasn't taken. Once its ticket merges, change it with windvane answer T-001 <letter>"])

    def test_the_driver_keeps_it_until_status_shows_it(self):
        self.dropped()
        for _ in range(2):
            driver.Pilot(self.p, driver_args(answer_wait=0)).poll_questions()
        self.assertIn("dropped answer: T-001:", self.status(), "a restart keeps the record")

    def test_the_drop_line_says_it_was_kept_seven_days(self):
        self.dropped()
        said = ("T-001: your answer from 7 days ago wasn't taken (building A (New)), and it was kept 7 days without its "
                "push going out, so it's dropped; `windvane status` names it once. Once its ticket merges, change it "
                "with `windvane answer T-001 <letter>`.")
        self.assertEqual(self.log().count(said), 1, self.log()[-500:])
        self.assertNotIn("failed for 7 days", self.log())

    def test_a_kill_before_the_drop_line_loses_the_line_not_the_record(self):
        pl, _ = self.failed_late_answer()
        self.age_kept(pl, phone.LATE_KEEP + 60)
        real = pl.log

        def killed(msg):
            if "kept 7 days" in msg:
                raise SystemExit("killed")
            real(msg)
        with mock.patch.object(pl, "log", killed), self.assertRaises(SystemExit):
            pl.poll_questions()
        driver.Pilot(self.p, driver_args(answer_wait=0)).poll_questions()
        self.assertNotIn("kept 7 days", self.log(), "the line is lost: the record says it")
        self.assertIn("dropped answer: T-001:", self.status())

    def test_the_failed_push_line_counts_its_answers(self):
        pl, _ = self.failed_late_answer()
        self.assertIn("the push of 1 late answer failed; trying again", self.log())
        pl.late_failed = False
        pl.late = [["T-001: building A", "alone", pl.late[0][2]]] * 3
        with mock.patch.dict(os.environ, FAKE_EXIT="1"):
            pl.tell_late()
        self.assertIn("the push of 3 late answers failed; trying again", self.log())


if __name__ == "__main__":
    unittest.main()
