"""isolate = false: the checkout is the autopilot's tree, and what someone left uncommitted there is stashed, never
deleted, before a periodic round or a desk PR cleans it."""
import json
import sys
import unittest
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from windvane import desk, driver  # noqa: E402
from windvane.config import Project  # noqa: E402
from windvane.util import now  # noqa: E402
from helpers import Sandbox  # noqa: E402
from test_desk import FAKE_GH  # noqa: E402
from test_pipeline import args  # noqa: E402

TRIAGE = '[[periodic]]\nname = "triage"\nminutes = 30\nprompt = "tools/TRIAGE.md"\n'


class InPlace(unittest.TestCase):
    def make(self, isolate=False, forge="git"):
        self.s = Sandbox(TRIAGE)
        self.addCleanup(self.s.close)
        cfg = self.s.root / ".windvane/config.toml"
        cfg.write_text(cfg.read_text().replace('forge = "git"', f'forge = "{forge}"'
                                               + ("" if isolate else "\nisolate = false")))
        self.s.write("tools/TRIAGE.md", "# Triage\nWRITE_TICKET\n")
        self.s.ticket("T-001")
        self.s.publish("setup")
        self.p = Project(self.s.root)
        self.pushes = []
        for mod in (driver, desk):
            orig = mod.push
            mod.push = lambda title, *a, **k: self.pushes.append(title)
            self.addCleanup(setattr, mod, "push", orig)
        self.pilot = driver.Pilot(self.p, args())
        self.pilot.deadline = now() + timedelta(hours=1)
        self.assertTrue(self.pilot.prepare(0))

    def desk_edits(self):
        """What someone at the desk leaves in the checkout: a new file and an edit to a tracked one."""
        self.s.write("notes.txt", "my notes\n")
        self.s.write("README", "hi, edited at the desk\n")

    def stashes(self):
        return self.s.git("stash", "list", "--format=%s").splitlines()

    def assert_kept(self, why):
        """One stash named for `why`, holding both desk edits, and the checkout clean."""
        [msg] = self.stashes()
        self.assertRegex(msg, rf"windvane kept: \d{{4}}-\d\d-\d\d \d\d:\d\d \({why}\)$")
        self.assertFalse((self.s.root / "notes.txt").exists())
        self.s.git("stash", "apply", "-q")
        self.assertEqual((self.s.root / "notes.txt").read_text(), "my notes\n")
        self.assertEqual((self.s.root / "README").read_text(), "hi, edited at the desk\n")
        self.assertIn("sandbox: your changes were stashed", self.pushes)

    def periodic_sessions(self):
        f = self.s.tmp / "calls.jsonl"
        return [c for c in map(json.loads, f.read_text().splitlines() if f.exists() else [])
                if c["system"].startswith("# windvane periodic")]

    def test_a_periodic_round_stashes_the_desk_edits(self):
        self.make()
        self.desk_edits()
        self.pilot.periodic(self.p.periodic["triage"])
        self.assertEqual(len(self.periodic_sessions()), 1)
        self.assert_kept("the triage round")
        self.assertIn("stashed the checkout's uncommitted changes", (self.p.state / "autopilot.log").read_text())

    def test_a_desk_pr_check_stashes_the_desk_edits(self):
        self.make(forge="github")
        gh = self.s.tmp / "gh.json"
        gh.write_text(json.dumps({"prs": []}))
        fake = self.s.bin / "gh"
        fake.write_text(f"#!/bin/sh\nexec python3 - {gh} {self.s.remote} \"$@\" <<'EOF'\n{FAKE_GH}EOF\n")
        fake.chmod(0o755)
        self.s.git("switch", "-q", "-c", "desk-1")
        self.s.write("feature.txt", "new\n")
        self.s.git("add", "-A")
        self.s.git("commit", "-qm", "feature")
        self.s.git("push", "-q", "origin", "desk-1")
        self.s.git("switch", "-q", "main")
        gh.write_text(json.dumps({"prs": [{"number": 1, "title": "desk change", "author": {"login": "me"},
                                           "state": "OPEN", "headRefName": "desk-1", "isCrossRepository": False,
                                           "isDraft": False, "labels": [{"name": "windvane merge"}]}]}))
        self.desk_edits()
        [pr] = self.pilot.desk_due()
        self.pilot.desk_one(pr)
        self.assertEqual(json.loads(gh.read_text())["prs"][0]["state"], "MERGED")
        self.assert_kept("checking desk PR #1")

    def test_a_clean_checkout_makes_no_stash(self):
        self.make()
        self.pilot.periodic(self.p.periodic["triage"])
        self.assertEqual(len(self.periodic_sessions()), 1)
        self.assertEqual(self.stashes(), [])
        self.assertEqual(self.pushes, [])

    def test_a_failed_stash_skips_the_round_and_deletes_nothing(self):
        self.make()
        self.desk_edits()
        lock = self.s.root / ".git/index.lock"
        lock.write_text("")
        for _ in range(2):
            self.pilot.periodic(self.p.periodic["triage"])
        lock.unlink()
        self.assertEqual(self.periodic_sessions(), [])
        self.assertEqual(self.stashes(), [])
        self.assertEqual((self.s.root / "notes.txt").read_text(), "my notes\n")
        self.assertEqual((self.s.root / "README").read_text(), "hi, edited at the desk\n")
        self.assertEqual(self.pushes, ["sandbox: checkout not cleaned"])  # pushed once, logged each time
        self.assertEqual((self.p.state / "autopilot.log").read_text().count("couldn't stash"), 2)

    def test_with_isolate_on_the_ci_worktree_is_cleaned_and_the_checkout_left_alone(self):
        self.make(isolate=True)
        self.desk_edits()
        (self.p.ci_dir / "leftover.txt").write_text("from an earlier session\n")
        self.pilot.periodic(self.p.periodic["triage"])
        self.assertEqual(len(self.periodic_sessions()), 1)
        self.assertFalse((self.p.ci_dir / "leftover.txt").exists())
        self.assertEqual(self.stashes(), [])
        self.assertEqual(self.pushes, [])
        self.assertEqual((self.s.root / "notes.txt").read_text(), "my notes\n")
        self.assertEqual((self.s.root / "README").read_text(), "hi, edited at the desk\n")


if __name__ == "__main__":
    unittest.main()
