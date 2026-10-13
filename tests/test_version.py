"""`windvane --version` and doctor's first line: a release's version for an install, a clone's commit for a checkout."""
import contextlib
import io
import subprocess
import sys
import unittest
from unittest import mock

import test_doctor
from helpers import tmpdir
from test_install import REPO

sys.path.insert(0, str(REPO))
from windvane import cli, doctor  # noqa: E402


def git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True).stdout.strip()


class Version(unittest.TestCase):
    def setUp(self):
        self.tmp = tmpdir(self, "windvane-version-")

    def clone(self, where):
        """A checkout laid out like windvane's (prompts/ at the top), committed; its short hash."""
        (where / "prompts").mkdir(parents=True)
        (where / "prompts/worker.md").write_text("rules\n")
        git(where, "init", "-q")
        git(where, "add", "-A")
        git(where, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-qm", "c")
        return git(where, "rev-parse", "--short", "HEAD")

    def line(self, home, checkout=True, installed="0"):
        from importlib.metadata import PackageNotFoundError
        meta = mock.patch("importlib.metadata.version", side_effect=lambda _: installed) if installed != "0" else \
            mock.patch("importlib.metadata.version", side_effect=PackageNotFoundError)
        with mock.patch.object(doctor, "HOME", home), mock.patch.object(doctor, "CHECKOUT", checkout), meta:
            return doctor.own_version()

    def test_an_installed_package_names_its_release(self):
        self.assertEqual(self.line(self.tmp / "site-packages/windvane", False, "2026.10.12"), "windvane 2026.10.12")

    def test_a_clone_names_its_commit(self):
        sha = self.clone(self.tmp / "wv")
        self.assertEqual(self.line(self.tmp / "wv", installed="2026.10.1"), f"windvane {sha} (clone)")

    def test_a_clone_with_changes_says_modified(self):
        sha = self.clone(self.tmp / "wv")
        (self.tmp / "wv/prompts/worker.md").write_text("changed\n")
        self.assertEqual(self.line(self.tmp / "wv"), f"windvane {sha} (clone, modified)")

    def test_a_copy_inside_another_repo_is_not_taken_for_a_clone(self):
        self.clone(self.tmp / "outer")
        (self.tmp / "outer/vendor/wv").mkdir(parents=True)
        self.assertEqual(self.line(self.tmp / "outer/vendor/wv", installed="2026.10.12"), "windvane 2026.10.12")
        self.assertIn("unknown version", self.line(self.tmp / "outer/vendor/wv"))

    def test_the_flag_prints_one_line_without_a_command(self):
        with contextlib.redirect_stdout(io.StringIO()) as out, self.assertRaises(SystemExit) as e:
            cli.main(["--version"])
        self.assertEqual(e.exception.code, 0)
        self.assertEqual(out.getvalue(), doctor.own_version() + "\n")

    def test_this_checkout_through_bin_windvane(self):
        out = subprocess.run([str(REPO / "bin/windvane"), "--version"], capture_output=True, text=True).stdout
        self.assertRegex(out, rf"^windvane {git(REPO, 'rev-parse', '--short', 'HEAD')} \(clone(, modified)?\)\n$")


class DoctorLine(unittest.TestCase):
    setUp = test_doctor.Doctor.setUp
    run_doctor = test_doctor.Doctor.run_doctor

    def test_doctor_starts_with_the_version(self):
        with mock.patch.object(doctor, "own_version", return_value="windvane 2026.10.12"):
            out, _ = self.run_doctor(test_doctor.GITHUB)
        self.assertEqual(out.splitlines()[0], "windvane 2026.10.12")


if __name__ == "__main__":
    unittest.main()
