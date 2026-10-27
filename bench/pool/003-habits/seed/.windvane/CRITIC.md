# What a 10 means for this project

Someone keeping a few habits (read, stretch, no sugar) who marks them done at the end of the day and wants to know
how long the streak is. A 10 is as dependable as a well-made Unix tool: streaks counted right across months and
years, every mistake in the file pointed at by its line, and the user's file never rewritten behind their back.

## works
What the ticket promises, run the way a user runs it.
- 2 runs: run as a user would, in a folder with a real `habits.txt`, the ticket's main promise does what it says.
- 5 safe: nothing tried crashes, hangs, prints a traceback, or removes or reorders a line of `habits.txt`.
- 7 failures: each failure the ticket names (an unknown habit, a bad date, a bad line in the file) was made to happen and gave a one-line message saying what is wrong and where.
- 8 dates: streaks and days come out right across a month's end, a year's end and a leap day, and a day marked twice counts once.
- 10 class: the change removes a whole class of failure (off-by-one days, a hand edit breaking the file) rather than one case of it.

## reads
What a user reads: output, errors, help.
- 3 plain: output and errors use the ticket's exact words and formats, nothing extra on stdout.
- 7 helpful: `--help` and each command's help say what it does and what its arguments are, in plain words.
- 9 consistent: names, dates, `day`/`days` and messages look the same in every command.
