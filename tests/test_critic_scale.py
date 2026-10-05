import argparse
import io
import sys
import unittest
from argparse import Namespace
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from windvane import cli, critic, driver  # noqa: E402
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


LADDER = """# What a 10 means
## works
- 2 shown: the promise is shown.
- 5 safe: no known crash.
- 8 failures: failure paths exercised.
- 10 class: removes a class.
"""


def verdicts(n=10):
    """`n` tickets' verdicts: `shown` yes on every first head, `class` never yes, `safe` no on the first head of every
    other ticket and yes after its revision, `failures` no on two of every four."""
    out = []
    for i in range(n):
        first = {"shown": "yes", "safe": "no" if i % 2 else "yes", "failures": "no" if i % 4 < 2 else "yes",
                 "class": "no"}
        out.append({"ticket": f"T-1{i:02}", "score": 2 if i % 2 else 5, "step": "revise" if i % 2 else "scope",
                    "answers": first})
        if i % 2:
            out.append({"ticket": f"T-1{i:02}", "score": 5, "step": "scope", "answers": {**first, "safe": "yes"}})
    return out


class CheckRates(unittest.TestCase):
    """T-054: each check's yes rate over the last 10 tickets replaces the collapse line: a check every first head
    passes is too easy, one no head ever passes can't be reached."""

    def setUp(self):
        s = Sandbox()
        self.addCleanup(s.close)
        s.write(".windvane/config.toml", s.root.joinpath(".windvane/config.toml").read_text()
                .replace("enabled = false", "enabled = true"))
        s.write(".windvane/CRITIC.md", LADDER)
        s.publish("critic on, as ladders")
        self.s, self.p = s, Project(s.root)

    def record(self, recs):
        for r in recs:
            append_jsonl(self.p.state / "critic.jsonl", {"sha": "abc1234", "fixes": 0, "gist": "g", **r})

    def run_cli(self, fn, **kw):
        out = io.StringIO()
        with redirect_stdout(out):
            fn(argparse.Namespace(**kw), self.p)
        return out.getvalue()

    def test_rates_per_check(self):
        rates = critic.rates(verdicts(12))
        self.assertEqual(rates["shown"], (10, 10, 10), "only the last 10 tickets")
        self.assertEqual(rates["safe"], (5, 10, 10), "first heads, and any head")
        self.assertEqual(rates["class"], (0, 0, 10))
        self.assertEqual(rates["failures"], (6, 6, 10))

    def test_a_check_yes_on_every_ticket_and_one_no_on_every_ticket_are_named(self):
        rates = critic.rates(verdicts())
        self.assertEqual(critic.advice(*rates["shown"]), "yes on every ticket: sharpen it")
        self.assertEqual(critic.advice(*rates["class"]), "never yes: lower it")
        self.assertEqual(critic.advice(*rates["safe"]), "", "no on first heads but reached by revisions")
        self.assertEqual(critic.advice(*rates["failures"]), "")
        self.assertEqual(critic.advice(4, 4, 4), "", "too few tickets to tell")

    def test_untouched_checks_and_numeric_verdicts_dont_count(self):
        recs = [{"ticket": f"T-2{i}", "score": 8, "answers": {"shown": "yes"}} for i in range(6)]
        recs += [{"ticket": "T-299", "score": 7, "dims": {"score": 7}}, {"ticket": "PLAN", "answers": {"shown": "no"}}]
        self.assertEqual(critic.rates(recs), {"shown": (6, 6, 6)})

    def test_stats_and_overview_name_them(self):
        self.record(verdicts())
        stats = self.run_cli(cli.cmd_stats, critic=True, last=10)
        self.assertIn("checks, yes on the first head / on any head, of the tickets that touched them:", stats)
        self.assertIn("  works 2   shown      10/10 10/10 yes on every ticket: sharpen it\n", stats)
        self.assertIn("  works 5   safe        5/10 10/10\n", stats)
        self.assertIn("  works 10  class       0/10  0/10\n", stats, "a 10 is rare by design (T-055)")
        self.assertNotIn("collapsed", stats)
        self.assertNotIn("cap checks answered", stats)
        over = self.run_cli(cli.cmd_overview)
        self.assertIn("Critic checks, last 10 tickets: works.shown yes on every ticket: sharpen it.\n", over)

    def test_overview_says_once_that_an_old_scale_is_the_old_kind(self):
        self.s.write(".windvane/CRITIC.md", "# Scale\n- **works**: 2: happy path. 5: the CLI. 8: failure paths.\n")
        self.record([{"ticket": "T-001", "score": 7, "step": "scope", "dims": {"works": 7}}])
        over = self.run_cli(cli.cmd_overview)
        self.assertEqual(over.count("Critic scale is the old numeric kind; to convert it, rewrite .windvane/CRITIC.md "
                                    "as ladders of yes/no checks, like "), 1)
        self.assertIn("templates/CRITIC.md.", over)
        self.assertIn("cap checks answered", self.run_cli(cli.cmd_stats, critic=True, last=10))

    def test_the_gate_logs_an_unanswered_check(self):
        self.record([{"ticket": "T-005", "score": 8, "step": "pass", "dims": {"works": 8},
                      "answers": {"shown": "yes", "safe": "yes", "failures": "yes", "class": "none"},
                      "unanswered": ["class"]}])
        pilot = driver.Pilot(self.p, Namespace(cap=75, week_cap=95, ticket_hours=1, min_slot=0, answer_wait=10,
                                               replan=0, goal="", parallel=1))
        with redirect_stdout(io.StringIO()):
            self.assertTrue(pilot.critic_gate("T-005", "abc1234def"))
        log = (self.p.state / "autopilot.log").read_text()
        self.assertIn("T-005: critic 8/10 (works=8) (pass)", log)
        self.assertIn("T-005: the critic left class unanswered (counted as no)", log)


if __name__ == "__main__":
    unittest.main()
