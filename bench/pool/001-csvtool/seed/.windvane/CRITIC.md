# What a 10 means for this project

People who export their spreadsheets as CSV and want a total or a slice from the shell, without opening the
spreadsheet. A 10 is as dependable as `sort` or `cut` on the commands it has: exact sums, output another program can
read, and every mistake answered with one line that says what is wrong and where.

## works
What the ticket promises, run the way a user runs it.
- 2 runs: run as a user would, on a real exported file, the ticket's main promise does what it says.
- 5 safe: nothing tried crashes, hangs, prints a traceback or changes the input file.
- 7 failures: each failure the ticket names (a missing file or column, a cell that isn't a number, a bad condition) was made to happen and gave a one-line message saying what is wrong.
- 8 real files: quoted cells with commas, a byte-order mark, Windows line endings and non-ASCII text all work.
- 10 class: the change removes a whole class of failure (float rounding, a header mismatch) rather than one case of it.

## reads
What a user reads: output, errors, help.
- 3 plain: output and errors use the ticket's exact words and formats, nothing extra on stdout.
- 7 helpful: `--help` and each command's help say what it does and what its arguments are, in plain words.
- 9 consistent: numbers, column names and messages look the same in every command.
