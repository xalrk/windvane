"""An approved head sent back only because main moved (a conflict, a red merged tree): when the worker just merges main
and resolves it, the critic's verdict stands and the reviewer sees only the resolution."""
import argparse
import io
import json
import os
import sys
import unittest
from contextlib import redirect_stdout
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from windvane import cli, critic, driver  # noqa: E402
from windvane.config import Project  # noqa: E402
from windvane.util import now, save_json  # noqa: E402
from helpers import Sandbox  # noqa: E402
from test_parallel import args  # noqa: E402


class ConflictBounce(unittest.TestCase):
    def setUp(self):
        self.s = Sandbox()
        self.s.write(".windvane/config.toml", self.s.root.joinpath(".windvane/config.toml").read_text()
                     .replace("enabled = false", "enabled = true"))
        self.s.ticket("T-001")
        self.s.publish()
        self.p = Project(self.s.root)
        pack = critic.pack_dir(self.p, "T-001")
        pack.mkdir(parents=True)
        (pack / "notes.md").write_text("User-facing: yes\nfaster\n")
        os.environ["FAKE_CONFLICT"] = "1"  # the worker writes shared.txt, and merges main when told it conflicts

    def tearDown(self):
        self.s.close()

    def run_pilot(self):
        """The first review approves; meanwhile a desk change to shared.txt lands on main, so the merge conflicts."""
        pilot = driver.Pilot(self.p, args(parallel=1))
        review = pilot.review

        def desk_lands(*a, **kw):
            out = review(*a, **kw)
            if not (self.s.root / "shared.txt").exists():
                self.s.write("shared.txt", "desk\n")
                self.s.publish("desk change")
            return out
        pilot.review = desk_lands
        pilot.run(now() + timedelta(minutes=15))
        self.assertEqual(pilot.merged, ["T-001"])
        return pilot

    def calls(self, kind):
        rows = [json.loads(l) for l in (self.s.tmp / "calls.jsonl").read_text().splitlines()]
        return [r["prompt"] for r in rows if r["system"].startswith(f"# windvane {kind}")]

    def runs(self):
        out = io.StringIO()
        with redirect_stdout(out):
            cli.cmd_stats(argparse.Namespace(days=7, by="ticket", critic=False), self.p)
        line = next(l for l in out.getvalue().splitlines() if l.startswith("T-001"))
        return int(line.split()[2])

    def test_a_resolution_only_bounce_skips_the_critic_and_reviews_only_the_resolution(self):
        os.environ["FAKE_RESOLVE_ONLY"] = "1"
        self.run_pilot()
        self.assertEqual(len(self.calls("critic")), 1)  # the first head's verdict stands
        reviews = self.calls("review")
        self.assertEqual(len(reviews), 2)
        self.assertIn("## The diff\n", reviews[0])
        last = reviews[1]
        self.assertIn("only its merge resolution", last)
        self.assertIn("## The resolution (shared.txt)", last)
        self.assertIn("<<<<<<<", last)  # from git's own merge, markers and all, to the resolved file
        self.assertNotIn("T-001.txt", last)  # the approved change itself isn't shown again
        self.assertNotIn("## The diff\n", last)
        self.assertIn("don't run `windvane critic`", self.calls("worker")[1])
        log = (self.p.state / "autopilot.log").read_text()
        self.assertIn("the critic's verdict stands", log)
        self.assertEqual(self.runs(), 5)  # worker, critic, review; worker, review

    def test_a_bounce_that_also_changes_other_code_gets_the_full_critic_and_review(self):
        self.run_pilot()  # the worker appends to T-001.txt again, beside the merge
        self.assertEqual(len(self.calls("critic")), 2)
        self.assertIn("## The diff\n", self.calls("review")[1])
        self.assertEqual(self.runs(), 6)  # worker, critic, review; worker, critic, review


class Resolution(unittest.TestCase):
    """critic.resolution on a hand-made history: branch `b` approved at A, main moved, the worker merged it."""

    def setUp(self):
        self.s = Sandbox()
        g = self.s.git
        g("switch", "-q", "-c", "b")
        self.s.write("shared.txt", "branch\n")
        self.s.write("own.txt", "mine\n")
        g("add", "-A")
        g("commit", "-qm", "build")
        self.approved = g("rev-parse", "HEAD")
        g("switch", "-q", "main")
        self.s.write("shared.txt", "main\n")
        self.s.write("other.txt", "main's\n")
        g("add", "-A")
        g("commit", "-qm", "desk")
        g("switch", "-q", "b")
        g("merge", "-q", "-X", "ours", "-m", "merge main", "main")

    def tearDown(self):
        self.s.close()

    def res(self, why="conflict"):
        return critic.resolution(self.s.root, self.approved, self.s.git("rev-parse", "HEAD"), "main", why)

    def test_a_merge_of_main_yields_only_its_resolution(self):
        diff, paths, _ = self.res()
        self.assertEqual(paths, ["shared.txt"])
        self.assertNotIn("other.txt", diff)  # main's own change
        self.assertNotIn("own.txt", diff)  # the approved change

    def test_a_fix_in_a_conflicted_file_still_counts(self):
        self.s.write("shared.txt", "both\n")
        self.s.git("commit", "-qam", "fix")
        self.assertEqual(self.res()[1], ["shared.txt"])

    def test_other_code_or_no_merge_needs_the_full_round(self):
        self.s.write("own.txt", "more\n")
        self.s.git("commit", "-qam", "more")
        self.assertIsNone(self.res())
        self.assertEqual(self.res("red")[1], ["own.txt", "shared.txt"])  # a red tree's fix may touch either side
        self.s.write("new.txt", "x\n")
        self.s.git("add", "-A")
        self.s.git("commit", "-qm", "new")
        self.assertIsNone(self.res("red"))
        self.assertIsNone(critic.resolution(self.s.root, self.approved, self.approved, "main"))

    def test_a_merge_of_another_branch_needs_the_full_round(self):
        g = self.s.git
        g("switch", "-q", "-c", "side", self.approved)
        self.s.write("side.txt", "x\n")
        g("add", "-A")
        g("commit", "-qm", "side")
        g("switch", "-q", "b")
        g("merge", "-q", "-m", "merge side", "side")
        self.assertIsNone(self.res())

    def test_the_critic_command_lets_a_resolution_through_without_a_session(self):
        p = Project(self.s.root)
        p.critic["enabled"] = True
        save_json(p.state / "state.json", {"approved": {"T-001": {"sha": self.approved, "why": "conflict"}}})
        self.s.git("update-ref", "refs/remotes/origin/main", "main")
        out = io.StringIO()
        cwd = os.getcwd()
        os.chdir(self.s.root)
        try:
            with redirect_stdout(out):
                cli.cmd_critic(argparse.Namespace(ticket="T-001"), p)
        finally:
            os.chdir(cwd)
        self.assertIn("CRITIC STEP: pass. The verdict on", out.getvalue())
        self.assertFalse((self.s.tmp / "calls.jsonl").exists())  # no critic session ran


if __name__ == "__main__":
    unittest.main()
