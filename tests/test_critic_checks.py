import argparse
import io
import os
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from windvane import cli, critic  # noqa: E402
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

    def test_an_out_of_scope_cap_caps_the_score_but_is_no_fix(self):
        """R-002: the user chose to merge as a step; a true `yes` cap sent the ticket back three times."""
        r = CHECKED.replace("wedge): yes:", "wedge): yes (out of scope: the user chose to merge as a step):")
        entry, critique = self.judge(r)
        self.assertEqual((entry["dims"]["works"], entry["score"]), (5.0, 5.0), "the score stays honest")
        self.assertEqual((entry["fixes"], entry["effective"], entry["step"]), (0, 0, "scope"))
        self.assertEqual(entry["hits"], [])
        self.assertEqual(entry["scoped"], ["works<=5 (out of scope: the user chose to merge as a step)"])
        self.assertIn("works<=5 answered yes, out of scope (works 7 → 5)", critique)
        self.assertIn(("T-040", "works<=5 (out of scope: the user chose to merge as a step)"), critic.followups(self.p))

    def test_a_plain_yes_cap_with_no_fixes_still_goes_back(self):
        entry, _ = self.judge(CHECKED)
        self.assertEqual((entry["fixes"], entry["effective"], entry["step"], entry["scoped"]), (0, 1, "revise", []))

    def test_a_cap_named_in_words_maps_to_the_dimension(self):
        caps, _ = critic.checks("- out of the way<=2 (x): **yes**, because\nCRITIC: T-1 out-of-the-way=2 fixes=0 g")
        self.assertEqual(caps, [("out-of-the-way", 2.0, True)])
        caps, _ = critic.checks("- **on the course**<=4 (x): no\n- `works<=5` (y): **yes** (out of scope)\n"
                                "CRITIC: T-1 on=5 works=3 fixes=0 g")
        self.assertEqual(caps, [("on", 4.0, False), ("works", 5.0, "no reason given")])

    def test_stats_count_out_of_scope_caps_beside_hits(self):
        self.judge(CHECKED)
        self.judge(CHECKED.replace("T-040", "T-041").replace("wedge): yes:", "wedge): yes (out of scope: later):"),
                   "T-041")
        c = critic.calibration(read_jsonl(self.p.state / "critic.jsonl"), 8)
        self.assertEqual((c["hits"], c["scoped_caps"]), (1, 1))
        self.assertIn("out of scope: <why>", critic.system_prompt(self.p, self.s.root))

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

    def test_the_old_failure_a_score_under_the_bar_that_never_went_back(self):
        """T-040 went on as `scope` three times at works 7 with nothing listed to fix, though each critique said a
        cut-off playtest loses its milestone. The same judgement as answers goes back by itself."""
        (self.s.root / ".windvane/CRITIC.md").unlink()
        old, _ = Checks.judge(self, PLAIN.replace("works=8 clear=7", "works=7 clear=8"), "T-040")
        self.assertEqual((old["score"], old["step"]), (7, "scope"))
        self.s.write(".windvane/CRITIC.md", LADDER)
        new, _ = self.judge({**YES, "safe": "no, a cut-off playtest loses its milestone"}, "T-041")
        self.assertEqual((new["score"], new["fixes"], new["step"], new["failed"]), (2, 0, "revise", ["safe"]))

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
                             "Fix in this ticket:\n1. nothing: no\nBeyond this ticket:\n- shown: no tests for X yet\n"
                             "CRITIC: T-1 fixes=0 g\n- class: yes")
        self.assertEqual(ans["safe"], ("scope", "the lock"))
        self.assertEqual(ans["shown"], ("yes", "transcript"), "a later follow-up line doesn't overturn an answer")
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


COURSE = LADDER + """
## on the course
- 3 reachable: a player can reach it.
- 8 `fair`: it plays fair.
"""
WRAP = "Checks:\n{}\n\nCRITIC: T-1 fixes=0 g"


class Labels(unittest.TestCase):
    """T-108: answers are read by any label the scale gives a check (`works shown`, `on the course 3 reachable`), and
    a reply with no verdict says why."""

    def setUp(self):
        s = Sandbox("pass = 8\nfloor = 4\n")
        self.addCleanup(s.close)
        self.s, self.p = s, Project(s.root)
        s.write(".windvane/CRITIC.md", COURSE)
        self.lad = critic.ladder(COURSE)

    def ans(self, line):
        return critic.answers(WRAP.format(line), self.lad)

    def judge(self, reply, tid="T-054"):
        return Checks.judge(self, reply, tid)

    def test_the_heading_keeps_its_full_text(self):
        self.assertEqual(list(self.lad), ["works", "clear", "on"])
        self.assertEqual(self.lad.heads["on the course"], "on")
        self.assertEqual([c for _, c, _ in self.lad["on"]], ["reachable", "fair"])

    def test_the_repro_lines_parse(self):
        self.assertEqual(self.ans("- works shown: yes. ok"), {"shown": ("yes", "ok")})
        self.assertEqual(self.ans("- on the course 3 reachable: yes"), {"reachable": ("yes", "")})
        self.assertEqual(self.ans("- **works shown**: yes"), {"shown": ("yes", "")})
        self.assertEqual(self.ans("- on the course: -"), {"on": ("-", "")})
        self.assertEqual(self.ans("- __on the course__: -"), {"on": ("-", "")})

    def test_the_old_forms_still_parse_and_unknown_labels_are_ignored(self):
        self.assertEqual(self.ans("- works 2 shown: no, nothing")["shown"], ("no", "nothing"))
        self.assertEqual(self.ans("works.shown: yes")["shown"][0], "yes")
        self.assertEqual(self.ans("- works/shown: yes")["shown"][0], "yes")
        self.assertEqual(self.ans("- `on.reachable`: no (out of scope: T-9)")["reachable"][0], "scope")
        self.assertEqual(self.ans("- clear: -"), {"clear": ("-", "")})
        self.assertEqual(self.ans("- nothing: no"), {})
        self.assertEqual(self.ans("- works shown is fine: no"), {}, "the last word must be a check id")
        self.assertEqual(critic.answers(WRAP.format("- works shown: yes")), {}, "no scale: today's forms only")

    def test_a_reply_in_the_dimension_and_id_form_passes(self):
        lines = [f"- {d} {c}: yes, shown" for d, cs in self.lad.items() for _, c, _ in cs]
        entry, critique = self.judge("\n".join(["Checks:", *lines, "Good.", "CRITIC: T-054 fixes=0 fine"]))
        self.assertIsNotNone(entry)
        self.assertEqual((entry["dims"], entry["step"], entry["unanswered"]), ({"works": 10, "clear": 8, "on": 8}, "pass", []))

    def test_a_multi_word_heading_is_scored_from_its_answers_or_left_out(self):
        base = ["- shown: yes", "- safe: yes", "- failures: yes", "- class: no", "- quoted: yes", "- awake: yes"]
        entry, _ = self.judge("\n".join(["Checks:", *base, "- on the course 3 reachable: yes",
                                          "- on the course 8 fair: no, a dead end", "CRITIC: T-054 fixes=0 g"]))
        self.assertEqual((entry["dims"]["on"], entry["failed"], entry["unanswered"]), (3, ["fair"], []))
        entry, critique = self.judge("\n".join(["Checks:", *base, "- on the course: -", "CRITIC: T-055 fixes=0 g"]), "T-055")
        self.assertEqual((entry["dims"], entry["step"]), ({"works": 8, "clear": 8}, "pass"))
        self.assertIn("on=- fixes=0", critique)

    def test_a_recritique_hides_answers_in_the_new_forms(self):
        earlier = ("Checks:\n- works shown: yes, ok\n- on the course 3 reachable: no, a wall\n- on the course: -\n"
                   "- **clear quoted**: yes\nFine.\nBeyond this ticket:\n- a map\nCRITIC: T-054 fixes=0 g")
        hidden = critic.unscored(earlier, self.lad)
        self.assertEqual(hidden, "Checks:\nFine.\nBeyond this ticket:\n- a map")
        append_jsonl(self.p.state / "critic.jsonl", {"ticket": "T-054", "step": "revise", "critique": earlier})
        seen = []
        d = critic.pack_dir(self.p, "T-054")
        d.mkdir(parents=True, exist_ok=True)
        (d / "notes.md").write_text("User-facing: yes\nit works\n")
        with mock.patch.object(critic, "run_claude", lambda p, r, l, prompt, *a, **kw: (seen.append(prompt), ("", None, None))[1]):
            critic.judge(self.p, "T-054", self.s.root)
        self.assertIn("- a map", seen[0])
        self.assertNotIn("reachable", seen[0])

    def step(self, reply, tid="T-054"):
        """What the worker reads from `windvane critic` when there's no verdict, and its exit."""
        if reply is not None:
            d = critic.pack_dir(self.p, tid)
            d.mkdir(parents=True, exist_ok=True)
            (d / "notes.md").write_text("User-facing: yes\nit works\n")
        out, cwd = io.StringIO(), os.getcwd()
        os.chdir(self.s.root)
        self.addCleanup(os.chdir, cwd)
        with mock.patch.object(critic, "run_claude", lambda *a, **kw: (reply, None, None)), redirect_stdout(out), \
                mock.patch.dict(self.p.critic, enabled=True), self.assertRaises(SystemExit):
            cli.cmd_critic(argparse.Namespace(ticket=tid), self.p)
        return out.getvalue().strip().splitlines()[-1]

    def test_no_verdict_says_why_and_only_a_missing_pack_says_fix_the_pack(self):
        cases = {
            None: "no critic pack. Write ",
            "Fine, no line.": "the critic's reply has no CRITIC line.",
            "Checks:\n- shown: yes\nCRITIC: T-099 fixes=0 g": "the critic's CRITIC line is for T-099, not T-054.",
            "Checks:\n- the demo: yes\n- `speed`: no\nCRITIC: T-054 fixes=0 g":
                "no answer the driver could match to a check (labels it saw: the demo, `speed`; the scale's ids: shown, "
                "safe, failures, class, quoted, awake, reachable, fair).",
        }
        for reply, why in cases.items():
            with self.subTest(reply=reply):
                last = self.step(reply)
                self.assertTrue(last.startswith(f"CRITIC STEP: none. No verdict: {why}"), last)
                self.assertEqual("pack isn't the problem" in last, reply is not None)
                self.assertEqual("notes.md, then run" in last, reply is None)
        self.assertEqual(read_jsonl(self.p.state / "critic.jsonl"), [])


if __name__ == "__main__":
    unittest.main()
