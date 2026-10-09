"""R-008: a heavy check whose wait for the machine lock times out (machine.toml's `heavy_wait`) never ran: it says
CHECK NOT RUN with the holder, exits apart from a failure, its ledger row says not run and `stats` doesn't count it
failed, and the driver's gate runs it again instead of bouncing the ticket to its worker."""
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from argparse import Namespace
from contextlib import redirect_stdout
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from windvane import cli, driver, heavy, jobs, stats  # noqa: E402
from windvane.config import Project  # noqa: E402
from windvane.runner import TICKET  # noqa: E402
from windvane.util import now, read_jsonl  # noqa: E402
from helpers import Sandbox  # noqa: E402
from test_parallel import args  # noqa: E402

# Holds the machine lock, with a holder line as a windvane job writes it, until a release file appears.
HOLD = ("import fcntl,json,os,sys,time; f=os.open(sys.argv[1],os.O_CREAT|os.O_RDWR); fcntl.flock(f,fcntl.LOCK_EX); "
        "os.write(f,json.dumps({'pid':os.getpid(),'since':time.time(),'cmd':'render.sh','cwd':'game/work'}).encode()); "
        "open(sys.argv[2],'w').close()\nwhile not os.path.exists(sys.argv[3]): time.sleep(0.05)")


def machine(d, wait_min):
    """An isolated machine: its own heavy lock and machine.toml (nothing busy, no memory floor, a short wait)."""
    (d / "cfg/windvane").mkdir(parents=True, exist_ok=True)
    (d / "cfg/windvane/machine.toml").write_text(f"busy = []\nmin_free_gb = 0\nheavy_wait = {wait_min}\n")
    return {"XDG_RUNTIME_DIR": str(d), "XDG_CONFIG_HOME": str(d / "cfg")}


def hold(test, d):
    """Another job holds the machine lock until `d/release` exists."""
    held = d / "held"
    proc = subprocess.Popen([sys.executable, "-c", HOLD, str(d / "windvane-heavy.lock"), str(held), str(d / "release")])
    test.addCleanup(lambda: ((d / "release").touch(), proc.wait()))
    end = time.time() + 10
    while not held.exists():
        test.assertLess(time.time(), end, "the holder never took the lock")
        time.sleep(0.05)
    return proc


class HeavyCheckWait(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="windvane-")
        self.addCleanup(tmp.cleanup)
        self.d = Path(tmp.name)
        patch = mock.patch.dict(os.environ, machine(self.d, 0.02))  # 1.2 s
        patch.start()
        self.addCleanup(patch.stop)
        os.environ.pop("WINDVANE_HEAVY_HELD", None)
        os.environ.pop(TICKET, None)
        (self.d / "work").mkdir()
        self.p = SimpleNamespace(state=self.d / "state", cfg={"check": "echo checked", "heavy_check": True})

    def test_a_timed_out_wait_is_not_run_naming_the_holder(self):
        holder = hold(self, self.d)
        ok, summary, log = jobs.check(self.p, self.d / "work", ticket="T-1")
        self.assertIsNone(ok)
        self.assertRegex(summary, rf"^CHECK NOT RUN: waited 0 min for the machine lock \(held by pid {holder.pid} "
                                  r"\(render.sh in game/work, since \d\d:\d\d\)\); run it again$")
        self.assertNotIn("checked", log.read_text())
        end = [r for r in read_jsonl(self.p.state / "ledger.jsonl") if r["event"] == "check"]
        self.assertEqual([(r["passed"], r.get("not_run")) for r in end], [(False, True)])

    def test_stats_doesnt_count_it_failed(self):
        hold(self, self.d)
        jobs.check(self.p, self.d / "work", ticket="T-1")
        out = io.StringIO()
        with redirect_stdout(out):
            stats.check_stats(read_jsonl(self.p.state / "ledger.jsonl"), "role", self.p)
        self.assertIn("checks: 0 runs", out.getvalue())
        self.assertIn("not-run checks: 1 (no bounce)", out.getvalue())
        self.assertNotIn("failed", out.getvalue())
        self.assertNotIn("wasted", out.getvalue())

    def test_windvane_check_exits_apart_from_a_failure(self):
        hold(self, self.d)
        out = io.StringIO()
        with redirect_stdout(out), mock.patch("os.getcwd", return_value=str(self.d / "work")), \
                self.assertRaises(SystemExit) as e:
            cli.cmd_check(Namespace(lines=5), self.p)
        self.assertEqual(e.exception.code, heavy.TIMED_OUT)
        self.assertIn("CHECK NOT RUN", out.getvalue())
        self.assertNotIn("heavy: waiting", out.getvalue())  # no log tail: nothing ran

    def test_a_check_that_exits_75_itself_still_failed(self):
        self.p.cfg["check"] = "exit 75"
        ok, summary, _ = jobs.check(self.p, self.d / "work", ticket="T-1")
        self.assertIs(ok, False)
        self.assertIn("CHECK FAILED (exit 75)", summary)

    def test_heavy_wait_sets_windvanes_heavy_wait_and_wait_overrides_it(self):
        hold(self, self.d)
        exe = Path(__file__).resolve().parents[1] / "bin/windvane"
        run = lambda *a: subprocess.run([str(exe), "heavy", *a, "--", "true"], capture_output=True, text=True,
                                        timeout=30)
        t = time.time()
        r = run()
        self.assertEqual(r.returncode, heavy.TIMED_OUT)
        self.assertLess(time.time() - t, 15)
        self.assertIn("heavy: gave up waiting for the machine lock: held by pid", r.stderr)
        t = time.time()
        self.assertEqual(run("--wait", "0.08").returncode, heavy.TIMED_OUT)
        self.assertGreater(time.time() - t, 4)


class GateRunsItAgain(unittest.TestCase):
    def setUp(self):
        self.s = Sandbox()
        self.addCleanup(self.s.close)
        self.d = Path(tempfile.mkdtemp(dir=self.s.tmp))
        os.environ.update(machine(self.d, 0.02))
        self.s.ticket("T-001")
        self.s.write(".windvane/config.toml", (self.s.root / ".windvane/config.toml").read_text()
                     .replace('check = "test -f README"', 'check = "test -f README"\nheavy_check = true'))
        self.s.publish()
        for k, v in {"NAP": 0.1, "STASH_WAIT": 0.5}.items():
            patch = mock.patch.object(driver, k, v)
            patch.start()
            self.addCleanup(patch.stop)

    def pilot(self):
        pilot = driver.Pilot(Project(self.s.root), args(parallel=1))
        self.lines, log = [], pilot.log
        pilot.log = lambda msg: (self.lines.append(msg), log(msg))
        return pilot

    def test_the_gate_runs_it_again_without_a_bounce(self):
        hold(self, self.d)
        pilot = self.pilot()
        log = pilot.log

        def freed_after_one_wait(msg):  # the holder lets go once the gate has waited in vain once
            log(msg)
            if "not run, waited" in msg:
                (self.d / "release").touch()
        pilot.log = freed_after_one_wait
        pilot.run(now() + timedelta(minutes=10))
        self.assertEqual(pilot.merged, ["T-001"])
        self.assertEqual(pilot.bounces.get("T-001", {}), {})
        waits = [m for m in self.lines if "not run" in m]
        self.assertEqual(len(waits), 1, waits)
        self.assertRegex(waits[0], r"^T-001: check on \w+: not run, waited 0 min for the machine lock \(held by pid "
                                   r"\d+ \(render.sh in game/work, since \d\d:\d\d\)\); the autopilot runs it again, "
                                   "nothing to do$")
        self.assertNotIn("run it again;", waits[0])
        workers = [c for c in read_jsonl(self.s.tmp / "calls.jsonl") if "worker" in c.get("system", "")]
        self.assertLessEqual(len(workers), 1)

    def test_a_stop_ends_the_retries(self):
        pilot = self.pilot()
        runs = []
        (pilot.p.state / "STOP").parent.mkdir(parents=True, exist_ok=True)
        (pilot.p.state / "STOP").write_text("")
        pilot.deadline = now() + timedelta(minutes=10)
        ok, summary, _ = pilot.ran("T-001", lambda: (runs.append(1), (None, "CHECK NOT RUN: x; run it again", None))[1],
                                 "check")
        self.assertIsNone(ok)
        self.assertEqual((len(runs), self.lines),
                         (1, ["T-001: check: not run, x; the next run checks it again, nothing to do"]))


if __name__ == "__main__":
    unittest.main()
