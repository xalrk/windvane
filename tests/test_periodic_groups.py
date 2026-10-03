"""[[periodic]] `after_group`: a session when a group of tickets (a milestone) is finished, not only every N merges."""
import argparse
import contextlib
import io
import json
import sys
import unittest
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from windvane import cli, driver  # noqa: E402
from windvane.config import Project, periodic  # noqa: E402
from windvane.util import now  # noqa: E402
from helpers import Sandbox  # noqa: E402
from test_pipeline import args  # noqa: E402

GROUPS = ('[[periodic]]\nname = "playtest"\nminutes = 30\nprompt = "tools/PLAYTEST.md"\n'
          "after_group = 'Milestone:\\s*(M\\d+)'\n")
FINISHED = "## Finished since the last run: "


class Groups(unittest.TestCase):
    def make(self, tickets, config=GROUPS, queue=""):
        """`tickets`: (id, milestone or "", touches or "") each, in order: each depends on nothing."""
        self.s = Sandbox(config)
        for tid, group, touches in tickets:
            self.s.write(f".windvane/tickets/{tid}.md", f"# {tid}: do {tid}\nDepends on: nothing.\n"
                         + (f"Milestone: {group} · Can start now\n" if group else "")
                         + (f"Touches: {touches}.\n" if touches else ""))
        if queue:
            self.s.write(".windvane/QUEUE.md", f"# Queue\n{queue}")
        self.s.write("tools/PLAYTEST.md", "# Playtest\nPLAY\n")
        self.s.publish()
        self.p = Project(self.s.root)
        self.addCleanup(self.s.close)

    def run_pilot(self, pilot=None, **kw):
        pilot = pilot or driver.Pilot(self.p, args(**kw))
        pilot.run(now() + timedelta(hours=1))
        return pilot

    def sessions(self):
        """Each session in order: the ticket a worker ran, or the periodic session's prompt."""
        f = self.s.tmp / "calls.jsonl"
        out = []
        for c in map(json.loads, f.read_text().splitlines() if f.exists() else []):
            if c["system"].startswith("# windvane periodic"):
                out.append(c["prompt"])
            elif c["system"].startswith("# windvane worker"):
                out.append(c["prompt"].split()[1].rstrip("."))
        return out

    def finished_line(self, prompt):
        return next((line for line in prompt.splitlines() if line.startswith(FINISHED)), None)

    def test_the_last_open_ticket_of_a_group_runs_it_once_naming_the_group(self):
        self.make([("T-001", "M1", ""), ("T-002", "M1", ""), ("T-003", "M2", ""), ("T-004", "", "")],
                  queue="- T-003\n- T-001\n- T-002\n- T-004\n")
        pilot = self.run_pilot()
        self.assertEqual(pilot.merged, ["T-003", "T-001", "T-002", "T-004"])
        got = self.sessions()
        self.assertEqual([x if x.startswith("T-") else "periodic" for x in got],
                         ["T-003", "periodic", "T-001", "T-002", "periodic", "T-004"])
        self.assertEqual(self.finished_line(got[1]), FINISHED + "M2 (1 ticket: T-003)")
        self.assertEqual(self.finished_line(got[4]), FINISHED + "M1 (2 tickets: T-001, T-002)")

    def test_a_group_finished_at_the_start_never_triggers(self):
        self.make([("T-001", "M1", ""), ("T-002", "", "")], queue="- done: T-001 (by hand)\n")
        self.run_pilot()
        self.assertEqual(self.sessions(), ["T-002"])

    def test_parked_tickets_dont_hold_a_group_open(self):
        self.make([("T-001", "M1", ""), ("T-002", "M1", ""), ("T-003", "", "")],
                  queue="- parked: T-002 (for the desk)\n")
        self.run_pilot()
        got = self.sessions()
        self.assertEqual([x if x.startswith("T-") else "periodic" for x in got], ["T-001", "periodic", "T-003"])
        self.assertEqual(self.finished_line(got[1]), FINISHED + "M1 (2 tickets: T-001, T-002 (parked))")

    def test_two_groups_finishing_together_give_one_session(self):
        self.make([("T-001", "M1", "a.py"), ("T-002", "M2", "b.py")])
        pilot = self.run_pilot(parallel=2)
        self.assertEqual(pilot.peak, 2)  # they ran side by side: the session waited for both
        got = self.sessions()
        self.assertEqual(sorted(got[:2]), ["T-001", "T-002"])
        self.assertEqual(len(got), 3)
        self.assertEqual(self.finished_line(got[2]), FINISHED + "M1 (1 ticket: T-001); M2 (1 ticket: T-002)")

    def test_restarts_carry_what_was_seen(self):
        self.make([("T-001", "M1", ""), ("T-002", "M2", "")])
        pilot = driver.Pilot(self.p, args())
        self.assertIsNone(pilot.periodic_due())  # the first look only notes what is finished
        self.s.write(".windvane/QUEUE.md", "# Queue\n- done: T-001 (by hand)\n")
        self.s.publish("done")
        self.assertEqual(pilot.periodic_due()["name"], "playtest")
        pilot.hand_over()
        again = driver.Pilot(self.p, args())
        again.carry_over(True)  # a restart: M1 finished in the run it replaced and still waits for its session
        self.assertEqual(again.periodic_due()["name"], "playtest")
        again.periodic_ran(again.p.periodic["playtest"])
        again.hand_over()
        third = driver.Pilot(self.p, args())
        third.carry_over(True)
        self.assertIsNone(third.periodic_due())  # it ran: a restart doesn't run it again
        fresh = driver.Pilot(self.p, args())
        fresh.carry_over(False)
        self.assertIsNone(fresh.periodic_due())  # a new run: M1 was finished at its start

    def test_a_run_restarted_after_the_session_doesnt_run_it_again(self):
        self.make([("T-001", "M1", "")])
        pilot = self.run_pilot()
        self.assertEqual(len(self.sessions()), 2)
        pilot.hand_over()
        again = driver.Pilot(self.p, args())
        again.carry_over(True)
        self.assertIsNone(again.periodic_due())

    def test_every_alone_behaves_as_before(self):
        every = GROUPS.replace("after_group = 'Milestone:\\s*(M\\d+)'\n", "every = 2\n")
        self.make([("T-001", "M1", ""), ("T-002", "M2", ""), ("T-003", "M2", "")], config=every)
        self.run_pilot()
        got = self.sessions()
        self.assertEqual([x if x.startswith("T-") else "periodic" for x in got], ["T-001", "T-002", "periodic", "T-003"])
        self.assertIsNone(self.finished_line(got[2]))

    def test_every_and_after_group_together_one_run_resets_both(self):
        self.make([("T-001", "M1", ""), ("T-002", "M1", ""), ("T-003", "", ""), ("T-004", "", "")],
                  config=GROUPS + "every = 2\n")
        self.run_pilot()
        got = self.sessions()
        self.assertEqual([x if x.startswith("T-") else "periodic" for x in got],
                         ["T-001", "T-002", "periodic", "T-003", "T-004", "periodic"])
        self.assertIsNotNone(self.finished_line(got[2]))
        self.assertIsNone(self.finished_line(got[5]))  # due by count alone

    def test_the_queue_line_shows_the_closest_groups(self):
        tickets = [(f"T-{n:03d}", f"M{g}", "") for n, g in enumerate([1, 1, 1, 2, 3, 3, 4, 5, 6, 7, 7, 10, 8], 1)]
        self.make(tickets, queue="- done: T-007 (by hand)\n- parked: T-012 (later)\n")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.cmd_queue(argparse.Namespace(), self.p)
        self.assertEqual(out.getvalue().splitlines()[-1], "playtest runs when a group finishes: M2 (1 left), "
                         "M5 (1 left), M6 (1 left), M8 (1 left), M3 (2 left), and 2 more")


class Config(unittest.TestCase):
    def test_a_bad_pattern_stops_at_load(self):
        for bad in ("Milestone: (M\\d+", "Milestone: M\\d+"):  # broken; no group to name the ticket's group
            with self.assertRaisesRegex(SystemExit, "after_group"):
                periodic([{"name": "a", "prompt": "x.md", "after_group": bad}])
        self.assertEqual(periodic([{"name": "a", "prompt": "x.md"}])["a"]["after_group"], "")


if __name__ == "__main__":
    unittest.main()
