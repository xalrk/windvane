"""R-021, R-023: a heavy job waiting on memory that its own project's helper holds (an emulator its checks reuse),
with no `heavy_reuses` pattern naming it, says once to add one; the free figure never reads as enough."""
import io
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from contextlib import redirect_stderr, redirect_stdout
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from windvane import cli, heavy, jobs, util  # noqa: E402
from windvane.runner import TICKET  # noqa: E402

# The driver's heavy check waits in a python child: this stands in for its interpreter, reading the fake /proc.
SHIM = """import sys
from pathlib import Path
from windvane import util
util.PROC = Path({proc!r})
code, sys.argv = sys.argv[2], ["-c", *sys.argv[3:]]
exec(compile(code, "<string>", "exec"), {{"__name__": "__main__"}})
"""

GB = 1048576  # kB
HINT = "is this project's own"


class ReuseHint(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="windvane-")
        self.addCleanup(tmp.cleanup)
        self.d = d = Path(tmp.name)
        self.proc = d / "proc"
        self.proc.mkdir()
        (self.proc / "meminfo").write_text(f"MemTotal: {32 * GB} kB\nMemAvailable: {int(2.4 * GB)} kB\n")
        for sub in ("proj", "state/slot-1", "other", "sdk"):
            (d / sub).mkdir(parents=True)
        for sub in ("proj", "state/slot-1", "other"):
            (d / sub / ".git").write_text("gitdir: x\n")
        self.dirs = [str(d / "proj"), str(d / "state"), str(d / "state/slot-1")]
        (d / "cfg/windvane").mkdir(parents=True)
        (d / "cfg/windvane/machine.toml").write_text("busy = []\nmin_free_gb = 0\nheavy_wait = 0.03\n")  # 1.8 s
        env = {"XDG_RUNTIME_DIR": str(d), "XDG_CONFIG_HOME": str(d / "cfg"), "XDG_STATE_HOME": str(d / "st")}
        for patch in (mock.patch.object(util, "PROC", self.proc), mock.patch.dict(os.environ, env)):
            patch.start()
            self.addCleanup(patch.stop)
        for k in ("WINDVANE_HEAVY_HELD", "WINDVANE_STOP_AT", TICKET):  # a session running the tests has them
            os.environ.pop(k, None)

    def ps(self, pid, comm, cwd, rss_gb=0.0, ppid=1):
        p = self.proc / str(pid)
        p.mkdir()
        (p / "comm").write_text(comm + "\n")
        (p / "cmdline").write_bytes(comm.encode() + b"\0")
        (p / "status").write_text(f"Name:\t{comm}\nPPid:\t{ppid}\nVmRSS:\t{int(rss_gb * GB)} kB\n")
        os.symlink(self.d / cwd, p / "cwd")

    def take(self, patterns):
        """What a job needing 6 GB of this project (its `heavy_reuses`: `patterns`) said while it waited a moment."""
        lines = []
        entry = heavy.join_queue(6)
        lock = os.open(heavy.lock_path(), os.O_CREAT | os.O_RDWR, 0o600)
        try:
            end = time.time() + 1.5
            heavy.take(lock, entry, 6, {"busy": []}, lambda: end, lines.append, reuse=(patterns, self.dirs))
        finally:
            heavy.leave_queue(entry)
            os.close(lock)
        return "\n".join(lines)

    def test_its_own_idle_emulator_is_named_once(self):
        self.ps(50, "bash", "state/slot-1", 0.01)
        self.ps(51, "qemu-system-x86", "sdk", 3.6, ppid=50)  # launched from a slot, working in the SDK
        said = self.take([])
        self.assertEqual(said.count(HINT), 1)
        self.assertIn('qemu-system-x86 (3.6 GB) is this project\'s own: if its heavy jobs reuse it, add '
                      '"qemu-system-x86" to heavy_reuses in .windvane/config.toml', said)

    def test_another_checkouts_helper_gets_no_hint(self):
        self.ps(60, "qemu-system-x86", "other", 3.6)
        said = self.take([])
        self.assertIn("largest: pid 60 qemu-system-x86", said)
        self.assertNotIn(HINT, said)

    def test_a_helper_already_in_heavy_reuses_gets_no_hint(self):
        self.ps(70, "qemu-system-x86", "state/slot-1", 3.0)  # counted, and still short: 2.4 + 3.0 < 6
        said = self.take(["qemu-system"])
        self.assertIn("2.4 GB free + 3.0 GB in this project's helpers (want 6)", said)
        self.assertNotIn(HINT, said)

    def test_the_lock_holders_job_gets_no_hint(self):
        self.ps(80, "python3", "state/slot-1", 0.02)  # the job holding the lock: its build's memory is in use
        self.ps(81, "java", "state/slot-1", 3.0, ppid=80)
        lines = []
        entry = heavy.join_queue(6)
        lock = os.open(heavy.lock_path(), os.O_CREAT | os.O_RDWR, 0o600)
        try:
            os.write(lock, b'{"pid": 80}')
            heavy.take(lock, entry, 6, {"busy": []}, lambda: time.time() + 1.5 if not lines else 0, lines.append,
                       reuse=([], self.dirs))
        finally:
            heavy.leave_queue(entry)
            os.close(lock)
        self.assertNotIn(HINT, "\n".join(lines))

    def test_the_free_figure_rounds_down(self):
        self.assertEqual(heavy.short(6, 5.96), "only 5.9 GB free (want 6)")
        self.assertEqual(heavy.short(6, 2.4 - 1 / GB * 0.2), "only 2.4 GB free (want 6)")  # meminfo's whole kB
        self.assertEqual(heavy.short(6, 1.0, 4.99), "1.0 GB free + 4.9 GB in this project's helpers (want 6)")

    def test_status_marks_this_projects_holder(self):
        self.ps(90, "qemu-system-x86", "state/slot-1", 3.6)
        self.ps(91, "qemu-system-x86", "other", 3.2)
        entry = heavy.join_queue(6)
        try:
            with mock.patch.object(heavy, "alive", lambda path: True):
                lines = heavy.status(([], self.dirs))
                plain = heavy.status()
        finally:
            heavy.leave_queue(entry)
        self.assertIn("(waiting for 6 GB, 2.4 free; letting smaller jobs by)", "\n".join(lines))
        self.assertTrue(any("pid 90" in ln and ln.endswith("(this project's)") for ln in lines), lines)
        self.assertFalse(any("pid 91" in ln and "this project's" in ln for ln in lines), lines)
        self.assertNotIn("this project's", "\n".join(plain))

    def test_the_check_log_hint_is_read_back(self):
        log = self.d / "check.log"
        self.ps(50, "qemu-system-x86", "state/slot-1", 3.6)
        log.write_text(self.take([]) + "\nchecked\n")
        self.assertEqual(heavy.hints_in(log), ['qemu-system-x86 (3.6 GB) is this project\'s own: if its heavy jobs '
                                               'reuse it, add "qemu-system-x86" to heavy_reuses in '
                                               '.windvane/config.toml'])
        self.assertEqual(heavy.hints_in(self.d / "none.log"), [])
        log.write_text("FAIL: test_hint\n  " + self.take([]).splitlines()[0].split("; ", 1)[1] + "\n")
        self.assertEqual(heavy.hints_in(log), [])  # only the waiting line's own clause counts

    def test_two_holders_of_one_name_are_named_once(self):
        self.ps(50, "qemu-system-x86", "state/slot-1", 3.6)
        self.ps(51, "qemu-system-x86", "proj", 1.5)
        said = self.take([])
        self.assertEqual(said.count(HINT), 1, said)
        self.assertIn("qemu-system-x86 (3.6 GB) " + HINT, said)

    def test_two_own_helpers_both_reach_the_project_log(self):
        self.ps(50, "qemu-system-x86", "state/slot-1", 3.6)
        self.ps(51, "java", "proj", 1.5)
        log = self.d / "check.log"
        log.write_text(self.take([]) + "\n")
        self.assertEqual([h.split()[0] for h in heavy.hints_in(log)], ["qemu-system-x86", "java"])


    def test_windvane_heavy_in_a_checkout_says_it(self):
        repo = self.d / "repo"
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        (repo / ".windvane").mkdir()
        self.ps(50, "bash", "repo", 0.01)
        self.ps(51, "qemu-system-x86", "sdk", 3.6, ppid=50)
        err = io.StringIO()
        with mock.patch.object(cli, "toplevel", return_value=repo), redirect_stderr(err), \
                self.assertRaises(SystemExit) as e:
            cli.main(["heavy", "--mem", "6", "--", "true"])
        self.assertEqual(e.exception.code, heavy.TIMED_OUT)
        self.assertEqual(err.getvalue().count(HINT), 1, err.getvalue())
        self.assertIn('add "qemu-system-x86" to heavy_reuses', err.getvalue())
        out = io.StringIO()
        entry = heavy.join_queue(6)
        try:
            with mock.patch.object(cli, "toplevel", return_value=repo), mock.patch.object(heavy, "alive", bool), \
                    redirect_stdout(out):
                cli.main(["heavy", "--status"])
        finally:
            heavy.leave_queue(entry)
        self.assertIn("qemu-system-x86 3.6 GB in tmp/sdk (this project's)".replace("tmp", self.d.name), out.getvalue())

    def test_the_drivers_heavy_check_logs_it_to_the_project_log(self):
        p = SimpleNamespace(root=self.d / "proj", state=self.d / "state",
                            cfg={"check": "echo checked", "heavy_check": True, "heavy_mem": 6})
        self.ps(50, "qemu-system-x86", "state/slot-1", 3.6)
        shim = self.d / "python"
        shim.write_text(f"#!{sys.executable}\n" + SHIM.format(proc=str(self.proc)))
        shim.chmod(0o755)
        with mock.patch.object(sys, "executable", str(shim)), redirect_stdout(io.StringIO()):
            ok, summary, log = jobs.check(p, self.d / "proj", ticket="T-1")
        self.assertIsNone(ok, summary)
        self.assertEqual(log.read_text().count(HINT), 1, log.read_text())
        project_log = (p.state / "autopilot.log").read_text().splitlines()
        self.assertEqual([ln[17:] for ln in project_log if HINT in ln],
                         ['heavy check: qemu-system-x86 (3.6 GB) is this project\'s own: if its heavy jobs reuse it, '
                          'add "qemu-system-x86" to heavy_reuses in .windvane/config.toml'])


if __name__ == "__main__":
    unittest.main()
