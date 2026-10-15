"""A ticket that would wait forever says so (T-165): `windvane queue` hints how to release an outside item, a parked
dependency or one set aside this run, warns of header lines it reads differently from how they look, and a set-aside
ticket's log line and summary name the open tickets waiting on it."""
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
from windvane.util import now  # noqa: E402
from helpers import Sandbox  # noqa: E402
from test_set_aside import args  # noqa: E402


class Hints(unittest.TestCase):
    def setUp(self):
        self.s = Sandbox()
        for n in range(1, 4):
            self.s.ticket(f"T-00{n}")

    def tearDown(self):
        self.s.close()

    def publish(self, qmd=""):
        self.s.write(".windvane/QUEUE.md", "# Queue\n\n" + qmd)
        self.s.publish()

    def queue_cli(self):
        return subprocess.run([str(self.s.bin / "windvane"), "queue"], cwd=self.s.root, capture_output=True,
                              text=True).stdout

    def test_an_outside_item_gets_the_exact_done_line_to_add(self):
        self.s.ticket("T-002", "the parser rewrite")
        self.publish()
        out = self.queue_cli()
        self.assertIn("(waiting on: the parser rewrite)", out)
        self.assertIn("hint: `the parser rewrite` isn't a ticket: once it's done, add `- done: the parser rewrite "
                      "(why)` to .windvane/QUEUE.md, or take it out of `Depends on:` in .windvane/tickets/T-002.md",
                      out)
        self.publish("- done: the parser rewrite (merged upstream)\n")
        self.assertNotIn("hint:", self.queue_cli())

    def test_a_parked_dependency_says_which_line_to_take_out(self):
        self.s.ticket("T-002", "T-001")
        self.publish("- parked: T-001 (needs a design call)\n")
        out = self.queue_cli()
        self.assertRegex(out, r"T-002 +waiting .*\(waiting on: T-001\)\n +hint: T-001 is parked: to unpark it, take "
                              r"`- parked: T-001 \(needs a design call\)` out of \.windvane/QUEUE\.md\n")

    def test_a_dependency_set_aside_by_the_running_driver_says_so(self):
        self.s.ticket("T-002", "T-001")
        self.publish()
        p = Project(self.s.root)
        p.state.mkdir(parents=True, exist_ok=True)
        (p.state / "state.json").write_text(json.dumps({"aside": {"T-001": "hit its max turns"}}))
        self.assertNotIn("hint:", self.queue_cli())  # no run holds the lock: the next one starts afresh
        with open(p.state / "lock", "a+") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            out = self.queue_cli()
        self.assertIn("hint: T-001 was set aside this run (hit its max turns): T-002 waits until it merges", out)
        self.assertEqual(Queue(p).hints("T-003", {"T-001": "x"}), [])  # a ticket that doesn't wait gets none

    def test_an_unclosed_backtick_and_an_indented_key_are_warned_of_above_the_list(self):
        self.s.write(".windvane/tickets/T-001.md", "# T-001: x\nMilestone: `M9 · Depends on: T-003\nTouches: a.py.\n")
        self.s.write(".windvane/tickets/T-002.md", "# T-002: y `quoted\n  Depends on: T-003.\nTouches: b.py.\n")
        self.publish()
        out = self.queue_cli()
        warnings = [line for line in out.splitlines() if line.startswith("warning:")]
        self.assertEqual(warnings, [
            "warning: .windvane/tickets/T-001.md line 2: an unclosed backtick: keys after it on this line aren't read",
            "warning: .windvane/tickets/T-002.md line 2: `Depends on: T-003.` is indented: a key must start its line, "
            "so it isn't read"])
        self.assertTrue(out.index("warning:") < out.index("T-001 "))

    def test_a_clean_queue_prints_no_warnings_or_hints(self):
        self.s.ticket("T-002", "T-001")
        self.s.write(".windvane/tickets/T-003.md", "# T-003: Quote `Depends on: x`\nMilestone: `M9` · Depends on: "
                     "T-001,\n  T-002 · Can start later\nTouches: c.py.\n\nProse with a stray ` backtick.\n")
        self.publish()
        out = self.queue_cli()
        self.assertNotIn("warning", out)
        self.assertNotIn("hint:", out)
        self.assertIn("(waiting on: T-001, T-002)", out)

    def test_hints_stay_out_of_the_listing_sessions_read(self):
        self.s.ticket("T-002", "the user")
        self.publish()
        self.assertNotIn("hint:", Queue(Project(self.s.root)).listing())


class SetAsideNames(unittest.TestCase):
    def setUp(self):
        self.s = Sandbox()
        self.s.ticket("T-001")
        self.s.ticket("T-002", "T-001")
        self.s.ticket("T-003", "T-001")
        self.s.write(".windvane/QUEUE.md", "# Queue\n\n- parked: T-003 (later)\n")
        self.s.publish()
        self.p = Project(self.s.root)
        orig, driver.push = driver.push, lambda *a, **k: None
        self.addCleanup(setattr, driver, "push", orig)

    def tearDown(self):
        self.s.close()

    def test_the_log_and_summary_name_the_open_tickets_waiting_on_it(self):
        os.environ["FAKE_MAX_TURNS"] = "# windvane worker"
        self.addCleanup(os.environ.pop, "FAKE_MAX_TURNS", None)
        pilot = driver.Pilot(self.p, args())
        pilot.sleep, pilot.deadline = lambda s: None, now() + timedelta(hours=2)
        pilot.run(now() + timedelta(hours=2))
        log = (self.p.state / "autopilot.log").read_text()
        self.assertIn("T-001: the run hit its max turns; set aside; T-002 waits on it\n", log)  # T-003 is parked
        self.assertIn("Set aside: do T-001 (T-001) (hit its max turns; T-002 waits on it).", pilot.summary())
        self.assertEqual(json.loads((self.p.state / "state.json").read_text())["aside"],
                         {"T-001": "hit its max turns"})


if __name__ == "__main__":
    unittest.main()
