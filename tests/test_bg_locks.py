"""`windvane bg` and a session's check through the ways they can be cut off: a reused pass that's pruned under a session
check, a `bg` caller killed holding the lock or before writing the pid, a session ending (or its driver restarting)
mid-check; merged tickets' job dirs pruned from the state dir; and on the `ps` path (macOS), a process started in the
run's own second not taken for one of its helpers."""
import io
import os
import signal
import shutil
import subprocess
import sys
import threading
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from windvane import cli, driver, jobs, runner, statedir, util  # noqa: E402
from windvane.config import Project  # noqa: E402
from windvane.heavy import STOP_AT, TIMED_OUT  # noqa: E402
from windvane.runner import IN_JOB, IN_SANDBOX, NEST, TICKET  # noqa: E402
from windvane.util import read_jsonl  # noqa: E402
from helpers import Sandbox  # noqa: E402
from test_statedir import OLD, Base  # noqa: E402
from test_temp_clean import age  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
LIMIT = 3  # seconds `await` blocks here, for its 570
UNSET = (NEST, IN_SANDBOX, TICKET, IN_JOB, STOP_AT, "WINDVANE_HEAVY_HELD")  # a suite run as a bg job is in one
# A `bg` caller that dies by SIGKILL right before (argv[2] "before") or right after (else) it starts the job's shell,
# so it never writes the pid: argv[1] is the project, the rest the job's command.
CUT = """import os, signal, subprocess, sys
sys.path.insert(0, %r)
from windvane import jobs
from windvane.config import Project
real = subprocess.Popen
def popen(args, **kw):
    if args[0] != "bash":
        return real(args, **kw)
    if sys.argv[2] == "before":
        os.kill(os.getpid(), signal.SIGKILL)
    proc = real(args, **kw)
    os.kill(os.getpid(), signal.SIGKILL)
jobs.subprocess.Popen = popen
jobs.bg(Project(sys.argv[1]), "j", sys.argv[3:], sys.argv[1])
""" % str(REPO)


def clean_env(test, **env):
    patch = mock.patch.dict(os.environ, env)
    patch.start()
    test.addCleanup(patch.stop)
    for k in UNSET:
        if k not in env:
            os.environ.pop(k, None)


class InProject(unittest.TestCase):
    def setUp(self):
        self.s = Sandbox()
        self.addCleanup(self.s.close)
        self.addCleanup(os.chdir, os.getcwd())
        clean_env(self)

    def project(self, check):
        conf = self.s.root / ".windvane/config.toml"
        self.s.write(".windvane/config.toml", conf.read_text().replace('check = "test -f README"', f'check = "{check}"'))
        self.s.publish("its check")
        self.p = Project(self.s.root)
        self.addCleanup(self.end_jobs)

    def end_jobs(self):
        for start in (self.p.state / "bg").rglob("*.start"):
            if jobs.alive(start.with_suffix("")):
                jobs.end_job(start.with_suffix(""), "the test's end")

    def windvane(self, *args):
        """`windvane <args>` in the project, in this process (so patches hold): (exit code, output)."""
        out = io.StringIO()
        with redirect_stdout(out), self.assertRaises(SystemExit) as e:
            cli.main(["-C", str(self.s.root), *args])
            raise SystemExit(0)
        code = e.exception.code
        return (code if isinstance(code, int) else 0 if code is None else 1), out.getvalue()


class ReusedPass(InProject):
    def test_a_pass_pruned_under_a_session_check_reports_the_failing_run(self):
        """The pass was there when the session's check looked, gone when `check` read it: the check ran right there
        and failed, and the session must not be told it passed."""
        self.project("test -f MISSING")
        os.environ.update({TICKET: "T-001", STOP_AT: str(time.time() + 600)})
        key = jobs.tree_key(self.p, self.s.root)
        (self.p.state / "passes").mkdir(parents=True)
        (self.p.state / "passes" / key).write_text("an older run\n")
        real = jobs.check

        def pruned(p, cwd, **kw):
            (p.state / "passes" / key).unlink()
            return real(p, cwd, **kw)

        with mock.patch.object(jobs, "check", pruned):
            code, out = self.windvane("check")
        self.assertEqual(code, 1, out)
        self.assertRegex(out.splitlines()[-1], "^CHECK FAILED")

    def test_a_reused_pass_still_exits_0(self):
        self.project("true")
        os.environ.update({TICKET: "T-001", STOP_AT: str(time.time() + 600)})
        key = jobs.tree_key(self.p, self.s.root)
        (self.p.state / "passes").mkdir(parents=True)
        (self.p.state / "passes" / key).write_text("an older run\n")
        code, out = self.windvane("check")
        self.assertEqual(code, 0, out)
        self.assertIn("CHECK PASSED (reused the pass", out)


class CutCaller(InProject):
    def setUp(self):
        super().setUp()
        self.project("true")
        self.base = self.p.state / "bg" / "j"

    def cut(self, when, *cmd):
        """A `bg j <cmd>` whose caller is killed `when` ("before"/"after") it starts the job's shell."""
        r = subprocess.run([sys.executable, "-c", CUT, str(self.s.root), when, *cmd], env=os.environ.copy(),
                           cwd=self.s.root, capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, -signal.SIGKILL, r.stderr)

    def await_j(self, limit):
        with redirect_stdout(io.StringIO()) as out:
            code = jobs.await_(self.p, "j", limit=limit)
        return code, out.getvalue()

    def test_killed_before_writing_the_pid_the_job_still_reads_as_running_and_its_exit_is_its_own(self):
        release = self.s.tmp / "release"
        self.cut("after", "sh", "-c", f"while [ ! -e {release} ]; do sleep 0.1; done; exit 7")
        end = time.time() + 5
        while not jobs.alive(self.base) and time.time() < end:  # its shell links its pid in as it starts
            time.sleep(0.05)
        self.assertTrue(jobs.alive(self.base))
        with redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as e:  # it holds the name
            jobs.bg(self.p, "j", ["true"], str(self.s.root))
        self.assertIn("still running", str(e.exception))
        release.touch()
        code, out = self.await_j(20)
        self.assertEqual(code, "7", out)
        self.assertFalse(list(self.base.parent.glob("j.pid.*")), "its pid's temp file stayed")

    def test_killed_before_starting_it_leaves_a_job_that_reads_as_ended(self):
        with redirect_stdout(io.StringIO()):  # an earlier job of the name, ended: its pid file stays behind
            jobs.bg(self.p, "j", ["true"], str(self.s.root))
        self.assertEqual(self.await_j(20)[0], "0")
        Path(f"{self.base}.pid.4242").write_text("4242\n")  # and an older shell, killed linking its pid in, left this
        self.cut("before", "sleep", "30")
        self.assertEqual(list(self.base.parent.glob("j.pid*")), [])
        self.assertFalse(jobs.alive(self.base))
        t = time.time()
        code, out = self.await_j(20)
        self.assertLess(time.time() - t, 5)
        self.assertEqual(code, jobs.NEVER, out)  # its command never ran: told at once, not RUNNING till the limit
        with redirect_stdout(io.StringIO()):
            jobs.bg(self.p, "j", ["true"], str(self.s.root))
        self.assertEqual(self.await_j(20)[0], "0")

    def test_a_lock_holder_killed_frees_the_next_bg(self):
        self.base.parent.mkdir(parents=True, exist_ok=True)
        holder = subprocess.Popen([sys.executable, "-c", "import fcntl, sys, time; f = open(sys.argv[1], 'a'); "
                                   "fcntl.flock(f, fcntl.LOCK_EX); print('held', flush=True); time.sleep(60)",
                                   f"{self.base}.lock"], stdout=subprocess.PIPE, text=True)
        self.addCleanup(lambda: (holder.kill(), holder.wait(), holder.stdout.close()))
        self.assertEqual(holder.stdout.readline().strip(), "held")
        done = threading.Event()

        def call():
            with redirect_stdout(io.StringIO()):
                jobs.bg(self.p, "j", ["true"], str(self.s.root))
            done.set()

        t = threading.Thread(target=call, daemon=True)
        t.start()
        self.assertFalse(done.wait(1), "bg didn't wait for the lock's holder")
        holder.send_signal(signal.SIGKILL)
        self.assertTrue(done.wait(10), "a killed holder's lock kept bg waiting")
        self.assertEqual(self.await_j(20)[0], "0")


class SessionCut(InProject):
    """A session's check is its bg job `check`: one the session's end killed is rerun by the next `windvane check`, one
    a restarted driver's next session finds running is awaited, not started twice."""

    def setUp(self):
        super().setUp()
        self.release = self.s.tmp / "release"
        self.project(f"while [ ! -e {self.release} ]; do sleep 0.1; done; test -f README")
        os.environ.update({TICKET: "T-001", STOP_AT: str(time.time() + 600), NEST: "session-1"})
        patch = mock.patch.object(jobs, "AWAIT_S", LIMIT)
        patch.start()
        self.addCleanup(patch.stop)
        self.addCleanup(self.release.touch)

    def starts(self):
        path = self.p.state / "ledger.jsonl"
        return [r for r in read_jsonl(path) if r["event"] == "check-start"] if path.exists() else []

    def passes(self):
        self.release.touch()
        code, out = self.windvane("check")
        for _ in range(10):
            if code != TIMED_OUT:
                break
            code, out = self.windvane("await", "check")
        self.assertEqual(code, 0, out)
        return out

    def test_a_check_its_session_ending_killed_is_run_again(self):
        code, out = self.windvane("check")
        self.assertEqual(code, TIMED_OUT, out)
        base = jobs.job_base(self.p, "check")
        self.assertTrue(jobs.alive(base))
        runner.kill_nested(self.p, "session-1")  # the session ended
        os.waitpid(int(Path(f"{base}.pid").read_text()), 0)  # reaped, as the `windvane check` that started it is gone
        self.assertFalse(jobs.alive(base))
        os.environ[NEST] = "session-2"
        self.passes()
        self.assertEqual(len(self.starts()), 2)

    def test_a_check_still_running_after_a_driver_restart_is_awaited(self):
        code, out = self.windvane("check")
        self.assertEqual(code, TIMED_OUT, out)
        os.environ[NEST] = "session-2"  # the driver died and its next run resumed the ticket: nothing killed it
        code, out = self.windvane("check")
        self.assertEqual(code, TIMED_OUT, out)
        self.assertIn("RUNNING check", out)
        self.passes()
        self.assertEqual(len(self.starts()), 1)


class PruneBg(Base):
    """`windvane clean` drops a merged ticket's bg dir (T-001 here) once old, unless a job in it runs or is kept."""

    def setUp(self):
        super().setUp()
        clean_env(self)
        self.addCleanup(self.end_jobs)

    def end_jobs(self):
        for start in (self.p.state / "bg").rglob("*.start"):
            if jobs.alive(start.with_suffix("")):
                jobs.end_job(start.with_suffix(""), "the test's end")

    def job(self, ticket, cmd=("true",), name="check"):
        with mock.patch.dict(os.environ, {TICKET: ticket} if ticket else {}), redirect_stdout(io.StringIO()):
            jobs.bg(self.p, name, list(cmd), str(self.s.root))
            base = jobs.job_base(self.p, name)
            if cmd == ("true",):
                jobs.await_(self.p, name, limit=20)
        return base

    def listed(self):
        return [i.what for i in statedir.reclaimable(self.p, procs=set())]

    def test_a_merged_tickets_old_bg_dir_goes_an_unmerged_ones_stays(self):
        self.job("T-001")
        self.job("T-002")
        self.assertEqual(self.listed(), [])  # merged, but this week's
        age(self.p.state / "bg", OLD)
        self.assertEqual(self.listed(), ["bg/ (1 merged ticket's job logs)"])
        statedir.clean(self.p, procs=set())
        self.assertEqual(sorted(d.name for d in (self.p.state / "bg").iterdir()), ["T-002"])

    def test_a_running_or_kept_job_keeps_its_dir(self):
        base = self.job("T-001", ("sleep", "30"))
        age(self.p.state / "bg", OLD)
        self.assertEqual(self.listed(), [])
        jobs.end_job(base, "ended")
        Path(f"{base}.keep").write_text(str(time.time() + 3600))  # kept on past the run's stop (heavy_keep_hours)
        age(self.p.state / "bg", OLD)
        self.assertEqual(self.listed(), [])
        Path(f"{base}.keep").write_text(str(time.time() - 60))  # its time is up
        age(self.p.state / "bg", OLD)
        self.assertEqual(self.listed(), ["bg/ (1 merged ticket's job logs)"])

    def test_the_desks_old_ended_jobs_go_a_running_one_stays(self):
        ended, running = self.job(None, name="build"), self.job(None, ("sleep", "30"), name="serve")
        Path(f"{ended}.pid.4242").write_text("4242\n")  # its shell was killed linking its pid in
        self.assertEqual(self.listed(), [])
        age(self.p.state / "bg", OLD)
        self.assertEqual(self.listed(), ["bg/ (1 desk job ended over 14 days ago)"])
        statedir.clean(self.p, procs=set())
        self.assertEqual({f.name.split(".")[0] for f in (self.p.state / "bg").iterdir() if f.is_file()}, {"serve"})
        self.assertTrue(jobs.alive(running))
        self.assertFalse(Path(f"{ended}.log").exists())
        self.assertFalse(Path(f"{ended}.pid.4242").exists())


@unittest.skipUnless(shutil.which("ps"), "no ps on this system")
class SameSecondOnPs(unittest.TestCase):
    """With /proc hidden (as on macOS), start times come from `ps lstart`, in whole seconds."""

    def setUp(self):
        tmp = Sandbox()
        self.addCleanup(tmp.close)
        self.root = tmp.tmp / "checkout"
        self.root.mkdir()
        self.p = SimpleNamespace(cfg={"isolate": False}, main_root=lambda: self.root)
        patch = mock.patch.object(util, "PROC", tmp.tmp / "no-proc")
        patch.start()
        self.addCleanup(patch.stop)
        c = subprocess.Popen(["sleep", "30"], cwd=self.root, start_new_session=True, stdin=subprocess.DEVNULL)
        self.addCleanup(lambda: (c.kill(), c.wait()))
        self.pid = c.pid
        patch = mock.patch.object(util, "cwds", lambda pid=None: {c.pid: str(self.root.resolve())})
        patch.start()
        self.addCleanup(patch.stop)
        self.t = util.started_at(c.pid)
        self.assertIsNotNone(self.t)
        self.assertEqual(self.t, int(self.t))

    def helpers(self, since):
        return [pid for pid, _ in driver.leftover_helpers(self.p, since)]

    def test_a_process_in_the_runs_own_second_isnt_a_helper(self):
        self.assertEqual(self.helpers(self.t), [])  # it may have started before the run, in the same second
        self.assertEqual(self.helpers(self.t + 0.5), [])

    def test_one_a_whole_second_later_is(self):
        self.assertEqual(self.helpers(self.t - util.PS_GRAIN), [self.pid])
        self.assertEqual(self.helpers(None), [])


if __name__ == "__main__":
    unittest.main()
