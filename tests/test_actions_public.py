"""Only private (and internal) repos' Actions minutes are counted: GitHub runs a public repo's workflows on its
standard runners for free, so their runs neither raise the count nor make `windvane ci` refuse."""
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from windvane import actions  # noqa: E402
from helpers import Sandbox  # noqa: E402
import test_actions_budget as budget  # noqa: E402  (a module, so its tests aren't collected here again)
from test_actions_budget import FAKE_GH, NOW, job  # noqa: E402

FLOW = {"name": "test", "path": ".github/workflows/test.yml", "state": "active"}


class Public(unittest.TestCase):
    B = budget.Budget
    serve, add, gh_calls, project, ci = B.serve, B.add, B.gh_calls, B.project, B.ci

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
        self.s.close()

    def repos(self, *visible):
        """The login's repos, the first one the project's: (owner/name, visibility) each."""
        self.data["repos"] = [{"nameWithOwner": n, "visibility": v} for n, v in visible]
        self.serve()

    def started(self):
        return [c for c in self.gh_calls() if c[:2] == ["workflow", "run"]]

    def test_a_month_counts_only_the_private_repos_runs(self):
        self.repos(("me/app", "PRIVATE"), ("me/lib", "PUBLIC"), ("org/tool", "INTERNAL"))
        self.add("me/app", 1, [job(10 * 60)])
        self.add("me/lib", 2, [job(500 * 60), job(60 * 60, ["macos-15"])])  # free: 500 + 600 not billed
        self.add("org/tool", 3, [job(3 * 60, ["windows-latest"])])  # 6
        self.assertEqual(actions.count(self.s.root, NOW)["used"], 10 + 6)
        self.assertFalse([c for c in self.gh_calls() if "me/lib" in " ".join(c)], "a public repo's runs aren't read")

    def test_ci_on_a_public_repo_starts_past_the_cap_and_says_why(self):
        self.repos(("me/lib", "PUBLIC"), ("me/app", "PRIVATE"))
        self.project(minutes=2000)
        self.data["workflows"]["me/lib"] = [FLOW]
        self.add("me/app", 1, [job(1900 * 60)])  # the private total is past the 1,800 cap
        self.add("me/lib", 2, [job(900 * 60, ["macos-15"])])  # a costly last run, free on a public repo
        code, out = self.ci("test.yml", "--ref", "main")
        self.assertEqual(code, 0, out)
        self.assertIn("me/lib is public: GitHub runs its workflows on standard runners for free, so no estimate "
                      "or cap.", out)
        self.assertNotIn("Refused", out)
        self.assertEqual(self.started(), [["workflow", "run", "test.yml", "--ref", "main"]])

    def test_ci_on_a_public_repo_still_needs_an_active_workflow(self):
        self.repos(("me/lib", "PUBLIC"),)
        self.project(minutes=2000)
        self.data["workflows"]["me/lib"] = [dict(FLOW, state="disabled_manually")]
        self.serve()
        code, out = self.ci()
        self.assertEqual(code, 1, out)
        self.assertIn("test.yml is disabled manually on GitHub", out)
        self.assertFalse(self.started())

    def test_ci_on_a_private_repo_over_the_cap_still_refuses(self):
        self.repos(("me/app", "PRIVATE"), ("me/lib", "PUBLIC"))
        self.project(minutes=2000)
        self.data["workflows"]["me/app"] = [FLOW]
        self.add("me/app", 1, [job(1900 * 60)])
        code, out = self.ci("test.yml")
        self.assertEqual(code, 1, out)
        self.assertIn("1,900 of 2,000 min used; the cap is 1,800 (90%), 0 left.", out)
        self.assertIn("Refused: 1,900 + 1,900 = 3,800 min would pass the cap of 1,800.", out)
        self.assertNotIn("is public", out)
        self.assertFalse(self.started())


if __name__ == "__main__":
    unittest.main()
