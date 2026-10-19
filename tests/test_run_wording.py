"""T-067: what the user reads after a run never contradicts itself: a deadline stop isn't "set aside", the max-turns
push fits one phone line, a critic that keeps skipping a check is named in `overview`, and a critic reply in another
order keeps its answers."""
import argparse
import io
import os
import sys
import unittest
from contextlib import redirect_stdout
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from windvane import cli, critic, driver  # noqa: E402
from windvane.config import HOME, Project  # noqa: E402
from windvane.util import append_jsonl, now  # noqa: E402
from helpers import Sandbox  # noqa: E402
from test_critic_scale import LADDER  # noqa: E402
from test_set_aside import Base  # noqa: E402


class DeadlineApart(Base):
    def test_a_deadline_stop_and_a_set_aside_ticket_get_their_own_lines_across_a_restart(self):
        pilot = self.pilot()
        pilot.deadline = now() + timedelta(minutes=2)  # inside the last 5 minutes: a time kill is the deadline's
        pilot.handle("T-001", "", "max turns")
        pilot.handle("T-002", "", "time")
        pilot.hand_over()
        again = self.pilot()
        again.carry_over(True)
        summary = again.summary()
        self.assertIn("\nStopped at the deadline (resumes next run): T-002.\nSet aside: T-001 (hit its max turns).\n",
                      summary)  # no queue read yet, so tickets go by id
        self.assertEqual(summary.count("T-002"), 1)
        self.assertEqual(again.skip, {"T-001"}, "a deadline stop isn't skipped: the next run resumes it")

    def test_no_deadline_line_when_nothing_was_stopped(self):
        self.assertNotIn("Stopped at the deadline", self.pilot().summary())


class SetAsidePushes(Base):
    tickets = ("T-001",)

    def test_each_limit_push_fits_a_lock_screen_and_names_the_limit_the_fix_and_the_branch(self):
        """A lock-screen preview shows about two lines: past 90 characters the branch, said last, is cut off."""
        cases = {"time": ("1.5-hour limit", "ticket_hours"), "max turns": ("max turns", "CLAUDE_CODE_MAX_TURNS"),
                 "dollar budget": ("dollar budget", "Split the ticket")}
        for killed, (limit, fix) in cases.items():
            with self.subTest(killed):
                self.pushes.clear()
                self.pilot(ticket_hours=1.5).handle("T-001", "", killed)
                [(title, msg)] = self.aside()
                self.assertLessEqual(len(msg), 90, msg)
                self.assertTrue(msg.startswith("Hit "), msg)
                for words in (limit, fix, "Branch kept."):
                    self.assertIn(words, msg)


def entry(i, missing=()):
    return {"ticket": f"T-{i:03}", "sha": "abc1234", "fixes": 0, "gist": "g", "score": 8, "step": "pass",
            "answers": {"shown": "yes"}, "unanswered": list(missing)}


class UnansweredLine(unittest.TestCase):
    def setUp(self):
        s = Sandbox()
        self.addCleanup(s.close)
        s.write(".windvane/config.toml", s.root.joinpath(".windvane/config.toml").read_text()
                .replace("enabled = false", "enabled = true"))
        s.write(".windvane/CRITIC.md", LADDER)
        s.publish("critic on, as ladders")
        self.s, self.p = s, Project(s.root)

    def overview(self, skipped):
        for i in range(12):  # the two oldest fall out of the last 10, so they never count
            append_jsonl(self.p.state / "critic.jsonl", entry(i, ["safe"] if i < 2 or i in skipped else []))
        append_jsonl(self.p.state / "critic.jsonl", {"ticket": "PLAN", "unanswered": ["safe"]})
        out = io.StringIO()
        with redirect_stdout(out):
            cli.cmd_overview(argparse.Namespace(), self.p)
        return out.getvalue()

    def test_three_of_ten_is_named(self):
        self.assertIn("Critic left safe unanswered in 3 of the last 10: check its wording in .windvane/CRITIC.md or "
                      f"windvane's {HOME / 'prompts/CRITIC.md'}.\n", self.overview({4, 7, 11}))

    def test_two_of_ten_is_not(self):
        self.assertNotIn("unanswered", self.overview({4, 11}))

    def test_a_check_counts_once_per_verdict(self):
        recs = [entry(1, ["safe", "safe"]), entry(2, ["safe"]), entry(3)]
        self.assertEqual(critic.unanswered(recs), [])
        self.assertEqual(critic.unanswered(recs + [entry(4, ["safe"])]), [("safe", 3, 4)])


class ReorderedReply(unittest.TestCase):
    def test_a_fix_list_before_the_checks_block_keeps_the_answers(self):
        text = ("Fix in this ticket:\n- the push is long\nChecks:\n- shown: yes, a transcript\n- safe: no, it crashes\n"
                "Fine on the whole.\nBeyond this ticket:\n- failures: yes, later\nCRITIC: T-001 fixes=1 long push")
        ans = critic.answers(text)
        self.assertEqual({k: v[0] for k, v in ans.items()}, {"shown": "yes", "safe": "no"})

    def test_a_verdict_starting_beyond_this_ticket_keeps_the_answers(self):
        text = ("Beyond this ticket's scope, a user gets a clear push.\nChecks:\n- shown: yes, a transcript\n"
                "- safe: yes, restarts\nFix in this ticket:\n- none\nCRITIC: T-001 fixes=0 clear")
        ans = critic.answers(text)
        self.assertEqual({k: v[0] for k, v in ans.items()}, {"shown": "yes", "safe": "yes"})

    def test_a_fix_list_before_the_checks_block_reaches_the_worker(self):
        s = Sandbox()
        self.addCleanup(s.close)
        s.write(".windvane/config.toml", s.root.joinpath(".windvane/config.toml").read_text()
                .replace("enabled = false", "enabled = true"))
        s.write(".windvane/CRITIC.md", "# Scale\n## works\n- 2 shown: shown.\n- 5 safe: no crash.\n")
        s.publish("critic on, as ladders")
        s.git("switch", "-q", "-c", "t-001-x")
        p = Project(s.root)
        notes = p.state / "critic" / "T-001" / "notes.md"
        notes.parent.mkdir(parents=True)
        notes.write_text("User-facing: yes\nfaster\n")
        os.environ["FAKE_CRITIC_TEXT"] = ("Fix in this ticket:\n- the push is two lines long\nChecks:\n"
                                          "- shown: yes, a transcript\n- safe: yes, restarts\nGood.\n"
                                          "CRITIC: {tid} fixes=1 long push")
        self.addCleanup(os.environ.pop, "FAKE_CRITIC_TEXT", None)
        entry, critique = critic.judge(p, "T-001", s.root)
        self.assertEqual((entry["dims"], entry["unanswered"]), ({"works": 5}, []))
        self.assertTrue(critique.startswith("Fix in this ticket:\n- the push is two lines long\nChecks:"), critique)

    def test_no_checks_line_keeps_the_old_rule(self):
        text = "- shown: yes, a transcript\nBeyond this ticket:\n- safe: yes\nCRITIC: T-001 fixes=0 ok"
        self.assertEqual(list(critic.answers(text)), ["shown"])


if __name__ == "__main__":
    unittest.main()
