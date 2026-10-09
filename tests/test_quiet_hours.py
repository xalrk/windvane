"""Quiet hours of the user's own, for every project. A project without a `quiet.txt` asked the user things at night
that they couldn't answer; a question asked just before a quiet time waited all night for an answer; and the worker
only learned about a quiet time from one line of its prompt, never when one was about to start."""
import os
import sys
import time
import unittest
from contextlib import ExitStack
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from windvane import driver, phone, questions  # noqa: E402
from windvane.config import Project  # noqa: E402
from windvane.queue import Queue  # noqa: E402
from windvane.util import now  # noqa: E402
from windvane.worktree import ensure_worktree  # noqa: E402
from helpers import Sandbox  # noqa: E402
from test_pipeline import args  # noqa: E402

D = lambda s: datetime.strptime(s, "%Y-%m-%d %H:%M")  # noqa: E731
NIGHT = "daily 23:00-08:00\n"


def around_now(before=60, after=60):
    """A dated quiet window from `before` minutes ago to `after` from now (one that may cross midnight)."""
    s, e = datetime.now() - timedelta(minutes=before), datetime.now() + timedelta(minutes=after)
    return f"{s:%Y-%m-%d %H:%M}-{e:%H:%M}\n"


class Base(unittest.TestCase):
    def setUp(self):
        self.s = Sandbox()
        self.addCleanup(self.s.close)
        self.s.ticket("T-001")
        self.s.publish()
        self.p = Project(self.s.root)
        self.user_file = Path(os.environ["XDG_CONFIG_HOME"]) / "windvane/quiet.txt"
        self.user_file.parent.mkdir(parents=True, exist_ok=True)
        self.p.state.mkdir(parents=True, exist_ok=True)
        self.sent = []
        stack = ExitStack()
        self.addCleanup(stack.close)
        self.patch = lambda obj, name, value: stack.enter_context(mock.patch.object(obj, name, value))
        self.patch(driver, "push", lambda title, msg="", *a, **k: self.sent.append((title, msg)) or True)
        self.patch(phone, "ask", lambda qid, q, opts: self.sent.append(("Question", q)) or True)
        self.addCleanup(os.chdir, os.getcwd())


class Files(Base):
    def test_the_users_file_counts_for_every_project(self):
        self.user_file.write_text(NIGHT)
        self.assertEqual(questions.quiet_now(self.p, D("2026-10-09 02:00")), (D("2026-10-09 08:00"), None))
        self.assertIsNone(questions.quiet_now(self.p, D("2026-10-09 12:00"))[0])

    def test_both_files_count(self):
        self.user_file.write_text(NIGHT)
        (self.p.state / "quiet.txt").write_text("daily 13:00-14:00\n")
        self.assertEqual(questions.quiet_now(self.p, D("2026-10-09 13:30"))[0], D("2026-10-09 14:00"))
        self.assertEqual(questions.quiet_now(self.p, D("2026-10-09 23:30"))[0], D("2026-10-10 08:00"))

    def test_a_typo_in_one_file_keeps_the_others_quiet_times(self):
        self.user_file.write_text(NIGHT)
        (self.p.state / "quiet.txt").write_text("weekends\n")
        end, err = questions.quiet_now(self.p, D("2026-10-09 02:00"))
        self.assertEqual(end, D("2026-10-09 08:00"))
        self.assertIn("'weekends'", err)
        self.assertIn(str(self.p.state / "quiet.txt"), err)


class Told(Base):
    def test_mode_names_a_quiet_time_about_to_start(self):
        self.user_file.write_text(NIGHT)
        self.assertEqual(questions.mode(self.p, D("2026-10-09 22:10")),
                         "ask (a quiet time starts Fri 23:00: a question still unanswered then gets its "
                         "recommendation built)")
        self.assertEqual(questions.mode(self.p, D("2026-10-09 12:00")), "ask")
        self.assertTrue(questions.mode(self.p, D("2026-10-09 23:10")).startswith(
            "provisional (the user is in a quiet time until Sat 08:00)"))

    def test_the_note_says_when_it_ends_inside_one(self):
        self.user_file.write_text(NIGHT)
        note = questions.quiet_note(self.p, at=D("2026-10-09 23:30"))
        self.assertIn("## Quiet hours\nThe user is in quiet hours until Sat 08:00", note)
        self.assertIn("build your recommendation", note)

    def test_the_note_says_when_it_starts_before_the_session_ends(self):
        self.user_file.write_text(NIGHT)
        at = D("2026-10-09 21:00")
        note = questions.quiet_note(self.p, until=at + timedelta(hours=3), at=at)
        self.assertIn("quiet hours start at Fri 23:00 and last until Sat 08:00", note)
        self.assertEqual(questions.quiet_note(self.p, until=at + timedelta(hours=1), at=at), "",
                         "a quiet time after the session's end isn't worth its tokens")
        self.assertEqual(questions.quiet_note(self.p, at=D("2026-10-09 12:00")), "")

    def test_the_worker_gets_it_in_its_system_prompt(self):
        self.user_file.write_text(around_now())
        ensure_worktree(self.p, self.p.work_dir, setup=False)
        pl = driver.Pilot(self.p, args())
        pl.deadline = now() + timedelta(hours=2)
        pl.queue = Queue(self.p)
        seen = []

        class Seen(Exception):
            pass

        def claude(role, label, prompt, system, *a, **k):
            seen.append((role, system))
            raise Seen

        pl.claude = claude
        with self.assertRaises(Seen):
            pl.work("T-001", "new", [])
        [(role, system)] = seen
        self.assertEqual(role, "worker")
        self.assertTrue(system.startswith(pl.worker_rules), "the rules stay first, so their cache holds")
        self.assertIn("## Quiet hours\nThe user is in quiet hours until", system)


class Settled(Base):
    def pilot(self, **q):
        ensure_worktree(self.p, self.p.work_dir, setup=False)
        pl = driver.Pilot(self.p, args(answer_wait=600))
        pl.queue = Queue(self.p)
        pl.questions["T-001"] = {"ticket": "T-001", "q": "Red or blue?", "opts": {"A": "red", "B": "blue"},
                                 "rec": "A", "must_wait": False, "branch": "", "asked": None, **q}
        pl.skip.add("T-001")
        return pl

    def test_a_question_waiting_when_quiet_starts_gets_its_recommendation_at_once(self):
        pl = self.pilot(asked=time.time() - 60)  # sent a minute ago; its 600-minute wait is far off
        self.patch(phone, "answer", lambda *a, **k: "")
        self.user_file.write_text(around_now())
        pl.poll_questions()
        self.assertIn("quiet time until", pl.feedback["T-001"])
        self.assertIn("build your recommendation (A)", pl.feedback["T-001"])
        self.assertNotIn("T-001", pl.skip)
        [(title, msg)] = self.sent
        self.assertEqual(title, "Quiet time: do T-001 (T-001)")
        self.assertTrue(msg.startswith("Going with A: red."), "the phone showed the question already")

    def test_a_held_question_is_built_and_spelled_out_once(self):
        pl = self.pilot()  # asked by a worker during the quiet time: never sent
        self.user_file.write_text(around_now())
        pl.quiet_check()
        pl.poll_questions()
        [(title, msg)] = self.sent
        self.assertEqual(title, "Quiet time: do T-001 (T-001)")
        self.assertTrue(msg.startswith("Red or blue? Going with A: red."), msg)
        self.assertIn("windvane answer T-001 <letter>", msg)
        self.assertEqual(questions.shown(self.p)["asked"], {"T-001": ["Red or blue?"]},
                         "the Done push then names it in one line, not in full again")

    def test_questions_that_must_wait_or_were_put_off_still_wait(self):
        self.user_file.write_text(around_now())
        for extra in ({"must_wait": True}, {"rec": ""}, {"asked": time.time(), "later": True}):
            self.sent.clear()
            self.patch(phone, "answer", lambda *a, **k: "")
            pl = self.pilot(**extra)
            pl.quiet_check()
            pl.poll_questions()
            self.assertNotIn("result", pl.questions["T-001"], extra)
            self.assertEqual(self.sent, [], extra)

    def test_outside_a_quiet_time_a_question_still_waits_its_time(self):
        self.user_file.write_text(around_now(before=-60, after=120))  # starts in an hour
        pl = self.pilot(asked=time.time() - 60)
        self.patch(phone, "answer", lambda *a, **k: "")
        pl.poll_questions()
        self.assertNotIn("result", pl.questions["T-001"])
        self.assertEqual(self.sent, [])


if __name__ == "__main__":
    unittest.main()
