"""T-087: `watch` follows CommonMark on `---` under a line of text (a heading, not a rule), and tools/watch-sweep.py
sizes rules with `watch.beside`, flags rules cut short as well as too long, and runs on its own with no logs."""
import importlib.util
import subprocess
import sys
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from windvane import watch  # noqa: E402
from helpers import tmpdir  # noqa: E402

SPEC = importlib.util.spec_from_file_location("watch_sweep", ROOT / "tools" / "watch-sweep.py")
sweep = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sweep)

BOLD_HEAD = lambda s: f"\033[1;36m{s}\033[0m"
RULE = lambda n: f"\033[2m{'─' * n}\033[0m"


class Setext(unittest.TestCase):
    def test_a_line_over_dashes_is_a_bold_heading_with_no_rule(self):
        self.assertEqual(watch.markdown("Title\n---", True, 80), [BOLD_HEAD("Title")])
        self.assertEqual(watch.markdown("Title\n---\nbody", False, 80), ["Title", "body"])

    def test_after_a_blank_line_it_is_still_a_rule(self):
        self.assertEqual(watch.markdown("Text\n\n---", True, 80), ["Text", "", RULE(4)])

    def test_equals_and_a_whole_paragraph_take_the_heading_too(self):
        self.assertEqual(watch.markdown("One\nTwo\n===", True, 80), [BOLD_HEAD("One"), BOLD_HEAD("Two")])
        self.assertEqual(watch.markdown("One\n\nTwo\n--", False, 80), ["One", "", "Two"])

    def test_after_a_list_quote_heading_table_or_fence_it_is_a_rule(self):
        for text in ("- item", "> quoted", "## Head", "| a |\n|---|\n| b |", "```\ncode\n```"):
            plain = [watch.ANSI.sub("", line) for line in watch.markdown(text + "\n---", True, 80)]
            self.assertTrue(plain[-1] and set(plain[-1]) == {"─"}, (text, plain))

    def test_a_second_dash_line_under_a_heading_is_a_rule(self):
        self.assertEqual(watch.markdown("Title\n---\n---", False, 80), ["Title", "─" * 5])

    def test_equals_with_nothing_above_is_text(self):
        self.assertEqual(watch.markdown("===", False, 80), ["==="])


class SweepOnItsOwn(unittest.TestCase):
    def sweep(self, *args):
        state = tmpdir(self)
        return subprocess.run([sys.executable, str(ROOT / "tools" / "watch-sweep.py"), str(state), *args],
                              capture_output=True, text=True)

    def test_an_empty_state_dir_checks_the_synthetic_messages_and_passes(self):
        r = self.sweep()
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertTrue(r.stdout.endswith(f"0 messages + {len(sweep.SYNTHETIC)} synthetic at widths 60,80,120: "
                                          "clean\n"), r.stdout)

    def test_nothing_at_all_to_check_is_2(self):
        r = self.sweep("--no-synthetic")
        self.assertEqual(r.returncode, 2)
        self.assertIn("nothing to check", r.stdout)

    def test_a_synthetic_setext_heading_is_one(self):
        self.assertTrue(any("\n---\n" in t and not t.startswith(("-", "|")) for t in sweep.SYNTHETIC))


class RuleCutShort(unittest.TestCase):
    block = staticmethod(lambda *lines: [(line, True) for line in lines])

    def test_a_rule_shorter_than_both_neighbours_is_a_finding(self):
        b = self.block
        parts = [b("a" * 30), b(""), b("─" * 10), b(""), b("b" * 20)]
        self.assertEqual(sweep.rule_misfits(parts, 80), [("rule short of text (10)", "─" * 10)])

    def test_capped_at_cols_or_short_of_only_one_side_is_fine(self):
        b = self.block
        self.assertEqual(sweep.rule_misfits([b("a" * 30), b("─" * 20), b("b" * 30)], 20), [], "capped")
        self.assertEqual(sweep.rule_misfits([b("a" * 30), b("─" * 20), b("b" * 20)], 80), [], "as wide as one")
        self.assertEqual(sweep.rule_misfits([b("─" * 10), b("b" * 20)], 80), [], "one side only")

    def test_watch_drawing_rules_short_is_caught_by_number(self):
        short = lambda out, r, tables, cols: 3
        out = StringIO()
        with mock.patch.object(watch, "rule_width", short), redirect_stdout(out):
            self.assertEqual(sweep.main([str(tmpdir(self)), "--widths", "120"]), 1)
        self.assertIn("synthetic:1 @120: rule short of text (3)", out.getvalue())

    def test_beside_is_what_rule_width_sizes_by(self):
        parts = watch.blocks("short\n\n---\n\nlonger line", False, 80)
        self.assertEqual(watch.beside(parts, 2), ([5], [11]))
        self.assertEqual(watch.width(parts[2][0][0]), 11)


if __name__ == "__main__":
    unittest.main()
