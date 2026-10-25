"""The run's last push leads with what needs the user (questions, provisional answers, failures, the config fixes) and
ends with what merged, as ids, and the critic's trail: no title shows twice, and a push past ntfy's limit loses its
end, never a line the user has to act on."""
import re
import sys
import unittest
from argparse import Namespace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from windvane import driver  # noqa: E402
from windvane.config import Project  # noqa: E402
from windvane.phone import NTFY  # noqa: E402
from windvane.queue import Queue  # noqa: E402
from windvane.util import append_jsonl  # noqa: E402
from helpers import Sandbox  # noqa: E402

PROVISIONAL = ("# Q\n\n## Open\n\n## Provisional\n\n### T-004: Which colour?\nctx\n- **A**: red\n- **B**: blue\n"
               "Recommendation: A, because it's louder\nBuilt: A; to change it: COLOUR in theme.py\n\n## Answered\n")
ORDER = ["Why:", "Waiting on you:", "Provisional answers to check:", "Failed:", "Set aside:", "Heavy jobs waited",
         "Merged ", "Critic (/10"]


class Base(unittest.TestCase):
    def setUp(self):
        self.s = Sandbox()
        self.addCleanup(self.s.close)

    def tickets(self, n, title=lambda i: f"Title of ticket {i}"):
        for i in range(1, n + 1):
            self.s.write(f".windvane/tickets/T-{i:03}.md", f"# T-{i:03}: {title(i)}\nDepends on: nothing.\n")
        self.s.write(".windvane/QUESTIONS.md", PROVISIONAL)
        self.s.publish()
        self.p = Project(self.s.root)

    def pilot(self, merged):
        pilot = driver.Pilot(self.p, Namespace(cap=75, week_cap=95, ticket_hours=1, min_slot=0, answer_wait=10,
                                               replan=0, goal="", parallel=1))
        pilot.queue = Queue(self.p)
        pilot.stop_reason = "queue empty"
        pilot.merged = list(merged)
        pilot.failed = ["T-002"]
        pilot.questions = {"T-003": {"ticket": "T-003", "q": "Which?", "opts": {"A": "a", "B": "b"}}}
        pilot.aside = {"T-005": "3 runs in a row ran out of time"}
        pilot.heavy_waits = {"/x/a.log": [0, 2400, ["GradleDaemon"], "6"]}
        pilot.note_heavy = lambda tid=None: None  # no real job logs: the wait above is the run's
        for t in merged:
            append_jsonl(self.p.state / "critic.jsonl", {"ticket": t, "sha": "abc1234", "score": 6, "step": "revise"})
            append_jsonl(self.p.state / "critic.jsonl", {"ticket": t, "sha": "abc1234", "score": 8, "step": "pass"})
            pilot.scores[t] = 8
        return pilot

    def lines(self, text):
        return [next((k for k in ORDER if line.startswith(k)), line) for line in text.splitlines()]


class Order(Base):
    def setUp(self):
        super().setUp()
        self.tickets(9)

    def test_what_needs_you_leads_and_the_merges_follow_as_ids(self):
        pilot = self.pilot(["T-001", "T-006", "T-007"])
        append_jsonl(self.p.state / "critic.jsonl", {"ticket": "T-007", "sha": "def5678", "score": 7, "step": "accept"})
        text = pilot.summary()
        self.assertEqual([k for k in self.lines(text) if k in ORDER], ORDER, text)
        self.assertIn("Waiting on you: Title of ticket 3 (T-003).\n", text)
        self.assertIn("Provisional answers to check: T-004 (built A: red).", text)
        self.assertIn("Failed: Title of ticket 2 (T-002).\n", text)
        self.assertIn("Set aside: Title of ticket 5 (T-005) (3 runs in a row ran out of time).\n", text)
        self.assertIn("\nMerged 3: T-001, T-006, T-007 (under the critic's bar).\n", text)
        self.assertNotIn("Merged under", text)  # one list, marked, not a second one
        self.assertTrue(text.endswith("\nCritic (/10, first try→final): T-001 6→8, T-006 6→8, T-007 6→8."), text)
        for i in range(1, 10):
            self.assertLessEqual(text.count(f"Title of ticket {i} "), 1, text)
        for i in (1, 6, 7):
            self.assertNotIn(f"Title of ticket {i} ", text)  # merged: its title is in git log

    def test_a_ticket_named_twice_shows_its_title_once(self):
        pilot = self.pilot([])
        pilot.stopped = ["T-002"]  # failed, then its rerun stopped at the deadline
        text = pilot.summary()
        self.assertEqual(text.count("Title of ticket 2"), 1, text)
        self.assertIn("Stopped at the deadline (resumes next run): T-002.\n", text)

    def test_no_merges_says_none(self):
        self.assertIn("\nMerged: none.", self.pilot([]).summary())


class Fit(Base):
    """Built past 5 KB: 595 merged tickets and their critic trail, and long titles on what needs the user."""

    def setUp(self):
        super().setUp()
        self.tickets(600, lambda i: f"A long title for ticket {i}, " + "with many words in it " * 4)

    def test_a_5kb_summary_fits_with_what_needs_you_whole(self):
        pilot = self.pilot([f"T-{i:03}" for i in range(6, 601)])
        whole = pilot.summary(most=10 ** 6)
        self.assertGreater(len(whole.encode()), 5000)
        text = pilot.summary()
        self.assertLessEqual(len(text.encode()), NTFY)
        for line in whole.splitlines():  # everything above the merges comes through as it was
            if line.startswith("Merged "):
                break
            self.assertIn(line + "\n", text)
        self.assertIn("Heavy jobs waited 40 min on memory", text)
        self.assertNotIn("Critic (/10", text)  # the end went first
        merged = re.search(r"^Merged 595: T-006, .*, T-\d+ and (\d+) more\.$", text, re.M)
        self.assertTrue(merged, text)
        self.assertTrue(text.endswith(merged.group(0)), text)

    def test_what_needs_you_loses_titles_before_any_line(self):
        pilot = self.pilot([])
        pilot.failed = [f"T-{i:03}" for i in range(10, 60)]  # 50 long titles: past the limit on their own
        text = pilot.summary()
        self.assertLessEqual(len(text.encode()), NTFY)
        self.assertIn("Failed: " + "; ".join(pilot.failed) + ".\n", text)
        self.assertIn("Waiting on you: T-003.\n", text)
        self.assertIn("Heavy jobs waited", text)

    def test_titles_outlast_every_merge_id(self):
        pilot = self.pilot([f"T-{i:03}" for i in range(60, 301)])
        pilot.failed = [f"T-{i:03}" for i in range(10, 60)]
        text = pilot.summary()
        self.assertIn("Failed: " + "; ".join(pilot.failed) + ".\n", text)
        self.assertTrue(text.endswith("\nMerged 241."), text)


if __name__ == "__main__":
    unittest.main()
