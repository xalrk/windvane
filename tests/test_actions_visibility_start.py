"""A run reads its repo's visibility afresh: an earlier run's "public" in the cache is dropped as the run starts, so a
repo made private since gets its first workers' commits tagged at the cap. And a 403 that isn't a rate limit names the
token's missing Actions permission, in `windvane ci` and in the recount's log line, not `gh auth status`. All through
test_actions_budget's fake `gh`, never the real one."""
import contextlib
import io
import os
import sys
import time
import unittest
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from windvane import actions, driver  # noqa: E402
from windvane.util import load_json, save_json  # noqa: E402
from windvane.util import now as local_now  # noqa: E402
from helpers import Sandbox  # noqa: E402
import test_actions_budget as budget  # noqa: E402  (modules, so their tests aren't collected here again)
import test_actions_commit_time as commit_time  # noqa: E402
from test_actions_budget import FAKE_GH, job  # noqa: E402
from test_pipeline import args  # noqa: E402

FLOW = {"name": "test", "path": ".github/workflows/test.yml", "state": "active"}
DENIED = "HTTP 403: Resource not accessible by integration (https://api.github.com/repos/me/proj/actions/runs)"


class VisibilityAtStart(unittest.TestCase):
    B = budget.Budget
    serve, add, gh_calls, project, ci = B.serve, B.add, B.gh_calls, B.project, B.ci
    commit = commit_time.CommitTime.commit

    def setUp(self):
        self.s = Sandbox()
        self.data = {"repos": [], "runs": {}, "jobs": {}, "workflows": {}}
        self.gh(FAKE_GH)
        os.environ["FAKE_GH_DATA"] = str(self.s.tmp / "gh.json")
        os.environ["FAKE_GH_LOG"] = str(self.s.tmp / "gh.log")

    def tearDown(self):
        self.idle()
        for kept in (actions.logged, actions.visibility, actions.kept, actions.unsure, actions.recount_said):
            kept.clear()
        self.s.close()

    def gh(self, script, name="gh"):
        f = self.s.bin / name
        f.write_text(script)
        f.chmod(0o755)

    def denied(self, first):
        """A gh whose calls starting with `first` fail as a token without the Actions permission makes them fail."""
        self.gh(FAKE_GH, "gh-fake")
        self.gh(f'#!/bin/sh\n[ "$1" = {first} ] && {{ echo "{DENIED}" >&2; exit 1; }}\n'
                f'exec {self.s.bin / "gh-fake"} "$@"\n')

    def idle(self):
        """Wait for this process's background count to end, as later workers start after it."""
        self.assertTrue(actions.counting.acquire(timeout=30))
        actions.counting.release()

    def made_private(self, visibility="PRIVATE"):
        """A project with 100 minutes (cap 90), 95 used, an earlier run's cache saying its repo is public, and the repo
        now `visibility`; and a worktree like the driver's."""
        self.data["repos"] = [{"nameWithOwner": "me/proj", "visibility": visibility}]
        self.serve()
        p = self.project(minutes=100)
        save_json(actions.cache_file(), {"month": actions.month(), "runs": {}, "total": {"used": 95, "at": time.time()},
                                         "public": {str(p.root): True}})
        self.wt = self.s.tmp / "slot-1"
        self.s.git("worktree", "add", "-q", "--detach", str(self.wt))
        return p

    def test_a_worker_started_before_the_runs_read_gets_the_hook_and_tags_at_the_cap(self):
        p = self.made_private()
        self.assertTrue(actions.cached_public(p))  # what the earlier run left
        with actions.counting:  # this run's read still in flight: no background count starts here
            actions.refresh(p, start=True)
            self.assertNotIn(str(p.root), load_json(actions.cache_file(), {})["public"])
            self.assertEqual(load_json(actions.cache_file(), {})["total"]["used"], 95)  # the count is kept
            env, line = actions.worker_git(p, self.wt)
            self.assertTrue(env, "a worker started before the read gets the hook")
            self.assertEqual(self.commit(env, "one"), f"one {actions.SKIP}")
        self.assertFalse(actions.public(p))  # the run's read: private
        self.assertEqual(self.commit(env, "two"), f"two {actions.SKIP}")
        self.assertTrue(actions.worker_git(p, self.wt)[0])

    def test_a_session_ends_refresh_keeps_the_runs_read(self):
        p = self.made_private("PUBLIC")
        actions.refresh(p, start=True)
        self.idle()
        actions.refresh(p)  # a session's end: nothing dropped
        self.idle()
        self.assertTrue(actions.cached_public(p))
        self.assertEqual(len([c for c in self.gh_calls() if c[:2] == ["repo", "view"]]), 1, "read once a run")

    def test_a_run_whose_read_says_public_leaves_later_workers_untagged(self):
        p = self.made_private("PUBLIC")
        with actions.counting:
            actions.refresh(p, start=True)
            env, _ = actions.worker_git(p, self.wt)  # before the read: the hook, which reads the cache at each commit
        actions.refresh(p)
        self.idle()
        self.assertTrue(actions.cached_public(p))
        self.assertEqual(self.commit(env, "one"), "one")
        self.assertEqual(actions.worker_git(p, self.wt), ({}, ""))  # a later worker: no hook at all

    def test_a_busy_lock_at_the_start_doubts_the_cache_in_this_process_and_its_hooks(self):
        p = self.made_private()
        with actions.locked() as got, actions.counting:  # another driver's count holds the lock
            self.assertTrue(got)
            actions.refresh(p, start=True)
            self.assertTrue(load_json(actions.cache_file(), {})["public"][str(p.root)])  # left as it was
            self.assertFalse(actions.cached_public(p))
            env, _ = actions.worker_git(p, self.wt)
        self.assertEqual(self.commit(env, "one"), f"one {actions.SKIP}")  # its hook ignores the stale entry
        actions.public(p)
        self.assertNotIn(str(p.root), actions.unsure)

    def test_a_driver_run_tags_its_workers_commits_once_the_repo_is_private(self):
        self.data["repos"] = [{"nameWithOwner": "me/proj", "visibility": "PRIVATE"},
                              {"nameWithOwner": "me/app", "visibility": "PRIVATE"}]
        self.add("me/app", 1, [job(95 * 60)])
        self.s.ticket("T-001")
        p = self.project(minutes=100)
        save_json(actions.cache_file(), {"public": {str(p.root): True}})
        with actions.counting, contextlib.redirect_stdout(io.StringIO()):  # the run's background read never comes:
            # the start's drop alone tags the worker
            pilot = driver.Pilot(p, args())
            pilot.run(local_now() + timedelta(hours=1))
        self.assertEqual(pilot.merged, ["T-001"])
        self.s.git("fetch", "-q", "origin")
        self.assertEqual(self.s.git("log", "-1", "--format=%s", "origin/main^2"), f"T-001: build {actions.SKIP}")

    # A token without the Actions permission

    def test_ci_whose_start_is_refused_names_the_actions_permission(self):
        self.data["repos"] = [{"nameWithOwner": "me/proj", "visibility": "PRIVATE"}]
        self.data["workflows"]["me/proj"] = [FLOW]
        self.serve()
        self.project(minutes=2000)
        self.denied("workflow")
        code, out = self.ci("test.yml", "--ref", "main")
        self.assertEqual(code, 1, out)
        failed = [x for x in out.splitlines() if x.startswith("windvane ci:")]
        self.assertEqual(failed, ["windvane ci: gh workflow run failed: " + DENIED + " (the token gh uses lacks the "
                                  "Actions permission: a fine-grained token needs Actions: read and write)"])
        self.assertNotIn("gh auth status", out)

    def test_ci_whose_count_is_refused_names_the_actions_permission(self):
        self.data["repos"] = [{"nameWithOwner": "me/proj", "visibility": "PRIVATE"}]
        self.data["workflows"]["me/proj"] = [FLOW]
        self.serve()
        self.project(minutes=2000)
        self.denied("api")
        code, out = self.ci("test.yml")
        self.assertEqual(code, 1, out)
        self.assertEqual(out.strip().splitlines(), [
            "windvane ci: couldn't count this month's Actions minutes: `gh api repos/me/proj/actions/runs`: " + DENIED
            + " (the token gh uses lacks the Actions permission: a fine-grained token needs Actions: read), so no "
            "workflow starts"])
        self.assertFalse([c for c in self.gh_calls() if c[:2] == ["workflow", "run"]])

    def test_a_recount_refused_logs_the_actions_permission(self):
        self.data["repos"] = [{"nameWithOwner": "me/proj", "visibility": "PRIVATE"}]
        self.serve()
        p = self.project(minutes=2000)
        self.denied("api")
        self.assertTrue(actions.counting.acquire(blocking=False))
        with contextlib.redirect_stdout(io.StringIO()):  # util.log echoes to stdout
            actions.refresh_now(p)
        said = [x for x in (p.state / "autopilot.log").read_text().splitlines() if "recount failed" in x]
        self.assertEqual(len(said), 1, said)
        self.assertTrue(said[0].endswith(f"recount failed: `gh api repos/me/proj/actions/runs`: {DENIED} (the token "
                                         "gh uses lacks the Actions permission: a fine-grained token needs Actions: "
                                         "read)"), said[0])

    def test_a_403_rate_limit_is_still_the_rate_limit_and_saml_a_login_problem(self):
        for why, name in (("gh: API rate limit exceeded for user ID 1. (HTTP 403)", "rate limit"),
                          ("HTTP 403: You have exceeded a secondary rate limit.", "rate limit"),
                          ("HTTP 403: Resource not accessible by personal access token", "permission"),
                          ("HTTP 403: Resource protected by organization SAML enforcement. You must grant your OAuth "
                           "token access to this organization.", "auth")):
            with self.subTest(why):
                self.assertEqual(actions.cause(why)[0], name)


if __name__ == "__main__":
    unittest.main()
