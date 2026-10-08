import argparse
import io
import os
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from windvane import cli, critic, stats  # noqa: E402
from windvane.config import Project  # noqa: E402
from windvane.util import read_jsonl  # noqa: E402
from helpers import Sandbox  # noqa: E402

SCALE = """# Scale
## works
- 2 shown: the promise is shown.
- 5 safe: no known crash.
- 8 tested: failure paths tested.
## cost
- 5 safe: no wasted reruns.
- 8 quiet: output cut to what's needed.
"""
GOOD = "Checks:\n- shown: yes, a transcript\n- works safe: yes, no crash\n- tested: yes\n- cost safe: yes\n- quiet: yes\nFine.\n" \
       "CRITIC: {tid} fixes=0 fine"
UNREAD = "Checks:\nThe demo works and overall it's safe: yes\nCRITIC: {tid} fixes=0 fine"


class Reask(unittest.TestCase):
    """T-116, through `windvane critic` and the Sandbox's fake `claude`: answers only from bulleted lines, a shared id
    told apart by its dimension, one re-ask in the same session before no verdict, and every no-verdict end logged."""

    def setUp(self):
        s = Sandbox()
        self.addCleanup(s.close)
        s.write(".windvane/config.toml", s.root.joinpath(".windvane/config.toml").read_text()
                .replace("enabled = false", "enabled = true"))
        s.write(".windvane/CRITIC.md", SCALE)
        s.ticket("T-001")
        s.publish("critic on, a shared id")
        s.git("switch", "-q", "-c", "t-001-x")
        self.s, self.p = s, Project(s.root)
        notes = critic.pack_dir(self.p, "T-001") / "notes.md"
        notes.parent.mkdir(parents=True)
        notes.write_text("User-facing: yes\nfaster\n")
        old = os.getcwd()
        os.chdir(s.root)
        self.addCleanup(os.chdir, old)

    def critic(self, first, again=None):
        """`windvane critic T-001` with these replies: (its output, the critic sessions it ran, critic.jsonl)."""
        os.environ["FAKE_CRITIC_TEXT"] = first
        if again:
            os.environ["FAKE_CRITIC_REASK"] = again
        out = io.StringIO()
        with redirect_stdout(out):
            try:
                cli.cmd_critic(argparse.Namespace(ticket="T-001", project=False), self.p)
            except SystemExit:
                pass
        calls = [c for c in read_jsonl(self.s.tmp / "calls.jsonl") if c["system"].startswith("# windvane critic")]
        return out.getvalue(), calls, read_jsonl(self.p.state / "critic.jsonl")

    def test_a_prose_line_answers_nothing(self):
        text = GOOD.replace("- works safe: yes, no crash\n", "overall it's safe: yes\n")
        _, _, recs = self.critic(text)
        self.assertEqual(recs[-1]["unanswered"], ["safe"], "only the bulleted cost line answered, and only cost")
        self.assertEqual(recs[-1]["dims"], {"works": 2, "cost": 8})
        self.assertNotIn("safe", critic.answers("Checks:\noverall it's safe: yes\nCRITIC: T-1 fixes=0 g",
                                                critic.ladder(SCALE)))

    def test_each_dimension_scores_from_its_own_line(self):
        _, _, recs = self.critic(GOOD.replace("- works safe: yes, no crash", "- works safe: no, it crashes"))
        self.assertEqual((recs[-1]["dims"], recs[-1]["failed"]), ({"works": 2, "cost": 8}, ["safe"]))
        _, _, recs = self.critic(GOOD.replace("- cost safe: yes", "- cost safe: no, reruns"))
        self.assertEqual((recs[-1]["dims"], recs[-1]["failed"]), ({"works": 8, "cost": 0}, ["safe"]))

    def test_the_id_alone_answers_every_dimension_not_answered_yet(self):
        ans = critic.answers("- cost safe: no, reruns\n- safe: yes\nCRITIC: T-1 fixes=0 g", critic.ladder(SCALE))
        self.assertEqual(ans, {("cost", "safe"): ("no", "reruns"), ("works", "safe"): ("yes", "")})

    def test_a_reask_in_the_same_session_gives_the_verdict(self):
        out, calls, recs = self.critic(UNREAD, GOOD)
        self.assertEqual(len(calls), 2, "the first run and one re-ask")
        self.assertNotIn("--resume", calls[0]["flags"])
        self.assertEqual(calls[1]["flags"][calls[1]["flags"].index("--resume") + 1], "fake-session")
        self.assertIn("- <id>: yes|no", calls[1]["prompt"])
        self.assertEqual([r["step"] for r in recs], ["pass"])
        self.assertIn("CRITIC STEP: pass", out)

    def test_two_unreadable_replies_are_no_verdict_logged_once(self):
        out, calls, recs = self.critic(UNREAD, UNREAD)
        self.assertEqual(len(calls), 2)
        self.assertIn("CRITIC STEP: none. No verdict: no answer the driver could match", out)
        self.assertEqual([(r["step"], r["reasked"]) for r in recs], [("none", True)])
        self.assertIn("labels it saw: The demo works and overall it's safe", recs[0]["why"])
        self.assertEqual(critic.history(self.p, "T-001"), [], "a log line, not a verdict")
        self.assertEqual(critic.followups(self.p), [])
        with redirect_stdout(io.StringIO()) as shown:
            stats.critic_stats(argparse.Namespace(last=10, since=None), self.p)
        self.assertIn("no critic verdicts yet", shown.getvalue())


if __name__ == "__main__":
    unittest.main()
