"""The whole driver against a fake `claude`: worker -> check -> review -> merge, questions, and planning."""
import json
import sys
import unittest
from argparse import Namespace
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from windvane import driver, jobs  # noqa: E402
from windvane.config import Project  # noqa: E402
from windvane.queue import Queue  # noqa: E402
from windvane.util import now  # noqa: E402
from helpers import Sandbox  # noqa: E402


def args(**kw):
    a = dict(cap=75, week_cap=95, ticket_hours=1, min_slot=0, answer_wait=10, replan=0, goal="")
    a.update(kw)
    return Namespace(**a)


class Pipeline(unittest.TestCase):
    def setUp(self):
        self.s = Sandbox()
        self.s.ticket("T-001")
        self.s.ticket("T-002", "T-001")
        self.s.publish()
        self.p = Project(self.s.root)

    def tearDown(self):
        self.s.close()

    def calls(self):
        f = self.s.tmp / "calls.jsonl"
        return [json.loads(l) for l in f.read_text().splitlines()] if f.exists() else []

    def test_tickets_merge_in_dependency_order_and_checkout_is_untouched(self):
        pilot = driver.Pilot(self.p, args())
        pilot.run(now() + timedelta(hours=1))
        self.assertEqual(pilot.merged, ["T-001", "T-002"])
        self.assertEqual(pilot.stop_reason, "queue empty")
        self.s.git("fetch", "-q", "origin")
        log = self.s.git("log", "origin/main", "--merges", "--format=%s")
        self.assertIn("Merge T-001: do T-001 (branch t-001-work)", log)
        self.assertEqual(self.s.git("ls-remote", "--heads", "origin", "t-001-*"), "")  # branch deleted
        self.assertEqual(self.s.git("branch", "--list", "t-001-*", cwd=self.p.work_dir), "")  # local one too
        self.assertFalse((self.s.root / "T-001.txt").exists())  # the user's checkout never moved
        self.assertEqual(self.s.git("rev-parse", "--abbrev-ref", "HEAD"), "main")
        roles = [c["system"].split(" run")[0] for c in self.calls() if c["system"]]  # not the usage probe
        self.assertEqual(roles, ["# windvane worker", "# windvane review"] * 2)
        ledger = (self.p.state / "ledger.jsonl").read_text().splitlines()
        self.assertEqual(len(ledger), 4)

    def test_merge_that_doesnt_land_keeps_the_branches(self):
        hook = self.s.remote / "hooks" / "post-receive"  # accepts the push to main, then puts main back
        hook.write_text("#!/bin/sh\nwhile read old new ref; do\n  [ \"$ref\" = refs/heads/main ] && "
                        "git update-ref refs/heads/main \"$old\"\ndone\nexit 0\n")
        hook.chmod(0o755)
        pilot = driver.Pilot(self.p, args())
        pilot.run(now() + timedelta(seconds=30))  # counted merged, T-001 would be redone until the deadline
        self.assertEqual(pilot.merged, [])
        self.assertIn("T-001", pilot.failed)
        self.s.git("fetch", "-q", "origin")
        head = self.s.git("rev-parse", "origin/t-001-work")  # the remote branch is still there
        self.assertNotIn("T-001", self.s.git("log", "origin/main", "--format=%s"))
        self.assertEqual(self.s.git("rev-parse", "t-001-work", cwd=self.p.work_dir), head)  # and the local one
        self.assertIn("is kept on branch t-001-work", (self.p.state / "autopilot.log").read_text())

    def test_review_changes_feed_the_next_worker_run(self):
        import os
        os.environ["FAKE_REVIEW"] = "CHANGES"
        pilot = driver.Pilot(self.p, args())
        pilot.run(now() + timedelta(hours=1))
        self.assertEqual(pilot.merged, [])
        self.assertIn("T-001", pilot.failed)
        worker_prompts = [c["prompt"] for c in self.calls() if "worker" in c["system"]]
        self.assertIn("The reviewer asks for changes", worker_prompts[1])
        self.assertIn("In progress on branch(es) t-001-work", worker_prompts[1])

    def test_question_times_out_to_the_recommendation(self):
        import os
        os.environ["FAKE_WORKER"] = "question"
        pilot = driver.Pilot(self.p, args(answer_wait=0))
        pilot.run(now() + timedelta(hours=1))
        self.assertEqual(pilot.merged, ["T-001", "T-002"])
        prompts = [c["prompt"] for c in self.calls() if "worker" in c["system"]]
        self.assertIn("didn't answer your question \"Red or blue?\"", prompts[1])
        self.assertIn("recommendation (A)", prompts[1])

    def test_desk_answer_is_built(self):
        import os
        from windvane import questions
        os.environ["FAKE_WORKER"] = "question"
        pilot = driver.Pilot(self.p, args(answer_wait=60))
        orig = pilot.poll_questions

        def answer_then_poll():
            for tid in pilot.questions:
                questions.give_answer(self.p, tid, "B")
            orig()
        pilot.poll_questions = answer_then_poll
        pilot.run(now() + timedelta(hours=1))
        prompts = [c["prompt"] for c in self.calls() if "worker" in c["system"]]
        self.assertIn("option B (blue)", prompts[1])
        self.assertEqual(pilot.merged, ["T-001", "T-002"])

    def pilot(self, fail):
        """A pilot whose fake sessions do `fail` (FAKE_FAIL) in turn, with an instant backoff and pushes recorded."""
        import os
        os.environ["FAKE_FAIL"] = fail
        pilot = driver.Pilot(self.p, args())
        self.waits, self.pushes = [], []
        pilot.sleep = self.waits.append
        orig, driver.push = driver.push, lambda title, *a, **k: self.pushes.append(title)
        self.addCleanup(setattr, driver, "push", orig)
        return pilot

    def roles(self):
        return [c["system"].split(" run")[0][11:] for c in self.calls() if c["system"]]

    def test_dead_sessions_are_retried_without_failing_the_ticket(self):
        pilot = self.pilot("error,crash")  # an API error, then a crash with no result event; then all is well
        pilot.run(now() + timedelta(hours=1))
        self.assertEqual(pilot.merged, ["T-001", "T-002"])
        self.assertEqual((pilot.failed, pilot.fail_streak), ([], 0))
        self.assertFalse([t for t in self.pushes if t.startswith("Failed")])
        self.assertEqual(self.waits, [60] * 9)  # 3 minutes, then 6
        self.assertEqual(self.roles(), ["worker"] * 3 + ["review", "worker", "review"])
        ends = [(r["end"], r["error"]) for r in map(json.loads, (self.p.state / "ledger.jsonl").read_text().splitlines())]
        self.assertEqual(ends[0], ("error", "API Error: 529 Overloaded"))
        self.assertEqual(ends[1], ("no result", "no result event (exit 1): Error: socket hang up"))
        self.assertEqual(ends[2], ("ok", None))

    def test_dead_reviewer_is_retried_in_place(self):
        pilot = self.pilot("ok,error")
        pilot.run(now() + timedelta(hours=1))
        self.assertEqual(pilot.merged, ["T-001", "T-002"])
        self.assertEqual(self.roles(), ["worker", "review", "review", "worker", "review"])

    def test_sessions_that_keep_dying_stop_the_run_without_failing_tickets(self):
        pilot = self.pilot("error*")
        pilot.run(now() + timedelta(hours=1))
        self.assertEqual(pilot.stop_reason, "4 sessions in a row died (API Error: 529 Overloaded)")
        self.assertEqual((pilot.merged, pilot.failed), ([], []))
        self.assertEqual(self.roles(), ["worker"] * 4)
        self.assertEqual(self.pushes, ["sandbox: autopilot started", "sandbox: autopilot stopped"])

    def test_no_retry_past_the_deadline(self):
        pilot = self.pilot("crash*")
        pilot.run(now() + timedelta(minutes=2))
        self.assertIn("no time left to retry it", pilot.stop_reason)
        self.assertEqual((self.roles(), self.waits, pilot.failed), (["worker"], [], []))

    def test_missing_result_line_gets_one_more_run(self):
        pilot = self.pilot("quiet")
        pilot.run(now() + timedelta(hours=1))
        self.assertEqual((pilot.merged, pilot.failed), (["T-001", "T-002"], []))
        prompts = [c["prompt"] for c in self.calls() if "worker" in c["system"]]
        self.assertIn("ended without the RESULT line for T-001. The worktree is on `t-001-work`", prompts[1])
        self.assertNotIn("without the RESULT line", prompts[2])  # T-002 starts clean

    def test_missing_result_line_twice_fails_the_ticket(self):
        pilot = self.pilot("quiet*")
        pilot.run(now() + timedelta(hours=1))
        self.assertEqual(pilot.failed, ["T-001"])
        self.assertEqual(self.roles(), ["worker"] * 2)

    def test_check_failure_goes_back_with_its_log(self):
        self.s.write(".windvane/config.toml", (self.s.root / ".windvane/config.toml").read_text()
                     .replace('check = "test -f README"', 'check = "echo boom; false"'))
        self.s.publish("red check")
        p = Project(self.s.root)
        pilot = driver.Pilot(p, args())
        pilot.run(now() + timedelta(hours=1))
        prompts = [c["prompt"] for c in self.calls() if "worker" in c["system"]]
        self.assertIn("The check fails", prompts[1])
        self.assertIn("boom", prompts[1])
        self.assertEqual(pilot.merged, [])

    def test_plan_writes_and_merges_tickets(self):
        n = driver.plan(self.p, "make it great", merge=True)
        self.assertEqual(n, 1)
        self.s.git("fetch", "-q", "origin")
        q = Queue(self.p)
        self.assertIn("T-003", q.files)
        self.assertEqual(q.order[0], "T-003")  # its QUEUE.md bullet was kept

    def test_plan_that_writes_outside_windvane_is_discarded(self):
        import os
        os.environ["FAKE_PLAN_OUTSIDE"] = "1"
        self.assertEqual(driver.plan(self.p, "make it great", merge=True), 0)
        self.s.git("fetch", "-q", "origin")
        self.assertNotIn("T-003", Queue(self.p).files)
        self.assertEqual(driver.changed_paths(self.p.ci_dir), [])  # the ci worktree is clean again


class CheckCache(unittest.TestCase):
    def setUp(self):
        self.s = Sandbox()
        self.p = Project(self.s.root)

    def tearDown(self):
        self.s.close()

    def test_pass_is_reused_for_the_same_clean_tree_only(self):
        ok, summary, _ = jobs.check(self.p, self.s.root)
        self.assertTrue(ok)
        self.assertNotIn("reused", summary)
        self.assertIn("reused", jobs.check(self.p, self.s.root)[1])
        self.s.write("new", "x")  # dirty: runs, and isn't recorded
        ok, summary, _ = jobs.check(self.p, self.s.root)
        self.assertIn("not recorded", summary)


if __name__ == "__main__":
    unittest.main()
