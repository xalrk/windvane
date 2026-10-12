"""Queue rules a long-lived project needs: tickets kept for last, tickets waiting on someone outside the queue, and
tickets with a question under `## Open` on main, whoever wrote it."""
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from windvane import driver  # noqa: E402
from windvane.config import Project  # noqa: E402
from windvane.queue import Queue  # noqa: E402
from helpers import Sandbox  # noqa: E402
from test_parallel import args  # noqa: E402

OPEN_QUESTION = """# Q

## Open
### T-002: Which colour for the button?
Written by hand at the desk.
- **A**: blue
- **B**: green
Recommendation: A, because it matches.

## Provisional

## Answered
"""


class QueueRules(unittest.TestCase):
    def setUp(self):
        self.s = Sandbox()
        for n in range(1, 6):
            self.s.ticket(f"T-00{n}")

    def tearDown(self):
        self.s.close()

    def queue(self, qmd="", questions=None):
        self.s.write(".windvane/QUEUE.md", "# Queue\n\n" + qmd)
        if questions is not None:
            self.s.write(".windvane/QUESTIONS.md", questions)
        self.s.publish()
        return Queue(Project(self.s.root))

    def test_queues_without_the_new_lines_order_as_before(self):
        q = self.queue("- T-004 first\n- done: T-001 (folded)\n- parked: T-003 (later)\n- T-002\n")
        self.assertEqual(q.order, ["T-004", "T-002", "T-001", "T-003", "T-005"])
        self.assertEqual(q.pick(), ("T-004", "todo", []))
        self.assertEqual([q.status(t) for t in q.order], ["todo", "todo", "done", "parked", "todo"])

    def test_last_tickets_come_after_all_others_in_the_order_listed(self):
        q = self.queue("- last: T-003 (after the rest)\n- T-004\n- last: T-001 (cleanup)\n- T-002\n")
        self.assertEqual(q.order, ["T-004", "T-002", "T-005", "T-003", "T-001"])
        q = self.queue("- last: T-002 (later)\n- T-002 listed too\n")
        self.assertEqual(q.order[-1], "T-002")
        self.assertEqual(q.pick(skip={"T-001", "T-003", "T-004", "T-005"}), ("T-002", "todo", []))

    def test_a_ticket_depending_on_the_user_waits_and_shows_why(self):
        self.s.ticket("T-001", "the user")
        self.s.ticket("T-002", "T-005, M9 (the beta ships)")
        q = self.queue()
        self.assertEqual(q.status("T-001"), "waiting")
        self.assertEqual(q.status("T-002"), "waiting")
        self.assertEqual(q.pick(), ("T-003", "todo", []))
        listing = q.listing()
        self.assertIn("(waiting on: the user)", listing)
        self.assertIn("(waiting on: T-005, M9)", listing)
        out = subprocess.run([str(self.s.bin / "windvane"), "queue"], cwd=self.s.root, capture_output=True,
                             text=True).stdout
        self.assertRegex(out, r"T-001 +waiting .*\(waiting on: the user\)")

    def test_done_word_releases_an_outside_dependency(self):
        self.s.ticket("T-001", "M9")
        self.s.ticket("T-002", "M9 and the user")
        q = self.queue("- done: M9 (shipped)\n")
        self.assertEqual(q.status("T-001"), "todo")
        self.assertEqual(q.pick(), ("T-001", "todo", []))
        self.assertEqual(q.waits_on("T-002"), ["the user"])

    def test_ticket_ids_and_nothing_work_as_today(self):
        self.s.ticket("T-002", "T-001 (the parser)")
        self.s.write(".windvane/tickets/T-003.md", "# T-003: x\nDepends on: T-001,\n  T-002\nTouches: a.py.\n")
        q = self.queue()
        self.assertEqual(q.outside, {t: [] for t in q.files})
        self.assertEqual([q.status(t) for t in q.order], ["todo", "waiting", "waiting", "todo", "todo"])
        self.assertEqual(q.waits_on("T-003"), ["T-001", "T-002"])
        q = self.queue("- done: T-001 (folded)\n")
        self.assertEqual(q.status("T-002"), "todo")

    def test_nothing_with_a_note_and_a_dot_separated_header_is_no_dependency(self):
        self.s.write(".windvane/tickets/T-001.md", "# T-001: x\nMilestone: M9 · Depends on: nothing open (the tapes are "
                     "merged) · **Can start now** (pure code)\nTouches: a.py.\n")
        self.s.write(".windvane/tickets/T-002.md", "# T-002: y\nMilestone: M9 · Depends on: T-003 · Waits for review\n"
                     "Touches: b.py.\n")
        q = self.queue()
        self.assertEqual(q.status("T-001"), "todo")
        self.assertEqual(q.waits_on("T-002"), ["T-003"])

    def test_a_title_or_prose_quoting_a_key_is_not_its_header_line(self):
        self.s.write(".windvane/tickets/T-001.md", "# T-001: The `Depends on: T-004` line reads ids\n"
                     "Depends on: nothing.\nTouches: a.py.\n")
        self.s.write(".windvane/tickets/T-002.md", "# T-002: Read the `Depends on:` line, never the title\n"
                     "Touches: b.py.\n\nA ticket's `Depends on:` and `Related: T-005` lines are headers.\n")
        self.s.write(".windvane/tickets/T-003.md", "# T-003: Quote `Depends on: the user` and `Related: T-005`\n"
                     "Depends on: T-004.\nRelated: T-001.\nTouches: c.py.\n")
        q = self.queue()
        self.assertEqual((q.deps["T-001"], q.outside["T-001"], q.status("T-001")), ([], [], "todo"))
        self.assertEqual((q.deps["T-002"], q.outside["T-002"], q.status("T-002")), ([], [], "todo"))
        self.assertEqual(q.related["T-002"], [])
        self.assertEqual(q.waits_on("T-003"), ["T-004"])
        self.assertEqual(q.related["T-003"], ["T-001"])

    def test_a_key_after_a_dot_separator_still_counts(self):
        self.s.write(".windvane/tickets/T-001.md", "# T-001: x · Depends on: T-005\n"
                     "Milestone: M9 · Related: T-004 · Depends on: T-003 · Can start later\nTouches: a.py.\n")
        q = self.queue()
        self.assertEqual(q.waits_on("T-001"), ["T-003"])
        self.assertEqual(q.related["T-001"], ["T-004"])

    def test_an_open_question_written_by_hand_blocks_its_ticket(self):
        q = self.queue(questions=OPEN_QUESTION)
        self.assertEqual(q.status("T-002"), "blocked")
        self.assertEqual(q.pick(skip={"T-001"}), ("T-003", "todo", []))
        self.assertIn("(question: Which colour for the button?)", q.listing())
        q = self.queue(questions=OPEN_QUESTION.replace("## Open\n", "## Open\n\n## Answered (by hand)\n", 1)
                       .replace("\n## Answered\n", "\n"))
        self.assertEqual(q.status("T-002"), "todo")  # moved out of ## Open: answered


class LastWaits(unittest.TestCase):
    """A last ticket starts only when nothing else could: a free slot beside a ticket that only waits for a running
    one's files isn't "run out of things to do"."""
    HELD = "T-003 waits: last, T-002 still to run"

    def setUp(self):
        self.s = Sandbox()

    def tearDown(self):
        self.s.close()

    def pilot(self, qmd, b_deps="", questions=None, running="T-001"):
        """A running on x.py in slot 0, B todo on x.py, C (T-003) on c.py, T-004 parked; slot 1 free."""
        self.s.ticket("T-001", touches="x.py")
        self.s.ticket("T-002", b_deps, touches="x.py")
        self.s.ticket("T-003", touches="c.py")
        self.s.ticket("T-004", touches="d.py")
        self.s.write(".windvane/QUEUE.md", "# Queue\n\n- parked: T-004 (later)\n" + qmd)
        if questions is not None:
            self.s.write(".windvane/QUESTIONS.md", questions)
        self.s.publish()
        p = Project(self.s.root)
        pilot = driver.Pilot(p, args())
        pilot.queue = Queue(p)
        pilot.running = {running: 0}
        return pilot

    def test_c_waits_while_b_waits_only_on_the_running_tickets_files(self):
        pilot = self.pilot("- last: T-003 (only if we run out)\n")
        self.assertEqual(pilot.pick([1])[0], None)
        self.assertEqual(pilot.waits, ["T-002 waits: overlaps T-001 on x.py", self.HELD])

    def test_c_starts_once_b_is_parked(self):
        pilot = self.pilot("- last: T-003 (only if we run out)\n- parked: T-002 (later)\n")
        self.assertEqual(pilot.pick([1]), ("T-003", "todo", [], 1))
        self.assertEqual(pilot.waits, [])

    def test_c_starts_once_b_is_blocked_by_an_open_question(self):
        pilot = self.pilot("- last: T-003 (only if we run out)\n", questions=OPEN_QUESTION)
        self.assertEqual(pilot.pick([1])[:2], ("T-003", "todo"))

    def test_c_starts_while_b_waits_on_an_unmerged_dependency(self):
        pilot = self.pilot("- last: T-003 (only if we run out)\n", b_deps="T-004")
        self.assertEqual(pilot.pick([1])[:2], ("T-003", "todo"))

    def test_c_starts_while_b_is_set_aside(self):
        pilot = self.pilot("- last: T-003 (only if we run out)\n")
        pilot.skip = {"T-002"}
        self.assertEqual(pilot.pick([1])[:2], ("T-003", "todo"))

    def test_two_last_tickets_with_disjoint_touches_run_side_by_side(self):
        pilot = self.pilot("- last: T-001 (cleanup)\n- last: T-003 (only if we run out)\n- parked: T-002 (later)\n")
        self.assertEqual(pilot.pick([1])[:2], ("T-003", "todo"))

    def test_a_last_ticket_waits_its_turn_among_the_last_ones_on_a_clash(self):
        self.s.ticket("T-005", touches="x.py")
        pilot = self.pilot("- last: T-005 (first of the last)\n- last: T-003 (then)\n- parked: T-002 (later)\n",
                           running="T-001")
        pilot.queue.last.add("T-001")  # A counts as last too: only the last tickets are left
        self.assertEqual(pilot.pick([1])[:2], ("T-003", "todo"))
        self.assertEqual(pilot.waits, ["T-005 waits: overlaps T-001 on x.py"])

    def test_the_queue_marks_last_tickets_and_resumes_none_ahead_of_the_rest(self):
        pilot = self.pilot("- last: T-003 (only if we run out)\n")
        self.s.git("switch", "-qc", "t-003-half")
        self.s.git("commit", "-qm", "half", "--allow-empty")
        self.s.git("push", "-q", "origin", "t-003-half")
        self.s.git("switch", "-q", "main")
        q = Queue(pilot.p)
        self.assertEqual(q.status("T-003"), "wip")
        self.assertEqual(q.pick(), ("T-001", "todo", []))  # a wip last ticket isn't resumed before new work
        self.assertEqual(q.pick(skip={"T-001", "T-002"}), ("T-003", "wip", ["t-003-half"]))
        self.assertRegex(q.listing(), r"T-003 +wip +\(last\) do T-003")
        self.assertNotIn("(last)", q.listing().splitlines()[0])


if __name__ == "__main__":
    unittest.main()
