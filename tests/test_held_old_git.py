"""T-092: before git 2.38 (no `merge-tree --write-tree`), a held change squash-merged from several commits drops off
`status` by its whole change's patch-id, which `git cherry` alone misses; one only half on upstream stays listed.
T-099: old git's patch-id leaves a binary file's content out, so a change with one drops off only when the squash
holds the same blob (a failed ls-tree never passes for one), and a squash main later reverted still counts as merged.
`status` prunes a hand-pushed change's remote branch once it's deleted, so its line reads as never pushed again."""
import json
import shlex
import shutil
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
        old = self.s.bin / "git"  # git before 2.38 on the PATH, for `windvane` too: no `merge-tree --write-tree`
        old.write_text(self.old_git())
        old.chmod(0o755)
        self.assertEqual(shutil.which("git"), str(old))

    def old_git(self):
        """The `git` put first on the PATH: today's, emulating one before 2.38."""
        return f"""#!/bin/sh
[ "$1 $2" = "merge-tree --write-tree" ] && {{ echo "error: unknown option \\`write-tree'" >&2; exit 129; }}
exec {shutil.which("git")} "$@"
"""

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
        self.assertNotIn(held.git(self.s.root, "merge-tree", "--write-tree", "main", branch).returncode, (0, 1))
        self.assertIn("+", self.s.git("cherry", "origin/main", branch))  # cherry alone keeps it listed
        self.assertEqual(held.held_now(self.p), [])
        self.assertNotIn("held", self.windvane("status"))
        self.assertNotIn("held", self.windvane("overview").lower())
        self.assertEqual(held.held_branches(self.p), {})  # a run's start stops tracking it
        self.assertEqual(json.loads((self.p.state / "held.json").read_text()), {})

    def with_logo(self, branch, logo=b"\x89PNG\0\1\2"):
        """`branch` with a binary file added on top, pushed too."""
        dev = self.s.tmp / "dev"
        (dev / "logo.png").write_bytes(logo)
        self.s.git("add", "logo.png", cwd=dev)
        self.s.git("commit", "-qm", "logo", cwd=dev)
        self.s.git("push", "-q", "origin", f"HEAD:{branch}-b", cwd=dev)
        self.s.git("fetch", "-q", "origin")
        self.s.git("branch", "-f", branch, f"origin/{branch}-b")
        self.assertIn("-\t-\tlogo.png", self.s.git("diff", "--numstat", "origin/main", branch))
        return branch

    def test_a_squash_with_a_binary_file_drops_off(self):
        branch = self.with_logo(self.three_commits())
        self.squash_onto_main(f"origin/{branch}-b")
        self.assertEqual(held.held_now(self.p), [])
        self.assertNotIn("held", self.windvane("status"))

    def test_a_squash_with_another_binary_blob_stays_listed(self):
        branch = self.with_logo(self.three_commits())
        self.on_remote(["merge", "-q", "--squash", f"origin/{branch}-b"])
        (self.other / "logo.png").write_bytes(b"\x89PNG\0\1\3")  # the same text change, another picture
        for step in (["add", "logo.png"], ["commit", "-qm", "squashed (#7)"], ["push", "-q", "origin", "main"]):
            self.s.git(*step, cwd=self.other)
        self.s.git("fetch", "-q", "origin")
        self.assertEqual([b for b, *_ in held.held_now(self.p)], [branch])  # can't tell it's in: kept, never lost
        self.assertIn(f"pushed to origin/{branch}-b, not merged", self.windvane("status"))

    def test_a_reverted_squash_still_counts_as_merged(self):
        branch = self.with_logo(self.three_commits())
        self.squash_onto_main(f"origin/{branch}-b")
        for step in (["pull", "-q"], ["revert", "--no-edit", "HEAD"], ["push", "-q", "origin", "main"]):
            self.s.git(*step, cwd=self.other)
        self.s.git("fetch", "-q", "origin")
        self.assertFalse(self.s.git("ls-tree", "origin/main", "logo.png"))  # main undid it on purpose
        self.assertEqual(held.held_now(self.p), [])

    def test_binary_files_alone(self):
        repo = self.s.tmp / "plain"
        self.s.git("init", "-q", "-b", "main", str(repo))
        (repo / "logo.png").write_bytes(b"\x89PNG\0\1")
        self.s.git("add", "logo.png", cwd=repo)
        self.s.git("commit", "-qm", "logo", cwd=repo)
        self.s.git("checkout", "-qb", "art", cwd=repo)
        for n, logo in enumerate((b"\x89PNG\0\2", b"\x89PNG\0\3")):
            (repo / "logo.png").write_bytes(logo)
            (repo / "icon.png").write_bytes(logo + b"icon")
            self.s.git("add", ".", cwd=repo)
            self.s.git("commit", "-qm", f"art {n}", cwd=repo)
        self.s.git("checkout", "-q", "main", cwd=repo)
        (repo / "README").write_text("main moved\n")
        self.s.git("add", "README", cwd=repo)
        self.s.git("commit", "-qm", "main moved", cwd=repo)
        self.assertFalse(held.squashed_on(repo, "main", "art"))
        self.s.git("merge", "-q", "--squash", "art", cwd=repo)
        (repo / "icon.png").write_bytes(b"\x89PNG\0\2icon")  # one picture is another one's
        self.s.git("commit", "-qam", "art, squashed", cwd=repo)
        self.assertFalse(held.squashed_on(repo, "main", "art"))
        (repo / "icon.png").write_bytes(b"\x89PNG\0\3icon")
        self.s.git("commit", "-qam", "icon fixed", cwd=repo)
        self.assertTrue(held.squashed_on(repo, "main", "art"))

    def test_a_binary_file_named_like_pathspec_magic(self):
        repo, logo = self.s.tmp / "magic", ":(icase)logo.png"  # a path to git, which ls-tree can't take as a pathspec
        self.s.git("init", "-q", "-b", "main", str(repo))
        (repo / "README").write_text("one\n")
        self.s.git("add", "README", cwd=repo)
        self.s.git("commit", "-qm", "one", cwd=repo)
        self.s.git("checkout", "-qb", "art", cwd=repo)
        for n in (2, 3):
            (repo / "README").write_text(f"one\n{n}\n")
            (repo / logo).write_bytes(b"\x89PNG\0" + bytes([n]))
            self.s.git("add", ".", cwd=repo)
            self.s.git("commit", "-qm", f"art {n}", cwd=repo)
        self.s.git("checkout", "-q", "main", cwd=repo)
        self.assertEqual(held.binary_files(repo, "main", "art"), [logo])
        self.s.git("merge", "-q", "--squash", "art", cwd=repo)
        (repo / logo).write_bytes(b"\x89PNG\0\4")  # the same text change, another picture
        self.s.git("commit", "-qam", "art, squashed", cwd=repo)
        self.assertFalse(held.squashed_on(repo, "main", "art"))
        (repo / logo).write_bytes(b"\x89PNG\0\3")
        self.s.git("commit", "-qam", "art, squashed right", cwd=repo)
        self.assertFalse(held.squashed_on(repo, "main", "art"))  # the fix-up commit alone isn't the change
        self.s.git("reset", "-q", "--soft", "HEAD~2", cwd=repo)
        self.s.git("commit", "-qm", "art, squashed in one", cwd=repo)
        self.assertTrue(held.squashed_on(repo, "main", "art"))

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
