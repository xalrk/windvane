# What a user of habits meets

Each check is answered by running `python3 habits.py` in a scratch folder of your own (`$TMPDIR`), with a
`habits.txt` made there by `add` and `done`, or written by hand in the form `CLAUDE.md` gives.

- J1: `python3 habits.py --help` lists `add`, `done`, `list`, `streak` and `week`, and each command's `--help` names its arguments and options (`--on`, `--ending`, `--per-week`) and says what they are for.
- J2: an unknown command (`habits.py skip read`) and a command missing its habit (`done` alone) each print a usage line to stderr and exit 2, with no traceback.
- J3: every error the tickets name was made to happen and printed on one line starting `error: `, with the exit code `CLAUDE.md` gives (usage 2, others 1), and a bad line's error names the right line number, blank and comment lines counted.
- J4: no command removes, changes or reorders a line already in `habits.txt`: comments, blank lines and hand-written lines are all still there, in their order, after `add` and `done`; a command that fails leaves the file's bytes as they were.
- J5: a `habits.txt` whose last line has no newline, then `done`, gives a file where the new line stands on its own line and every command still reads it.
- J6: marking a habit done for the last few days with `done --on`, across a month's end, gives the streak `streak` prints and the marks `week` shows, with no day off by one.
- J7: a streak that broke the day before yesterday shows 0 days, one done only yesterday shows 1 day, and doing it today then shows 2 days.
- J8: `python3 -m unittest discover -s tests` passes, in under a minute.
