"""habits: a plain-text habit log, marked done and counted into streaks from the shell."""
import argparse
import sys

VERSION = "0.1"


def parser():
    ap = argparse.ArgumentParser(prog="habits", description="a plain-text habit log, with streaks, from the shell")
    ap.add_argument("--version", action="version", version=f"habits {VERSION}")
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
