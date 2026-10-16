"""A worker starts only with its wrap-up still ahead: one launched after it ends PARTIAL at once, and each such
launch spends a worker's startup for nothing and counts toward setting a good ticket aside."""
import io
import json
import os
import re
import sys
import unittest
from contextlib import redirect_stderr
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from windvane import cli, driver  # noqa: E402
from windvane.config import Project  # noqa: E402
from windvane.queue import Queue  # noqa: E402
from windvane.util import now  # noqa: E402
from helpers import Sandbox  # noqa: E402
from test_pipeline import args  # noqa: E402

ROOT = str(Path(__file__).resolve().parents[1])


class WrapUpGate(unittest.TestCase):
    def setUp(self):
        self.s = Sandbox()
        self.addCleanup(self.s.close)
        self.s.ticket("T-001")
        self.s.publish()
        self.p = Project(self.s.root)
        os.environ["FAKE_WORKER"] = "partial"

    def workers(self):
        f = self.s.tmp / "calls.jsonl"
        calls = [json.loads(line) for line in f.read_text().splitlines()] if f.exists() else []
        return [c for c in calls if c["system"].startswith("# windvane worker run")]

    def test_no_worker_starts_with_its_wrap_up_behind_it(self):
        pilot = driver.Pilot(self.p, args(min_slot=0))
        pilot.run(now() + timedelta(minutes=10))
        self.assertEqual(self.workers(), [])
        self.assertEqual(pilot.partials, {})
        self.assertNotIn("T-001", pilot.aside)
        self.assertIn("no time left for a worker", pilot.stop_reason)

    def test_a_resumed_ticket_is_gated_too(self):
        self.s.git("switch", "-q", "-c", "t-001-work")
        self.s.write("T-001.txt", "begun\n")
        self.s.git("add", "-A")
        self.s.git("commit", "-qm", "T-001: begun")
        self.s.git("push", "-q", "origin", "t-001-work")
        self.s.git("switch", "-q", "main")
        self.assertEqual(Queue(self.p).status("T-001"), "wip")
        pilot = driver.Pilot(self.p, args(min_slot=0))
        pilot.run(now() + timedelta(minutes=10))
        self.assertEqual((self.workers(), pilot.partials), ([], {}))
        self.assertIn("no time left for a worker", pilot.stop_reason)

    def test_a_short_ticket_hours_is_named_as_the_cause(self):
        pilot = driver.Pilot(self.p, args(min_slot=0, ticket_hours=0.3))  # 18 minutes: its wrap-up is already past
        pilot.run(now() + timedelta(hours=2))
        self.assertEqual(self.workers(), [])
        self.assertEqual(pilot.stop_reason, "ticket_hours (18 min) leaves a worker no time: it needs over 25 min, "
                                            "as it wraps up 20 min before it's stopped")

    def test_a_late_ticket_waits_for_a_running_one_then_the_run_stops(self):
        os.environ.pop("FAKE_WORKER")
        os.environ["FAKE_SLOW"] = "T-001=2"  # still running when the second slot picks T-002
        self.s.ticket("T-001", touches="a.py")  # one without Touches runs alone
        self.s.ticket("T-002", touches="b.py")
        self.s.publish()
        pilot = driver.Pilot(Project(self.s.root), args(min_slot=0, parallel=2))
        pilot.no_worker_time = lambda: "too late" if pilot.began or pilot.merged else ""  # once T-001 has started
        lines, log = [], pilot.log
        pilot.log = lambda msg: (lines.append(msg), log(msg))
        pilot.run(now() + timedelta(hours=1))
        self.assertIn("T-002 waits: too late", lines)
        self.assertEqual(pilot.merged, ["T-001"])  # the running ticket went on to its merge
        self.assertEqual([re.match(r"Ticket (\S+)\.", c["prompt"]).group(1) for c in self.workers()], ["T-001"])
        self.assertEqual((pilot.stop_reason, pilot.partials), ("too late", {}))

    def test_a_worker_with_time_before_its_wrap_up_starts(self):
        os.environ.pop("FAKE_WORKER")
        started = now()
        pilot = driver.Pilot(self.p, args(min_slot=0))
        pilot.run(started + timedelta(minutes=27))
        calls = self.workers()
        self.assertTrue(calls)
        wrap = re.search(r"Wrap-up time: (\d\d:\d\d)", calls[0]["prompt"]).group(1)
        at = datetime.combine(started.date(), datetime.strptime(wrap, "%H:%M").time())
        if at < started - timedelta(hours=12):  # the wrap-up is past midnight
            at += timedelta(days=1)
        self.assertGreater(at, started.replace(second=0, microsecond=0))

    def test_a_run_too_short_for_any_worker_is_refused(self):
        self.addCleanup(os.chdir, ROOT)  # (the cwd may be a sandbox an earlier test deleted)
        err = io.StringIO()
        with redirect_stderr(err), self.assertRaises(SystemExit) as e:
            cli.main(["-C", str(self.s.root), "run", "--for", "10m", "--min-slot", "0"])
        self.assertIn("a worker starts only with its wrap-up (20 minutes before the stop) at least 5 minutes ahead",
                      str(e.exception.code))

    def test_a_ticket_hours_too_short_for_any_worker_is_refused(self):
        self.addCleanup(os.chdir, ROOT)  # (the cwd may be a sandbox an earlier test deleted)
        with self.assertRaises(SystemExit) as e:
            cli.main(["-C", str(self.s.root), "run", "--for", "2h", "--ticket-hours", "0.3"])
        self.assertEqual(e.exception.code, "ticket_hours is 18 minutes, but a worker needs at least 25: it wraps up "
                         "20 minutes before it's stopped, and starts only with that 5 or more minutes ahead. Raise it "
                         "(e.g. --ticket-hours 1)")

    def test_inside_min_slot_it_suggests_lowering_it_only_when_that_would_help(self):
        self.addCleanup(os.chdir, ROOT)  # (the cwd may be a sandbox an earlier test deleted)
        for span, hint in (("20m", False), ("40m", True)):
            with self.assertRaises(SystemExit) as e:
                cli.main(["-C", str(self.s.root), "run", "--for", span, "--min-slot", "45"])
            self.assertIn("(min_slot), so this run would do nothing", str(e.exception.code))
            self.assertEqual("--min-slot 0" in str(e.exception.code), hint, span)


if __name__ == "__main__":
    unittest.main()
