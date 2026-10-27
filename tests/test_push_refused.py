"""A worker whose push the pre-push scan refused: the hook notes the refusal where the driver reads it, a READY whose
branch isn't on the remote goes back at once with the reason (no check is paid for), the next prompt resumes the
local branch instead of starting the ticket afresh, and the same refusal twice sets the ticket aside.

The leak is built from pieces at run time, so this file itself stays clean for the working-tree scan."""
import json
import os
import subprocess
import sys
import unittest
from argparse import Namespace
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from windvane import driver  # noqa: E402
from windvane.config import Project  # noqa: E402
from windvane.queue import Queue  # noqa: E402
from windvane.util import now  # noqa: E402
from windvane.worktree import REFUSALS, ensure_worktree, guard_pushes, last_refusal  # noqa: E402
from helpers import Sandbox  # noqa: E402

TOKEN = "ghp_" + "Wq4" * 12


class Base(unittest.TestCase):
    def setUp(self):
        self.s = Sandbox()
        self.addCleanup(self.s.close)
        self.checks = self.s.tmp / "checks.log"  # every check run adds a line
        cfg = self.s.root / ".windvane/config.toml"
        cfg.write_text(cfg.read_text().replace('check = "test -f README"',
                                               f'check = "echo ran >> {self.checks} && test -f README"'))
        self.s.ticket("T-001")
        self.s.publish()
        self.p = Project(self.s.root)
        self.pushes = []
        self.addCleanup(setattr, driver, "push", driver.push)
        driver.push = lambda title, msg, *a, **k: self.pushes.append((title, msg))

    def pilot(self):
        return driver.Pilot(self.p, Namespace(cap=75, week_cap=95, ticket_hours=1, min_slot=0, answer_wait=10,
                                              replan=0, goal="", parallel=1))

    def log(self):
        return (self.p.state / "autopilot.log").read_text()

    def prompts(self):
        calls = [json.loads(line) for line in Path(os.environ["FAKE_LOG"]).read_text().splitlines()]
        return [c["prompt"] for c in calls if c["system"].startswith("# windvane worker run")]


class Refused(Base):
    def test_a_refused_push_goes_back_without_a_check_and_the_same_refusal_twice_sets_it_aside(self):
        os.environ["FAKE_LEAK"] = f"settings.py=KEY = '{TOKEN}'"
        pilot = self.pilot()
        pilot.run(now() + timedelta(hours=1))
        log = self.log()
        self.assertFalse(self.checks.exists())  # no check was paid for on a branch that never left the machine
        self.assertRegex(log, r"T-001: READY, but its push to t-001-work was refused \(it would make public a token "
                              r"at settings\.py:1 in \w{7}\); back to the worker")
        first, second = self.prompts()
        self.assertIn("A new ticket", first)
        self.assertNotIn("A new ticket", second)
        self.assertRegex(second, r"Your branch `t-001-work` isn't on origin: its push was refused \(it would make "
                                 r"public a token at settings\.py:1 in \w{7}\)\. Fix that and push again, then finish "
                                 r"READY\.")
        self.assertIn("In progress on branch `t-001-work`, which isn't on origin yet. Resume it, then push it "
                      "(`git push -u origin t-001-work`; without `-u` if git can't lock its config).", second)
        self.assertEqual(second.count("it would make public"), 1)  # the feedback says it: once is enough
        self.assertNotIn(TOKEN, second)
        self.assertIn("T-001", pilot.skip)
        self.assertIn("T-001: set aside: its push was refused twice in a row (it would make public a token", log)
        aside = [(t, m) for t, m in self.pushes if t.startswith("Set aside")]
        self.assertEqual(len(aside), 1, self.pushes)
        title, msg = aside[0]
        self.assertEqual(title, "Set aside: do T-001 (T-001)")
        self.assertIn("Its push was refused twice: it would make public a token at settings.py:1 in ", msg)
        self.assertIn("In your checkout, `git switch t-001-work` and take it out of the commits that add it (`git "
                      "commit --amend` for the last one, or `git reset --soft ", msg)
        self.assertIn("then `git push -u origin t-001-work`. `windvane retry T-001` sends it back. Its branch is only "
                      "on this machine.", msg)
        self.assertNotIn(TOKEN, msg + log)
        self.assertIn("git reset --soft", pilot.fixes["T-001"])  # `windvane queue` names the fix too

    def test_the_hook_notes_kinds_and_places_only_and_a_clean_push_clears_it(self):
        wt = self.s.tmp / "wt"
        ensure_worktree(self.p, wt, setup=False)
        env = {**os.environ, **guard_pushes(self.p, wt)}
        git = lambda *a: subprocess.run(["git", *a], cwd=wt, capture_output=True, text=True, env=env)  # noqa: E731
        git("switch", "-q", "-c", "t-001-work")
        (wt / "settings.py").write_text(f"KEY = '{TOKEN}'\n")
        git("add", "-A")
        git("commit", "-qm", "work")
        base = git("rev-parse", "origin/main").stdout.strip()
        self.assertNotEqual(git("push", "-q", "-u", "origin", "HEAD").returncode, 0)
        r = last_refusal(self.p, lambda b: b == "t-001-work")
        self.assertEqual((r["branch"], r["base"]), ("t-001-work", base[:12]))
        self.assertTrue(r["line"].startswith("windvane: push refused: it would make public a token at settings.py:1"))
        self.assertNotIn(TOKEN, (self.p.state / REFUSALS / "wt.json").read_text())
        git("rm", "-q", "settings.py")
        (wt / "notes.txt").write_text("no secret\n")
        git("add", "-A")
        git("commit", "-q", "--amend", "-m", "work")
        self.assertEqual(git("push", "-q", "-u", "origin", "HEAD").returncode, 0)
        self.assertIsNone(last_refusal(self.p, lambda b: True))


class NeverPushed(Base):
    def ready(self, setup, branches=()):
        """T-001's worker finishing READY in slot 0's tree as `setup` left it, through the real `after`."""
        ensure_worktree(self.p, self.p.work_dir, setup=False)
        setup(self.p.work_dir)
        pilot = self.pilot()
        pilot.queue = Queue(self.p)
        sha = self.s.git("rev-parse", "HEAD", cwd=self.p.work_dir)
        pilot.handle("T-001", f"RESULT: READY T-001 {sha}", None, list(branches))
        return pilot

    def local(self, wd):
        self.s.git("switch", "-q", "-c", "t-001-work", "origin/main", cwd=wd)
        self.s.git("commit", "-q", "--allow-empty", "-m", "T-001: build", cwd=wd)

    def test_a_branch_never_pushed_is_told_to_push_without_a_check(self):
        pilot = self.ready(self.local)
        self.assertEqual(pilot.feedback["T-001"], "Your branch `t-001-work` isn't on origin: push it "
                                                  "(`git push -u origin t-001-work`; without `-u` if git can't lock its config), then finish READY.")
        self.assertFalse(self.checks.exists())
        self.assertEqual(pilot.bounces["T-001"]["push"], 1)
        self.assertNotIn("T-001", pilot.skip)

    def test_a_detached_ready_after_a_refusal_names_it(self):
        def refused(wd):
            self.local(wd)
            (self.p.state / REFUSALS).mkdir(parents=True)
            (self.p.state / REFUSALS / "work-1.json").write_text(json.dumps(
                {"at": "2026-10-24T10:00:00", "branch": "t-001-work", "base": "abc",
                 "line": "windvane: push refused: it would make public a token at a.py:1 in 1a2b3c4."}))
            self.s.git("switch", "-q", "--detach", cwd=wd)
        pilot = self.ready(refused)
        self.assertTrue(pilot.feedback["T-001"].endswith(
            "then finish READY. Its last push was refused (it would make public a token at a.py:1 in 1a2b3c4): fix "
            "that before you push."), pilot.feedback["T-001"])
        self.assertIn("T-001: READY on no branch (its push was refused: it would make public a token at a.py:1 in "
                      "1a2b3c4); back to the worker", self.log())

    def test_an_amend_that_keeps_the_finding_is_the_same_refusal_after_a_killed_driver_too(self):
        ensure_worktree(self.p, self.p.work_dir, setup=False)
        self.local(self.p.work_dir)
        line = "windvane: push refused: it would make public a token at a.py:1 in {}."
        for sha in ("1a2b3c4", "5d6e7f8"):  # the amend's new commit; each READY seen by a driver of its own
            (self.p.state / REFUSALS).mkdir(parents=True, exist_ok=True)
            (self.p.state / REFUSALS / "work-1.json").write_text(json.dumps(
                {"at": "2026-10-24T10:00:00", "branch": "t-001-work", "base": "abc", "line": line.format(sha)}))
            pilot = self.pilot()  # state.json only: no restart hand-over from a killed one
            pilot.not_pushed("T-001", "t-001-work", "0" * 40)
            pilot.save()  # as `work` does after each worker run
        self.assertIn("T-001", pilot.skip)

    def test_a_retry_after_a_fix_by_hand_forgets_the_refusal(self):
        ensure_worktree(self.p, self.p.work_dir, setup=False)
        self.local(self.p.work_dir)
        (self.p.state / REFUSALS).mkdir(parents=True)
        (self.p.state / REFUSALS / "work-1.json").write_text(json.dumps(
            {"at": "2026-10-24T10:00:00", "branch": "t-001-work", "base": "abc",
             "line": "windvane: push refused: it would make public a token at a.py:1 in 1a2b3c4."}))
        pilot = self.pilot()
        pilot.skip, pilot.aside = {"T-001"}, {"T-001": "its push was refused twice in a row"}
        (self.p.state / driver.RETRY).write_text("T-001\n")  # `windvane retry T-001`, after the user's own push
        pilot.take_retries()
        pilot.not_pushed("T-001", "t-001-work", "0" * 40)
        self.assertEqual(pilot.feedback["T-001"], "Your branch `t-001-work` isn't on origin: push it "
                                                  "(`git push -u origin t-001-work`; without `-u` if git can't lock its config), then finish READY.")

    def test_a_pushed_branch_isnt_shadowed_by_an_older_local_attempt(self):
        ensure_worktree(self.p, self.p.work_dir, setup=False)
        wd = self.p.work_dir
        self.s.git("switch", "-q", "-c", "t-001-old", "origin/main", cwd=wd)
        self.s.git("commit", "-q", "--allow-empty", "-m", "T-001: an older try", cwd=wd)
        self.s.git("switch", "-q", "-c", "t-001-work", "origin/main", cwd=wd)
        self.s.git("commit", "-q", "--allow-empty", "-m", "T-001: build", cwd=wd)
        self.s.git("push", "-q", "-u", "origin", "HEAD", cwd=wd)
        self.s.git("switch", "-q", "--detach", cwd=wd)
        self.assertEqual(self.pilot().unpushed("T-001", ["t-001-work"], self.s.root), ("", None))
        self.assertEqual(self.pilot().unpushed("T-001", [], self.s.root)[0], "t-001-old")

    def test_the_next_worker_resumes_a_local_branch_and_pushes_it(self):
        ensure_worktree(self.p, self.p.work_dir, setup=False)
        self.local(self.p.work_dir)
        self.s.git("switch", "-q", "--detach", cwd=self.p.work_dir)
        pilot = self.pilot()
        pilot.run(now() + timedelta(hours=1))
        prompt = self.prompts()[0]
        self.assertIn("In progress on branch `t-001-work`, which isn't on origin yet. Resume it, then push it "
                      "(`git push -u origin t-001-work`; without `-u` if git can't lock its config).", prompt)
        self.assertNotIn("A new ticket", prompt)
        self.assertEqual(pilot.merged, ["T-001"])


if __name__ == "__main__":
    unittest.main()
