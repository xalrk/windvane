"""A `bg` job whose starter was killed before its command ran reads as never started in `await` (its own line and exit
code), not as a command that exited 1; one that ran and exited 1 still reads `DONE ... exit 1`. A name's lock file goes
with each start, without ever letting two callers hold different files for one name. And `doctor` names its clean
whenever some of the state dir can go, not only past the warning size."""
import io
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from windvane import doctor, jobs  # noqa: E402
from test_bg_locks import CutCaller, InProject  # noqa: E402
from test_statedir import OLD, Base  # noqa: E402
from test_temp_clean import age  # noqa: E402


class NeverStarted(CutCaller):
    def test_a_starter_killed_before_the_pid_reads_as_never_started(self):
        with redirect_stdout(io.StringIO()):  # an earlier run of the name: its log mustn't pass for the new one's
            jobs.bg(self.p, "j", ["echo", "the earlier run"], str(self.s.root))
        self.assertEqual(self.await_j(20)[0], "0")
        self.cut("before", "sleep", "30")
        self.assertFalse(Path(f"{self.base}.pid").exists())
        code, out = self.await_j(20)
        self.assertEqual(code, jobs.NEVER, out)
        self.assertEqual(out.strip(), "NEVER STARTED j: its start was killed before the command ran; start it again "
                                      "with windvane bg j sleep 30")
        self.assertNotIn("exit 1", out)
        self.assertFalse(Path(f"{self.base}.exit").exists(), "await made up an exit for it")
        self.assertEqual(self.windvane("await", "j")[0], jobs.NEVER_STARTED)  # asked again: the same answer
        with redirect_stdout(io.StringIO()):  # the name is free to start again
            jobs.bg(self.p, "j", ["true"], str(self.s.root))
        self.assertEqual(self.await_j(20)[0], "0")

    def test_a_command_that_exits_1_still_reads_done_exit_1(self):
        with redirect_stdout(io.StringIO()):
            jobs.bg(self.p, "j", ["sh", "-c", "echo failing; exit 1"], str(self.s.root))
        code, out = self.await_j(20)
        self.assertEqual(code, "1", out)
        self.assertTrue(out.startswith("DONE j exit 1 after 0 min\n"), out)
        self.assertIn("failing", out)
        self.assertEqual(self.windvane("await", "j")[0], 0)

    def test_a_command_killed_silently_ran_so_it_isnt_never_started(self):
        subprocess.run(["windvane", "bg", "j", "sleep", "30"], cwd=self.s.root, capture_output=True, check=True,
                       timeout=30)  # its own starter: a shell this test started would stay a zombie, alive to `await`
        end = time.time() + 5
        while not Path(f"{self.base}.log").exists() and time.time() < end:
            time.sleep(0.05)
        os.killpg(int(Path(f"{self.base}.pid").read_text()), 9)  # the shell too: no exit of its own written
        code, out = self.await_j(20)
        self.assertEqual(code, "1", out)
        self.assertTrue(out.startswith("DONE j exit 1"), out)

    def test_a_never_started_check_says_to_run_the_check_again(self):
        self.assertIn("start it again with windvane check", jobs.never_line(jobs.SESSION_CHECK, self.base))

    def test_the_line_names_the_command_with_its_flags_or_says_it_cant(self):
        self.base.parent.mkdir(parents=True, exist_ok=True)
        self.assertIn("start it again with windvane bg j <its command>", jobs.never_line("j", self.base))
        with redirect_stdout(io.StringIO()):
            jobs.bg(self.p, "j", ["sh", "-c", "echo it's done"], str(self.s.root))
        self.await_j(20)
        self.assertTrue(jobs.never_line("j", self.base).endswith(
            "start it again with windvane bg j sh -c 'echo it'\"'\"'s done'"))


class TwoStarts(InProject):
    def setUp(self):
        super().setUp()
        self.project("true")

    def test_two_bg_calls_together_leave_one_job_no_stray_pid_and_no_lock(self):
        for name in ("a", "b", "c", "d", "e"):
            calls = [subprocess.Popen(["windvane", "bg", name, "sleep", "20"], cwd=self.s.root, text=True,
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE) for _ in range(2)]
            outs = [c.communicate(timeout=60) for c in calls]
            self.assertEqual(sorted(c.returncode for c in calls), [0, 1], outs)
            self.assertEqual(sum(f"started '{name}'" in o for o, _ in outs), 1, outs)
            self.assertEqual([e.strip() for _, e in outs if e.strip()],
                             [f"bg: '{name}' is still running; wait with: windvane await {name}"])
            base = self.p.state / "bg" / name
            self.assertTrue(jobs.alive(base))
            left = sorted(f.name for f in base.parent.glob(f"{name}.*") if f.suffix in (".pid", ".lock")
                          or ".pid." in f.name)
            self.assertEqual(left, [f"{name}.pid"], "a dangling pid or lock file")


class NameLock(unittest.TestCase):
    def setUp(self):
        self.path = Path(self.enterContext(tempfile.TemporaryDirectory())) / "j.lock"

    def test_it_goes_with_its_holder(self):
        with jobs.name_lock(self.path):
            self.assertTrue(self.path.exists())
        self.assertFalse(self.path.exists())

    def test_a_waiter_on_the_removed_file_takes_the_new_one_so_no_two_hold_the_name(self):
        """B opens the file while A holds it, and gets its flock only once A removed it: B must take the file now at
        the path, else C, opening the path, would lock a different file and hold the name beside B."""
        b_in, b_go, c_in = threading.Event(), threading.Event(), threading.Event()

        def b():
            with jobs.name_lock(self.path):
                b_in.set()
                b_go.wait(10)

        def c():
            with jobs.name_lock(self.path):
                c_in.set()

        real_open, opened = open, threading.Event()

        def spy(*a, **kw):  # B's first open: the file A holds
            f = real_open(*a, **kw)
            opened.set()
            return f
        with jobs.name_lock(self.path):
            jobs.open = spy
            try:
                tb = threading.Thread(target=b)
                tb.start()
                self.assertTrue(opened.wait(5))
            finally:
                del jobs.open
            time.sleep(0.2)  # B waits on A's file
        self.assertTrue(b_in.wait(5))
        self.assertTrue(self.path.exists(), "B holds a file no longer at the path")
        tc = threading.Thread(target=c)
        tc.start()
        self.assertFalse(c_in.wait(0.5), "C held the name beside B")
        b_go.set()
        self.assertTrue(c_in.wait(5))
        tb.join(5), tc.join(5)
        self.assertFalse(self.path.exists())

    def test_a_file_someone_else_removed_isnt_the_holders_to_remove(self):
        with jobs.name_lock(self.path):
            self.path.unlink()
            self.path.write_text("")  # another's, now at the path
        self.assertTrue(self.path.exists())


class DoctorNamesTheClean(Base):
    def test_a_small_reclaimable_state_names_the_clean(self):
        small = self.p.state / "critic" / "T-001"
        small.mkdir(parents=True)
        (small / "notes.md").write_text("a pack\n")
        age(small, OLD)
        (level, what, fix), = doctor.state_warnings(self.p)
        self.assertEqual((level, fix), ("ok", "windvane doctor --clean"))
        self.assertIn("can go", what)


if __name__ == "__main__":
    unittest.main()
