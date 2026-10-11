"""A ticket scores its lowest dimension, and workers revise until they pass: on a scale with a ladder that stops at
the pass mark, every merged ticket gets that one score, and the final number alone hides what the critic caught."""
import io
import os
import sys
import unittest
from contextlib import ExitStack, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from windvane import cli, critic, driver  # noqa: E402
from windvane.config import DEFAULTS, Project  # noqa: E402
from windvane.queue import Queue  # noqa: E402
from windvane.util import append_jsonl  # noqa: E402
from windvane.worktree import ensure_worktree  # noqa: E402
from helpers import Sandbox  # noqa: E402
from test_pipeline import args  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
CAPPED = """# Scale
## works
- 2 shown: it works.
- 10 class: it removes a class of failure.
## clear
- 2 honest: nothing lies.
- 8 awake: readable half awake.
"""


class Ceilings(unittest.TestCase):
    def test_a_ladder_under_10_is_named(self):
        lad = critic.ladder(CAPPED)
        self.assertEqual(critic.ceilings(lad), {"clear": 8.0})
        p = SimpleNamespace(critic={"pass": 8}, rubric_rel=".windvane/CRITIC.md")
        self.assertEqual(critic.ceiling_line(p, lad),
                         "Critic scale: clear tops out at 8, so no ticket can score above 8 (the pass mark, so every "
                         "merged ticket scores it): give it a 10 rung in .windvane/CRITIC.md.")
        self.assertEqual(critic.ceiling_line(p, critic.ladder(CAPPED.replace("- 8 awake", "- 10 awake"))), "")

    def test_the_shipped_scale_reaches_10_above_the_pass_mark(self):
        lad = critic.ladder((REPO / "templates/CRITIC.md").read_text())
        self.assertTrue(lad)
        self.assertEqual(critic.ceilings(lad), {})
        for d, cs in lad.items():
            self.assertTrue([r for r, _, _ in cs if r > DEFAULTS["critic"]["pass"]], f"{d}: a rung above the bar")

    def test_rungs_over_the_bar_are_not_told_to_lower(self):
        self.assertEqual(critic.advice(0, 0, 10, top=True), "")
        self.assertEqual(critic.advice(0, 0, 10, top=False), "never yes: lower it")

    def test_trail(self):
        self.assertEqual(critic.trail(2.0, 8.0), "2→8")
        self.assertEqual(critic.trail(8.0, 8.0), "8")
        self.assertEqual(critic.trail(None, 9.0), "9")


class Shown(unittest.TestCase):
    def setUp(self):
        self.s = Sandbox()
        self.addCleanup(self.s.close)
        self.s.ticket("T-001")
        self.s.ticket("T-002")
        self.s.publish()
        self.p = Project(self.s.root)
        self.sent = []
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(mock.patch.object(driver, "push",
                                              lambda title, msg="", *a, **k: self.sent.append((title, msg)) or True))
        self.addCleanup(os.chdir, os.getcwd())
        for tid, score, step in (("T-001", 2, "revise"), ("T-001", 8, "pass"), ("T-002", 9, "pass")):
            append_jsonl(self.p.state / "critic.jsonl", {"ticket": tid, "score": score, "step": step, "fixes": 0})

    def pilot(self):
        ensure_worktree(self.p, self.p.work_dir, setup=False)
        pl = driver.Pilot(self.p, args())
        pl.queue = Queue(self.p)
        pl.scores = {"T-001": 8.0, "T-002": 9.0}
        return pl

    def test_the_summary_shows_first_try_and_final(self):
        self.assertIn("Critic (/10, first try→final): T-001 2→8, T-002 9.\n", self.pilot().summary())

    def test_the_done_push_shows_the_first_try(self):
        pl = self.pilot()
        pl.merged_ok("T-001", "", self.s.git("rev-parse", "HEAD"))
        pl.merged_ok("T-002", "", self.s.git("rev-parse", "HEAD"))
        done = dict(self.sent)
        self.assertIn("Built, checked, reviewed and merged. Critic 8/10 (first try 2).", done["Done: do T-001 (T-001)"])
        self.assertIn("Built, checked, reviewed and merged. Critic 9/10. ", done["Done: do T-002 (T-002)"])

    def test_overview_warns_about_a_capped_scale(self):
        self.s.write(".windvane/CRITIC.md", CAPPED)
        cfg = self.s.root / ".windvane/config.toml"
        cfg.write_text(cfg.read_text().replace("enabled = false", "enabled = true"))
        out = io.StringIO()
        with redirect_stdout(out):
            cli.cmd_overview(None, Project(self.s.root))
        self.assertIn("Critic scale: clear tops out at 8, so no ticket can score above 8", out.getvalue())


if __name__ == "__main__":
    unittest.main()
