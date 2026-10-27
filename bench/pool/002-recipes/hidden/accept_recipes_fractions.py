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


class Fractions(Base):
    def test_fractions(self):
        """T-004: a quantity with no unit or in `tsp`, `tbsp` or `cup` near a whole number plus halves, thirds, quarters or eighths prints as that fraction in lowest terms; a whole number still prints as `2`."""
        self.write("a.txt", "# A\nserves 2\n1/2 tsp salt\n1 1/2 cup milk\n0.66 cup oil\n0.375 tsp pepper\n"
                            "1.5 eggs\n2 tbsp sugar\n2/4 cup rice\n")
        self.assertEqual(self.ok("show", "a.txt")[2:],
                         ["- 1/2 tsp salt", "- 1 1/2 cup milk", "- 2/3 cup oil", "- 3/8 tsp pepper",
                          "- 1 1/2 eggs", "- 2 tbsp sugar", "- 1/2 cup rice"])

    def test_not_near(self):
        """T-004: a spoon, cup or count quantity that isn't near such a fraction keeps T-001's form (`0.2 tsp` shows as `0.2 tsp`)."""
        self.write("a.txt", "# A\nserves 2\n0.2 tsp salt\n1.7 cup milk\n")
        self.assertEqual(self.ok("show", "a.txt")[2:], ["- 0.2 tsp salt", "- 1.7 cup milk"])

    def test_metric_stays(self):
        """T-004: quantities in `g`, `kg`, `ml` and `l` keep T-001's form (`0.5 kg` stays `0.5 kg`), and so does the serves line (`serves 1.5`)."""
        self.write("a.txt", "# A\nserves 3\n1 kg flour\n1 l milk\n150 g butter\n3 eggs\n")
        self.assertEqual(self.ok("scale", "a.txt", "--by", "0.5"),
                         ["A", "serves 1.5", "- 0.5 kg flour", "- 0.5 l milk", "- 75 g butter", "- 1 1/2 eggs"])

    def test_everywhere(self):
        """T-004: a recipe with `1 cup milk` scaled `--by 1/3` prints `- 1/3 cup milk`, and two recipes with `1/4 tsp salt` each make `- 1/2 tsp salt` in `shop`."""
        self.write("a.txt", "# A\nserves 2\n1 cup milk\n1/4 tsp salt\n")
        self.write("b.txt", "# B\nserves 4\n1/4 tsp salt\n")
        self.assertEqual(self.ok("scale", "a.txt", "--by", "1/3")[2], "- 1/3 cup milk")
        self.assertEqual(self.ok("shop", "a.txt", "b.txt"), ["- 1 cup milk", "- 1/2 tsp salt"])
