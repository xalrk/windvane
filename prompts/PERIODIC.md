# windvane periodic run

The project runs you between tickets, on a detached copy of current main; its own rules for you follow below: do what
they say. The user is away; nobody answers questions.

- **Findings worth work become tickets**: `.windvane/tickets/<ID>.md` with the ids your prompt gives, in order, each
  with `# <ID>: <title>`, `Depends on:`, `Touches:` (`Kind: experiment` for a change to what sessions are told or do,
  with `bench/`; no rejected idea unchanged) and what "done" means; append `- <ID> <title>` to `.windvane/QUEUE.md` with
  a shell `>>`, unread, in run order. A finding an open ticket covers goes as lines at its end. Only these, lines added
  to QUEUE.md and your final message (say what was covered, what not ticketed) land on main; the rest is discarded.
  Don't commit, push or switch branches.
- A report for a later session goes in a file: name its path in your final message.
- Your last line is exactly `PERIODIC: <one-line summary of what you found and the tickets you wrote>`, ending in
  `score=<n>` when the project's rules ask for a score.
