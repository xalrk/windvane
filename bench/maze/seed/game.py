"""maze: a small, deterministic text maze game."""
import argparse
import sys


def load_map(text):
    """The map's rows, and the start square as (x, y)."""
    rows = [list(line) for line in text.splitlines() if line.strip()]
    for y, row in enumerate(rows):
        for x, c in enumerate(row):
            if c == "@":
                return rows, (x, y)
    raise ValueError("the map has no start square @")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="maze")
    ap.add_argument("map", help="the map file")
    a = ap.parse_args(argv)
    try:
        rows, (x, y) = load_map(open(a.map).read())
    except (OSError, ValueError) as e:
        print(f"error: can't read the map {a.map}: {e}", file=sys.stderr)
        return 2
    print(f"You are at ({x}, {y}).")
    for line in sys.stdin:
        cmd = line.strip()
        if not cmd:
            continue
        if cmd == "quit":
            print("Bye.")
            return 0
        print(f'I don\'t understand "{cmd}".')
    return 0


if __name__ == "__main__":
    sys.exit(main())
