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
- **Light roles get light models**: preflight runs on Sonnet at medium effort by default.
- **Parallel slots open only when they pay**: when one at a time wouldn't finish the queue before the deadline, or
  the 5-hour window is under half of `cap` (headroom that would otherwise go unused).

## `windvane stats`

`windvane stats` reads the token ledger (`ledger.jsonl`, one line per session, killed sessions included) and costs
no tokens itself. By role, over the last 7 days (`--days N` to change):

```
role         runs turns  fresh in  cached in     out   API $   min
worker         24   791    1,349k    41,878k    597k   31.10   215
reviewer       24   119      711k     3,594k    126k    8.93    24
critic         29   144      443k     2,433k     77k    5.57    16
planner         1    11       74k       376k     16k    0.99     3
preflight       1     1       12k         9k      0k    0.05     0
(API $ is what the tokens would cost on the API: a relative meter on a subscription.)
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

## Usage limits

The driver reads Claude's usage from the session streams. It pauses the run at `cap` percent of the 5-hour window
(default 75) until the window resets, and stops at `week_cap` percent of the week (default 95), leaving room for
your own work. Every merge line in the log (`windvane status` shows its tail) and the phone's `usage` command show
the current levels.
