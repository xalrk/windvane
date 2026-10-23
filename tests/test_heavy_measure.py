"""R-047: a heavy job that measures timing (`--measure`) also waits for a quiet CPU, a 1-minute load average at or
under machine.toml's `measure_load`, letting the jobs behind it by meanwhile; and every heavy job's log says the load at
its start, end and peak, so a contended timing run is plain in it."""
import io
import os
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from windvane import cli, heavy, jobs, util  # noqa: E402
from windvane.runner import IN_SANDBOX, TICKET  # noqa: E402
from test_heavy_mem import job_argv  # noqa: E402

ROOT = Path(__file__).parents[1]
GB = 1048576  # kB
# A job on a fake /proc (argv[2]), polling fast instead of every second and sampling the load as often. argv[4]: 1 for
# a measurement.
JOB = ("import sys,time; from pathlib import Path; import windvane.heavy as h, windvane.util as u; s=time.sleep; "
       "h.time.sleep=lambda t: s(min(t,0.1)); h.LOAD_EVERY=0.1; u.PROC=Path(sys.argv[2]); "
       "sys.exit(h.run(sys.argv[1], wait_s=float(sys.argv[3]), mem=0, measure=sys.argv[4]=='1'))")


def stat(utime, stime=0):
    """A /proc/<pid>/stat line with these CPU ticks (fields 14 and 15)."""
    return f"1 (x y) S {' '.join(['0'] * 10)} {utime} {stime} " + " ".join(["0"] * 30)


class Measure(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="windvane-")
        self.addCleanup(tmp.cleanup)
        self.d = d = Path(tmp.name)
        self.proc = d / "proc"
        self.proc.mkdir()
        (self.proc / "meminfo").write_text(f"MemTotal: {32 * GB} kB\nMemAvailable: {16 * GB} kB\n")
        for pid, name, where in ((4101, "qemu-system-x86", "emu/slot-1"), (4102, "java", "app/work"),
                                 (4103, "claude", "notes/main")):
            (self.proc / str(pid)).mkdir()
            (self.proc / str(pid) / "comm").write_text(name + "\n")
            (self.proc / str(pid) / "stat").write_text(stat(0))
            (d / where).mkdir(parents=True)
            (self.proc / str(pid) / "cwd").symlink_to(d / where)
        (d / "cfg/windvane").mkdir(parents=True)
        self.machine("measure_load = 4\n")
        self.load(9.5)
        self.vars = {"XDG_RUNTIME_DIR": str(d), "XDG_CONFIG_HOME": str(d / "cfg"), "XDG_STATE_HOME": str(d / "st")}
        self.env = {k: v for k, v in os.environ.items() if k not in (
            "WINDVANE_HEAVY_HELD", heavy.STOP_AT, heavy.STOP_ENV, heavy.DEADLINE_ENV, IN_SANDBOX, TICKET)}
        self.env.update(self.vars, PYTHONPATH=str(ROOT))
        self.order = d / "order"

    def machine(self, extra=""):
        (self.d / "cfg/windvane/machine.toml").write_text("busy = []\nmin_free_gb = 1\n" + extra)

    def load(self, one):
        (self.proc / "loadavg.new").write_text(f"{one} 1.00 1.00 2/900 4242\n")
        (self.proc / "loadavg.new").replace(self.proc / "loadavg")  # never read half-written

    def job(self, tag, measure, wait_s=30, cmd=None):
        """A heavy job appending `tag` to the order file when it runs; its stderr goes to a file named after it."""
        err = open(self.d / f"{tag}.err", "w")
        self.addCleanup(err.close)
        proc = subprocess.Popen([sys.executable, "-c", JOB, cmd or f"echo {tag} >> {self.order}", str(self.proc),
                                 str(wait_s), "1" if measure else "0"], env=self.env, stdout=err, stderr=err)
        self.addCleanup(lambda: proc.poll() is None and (proc.kill(), proc.wait()))
        return proc

    def said(self, tag):
        return (self.d / f"{tag}.err").read_text().splitlines()

    def until(self, ok, timeout=15):
        end = time.time() + timeout
        while not ok():
            self.assertLess(time.time(), end, "timed out")
            time.sleep(0.05)

    def ran(self):
        return self.order.read_text().split() if self.order.exists() else []

    def waiters(self):
        q = self.d / "windvane-heavy.queue"
        return [n for n in os.listdir(q) if not n.startswith(".")] if q.exists() else []

    def in_process(self, **patches):
        """util.PROC and the XDG dirs as the jobs see them, for `heavy.run` and `heavy.status` called here."""
        for patch in (mock.patch.object(util, "PROC", self.proc),
                      mock.patch.dict(os.environ, {**self.vars, IN_SANDBOX: "", TICKET: ""}),
                      *(mock.patch.object(heavy, k, v) for k, v in patches.items())):
            patch.start()
            self.addCleanup(patch.stop)
        for k in ("WINDVANE_HEAVY_HELD", heavy.STOP_AT, IN_SANDBOX, TICKET):
            os.environ.pop(k, None)

    def run_here(self, wait_s=1.2, measure=True):
        err = io.StringIO()
        with redirect_stderr(err):
            code = heavy.run(["true"], wait_s=wait_s, mem=0, measure=measure)
        return code, err.getvalue().splitlines()

    def test_a_measurement_waits_while_the_load_is_high_and_starts_once_it_drops(self):
        job = self.job("timing", measure=True)
        self.until(lambda: self.said("timing"))
        time.sleep(0.5)
        self.assertEqual(self.ran(), [])
        self.assertTrue(self.said("timing")[0].startswith("heavy: waiting: load 9.5 (want ≤ 4)"), self.said("timing"))
        self.load(5)
        time.sleep(0.5)
        self.assertEqual(self.ran(), [])
        self.assertEqual(len([l for l in self.said("timing") if l.startswith("heavy: waiting:")]), 1,
                         "said once, not again as the load drifts")
        self.assertIn("is machine.toml's measure_load", self.said("timing")[0])
        self.load(3.9)
        self.assertEqual(job.wait(15), 0)
        self.assertEqual(self.ran(), ["timing"])
        self.assertIn("heavy: load 3.9 at start", self.said("timing"))

    def test_its_waiting_line_names_the_busiest_processes(self):
        samples = iter([{4101: 10.0, 4102: 5.0, 4103: 1.0}, {4101: 13.1, 4102: 6.0, 4103: 1.0}])
        self.in_process(cpu_times=lambda: next(samples, {}))
        code, lines = self.run_here()
        self.assertEqual(code, heavy.TIMED_OUT)
        self.assertRegex(lines[0], r"^heavy: waiting: load 9\.5 \(want ≤ 4\); busiest: "
                                   r"pid 4101 qemu-system-x86 \d+% in emu/slot-1, pid 4102 java \d+% in app/work; "
                                   r"the ≤ 4 is machine.toml's measure_load \(default: a quarter of the CPU count\): "
                                   r"raise it there if this machine is never that quiet$")
        self.assertNotIn("4103", lines[0])  # it used no CPU between the samples

    def test_cpu_times_come_from_each_process_stat(self):
        (self.proc / "4101" / "stat").write_text(stat(250, 50))
        with mock.patch.object(util, "PROC", self.proc):
            times = heavy.cpu_times()
        self.assertEqual(times[4101], 300 / os.sysconf("SC_CLK_TCK"))
        self.assertEqual(times[4102], 0)

    def test_it_gives_up_at_its_deadline_naming_the_load(self):
        self.in_process(busiest=lambda: [])
        t0 = time.time()
        code, lines = self.run_here(wait_s=1.5)
        self.assertEqual(code, heavy.TIMED_OUT)
        self.assertGreaterEqual(time.time() - t0, 1.4)
        self.assertEqual(lines[-1], f"{heavy.GAVE_UP}: load 9.5 (want ≤ 4)")

    def test_a_job_behind_a_waiting_measurement_runs_first(self):
        head = self.job("timing", measure=True)
        self.until(lambda: self.said("timing"))
        plain = self.job("build", measure=False)
        self.assertEqual(plain.wait(15), 0)
        self.assertEqual(self.ran(), ["build"])
        self.assertIsNone(head.poll())
        self.assertEqual(len(self.waiters()), 1)  # still the head, still in line
        self.in_process(busiest=lambda: ["pid 4101 qemu-system-x86 310% in emu/slot-1"])
        lines = heavy.status()
        self.assertEqual(lines[0], "free")
        self.assertRegex(lines[1], rf"^waiting: pid {head.pid}, since \d\d:\d\d, measuring "
                                   r"\(waiting for load ≤ 4, now 9\.5; letting other jobs by\)$")
        self.assertEqual(lines[2], "  uses CPU: pid 4101 qemu-system-x86 310% in emu/slot-1")
        self.load(1)
        self.assertEqual(head.wait(15), 0)
        self.assertEqual(self.ran(), ["build", "timing"])

    def test_a_measurement_holding_the_lock_is_marked_in_status(self):
        self.load(1)
        go = self.d / "go"
        job = self.job("timing", measure=True, cmd=f"while [ ! -e {go} ]; do sleep 0.05; done")
        self.addCleanup(go.touch)
        self.until(lambda: "heavy: load 1 at start" in self.said("timing"))
        self.in_process()
        self.assertRegex(heavy.status()[0], rf"^held by pid {job.pid} \(.*; measuring\)$")
        go.touch()
        self.assertEqual(job.wait(15), 0)

    def test_measure_load_in_machine_toml_replaces_the_default(self):
        self.machine()
        self.in_process()
        self.assertEqual(heavy.machine()["measure_load"], 0.25 * os.cpu_count())
        self.machine("measure_load = 10\n")
        self.assertEqual(self.run_here()[0], 0)  # 9.5 is quiet enough for this machine
        self.machine("measure_load = 9\n")
        self.in_process(busiest=lambda: [])
        code, lines = self.run_here()
        self.assertEqual(code, heavy.TIMED_OUT)
        self.assertEqual(lines[-1], f"{heavy.GAVE_UP}: load 9.5 (want ≤ 9)")

    def test_a_measure_load_that_is_not_a_number_falls_back_to_the_default_and_says_so(self):
        self.machine('measure_load = "4"\n')
        self.in_process(busiest=lambda: [], default_load=lambda: 2)
        code, lines = self.run_here()
        self.assertEqual(code, heavy.TIMED_OUT)
        self.assertEqual(lines[0], heavy.BAD_LIMIT.format(2))
        self.assertEqual(lines[-1], f"{heavy.GAVE_UP}: load 9.5 (want ≤ 2)")

    def test_a_job_without_measure_never_waits_on_load(self):
        self.load(64)
        self.in_process()
        code, lines = self.run_here(measure=False)
        self.assertEqual(code, 0)
        self.assertFalse([l for l in lines if l.startswith("heavy: waiting")], lines)
        self.assertEqual(lines[0], "heavy: load 64 at start")

    def test_every_heavy_job_logs_its_start_end_and_peak_load(self):
        self.load(1.2)
        new = self.proc / "loadavg.new"
        cmd = (f"echo 5.1 1 1 1/1 1 > {new} && mv {new} {self.proc}/loadavg && sleep 0.6 && "
               f"echo 3.4 1 1 1/1 1 > {new} && mv {new} {self.proc}/loadavg && echo out")
        for measure in (False, True):
            with self.subTest(measure=measure):
                self.load(1.2)
                tag = f"job{int(measure)}"
                self.assertEqual(self.job(tag, measure, cmd=cmd).wait(15), 0)
                lines = self.said(tag)
                self.assertEqual(lines[0], "heavy: load 1.2 at start")
                self.assertEqual(lines[-1], "heavy: load 3.4 at end, 5.1 peak")
                self.assertFalse(heavy.waiting(self.d / f"{tag}.err"))  # it ran: not still in the queue

    def test_without_a_load_source_it_says_so_once_and_does_not_wait(self):
        (self.proc / "loadavg").unlink()

        def none():
            raise OSError("no load average")
        self.in_process()
        with mock.patch.object(heavy.os, "getloadavg", none):
            code, lines = self.run_here()
        self.assertEqual(code, 0)
        self.assertEqual(lines, [heavy.NO_LOAD, "heavy: load unknown at start"])

    def test_bg_heavy_measure_passes_it_on_to_the_job(self):
        p = SimpleNamespace(state=self.d / "state")
        with mock.patch.object(jobs.subprocess, "Popen") as popen, redirect_stdout(io.StringIO()), \
                mock.patch.dict(os.environ, {TICKET: "T-1"}):
            jobs.bg(p, "perf", ["./perf.sh"], self.d, heavy=True, mem=2.0, measure=True)
        argv = job_argv(popen)
        self.assertEqual(argv[argv.index("heavy"):], ["heavy", "--mem", "2", "--measure", "--", "./perf.sh"])
        again = next((self.d / "state").rglob("perf.cmd")).read_text()
        self.assertEqual(again, "windvane bg --heavy --mem 2 --measure perf ./perf.sh")
        with self.assertRaisesRegex(SystemExit, "--measure goes with --heavy"):
            cli.main(["bg", "--measure", "perf", "./perf.sh"])


if __name__ == "__main__":
    unittest.main()
