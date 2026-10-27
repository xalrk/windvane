"""recipes: plain-text recipes, shown, scaled and summed into a shopping list."""
import argparse
import sys

VERSION = "0.1"


def parser():
    ap = argparse.ArgumentParser(prog="recipes", description="plain-text recipes, scaled and summed from the shell")
    ap.add_argument("--version", action="version", version=f"recipes {VERSION}")
    ap.add_subparsers(dest="cmd", metavar="command")
    return ap


def main(argv=None):
    ap = parser()
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
