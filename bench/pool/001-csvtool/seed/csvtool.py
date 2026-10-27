"""csvtool: sum, filter and group the columns of a CSV file."""
import argparse
import sys

VERSION = "0.1"


def parser():
    ap = argparse.ArgumentParser(prog="csvtool", description="sum, filter and group the columns of a CSV file")
    ap.add_argument("--version", action="version", version=f"csvtool {VERSION}")
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
