"""T-094: each question reaches the user once. One question went to the phone four times (the ask, No answer, a
Provisional push on merge, the morning's Review digest of every provisional answer), and preflight offered it again on
every run start; an open question on main no run waited on couldn't be answered at all."""
import io
import os
import sys
import threading
import time
import unittest
from contextlib import ExitStack, redirect_stdout
from datetime import timedelta
from pathlib import Path
from unittest import mock
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from windvane import cli, driver, phone, preflight, questions  # noqa: E402
from windvane.config import Project  # noqa: E402
from windvane.queue import Queue  # noqa: E402
from windvane.util import now  # noqa: E402
from windvane.worktree import ensure_worktree  # noqa: E402
from helpers import Sandbox  # noqa: E402
from test_pipeline import args  # noqa: E402

COLOUR = "### T-001: Red or blue?\nContext.\n- **A**: red\n- **B**: blue\nRecommendation: A, because.\n"
SIZE = "### T-002: Big or small?\n- **A**: big\n- **B**: small\nRecommendation: A, because.\n"
BUILT = "Built: A; to change it: x.txt\n"


class AskOnce(unittest.TestCase):
    def setUp(self):
        self.s = Sandbox()
        self.addCleanup(self.s.close)
        self.s.ticket("T-001")
        self.s.ticket("T-002")
        self.s.publish()
        self.p = Project(self.s.root)
        self.sent = []  # (title, text) of every push and phone question, in order
        stack = ExitStack()
        self.addCleanup(stack.close)
        self.patch = lambda obj, name, value: stack.enter_context(mock.patch.object(obj, name, value))
        self.patch(driver, "push", lambda title, msg="", *a, **k: self.sent.append((title, msg)))
        self.patch(phone, "ask", lambda qid, q, opts: self.sent.append(("Question", f"{q} {opts}")) or True)
        self.addCleanup(os.chdir, os.getcwd())

    def questions_md(self, open_="", prov=""):
        self.s.git("pull", "-q", "--ff-only", "origin", "main")
        self.s.write(".windvane/QUESTIONS.md", f"# Q\n\n## Open\n{open_}\n## Provisional\n{prov}\n## Answered\n")
        self.s.publish("questions")

    def pilot(self):
        ensure_worktree(self.p, self.p.work_dir, setup=False)
        pl = driver.Pilot(self.p, args())
        pl.deadline = now() + timedelta(hours=2)
        pl.queue = Queue(self.p)
        return pl

    def holding(self, text):
        return [t for t, m in self.sent if text in t + m]

    def test_asked_unanswered_and_merged_is_one_push(self):
        os.environ["FAKE_WORKER"] = "question"
        self.s.write(".windvane/tickets/T-002.md", "# T-002: do T-002\nDepends on: T-001.\n")
        self.s.publish("serial")
        driver.Pilot(self.p, args(answer_wait=0)).run(now() + timedelta(hours=1))
        self.s.git("pull", "-q", "--ff-only", "origin", "main")
        self.assertIn("### T-001: Red or blue?", questions.part((self.s.root / ".windvane/QUESTIONS.md").read_text(),
                                                                "Provisional"))
        self.assertEqual(self.holding("Red or blue?"), ["Question", "Question"])  # T-001's and T-002's, nothing more
        done = dict(self.sent)["Done: do T-001 (T-001)"]
        self.assertIn("\nBuilt A for T-001; to change: windvane answer T-001 <letter>.", done)
        self.assertNotIn("Provisional: do T-001 (T-001)", dict(self.sent))  # no second push behind the Done one
        self.assertIn("T-001 (built A: red)", dict(self.sent)["windvane stopped"])

    def test_a_quiet_time_answer_comes_whole_in_the_done_push_and_not_in_the_digest(self):
        self.questions_md(prov=COLOUR + BUILT)
        pl = self.pilot()
        pl.merged_ok("T-001", "", self.s.git("rev-parse", "HEAD"))
        [(title, msg)] = self.sent
        self.assertTrue(title.startswith("Done:"), title)
        self.assertIn("\nRed or blue? Built A without asking you: red; to change: windvane answer T-001 <letter>.",
                      msg)
        self.patch(driver, "quiet_now", lambda p: (None, None))
        pl.was_quiet = True
        pl.quiet_check()
        self.assertEqual(len(self.sent), 1, self.sent)

    def test_the_digest_lists_only_answers_built_since_the_last_one(self):
        self.patch(driver, "quiet_now", lambda p: (None, None))
        self.questions_md(prov=COLOUR + BUILT)
        pl = self.pilot()
        pl.was_quiet = True
        pl.quiet_check()
        self.assertEqual(self.holding("Red or blue?"), ["Review: 1 provisional answer"])
        pl.was_quiet = True
        pl.quiet_check()  # the next morning: nothing new
        self.assertEqual(len(self.sent), 1, self.sent)
        self.questions_md(prov=COLOUR + BUILT + SIZE + BUILT)
        pl.was_quiet = True
        pl.quiet_check()
        self.assertEqual(len(self.sent), 2, self.sent)
        self.assertEqual(self.sent[-1][0], "Review: 1 provisional answer")
        self.assertIn("T-002: Big or small?", self.sent[-1][1])
        self.assertNotIn("Red or blue?", self.sent[-1][1])

    def test_preflight_offers_a_provisional_answer_once(self):
        self.questions_md(prov=COLOUR + BUILT)
        said = []
        for _ in range(2):
            qs = preflight.find(self.p, 0.1)
            preflight.ask(self.p, qs, inp=lambda prompt: "", say=said.append)  # Enter: skipped
        self.assertEqual(sum("Red or blue?" in s for s in said), 1, said)
        x = preflight.lookup(self.p, "T-001")  # still answerable at the desk
        self.assertEqual((x["kind"], x["question"]), ("provisional", "Red or blue?"))

    def test_answer_takes_an_open_question_no_run_waits_on(self):
        self.questions_md(open_=COLOUR)
        self.assertEqual(Queue(self.p).status("T-001"), "blocked")
        out = io.StringIO()
        with redirect_stdout(out):
            cli.main(["-C", str(self.s.root), "answer", "T-001", "B"])
        self.assertIn("1 answer recorded on main", out.getvalue())
        self.s.git("pull", "-q", "--ff-only", "origin", "main")
        text = (self.s.root / ".windvane/QUESTIONS.md").read_text()
        self.assertNotIn("Red or blue?", questions.part(text, "Open"))
        self.assertIn("Answer: B (blue)", questions.part(text, "Answered"))
        self.assertEqual(Queue(self.p).status("T-001"), "todo")

    def test_a_question_times_out_while_a_ticket_runs(self):
        """One slot: the driver used to wait for the running ticket before looking at questions, so a 10-minute
        wait ran out after 56."""
        self.addCleanup(setattr, driver, "NAP", driver.NAP)
        driver.NAP = 0.1
        os.environ["FAKE_WORK_S"] = "3"
        pl = driver.Pilot(self.p, args(answer_wait=0.01))
        self.s.ticket("T-003")
        self.s.publish()
        pl.questions["T-003"] = {"q": "Red or blue?", "opts": {"A": "red", "B": "blue"}, "rec": "A",
                                 "must_wait": False, "branch": "", "asked": None}
        pl.skip.add("T-003")
        timed_out = []
        start = pl.start

        def start_then_ask(slot, tid, kind, branches):  # the question goes out as T-001's worker starts
            start(slot, tid, kind, branches)
            if tid == "T-001":
                pl.send_question("T-003")
                threading.Thread(target=lambda: timed_out.append(self.wait_no_answer(pl)), daemon=True).start()
        pl.start = start_then_ask
        pl.run(now() + timedelta(hours=1))
        self.assertTrue(timed_out and timed_out[0] < 2.5, timed_out)  # well inside T-001's 3 s

    def wait_no_answer(self, pl):
        t0 = time.time()
        while not self.holding("No answer") and time.time() - t0 < 20:
            time.sleep(0.05)
        return time.time() - t0


if __name__ == "__main__":
    unittest.main()
