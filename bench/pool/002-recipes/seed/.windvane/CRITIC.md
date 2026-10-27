# What a 10 means for this project

A cook who keeps recipes in plain text files and wants them for six instead of four, or one shopping list for the
week. A 10 is as dependable as a well-made Unix tool: quantities a cook can measure, every mistake in a file pointed
at by its line, and the recipe files never touched.

## works
What the ticket promises, run the way a user runs it.
- 2 runs: run as a user would, on a real recipe, the ticket's main promise does what it says.
- 5 safe: nothing tried crashes, hangs, prints a traceback or changes a recipe file.
- 7 failures: each failure the ticket names (a missing file, a bad line, a bad number) was made to happen and gave a one-line message saying what is wrong and where.
- 8 sums: quantities come out right after scaling and summing, with no float noise (`0.30000000000000004`).
- 10 class: the change removes a whole class of failure (float rounding, a unit mix-up) rather than one case of it.

## reads
What a user reads: output, errors, help.
- 3 plain: output and errors use the ticket's exact words and formats, nothing extra on stdout.
- 7 helpful: `--help` and each command's help say what it does and what its arguments are, in plain words.
- 9 consistent: quantities, units and messages look the same in every command.
