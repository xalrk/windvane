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


class Shop(Base):
    def test_sorted_and_summed(self):
        """T-003: `shop a.txt b.txt` prints one line per ingredient sorted by name ignoring case, and nothing else; the same name (ignoring case, shown as first written) and unit are summed, across files and within one."""
        self.write("a.txt", PANCAKES)
        self.write("b.txt", "# Omelette\nserves 2\n3 Eggs\n50 ml milk\n20 g Butter\n1 eggs\n")
        self.assertEqual(self.ok("shop", "a.txt", "b.txt"),
                         ["- 20 g Butter", "- 6 eggs", "- 200 g flour", "- 350 ml milk"])

    def test_units_joined(self):
        """T-003: `g` and `kg` are one unit, shown in `g` below 1000 g and in `kg` from 1000 g up; `ml` and `l` the same way; the same name in other units gets one line per unit, in the order the units first appear."""
        self.write("a.txt", "# A\nserves 2\n600 g flour\n0.5 kg rice\n2 cup sugar\n700 ml water\n")
        self.write("b.txt", "# B\nserves 2\n1 kg flour\n100 g sugar\n0.3 l water\n1 cup sugar\n")
        self.assertEqual(self.ok("shop", "a.txt", "b.txt"),
                         ["- 1.6 kg flour", "- 500 g rice", "- 3 cup sugar", "- 100 g sugar", "- 1 l water"])

    def test_serves(self):
        """T-003: `shop a.txt b.txt --serves 2` scales each recipe to 2 servings before summing."""
        self.write("a.txt", PANCAKES)
        self.write("b.txt", "# Bread\nserves 1\n300 g flour\n")
        self.assertEqual(self.ok("shop", "a.txt", "b.txt", "--serves", "2"),
                         ["- 1 eggs", "- 700 g flour", "- 150 ml milk"])

    def test_errors(self):
        """T-003: `shop` with no file prints a usage line and exits 2; a missing or broken recipe file gives the same error and exit code as `show`, and prints nothing on stdout."""
        self.usage("shop")
        self.write("a.txt", PANCAKES)
        self.fails(1, "error: no such recipe: nothing.txt", "shop", "a.txt", "nothing.txt", quiet=True)
        self.write("b.txt", "# B\nserves 1\n\n200 g\n")
        self.fails(1, "error: b.txt:4: bad ingredient: 200 g", "shop", "a.txt", "b.txt", quiet=True)
