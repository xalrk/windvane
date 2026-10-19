"""R-043: in a session, a check that isn't heavy can still outlast the Bash tool's 10 minutes; run in the call's
foreground it was killed at that limit and its pass never recorded. Every session check runs as the session's bg
job `check` (one, however often it's asked for) and is awaited within the limit; `await check` gets its result,
recorded for the tree. At the desk it runs in the foreground as before."""
import io
import os
import sys
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from windvane import cli, heavy, jobs  # noqa: E402
from windvane.config import Project  # noqa: E402
from windvane.heavy import STOP_AT  # noqa: E402
from windvane.runner import IN_JOB, IN_SANDBOX, TICKET  # noqa: E402
from windvane.util import read_jsonl  # noqa: E402
from helpers import Sandbox  # noqa: E402

LIMIT = 3  # seconds `await` blocks here, for its 570


class SessionCheck(unittest.TestCase):
    def setUp(self):
        self.s = Sandbox()
        self.addCleanup(self.s.close)
        self.addCleanup(os.chdir, os.getcwd())
        self.release = self.s.tmp / "release"  # outside the tree: the check runs until it exists
        env = {TICKET: "T-001", STOP_AT: str(time.time() + 600)}
        patch = mock.patch.dict(os.environ, env)
        patch.start()
        self.addCleanup(patch.stop)
        for k in (IN_SANDBOX, IN_JOB, "WINDVANE_HEAVY_HELD"):  # a suite run as a background job is in one
            os.environ.pop(k, None)
        conf = self.s.root / ".windvane/config.toml"
        self.s.write(".windvane/config.toml", conf.read_text().replace(
            'check = "test -f README"', f'check = "while [ ! -e {self.release} ]; do sleep 0.1; done; test -f README"'))
        self.s.publish("a slow check")
        self.p = Project(self.s.root)
        self.addCleanup(self.end_job)
        patch = mock.patch.object(jobs, "AWAIT_S", LIMIT)
        patch.start()
        self.addCleanup(patch.stop)

    def end_job(self):
        self.release.touch()
        base = jobs.job_base(self.p, "check")
        if jobs.alive(base):
            jobs.end_job(base, "test over")

    def windvane(self, *args):
        """`windvane <args>` in the project, in this process (so the patched limit holds): (exit code, output)."""
        out = io.StringIO()
        with redirect_stdout(out), self.assertRaises(SystemExit) as e:
            cli.main(["-C", str(self.s.root), *args])
            raise SystemExit(0)
        code = e.exception.code
        return (code if isinstance(code, int) else 0 if code is None else 1), out.getvalue()

    def starts(self):
        return [r for r in read_jsonl(self.p.state / "ledger.jsonl") if r["event"] == "check-start"] \
            if (self.p.state / "ledger.jsonl").exists() else []

    def test_a_check_past_the_call_goes_on_as_the_bg_job_and_await_gets_its_pass(self):
        t = time.time()
        code, out = self.windvane("check")
        self.assertLess(time.time() - t, LIMIT + 5)
        self.assertEqual(code, heavy.TIMED_OUT, out)
        self.assertRegex(out, r"RUNNING check \(0 min so far\); call windvane await check again\n")
        self.assertTrue(jobs.alive(jobs.job_base(self.p, "check")))
        code, out = self.windvane("check")  # asked again meanwhile: it joins the running check
        self.assertEqual(code, heavy.TIMED_OUT, out)
        self.assertIn("RUNNING check", out)
        self.assertEqual(len(self.starts()), 1, "a second check started")
        self.release.touch()
        for _ in range(10):
            code, out = self.windvane("await", "check")
            if "DONE" in out:
                break
        self.assertIn("DONE check exit 0", out)
        self.assertRegex(out.splitlines()[-1], r"^CHECK PASSED in ")
        self.assertEqual([r["passed"] for r in read_jsonl(self.p.state / "ledger.jsonl") if r["event"] == "check"],
                         [True])
        code, out = self.windvane("check")  # its pass is recorded for the tree: reused at once, no job
        self.assertEqual(code, 0, out)
        self.assertIn("CHECK PASSED (reused the pass", out)
        self.assertEqual(len(self.starts()), 1)

    def test_with_no_check_configured_it_answers_at_once(self):
        conf = self.s.root / ".windvane/config.toml"
        self.s.write(".windvane/config.toml", "\n".join(ln for ln in conf.read_text().splitlines()
                                                        if not ln.startswith("check =")) + "\n")
        self.s.publish("no check")
        code, out = self.windvane("check")
        self.assertEqual(code, 0, out)
        self.assertNotIn("RUNNING", out)
        self.assertFalse(Path(f"{jobs.job_base(self.p, 'check')}.start").exists())

    def test_at_the_desk_it_runs_in_the_foreground_as_before(self):
        del os.environ[STOP_AT]
        self.release.touch()
        code, out = self.windvane("check")
        self.assertEqual(code, 0, out)
        self.assertRegex(out.splitlines()[-1], r"^CHECK PASSED in ")
        self.assertFalse(Path(f"{jobs.job_base(self.p, 'check')}.start").exists())


if __name__ == "__main__":
    unittest.main()
