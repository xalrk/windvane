"""R-030: in a session, a heavy check's wait for the machine lasts until the session's stop, hours past the Bash
tool's 10 minutes, so a foreground `windvane check` was killed and lost its place in line. There it runs as the
session's bg job `check` (one, however often it's asked for) and is awaited within the limit; `await check` gets
its result, recorded for the tree. `check --mem` sets this check's memory need."""
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
from windvane import cli, heavy, jobs, runner  # noqa: E402
from windvane.config import Project  # noqa: E402
from windvane.heavy import STOP_AT  # noqa: E402
from windvane.runner import IN_JOB, IN_SANDBOX, TICKET  # noqa: E402
from windvane.util import read_jsonl  # noqa: E402
from helpers import Sandbox  # noqa: E402
from test_heavy_check_wait import hold, machine  # noqa: E402

LIMIT = 3  # seconds `await` blocks here, for its 570


class SessionHeavyCheck(unittest.TestCase):
    def setUp(self):
        self.s = Sandbox()
        self.addCleanup(self.s.close)
        self.addCleanup(os.chdir, os.getcwd())
        self.run_dir = self.s.tmp / "run"
        self.run_dir.mkdir()
        os.environ.update({**machine(self.run_dir, 60), TICKET: "T-001", STOP_AT: str(time.time() + 600)})
        for k in (IN_SANDBOX, IN_JOB, "WINDVANE_HEAVY_HELD"):  # a suite run as a background job is in one
            os.environ.pop(k, None)
        self.plain = (self.s.root / ".windvane/config.toml").read_text()
        self.config("heavy_check = true")
        patch = mock.patch.object(jobs, "AWAIT_S", LIMIT)
        patch.start()
        self.addCleanup(patch.stop)

    def config(self, extra):
        """The project's config with `extra` after its check, committed: a pass is recorded only for a clean tree."""
        self.s.write(".windvane/config.toml", self.plain.replace('check = "test -f README"\n',
                                                                 f'check = "test -f README"\n{extra}\n'))
        self.s.publish("config")
        self.p = Project(self.s.root)

    def hold(self):
        """Another job holds the machine lock until `release`; the session's check job is ended before it frees."""
        release = self.run_dir / "release"
        hold(self, self.run_dir)
        self.addCleanup(lambda: jobs.end_job(jobs.job_base(self.p, "check"), "test over")
                        if Path(f"{jobs.job_base(self.p, 'check')}.start").exists() else None)
        return release

    def windvane(self, *args):
        """`windvane <args>` in the project, in this process (so the patched limit holds): (exit code, output)."""
        out = io.StringIO()
        with redirect_stdout(out), self.assertRaises(SystemExit) as e:
            cli.main(["-C", str(self.s.root), *args])
            raise SystemExit(0)
        code = e.exception.code
        return (code if isinstance(code, int) else 0 if code is None else 1), out.getvalue()

    def queue(self):
        q = self.run_dir / "windvane-heavy.queue"
        return [f for f in q.iterdir() if not f.name.startswith(".")] if q.is_dir() else []

    def queued(self, n=1):
        end = time.time() + 20
        while len(self.queue()) < n:
            self.assertLess(time.time(), end, "the check never joined the heavy queue")
            time.sleep(0.1)
        return self.queue()

    def checks(self):
        return [r for r in read_jsonl(self.p.state / "ledger.jsonl") if r["event"] == "check"]

    def test_it_waits_in_line_past_the_call_and_await_gets_the_pass(self):
        release = self.hold()
        t = time.time()
        code, out = self.windvane("check")
        self.assertLess(time.time() - t, LIMIT + 5)
        self.assertEqual(code, heavy.TIMED_OUT, out)
        self.assertEqual(len(self.queued()), 1)
        code, out = self.windvane("check", "--mem", "2")  # asked again meanwhile: the same job, still one in line
        self.assertEqual(code, heavy.TIMED_OUT, out)
        self.assertEqual(out.splitlines()[0], "check: the running check keeps its own need; --mem 2 goes to the next one")
        self.assertRegex(out, r"WAITING check \(in the heavy queue, 0 min so far\); call windvane await check again\n")
        self.assertIn("heavy: waiting for the machine lock: held by pid", out)
        self.assertEqual(len(self.queue()), 1)
        self.assertEqual(self.checks(), [])
        old = self.s.git("rev-parse", "--short", "HEAD")
        self.s.write("NOTES", "more\n")
        self.s.publish("a new head meanwhile")
        code, out = self.windvane("check")
        self.assertIn(f"check: the running check started on {old}, not {self.s.git('rev-parse', '--short', 'HEAD')}: "
                      "once it ends, run `windvane check` again\n", out)
        self.assertEqual(len(self.queue()), 1)  # nothing ended, so no pass and no failure
        release.touch()
        for _ in range(10):
            code, out = self.windvane("await", "check")
            if "DONE" in out:
                break
        self.assertIn("DONE check exit 0", out)
        r = subprocess.run(["windvane", "await", "check"], cwd=self.s.root, capture_output=True, text=True,
                           timeout=60)  # the real entry point: the code it prints isn't taken for an error message
        self.assertEqual((r.returncode, r.stderr), (0, ""))
        self.assertRegex(r.stdout.splitlines()[-1], r"^CHECK PASSED in ")
        self.assertRegex(out.splitlines()[-1], r"^CHECK PASSED in ")
        self.assertEqual([r["passed"] for r in self.checks()], [True])
        self.assertEqual(self.queue(), [])
        self.assertFalse((self.p.state / "passes").exists())  # the tree changed under it: not recorded
        code, out = self.session_check()  # that pass was for the old head: this one is checked on its own
        self.assertRegex(out.splitlines()[-1], r"^CHECK PASSED in ")
        code, out = self.windvane("check")  # and its pass is recorded for the tree: reused at once
        self.assertEqual(code, 0)
        self.assertIn("CHECK PASSED (reused the pass", out)

    def session_check(self):
        """`windvane check` once the machine is free: its own result, from `await` on a machine too slow for it."""
        code, out = self.windvane("check")
        if code == heavy.TIMED_OUT:
            code, out = self.windvane("await", "check")
        return code, out

    def test_a_check_that_gets_the_machine_in_time_ends_with_its_own_result(self):
        code, out = self.session_check()
        self.assertEqual(code, 0, out)
        self.assertRegex(out.splitlines()[-1], r"^CHECK PASSED in ")

    def test_a_failing_check_exits_1_with_its_log_tail(self):
        self.s.write(".windvane/config.toml", self.plain.replace('check = "test -f README"',
                                                                 'check = "echo boom; exit 3"\nheavy_check = true'))
        self.s.publish("failing")
        code, out = self.session_check()
        self.assertEqual(code, 1, out)
        self.assertIn("boom", out)
        self.assertEqual(out.count("log "), 1, out)  # the summary names the log; the job's own log line isn't shown
        self.assertRegex(out.splitlines()[-1], r"^CHECK FAILED \(exit 3\) in [\d.]+ min; log ")
        self.assertEqual([r["passed"] for r in self.checks()], [False])

    def test_a_workers_own_check_job_runs_the_check_in_it(self):
        """The old recipe, `bg check windvane check` (or any name): the check runs in that job, which mustn't
        wait on itself as the session's `check` job, and its wait shows in `await` as a check job's does."""
        release = self.hold()
        out = io.StringIO()
        with redirect_stdout(out):
            jobs.bg(self.p, "check", ["windvane", "check"], str(self.s.root))
        self.queued()
        code, out = self.windvane("await", "check")
        self.assertIn("WAITING check (in the heavy queue", out)
        release.touch()
        for _ in range(10):
            code, out = self.windvane("await", "check")
            if "DONE" in out:
                break
        self.assertRegex(out.splitlines()[-1], r"^CHECK PASSED in ")

    def test_mem_sets_the_need_over_heavy_mem(self):
        self.config("heavy_check = true\nheavy_mem = 5")
        self.hold()
        self.assertEqual(self.windvane("check", "--mem", "3")[0], heavy.TIMED_OUT)
        self.assertEqual([heavy.need_of(f) for f in self.queued()], [3])

    def test_heavy_mem_stands_without_mem(self):
        self.config("heavy_check = true\nheavy_mem = 5")
        self.hold()
        self.windvane("check")
        self.assertEqual([heavy.need_of(f) for f in self.queued()], [5])

    def test_mem_without_heavy_check_is_an_error_naming_it(self):
        self.config("")
        r = subprocess.run(["windvane", "check", "--mem", "3"], cwd=self.s.root, capture_output=True, text=True,
                           timeout=60)
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(r.stderr, "check --mem: only a heavy check waits for free memory; this project has "
                                   "heavy_check off (.windvane/config.toml)\n")
        self.assertNotIn("CHECK", r.stdout)

    def test_it_refuses_in_the_sandbox_touching_nothing(self):
        r = subprocess.run(["windvane", "check"], cwd=self.s.root, capture_output=True, text=True, timeout=60,
                           env={**os.environ, IN_SANDBOX: "1"})
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("windvane: a heavy check can't run inside the Bash sandbox", r.stderr)
        self.assertFalse((self.run_dir / "windvane-heavy.lock").exists())
        self.assertEqual(self.queue(), [])
        self.assertFalse((self.p.state / "ledger.jsonl").exists())
        self.assertFalse(jobs.job_base(self.p, "check").with_suffix(".start").exists())

    def test_at_the_desk_it_runs_in_the_foreground_as_before(self):
        del os.environ[STOP_AT]
        code, out = self.windvane("check")
        self.assertEqual(code, 0, out)
        self.assertRegex(out.splitlines()[-1], r"^CHECK PASSED in ")
        self.assertFalse(jobs.job_base(self.p, "check").with_suffix(".start").exists())

    def test_a_session_started_from_a_job_isnt_in_it(self):
        with mock.patch.dict(os.environ, {IN_JOB: "check"}):
            self.assertNotIn(IN_JOB, runner.env())


if __name__ == "__main__":
    unittest.main()
