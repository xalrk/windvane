"""`windvane bg --cancel <name>` ends the caller's own job, waiting for the machine or running: its process groups go,
its heavy queue place frees for the jobs behind it, and its log and exit say it was cancelled. `await` tells a heavy
job still in the queue (`WAITING`) from one running."""
import io
import os
import subprocess
import sys
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from windvane import heavy, jobs  # noqa: E402
from windvane.config import Project  # noqa: E402
from windvane.runner import IN_SANDBOX, NEST, TICKET  # noqa: E402
from helpers import Sandbox  # noqa: E402
from test_heavy_check_wait import hold  # noqa: E402


def group_gone(pgid):
    """No live process left in group `pgid`: a zombie the test process hasn't reaped (the job's shell is its child
    here, not init's as in real use) counts as gone."""
    out = subprocess.run(["ps", "-A", "-o", "pgid=,stat="], capture_output=True, text=True).stdout
    return not any(f.split()[0] == str(pgid) and not f.split()[1].startswith("Z") for f in out.splitlines() if f.strip())


class BgCancel(unittest.TestCase):
    def setUp(self):
        self.s = Sandbox()
        self.addCleanup(self.s.close)
        self.run_dir = self.s.tmp / "run"  # the machine's heavy lock and line, this test's own
        self.run_dir.mkdir()
        (self.s.tmp / "config/windvane").mkdir(parents=True, exist_ok=True)
        (self.s.tmp / "config/windvane/machine.toml").write_text("busy = []\nmin_free_gb = 0\nheavy_wait = 60\n")
        os.environ["XDG_RUNTIME_DIR"] = str(self.run_dir)
        for k in ("WINDVANE_HEAVY_HELD", jobs.RUN_STOP, heavy.STOP_AT, NEST, IN_SANDBOX):
            os.environ.pop(k, None)
        self.p = Project(self.s.root)
        self.addCleanup(self.end_all)
        grace = mock.patch.object(jobs, "GRACE", 1)  # the job's shell is a zombie until reaped: see group_gone
        grace.start()
        self.addCleanup(grace.stop)

    def end_all(self):
        for base in (self.p.state / "bg").glob("*/*.start"):
            if jobs.alive(base.with_suffix("")):
                jobs.end_job(base.with_suffix(""), "the test's end")

    def until(self, ok, timeout=30, what="the condition"):
        end = time.time() + timeout
        while not ok():
            self.assertLess(time.time(), end, f"timed out waiting for {what}")
            time.sleep(0.05)

    def call(self, ticket, fn, *args, **kw):
        """`fn(p, *args)` as `ticket`'s session: (its output, its exit message or None)."""
        os.environ[TICKET] = ticket
        out = io.StringIO()
        try:
            with redirect_stdout(out):
                fn(self.p, *args, **kw)
        except SystemExit as e:
            return out.getvalue(), str(e.code)
        return out.getvalue(), None

    def bg(self, ticket, name, script, **kw):
        out, err = self.call(ticket, jobs.bg, name, ["bash", "-c", script], str(self.s.root), **kw)
        self.assertIsNone(err, err)
        return self.p.state / "bg" / ticket / name

    def await_(self, ticket, name):
        return self.call(ticket, jobs.await_, name, 3, 0)[0].splitlines()[0]

    def queued(self, base):
        self.until(lambda: heavy.waiting(f"{base}.log"), what=f"{base.name} waiting in the heavy queue")

    def entries(self):
        q = heavy.queue_dir()
        return [e for e in q.iterdir() if not e.name.startswith(".") and heavy.alive(e)] if q.is_dir() else []

    def assert_cancelled(self, base):
        pid = int(Path(f"{base}.pid").read_text())
        self.assertTrue(group_gone(pid), "the job's process group still runs")
        self.assertFalse(jobs.alive(base))
        self.assertEqual(Path(f"{base}.exit").read_text().strip(), str(jobs.KILLED))
        self.assertEqual(jobs.tail(f"{base}.log", 1), jobs.CANCELLED)

    def test_a_queued_heavy_job_is_cancelled_and_the_one_behind_moves_up(self):
        holder = hold(self, self.run_dir)
        order = self.s.tmp / "order"
        mine = self.bg("T-001", "eval", f"echo A >> {order}", heavy=True)
        self.queued(mine)
        theirs = self.bg("T-002", "eval", f"echo B >> {order}; sleep 60", heavy=True)
        self.queued(theirs)
        self.until(lambda: len(self.entries()) == 2, what="both in the queue")
        self.assertTrue(self.await_("T-002", "eval").startswith("WAITING eval (in the heavy queue, 0 min so far)"))

        out, err = self.call("T-001", jobs.cancel, "eval")
        self.assertIsNone(err)
        self.assertEqual(out.strip(), "cancelled 'eval' (was waiting for the machine, 0 min after it started)")
        self.assert_cancelled(mine)
        self.assertEqual(len(self.entries()), 1, "the cancelled job's queue entry is still there")
        self.assertTrue(jobs.alive(theirs), "another ticket's job of the same name was touched")
        self.assertFalse(Path(f"{theirs}.exit").exists())

        out, err = self.call("T-001", jobs.bg, "eval", ["bash", "-c", f"echo A >> {order}"], str(self.s.root),
                             heavy=True)  # the name starts again at once, now behind T-002's
        self.assertIsNone(err, err)
        self.assertIn("started 'eval'", out)
        (self.run_dir / "release").touch()
        holder.wait(10)
        self.until(lambda: order.exists(), what="the job behind the cancelled one to run")
        self.until(lambda: heavy.WAITED_RE.search(heavy.log_head(f"{theirs}.log")), what="its waited line")
        self.assertTrue(self.await_("T-002", "eval").startswith("RUNNING eval (0 min so far)"))
        self.assertEqual(order.read_text(), "B\n")

        out, err = self.call("T-002", jobs.cancel, "eval")  # a running heavy job frees the lock for the next
        self.assertIn("(was running,", out)
        self.assert_cancelled(theirs)
        self.until(lambda: order.read_text() == "B\nA\n", what="the requeued job to run")

    def test_a_running_job_that_ignores_sigterm_is_killed_after_the_grace(self):
        base = self.bg("T-001", "loop", "trap 'echo got TERM' TERM; echo started; while :; do sleep 0.1; done")
        self.until(lambda: "started" in jobs.tail(f"{base}.log"), what="the job to start")
        t0 = time.time()
        out, err = self.call("T-001", jobs.cancel, "loop")
        self.assertIsNone(err)
        self.assertIn("cancelled 'loop' (was running,", out)
        self.assertGreaterEqual(time.time() - t0, 1, "it was killed before the grace")
        self.assert_cancelled(base)
        self.assertIn("got TERM", jobs.tail(f"{base}.log"))  # what it said as it died comes before the last line

    def test_a_kept_job_cancelled_no_longer_holds_its_ticket(self):
        hold(self, self.run_dir)
        base = self.bg("T-001", "eval", "sleep 60", heavy=True, keep=True)
        self.queued(base)
        self.assertEqual(jobs.running_kept(self.p, "T-001"), [base])
        self.call("T-001", jobs.cancel, "eval")
        self.assert_cancelled(base)
        self.assertEqual(jobs.running_kept(self.p, "T-001"), [])
        [line] = jobs.ended_kept(self.p, "T-001")
        self.assertTrue(line.startswith("- 'eval' cancelled (`bg --cancel`) after 0 min"), line)

    def test_cancelling_what_is_not_running_is_an_error(self):
        out, err = self.call("T-001", jobs.cancel, "eval")
        self.assertEqual(err, "bg --cancel: nothing started as 'eval' for T-001")
        base = self.bg("T-001", "eval", "exit 3")
        self.until(lambda: Path(f"{base}.exit").exists() and not jobs.alive(base), what="the job to end")
        out, err = self.call("T-001", jobs.cancel, "eval")
        self.assertEqual(err, f"bg --cancel: 'eval' already ended (exit 3); log {base}.log")
        self.assertEqual(Path(f"{base}.exit").read_text().strip(), "3")

    def test_inside_the_sandbox_it_refuses(self):
        base = self.bg("T-001", "loop", "sleep 60")
        os.environ[IN_SANDBOX] = "1"
        out, err = self.call("T-001", jobs.cancel, "loop")
        self.assertIn("can't run inside the Bash sandbox", err)
        self.assertTrue(jobs.alive(base))


if __name__ == "__main__":
    unittest.main()
