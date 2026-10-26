"""`check_quick`: a worker's `windvane check` runs it first, and its failure ends the check in a minute with no pass
recorded; its pass goes on to `check`. The driver's gate runs `check` alone. Both commands are told the branch's
changed paths (`WINDVANE_CHANGED`, a file) and who runs them (`WINDVANE_CHECK_BY`). `stats --projects` and `doctor`
point a project with slow checks and no quick pass at it."""
import io
import json
import os
import re
import sys
import time
import unittest
from contextlib import redirect_stdout
from datetime import timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from windvane import cli, doctor, driver, heavy, jobs  # noqa: E402
from windvane.config import Project  # noqa: E402
from windvane.heavy import STOP_AT  # noqa: E402
from windvane.runner import IN_JOB, IN_SANDBOX, TICKET  # noqa: E402
from windvane.util import append_jsonl, now, read_jsonl  # noqa: E402
from helpers import Sandbox  # noqa: E402
from test_pipeline import args  # noqa: E402


def seer(name, seen, end):
    """A check command that writes what it saw outside the tree (a write inside would keep its pass unrecorded),
    says its name in the log, and ends with `end`."""
    return (f'echo "$WINDVANE_CHECK_BY" >> {seen}/{name}.by; cp "$WINDVANE_CHANGED" {seen}/{name}.changed; '
            f'echo {name}-out; {end}')


class Base(unittest.TestCase):
    QUICK = "exit $(cat {seen}/quick.exit)"

    def setUp(self):
        self.s = Sandbox()
        self.addCleanup(self.s.close)
        self.addCleanup(os.chdir, os.getcwd())
        self.seen = self.s.tmp / "seen"
        self.seen.mkdir()
        (self.seen / "quick.exit").write_text("3")
        quick = f"\ncheck_quick = '{seer('quick', self.seen, self.QUICK.format(seen=self.seen))}'" if self.QUICK else ""
        conf = (self.s.root / ".windvane/config.toml").read_text().replace(
            'check = "test -f README"', f"check = '{seer('check', self.seen, 'test -f README')}'{quick}")
        self.s.write(".windvane/config.toml", conf)
        self.s.write("old.txt", "going\n")
        self.s.publish("config")
        self.p = Project(self.s.root)

    def branch(self):
        """A branch that adds, changes and deletes a path, committed and pushed (the tree clean, so a pass is
        recorded): the paths WINDVANE_CHANGED must list."""
        self.s.git("switch", "-q", "-c", "t-001-work")
        self.s.write("sub/new.txt", "new\n")
        self.s.write("sub/naïve.txt", "unquoted\n")
        self.s.write("README", "changed\n")
        (self.s.root / "old.txt").unlink()
        self.s.git("add", "-A")
        self.s.git("commit", "-qm", "T-001: work")
        self.s.git("push", "-q", "-u", "origin", "t-001-work")
        return ["README", "old.txt", "sub/naïve.txt", "sub/new.txt"]

    def windvane(self, *argv):
        """`windvane <argv>` in the project, in this process: (exit code, output)."""
        out = io.StringIO()
        with redirect_stdout(out), self.assertRaises(SystemExit) as e:
            cli.main(["-C", str(self.s.root), *argv])
        code = e.exception.code
        return (code if isinstance(code, int) else 0 if code is None else 1), out.getvalue()

    def saw(self, name):
        """(the WINDVANE_CHECK_BY lines, the changed paths) the command `name` last saw; None if it never ran."""
        by = self.seen / f"{name}.by"
        if not by.exists():
            return None
        return by.read_text().split(), (self.seen / f"{name}.changed").read_text().splitlines()

    def passes(self):
        d = self.p.state / "passes"
        return sorted(d.iterdir()) if d.exists() else []

    def check_rows(self):
        return [r for r in read_jsonl(self.p.state / "ledger.jsonl") if r.get("event") == "check"]


class WorkerQuickPass(Base):
    def setUp(self):
        super().setUp()
        os.environ[TICKET] = "T-001"  # a ticket's check, so the ledger counts it

    def test_a_failing_quick_pass_ends_the_check_without_running_check(self):
        changed = self.branch()
        code, out = self.windvane("check")
        self.assertEqual(code, 1, out)
        self.assertIsNone(self.saw("check"), "check ran after the quick pass failed")
        self.assertEqual(self.saw("quick"), (["worker"], changed))
        summary = out.splitlines()[-1]
        self.assertRegex(summary, r"^CHECK FAILED \(quick pass, exit 3\) in \d+\.\d min; log \S+\.log$")
        self.assertIn("quick-out", out.splitlines()[:-1], "the quick log's tail isn't shown")
        self.assertEqual(self.passes(), [])
        row = self.check_rows()[-1]
        self.assertEqual((row["passed"], row["role"]), (False, "driver"))  # no session role at the desk
        self.assertIn("quick_minutes", row)
        self.assertEqual(list((self.p.state / "checks").glob("*.changed")), [], "the changed list stayed")

    def test_a_passing_quick_pass_runs_check_and_records_its_pass(self):
        (self.seen / "quick.exit").write_text("0")
        changed = self.branch()
        code, out = self.windvane("check")
        self.assertEqual(code, 0, out)
        self.assertRegex(out.splitlines()[-1], r"^CHECK PASSED in \d+\.\d min$")
        self.assertEqual(self.saw("quick"), (["worker"], changed))
        self.assertEqual(self.saw("check"), (["worker"], changed))
        log = (self.p.state / "checks").glob("*.log")
        text = next(log).read_text()
        self.assertLess(text.index("quick-out"), text.index("check-out"), "check didn't run after the quick pass")
        self.assertEqual(len(self.passes()), 1)
        row = self.check_rows()[-1]
        self.assertTrue(row["passed"])
        self.assertGreaterEqual(row["minutes"], row["quick_minutes"])
        code, out = self.windvane("check")  # the same tree: reused, nothing runs
        self.assertIn("reused the pass", out)
        self.assertEqual(self.saw("quick")[0], ["worker"], "the quick pass ran again on a passed tree")


    def test_an_unknown_upstream_gives_an_empty_list_and_the_check_still_runs(self):
        (self.seen / "quick.exit").write_text("0")
        self.branch()
        self.s.git("update-ref", "-d", "refs/remotes/origin/main")
        code, out = self.windvane("check")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.saw("quick"), (["worker"], []))
        self.assertEqual(self.saw("check"), (["worker"], []))

    def test_a_check_killed_in_its_quick_pass_leaves_nothing_the_next_check_trips_on(self):
        """SIGKILL skips every `finally`: its change list stays until the next check's prune, which takes it once
        the dead check's log is unlocked (its orphaned command gone too); the next check runs its quick pass as
        usual."""
        self.branch()
        go = self.s.tmp / "go"
        self.s.write(".windvane/config.toml", (self.s.root / ".windvane/config.toml").read_text().replace(
            "exit $(cat", f"while [ ! -e {go} ]; do sleep 0.05; done; exit $(cat"))
        self.s.git("commit", "-qam", "a quick pass that waits")
        p = Project(self.s.root)
        child = os.fork()
        if not child:
            try:
                jobs.check(p, self.s.root, ticket="T-001", by="worker")
            finally:
                os._exit(0)
        end = time.time() + 10
        while not self.saw("quick"):
            self.assertLess(time.time(), end, "the quick pass never started")
            time.sleep(0.05)
        os.kill(child, 9)
        os.waitpid(child, 0)
        left = list((p.state / "checks").glob("*.changed"))
        self.assertEqual(len(left), 1, "SIGKILL ran the cleanup after all: nothing to prove")
        go.touch()  # the orphaned quick pass ends, and with it the lock it inherited on the dead check's log
        while jobs.locked(left[0].with_suffix(".log")):
            self.assertLess(time.time(), end, "the orphaned quick pass never ended")
            time.sleep(0.05)
        (self.seen / "quick.by").unlink()
        ok, summary, _ = jobs.check(p, self.s.root, ticket="T-001", by="worker")
        self.assertIn("quick pass, exit 3", summary)
        self.assertEqual(self.saw("quick")[0], ["worker"])
        self.assertEqual(list((p.state / "checks").glob("*.changed")), [])
        self.assertEqual(self.passes(), [])


class HeavyQuickPass(Base):
    """With `heavy_check`, the quick pass isn't queued for the machine: it fails in its own time while another job
    holds the machine, and only `check` waits for it."""

    def setUp(self):
        super().setUp()
        from test_heavy_check_wait import hold, machine
        patch = mock.patch.dict(os.environ, machine(self.s.tmp / "machine", 0.02))  # gives up after 1.2 s
        patch.start()
        self.addCleanup(patch.stop)
        for k in ("WINDVANE_HEAVY_HELD", STOP_AT, TICKET):
            os.environ.pop(k, None)
        self.s.write(".windvane/config.toml", (self.s.root / ".windvane/config.toml").read_text().replace(
            "check_quick =", "heavy_check = true\ncheck_quick ="))
        self.s.publish("heavy")
        self.p = Project(self.s.root)
        hold(self, self.s.tmp / "machine")

    def test_a_failing_quick_pass_never_waits_for_the_machine(self):
        ok, summary, log = jobs.check(self.p, self.s.root, ticket="T-001", by="worker")
        self.assertFalse(ok)
        self.assertRegex(summary, r"^CHECK FAILED \(quick pass, exit 3\)")
        self.assertNotIn("waiting", log.read_text())
        self.assertNotIn("waited_s", self.check_rows()[-1])

    def test_a_passing_one_hands_over_to_the_heavy_check(self):
        (self.seen / "quick.exit").write_text("0")
        ok, summary, _ = jobs.check(self.p, self.s.root, ticket="T-001", by="worker")
        self.assertIsNone(ok, summary)  # the machine stayed held: `check` waited and never ran
        self.assertTrue(summary.startswith(jobs.NOT_RUN), summary)
        self.assertEqual(self.saw("quick")[0], ["worker"])
        self.assertIsNone(self.saw("check"))


class SessionQuickPass(Base):
    """In a session the whole check is the one bg job `check`, the quick pass inside it."""

    def setUp(self):
        super().setUp()
        patch = mock.patch.dict(os.environ, {TICKET: "T-001", STOP_AT: str(time.time() + 600)})
        patch.start()
        self.addCleanup(patch.stop)
        for k in (IN_SANDBOX, IN_JOB, "WINDVANE_HEAVY_HELD"):  # a suite run as a background job is in one
            os.environ.pop(k, None)
        self.addCleanup(lambda: jobs.alive(b := jobs.job_base(self.p, "check")) and jobs.end_job(b, "test over"))

    def test_the_quick_failure_comes_back_through_the_check_job(self):
        self.branch()
        code, out = self.windvane("check")
        for _ in range(20):
            if code != heavy.TIMED_OUT:
                break
            code, out = self.windvane("await", "check")
        self.assertEqual(code, 1, out)
        self.assertRegex(out, r"CHECK FAILED \(quick pass, exit 3\) in \d+\.\d min; log ")
        self.assertIn("quick-out", out)
        self.assertIsNone(self.saw("check"))
        self.assertEqual(self.passes(), [])


class DriverGate(Base):
    def test_the_gate_runs_check_alone(self):
        changed = self.branch()
        ok, summary, _ = jobs.check(self.p, self.s.root, ticket="T-001")
        self.assertTrue(ok, summary)
        self.assertIsNone(self.saw("quick"), "the gate ran the quick pass")
        self.assertEqual(self.saw("check"), (["driver"], changed))
        self.assertNotIn("quick_minutes", self.check_rows()[-1])

    def test_a_run_merges_on_check_alone_whatever_the_quick_pass_says(self):
        self.s.ticket("T-001")
        self.s.publish()
        pilot = driver.Pilot(self.p, args())
        pilot.run(now() + timedelta(hours=1))
        self.assertEqual(pilot.merged, ["T-001"])
        self.assertIsNone(self.saw("quick"), "the driver ran the quick pass (it fails: exit 3)")
        by, changed = self.saw("check")
        self.assertEqual(set(by), {"driver"})
        self.assertIn("T-001.txt", changed)


class Unset(Base):
    QUICK = ""

    def test_without_check_quick_the_check_is_as_before_and_still_told_the_change(self):
        changed = self.branch()
        (self.s.root / "README").unlink()  # the check fails
        self.s.git("commit", "-qam", "no README")
        code, out = self.windvane("check")
        self.assertEqual(code, 1, out)
        log = next((self.p.state / "checks").glob("*.log"))
        self.assertRegex(out.splitlines()[-1], rf"^CHECK FAILED \(exit 1\) in \d+\.\d min(, load peaked .+ to tell)?; "
                                                 rf"log {re.escape(str(log))}$")  # a busy machine adds the load
        self.assertEqual(self.saw("check"), (["worker"], changed))
        self.assertIsNone(self.saw("quick"))


class Hint(unittest.TestCase):
    """The `check_quick` hint, for a project with 12-minute checks."""

    def setUp(self):
        self.s = Sandbox()
        self.addCleanup(self.s.close)
        self.addCleanup(os.chdir, os.getcwd())
        self.p = Project(self.s.root)
        root = Path(os.environ["XDG_STATE_HOME"]) / "windvane"
        (root / "phone").mkdir(parents=True, exist_ok=True)
        (root / "phone/projects.json").write_text(json.dumps({"sandbox": str(self.s.root)}))

    def checks(self, minutes):
        for i, tid in enumerate(("T-001", "T-002")):
            append_jsonl(self.p.state / "ledger.jsonl", dict(event="check", ticket=tid, role="worker",
                                                             start=float(i), minutes=minutes, passed=True))

    def quick(self):
        self.s.write(".windvane/config.toml", (self.s.root / ".windvane/config.toml").read_text().replace(
            'check = "test -f README"\n', 'check = "test -f README"\ncheck_quick = "pytest -x --lf -q"\n'))
        self.p = Project(self.s.root)

    def stats(self):
        out = io.StringIO()
        os.chdir(self.s.tmp)
        with redirect_stdout(out):
            cli.main(["stats", "--projects"])
        lines = []
        for ln in out.getvalue().splitlines():  # a line wrapped onto the next, indented deeper, read as one
            if ln.startswith("    ") and lines:
                lines[-1] += " " + ln.strip()
            else:
                lines.append(ln.strip())
        return [ln for ln in lines if "check_quick" in ln]

    def doctor(self):
        return [what for _, what, _ in doctor.quick_warnings(self.p)]

    def test_slow_checks_with_no_quick_pass_get_the_hint(self):
        self.checks(12)
        line = ("checks averaged 12 min per checked ticket: set `check_quick` for a first pass that shows a failure "
                "in a minute (docs/configuration.md#the-checks-environment)")
        self.assertEqual(self.stats(), [line])
        self.assertEqual(self.doctor(), [line])
        with mock.patch.object(doctor, "claude_logged_in", lambda: True), redirect_stdout(io.StringIO()) as out:
            doctor.main(self.p)
        self.assertIn(f"WARNING   {line}  -> check_quick in .windvane/config.toml", out.getvalue())

    def test_no_hint_once_it_is_set(self):
        self.checks(12)
        self.quick()
        self.assertEqual((self.stats(), self.doctor()), ([], []))

    def test_no_hint_for_short_checks(self):
        self.checks(4)
        self.assertEqual((self.stats(), self.doctor()), ([], []))

    def test_no_hint_from_checks_before_the_window(self):
        old = f"{now() - timedelta(days=40):%Y-%m-%d %H:%M}"
        append_jsonl(self.p.state / "ledger.jsonl", dict(at=old, event="check", ticket="T-001", role="worker",
                                                         start=1.0, minutes=50, passed=True))
        self.assertEqual((self.stats(), self.doctor()), ([], []))


if __name__ == "__main__":
    unittest.main()
