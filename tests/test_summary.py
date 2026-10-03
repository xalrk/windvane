"""T-035: the run's last push lists what still needs the user, a critique that sends work back lands on the PR, a
broken quiet file says so once, and a project can add its own lines to `overview`."""
import argparse
import io
import os
import sys
import unittest
from argparse import Namespace
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from windvane import cli, driver  # noqa: E402
from windvane.config import Project  # noqa: E402
from windvane.util import append_jsonl  # noqa: E402
from helpers import Sandbox  # noqa: E402

PROVISIONAL = ("# Q\n\n## Open\n\n## Provisional\n\n### T-002: Which colour?\nctx\n- **A**: red\n- **B**: blue\n"
               "Recommendation: A, because it's louder\nBuilt: A; to change it: COLOUR in theme.py\n\n## Answered\n")
# Records every call; `pr list` finds PR 7 unless NO_PR is set.
FAKE_GH = ('#!/bin/sh\necho "$@" >> "$GH_LOG"\n'
           'if [ "$2" = list ] && [ -z "$NO_PR" ]; then echo 7; fi\n'
           'if [ "$2" = comment ] && [ -n "$FAIL_COMMENT" ]; then exit 1; fi\n')


class Base(unittest.TestCase):
    top = ""  # top-level config lines
    critic = False

    def setUp(self):
        self.s = Sandbox()
        self.addCleanup(self.s.close)
        cfg = self.s.root.joinpath(".windvane/config.toml").read_text()
        cfg = cfg.replace('forge = "git"\n', 'forge = "git"\n' + self.top)
        if self.critic:
            cfg = cfg.replace("enabled = false", "enabled = true").replace('forge = "git"', 'forge = "github"')
        if self.top or self.critic:
            self.s.write(".windvane/config.toml", cfg)
            self.s.publish("config")
        self.p = Project(self.s.root)
        self.pushes = []
        orig, driver.push = driver.push, lambda title, msg, *a, **k: self.pushes.append((title, msg))
        self.addCleanup(setattr, driver, "push", orig)

    def pilot(self):
        return driver.Pilot(self.p, Namespace(cap=75, week_cap=95, ticket_hours=1, min_slot=0, answer_wait=10,
                                              replan=0, goal="", parallel=1))

    def verdict(self, tid, step, score=8):
        append_jsonl(self.p.state / "critic.jsonl", {"ticket": tid, "sha": "abc1234", "score": score, "fixes": 1,
                                                     "step": step, "gist": "g", "critique": f"Fix in this ticket:\n- {tid} x"})


class Summary(Base):
    def test_both_lists_appear_when_they_apply(self):
        self.s.write(".windvane/QUESTIONS.md", PROVISIONAL)
        self.s.publish("a provisional answer")
        self.verdict("T-001", "revise", 6)
        self.verdict("T-001", "accept", 7)
        self.verdict("T-002", "pass", 9)
        pilot = self.pilot()
        pilot.merged = ["T-001", "T-002"]
        text = pilot.summary()
        self.assertIn("Merged under the critic's bar (out of revisions): T-001.\n", text)
        self.assertIn("Provisional answers to check: T-002 Which colour? (built A: red).", text)

    def test_neither_list_without_cause(self):
        self.verdict("T-001", "accept", 7)  # accepted but never merged: nothing to read on main
        self.verdict("T-002", "pass", 9)
        pilot = self.pilot()
        pilot.merged = ["T-002"]
        text = pilot.summary()
        self.assertNotIn("under the critic's bar", text)
        self.assertNotIn("Provisional", text)
        self.assertIn("Merged: T-002.", text)


class Quiet(Base):
    def test_a_malformed_quiet_file_pushes_once(self):
        self.p.state.mkdir(parents=True, exist_ok=True)
        (self.p.state / "quiet.txt").write_text("weekends\n")
        pilot = self.pilot()
        with redirect_stdout(io.StringIO()):
            for _ in range(3):
                pilot.quiet_check()
        self.assertEqual(len(self.pushes), 1)
        title, msg = self.pushes[0]
        self.assertEqual(title, "sandbox: quiet times ignored")
        self.assertIn("'weekends'", msg)
        with redirect_stdout(io.StringIO()):  # fixed, then broken again: a new push
            (self.p.state / "quiet.txt").write_text("")
            pilot.quiet_check()
            (self.p.state / "quiet.txt").write_text("weekends\n")
            pilot.quiet_check()
        self.assertEqual(len(self.pushes), 2)

    def test_an_unreadable_quiet_file_pushes_too(self):
        (self.p.state / "quiet.txt").mkdir(parents=True)  # read_text raises IsADirectoryError
        pilot = self.pilot()
        with redirect_stdout(io.StringIO()):
            pilot.quiet_check()
            pilot.quiet_check()
        self.assertEqual([t for t, _ in self.pushes], ["sandbox: quiet times ignored"])
        self.assertIn("can't read", self.pushes[0][1])

    def test_a_good_quiet_file_pushes_nothing(self):
        self.p.state.mkdir(parents=True, exist_ok=True)
        (self.p.state / "quiet.txt").write_text("2001-01-01 01:00-02:00\n")
        self.pilot().quiet_check()
        self.assertEqual(self.pushes, [])


class CritiqueOnPR(Base):
    """A revise from the driver's own critic run, or from the worker's `windvane critic`, lands on the ticket's PR."""
    critic = True

    def setUp(self):
        super().setUp()
        gh = self.s.bin / "gh"
        gh.write_text(FAKE_GH)
        gh.chmod(0o755)
        self.log = self.s.tmp / "gh.log"
        os.environ.update(GH_LOG=str(self.log), FAKE_CRITIC="6 2")
        self.s.git("switch", "-q", "-c", "t-001-x")
        notes = self.p.state / "critic" / "T-001" / "notes.md"
        notes.parent.mkdir(parents=True)
        notes.write_text("User-facing: yes\nfaster\n")

    def comments(self):
        return [l for l in (self.log.read_text().splitlines() if self.log.exists() else []) if l.startswith("pr comment")]

    def gate(self):
        pilot = self.pilot()
        pilot.tl.wd, pilot.deadline = self.s.root, driver.now() + driver.timedelta(hours=1)
        with redirect_stdout(io.StringIO()):
            return pilot.critic_gate("T-001", self.s.git("rev-parse", "HEAD"), branch="t-001-x")

    def test_a_revise_posts_the_critique(self):
        self.assertFalse(self.gate())
        sha = self.s.git("rev-parse", "--short", "HEAD")
        self.assertEqual(self.comments(), [f"pr comment 7 --body **windvane critic: 6/10, revise** at {sha}"])
        self.assertIn("Score: 6/10\nGood.\nFix in this ticket:\n- T-001 x\nBeyond", self.log.read_text())

    def test_the_workers_own_revise_too(self):
        out = io.StringIO()
        cwd = os.getcwd()
        os.chdir(self.s.root)
        self.addCleanup(os.chdir, cwd)
        with redirect_stdout(out):
            cli.cmd_critic(argparse.Namespace(ticket="T-001"), self.p)
        self.assertIn("CRITIC STEP: revise", out.getvalue())
        self.assertEqual(len(self.comments()), 1)
        self.assertIn("pr list --head t-001-x", self.log.read_text())

    def test_a_failed_comment_is_logged(self):
        os.environ["FAIL_COMMENT"] = "1"
        logged = []
        orig, driver.Pilot.log = driver.Pilot.log, lambda pilot, msg: logged.append(msg)
        self.addCleanup(setattr, driver.Pilot, "log", orig)
        self.assertFalse(self.gate())
        self.assertIn("T-001: couldn't post the critique on PR #7", logged)

    def test_a_pass_posts_nothing(self):
        os.environ["FAKE_CRITIC"] = "9 0"
        self.assertTrue(self.gate())
        self.assertEqual(self.comments(), [])

    def test_no_pr_no_comment(self):
        os.environ["NO_PR"] = "1"
        self.assertFalse(self.gate())
        self.assertEqual(self.comments(), [])


class Overview(Base):
    top = "overview_extra = \"printf 'Milestone 1: done\\\\nMilestone 2: underway\\\\n'\"\n"

    def overview(self):
        out = io.StringIO()
        with redirect_stdout(out):
            cli.cmd_overview(argparse.Namespace(), self.p)
        return out.getvalue()

    def test_its_output_follows_the_overview(self):
        out = self.overview().splitlines()
        self.assertEqual(out[-2:], ["Milestone 1: done", "Milestone 2: underway"])
        self.assertTrue(out[0].startswith("sandbox: "))

    def test_a_failing_command_is_one_line(self):
        self.p.cfg["overview_extra"] = "echo noise; echo first >&2; echo boom >&2; exit 3"
        out = self.overview().splitlines()
        self.assertEqual(out[-1], "overview_extra failed (exit 3): boom")
        self.assertNotIn("noise", out)

    def test_bounded_in_lines_and_time(self):
        self.p.cfg["overview_extra"] = "seq 40"
        self.assertEqual(cli.overview_extra(self.p), [str(i) for i in range(1, 16)])
        self.p.cfg["overview_extra"] = "sleep 5"
        self.assertEqual(cli.overview_extra(self.p, limit=0.5), ["overview_extra: no answer in 0.5 s"])

    def test_off_by_default(self):
        self.p.cfg["overview_extra"] = ""
        self.assertEqual(cli.overview_extra(self.p), [])


if __name__ == "__main__":
    unittest.main()
