# ledger

A small command-line ledger: amounts with a note, kept in a JSON file, added, listed, summed and removed from the
shell. For people who want their spending in a plain file they can read, not an app.

## Hard rules
- Python 3.11+, standard library only: nothing to install.
- One program, `ledger.py`, run as `python3 ledger.py <command> ...`; the ledger file is `--file PATH` (default
  `ledger.json` in the current directory).
- Errors go to stderr as one line starting `error: `, never a traceback; usage errors exit 2, other errors exit 1.
- Every change comes with tests in `tests/`.

## Run and test
- `python3 ledger.py --help`
- `python3 -m unittest discover -s tests`
