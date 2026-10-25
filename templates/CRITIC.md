# What a 10 means for this project

<Who uses this project, and what would make it best in class for them. Name the products to beat.>

The critic answers yes or no to each check of each dimension a ticket touches; the driver scores it. A dimension's
score is its highest rung whose checks and every lower rung's are all `yes`; the ticket's is its lowest dimension.
Name 2-4 dimensions as `## <name>` headings, the first being whether the ticket works at all (failing its first rung
means start over). Under each, rungs as `- <rung> <id>: <check>`, ids unique across the scale: a few checks in all,
each one that some tickets fail. The critic tries every change in a copy of its own: it runs it the way a user would,
makes its failures happen, reads its text and looks at what it draws. So each check asks what a user meets then ("a
crash keeps the file"), never what the evidence shows ("a test covers the crash", "the pack quotes the output"):
those get answered by pasting, not by trying. Take every ladder to 10, with rungs above the pass mark
(`[critic] pass`, 8 by default) that a good ticket can reach: a ladder that stops at 8 holds every ticket at 8 or
under. An example, for a command-line tool:

## works
What the ticket promises, run the way a user runs it.
- 2 runs: run as a user would, on real input, the ticket's main promise does what it says.
- 5 safe: nothing tried crashes, hangs or loses the user's work.
- 7 failures: each failure the ticket names (a missing file, bad input, no network) was made to happen and gave a message saying what to do.
- 8 state: what it saves (a file, a cache, a lock) is right on every way out: another failure, a restart mid-step, a second run.
- 10 class: it removes a whole class of failure: the old failure, tried again, happens on the main branch and not with the change.

For other kinds of project, a first ladder to start from (keep what fits, delete the rest):

## screens (an app with a UI)
- 2 flow: in the running app, a user can do the ticket's task start to finish, on real data.
- 8 interrupted: a failed request, a reload or going back mid-task keeps what the user entered, and the screen says what happened.
- 10 every-state: each screen it touches reads right empty, full, loading and failed, at phone and desktop width.

## game (a game or visual output)
- 2 visible: launched or rendered, the change is on screen and does what the ticket says.
- 8 holds: played past it (a save and load, a restart, the edge of the map, a long session), it stays right and smooth.
- 10 beside: next to the reference the project names (a game to beat, a design), a player sees nothing worse.

## api (a library)
- 2 example: the example in its docs, pasted into a fresh project, runs and returns what they say.
- 8 errors: wrong input raises the documented error naming the argument, and leaves nothing half changed.
- 10 drop-in: an existing caller upgrades with no change, or the changelog's one-line fix is all it takes.
