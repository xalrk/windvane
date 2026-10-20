import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from windvane import critic  # noqa: E402
from windvane.config import Project  # noqa: E402
from windvane.util import read_jsonl  # noqa: E402
from helpers import Sandbox  # noqa: E402
from test_critic_unanswered import CHECKS, SCALE  # noqa: E402


class DriverRunCutsNotes(unittest.TestCase):
    """The driver's own critic run (`critic.judge`, the fake `claude`) on a worker's leftover pack: notes over the cap
    reach the critic cut at a line end, with a line saying so; notes under it reach it unchanged."""

    def setUp(self):
        s = Sandbox()
        self.addCleanup(s.close)
        s.ticket("T-001")
        s.publish("a ticket")
        self.s, self.p = s, Project(s.root)
        self.notes = critic.pack_dir(self.p, "T-001") / "notes.md"
        self.notes.parent.mkdir(parents=True)

    def prompt(self, text):
        """The notes section of the one critic prompt `judge` sends for a pack holding `text`."""
        self.notes.write_text(text)
        critic.judge(self.p, "T-001", self.s.root)
        calls = [c["prompt"] for c in read_jsonl(self.s.tmp / "calls.jsonl") if c["system"].startswith("# windvane critic")]
        self.assertEqual(len(calls), 1)
        return calls[0].split("## The worker's pack notes\n", 1)[1].split("\n\n## Files the change touches", 1)[0]

    def test_long_notes_are_cut_at_a_line_end_and_say_so(self):
        lines = ["User-facing: yes"] + [f"line {i:03}: the verdict → measured, in full — no cut" for i in range(200)]
        text = "\n".join(lines) + "\n"
        text = text[:4999].rsplit("\n", 1)[0] + "\n"
        text += "x" * (5000 - len(text) - 1) + "\n"
        self.assertEqual(len(text), 5000)
        got = self.prompt(text)
        kept, cut = got.rstrip("\n").rsplit("\n", 1)
        self.assertLessEqual(len(got), critic.NOTES_CAP)
        self.assertIn("5000 characters", cut)
        self.assertIn(f"at most {critic.NOTES_CAP}", cut)
        self.assertIn("don't judge the notes unclear", cut)
        kept = kept.rstrip("\n")
        self.assertTrue(text.startswith(kept + "\n"), "the notes up to a line end, unchanged")
        self.assertGreater(len(kept), critic.NOTES_CAP - 400, "it keeps as much as fits")

    def test_short_notes_reach_the_critic_unchanged(self):
        text = "User-facing: yes\nWhat the user gets → a reply in 2 s — measured.\n"
        self.assertEqual(self.prompt(text), text)

    def test_one_long_line_is_still_cut_under_the_cap(self):
        got = critic.capped_notes("User-facing: yes " + "word " * 1200)
        self.assertLessEqual(len(got), critic.NOTES_CAP)
        self.assertIn("6017 characters", got.splitlines()[-1])


class RefusalCountsCharacters(unittest.TestCase):
    def test_the_refusal_names_wc_m(self):
        s = Sandbox()
        self.addCleanup(s.close)
        p = Project(s.root)
        f = critic.pack_dir(p, "T-001") / "notes.md"
        f.parent.mkdir(parents=True)
        f.write_text("→" * (critic.NOTES_CAP + 1))
        said = critic.too_long(p, "T-001")
        self.assertIn(f"{critic.NOTES_CAP + 1} characters", said)
        self.assertIn("`wc -m`", said)
        f.write_text("→" * critic.NOTES_CAP)  # 12000 bytes, but at the cap in characters
        self.assertEqual(critic.too_long(p, "T-001"), "")


class BeyondEndsTheBlock(unittest.TestCase):
    """A `- beyond: yes` answer in a scale with no `beyond` id ends the Checks block; the log names it."""

    def setUp(self):
        s = Sandbox()
        self.addCleanup(s.close)
        s.write(".windvane/CRITIC.md", SCALE)
        self.s, self.p, self.lad = s, Project(s.root), critic.ladder(SCALE)

    def climbed(self, text):
        entry, _ = critic.climbed(self.p, "T-001", self.s.root, "HEAD", self.lad, text, 0, "ok", 1)
        f = self.p.state / "autopilot.log"
        return entry, [line for line in (f.read_text() if f.is_file() else "").splitlines() if "T-001" in line]

    def test_a_beyond_line_before_the_last_checks_is_logged_once(self):
        text = CHECKS + "- beyond: yes, a later ticket could add retries\n- failures: yes, tested\nCRITIC: T-001 fixes=0 ok"
        entry, logged = self.climbed(text)
        self.assertEqual(entry["unanswered"], ["failures"])
        self.assertEqual(len(logged), 1, logged)
        self.assertIn("`- beyond: yes, a later ticket could add retries` ended its Checks block", logged[0])
        self.assertIn("failures unanswered, counted as no", logged[0])

    def test_nothing_is_logged_when_every_check_is_answered_or_it_is_a_heading(self):
        for tail in ("- failures: yes, tested\n- beyond: yes, add retries\n", "- Beyond: - add retries\n"):
            with self.subTest(tail=tail):
                self.assertEqual(self.climbed(CHECKS + tail + "CRITIC: T-001 fixes=0 ok")[1], [])


if __name__ == "__main__":
    unittest.main()
