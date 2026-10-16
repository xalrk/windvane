"""R-024: a small heavy job waiting on machine.toml's `min_free_gb` (no `--mem`, no `heavy_mem`) says once that its
need is that default and how to lower it, and `await` on a heavy job not yet started says it's all been waiting."""
import io
import itertools
import os
import shutil
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
import helpers  # noqa: E402,F401  (drops the sandbox flag the tests may run under)
from windvane import heavy, jobs, scratch, util  # noqa: E402
from windvane.runner import IN_SANDBOX, TICKET  # noqa: E402

GB = 1048576  # kB
CLAUSE = ("is machine.toml's min_free_gb: if this job needs less, give it --mem <GB> "
          "(or set heavy_mem in .windvane/config.toml)")


class DefaultNeed(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="windvane-")
        self.addCleanup(tmp.cleanup)
        d = Path(tmp.name)
        proc = d / "proc"
        proc.mkdir()
        (proc / "meminfo").write_text(f"MemTotal: {32 * GB} kB\nMemAvailable: {int(3.35 * GB)} kB\n")
        (d / "cfg/windvane").mkdir(parents=True)
        (d / "cfg/windvane/machine.toml").write_text("busy = []\nmin_free_gb = 6\nheavy_wait = 60\n")
        env = {"XDG_RUNTIME_DIR": str(d), "XDG_CONFIG_HOME": str(d / "cfg"), "XDG_STATE_HOME": str(d / "st")}
        for patch in (mock.patch.object(util, "PROC", proc), mock.patch.dict(os.environ, env)):
            patch.start()
            self.addCleanup(patch.stop)
        for k in ("WINDVANE_HEAVY_HELD", "WINDVANE_STOP_AT", TICKET):  # a session running the tests has them
            os.environ.pop(k, None)

    def run_job(self, mem):
        """What `heavy.run` said for a job asking `mem` GB (None: no `--mem` and no `heavy_mem`) that waited a
        moment, short of memory, and gave up."""
        err = io.StringIO()
        with redirect_stderr(err):
            code = heavy.run(["true"], wait_s=1.2, mem=mem)
        self.assertEqual(code, heavy.TIMED_OUT)
        return err.getvalue()

    def test_the_default_need_is_named_once(self):
        said = self.run_job(None)
        self.assertIn("only 3.3 GB free (want 6)", said)
        self.assertEqual(said.count(CLAUSE), 1, said)
        self.assertIn(f"the 6 GB {CLAUSE}", said)

    def test_a_need_of_its_own_gets_no_clause(self):
        """`--mem 6` and a project's `heavy_mem = 6` reach `run` the same way: as its `mem`."""
        said = self.run_job(6)
        self.assertIn("only 3.3 GB free (want 6)", said)
        self.assertNotIn(CLAUSE, said)

    def test_take_says_it_again_only_after_a_new_wait(self):
        """Once per wait: a memory line said again as what it waits on changes doesn't repeat the clause."""
        lines, frees = [], itertools.chain([3.3, 3.3, 9.0], itertools.repeat(3.3))
        entry = heavy.join_queue(6)
        lock = os.open(heavy.lock_path(), os.O_CREAT | os.O_RDWR, 0o600)
        os.write(lock, b'{"pid": 1}')  # held, so free memory alone doesn't let it start
        try:
            with mock.patch.object(heavy, "free_gb", lambda: next(frees)), \
                    mock.patch.object(heavy.fcntl, "flock", side_effect=OSError), \
                    mock.patch.object(heavy.time, "sleep"):
                end = time.time() + 1
                heavy.take(lock, entry, 6, {"busy": []}, lambda: end, lines.append, default=True)
        finally:
            heavy.leave_queue(entry)
            os.close(lock)
        memory = [ln for ln in lines if ln.startswith("heavy: waiting: only")]
        self.assertGreaterEqual(len(memory), 2, lines)
        self.assertEqual("\n".join(lines).count(CLAUSE), 1, lines)


class AwaitWaiting(unittest.TestCase):
    def setUp(self):
        d = Path(tempfile.mkdtemp(prefix=scratch.PREFIX))
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        self.base = d / "decode"
        Path(f"{self.base}.start").write_text(str(int(time.time()) - 48 * 60))
        Path(f"{self.base}.pid").write_text(str(os.getpid()))
        for patch in (mock.patch.object(jobs, "find_job", return_value=(self.base, None)),
                      mock.patch.object(jobs, "ledger"),
                      mock.patch.dict(os.environ, {k: v for k, v in os.environ.items()
                                                   if k not in (IN_SANDBOX, TICKET)}, clear=True)):
            patch.start()
            self.addCleanup(patch.stop)

    def await_(self, log):
        Path(f"{self.base}.log").write_text(log)
        out = io.StringIO()
        with redirect_stdout(out):
            jobs.await_(SimpleNamespace(), "decode", limit=0)
        return out.getvalue().splitlines()[0]

    def test_a_heavy_job_not_started_says_it_is_waiting(self):
        line = self.await_("heavy: waiting: only 3.3 GB free (want 6); largest: pid 9 qemu (4.0 GB)\n")
        self.assertEqual(line, "WAITING decode (in the heavy queue, 48 min so far); call windvane await decode again")

    def test_once_it_got_the_machine_it_says_plain_running(self):
        line = self.await_("heavy: waiting: only 3.3 GB free (want 6)\n" + heavy.WAITED.format(2880) + "\nframe 1\n")
        self.assertEqual(line, "RUNNING decode (48 min so far); call windvane await decode again")

    def test_a_job_that_never_waited_says_plain_running(self):
        line = self.await_("frame 1\nframe 2\n")
        self.assertEqual(line, "RUNNING decode (48 min so far); call windvane await decode again")


if __name__ == "__main__":
    unittest.main()
