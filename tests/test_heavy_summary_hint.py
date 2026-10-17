"""R-030: a heavy job that waited on memory was told which settings would have spared it the wait (a helper to add to
`heavy_reuses`, a need left at the default), but only in its own log, which the session read and couldn't act on.
The run's summary names them once, for the user, who can."""
import os
import sys
import time
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from windvane import driver, heavy, jobs  # noqa: E402
from windvane.config import Project  # noqa: E402
from windvane.phone import MOST, whole  # noqa: E402
from windvane.runner import IN_JOB  # noqa: E402
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
        for k in (IN_JOB, "WINDVANE_HEAVY_HELD"):  # a suite run as a background job is in one
            os.environ.pop(k, None)

    def run_one(self, check_out, job_out=None, tickets=("T-001",)):
        self.s.write("check-out.txt", check_out)
        for t in tickets:
            self.s.ticket(t, touches=f"{t}.txt")
        self.s.publish()
        if job_out is not None:
            (self.s.tmp / "job-out.txt").write_text(job_out)
            os.environ["FAKE_BG_HEAVY"] = f"cat {self.s.tmp / 'job-out.txt'}"
        self.p = Project(self.s.root)
        pilot = driver.Pilot(self.p, args(parallel=len(tickets)))
        pilot.run(now() + timedelta(minutes=30))
        self.assertEqual(sorted(pilot.merged), list(tickets))
        return pilot

    def line(self, pilot):
        summary = pilot.summary()
        lines = [x for x in summary.splitlines() if x.startswith("Heavy jobs")]
        self.assertEqual(len(lines), 1, summary)
        self.assertEqual(pilot.summary().count("Heavy jobs"), 1)  # asked again, nothing counts twice
        return lines[0]

    def test_the_summary_names_each_new_word_once_with_the_runs_wait(self):
        pilot = self.run_one(waited(4800, QEMU, GRADLE, ADB), waited(3000, GRADLE, default=False))
        self.assertFalse((self.p.state / "bg/T-001").exists(), "its bg logs outlived the ticket")
        # the job's 50 min fall within the check's 80, which began a few seconds after it
        self.assertEqual(self.line(pilot), f'Heavy jobs waited 1 h 20 min on memory: in {CFG}, add "GradleDaemon", '
                                           '"qemu-system-x86" to heavy_reuses and set heavy_mem to the GB they need '
                                           "(they waited for 6 GB, machine.toml's min_free_gb).")  # adb: configured

    def test_tickets_side_by_side_name_a_word_once(self):
        pilot = self.run_one("all good\n", waited(3000, GRADLE, default=False), tickets=("T-001", "T-002"))
        self.assertEqual(self.line(pilot), f'Heavy jobs waited 50 min on memory: in {CFG}, add "GradleDaemon" to '
                                           "heavy_reuses.")

    def test_a_run_whose_heavy_jobs_never_waited_has_no_line(self):
        pilot = self.run_one("all good\n", "job ran at once\n")
        self.assertNotIn("Heavy jobs", pilot.summary())

    def test_only_settings_already_made_say_nothing(self):
        cfg = self.s.root / CFG
        cfg.write_text("heavy_mem = 0\n" + cfg.read_text())  # set, and never waits on the real machine's memory
        pilot = self.run_one(waited(600, ADB))
        self.assertNotIn("Heavy jobs", pilot.summary())


class Line(unittest.TestCase):
    def setUp(self):
        self.s = Sandbox()
        self.addCleanup(self.s.close)
        self.p = Project(self.s.root)
        self.pilot = driver.Pilot(self.p, args(parallel=1))

    def test_a_long_word_list_is_cut_by_wholes_rule_and_logged_whole(self):
        self.pilot.heavy_waits = {f"/x/{i}.log": [i * 60, 60, [f"helper-number-{i}"], None] for i in range(300)}
        line = self.pilot.heavy_line()
        self.assertLess(len(line.encode()), MOST + 200)
        self.assertTrue(line.startswith(f'Heavy jobs waited 5 h on memory: in {CFG}, add "helper-number-0", '), line)
        self.assertTrue(line.endswith('" (the rest is in the log).\n'), line)
        self.assertEqual(line.count("\n"), 1)
        self.assertIn("helper-number-299", (self.p.state / "autopilot.log").read_text())

    def test_only_the_default_need_suggests_heavy_mem_alone(self):
        self.pilot.heavy_waits = {"/x/a.log": [0, 2400, [], "6"]}
        self.assertEqual(self.pilot.heavy_line(),
                         whole(f"Heavy jobs waited 40 min on memory: in {CFG}, ", "set heavy_mem to the GB they need "
                               "(they waited for 6 GB, machine.toml's min_free_gb)", ".\n", log=self.pilot.log))

    def test_waits_at_the_same_time_count_once(self):
        self.pilot.heavy_waits = {"/x/a.log": [0, 3600, ["a"], None], "/x/b.log": [1800, 3600, ["b"], None],
                                  "/x/c.log": [9000, 600, ["a"], None]}
        self.assertIn("waited 1 h 40 min on memory", self.pilot.heavy_line())

    def test_the_waits_carry_over_a_restart(self):
        self.pilot.heavy_waits = {"/x/a.log": [0, 2400, ["GradleDaemon"], "6"]}
        line = self.pilot.heavy_line()
        self.pilot.hand_over()
        nxt = driver.Pilot(self.p, args(parallel=1))
        nxt.carry_over(True)
        self.assertEqual(nxt.opened, self.pilot.opened)
        self.assertEqual(nxt.heavy_line(), line)

    def bg_log(self, name, text, start, code="143"):
        base = self.p.state / "bg/T-009" / name
        base.parent.mkdir(parents=True, exist_ok=True)
        base.with_suffix(".start").write_text(str(int(start)))
        base.with_suffix(".log").write_text(text)
        base.with_suffix(".exit").write_text(code + "\n")
        return base

    def test_a_job_that_never_got_the_machine_counts_all_its_time(self):
        start = time.time() - 3600
        log = self.bg_log("job", waited(0, GRADLE).split("\n")[0] + "\nwindvane: kept job stopped\n", start)
        found = jobs.heavy_waits(self.p, start - 1, "T-009")
        self.assertEqual(list(found), [log.with_suffix(".log")])
        began, secs, words, need = found[log.with_suffix(".log")]
        self.assertEqual((int(began), words, need), (int(start), ["GradleDaemon"], "6"))
        self.assertAlmostEqual(secs, 3600, delta=5)
        self.assertEqual(jobs.heavy_waits(self.p, time.time() + 60), {})  # last at work before the run: not its own

    def test_a_log_removed_as_it_is_read_is_skipped(self):
        start = time.time() - 600
        gone = self.bg_log("a", waited(0, GRADLE).split("\n")[0] + "\n", start)
        kept = self.bg_log("b", waited(0, QEMU).split("\n")[0] + "\n", start)
        hints = heavy.memory_hints

        def remove_then_read(log):  # another slot's ticket ending drops it between the stat and the read
            if log == gone.with_suffix(".log"):
                gone.with_suffix(".start").unlink()
            return hints(log)
        with mock.patch.object(heavy, "memory_hints", remove_then_read):
            found = jobs.heavy_waits(self.p, start - 1)
        self.assertEqual(list(found), [kept.with_suffix(".log")])


if __name__ == "__main__":
    unittest.main()
