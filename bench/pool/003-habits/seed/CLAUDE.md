# habits

A small command-line streak tracker: the habits a user keeps and the days they did them, in one plain-text file,
marked done and counted into streaks from the shell. For someone who wants a habit log they can read and edit, not
an app.

## The habits file
`habits.txt` in the folder the command runs in, made by the first `add`:
```
habit read
habit stretch
2026-10-25 read
2026-10-26 read
2026-10-26 stretch
```
`habit <name>` is a habit, in the order they were added; `<YYYY-MM-DD> <name>` is a day that habit was done. Blank
lines and lines starting `#` are ignored. A user may edit the file by hand. Commands only add lines at its end: they
never remove, change or reorder the lines already there.

## Hard rules
- Python 3.11+, standard library only: nothing to install.
- One program, `habits.py`, run as `python3 habits.py <command> ...` from the folder holding `habits.txt`.
- "Today" is the computer's local date.
- Errors go to stderr as one line starting `error: `, never a traceback; usage errors exit 2, other errors exit 1.
- Every change comes with tests in `tests/`.

## Run and test
- `python3 habits.py --help`
- `python3 -m unittest discover -s tests`
