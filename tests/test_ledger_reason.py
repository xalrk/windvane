"""A worker's ledger row keeps why it stopped (the rest of its RESULT line), so `windvane stats` says why idle
launches stopped without the log or the transcripts; and `autopilot.log` moves to `autopilot.log.1` past a cap, its
readers reading across the cut."""
import io
import os
import shutil
import subprocess
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from windvane import cli, runner, stats, util, watch  # noqa: E402
from windvane.config import Project  # noqa: E402
from windvane.queue import Queue  # noqa: E402
from windvane.util import append_jsonl, last_log, log, read_jsonl  # noqa: E402
from helpers import Sandbox  # noqa: E402

REASON = "out of time, tests left"


class Base(unittest.TestCase):
    def setUp(self):
        self.s = Sandbox()
        self.addCleanup(self.s.close)
        self.p = Project(self.s.root)
        self.old, self.logf = util.log_paths(self.p)

    def stats_out(self):
        out, here = io.StringIO(), os.getcwd()
        self.addCleanup(os.chdir, here)
        with redirect_stdout(out):
            cli.main(["-C", str(self.s.root), "stats"])
        return out.getvalue()

    def why(self):
        out = self.stats_out()
        lines = [ln for ln in out.splitlines() if ln.startswith(" why: ")]
        self.assertEqual(len(lines), 1, out)
        return lines[0][len(" why: "):]


class LedgerReason(Base):
    def run_worker(self, reason):
        with mock.patch.dict(os.environ, FAKE_WORKER="partial", FAKE_REASON=reason):
            runner.run_claude(self.p, "worker", "T-001", "Ticket T-001. Go.", "# windvane worker run", self.s.root,
                              ticket="T-001")
        return read_jsonl(self.p.state / "ledger.jsonl")[-1]

    def test_a_partial_worker_keeps_its_reason_and_stats_prints_it_alone(self):
        row = self.run_worker(REASON)
        self.assertEqual((row["result"], row["reason"]), ("PARTIAL", REASON))
        self.assertLessEqual(row["turns"] or 0, stats.IDLE_TURNS)  # an idle launch, as stats counts them
        shutil.rmtree(self.p.state / "runs", ignore_errors=True)  # no transcript left, and no log was ever written
        self.assertFalse(self.logf.exists())
        self.assertEqual(self.why(), f"T-001's last idle run said: {REASON}")

    def test_the_newest_idle_row_with_a_reason_is_the_one_printed(self):
        for reason in ("started past its wrap-up", REASON, None):  # the last: a row from before the ledger kept it
            append_jsonl(self.p.state / "ledger.jsonl", {"role": "worker", "ticket": "T-001", "turns": 2,
                                                         "result": "PARTIAL", "reason": reason})
        self.assertEqual(self.why(), f"T-001's last idle run said: {REASON}")

    def test_a_long_reason_is_cut_at_a_word(self):
        reason = runner.result_reason("RESULT: PARTIAL T-001 " + "word " * 100)
        self.assertLessEqual(len(reason), runner.REASON_MAX + 1)
        self.assertTrue(reason.endswith("word…"), reason)
        self.assertEqual(runner.result_reason("RESULT: PARTIAL T-001 " + "x" * 300), "x" * runner.REASON_MAX + "…")

    def test_only_the_last_result_lines_rest_is_kept(self):
        self.assertEqual(runner.result_reason("RESULT: READY T-1 abc\nRESULT: BLOCKED T-1  Red   or blue?\nbye"),
                         "Red or blue?")
        self.assertIsNone(runner.result_reason("RESULT: PARTIAL T-1"))
        self.assertIsNone(runner.result_reason("RESULT: PARTIAL T-1\nmore text"))
        self.assertIsNone(runner.result_reason("no result line"))
        self.assertEqual(runner.result_reason("RESULT: PARTIAL out of time"), "out of time")  # no id: all of it


class LogRotation(Base):
    def setUp(self):
        super().setUp()
        cap = mock.patch.object(util, "LOG_CAP", 400)
        cap.start()
        self.addCleanup(cap.stop)

    def logged(self, start, end):
        for i in range(start, end):
            log(self.p, f"line {i:03}", echo=False)

    def test_past_the_cap_one_old_log_is_kept_and_last_log_reads_across_the_cut(self):
        i = 0
        while not self.old.exists():
            self.logged(i, i + 1)
            i += 1
        self.logged(i, i + 3)  # the new log holds 3 lines, short of the 12 asked for
        self.assertEqual([ln[17:] for ln in last_log(self.p, 12)], [f"line {j:03}" for j in range(i - 9, i + 3)])
        self.assertEqual(len(last_log(self.p, None)), i + 3)
        self.logged(i + 3, i + 60)  # rotated again: the first file's lines are gone, never a `.2`
        self.assertEqual(sorted(f.name for f in self.p.state.glob("autopilot.log*")),
                         ["autopilot.log", "autopilot.log.1"])
        self.assertLessEqual(self.old.stat().st_size, util.LOG_CAP + 40)
        every = last_log(self.p, None)
        self.assertNotIn("line 000", "\n".join(every))
        self.assertEqual(every[-1][17:], f"line {i + 59:03}")
        self.assertEqual(last_log(self.p)[0][17:], f"line {i + 59:03}")

    def test_writers_past_the_cap_at_once_move_it_once_and_lose_no_line(self):
        import threading
        with mock.patch.object(util, "LOG_CAP", 6000):  # 400 lines of 26 bytes: past it once, never twice
            threads = [threading.Thread(target=lambda t=t: [log(self.p, f"w{t} {i:03}", echo=False)
                                                            for i in range(100)]) for t in range(4)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        every = [ln[17:] for ln in last_log(self.p, None)]
        self.assertTrue(self.old.exists())
        self.assertEqual(sorted(every), sorted(f"w{t} {i:03}" for t in range(4) for i in range(100)))

    def test_a_long_log_is_read_from_its_end(self):
        with mock.patch.object(util, "LOG_CAP", 10 ** 9):
            self.logf.parent.mkdir(parents=True, exist_ok=True)
            self.logf.write_text("".join(f"2026-10-01 01:00 line {i:05}\n" for i in range(5000)))
            self.assertEqual([ln[17:] for ln in last_log(self.p, 3000)], [f"line {i:05}" for i in range(2000, 5000)])

    def test_stats_greps_the_old_log_for_rows_without_a_reason(self):
        append_jsonl(self.p.state / "ledger.jsonl", {"role": "worker", "ticket": "T-007", "turns": 2,
                                                     "result": "PARTIAL"})
        log(self.p, f"T-007: partial (1): {REASON}", echo=False)
        self.logged(0, 20)  # it rotates once, into `.1`, past the cut
        self.assertFalse(stats.log_has(self.logf, "T-007: partial"))
        cmd = self.why()
        self.assertEqual(cmd, f'grep "T-007: partial" {stats.shell_path(self.old)}')
        res = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True)
        self.assertIn(REASON, res.stdout)

    def test_the_failed_hint_names_the_old_log_when_the_line_rotated_into_it(self):
        log(self.p, "T-001: FAILED: the API it needs is gone", echo=False)
        self.logged(0, 20)
        hint = Queue(self.p).release("T-001", [], {"failed": ["T-001"]})
        self.assertIn(f"`T-001: FAILED` line in {stats.shell_path(self.old)} says why", hint)

    def test_watch_follows_the_log_across_a_rotation(self):
        self.logged(0, 2)  # there before it starts: not shown
        calls = []

        def sleep(_):
            calls.append(1)
            if len(calls) == 1:
                self.logged(100, 120)  # past the cap once: some lines land in the old log after the move
            else:
                raise KeyboardInterrupt

        out = io.StringIO()
        with mock.patch.object(watch.time, "sleep", sleep), redirect_stdout(out), \
                mock.patch.object(sys.stdout, "reconfigure", create=True):
            watch.main(self.p)
        shown = [ln.split(" line ")[1] for ln in out.getvalue().splitlines() if "[driver]" in ln]
        self.assertTrue(self.old.exists())
        self.assertEqual(shown, [f"{i:03}" for i in range(100, 120)])


if __name__ == "__main__":
    unittest.main()
