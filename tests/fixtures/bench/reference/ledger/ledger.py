"""ledger: amounts with a note, kept in a JSON file (the bench's reference solution)."""
import argparse
import json
import os
import re
import sys
from decimal import Decimal
from pathlib import Path

VERSION = "0.1"


class Refused(Exception):
    pass


def parser():
    ap = argparse.ArgumentParser(prog="ledger", description="amounts with a note, kept in a JSON file")
    ap.add_argument("--version", action="version", version=f"ledger {VERSION}")
    ap.add_argument("--file", default="ledger.json", help="the ledger file (default: ledger.json here)")
    sub = ap.add_subparsers(dest="cmd", metavar="command")
    s = sub.add_parser("add", help="add an entry")
    s.add_argument("amount")
    s.add_argument("note", nargs="+")
    sub.add_parser("list", help="every entry, oldest first")
    sub.add_parser("balance", help="the sum of every entry")
    s = sub.add_parser("remove", help="remove an entry by its id")
    s.add_argument("id", type=int)
    return ap


def load(f):
    if not f.exists():
        return {"next": 1, "entries": []}
    try:
        d = json.loads(f.read_text())
        assert isinstance(d, dict) and isinstance(d["next"], int) and isinstance(d["entries"], list)
        return d
    except (ValueError, KeyError, AssertionError):
        raise Refused(f"{f} is not a ledger file")


def save(f, d):
    tmp = f.with_name(f.name + ".tmp")
    try:
        tmp.write_text(json.dumps(d, indent=1))
    except OSError as e:
        raise Refused(f"can't write {f}: {e.strerror}")
    os.replace(tmp, f)


def main(argv=None):
    ap = parser()
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 0
    f = Path(a.file)
    try:
        d = load(f)
        if a.cmd == "add":
            if not re.fullmatch(r"-?\d+(\.\d{1,2})?", a.amount):
                print(f"error: bad amount: {a.amount}", file=sys.stderr)
                return 2
            d["entries"].append({"id": d["next"], "amount": f"{Decimal(a.amount):.2f}", "note": " ".join(a.note)})
            d["next"] += 1
            save(f, d)
            print(f"added #{d['next'] - 1}")
        elif a.cmd == "list":
            for e in d["entries"]:
                print(f"#{e['id']} {e['amount']} {e['note']}")
        elif a.cmd == "balance":
            print(f"balance: {sum((Decimal(e['amount']) for e in d['entries']), Decimal(0)):.2f}")
        elif a.cmd == "remove":
            left = [e for e in d["entries"] if e["id"] != a.id]
            if len(left) == len(d["entries"]):
                raise Refused(f"no entry #{a.id}")
            d["entries"] = left
            save(f, d)
            print(f"removed #{a.id}")
    except Refused as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
