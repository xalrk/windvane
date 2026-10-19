"""`windvane queue`'s hints (T-187): one line per cause naming every ticket waiting on it, worded for a person's answer
apart from a milestone, for a dependency that failed this run or waits on a question, and with a set-aside's own fix;
a set-aside push names the tickets waiting on it below a first line that still fits a lock screen."""
import fcntl
import json
import os
import subprocess
import sys
import unittest
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from windvane import driver  # noqa: E402
from windvane.config import Project  # noqa: E402
from windvane.queue import Queue  # noqa: E402
from windvane.util import now, shell_path  # noqa: E402
from helpers import Sandbox  # noqa: E402
from test_set_aside import args  # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        self.s = Sandbox()
        self.addCleanup(self.s.close)
        self.p = Project(self.s.root)

    def publish(self, qmd=""):
        self.s.write(".windvane/QUEUE.md", "# Queue\n\n" + qmd)
        self.s.publish()

    def queue_cli(self, state=None):
        """`windvane queue`, with `state` as a running driver's state.json (it holds the lock meanwhile)."""
        def cmd():
            r = subprocess.run([str(self.s.bin / "windvane"), "queue"], cwd=self.s.root, capture_output=True, text=True)
            self.assertEqual((r.returncode, r.stderr), (0, ""))
            return r.stdout
        if state is None:
            return cmd()
        self.p.state.mkdir(parents=True, exist_ok=True)
        (self.p.state / "state.json").write_text(json.dumps(state))
        with open(self.p.state / "lock", "a+") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return cmd()

    def hints(self, out):
        return [line.strip() for line in out.splitlines() if line.strip().startswith("hint:")]


class OnePerCause(Base):
    def test_three_tickets_on_the_user_give_one_line_naming_all_three(self):
        self.s.ticket("T-001")
        for t in ("T-002", "T-004", "T-005"):
            self.s.ticket(t, "the user")
        self.publish()
        out = self.queue_cli()
        self.assertEqual(self.hints(out), ["hint: `the user`: T-002, T-004, T-005 wait; once you've answered, add "
                                           "`- done: the user (why)` to .windvane/QUEUE.md"])
        self.assertRegex(out, r"T-002 +waiting .*\n +hint: `the user`")  # under the first one waiting
        self.assertEqual(sum("(waiting on: the user)" in line for line in out.splitlines()), 3)  # the rows stay

    def test_a_milestone_is_worded_apart_from_a_person(self):
        self.s.ticket("T-001", "M9")
        self.s.ticket("T-002", "M9, you")
        self.publish()
        self.assertEqual(self.hints(self.queue_cli()), [
            "hint: `M9` isn't a ticket: T-001, T-002 wait; once it's done, add `- done: M9 (why)` to "
            ".windvane/QUEUE.md, or take it out of `Depends on:` in each of their tickets",
            "hint: `you`: T-002 waits; once you've answered, add `- done: you (why)` to .windvane/QUEUE.md"])

    def test_a_dependency_still_running_or_queued_gets_none(self):
        self.s.ticket("T-001")
        self.s.ticket("T-002", "T-001")
        self.publish()
        self.assertEqual(self.hints(self.queue_cli()), [])


class RunCauses(Base):
    def setUp(self):
        super().setUp()
        self.s.ticket("T-001")
        self.s.ticket("T-002", "T-001")
        self.s.ticket("T-003", "T-001")

    def test_a_dependency_that_failed_this_run_says_how_to_retry_it(self):
        self.publish()
        out = self.queue_cli({"failed": ["T-001"]})
        self.assertEqual(self.hints(out), [f"hint: {self.failed}"])
        self.assertEqual(self.hints(self.queue_cli()), [])  # no run on: the next one tries it as usual

    def test_a_dependency_held_for_a_phone_question_names_its_id_and_the_answer_command(self):
        self.publish()
        state = {"questions": {"T-001.2": {"ticket": "T-001", "question": "Which?"}}}
        self.assertEqual(self.hints(self.queue_cli(state)), [
            "hint: T-001 has an open question (T-001.2): T-002, T-003 wait until it's answered: `windvane answer "
            "T-001.2 <letter>`"])
        state["questions"]["T-001.2"]["result"] = "A"  # settled: no longer waiting for anyone
        out = self.queue_cli(state)
        self.assertEqual(self.hints(out), [])
        self.assertRegex(out, r"T-002 +waiting")
        self.assertRegex(out, r"T-003 +waiting")

    def test_a_dependency_blocked_on_an_open_question_in_the_file_names_it(self):
        self.s.write(".windvane/QUESTIONS.md", "# Questions\n\n## Open\n\n### T-001: Which?\n- **A**: a\n- **B**: b\n"
                     "Recommendation: A, because.\n\n## Provisional\n\n## Answered\n")
        self.publish()
        self.assertIn("hint: T-001 has an open question: T-002, T-003 wait until it's answered: "
                      "`windvane answer T-001 <letter>`", self.queue_cli())

    def test_a_set_aside_hint_carries_its_own_fix(self):
        self.publish()
        out = self.queue_cli({"aside": {"T-001": "hit its max turns"},
                              "fixes": {"T-001": f"Split it, or raise {driver.MAX_TURNS}."}})
        self.assertEqual(self.hints(out), [
            "hint: T-001 was set aside this run (hit its max turns): T-002, T-003 wait until it merges; so it isn't "
            "set aside again, split it, or raise CLAUDE_CODE_MAX_TURNS"])

    def test_a_secret_set_aside_says_runs_leave_it_be_until_its_branch_is_rewritten(self):
        self.publish()
        out = self.queue_cli({"aside": {"T-001": "its .windvane/ lines hold a token"}, "fixes": {"T-001": driver.REWRITE}})
        self.assertIn("wait until it merges; so it isn't set aside again, rewrite its branch without the secret, as "
                      "its push says: runs leave it be until then\n", out)

    def test_a_set_aside_without_a_fix_says_the_next_run_takes_it_up(self):
        self.publish()
        out = self.queue_cli({"aside": {"T-001": "its sessions kept dying: API Error: 500"}, "fixes": {"T-001": ""}})
        self.assertIn("hint: T-001 was set aside this run (its sessions kept dying: API Error: 500): T-002, T-003 wait "
                      "until it merges; the next run takes it up again\n", out)

    def test_hints_returns_one_entry_per_first_waiting_ticket(self):
        self.publish()
        self.assertEqual(Queue(self.p).hints(run={"failed": ["T-001"]}),
                         {"T-002": [self.failed]})

    @property
    def failed(self):
        return (f"T-001 failed this run (the `T-001: FAILED` line in {shell_path(self.p.state / 'autopilot.log')} "
                "says why): T-002, T-003 "
                "wait until it merges; `windvane retry T-001` puts it back in this run, or the next `windvane run` "
                "takes it up")


class SetAsidePush(Base):
    def setUp(self):
        super().setUp()
        self.s.ticket("T-001")
        self.s.ticket("T-002", "T-001")
        self.publish()
        self.pushes = []
        orig, driver.push = driver.push, lambda title, msg, *a, **k: self.pushes.append((title, msg))
        self.addCleanup(setattr, driver, "push", orig)

    def test_a_set_aside_push_names_the_waiting_ticket_and_its_first_line_still_fits(self):
        os.environ["FAKE_MAX_TURNS"] = "# windvane worker"
        self.addCleanup(os.environ.pop, "FAKE_MAX_TURNS", None)
        pilot = driver.Pilot(self.p, args())
        pilot.sleep, pilot.deadline = lambda s: None, now() + timedelta(hours=2)
        pilot.run(now() + timedelta(hours=2))
        [msg] = [m for t, m in self.pushes if t.startswith("Set aside")]
        first, rest = msg.split("\n", 1)
        self.assertEqual(first, f"Hit its max turns. Split it, or raise {driver.MAX_TURNS}. Branch kept.")
        self.assertLessEqual(len(first), 90)  # a lock screen's two lines, as T-079's limit pushes
        self.assertEqual(rest, f"{driver.NEW_MAX_TURNS}\nT-002 waits on it.")
        self.assertEqual(json.loads((self.p.state / "state.json").read_text())["fixes"],
                         {"T-001": f"Split it, or raise {driver.MAX_TURNS}."})

    def test_a_set_aside_fix_survives_a_restart(self):
        pilot = driver.Pilot(self.p, args())
        pilot.set_aside_limit("T-001", "its max turns", "Split it.")
        pilot.hand_over()
        again = driver.Pilot(self.p, args())
        again.carry_over(True)
        self.assertEqual(again.fixes, {"T-001": "Split it."})
        again.save()
        self.assertEqual(json.loads((self.p.state / "state.json").read_text())["fixes"], {"T-001": "Split it."})

    def test_a_long_list_of_waiting_tickets_is_counted_past_four(self):
        pilot = driver.Pilot(self.p, args())
        pilot.queue = type("Q", (), {"waiting_for": lambda self, t: [f"T-00{n}" for n in range(2, 9)]})()
        self.assertEqual(pilot.held_line("T-001"), "\nT-002, T-003, T-004, 4 more wait on it.")
        self.assertEqual(pilot.holds_up("T-001"), "; T-002, T-003, T-004, T-005, T-006, T-007, T-008 wait on it")


if __name__ == "__main__":
    unittest.main()
