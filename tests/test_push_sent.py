"""T-112: a provisional answer counts as shown only once its push went out (a failed `notify` left the whole backlog
marked seen, so the user never heard of it), and every question and digest stays under ntfy's 4,096 bytes, past which
a push arrives as an attachment no button can answer."""
import io
import json
import os
import sys
import unittest
from contextlib import ExitStack, redirect_stderr
from datetime import timedelta
from pathlib import Path
from unittest import mock
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from windvane import driver, phone  # noqa: E402
from windvane.config import Project  # noqa: E402
from windvane.queue import Queue  # noqa: E402
from windvane.util import now  # noqa: E402
from windvane.worktree import ensure_worktree  # noqa: E402
from helpers import Sandbox  # noqa: E402
from test_pipeline import args  # noqa: E402

FAKE = """#!/usr/bin/env python3
import json, os, sys
with open(os.environ["FAKE_SENT"], "a") as f:
    f.write(json.dumps({"tool": os.path.basename(sys.argv[0]), "args": sys.argv[1:]}) + "\\n")
sys.exit(int(os.environ.get("FAKE_EXIT", "0")))
"""
LONG = "Ünïcödé, ünïcödé " * 35  # 595 characters, 945 bytes


def answers(questions):
    return "".join(f"### {tid}: {q}\n- **A**: yes\n- **B**: no\nRecommendation: A, because.\n"
                   "Built: A; to change it: x.txt\n" for tid, q in questions)


def asked_text(qid, question, options):
    """The message notify-ask builds from its arguments, as `phone.send` does."""
    opts = dict(zip("ABCDE", options))
    more = [k for k in opts if k > "B"]
    typed = f"\nFor {'/'.join(more)}, just send the letter (or: answer {qid} {more[0]})" if more else ""
    return question + "\n" + "\n".join(f"{k}: {v}" for k, v in opts.items()) + typed


class Base(unittest.TestCase):
    def setUp(self):
        self.s = Sandbox()
        self.addCleanup(self.s.close)
        self.s.ticket("T-001")
        self.s.publish()
        self.p = Project(self.s.root)
        self.sent_file = self.s.tmp / "sent.jsonl"
        env = mock.patch.dict(os.environ, FAKE_SENT=str(self.sent_file))  # gone again after each test
        env.start()
        self.addCleanup(env.stop)
        self.addCleanup(phone.use, None)
        self.addCleanup(os.chdir, os.getcwd())

    def fake(self, name, exit_code=0):
        f = self.s.bin / name
        f.write_text(FAKE)
        f.chmod(0o755)
        os.environ["FAKE_EXIT"] = str(exit_code)

    def sent(self):
        return [json.loads(l) for l in self.sent_file.read_text().splitlines()] if self.sent_file.exists() else []

    def fake_send(self):
        """No `notify`: the built-in push, with the phone on and `send` recording what would go to ntfy."""
        phone.save_conf({"enabled": True, "server": "http://127.0.0.1:9", "topic": phone.new_topic(),
                         "reply_topic": phone.new_topic()})
        got = []
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(mock.patch.object(phone, "send", lambda c, title, msg, *a, **k: got.append(msg) or True))
        return got


class Digest(Base):
    def morning(self, questions, pl=None):
        if pl is None:
            self.s.write(".windvane/QUESTIONS.md", f"# Q\n\n## Open\n\n## Provisional\n{answers(questions)}\n"
                                                   "## Answered\n")
            self.s.publish("questions")
            ensure_worktree(self.p, self.p.work_dir, setup=False)
            pl = driver.Pilot(self.p, args())
            pl.deadline = now() + timedelta(hours=2)
            pl.queue = Queue(self.p)
        pl.was_quiet = True
        with mock.patch.object(driver, "quiet_now", lambda p: (None, None)):
            pl.quiet_check()
        return pl

    def pushed(self):
        return json.loads((self.p.state / "shown.json").read_text())["pushed"]

    def test_a_failed_push_marks_nothing_and_the_next_digest_pushes_them_once(self):
        self.fake("notify", 1)
        pl = self.morning([(f"T-{100 + i}", f"Question {i}?") for i in range(3)])
        self.assertEqual(len(self.sent()), 1)
        self.assertEqual(self.pushed(), [])
        os.environ["FAKE_EXIT"] = "0"
        self.morning(None, pl)
        self.assertEqual(len(self.sent()), 2)
        msg = self.sent()[-1]["args"][-1]
        self.assertEqual(sum(f"T-{100 + i}: Question {i}? Built A." in msg for i in range(3)), 3, msg)
        self.assertEqual(len(self.pushed()), 3)
        self.morning(None, pl)
        self.assertEqual(len(self.sent()), 2, "an answer the phone showed is pushed once")

    def test_without_a_phone_nothing_counts_as_shown(self):
        self.morning([("T-100", "Question?")])
        self.assertEqual(self.pushed(), [])

    def backlog(self):
        return [(f"T-{100 + i}", f"{LONG}{i}?") for i in range(10)] + [(f"T-{200 + i}", "Short?") for i in range(200)]

    def test_a_long_non_ascii_backlog_is_one_push_under_ntfys_bytes_through_notify(self):
        self.fake("notify")
        self.morning(self.backlog())
        [call] = self.sent()
        self.assertIn("Sandbox: Review: 210 provisional answers", call["args"])
        msg = call["args"][-1]
        self.assertLess(len(msg.encode()), phone.NTFY)
        self.assertEqual(msg.count(" Built A."), 8, "each of the first eight is named, a long one cut")
        rest = [f"T-{108 + i}" for i in range(2)] + [f"T-{200 + i}" for i in range(200)]
        self.assertTrue(msg.endswith(f"\nAlso: {', '.join(rest)}; windvane status lists them."), msg[-200:])
        self.assertIn(f"whole text: {LONG}7?", (self.p.state / "autopilot.log").read_text())

    def test_a_long_non_ascii_backlog_is_one_push_under_ntfys_bytes_through_send(self):
        got = self.fake_send()
        self.morning(self.backlog())
        [msg] = got
        self.assertLess(len(msg.encode()), phone.NTFY)


class Done(Base):
    def merge(self, exit_code):
        self.fake("notify", exit_code)
        self.s.write(".windvane/QUESTIONS.md", f"# Q\n\n## Open\n\n## Provisional\n{answers([('T-001', 'Q?')])}\n"
                                               "## Answered\n")
        self.s.publish("questions")
        ensure_worktree(self.p, self.p.work_dir, setup=False)
        driver.Pilot(self.p, args()).merged_ok("T-001", "", self.s.git("rev-parse", "HEAD"))
        self.assertIn("\nQ? Built A without asking you", self.sent()[-1]["args"][-1])
        return json.loads((self.p.state / "shown.json").read_text())["pushed"]

    def test_a_failed_done_push_leaves_its_answers_to_the_digest(self):
        self.assertEqual(self.merge(1), [])

    def test_a_done_push_that_went_out_marks_them_shown(self):
        self.assertEqual(self.merge(0), ["T-001: Q?"])


class Ask(Base):
    def setUp(self):
        super().setUp()
        phone.use(self.p)
        self.option = "Ä" + "Rebuild the whole cache from scratch on every run, " * 60  # 3,061 characters

    def logged_whole(self):
        return self.option.strip() in (self.p.state / "autopilot.log").read_text()

    def test_a_long_option_stays_under_ntfys_bytes_through_notify_ask(self):
        self.fake("notify-ask")
        self.assertTrue(phone.ask("t-001-q", "Which cache? " + LONG * 4, [self.option, "Keep it", "Drop it"]))
        [call] = self.sent()
        qid, question, *options = call["args"]
        self.assertLess(len(asked_text(qid, question, options).encode()), phone.NTFY)
        self.assertEqual(options[1:], ["Keep it", "Drop it"])
        self.assertTrue(options[0].endswith(phone.REST), options[0])
        self.assertTrue(self.logged_whole())

    def test_a_failed_notify_ask_is_not_counted_as_asked(self):
        self.fake("notify-ask", 1)
        self.assertFalse(phone.ask("t-001-q", "Which cache?", ["New", "Old"]))
        pl = driver.Pilot(self.p, args())
        pl.questions["T-001"] = {"q": "Which cache?", "opts": {"A": "New", "B": "Old"}, "rec": "A",
                                 "must_wait": False, "asked": None}
        pl.send_question("T-001")
        self.assertIsNone(pl.questions["T-001"]["asked"], "the wait for an answer starts once it's out")
        self.assertFalse((self.p.state / "shown.json").exists())
        os.environ["FAKE_EXIT"] = "0"
        pl.send_question("T-001")
        self.assertIsNotNone(pl.questions["T-001"]["asked"])
        self.assertEqual(json.loads((self.p.state / "shown.json").read_text())["asked"], {"T-001": ["Which cache?"]})

    def test_a_long_option_stays_under_ntfys_bytes_through_send(self):
        got = self.fake_send()
        self.assertTrue(phone.ask("t-001-q", "Which cache?", [self.option, "Keep it"]))
        [msg] = got
        self.assertLess(len(msg.encode()), phone.NTFY)
        self.assertIn("\nB: Keep it", msg)
        self.assertTrue(self.logged_whole())


class Fit(unittest.TestCase):
    def fit(self, msg):
        err = io.StringIO()
        with redirect_stderr(err):
            out = phone.fit(msg)
        return out, err.getvalue()

    def test_one_long_line_is_cut_mid_line(self):
        out, err = self.fit("x" * 5000)
        self.assertIn("push cut mid-line", err)
        self.assertLessEqual(len(out.encode()), phone.NTFY)

    def test_a_blank_first_line_leaves_no_leading_space(self):
        out, err = self.fit("\n" + "x" * 5000)
        self.assertEqual(out, phone.REST.lstrip())
        self.assertIn("push cut at a line end", err)

    def test_lines_that_fit_are_kept_whole(self):
        out, err = self.fit("short line\n" + "y" * 5000)
        self.assertEqual(out, "short line" + phone.REST)
        self.assertIn("push cut at a line end", err)


class NoEnvLeft(unittest.TestCase):
    def test_no_fake_variable_outlives_its_test(self):
        for cls in (Digest, Done, Ask):
            unittest.TextTestRunner(stream=io.StringIO()).run(unittest.defaultTestLoader.loadTestsFromTestCase(cls))
            self.assertFalse({"FAKE_SENT", "FAKE_EXIT"} & set(os.environ), cls.__name__)


class AlsoBytes(unittest.TestCase):
    def test_non_ascii_ticket_ids_are_measured_in_bytes(self):
        ids = [f"T-{i}-ünïcödé" for i in range(100)]
        line = driver.also(ids, 500)
        self.assertLessEqual(len(line.encode()), 500)
        self.assertRegex(line, r" and \d+ more; windvane status lists them\.$")


if __name__ == "__main__":
    unittest.main()
