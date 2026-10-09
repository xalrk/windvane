"""T-127: `stats` shows the time lost waiting (await and the heavy lock, by role), the not-run checks that bounced
nothing, and in `--critic` what replies with no verdict and their re-asks cost; each line only when it has a count.
The heavy check and `await` write the rows it reads."""
import io
import os
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from windvane import cli, jobs  # noqa: E402
from windvane.config import Project  # noqa: E402
from windvane.runner import ROLE, TICKET  # noqa: E402
from windvane.util import append_jsonl, read_jsonl  # noqa: E402
from helpers import Sandbox  # noqa: E402
from test_heavy_check_wait import hold, machine  # noqa: E402


class StatsWaits(unittest.TestCase):
    def setUp(self):
        self.s = Sandbox()
        self.addCleanup(self.s.close)
        self.s.ticket("T-001")
        self.s.publish()
        self.p = Project(self.s.root)
        self.ledger = self.p.state / "ledger.jsonl"
        self.row(role="worker", ticket="T-001", label="T-001", input=1000, turns=3)

    def row(self, **kw):
        append_jsonl(self.ledger, kw)

    def cli(self, *argv):
        out, here = io.StringIO(), os.getcwd()
        self.addCleanup(os.chdir, here)
        with redirect_stdout(out):
            cli.main(["-C", str(self.s.root), *argv])
        return out.getvalue()

    def test_waits_by_role_and_the_not_run_count(self):
        self.row(event="await", ticket="T-001", role="worker", name="check", seconds=480)
        self.row(event="await", ticket="T-001", role="worker", name="check", seconds=120)
        self.row(event="await", ticket="T-002", role="reviewer", name="build", seconds=90)
        self.row(event="check", ticket="T-001", role="driver", minutes=9, passed=True, reused=False, waited_s=300)
        self.row(event="check", ticket="T-001", role="worker", minutes=2, passed=False, reused=False, not_run=True,
                 waited_s=120)
        self.row(event="check", ticket="T-002", minutes=3, passed=False, reused=False, not_run=True)  # an older row
        out = self.cli("stats")
        self.assertIn("waits, worker: 10 min in await, 2.0 min for the heavy lock\n", out)
        self.assertIn("waits, driver: 8.0 min for the heavy lock\n", out)  # 5 min, and the old not-run row's 3
        self.assertIn("waits, reviewer: 1.5 min in await\n", out)
        self.assertLess(out.index("waits, worker"), out.index("waits, driver"))  # the longest wait first
        self.assertIn("not-run checks: 2 (no bounce)\n", out)
        self.assertIn("checks: 1 run, 9.0 min", out)

    def test_no_wait_and_no_not_run_check_print_nothing(self):
        self.row(event="check", ticket="T-001", role="driver", minutes=4, passed=True, reused=False)
        self.row(event="await", ticket="T-001", role="worker", name="check", seconds=0)
        out = self.cli("stats")
        self.assertIn("checks: 1 run", out)
        self.assertNotIn("waits", out)
        self.assertNotIn("not-run", out)

    def test_critic_no_verdicts_and_reasks_with_their_tokens(self):
        crit = self.p.state / "critic.jsonl"
        append_jsonl(crit, {"ticket": "T-001", "sha": "a", "score": 8, "step": "pass", "revisions": 0})
        append_jsonl(crit, {"ticket": "T-004", "sha": "b", "score": 9, "step": "pass", "reasked": True})  # saved
        append_jsonl(crit, {"ticket": "T-002", "step": "none", "why": "x", "reasked": True})
        append_jsonl(crit, {"ticket": "T-003", "step": "none", "why": "x", "reasked": False, "reask_failed": "API"})
        self.row(role="critic", ticket="T-002", label="T-002-critic", input=50000)  # the first ask: not a re-ask
        self.row(role="critic", ticket="T-002", label="T-002-critic-reask", input=2000, cache_read=100000)  # 12k
        self.row(role="critic", ticket="T-003", label="T-003-critic-reask", input=1000, output=200)  # 2k
        self.row(role="critic", ticket="T-004", label="T-004-critic-reask", input=3000)  # 3k
        out = self.cli("stats", "--critic")
        self.assertIn("no verdict: 2 critic runs (step: none)\n", out)
        self.assertIn("re-asks: 1 saved a verdict, 1 still no verdict, 1 failed (3 sessions, 17k weighted tokens)\n", out)

    def test_critic_lines_absent_without_any(self):
        append_jsonl(self.p.state / "critic.jsonl", {"ticket": "T-001", "sha": "a", "score": 8, "step": "pass"})
        out = self.cli("stats", "--critic")
        self.assertIn("final scores", out)
        self.assertNotIn("no verdict", out)
        self.assertNotIn("re-asks", out)

    def test_a_none_without_a_reask_shows_only_its_count(self):
        append_jsonl(self.p.state / "critic.jsonl", {"ticket": "T-002", "step": "none", "why": "x", "reasked": False})
        out = self.cli("stats", "--critic")
        self.assertIn("no critic verdicts yet\nno verdict: 1 critic run (step: none)\n", out)
        self.assertNotIn("re-asks", out)


class RowsWritten(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="windvane-")
        self.addCleanup(tmp.cleanup)
        self.d = Path(tmp.name)
        patch = mock.patch.dict(os.environ, {**machine(self.d, 1), TICKET: "T-001", ROLE: "worker"})
        patch.start()
        self.addCleanup(patch.stop)
        os.environ.pop("WINDVANE_HEAVY_HELD", None)
        (self.d / "work").mkdir()
        self.p = SimpleNamespace(state=self.d / "state", cfg={"check": "echo checked", "heavy_check": True})

    def ends(self):
        return [r for r in read_jsonl(self.p.state / "ledger.jsonl") if r["event"] == "check"]

    def test_a_heavy_check_that_waited_says_how_long(self):
        hold(self, self.d)
        threading.Timer(1.5, (self.d / "release").touch).start()
        ok, _, log = jobs.check(self.p, self.d / "work")
        self.assertIs(ok, True)
        self.assertRegex(log.read_text(), r"heavy: waited \d+ s for the machine\nchecked\n")
        [end] = self.ends()
        self.assertEqual(end["role"], "worker")
        self.assertGreaterEqual(end["waited_s"], 1)
        self.assertLessEqual(end["waited_s"], round(end["minutes"] * 60) + 1)

    def test_a_check_that_didnt_wait_has_no_wait(self):
        jobs.check(self.p, self.d / "work")
        [end] = self.ends()
        self.assertNotIn("waited_s", end)

    def test_a_not_run_check_waited_all_its_time(self):
        hold(self, self.d)
        with mock.patch.dict(os.environ, machine(self.d, 0.02)):  # gives up after 1.2 s
            ok, _, _ = jobs.check(self.p, self.d / "work")
        self.assertIsNone(ok)
        [end] = self.ends()
        self.assertTrue(end["not_run"])
        self.assertEqual(end["waited_s"], round(end["minutes"] * 60))

    def test_await_logs_the_seconds_it_blocked_under_the_sessions_role(self):
        with redirect_stdout(io.StringIO()):
            jobs.bg(self.p, "nap", ["sleep", "1.5"], self.d / "work")
            jobs.await_(self.p, "nap")
        [row] = [r for r in read_jsonl(self.p.state / "ledger.jsonl") if r["event"] == "await"]
        self.assertEqual((row["role"], row["ticket"], row["name"]), ("worker", "T-001", "nap"))
        self.assertGreaterEqual(row["seconds"], 1)
        with redirect_stdout(io.StringIO()):
            jobs.await_(self.p, "nap")  # already done: no time blocked, no row
        self.assertEqual(sum(r["event"] == "await" for r in read_jsonl(self.p.state / "ledger.jsonl")), 1)


if __name__ == "__main__":
    unittest.main()
