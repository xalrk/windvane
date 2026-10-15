"""T-132: what `windvane stats` used to miss. The usage probe (a Haiku call at every run's start) writes a `probe` row
to the ledger and `stats` shows it as its own role line; a heavy check whose lock wait a stop cut short keeps its
waited seconds, so the `waits` line counts them."""
import io
import json
import os
import sys
import threading
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from windvane import cli, jobs, runner  # noqa: E402
from windvane.config import Project  # noqa: E402
from windvane.runner import ROLE, TICKET, Usage  # noqa: E402
from windvane.util import read_jsonl  # noqa: E402
from helpers import Sandbox  # noqa: E402
from test_heavy_check_wait import hold, machine  # noqa: E402

# What a real probe streams, trimmed: its rate-limit event, one assistant message and the result with its usage
PROBE = r'''#!/usr/bin/env python3
import json, os, sys
open(os.environ["PROBE_ARGS"], "w").write(json.dumps(sys.argv[1:]))
emit = lambda ev: print(json.dumps(ev), flush=True)
emit({"type": "system", "subtype": "init"})
if os.environ.get("PROBE_MODE") == "junk":  # lines that parse as JSON but aren't events
    print("1\n\"x\"\n[]", flush=True)
emit({"type": "rate_limit_event", "rate_limit_info": {"status": "allowed", "unifiedWindows": {
    "five_hour": {"utilization": 0.42, "resetsAt": 0}, "seven_day": {"utilization": 0.13}}}})
use = {"input_tokens": 3, "output_tokens": 6, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 9600}
emit({"type": "assistant", "message": {"id": "m1", "usage": use, "content": [{"type": "text", "text": "ok"}]}})
mode = os.environ.get("PROBE_MODE")
if mode == "hang":  # streamed its first message, then hangs until the probe's time box kills it
    import time
    time.sleep(30)
emit({"type": "result", "subtype": "success", "is_error": mode == "error", "num_turns": 1, "total_cost_usd": 0.012,
      "result": "API Error: 500 Internal server error" if mode == "error" else "ok", "usage": use})
'''


class Probe(unittest.TestCase):
    def setUp(self):
        self.s = Sandbox()
        self.addCleanup(self.s.close)
        fake = self.s.bin / "claude"
        fake.write_text(PROBE)
        self.args = self.s.tmp / "args.json"
        os.environ["PROBE_ARGS"] = str(self.args)
        os.environ.pop("PROBE_MODE", None)
        self.p = Project(self.s.root)

    def stats(self, *more):
        out, here = io.StringIO(), os.getcwd()
        self.addCleanup(os.chdir, here)
        with redirect_stdout(out):
            cli.main(["-C", str(self.s.root), "stats", *more])
        return out.getvalue()

    def test_a_probe_writes_one_ledger_row_with_its_tokens(self):
        u = Usage(self.p)
        u.probe(self.p)
        self.assertEqual((u.five, u.week), (42.0, 13.0))  # it still reads the usage
        self.assertIn("--no-session-persistence", json.loads(self.args.read_text()))
        [row] = read_jsonl(self.p.state / "ledger.jsonl")
        self.assertEqual((row["role"], row["model"], row["ticket"], row["end"], row["error"]),
                         ("probe", "haiku", "", "ok", None))
        self.assertEqual((row["turns"], row["input"], row["output"], row["cache_read"], row["cache_write"]),
                         (1, 3, 6, 0, 9600))
        self.assertEqual((row["usd"], row["base"], row["estimated"]), (0.012, 9603, False))

    def test_stats_shows_the_probes_as_their_own_role_line(self):
        u = Usage(self.p)
        for _ in range(3):
            u.probe(self.p)
        lines = self.stats().splitlines()
        [probe] = [ln for ln in lines if ln.startswith("probe ")]
        # 3 runs, 3 turns, 28.8k fresh in (the cache writes), 0k cached, 0k out, 36k weighted, $0.04
        self.assertRegex(probe, r"^probe\s+3\s+·\s+3\s+29k\s+0k\s+0k\s+36k\s+0\.04\s+0$")
        self.assertEqual(sum(ln.startswith("probe") for ln in lines), 1)
        by_ticket = self.stats("--by", "ticket")
        self.assertRegex(by_ticket, r"\nprobe\s+3 ")  # no ticket: grouped under its role there too

    def test_a_hung_probe_killed_at_its_time_box_still_writes_what_it_streamed(self):
        os.environ["PROBE_MODE"] = "hang"
        u = Usage(self.p)
        with mock.patch.object(runner, "PROBE_S", 1):
            u.probe(self.p)
        self.assertEqual(u.five, 42.0)
        [row] = read_jsonl(self.p.state / "ledger.jsonl")
        self.assertEqual((row["role"], row["end"], row["killed"], row["exit"]), ("probe", "killed", "time", None))
        self.assertEqual((row["estimated"], row["turns"], row["cache_write"], row["usd"]), (True, 1, 9600, None))
        [probe] = [ln for ln in self.stats().splitlines() if ln.startswith("probe ")]
        self.assertRegex(probe, r"^probe\s+1\s+1\s+1\s+10k\s+0k\s+0k\s+12k\s+\?\s+0$")  # lost; no $ known

    def test_a_probe_the_api_failed_says_so(self):
        os.environ["PROBE_MODE"] = "error"
        Usage(self.p).probe(self.p)
        [row] = read_jsonl(self.p.state / "ledger.jsonl")
        self.assertEqual((row["end"], row["error"], row["killed"]), ("error", "API Error: 500 Internal server error",
                                                                     None))
        self.assertEqual((row["cache_write"], row["usd"]), (9600, 0.012))
        self.assertIn("died or killed: 1 run (died)", self.stats())

    def test_a_probe_skips_stream_lines_that_are_not_events(self):
        os.environ["PROBE_MODE"] = "junk"
        Usage(self.p).probe(self.p)
        [row] = read_jsonl(self.p.state / "ledger.jsonl")
        self.assertEqual((row["role"], row["cache_write"]), ("probe", 9600))

    def test_a_probe_that_streams_nothing_writes_no_row(self):
        (self.s.bin / "claude").write_text("#!/bin/sh\nexit 1\n")
        Usage(self.p).probe(self.p)
        self.assertEqual(read_jsonl(self.p.state / "ledger.jsonl"), [])


class StoppedWait(unittest.TestCase):
    """A stop ends a heavy check's wait for the machine (`heavy.run` exits 75 and says why): the check's end row says
    `not_run` and counts every second it ran as waited, and `stats` puts them on the waiting role's `waits` line."""

    def setUp(self):
        self.s = Sandbox()
        self.addCleanup(self.s.close)
        self.s.ticket("T-001")
        self.s.publish()
        self.d = self.s.tmp
        patch = mock.patch.dict(os.environ, {**machine(self.d, 1), TICKET: "T-001", ROLE: "worker"})
        patch.start()
        self.addCleanup(patch.stop)
        os.environ.pop("WINDVANE_HEAVY_HELD", None)
        os.environ.pop("WINDVANE_STOP_AT", None)  # a session running the tests has one

    def test_a_check_stopped_while_waiting_counts_its_wait(self):
        state = Project(self.s.root).state
        p = SimpleNamespace(root=self.s.root, state=state, cfg={"check": "echo checked", "heavy_check": True})
        hold(self, self.d)
        timer = threading.Timer(2.5, lambda: (state / "STOP").touch())
        timer.start()
        self.addCleanup(timer.cancel)
        ok, summary, _ = jobs.check(p, self.s.root)
        (state / "STOP").unlink()
        self.assertIsNone(ok)
        self.assertIn("stopped waiting for the machine lock", summary)
        [end] = [r for r in read_jsonl(state / "ledger.jsonl") if r["event"] == "check"]
        self.assertTrue(end["not_run"])
        self.assertGreaterEqual(end["waited_s"], 2)
        self.assertAlmostEqual(end["waited_s"], end["minutes"] * 60, delta=1)
        out, here = io.StringIO(), os.getcwd()
        self.addCleanup(os.chdir, here)
        with redirect_stdout(out):
            cli.main(["-C", str(self.s.root), "stats"])
        self.assertIn(f"waits, worker: {end['waited_s'] / 60:.1f} min for the heavy lock\n", out.getvalue())
        self.assertIn("not-run checks: 1 (no bounce)\n", out.getvalue())


if __name__ == "__main__":
    unittest.main()
