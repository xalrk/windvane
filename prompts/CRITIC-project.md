## Project mode: this time, the whole project
There is no ticket and no pack: you judge the project as it stands on main, in the one dimension your prompt names.
Wherever the rules above say "this ticket", read "the project"; there is no earlier critique.
- Answer every check of that dimension `yes` or `no` (no `-`, nothing out of scope). Each `no` names its evidence: a
  file:line, or a command you ran and the line of its output that shows it.
- Read the code and docs, and run read-only commands (the tests, the CLI's `--help`). This tree is reset after you:
  never fix anything.
- No "Fix in this ticket" list: every shortfall goes under `Beyond:` as one follow-up idea per line, ending
  `(covered by <ID>)` when an open ticket in your prompt already does it, else `(new)`.
- End with `CRITIC: PROJECT fixes=0 <the gist, one line>`.
