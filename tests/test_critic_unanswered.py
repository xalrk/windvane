import argparse
import contextlib
import io
import sys
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from windvane import cli, critic, driver, stats  # noqa: E402
from windvane.config import Project  # noqa: E402
from windvane.util import append_jsonl, now  # noqa: E402
from helpers import Sandbox  # noqa: E402
from test_pipeline import args  # noqa: E402

SCALE = """# Scale
## works
- 2 shown: is the promise shown?
- 5 safe: no known crash?
- 8 failures: failure paths tested?
"""
CHECKS = """Checks:
- shown: yes, the transcript shows it
- safe: yes, no crash in 40 runs
"""
LATER = "- failures: yes, a later ticket could test the timeout\n"


class OneCount(unittest.TestCase):
    """`stats`, `overview` and `critic.unanswered` count the critic's unanswered ids by one rule, so the same scale
    problem shows in all of them or in none."""

    def setUp(self):
        self.s = Sandbox()
        self.addCleanup(self.s.close)
        self.p = Project(self.s.root)
        self.p.critic["enabled"] = True
        self.s.write(".windvane/CRITIC.md", SCALE)

    def ledger(self):
        recs = [{"ticket": "PLAN", "unanswered": ["safe"] * 3},  # a plan marker and a project verdict skew nothing
                {"ticket": "PROJECT", "unanswered": ["safe", "shown"]},
                {"ticket": "T-001", "score": 5}]  # an old record with no unanswered list
        recs += [{"ticket": f"T-0{i:02}", "unanswered": ids} for i, ids in enumerate(
            [["shown"]] * 4 + [["safe", "safe"], ["safe", "failures"], ["safe"], ["failures"], [], ["failures"],
                               [], [], [], ["shown"]], 10)]  # the first four drop out of the last ten
        for r in recs:
            append_jsonl(self.p.state / "critic.jsonl", {"at": "2026-10-01 10:00", "dims": {"works": 2},
                                                         "step": "pass", **r, "score": r.get("score", 2)})
        return recs

    def test_stats_overview_and_unanswered_agree_on_a_mixed_ledger(self):
        recs = self.ledger()
        self.assertEqual(critic.unanswered(recs), [("safe", 3, 10), ("failures", 3, 10)])
        with contextlib.redirect_stdout(io.StringIO()) as out:
            stats.critic_stats(argparse.Namespace(last=10, since=None), self.p)
        self.assertIn("unanswered checks, counted as no (times of the last 10 verdicts): safe 3, failures 3; reword "
                      "or rename them in .windvane/CRITIC.md.\n", out.getvalue())
        self.assertNotIn("shown", out.getvalue().split("unanswered checks")[1].split("\n")[0])
        gaps = [line for line in cli.check_line(self.p, recs).splitlines() if "unanswered" in line]
        self.assertEqual([g.split(" unanswered in ")[0] for g in gaps],
                         ["Critic left safe", "Critic left failures"])
        self.assertTrue(all(" in 3 of the last 10:" in g for g in gaps))

    def test_the_stats_line_says_where_to_fix_it(self):
        recs = [{"ticket": "T-001", "unanswered": ["safe"]}] * 3
        self.assertEqual(stats.unanswered_line(self.p, recs), "unanswered checks, counted as no (times of the last 3 "
                         "verdicts): safe 3; reword or rename it in .windvane/CRITIC.md.")
        self.assertEqual(stats.unanswered_line(self.p, recs[:2]), "", "twice isn't a pattern yet")


class OnlyScaleIds(unittest.TestCase):
    """An answer line counts only when its label names an id in the scale."""

    def score(self, text):
        lad = critic.ladder(SCALE)
        dims, said, _, _, missing = critic.climb(lad, critic.answers(text, lad), 8)
        return dims, said, missing

    def test_a_bulleted_beyond_line_after_the_checks_changes_nothing(self):
        plain = self.score(CHECKS + "CRITIC: T-001 fixes=0 good")
        self.assertEqual(plain, ({"works": 5}, {"shown": "yes", "safe": "yes", "failures": "none"}, ["failures"]))
        for tail in ("- Beyond: - add X\n", "- Beyond: - add X\n" + LATER, "* **Beyond:** - add X\n" + LATER):
            with self.subTest(tail=tail):
                self.assertEqual(self.score(CHECKS + tail + "CRITIC: T-001 fixes=0 good"), plain)

    def test_an_answer_naming_no_id_in_the_scale_answers_nothing(self):
        lad = critic.ladder(SCALE)
        text = CHECKS + "- failure: yes, close but not the id\n- timeouts: no, none\nCRITIC: T-001 fixes=0 good"
        self.assertEqual(self.score(text)[2], ["failures"])
        self.assertEqual(critic.ignored(text, lad), ["failure", "timeouts"])
        self.assertEqual(critic.ignored(CHECKS + LATER, lad), [])

    def test_a_check_named_beyond_is_still_an_answer(self):
        lad = critic.ladder(SCALE + "- 10 beyond: goes past the ticket?\n")
        ans = critic.answers(CHECKS + "- beyond: no, nothing more\n" + LATER + "CRITIC: T-001 fixes=0 ok", lad)
        self.assertEqual((ans["beyond"][0], ans["failures"][0]), ("no", "yes"))

    def test_the_driver_logs_the_ignored_labels(self):
        s = Sandbox()
        self.addCleanup(s.close)
        p = Project(s.root)
        s.write(".windvane/CRITIC.md", SCALE)
        lad = critic.ladder(SCALE)
        entry, _ = critic.climbed(p, "T-001", s.root, "HEAD", lad, CHECKS + "- failure: yes\nCRITIC: T-001 fixes=0 ok",
                                  0, "ok", 1)
        self.assertEqual((entry["ignored"], entry["unanswered"]), (["failure"], ["failures"]))
        self.assertEqual(critic.ignored_note(p, "T-001", entry["ignored"]),
                         "T-001: ignored the critic's answer `- failure:`, which names no check in "
                         ".windvane/CRITIC.md; if it keeps writing it, rename the check it meant to that word.")
        self.assertIn("answers `- failure:`, `- speed:`, which name no check",
                      critic.ignored_note(p, "T-001", ["failure", "speed"]))


class ScalePushSuffix(unittest.TestCase):
    def test_two_warnings_send_one_push_naming_the_other(self):
        s = Sandbox()
        self.addCleanup(s.close)
        p = Project(s.root)
        s.write(".windvane/CRITIC.md", "## works\n- 5 safe: safe?\n- 8 safe: still safe?\n"
                                       "## works again\n- 10 fast: fast?\n")
        self.assertEqual(len(critic.scale_warnings(p, p.root)), 2)
        pushes = []
        with mock.patch.object(driver, "push", lambda t, m="", *a, **k: pushes.append((t, m)) or True), \
                contextlib.redirect_stdout(io.StringIO()):
            driver.Pilot(p, args()).run(now() + timedelta(minutes=1))
        self.assertEqual([m for t, m in pushes if t == "Critic scale"],
                         ["Rename one in .windvane/CRITIC.md: dimension works has 2 checks named `safe`, so one answer "
                          "scores them all. (1 more: windvane doctor lists them)"])


if __name__ == "__main__":
    unittest.main()
