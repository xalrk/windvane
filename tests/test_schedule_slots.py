"""How many tickets run side by side: conflicts cost a slot, and a queue that finishes one at a time before the
deadline gets no extra slot unless the usage window is under half the cap. Apart from test_schedule, so the check's
pool runs the two at once."""
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from helpers import no_wrap_up  # noqa: E402
from test_schedule import AFTER, SIDE_BY_SIDE, Base  # noqa: E402


class Slots(Base):
    def test_two_conflict_bounces_drop_a_slot(self):
        os.environ["FAKE_CONFLICT"] = "1"
        os.environ["FAKE_BARRIER"] = "3"  # T-003 starting after T-001 merged would fork past it and not conflict
        self.ticket("T-001", "a.py")
        self.ticket("T-002", "b.py")
        self.ticket("T-003", "c.py")
        no_wrap_up(self)
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
        self.assertLess(abs(at["T-002"] - at["T-001"]), SIDE_BY_SIDE)
        self.assertEqual(pilot.peak, 2)
        self.assertNotIn("no 2nd slot", self.log())


    def test_a_short_queue_with_a_far_deadline_stays_at_one_slot_past_half_the_cap(self):
        os.environ["FAKE_FIVE"] = "0.4"  # 40% of the window, cap 75: no headroom, and one at a time fits
        self.ticket("T-001", "a.py")
        self.ticket("T-002", "b.py")
        pilot = self.run_pilot(minutes=600)
        self.assertEqual(sorted(pilot.merged), ["T-001", "T-002"])
        at = self.start_times()
        self.assertGreater(at["T-002"] - at["T-001"], AFTER)
        self.assertEqual(pilot.peak, 1)
        self.assertEqual(self.log().count("no 2nd slot: 2 tickets x 10 min fit before"), 1)



if __name__ == "__main__":
    unittest.main()
