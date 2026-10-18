"""`windvane retry <ID>`: a failed ticket goes back in a running autopilot's queue once its cause is fixed by hand,
without stopping the run. One that failed after its review approved goes straight to the merge while its branch still
has the approved head. A ticket held for another reason is refused with what lets it go."""
import fcntl
import json
import os
import re
import subprocess
import sys
import unittest
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from windvane import driver  # noqa: E402
from windvane.config import Project  # noqa: E402
from windvane.util import now, save_json  # noqa: E402
from helpers import Sandbox  # noqa: E402
from test_merge_draft import GH, args  # noqa: E402


class Base(unittest.TestCase):
    github = False

    def setUp(self):
        self.s = Sandbox()
        self.addCleanup(self.s.close)
        self.s.ticket("T-001")
        self.s.ticket("T-002")
        if self.github:
            cfg = self.s.root / ".windvane/config.toml"
            cfg.write_text(cfg.read_text().replace('forge = "git"', 'forge = "github"'))
            self.gh = self.s.tmp / "gh"
            self.gh.mkdir()
            gh = self.s.bin / "gh"
            gh.write_text(GH.format(python=sys.executable, state=self.gh, remote=self.s.remote))
            gh.chmod(0o755)
        self.s.publish()
        self.p = Project(self.s.root)
        self.p.state.mkdir(parents=True, exist_ok=True)
        orig, driver.push = driver.push, lambda *a, **k: True
        self.addCleanup(setattr, driver, "push", orig)

    def retry(self, tid="T-001"):
        return subprocess.run(["windvane", "retry", tid], cwd=self.s.root, capture_output=True, text=True)

    def pilot(self, before_t002=None):
        """A run whose T-002 worker first calls `before_t002()`: the user, at the desk while the run goes on."""
        pilot = driver.Pilot(self.p, args())
        pilot.sleep = lambda s: None
        work = pilot.work

        def hooked(tid, kind, branches):
            if tid == "T-002" and before_t002:
                self.said = before_t002()
            return work(tid, kind, branches)
        pilot.work = hooked
        pilot.run(now() + timedelta(hours=1))
        return pilot

    def log(self):
        return re.sub(r"^\S+ \S+ ", "", (self.p.state / "autopilot.log").read_text(), flags=re.M)

    def ledger(self):
        return [json.loads(line) for line in (self.p.state / "ledger.jsonl").read_text().splitlines()]

    def hold_lock(self):
        """A driver's lock held, as a running autopilot holds it."""
        fh = open(self.p.state / "lock", "a+")
        self.addCleanup(fh.close)
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)


class RetryInRun(Base):
    def test_a_failed_ticket_retried_while_the_run_goes_on_merges_in_the_same_run(self):
        os.environ["FAKE_FAIL"] = "quiet,quiet"  # T-001's worker twice ends without its RESULT line: it fails
        pilot = self.pilot(self.retry)
        self.assertEqual(self.said.returncode, 0, self.said.stderr)
        self.assertIn("T-001 goes back in the queue; the autopilot takes it up between tickets", self.said.stdout)
        self.assertEqual((pilot.merged, pilot.failed), (["T-002", "T-001"], []))
        self.assertIn("T-001: FAILED: the run ended without a result line, twice", self.log())
        self.assertIn("T-001: back in the queue (windvane retry)\n", self.log())
        self.assertFalse((self.p.state / driver.RETRY).exists())
        outcomes = [e["outcome"] for e in self.ledger() if e.get("event") == "outcome"]
        self.assertEqual(outcomes, ["failed", "retried"])  # `windvane stats` no longer counts it failed

    def test_a_ticket_not_failed_in_this_run_is_left_be(self):
        (self.p.state / driver.RETRY).write_text("T-002\n")
        pilot = driver.Pilot(self.p, args())
        pilot.failed, pilot.skip = [], set()
        pilot.take_retries()
        self.assertIn("T-002: asked to retry it, but it hasn't failed in this run", self.log())
        self.assertFalse((self.p.state / driver.RETRY).exists())


    def test_a_request_left_by_a_driver_that_died_reading_it_still_counts_beside_a_new_one(self):
        (self.p.state / (driver.RETRY + ".taking")).write_text("T-001\n")
        (self.p.state / driver.RETRY).write_text("T-002\n")
        pilot = driver.Pilot(self.p, args())
        pilot.failed, pilot.skip = ["T-001", "T-002"], {"T-001", "T-002"}
        pilot.take_retries()
        self.assertEqual((pilot.failed, pilot.skip), ([], set()))
        self.assertEqual(list(self.p.state.glob(driver.RETRY + "*")), [])


class RetryMerge(Base):
    github = True

    def sessions(self, after_retry=False):
        """T-001's worker and review sessions, all of them or only those after its retry."""
        rows, seen = [], not after_retry
        for e in self.ledger():
            seen = seen or (e.get("event") == "outcome" and e["outcome"] == "retried")
            if seen and e.get("ticket") == "T-001" and e.get("role") in ("worker", "reviewer"):
                rows.append(e["role"])
        return rows

    def test_a_refused_merge_retried_with_its_head_unchanged_merges_with_no_session(self):
        (self.gh / "refuse").write_text("BLOCKED")

        def fixed():  # the user lifts the branch protection, then retries
            (self.gh / "refuse").unlink()
            return self.retry()
        pilot = self.pilot(fixed)
        self.assertEqual((pilot.merged, pilot.failed), (["T-002", "T-001"], []))
        self.assertIn("T-001: FAILED: the merge was refused: a required check or review blocks it", self.log())
        self.assertRegex(self.log(), r"T-001: back in the queue \(windvane retry\); its approved head \w{7} merges "
                                     r"without a worker if its branch still has it")
        self.assertRegex(self.log(), r"T-001: t-001-work is still at its approved head \w{7}: straight to the merge")
        self.assertEqual(self.sessions(), ["worker", "reviewer"])
        self.assertEqual(self.sessions(after_retry=True), [])
        self.assertEqual(pilot.landing, {})

    def test_a_branch_moved_since_the_approval_goes_back_to_the_worker(self):
        self.s.git("push", "-q", "origin", "main:refs/heads/t-001-work")  # pushed to by hand since its approval
        pilot = driver.Pilot(self.p, args())
        pilot.landing = {"T-001": {"branch": "t-001-work", "sha": "f" * 40}}
        pilot.integrate = lambda *a: self.fail("merged a head the review never saw")
        self.assertFalse(pilot.land_again("T-001"))
        self.assertIn("T-001: t-001-work moved off its approved head fffffff: the worker takes it up again", self.log())
        self.assertEqual(pilot.landing, {})


class Refused(Base):
    def state(self, **st):
        save_json(self.p.state / "state.json", st)

    def test_a_ticket_held_for_a_question_is_refused_with_how_to_answer_it(self):
        self.hold_lock()
        self.state(failed=[], questions={"T-001": {"ticket": "T-001", "q": "Red or blue?", "opts": {"A": "red"},
                                                   "rec": "A", "must_wait": False, "branch": "", "asked": None}})
        r = self.retry()
        self.assertEqual(r.returncode, 1)
        self.assertIn("T-001 isn't retried: only a failed ticket is. T-001 has an open question: "
                      "`windvane answer T-001 <letter>`", r.stderr)
        self.assertFalse((self.p.state / driver.RETRY).exists())

    def test_a_ticket_set_aside_is_refused_with_its_fix(self):
        self.hold_lock()
        self.state(failed=[], aside={"T-001": "PR #7 is a draft on purpose"}, fixes={"T-001": "`gh pr ready 7`"})
        r = self.retry()
        self.assertEqual(r.returncode, 1)
        self.assertIn("T-001 was set aside this run (PR #7 is a draft on purpose); so it isn't set aside again, "
                      "`gh pr ready 7`", r.stderr)

    def test_a_parked_ticket_is_refused_with_its_queue_line(self):
        self.s.write(".windvane/QUEUE.md", "# Queue\n- parked: T-001 (later)\n")
        self.s.publish()
        self.hold_lock()
        self.state(failed=[])
        r = self.retry()
        self.assertIn("T-001 is parked; to unpark it, take `- parked: T-001 (later)` out of .windvane/QUEUE.md",
                      r.stderr)

    def test_a_ticket_with_nothing_holding_it_hasnt_failed(self):
        self.hold_lock()
        self.state(failed=["T-002"])
        r = self.retry()
        self.assertEqual(r.returncode, 1)
        self.assertIn("T-001 isn't retried: it hasn't failed in this run", r.stderr)

    def test_with_no_run_going_it_says_so_and_writes_nothing(self):
        self.state(failed=["T-001"])
        r = self.retry("t-001")
        self.assertEqual(r.returncode, 0)
        self.assertIn("autopilot isn't running; the next `windvane run` takes T-001 up anyway", r.stdout)
        self.assertEqual(sorted(f.name for f in self.p.state.iterdir()), ["state.json"])

    def test_status_names_each_failed_ticket_with_its_retry_once(self):
        self.hold_lock()
        self.state(failed=["T-001", "T-002", "T-001"])
        out = subprocess.run(["windvane", "status"], cwd=self.s.root, capture_output=True, text=True).stdout
        self.assertEqual(out.count("windvane retry T-001"), 1)
        self.assertIn("failed: T-001; once its cause is fixed: windvane retry T-001\n"
                      "failed: T-002; once its cause is fixed: windvane retry T-002", out)


class Restart(Base):
    def test_a_request_survives_a_restart_before_the_driver_reads_it(self):
        save_json(self.p.state / "restart.json", {"failed": ["T-001"], "skip": ["T-001"], "merged": []})
        (self.p.state / driver.RETRY).write_text("T-001\n")
        os.environ["WINDVANE_RESTARTED"] = "1"  # a self-update's exec: the run's memory comes from restart.json
        pilot = self.pilot()
        self.assertIn("T-001: back in the queue (windvane retry)", self.log())
        self.assertEqual((pilot.merged, pilot.failed), (["T-001", "T-002"], []))

    def test_without_the_request_the_restarted_run_keeps_it_failed(self):
        save_json(self.p.state / "restart.json", {"failed": ["T-001"], "skip": ["T-001"], "merged": []})
        os.environ["WINDVANE_RESTARTED"] = "1"
        pilot = self.pilot()
        self.assertEqual((pilot.merged, pilot.failed), (["T-002"], ["T-001"]))

    def test_a_fresh_run_drops_an_earlier_runs_request(self):
        (self.p.state / driver.RETRY).write_text("T-001\n")
        self.pilot()
        self.assertNotIn("windvane retry", self.log())
        self.assertFalse((self.p.state / driver.RETRY).exists())


if __name__ == "__main__":
    unittest.main()
