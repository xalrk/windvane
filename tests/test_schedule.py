"""Which ready ticket starts beside the running ones: related work waits, conflicts cost a slot, and a queue that
finishes one at a time before the deadline gets no extra slot unless the usage window is under half the cap."""
import json
import os
import sys
import time
import unittest
from argparse import Namespace
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from windvane import driver, questions  # noqa: E402
from windvane.config import Project  # noqa: E402
from windvane.queue import Queue  # noqa: E402
from windvane.util import now  # noqa: E402
from helpers import Sandbox  # noqa: E402
from test_parallel import args  # noqa: E402


class Schedule(unittest.TestCase):
    def setUp(self):
        self.s = Sandbox()
        os.environ["FAKE_WORK_S"] = "1.5"

    def tearDown(self):
        self.s.close()

    def ticket(self, tid, touches, body="", related=""):
        self.s.write(f".windvane/tickets/{tid}.md", f"# {tid}: do {tid}\nDepends on: nothing.\n"
                     + (f"Related: {related}.\n" if related else "") + f"Touches: {touches}.\n\n## Goal\n{body}\n")

    def run_pilot(self, minutes=15, **kw):
        self.s.publish()
        self.p = Project(self.s.root)
        pilot = driver.Pilot(self.p, args(**kw))
        pilot.run(now() + timedelta(minutes=minutes))
        return pilot

    def start_times(self):
        out = {}
        for l in (self.s.tmp / "calls.jsonl").read_text().splitlines():
            c = json.loads(l)
            if c["system"] == "span":
                tid, _, at = c["prompt"].split()
                out.setdefault(tid, float(at))
        return out

    def log(self):
        return (self.p.state / "autopilot.log").read_text()

    def test_a_ticket_naming_a_running_tickets_file_waits_and_a_lower_one_starts(self):
        self.ticket("T-001", "windvane/critic.py")
        self.ticket("T-002", "windvane/cli.py", "Builds on the gate in windvane/critic.py.")
        self.ticket("T-003", "docs/x.md", "Unrelated: windvane/critic.pyc and data/windvane/critic.py don't count.")
        pilot = self.run_pilot()
        self.assertEqual(sorted(pilot.merged), ["T-001", "T-002", "T-003"])
        at = self.start_times()
        self.assertLess(abs(at["T-003"] - at["T-001"]), 1.0)  # the lower, unrelated ticket took the slot
        self.assertGreater(at["T-002"] - at["T-001"], 1.4)  # only after T-001 merged
        self.assertEqual(self.log().count("T-002 waits: names windvane/critic.py, which T-001 changes"), 1)

    def test_related_tickets_and_named_ids_wait(self):
        self.ticket("T-001", "a.py")
        self.ticket("T-002", "b.py", related="T-001")
        self.ticket("T-003", "c.py", "Extends what T-001 adds.")
        self.ticket("T-004", "d.py", "Mentions T-0011 and xa.py only.")
        self.s.publish()
        pilot = driver.Pilot(Project(self.s.root), args())
        pilot.queue = Queue(pilot.p)
        self.assertEqual(pilot.queue.related["T-002"], ["T-001"])
        self.assertEqual(pilot.clash("T-002", ["T-001"]), "related to T-001")
        self.assertEqual(pilot.clash("T-001", ["T-002"]), "related to T-002")  # either side's line holds
        self.assertEqual(pilot.clash("T-003", ["T-001"]), "names T-001, which is running")
        self.assertEqual(pilot.clash("T-004", ["T-001"]), "")

    def test_two_conflict_bounces_drop_a_slot(self):
        os.environ["FAKE_CONFLICT"] = "1"
        os.environ["FAKE_BARRIER"] = "3"  # T-003 starting after T-001 merged would fork past it and not conflict
        self.ticket("T-001", "a.py")
        self.ticket("T-002", "b.py")
        self.ticket("T-003", "c.py")
        pilot = self.run_pilot(minutes=15)  # 3 x 10 min one at a time would miss it: all three start
        self.assertEqual(sorted(pilot.merged), ["T-001", "T-002", "T-003"])
        self.assertGreaterEqual(pilot.conflicts, 2)
        self.assertEqual(pilot.limit, 2)
        self.assertIn("2 conflict bounces between side-by-side tickets: at most 2 at a time for the rest of the run",
                      self.log())
        self.assertIn("Side by side: up to 3 at once,", self.log())

    def test_usage_under_half_the_cap_opens_a_slot_even_when_the_queue_fits(self):
        self.ticket("T-001", "a.py")
        self.ticket("T-002", "b.py")
        pilot = self.run_pilot(minutes=600)  # 10% of the 5-hour window, cap 75: headroom to spend on speed
        self.assertEqual(sorted(pilot.merged), ["T-001", "T-002"])
        at = self.start_times()
        self.assertLess(abs(at["T-002"] - at["T-001"]), 1.0)
        self.assertEqual(pilot.peak, 2)
        self.assertNotIn("no 2nd slot", self.log())

    def test_a_short_queue_with_a_far_deadline_stays_at_one_slot_past_half_the_cap(self):
        os.environ["FAKE_FIVE"] = "0.4"  # 40% of the window, cap 75: no headroom, and one at a time fits
        self.ticket("T-001", "a.py")
        self.ticket("T-002", "b.py")
        pilot = self.run_pilot(minutes=600)
        self.assertEqual(sorted(pilot.merged), ["T-001", "T-002"])
        at = self.start_times()
        self.assertGreater(at["T-002"] - at["T-001"], 1.4)
        self.assertEqual(pilot.peak, 1)
        self.assertEqual(self.log().count("no 2nd slot: 2 tickets x 10 min fit before"), 1)

    def test_ticket_minutes_count_only_running_time(self):
        """A late answer to a question isn't time the ticket took: the slot gate would think tickets are slower."""
        os.environ["FAKE_WORKER"] = "question"
        self.ticket("T-001", "a.py")
        self.s.publish()
        self.p = Project(self.s.root)
        pilot = driver.Pilot(self.p, args(answer_wait=60))
        asked, poll = {}, pilot.poll_questions

        def answer_late():
            for tid in pilot.questions:
                if time.time() - asked.setdefault(tid, time.time()) > 6:
                    questions.give_answer(self.p, tid, "B")
            poll()
        pilot.poll_questions = answer_late
        sleep = time.sleep
        self.addCleanup(setattr, time, "sleep", sleep)
        time.sleep = lambda sec: sleep(min(sec, 0.2))  # the idle wait for an answer polls often
        pilot.run(now() + timedelta(minutes=15))
        self.assertEqual(pilot.merged, ["T-001"])
        self.assertGreater(time.time() - asked["T-001"], 6)
        self.assertLess(pilot.minutes[-1] * 60, 6)  # two worker attempts and the checks, not the wait

    def test_names_matches_whole_paths(self):
        self.assertTrue(driver.names("see `a.py`.", "a.py"))
        self.assertFalse(driver.names("data.py, a.py.bak, x/a.py, a.pyc", "a.py"))
        self.assertTrue(driver.names("docs/guide.md", "docs/"))


if __name__ == "__main__":
    unittest.main()
