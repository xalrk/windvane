"""T-058: a table too wide to narrow renders as records, a narrowed table rules off only its wrapped rows, and
tools/watch-sweep.py reports what a renderer change broke."""
import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from windvane import watch  # noqa: E402

ANSI = re.compile(r"\033\[[0-9;]*m")


def ten_columns():
    head = "| " + " | ".join(f"Column {k}" for k in range(10)) + " |"
    rows = ["| " + " | ".join(f"row {r} cell {k} with a few more words to wrap" for k in range(10)) + " |"
            for r in range(2)]
    return "\n".join([head, "|" + "---|" * 10] + rows)


class Records(unittest.TestCase):
    def test_a_ten_column_table_at_40_renders_as_records(self):
        rows = watch.markdown(ten_columns(), False, 40)
        self.assertTrue(all(watch.width(r) <= 40 for r in rows), rows)
        self.assertNotIn("│", "".join(rows), "no columns left to garble")
        firsts = [r for r in rows if r.startswith("Column ")]
        self.assertEqual(len(firsts), 20, "one header: value line per cell, per row")
        self.assertTrue(firsts[0].startswith("Column 0: row 0 cell 0 with"), firsts[0])
        self.assertEqual(sum(1 for r in rows if set(r) == {"─"}), 1, "one rule between the two records")
        cell = rows[rows.index(firsts[0]):rows.index(firsts[1])]
        self.assertGreater(len(cell), 1, "a long value wraps")
        self.assertTrue(all(r.startswith(" " * len("Column 0: ")) for r in cell[1:]), "under its value")
        squeeze = lambda s: sorted(re.sub(r"[\s─:|]", "", s))
        head, *body = [line for i, line in enumerate(ten_columns().split("\n")) if i != 1]
        self.assertEqual(squeeze("".join(rows)), squeeze(head * 2 + "".join(body)), "no character lost")

    def test_record_headers_are_dim_and_every_line_ends_reset(self):
        rows = watch.markdown(ten_columns(), True, 40)
        self.assertTrue(rows[0].startswith("\033[2mColumn 0:"), rows[0])
        self.assertTrue(all(watch.width(ANSI.sub("", r)) <= 40 for r in rows))
        last = lambda r: (re.findall(r"\033\[[0-9;]*m", r) or ["\033[0m"])[-1]
        self.assertTrue(all(last(r) == "\033[0m" for r in rows), "no colour runs on")

    def test_rules_dont_count_toward_the_clip(self):
        block = watch.blocks(ten_columns(), False, 40)[0]
        self.assertEqual([k for _, k in block], [set(r) != {"─"} for r, _ in block])


class NarrowedRules(unittest.TestCase):
    def test_rows_that_dont_wrap_have_no_rules_between_them(self):
        text = "| Name | Note |\n|---|---|\n" + "\n".join(f"| n{i} | short {i} |" for i in range(4))
        text = text.replace("| Note |", "| " + "Note " * 12 + "|")  # a wide header narrows the table
        rows = watch.markdown(text, False, 40)
        self.assertTrue(all(watch.width(r) <= 40 for r in rows), rows)
        rules = [i for i, r in enumerate(rows) if "─┼─" in r]
        self.assertEqual(len(rules), 1, rows)
        self.assertEqual(rows[rules[0] + 1:], [f"n{i}   │ short {i}" for i in range(4)])

    def test_a_wrapped_row_is_ruled_off_and_the_others_are_not(self):
        text = ("| Name | Note |\n|---|---|\n| a | one |\n| b | " + "a long note that wraps " * 3 + "|\n"
                "| c | three |\n| d | four |")
        rows = watch.markdown(text, False, 40)
        self.assertTrue(all(watch.width(r) <= 40 for r in rows), rows)
        names = [r.split(" │ ")[0].strip() if "─┼─" not in r else "rule" for r in rows]
        # a rule under the header, then on both sides of b, so a's line can't be read as part of b
        self.assertEqual([x for x in names if x], ["Name", "rule", "a", "rule", "b", "rule", "c", "d"])
        self.assertGreater(names.count(""), 0, "b took more than one line")


class Sweep(unittest.TestCase):
    def run_sweep(self, texts, *args):
        state = Path(tempfile.mkdtemp())
        (state / "runs").mkdir()
        events = [{"type": "system"}] + [{"type": "assistant", "message": {"content": [{"type": "text", "text": t}]}}
                                         for t in texts]
        (state / "runs" / "20261004-000000-T-001.jsonl").write_text(
            "\n".join(json.dumps(e) for e in events) + "\nnot json\n")
        return subprocess.run([sys.executable, str(ROOT / "tools" / "watch-sweep.py"), str(state), *args],
                              capture_output=True, text=True)

    def test_a_clean_log_passes(self):
        r = self.run_sweep(["# Title\nSome **bold** and `**literal**` text.", ten_columns(),
                            "```\nprint('**not a marker**')\n```"])
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(r.stdout, "3 messages at widths 60,80,120: clean\n")

    def test_leftover_markers_are_reported_with_their_place(self):
        r = self.run_sweep(["fine", "a stray ** and an unclosed `tick"], "--widths", "60")
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("20261004-000000-T-001.jsonl:3 @60: marker bold **\n", r.stdout)
        self.assertIn("20261004-000000-T-001.jsonl:3 @60: marker backtick\n", r.stdout)
        self.assertNotIn("stray", r.stdout, "the rendered line only with --show")
        self.assertTrue(r.stdout.endswith("2 messages at widths 60: 1 marker backtick, 1 marker bold **\n"), r.stdout)
        self.assertIn("    a stray ** and an unclosed `tick", self.run_sweep(["a stray ** and an unclosed `tick"], "--show").stdout)

    def test_an_empty_state_dir_is_an_error(self):
        r = self.run_sweep([])
        self.assertEqual(r.returncode, 2)
        self.assertIn("no assistant messages", r.stdout)


if __name__ == "__main__":
    unittest.main()
