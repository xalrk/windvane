"""A public project's commits never get the cap's `[skip ci]`: its workflows run free, so tagging them would only stop
its own CI for the rest of the month. A visibility gh can't give counts as private. And `windvane ci` on a public repo
says only what applies. All through test_actions_budget's fake `gh`, never the real one."""
import os
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from windvane import actions  # noqa: E402
from windvane.util import save_json  # noqa: E402
from helpers import Sandbox  # noqa: E402
import test_actions_budget as budget  # noqa: E402  (modules, so their tests aren't collected here again)
import test_actions_commit_time as commit_time  # noqa: E402
from test_actions_budget import FAKE_GH  # noqa: E402

FLOW = {"name": "test", "path": ".github/workflows/test.yml", "state": "active"}


class PublicTag(unittest.TestCase):
    B = budget.Budget
    serve, gh_calls, project, ci = B.serve, B.gh_calls, B.project, B.ci
    commit = commit_time.CommitTime.commit

    def setUp(self):
        self.s = Sandbox()
        self.data = {"repos": [], "runs": {}, "jobs": {}, "workflows": {}}
        gh = self.s.bin / "gh"
        gh.write_text(FAKE_GH)
        gh.chmod(0o755)
        os.environ["FAKE_GH_DATA"] = str(self.s.tmp / "gh.json")
        os.environ["FAKE_GH_LOG"] = str(self.s.tmp / "gh.log")

    def tearDown(self):
        actions.logged.clear()
        actions.visibility.clear()
        self.s.close()

    def over_cap(self, visibility):
        """A project with 100 minutes (cap 90) whose repo has `visibility`, and a fresh cached total of 95 from the
        login's private repos; and a worktree like the driver's."""
        self.data["repos"] = [{"nameWithOwner": "me/proj", "visibility": visibility}]
        self.serve()
        p = self.project(minutes=100)
        save_json(actions.cache_file(), {"month": actions.month(), "runs": {}, "total": {"used": 95, "at": time.time()}})
        self.wt = self.s.tmp / "slot-1"
        self.s.git("worktree", "add", "-q", "--detach", str(self.wt))
        return p

    def hook(self, env):
        n = int(env["GIT_CONFIG_COUNT"]) - 1  # the last setting: the one hooks_env added
        return Path(env[f"GIT_CONFIG_VALUE_{n}"]).parent / "prepare-commit-msg"  # beside the hooksPath file

    def test_a_public_projects_commits_never_carry_skip_ci(self):
        p = self.over_cap("PUBLIC")
        self.assertEqual(actions.tag(p, "Merge T-1: a"), "Merge T-1: a")
        self.assertEqual(actions.worker_git(p, self.wt), ({}, ""))  # no hook: the driver's read is in the cache
        self.s.git("config", "core.hooksPath", ".githooks")  # the project's own hooks: no prompt line either
        self.assertEqual(actions.worker_git(p, self.wt), ({}, ""))
        views = [c for c in self.gh_calls() if c[:2] == ["repo", "view"]]
        self.assertEqual(len(views), 1, "read once a run")

    def test_a_hook_given_before_the_visibility_is_read_still_leaves_a_public_projects_commits_alone(self):
        p = self.over_cap("PUBLIC")
        env, _ = actions.worker_git(p, self.wt)  # a worker started before the driver's read: the cache doesn't know
        self.assertTrue(env)
        self.assertEqual(self.commit(env, "one"), f"one {actions.SKIP}")
        actions.public(p)
        self.assertEqual(self.commit(env, "two"), "two")

    def test_a_private_projects_commits_carry_skip_ci(self):
        p = self.over_cap("PRIVATE")
        self.assertEqual(actions.tag(p, "Merge T-1: a"), f"Merge T-1: a {actions.SKIP}")
        env, _ = actions.worker_git(p, self.wt)
        self.assertTrue(self.hook(env).exists())
        self.assertEqual(self.commit(env, "one"), f"one {actions.SKIP}")

    def test_a_visibility_gh_cant_give_keeps_skip_ci(self):
        p = self.over_cap("PUBLIC")
        self.data["fail"] = "view"
        self.serve()
        self.assertEqual(actions.tag(p, "Merge T-1: a"), f"Merge T-1: a {actions.SKIP}")
        env, _ = actions.worker_git(p, self.wt)
        self.assertEqual(self.commit(env, "one"), f"one {actions.SKIP}")

    def test_ci_on_a_public_repo_with_no_budget_says_nothing_of_a_cap(self):
        self.data["repos"] = [{"nameWithOwner": "me/lib", "visibility": "PUBLIC"}]
        self.data["workflows"]["me/lib"] = [FLOW]
        self.serve()
        code, out = self.ci("test.yml", "--ref", "main")
        self.assertEqual(code, 0, out)
        self.assertNotIn("is public", out)
        self.assertNotIn("budget", out)
        self.assertEqual(out.strip(), "Started test.yml on main.")

    def test_ci_on_a_public_repo_whose_workflow_wont_start_fails_in_one_line(self):
        self.data["repos"] = [{"nameWithOwner": "me/lib", "visibility": "PUBLIC"}]
        self.data["workflows"]["me/lib"] = [FLOW]
        self.serve()
        (self.s.bin / "gh-fake").write_text(FAKE_GH)
        (self.s.bin / "gh-fake").chmod(0o755)
        (self.s.bin / "gh").write_text('#!/bin/sh\n[ "$1 $2" = "workflow run" ] && '
                                       '{ echo "HTTP 403: Resource not accessible by integration" >&2; exit 1; }\n'
                                       f'exec {self.s.bin / "gh-fake"} "$@"\n')
        code, out = self.ci("test.yml", "--ref", "main")
        self.assertEqual(code, 1, out)
        self.assertEqual(out.strip().splitlines(), ["windvane ci: gh workflow run failed: HTTP 403: Resource not "
                                                    "accessible by integration"])


if __name__ == "__main__":
    unittest.main()
