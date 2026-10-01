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
        self.assertEqual(watch.markdown("a | b\n---", False), ["a | b", "─" * 40])  # a rule, not a table


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


if __name__ == "__main__":
    unittest.main()
