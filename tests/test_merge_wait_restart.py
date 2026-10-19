"""A merge held by required checks still running keeps its clock across a restart of the same run: a driver killed
mid-wait and restarted merges the kept approval with no new session, and fails CHECKS_MOST minutes after its first
refusal, not the restart's; a new run starts the clock over. Only the checks the base requires hold the merge, a
wait shows in `windvane status`, and a `gh pr ready` refused with a 403 names the fix."""
import contextlib
import fcntl
import io
import json
import os
import re
import signal
import subprocess
import sys
import time
import unittest
from argparse import Namespace
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from windvane import cli, driver  # noqa: E402
from windvane.config import Project  # noqa: E402
from windvane.util import now  # noqa: E402
from test_merge_blocked import GH, RUNNING, args  # noqa: E402
from helpers import Sandbox  # noqa: E402

# test_merge_blocked's fake gh, with `gh pr checks --required`: the state dir's `required` (a JSON list of names)
# answers it; with no such file it fails as a gh without the flag does, and `required_fails` (a count) fails that many
# calls first as an API hiccup does. `held` blocks the PR by a rule its checks and review don't show (a signature).
# `break` (a count of refused merges) makes the checks fail after that many, as `clear` makes them pass.
GH = GH.replace("""elif args[:2] == ["pr", "view"] and""", """elif args[:2] == ["pr", "checks"]:
    if int(read("required_fails", "0")) > 0:
        (st / "required_fails").write_text(str(int(read("required_fails")) - 1))
        sys.exit("HTTP 502: Bad Gateway (https://api.github.com/graphql)")
    if not (st / "required").exists():
        sys.exit("unknown flag: --required")
    names = json.loads(read("required"))
    if not names:
        sys.exit("no required checks reported on the 'main' branch")
    print(json.dumps([{{"name": n}} for n in names]))
elif args[:2] == ["pr", "view"] and""").replace("blocked = review", "blocked = (st / 'held').exists() or review").replace(
    """                           ("stop",""", """                           ("break", lambda: (st / "checks").write_text(json.dumps(
                [dict(c, status="COMPLETED", conclusion="FAILURE") for c in checks]))),
                           ("stop",""")

# A driver in a process of its own, killed by the test: it tries the merge again only after a minute, so the kill
# comes in the wait between tries, with its state saved.
CHILD = """import sys, time
from argparse import Namespace
from datetime import timedelta
sys.path.insert(0, {root!r})
from windvane import driver
from windvane.config import Project
from windvane.util import now
driver.CHECKS_EVERY = 1
pilot = driver.Pilot(Project({project!r}), Namespace(**{args!r}))
pilot.sleep = lambda s: None
pilot.pause = lambda seconds=30: time.sleep(0.05)
pilot.run(now() + timedelta(hours=1))
"""

PREVIEW = {"__typename": "CheckRun", "name": "preview", "status": "IN_PROGRESS", "conclusion": ""}


class MergeWaitRestart(unittest.TestCase):
    def setUp(self):
        self.s = Sandbox()
        self.addCleanup(self.s.close)
        self.s.ticket("T-001")
        cfg = self.s.root / ".windvane/config.toml"
        cfg.write_text(cfg.read_text().replace('forge = "git"', 'forge = "github"'))
        self.s.publish()
        self.p = Project(self.s.root)
        self.state = self.s.tmp / "gh"
        self.state.mkdir()
        gh = self.s.bin / "gh"
        gh.write_text(GH.format(python=sys.executable, state=self.state, remote=self.s.remote, run_state=self.p.state))
        gh.chmod(0o755)
        orig, driver.push = driver.push, lambda *a, **k: None
        self.addCleanup(setattr, driver, "push", orig)
        for name in ("CHECKS_EVERY", "CHECKS_MOST"):
            self.addCleanup(setattr, driver, name, getattr(driver, name))
        driver.CHECKS_EVERY = 0  # a test can't wait minutes between tries
        self.addCleanup(os.environ.pop, "WINDVANE_RESTARTED", None)

    def files(self, **files):
        for name, text in files.items():
            (self.state / name).write_text(text)

    def run_pilot(self, restarted=False, **files):
        self.files(**files)
        if restarted:  # as `supervise` and a self-update start the driver that goes on with the run
            os.environ["WINDVANE_RESTARTED"] = "1"
        pilot = driver.Pilot(self.p, args())
        pilot.sleep = lambda s: None
        pilot.pause = lambda seconds=30: time.sleep(0.05)
        pilot.run(now() + timedelta(hours=1))
        return pilot

    def start_child(self, **files):
        """A driver in its own process, once its merge waits on the checks (state.json says so)."""
        self.files(**files)
        script = self.s.tmp / "child.py"
        script.write_text(CHILD.format(root=str(Path(__file__).resolve().parents[1]), project=str(self.s.root),
                                       args=vars(args())))
        child = subprocess.Popen([sys.executable, str(script)], cwd=self.s.root, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL)
        self.addCleanup(lambda: child.poll() is None and (child.kill(), child.wait()))
        until = time.monotonic() + 60
        while "T-001" not in self.saved().get("checks", {}):
            self.assertIsNone(child.poll(), "the driver ended before its merge waited")
            self.assertLess(time.monotonic(), until, "the merge never waited")
            time.sleep(0.05)
        return child

    def kill(self, child):
        child.send_signal(signal.SIGKILL)
        child.wait()

    def saved(self):
        try:
            return json.loads((self.p.state / "state.json").read_text())
        except (OSError, ValueError):
            return {}

    def log(self):
        return re.sub(r"^\S+ \S+ ", "", (self.p.state / "autopilot.log").read_text(), flags=re.M)

    def merges(self):
        return sum(c.startswith("pr merge 7") for c in (self.state / "log").read_text().splitlines())

    def sessions(self):
        """Worker, critic and review sessions in the ledger, in that order."""
        rows = [json.loads(line) for line in (self.p.state / "ledger.jsonl").read_text().splitlines()]
        return tuple(sum(r.get("role") == role and r.get("ticket") == "T-001" for r in rows)
                     for role in ("worker", "critic", "reviewer"))

    def status(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.cmd_status(Namespace(line=False), self.p)
        return out.getvalue()

    def test_a_driver_killed_mid_wait_and_restarted_merges_the_kept_approval_with_no_new_session(self):
        child = self.start_child(checks=RUNNING, required='["build"]')
        self.assertEqual(self.sessions(), (1, 0, 1))
        self.kill(child)
        self.assertIn("T-001", self.saved()["landing"])
        before = self.merges()
        pilot = self.run_pilot(restarted=True, clear="1")
        self.assertEqual((pilot.merged, pilot.failed), (["T-001"], []))
        self.assertEqual(self.merges() - before, 2)  # tried again at once, merged once the checks passed
        self.assertEqual(self.sessions(), (1, 0, 1))
        self.assertEqual(self.log().count("merge waits: PR #7's required checks are still running"), 1)
        self.assertEqual(self.saved()["checks"], {})

    def test_the_restarted_wait_fails_counted_from_its_first_refusal_and_a_new_run_starts_over(self):
        child = self.start_child(checks=RUNNING, required='["build"]')
        time.sleep(2)
        since = self.saved()["checks"]["T-001"]["since"]
        self.kill(child)
        driver.CHECKS_MOST = (time.time() - since - 1) / 60  # over already, from the first refusal; not from now
        before = self.merges()
        pilot = self.run_pilot(restarted=True)
        self.assertEqual((pilot.merged, pilot.failed), ([], ["T-001"]))
        self.assertEqual(self.merges() - before, 1)  # its first try back fails: no fresh hour
        self.assertIn(f"T-001: FAILED: the merge was refused: required checks are still running after "
                      f"{driver.CHECKS_MOST:g} min", self.log())
        self.assertIn("T-001", self.saved()["landing"])
        self.assertEqual(self.saved()["checks"], {})
        before = self.merges()
        pilot = self.run_pilot()  # a new run: `windvane retry`'s own clock
        self.assertEqual(pilot.failed, ["T-001"])
        self.assertGreaterEqual(self.merges() - before, 2)  # waited again
        self.assertEqual(self.sessions(), (1, 0, 1))

    def test_status_shows_the_waiting_ticket_its_minutes_and_its_required_checks_only(self):
        checks = json.dumps(json.loads(RUNNING) + [PREVIEW])
        child = self.start_child(checks=checks, required='["build"]')
        self.assertIn("merge waits: T-001 on PR #7's required checks (build), 0 min of 60", self.status())
        self.kill(child)
        self.assertNotIn("merge waits: T-001 on", self.status())  # no run going: nothing waits
        st = self.saved()
        st["checks"]["T-001"]["since"] -= 12 * 60  # a wait that began 12 minutes ago, its run still going
        (self.p.state / "state.json").write_text(json.dumps(st))
        with open(self.p.state / "lock", "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            self.assertIn("merge waits: T-001 on PR #7's required checks (build), 12 min of 60", self.status())
        self.assertIn("T-001: merge waits: PR #7's required checks are still running (build); trying again every 1 "
                      "min", self.log())

    def test_optional_checks_beside_a_required_review_fail_it_at_once_as_a_review_block(self):
        lint = {"__typename": "CheckRun", "name": "lint", "status": "COMPLETED", "conclusion": "FAILURE"}
        pilot = self.run_pilot(checks=json.dumps([PREVIEW, lint]), required='["build"]', review="REVIEW_REQUIRED")
        self.assertEqual((pilot.failed, self.merges()), (["T-001"], 1))
        self.assertIn("T-001: FAILED: the merge was refused: a required review hasn't approved it; once it's given",
                      self.log())
        self.assertNotIn("merge waits", self.log())

    def test_an_optional_check_running_beside_a_block_neither_shows_fails_at_once(self):
        pilot = self.run_pilot(checks=json.dumps([PREVIEW]), required="[]", held="")
        self.assertEqual((pilot.failed, self.merges()), (["T-001"], 1))
        self.assertIn("T-001: FAILED: the merge was refused: a required check or review blocks it", self.log())
        self.assertNotIn("merge waits", self.log())

    def test_a_gh_that_cant_say_which_checks_are_required_counts_any_running_one_and_says_so_once(self):
        driver.CHECKS_EVERY, driver.CHECKS_MOST = 0.002, 0.005
        pilot = self.run_pilot(checks=json.dumps([PREVIEW]), held="")
        self.assertEqual(pilot.failed, ["T-001"])
        self.assertGreaterEqual(self.merges(), 2)
        self.assertEqual(self.log().count("gh couldn't say which of PR #7's checks are required"), 1)
        self.assertIn("T-001: merge waits: PR #7's checks are still running (preview)", self.log())
        self.assertIn("T-001: FAILED: the merge was refused: checks are still running after 0.005 min", self.log())
        self.assertIn("a newer gh, with `--required`, tells them apart", self.log())

    def test_a_gh_hiccup_on_the_required_checks_is_asked_again_at_the_next_refusal(self):
        checks = json.dumps(json.loads(RUNNING) + [PREVIEW])
        pilot = self.run_pilot(checks=checks, required='["build"]', required_fails="1", clear="3")
        self.assertEqual(pilot.merged, ["T-001"])
        asked = [c for c in (self.state / "log").read_text().splitlines() if c.startswith("pr checks 7")]
        self.assertEqual(len(asked), 2)  # the hiccup, then the answer; not again once it's known
        self.assertEqual(self.log().count("gh couldn't say which of PR #7's checks are required"), 1)

    def test_status_says_checks_not_required_ones_when_gh_couldnt_say(self):
        child = self.start_child(checks=json.dumps([PREVIEW]), held="")
        self.assertIn("merge waits: T-001 on PR #7's checks (preview), 0 min of 60", self.status())
        self.kill(child)

    def test_a_wait_that_ends_in_a_failed_check_leaves_no_wait_for_status_or_a_restart(self):
        pilot = self.run_pilot(checks=RUNNING, required='["build"]', **{"break": "2"})
        self.assertEqual((pilot.failed, self.merges()), (["T-001"], 2))  # waited once, then the check failed
        self.assertIn("T-001: FAILED: the merge was refused: a check failed", self.log())
        self.assertEqual(self.saved()["checks"], {})
        self.assertIn("T-001", self.saved()["landing"])  # the approval stays, for `windvane retry`
        with open(self.p.state / "lock", "a") as lock:  # as if the run were still going
            fcntl.flock(lock, fcntl.LOCK_EX)
            self.assertNotIn("merge waits: T-001 on", self.status())
        pilot.hand_over()  # a self-update: the same run goes on
        os.environ["WINDVANE_RESTARTED"] = "1"
        again = driver.Pilot(self.p, args())
        again.sleep, pauses = (lambda s: None), []
        # idling on a wait that ended would last until the deadline: a stop after a few pauses ends the test instead
        again.pause = lambda seconds=30: pauses.append(1) or len(pauses) < 5 or (self.p.state / "STOP").write_text("")
        again.run(now() + timedelta(hours=1))
        self.assertEqual((pauses, self.merges()), ([], 2))
        self.assertEqual(self.log().count("T-001: FAILED"), 1)

    def test_a_ready_refused_with_a_403_names_gh_auth_refresh(self):
        pilot = self.run_pilot(draft="", stuck="gh: Resource not accessible by integration (HTTP 403)")
        self.assertEqual(pilot.failed, ["T-001"])
        self.assertIn("T-001: FAILED: PR #7 is a draft and GitHub refused to mark it ready: the gh login lacks write "
                      "access to pull requests (`gh auth refresh -s repo` gives it, or a token with pull-request "
                      "write access) (gh: Resource not accessible by integration (HTTP 403))", self.log())


if __name__ == "__main__":
    unittest.main()
