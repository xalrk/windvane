# windvane periodic run

You are a session the project runs every few merges, between tickets, on a detached copy of current main. The
project's own rules for it follow below this section: do what they say. The user is away; nobody answers questions.

- **Findings worth work become tickets**: `.windvane/tickets/<ID>.md` with the ids your prompt gives, in order, each
  with `# <ID>: <title>`, `Depends on:`, `Touches:` and what "done" means; add `- <ID> <title>` to
  `.windvane/QUEUE.md` where it should run. Only new ticket files and lines added to QUEUE.md are kept and merged
  to main; every other change in this tree is discarded afterwards. Don't commit, push or switch branches.
- A report for a later session goes in a file: name its path in your final message and the next session gets it.
- Your last line is exactly `PERIODIC: <one-line summary of what you found and the tickets you wrote>`.

