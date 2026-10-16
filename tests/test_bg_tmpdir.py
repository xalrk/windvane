"""A path under `$TMPDIR` means the same file to a session's sandboxed commands and to its `bg` and `heavy` jobs:
Claude Code gives a sandboxed command its own dir under the session's TMPDIR (`claude-<uid>`), while `windvane` runs
outside the sandbox, where TMPDIR is the parent. A job gets the sandbox's dir as its TMPDIR, and an argument the
shell expanded to the outside twin of a file in the sandbox's dir is refused with the path to pass."""
import io
import os
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from windvane import cli, heavy, jobs, runner, scratch  # noqa: E402
from helpers import Sandbox  # noqa: E402


class BgTmpdir(unittest.TestCase):
    def setUp(self):
        self.s = Sandbox()
        self.addCleanup(self.s.close)
        self.p = SimpleNamespace(state=self.s.tmp / "state")
        self.outer = self.s.tmp / "tmp" / "T-1"  # the session's TMPDIR, as the runner sets it
        self.inner = self.outer / f"claude-{os.getuid()}"  # its sandboxed commands' TMPDIR
        self.inner.mkdir(parents=True)
        os.environ.update(TMPDIR=str(self.outer), CLAUDECODE="1", CLAUDE_TMPDIR=str(self.inner))

    def bg(self, name, *cmd):
        out = io.StringIO()
        with redirect_stdout(out):
            jobs.bg(self.p, name, list(cmd), str(self.s.root))
        return out.getvalue()

    def job_output(self, name, *cmd):
        self.assertIn("started", self.bg(name, *cmd))
        out = io.StringIO()
        with redirect_stdout(out):
            jobs.await_(self.p, name, 20, 30)
        self.assertIn(f"DONE {name} exit 0", out.getvalue())
        return jobs.job_base(self.p, name).with_suffix(".log").read_text()

    def test_a_job_sees_the_sandboxed_commands_tmpdir(self):
        self.assertEqual(self.job_output("t", "bash", "-c", 'echo "$TMPDIR"').strip(), str(self.inner))

    def test_claude_code_tmpdir_wins_as_it_does_for_the_sandbox(self):
        own = self.s.tmp / "own"
        own.mkdir()
        os.environ["CLAUDE_CODE_TMPDIR"] = str(own)
        self.assertEqual(self.job_output("t", "bash", "-c", 'echo "$TMPDIR"').strip(), str(own))

    def test_without_claude_tmpdir_it_finds_claude_codes_dir_under_tmpdir(self):
        del os.environ["CLAUDE_TMPDIR"]
        self.assertEqual(self.job_output("t", "bash", "-c", 'echo "$TMPDIR"').strip(), str(self.inner))

    def test_outside_a_session_the_job_keeps_tmpdir(self):
        for k in ("CLAUDECODE", "CLAUDE_TMPDIR"):
            del os.environ[k]
        self.assertEqual(self.job_output("t", "bash", "-c", 'echo "$TMPDIR"').strip(), str(self.outer))

    def refused(self, arg, twin):
        with self.assertRaises(SystemExit) as e:
            self.bg("t", "cat", arg)
        self.assertEqual(str(e.exception), f"bg: `{arg}` isn't there outside the sandbox; the file is `{twin}`: "
                                           "pass that path")
        self.assertFalse(jobs.job_base(self.p, "t").parent.exists(), "a refused job leaves nothing behind")

    def test_the_outside_twin_of_a_sandbox_file_is_refused_with_the_full_path(self):
        (self.inner / "v").mkdir()
        (self.inner / "v" / "in.txt").write_text("hi\n")
        self.refused(str(self.outer / "v" / "in.txt"), self.inner / "v" / "in.txt")
        self.refused(f"--input={self.outer / 'v' / 'in.txt'}", self.inner / "v" / "in.txt")
        # an output whose folder is only in the sandbox's dir: the job couldn't write it
        self.refused(str(self.outer / "v" / "eval.log"), self.inner / "v" / "eval.log")

    def test_a_symlinked_tmpdir_is_seen_through(self):
        (self.inner / "in.txt").write_text("hi\n")
        link = self.s.tmp / "link"
        link.symlink_to(self.outer)
        os.environ["TMPDIR"] = str(link)
        self.refused(str(self.outer / "in.txt"), self.inner / "in.txt")

    def test_heavy_refuses_it_too_and_runs_with_the_sandboxes_tmpdir(self):
        (self.inner / "in.txt").write_text("hi\n")
        self.addCleanup(os.chdir, os.getcwd())  # back before the next test: the Sandbox's close removes its root
        os.chdir(self.s.root)
        with mock.patch.object(heavy, "run", return_value=0) as run, \
                self.assertRaisesRegex(SystemExit, "^heavy: `.*` isn't there outside the sandbox"):
            cli.main(["heavy", "--", "cat", str(self.outer / "in.txt")])
        run.assert_not_called()
        seen = {}
        with mock.patch.object(heavy, "run", side_effect=lambda *a, **k: seen.setdefault("t", os.environ["TMPDIR"])
                               and 0), self.assertRaises(SystemExit):
            cli.main(["heavy", "--", "cat", str(self.inner / "in.txt")])
        self.assertEqual(seen["t"], str(self.inner))

    def test_existing_paths_and_paths_outside_tmpdir_pass_untouched(self):
        (self.inner / "in.txt").write_text("inner\n")
        (self.outer / "in.txt").write_text("outer\n")  # there on both sides: the one named is the one read
        args = [str(self.outer / "in.txt"), str(self.inner / "in.txt"), str(self.s.root / "README"),
                str(self.outer / "new.log"), "--x=1", "plain"]
        out = self.job_output("t", "bash", "-c", 'for a; do echo "$a"; done', "_", *args)
        self.assertEqual(out.split(), args)


class SessionEnv(unittest.TestCase):
    def test_a_session_never_inherits_its_starters_sandbox_dir(self):
        """Claude Code keeps a CLAUDE_TMPDIR it inherits: a session started from another would share its dir."""
        with mock.patch.dict(os.environ, {"CLAUDE_TMPDIR": "/elsewhere/claude-1"}):
            self.assertNotIn("CLAUDE_TMPDIR", runner.env({"TMPDIR": "/x"}))


if __name__ == "__main__":
    unittest.main()
