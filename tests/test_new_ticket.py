"""`windvane new` takes the id after every ticket in the checkout it writes to, and never overwrites one."""
import contextlib
import io
import unittest
from unittest import mock

from helpers import Sandbox
from windvane import cli
from windvane.queue import Queue


class NewTicket(unittest.TestCase):
    def setUp(self):
        self.s = Sandbox()
        self.addCleanup(self.s.close)

    def new(self, root, *title):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.main(["-C", str(root), "new", *title])
        return out.getvalue().strip()

    def test_two_calls_before_a_commit_keep_both(self):
        first, second = self.new(self.s.root, "first"), self.new(self.s.root, "second")
        self.assertTrue(first.endswith("T-001.md") and second.endswith("T-002.md"), (first, second))
        tickets = self.s.root / ".windvane/tickets"
        self.assertIn("# T-001: first", (tickets / "T-001.md").read_text())
        self.assertIn("# T-002: second", (tickets / "T-002.md").read_text())

    def test_a_worktree_ahead_of_the_main_checkout(self):
        self.s.ticket("T-001")
        self.s.publish()
        wt = self.s.tmp / "wt"
        self.s.git("worktree", "add", "-q", "-b", "ahead", str(wt), "origin/main")
        for n in range(2, 6):
            (wt / f".windvane/tickets/T-00{n}.md").write_text(f"# T-00{n}: done {n}\n")
        self.s.git("add", "-A", cwd=wt)
        self.s.git("commit", "-qm", "four more", cwd=wt)
        before = {f.name: f.read_text() for f in (wt / ".windvane/tickets").iterdir()}
        out = self.new(wt, "sixth")
        self.assertEqual(out, str(wt / ".windvane/tickets/T-006.md"))
        self.assertEqual({n: (wt / ".windvane/tickets" / n).read_text() for n in before}, before)
        self.assertEqual(self.s.git("status", "--porcelain", cwd=wt), "?? .windvane/tickets/T-006.md")

    def test_an_id_merged_upstream_but_not_pulled(self):
        self.s.ticket("T-001")
        self.s.publish()
        self.s.ticket("T-002")
        self.s.git("add", "-A")
        self.s.git("commit", "-qm", "T-002")
        self.s.git("push", "-q", "origin", "main")
        self.s.git("reset", "-q", "--hard", "HEAD~1")  # the remote has T-002, this checkout doesn't yet
        self.s.git("fetch", "-q")
        self.assertTrue(self.new(self.s.root, "next").endswith("T-003.md"))

    def test_an_existing_file_is_refused(self):
        self.s.ticket("T-001")
        target = self.s.root / ".windvane/tickets/T-001.md"
        before = target.read_text()
        with mock.patch.object(Queue, "next_id", return_value="T-001"), \
                self.assertRaises(SystemExit) as e, contextlib.redirect_stdout(io.StringIO()) as out:
            cli.main(["-C", str(self.s.root), "new", "clash"])
        self.assertEqual(str(e.exception), f"{target} already exists: not overwritten")
        self.assertEqual(target.read_text(), before)
        self.assertEqual(out.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
