"""`windvane stats`' idle-launch hint: the log's grep only when the log holds the top ticket's partial line, else a
command that finds the reason where it still is (the run's transcript, the ticket's branch)."""
import builtins
import io
import json
import os
import subprocess
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from windvane import cli, stats  # noqa: E402
from windvane.config import Project  # noqa: E402
from windvane.util import append_jsonl, log  # noqa: E402
from helpers import Sandbox  # noqa: E402

STREAM = "20261001-010000-T-007.jsonl"
REASON = "T-007 started past its wrap-up"


class IdleLog(unittest.TestCase):
    def setUp(self):
        s = self.s = Sandbox()
        self.addCleanup(s.close)
        self.p = Project(s.root)
        self.logf = self.p.state / "autopilot.log"
        for stream in ("20261001-000000-T-007.jsonl", STREAM):  # the latest idle run's transcript is the one named
            append_jsonl(self.p.state / "ledger.jsonl", {"role": "worker", "ticket": "T-007", "turns": 3, "input": 9000,
                                                         "result": "PARTIAL", "stream": stream})

    def why(self):
        out, here = io.StringIO(), os.getcwd()
        self.addCleanup(os.chdir, here)
        with redirect_stdout(out):
            cli.main(["-C", str(self.s.root), "stats"])
        lines = [ln for ln in out.getvalue().splitlines() if ln.startswith(" why: ")]
        self.assertEqual(len(lines), 1, out.getvalue())
        return lines[0][len(" why: "):]

    def transcript(self):
        """A worker's stream as the runner keeps it, its RESULT line in a message and in the final result event."""
        f = self.p.state / "runs" / STREAM
        f.parent.mkdir(parents=True, exist_ok=True)
        text = f'Stopping here.\nRESULT: PARTIAL {REASON}'
        f.write_text(json.dumps({"type": "system", "subtype": "init"}) + "\n"
                     + json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}})
                     + "\n" + json.dumps({"type": "result", "result": text + "\n"}) + "\n")

    def run_cmd(self, cmd):
        return subprocess.run(["bash", "-c", cmd], cwd=self.s.root, capture_output=True, text=True)

    def test_a_log_with_the_partial_line_gets_the_grep(self):
        log(self.p, "T-006: partial (1): another ticket", echo=False)
        log(self.p, f"T-007: partial (2): {REASON}", echo=False)
        self.transcript()
        cmd = self.why()
        self.assertEqual(cmd, f'grep "T-007: partial" {stats.shell_path(self.logf)}')
        self.assertIn(REASON, self.run_cmd(cmd).stdout)

    def test_a_log_without_it_points_at_the_latest_idle_runs_transcript(self):
        log(self.p, "T-006: partial (1): another ticket", echo=False)  # rotated: T-007's lines are gone
        self.transcript()
        why = self.why()
        self.assertTrue(why.startswith("the log no longer has T-007's partials; its last idle run's: grep -o "), why)
        cmd = why.split(": ", 1)[1]
        self.assertIn(str(self.p.state / "runs" / STREAM), cmd)
        self.assertEqual(self.run_cmd(cmd).stdout.strip(), f"RESULT: PARTIAL {REASON}")

    def test_no_log_points_at_the_transcript_too(self):
        self.transcript()
        why = self.why()
        self.assertTrue(why.startswith(f"no log at {stats.shell_path(self.logf)}; its last idle run's: "), why)
        self.assertEqual(self.run_cmd(why.split("run's: ", 1)[1]).stdout.strip(), f"RESULT: PARTIAL {REASON}")

    def test_with_its_transcripts_pruned_its_branch_is_named(self):
        self.s.git("checkout", "-qb", "t-007-thing")
        self.s.write("wip.txt", "half\n")
        self.s.git("add", "-A")
        self.s.git("commit", "-qm", "WIP: half of T-007")
        self.s.git("push", "-q", "origin", "t-007-thing")
        self.s.git("checkout", "-q", "main")
        why = self.why()
        self.assertTrue(why.startswith(f"no log at {stats.shell_path(self.logf)}, nor its transcripts; its branch's "
                                       "commits: git log origin/t-007-thing"), why)
        res = self.run_cmd(why.split("commits: ", 1)[1])
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("WIP: half of T-007", res.stdout)

    def test_nothing_left_says_so(self):
        self.logf.write_text("01:00 T-006: partial (1): another ticket\n")
        self.assertEqual(self.why(), "the log no longer has T-007's partials, nor its transcripts, and T-007 has no "
                                     "branch left to say why")

    def test_the_scan_stops_at_the_first_match(self):
        read = []

        class Log:
            """A log far too long to read whole: a line past the match fails the test."""
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def __iter__(self):
                yield "01:00 T-006: partial (1): x\n"
                yield "01:01 T-007: partial (1): y\n"
                read.append("past the match")
                raise AssertionError("read past the first match")

        with mock.patch.object(builtins, "open", return_value=Log()):
            self.assertTrue(stats.log_has(self.logf, "T-007: partial"))
        self.assertEqual(read, [])

    def test_a_large_log_is_read_by_lines(self):
        with open(self.logf, "w") as fh:
            fh.write("01:00 T-007: partial (1): early\n")
            fh.writelines(f"01:{i % 60:02} T-0{i % 90 + 10}: working\n" for i in range(200_000))
        self.assertTrue(stats.log_has(self.logf, "T-007: partial"))
        self.assertFalse(stats.log_has(self.logf, "T-008: partial"))
        self.assertFalse(stats.log_has(self.p.state / "none.log", "T-007: partial"))


if __name__ == "__main__":
    unittest.main()
