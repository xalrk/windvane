# Token efficiency and `stats`

On a Claude subscription, tokens are the budget an autopilot spends. windvane is built to get the most merged work
out of each one, and it measures where they go so you can tune it.

## Where tokens go

Every tool call a session makes re-reads its whole context. The cost of a session is mostly its number of turns
times its context size, so windvane cuts turns:
- **The prompt carries what the session would otherwise fetch**: the ticket, earlier feedback, the pull request
  body, the critic's verdict, and for the reviewer the diff itself (up to `inline_diff_lines`).
- **One fresh session per ticket**, not one long session for the whole run: context doesn't pile up across tickets.
  Subagents are off unless you turn them on.
- **Nothing runs twice**: a passing check is recorded per git tree, so the driver reuses the worker's own pass on
  the same tree.
- **Long commands don't poll**: sessions start them with `windvane bg` and block on `windvane await`, rather than
  checking in a loop, each check re-reading the context.
- **A lean base context**: each role starts with only the built-in tools its rules use (the worker: Bash, Read, Edit,
  Write; the critic and preflight: Read, Glob, Grep; the reviewer: Bash, Read), no MCP servers, no skills and none
  of your own hooks. Every tool's description is re-read on every turn: the full set doubled a session's first turn.
  A project's `[[periodic]]` sessions keep the default tools and the project's skills.
- **Slow growth**: everything a tool prints is re-read on each later turn, so the rules ask for test failures and
  the summary line, diffs by file, and independent tool calls in one turn.
- **Light roles get light models**: preflight runs on Sonnet at medium effort by default.
- **Parallel slots open only when they pay**: when one at a time wouldn't finish the queue before the deadline, or
  the 5-hour window is under half of `cap` (headroom that would otherwise go unused).

## `windvane stats`

`windvane stats` reads the token ledger (`ledger.jsonl`, one line per session, killed sessions included) and costs
no tokens itself. By role, over the last 7 days (`--days N` to change):

```
role         runs turns  fresh in  cached in     out   API $   min   base cached/turn
worker         34  1166    2,016k    66,032k    877k   46.87   353  20.8k       56.6k
reviewer       34   183    1,062k     5,881k    194k   13.55    37  29.5k       32.1k
critic         42   199      632k     3,368k    108k    7.89    23  21.2k       16.9k
planner         1    11       74k       376k     16k    0.99     3  17.4k       34.2k
preflight       2     2       26k        18k      1k    0.11     0      -        9.1k
(API $ is what the tokens would cost on the API: a relative meter on a subscription.)
(base: a run's first-turn context; cached/turn: what each turn re-reads, base plus what the run has added.)
```

| Column | Meaning |
|---|---|
| `runs` | sessions started |
| `turns` | model turns: each one re-reads the context |
| `fresh in` | input tokens not served from the prompt cache: the expensive part of input |
| `cached in` | input tokens read from the cache: much cheaper |
| `out` | output tokens |
| `API $` | what these tokens would cost at API prices: a relative meter, not a bill, on a subscription |
| `min` | wall-clock minutes |
| `base` | average first-turn context per run: system prompt, tools, `CLAUDE.md` files and the prompt (`-` for runs logged before the ledger kept it) |
| `cached/turn` | `cached in` over `turns`: what each turn re-reads; the gap to `base` is what the run's own tool output added |

By ticket (`--by ticket`), with each ticket's outcome and a summary of the cost per merged ticket:

```
ticket       outcome runs turns  fresh in  cached in     out   API $   min
T-017        merged     6   106      286k     7,412k     86k    5.48    20
T-015        merged     7   110      282k     6,174k     95k    5.39    35
T-014        merged     4    73      172k     4,596k     71k    3.71    15
...
per merged ticket (20): 3.9 runs, 165k tokens, 2.28 API $
spent on tickets not merged: 0k tokens, 0% of ticket tokens (fresh in + out)
```

The last line is the waste: tokens spent on tickets that are parked or not merged yet.

## Tuning

- **Turns per worker run high?** Tickets may be too big or too vague. Split them, and make "Done when" points
  checkable.
- **Many reviewer or critic runs per ticket?** Work is bouncing. Read the feedback in the log; tighten the ticket or
  the scale.
- **A role costs more than it is worth?** Change its model or effort in `[models]` and compare `stats` for the next
  runs. Changes meant to save tokens should show up here: fewer turns, less fresh input, or less output per role.
- **The critic's scores all the same?** See `windvane stats --critic` and
  [calibration](concepts.md#the-critic-and-calibration).

## Lean sessions: before and after (T-030)

Before, every session started with the CLI's full tool set (25 tools). `base` above is what those runs' streams
show (median per role, from `runs/`); a one-turn session with each role's rules and nothing else measured:

| role | base before | base after |
|---|---|---|
| worker | 21.7k | 11.4k |
| reviewer | 19.9k | 8.9k |
| critic | 20.4k | 9.6k |

Compare on the next run's `windvane stats`: the worker's `base` should drop from ~21k to ~12k, and its `cached/turn`
from ~56k to ~46k or lower (the base cut on every turn, plus slower growth). At ~1,100 worker turns a week, that is
~11M fewer cached tokens for the worker alone. If `turns` per worker run rises instead, a missing tool is costing
turns: see which in the run streams.

## Usage limits

The driver reads Claude's usage from the session streams. It pauses the run at `cap` percent of the 5-hour window
(default 75) until the window resets, and stops at `week_cap` percent of the week (default 95), leaving room for
your own work. Every merge line in the log (`windvane status` shows its tail) and the phone's `usage` command show
the current levels.
