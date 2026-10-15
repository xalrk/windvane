"""T-166: a pass's SEND_WAIT goes to the open questions first. Late answers to timed-out questions were looked at
first, so a stalled network spent the pass on them and a real answer waited a pass more; the reply to a late answer
ran on a SEND_WAIT of its own, past the pass's; and that reply said "building A" without saying what A was."""
import json
import os
import sys
import time
import unittest
from pathlib import Path
from unittest import mock
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from windvane import driver, phone  # noqa: E402
from test_phone_late import Late, driver_args  # noqa: E402

# `--get <qid>` logs the look, waits $FAKE_ANSWERS/<qid>.slow seconds (a stalled network), then prints the answer.
STALLS = """#!/usr/bin/env python3
import os, sys, time
d = os.environ["FAKE_ANSWERS"]
if sys.argv[1] == "--get":
    with open(os.path.join(d, "looks"), "a") as f:
        f.write(sys.argv[2] + "\\n")
    slow = os.path.join(d, sys.argv[2] + ".slow")
    if os.path.exists(slow):
        time.sleep(float(open(slow).read()))
    f = os.path.join(d, sys.argv[2])
    print(open(f).read() if os.path.exists(f) else "WAITING")
"""

# Logs a push that went out; with $FAKE_STALL set it hangs first, as a `curl` on a stalled network does.
NOTIFY = """#!/usr/bin/env python3
import json, os, sys, time
if os.environ.get("FAKE_STALL"):
    time.sleep(30)
with open(os.environ["FAKE_SENT"], "a") as f:
    f.write(json.dumps({"tool": os.path.basename(sys.argv[0]), "args": sys.argv[1:]}) + "\\n")
"""

WAIT = 1.5  # the tests' SEND_WAIT
MARGIN = 0.6  # a pass's own work besides its looks and pushes


class Pass(Late):
    def setUp(self):
        super().setUp()
        self.tool("notify-ask", STALLS)
        self.tool("notify", NOTIFY)
        for name, value in (("SEND_WAIT", WAIT), ("_late_due", False)):
            patch = mock.patch.object(phone, name, value)
            patch.start()
            self.addCleanup(patch.stop)
        self.late = self.p.qid("T-009")
        phone.timed_out(self.late, "T-009", "A", "Keep the old format")
        self.pl = driver.Pilot(self.p, driver_args(answer_wait=60))
        self.pl.questions["T-001"] = {"q": "Which cache?", "opts": {"A": "New", "B": "Old"}, "rec": "A",
                                      "must_wait": False, "asked": time.time(), "ticket": "T-001"}
        self.open = self.p.qid("T-001")

    def stall(self, qid, seconds):
        (self.answers / f"{qid}.slow").write_text(str(seconds))

    def poll(self):
        """One pass of the driver's, and how long it took."""
        t = time.time()
        self.pl.poll_questions()
        return time.time() - t

    def late_list(self):
        return json.loads((phone.state_dir() / "late.json").read_text())

    def test_open_questions_are_looked_at_before_late_answers(self):
        self.poll()
        self.assertEqual(self.looks(), [self.open, self.late])

    def test_a_stalled_late_look_leaves_the_open_question_its_answer(self):
        self.stall(self.late, 30)
        (self.answers / self.open).write_text("ANSWER B\tOld")
        took = self.poll()
        self.assertIn("option B (Old)", self.pl.feedback["T-001"])
        self.assertLess(took, WAIT + MARGIN)

    def test_a_spent_budget_leaves_late_answers_to_the_next_pass(self):
        self.stall(self.open, 30)
        (self.answers / self.late).write_text("ANSWER B\tNew format")
        self.assertLess(self.poll(), WAIT + MARGIN)
        self.assertEqual(self.looks(), [self.open], "a late look started on a spent budget")
        (self.answers / f"{self.open}.slow").unlink()
        self.poll()
        self.assertEqual(self.looks(), [self.open, self.open, self.late])
        self.assertEqual(len(self.pushed("already building")), 1)

    def test_a_pending_reply_keeps_the_pass_in_its_budget_and_goes_out_later(self):
        self.stall(self.open, 1)  # most of the budget, then the late look and its stalled reply share the rest
        (self.answers / self.late).write_text("ANSWER B\tNew format")
        with mock.patch.dict(os.environ, FAKE_STALL="1"):
            took = self.poll()
        self.assertLess(took, WAIT + MARGIN, "the reply ran past the pass's budget")
        self.assertEqual(self.pushed("already building"), [])
        self.assertIn("already building A (Keep the old format)", self.late_list()[self.late]["told"])
        self.poll()
        told = self.pushed("already building")
        self.assertEqual(len(told), 1)
        self.assertEqual(self.looks().count(self.late), 1, "the kept answer was looked at again")
        self.assertEqual(self.late_list(), {})

    def test_the_building_push_names_the_option(self):
        (self.answers / self.late).write_text("ANSWER B\tNew format")
        self.poll()
        self.assertEqual(self.pushed("already building")[0]["args"][-1],
                         f"{self.late}: no answer came in time, so it's already building A (Keep the old format); "
                         "change it with `windvane answer T-009 <letter>`.")

    def test_a_long_option_is_cut_by_the_push_rule(self):
        phone.timed_out(self.late, "T-009", "A", "Keep the old format. " + "word " * 600)
        (self.answers / self.late).write_text("ANSWER B\tNew format")
        self.poll()
        msg = self.pushed("already building")[0]["args"][-1]
        self.assertIn("already building A (Keep the old format. word", msg)
        self.assertIn(f"word{phone.REST}); change it with `windvane answer T-009 <letter>`.", msg)
        self.assertLess(len(msg.encode()), phone.MOST + 200)

    def test_an_entry_from_an_older_version_is_still_told(self):
        f = phone.state_dir() / "late.json"
        f.write_text(json.dumps({self.late: ["T-009", "A", time.time()]}))
        (self.answers / self.late).write_text("ANSWER B\tNew format")
        self.poll()
        self.assertIn(f"{self.late}: no answer came in time, so it's already building A; change it",
                      self.pushed("already building")[0]["args"][-1])

    def test_a_wait_outside_the_driver_still_looks_at_late_answers(self):
        """A usage pause naps on pump alone: its passes look at late answers themselves."""
        (self.answers / self.late).write_text("ANSWER B\tNew format")
        phone.pump()
        self.assertEqual(self.looks(), [self.late])
        self.assertEqual(len(self.pushed("already building")), 1)


if __name__ == "__main__":
    unittest.main()
