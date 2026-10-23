"""A submodule cloned in place in a session's worktree (its own `.git` folder, not one under the worktree's git dir)
gets the submodules' push scan too, while a repo a test makes there keeps its own hooks; and a hooks folder rebuilt
by a driver killed halfway is still a working one. All in a Sandbox, with bare remotes."""
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from windvane import actions, runner, worktree  # noqa: E402
from windvane.config import Project  # noqa: E402
from windvane.worktree import ensure_worktree, guard_pushes, hooks_folder, submodule_folder  # noqa: E402
from helpers import Sandbox  # noqa: E402
from test_actions_budget import FAKE_GH  # noqa: E402

TOKEN = "ghp_" + "Wq4" * 12


class Base(unittest.TestCase):
    def setUp(self):
        self.s = Sandbox()
        self.addCleanup(self.s.close)
        self.addCleanup(actions.asked.clear)
        gh = self.s.bin / "gh"
        gh.write_text(FAKE_GH)
        gh.chmod(0o755)
        os.environ["FAKE_GH_LOG"] = str(self.s.tmp / "gh.log")
        self.p = Project(self.s.root)
        self.wt = self.s.tmp / "work-1"
        ensure_worktree(self.p, self.wt, setup=False)

    def git(self, env, cwd, *args):
        return subprocess.run(["git", *args], cwd=cwd, env=env, capture_output=True, text=True)

    def commit(self, env, repo, name, text):
        Path(repo, name).write_text(text)
        self.git(env, repo, "add", name)
        r = self.git(env, repo, "commit", "-qm", name)
        self.assertEqual(r.returncode, 0, r.stderr)

    def bare(self, name):
        remote = self.s.tmp / f"{name}.git"
        self.git(None, self.s.tmp, "init", "-q", "--bare", "-b", "main", str(remote))
        return remote

    def session(self, budget=False):
        """A worker session's environment for the slot, as the driver builds it."""
        if budget:
            cfg = self.s.root / ".windvane/config.toml"
            cfg.write_text("actions_minutes = 100\n" + cfg.read_text())
            self.p = Project(self.s.root)
        push_env = guard_pushes(self.p, self.wt)
        git_env, _ = actions.worker_git(self.p, self.wt)
        return runner.env({**push_env, **git_env}, self.p)


class InPlace(Base):
    """A submodule whose clone sits in the worktree with its own `.git` folder, registered before the session."""

    def setUp(self):
        super().setUp()
        self.sub_remote = self.bare("sub")
        seed = self.s.tmp / "seed"
        self.git(None, self.s.tmp, "init", "-q", "-b", "main", str(seed))
        self.commit(None, seed, "lib.txt", "lib\n")
        self.git(None, seed, "push", "-q", str(self.sub_remote), "main")
        self.sub = self.wt / "sub"
        self.git(None, self.wt, "clone", "-q", str(self.sub_remote), "sub")  # an existing clone, then added
        r = self.git(None, self.wt, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(self.sub_remote),
                     "sub")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.git(None, self.wt, "commit", "-qm", "sub")
        self.assertTrue((self.sub / ".git").is_dir())  # in place: not under the worktree's git dir

    def test_a_push_adding_a_dotenv_from_it_is_refused_and_its_own_hooks_still_run(self):
        env = self.session()
        self.assertEqual(self.git(env, self.sub, "config", "core.hooksPath").stdout.strip(),
                         str(submodule_folder(self.p, self.wt)))
        self.assertEqual(self.git(None, self.sub, "config", "core.hooksPath").stdout, "")  # outside the session
        hook = self.sub / ".git/hooks/pre-commit"
        hook.write_text(f"#!/bin/sh\necho pre-commit >> {self.s.tmp}/own.log\n")
        hook.chmod(0o755)
        self.commit(env, self.sub, ".env", "API_KEY=x\n")
        self.assertEqual((self.s.tmp / "own.log").read_text(), "pre-commit\n")
        r = self.git(env, self.sub, "push", "-q", "origin", "HEAD:refs/heads/topic")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("a file named like a secret", r.stderr)
        self.assertIn(".env", r.stderr)
        self.assertEqual(self.git(None, self.sub_remote, "branch", "--list", "topic").stdout, "")

    def test_at_the_cap_it_is_still_covered(self):
        env = self.session(budget=True)
        self.assertEqual(self.git(env, self.sub, "config", "core.hooksPath").stdout.strip(),
                         str(submodule_folder(self.p, self.wt)))

    def test_a_submodule_nested_in_it_is_covered_and_a_repo_inited_after_the_start_keeps_its_own_hooks(self):
        inner = self.bare("inner")
        seed = self.s.tmp / "seed"
        self.git(None, seed, "push", "-q", str(inner), "main")
        r = self.git(None, self.sub, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(inner), "inner")
        self.assertEqual(r.returncode, 0, r.stderr)  # its git dir goes under sub/.git/modules
        env = self.session()
        self.assertEqual(self.git(env, self.sub / "inner", "config", "core.hooksPath").stdout.strip(),
                         str(submodule_folder(self.p, self.wt)))
        for repo in (self.wt / "fixture", self.sub / "fixture"):  # as a test under the check makes one
            self.assertEqual(self.git(env, self.wt, "init", "-q", str(repo)).returncode, 0)
            self.assertEqual(self.git(env, repo, "config", "core.hooksPath").stdout, "", repo)
            hook = repo / ".git/hooks/pre-push"
            hook.write_text(f"#!/bin/sh\necho ran >> {self.s.tmp}/fixture.log\n")
            hook.chmod(0o755)
            self.commit(env, repo, ".env", "API_KEY=x\n")
            r = self.git(env, repo, "push", "-q", str(self.bare(f"fixture-{repo.parent.name}")), "HEAD:refs/heads/x")
            self.assertEqual(r.returncode, 0, r.stderr)  # a test's fixture: only its own hook ran
        self.assertEqual((self.s.tmp / "fixture.log").read_text(), "ran\nran\n")

    def test_a_gitmodules_path_outside_the_worktree_is_never_included(self):
        outside = self.s.tmp / "outside"
        self.git(None, self.s.tmp, "init", "-q", str(outside))
        self.git(None, self.wt, "config", "-f", ".gitmodules", "submodule.out.path", "../outside")
        env = self.session()
        self.assertEqual(self.git(env, outside, "config", "core.hooksPath").stdout, "")
        self.assertEqual(worktree.in_place_submodules(self.wt), [self.sub.resolve()])


class Rebuild(Base):
    """The hooks folders are swapped in whole: a driver killed while building one leaves the old one working."""

    def assert_full(self, env, clean=True):
        """The slot's folders hold a pre-push and a .gitconfig, with nothing a build left beside them (`clean`), and a
        token pushed with `env` is refused."""
        for folder in (hooks_folder(self.p, self.wt), submodule_folder(self.p, self.wt)):
            for name in ("pre-push", worktree.HOOKS_CONFIG):
                self.assertTrue((folder / name).is_file(), f"{folder}/{name}")
            if clean:
                self.assertEqual([e.name for e in folder.parent.iterdir() if e.name.startswith(f".{folder.name}.")],
                                 [os.readlink(folder)])  # only the build the folder links to
        (self.wt / "b.txt").write_text(TOKEN + "\n")
        self.git(env, self.wt, "add", "b.txt")
        self.git(env, self.wt, "commit", "-qm", "work")
        r = self.git(env, self.wt, "push", "-q", "origin", "HEAD:refs/heads/t-001-work")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("a token at b.txt:1", r.stderr)
        self.git(env, self.wt, "reset", "-q", "--hard", "HEAD~1")

    def test_a_swap_that_fails_leaves_the_old_folder_working(self):
        env = self.session()
        before = os.readlink(hooks_folder(self.p, self.wt))
        with mock.patch.object(worktree, "swap", side_effect=OSError("disk gone")):
            with self.assertRaises(OSError):
                guard_pushes(self.p, self.wt)
        self.assertEqual(os.readlink(hooks_folder(self.p, self.wt)), before)
        self.assert_full(env)  # the running session's env still finds its hooks

    def test_a_driver_killed_between_build_and_swap_leaves_the_old_folder_and_the_next_build_clears_its_leftovers(self):
        env = self.session()
        killed = ("import os, sys\nsys.path.insert(0, sys.argv[1])\n"
                  "from windvane import worktree\nfrom windvane.config import Project\n"
                  "worktree.swap = lambda folder, new: os._exit(9)\n"
                  "worktree.guard_pushes(Project(sys.argv[2]), sys.argv[3])\n")
        r = subprocess.run([sys.executable, "-c", killed, str(Path(__file__).resolve().parents[1]), str(self.s.root),
                            str(self.wt)], capture_output=True, text=True)
        self.assertEqual(r.returncode, 9, r.stderr)
        folder = hooks_folder(self.p, self.wt)
        left = [e for e in folder.parent.iterdir() if e.name.startswith(f".{folder.name}.")]
        self.assertEqual(len(left), 2)  # the build it linked to, and the one the killed driver never swapped in
        self.assert_full(env, clean=False)  # the session already running still pushes through the old build
        self.assert_full(self.session())

    def test_a_plain_folder_from_before_is_replaced_by_a_link_to_a_build(self):
        folder = hooks_folder(self.p, self.wt)
        folder.mkdir(parents=True)
        (folder / "stale").write_text("from an older windvane\n")
        env = self.session()
        self.assertTrue(folder.is_symlink())
        self.assertFalse((folder / "stale").exists())
        self.assert_full(env)


if __name__ == "__main__":
    unittest.main()
