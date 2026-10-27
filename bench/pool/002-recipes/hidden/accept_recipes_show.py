import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

RECIPES = Path("recipes.py").resolve()

PANCAKES = """# Pancakes
serves 4

200 g flour
2 eggs
300 ml milk
"""


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, True)

    def write(self, name, text):
        (self.dir / name).write_text(text, encoding="utf-8")
        return name

    def recipes(self, *args):
        return subprocess.run([sys.executable, str(RECIPES), *args], cwd=self.dir, capture_output=True, text=True,
                              encoding="utf-8", timeout=30)

    def ok(self, *args):
        r = self.recipes(*args)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout.strip().splitlines()

    def fails(self, code, error, *args, quiet=False):
        r = self.recipes(*args)
        self.assertEqual((r.returncode, r.stderr.strip()), (code, error), args)
        if quiet:
            self.assertEqual(r.stdout.strip(), "", args)

    def usage(self, *args):
        r = self.recipes(*args)
        self.assertEqual(r.returncode, 2, args)
        self.assertIn("usage", r.stderr.lower(), args)
        self.assertNotIn("Traceback", r.stderr)


class Show(Base):
    def test_show(self):
        """T-001: `show pancakes.txt` prints the title (without the `# `), then `serves <N>`, then one line per ingredient in the file's order, `- <quantity> <unit> <name>`, or `- <quantity> <name>` when it has no unit."""
        self.write("pancakes.txt", PANCAKES + "3 large eggs\n")
        self.assertEqual(self.ok("show", "pancakes.txt"),
                         ["Pancakes", "serves 4", "- 200 g flour", "- 2 eggs", "- 300 ml milk", "- 3 large eggs"])

    def test_quantities(self):
        """T-001: each quantity is printed as a number with at most two decimals and no trailing zeros: `1/2 kg` shows as `0.5 kg`, `1 1/2 kg` as `1.5 kg`, `0.250 l` as `0.25 l`, `200 g` as `200 g`."""
        self.write("cake.txt", "# Cake\nserves 8\n1/2 kg sugar\n1 1/2 kg flour\n0.250 l cream\n200 g butter\n")
        self.assertEqual(self.ok("show", "cake.txt"),
                         ["Cake", "serves 8", "- 0.5 kg sugar", "- 1.5 kg flour", "- 0.25 l cream", "- 200 g butter"])

    def test_missing(self):
        """T-001: a file that doesn't exist prints `error: no such recipe: <file>` to stderr and exits 1."""
        self.fails(1, "error: no such recipe: nothing.txt", "show", "nothing.txt")

    def test_bad_ingredient(self):
        """T-001: an ingredient line with no quantity, a quantity of zero or less, or no name prints `error: <file>:<line>: bad ingredient: <the line>` and exits 1, counting every line from 1, blank ones too."""
        for bad in ("salt to taste", "0 g salt", "200 g", "-2 eggs"):
            self.write("soup.txt", f"# Soup\nserves 2\n\n1 l water\n{bad}\n")
            self.fails(1, f"error: soup.txt:5: bad ingredient: {bad}", "show", "soup.txt")

    def test_no_title_or_serves(self):
        """T-001: a first line that isn't `# <title>` prints `error: <file>:1: no title`, and a second non-blank line that isn't `serves <N>` (N a whole number above zero) prints `error: <file>:<line>: no serves line`, each with exit 1."""
        self.write("a.txt", "Soup\nserves 2\n1 l water\n")
        self.fails(1, "error: a.txt:1: no title", "show", "a.txt")
        for bad in ("serves four", "serves 0", "1 l water"):
            self.write("b.txt", f"# Soup\n\n{bad}\n1 l water\n")
            self.fails(1, "error: b.txt:3: no serves line", "show", "b.txt")
