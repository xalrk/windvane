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
from windvane.util import now  # noqa: E402
from helpers import Sandbox  # noqa: E402
from test_pipeline import args  # noqa: E402


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

    def test_a_short_ticket_hours_gates_it_too(self):
        pilot = driver.Pilot(self.p, args(min_slot=0, ticket_hours=0.3))  # 18 minutes: its wrap-up is already past
        pilot.run(now() + timedelta(hours=2))
        self.assertEqual(self.workers(), [])
        self.assertIn("no time left for a worker", pilot.stop_reason)

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
        self.addCleanup(os.chdir, os.getcwd())
        err = io.StringIO()
        with redirect_stderr(err), self.assertRaises(SystemExit) as e:
            cli.main(["-C", str(self.s.root), "run", "--for", "10m", "--min-slot", "0"])
        self.assertIn("a worker starts only with its wrap-up (20 minutes before the stop) at least 5 minutes ahead",
                      str(e.exception.code))

    def test_inside_min_slot_it_suggests_lowering_it_only_when_that_would_help(self):
        self.addCleanup(os.chdir, os.getcwd())
        for span, hint in (("20m", False), ("40m", True)):
            with self.assertRaises(SystemExit) as e:
                cli.main(["-C", str(self.s.root), "run", "--for", span, "--min-slot", "45"])
            self.assertIn("(min_slot), so this run would do nothing", str(e.exception.code))
            self.assertEqual("--min-slot 0" in str(e.exception.code), hint, span)


if __name__ == "__main__":
    unittest.main()
