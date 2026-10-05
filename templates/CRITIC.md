# What a 10 means for this project

<Who uses this project, and what would make it best in class for them. Name the products to beat.>

The critic answers yes or no to each check of each dimension a ticket touches; the driver scores it. A dimension's
score is its highest rung whose checks and every lower rung's are all `yes`; the ticket's is its lowest dimension.
Name 2-4 dimensions as `## <name>` headings, the first being whether the ticket works at all (failing its first rung
means start over). Under each, rungs as `- <rung> <id>: <check>`, ids unique across the scale: a few checks in all,
each one someone reading the evidence pack could answer yes or no, and that some tickets fail. An example:

## works
What the ticket promises, failure paths included.
- 2 shown: a test or transcript in the pack shows the ticket's main promise working.
- 5 safe: there's no known way its own code crashes, loses the user's work or hangs.
- 8 failures: every failure path the ticket names is exercised by a test or a transcript.
- 10 class: it removes a whole class of failure, and a test reproduces the old failure.
