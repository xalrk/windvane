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


class Scale(Base):
    def test_serves(self):
        """T-002: `scale pancakes.txt --serves 6` on a recipe for 4 prints `serves 6` and every quantity times 6/4."""
        self.write("pancakes.txt", PANCAKES)
        self.assertEqual(self.ok("scale", "pancakes.txt", "--serves", "6"),
                         ["Pancakes", "serves 6", "- 300 g flour", "- 3 eggs", "- 450 ml milk"])

    def test_by(self):
        """T-002: `scale pancakes.txt --by 2` doubles every quantity and the serves line; `--by` also takes a decimal or a fraction, and the serves line is printed like a quantity (a recipe for 3 `--by 0.5` prints `serves 1.5`)."""
        self.write("pancakes.txt", PANCAKES)
        self.assertEqual(self.ok("scale", "pancakes.txt", "--by", "2"),
                         ["Pancakes", "serves 8", "- 400 g flour", "- 4 eggs", "- 600 ml milk"])
        half = ["Pancakes", "serves 2", "- 100 g flour", "- 1 eggs", "- 150 ml milk"]
        self.assertEqual(self.ok("scale", "pancakes.txt", "--by", "0.5"), half)
        self.assertEqual(self.ok("scale", "pancakes.txt", "--by", "1/2"), half)
        self.write("stew.txt", "# Stew\nserves 3\n600 g beef\n")
        self.assertEqual(self.ok("scale", "stew.txt", "--by", "0.5"), ["Stew", "serves 1.5", "- 300 g beef"])

    def test_rounding(self):
        """T-002: a recipe for 3 with `100 g butter`, scaled `--serves 1`, prints `- 33.33 g butter`; one with `0.1 l stock` scaled `--by 3` prints `- 0.3 l stock`."""
        self.write("a.txt", "# A\nserves 3\n100 g butter\n0.1 l stock\n")
        self.assertEqual(self.ok("scale", "a.txt", "--serves", "1")[2], "- 33.33 g butter")
        self.assertEqual(self.ok("scale", "a.txt", "--by", "3")[3], "- 0.3 l stock")

    def test_both_or_neither(self):
        """T-002: giving both `--serves` and `--by`, or neither, prints a usage line to stderr and exits 2."""
        self.write("pancakes.txt", PANCAKES)
        self.usage("scale", "pancakes.txt")
        self.usage("scale", "pancakes.txt", "--serves", "2", "--by", "2")

    def test_bad_values(self):
        """T-002: a `--serves` that isn't a whole number above zero prints `error: bad serves: <value>`, and a `--by` that isn't a number above zero prints `error: bad factor: <value>`, each with exit 2."""
        self.write("pancakes.txt", PANCAKES)
        for bad in ("0", "abc", "2.5"):
            self.fails(2, f"error: bad serves: {bad}", "scale", "pancakes.txt", "--serves", bad)
        for bad in ("0", "abc", "-1"):
            self.fails(2, f"error: bad factor: {bad}", "scale", "pancakes.txt", "--by", bad)

    def test_file_errors(self):
        """T-002: a missing or broken recipe file gives the same error and exit code as `show`."""
        self.fails(1, "error: no such recipe: nothing.txt", "scale", "nothing.txt", "--by", "2")
        self.write("soup.txt", "# Soup\nserves 2\nsalt to taste\n")
        self.fails(1, "error: soup.txt:3: bad ingredient: salt to taste", "scale", "soup.txt", "--serves", "4")
