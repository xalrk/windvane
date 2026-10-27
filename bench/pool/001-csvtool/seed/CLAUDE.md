# csvtool

A small command-line tool for CSV files: sum a column, filter rows, and total a column by group, straight from the
shell. For people who keep their numbers in spreadsheets exported as CSV and want a quick answer without opening one.

## Hard rules
- Python 3.11+, standard library only: nothing to install.
- One program, `csvtool.py`, run as `python3 csvtool.py <command> <file> ...`. The first row of a file is its
  header, and columns are named by it.
- csvtool only reads: it never changes, creates or deletes the files it is given.
- Results go to stdout and nothing else does. Errors go to stderr as one line starting `error: `, never a traceback;
  usage errors exit 2, other errors exit 1.
- Every change comes with tests in `tests/`.

## Run and test
- `python3 csvtool.py --help`
- `python3 -m unittest discover -s tests`
