import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from windvane import critic  # noqa: E402
from windvane.config import Project  # noqa: E402
from windvane.util import append_jsonl, read_jsonl  # noqa: E402
from helpers import Sandbox  # noqa: E402

CHECKED = """Caps:
- works<=5 (crash, lose work, wedge): yes: a playtest cut off by an API error loses its milestone
- lean<=5 (tokens on nothing): no, because nothing reruns
- `clear<=7` (confusing text): **no**, the log line names the milestone
Nearest anchor: T-004 (works 5), because a known loss path is left open
Scores: works=7 clear=8 lean=8
Good otherwise.
Fix in this ticket:
1. a cut-off playtest loses its milestone -> keep it for the next round
CRITIC: T-040 works=7 clear=8 lean=8 fixes=0 loses a milestone on a cut-off playtest"""

PLAIN = "Scores: works=8 clear=7\nFine.\nCRITIC: T-040 works=8 clear=7 fixes=0 fine"


class Checks(unittest.TestCase):
    """T-046: on an old numeric scale the critic answers each calibration cap before scoring; a hit cap overrides its
    number."""

    def setUp(self):
        s = Sandbox("pass = 8\nfloor = 4\n")
        self.addCleanup(s.close)
        self.s, self.p = s, Project(s.root)

    def judge(self, reply, tid="T-040"):
        d = critic.pack_dir(self.p, tid)
        d.mkdir(parents=True, exist_ok=True)
        (d / "notes.md").write_text("User-facing: yes\nit works\n")
        with mock.patch.object(critic, "run_claude", lambda *a, **kw: (reply, None, None)):
            return critic.judge(self.p, tid, self.s.root)

    def test_checks_are_parsed(self):
        caps, anchor = critic.checks(CHECKED)
        self.assertEqual(caps, [("works", 5.0, True), ("lean", 5.0, False), ("clear", 7.0, False)])
        self.assertEqual(anchor, "T-004 (works 5), because a known loss path is left open")

    def test_a_hit_cap_overrides_the_score(self):
        entry, critique = self.judge(CHECKED)
        self.assertEqual(entry["dims"], {"works": 5.0, "clear": 8.0, "lean": 8.0})
        self.assertEqual(entry["score"], 5.0)
        self.assertEqual(entry["capped"], {"works": 7.0})
        self.assertEqual(entry["hits"], ["works<=5"])
        self.assertEqual(entry["checks"], 3)
        self.assertEqual((entry["fixes"], entry["step"]), (0, "revise"), "a hit cap with nothing listed goes back")
        self.assertEqual(entry["effective"], 1, "the count the step used is stored beside the critic's own")
        self.assertIn("Capped by the driver: works<=5 answered yes (works 7 → 5)", critique)
        self.assertNotIn("Capped by the driver", critic.unscored(critique))
        self.assertTrue(critique.startswith("Caps:"), "the checks stay in the recorded critique")
        self.assertEqual(read_jsonl(self.p.state / "critic.jsonl")[-1]["score"], 5.0)

    def test_a_hit_cap_still_runs_out_of_revisions(self):
        for _ in range(3):
            append_jsonl(self.p.state / "critic.jsonl", {"ticket": "T-040", "score": 5, "step": "revise", "fixes": 1})
        entry, _ = self.judge(CHECKED)
        self.assertEqual(entry["step"], "accept")

    def test_a_cap_under_the_number_already_leaves_it(self):
        dims, was = critic.capped({"works": 3, "clear": 8}, [("works", 5, True), ("clear", 7, False)])
        self.assertEqual((dims, was), ({"works": 3, "clear": 8}, {}))

    def test_a_hit_cap_on_an_untouched_dimension_still_counts(self):
        dims, was = critic.capped({"works": 8}, [("lean", 5, True)])
        self.assertEqual((dims, was), ({"works": 8, "lean": 5}, {}))

    def test_a_reply_without_checks_still_parses(self):
        self.assertEqual(critic.checks(PLAIN), ([], ""))
        entry, critique = self.judge(PLAIN)
        self.assertEqual((entry["dims"], entry["score"], entry["step"]), ({"works": 8.0, "clear": 7.0}, 7.0, "scope"))
        self.assertEqual((entry["checks"], entry["capped"], entry["hits"], entry["anchor"]), (0, {}, [], ""))
        self.assertTrue(critique.startswith("Scores:"))

    def test_nothing_after_the_critic_line_counts(self):
        caps, _ = critic.checks(PLAIN + "\n- works<=5 (crash): yes")
        self.assertEqual(caps, [])

    def test_recritique_hides_the_checks_and_anchor(self):
        self.assertNotIn("Nearest anchor", critic.unscored(CHECKED))
        self.assertNotIn("works<=5", critic.unscored(CHECKED))
        self.assertIn("Good otherwise.", critic.unscored(CHECKED))

    def test_stats_count_checks_and_caps(self):
        self.judge(CHECKED)
        self.judge(PLAIN.replace("T-040", "T-041"), "T-041")
        c = critic.calibration(read_jsonl(self.p.state / "critic.jsonl"), 8)
        self.assertEqual((c["checked"], c["capped"], c["verdicts"]), (1, 1, 2))

    def test_an_old_style_scale_keeps_the_numeric_prompt(self):
        self.s.write(".windvane/CRITIC.md", "# What a 10 means\n- **works**: 2: one test. 5: the CLI. 8: failures.\n")
        sp = critic.system_prompt(self.p, self.s.root)
        self.assertIn("Nearest anchor:", sp)
        self.assertNotIn("<check id>", sp)
        self.assertEqual(critic.ladder(sp), {})


LADDER = """# What a 10 means for this project
Someone who uses it.

## works
What the ticket promises.
- 2 shown: a test or transcript shows the main promise.
- 5 safe: no known way its code crashes or loses work.
- 8 failures: every failure path is exercised.
- 10 class: it removes a class of failure.

## clear
- 5 quoted: the pack quotes the new text.
- 8 awake: read half awake, it says what to do.
"""
YES = {"shown": "yes", "safe": "yes", "failures": "yes", "class": "no", "quoted": "yes", "awake": "yes"}


def reply(answers, tid="T-054", fixes=0):
    lines = [f"- {k}: {v}, because the pack says so" for k, v in answers.items()]
    return "\n".join(["Checks:", *lines, "A user would be glad of it.", f"CRITIC: {tid} fixes={fixes} the gist"])


class Ladder(unittest.TestCase):
    """T-054: on a scale written as ladders the critic answers yes/no checks and the driver works out the score."""

    def setUp(self):
        s = Sandbox("pass = 8\nfloor = 4\n")
        self.addCleanup(s.close)
        self.s, self.p = s, Project(s.root)
        s.write(".windvane/CRITIC.md", LADDER)

    def judge(self, answers, tid="T-054", **kw):
        return Checks.judge(self, reply(answers, tid, **kw), tid)

    def test_a_ladder_is_parsed(self):
        lad = critic.ladder(LADDER)
        self.assertEqual(list(lad), ["works", "clear"])
        self.assertEqual([(r, c) for r, c, _ in lad["works"]], [(2, "shown"), (5, "safe"), (8, "failures"), (10, "class")])
        self.assertEqual(lad["clear"][0], (5, "quoted", "the pack quotes the new text."))
        self.assertEqual(critic.ladder("# Scale\n- **works**: 2: happy path. 5: the CLI. 8: failure paths.\n"), {})

    def test_the_score_is_the_highest_rung_whose_checks_and_all_below_are_yes(self):
        entry, critique = self.judge(YES)
        self.assertEqual((entry["dims"], entry["score"], entry["step"]), ({"works": 8, "clear": 8}, 8, "pass"))
        self.assertEqual(entry["answers"], YES, "every answer is kept for the check rates")
        self.assertTrue(critique.startswith("Checks:"))
        self.assertTrue(critique.endswith("CRITIC: T-054 works=8 clear=8 fixes=0 the gist"), "the line carries the score")
        self.assertEqual(critic.verdict(critique)[1], {"works": 8, "clear": 8})
        entry, _ = self.judge({**YES, "class": "yes"})
        self.assertEqual(entry["dims"]["works"], 10)

    def test_a_no_on_rung_5_holds_the_score_at_2_even_with_rung_8_yes(self):
        entry, critique = self.judge({**YES, "safe": "no"})
        self.assertEqual((entry["dims"]["works"], entry["score"]), (2, 2))
        self.assertEqual((entry["step"], entry["failed"], entry["fixes"], entry["effective"]), ("revise", ["safe"], 0, 1))
        self.assertIn("- works.safe (rung 5) failed: no known way its code crashes or loses work. (because the pack says so)",
                      critique)

    def test_a_plain_no_under_the_bar_sends_it_back_with_the_failed_checks_as_fixes(self):
        entry, critique = self.judge({**YES, "failures": "no", "awake": "no"})
        self.assertEqual((entry["score"], entry["step"], entry["failed"]), (5, "revise", ["failures", "awake"]))
        self.assertIn("Failed checks under the bar (fix these too):\n- works.failures (rung 8) failed:", critique)
        self.assertIn("- clear.awake (rung 8) failed:", critique)
        self.assertIn("CRITIC: T-054 works=5 clear=5 fixes=2", critique)

    def test_out_of_scope_gives_scope_and_a_follow_up(self):
        entry, critique = self.judge({**YES, "failures": "no (out of scope: needs the lock T-060 adds)"})
        self.assertEqual((entry["score"], entry["step"], entry["failed"]), (5, "scope", []))
        self.assertEqual(entry["scoped"], ["every failure path is exercised. (out of scope: needs the lock T-060 adds; "
                                           "because the pack says so)"])
        self.assertIn(("T-054", entry["scoped"][0]), critic.followups(self.p))

    def test_a_free_form_fix_still_blocks(self):
        entry, _ = self.judge(YES, fixes=1)
        self.assertEqual((entry["score"], entry["step"], entry["effective"]), (8, "revise", 1))

    def test_an_unanswered_check_counts_as_no(self):
        entry, critique = self.judge({k: v for k, v in YES.items() if k != "awake"})
        self.assertEqual((entry["dims"]["clear"], entry["step"]), (5, "revise"))
        self.assertEqual((entry["unanswered"], entry["failed"], entry["answers"]["awake"]), (["awake"], ["awake"], "none"))
        self.assertIn("Unanswered, counted as no: awake", critique)
        self.assertIn("- clear.awake (rung 8) failed: read half awake, it says what to do. (unanswered)", critique)

    def test_an_untouched_dimension_is_left_out_but_never_the_first(self):
        entry, critique = self.judge({"works": "-", "shown": "yes", "safe": "yes", "failures": "yes", "clear": "-"})
        self.assertEqual((entry["dims"], entry["step"]), ({"works": 8}, "pass"))
        self.assertNotIn("quoted", entry["answers"])
        self.assertIn("works=8 clear=- fixes=0", critique)

    def test_failing_the_first_rung_of_the_first_dimension_is_a_rethink(self):
        self.assertEqual(self.judge({**YES, "shown": "no"})[0]["step"], "rethink")
        self.assertEqual(self.judge({**YES, "quoted": "no"}, "T-055")[0]["step"], "revise", "only the first dimension")

    def test_a_reply_that_answers_no_check_is_no_verdict(self):
        entry, text = Checks.judge(self, PLAIN.replace("T-040", "T-054"), "T-054")
        self.assertIsNone(entry)
        self.assertEqual(read_jsonl(self.p.state / "critic.jsonl"), [])

    def test_answers_parse_the_ways_a_critic_writes_them(self):
        ans = critic.answers("- `safe`: **no** (out of scope) the lock\n* shown: Yes: transcript\nclear: -\n"
                             "Fix in this ticket:\n1. nothing: no\nCRITIC: T-1 fixes=0 g\n- class: yes")
        self.assertEqual(ans["safe"], ("scope", "the lock"))
        self.assertEqual(ans["shown"], ("yes", "transcript"))
        self.assertEqual(ans["clear"], ("-", ""))
        self.assertNotIn("class", ans, "nothing after the CRITIC line counts")
        self.assertNotIn("nothing", ans)

    def test_a_check_named_with_its_dimension_or_rung_still_counts(self):
        ans = critic.answers("- works.safe: no, a race\n- 8 failures: yes, tested\n- works 10 class: no (out of scope: "
                             "T-060)\n- `clear.quoted`: yes\n- clear 8 awake: yes")
        self.assertEqual({k: a for k, (a, _) in ans.items()},
                         {"safe": "no", "failures": "yes", "class": "scope", "quoted": "yes", "awake": "yes"})
        entry, _ = self.judge({"works.shown": "yes", "works.safe": "no", "8 failures": "yes", "clear 5 quoted": "yes",
                               "clear 8 awake": "yes"})
        self.assertEqual((entry["dims"], entry["failed"], entry["unanswered"]), ({"works": 2, "clear": 8}, ["safe"], ["class"]))

    def test_a_recritique_hides_the_answers_but_keeps_the_fix_list(self):
        _, critique = self.judge({**YES, "safe": "no"})
        hidden = critic.unscored(critique)
        self.assertNotIn("- shown: yes", hidden)
        self.assertNotIn("CRITIC:", hidden)
        self.assertIn("- works.safe (rung 5) failed:", hidden)
        self.assertEqual(critic.answers(critique)["safe"][0], "no", "the fix list never reads as an answer")
        self.assertNotIn("failures", {k for k, (a, _) in critic.answers(hidden).items()})

    def test_the_prompt_asks_for_answers_not_numbers(self):
        sp = critic.system_prompt(self.p, self.s.root)
        self.assertIn("- <check id>: yes", sp)
        self.assertIn("CRITIC: <ID> fixes=", sp)
        self.assertNotIn("Nearest anchor:", sp)
        self.assertNotIn("Scores:", sp)

    def test_the_shipped_scale_is_a_short_ladder(self):
        root = Path(__file__).resolve().parents[1]
        lad = critic.ladder((root / "templates/CRITIC.md").read_text())
        ids = [c for cs in lad.values() for _, c, _ in cs]
        self.assertEqual(list(lad)[0], "works")
        self.assertLessEqual(len(ids), 12)
        self.assertEqual(len(ids), len(set(ids)), "check ids are unique across the scale")
        self.assertLessEqual(len((root / "prompts/CRITIC.md").read_text().splitlines()), 54, "the prompt never grows")


if __name__ == "__main__":
    unittest.main()
