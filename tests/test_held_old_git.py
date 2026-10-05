"""T-092: before git 2.38 (no `merge-tree --write-tree`), a held change squash-merged from several commits drops off
`held_now` by its whole change's patch-id, which `git cherry` alone misses; one only half on upstream stays listed.
`status` prunes a hand-pushed change's remote branch once it's deleted, so its line reads as never pushed again."""
import json
import shlex
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from windvane import held, planner  # noqa: E402
from test_held_age import HOOK  # noqa: E402
import test_held_merged  # noqa: E402
from test_plan_retry import Base  # noqa: E402


class HeldOldGit(Base):
    windvane = test_held_merged.HeldMerged.windvane
    hand_pushed_land = test_held_merged.HeldMerged.hand_pushed_land
    on_remote = test_held_merged.HeldMerged.on_remote

    def setUp(self):
        super().setUp()
        self.other = self.s.tmp / "other"
        orig = held.git
        self.addCleanup(setattr, held, "git", orig)
        held.git = lambda cwd, *a: (subprocess.CompletedProcess(a, 129, "", "error: unknown option `write-tree'")
                                    if a[:2] == ("merge-tree", "--write-tree") else orig(cwd, *a))

    def three_commits(self):
        """A held change pushed by hand, grown to three commits (pushed too, as its local branch): its branch."""
        branch, dev = self.hand_pushed_land(), self.s.tmp / "dev"
        subprocess.run(["git", "clone", "-q", "-b", f"{branch}-b", str(self.s.remote), str(dev)], check=True,
                       capture_output=True)
        for n in (2, 3):
            (dev / f"PART{n}").write_text(f"part {n}\n")
            self.s.git("add", f"PART{n}", cwd=dev)
            self.s.git("commit", "-qm", f"part {n}", cwd=dev)
        self.s.git("push", "-q", "origin", f"HEAD:{branch}-b", cwd=dev)
        self.s.git("fetch", "-q", "origin")
        self.s.git("branch", "-f", branch, f"origin/{branch}-b")
        self.assertEqual(self.s.git("rev-list", "--count", f"origin/main..{branch}"), "3")
        return branch

    def squash_onto_main(self, rev):
        self.on_remote(["merge", "-q", "--squash", rev], ["commit", "-qm", "squashed (#7)"],
                       ["push", "-q", "origin", "main"])
        self.s.git("fetch", "-q", "origin")

    def test_a_squash_of_three_commits_drops_off(self):
        branch = self.three_commits()
        self.squash_onto_main(f"origin/{branch}-b")
        self.assertEqual(held.git(self.s.root, "merge-tree", "--write-tree", "a", "b").returncode, 129)
        self.assertIn("+", self.s.git("cherry", "origin/main", branch))  # cherry alone keeps it listed
        self.assertEqual(held.held_now(self.p), [])

    def test_a_change_half_on_main_stays_listed(self):
        branch = self.three_commits()
        self.squash_onto_main(f"origin/{branch}-b~1")
        self.assertEqual([b for b, *_ in held.held_now(self.p)], [branch])

    def test_the_walk_is_bounded(self):
        branch = self.three_commits()
        self.squash_onto_main(f"origin/{branch}-b")
        (self.other / "LATER").write_text("later\n")
        for step in (["add", "LATER"], ["commit", "-qm", "later"], ["push", "-q", "origin", "main"]):
            self.s.git(*step, cwd=self.other)
        self.s.git("fetch", "-q", "origin")
        self.addCleanup(setattr, held, "SQUASH_WALK", held.SQUASH_WALK)
        held.SQUASH_WALK = 1  # the squash is the second newest upstream commit: out of reach, so it stays listed
        self.assertEqual([b for b, *_ in held.held_now(self.p)], [branch])
        held.SQUASH_WALK = 2
        self.assertEqual(held.held_now(self.p), [])

    def test_status_prunes_a_deleted_remote_branch(self):
        self.refusal = HOOK
        planner.plan(self.p, "make it great", merge=True)
        branch = self.local()
        never_pushed = self.windvane("status")
        self.assertIn(f"not pushed: {branch} (", never_pushed)
        why = json.loads((self.p.state / "held_why.json").read_text())[branch]["why"]
        subprocess.run(shlex.split(next(part for part in why.split("`") if part.startswith("git push"))),
                       cwd=self.s.root, check=True, capture_output=True)
        self.assertIn(f"pushed to origin/{branch}-b, not merged", self.windvane("status"))
        self.s.git("branch", "-D", f"{branch}-b", cwd=self.s.remote)  # deleted on the remote, unmerged
        self.assertTrue(self.s.git("branch", "-r", "--list", f"origin/{branch}-b"))  # stale until a fetch prunes
        self.assertEqual(self.windvane("status"), never_pushed)
        self.assertEqual(self.s.git("branch", "-r", "--list", f"origin/{branch}-b"), "")


if __name__ == "__main__":
    unittest.main()
