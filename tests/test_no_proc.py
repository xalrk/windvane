"""T-134: every path that reads /proc on Linux takes its macOS branch here when util.PROC points at an empty dir: against
this machine's real `ps` where it exists, and a fake `ps` that's missing, hangs or prints nonsense, which must cost a
wait no longer than util.PS_WAIT and never an exception."""
import contextlib
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from windvane import heavy, jobs, requests, scratch, util  # noqa: E402

HAS_PROC = Path("/proc/self/stat").exists()
# Fake `ps`es: one that never answers (exec, so the timeout's kill ends it; a full path, as PATH holds only the fake)
# and one that prints nonsense.
HUNG = f"#!/bin/sh\nexec {shutil.which('sleep') or '/bin/sleep'} 30\n"
GARBLED = "#!/bin/sh\necho 'ps: what?'\n"


class NoProc(unittest.TestCase):
    """/proc hidden, and heavy.paused's cache emptied before and after so no answer leaks between tests."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="windvane-")
        self.addCleanup(tmp.cleanup)
        self.d = Path(tmp.name)
        self.real = {"ancestors": requests.ancestors() if HAS_PROC else None,
                     "free": heavy.free_gb() if HAS_PROC else None}
        for patch in (mock.patch.object(util, "PROC", self.d / "no-proc"), mock.patch.dict(heavy._ps_seen, clear=True)):
            patch.start()
            self.addCleanup(patch.stop)

    def child(self, *cmd):
        c = subprocess.Popen(cmd or ["sleep", "30"])
        self.addCleanup(lambda: (c.kill(), c.wait()))
        return c

    def dead_pid(self):
        c = subprocess.Popen(["true"])
        c.wait()
        return c.pid


@unittest.skipUnless(shutil.which("ps"), "no ps on this system")
class RealPs(NoProc):
    def test_started_at_is_when_a_child_started(self):
        before = time.time()
        c = self.child()
        t = util.started_at(c.pid)
        self.assertIsNotNone(t)
        self.assertGreaterEqual(t, int(before) - 1)
        self.assertLessEqual(t, time.time() + 1)
        self.assertIsNone(util.started_at(self.dead_pid()))

    def test_paused_sees_a_stopped_child_and_not_a_running_one(self):
        c = self.child()
        self.assertFalse(heavy.paused(c.pid))
        os.kill(c.pid, signal.SIGSTOP)
        self.addCleanup(os.kill, c.pid, signal.SIGCONT)
        heavy._ps_seen.clear()  # the running answer is kept PS_KEEP seconds
        deadline = time.time() + 5
        while not heavy.paused(c.pid) and time.time() < deadline:  # SIGSTOP lands a moment later
            heavy._ps_seen.clear()
            time.sleep(0.05)
        self.assertTrue(heavy.paused(c.pid))
        self.assertFalse(heavy.paused(self.dead_pid()))

    def test_ancestors_agree_with_proc(self):
        chain = requests.ancestors()
        self.assertEqual(chain[0], os.getppid())
        self.assertNotIn(1, chain)
        if self.real["ancestors"] is None:
            self.skipTest("no /proc to compare with")
        self.assertEqual(chain, self.real["ancestors"])

    def test_process_session_is_found_through_ps(self):
        """The parent chain and its start times, both from `ps`, tie this process to the session its parent recorded."""
        nested = self.d / "state" / "windvane" / "proj" / "nested" / "s1"
        nested.mkdir(parents=True)
        (nested / "session").write_text(f"{os.getppid()}\n")
        (nested / "role").write_text("worker:T-1\n")
        with mock.patch.dict(os.environ, XDG_STATE_HOME=str(self.d / "state")):
            self.assertEqual(requests.process_session(), ("s1", "worker:T-1"))


class NoPs(NoProc):
    """`ps` missing, hung or garbled: each path gives its "don't know" answer, within PS_WAIT."""

    @contextlib.contextmanager
    def fake(self, name, script):
        """PATH holding only the fake `ps` (none when `script` is None), PS_WAIT short, and an empty cache."""
        bin_ = self.d / f"bin-{name}"
        bin_.mkdir()
        if script is not None:
            (bin_ / "ps").write_text(script)
            (bin_ / "ps").chmod(0o755)
        heavy._ps_seen.clear()
        with mock.patch.dict(os.environ, PATH=str(bin_)), mock.patch.object(util, "PS_WAIT", 0.5):
            yield

    def each(self, check):
        for name, script in (("missing", None), ("hung", HUNG), ("garbled", GARBLED)):
            with self.subTest(ps=name), self.fake(name, script):
                t = time.monotonic()
                check()
                took = time.monotonic() - t
                self.assertLess(took, 5)
                if script == HUNG:  # it really hung, and was given up on
                    self.assertGreaterEqual(took, util.PS_WAIT)

    def test_started_at_is_none(self):
        self.each(lambda: self.assertIsNone(util.started_at(os.getpid())))

    def test_paused_is_false_and_kept(self):
        def check():
            self.assertFalse(heavy.paused(os.getpid()))
            self.assertIn(os.getpid(), heavy._ps_seen)  # a hung ps isn't asked again on the next poll
        self.each(check)

    def test_ancestors_stop_at_the_parent(self):
        self.each(lambda: self.assertEqual(requests.ancestors(), [os.getppid()]))

    def test_process_session_is_not_found(self):
        nested = self.d / "state" / "windvane" / "proj" / "nested" / "s1"

        def check():
            nested.mkdir(parents=True, exist_ok=True)
            (nested / "session").write_text(f"{os.getppid()}\n")
            with mock.patch.dict(os.environ, XDG_STATE_HOME=str(self.d / "state")):
                self.assertEqual(requests.process_session(), ("", ""))
        self.each(check)


class NoPsNeeded(NoProc):
    """The paths with no `ps` branch: without /proc they know nothing, and say so instead of failing."""

    def test_free_memory_is_unknown_and_never_waited_on(self):
        self.assertIsNone(heavy.free_gb())
        self.assertIsNone(heavy.short(10 ** 6, heavy.free_gb()))
        if self.real["free"] is None:
            self.skipTest("no /proc to compare with")
        self.assertGreater(self.real["free"], 0)


if __name__ == "__main__":
    unittest.main()


LSOF = shutil.which("lsof") or next((x for x in ("/usr/sbin/lsof",) if os.access(x, os.X_OK)), None)


@unittest.skipUnless(LSOF and shutil.which("ps"), "no lsof or ps on this system")
class LsofAndPs(NoProc):
    """Which dirs processes work in, as macOS tells it: `lsof` for each one's cwd, `ps` for its TMPDIR."""

    def test_an_older_jobs_cwd_comes_from_its_live_pid(self):
        here = self.d / "worktree"
        here.mkdir()
        c = self.child("sh", "-c", f"cd {here} && exec sleep 30")
        base = self.d / "bg" / "check"
        base.parent.mkdir()
        Path(f"{base}.pid").write_text(str(c.pid))
        for _ in range(50):  # until the shell has changed dir
            if jobs.job_cwd(base) == os.path.realpath(here):
                break
            time.sleep(0.05)
        self.assertEqual(jobs.job_cwd(base), os.path.realpath(here))
        c.kill()
        c.wait()
        self.assertIsNone(jobs.job_cwd(base))  # dead: nothing ties it to anyone
        Path(f"{base}.cwd").write_text("/somewhere\n")
        self.assertEqual(jobs.job_cwd(base), "/somewhere")

    def test_scratch_keeps_an_old_dir_a_process_works_in_or_has_as_its_tmpdir(self):
        cwd, env, idle = (self.d / f"windvane-{n}" for n in ("cwd", "env", "idle"))
        for d in (cwd, env, idle):
            d.mkdir()
            os.utime(d, (time.time() - 2 * scratch.AGE,) * 2)
        self.child("sh", "-c", f"cd {cwd} && exec sleep 30")
        b = subprocess.Popen(["sleep", "30"], env={**os.environ, "TMPDIR": str(env)})
        self.addCleanup(lambda: (b.kill(), b.wait()))
        for _ in range(50):
            if scratch.stale([cwd, env, idle]) == [idle]:
                break
            time.sleep(0.05)
        self.assertEqual(scratch.stale([cwd, env, idle]), [idle])
        os.utime(idle)
        self.assertEqual(scratch.stale([idle]), [])  # the age rule still holds
