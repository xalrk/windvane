"""New driver code a self-update takes up is on trial: two crashes, or every ticket failing the same way, revert it on
main (a revert commit, never a rewrite) with a ticket to fix it and one push, and the run goes on on the good code. A
second bad update in a row stops the run instead. The user's checkout is never touched."""
import io
import json
import os
import sys
import unittest
from contextlib import redirect_stdout
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from windvane import driver, selfupdate  # noqa: E402
from windvane.config import Project  # noqa: E402
from windvane.queue import Queue, landed  # noqa: E402
from windvane.util import now  # noqa: E402
from helpers import Sandbox  # noqa: E402
from test_parallel import args  # noqa: E402


class Restarted(Exception):
    pass


class Trial(unittest.TestCase):
    """A project that is windvane itself: its install is a clone of the same remote, and a ticket's merge (T-010)
    changed the driver's code; the driver restarted on it, which put it on trial."""

    def setUp(self):
        self.s = s = Sandbox()
        s.write("windvane/driver.py", "good\n")
        for t in ("T-001", "T-002", "T-010"):
            s.ticket(t)
        s.publish()
        self.good = s.git("rev-parse", "HEAD")
        self.pushes = []
        orig, driver.push = driver.push, lambda title, msg, *a, **k: self.pushes.append((title, msg))
        self.addCleanup(setattr, driver, "push", orig)
        self.p = Project(s.root)
        self.p.state.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        self.s.close()

    def update(self, text, tid="T-010"):
        """`tid`'s branch changes the driver to `text` and merges into main the way the driver merges; the install
        takes it up, and a restarted driver would now run it on trial."""
        s = self.s
        s.git("pull", "-q", "--ff-only", "origin", "main")
        s.git("switch", "-q", "-c", f"{tid.lower()}-change")
        s.write("windvane/driver.py", text)
        s.git("commit", "-qam", "change the driver")
        s.git("switch", "-q", "main")
        s.git("merge", "-q", "--no-ff", "-m", f"Merge {tid}: do {tid} (branch {tid.lower()}-change)", f"{tid.lower()}-change")
        s.git("push", "-q", "origin", "main")
        self.new = s.git("rev-parse", "HEAD")
        self.home = s.tmp / "install"
        if not self.home.exists():
            s.git("clone", "-q", str(s.remote), str(self.home), cwd=s.tmp)
        else:
            s.git("pull", "-q", "--ff-only", cwd=self.home)
        selfupdate.put_on_trial(self.p.state, self.good, self.new, 0)
        self.checkout = self.users_checkout()

    def users_checkout(self):
        s = self.s
        return (s.git("rev-parse", "--abbrev-ref", "HEAD"), s.git("rev-parse", "HEAD"), s.git("status", "--porcelain"),
                (s.root / "windvane/driver.py").read_text())

    def main(self, *a):
        return self.s.git(*a, cwd=self.s.remote)

    def installed(self):
        return (self.home / "windvane/driver.py").read_text()

    def titles(self):
        return [t for t, _ in self.pushes]

    def assert_reverted(self, how):
        """The merge is undone on main by a revert commit on top (main's history only grew), a ticket says why, and
        T-010 is back in the queue after it, waiting for it."""
        self.assertEqual(self.main("show", "main:windvane/driver.py"), "good")
        subjects = self.main("log", "--format=%s", "main").splitlines()
        self.assertIn(f"Merge T-010: do T-010 (branch t-010-change)", subjects)  # not rewritten
        revert = next(x for x in subjects if x.startswith("Revert"))
        self.assertTrue(revert.startswith(f"Revert T-010: windvane {self.new[:8]} {how}"), revert)
        self.main("merge-base", "--is-ancestor", self.new, "main")  # a fast-forward of the old main: no force push
        tid = "T-011"
        ticket = self.main("show", f"main:.windvane/tickets/{tid}.md")
        self.assertIn(f"# {tid}: Find and fix what broke windvane {self.new[:8]}", ticket)
        self.assertIn(how, ticket)
        self.assertIn(f"Depends on: {tid}.", self.main("show", "main:.windvane/tickets/T-010.md"))
        self.s.git("fetch", "-q", "origin")
        q = Queue(self.p)
        self.assertNotIn("T-010", q.done)
        self.assertEqual(q.order[:2], [tid, "T-010"])
        self.assertEqual(q.waits_on("T-010"), [tid])
        self.assertEqual(self.installed(), "good\n")  # the install is on the revert: the good code
        self.assertEqual(self.titles().count("windvane update reverted"), 1)
        self.assertEqual(self.users_checkout(), self.checkout)


class Crashes(Trial):
    """A driver that raises on start, run by `supervise` (a detached run)."""

    def supervise(self):
        self.started = []

        def start(argv, env):
            code = self.installed()
            self.started.append(code)
            return 1 if code == "broken\n" else 0  # dies before its run could write a crash report; the good one ends

        with redirect_stdout(io.StringIO()):
            return driver.supervise(self.p, ["run", "--for", "6h"], now() + timedelta(hours=2), start=start,
                                    sleep=lambda s: None, home=self.home)

    def test_a_driver_that_raises_on_start_is_reverted_and_the_run_goes_on_on_the_good_code(self):
        self.update("broken\n")
        self.assertEqual(self.supervise(), 0)
        self.assertEqual(self.started, ["broken\n", "broken\n", "good\n"])
        self.assert_reverted("crashed the driver twice on trial (it exited with code 1 before its run began)")
        self.assertEqual(self.titles(), ["windvane restarted", "windvane update reverted"])  # the 1st crash's, the revert's
        title, msg = self.pushes[-1]
        self.assertIn(f"Reverted on main", msg)
        self.assertIn("The run goes on without it.", msg)
        self.assertFalse((self.p.state / selfupdate.TRIAL).exists())
        self.assertTrue((self.p.state / selfupdate.REVERTED).exists())  # until a ticket merges

    def test_a_second_bad_update_in_a_row_stops_the_run(self):
        self.update("broken\n")
        self.supervise()
        self.good = self.main("rev-parse", "main")  # the run went on on the revert: the good code now
        self.update("broken again\n", "T-002")  # T-011 didn't merge: T-002's change breaks it again
        head = self.main("rev-parse", "main")
        self.pushes.clear()

        def start(argv, env):
            return 1 if self.installed() == "broken again\n" else 0

        with redirect_stdout(io.StringIO()):
            rc = driver.supervise(self.p, ["run", "--for", "6h"], now() + timedelta(hours=2), start=start,
                                  sleep=lambda s: None, home=self.home)
        self.assertEqual(rc, 1)
        self.assertEqual(self.main("rev-parse", "main"), head)  # nothing more reverted
        self.assertEqual(self.titles(), ["windvane restarted", "windvane stopped"])
        self.assertIn("right after a revert, with no ticket merged since: nothing more is reverted. To go on, revert "
                      f"{self.new[:8]} on main by hand", self.pushes[-1][1])
        self.assertIn(f"`git -C {self.home} pull --ff-only`", self.pushes[-1][1])
        self.assertEqual(self.users_checkout(), self.checkout)

    def take_up(self):
        """Doing what the stop's push says: the install pulls the revert by hand, and the next run starts on it."""
        self.assertIn(f"take it up with `git -C {self.home} pull --ff-only`", self.pushes[-1][1])
        self.s.git("pull", "-q", "--ff-only", cwd=self.home)
        self.pushes.clear()
        self.assertEqual(self.supervise(), 0)
        self.assertEqual(self.started, ["good\n"])
        self.assertEqual(self.pushes, [])

    def test_a_revert_left_waiting_on_a_pr_says_how_to_take_it_up(self):
        from windvane import held
        self.update("broken\n")
        real = held.land

        def land(*a, **k):
            real(*a, **k)  # the PR the user then merges
            return False, "PR #7"
        held.land = land
        try:
            self.assertEqual(self.supervise(), 1)
        finally:
            held.land = real
        self.assertEqual(self.titles(), ["windvane restarted", "windvane stopped"])
        self.assertIn("the revert waits on PR #7; merge it, take it up", self.pushes[-1][1])
        self.take_up()
        self.assertEqual(self.users_checkout(), self.checkout)

    def test_a_revert_the_install_did_not_take_up_says_how_to(self):
        self.update("broken\n")
        (self.home / ".windvane/tickets/T-001.md").write_text("edited in the install\n")
        self.assertEqual(self.supervise(), 1)
        self.assertEqual(self.main("show", "main:windvane/driver.py"), "good")
        self.assertIn("the install didn't take that up (windvane not updated: it has local changes): fix that, take it up", self.pushes[-1][1])
        self.take_up()
        self.assertEqual(self.users_checkout(), self.checkout)

    @unittest.skipUnless(os.path.expanduser("~").count("/") >= 2, "no home path to mask")
    def test_an_error_naming_the_home_path_reaches_main_without_it(self):
        self.update("broken\n")
        home = os.path.expanduser("~").rstrip("/")
        error = f"FileNotFoundError: [Errno 2] No such file or directory: '{home}/work/x.py'"

        def start(argv, env):
            if self.installed() == "broken\n":
                (self.p.state / "crash.json").write_text(json.dumps({"error": error, "summary": ""}))
                return 1
            return 0
        with redirect_stdout(io.StringIO()):
            driver.supervise(self.p, ["run"], now() + timedelta(hours=2), start=start, sleep=lambda s: None,
                             home=self.home)
        self.assertEqual(self.main("show", "main:windvane/driver.py"), "good")
        landed_text = self.main("log", "--format=%B", "main") + self.main("show", "main:.windvane/tickets/T-011.md") \
            + self.main("show", "main:.windvane/tickets/T-010.md")
        self.assertNotIn(home, landed_text)
        self.assertIn("No such file or directory: '~/work/x.py'", landed_text)
        self.assertEqual(self.main("show", "main:.windvane/QUEUE.md").count("- T-010 "), 1)  # moved up, not listed twice

    def test_a_crash_off_trial_is_restarted_as_before(self):
        self.update("broken\n")
        selfupdate.end_trial(self.p.state)
        with redirect_stdout(io.StringIO()):
            rc = driver.supervise(self.p, ["run"], now() + timedelta(hours=2), start=lambda a, e: 1,
                                  sleep=lambda s: None, home=self.home)
        self.assertEqual(rc, 1)  # its own end (a failed setup, say): not a crash, nothing reverted
        self.assertNotIn("Revert", self.main("log", "--format=%s", "main"))
        self.assertEqual(self.pushes, [])


class Tickets(Trial):
    """New code that fails every ticket at the check the same way: the driver reverts it itself."""

    def pilot(self):
        pilot = driver.Pilot(self.p, args(parallel=1))
        pilot.home, pilot.argv, self.calls = self.home, ["run", "--for", "6h", "--detach"], []
        pilot.loaded = self.s.git("rev-parse", "HEAD", cwd=self.home)  # as a driver started on the install reads it

        def execv(path, argv):
            self.calls.append(argv)
            raise Restarted()
        pilot.execv = execv
        return pilot

    def test_every_ticket_failing_the_check_the_same_way_is_reverted_and_the_run_goes_on(self):
        # the driver's broken code is what fails the check here: it reads the install's code
        cfg = self.s.root / ".windvane/config.toml"
        broken = f"grep -q broken {self.s.tmp}/install/windvane/driver.py"
        cfg.write_text(cfg.read_text().replace('check = "test -f README"',
                                               f"check = 'if {broken}; then echo \"Error: the driver is broken\"; exit 1; fi'"))
        self.s.publish("config")
        self.good, self.p = self.s.git("rev-parse", "HEAD"), Project(self.s.root)
        self.update("broken\n")
        pilot = self.pilot()
        with redirect_stdout(io.StringIO()), self.assertRaises(Restarted):
            pilot.run(now() + timedelta(hours=2))  # restarts itself on the revert
        log = (self.p.state / "autopilot.log").read_text()
        self.assertIn("windvane's new code failed T-001, T-002 the same way at the check", log)
        self.assert_reverted("failed T-001, T-002 the same way at the check: CHECK FAILED (exit 1): Error: the driver is broken")
        self.assertEqual(pilot.merged, [])
        self.assertFalse((self.p.state / selfupdate.TRIAL).exists())  # the good code back isn't on trial
        again = self.pilot()
        with redirect_stdout(io.StringIO()):
            again.run(now() + timedelta(hours=2))  # the restarted driver, on the good code
        self.assertEqual(sorted(again.merged), ["T-001", "T-002", "T-010", "T-011"])  # the failed one retried too
        self.assertLess(again.merged.index("T-011"), again.merged.index("T-010"))  # the fix, then what was reverted
        self.assertFalse((self.p.state / selfupdate.REVERTED).exists())  # a merge allows the next revert
        self.assertEqual(self.titles().count("windvane update reverted"), 1)
        self.assertEqual(self.users_checkout(), self.checkout)

    def test_a_good_update_passes_its_trial_and_is_kept(self):
        self.update("better\n")
        pilot = self.pilot()
        with redirect_stdout(io.StringIO()):
            pilot.run(now() + timedelta(hours=2))
        self.assertEqual(pilot.merged, ["T-001", "T-002"])
        self.assertIn(f"windvane {self.new[:8]} passed its trial: T-001 merged on it",
                      (self.p.state / "autopilot.log").read_text())
        self.assertFalse((self.p.state / selfupdate.TRIAL).exists())
        self.assertNotIn("Revert", self.main("log", "--format=%s", "main"))
        self.assertEqual(self.installed(), "better\n")
        self.assertNotIn("windvane update reverted", self.titles())
        self.assertEqual(self.users_checkout(), self.checkout)

    def test_a_restart_on_new_code_puts_it_on_trial_and_one_on_the_good_code_back_does_not(self):
        self.update("broken\n")
        selfupdate.end_trial(self.p.state)
        self.s.git("reset", "-q", "--hard", self.good, cwd=self.home)  # the install before it pulled the merge
        pilot = self.pilot()
        pilot.loaded, pilot.deadline = self.good, now() + timedelta(hours=2)
        with redirect_stdout(io.StringIO()), self.assertRaises(Restarted):
            pilot.self_update()
        t = selfupdate.on_trial(self.p.state)
        self.assertEqual((t["good"], t["new"], t["merged"]), (self.good, self.new, 0))
        (self.p.state / selfupdate.REVERTED).write_text(json.dumps({"good": self.good}))
        self.s.write("windvane/driver.py", "good\n")  # a revert landed: the install takes it up
        self.s.git("commit", "-qam", "Revert T-010: windvane broke")
        self.s.git("push", "-q", "origin", "main")
        pilot.loaded, pilot.pulled, pilot.behind = self.new, (0, -1), None  # the restarted driver, at its next pull
        with redirect_stdout(io.StringIO()), self.assertRaises(Restarted):
            pilot.self_update()
        self.assertIsNone(selfupdate.on_trial(self.p.state))

    def test_two_tickets_failing_their_own_ways_or_offline_are_no_bad_update(self):
        self.update("better\n")
        pilot = self.pilot()
        st, id_re = self.p.state, self.p.id_re
        one = selfupdate.error_lines("FAIL: test_a (tests.test_x)\nRan 2356 tests in 3.1s\nFAILED (failures=1)")
        two = selfupdate.error_lines("FAIL: test_b (tests.test_y)\nRan 2361 tests in 2.9s\nFAILED (failures=3)")
        self.assertFalse(selfupdate.setback(st, "T-001", "check", "x", "\n".join(one), id_re))
        self.assertFalse(selfupdate.setback(st, "T-002", "check", "x", "\n".join(two), id_re))
        same = "FAIL: test_a (tests.test_x)\nFAILED (failures=1)"
        selfupdate.put_on_trial(st, self.good, self.new, 0)
        selfupdate.setback(st, "T-001", "check", "x", same, id_re)
        selfupdate.passed(st, "check")  # a third ticket's check passed on this code: those were the tickets' own
        self.assertFalse(selfupdate.setback(st, "T-002", "check", "x", same, id_re))
        selfupdate.put_on_trial(st, self.good, self.new, 0)
        orig, driver.net.online = driver.net.online, lambda: False
        try:
            with redirect_stdout(io.StringIO()):
                pilot.fail("T-001", "driver error: KeyError: 'x'")
                pilot.fail("T-002", "driver error: KeyError: 'x'")
        finally:
            driver.net.online = orig
        self.assertIsNone(pilot.bad)  # an outage's failures are no one's
        with redirect_stdout(io.StringIO()):
            pilot.fail("T-001", "the reviewer asked for changes 3 times")  # the tickets' own: not compared
            pilot.fail("T-002", "the reviewer asked for changes 3 times")
        self.assertIsNone(pilot.bad)
        with redirect_stdout(io.StringIO()):
            pilot.fail("T-001", "driver error: KeyError: 'x'")
            pilot.fail("T-002", "driver error: KeyError: 'x'")
        self.assertEqual(pilot.bad, "failed T-001, T-002 the same way at the driver error: driver error: KeyError: 'x'")

    def killed(self, after_landing):
        """The supervisor killed while it reverts: before its push to main, or right after it."""
        from windvane import held
        real = held.land

        def land(*a, **k):
            if after_landing:
                real(*a, **k)
            raise SystemExit("killed")
        held.land = land
        try:
            with self.assertRaises(SystemExit):
                Crashes.supervise(self)
        finally:
            held.land = real
        self.assertIn("reverting", selfupdate.on_trial(self.p.state))  # still on trial: its revert isn't done
        self.assertFalse((self.p.state / selfupdate.REVERTED).exists())

    def test_a_revert_killed_before_it_lands_is_done_when_the_driver_starts_again(self):
        self.update("broken\n")
        self.killed(after_landing=False)
        self.assertNotIn("Revert", self.main("log", "--format=%s", "main"))
        with redirect_stdout(io.StringIO()), self.assertRaises(Restarted):
            self.pilot().run(now() + timedelta(hours=2))  # the next start finishes it, then restarts on the good code
        self.assert_reverted("crashed the driver twice on trial (it exited with code 1 before its run began)")

    def test_a_revert_killed_after_it_landed_is_not_landed_twice(self):
        self.update("broken\n")
        self.killed(after_landing=True)
        self.s.git("pull", "-q", "--ff-only", "origin", "main")
        self.s.write("windvane/other.py", "x\n")  # more driver code lands meanwhile
        self.s.git("add", "-A")
        self.s.git("commit", "-qm", "other")
        self.s.git("push", "-q", "origin", "main")
        self.checkout = self.users_checkout()
        with redirect_stdout(io.StringIO()), self.assertRaises(Restarted):
            self.pilot().run(now() + timedelta(hours=2))
        self.assert_reverted("crashed the driver twice on trial (it exited with code 1 before its run began)")
        self.assertEqual(sum(x.startswith("Revert") for x in self.main("log", "--format=%s", "main").splitlines()), 1)
        self.assertTrue((self.p.state / selfupdate.REVERTED).exists())  # the next bad update stops the run

    def test_a_trial_ends_after_an_hour(self):
        self.update("better\n")
        t = json.loads((self.p.state / selfupdate.TRIAL).read_text())
        t["since"] -= selfupdate.TRIAL_S
        (self.p.state / selfupdate.TRIAL).write_text(json.dumps(t))
        self.assertIsNone(selfupdate.on_trial(self.p.state))
        self.assertFalse(selfupdate.setback(self.p.state, "T-001", "check", "exit 1", "exit 1", self.p.id_re))


class Parts(unittest.TestCase):
    def test_a_revert_puts_its_tickets_back_until_they_merge_again(self):
        log = lambda *lines: "\n".join(lines)
        merge, plain = "a b\x1f", "a\x1f"
        self.assertEqual(landed(log(merge + "Merge T-002: x (branch t-002-x)", merge + "Merge T-001: x (branch t-001-x)"),
                                "t"), ["t-001", "t-002"])
        self.assertEqual(landed(log(plain + "Revert T-001, T-002: windvane abc crashed",
                                    merge + "Merge T-002: x (branch t-002-x)", merge + "Merge T-001: x (branch t-001-x)"),
                                "t"), [])
        self.assertEqual(landed(log(merge + "Merge T-001: again (branch t-001-y)", plain + "Revert T-001: windvane x",
                                    merge + "Merge T-001: x (branch t-001-x)"), "t"), ["t-001"])
        self.assertEqual(landed(log(plain + 'Revert "Merge T-001: x (branch t-001-x)"',
                                    merge + "Merge T-001: x (branch t-001-x)"), "t"), [])  # git's own revert
        self.assertEqual(landed(log(plain + 'Revert "Revert "Merge T-001: x (branch t-001-x)""',
                                    plain + 'Revert "Merge T-001: x (branch t-001-x)"',
                                    merge + "Merge T-001: x (branch t-001-x)"), "t"), ["t-001"])  # and its revert
        self.assertEqual(landed(log(plain + 'Revert "Revert T-001: windvane abc crashed"',
                                    plain + "Revert T-001: windvane abc crashed",
                                    merge + "Merge T-001: x (branch t-001-x)"), "t"), ["t-001"])  # the user undid ours
        self.assertEqual(landed(plain + "Merge T-001: squashed by hand (branch t-001-x)", "t"), [])  # not a merge

    def test_signature_reads_two_tickets_failing_alike_the_same(self):
        id_re = r"T-\d+[a-z]?"
        a = selfupdate.signature("Error at 1a2b3c4: exit 1 after 12.5s in /w/work-1/x.py (t-001-work, T-001)", id_re)
        b = selfupdate.signature("Error at 9f8e7d6: exit 1 after 3.1s in /w/work-2/x.py (t-002-other, T-002)", id_re)
        self.assertEqual(a, b)
        self.assertNotEqual(a, selfupdate.signature("Error at 1a2b3c4: exit 2 after 1s in /w/x.py (T-001)", id_re))

    def test_waits_for(self):
        self.assertEqual(selfupdate.waits_for("# T-1: x\nDepends on: nothing.\n", "T-9"), "# T-1: x\nDepends on: T-9.\n")
        self.assertEqual(selfupdate.waits_for("# T-1: x\nDepends on: T-3.\n", "T-9"), "# T-1: x\nDepends on: T-9, T-3.\n")
        self.assertEqual(selfupdate.waits_for("# T-1: x\n\nBody\n", "T-9"), "# T-1: x\nDepends on: T-9.\n\nBody\n")


if __name__ == "__main__":
    unittest.main()
