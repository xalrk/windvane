"""T-207: the answers that come too late in one pass (`windvane answer`s and phone answers to questions already settled)
reach the phone as one push, not one each; and a process killed in the middle of writing windvane's state loses
nothing: a writer holding held.json's lock, or the driver between reading the answers and dropping them."""
import json
import os
import signal
import subprocess
import sys
import threading
import time
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock
TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS.parent))
sys.path.insert(0, str(TESTS))
from windvane import driver, held, phone, questions  # noqa: E402
from windvane.config import Project  # noqa: E402
from windvane.queue import Queue  # noqa: E402
from windvane.util import load_json, now  # noqa: E402
from helpers import Sandbox  # noqa: E402
from test_phone_late import Late, driver_args  # noqa: E402
from test_state_edits import waiting  # noqa: E402


class OnePush(Late):
    def not_taken(self):
        return [s["args"] for s in self.sent() if s["tool"] == "notify" and "not taken" in " ".join(s["args"])]

    def settled(self, pl, *tids):
        """Questions `tids` asked and settled on their timeout by one pass, as T-001 is by timed_out_pilot."""
        for tid in tids:
            pl.questions[tid] = {"ticket": tid, "q": f"{tid}'s question?", "opts": {"A": "New", "B": "Old"},
                                 "rec": "A", "must_wait": False, "asked": time.time() - 60}
        pl.poll_questions()
        for tid in tids:
            self.assertIn("build your recommendation (A)", pl.feedback[tid])

    def test_three_late_answers_in_one_pass_make_one_push_naming_each(self):
        pl, qid = self.timed_out_pilot()
        self.settled(pl, "T-002", "T-003")
        before = len(self.not_taken())
        (self.answers / qid).write_text("ANSWER B\tOld")  # on the phone, to T-001
        questions.give_answer(self.p, "T-002", "B")  # and at the desk, to the other two
        questions.give_answer(self.p, "T-003", None, "use the old one")
        pl.poll_questions()
        told = self.not_taken()[before:]
        self.assertEqual(len(told), 1, told)
        title, msg = told[0][told[0].index("-t") + 1], told[0][-1]
        self.assertIn("Answers not taken", title)
        self.assertEqual(msg.split("\n"), [
            "3 answers came after their questions were settled, so they weren't taken; once a ticket merges, change "
            "one with `windvane answer <id> <letter>`:",
            "T-002: building A (New)", "T-003: building A (New)", "T-001: building A (New)"])
        pl.poll_questions()
        self.assertEqual(self.not_taken()[before + 1:], [], "each is told once")
        self.assertEqual(questions.desk_answers(self.p), {})

    def test_after_a_restart_the_line_says_only_that_it_was_settled(self):
        pl, _ = self.timed_out_pilot()
        self.settled(pl, "T-002")
        before = len(self.not_taken())
        questions.give_answer(self.p, "T-001", "B")
        questions.give_answer(self.p, "T-002", "B")
        driver.Pilot(self.p, driver_args(answer_wait=0)).poll_questions()  # a new run: what was built is its workers'
        self.assertEqual(self.not_taken()[before:][0][-1].split("\n")[1:], [
            "T-001: settled before your answer came", "T-002: settled before your answer came"])

    def test_one_late_answer_keeps_its_own_words(self):
        pl, _ = self.timed_out_pilot()
        before = len(self.not_taken())
        questions.give_answer(self.p, "T-001", "B")
        pl.poll_questions()
        told = self.not_taken()[before:]
        self.assertEqual(len(told), 1, told)
        self.assertTrue(told[0][told[0].index("-t") + 1].endswith("Answer not taken"), told)
        self.assertEqual(told[0][-1], "T-001: your answer came after it was settled, so it wasn't taken; once its "
                                      "ticket merges, change it with `windvane answer T-001 <letter>`.")

    def test_a_list_past_ntfys_limit_is_cut_at_a_line_end(self):
        lines = [f"T-{i:03}: building A ({'x' * 300})" for i in range(20)]
        with mock.patch.object(phone, "whole_to_log", lambda line: None):
            self.assertTrue(phone.not_taken([(line, "alone") for line in lines]))
        msg = self.not_taken()[-1][-1]
        self.assertLessEqual(len(msg.encode()), phone.NTFY)
        self.assertTrue(msg.startswith("20 answers came after"), msg[:80])
        kept = msg.removesuffix(phone.REST).split("\n")[1:]
        self.assertEqual(kept, lines[:len(kept)], "cut inside a line")
        self.assertGreater(len(kept), 5)


# Holds held.json's lock inside an edit after one finished edit of its own, says so, and waits to be killed.
HOLDER = """import sys, time
sys.path.insert(0, sys.argv[1])
from pathlib import Path
from windvane import held
from windvane.config import Project
p = Project(sys.argv[2])
held.hold(p, "kept-by-the-killed", "push", "network down")
def stuck(h, reasons):
    h["never-saved"] = "push"
    Path(sys.argv[3]).write_text("in")
    time.sleep(60)
held.edit_held(p, stuck)
"""


class Killed(unittest.TestCase):
    def setUp(self):
        self.s = Sandbox()
        self.addCleanup(self.s.close)
        self.p = Project(self.s.root)
        self.p.state.mkdir(parents=True, exist_ok=True)

    def test_a_writer_killed_holding_the_held_lock_leaves_the_next_to_go_on(self):
        inside = self.s.tmp / "inside"
        child = subprocess.Popen([sys.executable, "-c", HOLDER, str(TESTS.parent), str(self.s.root), str(inside)])
        self.addCleanup(child.wait)
        self.addCleanup(lambda: child.poll() is None and child.kill())
        end = time.time() + 20
        while not inside.exists() and time.time() < end and child.poll() is None:
            time.sleep(0.02)
        self.assertTrue(inside.exists(), "the child never got into its edit")
        done = threading.Event()
        writer = threading.Thread(target=lambda: (held.hold(self.p, "next", "land"), done.set()), daemon=True)
        writer.start()
        self.assertFalse(done.wait(0.3), "the lock let a second writer in")
        os.kill(child.pid, signal.SIGKILL)
        self.assertTrue(done.wait(5), "the next writer waits on a dead process's lock")
        self.assertEqual(load_json(self.p.state / "held.json", {}), {"kept-by-the-killed": "push", "next": "land"})
        self.assertEqual(set(load_json(self.p.state / "held_why.json", {})), {"kept-by-the-killed", "next"})


# The driver's pass in a process of its own, SIGKILLed just before (`before`) or just after (`after`) it drops the
# answers it read.
PASS = """import os, signal, sys
sys.path[:0] = sys.argv[1:3]
from datetime import timedelta
from windvane import driver, phone
from windvane.config import Project
from windvane.queue import Queue
from windvane.util import now
from test_pipeline import args
p, when = Project(sys.argv[3]), sys.argv[4]
die = lambda: os.kill(os.getpid(), signal.SIGKILL)
drop = driver.drop_answers
driver.drop_answers = (lambda *a: die()) if when == "before" else (lambda *a: (drop(*a), die()))
driver.push = phone.push = lambda *a, **k: True
driver.quiet_now = lambda p: (None, "")
pl = driver.Pilot(p, args(answer_wait=10))
pl.deadline, pl.queue = now() + timedelta(hours=2), Queue(p)
pl.poll_questions()
"""


class Restarted(Killed):
    def setUp(self):
        super().setUp()
        self.pushes = []
        for target, name in ((driver, "push"), (phone, "push"), (driver, "quiet_now")):
            patch = mock.patch.object(target, name, (lambda p: (None, "")) if name == "quiet_now"
                                      else lambda *a, **k: self.pushes.append(a[:2]) or True)
            patch.start()
            self.addCleanup(patch.stop)

    def pilot(self):
        pl = driver.Pilot(self.p, driver_args(answer_wait=10))
        pl.deadline, pl.queue = now() + timedelta(hours=2), Queue(self.p)
        pl.log = lambda *a, **k: None
        return pl

    def killed_then_restarted(self, when):
        pl = self.pilot()
        pl.questions = {"T-001": waiting("T-001", 1, time.time() - 60), "T-002": waiting("T-002", 2, time.time() - 60),
                        "T-003": waiting("T-003", 3, time.time() - 3600)}  # its 10 minutes ran out
        pl.save()
        questions.give_answer(self.p, "T-001", "B")
        questions.give_answer(self.p, "T-002", None, "neither: keep both")
        run = subprocess.run([sys.executable, "-c", PASS, str(TESTS.parent), str(TESTS), str(self.s.root), when],
                             capture_output=True, text=True, timeout=60)
        self.assertEqual(run.returncode, -signal.SIGKILL, run.stderr[-2000:])
        pl = self.pilot()  # the restarted driver, from what the killed one left on disk
        pl.poll_questions()
        pl.poll_questions()
        self.assertEqual(pl.questions, {})
        self.assertEqual(pl.feedback["T-001"].count("option B (no)"), 1, pl.feedback)
        self.assertEqual(pl.feedback["T-002"].count("They added: neither: keep both"), 1, pl.feedback)
        self.assertNotIn("option", pl.feedback["T-002"])
        self.assertEqual(pl.feedback["T-003"].count("build your recommendation (A)"), 1, pl.feedback)
        self.assertEqual(questions.desk_answers(self.p), {})
        self.assertEqual([t for t, _ in self.pushes if "not taken" in t], [], "an answer taken was told it wasn't")

    def test_killed_after_reading_before_dropping(self):
        self.killed_then_restarted("before")

    def test_killed_after_dropping_before_saving(self):
        self.killed_then_restarted("after")


if __name__ == "__main__":
    unittest.main()
