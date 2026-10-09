"""`windvane bg`/`await` keep each ticket's jobs its own: tickets side by side that pick the same name both start,
and each await returns its own job, never another ticket's."""
import io
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from windvane import jobs, scratch  # noqa: E402
from windvane.runner import NEST, TICKET  # noqa: E402


class BgScope(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp(prefix=scratch.PREFIX))
        self.addCleanup(shutil.rmtree, self.d, ignore_errors=True)
        self.p = SimpleNamespace(state=self.d / "state")
        env = {k: v for k, v in os.environ.items() if k not in (TICKET, NEST)}
        patch = mock.patch.dict(os.environ, env, clear=True)
        patch.start()
        self.addCleanup(patch.stop)

    def as_ticket(self, ticket):
        os.environ.pop(TICKET, None)
        if ticket:
            os.environ[TICKET] = ticket

    def run_cli(self, fn, *args):
        out = io.StringIO()
        with redirect_stdout(out):
            fn(self.p, *args)
        return out.getvalue()

    def bg(self, ticket, name, script):
        self.as_ticket(ticket)
        return self.run_cli(jobs.bg, name, ["bash", "-c", script], str(self.d))

    def await_(self, ticket, name):
        self.as_ticket(ticket)
        return self.run_cli(jobs.await_, name, 40, 30)

    def test_two_tickets_each_start_and_await_their_own_check(self):
        first = self.bg("T-1", "check", "sleep 1; echo first ticket; exit 3")
        second = self.bg("T-2", "check", "echo second ticket")
        self.assertIn("started 'check'", first)
        self.assertIn("started 'check'", second)
        self.assertIn(str(self.p.state / "bg" / "T-2" / "check.log"), second)
        two = self.await_("T-2", "check")
        one = self.await_("T-1", "check")
        self.assertIn("DONE check exit 0", two)
        self.assertIn("second ticket", two)
        self.assertNotIn("first ticket", two)
        self.assertIn("DONE check exit 3", one)
        self.assertIn("first ticket", one)
        self.assertNotIn("second ticket", one)

    def test_await_never_attaches_to_another_tickets_job(self):
        self.bg("T-1", "check", "echo other")
        self.await_("T-1", "check")
        self.as_ticket("T-2")
        with self.assertRaises(SystemExit) as e:
            self.run_cli(jobs.await_, "check", 40, 30)
        self.assertEqual(str(e.exception), "await: nothing started as 'check' for T-2")

    def test_the_desk_keeps_the_unscoped_path(self):
        out = self.bg(None, "build", "echo desk")
        self.assertIn(str(self.p.state / "bg" / "build.log"), out)
        self.assertIn("DONE build exit 0", self.await_(None, "build"))
        self.assertTrue((self.p.state / "bg" / "build.exit").exists())

    def test_a_tickets_jobs_go_with_its_other_leftovers(self):
        self.bg("T-1", "check", "true")
        self.await_("T-1", "check")
        self.bg(None, "check", "true")
        self.await_(None, "check")
        scratch.session_dir(self.p, "T-1")
        scratch.drop(self.p, "T-1")
        self.assertFalse((self.p.state / "bg" / "T-1").exists())
        self.assertFalse((self.p.state / "tmp" / "T-1").exists())
        self.assertTrue((self.p.state / "bg" / "check.exit").exists())  # the desk's job isn't the ticket's


if __name__ == "__main__":
    unittest.main()
