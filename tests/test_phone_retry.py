"""T-124: the phone never freezes the run or loses a message. A hung `notify-ask` blocked the driver's loop; a failed
Done push left its provisional answers to a quiet time's end that never came without quiet hours; the digest's
"Also" line named no ids once its eight lines passed 1,500 bytes; a phone answer to a question that had already
timed out to its recommendation was "recorded" and never read."""
import json
import os
import sys
import time
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest import mock
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from windvane import driver, phone  # noqa: E402
from windvane.config import Project  # noqa: E402
from windvane.worktree import ensure_worktree  # noqa: E402
from helpers import Sandbox  # noqa: E402
from test_pipeline import args  # noqa: E402
from test_push_sent import FAKE, answers  # noqa: E402

SLOW = """#!/usr/bin/env python3
import time
time.sleep(30)
"""


class Base(unittest.TestCase):
    def setUp(self):
        self.s = Sandbox()
        self.addCleanup(self.s.close)
        self.s.ticket("T-001")
        self.s.publish()
        self.p = Project(self.s.root)
        self.sent_file = self.s.tmp / "sent.jsonl"
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(mock.patch.dict(os.environ, FAKE_SENT=str(self.sent_file), FAKE_EXIT="0"))
        stack.enter_context(mock.patch.object(driver, "quiet_now", lambda p: (None, None)))  # no quiet hours
        self.addCleanup(phone.use, None)
        self.addCleanup(os.chdir, os.getcwd())

    def tool(self, name, text=FAKE):
        f = self.s.bin / name
        f.write_text(text)
        f.chmod(0o755)

    def sent(self):
        return [json.loads(x) for x in self.sent_file.read_text().splitlines()] if self.sent_file.exists() else []

    def pilot(self, questions):
        self.s.write(".windvane/QUESTIONS.md", f"# Q\n\n## Open\n\n## Provisional\n{answers(questions)}\n"
                                               "## Answered\n")
        self.s.publish("questions")
        ensure_worktree(self.p, self.p.work_dir, setup=False)
        return driver.Pilot(self.p, args())


class HungNotifyAsk(Base):
    def setUp(self):
        super().setUp()
        self.tool("notify-ask", SLOW)
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(mock.patch.object(phone, "SEND_WAIT", 1))

    def test_a_question_that_hangs_is_a_failed_send_and_the_pass_goes_on(self):
        pl = driver.Pilot(self.p, args())
        pl.questions["T-001"] = {"q": "Which cache?", "opts": {"A": "New", "B": "Old"}, "rec": "A",
                                 "must_wait": False, "asked": None}
        t = time.time()
        pl.send_question("T-001")
        self.assertLess(time.time() - t, 10)
        self.assertIsNone(pl.questions["T-001"]["asked"], "sent again on the next pass")

    def test_a_hung_look_for_an_answer_is_no_answer_yet(self):
        t = time.time()
        self.assertEqual(phone.answer("t-001"), "")
        self.assertLess(time.time() - t, 10)


class MissedAnswers(Base):
    def pass_(self, pl, minutes_later=0):
        pl.digest_tried -= minutes_later * 60
        pl.quiet_check()
        return len(self.sent())

    def test_a_failed_done_push_comes_again_as_one_digest_without_quiet_hours(self):
        self.tool("notify")
        os.environ["FAKE_EXIT"] = "1"
        pl = self.pilot([("T-001", "Q?")])
        pl.merged_ok("T-001", "", self.s.git("rev-parse", "HEAD"))
        self.assertEqual(len(self.sent()), 1)
        self.assertEqual(self.pass_(pl), 1, "not straight after the failed push")
        self.assertEqual(self.pass_(pl, 10), 2, "ten minutes on, a digest")
        self.assertEqual(self.pass_(pl, 5), 2, "a failed digest waits ten minutes too")
        os.environ["FAKE_EXIT"] = "0"
        self.assertEqual(self.pass_(pl, 5), 3)
        call = self.sent()[-1]["args"]
        self.assertIn("Sandbox: Review: 1 provisional answer", call)
        self.assertIn("\nT-001: Q? Built A.", call[-1])
        self.assertEqual(self.pass_(pl, 10), 3, "once the phone has it, never again")
        self.assertEqual(self.pass_(pl, 60), 3)

    def test_nothing_is_retried_inside_a_quiet_time(self):
        self.tool("notify")
        os.environ["FAKE_EXIT"] = "1"
        pl = self.pilot([("T-001", "Q?")])
        pl.merged_ok("T-001", "", self.s.git("rev-parse", "HEAD"))
        with mock.patch.object(driver, "quiet_now", lambda p: ("07:00", None)):
            self.assertEqual(self.pass_(pl, 30), 1)

    def test_twelve_long_answers_name_the_four_past_eight(self):
        self.tool("notify")
        pl = self.pilot([(f"T-{100 + i}", "Why " * 70 + f"{i}?") for i in range(12)])
        self.pass_(pl, 10)
        [call] = self.sent()
        msg = call["args"][-1]
        self.assertGreater(len(msg.encode()), phone.MOST, "the old budget went negative here")
        self.assertLessEqual(len(msg.encode()), phone.NTFY)
        self.assertTrue(msg.endswith("\nAlso: T-108, T-109, T-110, T-111; windvane status lists them."), msg[-200:])


class LateAnswer(Base):
    def setUp(self):
        super().setUp()
        phone.save_conf({"enabled": True, "server": "http://127.0.0.1:9", "topic": phone.new_topic(),
                         "reply_topic": phone.new_topic()})
        self.c = phone.conf()
        self.got = []
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(mock.patch.object(phone, "send", lambda c, title, msg, *a, **k:
                                              self.got.append((title, msg)) or True))
        stack.enter_context(mock.patch.object(phone, "pump", lambda: 0))
        self.qid = self.p.qid("T-001")
        pl = driver.Pilot(self.p, args(answer_wait=0))
        pl.questions["T-001"] = {"q": "Which cache?", "opts": {"A": "New", "B": "Old"}, "rec": "A",
                                 "must_wait": False, "asked": None}
        pl.send_question("T-001")
        pl.questions["T-001"]["asked"] -= 60
        pl.poll_questions()
        self.assertIn("build your recommendation (A)", pl.feedback["T-001"])

    def test_an_answer_after_the_timeout_is_told_what_is_being_built(self):
        phone.handle(self.c, f"answer {self.qid} B")
        title, msg = self.got[-1]
        self.assertEqual(title, "Answer not taken")
        self.assertIn("already building A; change it with `windvane answer T-001 <letter>`", msg)
        self.assertFalse((phone.state_dir() / "answers" / f"{self.qid}.json").exists())

    def test_a_bare_letter_answers_only_an_open_question(self):
        phone.handle(self.c, "B")
        self.assertEqual(self.got[-1], ("Answer not taken", "No open question to answer with B."))


if __name__ == "__main__":
    unittest.main()
