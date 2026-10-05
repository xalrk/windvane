"""T-081: a `---` rule is sized from the blocks just above and below it, not from the whole message, so a rule
beside a narrow table matches the table even when other prose fills the terminal."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from windvane import watch  # noqa: E402

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


if __name__ == "__main__":
    unittest.main()
