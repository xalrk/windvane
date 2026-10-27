import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from windvane import runner  # noqa: E402
from windvane.config import Project  # noqa: E402
from helpers import Sandbox  # noqa: E402


class WorkerEffort(unittest.TestCase):
    """T-326: the worker thinks at medium effort unless the project names its own; the other roles stay at high."""

    def effort(self, role, config_extra=""):
        """The `--effort` the Sandbox's fake `claude` gets for a `role` session in a project with this config."""
        s = Sandbox(config_extra)
        self.addCleanup(s.close)
        runner.run_claude(Project(s.root), role, "x", "hi", "", s.root)
        flags = json.loads((s.tmp / "calls.jsonl").read_text().splitlines()[-1])["flags"]
        return flags[flags.index("--effort") + 1]

    def test_a_project_with_no_worker_effort_runs_its_worker_at_medium(self):
        self.assertEqual(self.effort("worker"), "medium")

    def test_a_project_that_sets_high_keeps_it(self):
        self.assertEqual(self.effort("worker", '[models]\nworker_effort = "high"\n'), "high")

    def test_the_other_roles_stay_at_high(self):
        for role in ("reviewer", "critic", "planner", "fix"):
            with self.subTest(role=role):
                self.assertEqual(self.effort(role), "high")


if __name__ == "__main__":
    unittest.main()
