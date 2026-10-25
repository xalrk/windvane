# What a user of ledger meets

Each check is answered by running `python3 ledger.py` in a scratch folder of your own (`$TMPDIR`), with `--file`.

- J1: `python3 ledger.py --help` lists every command the tickets built, with its arguments.
- J2: no command, an unknown command (`ledger.py spend 3`) and a command missing its arguments (`add` alone) each print a usage line to stderr and exit 2, with no traceback.
- J3: a note with spaces, quotes and non-ASCII letters (`add 4 café "flat white"`) comes back from `list` exactly as typed.
- J4: the ledger file opened in a text editor shows each entry's amount as it was added (`12.50`, not `12.5` or `12.500000001`).
- J5: every error the tickets name was made to happen and printed on one line starting `error: `, with the exit code `CLAUDE.md` gives (usage 2, others 1).
- J6: a run of add, list, remove, add, balance in a fresh folder gives the ids and the balance a user works out by hand.
- J7: `python3 -m unittest discover -s tests` passes, in under a minute.
- J8: the ledger file is read in one function and written in one function: `grep -n "open(" ledger.py` finds no other place that opens it.
