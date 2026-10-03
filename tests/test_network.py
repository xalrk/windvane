"""A network outage is waited out: a session it cut off is resumed without spending a crash retry or failing its
ticket, a git call it broke (even one that times out) is tried again once it's back, and failures during it don't
stop the run. With the network up, everything behaves as before."""
import io
import json
import os
import subprocess
import sys
import unittest
from contextlib import redirect_stdout
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from windvane import driver, net  # noqa: E402
from windvane.config import Project  # noqa: E402
from windvane.util import now  # noqa: E402
from helpers import Sandbox  # noqa: E402
from test_pipeline import args  # noqa: E402
from test_selfupdate import Install  # noqa: E402


class Probe:
    """A fake `online()`: down for the first `down` probes (or as `script` says, probe by probe), up after."""

    def __init__(self, down=0, script=()):
        self.script = list(script) or [False] * down

    def __call__(self):
        return self.script.pop(0) if self.script else True


class Base(unittest.TestCase):
    def setUp(self):
        self.s = Sandbox()
        self.s.ticket("T-001")
        self.s.ticket("T-002", "T-001")
        self.s.publish()
        self.p = Project(self.s.root)
        self.waits, self.pushes = [], []
        for mod in (driver, net):
            orig = mod.push
            mod.push = lambda title, *a, **k: self.pushes.append(title)
            self.addCleanup(setattr, mod, "push", orig)

    def tearDown(self):
        self.s.close()

    def pilot(self, fail="", down=0, script=()):
        os.environ["FAKE_FAIL"] = fail
        net.probe = Probe(down, script)
        pilot = driver.Pilot(self.p, args())
        pilot.sleep = self.waits.append
        return pilot

    def run_pilot(self, pilot, hours=1):
        with redirect_stdout(io.StringIO()):
            pilot.run(now() + timedelta(hours=hours))

    def log(self):
        return (self.p.state / "autopilot.log").read_text()

    def roles(self):
        calls = [json.loads(line) for line in (self.s.tmp / "calls.jsonl").read_text().splitlines()]
        return [c["system"].split(" run")[0][11:] for c in calls if c["system"].startswith("# windvane")]

    def prompts(self):
        calls = [json.loads(line) for line in (self.s.tmp / "calls.jsonl").read_text().splitlines()]
        return [c["prompt"] for c in calls if "worker" in c["system"]]


class Sessions(Base):
    def test_a_session_that_dies_in_an_outage_is_rerun_with_no_retry_spent(self):
        pilot = self.pilot("crash", down=2)  # the crash's probe and the wait's first: back a minute later
        self.run_pilot(pilot)
        self.assertEqual((pilot.merged, pilot.failed), (["T-001", "T-002"], []))
        self.assertEqual(self.roles(), ["worker", "worker", "review", "worker", "review"])
        self.assertEqual(self.waits, [60])  # one minute's wait for the network, not a 3-minute crash backoff
        self.assertIn("no retry spent", self.log())
        self.assertNotIn("retry 1/", self.log())
        self.assertIn("network down since", self.log())
        self.assertNotIn("sandbox: network back", self.pushes)  # a short outage pushes nothing

    def test_a_run_cut_off_without_a_result_line_is_resumed_and_not_counted(self):
        # "API Error: No response from API" as the run's last words, twice: neither counts toward failing it
        pilot = self.pilot("quiet,quiet", script=[False, False, True] * 2)  # each: its probe, the wait's, back
        self.run_pilot(pilot)
        self.assertEqual((pilot.merged, pilot.failed), (["T-001", "T-002"], []))
        prompts = self.prompts()
        self.assertIn("Your last run was cut off by a network outage. The worktree is on", prompts[1])
        self.assertIn("Your last run was cut off by a network outage.", prompts[2])
        self.assertEqual(self.waits, [60, 60])
        self.assertIn("the network is back; resuming the run", self.log())

    def test_a_long_outage_pushes_once_when_its_back(self):
        pilot = self.pilot("crash", down=7)  # probes at 0, 1, 3, 7, 12, 17 min down; up at 22
        self.run_pilot(pilot)
        self.assertEqual(pilot.merged, ["T-001", "T-002"])
        self.assertEqual(sum(self.waits) // 60, 22)
        self.assertEqual(self.pushes.count("sandbox: network back"), 1)
        self.assertIn("network back after 22 min", self.log())

    def test_a_stop_ends_the_wait_and_fails_nothing(self):
        pilot = self.pilot("crash", down=10 ** 6)

        def sleep(s):
            self.waits.append(s)
            if len(self.waits) == 2:
                (self.p.state / "STOP").write_text("")
        pilot.sleep = sleep
        self.run_pilot(pilot)
        self.assertEqual((pilot.merged, pilot.failed, pilot.crashes), ([], [], 0))
        self.assertEqual(self.roles(), ["worker"])
        self.assertIn("stopped waiting", self.log())
        self.assertEqual(pilot.stop_reason, "asked to stop")

    def test_with_the_network_up_a_dead_session_spends_a_retry_as_before(self):
        pilot = self.pilot("crash")
        self.run_pilot(pilot)
        self.assertEqual(pilot.merged, ["T-001", "T-002"])
        self.assertEqual(self.waits, [60] * 3)
        self.assertIn("retry 1/", self.log())
        self.assertNotIn("network", self.log())

    def test_failures_during_an_outage_dont_stop_the_run(self):
        pilot = self.pilot(down=2)
        pilot.fail("T-001", "x")
        pilot.fail("T-002", "y")
        self.assertEqual(pilot.fail_streak, 0)
        self.assertIn("not counted toward two failures in a row", self.log())
        net.probe = Probe(0)
        pilot.fail("T-003", "z")
        self.assertEqual(pilot.fail_streak, 1)


class Wait(Base):
    def wait(self, down, stop_after=None):
        net.probe, slept = Probe(down), []

        def sleep(s):
            slept.append(s)
            if stop_after and len(slept) >= stop_after:
                deadline[0] = now() - timedelta(minutes=1)
        deadline = [now() + timedelta(hours=1)]
        return net.wait(self.p, lambda: deadline[0], lambda: False, sleep), slept

    def test_backoff_is_1_2_4_then_5_minutes(self):
        ok, slept = self.wait(down=6)
        self.assertTrue(ok)
        self.assertEqual(len(slept), 1 + 2 + 4 + 5 + 5 + 5)
        self.assertEqual(self.log().count("network down since"), 1)

    def test_the_deadline_ends_a_wait(self):
        ok, slept = self.wait(down=10 ** 6, stop_after=3)
        self.assertFalse(ok)
        self.assertEqual(len(slept), 3)
        self.assertEqual(self.pushes, [])

    def test_up_at_once_waits_for_nothing(self):
        ok, slept = self.wait(down=0)
        self.assertEqual((ok, slept), (True, []))
        self.assertFalse((self.p.state / "autopilot.log").exists())


class GitCalls(Base):
    """`net.call` with a git that times out (or fails) until the network is back."""

    def fake_run(self, fails):
        calls, orig = [], net.run

        def run(cmd, cwd, timeout=None, env=None):
            calls.append(cmd)
            if len(calls) <= fails:
                raise subprocess.TimeoutExpired(cmd, timeout)
            return subprocess.CompletedProcess(cmd, 0, "ok\n", "")
        net.run = run
        self.addCleanup(setattr, net, "run", orig)
        return calls

    def test_a_timeout_with_the_network_down_waits_and_tries_again(self):
        calls, waited = self.fake_run(1), []
        net.probe, net.waiter = Probe(1), lambda: waited.append(1) or True
        self.assertEqual(net.sh(None, "git", "ls-remote", "origin"), "ok")
        self.assertEqual((len(calls), waited), (2, [1]))

    def test_a_timeout_with_the_network_up_is_a_failure_not_an_exception(self):
        calls, waited = self.fake_run(1), []
        net.waiter = lambda: waited.append(1) or True
        r = net.git(None, "fetch", "origin")
        self.assertEqual((r.returncode, len(calls), waited), (124, 1, []))

    def test_a_missing_gh_is_a_failure(self):
        self.assertEqual(net.call(["no-such-gh-here"], None).returncode, 127)

    def test_remote_hosts(self):
        self.assertEqual(net.remote_host("git@github.com:o/r.git"), ("github.com", 22))
        self.assertEqual(net.remote_host("https://github.com/o/r"), ("github.com", 443))
        self.assertEqual(net.remote_host("ssh://git@git.example.org:2222/r"), ("git.example.org", 2222))
        for local in ("/tmp/remote.git", "file:///tmp/r.git", "myalias:o/r.git"):
            self.assertIsNone(net.remote_host(local))


class SelfUpdate(Base):
    """The self-update's `git fetch` timing out (selfupdate.py once raised it out of the driver)."""

    def setUp(self):
        super().setUp()
        self.i = Install(self.s)
        self.new = self.i.commit("docs/x.md", "v2\n")  # no restart: the run goes on after the update
        orig, self.fetches = net.run, []

        def run(cmd, cwd, timeout=None, env=None):
            if cmd[:2] == ["git", "fetch"] and cwd == self.i.home:
                self.fetches.append(1)
                if len(self.fetches) == 1:
                    raise subprocess.TimeoutExpired(cmd, timeout)
            return orig(cmd, cwd, timeout, env)
        net.run = run
        self.addCleanup(setattr, net, "run", orig)

    def pilot(self, fail="", down=0):
        pilot = super().pilot(fail, down)
        pilot.home = self.i.home
        return pilot

    def test_a_fetch_timeout_with_the_network_up_is_logged_and_the_run_goes_on(self):
        pilot = self.pilot()
        self.run_pilot(pilot)
        self.assertEqual(pilot.merged, ["T-001", "T-002"])
        self.assertEqual(self.log().count("windvane not updated: git fetch timed out"), 1)

    def test_a_fetch_timeout_in_an_outage_waits_and_updates(self):
        pilot = self.pilot(down=2)
        self.run_pilot(pilot)
        self.assertEqual(pilot.merged, ["T-001", "T-002"])
        self.assertEqual((self.fetches[:2], self.waits), ([1, 1], [60]))
        self.assertNotIn("not updated", self.log())
        self.assertEqual(self.s.git("rev-parse", "HEAD", cwd=self.i.home), self.new)


if __name__ == "__main__":
    unittest.main()
