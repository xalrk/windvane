import sys
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from windvane import critic, questions  # noqa: E402
from windvane.config import Project  # noqa: E402
from windvane.queue import Queue  # noqa: E402
from helpers import Sandbox  # noqa: E402

D = lambda s: datetime.strptime(s, "%Y-%m-%d %H:%M")


class QuietTimes(unittest.TestCase):
    def test_daily_window_crossing_midnight(self):
        self.assertEqual(questions.quiet_until(["daily 23:00-08:00"], D("2026-10-01 02:00")), D("2026-10-01 08:00"))
        self.assertEqual(questions.quiet_until(["daily 23:00-08:00"], D("2026-10-01 23:30")), D("2026-10-02 08:00"))
        self.assertIsNone(questions.quiet_until(["daily 23:00-08:00"], D("2026-10-01 12:00")))

    def test_touching_windows_chain_and_comments(self):
        lines = ["daily 22:00-23:00  # evening", "2026-10-01 23:00-23:30", ""]
        self.assertEqual(questions.quiet_until(lines, D("2026-10-01 22:10")), D("2026-10-01 23:30"))

    def test_bad_line_raises(self):
        with self.assertRaises(ValueError):
            questions.quiet_until(["nightly 1-2"], D("2026-10-01 22:10"))


QFILE = """# Q

## Open
### T-003: Red or blue?
Some context.
- **A**: red
- **B**: blue
Recommendation: B, because blue.
Waits for the user: money

## Provisional
### T-001: Big or small?
- **A**: big
- **B**: small
Built: B; to change it: config.py SIZE

## Answered
"""


class Questions(unittest.TestCase):
    def test_parts(self):
        opts, rec, wait, q = questions.parts(questions.section(QFILE, "T-003"))
        self.assertEqual(opts, {"A": "red", "B": "blue"})
        self.assertEqual((rec, wait, q), ("B", True, "Red or blue?"))

    def test_section_is_per_heading(self):
        self.assertEqual(questions.section(QFILE, "T-001"), "")
        self.assertIn("Big or small", questions.section(QFILE, "T-001", "Provisional"))

    def test_provisional(self):
        self.assertEqual(questions.provisional(QFILE), [{"ticket": "T-001", "question": "Big or small?",
                                                         "built": "B", "option": "small"}])


class FakeP:
    critic = {"pass": 8, "floor": 4, "revisions": 3}


class Critic(unittest.TestCase):
    def test_step(self):
        p = FakeP()
        self.assertEqual(critic.step(p, 8, 2, 0), "pass")
        self.assertEqual(critic.step(p, 5, 0, 0), "scope")
        self.assertEqual(critic.step(p, 5, 1, 0), "revise")
        self.assertEqual(critic.step(p, 3, 1, 0), "rethink")
        self.assertEqual(critic.step(p, 5, 1, 3), "accept")

    def test_beyond(self):
        text = "Score: 5/10\nmeh\nFix in this ticket:\n1. x\nBeyond this ticket:\n- faster\n- 2. docs\nCRITIC: ..."
        self.assertEqual(critic.beyond(text), ["faster", "2. docs"])


class QueueFromGit(unittest.TestCase):
    def setUp(self):
        self.s = Sandbox()

    def tearDown(self):
        self.s.close()

    def test_order_deps_done_parked_wip(self):
        s = self.s
        for t in ("T-001", "T-002", "T-003", "T-004a", "T-004b"):
            s.ticket(t, "T-001" if t == "T-002" else "")
        s.write(".windvane/QUEUE.md", "# Queue\n- T-004 split\n- **T-003** first\n- parked: T-002 (later)\n")
        s.publish()
        p = Project(s.root)
        q = Queue(p)
        self.assertEqual(q.order, ["T-004a", "T-004b", "T-003", "T-001", "T-002"])
        self.assertEqual(q.status("T-002"), "parked")
        # a merged branch marks its ticket done; an unmerged one is wip and resumes first
        s.git("switch", "-q", "-c", "t-003-x")
        s.write("x", "1")
        s.git("add", "-A")
        s.git("commit", "-qm", "x")
        s.git("switch", "-q", "main")
        s.git("merge", "-q", "--no-ff", "-m", "Merge T-003: x (branch t-003-x)", "t-003-x")
        s.git("push", "-q", "origin", "main")
        s.git("switch", "-q", "-c", "t-001-y")
        s.write("y", "1")
        s.git("add", "-A")
        s.git("commit", "-qm", "y")
        s.git("push", "-q", "origin", "t-001-y")
        s.git("fetch", "-q", "origin")
        q = Queue(p)
        self.assertEqual(q.status("T-003"), "done")
        self.assertEqual(q.pick(), ("T-001", "wip", ["t-001-y"]))
        self.assertEqual(q.pick(skip={"T-001"})[0], "T-004a")
        self.assertEqual(q.next_id(), "T-005")


if __name__ == "__main__":
    unittest.main()
