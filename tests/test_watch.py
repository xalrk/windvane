import io
import re
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from windvane import watch  # noqa: E402

FIXTURE = """# Plan for `T-020`
Some **bold**, *italic* and `ls *.py` text, see [the docs](https://example.com/a_b).

- first **point**
  - nested
1. step one

```python
print(snake_case * 2)
```

| Name | Count |
|------|------:|
| `a` | 7 |
| longer name | 12 |"""

ANSI = re.compile(r"\033\[[0-9;]*m")


def say(text):
    return {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}


def shown(ev, color):
    out = io.StringIO()
    with mock.patch.object(watch, "tty", color), redirect_stdout(out):
        watch.show(ev, 6)
    return out.getvalue()


class Markdown(unittest.TestCase):
    def test_colour_on_styles_each_element(self):
        rows = watch.markdown(FIXTURE, True)
        self.assertEqual(rows[0], "\033[1;36mPlan for \033[0m\033[1;36;94mT-020\033[0m")
        self.assertEqual(rows[1], "Some \033[1mbold\033[0m, \033[3mitalic\033[0m and \033[94mls *.py\033[0m text, "
                                  "see the docs\033[2m (https://example.com/a_b)\033[0m.")
        self.assertEqual(rows[3], "  • first \033[1mpoint\033[0m")
        self.assertEqual(rows[4], "    • nested")
        self.assertEqual(rows[5], "  1. step one")
        self.assertEqual(rows[7], "\033[2m  ┌ python\033[0m")
        self.assertEqual(rows[8], "\033[2m  │ \033[0mprint(snake_case * 2)")
        self.assertEqual(rows[10], "\033[1mName\033[0m       \033[2m │ \033[0m\033[1mCount\033[0m")
        self.assertEqual(rows[11], "\033[2m────────────┼──────\033[0m")
        self.assertEqual(rows[12], "\033[94ma\033[0m          \033[2m │ \033[0m    7")
        for row in rows:  # every line closes what it opens
            self.assertIsNone(ANSI.search(row.rsplit("\033[0m", 1)[-1]), row)

    def test_colour_off_is_plain_text_without_markers(self):
        rows = watch.markdown(FIXTURE, False)
        text = "\n".join(rows)
        self.assertNotIn("\033", text)
        self.assertNotIn("**", text)
        self.assertNotIn("`", text)
        self.assertFalse([r for r in rows if r.lstrip().startswith("#")])
        self.assertEqual(rows[:2], ["Plan for T-020",
                                    "Some bold, italic and ls *.py text, see the docs (https://example.com/a_b)."])
        self.assertEqual(rows[10:], ["Name        │ Count", "────────────┼──────",
                                     "a           │     7", "longer name │    12"])

    def test_words_globs_and_code_blocks_keep_their_characters(self):
        self.assertEqual(watch.markdown("rename snake_case_names and __init__.py", True),
                         ["rename snake_case_names and __init__.py"])
        self.assertEqual(watch.markdown("run `rm *.tmp *.bak` then `ls a_b_c`", False),
                         ["run rm *.tmp *.bak then ls a_b_c"])
        self.assertEqual(watch.markdown("2*3*4 and a \\*star\\*", False), ["2*3*4 and a *star*"])
        self.assertEqual(watch.markdown("```\n# **not** a heading\n```", False), ["  ┌", "  │ # **not** a heading"])

    def test_nested_spans_and_table_pipes(self):
        self.assertEqual(watch.markdown("**the `x` file**", True),
                         ["\033[1mthe \033[0m\033[1;94mx\033[0m\033[1m file\033[0m"])
        self.assertEqual(watch.markdown("| a |\n|---|\n| `x|y` |", False), ["a", "───", "x|y"])
        self.assertEqual(watch.markdown("a | b\n---", False), ["a | b"])  # a setext heading, not a table


class Show(unittest.TestCase):
    def test_clipped_message_ends_with_a_reset(self):
        out = shown(say("\n".join(f"- **item {i}**" for i in range(20))), True)
        body, tail = out.rsplit("\n…", 1)
        self.assertTrue(body.endswith("\033[0m"))
        self.assertEqual(tail, " (+8 lines)\n")
        self.assertEqual(len(body.split("\n")), 12)

    def test_piped_output_is_plain(self):
        self.assertEqual(shown(say("## Done\n**All** tests `pass`."), False), "› Done\nAll tests pass.\n")

    def test_tool_calls_stay_raw(self):
        ev = {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Bash", "input": {"command": "grep -n '**x**' `pwd`/a_b_c"}}]}}
        self.assertEqual(shown(ev, False), "  Bash: grep -n '**x**' `pwd`/a_b_c\n")

    def test_a_long_bullet_wraps_under_its_text(self):
        words = " ".join(f"word{i}" for i in range(15))
        with mock.patch.dict("os.environ", {"COLUMNS": "42"}):
            out = shown(say(f"- {words}\n  - nested {words}\n> quoted {words}"), False)
        rows = out.removeprefix("› ").rstrip("\n").split("\n")
        self.assertTrue(all(watch.width(r) <= 40 for r in rows), rows)
        self.assertEqual(rows[0][:4], "  • ")
        self.assertRegex(rows[1], r"^    word\d")  # under the first word, not at column 0
        self.assertTrue(any(re.match(r"      word\d", r) for r in rows))  # the nested bullet's own indent
        self.assertTrue(any(r.startswith("▎ word") for r in rows))
        self.assertEqual(" ".join(r.strip(" •▎") for r in rows), f"{words} nested {words} quoted {words}")

    def test_a_word_wider_than_the_terminal_is_cut_not_lost(self):
        url = "https://example.com/" + "x" * 90
        rows = watch.markdown(f"- see {url}", False, 40)
        self.assertTrue(all(watch.width(r) <= 40 for r in rows), rows)
        self.assertEqual("".join(r.strip(" •") for r in rows[1:]), url)

    def test_a_clipped_table_shows_its_rows(self):
        text = "\n\n".join(f"Line {i}." for i in range(10)) + "\n\n| a | b |\n|---|---|\n| 1 | 2 |\n| 3 | 4 |\n\nAfter."
        out = shown(say(text), False)
        self.assertIn("1 │ 2", out)
        self.assertIn("3 │ 4", out)
        self.assertNotIn("After", out)
        self.assertTrue(out.endswith("… (+2 lines)\n"), out)

    def test_a_huge_table_is_still_clipped(self):
        rows = "\n".join(f"| {i} | x |" for i in range(40))
        out = shown(say("Intro.\n\n" + "\n".join(f"- {i}" for i in range(10)) + f"\n\n| a | b |\n|---|---|\n{rows}"),
                    False)
        self.assertLess(len(out.split("\n")), 22)
        self.assertRegex(out, r"… \(\+\d+ lines\)\n$")

    def test_a_url_with_brackets_stays_whole(self):
        self.assertEqual(watch.markdown("see [Foo](https://en.wikipedia.org/wiki/Foo_(bar)) now", False),
                         ["see Foo (https://en.wikipedia.org/wiki/Foo_(bar)) now"])
        self.assertEqual(watch.markdown("see [Foo](https://w.org/Foo_(bar)) now", True),
                         ["see Foo\033[2m (https://w.org/Foo_(bar))\033[0m now"])  # dim to its last bracket
        self.assertEqual(watch.markdown("[https://w.org/a_(b)](https://w.org/a_(b))", False), ["https://w.org/a_(b)"])
        self.assertEqual(watch.markdown("[a](https://x.org/a) (b)", False), ["a (https://x.org/a) (b)"])

    # T-053: cases from running the wrap over every message in the real stream logs.
    def test_a_long_code_line_wraps_under_its_gutter(self):
        line = "Scores: works=8 clear=7 lean=8 " + " ".join(f"word{i}" for i in range(20))  # critics fence verdicts
        rows = watch.markdown(f"```\n{line}\n```", False, 40)
        self.assertTrue(all(watch.width(r) <= 40 for r in rows), rows)
        self.assertTrue(all(r.startswith("  │ ") for r in rows[1:]), rows)
        self.assertEqual(" ".join(r.removeprefix("  │ ") for r in rows[1:]), line)

    def test_a_table_wider_than_the_terminal_wraps_its_cells(self):
        text = ("| Send-back | Budget it spends | Default |\n|---|---|--:|\n"
                "| Reviewer asks for changes | review_rounds, as before | 3 |\n"
                "| Merge conflict | conflict_retries (new); conflicts it resolves itself don't count | 2 |")
        rows = watch.markdown(text, False, 40)
        self.assertTrue(all(watch.width(r) <= 40 for r in rows), rows)
        self.assertEqual(rows[0].split(" │ ")[0].strip(), "Send-back")
        rules = [i for i, r in enumerate(rows) if "─┼─" in r]
        self.assertEqual(len(rules), 2, "a rule under the header and one between the body rows")
        body = rows[rules[0] + 1:rules[1]]
        self.assertGreater(len(body), 1, "the first row wrapped")
        self.assertEqual(" ".join(r.split(" │ ")[0].strip() for r in body).split(), "Reviewer asks for changes".split())
        self.assertEqual(body[0].split(" │ ")[2].strip(), "3", "right-aligned on the row's first line")
        squeeze = lambda rs: sorted(re.sub(r"[\s│─┼]", "", "".join(rs)))
        self.assertEqual(squeeze(rows), squeeze(watch.markdown(text, False)), "no character lost")
        block = watch.blocks(text, False, 40)[0]
        self.assertEqual([k for _, k in block], ["─┼─" not in r for r in rows], "rules don't count toward the clip")


if __name__ == "__main__":
    unittest.main()
