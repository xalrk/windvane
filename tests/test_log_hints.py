"""Hints that send the user to the log name it and stay true (T-209): the queue's failed hint says which file, `stats`
says when there is no log rather than print a grep that can't work, its grep finds the very line the driver writes
for a partial, a token count under 1,000 never reads as `0k`, and the max-turns push says a new limit needs a fresh
`windvane run`."""
import io
import json
import os
import re
import sys
import unittest
from contextlib import redirect_stdout
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from windvane import cli, driver, stats  # noqa: E402
from windvane.config import Project  # noqa: E402
from windvane.queue import Queue  # noqa: E402
from windvane.util import append_jsonl, now, read_jsonl, shell_path  # noqa: E402
from helpers import Sandbox  # noqa: E402
from test_set_aside import Base  # noqa: E402

IDLE = [dict(role="worker", ticket="T-007", turns=2, input=5000, output=300, result="PARTIAL")]


class LogHints(unittest.TestCase):
    def setUp(self):
        self.s = Sandbox()
        self.addCleanup(self.s.close)
        self.p = Project(self.s.root)
        self.log = self.p.state / "autopilot.log"

    def stats(self):
        for row in IDLE:
            append_jsonl(self.p.state / "ledger.jsonl", row)
        out, here = io.StringIO(), os.getcwd()
        self.addCleanup(os.chdir, here)
        with redirect_stdout(out):
            cli.main(["-C", str(self.s.root), "stats"])
        return out.getvalue()

    def test_the_failed_hint_names_the_log(self):
        hint = Queue(self.p).release("T-001", [], {"failed": ["T-001"]})
        self.assertIn(f"`T-001: FAILED` line in {shell_path(self.log)} says why", hint)

    def test_a_missing_log_says_so_instead_of_a_grep(self):
        out = self.stats()
        self.assertFalse(self.log.exists())
        self.assertIn(f" why: no log at {shell_path(self.log)}, nor its transcripts, and T-007 has no branch left to "
                      "say why", out)
        self.assertNotIn("grep", out)

    def test_under_a_thousand_tokens_is_not_0k(self):
        self.assertEqual(stats.k(400), "<1k")
        self.assertEqual(stats.k(999), "<1k")
        self.assertEqual(stats.k(1500), "2k")
        self.assertEqual(stats.k(0), "0")
        self.assertIn("<1k out", " ".join(self.stats().split()))


class DriverLines(Base):
    tickets = ("T-001",)

    def test_the_grep_finds_the_line_the_driver_writes_for_a_partial(self):
        os.environ["FAKE_WORKER"] = "partial"
        self.addCleanup(os.environ.pop, "FAKE_WORKER", None)
        self.pilot().run(now() + timedelta(hours=2))
        ledger = self.p.state / "ledger.jsonl"  # rows from before the ledger kept the reason: the grep is the way
        rows = [{k: v for k, v in r.items() if k != "reason"} for r in read_jsonl(ledger)]
        ledger.write_text("".join(json.dumps(r) + "\n" for r in rows))
        append_jsonl(ledger, {**IDLE[0], "ticket": "T-001"})
        out, here = io.StringIO(), os.getcwd()
        self.addCleanup(os.chdir, here)
        with redirect_stdout(out):
            cli.main(["-C", str(self.s.root), "stats"])
        [why] = [ln for ln in out.getvalue().splitlines() if ln.startswith(" why: ")]
        grep = re.fullmatch(r' why: grep "([^"]+)" (\S+)', why)
        log = self.p.state / "autopilot.log"
        self.assertEqual(grep[2], shell_path(log))
        hits = [ln for ln in log.read_text().splitlines() if grep[1] in ln]
        self.assertTrue(hits)
        self.assertTrue(all("out of time" in ln for ln in hits), hits)

    def test_the_max_turns_push_says_to_start_windvane_run_again(self):
        os.environ["FAKE_MAX_TURNS"] = "# windvane worker"
        self.addCleanup(os.environ.pop, "FAKE_MAX_TURNS", None)
        self.pilot().run(now() + timedelta(hours=2))
        [(_, msg)] = self.aside()
        first, then = msg.split("\n")
        self.assertLessEqual(len(first), 90)  # a lock screen's two lines
        self.assertIn("then start `windvane run` again", then)


if __name__ == "__main__":
    unittest.main()
