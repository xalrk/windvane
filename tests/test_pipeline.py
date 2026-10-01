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
        self.assertFalse((self.s.root / "T-001.txt").exists())  # the user's checkout never moved
        self.assertEqual(self.s.git("rev-parse", "--abbrev-ref", "HEAD"), "main")
        roles = [c["system"].split(" run")[0] for c in self.calls() if c["system"]]  # not the usage probe
        self.assertEqual(roles, ["# windvane worker", "# windvane review"] * 2)
        ledger = (self.p.state / "ledger.jsonl").read_text().splitlines()
        self.assertEqual(len(ledger), 4)

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
        self.assertIn("T-003", Queue(self.p).files)


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
