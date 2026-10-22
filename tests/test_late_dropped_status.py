"""T-233: a late `windvane answer` dropped after phone.LATE_KEEP is kept in state.json for `windvane status` to name
once, so the user learns their answer didn't count without reading the log; the drop line says it was kept that long
(the answer may be older because the driver was stopped); and the failed push's line counts its answers right.
T-238: the line says whether its ticket has merged, state.json keeps at most driver.DROPPED_MOST of them, and
`windvane status` racing a driver save names each exactly once."""
import contextlib
import io
import os
import random
import sys
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
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
                               "wasn't taken; T-001 is building A (New). Its ticket is still open: once it merges, "
                               "change it with windvane answer T-001 <letter>"])
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
                               "wasn't taken. Its ticket is still open: once it merges, change it with windvane answer "
                               "T-001 <letter>"])

    def test_the_driver_keeps_it_until_status_shows_it(self):
        self.dropped()
        for _ in range(2):
            driver.Pilot(self.p, driver_args(answer_wait=0)).poll_questions()
        self.assertIn("dropped answer: T-001:", self.status(), "a restart keeps the record")

    def test_the_drop_line_says_it_was_kept_seven_days(self):
        self.dropped()
        said = ("T-001: your answer from 7 days ago wasn't taken (building A (New)), and it was kept 7 days without its "
                "push going out, so it's dropped; `windvane status` names it once. Its ticket is still open: once it "
                "merges, change it with `windvane answer T-001 <letter>`.")
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

    def merge_t001(self):
        """T-001's branch merged on the upstream, as the driver merges one."""
        self.s.git("checkout", "-qb", "t-001-work")
        self.s.write("done.txt", "T-001\n")
        self.s.git("add", "-A")
        self.s.git("commit", "-qm", "T-001")
        self.s.git("checkout", "-q", "main")
        self.s.git("merge", "--no-ff", "-qm", "Merge T-001: do T-001 (branch t-001-work)", "t-001-work")
        self.s.git("push", "-q", "origin", "main")

    def test_one_whose_ticket_merged_says_so_and_to_change_it_now(self):
        self.dropped()
        self.merge_t001()
        out = [line for line in self.status().splitlines() if line.startswith("dropped answer:")]
        self.assertEqual(out, ["dropped answer: T-001: your answer from 7 days ago came after it was settled, so it "
                               "wasn't taken; T-001 built A (New). Its ticket has merged: change it now with windvane "
                               "answer T-001 <letter>"])

    def test_a_question_of_several_is_read_by_its_ticket(self):
        pl = self.dropped()
        self.merge_t001()
        pl.dropped = [["T-001.q2", "settled before your answer came", pl.dropped[0][2]]]
        pl.save()
        self.assertIn("T-001.q2: your answer from 7 days ago came after it was settled, so it wasn't taken. Its ticket "
                      "has merged: change it now with windvane answer T-001.q2 <letter>", self.status())

    def drop(self, pl, n, ago=phone.LATE_KEEP + 60, key="T-{:03}"):
        """`n` late answers past phone.LATE_KEEP dropped in one pass whose push goes out, the oldest first."""
        now = time.time()
        pl.late = [[f"{key.format(i)}: building A (New)", "alone", now - ago - (n - i)] for i in range(n)]
        with mock.patch.object(phone, "late_pass", lambda lines: True):
            pl.tell_late()

    def test_the_list_keeps_the_newest_and_logs_the_rest(self):
        pl = self.dropped()
        self.status()  # T-001 shown: it doesn't count to the cap
        self.drop(pl, driver.DROPPED_MOST + 3, key="T-2{:02}")
        kept = load_json(self.p.state / "state.json", {})["dropped"]
        self.assertEqual([e[0] for e in kept], [f"T-2{i:02}" for i in range(3, driver.DROPPED_MOST + 3)])
        for i in range(3):
            self.assertEqual(self.log().count(f"T-2{i:02}: "), 1, "one line for a cut answer")
            self.assertIn(self.cut_line(f"T-2{i:02}"), self.log())
        self.assertNotIn("T-203: your answer from 7 days ago wasn't taken (building A (New)), and `windvane", self.log())
        self.assertNotIn("T-001: your answer from 7 days ago wasn't taken (building A (New)), and `windvane", self.log(),
                         "a shown one isn't capped")
        self.drop(pl, 5, ago=phone.LATE_KEEP + 10, key="T-3{:02}")  # given after the first batch
        kept = load_json(self.p.state / "state.json", {})["dropped"]
        self.assertEqual(len(kept), driver.DROPPED_MOST, "bounded across passes")
        self.assertEqual(kept[-5:], [e for e in kept if e[0].startswith("T-3")], "the newest stay")
        self.assertIn(self.cut_line("T-207"), self.log(), "one dropped in an earlier pass is logged when cut")

    def cut_line(self, key, merged=False):
        return (f"{key}: your answer from 7 days ago wasn't taken (building A (New)), and `windvane status` won't name "
                f"it: it keeps only the newest {driver.DROPPED_MOST} dropped answers. "
                + (f"Its ticket has merged: change it now with `windvane answer {key} <letter>`." if merged else
                   f"Its ticket is still open: once it merges, change it with `windvane answer {key} <letter>`."))

    def test_a_cut_answer_whose_ticket_merged_says_to_change_it_now(self):
        pl = self.dropped()
        self.merge_t001()
        pl.dropped = [["T-001.q2", "building A (New)", time.time() - phone.LATE_KEEP - 999]]
        pl.save()
        self.drop(pl, driver.DROPPED_MOST, key="T-2{:02}")
        self.assertIn(self.cut_line("T-001.q2", merged=True), self.log())
        self.assertIn("T-200: your answer from 7 days ago wasn't taken (building A (New)), and it was kept 7 days "
                      "without its push going out, so it's dropped; `windvane status` names it once. Its ticket is "
                      "still open", self.log())

    def test_a_kill_at_the_save_logs_a_cut_answer_again_never_loses_it(self):
        pl = self.dropped()
        now = time.time() - phone.LATE_KEEP - 60
        pl.late = [[f"T-2{i:02}: building A (New)", "alone", now - 30 + i] for i in range(driver.DROPPED_MOST + 2)]
        pl.save()

        def killed():
            raise SystemExit("killed")
        with mock.patch.object(pl, "save", killed), self.assertRaises(SystemExit):
            pl.tell_late()
        self.assertIn(self.cut_line("T-200"), self.log(), "logged before the save")
        with mock.patch.object(phone, "late_pass", lambda lines: True):
            driver.Pilot(self.p, driver_args(answer_wait=0)).tell_late()
        for key in ("T-200", "T-201"):
            self.assertEqual(self.log().count(self.cut_line(key)), 2, "logged again after the restart")
        kept = load_json(self.p.state / "state.json", {})
        self.assertEqual(len(kept["dropped"]), driver.DROPPED_MOST)
        self.assertEqual(kept["late"], [])

    def test_status_racing_a_driver_save_names_each_once(self):
        """`windvane status` marks shown without the driver's lock while the driver drops more and saves: every
        dropped answer is named exactly once, and the driver forgets none before status has named it."""
        pl = self.dropped()
        pl.dropped = []
        pl.save()
        real_load, real_save = driver.load_json, driver.save_json

        def jitter():
            time.sleep(random.random() * 0.002)

        def load(*a):
            jitter()
            out = real_load(*a)
            jitter()
            return out

        def save(*a):
            jitter()
            real_save(*a)
            jitter()
        named, n, done = [], 12, threading.Event()

        def status():
            while not done.is_set():
                named.extend(driver.dropped_unseen(self.p))
            named.extend(driver.dropped_unseen(self.p))

        def drive():
            for i in range(n):
                self.drop(pl, 1, key=f"T-{100 + i}")
                pl.save()
            done.set()
        open_queue = mock.patch.object(driver, "Queue", lambda p: SimpleNamespace(is_done=lambda tid: False))
        with open_queue, mock.patch.object(driver, "load_json", load), mock.patch.object(driver, "save_json", save):
            threads = [threading.Thread(target=status), threading.Thread(target=drive)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(30)
            pl.save()
            named.extend(driver.dropped_unseen(self.p))
        keys = [line.split(":")[1].strip() for line in named]
        self.assertEqual(sorted(keys), [f"T-{100 + i}" for i in range(n)], "each named exactly once")
        pl.save()
        self.assertEqual(load_json(self.p.state / "state.json", {})["dropped"], [], "all forgotten once named")


if __name__ == "__main__":
    unittest.main()
