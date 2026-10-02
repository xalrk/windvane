# Concepts

## Tickets

A ticket is a Markdown file in `.windvane/tickets/`, named after its id:

```markdown
# T-007: Retry failed uploads with backoff
Depends on: T-004.
Touches: src/upload.py, tests/test_upload.py.

## Goal
Uploads on a flaky connection fail outright; users lose their work. (Evidence: issue #31.)

## Scope
Retry with exponential backoff, at most 5 tries. Don't change the upload API.

## Done when
- A test with a server failing twice shows the upload succeeding on the third try.
- After 5 failures the error names the file and the last HTTP status.
```

- **Depends on:** tickets that must be merged first. A ticket waits until all of them are.
- **Touches:** the files and directories the ticket changes. It decides what may run in parallel (below). A ticket
  without this line runs alone.
- **Related:** (optional, written by the planner) open tickets that change the same feature through other files.
  Related tickets never run side by side.
- **Goal / Scope / Done when:** what the worker builds and checks itself against. "Done when" points should be
  checkable, ideally by a test. The reviewer judges the change against them.

`windvane new "<title>"` writes a ticket from the template with the next free id. `windvane plan "<goal>"` has a
planner session write several, from a goal and the critic's follow-up notes; it opens a pull request with them for
you to look at (`--merge` merges them at once).

### The queue and ticket status

`.windvane/QUEUE.md` gives the order: tickets listed as `- T-004` are worked top to bottom, then any other ticket by
number. Two more kinds of line:
```
- done: T-002 (folded into T-005)     counts as merged without a merge of its own
- parked: T-003 (needs a design call)  never started
```
Status is never kept by hand: it is derived from git. A ticket is **done** when its branch (`t-007-...`, by the
name's start: `q-t-007-...` is another branch) is merged into `main`, **in progress** while such a branch exists
unmerged (a new run resumes it first, unless nobody committed to it for `stale_days`: then it waits for its turn in
the order), and **open** otherwise. `windvane queue` shows all three.

## A ticket's way through

```
             ┌──────────────── feedback ─────────────────┐
             ▼                                           │
 ticket ─▶ worker ─▶ critic ─▶ check ─▶ reviewer ─▶ merge ─▶ main
          builds,    scores,   the       reads the    by the
          tests,     sends     project's diff,        driver, on
          pushes     fixes     tests     approves     the merged
                     back                or not       tree
```

1. **Worker.** A fresh headless Claude Code session in the autopilot's worktree. Its prompt already holds the
   ticket and any feedback from earlier rounds. It branches, builds with tests, pushes after each step, asks the
   critic, runs the check, and opens a pull request. It ends with a `RESULT:` line: `READY`, `PARTIAL` (out of
   time; the next run resumes the branch), `BLOCKED` (a question for you) or `FAILED`.
2. **Critic.** Started by the worker; scores the outcome (below).
3. **Check.** The driver runs the project's `check` on the exact pushed head. A passing result is recorded per git
   tree, so the worker's own passing check is reused rather than run twice.
4. **Reviewer.** A fresh read-only session with the diff, the ticket and the critic's verdict in its prompt. It
   approves or lists changes.
5. **Merge.** The driver merges `main` into the ticket's head in its own worktree, runs the check again if `main`
   moved meanwhile, then merges the pull request (forge `github`) or pushes `main` (forge `git`).

Anything sent back (critic points, review changes, a red check, a merge conflict) reaches the ticket's next worker
run as written feedback. A ticket that keeps bouncing is **set aside** for the rest of the run: after
`review_rounds` reviews asking for changes, `partials` runs in a row out of time, or a run longer than
`ticket_hours`. The run's summary and the phone say which and why.

## Touches and parallel slots

With `parallel = "auto"` (the default), windvane starts another ticket beside the running ones when all of these hold:
- its `Touches:` line shares no file with a running ticket's (a directory covers every file under it);
- it doesn't name a running ticket's id or one of the files that ticket touches, and isn't `Related:` to it;
- the 5-hour usage window has room for one more: current usage plus the recent cost per ticket times the tickets
  running stays under `cap`;
- another slot pays: the 5-hour window is under half of `cap`, or the tickets left, at recent tickets' minutes each,
  wouldn't finish one at a time before the deadline.

A held ticket keeps its place in the queue; the next one that passes starts instead. Each slot gets its own pair of
worktrees. Every two merge conflicts between side-by-side tickets drop one slot for the rest of the run. Merges stay
one at a time, each checked on the tree `main` will have.

The log says why a slot is idle:
```
T-010 waits: overlaps T-008 on src/api.py
T-013 waits: names src/auth.py, which T-010 changes
no 2nd slot: 3 tickets x 9 min fit before 18:45 one at a time
```

## The critic and calibration

The critic is a fresh, read-only session that judges what a ticket gives the people who use the project, not the
code (that is the reviewer's job). The worker leaves an **evidence pack** for it outside git: `notes.md` (what a
user now sees, with measured numbers, transcripts, before and after) and up to four images. A change nobody using
the project can see (a refactor, test tooling) is declared `User-facing: no` and skips the critique; the reviewer
checks that claim.

**The scale** is `.windvane/CRITIC.md`, written per project: who uses it, what best in class looks like, and two to
four dimensions, each with what a 2, a 5 and an 8 look like. The first dimension is always whether the ticket works
at all. The critic scores each dimension it can judge; the ticket's score is the lowest.

**The verdict** splits its points into "Fix in this ticket" and "Beyond this ticket". The critic never sees the pass
mark; the driver turns its scores into the worker's next step:

| Step | When | The worker |
|---|---|---|
| `pass` | lowest dimension ≥ `pass` | goes on to the check |
| `scope` | under the mark, but nothing this ticket can fix | goes on; the points go in the PR |
| `revise` | under the mark, with fixes listed | fixes them and asks again, up to `revisions` times |
| `rethink` | the first dimension under `floor` | rethinks the approach |
| `accept` | out of revisions | goes on, score recorded |

"Beyond this ticket" points go in the pull request and feed the next `windvane plan`.

**Calibration.** A scale whose scores all land on the pass mark measures nothing. `.windvane/CALIBRATION.md` holds
your own verdicts where you disagreed with the critic (date, ticket, its score, yours, why); every critic reads it.
`windvane stats --critic` shows how scores spread over recent tickets: first and final scores, revisions, how many
sit exactly on the pass mark. Once five tickets are scored on the current scale, the driver checks for a
**collapsed scale** (half the recent first scores share one value, or the number of fixes alone predicts the score)
and reports it once per run in the log, on the phone, and in `overview` until the scale is changed.

## Questions and quiet times

Workers make engineering calls themselves and say why in the pull request. **Product calls** (what users see or
do, naming, scope, look and feel, money, licences) are yours. A worker that needs one writes it into
`.windvane/QUESTIONS.md`:

```markdown
## Open
### T-007: How many upload retries before giving up?
Each retry waits twice as long; 5 tries take about a minute on a dead connection.
- **A**: 5 tries, about a minute
- **B**: 3 tries, about 15 seconds
Recommendation: A, because large uploads on mobile often need the longer window.
```

What happens next depends on the time:
- **Normally**, the worker stops `BLOCKED`, the question goes to your phone (if set up), and the ticket waits while
  others run. Answer with a tap, or at the desk with `windvane answer T-007 B` (or free text in quotes). With no
  answer in `answer_wait` minutes, the next run builds the recommendation and marks it provisional.
- **In a quiet time** (see [configuration](configuration.md#quiet-times)) nobody is asked: the worker builds its
  recommendation at once, records it under `## Provisional` with what to change to pick another option, and lists it
  in the pull request. When the quiet time ends, the phone gets one list of the provisional answers to review.
- Questions marked `Waits for the user:` (spending money, assets of unclear licence, undoing merged work) are never
  answered by default: they are asked even in a quiet time, and wait for you.

**Preflight.** When `windvane run` starts in a terminal, a light session reads the tickets likely to run before the
deadline and lists the product calls they will predictably need, so you can answer them before you leave.
`windvane preflight` runs it on its own; `run --no-preflight` skips it. Answers go under `## Answered` on `main`
and into that ticket's worker prompt.

## Where things live

In the project (committed): `.windvane/config.toml`, `tickets/`, `QUEUE.md`, `QUESTIONS.md`, `CRITIC.md`,
`CALIBRATION.md`.

On this machine only, under `~/.local/state/windvane/<name>/`:

| Path | What |
|---|---|
| `autopilot.log` | the driver's log, one line per event |
| `runs/` | each session's full stream, which `windvane watch` reads |
| `ledger.jsonl` | tokens, turns and minutes per session, which `windvane stats` reads |
| `critic.jsonl`, `critic/<ID>/` | the critic's verdicts, and each ticket's evidence pack |
| `quiet.txt` | quiet times |
| `work/`, `ci/`, `plan/` | worktrees: the sessions', the driver's checks and merges, the planner's (`work-1/`, `ci-1/` and so on for extra slots) |
