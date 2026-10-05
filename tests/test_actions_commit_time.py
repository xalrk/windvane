"""A worker's `[skip ci]` decided at each commit from the cached count, not once at the session's start, and worker
starts that never wait on a count: the recount runs in the background when a session ends (T-070). All through
test_actions_budget's fake `gh`, never the real one."""
import json
import os
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from windvane import actions, runner  # noqa: E402
from windvane.config import Project  # noqa: E402
from windvane.util import save_json  # noqa: E402
from helpers import Sandbox  # noqa: E402
from test_actions_budget import FAKE_GH, job, run  # noqa: E402


class CommitTime(unittest.TestCase):
    def setUp(self):
        self.s = Sandbox()
        self.data = {"repos": ["me/proj"], "runs": {}, "jobs": {}, "workflows": {}}
        gh = self.s.bin / "gh"
        gh.write_text(FAKE_GH)
        gh.chmod(0o755)
        os.environ["FAKE_GH_DATA"] = str(self.s.tmp / "gh.json")
        os.environ["FAKE_GH_LOG"] = str(self.s.tmp / "gh.log")
        self.add(1, 10)
        cfg = self.s.root / ".windvane/config.toml"
        cfg.write_text("actions_minutes = 100\n" + cfg.read_text())  # cap 90
        self.s.publish("budget")
        self.p = Project(self.s.root)
        self.wt = self.s.tmp / "slot-1"  # a worktree like the driver's: the main checkout's .git/config is shared
        self.s.git("worktree", "add", "-q", "--detach", str(self.wt))
        self.config = (self.s.root / ".git/config").read_text()

    def tearDown(self):
        self.assertTrue(actions.counting.acquire(timeout=10))  # no background count outlives its sandbox
        actions.counting.release()
        actions.asked.clear()
        self.s.close()

    def add(self, rid, minutes):
        self.data["runs"].setdefault("me/proj", []).append(run(rid))
        self.data["jobs"][f"me/proj#{rid}"] = [job(minutes * 60)]
        Path(os.environ["FAKE_GH_DATA"]).write_text(json.dumps(self.data))

    def gh_calls(self):
        f = Path(os.environ["FAKE_GH_LOG"])
        return f.read_text().splitlines() if f.exists() else []

    def start(self):
        """A worker session's git environment, as the driver gives it."""
        env, line = actions.worker_git(self.p, self.wt)
        self.assertEqual(line, "")
        return env

    def commit(self, env, msg):
        """A commit in the worker's worktree under its environment; its subject."""
        (self.wt / "a.txt").write_text(msg)
        self.s.git("add", "a.txt", cwd=self.wt)
        os.environ.update(env)
        try:
            self.s.git("commit", "-qm", msg, cwd=self.wt)
        finally:
            for k in env:
                os.environ.pop(k)
        return self.s.git("log", "-1", "--format=%s", cwd=self.wt)

    def test_a_commit_after_the_count_crosses_the_cap_carries_skip_ci(self):
        actions.count(self.s.root)  # 10 of 90
        env = self.start()
        self.assertEqual(self.commit(env, "one"), "one")  # still under it: no tag
        self.add(2, 85)  # another project's run, mid-session
        self.assertEqual(actions.count(self.s.root, max_age=0)["used"], 95)
        self.assertEqual(self.commit(env, "two"), f"two {actions.SKIP}")
        self.assertEqual((self.s.root / ".git/config").read_text(), self.config)  # the user's checkout: untouched

    def test_a_missing_or_last_months_cache_tags_the_commit(self):
        env = self.start()
        self.assertFalse(actions.cache_file().exists())
        self.assertEqual(self.commit(env, "one"), f"one {actions.SKIP}")
        save_json(actions.cache_file(), {"month": "2026-01", "runs": {}, "total": {"used": 1, "at": time.time()}})
        self.assertEqual(self.commit(env, "two"), f"two {actions.SKIP}")
        self.assertEqual((self.s.root / ".git/config").read_text(), self.config)

    def test_a_worker_start_reads_only_the_cache(self):
        actions.count(self.s.root)
        cache = json.loads(actions.cache_file().read_text())
        cache["total"]["at"] -= 3 * actions.FRESH_S  # stale: the old start counted again here
        save_json(actions.cache_file(), cache)
        calls = len(self.gh_calls())
        self.start()
        self.s.git("config", "core.hooksPath", ".githooks")  # the project's own hooks: the prompt-line fallback
        self.assertEqual(actions.worker_git(self.p, self.wt), ({}, ""))  # 10 of 90 cached
        self.assertEqual(len(self.gh_calls()), calls)

    def test_the_prompt_line_fallback_reads_the_cache(self):
        self.s.git("config", "core.hooksPath", ".githooks")
        self.assertIn(actions.SKIP, actions.worker_git(self.p, self.wt)[1])  # nothing counted yet
        self.assertEqual(self.gh_calls(), [])

    def test_a_session_ending_starts_a_recount(self):
        self.assertFalse(actions.cache_file().exists())
        runner.run_claude(self.p, "reviewer", "reviewer-run", "hi", "# windvane test", self.s.root)
        self.assertTrue(actions.counting.acquire(timeout=10))  # the background count, done
        actions.counting.release()
        self.assertIn(["repo", "list"], [json.loads(c)[:2] for c in self.gh_calls()])
        self.assertEqual(actions.cached()["used"], 10)


if __name__ == "__main__":
    unittest.main()
