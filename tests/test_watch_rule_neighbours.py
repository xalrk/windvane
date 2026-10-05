"""T-081: a `---` rule is sized from the blocks just above and below it, not from the whole message, so a rule
beside a narrow table matches the table even when other prose fills the terminal."""
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from windvane import watch  # noqa: E402

SPEC = importlib.util.spec_from_file_location("watch_sweep", ROOT / "tools" / "watch-sweep.py")
sweep = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sweep)

PROSE = "word " * 60  # wraps at the full width
TABLE = "| Name | Value |\n|---|---|\n| " + "a" * 32 + " | 1 |"  # 40 columns drawn


def rule_widths(text, cols=120):
    plain = [watch.ANSI.sub("", line) for line in watch.markdown(text, True, cols)]
    return [watch.width(p) for p in plain if p and set(p) == {"─"}]


def table_width(text, cols=120):
    plain = [watch.ANSI.sub("", line) for line in watch.markdown(text, True, cols)]
    return watch.width(next(p for p in plain if "┼" in p))


class RuleNeighbours(unittest.TestCase):
    def test_the_table_is_40_columns_and_the_prose_fills_the_line(self):
        self.assertEqual(table_width(TABLE), 40)
        self.assertEqual(max(map(len, watch.markdown(PROSE, False, 120))), 119)  # the next word would pass 120

    def test_prose_above_a_table_below_takes_the_tables_width(self):
        self.assertEqual(rule_widths(PROSE + "\n\n---\n\n" + TABLE), [40])

    def test_a_table_above_prose_below_takes_the_proses_width(self):
        self.assertEqual(rule_widths(TABLE + "\n\n---\n\n" + PROSE), [119])

    def test_between_two_tables_the_wider_one(self):
        wide = "| Key | Meaning |\n|---|---|\n| k | " + "m" * 50 + " |"
        self.assertEqual(rule_widths(TABLE + "\n\n---\n\n" + wide), [table_width(wide)])
        self.assertEqual(rule_widths(wide + "\n\n---\n\n" + TABLE), [table_width(wide)])

    def test_two_rules_in_one_message_differ(self):
        text = PROSE + "\n\n---\n\n" + TABLE + "\n\n---\n\nshort tail"
        self.assertEqual(rule_widths(text), [40, 40])
        self.assertEqual(rule_widths(TABLE + "\n\n---\n\n" + PROSE + "\n\n---\n\n" + TABLE), [119, 40])

    def test_rules_with_no_blank_lines_stop_at_each_other(self):
        self.assertEqual(rule_widths(PROSE + "\n---\n" + TABLE + "\n---\nDone."), [40, 40])
        self.assertEqual(rule_widths(TABLE + "\n---\n" + PROSE + "\n---\n" + TABLE), [119, 40])

    def test_prose_far_away_doesnt_count(self):
        text = PROSE + "\n\nnear\n\n---\n\nafter"
        self.assertEqual(rule_widths(text), [len("after")])

    def test_a_rule_at_the_start_or_end_takes_its_one_neighbour(self):
        self.assertEqual(rule_widths("---\n\n" + TABLE), [40])
        self.assertEqual(rule_widths(TABLE + "\n\n---"), [40])

    def test_piped_output_stays_40(self):
        for text in (PROSE + "\n\n---\n\n" + TABLE, TABLE + "\n\n---\n\n" + PROSE):
            rules = [line for line in watch.markdown(text, False) if line and set(line) == {"─"}]
            self.assertEqual([len(r) for r in rules], [40])


class SweepSynthetic(unittest.TestCase):
    def run_sweep(self, *args):
        state = Path(tempfile.mkdtemp())
        (state / "runs").mkdir()
        event = {"type": "assistant", "message": {"content": [{"type": "text", "text": "fine"}]}}
        (state / "runs" / "20261005-000000-T-001.jsonl").write_text(json.dumps(event) + "\n")
        return subprocess.run([sys.executable, str(ROOT / "tools" / "watch-sweep.py"), str(state), *args],
                              capture_output=True, text=True)

    def test_the_synthetic_messages_are_clean_at_every_width(self):
        r = self.run_sweep()
        self.assertEqual((r.returncode, r.stdout), (0, "1 messages + 5 synthetic at widths 60,80,120: clean\n"))
        self.assertEqual(self.run_sweep("--no-synthetic").stdout, "1 messages at widths 60,80,120: clean\n")

    def test_a_rule_sized_from_the_whole_message_is_named_by_its_synthetic_number(self):
        whole = lambda out, r, tables, cols: min(cols, max(watch.width(watch.ANSI.sub("", line)) for b in out if b
                                                          for line, _ in b))
        state = Path(tempfile.mkdtemp())
        (state / "runs").mkdir()
        (state / "runs" / "x.jsonl").write_text(json.dumps(
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "fine"}]}}) + "\n")
        out = StringIO()
        with mock.patch.object(watch, "rule_width", whole), redirect_stdout(out):
            self.assertEqual(sweep.main([str(state), "--widths", "120"]), 1)
        self.assertTrue(out.getvalue().startswith("synthetic:1 @120: rule past text (116)\n"), out.getvalue())

    def test_a_rule_wider_than_both_neighbours_is_a_finding(self):
        block = lambda *lines: [(line, True) for line in lines]
        blank, rule = block(""), block("─" * 30)
        parts = [block("wide " * 10), blank, block("short"), blank, rule, blank, block("tiny")]
        self.assertEqual(sweep.rule_past_text(parts), [("rule past text (30)", "─" * 30)])
        self.assertEqual(sweep.rule_past_text([block("wide " * 10), rule, block("short"), rule, block("tiny")]),
                         [("rule past text (30)", "─" * 30)], "the second stops at the first")
        self.assertEqual(sweep.rule_past_text([block("─" * 40)]), [], "nothing beside it")
        self.assertEqual(sweep.rule_past_text([block("a long record line", "─" * 18, "short")]), [], "records' own")


if __name__ == "__main__":
    unittest.main()
