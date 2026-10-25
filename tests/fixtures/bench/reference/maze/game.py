"""maze: a small, deterministic text maze game (the bench's reference solution)."""
import argparse
import json
import os
import sys

STEPS = {"n": (0, -1), "s": (0, 1), "e": (1, 0), "w": (-1, 0)}
WORDS = {"north": "n", "south": "s", "east": "e", "west": "w"}


def load_map(text):
    """The map's rows, and the start square as (x, y)."""
    rows = [list(line) for line in text.splitlines() if line.strip()]
    for y, row in enumerate(rows):
        for x, c in enumerate(row):
            if c == "@":
                row[x] = "."
                return rows, (x, y)
    raise ValueError("the map has no start square @")


class Game:
    def __init__(self, rows, pos):
        self.rows, self.pos, self.keys, self.moves = rows, pos, 0, 0

    def square(self, x, y):
        return self.rows[y][x] if 0 <= y < len(self.rows) and 0 <= x < len(self.rows[y]) else "#"

    def move(self, d):
        x, y = self.pos[0] + STEPS[d][0], self.pos[1] + STEPS[d][1]
        c, out = self.square(x, y), []
        if c == "#":
            return ["You can't go that way."]
        if c == "D":
            if not self.keys:
                return ["The door is locked."]
            self.keys -= 1
            self.rows[y][x] = "."
            out.append("You unlock the door.")
        self.pos, self.moves = (x, y), self.moves + 1
        out.append(f"You are at ({x}, {y}).")
        if c == "k":
            self.keys += 1
            self.rows[y][x] = "."
            out.append("You pick up a key.")
        if c == "E":
            out.append(f"You escape in {self.moves} moves!")
        return out

    def look(self):
        return ["".join("@" if (x, y) == self.pos else c for x, c in enumerate(row)) for y, row in enumerate(self.rows)]

    def save(self, path):
        tmp = f"{path}.tmp"
        try:
            with open(tmp, "w") as f:
                json.dump({"maze-save": 1, "rows": ["".join(r) for r in self.rows], "pos": self.pos,
                           "keys": self.keys, "moves": self.moves}, f)
            os.replace(tmp, path)
        except OSError:
            return "Can't save there."
        return "Saved."

    @classmethod
    def load(cls, path):
        d = json.loads(open(path).read())
        if not (isinstance(d, dict) and d.get("maze-save") == 1):
            raise ValueError
        g = cls([list(r) for r in d["rows"]], tuple(d["pos"]))
        g.keys, g.moves = d["keys"], d["moves"]
        return g


def main(argv=None):
    ap = argparse.ArgumentParser(prog="maze")
    ap.add_argument("map", help="the map file")
    ap.add_argument("--load", metavar="FILE", help="go on with a saved game")
    a = ap.parse_args(argv)
    try:
        rows, pos = load_map(open(a.map).read())
    except (OSError, ValueError) as e:
        print(f"error: can't read the map {a.map}: {e}", file=sys.stderr)
        return 2
    game = Game(rows, pos)
    if a.load:
        try:
            game = Game.load(a.load)
        except (OSError, ValueError, KeyError, TypeError):
            print(f"error: can't load {a.load}", file=sys.stderr)
            return 2
    print(f"You are at ({game.pos[0]}, {game.pos[1]}).")
    for line in sys.stdin:
        cmd = line.strip()
        word = WORDS.get(cmd, cmd)
        if not cmd:
            continue
        if cmd == "quit":
            print("Bye.")
            return 0
        if word in STEPS:
            out = game.move(word)
            print("\n".join(out))
            if out[-1].startswith("You escape"):
                return 0
        elif cmd == "look":
            print("\n".join(game.look()))
        elif cmd == "inventory":
            print(f"Keys: {game.keys}")
        elif cmd == "moves":
            print(f"Moves: {game.moves}")
        elif cmd.startswith("save "):
            print(game.save(cmd[5:].strip()))
        else:
            print(f'I don\'t understand "{cmd}".')
    print("You are still in the maze.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
