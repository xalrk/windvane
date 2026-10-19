"""Two `windvane bg` calls for one name at once (a session's parallel tool calls) start one job: `bg` holds a lock
beside the job's files from its "still running" check through writing the pid, so the second call waits for the
first and then refuses, instead of starting a second job whose pid hides the first's."""
import io
import os
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
from windvane import jobs  # noqa: E402
from windvane.config import Project  # noqa: E402
from windvane.runner import IN_SANDBOX, NEST, TICKET  # noqa: E402
from helpers import Sandbox  # noqa: E402


class BgSameName(unittest.TestCase):
    def setUp(self):
        self.s = Sandbox()
        self.addCleanup(self.s.close)
        self.p = SimpleNamespace(state=self.s.tmp / "state")
        for k in (NEST, IN_SANDBOX, TICKET):
            os.environ.pop(k, None)
        self.addCleanup(self.end_all)

    def end_all(self):
        for start in (self.p.state / "bg").glob("*.start"):
            if jobs.alive(start.with_suffix("")):
                jobs.end_job(start.with_suffix(""), "the test's end")

    def call(self, errors):
        try:
            jobs.bg(self.p, "j", ["sleep", "30"], str(self.s.root))
        except SystemExit as e:
            errors.append(str(e.code))

    def test_the_second_of_two_calls_together_refuses(self):
        """The first call's check is held open until the second has had time to make its own: without the lock the
        second's check sees nothing yet and starts a job too."""
        real, second, errors = jobs.alive, [], []
        out = io.StringIO()

        def alive(base):
            seen = real(base)
            if not second:  # the first call has looked: the second call starts before it acts on what it saw
                second.append(threading.Thread(target=self.call, args=(errors,)))
                second[0].start()
                time.sleep(0.5)
            return seen
        with mock.patch.object(jobs, "alive", alive), redirect_stdout(out):
            self.call(errors)
            second[0].join(30)
        self.assertEqual(out.getvalue().count("started 'j'"), 1, out.getvalue())
        self.assertEqual(errors, ["bg: 'j' is still running; wait with: windvane await j"])
        self.assertTrue(jobs.alive(jobs.job_base(self.p, "j")))

    def test_the_lock_held_keeps_a_call_waiting_until_it_frees(self):
        base = jobs.job_base(self.p, "j")
        base.parent.mkdir(parents=True)
        out, errors = io.StringIO(), []
        with redirect_stdout(out):
            with open(f"{base}.lock", "a") as lock:
                jobs.fcntl.flock(lock, jobs.fcntl.LOCK_EX)
                t = threading.Thread(target=self.call, args=(errors,))
                t.start()
                time.sleep(0.3)
                self.assertTrue(t.is_alive(), "it started while another call held the name")
                self.assertFalse(Path(f"{base}.pid").exists())
            t.join(30)
        self.assertEqual((out.getvalue().count("started 'j'"), errors), (1, []))


class TwoCommands(unittest.TestCase):
    """The same through the `windvane bg` command, as a session's two parallel tool calls run it."""

    def setUp(self):
        self.s = Sandbox()
        self.addCleanup(self.s.close)
        for k in (NEST, IN_SANDBOX, TICKET):
            os.environ.pop(k, None)
        self.p = Project(self.s.root)
        self.addCleanup(BgSameName.end_all, self)

    def test_two_commands_started_together_start_one_job(self):
        for name in ("a", "b", "c", "d", "e"):
            calls = [subprocess.Popen(["windvane", "bg", name, "sleep", "20"], cwd=self.s.root, text=True,
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE) for _ in range(2)]
            outs = [c.communicate(timeout=60) for c in calls]
            started = [o for o, _ in outs if f"started '{name}'" in o]
            refused = [e.strip() for _, e in outs if e.strip()]
            self.assertEqual((len(started), refused),
                             (1, [f"bg: '{name}' is still running; wait with: windvane await {name}"]), outs)
            self.assertTrue(jobs.alive(self.p.state / "bg" / name))


if __name__ == "__main__":
    unittest.main()
