# Queue

The autopilot works tickets top to bottom, then any ticket not listed here by number. A ticket in progress (an
unmerged branch) is resumed first; a ticket waits until everything in its `Depends on:` line is merged.

- done: <ID> (why)     marks a ticket done without its own merge
- parked: <ID> (why)   the autopilot never starts it
- last: <ID> (why)     runs after every ticket not marked last
