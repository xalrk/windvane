"""T-119: a ticket's every open question gets its own id. A worker that ended BLOCKED with two questions under ## Open
had only the first reach the user: one phone question, one `windvane answer` per ticket, the second left stuck."""
import io
import json
import os
import sys
import time
import unittest
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from datetime import timedelta
from pathlib import Path
from unittest import mock
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from windvane import cli, driver, phone, questions  # noqa: E402
from windvane.config import Project  # noqa: E402
from windvane.queue import Queue  # noqa: E402
from windvane.util import now  # noqa: E402
from windvane.worktree import ensure_worktree  # noqa: E402
from helpers import Sandbox  # noqa: E402
from test_pipeline import args  # noqa: E402

COLOUR = "### T-001: Red or blue?\nContext.\n- **A**: red\n- **B**: blue\nRecommendation: A, because.\n"
SIZE = "### T-001: Big or small?\n- **A**: big\n- **B**: small\nRecommendation: A, because.\n"


class ManyQuestions(unittest.TestCase):
    def setUp(self):
        self.s = Sandbox()
        self.addCleanup(self.s.close)
        self.s.ticket("T-001")
        self.s.publish()
        self.p = Project(self.s.root)
        self.asked = []  # (qid, question) of every phone question
        stack = ExitStack()
        self.addCleanup(stack.close)
        self.pushes = []
        stack.enter_context(mock.patch.object(driver, "push", lambda t, m="", *a, **k: self.pushes.append((t, m))))
        stack.enter_context(mock.patch.object(phone, "ask", lambda qid, q, opts: self.asked.append((qid, q)) or True))
        self.addCleanup(os.chdir, os.getcwd())

    def branch(self, open_):
        """A ticket branch on the remote whose QUESTIONS.md has `open_` under ## Open."""
        self.s.git("switch", "-q", "-c", "t-001-x")
        self.s.write(".windvane/QUESTIONS.md", f"# Q\n\n## Open\n{open_}\n## Provisional\n\n## Answered\n")
        self.s.git("add", "-A")
        self.s.git("commit", "-qm", "questions")
        self.s.git("push", "-q", "origin", "t-001-x")
        self.s.git("switch", "-q", "main")

    def pilot(self, wait=60):
        ensure_worktree(self.p, self.p.work_dir, setup=False)
        pl = driver.Pilot(self.p, args(answer_wait=wait))
        pl.deadline = now() + timedelta(hours=2)
        pl.queue = Queue(self.p)
        return pl

    def cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            try:
                cli.main(["-C", str(self.s.root), *argv])
            except SystemExit as e:
                return out.getvalue() + str(e.code or "")
        return out.getvalue()

    def test_two_questions_are_asked_listed_and_answered_one_by_one(self):
        self.branch(COLOUR + SIZE)
        pl = self.pilot()
        pl.ask("T-001", "t-001-x", "Red or blue?")
        pl.send_unsent()
        self.assertEqual([qid for qid, _ in self.asked], [self.p.qid("T-001.q1"), self.p.qid("T-001.q2")])
        self.assertTrue(self.p.qid("T-001.q2").endswith("-t-001-q2"))
        self.assertTrue(self.asked[1][1].startswith("do T-001 (T-001.q2): Big or small?"), self.asked[1][1])
        status = self.cli("status")
        self.assertIn("waiting on you: T-001.q1: Red or blue?", status)
        self.assertIn("waiting on you: T-001.q2: Big or small?", status)
        listed = json.loads(self.cli("preflight", "--json"))
        self.assertEqual([(x["id"], x["ticket"], x["question"]) for x in listed if x["kind"] == "open"],
                         [("T-001.q1", "T-001", "Red or blue?"), ("T-001.q2", "T-001", "Big or small?")])
        refused = self.cli("answer", "T-001", "A")
        self.assertIn("T-001 has 2 open questions", refused)
        self.assertIn("T-001.q1, T-001.q2", refused)
        self.assertEqual(questions.desk_answers(self.p), {})  # nothing guessed
        self.assertIn("T-001.q2: answer recorded", self.cli("answer", "t-001.Q2", "B"))
        pl.poll_questions()
        self.assertIn("T-001", pl.skip)  # still waiting on .q1
        self.assertEqual(Queue(self.p).status("T-001", questions.tickets(pl.questions)), "blocked")
        self.assertNotIn("T-001", pl.feedback)
        self.assertNotIn("waiting on you: T-001.q2", self.cli("status"))
        self.assertIn("waiting on you: T-001.q1", self.cli("status"))
        self.assertIn("T-001.q1: answer recorded", self.cli("answer", "T-001", "A"))  # the one left: no doubt
        pl.poll_questions()
        self.assertEqual(pl.questions, {})
        self.assertNotIn("T-001", pl.skip)
        self.assertIn("\"Red or blue?\": option A (red)", pl.feedback["T-001"])
        self.assertIn("\"Big or small?\": option B (small)", pl.feedback["T-001"])
        self.assertEqual(questions.desk_answers(self.p), {})

    def test_each_question_times_out_on_its_own(self):
        self.branch(COLOUR + SIZE)
        pl = self.pilot(wait=10)
        pl.ask("T-001", "t-001-x", "Red or blue?")
        pl.send_unsent()
        pl.questions["T-001.q1"]["asked"] = time.time() - 11 * 60  # its own wait ran out; .q2's hasn't
        pl.poll_questions()
        self.assertNotIn("T-001", pl.feedback)
        self.assertEqual(self.pushes[-1], ("No answer: do T-001 (T-001.q1)",
                                           "Going with A: red.\nTo change it later: windvane answer T-001.q1 <letter>."))
        self.cli("answer", "T-001.q2", "B")
        pl.poll_questions()
        self.assertIn("didn't answer your question \"Red or blue?\" within 10 minutes", pl.feedback["T-001"])
        self.assertIn("\"Big or small?\": option B (small)", pl.feedback["T-001"])
        self.assertIn("isn't on main yet; once its ticket merges, answer again", self.cli("answer", "T-001.q1", "B"))
        # its worker builds A and moves it to ## Provisional (after another of the ticket's), and the ticket merges
        self.s.write(".windvane/QUESTIONS.md", "# Q\n\n## Open\n\n## Provisional\n" + SIZE + "Built: A; y\n"
                     + COLOUR + "Built: A; x\n\n## Answered\n")
        self.s.publish("merged")
        self.assertIn("To change: windvane answer T-001.p2 <letter>", self.cli("status"))
        self.assertIn("1 answer recorded on main", self.cli("answer", "T-001.q1", "B"))  # the push's id still works
        self.s.git("pull", "-q", "--ff-only", "origin", "main")
        answered = questions.part((self.s.root / ".windvane/QUESTIONS.md").read_text(), "Answered")
        self.assertIn("### T-001: Red or blue?", answered)
        self.assertNotIn("Big or small?", answered)

    def test_one_question_keeps_the_ticket_id(self):
        self.branch(COLOUR)
        pl = self.pilot()
        pl.ask("T-001", "t-001-x", "Red or blue?")
        pl.send_unsent()
        self.assertEqual(list(pl.questions), ["T-001"])
        self.assertEqual(self.asked, [(self.p.qid("T-001"), "do T-001 (T-001): Red or blue? No answer in 60 min: I go "
                                       "with A and mark it to revisit.")])
        self.assertIn("waiting on you: T-001: Red or blue?", self.cli("status"))
        self.assertIn("T-001: answer recorded", self.cli("answer", "T-001", "B"))
        pl.poll_questions()
        self.assertEqual(pl.feedback["T-001"], "The user answered your question \"Red or blue?\": option B (blue). "
                         "Build that, and move the question to ## Answered with the answer.")

    def test_a_restart_keeps_the_answered_one_and_reads_an_older_state(self):
        self.p.state.mkdir(parents=True, exist_ok=True)
        q = {"opts": {"A": "red", "B": "blue"}, "rec": "A", "must_wait": False, "branch": "t-001-x",
             "asked": time.time()}
        (self.p.state / "state.json").write_text(json.dumps({"questions": {
            "T-001.q1": {**q, "ticket": "T-001", "q": "Red or blue?", "result": "Red: answered B."},
            "T-001.q2": {**q, "ticket": "T-001", "q": "Big or small?"},
            "T-002": {**q, "q": "Old or new?"}}}))  # an older run's: keyed by its ticket, no "ticket" field
        pl = self.pilot()
        self.assertEqual(questions.tickets(pl.questions), {"T-001", "T-002"})
        status = self.cli("status")
        self.assertNotIn("T-001.q1", status)  # settled: waits only for its sibling
        self.assertIn("waiting on you: T-001.q2: Big or small?", status)
        self.assertIn("waiting on you: T-002: Old or new?", status)
        self.cli("answer", "T-001", "A")  # the one of T-001's still open
        self.cli("answer", "T-002", "B")
        pl.poll_questions()
        self.assertEqual(pl.feedback["T-001"].splitlines()[0], "Red: answered B.")
        self.assertIn("\"Big or small?\": option A (red)", pl.feedback["T-001"])
        self.assertIn("\"Old or new?\": option B (blue)", pl.feedback["T-002"])
        self.assertEqual(pl.questions, {})

    def test_status_names_each_of_several_provisional_answers(self):
        self.s.write(".windvane/QUESTIONS.md", "# Q\n\n## Open\n\n## Provisional\n" + COLOUR + "Built: A; x\n"
                     + SIZE + "Built: A; y\n\n## Answered\n")
        self.s.publish("questions")
        status = self.cli("status")
        self.assertIn("Red or blue? Built A: red. To change: windvane answer T-001.p1 <letter>", status)
        self.assertIn("Big or small? Built A: big. To change: windvane answer T-001.p2 <letter>", status)
        self.assertIn("1 answer recorded on main", self.cli("answer", "T-001.p2", "B"))

    def workers(self):
        """The worker runs' prompts so far."""
        log = self.s.tmp / "calls.jsonl"
        calls = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
        return [c["prompt"] for c in calls if "worker" in c["system"]]

    def test_the_ticket_runs_again_only_once_both_are_settled(self):
        os.environ["FAKE_WORKER"] = "questions"
        pl = driver.Pilot(self.p, args(answer_wait=60))
        pl.nap = lambda until, answers=False: None
        polls, orig = [], pl.poll_questions

        def answer_then_poll():
            workers = len(self.workers())
            if "T-001.q2" in pl.questions and pl.questions["T-001.q2"]["asked"] and "T-001.q2" not in polls:
                polls.append("T-001.q2")
                questions.give_answer(self.p, "T-001.q2", "B")
            elif "T-001.q1" in pl.questions and "result" in pl.questions.get("T-001.q2", {}):
                self.assertEqual(workers, 1)  # half answered: not run again
                polls.append("T-001.q1")
                pl.questions["T-001.q1"]["asked"] = time.time() - 61 * 60
            orig()
        pl.poll_questions = answer_then_poll
        pl.run(now() + timedelta(hours=1))
        self.assertEqual(polls, ["T-001.q2", "T-001.q1"])
        self.assertEqual(pl.merged, ["T-001"])
        prompts = self.workers()
        self.assertEqual(len(prompts), 2)
        self.assertIn("didn't answer your question \"Red or blue?\"", prompts[1])
        self.assertIn("\"Big or small?\": option B (small)", prompts[1])


if __name__ == "__main__":
    unittest.main()
