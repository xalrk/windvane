"""`windvane await` records an exit only for a job it saw end: inside the Bash sandbox (its own pid namespace) a
running job's pid looks gone, and an EPERM pid is a live process, so neither may become a false `exit 1`."""
import errno
import io
import os
import shutil
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import helpers  # noqa: E402,F401  (drops the sandbox flag the tests may run under)
from windvane import jobs, scratch  # noqa: E402
from windvane.runner import IN_SANDBOX, TICKET  # noqa: E402


def gone(pid, sig):
    raise ProcessLookupError(errno.ESRCH, "No such process")


def not_ours(pid, sig):
    raise PermissionError(errno.EPERM, "Operation not permitted")


class AwaitSandbox(unittest.TestCase):
    def setUp(self):
        d = Path(tempfile.mkdtemp(prefix=scratch.PREFIX))
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        self.base = d / "check"
        Path(f"{self.base}.start").write_text(str(int(time.time())))
        Path(f"{self.base}.pid").write_text(str(os.getpid()))
        Path(f"{self.base}.log").write_text("..........\n")
        self.exit_f = Path(f"{self.base}.exit")
        for patch in (mock.patch.object(jobs, "find_job", return_value=(self.base, None)),
                      mock.patch.object(jobs, "ledger"),
                      mock.patch.object(jobs.time, "sleep"),
                      mock.patch.dict(os.environ, {k: v for k, v in os.environ.items()
                                                   if k not in (IN_SANDBOX, TICKET)}, clear=True)):
            patch.start()
            self.addCleanup(patch.stop)

    def await_(self, limit=5):
        out = io.StringIO()
        with redirect_stdout(out):
            jobs.await_(SimpleNamespace(), "check", limit=limit)
        return out.getvalue()

    def test_in_the_sandbox_it_stops_before_reading_or_writing(self):
        """R-022's repro: the sandbox hides the running job's pid; await used to write exit 1 and say DONE."""
        os.environ[IN_SANDBOX] = "1"
        with mock.patch.object(jobs.os, "kill", side_effect=gone), \
                self.assertRaises(SystemExit) as stop, redirect_stdout(io.StringIO()) as out:
            jobs.await_(SimpleNamespace(), "check", limit=5)
        self.assertIn("can't run inside the Bash sandbox", str(stop.exception.code))
        self.assertNotIn("DONE", out.getvalue())
        self.assertFalse(self.exit_f.exists())

    def test_a_heavy_job_still_queued_gets_no_exit_from_the_sandbox(self):
        """R-025: a `bg --heavy` job waiting for the machine, awaited from the sandbox, isn't declared failed."""
        Path(f"{self.base}.log").write_text("heavy: waiting for 3 GB free\n")
        os.environ[IN_SANDBOX] = "1"
        with mock.patch.object(jobs.os, "kill", side_effect=gone), self.assertRaises(SystemExit):
            jobs.await_(SimpleNamespace(), "check", limit=5)
        self.assertFalse(self.exit_f.exists())

    def test_eperm_is_a_live_process(self):
        """EPERM: a process holds the pid (someone else's), so the job isn't known to be dead."""
        with mock.patch.object(jobs.os, "kill", side_effect=not_ours) as kill:
            out = self.await_(limit=0.2)  # sleeps are patched out: it looks again and again for 0.2 s
            self.assertGreater(kill.call_count, 1)
            self.assertTrue(jobs.alive(self.base))
        self.assertIn("RUNNING check", out)
        self.assertFalse(self.exit_f.exists())

    def test_a_pid_really_gone_still_gets_exit_1(self):
        with mock.patch.object(jobs.os, "kill", side_effect=gone):
            self.assertFalse(jobs.alive(self.base))
            out = self.await_()
        self.assertIn("DONE check exit 1", out)
        self.assertEqual(self.exit_f.read_text(), "1")

    def test_a_reused_pid_counts_as_gone(self):
        """A number a stranger took after `bg` wrote it, EPERM or not, is the job's death, as alive() tells."""
        later = Path(f"{self.base}.pid").stat().st_mtime + 60
        with mock.patch.object(jobs.os, "kill", side_effect=not_ours), \
                mock.patch.object(jobs.util, "started_at", return_value=later):
            self.assertFalse(jobs.alive(self.base))
            out = self.await_()
        self.assertIn("DONE check exit 1", out)


if __name__ == "__main__":
    unittest.main()
