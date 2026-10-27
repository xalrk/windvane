# What a user of csvtool meets

Each check is answered by running `python3 csvtool.py` in a scratch folder of your own (`$TMPDIR`), on CSV files you
write there.

- J1: `python3 csvtool.py --help` and `sum --help` / `filter --help` list every command and option the tickets built, with their arguments and the condition syntax.
- J2: no command, an unknown command (`csvtool.py avg data.csv x`) and a command missing its arguments (`sum data.csv` alone) each print a usage line to stderr and exit 2, with no traceback.
- J3: a file exported by a spreadsheet (byte-order mark, `\r\n` line endings, quoted cells with commas and newlines, non-ASCII text) gives the same answers as the same data written by hand.
- J4: a file that isn't UTF-8 or isn't CSV at all (a PNG renamed `.csv`) prints one line starting `error: ` and exits 1, with no traceback.
- J5: every error the tickets name was made to happen and printed on one line starting `error: `, with the exit code `CLAUDE.md` gives (usage 2, others 1), and nothing on stdout.
- J6: a month of spending summed by hand (total, one category, by category) matches `sum`, `sum --where` and `sum --by` to the cent, with no `0.30000000000000004` anywhere.
- J7: the output of `filter` and `sum --by`, saved and read back by `csvtool.py` itself, gives the same sums as the original file.
- J8: `python3 -m unittest discover -s tests` passes, in under a minute, and the input files are never changed: `ls -l` before and after a session of commands shows the same files and sizes.
