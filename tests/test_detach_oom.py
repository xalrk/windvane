"""A run outlives running out of memory: `--detach` starts it in a systemd scope with OOMPolicy=continue, the driver's
children are the kernel's first pick, a SIGTERM that follows an OOM kill says so, and a detached driver that is killed
(not stopped) is restarted, at most 3 times an hour."""
import contextlib
import fcntl
import io
import os
import signal
import subprocess
import sys
import textwrap
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from windvane import cli, doctor, driver, jobs, runner, supervise, util  # noqa: E402
from windvane.config import Project  # noqa: E402
from windvane.util import now  # noqa: E402
from helpers import Sandbox  # noqa: E402
from test_pipeline import args  # noqa: E402

LINUX = sys.platform.startswith("linux") and Path("/proc/self/oom_score_adj").exists()


class Base(unittest.TestCase):
    def setUp(self):
        self.addCleanup(os.chdir, os.getcwd())
        self.s = Sandbox()
        self.addCleanup(self.s.close)
        self.s.ticket("T-001")
        self.s.publish()
        self.p = Project(self.s.root)
        self.p.state.mkdir(parents=True, exist_ok=True)
        self.pushes = []
        orig, driver.push = driver.push, lambda title, msg="", *a, **k: self.pushes.append((title, msg))
        self.addCleanup(setattr, driver, "push", orig)

    def cgroup(self, kills):
        """A fake cgroup v2 tree whose memory.events counts `kills` OOM kills; call again to change the count."""
        proc, root = self.s.tmp / "proc", self.s.tmp / "cgroup"
        (proc / "self").mkdir(parents=True, exist_ok=True)
        (proc / "self" / "cgroup").write_text("0::/user.slice/run.scope\n")
        (root / "user.slice/run.scope").mkdir(parents=True, exist_ok=True)
        (root / "user.slice/run.scope/memory.events").write_text(f"low 0\nhigh 0\nmax 4\noom 2\noom_kill {kills}\n")
        for name, value in (("PROC", proc), ("CGROUP", root)):
            if getattr(supervise, name) != value:
                self.addCleanup(setattr, supervise, name, getattr(supervise, name))
                setattr(supervise, name, value)


class Detach(Base):
    def detach(self, systemd):
        """The tmux command `run --detach` starts, with or without a systemd user manager."""
        seen, orig = [], cli.subprocess.run
        cli.subprocess.run = lambda cmd, **kw: seen.append(cmd) if cmd[0] == "tmux" and "new-session" in cmd \
            else orig(cmd, **kw)
        self.addCleanup(setattr, cli.subprocess, "run", orig)
        self.addCleanup(setattr, sys, "argv", sys.argv)
        sys.argv = ["windvane", "run", "--for", "1h", "--detach"]
        with mock.patch.object(supervise.phone, "has_systemd", lambda: systemd), \
                contextlib.redirect_stdout(io.StringIO()):
            cli.main(["-C", str(self.s.root), "run", "--for", "1h", "--detach", "--no-preflight"])
        return seen[0][-1]

    @unittest.skipUnless(sys.platform.startswith("linux"), "systemd scopes are Linux's")
    def test_with_systemd_the_run_starts_in_a_scope_an_oom_kill_does_not_stop(self):
        cmd = self.detach(True)
        self.assertTrue(cmd.startswith('exec "systemd-run" "--user" "--scope" "--quiet" "-p" "OOMPolicy=continue" "--" '),
                        cmd)
        self.assertTrue(cmd.endswith('"--supervise"'))

    def test_without_systemd_it_starts_as_before(self):
        cmd = self.detach(False)
        self.assertNotIn("systemd-run", cmd)
        self.assertTrue(cmd.startswith("exec ") and cmd.endswith('"--supervise"'), cmd)

    @unittest.skipUnless(sys.platform.startswith("linux"), "systemd scopes are Linux's")
    def test_a_systemd_run_that_fails_means_no_scope(self):
        (self.s.bin / "systemd-run").write_text("#!/bin/sh\necho 'Failed to connect to bus' >&2\nexit 1\n")
        self.assertNotIn("systemd-run", self.detach(True))

    @unittest.skipUnless(sys.platform.startswith("linux"), "systemd is Linux's")
    def test_doctor_warns_when_a_unit_stops_on_oom_and_no_scope_helps(self):
        warned = lambda: [w for w in doctor.oom_warnings() if "out-of-memory" in w[1]]  # noqa: E731
        with mock.patch.object(supervise.phone, "has_systemd", lambda: True), \
                mock.patch.object(supervise, "default_policy", lambda: "stop"):
            self.assertEqual(warned(), [])  # systemd-run works: --detach uses it
            (self.s.bin / "systemd-run").write_text("#!/bin/sh\nexit 1\n")  # (not unlinked: /usr/bin has the real one)
            self.assertIn("a single runaway command will end a run", warned()[0][1])
            with mock.patch.object(supervise, "default_policy", lambda: "continue"):
                self.assertEqual(warned(), [])


class StopWhy(Base):
    def stop_reason(self, before, after):
        self.cgroup(before)
        pilot = driver.Pilot(self.p, args())
        self.cgroup(after)
        with self.assertRaises(driver.Signalled):
            pilot.on_signal(signal.SIGTERM, None)
        return pilot.stop_reason

    def test_a_sigterm_after_an_oom_kill_names_it_and_the_fix(self):
        why = self.stop_reason(0, 1)
        self.assertIn("the kernel's out-of-memory killer stopped a process in this run, and its service manager "
                      "stopped the run with it", why)
        self.assertIn("OOMPolicy=continue", why)
        self.assertIn("out-of-memory killer", (self.p.state / "autopilot.log").read_text())

    def test_a_sigterm_without_one_says_what_it_said(self):
        self.assertEqual(self.stop_reason(3, 3),
                         "the driver got SIGTERM (a kill or a shutdown); the running sessions were killed")

    def test_an_oom_kill_long_before_a_sigterm_is_not_blamed_for_it(self):
        """In a scope with OOMPolicy=continue a session's OOM kill is survived; a shutdown hours later is a shutdown."""
        self.cgroup(0)
        pilot = driver.Pilot(self.p, args())
        self.cgroup(1)
        pilot.oom.look()  # the run's looks every OOM_LOOK seconds catch up with it
        pilot.oom.look()
        with self.assertRaises(driver.Signalled):
            pilot.on_signal(signal.SIGTERM, None)
        self.assertTrue(pilot.stop_reason.startswith("the driver got SIGTERM (a kill or a shutdown)"))

    def test_no_cgroup_to_read_says_what_it_said(self):
        self.cgroup(0)
        (supervise.PROC / "self" / "cgroup").write_text("12:memory:/old-v1\n")
        self.assertIsNone(supervise.oom_kills())


@unittest.skipUnless(LINUX, "oom_score_adj is Linux's")
class Children(Base):
    def adj(self):
        return int(Path("/proc/self/oom_score_adj").read_text())

    def expected(self):
        return min(1000, self.adj() + util.OOM_STEP)

    def test_a_session_is_the_kernels_pick_before_the_driver(self):
        seen = self.s.tmp / "adj"
        claude = self.s.bin / "claude"
        claude.write_text(textwrap.dedent(f"""\
            #!/bin/sh
            cat /proc/self/oom_score_adj > {seen}
            echo '{{"type": "result", "subtype": "success", "is_error": false, "num_turns": 1, "result": "done"}}'
            """))
        claude.chmod(0o755)
        text, stopped, failed = runner.run_claude(self.p, "worker", "oom", "hi", "rules", self.s.root)
        self.assertEqual((text, stopped, failed), ("done", None, None))
        self.assertEqual(int(seen.read_text()), self.expected())
        self.assertGreater(int(seen.read_text()), self.adj())

    def test_the_drivers_check_and_commands_are_too_and_from_their_first_line(self):
        seen = self.s.tmp / "adj"
        code, why, _ = util.run_limited(f"cat /proc/self/oom_score_adj > {seen}", self.s.root, now() + timedelta(1))
        self.assertEqual((code, why, int(seen.read_text())), (0, "", self.expected()))
        self.p.cfg["check"] = f"sh -c 'cat /proc/self/oom_score_adj' > {seen}"  # a child the check forks at once
        self.assertTrue(jobs.check(self.p, self.s.root)[0])
        self.assertEqual(int(seen.read_text()), self.expected())

    def test_a_sessions_own_commands_keep_the_session_s(self):
        with mock.patch.dict(os.environ, {"WINDVANE_SESSION": "x"}):
            self.assertEqual(util.doomed("make"), "make")
            self.assertEqual(util.doomed(["make"]), ["make"])


class Restarts(Base):
    def supervise(self, *codes, sleep=lambda s: None):
        """`supervise` around a fake driver that ends with each of `codes` in turn (then 0); how often it ran."""
        calls = []

        def start(argv, env):
            calls.append(argv)
            code = codes[len(calls) - 1] if len(calls) <= len(codes) else 0
            return code() if callable(code) else code
        rc = driver.supervise(self.p, ["run", "--for", "2h"], now() + timedelta(hours=2), start=start, sleep=sleep)
        return rc, len(calls)

    def titles(self):
        return [t for t, _ in self.pushes]

    def test_a_driver_killed_by_sigkill_is_restarted_with_one_push(self):
        self.assertEqual(self.supervise(-signal.SIGKILL), (0, 2))
        self.assertEqual(self.titles(), ["windvane restarted"])
        self.assertIn("The driver was killed: SIGKILL. Restart 1 of 3 this hour.", self.pushes[0][1])
        self.assertIn("driver was killed (SIGKILL); restart 1 of 3 this hour",
                      (self.p.state / "autopilot.log").read_text())
        self.assertEqual(util.load_json(self.p.state / supervise.RESTARTED, {})["restarts"], 1)

    def test_an_oom_kill_is_named_as_the_cause(self):
        self.cgroup(0)

        def oom():
            self.cgroup(1)
            return -signal.SIGKILL
        self.assertEqual(self.supervise(oom), (0, 2))
        self.assertIn("The driver was killed: out of memory: the kernel's OOM killer, SIGKILL.", self.pushes[0][1])

    def test_a_clean_end_or_a_stop_is_not_restarted(self):
        for code in (0, driver.SETUP_FAILED, -signal.SIGTERM, -signal.SIGHUP, -signal.SIGINT):
            self.pushes = []
            self.assertEqual(self.supervise(code), (code, 1))
            self.assertEqual(self.pushes, [])

    def test_a_kill_after_windvane_stop_is_not_restarted(self):
        def stopped():
            with contextlib.redirect_stdout(io.StringIO()), mock.patch.object(cli, "running", lambda p: True):
                cli.main(["-C", str(self.s.root), "stop"])
            return -signal.SIGKILL
        self.assertEqual(self.supervise(stopped), (-signal.SIGKILL, 1))
        self.assertEqual(self.titles(), ["windvane stopped"])
        self.assertIn("not restarted: asked to stop", self.pushes[0][1])

    def test_the_fourth_death_in_an_hour_gives_up_and_says_so(self):
        rc, runs = self.supervise(*[-signal.SIGKILL] * 5)
        self.assertEqual((rc, runs), (-signal.SIGKILL, 4))
        self.assertEqual(self.titles(), ["windvane restarted"] * 3 + ["windvane stopped"])
        self.assertIn("Why: the driver was killed (SIGKILL); not restarted: killed 4 times in an hour.",
                      self.pushes[-1][1])

    def test_status_shows_the_restarts(self):
        self.supervise(-signal.SIGKILL, -signal.SIGSEGV)
        with open(self.p.state / "lock", "a") as fh:  # a run holds the lock
            fcntl.flock(fh, fcntl.LOCK_EX)
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                cli.main(["-C", str(self.s.root), "status"])
        self.assertIn("restarted: 2 times; the last at ", out.getvalue())
        self.assertIn(": the driver was killed (SIGSEGV)", out.getvalue())

    def test_a_new_run_starts_with_no_restarts(self):
        self.supervise(-signal.SIGKILL)
        with contextlib.redirect_stdout(io.StringIO()):
            cli.main(["-C", str(self.s.root), "run", "--for", "1h", "--no-preflight"])
        self.assertFalse((self.p.state / supervise.RESTARTED).exists())

    @unittest.skipUnless(LINUX, "it reads /proc")
    def test_a_killed_drivers_setup_is_swept_by_the_restart(self):
        """A setup (`run_limited`) runs in a group of its own: recorded while it runs, swept once its driver is gone."""
        groups, seen = self.p.state / "nested" / "driver-1", self.s.tmp / "marks"
        code, _, _ = util.run_limited(f"ls {groups} > {seen}", self.s.root, now() + timedelta(1), groups=groups)
        self.assertEqual(code, 0)
        self.assertEqual(len(seen.read_text().split()), 1)  # its own mark, while it ran
        self.assertEqual(list(groups.iterdir()), [])  # gone once it ended
        left = subprocess.Popen(["sleep", "60"], start_new_session=True)  # a setup its killed driver left running
        self.addCleanup(left.kill)
        util.record_group(groups, left.pid)
        self.assertEqual(runner.sweep_nested(self.p), 1)
        self.assertEqual(left.wait(10), -signal.SIGTERM)
        self.assertFalse(groups.exists())

    def test_where_a_group_is_recorded(self):
        self.assertIsNone(util.groups_dir(self.p))  # a desk command's groups are its own
        with mock.patch.dict(os.environ, {"WINDVANE_SESSION": "w1"}):
            self.assertEqual(util.groups_dir(self.p), self.p.state / "nested" / "w1")  # ended with its session

    @unittest.skipUnless(LINUX, "it reads /proc")
    def test_what_a_killed_driver_left_in_the_group_is_ended(self):
        """A supervisor leading its group (as `exec` in tmux makes it) ends the dead driver's leftovers there."""
        code = textwrap.dedent("""\
            import subprocess, sys
            sys.path.insert(0, sys.argv[1])
            from windvane import supervise
            left = subprocess.Popen(["sleep", "60"])
            print(left.pid, supervise.end_orphans(grace=1), flush=True)
            left.wait()
            """)
        r = subprocess.run([sys.executable, "-c", code, str(Path(__file__).resolve().parents[1])], capture_output=True,
                           text=True, timeout=30, start_new_session=True)
        pid, ended = map(int, r.stdout.split())
        self.assertEqual(ended, 1)
        self.assertFalse(supervise.lives(pid))
        self.assertEqual(supervise.end_orphans(), 0)  # not a group's leader here: the group is someone else's


if __name__ == "__main__":
    unittest.main()
