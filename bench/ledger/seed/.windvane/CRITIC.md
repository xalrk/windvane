# What a 10 means for this project

People who keep their spending in a plain file from the shell, instead of a spreadsheet or an app. A 10 is as
dependable as `git` or `ledger-cli` on the commands it has: exact sums, the file never lost or half written, and
every mistake answered with one line that says what to do.

## works
What the ticket promises, run the way a user runs it.
- 2 runs: run as a user would, on real input, the ticket's main promise does what it says.
- 5 safe: nothing tried crashes, hangs, prints a traceback or loses an entry.
- 7 failures: each failure the ticket names (bad input, a missing or broken file) was made to happen and gave a one-line message saying what is wrong.
- 8 state: the ledger file is right on every way out: an error, a second run, a run from another folder with `--file`.
- 10 class: the change removes a whole class of failure (rounding, a half-written file) rather than one case of it.

## reads
What a user reads: output, errors, help.
- 3 plain: output and errors use the ticket's exact words and formats, nothing extra on stdout.
- 7 helpful: `--help` and each command's help say what it does and what its arguments are, in plain words.
- 9 consistent: amounts, ids and messages look the same in every command.
