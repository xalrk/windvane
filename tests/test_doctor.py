"""windvane doctor against fake tools on PATH, and the parts of windvane that must degrade off Linux."""
import contextlib
import io
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from test_install import REPO, fake_bin

sys.path.insert(0, str(REPO))
from windvane import doctor, heavy  # noqa: E402

LOGGED_OUT = '#!/bin/sh\necho \'{"loggedIn": false}\'\nexit 1\n'
GITHUB = SimpleNamespace(exists=True, cfg={"forge": "github"})


class Doctor(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="windvane-doctor-"))
        (self.tmp / "bin").mkdir()
        (self.tmp / "bin/windvane").symlink_to(REPO / "bin/windvane")
        (self.tmp / "bin/git").symlink_to(shutil.which("git"))  # PATH has no system dir: a real gh there can't leak in
        patch = mock.patch.dict(os.environ, {"HOME": str(self.tmp / "home"),
                                             "CLAUDE_CONFIG_DIR": str(self.tmp / "claude")})
        patch.start()
        self.addCleanup(patch.stop)

    def run_doctor(self, p=None, **fakes):
        """doctor's output and exit code with the fakes (minus any set to None) first on PATH."""
        bindir = fake_bin(self.tmp / "fakes", **{k: v for k, v in fakes.items() if v})
        for name, text in fakes.items():
            if text is None:
                (bindir / name).unlink()
        with mock.patch.dict(os.environ, {"PATH": f"{bindir}:{self.tmp / 'bin'}"}), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            code = doctor.main(p)
        return out.getvalue(), code

    def test_all_set_with_the_tools_logged_in(self):
        out, code = self.run_doctor(GITHUB)
        self.assertEqual(code, 0, out)
        self.assertIn("all set", out)
        self.assertRegex(out, r"optional +the windvane skill .*-> windvane install")
        self.assertRegex(out, r"optional +phone pushes")

    def test_claude_logged_out_says_how_to_log_in(self):
        out, code = self.run_doctor(claude=LOGGED_OUT)
        self.assertEqual(code, 1)
        self.assertRegex(out, r"MISSING +Claude Code logged in +-> claude auth login")

    def test_a_claude_without_the_session_flags_says_to_update_it(self):
        old = ('#!/bin/sh\n[ "$1" = "--help" ] && echo "--effort --allowedTools --disallowed-tools"\n'
               'echo \'{"loggedIn": true}\'\n')
        out, code = self.run_doctor(claude=old)
        self.assertEqual(code, 1)
        self.assertRegex(out, r"MISSING +Claude Code new enough \(.* lacks --setting-sources, .*--tools, "
                              r"--disable-slash-commands.*\) +-> claude update")
        self.assertNotIn("lacks --effort", out)

    def test_no_claude_says_how_to_install_it(self):
        out, code = self.run_doctor(claude=None)
        self.assertEqual(code, 1)
        self.assertRegex(out, r"MISSING +Claude Code \(`claude`\) +-> curl -fsSL https://claude.ai/install.sh")

    def test_gh_needed_only_for_a_github_project(self):
        out, code = self.run_doctor(None, gh=None)
        self.assertEqual(code, 0, out)
        self.assertRegex(out, r"optional +GitHub CLI .*-> install gh")
        out, code = self.run_doctor(GITHUB, gh="#!/bin/sh\nexit 1\n")
        self.assertEqual(code, 1)
        self.assertRegex(out, r"MISSING +gh logged in +-> gh auth login")

    def test_a_broken_skill_link_says_so(self):
        skill = self.tmp / "claude/skills/windvane"
        skill.parent.mkdir(parents=True)
        skill.symlink_to(self.tmp / "gone/skills/windvane")
        out, _ = self.run_doctor()
        self.assertRegex(out, r"optional +the windvane skill .*: a broken link to .*gone.* -> windvane install")

    def test_off_linux_it_says_what_degrades(self):
        with mock.patch.object(sys, "platform", "darwin"):
            out, code = self.run_doctor()
        self.assertEqual(code, 0, out)
        self.assertIn("this is darwin", out)


class OffLinux(unittest.TestCase):
    def test_heavy_without_meminfo_or_pgrep(self):
        with mock.patch.object(heavy, "Path", side_effect=lambda *a: Path("/nonexistent/meminfo")):
            self.assertIsNone(heavy.free_gb())
        with mock.patch.dict(os.environ, {"PATH": tempfile.mkdtemp()}):
            self.assertIsNone(heavy.busy(["blender"]))


if __name__ == "__main__":
    unittest.main()
