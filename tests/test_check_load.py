"""R-056, R-060: a project whose check measures timing (`check_measure`, with `heavy_check`) gets it run as a
`--measure` heavy job, waiting for a quiet CPU; every check's log says the load at its start, end and peak, heavy or not; and a
failed check whose peak load passed machine.toml's `measure_load` says so in its summary, since timing tests fail on
a busy machine whatever the change."""
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from windvane import config, heavy, jobs, util  # noqa: E402
from windvane.config import Project  # noqa: E402
from windvane.runner import IN_SANDBOX, TICKET  # noqa: E402

# Run first in a heavy check's own process (`run_logged`'s `python -c`): the fake /proc, 16 CPUs, and fast polls.
PRELUDE = ("import os, time; from pathlib import Path; import windvane.heavy as h, windvane.util as u; "
           "u.PROC = Path(os.environ['FAKE_PROC']); os.cpu_count = lambda: 16; h.LOAD_EVERY = 0.05; "
           "s = time.sleep; h.time.sleep = lambda t: s(min(t, 0.1)); ")


class Fixture(unittest.TestCase):
    """A fake /proc on a 16-CPU machine whose measure_load is the default, 4, and a project checking `work`."""
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="windvane-")
        self.addCleanup(tmp.cleanup)
        self.d = d = Path(tmp.name)
        self.proc = d / "proc"
        self.proc.mkdir()
        (self.proc / "meminfo").write_text("MemTotal: 33554432 kB\nMemAvailable: 16777216 kB\n")
        self.load(14.65)
        (d / "cfg/windvane").mkdir(parents=True)
        (d / "cfg/windvane/machine.toml").write_text("busy = []\nmin_free_gb = 0\nheavy_wait = 1\n")
        env = {"XDG_RUNTIME_DIR": str(d), "XDG_CONFIG_HOME": str(d / "cfg"), "XDG_STATE_HOME": str(d / "st"),
               "FAKE_PROC": str(self.proc)}
        for patch in (mock.patch.dict(os.environ, env), mock.patch.object(util, "PROC", self.proc),
                      mock.patch.object(heavy.os, "cpu_count", lambda: 16),
                      mock.patch.object(heavy, "LOAD_EVERY", 0.05),
                      mock.patch.object(jobs.subprocess, "run", self.run_fast)):
            patch.start()
            self.addCleanup(patch.stop)
        for k in ("WINDVANE_HEAVY_HELD", heavy.STOP_AT, heavy.STOP_ENV, heavy.DEADLINE_ENV, IN_SANDBOX, TICKET):
            os.environ.pop(k, None)
        (d / "work").mkdir()
        self.p = SimpleNamespace(root=d, state=d / "state", cfg={"check": "echo checked", "heavy_check": False})

    real_run = staticmethod(subprocess.run)

    def run_fast(self, argv, *args, **kw):
        """A heavy check's `python -c` with PRELUDE first; anything else as it is."""
        if isinstance(argv, list) and argv[1:2] == ["-c"]:
            argv = [argv[0], "-c", PRELUDE + argv[2], *argv[3:]]
        return self.real_run(argv, *args, **kw)

    def load(self, one):
        (self.proc / "loadavg.new").write_text(f"{one} 1.00 1.00 2/900 4242\n")
        (self.proc / "loadavg.new").replace(self.proc / "loadavg")  # never read half-written

    def set_load(self, one):
        """A shell command, for a check to run, that sets the fake load."""
        return f"printf '{one} 1.00 1.00 2/900 4242\\n' > {self.proc}/loadavg"

    def check(self, cwd=None):
        return jobs.check(self.p, cwd or self.d / "work", ticket="T-1")

    def started(self, cwd=None):
        """`check` in a thread: its result, filled in once it ends."""
        out = []
        t = threading.Thread(target=lambda: out.append(self.check(cwd)))
        t.start()
        self.addCleanup(t.join, 30)
        return t, out

    def logs(self):
        return sorted((self.p.state / "checks").glob("*.log"))

    def until(self, ok, timeout=15):
        end = time.time() + timeout
        while not ok():
            self.assertLess(time.time(), end, "timed out")
            time.sleep(0.05)


class Measured(Fixture):
    def setUp(self):
        super().setUp()
        self.p.cfg.update(heavy_check=True, check_measure=True)

    def test_it_waits_for_the_load_to_drop_before_its_check_runs(self):
        t, out = self.started()
        self.until(lambda: self.logs() and "waiting: load 14.65 (want ≤ 4)" in self.logs()[0].read_text())
        time.sleep(1)
        self.assertNotIn("checked", self.logs()[0].read_text())
        self.load(2.5)
        t.join(30)
        ok, summary, log = out[0]
        self.assertTrue(ok, summary)
        self.assertIn("heavy: load 2.5 at start\nchecked\n", log.read_text())

    def test_the_upstreams_check_and_the_heads_both_wait_instead_of_starting_at_load_14(self):
        """R-060: both checks on a 16-CPU machine at load 14+, the limit 4: neither starts until it drops."""
        (self.d / "upstream").mkdir()
        runs = [self.started(self.d / "upstream"), self.started()]
        self.until(lambda: len(self.logs()) == 2)
        time.sleep(1.5)
        self.assertEqual([("checked" in log.read_text()) for log in self.logs()], [False, False])
        self.load(3)
        for t, out in runs:
            t.join(30)
            self.assertTrue(out[0][0], out[0][1])
        starts = [next(ln for ln in log.read_text().splitlines() if ln.endswith(" at start")) for log in self.logs()]
        self.assertEqual(starts, ["heavy: load 3 at start"] * 2)

    def test_a_wait_that_gives_up_is_not_run_naming_the_load(self):
        (self.d / "cfg/windvane/machine.toml").write_text("busy = []\nmin_free_gb = 0\nheavy_wait = 0.02\n")  # 1.2 s
        ok, summary, log = self.check()
        self.assertIsNone(ok)
        self.assertEqual(summary, "CHECK NOT RUN: waited 0 min for the machine (load 14.65 (want ≤ 4)); run it again")
        self.assertNotIn("checked", log.read_text())


class Unmeasured(Fixture):
    def test_a_check_without_either_never_waits_on_load_and_logs_it(self):
        ok, summary, log = self.check()
        self.assertTrue(ok)
        self.assertRegex(summary, r"^CHECK PASSED in \d+\.\d min( \(|$)")
        self.assertEqual(log.read_text(), "heavy: load 14.65 at start\nchecked\nheavy: load 14.65 at end, 14.65 peak\n")

    def test_a_heavy_check_without_check_measure_never_waits_on_load(self):
        self.p.cfg["heavy_check"] = True
        ok, summary, log = self.check()
        self.assertTrue(ok, summary)
        self.assertNotIn("waiting", log.read_text())
        self.assertIn("heavy: load 14.65 at start\nchecked\n", log.read_text())

    def test_a_non_heavy_checks_log_has_its_peak_between_start_and_end(self):
        self.load(1.5)
        self.p.cfg["check"] = f"{self.set_load(9.5)}; sleep 0.4; {self.set_load(2)}; echo checked"
        ok, _, log = self.check()
        self.assertTrue(ok)
        self.assertEqual(log.read_text(), "heavy: load 1.5 at start\nchecked\nheavy: load 2 at end, 9.5 peak\n")


class Summary(Fixture):
    def test_a_failed_check_over_the_limit_names_the_peak_and_the_limit(self):
        self.load(1.5)
        self.p.cfg["check"] = f"{self.set_load(34.12)}; sleep 0.4; {self.set_load(3)}; exit 1"
        ok, summary, log = self.check()
        self.assertFalse(ok)
        self.assertRegex(summary, r"^CHECK FAILED \(exit 1\) in \d+\.\d min, load peaked 34.12 \(quiet is ≤ 4\): "
                                  r"timing failures may be load; rerun them on the upstream at the same load to tell; "
                                  rf"log {log}$")

    def test_a_heavy_checks_failure_over_the_limit_says_it_too(self):
        self.p.cfg.update(heavy_check=True, check="exit 3")
        ok, summary, _ = self.check()
        self.assertFalse(ok)
        self.assertIn("(exit 3) in ", summary)
        self.assertIn(", load peaked 14.65 (quiet is ≤ 4): timing failures may be load;", summary)

    def test_a_failed_check_under_the_limit_says_nothing_of_load(self):
        self.load(3.9)
        self.p.cfg["check"] = "exit 1"
        ok, summary, log = self.check()
        self.assertFalse(ok)
        self.assertRegex(summary, rf"^CHECK FAILED \(exit 1\) in \d+\.\d min; log {log}$")

    def test_a_passing_check_never_mentions_load(self):
        self.p.cfg["check"] = f"{self.set_load(40)}; sleep 0.2; echo checked"
        ok, summary, _ = self.check()
        self.assertTrue(ok)
        self.assertNotIn("load", summary)

    def test_a_raised_measure_load_is_the_limit_it_names(self):
        (self.d / "cfg/windvane/machine.toml").write_text("measure_load = 12\n")
        self.p.cfg["check"] = "exit 1"
        self.assertIn("load peaked 14.65 (quiet is ≤ 12)", self.check()[1])
        (self.d / "cfg/windvane/machine.toml").write_text("measure_load = 15\n")
        self.assertNotIn("load", self.check()[1])


class Setting(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="windvane-")
        self.addCleanup(tmp.cleanup)
        self.d = Path(tmp.name)
        subprocess.run(["git", "init", "-q", str(self.d)], check=True)
        (self.d / ".windvane").mkdir()

    def project(self, text):
        (self.d / ".windvane/config.toml").write_text(text)
        return Project(self.d)

    def test_it_is_off_by_default_and_on_with_heavy_check(self):
        self.assertFalse(self.project("").cfg["check_measure"])
        self.assertTrue(self.project("heavy_check = true\ncheck_measure = true\n").cfg["check_measure"])

    def test_without_heavy_check_it_is_refused_with_its_reason(self):
        with self.assertRaisesRegex(SystemExit, r"^windvane: check_measure = true in config.toml needs heavy_check = "
                                                r"true: only a heavy check holds the machine lock that keeps other "
                                                r"heavy jobs off it while it measures$"):
            self.project("check_measure = true\n")

    def test_a_value_that_isnt_true_or_false_is_refused(self):
        with self.assertRaisesRegex(SystemExit, "check_measure = 'yes' in config.toml: use true or false"):
            config.check_measure({"check_measure": "yes", "heavy_check": True})


if __name__ == "__main__":
    unittest.main()
