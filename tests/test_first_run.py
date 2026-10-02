"""A new user's first steps, as a desk test on a clean home found them: init's forge, an unpushed ticket, install
from a Claude session's Bash (no terminal)."""
import argparse
import io
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from windvane import cli, install  # noqa: E402
from windvane.config import Project  # noqa: E402
from helpers import Sandbox  # noqa: E402


class FirstRun(unittest.TestCase):
    def setUp(self):
        self.s = Sandbox()
        self.addCleanup(self.s.close)

    def init(self, url, gh=True, forge=None):
        """A fresh repo with `origin` at `url`; init's output and the forge it wrote."""
        root = self.s.tmp / f"new{len(list(self.s.tmp.glob('new*')))}"
        root.mkdir()
        self.s.git("init", "-q", "-b", "main", cwd=root)
        if url:
            self.s.git("remote", "add", "origin", url, cwd=root)
        out = io.StringIO()
        with mock.patch("windvane.cli.shutil.which", lambda n: "/usr/bin/gh" if gh and n == "gh" else None), \
                redirect_stdout(out):
            cli.cmd_init(argparse.Namespace(dir=str(root), name=None, check="true", forge=forge))
        return out.getvalue(), Project(root).cfg["forge"]

    def test_init_picks_github_only_for_a_github_remote_with_gh(self):
        self.assertEqual(self.init("git@github.com:me/x.git")[1], "github")
        self.assertEqual(self.init("https://github.com/me/x", gh=False)[1], "git")
        self.assertEqual(self.init(str(self.s.remote))[1], "git")
        out, forge = self.init("https://gitlab.com/me/x.git")
        self.assertEqual(forge, "git")
        self.assertIn("forge: git (from the remote", out)
        self.assertEqual(self.init(None)[1], "git")
        self.assertEqual(self.init(str(self.s.remote), forge="github")[1], "github")  # --forge still wins

    def test_queue_names_tickets_not_pushed_yet(self):
        self.s.ticket("T-001")
        self.s.publish()
        self.s.ticket("T-002")
        out = io.StringIO()
        with redirect_stdout(out):
            cli.cmd_queue(argparse.Namespace(), Project(self.s.root))
        self.assertIn("T-001", out.getvalue())
        self.assertIn("not on origin/main yet (the autopilot can't see them): T-002; commit and push", out.getvalue())

    def test_install_without_a_terminal_names_the_flag(self):
        out = io.StringIO()
        with mock.patch("sys.stdin", io.StringIO()), redirect_stdout(out):
            self.assertFalse(install.ask("add the hook?", False))
        self.assertIn("`windvane install --yes`", out.getvalue())


if __name__ == "__main__":
    unittest.main()
