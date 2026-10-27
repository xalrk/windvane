"""A worker run that ends PARTIAL with only the check left (the critic done on its pushed head, no failed check on it)
hands the head to the driver's check instead of counting as out of time, as does one that recorded a new critic
revision within `[critic] revisions`; any other PARTIAL counts as before."""
import os
import sys
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from windvane import critic, driver, jobs  # noqa: E402
from windvane.config import Project  # noqa: E402
from windvane.util import PARTIAL_LOG, append_jsonl, now, read_jsonl  # noqa: E402
from windvane.worktree import ensure_worktree  # noqa: E402
from helpers import Sandbox  # noqa: E402
from test_parallel import args  # noqa: E402

LEFT = "the check was still running at wrap-up"


class Base(unittest.TestCase):
    critic_on = True
    extra = ""

    def setUp(self):
        self.s = Sandbox(self.extra)
        if self.critic_on:
            cfg = self.s.root / ".windvane/config.toml"
            cfg.write_text(cfg.read_text().replace("enabled = false", "enabled = true"))
        self.s.ticket("T-001")
        self.s.publish()
        self.p = Project(self.s.root)
        pack = critic.pack_dir(self.p, "T-001")
        pack.mkdir(parents=True)
        (pack / "notes.md").write_text("User-facing: yes\nfaster\n")
        self.pushes = []
        orig, driver.push = driver.push, lambda title, msg, *a, **k: self.pushes.append((title, msg))
        self.addCleanup(setattr, driver, "push", orig)

    def tearDown(self):
        self.s.close()

    def log(self):
        f = self.p.state / "autopilot.log"
        return f.read_text() if f.exists() else ""

    def ledger(self, event="check"):
        return [r for r in read_jsonl(self.p.state / "ledger.jsonl") if r.get("event") == event]


class FullRun(Base):
    """The fake worker builds, runs `windvane critic` on its head, pushes and ends PARTIAL: only the check is left."""
    extra = "[autopilot]\ncheck_retries = 1\n"

    def setUp(self):
        super().setUp()
        os.environ.update(FAKE_WORKER="partial", FAKE_REASON=LEFT, FAKE_WORKER_RUN="windvane critic T-001")

    def run_pilot(self):
        pilot = driver.Pilot(self.p, args(parallel=1))
        pilot.run(now() + timedelta(minutes=40))
        return pilot

    def test_the_driver_checks_reviews_and_merges_it_in_the_same_run(self):
        pilot = self.run_pilot()
        self.assertEqual(pilot.merged, ["T-001"])
        self.assertEqual(pilot.partials, {})
        log = self.log()
        self.assertRegex(log, r"T-001: only the check was left; the driver checks [0-9a-f]{7}\n")
        self.assertNotIn(PARTIAL_LOG.format(tid="T-001"), log)
        self.assertIn("T-001: critic 9/10", log)  # the worker's verdict stands: no critic run of the driver's
        self.assertNotIn("critic run (the worker didn't finish it)", log)
        driver_checks = [r for r in self.ledger() if r["role"] == "driver"]
        self.assertTrue(driver_checks and all(r["passed"] and r.get("sha") for r in driver_checks), driver_checks)

    def test_a_red_driver_check_goes_back_to_the_worker_against_check_retries(self):
        cfg = self.s.root / ".windvane/config.toml"  # red only for the driver's gate, as a flaky suite can be
        cfg.write_text(cfg.read_text().replace('check = "test -f README"',
                                               'check = "test \\"$WINDVANE_CHECK_BY\\" != driver"'))
        self.s.publish("red for the driver")
        self.p = Project(self.s.root)
        pilot = self.run_pilot()
        self.assertEqual(pilot.merged, [])
        self.assertEqual(pilot.partials, {})
        log = self.log()
        self.assertEqual(log.count("T-001: only the check was left"), 2)  # its first run and the one retry
        self.assertIn("T-001: check on", log)
        self.assertIn("the check sent it back 2 times; `[autopilot] check_retries` = 1 allows 1", log)
        self.assertNotIn("ran out of time", log)


class HandleBase(Base):
    """`Pilot.handle` on a PARTIAL line, with the branch pushed (or not) and a verdict recorded by hand."""

    def setUp(self):
        super().setUp()
        ensure_worktree(self.p, self.p.work_dir, setup=False)
        self.wd = self.p.work_dir
        self.s.git("switch", "-q", "-c", "t-001-x", cwd=self.wd)
        self.head = self.commit("one")
        self.s.git("push", "-q", "-u", "origin", "HEAD", cwd=self.wd)
        self.pilot = driver.Pilot(self.p, args(parallel=1))
        self.pilot.queue = driver.Queue(self.p)
        self.pilot.sleep, self.pilot.deadline = (lambda *_: None), now() + timedelta(hours=2)
        self.after = []
        self.pilot.after = lambda *a: self.after.append(a)

    def commit(self, text):
        (self.wd / "work.txt").write_text(text + "\n")
        self.s.git("add", "-A", cwd=self.wd)
        self.s.git("commit", "-qm", text, cwd=self.wd)
        return self.s.git("rev-parse", "HEAD", cwd=self.wd)

    def verdict(self, sha, step="pass"):
        append_jsonl(self.p.state / "critic.jsonl", {"ticket": "T-001", "sha": sha[:7], "score": 9, "step": step})

    def handle(self):
        self.pilot.handle("T-001", f"RESULT: PARTIAL T-001 {LEFT}", None)

    def assert_partial(self):
        self.assertEqual(self.after, [])
        self.assertEqual(self.pilot.partials, {"T-001": 1})
        log = self.log()
        self.assertIn(f"{PARTIAL_LOG.format(tid='T-001')} (1): {LEFT}\n", log)  # the line as before
        self.assertNotIn("only the check was left", log)


class Handle(HandleBase):
    def test_a_pass_on_the_pushed_head_goes_on_as_a_ready(self):
        self.verdict(self.head)
        self.handle()
        self.assertEqual(self.after, [("T-001", "t-001-x", self.head)])
        self.assertEqual(self.pilot.partials, {})

    def test_a_passing_or_cut_off_worker_check_on_the_head_still_hands_it_over(self):
        self.verdict(self.head)
        append_jsonl(self.p.state / "ledger.jsonl", {"event": "check", "ticket": "T-001", "sha": self.head[:7],
                                                     "passed": False, "finished": False})
        self.handle()
        self.assertEqual(len(self.after), 1)

    def test_a_verdict_on_an_older_head_is_a_partial(self):
        self.verdict(self.head)
        self.commit("two")
        self.s.git("push", "-q", cwd=self.wd)
        self.handle()
        self.assert_partial()

    def test_a_revise_verdict_on_the_head_is_a_partial(self):
        self.verdict(self.head, "revise")
        self.handle()
        self.assert_partial()

    def test_an_unpushed_head_is_a_partial(self):
        self.verdict(self.commit("two"))
        self.handle()
        self.assert_partial()

    def test_a_dirty_tree_is_a_partial(self):
        self.verdict(self.head)
        (self.wd / "work.txt").write_text("half done\n")
        self.handle()
        self.assert_partial()

    def test_a_failed_worker_check_on_the_head_is_a_partial(self):
        self.verdict(self.head)
        cfg = self.p.cfg
        cfg["check"] = "false"
        ok, _, _ = jobs.check(self.p, self.wd, ticket="T-001", by="worker")
        self.assertFalse(ok)
        [row] = self.ledger()
        self.assertTrue(self.head.startswith(row["sha"]))
        self.handle()
        self.assert_partial()

    def test_a_failed_check_on_an_older_head_doesnt_count(self):
        self.verdict(self.head)
        append_jsonl(self.p.state / "ledger.jsonl", {"event": "check", "ticket": "T-001", "sha": "0000000",
                                                     "passed": False})
        self.handle()
        self.assertEqual(len(self.after), 1)


class Revisions(HandleBase):
    """A PARTIAL whose run recorded a new `revise` on a newer head, with `[critic] revisions` left, is the critic's
    loop going round: it isn't a strike toward `partials` (3 here, as is `revisions`)."""

    runs = 0

    def run_ends(self, step="revise", moved=True):
        """One run's end: a new head pushed (`moved`), a verdict on it (`step`), then `handle` with the run's start."""
        self.runs += 1
        before = (len(critic.history(self.p, "T-001")), self.s.git("rev-parse", "HEAD", cwd=self.wd))
        if moved:
            self.commit(f"run {self.runs}")
            self.s.git("push", "-q", cwd=self.wd)
        if step:
            self.verdict(self.s.git("rev-parse", "HEAD", cwd=self.wd), step)
        self.pilot.handle("T-001", f"RESULT: PARTIAL T-001 {LEFT}", None, ("t-001-x",), before)

    def test_revisions_arent_counted_until_spent(self):
        for _ in range(3):
            self.run_ends()
        self.assertEqual(self.pilot.partials, {})
        self.assertNotIn("T-001", self.pilot.aside)
        log = self.log()
        for n in (1, 2, 3):
            self.assertIn(f"{PARTIAL_LOG.format(tid='T-001')} (critic revision {n} of 3, not counted): {LEFT}\n", log)
        self.run_ends(step=None)  # revisions spent, no new verdict: counted as before
        self.assertEqual(self.pilot.partials, {"T-001": 1})
        self.assertIn(f"{PARTIAL_LOG.format(tid='T-001')} (1): {LEFT}\n", self.log())
        self.assertEqual(self.after, [])

    def test_runs_with_no_new_verdict_are_set_aside_as_before(self):
        self.verdict(self.head, "revise")  # an older verdict, from before these runs
        for _ in range(3):
            self.run_ends(step=None)
        self.assertEqual(self.pilot.partials, {"T-001": 3})
        self.assertEqual(self.pilot.aside["T-001"], "3 runs in a row ran out of time")
        self.assertIn("Set aside: ", self.pushes[-1][0])

    def test_a_revise_past_the_budget_or_on_the_head_it_began_on_counts(self):
        self.p.critic["revisions"] = 1
        self.run_ends()
        self.assertEqual(self.pilot.partials, {})
        self.run_ends()  # revision 2 of 1: the budget is spent
        self.assertEqual(self.pilot.partials, {"T-001": 1})
        self.p.critic["revisions"] = 5
        self.run_ends(moved=False)  # a verdict on the very head the run began on: no new work
        self.assertEqual(self.pilot.partials, {"T-001": 2})

    def test_critic_off_counts(self):
        self.p.critic["enabled"] = False
        self.run_ends()
        self.assertEqual(self.pilot.partials, {"T-001": 1})


class CriticOff(HandleBase):
    critic_on = False

    def test_critic_off_is_a_partial(self):
        self.verdict(self.head)
        self.handle()
        self.assert_partial()


class OpenPr(HandleBase):
    """On a GitHub project the cut-off run may not have opened its PR: the driver opens it, the merge goes through it."""

    def setUp(self):
        super().setUp()
        self.p.cfg["forge"] = "github"
        self.verdict(self.head)

    def test_a_missing_pr_is_opened(self):
        with mock.patch.object(driver, "pr_lookup", return_value=""), \
                mock.patch.object(driver.net, "sh", return_value="https://example.invalid/pull/7") as sh:
            self.handle()
        [call] = sh.call_args_list
        self.assertEqual(call.args[1:7], ("gh", "pr", "create", "--head", "t-001-x", "--base"))
        self.assertIn("T-001: do T-001", call.args)
        self.assertIn("T-001: opened https://example.invalid/pull/7", self.log())
        self.assertEqual(len(self.after), 1)

    def test_an_open_pr_or_a_failing_gh_opens_none(self):
        for found in ("7", None):
            with mock.patch.object(driver, "pr_lookup", return_value=found), \
                    mock.patch.object(driver.net, "sh") as sh:
                self.handle()
            sh.assert_not_called()
        self.assertEqual(len(self.after), 2)


if __name__ == "__main__":
    unittest.main()
