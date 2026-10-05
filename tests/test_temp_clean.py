"""Temp files stay out of /tmp's way: the tests remove what they make, sessions and checks get a TMPDIR of their own,
and the cleaner removes what leaked anyway (a full tmpfs once stopped every shell command on the machine)."""
import contextlib
import io
import json
import os
import subprocess
import sys
import textwrap
import time
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock

from helpers import Sandbox, tmpdir
from test_install import REPO
from test_pipeline import args

sys.path.insert(0, str(REPO))
from windvane import doctor, driver, jobs, scratch  # noqa: E402
from windvane.config import Project  # noqa: E402
from windvane.util import now  # noqa: E402

HOUR_AGO = time.time() - 2 * scratch.AGE

FAILING = '''\
import unittest
from helpers import Sandbox, tmpdir


class Fails(unittest.TestCase):
    def setUp(self):
        self.s = Sandbox()
        raise RuntimeError("a setUp that fails after its Sandbox: tearDown never runs")

    def test_never(self):
        pass


class HalfMade(Sandbox):
    def setup(self, config_extra):
        super().setup(config_extra)
        raise RuntimeError("a Sandbox that fails in the making")


class FailsToo(unittest.TestCase):
    def test_a_sandbox_that_fails_half_made(self):
        HalfMade()

    def test_fails_with_a_temp_dir(self):
        (tmpdir(self) / "f").write_text("x")
        self.fail("on purpose")
'''


def age(path, when=HOUR_AGO):
    """Set the mtime of everything under `path` (and itself) to `when`."""
    for d, dirs, files in os.walk(path, topdown=False):
        for name in dirs + files:
            os.utime(os.path.join(d, name), (when, when), follow_symlinks=False)
    os.utime(path, (when, when))


class SuiteLeavesNothing(unittest.TestCase):
    def unittest_in(self, tmp, *modules, cwd=REPO / "tests"):
        env = {k: v for k, v in os.environ.items() if not k.startswith("WINDVANE_")}
        return subprocess.run([sys.executable, "-m", "unittest", "-q", *modules], cwd=cwd, capture_output=True,
                              text=True, env={**env, "TMPDIR": str(tmp), "PYTHONPATH": str(REPO)}, timeout=600)

    def test_install_doctor_and_sandbox_tests_leave_their_temp_dir_empty(self):
        tmp = tmpdir(self)
        r = self.unittest_in(tmp, "test_install", "test_doctor", "test_first_run")
        self.assertEqual(r.returncode, 0, r.stderr[-2000:])
        self.assertEqual(sorted(p.name for p in tmp.iterdir()), [])

    def test_failing_tests_leave_nothing_either(self):
        tmp = tmpdir(self)
        (tmp / "tests").mkdir()
        (tmp / "tests/test_fails.py").write_text(FAILING)
        (tmp / "tests/helpers.py").symlink_to(REPO / "tests/helpers.py")
        (tmp / "run").mkdir()
        r = self.unittest_in(tmp / "run", "test_fails", cwd=tmp / "tests")
        self.assertIn("FAILED (failures=1, errors=2)", r.stderr)
        self.assertEqual(sorted(p.name for p in (tmp / "run").iterdir()), [])


class Cleaner(unittest.TestCase):
    def setUp(self):
        self.s = Sandbox()
        self.addCleanup(self.s.close)
        self.tmp = Path(os.environ["TMPDIR"])  # the Sandbox's: the cleaner reads TMPDIR fresh
        self.p = Project(self.s.root)

    def leftover(self, name, old=True, files=3):
        d = self.tmp / name if not name.startswith("T-") and name != "plan" else self.p.state / "tmp" / name
        (d / "sub").mkdir(parents=True)
        for i in range(files):
            (d / "sub" / f"f{i}").write_text("x" * 10)
        if old:
            age(d)
        return d

    def test_it_removes_old_windvane_dirs_and_session_dirs_of_tickets_not_running(self):
        gone = [self.leftover("windvane-venv-a", files=5), self.leftover("windvane-doctor-b"),
                self.leftover("T-001"), self.leftover("plan")]
        kept = [self.leftover("windvane-test-new", old=False),  # under an hour old: a check may be writing it
                self.leftover("tmpabc123"), self.leftover("pnq-x"), self.leftover("t066"),  # not windvane's
                self.leftover("T-002")]  # its ticket is running
        age(kept[0])
        os.utime(kept[0] / "sub" / "f2")  # one file deep inside still being written
        left = scratch.leftovers(self.p, {"T-002"})
        self.assertEqual(sorted(d.name for d, _, _ in left), sorted(d.name for d in gone))
        self.assertEqual(left[0][:2], (gone[0], 7))  # the biggest first
        dirs, n = scratch.clean(self.p, {"T-002"})
        self.assertEqual((dirs, n), (4, 7 + 5 + 5 + 5))
        self.assertEqual([d for d in gone if d.exists()], [])
        self.assertEqual([d for d in kept if not d.exists()], [])

    def test_a_dir_a_process_works_in_is_kept_however_old(self):
        cwd, env = self.leftover("windvane-check-cwd"), self.leftover("windvane-check-env")
        (env / "tmp3.11").mkdir()
        age(env)
        with subprocess.Popen(["sleep", "30"], cwd=cwd) as a, \
                subprocess.Popen(["sleep", "30"], env={**os.environ, "TMPDIR": str(env / "tmp3.11")}) as b:
            try:
                time.sleep(0.2)
                self.assertEqual(scratch.clean(), (0, 0))
            finally:
                a.kill(), b.kill()
        self.assertEqual(scratch.clean()[0], 2)

    def test_it_never_follows_a_link_out(self):
        target = self.leftover("precious")
        (self.tmp / "windvane-link").symlink_to(target)
        d = self.leftover("windvane-holds-a-link")
        (d / "out").symlink_to(target)
        age(d)
        self.assertEqual(scratch.clean()[0], 1)
        self.assertTrue((target / "sub/f0").exists())
        self.assertTrue((self.tmp / "windvane-link").is_symlink())


class Doctor(unittest.TestCase):
    def setUp(self):
        self.s = Sandbox()
        self.addCleanup(self.s.close)
        self.p = Project(self.s.root)
        self.tmp = Path(os.environ["TMPDIR"])

    def doctor(self, *a, fullness=(10, 10)):
        with mock.patch.object(scratch, "fullness", return_value=fullness), \
                mock.patch.object(doctor, "checks", side_effect=lambda p: [("ok", "x", "")] + doctor.tmp_warnings(p)), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            doctor.main(self.p, *a)
        return out.getvalue()

    def test_it_warns_of_a_nearly_full_temp_dir_and_names_the_biggest_leftovers(self):
        self.assertNotIn("WARNING", self.doctor())
        self.assertRegex(self.doctor(fullness=(93.4, 20)), rf"WARNING +the temp dir {self.tmp} is 93% full of inodes "
                                                           r"and 20% of space.*-> remove what fills it")
        for name, n in (("windvane-venv-x", 40), ("windvane-test-y", 0), ("windvane-inst-z", 9)):
            d = self.tmp / name
            d.mkdir()
            for i in range(n):
                (d / str(i)).touch()
            age(d)
        (self.p.state / "tmp/T-004").mkdir(parents=True)
        age(self.p.state / "tmp/T-004")
        (self.p.state / "state.json").write_text(json.dumps({"running": {"T-004": 0}}))  # still running: kept
        out = self.doctor()
        self.assertIn("3 leftover windvane temp dirs, 52 files (biggest: windvane-venv-x (41 files), "
                      "windvane-inst-z (10 files), windvane-test-y (1 file))  -> windvane doctor --clean", out)
        out = self.doctor(True)
        self.assertIn(f"removed 3 leftover windvane temp dirs (52 files) from {self.tmp} and {self.p.state / 'tmp'}",
                      out)
        self.assertNotIn("leftover windvane temp dirs,", out)
        d = self.tmp / "windvane-one"
        d.mkdir()
        age(d)
        self.assertIn("1 leftover windvane temp dir, 1 file (biggest: windvane-one (1 file))", self.doctor())
        self.assertTrue((self.p.state / "tmp/T-004").exists())


class Sessions(unittest.TestCase):
    def setUp(self):
        self.s = Sandbox()
        self.addCleanup(self.s.close)
        self.p = Project(self.s.root)

    def test_sessions_work_in_their_tickets_temp_dir_and_it_goes_when_the_ticket_merges(self):
        self.s.ticket("T-001")
        self.s.publish()
        pilot = driver.Pilot(self.p, args())
        pilot.run(now() + timedelta(hours=1))
        self.assertEqual(pilot.merged, ["T-001"])
        calls = [json.loads(line) for line in (self.s.tmp / "calls.jsonl").read_text().splitlines()]
        dirs = {c["tmpdir"] for c in calls if c["system"].startswith(("# windvane worker", "# windvane review"))}
        self.assertEqual(dirs, {str(self.p.state / "tmp/T-001")})
        self.assertFalse((self.p.state / "tmp/T-001").exists())

    def test_a_tickets_check_gets_its_temp_dir_and_a_failed_ticket_loses_it(self):
        log = self.s.tmp / "check.log"
        jobs.run_logged(self.p, "T-007", 'echo "$TMPDIR" > seen', self.s.root, log)
        self.assertEqual((self.s.root / "seen").read_text().strip(), str(self.p.state / "tmp/T-007"))
        pilot = driver.Pilot(self.p, args())
        with contextlib.redirect_stdout(io.StringIO()):
            pilot.fail("T-007", "on purpose")
        self.assertFalse((self.p.state / "tmp/T-007").exists())


if __name__ == "__main__":
    unittest.main()
