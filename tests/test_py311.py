"""Python 3.11 rejects f-strings that 3.12+ accept (PEP 701): a backslash, or the outer quote, inside a {}. The
check usually runs on a newer Python, so this catches them in the code and in the fake `claude` script."""
import ast
import io
import sys
import tokenize
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def problems(src, name):
    """Lines where an f-string's expression holds a backslash or reuses an enclosing quote."""
    found, stack = [], []  # stack: [quote, brace depth] per open f-string
    for t in tokenize.generate_tokens(io.StringIO(src).readline):
        inside = any(d > 0 for _, d in stack)
        if t.type == tokenize.FSTRING_START:
            quote = t.string.lstrip("rRfFbB")
            if inside and any(q[0] == quote[0] for q, _ in stack):
                found.append(f"{name}:{t.start[0]}: an enclosing f-string's quote")
            stack.append([quote, 0])
        elif t.type == tokenize.FSTRING_END:
            stack.pop()
        elif stack and t.type == tokenize.OP and t.string in "{}":
            stack[-1][1] += 1 if t.string == "{" else -1
        elif inside and t.type in (tokenize.STRING, tokenize.FSTRING_MIDDLE) and "\\" in t.string:
            found.append(f"{name}:{t.start[0]}: a backslash")
    return found


@unittest.skipIf(sys.version_info < (3, 12), "3.11 parses these files itself")
class Py311FStrings(unittest.TestCase):
    def test_the_old_fake_critic_is_caught(self):
        self.assertTrue(problems("x = f\"a{f'b\\\\n' * 2}\"\n", "s"))

    def test_none_in_the_code(self):
        found = []
        for f in sorted([*ROOT.glob("windvane/*.py"), *ROOT.glob("tests/*.py")]):
            src = f.read_text()
            found += problems(src, f.name)
            for n in ast.walk(ast.parse(src)):  # scripts the tests write out, such as FAKE_CLAUDE
                if isinstance(n, ast.Assign) and isinstance(n.value, ast.Constant) and isinstance(n.value.value, str) \
                        and n.value.value.startswith("#!/usr/bin/env python3"):
                    found += problems(n.value.value, f"{f.name}:{n.targets[0].id}")
        self.assertEqual(found, [])


if __name__ == "__main__":
    unittest.main()
