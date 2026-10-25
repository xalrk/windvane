"""The scale new projects start from asks what a user meets when the change is tried, with an example ladder per
kind of project, and doctor warns of a check worded to grade the evidence pack or the tests instead (T-266)."""
import contextlib
import io
import sys
import unittest
from datetime import timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
from windvane import critic, doctor, driver  # noqa: E402
from windvane.config import DEFAULTS, Project  # noqa: E402
from windvane.util import now  # noqa: E402

from helpers import Sandbox  # noqa: E402
from test_pipeline import args  # noqa: E402

TEMPLATE = (REPO / "templates/CRITIC.md").read_text()
GRADES_PACK = "## works\n- 2 runs: it runs.\n- 5 quoted: the pack quotes every line it prints.\n- 10 class: it holds.\n"
NEAR_MISSES = """## lean
- 8 earns: each turn it adds buys something the pack names, or the pack says why none could go.
- 9 unplanned: a case neither the ticket nor the pack names was tried and held, an input odder than the tests'.
- 10 class: the trial shows the class failing on `main` and not on the head, run through the test sandbox.
"""


class Template(unittest.TestCase):
    def test_it_parses_as_ladders_with_no_warnings(self):
        lad = critic.ladder(TEMPLATE)
        self.assertEqual(list(lad), ["works", "screens", "game", "api"])
        self.assertEqual(critic.misread(TEMPLATE, "x") + critic.evidence_graded(TEMPLATE, "x"), [])

    def test_every_example_ladder_reaches_10_above_the_default_pass_mark(self):
        for d, cs in critic.ladder(TEMPLATE).items():
            self.assertEqual(cs[-1][0], 10, d)
            self.assertGreater(cs[-1][0], DEFAULTS["critic"]["pass"], d)

    def test_ids_are_unique_and_none_is_a_dimension(self):
        lad = critic.ladder(TEMPLATE)
        ids = [cid for cs in lad.values() for _, cid, _ in cs]
        self.assertEqual(len(ids), len(set(ids)), ids)
        self.assertFalse(set(ids) & set(lad), "an id named like a dimension answers the dimension")

    def test_the_command_line_ladder_keeps_the_state_check(self):
        rungs = {cid: (r, q) for r, cid, q in critic.ladder(TEMPLATE)["works"]}
        self.assertEqual(rungs["state"][0], 8)
        for way_out in ("another failure", "a restart", "a second run"):
            self.assertIn(way_out, rungs["state"][1])

    def test_no_check_asks_for_the_pack_or_a_test(self):
        for _, cid, q in [c for cs in critic.ladder(TEMPLATE).values() for c in cs]:
            for word in ("pack", "test", "transcript", "quote"):
                self.assertNotIn(word, q.lower(), cid)


class EvidenceWording(unittest.TestCase):
    def test_a_check_that_grades_the_pack_is_warned_naming_it_and_the_fix(self):
        self.assertEqual(critic.evidence_graded(GRADES_PACK, ".windvane/CRITIC.md"),
                         [('critic scale: check `quoted` grades the evidence ("the pack quotes"), not what trying the '
                           'change shows', "reword it in .windvane/CRITIC.md as what a user meets when the change is "
                           "run, broken or read")])

    def test_the_old_template_wording_is_caught_once_per_check(self):
        old = ("## works\n- 2 shown: a test or transcript in the pack shows the main promise working.\n"
               "- 8 failures: every failure path is exercised by a test or a transcript.\n"
               "- 10 class: it removes a class of failure, and a test reproduces the old failure.\n"
               "## clear\n- 5 said: the pack quotes each new line, quoted whole.\n- 10 tested: a test exists for it.\n")
        got = [w.split("`")[1] for w, _ in critic.evidence_graded(old, "x")]
        self.assertEqual(got, ["shown", "failures", "class", "said", "tested"])

    def test_mentioning_the_pack_or_the_tests_is_no_warning(self):
        self.assertEqual(critic.evidence_graded(NEAR_MISSES, "x"), [])

    def test_this_repos_own_scale_gets_none(self):
        own = REPO / ".windvane/CRITIC.md"
        if not own.is_file():
            self.skipTest("no project scale in this checkout")
        text = own.read_text()
        self.assertEqual(critic.misread(text, "x") + critic.evidence_graded(text, "x"), [])

    def test_doctor_and_the_run_show_it(self):
        s = Sandbox()
        self.addCleanup(s.close)
        s.write(".windvane/CRITIC.md", GRADES_PACK)
        p = Project(s.root)
        with contextlib.redirect_stdout(io.StringIO()):
            got = [(w, f) for level, w, f in doctor.checks(p) if w.startswith("critic scale")]
            driver.Pilot(p, args()).run(now() + timedelta(minutes=1))
        self.assertEqual(len(got), 1, got)
        self.assertIn("check `quoted` grades the evidence", got[0][0])
        log = (p.state / "autopilot.log").read_text()
        self.assertEqual(log.count("critic scale:"), 1, log)

    def test_the_template_in_a_project_gives_doctor_nothing_to_say(self):
        s = Sandbox()
        self.addCleanup(s.close)
        s.write(".windvane/CRITIC.md", TEMPLATE)
        with contextlib.redirect_stdout(io.StringIO()):
            got = [w for level, w, f in doctor.checks(Project(s.root)) if w.startswith("critic scale")]
        self.assertEqual(got, [])


if __name__ == "__main__":
    unittest.main()
