"""The worker's `gh` guard (T-071): first on a worker session's PATH, it refuses every way of starting, re-running or
enabling a workflow, however it's typed, and passes the rest to the real gh unchanged; `windvane ci` still reaches
the real one. All through a fake "real" gh that logs its calls, never the real one."""
import json
import os
import subprocess
import sys
import unittest
from unittest import mock
from argparse import Namespace
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from windvane import actions, driver  # noqa: E402
from windvane.config import Project  # noqa: E402
from helpers import Sandbox  # noqa: E402
from test_actions_budget import FAKE_GH  # noqa: E402

# The "real" gh for the pass-through tests: logs its arguments, prints a line, exits with FAKE_GH_EXIT.
ECHO_GH = r'''#!/usr/bin/env python3
import json, os, sys
with open(os.environ["FAKE_GH_LOG"], "a") as f:
    f.write(json.dumps(sys.argv[1:]) + "\n")
print("real gh:", " ".join(sys.argv[1:]))
sys.exit(int(os.environ.get("FAKE_GH_EXIT", "0")))
'''

REFUSED = [
    "gh workflow run test.yml",
    "gh workflow enable test.yml",
    "gh run rerun 123",
    "gh run rerun --failed 123",
    "gh run --repo me/proj rerun 123",
    "gh run -R me/proj rerun 123",
    "gh workflow -R me/proj run test.yml --ref main",
    "gh workflow --repo=me/proj run test.yml",
    "env gh workflow run test.yml",
    "command gh workflow run test.yml",
    "env FOO=1 gh run rerun 9",
    "python3 -c \"import subprocess, sys; sys.exit(subprocess.run(['gh', 'workflow', 'run', 'x']).returncode)\"",
    "gh api repos/me/proj/actions/workflows/test.yml/dispatches -f ref=main",
    "gh api -f ref=main repos/me/proj/actions/workflows/test.yml/dispatches",
    "gh api repos/me/proj/dispatches -f event_type=go",
    "gh api --method POST repos/me/proj/actions/runs/1/rerun",
    "gh api --method=POST repos/me/proj/actions/runs/1/rerun-failed-jobs",
    "gh api -X POST repos/me/proj/actions/runs/1/cancel",
    "gh api -XPOST repos/me/proj/actions/runs/1/cancel",
    "gh api repos/me/proj/actions/runs/1/cancel -X post",
    "gh api -F x=1 repos/me/proj/actions/variables",
    "gh api --input body.json repos/me/proj/actions/permissions",
    "gh api -X PUT repos/me/proj/actions/workflows/1/enable",
    "gh api repos/me/proj/actions/workflows/1/enable",
    "gh api https://api.github.com/repos/me/proj/actions/runs/1/rerun",
    "gh alias set wr 'workflow run'",
    # combined short flags, read as gh reads them: -if ref=main is -i -f ref=main
    "gh api -if ref=main repos/me/proj/actions/workflows/test.yml/dispatches",
    "gh api -iX POST repos/me/proj/actions/runs/1/rerun",
    "gh api -iXPOST repos/me/proj/actions/runs/1/cancel",
    "gh api -iX=POST repos/me/proj/actions/runs/1/cancel",
    "gh api -iF x=1 repos/me/proj/actions/variables",
    "gh api -iFx=1 repos/me/proj/actions/variables",
    "gh -R me/proj api -if ref=main repos/me/proj/dispatches",
]

PASSED = [
    ("gh pr view 12", ["pr", "view", "12"]),
    ("gh api repos/x/actions/runs", ["api", "repos/x/actions/runs"]),
    ("gh api -X GET repos/x/actions/runs -f per_page=100", ["api", "-X", "GET", "repos/x/actions/runs", "-f",
                                                           "per_page=100"]),
    ("gh run list --limit 5", ["run", "list", "--limit", "5"]),
    ("gh api -iX GET repos/x/actions/runs", ["api", "-iX", "GET", "repos/x/actions/runs"]),
    ("gh api -iq .total_count repos/x/actions/runs", ["api", "-iq", ".total_count", "repos/x/actions/runs"]),
    ("gh workflow list", ["workflow", "list"]),
    ("gh workflow view run", ["workflow", "view", "run"]),  # a workflow called "run" is only viewed
    ("gh api -X POST repos/x/issues/1/comments -f body='see actions/runs'",
     ["api", "-X", "POST", "repos/x/issues/1/comments", "-f", "body=see actions/runs"]),
    ("gh pr create --title 'a b' --body \"two\nlines\"", ["pr", "create", "--title", "a b", "--body", "two\nlines"]),
]


class Guard(unittest.TestCase):
    def setUp(self):
        self.s = Sandbox()
        self.real = self.s.tmp / "realbin"
        self.real.mkdir()
        os.environ["FAKE_GH_LOG"] = str(self.s.tmp / "gh.log")
        self.fake(ECHO_GH)
        cfg = self.s.root / ".windvane/config.toml"
        cfg.write_text(cfg.read_text().replace('forge = "git"', 'forge = "github"'))
        self.p = Project(self.s.root)
        self.assertEqual(self.p.cfg["forge"], "github")

    def tearDown(self):
        self.s.close()

    def fake(self, text):
        """The "real" gh, in a dir of its own on PATH (after the guard), as `gh` is on a machine."""
        gh = self.real / "gh"
        gh.write_text(text)
        gh.chmod(0o755)
        os.environ["PATH"] = f"{self.real}{os.pathsep}{os.environ['PATH']}"

    def worker_env(self):
        """The environment a worker session gets from the driver, guard included."""
        return {**os.environ, **actions.gh_guard(self.p, self.s.root)}

    def sh(self, command, env=None):
        r = subprocess.run(["bash", "-c", command], cwd=self.s.root, capture_output=True, text=True,
                           env=env or self.worker_env())
        return r.returncode, r.stdout, r.stderr

    def calls(self):
        f = Path(os.environ["FAKE_GH_LOG"])
        calls = [json.loads(x) for x in f.read_text().splitlines()] if f.exists() else []
        f.unlink(missing_ok=True)
        return calls

    def test_every_way_of_starting_a_workflow_is_refused_before_the_real_gh(self):
        env = self.worker_env()
        for command in REFUSED:
            with self.subTest(command):
                code, out, err = self.sh(command, env)
                self.assertEqual(code, 1, out + err)
                self.assertIn("`windvane ci [workflow]`", err)
                self.assertEqual(len(err.strip().splitlines()), 1, err)
                self.assertEqual(self.calls(), [])

    def test_everything_else_reaches_the_real_gh_unchanged(self):
        env = self.worker_env()
        for command, args in PASSED:
            with self.subTest(command):
                code, out, err = self.sh(command, env)
                self.assertEqual((code, err), (0, ""))
                self.assertEqual(out, f"real gh: {' '.join(args)}\n")
                self.assertEqual(self.calls(), [args])

    def test_the_real_gh_exit_code_comes_back(self):
        code, out, err = self.sh("gh pr view 12", {**self.worker_env(), "FAKE_GH_EXIT": "4"})
        self.assertEqual((code, out), (4, "real gh: pr view 12\n"))

    def test_the_guard_is_first_on_a_worker_sessions_path(self):
        env = actions.gh_guard(self.p, self.s.root)
        first = Path(env["PATH"].split(os.pathsep)[0], "gh")
        self.assertTrue(actions.is_guard(first))
        self.assertEqual(first.parent, self.p.state / "gh-guard")
        code, out, err = self.sh("command -v gh")
        self.assertEqual(out.strip(), str(first))

    def test_shell_setup_that_puts_the_real_gh_first_is_undone_by_the_env_file(self):
        # Claude Code's Bash tool: the user's shell setup first, then CLAUDE_ENV_FILE, then the command
        code, out, err = self.sh(f'PATH={self.real}:$PATH; . "$CLAUDE_ENV_FILE"; gh workflow run x')
        self.assertEqual(code, 1, out + err)
        self.assertEqual(self.calls(), [])

    def test_the_users_own_env_file_is_still_sourced(self):
        own = self.s.tmp / "own.env"
        own.write_text("export OWN_ENV=yes\n")
        os.environ["CLAUDE_ENV_FILE"] = str(own)
        code, out, err = self.sh('. "$CLAUDE_ENV_FILE"; echo "$OWN_ENV"; command -v gh')
        self.assertEqual(out.split(), ["yes", str(self.p.state / "gh-guard/gh")])

    def test_an_env_file_that_is_the_guards_own_is_not_sourced_by_itself(self):
        os.environ.update(actions.gh_guard(self.p, self.s.root))  # the driver started from a worker of this worktree
        env_file = Path(actions.gh_guard(self.p, self.s.root)["CLAUDE_ENV_FILE"])
        self.assertNotIn(". ", env_file.read_text())
        code, out, err = self.sh('. "$CLAUDE_ENV_FILE"; command -v gh')
        self.assertEqual((code, out.strip()), (0, str(self.p.state / "gh-guard/gh")))

    def test_a_python_path_with_spaces_or_over_a_shebangs_length_still_runs_the_guard(self):
        deep = self.s.tmp / "a venv" / ("x" * 200) / ("y" * 200)
        deep.mkdir(parents=True)
        (deep / "python3").symlink_to(sys.executable)
        with mock.patch.object(sys, "executable", str(deep / "python3")):
            env = self.worker_env()
        self.assertTrue(actions.is_guard(self.p.state / "gh-guard/gh"))
        self.assertEqual(self.sh("gh workflow run x", env)[0], 1)
        self.assertEqual(self.sh("gh run list", env)[:2], (0, "real gh: run list\n"))
        self.assertEqual(self.calls(), [["run", "list"]])

    def test_the_driver_gives_a_worker_session_the_guard(self):
        os.environ["FAKE_LOG"] = str(self.s.tmp / "claude.log")
        os.environ["FAKE_FAIL"] = "quiet"
        self.s.ticket("T-001")
        self.s.publish()
        pilot = driver.Pilot(self.p, Namespace(cap=75, week_cap=95, ticket_hours=1, min_slot=0, answer_wait=10,
                                               replan=0, goal="", parallel=1))
        pilot.deadline = datetime.now() + timedelta(hours=1)
        driver.ensure_worktree(self.p, pilot.wd, setup=False)
        pilot.work("T-001", "new", [])
        worker = [json.loads(x) for x in Path(os.environ["FAKE_LOG"]).read_text().splitlines()
                  if json.loads(x)["system"].startswith("# windvane worker run")]
        self.assertEqual([w["gh"] for w in worker], [str(self.p.state / "gh-guard/gh")])

    def test_a_guard_on_path_is_never_taken_for_the_real_gh(self):
        os.environ.update(actions.gh_guard(self.p, self.s.root))  # a session nested in a worker's writes its own guard
        nested = actions.gh_guard(self.p, self.s.root)["PATH"].split(os.pathsep)
        self.assertEqual(nested[0], nested[1])
        self.assertEqual(actions.real_gh(), str(self.real / "gh"))
        code, out, err = self.sh("gh run list")
        self.assertEqual((code, out), (0, "real gh: run list\n"))

    def test_no_real_gh_says_so(self):
        path = os.environ["PATH"]
        os.environ["PATH"] = str(self.s.bin)  # no gh at all when the guard is written (not even the machine's)
        actions.gh_guard(self.p, self.s.root)
        os.environ["PATH"] = path
        guard = self.p.state / "gh-guard"
        code, out, err = self.sh("gh pr view", {**os.environ, "PATH": f"{guard}{os.pathsep}{path}"})
        self.assertEqual(code, 127)
        self.assertIn("gh: not found", err)

    def test_forge_git_has_no_guard(self):
        cfg = self.s.root / ".windvane/config.toml"
        cfg.write_text(cfg.read_text().replace('forge = "github"', 'forge = "git"'))
        self.assertEqual(actions.gh_guard(Project(self.s.root), self.s.root), {})

    def test_claude_ci_inside_a_worker_still_reaches_the_real_gh(self):
        self.fake(FAKE_GH)
        data = {"repos": ["me/proj"], "runs": {}, "jobs": {},
                "workflows": {"me/proj": [{"name": "test", "path": ".github/workflows/test.yml", "state": "active"}]}}
        os.environ["FAKE_GH_DATA"] = str(self.s.tmp / "gh.json")
        Path(os.environ["FAKE_GH_DATA"]).write_text(json.dumps(data))
        env = self.worker_env()
        code, out, err = self.sh("windvane ci --dry-run", env)
        self.assertEqual(code, 0, out + err)
        self.assertIn("Dry run: would run gh workflow run test.yml --ref main", out)
        self.assertIn(["repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"], self.calls())
        code, out, err = self.sh("windvane ci", env)
        self.assertEqual(code, 0, out + err)
        self.assertIn("Started test.yml on main.", out)
        self.assertIn(["workflow", "run", "test.yml", "--ref", "main"], self.calls())
        code, out, err = self.sh("gh workflow run test.yml", env)  # the same session, typed by hand
        self.assertEqual(code, 1)
        self.assertEqual(self.calls(), [])


if __name__ == "__main__":
    unittest.main()
