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


class Escape(Base):
    def test_escape(self):
        """T-003: reaching the exit `E` prints, after the position line, `You escape in <n> moves!` where n counts the moves that changed square, and the game ends with exit code 0, reading no more input."""
        code, out, _ = self.play(["n", "e", "e", "s", "s", "e", "e", "n", "n", "look"])
        self.assertEqual(code, 0)
        self.assertEqual(out[-2:], [at(5, 1), "You escape in 8 moves!"])
        self.assertNotIn("#######", out)

    def test_moves(self):
        """T-003: `moves` prints `Moves: <n>`."""
        _, out, _ = self.play(["moves", "e", "n", "moves"])
        self.assertEqual(out[:5], [at(1, 1), "Moves: 0", at(2, 1), "You can't go that way.", "Moves: 1"])

    def test_still_in(self):
        """T-003: when the input ends before the exit is reached, the game prints `You are still in the maze.` and exits 1."""
        code, out, _ = self.play(["e"])
        self.assertEqual((code, out[-1]), (1, "You are still in the maze."))
