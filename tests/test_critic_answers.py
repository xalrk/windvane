import argparse
import io
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from windvane import cli, critic  # noqa: E402
from windvane.config import Project  # noqa: E402
from windvane.util import append_jsonl  # noqa: E402
from helpers import Sandbox  # noqa: E402

CRITIQUE = """Checks:
- shown: yes, the transcript shows a restart
- failures: no, the timeout path has no test
{heading}
- safe: no test covers the restart
- works.class: yes it removes the class
CRITIC: T-055 fixes=1 restart untested"""

SHARED = """# What a 10 means
## works
- 2 shown: the promise is shown.
- 5 safe: no known crash.
- 8 failures: failure paths tested.
- 10 class: a class of failure gone.
## lean
- 5 safe: no wasted reruns.
- 8 quiet: output cut to what's needed.
"""


class Answers(unittest.TestCase):
    """T-055: the driver scores the critic's answers, so only its Checks block may answer a check."""

    def test_a_follow_up_list_never_answers_a_check_the_checks_block_left_out(self):
        for heading in ("Fix in this ticket:", "Beyond this ticket:", "**Beyond this ticket:**", "## Fix in this ticket"):
            with self.subTest(heading=heading):
                ans = critic.answers(CRITIQUE.format(heading=heading))
                self.assertNotIn("safe", ans)
                self.assertNotIn("class", ans)
                self.assertEqual(ans["shown"][0], "yes")
                self.assertEqual(ans["failures"], ("no", "the timeout path has no test"))

    def test_without_the_lists_every_answer_still_counts(self):
        ans = critic.answers(CRITIQUE.format(heading="Also:"))
        self.assertEqual(ans["safe"], ("no", "test covers the restart"))

    def test_evidence_is_cut_at_a_word(self):
        long = "the restart path " * 20
        ev = critic.answers(f"Checks:\n- safe: no, {long}\nCRITIC: T-055 fixes=0 x")["safe"][1]
        self.assertLessEqual(len(ev), 200)
        self.assertTrue(ev.endswith("…"), ev)
        self.assertIn(ev[:-1].split()[-1], ("the", "restart", "path"))
        self.assertTrue(long.startswith(ev[:-1]))
        self.assertEqual(critic.clip("short enough"), "short enough")
        self.assertEqual(critic.clip("x" * 300), "x" * 199 + "…")
        self.assertEqual(critic.clip("a, " + "b" * 300)[:3], "a…", "trailing punctuation goes before the …")


class Advice(unittest.TestCase):
    """T-055: a 10 is rare by design, so never reaching it isn't advice."""

    def test_no_never_yes_on_a_dimensions_top_rung(self):
        self.assertEqual(critic.advice(0, 0, 10, top=True), "")
        self.assertEqual(critic.advice(0, 0, 10), "never yes: lower it")
        self.assertEqual(critic.advice(10, 10, 10, top=True), "yes on every ticket: sharpen it")


class Shared(unittest.TestCase):
    """T-055: a check id two dimensions share is named where the scale is checked, never a crash."""

    def setUp(self):
        s = Sandbox()
        self.addCleanup(s.close)
        s.write(".windvane/config.toml", s.root.joinpath(".windvane/config.toml").read_text()
                .replace("enabled = false", "enabled = true"))
        s.write(".windvane/CRITIC.md", SHARED)
        s.publish("critic on, a shared id")
        self.p = Project(s.root)

    def run_cli(self, fn, **kw):
        out = io.StringIO()
        with redirect_stdout(out):
            fn(argparse.Namespace(**kw), self.p)
        return out.getvalue()

    def test_shared_ids_are_found(self):
        self.assertEqual(critic.shared(critic.ladder(SHARED)), {"safe": ["works", "lean"]})
        self.assertEqual(critic.shared(critic.ladder(SHARED.replace("5 safe: no wasted", "5 lean: no wasted"))), {})

    def test_overview_and_stats_name_them(self):
        line = ("Critic scale reuses check ids, so one answer scores both: safe (works, lean); rename one in "
                ".windvane/CRITIC.md.")
        self.assertIn(line + "\n", self.run_cli(cli.cmd_overview))
        append_jsonl(self.p.state / "critic.jsonl", {"ticket": "T-001", "score": 5, "step": "scope", "sha": "abc",
                                                     "fixes": 0, "answers": {"shown": "yes", "safe": "yes"}})
        self.assertIn(line + "\n", self.run_cli(cli.cmd_stats, critic=True, last=10))

    def test_top_rung_advice_follows_each_dimensions_own_top(self):
        for i in range(6):
            append_jsonl(self.p.state / "critic.jsonl", {
                "ticket": f"T-0{i}", "score": 5, "step": "scope", "sha": "abc", "fixes": 0,
                "answers": {"shown": "yes" if i else "no", "safe": "yes", "failures": "no", "class": "no",
                            "quiet": "no"}})
        over = self.run_cli(cli.cmd_overview)
        self.assertIn("works.failures never yes: lower it", over)
        self.assertNotIn("class never", over)
        self.assertNotIn("quiet never", over, "lean's top rung is 8")


if __name__ == "__main__":
    unittest.main()
