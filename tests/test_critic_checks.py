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
    """T-046: the critic answers each calibration cap before scoring; a hit cap overrides its number."""

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

    def test_the_prompt_and_calibration_ask_for_the_checks(self):
        sp = critic.system_prompt(self.p, Path(__file__).resolve().parents[1])
        self.assertIn("Nearest anchor:", sp)
        caps = [c for c, _, _ in critic.checks(sp)[0]]
        self.assertIn("works", caps)


if __name__ == "__main__":
    unittest.main()
