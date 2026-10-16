"""R-030: a heavy job that waited on memory was told which settings would have spared it the wait (a helper to add to
`heavy_reuses`, a need left at the default), but only in its own log, which the session read and couldn't act on.
The run's summary names them once, for the user, who can."""
import os
import sys
import time
import unittest
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from windvane import driver, heavy, jobs  # noqa: E402
from windvane.config import Project  # noqa: E402
from windvane.phone import MOST, whole  # noqa: E402
from windvane.util import now  # noqa: E402
from helpers import Sandbox  # noqa: E402
from test_parallel import args  # noqa: E402

CFG = ".windvane/config.toml"


def hint(label, word, gb):
    return f'{label} ({gb} GB) {heavy.OWN} "{word}" to heavy_reuses in {CFG}'


def waited(seconds, *hints, default=True):
    """A heavy job's log head as `take` and `run` write it: the wait on memory with its hints, then its wait."""
    clauses = [*hints, *([heavy.DEFAULT_NEED.format("6")] if default else [])]
    return (f"heavy: waiting: needs 6 GB free, 2.4 GB free; largest: 4123 java 2.6 GB (proj)"
            + "".join(f"; {c}" for c in clauses) + "\n" + heavy.WAITED.format(seconds) + "\n")


GRADLE = hint("java GradleDaemon", "GradleDaemon", 2.6)
QEMU = hint("qemu-system-x86", "qemu-system-x86", 3.1)
ADB = hint("adb", "adb", 1.2)


class SummaryHint(unittest.TestCase):
    def setUp(self):
        self.s = Sandbox()
        self.addCleanup(self.s.close)
        cfg = self.s.root / CFG  # top level: the sandbox's own ends in a table
        cfg.write_text('heavy_check = true\nheavy_reuses = ["^adb$"]\n' + cfg.read_text().replace(
            'check = "test -f README"', 'check = "cat check-out.txt; test -f README"'))
        run_dir = self.s.tmp / "run"  # the machine's heavy lock and line, this test's own
        run_dir.mkdir()
        (self.s.tmp / "config/windvane").mkdir(parents=True, exist_ok=True)
        (self.s.tmp / "config/windvane/machine.toml").write_text("busy = []\nmin_free_gb = 0\nheavy_wait = 1\n")
        os.environ["XDG_RUNTIME_DIR"] = str(run_dir)
        os.environ.pop("WINDVANE_HEAVY_HELD", None)
        self.s.ticket("T-001")

    def run_one(self, check_out, job_out=None):
        self.s.write("check-out.txt", check_out)
        self.s.publish()
        if job_out is not None:
            (self.s.tmp / "job-out.txt").write_text(job_out)
            os.environ["FAKE_BG_HEAVY"] = f"cat {self.s.tmp / 'job-out.txt'}"
        self.p = Project(self.s.root)
        pilot = driver.Pilot(self.p, args(parallel=1))
        pilot.run(now() + timedelta(minutes=30))
        self.assertEqual(pilot.merged, ["T-001"])
        return pilot

    def test_the_summary_names_each_new_word_once_with_the_runs_wait(self):
        pilot = self.run_one(waited(4800, QEMU, GRADLE, ADB), waited(3000, GRADLE, default=False))
        self.assertFalse((self.p.state / "bg/T-001").exists(), "its bg logs outlived the ticket")
        summary = pilot.summary()
        lines = [x for x in summary.splitlines() if x.startswith("Heavy jobs")]
        self.assertEqual(len(lines), 1, summary)
        line = lines[0]
        self.assertEqual(line, 'Heavy jobs waited 2 h 10 min on memory: consider heavy_reuses = ["qemu-system-x86", '
                               f'"GradleDaemon"] and heavy_mem in {CFG}.')
        self.assertNotIn("adb", line)  # its heavy_reuses already matches it
        self.assertEqual(line.count("GradleDaemon"), 1)
        self.assertEqual(pilot.summary().count("Heavy jobs"), 1)  # asked again, nothing counts twice

    def test_a_run_whose_heavy_jobs_never_waited_has_no_line(self):
        pilot = self.run_one("all good\n", "job ran at once\n")
        self.assertNotIn("Heavy jobs", pilot.summary())

    def test_only_settings_already_made_say_nothing(self):
        pilot = self.run_one(waited(600, ADB, default=False))
        self.assertNotIn("Heavy jobs", pilot.summary())


class Line(unittest.TestCase):
    def setUp(self):
        self.s = Sandbox()
        self.addCleanup(self.s.close)
        self.p = Project(self.s.root)
        self.pilot = driver.Pilot(self.p, args(parallel=1))

    def test_a_long_word_list_is_cut_by_wholes_rule_and_logged_whole(self):
        self.pilot.heavy_waits = {f"/x/{i}.log": [60, [f"helper-number-{i}"], False] for i in range(300)}
        line = self.pilot.heavy_line()
        self.assertLess(len(line.encode()), MOST + 200)
        self.assertTrue(line.endswith(f" in {CFG}.\n"), line)
        self.assertTrue(line.startswith("Heavy jobs waited 5 h on memory: consider heavy_reuses = ["), line)
        self.assertIn("helper-number-299", (self.p.state / "autopilot.log").read_text())

    def test_only_the_default_need_suggests_heavy_mem_alone(self):
        self.pilot.heavy_waits = {"/x/a.log": [2400, [], True]}
        self.assertEqual(self.pilot.heavy_line(), whole("Heavy jobs waited 40 min on memory: consider ", "heavy_mem",
                                                        f" in {CFG}.\n", log=self.pilot.log))

    def test_a_job_that_never_got_the_machine_counts_all_its_time(self):
        base = self.p.state / "bg/T-009/job"
        base.parent.mkdir(parents=True)
        start = time.time() - 3600
        base.with_suffix(".start").write_text(str(int(start)))
        base.with_suffix(".log").write_text(waited(0, GRADLE).split("\n")[0] + "\nwindvane: kept job stopped\n")
        base.with_suffix(".exit").write_text("143\n")
        found = jobs.heavy_waits(self.p, start - 1)
        self.assertEqual(list(found.values()), [(found[base.with_suffix(".log")][0], ["GradleDaemon"], True)])
        self.assertAlmostEqual(found[base.with_suffix(".log")][0], 3600, delta=5)
        self.assertEqual(jobs.heavy_waits(self.p, time.time() + 60), {})  # last at work before the run: not its own


if __name__ == "__main__":
    unittest.main()
