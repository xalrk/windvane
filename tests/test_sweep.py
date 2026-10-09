"""The start's sweep of what a dead driver's sessions left running: on a system without /proc (macOS) through `ps`,
and a worker that outlived its driver, told apart from a desk session by the owner its `session` file records."""
import os
import re
import sys
import time
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from windvane import runner, util  # noqa: E402
from windvane.config import Project  # noqa: E402
from windvane.util import read_jsonl  # noqa: E402
import test_hangs  # noqa: E402
from test_hangs import alive  # noqa: E402

FAKE_PS = """#!/bin/sh
# `ps -o lstart= -p <pid>`: the start written for that pid, in ps's C-locale format, in UTC
[ "$LC_ALL" = C ] && [ "$TZ" = UTC ] || exit 1
for a; do pid=$a; done
cat "$FAKE_PS/$pid" 2>/dev/null
"""


def lstart(t):
    return time.strftime("%a %b %e %H:%M:%S %Y", time.gmtime(t))  # "Sun Oct  4 15:03:37 2026", as ps prints it


class Sweep(test_hangs.Hangs):
    """Hangs' sandbox and process helpers; its own tests run in test_hangs only."""

    def setUp(self):
        super().setUp()
        self.p = Project(self.s.root)

    def swept_rows(self):
        return [r for r in read_jsonl(self.p.state / "ledger.jsonl") if r.get("event") == "swept"]

    def session(self, name, pid, owner):
        f = self.mark(self.p, name, pid, "session")
        f.write_text(f"{pid} {owner}")
        return f


for name in [n for n in dir(test_hangs.Hangs) if n.startswith("test_")]:
    setattr(Sweep, name, None)


class NoProc(Sweep):
    def setUp(self):
        super().setUp()
        self.ps = self.s.tmp / "ps-starts"
        self.ps.mkdir()
        (self.s.bin / "ps").write_text(FAKE_PS)
        (self.s.bin / "ps").chmod(0o755)
        os.environ["FAKE_PS"] = str(self.ps)
        patch = unittest.mock.patch.object(util, "PROC", self.s.tmp / "no-proc")
        patch.start()
        self.addCleanup(patch.stop)

    def started(self, pid, t):
        (self.ps / str(pid)).write_text(f"{lstart(t)}\n")

    def test_ps_tells_a_start_time_without_proc(self):
        t = int(time.time()) - 90
        self.started(4242, t)
        self.assertEqual(runner.started_at(4242), t)
        self.assertIsNone(runner.started_at(4243))  # gone: ps prints nothing

    def test_an_orphaned_group_is_killed_and_a_reused_number_spared(self):
        orphan, stranger = self.group(), self.group()
        marked = time.time() - 30
        for g in (orphan, stranger):
            os.utime(self.mark(self.p, "T-007-worker", g.pid), (marked, marked))
        self.started(orphan.pid, marked - 5)  # started before its mark: the session's own
        self.started(stranger.pid, marked + 60)  # after it: someone else has the number now
        self.assertEqual(runner.sweep_nested(self.p), 1)
        self.assertTrue(self.gone(orphan))
        self.assertTrue(alive(stranger.pid))
        self.assertFalse((self.p.state / "nested" / "T-007-worker").exists())


class Owners(Sweep):
    def test_a_worker_that_outlived_its_driver_is_killed_with_its_own(self):
        worker, critic, driver = self.group(), self.group(), self.group("true")
        driver.wait()
        self.session("T-007-worker", worker.pid, f"{driver.pid} {time.time() - 60:.2f}")
        self.mark(self.p, "T-007-worker", critic.pid)
        self.assertEqual(runner.sweep_nested(self.p), 2)
        self.assertTrue(self.gone(worker) and self.gone(critic))
        self.assertFalse((self.p.state / "nested" / "T-007-worker").exists())
        [row] = self.swept_rows()
        self.assertEqual({k: row[k] for k in ("role", "groups")}, {"role": "driver", "groups": 2})
        self.assertRegex(row["at"], r"^\d{4}-\d\d-\d\d \d\d:\d\d$")

    def test_a_session_whose_owner_lives_keeps_its_own(self):
        plan, job = self.group(), self.group()
        f = self.mark(self.p, "plan-desk", plan.pid, "session")
        f.write_text(runner.owner_line(plan.pid))  # this test process stands in for the desk `windvane plan`
        self.mark(self.p, "plan-desk", job.pid)
        self.assertEqual(runner.sweep_nested(self.p), 0)
        self.assertTrue(alive(plan.pid) and alive(job.pid))
        self.assertTrue((self.p.state / "nested" / "plan-desk" / str(job.pid)).exists())
        self.assertEqual(self.swept_rows(), [])

    def test_an_owner_number_now_someone_elses_counts_as_gone(self):
        worker = self.group()
        self.session("T-007-worker", worker.pid, f"{os.getpid()} {time.time() - 86400:.2f}")  # started a day apart
        self.assertEqual(runner.sweep_nested(self.p), 1)
        self.assertTrue(self.gone(worker))

    def test_a_session_from_before_owners_were_recorded_is_spared(self):
        worker = self.group()
        self.mark(self.p, "T-007-worker", worker.pid, "session")  # the pid alone
        self.assertEqual(runner.sweep_nested(self.p), 0)
        self.assertTrue(alive(worker.pid))

    def test_a_run_records_its_owner(self):
        line = runner.owner_line(123).split()
        self.assertEqual(line[:2], ["123", str(os.getpid())])
        self.assertTrue(runner.owner_alive(line[1:]))
        self.assertTrue(re.fullmatch(r"\d+(\.\d+)?", line[2]))


if __name__ == "__main__":
    unittest.main()
