import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from windvane import critic  # noqa: E402
from windvane.config import Project  # noqa: E402
from windvane.util import append_jsonl  # noqa: E402
from helpers import Sandbox  # noqa: E402


class Scale(unittest.TestCase):
    """T-022: the critic scores blind, per dimension; the driver alone compares with the bar."""

    def setUp(self):
        s = Sandbox("pass = 7.5\nfloor = 3.5\n")
        self.addCleanup(s.close)
        self.s, self.p = s, Project(s.root)

    def test_system_prompt_has_no_bar(self):
        sp = critic.system_prompt(self.p, self.s.root)
        for leak in ("7.5", "3.5", "{pass}", "{floor}", "bar to pass"):
            self.assertNotIn(leak, sp)

    def test_lowest_dimension_is_the_score(self):
        tid, dims, fixes, gist = critic.verdict("Scores: ...\nCRITIC: T-009 works=8 clear=6 lean=9 fixes=0 tidy")
        self.assertEqual((tid, fixes, gist), ("T-009", 0, "tidy"))
        self.assertEqual(min(dims.values()), 6)
        self.assertEqual(critic.step(self.p, dims, 0, 0), "scope")
        self.assertEqual(critic.step(self.p, {"works": 8, "clear": 9}, 0, 0), "pass")

    def test_dash_dimensions_are_skipped(self):
        _, dims, _, _ = critic.verdict("CRITIC: T-009 works=8 clear=- lean=- fixes=0 ok")
        self.assertEqual(dims, {"works": 8})
        self.assertEqual(critic.step(self.p, dims, 0, 0), "pass")

    def test_rethink_only_when_the_first_dimension_is_under_the_floor(self):
        self.assertEqual(critic.step(self.p, {"works": 6, "clear": 2}, 1, 0), "revise")
        self.assertEqual(critic.step(self.p, {"works": 3, "clear": 8}, 1, 0), "rethink")
        self.assertEqual(critic.step(self.p, {"works": 3, "clear": 8}, 1, 3), "accept")

    def test_old_line_still_parses(self):
        self.assertEqual(critic.verdict("Score: 7/10\nCRITIC: 7/10 T-003 fixes=1 meh"),
                         ("T-003", {"score": 7.0}, 1, "meh"))
        self.assertEqual(critic.verdict("CRITIC: T-003 score=4 fixes=2 bad"), ("T-003", {"score": 4.0}, 2, "bad"))
        self.assertIsNone(critic.verdict("no verdict here"))

    def test_recritique_shows_the_earlier_critique_without_its_score(self):
        d = critic.pack_dir(self.p, "T-001")
        d.mkdir(parents=True)
        (d / "notes.md").write_text("User-facing: yes\nit works\n")
        append_jsonl(self.p.state / "critic.jsonl", {
            "ticket": "T-001", "score": 6, "step": "revise", "fixes": 1,
            "critique": "Scores: works=6 clear=8\nThe retry path wedges.\nFix in this ticket:\n1. retry wedges\n"
                        "CRITIC: T-001 works=6 clear=8 fixes=1 wedges"})
        seen = {}

        def fake(p, role, name, prompt, system, cwd, **kw):
            seen["prompt"] = prompt
            return "Scores: works=8 clear=8\nFixed.\nCRITIC: T-001 works=8 clear=8 fixes=0 fixed", None, None

        with mock.patch.object(critic, "run_claude", fake):
            entry, _ = critic.judge(self.p, "T-001", self.s.root)
        earlier = seen["prompt"].split("## Your earlier critiques")[1]
        self.assertIn("The retry path wedges.", earlier)
        self.assertNotIn("works=6", earlier)
        self.assertNotIn("CRITIC:", earlier)
        self.assertEqual((entry["score"], entry["dims"], entry["step"]), (8, {"works": 8, "clear": 8}, "pass"))


if __name__ == "__main__":
    unittest.main()
