"""T-058: a table too wide to narrow renders as records, a narrowed table rules off only its wrapped rows, and
tools/watch-sweep.py reports what a renderer change broke. T-069: records pack short cells several to a line, and the
sweep checks the clip `watch` prints too."""
import importlib.util
import json
import re
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


STATS = "\n".join(["Totals by run:", "",
                   "| Ticket | Role | Turns | Fresh in | Output | Cache read | Cost | Minutes | Result | Branch |",
                   "|---" * 10 + "|"]
                  + [f"| T-04{i} | worker | 41 | 12k | 9k | 2.1M | $3.10 | 22 | READY | t-04{i}-x |" for i in range(6)])


def records(text, cols=40):
    """A too-wide table's rendered lines, each checked to fit `cols`."""
    rows = watch.markdown(text, False, cols)
    assert all(watch.width(r) <= cols for r in rows), rows
    return rows


class Packed(unittest.TestCase):
    def test_short_cells_share_lines_in_order(self):
        rows = records(STATS.split("\n", 2)[2])
        first = rows[:rows.index("─" * 40)]
        self.assertEqual(first, ["Ticket: T-040 · Role: worker · Turns: 41", "Fresh in: 12k · Output: 9k",
                                 "Cache read: 2.1M · Cost: $3.10", "Minutes: 22 · Result: READY", "Branch: t-040-x"])
        self.assertIn("T-040\033[2m · Role: \033[0mworker", watch.markdown(STATS, True, 40)[2], "the joint is dim")

    def test_a_long_cell_breaks_the_packing_onto_its_own_lines(self):
        head = "| A | B | Note | C | D |" + "".join(f" X{k} |" for k in range(8))
        row = "| a | b | " + "words that wrap " * 4 + "| c | d |" + " x |" * 8
        rows = records(head + "\n" + "|---" * 13 + "|\n" + row)
        self.assertEqual(rows[0], "A: a · B: b")
        self.assertTrue(rows[1].startswith("Note: words that wrap"), rows)
        note = rows[1:rows.index(next(r for r in rows if r.startswith("C: ")))]
        self.assertGreater(len(note), 1, "the long cell wraps")
        self.assertTrue(all(r.startswith(" " * len("Note: ")) for r in note[1:]), "under its value")
        self.assertTrue(rows[len(note) + 1].startswith("C: c · D: d · X0: x"), rows)
        self.assertEqual("".join(rows).count("X7: x"), 1, "no cell lost")

    def test_a_header_only_table_packs_its_headers(self):
        text = "| " + " | ".join(f"Head {k}" for k in range(12)) + " |\n" + "|---" * 12 + "|"
        rows = records(text)
        self.assertEqual(rows[0], "Head 0 · Head 1 · Head 2 · Head 3")
        self.assertEqual(" · ".join(rows).split(" · "), [f"Head {k}" for k in range(12)])
        self.assertTrue(watch.markdown(text, True, 40)[0].startswith("\033[1mHead 0"), "headers stay bold")

    def test_a_header_wider_than_half_drops_the_hanging_indent(self):
        name = "A header past half a line"
        text = f"| {name} | B |" + " C |" * 9 + "\n" + "|---" * 11 + "|\n| " + "long value " * 8 + "| b |" + " c |" * 9
        rows = records(text)
        self.assertTrue(rows[0].startswith(name + ": long"), rows)
        value = rows[:rows.index(next(r for r in rows if r.startswith("B: ")))]
        self.assertGreater(len(value), 1)
        self.assertTrue(all(r[0] != " " for r in value[1:]), "from column 0, not 27 columns in")

    def test_the_rule_spans_the_widest_line_of_the_records(self):
        short = "| " + " | ".join(f"H{k}" for k in range(12)) + " |\n" + "|---" * 12 + "|\n"
        rows = records(short + "\n".join("| " + " | ".join(f"v{r}" for _ in range(12)) + " |" for r in range(3)))
        rules = [r for r in rows if set(r) == {"─"}]
        widest = max(watch.width(r) for r in rows if r not in rules)
        self.assertLess(widest, 40)
        self.assertEqual(rules, ["─" * widest] * 2, "between records, as wide as their widest line")
        wrapped = records(ten_columns())
        self.assertEqual([r for r in wrapped if set(r) == {"─"}], ["─" * max(map(watch.width, wrapped))])


def watched(text, color=False):
    """What `windvane watch` prints for one assistant message in a 42-column terminal."""
    ev = {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}
    out = StringIO()
    with mock.patch.dict("os.environ", {"COLUMNS": "42"}), mock.patch.object(watch, "tty", color), \
            redirect_stdout(out):
        watch.show(ev, 6)
    return out.getvalue()


class Watched(unittest.TestCase):
    def test_watch_shows_three_packed_records_whole_then_clips(self):
        out = watched(STATS)
        rows = out.removeprefix("› ").rstrip("\n").split("\n")
        self.assertTrue(all(watch.width(r) <= 40 for r in rows), rows)
        self.assertEqual(rows[:3], ["Totals by run:", "", "Ticket: T-040 · Role: worker · Turns: 41"])
        self.assertIn("Branch: t-042-x", rows, "three records whole")
        self.assertRegex(rows[-1], r"^… \(\+\d+ lines\)$")
        self.assertNotIn("T-043", out, "the clip still holds")
        self.assertEqual(rows[-2], "Branch: t-042-x", "no rule left dangling before the clip line")
        coloured = watched(STATS, True)
        self.assertIn("\033[2mTicket: \033[0mT-040", coloured)
        self.assertIn("\033[0m\n… (+", coloured, "a reset before the clip line")


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
        self.assertEqual(r.stdout, "3 messages + 5 synthetic at widths 60,80,120: clean\n")

    def test_leftover_markers_are_reported_with_their_place(self):
        r = self.run_sweep(["fine", "a stray ** and an unclosed `tick"], "--widths", "60")
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("20261004-000000-T-001.jsonl:3 @60: marker bold **\n", r.stdout)
        self.assertIn("20261004-000000-T-001.jsonl:3 @60: marker backtick\n", r.stdout)
        self.assertNotIn("stray", r.stdout, "the rendered line only with --show")
        self.assertTrue(r.stdout.endswith("2 messages + 5 synthetic at widths 60: 1 marker backtick, 1 marker bold **\n"), r.stdout)
        shown = self.run_sweep(["a stray ** and an unclosed `tick"], "--show").stdout
        self.assertIn("\n    a stray ** and an unclosed `tick\n", shown)

    def test_a_line_only_the_clip_breaks_is_reported_as_clipped(self):
        spec = importlib.util.spec_from_file_location("watch_sweep", ROOT / "tools" / "watch-sweep.py")
        sweep = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(sweep)
        self.assertEqual(sweep.check(STATS, 60), [], "clean whole and clipped")
        with mock.patch.object(watch, "clip_rendered", lambda parts, n, color: "fine\n" + "x" * 61):
            self.assertEqual(sweep.check(STATS, 60), [("clipped too wide (61)", "x" * 61)])

    def test_an_empty_state_dir_is_an_error(self):
        r = self.run_sweep([])
        self.assertEqual(r.returncode, 2)
        self.assertIn("no assistant messages", r.stdout)


if __name__ == "__main__":
    unittest.main()
