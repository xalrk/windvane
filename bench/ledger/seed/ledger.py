"""ledger: amounts with a note, kept in a JSON file."""
import argparse
import sys

VERSION = "0.1"


def parser():
    ap = argparse.ArgumentParser(prog="ledger", description="amounts with a note, kept in a JSON file")
    ap.add_argument("--version", action="version", version=f"ledger {VERSION}")
    ap.add_argument("--file", default="ledger.json", help="the ledger file (default: ledger.json here)")
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
