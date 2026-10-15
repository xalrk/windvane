"""T-129: the user's own `notify-ask` never sends a question twice, never freezes a pass, and never drops a late answer.
A timed-out `notify-ask` left its `curl` alive to post the question after the next pass had sent it again; each open
question's look took its own 30 s; a phone answer after a question timed out to its recommendation went unread."""
import json
import os
import subprocess
import sys
import time
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest import mock
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from windvane import driver, phone  # noqa: E402
from test_phone_retry import Base  # noqa: E402

# Forks a child that posts (writes $FAKE_POSTED) after a moment, then hangs: a script whose `curl` outlives it.
FORKS = """#!/usr/bin/env python3
import os, sys, time
pid = os.fork()
if pid == 0:
    time.sleep(1.5)
    open(os.environ["FAKE_POSTED"], "w").write(" ".join(sys.argv[1:]))
    os._exit(0)
open(os.environ["FAKE_CHILD"], "w").write(str(pid))
time.sleep(30)
"""

# `--get <qid>` prints $FAKE_ANSWERS/<qid> (and counts the look); `--get` hangs when $FAKE_HANG is set.
ANSWERS = """#!/usr/bin/env python3
import os, sys, time
d = os.environ["FAKE_ANSWERS"]
if sys.argv[1] == "--get":
    with open(os.path.join(d, "looks"), "a") as f:
        f.write(sys.argv[2] + "\\n")
    if os.environ.get("FAKE_HANG"):
        time.sleep(30)
    f = os.path.join(d, sys.argv[2])
    print(open(f).read() if os.path.exists(f) else "WAITING")
"""


def gone(pid, within=3):
    """Whether process `pid` is dead (gone, or a zombie waiting for its reaper) within `within` seconds."""
    end = time.time() + within
    while time.time() < end:
        try:
            if Path(f"/proc/{pid}/stat").read_text().split(") ")[1][0] == "Z":
                return True
        except (OSError, IndexError):
            return True
        time.sleep(0.05)
    return False


class Late(Base):
    def setUp(self):
        super().setUp()
        self.answers = self.s.tmp / "answers"
        self.answers.mkdir()
        self.tool("notify")
        self.tool("notify-ask", ANSWERS)
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(mock.patch.dict(os.environ, FAKE_ANSWERS=str(self.answers)))
        stack.enter_context(mock.patch.object(phone, "PUMP_EVERY", 0))
        stack.enter_context(mock.patch.object(phone, "_last_pump", 0.0))
        stack.enter_context(mock.patch.object(phone, "_pass_end", 0.0))

    def looks(self):
        f = self.answers / "looks"
        return f.read_text().split() if f.exists() else []

    def pushed(self, text):
        return [s for s in self.sent() if s["tool"] == "notify" and text in s["args"][-1]]

    def timed_out_pilot(self, rec="A"):
        pl = driver.Pilot(self.p, driver_args(answer_wait=0))
        pl.questions["T-001"] = {"q": "Which cache?", "opts": {"A": "New", "B": "Old"}, "rec": rec,
                                 "must_wait": False, "asked": None}
        pl.send_question("T-001")
        pl.questions["T-001"]["asked"] -= 60
        pl.poll_questions()
        self.assertIn(f"build your recommendation ({rec})", pl.feedback["T-001"])
        (self.answers / "looks").unlink(missing_ok=True)  # the pass's own look before it timed out
        return pl, self.p.qid("T-001")


def driver_args(**kw):
    from test_pipeline import args
    return args(**kw)


class GroupKill(Late):
    def setUp(self):
        super().setUp()
        self.tool("notify-ask", FORKS)
        self.posted, self.child = self.s.tmp / "posted", self.s.tmp / "child"
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(mock.patch.dict(os.environ, FAKE_POSTED=str(self.posted), FAKE_CHILD=str(self.child)))
        stack.enter_context(mock.patch.object(phone, "SEND_WAIT", 0.5))

    def check_killed(self):
        pid = int(self.child.read_text())
        self.assertTrue(gone(pid), f"the child {pid} outlived its timed-out notify-ask")
        time.sleep(2)  # past when the child would have posted
        self.assertFalse(self.posted.exists(), "the question went out late, after the timeout")

    def test_a_timed_out_question_is_killed_with_its_child(self):
        t = time.time()
        self.assertFalse(phone.ask("t-001", "Which cache?", ["New", "Old"]))
        self.assertLess(time.time() - t, 3)
        self.check_killed()

    def test_a_timed_out_look_is_killed_with_its_child(self):
        self.assertEqual(phone.answer("t-001"), "")
        self.check_killed()

    def test_a_group_out_of_reach_still_kills_the_script(self):
        t = time.time()
        with mock.patch.object(phone.os, "killpg", side_effect=ProcessLookupError):
            self.assertIsNone(phone.tool(["notify-ask", "x"], 0.3))
        self.assertLess(time.time() - t, 5, "waited for the hung script itself")


# Forks a child that leaves the group (as a daemonized `curl` would) and holds stdout open, then hangs.
ESCAPES = """#!/usr/bin/env python3
import os, time
if os.fork() == 0:
    os.setsid()
    open(os.environ["FAKE_CHILD"], "w").write(str(os.getpid()))
    time.sleep(30)
    os._exit(0)
time.sleep(30)
"""


class EscapedChild(Late):
    def test_a_child_outside_the_group_holding_stdout_does_not_hold_the_pass(self):
        self.tool("notify-ask", ESCAPES)
        child = self.s.tmp / "child"
        with mock.patch.dict(os.environ, FAKE_CHILD=str(child)):
            t = time.time()
            self.assertIsNone(phone.tool(["notify-ask", "--get", "x"], 0.5))
            self.assertLess(time.time() - t, 3)
        for _ in range(50):
            if child.exists() and child.read_text():
                break
            time.sleep(0.05)
        os.kill(int(child.read_text()), 9)


class OnePassBudget(Late):
    def test_three_hung_looks_take_one_send_wait(self):
        pl = driver.Pilot(self.p, driver_args(answer_wait=60))
        for i in (1, 2, 3):
            pl.questions[f"T-001.q{i}"] = {"q": f"Q{i}?", "opts": {"A": "x", "B": "y"}, "rec": "A",
                                          "must_wait": False, "asked": time.time(), "ticket": "T-001"}
        with mock.patch.dict(os.environ, FAKE_HANG="1"), mock.patch.object(phone, "SEND_WAIT", 1.5):
            t = time.time()
            pl.poll_questions()
            took = time.time() - t
        self.assertGreater(took, 1.3)
        self.assertLess(took, 2.5, "each look took its own SEND_WAIT")
        self.assertEqual(len(self.looks()), 1, "the looks after the budget ran out started nothing")
        self.assertNotIn("result", pl.questions["T-001.q1"])

    def test_a_look_outside_a_pass_gets_its_own_send_wait(self):
        """A run's other waits (a planner's queue question) look on their own, never on a spent pass's budget."""
        phone.pump()
        with mock.patch.object(phone, "_pass_end", time.time() - 100):
            (self.answers / "q-one").write_text("ANSWER B\tOld")
            self.assertEqual(phone.answer("q-one"), "ANSWER B\tOld")


class LateAnswer(Late):
    def test_a_late_answer_is_told_once_what_is_being_built(self):
        pl, qid = self.timed_out_pilot()
        phone.pump()
        self.assertEqual(self.pushed("already building"), [])  # no answer yet: nothing to say
        (self.answers / qid).write_text("ANSWER B\tOld")
        phone.pump()
        phone.pump()
        told = self.pushed("already building")
        self.assertEqual(len(told), 1, told)
        self.assertEqual(told[0]["args"][-1], f"{qid}: no answer came in time, so it's already building A (New); change "
                                              "it with `windvane answer T-001 <letter>`.")
        self.assertEqual(self.looks().count(qid), 2, "looked at once a pass until answered, then dropped")

    def test_a_late_answer_matching_the_build_says_nothing(self):
        pl, qid = self.timed_out_pilot()
        (self.answers / qid).write_text("ANSWER A\tNew")
        phone.pump()
        phone.pump()
        self.assertEqual(self.pushed("already building"), [])
        self.assertEqual(self.looks().count(qid), 1)

    def test_a_failed_push_is_tried_again(self):
        pl, qid = self.timed_out_pilot()
        (self.answers / qid).write_text("ANSWER B\tOld")
        with mock.patch.dict(os.environ, FAKE_EXIT="1"):
            phone.pump()
        phone.pump()
        phone.pump()
        self.assertEqual(len(self.pushed("already building")), 2)  # the failed one, then the one that went out
        self.assertEqual(self.looks().count(qid), 1, "the answer is kept for the retry, not looked at again")

    def test_after_a_day_the_question_is_dropped(self):
        pl, qid = self.timed_out_pilot()
        f = phone.state_dir() / "late.json"
        late = json.loads(f.read_text())
        late[qid]["when"] -= phone.LATE_FOR + 1
        f.write_text(json.dumps(late))
        (self.answers / qid).write_text("ANSWER B\tOld")
        phone.pump()
        self.assertEqual(self.pushed("already building"), [])
        self.assertEqual(json.loads(f.read_text()), {})

    def test_another_process_holding_the_list_skips_this_pass(self):
        """Two runs on one machine: only one looks, so a late answer is told once."""
        pl, qid = self.timed_out_pilot()
        (self.answers / qid).write_text("ANSWER B\tOld")
        code = (f"import sys; sys.path.insert(0, {str(Path(phone.__file__).parents[1])!r})\n"
                "from windvane import phone\nwith phone.late_lock() as l: print(l is None)")
        with phone.late_lock() as held:
            self.assertIsNotNone(held)
            out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True).stdout.strip()
            self.assertEqual(out, "True")


if __name__ == "__main__":
    unittest.main()
