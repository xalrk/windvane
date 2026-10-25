"""A SIGTERM or SIGHUP ends a retry's backoff at once, in a slot's thread too, where no signal lands: the wind-down
joins that slot, so a backoff left to run its minutes out would outlast a service manager's stop timeout (systemd's
90 s) and its SIGKILL would cost the run its save, its teardown and its unlock. The other waits a slot or the
wind-down sits in end on the signal (or `stop --now`) within a second or two as well."""
import os
import signal
import subprocess
import sys
import threading
import time
import unittest
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from windvane import critic, driver, net, periodic, phone  # noqa: E402
from windvane.config import Project  # noqa: E402
from windvane.util import now  # noqa: E402
from helpers import Sandbox  # noqa: E402
from test_pipeline import args  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
QUICK = 10  # seconds a signalled run may take to end: far below systemd's 90 s, far above the backoff's wake-up


def wait_for(cond, seconds=60):
    until = time.monotonic() + seconds
    while not cond() and time.monotonic() < until:
        time.sleep(0.05)
    return cond()


class Backoff(unittest.TestCase):
    """`windvane run --parallel 2` as a service manager starts it, with T-001 in its first retry's real 3-minute
    backoff (the default `retry_minutes`) while T-002's worker works on in the other slot."""

    def setUp(self):
        self.addCleanup(os.chdir, os.getcwd())
        self.s = Sandbox()
        self.addCleanup(self.s.close)
        self.s.ticket("T-001", touches="a.py")
        self.s.ticket("T-002", touches="b.py")
        self.marker = self.s.tmp / "torn"
        self.cfg = self.s.root / ".windvane/config.toml"
        self.cfg.write_text(f'teardown = "echo x >> {self.marker}"\n' + self.cfg.read_text())
        self.s.publish()
        self.p = Project(self.s.root)
        for k in ("FAKE_ERROR_FOR", "FAKE_SLOW"):
            self.addCleanup(os.environ.pop, k, None)
        os.environ["FAKE_SLOW"] = "T-002=120"

    def critic_on(self):
        self.cfg.write_text(self.cfg.read_text().replace("enabled = false", "enabled = true"))
        self.s.publish("critic")
        self.p = Project(self.s.root)
        pack = critic.pack_dir(self.p, "T-001")  # the worker wrote its pack but didn't run the critic
        pack.mkdir(parents=True)
        (pack / "notes.md").write_text("User-facing: yes\nIt works.\n")

    def log(self):
        f = self.p.state / "autopilot.log"
        return f.read_text() if f.exists() else ""

    def run_signalled(self, sig, retrying):
        """The run, sent `sig` once `retrying` (its retry line) is in the log: the seconds it took to end, its code."""
        proc = subprocess.Popen([sys.executable, "-m", "windvane", "-C", str(self.s.root), "run", "--for", "1h",
                                 "--parallel", "2", "--no-preflight"], cwd=REPO, stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                env={**os.environ, "PYTHONPATH": str(REPO)})
        self.addCleanup(lambda: proc.poll() is None and proc.kill())
        # the other slot busy too: a two-slot run
        self.assertTrue(wait_for(lambda: retrying in self.log() and "T-002: worker run" in self.log()), self.log())
        self.sent = time.time()
        proc.send_signal(sig)
        try:
            rc = proc.wait(60)
        except subprocess.TimeoutExpired:
            rc = None
        return time.time() - self.sent, rc

    def assert_ended_whole(self, took, rc, sig, label):
        self.assertLess(took, QUICK, self.log())
        self.assertEqual(rc, -sig)
        self.assertIn(f"{label}: retry backoff ended: the driver got {signal.Signals(sig).name}", self.log())
        self.assertGreaterEqual((self.p.state / "state.json").stat().st_mtime, self.sent - 1)  # saved in the wind-down
        self.assertEqual(self.marker.read_text().splitlines(), ["x"])  # the teardown ran
        self.assertFalse(driver.locked(self.p))

    def test_sigterm_ends_a_slots_backoff_and_the_wind_down_finishes(self):
        os.environ["FAKE_ERROR_FOR"] = r"^Ticket T-001\."
        took, rc = self.run_signalled(signal.SIGTERM, "T-001 died (")
        self.assert_ended_whole(took, rc, signal.SIGTERM, "T-001")

    def test_sighup_too(self):
        os.environ["FAKE_ERROR_FOR"] = r"^Ticket T-001\."
        took, rc = self.run_signalled(signal.SIGHUP, "T-001 died (")
        self.assert_ended_whole(took, rc, signal.SIGHUP, "T-001")

    def test_a_critic_retrys_backoff_ends_as_well(self):
        self.critic_on()
        os.environ["FAKE_ERROR_FOR"] = r"^Critique T-001\."
        took, rc = self.run_signalled(signal.SIGTERM, "T-001-critic died (")
        self.assert_ended_whole(took, rc, signal.SIGTERM, "T-001-critic")


class Waits(unittest.TestCase):
    """Each wait, sat in by a thread other than the main one (a slot's), ended by a signal on the main thread."""

    def setUp(self):
        self.addCleanup(os.chdir, os.getcwd())
        self.s = Sandbox()
        self.addCleanup(self.s.close)
        self.s.ticket("T-001")
        self.s.publish()
        self.p = Project(self.s.root)
        self.pilot = driver.Pilot(self.p, args(answer_wait=10))
        self.pilot.deadline = now() + timedelta(hours=1)
        self.lines = []
        self.pilot.log = self.lines.append
        self.p.state.mkdir(parents=True, exist_ok=True)

    def in_slot(self, wait, signalled=signal.SIGTERM):
        """`wait()` in a thread, the pilot signalled a second in (as `on_signal` leaves it): (its result, seconds)."""
        got = {}

        def slot():
            t = time.monotonic()
            got["result"] = wait()
            got["took"] = time.monotonic() - t
        th = threading.Thread(target=slot, daemon=True)
        th.start()
        time.sleep(1)
        if signalled:
            self.pilot.signalled, self.pilot.kill_all = signalled, True
        else:
            (self.p.state / "STOP").write_text("now")
            self.pilot.stopping_now()  # a session's poll, in another slot
        self.pilot.interrupted.set()
        th.join(10)
        self.assertFalse(th.is_alive())
        return got["result"], got["took"]

    def test_a_backoff_says_what_ended_it(self):
        result, took = self.in_slot(lambda: self.pilot.backoff(3, "T-007"))
        self.assertIs(result, False)
        self.assertLess(took, 3)
        self.assertEqual(self.lines, ["T-007: retry backoff ended: the driver got SIGTERM"])

    def test_stop_now_ends_it_too(self):
        result, took = self.in_slot(lambda: self.pilot.backoff(3, "T-007"), signalled=None)
        self.assertIs(result, False)
        self.assertLess(took, 3)
        self.assertEqual(self.lines, ["T-007: retry backoff ended: `windvane stop --now`"])

    def test_a_backoff_already_signalled_never_naps(self):
        self.pilot.signalled, self.pilot.kill_all = signal.SIGHUP, True
        naps = []
        self.pilot.sleep = naps.append
        self.assertIs(self.pilot.backoff(3, "T-007"), False)
        self.assertEqual(naps, [])
        self.assertEqual(self.lines, ["T-007: retry backoff ended: the driver got SIGHUP"])

    def test_an_unsignalled_backoff_still_naps_its_minutes(self):
        naps = []
        self.pilot.sleep = naps.append
        self.assertIs(self.pilot.backoff(3, "T-007"), True)
        self.assertEqual(naps, [60, 60, 60])

    def test_a_nap_ends(self):
        _, took = self.in_slot(lambda: self.pilot.nap(now() + timedelta(minutes=5)))
        self.assertLess(took, 3)

    def test_an_outage_wait_ends(self):
        orig, net.probe = net.probe, lambda: False
        self.addCleanup(setattr, net, "probe", orig)
        result, took = self.in_slot(self.pilot.wait_online)
        self.assertIs(result, False)
        self.assertLess(took, 3)
        log = (self.p.state / "autopilot.log").read_text()
        self.assertIn("network still down after 0 min; stopped waiting", log)  # the nap cut short isn't a minute

    def test_a_login_wait_ends_without_counting_the_nap_it_cut(self):
        gone = self.s.tmp / "login"
        gone.write_text("out")  # the fake `claude auth status` says logged out
        os.environ["FAKE_LOGIN"] = str(gone)
        self.addCleanup(os.environ.pop, "FAKE_LOGIN", None)
        result, took = self.in_slot(lambda: self.pilot.wait_login("T-001", "OAuth token has expired"))
        self.assertIs(result, False)
        self.assertLess(took, 3)
        self.assertIn("Claude login still gone after 0 min; stopped waiting", (self.p.state / "autopilot.log").read_text())

    def test_the_gh_wait_says_it_was_the_gh_wait(self):
        result, took = self.in_slot(lambda: self.pilot.gh_wait("T-007", "list its PRs", lambda: None))
        self.assertIsNone(result)
        self.assertLess(took, 3)
        self.assertIn("T-007: wait for gh ended: the driver got SIGTERM", self.lines)

    def test_a_periodic_land_question_ends(self):
        orig = (phone.ask, phone.answer, periodic.quiet_now)
        phone.ask, phone.answer = (lambda *a, **k: True), (lambda qid: "WAITING")
        periodic.quiet_now = lambda p: (False, None)
        self.addCleanup(lambda: (setattr(phone, "ask", orig[0]), setattr(phone, "answer", orig[1]),
                                 setattr(periodic, "quiet_now", orig[2])))
        result, took = self.in_slot(lambda: self.pilot.ask_land("scout", ["T-009"], {"T-009": "x"}))
        self.assertEqual(result, "hold")
        self.assertLess(took, 3)


if __name__ == "__main__":
    unittest.main()
