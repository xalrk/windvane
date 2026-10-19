"""A check or dimension id never ends the critic's Checks block, whatever word it is (T-205): a bulleted answer
(`- <id>: yes|no|-`) is always an answer, while a heading written as one still ends or opens the block. A scale the
driver warns about pushes once per content, and `stats --critic` counts unanswered checks per id."""
import argparse
import contextlib
import io
import sys
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
from windvane import critic, driver, stats  # noqa: E402
from windvane.config import Project  # noqa: E402
from windvane.util import append_jsonl, now  # noqa: E402

from helpers import Sandbox  # noqa: E402
from test_pipeline import args  # noqa: E402

SCALE = """# Scale
## works
- 2 checks: does it run?
- 3 beyond: does it do more?
- 4 fix: is the bug gone?
- 5 critic: does the critic like it?
- 8 safe: no lost work?
- 10 fast: under a second?
"""
WORDS = ("checks", "beyond", "fix", "critic")
DUP = "## works\n- 5 safe: safe?\n- 8 safe: still safe?\n"


class ReplyIds(unittest.TestCase):
    def read(self, reply):
        lad = critic.ladder(SCALE)
        ans = critic.answers(reply, lad)
        return ans, critic.climb(lad, ans, 8)[4]

    def test_an_answer_to_a_heading_word_reads_every_answer_after_it(self):
        for bullet in ("-", "*", "1."):
            for word in WORDS:
                rest = [w for w in WORDS if w != word] + ["safe", "fast"]
                lines = [f"{bullet} {word}: no, a clause"] + [f"{bullet} {w}: yes, ok" for w in rest]
                for heading in ("Checks:\n", ""):  # with the block's heading, and with none
                    with self.subTest(bullet=bullet, word=word, heading=heading):
                        reply = (heading + "\n".join(lines) + "\nFine.\nFix in this ticket:\n1. nothing\n"
                                 f"Beyond this ticket:\n{bullet} {word}: yes, a follow-up\nCRITIC: T-001 fixes=0 ok")
                        ans, missing = self.read(reply)
                        self.assertEqual(missing, [])
                        self.assertEqual(ans[word][0], "no", "the list after the block answers nothing")
                        self.assertTrue(all(ans[w][0] == "yes" for w in rest), ans)

    def test_a_bulleted_checks_answer_after_others_keeps_them(self):
        reply = "* safe: yes, ok\n* checks: yes, ok\n* fast: no, slow\nCRITIC: T-001 fixes=0 ok"
        ans, _ = self.read(reply)
        self.assertEqual((ans["safe"][0], ans["checks"][0], ans["fast"][0]), ("yes", "yes", "no"))

    def test_each_real_list_heading_still_ends_the_block(self):
        for heading in ("Beyond this ticket:", "**Beyond this ticket:**", "## Beyond this ticket", "Beyond:",
                        "**Beyond:**", "- Beyond:", "Fix in this ticket:", "**Fix in this ticket:**",
                        "## Fix in this ticket", "- Fix in this ticket:"):
            with self.subTest(heading=heading):
                ans, missing = self.read(f"Checks:\n- fast: yes, quick\n{heading}\n- safe: yes, later\n"
                                         "CRITIC: T-001 fixes=0 ok")
                self.assertEqual(ans["fast"][0], "yes")
                self.assertIn("safe", missing, "an answer under a list heading is a follow-up")

    def test_each_real_checks_heading_still_opens_the_block(self):
        for heading in ("Checks:", "**Checks:**", "## Checks:", "* Checks:", "_Checks_:"):
            with self.subTest(heading=heading):
                ans, _ = self.read(f"The pack shows - safe: no, at first glance\n- safe: no, a preamble\n{heading}\n"
                                   "- safe: yes, kept\nCRITIC: T-001 fixes=0 ok")
                self.assertEqual(ans["safe"], ("yes", "kept"))


class ScalePush(unittest.TestCase):
    def setUp(self):
        self.s = Sandbox()
        self.addCleanup(self.s.close)
        self.p = Project(self.s.root)
        self.pushes, self.sent = [], True
        fake = lambda t, m="", *a, **k: self.pushes.append((t, m)) or self.sent  # noqa: E731
        patch = mock.patch.object(driver, "push", fake)
        patch.start()
        self.addCleanup(patch.stop)

    def run_once(self, scale):
        self.s.write(".windvane/CRITIC.md", scale)
        with contextlib.redirect_stdout(io.StringIO()):
            driver.Pilot(self.p, args()).run(now() + timedelta(minutes=1))
        return [m for t, m in self.pushes if t == "Critic scale"]

    def test_a_scale_warning_is_pushed_once_per_scale_content(self):
        got = self.run_once(DUP)
        self.assertEqual(got, ["Rename one in .windvane/CRITIC.md: dimension works has 2 checks named `safe`, so one "
                               "answer scores them all."])
        self.assertEqual(len(self.run_once(DUP)), 1, "the same scale again: logged, not pushed")
        self.assertEqual((self.p.state / "autopilot.log").read_text().count("checks named `safe`"), 2)
        self.assertEqual(len(self.run_once(DUP + "## cost\n- 5 cheap: cheap?\n")), 2, "a changed scale: once more")

    def test_a_push_that_did_not_go_out_is_tried_again_next_run(self):
        self.sent = False
        self.assertEqual(len(self.run_once(DUP)), 1)
        self.sent = True
        self.assertEqual(len(self.run_once(DUP)), 2)

    def test_a_clean_scale_pushes_nothing(self):
        self.assertEqual(self.run_once(SCALE), [])


class UnansweredStats(unittest.TestCase):
    def test_stats_counts_unanswered_checks_per_id_top_three(self):
        s = Sandbox()
        self.addCleanup(s.close)
        p = Project(s.root)
        s.write(".windvane/CRITIC.md", SCALE)
        for i, missed in enumerate((["beyond", "safe", "fast"], ["beyond", "safe"], ["beyond", "fix"], [])):
            append_jsonl(p.state / "critic.jsonl", {"ticket": f"T-00{i}", "at": f"2026-10-0{i + 1} 10:00",
                                                    "score": 5, "dims": {"works": 5}, "step": "pass",
                                                    "unanswered": missed})
        with contextlib.redirect_stdout(io.StringIO()) as out:
            stats.critic_stats(argparse.Namespace(last=10, since=None), p)
        self.assertIn("unanswered checks, counted as no: beyond 3, safe 2, fast 1, 1 more\n", out.getvalue())
        self.assertEqual(stats.unanswered_line([{"unanswered": []}, {}]), "")


if __name__ == "__main__":
    unittest.main()
