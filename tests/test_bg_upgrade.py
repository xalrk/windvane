"""R-008: a bg job a ticket's session started under an older windvane (at the unscoped `bg/<name>`, before job_base
scoped jobs by ticket) stays findable after windvane updates itself mid-job, but only when it can be tied to the
caller's worktree; any other unscoped job isn't adopted, and `await` names it in one line."""
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from windvane import jobs, scratch  # noqa: E402
from windvane.runner import NEST, TICKET  # noqa: E402


class BgUpgrade(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp(prefix=scratch.PREFIX)).resolve()
        self.addCleanup(shutil.rmtree, self.d, ignore_errors=True)
        self.p = SimpleNamespace(state=self.d / "state")
        self.work, self.other = self.d / "work", self.d / "other"  # this ticket's worktree, and another's
        for w in (self.work, self.other):
            w.mkdir()
            subprocess.run(["git", "init", "-q", str(w)], check=True)
        (self.work / "sub").mkdir()
        env = {k: v for k, v in os.environ.items() if k not in (TICKET, NEST)}
        patch = mock.patch.dict(os.environ, {**env, TICKET: "T-1"}, clear=True)
        patch.start()
        self.addCleanup(patch.stop)
        here = os.getcwd()
        os.chdir(self.work / "sub")  # a worker may call from anywhere in its worktree
        self.addCleanup(os.chdir, here)
        self.old = self.p.state / "bg" / "check"
        self.old.parent.mkdir(parents=True)

    def old_job(self, cwd, alive=True, exit=None):
        """A job as an older windvane left it: pid and start, no cwd record."""
        proc = subprocess.Popen(["sleep", "30"] if alive else ["true"], cwd=cwd)
        self.addCleanup(lambda: (proc.kill(), proc.wait()))
        if not alive:
            proc.wait()
        Path(f"{self.old}.pid").write_text(str(proc.pid))
        Path(f"{self.old}.start").write_text("0")
        Path(f"{self.old}.log").write_text("old job output\n")
        if exit is not None:
            Path(f"{self.old}.exit").write_text(f"{exit}\n")
        return proc

    def call(self, fn, *args):
        out = io.StringIO()
        with redirect_stdout(out):
            fn(self.p, *args)
        return out.getvalue()

    def await_(self):
        return self.call(jobs.await_, "check", 40, 1)

    def test_the_repro_finds_the_old_job_and_starts_no_second_one(self):
        self.old_job(self.work)
        self.assertIn("RUNNING check", self.await_())
        with self.assertRaises(SystemExit) as e:
            self.call(jobs.bg, "check", ["true"], str(self.work))
        self.assertIn("'check' is still running", str(e.exception))
        self.assertFalse((self.p.state / "bg" / "T-1").exists())

    def test_a_finished_job_with_a_recorded_cwd_is_awaited(self):
        Path(f"{self.old}.cwd").write_text(str(self.work / "sub"))
        self.old_job(self.d, alive=False, exit=3)  # its pid is gone: only the record ties it
        out = self.await_()
        self.assertIn("DONE check exit 3", out)
        self.assertIn("old job output", out)

    def test_another_worktrees_job_isnt_adopted_and_is_named(self):
        self.old_job(self.other)
        with self.assertRaises(SystemExit) as e:
            self.await_()
        first, second = str(e.exception).splitlines()
        self.assertEqual(first, "await: nothing started as 'check' for T-1")
        self.assertIn("): running; log " + str(self.old) + ".log", second)
        out = self.call(jobs.bg, "check", ["bash", "-c", "echo mine"], str(self.work))
        self.assertIn(str(self.p.state / "bg" / "T-1" / "check.log"), out)
        self.assertIn("mine", self.await_())

    def test_a_dead_job_with_nothing_to_tie_it_is_named_with_its_exit(self):
        self.old_job(self.work, alive=False, exit=2)
        with self.assertRaises(SystemExit) as e:
            self.await_()
        self.assertIn("): exited 2; log", str(e.exception))

    def test_bg_records_the_cwd_and_the_ticket_keeps_its_own_path(self):
        self.call(jobs.bg, "check", ["true"], str(self.work / "sub"))
        base = self.p.state / "bg" / "T-1" / "check"
        self.assertEqual(Path(f"{base}.cwd").read_text(), str(self.work / "sub"))
        self.assertIn("DONE check exit 0", self.await_())


if __name__ == "__main__":
    unittest.main()
