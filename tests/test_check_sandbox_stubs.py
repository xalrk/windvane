"""A check run inside a sandbox records its pass: the sandbox's placeholders (character devices bound over paths it
guards, never tracked by git) don't count as changes, while any real untracked file or change to a tracked one
still keeps the pass unrecorded, and the summary names it."""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from windvane import jobs  # noqa: E402
from windvane.config import Project  # noqa: E402
from helpers import Sandbox  # noqa: E402

STUBS = {".bashrc", "settings.json", ".mcp.json"}  # made as plain files; mknod for a real device needs root


class Base(unittest.TestCase):
    def setUp(self):
        self.s = Sandbox()
        self.addCleanup(self.s.close)
        self.p = Project(self.s.root)
        # by name, made as files, but only where the path really is: a lookup in the wrong dir finds nothing
        stub = mock.patch.object(jobs, "placeholder", lambda path, rel="": Path(path).name in STUBS and Path(path).exists())
        stub.start()
        self.addCleanup(stub.stop)

    def check(self):
        ok, summary, _ = jobs.check(self.p, self.s.root)
        self.assertTrue(ok, summary)
        return summary

    def placeholders(self):
        self.s.write(".bashrc", "")
        self.s.write(".claude/settings.json", "")  # a dir `normal` would list whole, as `.claude/`
        self.s.write(".mcp.json", "")


class Placeholders(Base):
    def test_a_clean_tree_with_placeholders_records_its_pass_and_the_next_check_reuses_it(self):
        self.placeholders()
        first = self.check()
        self.assertIn(" min", first)
        self.assertNotIn("not recorded", first)
        self.assertIn("reused the pass", self.check())

    def test_the_key_is_the_same_with_and_without_placeholders(self):
        """A pass recorded inside the sandbox is reused outside it, and the other way round."""
        bare = jobs.tree_key(self.p, self.s.root)
        self.placeholders()
        self.assertEqual(jobs.tree_key(self.p, self.s.root), bare)
        self.assertIsNotNone(bare)


class RealChanges(Base):
    def assert_unrecorded(self, *named):
        summary = self.check()
        self.assertIn("not recorded: " + ", ".join(named), summary)
        self.assertNotIn("reused", self.check())

    def test_a_real_untracked_file_among_placeholders_blocks_recording_and_is_named(self):
        self.placeholders()
        self.s.write(".claude/notes.md", "mine")
        self.assert_unrecorded(".claude/notes.md")

    def test_a_modified_tracked_file_blocks_recording_and_is_named(self):
        self.s.write("README", "changed\n")
        self.assert_unrecorded("README")

    def test_more_than_two_paths_name_the_first_two(self):
        for name in ("a", "b", "c"):
            self.s.write(f"src/{name}.py", "")
        summary = self.check()
        self.assertIn("not recorded: src/a.py, src/b.py, …)", summary)

    def test_a_rename_names_its_new_path_not_its_source_as_a_second(self):
        self.s.git("mv", "README", "README.md")
        self.s.write("README", "hi\n")  # the check needs it; untracked now
        self.assertIn("not recorded: README.md, README)", self.check())


class Real(unittest.TestCase):
    def test_placeholder_is_a_character_device_never_followed(self):
        self.assertTrue(jobs.placeholder("/dev/null"))
        self.assertFalse(jobs.placeholder(__file__))
        self.assertFalse(jobs.placeholder("/no/such/path"))
        with tempfile.TemporaryDirectory() as tmp:  # a link to a device is a file someone made: it counts
            link = Path(tmp) / "null"
            link.symlink_to("/dev/null")
            self.assertFalse(jobs.placeholder(link))
            dot = Path(tmp) / ".bashrc"  # nor a link to an empty dotfile
            dot.write_text("")
            (Path(tmp) / ".link").symlink_to(dot)
            self.assertFalse(jobs.placeholder(Path(tmp) / ".link", ".link"))
            self.assertTrue(jobs.placeholder(dot, ".bashrc"))



class FromASubdir(Base):
    """A worker's shell keeps its cwd between calls: a check from a subdirectory is the gate's check all the same."""
    def setUp(self):
        super().setUp()
        self.s.write("src/x.py", "")
        self.s.git("add", "-A")
        self.s.git("commit", "-qm", "src")

    def test_a_check_from_a_subdir_records_its_pass_past_the_placeholders(self):
        self.placeholders()
        ok, summary, _ = jobs.check(self.p, Path(self.s.root) / "src")  # its check, `test -f README`, needs the root
        self.assertTrue(ok, summary)
        self.assertNotIn("not recorded", summary)
        self.assertIn("reused the pass", self.check())

    def test_the_key_from_a_subdir_is_the_roots(self):
        """git status's paths are the top level's, whatever the cwd: the stub must look where the path really is."""
        self.placeholders()
        root = jobs.tree_key(self.p, self.s.root)
        self.assertIsNotNone(root)
        self.assertEqual(jobs.tree_key(self.p, Path(self.s.root) / "src"), root)

    def test_a_real_change_still_blocks_from_a_subdir_and_is_named_from_the_root(self):
        self.s.write("src/x.py", "changed")
        ok, summary, _ = jobs.check(self.p, Path(self.s.root) / "src")
        self.assertIn("not recorded: src/x.py)", summary)

    def test_outside_a_checkout_the_check_runs_in_the_cwd(self):
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.dict(os.environ, {"GIT_CEILING_DIRECTORIES": d}):  # git looks no further up
            (Path(d) / "here").mkdir()
            (Path(d) / "here" / "marker").write_text("")
            self.p.cfg["check"] = "test -f marker"
            ok, summary, _ = jobs.check(self.p, Path(d) / "here")
            self.assertTrue(ok, summary)

class OutsideTheSandbox(unittest.TestCase):
    """Seen from outside, a live sandbox's mount points are empty regular files: no character devices to skip."""
    def setUp(self):
        self.s = Sandbox()
        self.addCleanup(self.s.close)
        self.p = Project(self.s.root)

    def mount_points(self):
        for name in (".bashrc", ".gitconfig", ".vscode", ".claude/settings.json", ".claude/agents"):
            self.s.write(name, "")

    def test_empty_mount_points_dont_block_recording(self):
        bare = jobs.tree_key(self.p, self.s.root)
        self.mount_points()
        self.assertEqual(jobs.blockers(self.s.root), [])
        self.assertEqual(jobs.tree_key(self.p, self.s.root), bare)

    def test_a_real_empty_file_still_blocks(self):
        self.s.write("tests/__init__.py", "")
        self.assertEqual(jobs.blockers(self.s.root), ["tests/__init__.py"])

    def test_a_dotfile_with_content_still_blocks(self):
        self.s.write(".bashrc", "alias x=y\n")
        self.assertEqual(jobs.blockers(self.s.root), [".bashrc"])

    def test_a_real_file_beside_the_mount_points_is_the_only_one_named(self):
        self.mount_points()
        self.s.write("src/x.py", "")
        ok, summary, _ = jobs.check(self.p, self.s.root)
        self.assertTrue(ok, summary)
        self.assertIn("not recorded: src/x.py)", summary)
        self.mount_points()  # the pass now recorded for the clean tree is reused with them there
        (Path(self.s.root) / "src/x.py").unlink()
        self.assertNotIn("not recorded", jobs.check(self.p, self.s.root)[1])
        self.assertIn("reused the pass", jobs.check(self.p, self.s.root)[1])


if __name__ == "__main__":
    unittest.main()
