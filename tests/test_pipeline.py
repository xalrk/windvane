"""The whole driver against a fake `claude`: worker -> check -> review -> merge, questions, and planning."""
import json
import os
import sys
import time
import unittest
from argparse import Namespace
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from windvane import critic, driver, jobs, runner  # noqa: E402
from windvane.config import Project  # noqa: E402
from windvane.queue import Queue  # noqa: E402
from windvane.util import now  # noqa: E402
from helpers import Sandbox  # noqa: E402


def args(**kw):
    a = dict(cap=75, week_cap=95, ticket_hours=1, min_slot=0, answer_wait=10, replan=0, goal="", parallel=1)
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
        calls = [json.loads(l) for l in f.read_text().splitlines()] if f.exists() else []
        return [c for c in calls if c["system"] != "span"]

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
        self.assertEqual(self.pushes, ["windvane started", "windvane stopped"])

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
        self.assertEqual(driver.changed_paths(self.p.plan_dir), [])  # the plan worktree is clean again

    def config(self, **kw):
        """Rewrite the top of the project's config (e.g. isolate, forge) and publish it."""
        f = self.s.root / ".windvane/config.toml"
        text = f.read_text().replace('forge = "git"\n', "".join(f"{k} = {v}\n" for k, v in kw.items()))
        f.write_text(text if "forge" in kw else 'forge = "git"\n' + text)
        self.s.publish("config")
        self.p = Project(self.s.root)

    def test_plan_leaves_the_checkout_alone_without_isolate(self):
        self.config(isolate="false")
        self.s.git("switch", "-q", "-c", "mine")
        self.s.write("README", "my edit\n")
        self.s.write("notes.txt", "untracked\n")
        self.assertEqual(driver.plan(self.p, "make it great", merge=True), 1)
        self.assertEqual(self.s.git("rev-parse", "--abbrev-ref", "HEAD"), "mine")
        self.assertEqual((self.s.root / "README").read_text(), "my edit\n")
        self.assertEqual((self.s.root / "notes.txt").read_text(), "untracked\n")
        self.assertEqual(self.s.git("status", "--porcelain"), "M README\n?? notes.txt")
        self.s.git("fetch", "-q", "origin")
        self.assertIn("T-003", Queue(self.p).files)

    def test_plan_skips_ids_claimed_by_an_open_plan(self):
        self.s.git("switch", "-q", "-c", "plan-20260101-0000")
        self.s.ticket("T-003")
        self.s.git("add", "-A")
        self.s.git("commit", "-qm", "plan")
        self.s.git("push", "-q", "origin", "HEAD")
        self.s.git("switch", "-q", "main")
        self.assertEqual(driver.plan(self.p, "make it great"), 1)
        prompt = [c["prompt"] for c in self.calls() if "planner" in c["system"]][0]
        self.assertIn("## Ids for new tickets, in order (at most 6)\nT-004, ", prompt)

    def test_plan_whose_merge_is_refused_counts_nothing(self):
        gh = self.s.bin / "gh"  # opens the PR, refuses to merge it
        gh.write_text('#!/bin/sh\ncase "$2" in\n  create) echo https://github.com/o/r/pull/7 ;;\n'
                      '  merge) echo "not mergeable" >&2; exit 1 ;;\n  view) echo OPEN ;;\nesac\n')
        gh.chmod(0o755)
        self.config(forge='"github"')
        pushes = self.pushes()
        self.assertEqual(driver.plan(self.p, "make it great", merge=True), 0)
        self.assertEqual(pushes, ["plan not merged"])
        self.s.git("fetch", "-q", "origin")
        self.assertNotIn("T-003", Queue(self.p).files)
        self.assertIn("plan-", self.s.git("ls-remote", "--heads", "origin", "plan-*"))  # kept for its PR
        self.assertIn("not merged", (self.p.state / "autopilot.log").read_text())

    def pushes(self):
        pushes, orig = [], driver.push
        driver.push = lambda title, *a, **k: pushes.append(title)
        self.addCleanup(setattr, driver, "push", orig)
        return pushes

    def test_plan_replays_onto_a_main_that_moved(self):
        os.environ["FAKE_PLAN_MOVE_MAIN"] = "1"
        self.assertEqual(driver.plan(self.p, "make it great", merge=True), 1)
        self.s.git("fetch", "-q", "origin")
        self.assertIn("T-003", Queue(self.p).files)
        self.assertIn("moved", self.s.git("log", "origin/main", "--format=%s"))

    def test_plan_that_cant_reach_main_is_kept_on_its_branch(self):
        hook = self.s.remote / "hooks" / "pre-receive"  # main refuses every push
        hook.write_text("#!/bin/sh\nwhile read old new ref; do [ \"$ref\" = refs/heads/main ] && exit 1; done\n"
                        "exit 0\n")
        hook.chmod(0o755)
        pushes = self.pushes()
        self.assertEqual(driver.plan(self.p, "make it great", merge=True), 0)
        self.assertEqual(pushes, ["plan not merged"])
        self.s.git("fetch", "-q", "origin")
        self.assertNotIn("T-003", Queue(self.p).files)
        branch = self.s.git("ls-remote", "--heads", "origin", "plan-*").split("refs/heads/")[1]
        self.assertIn(".windvane/tickets/T-003.md", self.s.git("ls-tree", "-r", "--name-only", f"origin/{branch}"))

    def test_plan_while_another_plan_runs_does_nothing(self):
        import fcntl
        self.p.state.mkdir(parents=True, exist_ok=True)
        with open(self.p.state / "plan.lock", "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            self.assertEqual(driver.plan(self.p, "make it great", merge=True), 0)
        self.assertEqual([c for c in self.calls() if "planner" in c["system"]], [])


class PhoneSync(unittest.TestCase):
    """What the phone's status/stop hooks and questions see when a run isn't the detached one that started them."""
    def setUp(self):
        self.s = Sandbox()
        self.s.ticket("T-001")
        self.s.publish()
        self.p = Project(self.s.root)

    def tearDown(self):
        self.s.close()

    def cli(self, fn, **kw):
        import contextlib
        import io
        from windvane import cli
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            getattr(cli, fn)(Namespace(**kw), self.p)
        return out.getvalue()

    def test_a_foreground_run_shows_as_running_and_stops_from_the_phone(self):
        os.environ["PATH"] = str(self.s.bin)  # no tmux: only the lock can tell
        self.assertIn("autopilot off", self.cli("cmd_status", line=True))
        self.assertIn("isn't running", self.cli("cmd_stop", if_running=True))
        self.assertFalse((self.p.state / "STOP").exists())
        pilot = driver.Pilot(self.p, args())
        pilot.take_lock()
        self.addCleanup(pilot.lockfile.close)
        self.assertIn("autopilot running", self.cli("cmd_status", line=True))
        self.assertIn("stops after the current ticket", self.cli("cmd_stop", if_running=True))
        self.assertTrue((self.p.state / "STOP").exists())

    def test_a_question_left_unsent_is_sent_by_the_next_run(self):
        record = self.s.tmp / "asks.txt"
        fake = self.s.bin / "notify-ask"
        fake.write_text(f"#!/bin/sh\necho \"$@\" >> {record}\n")
        fake.chmod(0o755)
        self.p.state.mkdir(parents=True, exist_ok=True)
        (self.p.state / "state.json").write_text(json.dumps({"questions": {"T-001": {
            "q": "Red or blue?", "opts": {"A": "red", "B": "blue"}, "rec": "A", "must_wait": False,
            "branch": "t-001-x", "asked": None}}}))  # asked in a quiet time; that run stopped before it ended
        pilot = driver.Pilot(self.p, args(answer_wait=60))
        orig = driver.time.sleep
        driver.time.sleep = lambda s: (self.p.state / "STOP").touch()  # the idle wait for an answer ends the run
        self.addCleanup(setattr, driver.time, "sleep", orig)
        pilot.run(now() + timedelta(hours=1))
        self.assertIn(f"{self.p.qid('T-001')} Sandbox: T-001: Red or blue?", record.read_text())
        self.assertEqual(record.read_text().count("Red or blue?"), 1)
        self.assertIsNotNone(json.loads((self.p.state / "state.json").read_text())["questions"]["T-001"]["asked"])


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


class Setup(unittest.TestCase):
    """The project's `setup` command runs in each fresh worktree; a failed one stops the run before any session."""

    def setUp(self):
        self.s = Sandbox()
        self.s.ticket("T-001", touches="a.py")
        self.s.ticket("T-002", touches="docs/")
        self.pushes = []
        orig, driver.push = driver.push, lambda title, msg, *a, **k: self.pushes.append((title, msg))
        self.addCleanup(setattr, driver, "push", orig)

    def tearDown(self):
        self.s.close()

    def run_pilot(self, setup, parallel=1):
        cfg = self.s.root / ".windvane/config.toml"
        cfg.write_text(f"setup = {json.dumps(setup)}\n" + cfg.read_text())
        self.s.publish("setup")
        p = Project(self.s.root)
        pilot = driver.Pilot(p, args(parallel=parallel))
        self.started = pilot.run(now() + timedelta(minutes=15))
        return p, pilot

    def calls(self):
        f = self.s.tmp / "calls.jsonl"
        return f.read_text().splitlines() if f.exists() else []

    def test_failed_setup_stops_the_run_before_any_session(self):
        p, pilot = self.run_pilot("echo installing; echo 'npm ERR! no lockfile' >&2; exit 3")
        self.assertEqual(self.calls(), [])  # not even the usage probe
        self.assertFalse(self.started)  # `windvane run` exits 1
        self.assertEqual(pilot.merged + pilot.failed, [])
        self.assertIn("setup `echo installing;", pilot.stop_reason)
        self.assertIn("(exit 3)", pilot.stop_reason)
        self.assertIn("npm ERR! no lockfile", pilot.stop_reason)
        self.assertFalse(p.work_dir.exists())
        self.assertNotIn(str(p.work_dir), self.s.git("worktree", "list"))
        self.assertEqual([t for t, _ in self.pushes], ["windvane couldn't start"])  # the one push says why
        self.assertTrue(self.pushes[0][1].startswith("Setup `echo installing;"))
        log = (p.state / "autopilot.log").read_text()
        self.assertIn("installing\nnpm ERR! no lockfile", log)  # the command's last lines
        cfg = self.s.root / ".windvane/config.toml"  # fixed: the next start makes the tree afresh
        cfg.write_text(cfg.read_text().replace("exit 3", "true"))
        self.s.publish("fix setup")
        again = driver.Pilot(Project(self.s.root), args())
        self.assertTrue(again.run(now() + timedelta(hours=1)))
        self.assertEqual(sorted(again.merged), ["T-001", "T-002"])

    def test_passing_setup_runs_in_each_worktree_and_the_run_goes_on(self):
        ran = self.s.tmp / "setup.log"
        p, pilot = self.run_pilot(f"pwd >> {ran}")
        self.assertEqual(sorted(pilot.merged), ["T-001", "T-002"])
        self.assertEqual(pilot.stop_reason, "queue empty")
        self.assertTrue(self.started)
        self.assertEqual(sorted(Path(l).name for l in ran.read_text().split()), ["ci", "work"])

    def test_failed_setup_in_a_later_slot_starts_nothing_more(self):
        import os
        os.environ["FAKE_WORK_S"] = "1"
        p, pilot = self.run_pilot('case "$PWD" in *-1) exit 4;; esac', parallel=2)
        self.assertEqual(pilot.merged, ["T-001"])  # slot 0's ticket still finishes
        self.assertIn("(exit 4) in work-1:", pilot.stop_reason)
        self.assertFalse((p.state / "work-1").exists())
        workers = [l for l in self.calls() if "windvane worker" in l]
        self.assertEqual(len(workers), 1)


if __name__ == "__main__":
    unittest.main()


class Parallel(unittest.TestCase):
    """Tickets whose Touches don't overlap run side by side, each in its own worktrees; others take turns."""

    def setUp(self):
        import os
        self.s = Sandbox()
        os.environ["FAKE_WORK_S"] = "1.5"

    def tearDown(self):
        self.s.close()

    def spans(self):
        f = self.s.tmp / "calls.jsonl"
        starts = [json.loads(l)["prompt"].split() for l in f.read_text().splitlines()
                  if json.loads(l)["system"] == "span"]
        return {t: float(at) for t, _, at in starts}

    def run_pilot(self):
        p = Project(self.s.root)
        pilot = driver.Pilot(p, args(parallel=2))
        pilot.run(now() + timedelta(minutes=15))  # 10 min a ticket until measured: 2 need the 2nd slot
        return pilot

    def test_disjoint_tickets_run_at_once_and_both_merge(self):
        self.s.ticket("T-001", touches="a.py")
        self.s.ticket("T-002", touches="docs/")
        self.s.publish()
        pilot = self.run_pilot()
        self.assertEqual(sorted(pilot.merged), ["T-001", "T-002"])
        at = self.spans()
        self.assertLess(abs(at["T-001"] - at["T-002"]), 1.0)  # started together, not one after the other
        self.assertTrue((self.s.tmp / "state/windvane/sandbox/work-1").exists())

    def test_overlapping_or_unknown_touches_take_turns(self):
        self.s.ticket("T-001", touches="windvane/")
        self.s.ticket("T-002", touches="windvane/driver.py")
        self.s.ticket("T-003")  # no Touches: runs alone
        self.s.publish()
        pilot = self.run_pilot()
        self.assertEqual(sorted(pilot.merged), ["T-001", "T-002", "T-003"])
        at = sorted(self.spans().values())
        self.assertTrue(all(b - a >= 1.4 for a, b in zip(at, at[1:])))

    def test_overlaps(self):
        self.assertTrue(driver.overlaps({"windvane/"}, {"windvane/driver.py"}))
        self.assertTrue(driver.overlaps({"a.py"}, {"a.py"}))
        self.assertFalse(driver.overlaps({"a.py"}, {"a.pyc", "docs"}))


class Sessions(unittest.TestCase):
    def setUp(self):
        self.s = Sandbox()
        self.s.write(".windvane/config.toml", self.s.root.joinpath(".windvane/config.toml").read_text()
                     .replace("enabled = false", "enabled = true"))
        self.s.publish("critic on")
        self.p = Project(self.s.root)

    def tearDown(self):
        self.s.close()

    def test_tool_calls_get_ten_minutes(self):
        runner.run_claude(self.p, "worker", "x", "hi", "", self.s.root)
        call = json.loads((self.s.tmp / "calls.jsonl").read_text().splitlines()[-1])
        self.assertEqual(call["tool_ms"], "600000")

    def test_a_killed_session_still_counts_its_tokens(self):
        marker = self.s.tmp / "streamed"
        os.environ["FAKE_HANG"] = str(marker)
        _, killed, _ = runner.run_claude(self.p, "worker", "T-001", "hi", "", self.s.root, ticket="T-001",
                                         stop_if=lambda: "time" if marker.exists() else None)
        self.assertEqual(killed, "time")
        row = json.loads((self.p.state / "ledger.jsonl").read_text().splitlines()[-1])
        self.assertEqual((row["input"], row["output"], row["cache_read"]), (30, 12, 2000))  # m1 counted once
        self.assertEqual(row["turns"], 2)
        self.assertTrue(row["estimated"])

    def test_a_driver_error_mid_session_kills_the_session(self):
        """A crash in the driver while it reads a session (once: two slots' usage saves collided) must not leave the
        session working unwatched in a slot the driver now thinks is free."""
        marker = self.s.tmp / "streamed"
        os.environ["FAKE_HANG"] = str(marker)

        def broken():
            if marker.exists():
                raise FileNotFoundError("usage.json.tmp")

        started = time.time()
        with self.assertRaises(FileNotFoundError):
            runner.run_claude(self.p, "worker", "T-001", "hi", "", self.s.root, ticket="T-001", stop_if=broken)
        pid = int(Path(str(marker) + ".pid").read_text())
        self.assertFalse(Path(f"/proc/{pid}").exists() and
                         not Path(f"/proc/{pid}/stat").read_text().split(") ")[-1].startswith("Z"))
        self.assertLess(time.time() - started, 30)

    def test_killing_a_worker_kills_the_critic_it_started(self):
        pack = critic.pack_dir(self.p, "T-001")
        pack.mkdir(parents=True)
        (pack / "notes.md").write_text("User-facing: yes\nfaster\n")
        pid_file = self.s.tmp / "critic.pid"
        os.environ["FAKE_CRITIC_PID"] = str(pid_file)
        started = time.time()
        _, killed, _ = runner.run_claude(
            self.p, "worker", "T-001", "Ticket T-001. Go.", "# windvane worker run", self.s.root,
            stop_if=lambda: "time" if pid_file.exists() else None, limits=(now() + timedelta(hours=1), 75, 95))
        self.assertEqual(killed, "time")
        pid = int(pid_file.read_text())
        for _ in range(100):
            stat = Path(f"/proc/{pid}/stat")
            if not stat.exists() or stat.read_text().split(") ")[-1].startswith("Z"):
                break
            time.sleep(0.05)
        else:
            self.fail("the critic outlived its worker")
        self.assertLess(time.time() - started, 30)
        self.assertEqual(list((self.p.state / "nested").glob("*/*")), [])

    def test_a_nested_session_keeps_the_outer_deadline_and_caps(self):
        u = runner.Usage(self.p)
        self.assertIsNone(runner.inherited_stop(self.p, u))
        os.environ.update(WINDVANE_STOP_AT=str(time.time() + 3600), WINDVANE_CAPS="75,95")
        stop_if = runner.inherited_stop(self.p, u)
        self.assertIsNone(stop_if())
        u.update({"unifiedWindows": {"five_hour": {"utilization": 0.8}}})  # usage.json, as any session writes it
        self.assertEqual(stop_if(), "5-hour cap")
        os.environ["WINDVANE_STOP_AT"] = str(time.time() - 1)
        self.assertEqual(runner.inherited_stop(self.p, u)(), "time")


class Heavy(unittest.TestCase):
    def test_runs_the_command_and_nests_without_deadlock(self):
        import os
        import subprocess
        import tempfile
        from windvane import heavy
        env_tmp = tempfile.mkdtemp()
        old = dict(os.environ)
        os.environ.update({"XDG_RUNTIME_DIR": env_tmp, "XDG_CONFIG_HOME": env_tmp})
        try:
            (Path(env_tmp) / "windvane").mkdir()
            (Path(env_tmp) / "windvane/machine.toml").write_text('busy = []\nmin_free_gb = 0\n')
            self.assertEqual(heavy.run(["sh", "-c", "exit 3"], wait_s=5), 3)
            inner = f"{sys.executable} -c 'import sys; sys.path.insert(0, \"{Path(__file__).resolve().parents[1]}\"); " \
                    "from windvane.heavy import run; sys.exit(run([\"true\"], wait_s=2))'"
            self.assertEqual(heavy.run(inner, wait_s=5), 0)  # a heavy tool calling another heavy tool
            with open(heavy.lock_path(), "w") as held:  # someone else holds the machine: give up after the wait
                import fcntl
                fcntl.flock(held, fcntl.LOCK_EX)
                out = subprocess.run([sys.executable, "-c", f"import sys; sys.path.insert(0, '{Path(__file__).resolve().parents[1]}'); "
                                      "from windvane.heavy import run; sys.exit(run(['true'], wait_s=1))"],
                                     capture_output=True, text=True)
                self.assertEqual(out.returncode, 75)
        finally:
            os.environ.clear()
            os.environ.update(old)
