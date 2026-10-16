"""Slots under `parallel = "auto"`: as many tickets at once as their Touches and the usage window allow."""
import contextlib
import io
import json
import os
import sys
import threading
import time
import unittest
from argparse import Namespace
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from windvane import cli, driver  # noqa: E402
from windvane.config import Project, slots  # noqa: E402
from windvane.util import now  # noqa: E402
from helpers import Sandbox  # noqa: E402


def args(**kw):
    a = dict(cap=75, week_cap=95, ticket_hours=1, min_slot=0, answer_wait=10, replan=0, goal="", parallel="auto")
    a.update(kw)
    return Namespace(**a)


class Slots(unittest.TestCase):
    def setUp(self):
        self.s = Sandbox()
        os.environ["FAKE_WORK_S"] = "1.5"  # each worker run takes 1.5 s: side by side shows in the start times

    def tearDown(self):
        self.s.close()

    def tickets(self, **touches):
        for tid, paths in touches.items():
            self.s.ticket(tid.replace("_", "-"), touches=paths)
        self.s.publish()

    def run_pilot(self, **kw):
        self.p = Project(self.s.root)
        pilot = driver.Pilot(self.p, args(**kw))
        pilot.run(now() + timedelta(minutes=40))  # 10 min a ticket until measured: 2 or more need slots
        return pilot

    def starts(self):
        """Ticket -> when its first worker run started."""
        out = {}
        for l in (self.s.tmp / "calls.jsonl").read_text().splitlines():
            c = json.loads(l)
            if c["system"] == "span":
                tid, _, at = c["prompt"].split()
                out.setdefault(tid, float(at))
        return out

    def log(self):
        return (self.p.state / "autopilot.log").read_text()

    def test_three_disjoint_tickets_run_at_once(self):
        os.environ["FAKE_WORK_S"] = "4"  # room for a slow machine's starts, and still under one worker run
        self.tickets(T_001="a.py", T_002="docs/", T_003="tests/test_c.py")
        pilot = self.run_pilot()
        self.assertEqual(sorted(pilot.merged), ["T-001", "T-002", "T-003"])
        at = self.starts().values()
        self.assertLess(max(at) - min(at), 3.0)  # all three started before the first finished
        self.assertTrue((self.p.state / "work-2").exists())
        self.assertFalse((self.p.state / "work-3").exists())  # worktrees only for the slots used
        self.assertIn("T-003: slot 2, beside T-001, T-002", self.log())
        self.assertIn("up to 4 tickets at a time", self.log())

    def test_a_ticket_sharing_a_file_waits_and_the_log_names_the_file(self):
        self.tickets(T_001="windvane/a.py, windvane/b.py", T_002="windvane/", T_003="docs/")
        pilot = self.run_pilot()
        self.assertEqual(sorted(pilot.merged), ["T-001", "T-002", "T-003"])
        at = self.starts()
        self.assertLess(abs(at["T-003"] - at["T-001"]), 1.0)
        self.assertGreater(at["T-002"] - at["T-001"], 1.4)  # only after T-001 merged
        self.assertEqual(self.log().count("T-002 waits: overlaps T-001 on windvane/a.py, windvane/b.py"), 1)

    def test_a_ticket_without_touches_runs_alone_and_says_so(self):
        self.tickets(T_001="a.py", T_002="")
        self.run_pilot()
        at = self.starts()
        self.assertGreater(at["T-002"] - at["T-001"], 1.4)
        self.assertIn("T-002 waits: no Touches line, so it runs alone", self.log())

    def test_the_usage_estimate_holds_back_a_slot(self):
        self.tickets(T_001="a.py", T_002="b.py", T_003="c.py")
        pilot = self.run_pilot(cap=30)  # the fake reports 10% used, 8% a ticket before any is measured: room for 2
        self.assertEqual(sorted(pilot.merged), ["T-001", "T-002", "T-003"])
        at = self.starts()
        self.assertLess(abs(at["T-002"] - at["T-001"]), 1.0)
        self.assertGreater(at["T-003"] - at["T-001"], 1.4)
        self.assertEqual(self.log().count("no 3rd slot: usage estimate (10% + 3 x 8% > cap 30%)"), 1)

    def test_a_ticket_merged_this_run_is_not_picked_again_from_a_queue_that_lags(self):
        """A slot can merge and free itself after the pass's fetch: that queue still shows its ticket to do. Picked
        again, it merged and was picked again for as long as the race kept winning (173 times in one CI run)."""
        self.tickets(T_001="a.py", T_002="b.py")
        self.p = Project(self.s.root)
        pilot = driver.Pilot(self.p, args())
        pilot.queue = driver.Queue(self.p)  # fetched before T-001's merge landed
        self.assertEqual(pilot.queue.status("T-001"), "todo")
        pilot.merged.append("T-001")
        self.assertEqual(pilot.pick([0])[0], "T-002")

    def test_parallel_1_runs_one_at_a_time(self):
        self.tickets(T_001="a.py", T_002="b.py", T_003="c.py")
        pilot = self.run_pilot(parallel=1)
        self.assertEqual(sorted(pilot.merged), ["T-001", "T-002", "T-003"])
        at = sorted(self.starts().values())
        self.assertTrue(all(b - a >= 1.4 for a, b in zip(at, at[1:])))
        self.assertFalse((self.p.state / "work-1").exists())

    def test_status_shows_the_running_tickets_and_their_slots(self):
        os.environ["FAKE_WORK_S"] = "3"
        self.tickets(T_001="a.py", T_002="b.py")
        t = threading.Thread(target=self.run_pilot)
        t.start()
        state = self.s.tmp / "state/windvane/sandbox/state.json"
        for _ in range(100):
            if state.exists() and len(json.loads(state.read_text()).get("running", {})) == 2:
                break
            time.sleep(0.05)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.cmd_status(Namespace(line=False), Project(self.s.root))
        t.join()
        self.assertIn("slot 0: T-001\nslot 1: T-002\n", out.getvalue())
        self.assertEqual(json.loads(state.read_text())["running"], {})  # cleared when the run ends

    def test_slot_count(self):
        p = Project(self.s.root)
        self.assertEqual(slots(p), 4)  # the default: auto, up to max_parallel
        self.assertEqual(slots(p, 2), 2)
        p.ap["max_parallel"] = 6
        self.assertEqual(slots(p, "auto"), 6)
        with self.assertRaisesRegex(SystemExit, 'parallel = .two. in config.toml: use a number of tickets or "auto"'):
            slots(p, "two")
        p.cfg["isolate"] = False
        self.assertEqual(slots(p, "auto"), 1)  # one checkout: one ticket

    def test_shared_names_the_common_paths(self):
        self.assertEqual(driver.shared({"windvane/", "README.md"}, {"windvane/driver.py", "x"}),
                         ["windvane/driver.py"])
        self.assertEqual(driver.shared({"tests/test_a.py"}, {"tests/test_b.py"}), [])


if __name__ == "__main__":
    unittest.main()
