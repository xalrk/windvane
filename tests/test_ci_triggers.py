"""Testing every Python here instead of on hosted CI: tools/check_pythons.sh on a trimmed PATH with fake Pythons, and
`windvane doctor`'s warning for a workflow that runs on every push or pull request (it spends the user's minutes)."""
import contextlib
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from helpers import Sandbox
from test_install import REPO, fake_bin

sys.path.insert(0, str(REPO))
from windvane import doctor  # noqa: E402
from windvane.config import Project  # noqa: E402

SCRIPT = REPO / "tools/check_pythons.sh"
# What the script may use besides the Pythons: a PATH of only these keeps the machine's real python3.1x out.
TOOLS = ("bash", "sh", "dirname", "mktemp", "rm", "mkdir", "ln", "grep", "tr", "tail")
# A fake Python passes only when the run's `python3` is itself, as the tests' `windvane` and tools rely on.
FAKE_PY = """#!/bin/sh
[ "$1" = "--version" ] && {{ echo "fake {v}"; exit 0; }}
[ "$(python3 --version)" = "fake {v}" ] || {{ echo "FAILED (python3 isn't me)"; exit 1; }}
echo "args: $*"
echo "Ran 3 tests in 0.1s"
echo
echo "{result}"
{exit}
"""


class CheckPythons(unittest.TestCase):
    def run_script(self, *args, **pythons):
        """The script's output and exit code with a PATH of the basic tools and the fake Pythons given as
        python3_11="OK" (or a failing "FAILED (failures=1)")."""
        bindir = Path(tempfile.mkdtemp(prefix="windvane-pythons-"))
        self.addCleanup(shutil.rmtree, bindir)
        for tool in TOOLS:
            (bindir / tool).symlink_to(shutil.which(tool))
        for name, result in pythons.items():
            v = name.removeprefix("python").replace("_", ".")
            exit_line = "exit 0" if result == "OK" else "exit 1"
            (bindir / f"python{v}").write_text(FAKE_PY.format(v=v, result=result, exit=exit_line))
            (bindir / f"python{v}").chmod(0o755)
        r = subprocess.run([str(SCRIPT), *args], capture_output=True, text=True, timeout=60,
                           env={"PATH": str(bindir), "HOME": str(bindir)})
        return r.stdout + r.stderr, r.returncode

    def test_a_missing_version_is_named_as_not_installed(self):
        """Not "skipped": that word is unittest's, for tests it skipped, in each version's summary line."""
        out, code = self.run_script(python3_11="OK", python3_13="OK")
        self.assertEqual(code, 0, out)
        self.assertIn("python3.11: Ran 3 tests in 0.1s OK\n", out)
        self.assertIn("not installed: python3.12 python3.14\n", out)
        self.assertNotIn("skipped", out)
        self.assertTrue(out.endswith("passed on: python3.11 python3.13\n"), out)

    def test_one_failing_version_fails_the_check_and_shows_its_output(self):
        out, code = self.run_script(python3_12="OK", python3_14="FAILED (failures=1)")
        self.assertEqual(code, 1, out)
        self.assertIn("python3.14: Ran 3 tests in 0.1s FAILED (failures=1)\n", out)
        self.assertIn("--- python3.14, the end of its output:\nargs: -m unittest discover -s tests -q\n", out)
        self.assertNotIn("--- python3.12", out)
        self.assertTrue(out.endswith("FAILED on: python3.14\n"), out)

    def test_arguments_go_to_unittest(self):
        out, code = self.run_script("tests.test_units", python3_11="FAILED")
        self.assertEqual(code, 1)
        self.assertIn("args: -m unittest tests.test_units\n", out)

    def test_no_supported_python_fails(self):
        out, code = self.run_script()
        self.assertEqual(code, 1)
        self.assertIn("no supported Python on PATH (looked for python3.11 python3.12 python3.13 python3.14)", out)


ON_PUSH_AND_PR = """name: tests
on:
  push:
    branches: [main]
  pull_request:
jobs:
  lint:
    runs-on: ubuntu-latest
    steps:
      - run: make lint
  test:
    runs-on: ${{ matrix.os }}
    strategy:
      matrix:
        os: [ubuntu-latest, windows-latest]
        python:
          - "3.12"
          - "3.13"
          - "3.14"
        include:
          - os: macos-latest
            python: "3.13"
    steps:
      - run: make test
"""


class DoctorOnWorkflows(unittest.TestCase):
    def setUp(self):
        self.s = Sandbox()
        self.addCleanup(self.s.close)
        self.fakes = fake_bin(self.s.tmp / "fakes")  # doctor runs `claude` and `gh`: fakes only, no system dirs
        (self.fakes / "git").symlink_to(shutil.which("git"))
        (self.fakes / "windvane").symlink_to(REPO / "bin/windvane")

    def doctor(self, **workflows):
        for name, text in workflows.items():
            self.s.write(f".github/workflows/{name}", text)
        with mock.patch.dict(os.environ, {"PATH": str(self.fakes)}), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            code = doctor.main(Project(self.s.root))
        return out.getvalue(), code

    def test_a_workflow_on_push_and_pull_request_is_named_with_its_jobs_per_ticket(self):
        out, code = self.doctor(**{"test.yml": ON_PUSH_AND_PR})
        self.assertEqual(code, 0, out)  # a warning, not a gap: windvane still runs
        # lint 1 + test 2 x 3 + 1 include = 8 jobs a run; a PR and a merge push = 16 a ticket.
        self.assertRegex(out, r"WARNING +\.github/workflows/test\.yml runs on push and pull_request: about 16 hosted "
                              r"jobs a ticket \(8 a run, for its PR and again "
                              r"for its merge\)  -> keep only "
                              r"`on: workflow_dispatch` and run it by hand \(`gh workflow run test\.yml`\)")

    def test_inline_triggers_and_a_push_only_workflow(self):
        out, _ = self.doctor(**{"a.yaml": '"on": [push]\njobs:\n  build:\n    runs-on: x\n',
                                "b.yml": "on: pull_request_target\njobs:\n  x:\n    runs-on: y\n"})
        self.assertRegex(out, r"WARNING +\.github/workflows/a\.yaml runs on push: about 1 hosted jobs a ticket "
                              r"\(1 a run, for its merge\)")
        self.assertRegex(out, r"WARNING +\.github/workflows/b\.yml runs on pull_request_target: about 1 ")

    def test_jobs_it_cant_count_are_not_called_zero(self):
        out, _ = self.doctor(**{"c.yml": "on: push\njobs: {build: {runs-on: x}}\n"})
        self.assertIn("c.yml runs on push: hosted jobs on every ticket, for its merge (doctor can't count them", out)

    def test_a_manual_only_workflow_and_this_repos_own_are_quiet(self):
        out, _ = self.doctor(**{"test.yml": ON_PUSH_AND_PR.replace(
            "  push:\n    branches: [main]\n  pull_request:\n", "  workflow_dispatch:\n"),
            "nightly.yml": "on:\n  schedule:\n    - cron: '0 3 * * *'\njobs:\n  a:\n    runs-on: x\n"})
        self.assertNotIn("WARNING", out)
        self.assertEqual(doctor.ci_warnings(REPO), [])

    def test_no_workflows_folder_is_quiet(self):
        out, _ = self.doctor()
        self.assertNotIn("WARNING", out)


if __name__ == "__main__":
    unittest.main()
