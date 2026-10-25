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


class Move(Base):
    def test_moves(self):
        """T-001: `n`, `s`, `e` and `w` (or `north`, `south`, `east`, `west`) move one square and print `You are at (x, y).`"""
        _, out, _ = self.play(["s", "south", "e", "east", "north", "n", "w", "west"], ROOM)
        self.assertEqual(out[:9], [at(1, 1), at(1, 2), at(1, 3), at(2, 3), at(3, 3), at(3, 2), at(3, 1), at(2, 1),
                                   at(1, 1)])

    def test_walls(self):
        """T-001: a move into a wall `#` or off the map prints `You can't go that way.` and the player stays where they were."""
        no = "You can't go that way."
        _, out, _ = self.play(["n", "w", "e", "e", "e", "s"], ROOM)
        self.assertEqual(out[:7], [at(1, 1), no, no, at(2, 1), at(3, 1), no, at(3, 2)])
        _, out, _ = self.play(["n", "w", "e", "e"], "@.\n..\n")
        self.assertEqual(out[:5], [at(0, 0), no, no, at(1, 0), no])

    def test_look(self):
        """T-001: `look` prints the map's rows as loaded, with `@` on the player's square and the start square shown as `.`"""
        _, out, _ = self.play(["e", "look"], ROOM)
        self.assertEqual(out[:7], [at(1, 1), at(2, 1), "#####", "#.@.#", "#.#.#", "#...#", "#####"])
