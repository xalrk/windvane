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


class Keys(Base):
    def test_key(self):
        """T-002: stepping on a key `k` prints `You pick up a key.` after the position line, and the square becomes floor."""
        _, out, _ = self.play(["e", "e", "inventory", "w", "look"])
        self.assertEqual(out[:10], [at(1, 1), at(2, 1), at(3, 1), "You pick up a key.", "Keys: 1", at(2, 1),
                                    "#######", "#.@.#E#", "#.#.#D#", "#.....#"])

    def test_locked(self):
        """T-002: moving into a door `D` with no key prints `The door is locked.` and the player stays where they were."""
        _, out, _ = self.play(["s", "s", "e", "e", "e", "e", "n", "inventory", "w"])
        self.assertEqual(out[6:10], [at(5, 3), "The door is locked.", "Keys: 0", at(4, 3)])

    def test_unlock(self):
        """T-002: moving into a door with a key prints `You unlock the door.` and then the position line: the key is used up and the door's square becomes floor."""
        _, out, _ = self.play(["e", "e", "s", "s", "e", "e", "n", "inventory", "s", "n"])
        self.assertEqual(out[8:13], ["You unlock the door.", at(5, 2), "Keys: 0", at(5, 3), at(5, 2)])
