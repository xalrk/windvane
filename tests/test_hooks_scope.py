"""A worker session's hooks (the push scan, the `[skip ci]` hook at the cap) apply to its own worktree only: its git
environment reaches every git it runs, its check's included, and a repo a test clones or inits there keeps its own
hooks. All through test_actions_budget's fake `gh`, never the real one."""
import os
import subprocess
import sys
import unittest
from argparse import Namespace
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from windvane import actions, driver, runner  # noqa: E402
from windvane.config import Project  # noqa: E402
from windvane.util import now  # noqa: E402
from windvane.worktree import ensure_worktree, guard_pushes, hooks_env, hooks_folder  # noqa: E402
from helpers import Sandbox  # noqa: E402
from test_actions_budget import FAKE_GH  # noqa: E402

TOKEN = "ghp_" + "Wq4" * 12


class Scope(unittest.TestCase):
    def setUp(self):
        self.s = Sandbox()
        self.addCleanup(self.s.close)
        self.addCleanup(actions.asked.clear)
        gh = self.s.bin / "gh"
        gh.write_text(FAKE_GH)
        gh.chmod(0o755)
        os.environ["FAKE_GH_LOG"] = str(self.s.tmp / "gh.log")
        self.p = Project(self.s.root)
        self.wt = self.s.tmp / "wt"
        ensure_worktree(self.p, self.wt, setup=False)
        self.config = (self.s.root / ".git/config").read_text()

    def session(self, budget):
        """A worker session's environment, as the driver builds it; with a minutes budget and no count yet, its
        commits are at the cap."""
        if budget:
            cfg = self.s.root / ".windvane/config.toml"
            cfg.write_text("actions_minutes = 100\n" + cfg.read_text())
            self.p = Project(self.s.root)
        push_env = guard_pushes(self.p, self.wt)
        git_env, line = actions.worker_git(self.p, self.wt)
        self.assertEqual(line, "")
        self.assertEqual(bool(git_env), budget)
        return runner.env({**push_env, **git_env}, self.p)

    def git(self, env, cwd, *args):
        return subprocess.run(["git", *args], cwd=cwd, env=env, capture_output=True, text=True)

    def throwaways(self, env):
        """A clone and a fresh repo, made the way a project's test makes them under `windvane check`."""
        self.assertEqual(self.git(env, self.s.tmp, "clone", "-q", str(self.s.remote), "clone").returncode, 0)
        self.assertEqual(self.git(env, self.s.tmp, "init", "-q", "fresh").returncode, 0)
        return self.s.tmp / "clone", self.s.tmp / "fresh"

    def assert_own_hooks(self, env, repo):
        """`repo` has no core.hooksPath and runs the hooks in its own `.git/hooks`."""
        self.assertEqual(self.git(env, repo, "config", "core.hooksPath").stdout, "", repo.name)
        self.assertEqual(Path(repo, self.git(env, repo, "rev-parse", "--git-path", "hooks").stdout.strip()),
                         repo / ".git/hooks", repo.name)
        bare = self.s.tmp / f"{repo.name}-remote.git"
        self.git(env, self.s.tmp, "init", "-q", "--bare", str(bare))
        hook = repo / ".git/hooks/pre-push"
        hook.write_text(f"#!/bin/sh\necho ran >> {self.s.tmp}/{repo.name}.log\n")
        hook.chmod(0o755)
        (repo / "key.txt").write_text(TOKEN + "\n")
        self.git(env, repo, "add", "key.txt")
        self.assertEqual(self.git(env, repo, "commit", "-qm", "fixture").returncode, 0)
        self.assertEqual(self.git(env, repo, "log", "-1", "--format=%s").stdout.strip(), "fixture")  # no [skip ci]
        r = self.git(env, repo, "push", "-q", str(bare), "HEAD:refs/heads/x")
        self.assertEqual(r.returncode, 0, r.stderr)  # a test's fixture, not a push of the project's: no scan
        self.assertEqual((self.s.tmp / f"{repo.name}.log").read_text(), "ran\n")

    def assert_push_scanned(self, env):
        (self.wt / "b.txt").write_text(TOKEN + "\n")
        self.git(env, self.wt, "add", "b.txt")
        self.git(env, self.wt, "commit", "-qm", "work")
        r = self.git(env, self.wt, "push", "-q", "origin", "HEAD:refs/heads/t-001-work")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("a token at b.txt:1", r.stderr)

    def test_without_a_budget_a_clone_and_a_fresh_repo_keep_their_own_hooks_and_the_worktree_is_scanned(self):
        env = self.session(budget=False)
        self.assertEqual(self.git(env, self.wt, "config", "core.hooksPath").stdout.strip(),
                         str(hooks_folder(self.p, self.wt)))
        self.assertEqual(self.git(env, self.s.root, "config", "core.hooksPath").stdout, "")  # the user's checkout
        for repo in self.throwaways(env):
            self.assert_own_hooks(env, repo)
        self.assert_push_scanned(env)
        self.assertEqual((self.s.root / ".git/config").read_text(), self.config)

    def test_at_the_cap_only_the_worktrees_commits_carry_skip_ci_and_its_pushes_are_still_scanned(self):
        env = self.session(budget=True)
        for repo in self.throwaways(env):
            self.assert_own_hooks(env, repo)
        (self.wt / "a.txt").write_text("a\n")
        self.git(env, self.wt, "add", "a.txt")
        self.git(env, self.wt, "commit", "-qm", "one")
        self.assertEqual(self.git(env, self.wt, "log", "-1", "--format=%s").stdout.strip(), f"one {actions.SKIP}")
        self.assert_push_scanned(env)
        self.assertEqual((self.s.root / ".git/config").read_text(), self.config)

    def test_a_worktree_path_with_glob_characters_matches_only_itself(self):
        odd, near = self.s.tmp / "slot[1]*", self.s.tmp / "slot1x"  # `[1]*` as a glob would match the other one
        for wt in (odd, near):
            self.s.git("worktree", "add", "-q", "--detach", str(wt))
        folder = self.s.tmp / "hooks"
        folder.mkdir()
        env = {**os.environ, **hooks_env(odd, folder)}
        self.assertEqual(self.git(env, odd, "config", "core.hooksPath").stdout.strip(), str(folder))
        self.assertEqual(self.git(env, near, "config", "core.hooksPath").stdout, "")


# A project's check whose tests make repos of their own: it fails when one gets a core.hooksPath it didn't set
FIXTURE_CHECK = """set -e
d=$(mktemp -d)
git clone -q "$(git remote get-url origin)" "$d/clone"
git init -q "$d/fresh"
for r in clone fresh; do
    h=$(git -C "$d/$r" config core.hooksPath || true)
    [ -z "$h" ] || { echo "$r has core.hooksPath $h"; exit 1; }
done
echo "the fixtures keep their own hooks"
"""


class Pipeline(unittest.TestCase):
    """A worker session as the driver starts it, running `windvane check` from its worktree."""

    def setUp(self):
        self.s = Sandbox()
        self.addCleanup(self.s.close)
        self.s.ticket("T-001")
        self.s.write("fixture_check.sh", FIXTURE_CHECK)
        cfg = self.s.root / ".windvane/config.toml"
        cfg.write_text(cfg.read_text().replace('check = "test -f README"', 'check = "sh fixture_check.sh"'))
        self.s.publish()
        os.environ["FAKE_WORKER_RUN"] = "windvane check; echo hooks: $(git config core.hooksPath)"
        self.addCleanup(setattr, driver, "push", driver.push)
        driver.push = lambda *a, **k: None

    def test_the_sessions_check_makes_repos_without_its_hooks_while_its_worktree_keeps_the_push_scan(self):
        p = Project(self.s.root)
        driver.Pilot(p, Namespace(cap=75, week_cap=95, ticket_hours=1, min_slot=0, answer_wait=10, replan=0, goal="",
                                  parallel=1)).run(now() + timedelta(hours=1))
        ran = Path(os.environ["FAKE_LOG"] + ".ran").read_text()  # what the session's commands said
        self.assertIn("CHECK PASSED", ran)
        self.assertNotIn("has core.hooksPath", ran)
        self.assertIn(f"hooks: {hooks_folder(p, p.work_dir)}\n", ran)  # test_push_scan's Merge: its push is refused
        self.assertIn("T-001", self.s.git("log", "--format=%s", "origin/main"))  # merged: its push went through


if __name__ == "__main__":
    unittest.main()
