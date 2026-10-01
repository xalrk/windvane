"""Preflight: the next tickets' questions asked at the start of a run, answers on main and in the worker's prompt."""
import contextlib
import io
import json
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from windvane import cli, preflight, questions  # noqa: E402
from windvane.config import Project  # noqa: E402
from windvane.queue import Queue  # noqa: E402
from windvane.util import append_jsonl  # noqa: E402
from helpers import Sandbox  # noqa: E402

QUESTION = ("### T-001: Red or blue?\nThe ticket doesn't say the colour.\n- **A**: red\n- **B**: blue\n"
            "Recommendation: A, because red stands out.\n")


class Tty(io.StringIO):
    """A terminal the user types `text` into."""

    def isatty(self):
        return True


class Untouchable(Tty):
    def readline(self, *a):
        raise AssertionError("asked the user")


class Preflight(unittest.TestCase):
    def setUp(self):
        self.s = Sandbox()
        self.s.ticket("T-001")
        self.s.publish()
        self.p = Project(self.s.root)
        self.asks = self.s.tmp / "asks.txt"
        fake = self.s.bin / "notify-ask"
        fake.write_text(f"#!/bin/sh\necho \"$@\" >> {self.asks}\n")
        fake.chmod(0o755)
        self.addCleanup(os.chdir, os.getcwd())
        self.addCleanup(setattr, sys, "stdin", sys.stdin)

    def tearDown(self):
        self.s.close()

    def calls(self, role):
        f = self.s.tmp / "calls.jsonl"
        calls = [json.loads(l) for l in f.read_text().splitlines()] if f.exists() else []
        return [c["prompt"] for c in calls if c["system"].startswith(f"# windvane {role}")]

    def cli(self, *argv, stdin=None):
        sys.stdin = stdin or io.StringIO()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.main(["-C", str(self.s.root), *argv])
        return out.getvalue()

    def main_questions(self):
        self.s.git("fetch", "-q", "origin")
        return self.s.git("show", "origin/main:.windvane/QUESTIONS.md")

    def test_terminal_answer_lands_on_main_and_the_worker_builds_it_without_the_phone(self):
        os.environ.update(FAKE_PREFLIGHT=QUESTION, FAKE_WORKER="question")
        out = self.cli("run", "--for", "1h", stdin=Tty("B\n"))
        self.assertIn("Red or blue?", out)
        self.assertIn("A: red   <- recommended, because red stands out.", out)
        answered = questions.part(self.main_questions(), "Answered")
        self.assertIn("### T-001: Red or blue?", answered)
        self.assertIn("Answer: B (blue)", answered)
        worker = self.calls("worker")
        self.assertEqual(len(worker), 1)
        self.assertIn("The user already answered these", worker[0])
        self.assertIn("Answer: B (blue)", worker[0])
        self.assertFalse(self.asks.exists())  # never reached the phone
        self.assertIn("T-001", self.s.git("log", "origin/main", "--merges", "--format=%s"))
        self.assertIn("preflight", [json.loads(l)["role"] for l in (self.p.state / "ledger.jsonl").read_text().splitlines()])

    def test_a_skipped_question_is_asked_on_the_phone_as_before(self):
        os.environ.update(FAKE_PREFLIGHT=QUESTION, FAKE_WORKER="question")
        self.cli("run", "--for", "1h", "--answer-wait", "0", stdin=Tty("\n"))
        self.assertIn("Red or blue?", self.asks.read_text())
        self.assertNotIn("Red or blue?", questions.part(self.main_questions(), "Answered"))

    def test_no_preflight_and_no_terminal_start_without_asking(self):
        os.environ["FAKE_PREFLIGHT"] = QUESTION
        self.cli("run", "--for", "1h", "--no-preflight", stdin=Untouchable())
        self.cli("run", "--for", "1h", stdin=io.StringIO("B\n"))  # a script: no TTY
        self.assertEqual(self.calls("preflight"), [])
        self.assertEqual(len(self.calls("worker")), 1)

    def test_nothing_found_starts_the_run_at_once(self):
        out = self.cli("run", "--for", "1h", stdin=Untouchable())
        self.assertIn("no questions; starting", out)
        self.assertEqual(len(self.calls("preflight")), 1)
        self.assertEqual(len(self.calls("worker")), 1)

    def test_detached_run_never_asks_again_in_tmux(self):
        seen = []
        orig = cli.subprocess.run
        cli.subprocess.run = lambda cmd, **kw: seen.append(cmd) if cmd[0] == "tmux" and "new-session" in cmd \
            else orig(cmd, **kw)
        self.addCleanup(setattr, cli.subprocess, "run", orig)
        self.addCleanup(setattr, sys, "argv", sys.argv)
        sys.argv = ["windvane", "run", "--for", "1h", "--detach"]
        self.cli(*sys.argv[1:], stdin=Untouchable())
        self.assertTrue(seen[0][-1].endswith('"run" "--for" "1h" "--no-preflight"'))

    def test_json_then_desk_answer(self):
        os.environ["FAKE_PREFLIGHT"] = QUESTION
        qs = json.loads(self.cli("preflight", "--json"))
        self.assertEqual([(q["id"], q["question"], q["recommendation"]) for q in qs],
                         [("T-001.1", "Red or blue?", "A")])
        self.assertEqual(qs[0]["options"], {"A": "red", "B": "blue"})
        self.assertIn("windvane answer T-001.1", qs[0]["answer_with"])
        self.assertIn("1 answer recorded on main", self.cli("answer", "T-001", "purple, please"))
        self.assertIn("Answer: purple, please", questions.part(self.main_questions(), "Answered"))
        self.assertEqual(preflight.pending(self.p), [])

    def test_waiting_questions_are_asked_again(self):
        self.s.write(".windvane/QUESTIONS.md", "# Q\n\n## Open\n\n## Provisional\n### T-009: Big or small?\n"
                     "- **A**: big\n- **B**: small\nRecommendation: A, because.\nBuilt: A; to change it: x.py\n\n"
                     "## Answered\n### T-000: Old?\nAnswer: A\n")
        self.s.publish("provisional")
        self.p.state.mkdir(parents=True, exist_ok=True)
        (self.p.state / "state.json").write_text(json.dumps({"questions": {"T-001": {
            "q": "Left or right?", "opts": {"A": "left", "B": "right"}, "rec": "B", "must_wait": False,
            "branch": "t-001-x", "asked": 1}}}))
        qs = preflight.find(self.p, 1)
        self.assertEqual([(q["id"], q["kind"]) for q in qs], [("T-001", "open"), ("T-009.p1", "provisional")])
        self.assertEqual(self.calls("preflight"), [])  # T-001 waits on its open question: nothing new to read
        self.cli("answer", "T-001", "A")
        self.assertEqual(questions.desk_answers(self.p)["T-001"]["option"], "A")  # the waiting run takes it
        self.cli("answer", "T-009.p1", "A")
        text = self.main_questions()
        self.assertNotIn("Big or small?", questions.part(text, "Provisional"))
        self.assertIn("Answer: A (confirmed the provisional answer)", questions.part(text, "Answered"))
        self.assertNotIn("T-002", Queue(self.p).files)  # a confirmation writes no ticket
        self.assertIn("### T-000: Old?", questions.part(text, "Answered"))

    def test_changing_a_provisional_answer_queues_a_ticket_that_builds_it(self):
        self.s.write(".windvane/QUESTIONS.md", "# Q\n\n## Open\n\n## Provisional\n### T-001: Big or small?\n"
                     "- **A**: big\n- **B**: small\nRecommendation: A, because.\nBuilt: A; to change it: SIZE in x.py\n"
                     "\n## Answered\n")
        self.s.publish("provisional")  # no preflight run: `answer` finds it on main (T-007's case, after a merge)
        self.assertIn("T-002 will change what was built", self.cli("answer", "T-001", "B"))
        self.s.git("fetch", "-q", "origin")
        ticket = self.s.git("show", "origin/main:.windvane/tickets/T-002.md")
        self.assertIn("# T-002: Change T-001's provisional answer: Big or small?", ticket)
        self.assertIn("they chose option B (small)", ticket)
        self.assertIn("SIZE in x.py", ticket)
        self.assertIn("- T-002 change T-001's answer", self.s.git("show", "origin/main:.windvane/QUEUE.md"))
        self.assertIn("Answer: B (small). Changes the built A: T-002 makes the change",
                      questions.part(self.main_questions(), "Answered"))
        self.assertEqual(Queue(self.p).pick()[:2], ("T-002", "todo"))  # listed in QUEUE.md: it goes first

    def test_an_answer_with_no_question_anywhere_says_so(self):
        with self.assertRaises(SystemExit) as e:
            self.cli("answer", "T-001", "B")
        self.assertIn("no question of it waits", str(e.exception.code))


class Units(unittest.TestCase):
    def setUp(self):
        self.s = Sandbox()
        for n in range(1, 13):
            self.s.ticket(f"T-{n:03d}")
        self.s.publish()
        self.p = Project(self.s.root)

    def tearDown(self):
        self.s.close()

    def test_likely_tickets_follow_the_ledger_minutes(self):
        q = Queue(self.p)
        self.assertEqual(len(preflight.likely(self.p, q, 4, parallel=1)), 4)  # no ledger: an hour a ticket
        for t, m in (("T-001", 20), ("T-002", 40)):
            append_jsonl(self.p.state / "ledger.jsonl", {"role": "worker", "ticket": t, "minutes": m,
                                                         "at": f"{preflight.now():%Y-%m-%d %H:%M}"})
        self.assertEqual(preflight.likely(self.p, q, 1, parallel=2), ["T-001", "T-002", "T-003", "T-004"])
        self.assertEqual(len(preflight.likely(self.p, q, 40)), 8)  # the prompt stays small

    def test_file_answers_adds_under_answered_and_keeps_the_rest(self):
        x = preflight.question("T-002", "Red or blue?", QUESTION.split("\n", 1)[1], "new", 1)
        text = preflight.file_answers("# Q\n\n## Open\n### T-005: x?\n- **A**: a\n\n## Answered\n### T-000: y?\n"
                                      "Answer: A\n\n## Notes\nkeep\n", [(x, "A", "")])
        self.assertIn("### T-005: x?", questions.part(text, "Open"))
        self.assertEqual([b[:2] for b in questions.blocks(questions.part(text, "Answered"))],
                         [("T-000", "y?"), ("T-002", "Red or blue?")])
        self.assertIn("The ticket doesn't say the colour.", questions.answered(text, "T-002")[0])
        self.assertIn("\n## Notes\nkeep", text)


if __name__ == "__main__":
    unittest.main()
