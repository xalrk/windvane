"""A `windvane` command never reads a stale `$TMPDIR` file when the sandbox has a newer one under that name: in Claude
Code's sandbox `$TMPDIR` is `claude-<uid>` below the session's TMPDIR, so `cat > "$TMPDIR/req.md"` there and
`--body <session TMPDIR>/req.md` out here are two files, and the outer one may be an earlier command's. When they
differ (or only the sandbox's exists) the command stops before doing anything and names both."""
import contextlib
import io
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from windvane import cli, jobs, requests, scratch  # noqa: E402
from helpers import Sandbox  # noqa: E402


class Twin(unittest.TestCase):
    def setUp(self):
        self.s = Sandbox()
        self.addCleanup(self.s.close)
        self.outer = self.s.tmp / "tmp" / "T-1"  # the session's TMPDIR, as the runner sets it
        self.inner = self.outer / f"claude-{os.getuid()}"  # its sandboxed commands' TMPDIR
        self.inner.mkdir(parents=True)
        env = mock.patch.dict(os.environ, TMPDIR=str(self.outer), CLAUDECODE="1", CLAUDE_TMPDIR=str(self.inner))
        env.start()
        self.addCleanup(env.stop)
        self.addCleanup(os.chdir, os.getcwd())
        os.chdir(self.s.root)
        self.rel = f"claude-{os.getuid()}/req.md"

    def write(self, outer=None, inner=None):
        if outer is not None:
            (self.outer / "req.md").write_text(outer)
        if inner is not None:
            (self.inner / "req.md").write_text(inner)
        return str(self.outer / "req.md")

    def request(self, body):
        """(exit code, what it printed) of `windvane request ... --body <body>`."""
        out, code = io.StringIO(), 0
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            try:
                cli.main(["request", "critic: notes.md is cut without saying so", "--body", body])
            except SystemExit as e:
                code = e.code if isinstance(e.code, int) else e.code and (print(e.code, file=out) or 1) or 0
        return code, out.getvalue()

    def inbox(self):
        return {f.stem: requests.read(f) for f in requests.files(False)}

    def test_the_repro_names_the_sandbox_file(self):
        outer = self.write("old\n", "new\n")
        self.assertEqual(scratch.sandbox_twin(outer), self.inner / "req.md")

    def test_a_differing_outer_body_is_refused_and_nothing_is_filed(self):
        outer = self.write("the body of an earlier request\n", "the body meant\n")
        code, out = self.request(outer)
        self.assertNotEqual(code, 0)
        self.assertIn(outer, out)
        self.assertIn(self.rel, out)
        self.assertIn("pass the full path of the one meant", out)
        self.assertEqual(self.inbox(), {})

    def test_the_same_bytes_on_both_sides_files(self):
        code, out = self.request(self.write("the body\n", "the body\n"))
        self.assertEqual(code, 0, out)
        self.assertIn("filed", out)
        self.assertEqual(len(self.inbox()), 1)

    def test_a_missing_outer_body_names_its_twin(self):
        code, out = self.request(self.write(inner="the body\n"))
        self.assertNotEqual(code, 0)
        self.assertIn(f"the file is `{self.inner / 'req.md'}`: pass that path", out)
        self.assertNotIn("can't read the body", out)
        self.assertEqual(self.inbox(), {})

    def test_without_a_sandbox_dir_the_outer_body_files(self):
        outer = self.write("the body\n", "another\n")
        with mock.patch.dict(os.environ, CLAUDE_TMPDIR=str(self.s.tmp / "gone")):
            del os.environ["CLAUDECODE"]
            code, out = self.request(outer)
        self.assertEqual(code, 0, out)
        self.assertEqual(len(self.inbox()), 1)

    def test_directories_are_left_alone(self):
        (self.outer / "d").mkdir()
        (self.inner / "d").mkdir()
        (self.inner / "d" / "x").write_text("x\n")
        self.assertIsNone(scratch.sandbox_twin(str(self.outer / "d")))
        (self.outer / "f").mkdir()
        (self.inner / "f").write_text("a file\n")
        self.assertIsNone(scratch.sandbox_twin(str(self.outer / "f")))

    def test_a_bg_job_given_a_differing_outer_file_is_refused(self):
        p = SimpleNamespace(state=self.s.tmp / "state")
        outer = self.write("old\n", "new\n")
        with self.assertRaises(SystemExit) as e, contextlib.redirect_stdout(io.StringIO()):
            jobs.bg(p, "t", ["cat", f"--in={outer}"], str(self.s.root))
        self.assertIn(f"bg: `{outer}` and `{self.inner / 'req.md'}` differ", str(e.exception))
        self.assertFalse(jobs.job_base(p, "t").parent.exists(), "a refused job leaves nothing behind")


if __name__ == "__main__":
    unittest.main()
