"""A question's `Recommendation:`, `Built:` or `Waits for` line wrapped over several lines (workers wrap at the repo's
width) reads as one line: none of it is left in the context, and the reason and the place to change it are whole."""
import contextlib
import io
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from windvane import cli, preflight, questions  # noqa: E402
from helpers import Sandbox  # noqa: E402

WRAPPED = """# Questions

## Open

## Provisional
### T-007: Block reading the env file, or warn?
Workers could print the env file.
- **A**: block it in the sandbox
- **B**: warn in the prompt
Recommendation: A, because a plain cat is the most likely way it leaks, and
the sandbox stops it for good while a warning only asks.
Waits for the user: it changes what
every project's workers may read.
Built: A; to change it: the deny list in
windvane/sandbox.py

## Answered
"""
FLAT = """# Questions

## Open

## Provisional
### T-007: Block reading the env file, or warn?
Workers could print the env file.
- **A**: block it in the sandbox
- **B**: warn in the prompt
Recommendation: A, because the sandbox stops it for good.
Built: A; to change it: windvane/sandbox.py

## Answered
"""
WHY = "a plain cat is the most likely way it leaks, and the sandbox stops it for good while a warning only asks."


class Wrapped(unittest.TestCase):
    def setUp(self):
        self.x = preflight.provisionals(WRAPPED)[0]

    def test_no_continuation_in_the_context(self):
        self.assertEqual(self.x["context"], "Workers could print the env file.")

    def test_reason_and_place_are_whole(self):
        self.assertEqual((self.x["recommendation"], self.x["why"]), ("A", WHY))
        self.assertEqual((self.x["built"], self.x["change_at"]), ("A", "the deny list in windvane/sandbox.py"))
        self.assertEqual(self.x["options"], {"A": "block it in the sandbox", "B": "warn in the prompt"})

    def test_answered_block_has_no_half_line(self):
        out = preflight.file_answers(WRAPPED, [(self.x, "B", "")])
        (_, q, body), = questions.blocks(questions.part(out, "Answered"))
        self.assertEqual(q, "Block reading the env file, or warn?")
        self.assertEqual(body.splitlines()[:3], ["Workers could print the env file.",
                                                 "- **A**: block it in the sandbox", "- **B**: warn in the prompt"])
        for half in ("the sandbox stops it for good", "every project's", "windvane/sandbox.py"):
            self.assertNotIn(half, out)
        self.assertEqual(questions.provisional(out), [])

    def test_show_prints_the_whole_reason(self):
        shown = preflight.show(self.x, 1, 1)
        self.assertIn(f"A: block it in the sandbox   <- recommended, because {WHY}", shown)
        self.assertNotIn("\n  the sandbox stops", shown)
        self.assertNotIn("every project's", shown)


class Tty(io.StringIO):
    """A terminal the user types into."""

    def isatty(self):
        return True


class AtTheDesk(unittest.TestCase):
    """`windvane preflight` at a terminal shows the wrapped block whole and files the answer on main without it."""

    def setUp(self):
        self.s = Sandbox()
        self.s.ticket("T-007")
        self.s.write(".windvane/QUESTIONS.md", WRAPPED)
        self.s.publish("provisional")
        self.addCleanup(os.chdir, os.getcwd())
        self.addCleanup(setattr, sys, "stdin", sys.stdin)

    def tearDown(self):
        self.s.close()

    def test_preflight_shows_the_whole_reason_and_files_a_clean_block(self):
        sys.stdin, out = Tty("B\n"), io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.main(["-C", str(self.s.root), "preflight"])
        shown = out.getvalue()
        self.assertIn(f"A: block it in the sandbox   <- recommended, because {WHY}", shown)
        self.assertNotIn("every project's", shown)
        self.s.git("fetch", "-q", "origin")
        text = self.s.git("show", "origin/main:.windvane/QUESTIONS.md")
        (_, _, body), = questions.blocks(questions.part(text, "Answered"))
        self.assertEqual(body.splitlines()[:3], ["Workers could print the env file.",
                                                 "- **A**: block it in the sandbox", "- **B**: warn in the prompt"])
        self.assertIn("Answer: B (warn in the prompt). Changes the built A", body)
        self.assertNotIn("the sandbox stops it", text)
        followup = self.s.git("show", "origin/main:.windvane/tickets/T-008.md")
        self.assertIn("(where it lives: the deny list in windvane/sandbox.py)", followup)


class Flat(unittest.TestCase):
    def test_reads_as_before(self):
        x = preflight.provisionals(FLAT)[0]
        self.assertEqual({k: x[k] for k in ("context", "recommendation", "why", "built", "change_at")},
                         {"context": "Workers could print the env file.", "recommendation": "A",
                          "why": "the sandbox stops it for good.", "built": "A", "change_at": "windvane/sandbox.py"})

    def test_context_over_lines_stays(self):
        text = FLAT.replace("Workers could print the env file.\n", "Workers could print\nthe env file.\n")
        self.assertEqual(preflight.provisionals(text)[0]["context"], "Workers could print\nthe env file.")


if __name__ == "__main__":
    unittest.main()
