import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

GAME = Path("game.py").resolve()
MAZE = "#######\n#@.k#E#\n#.#.#D#\n#.....#\n#######\n"  # the key, the door before the exit, 8 moves out
ROOM = "#####\n#@..#\n#.#.#\n#...#\n#####\n"  # nothing but floor and walls


def at(x, y):
    return f"You are at ({x}, {y})."


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, True)

    def play(self, commands, maze=MAZE, *extra):
        f = self.dir / "map.txt"
        f.write_text(maze)
        r = subprocess.run([sys.executable, str(GAME), str(f), *extra], input="".join(c + "\n" for c in commands),
                           capture_output=True, text=True, cwd=self.dir, timeout=30)
        self.assertNotIn("Traceback", r.stderr)
        return r.returncode, r.stdout.splitlines(), r.stderr


class Save(Base):
    def test_save_and_load(self):
        """T-004: `save <file>` writes the game to `<file>` and prints `Saved.`; `python3 game.py <map> --load <file>` goes on from there: the same square, keys and moves, and the keys taken and doors opened stay so."""
        save = self.dir / "game.sav"
        _, out, _ = self.play(["e", "e", "s", "s", "e", "e", "n", f"save {save}"])
        self.assertEqual(out[10], "Saved.")
        code, out, _ = self.play(["moves", "inventory", "look", "n"], MAZE, "--load", str(save))
        self.assertEqual(code, 0)
        self.assertEqual(out, [at(5, 2), "Moves: 7", "Keys: 0", "#######", "#...#E#", "#.#.#@#", "#.....#",
                               "#######", at(5, 1), "You escape in 8 moves!"])

    def test_bad_save(self):
        """T-004: a save file that is missing or isn't one this program wrote prints `error: can't load <file>` to stderr and exits 2, before reading any input."""
        junk = self.dir / "junk.sav"
        junk.write_text("not a save\n")
        for f in (self.dir / "missing.sav", junk):
            code, out, err = self.play(["e"], MAZE, "--load", str(f))
            self.assertEqual((code, out, err.strip()), (2, [], f"error: can't load {f}"))

    def test_save_nowhere(self):
        """T-004: a save to a folder that doesn't exist prints `Can't save there.` and the game goes on."""
        _, out, _ = self.play(["save nowhere/game.sav", "e"])
        self.assertEqual(out[:3], [at(1, 1), "Can't save there.", at(2, 1)])
