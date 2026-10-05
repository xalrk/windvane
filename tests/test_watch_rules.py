"""T-074: a `---` rule is as wide as the message around it (at most the terminal), and a clipped message always
shows its first line of text before `… (+N lines)`."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from windvane import watch  # noqa: E402


def rules(lines):
    return [line for line in lines if line and set(line) == {"─"}]


class RuleWidth(unittest.TestCase):
    def test_a_rule_spans_cols_when_the_text_is_wider(self):
        lines = watch.markdown("x" * 100 + "\n\n---\n\nafter", False, 60)
        self.assertEqual([len(r) for r in rules(lines)], [60])

    def test_a_rule_spans_the_widest_line_when_that_is_narrower(self):
        lines = watch.markdown("a short line\n\n---\n\nshorter", False, 80)
        self.assertEqual([len(r) for r in rules(lines)], [len("a short line")])

    def test_a_rule_is_40_wide_with_no_width(self):
        lines = watch.markdown("word " * 40 + "\n\n---\n\nx", False)
        self.assertEqual([len(r) for r in rules(lines)], [40])
        self.assertEqual([len(r) for r in rules(watch.markdown("---", False, 80))], [40], "nothing but a rule")

    def test_a_rule_beside_a_table_matches_its_rule(self):
        text = "Results:\n\n| Name | Value |\n|---|---|\n| alpha | 1 |\n| beta | 22 |\n\n---\n\nDone."
        lines = watch.markdown(text, True, 100)
        plain = [watch.ANSI.sub("", line) for line in lines]
        table_rule = next(p for p in plain if "┼" in p)
        self.assertEqual([watch.width(r) for r in rules(plain)], [watch.width(table_rule)])

    def test_a_rule_beside_records_matches_their_rule(self):
        head = "| " + " | ".join(f"Column {k}" for k in range(10)) + " |"
        rows = ["| " + " | ".join(f"r{r} c{k} some words" for k in range(10)) + " |" for r in range(2)]
        text = "\n".join([head, "|" + "---|" * 10] + rows + ["", "---", "", "end"])
        plain = watch.markdown(text, False, 40)
        self.assertEqual(len(set(map(len, rules(plain)))), 1, rules(plain))


class ClipShowsText(unittest.TestCase):
    TEXT = "\n\n\n---\n---\nFirst text\n" + "\n".join(f"line {k}" for k in range(10))

    def test_clipped_at_one_line_the_first_text_line_shows(self):
        out = watch.clip_rendered(watch.blocks(self.TEXT, False, 80), 1, False).split("\n")
        self.assertEqual(out[-2:], ["First text", "… (+10 lines)"])

    def test_even_clipped_at_nothing_the_first_text_line_shows(self):
        for more in (0, 4):
            out = watch.clip_rendered(watch.blocks(self.TEXT, False, 80), 0, False, more).split("\n")
            self.assertEqual(out[-2:], ["First text", "… (+10 lines)"], more)

    def test_a_message_with_no_text_shows_as_before(self):
        out = watch.clip_rendered(watch.blocks("\n\n---\n\n---", False, 80), 0, False)
        self.assertEqual(out, "\n\n" + "─" * 40 + "\n\n" + "─" * 40)


if __name__ == "__main__":
    unittest.main()
