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
  A project's `[[periodic]]` sessions keep the default tools and the project's skills. `windvane doctor` says when
  the installed `claude` is too old for these flags.
- **Slow growth**: everything a tool prints is re-read on each later turn, so the rules ask for test failures and
  the summary line, diffs by file, and independent tool calls in one turn.
- **Light roles get light models**: preflight runs on Sonnet at medium effort by default.
- **Parallel slots open only when they pay**: when one at a time wouldn't finish the queue before the deadline, or
  the 5-hour window is under half of `cap` (headroom that would otherwise go unused).

## `windvane stats`

`windvane stats` reads the token ledger (`ledger.jsonl`, one line per session, killed sessions included) and costs
no tokens itself. By role, over the last 7 days (`--days N` to change), costliest first, in 100 columns:

```
role         runs died/killed turns  fresh in  cached in     out  weighted   API $   min
worker         65           1  2204    3,741k   116,066k  1,595k   24,258k   85.03   911
reviewer       63           ·   342    1,895k     9,728k    349k    5,087k   24.09    78
critic         79           ·   366    1,125k     5,394k    197k    2,929k   14.01    43
planner         3           ·    55      193k     1,903k     49k      674k    2.90     9
preflight       2           ·     2       26k        18k      1k       38k    0.11     0
checks: 71 runs, 203 min (5.8 min a ticket checked), 9 failed, 2 cut off, 40 reused a pass
died or killed: 1 run (1 died, 0 killed for time), 948k weighted tokens, 3% of all
```

| Column | Meaning |
|---|---|
| `runs` | sessions started |
| `died/killed` | sessions that died on an API error or crashed (the ledger's `error` is set), or were killed for time (`killed: time`: the ticket's time box or the run's deadline, where a hung session ends too): the ticket runs again, so their tokens buy nothing. A deliberate stop (`stop --now`, a usage cap, a session's own turn or budget limit) isn't counted |
| `turns` | model turns: each one re-reads the context |
| `fresh in` | input tokens not served from the prompt cache (cache writes included): the expensive part of input |
| `cached in` | input tokens read from the cache: cheap per token, but most of the volume |
| `out` | output tokens |
| `weighted` | what the tokens cost, in fresh-input equivalents (below); rows sort by it |
| `API $` | what these tokens would cost at API prices: a relative meter, not a bill, on a subscription. `≥` when some runs ended before their result (no $ for them), `?` when none has one |
| `min` | wall-clock minutes |
| `base` (`--wide`) | average first-turn context per run: system prompt, tools, `CLAUDE.md` files and the prompt (`-` for runs logged before the ledger kept it) |
| `cached/turn` (`--wide`) | `cached in` over `turns`: what each turn re-reads; the gap to `base` is what the run's own tool output added |

**Weighted tokens** count each kind of token by what it costs relative to fresh input, from the API's price ratios:
fresh input 1, cache write 1.25, cached input 0.1, output 5. They are fixed weights, not a price table, so the figure
doesn't go stale when prices change, and it is the same for every model (API $ is the one that prices by model).
Cached input is ~94% of all tokens, so at 0.1 it still costs more than fresh input: a figure that leaves it out
flatters a run that re-reads a big context.

`checks:` is the check's time for tickets (the driver's and their sessions' `windvane check`, not the desk's), apart
from the `min` column; no line without any. The average is over the tickets that ran a check (a ticket that only
reused a pass isn't one). `cut off` counts checks that never finished: stopped by Ctrl-C or an error (their minutes so
far count), or killed outright, a driver or a session's tool timeout, which leaves only the check's start row and no
minutes, so the total is a floor. With `--by ticket`, `most check time: T-060, 4 runs, 31 min` follows: the ticket
whose checks took longest.

The `died or killed` line is what outages, crashes and hung sessions cost: their runs' weighted tokens, and their
share of all. `orphaned process groups killed: N` follows when a run killed process groups a dead driver's sessions
left running (the ledger's `swept` rows); without any, there's no line.

By ticket (`--by ticket`), with each ticket's outcome and what a merged ticket costs:

```
ticket       outcome runs died/killed turns  fresh in  cached in     out  weighted   API $   min
T-017        merged     6           ·   106      286k     7,412k     86k    1,527k    5.48    20
T-016        merged     3           ·   118      174k     9,505k     66k    1,499k    4.62    30
T-032        merged     8           1   115      302k     6,706k     89k    1,492k    5.53    76
...
planner      -          3           ·    55      193k     1,903k     49k      674k    2.90     9
preflight    -          2           ·     2       26k        18k      1k       38k    0.11     0
per merged ticket (49): 4.2 runs, 659k weighted tokens, 2.51 API $
with overhead: 673k weighted tokens per merged ticket
  overhead: planner, preflight: 712k
spent on tickets not merged (open, parked, failed): 0k weighted tokens, 0% of ticket tokens
died or killed: 1 run (1 died, 0 killed for time), 948k weighted tokens, 3% of all
```

- `outcome`: `merged` and `parked` come from git; `failed` is a ticket whose last attempt failed: the ledger's
  `{"event": "outcome", "outcome": "failed"}` row, which the driver writes when it gives up on a ticket (a later
  worker run makes it `open` again; for failures from before the ledger kept them, the log's `FAILED` line);
  `gone` is a ticket no longer in the queue (renumbered, folded, deleted); `open` is everything else; `-` is a run
  with no ticket.
- `per merged ticket`: the merged tickets' own runs, averaged.
- `with overhead`: the same, plus every run with no ticket (planner, preflight, periodic sessions) and every
  `gone` ticket spread over the merged tickets: the figure to compare with a person's work. The `overhead` line
  under it names what it adds and how much (`planner, preflight: 712k; 2 tickets no longer in the queue (T-009,
  T-010): 40k`).
- `spent on tickets not merged`: the waste, tokens on tickets that are open, parked or failed.

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

Compare on the next run's `windvane stats --wide`: the worker's `base` should drop from ~21k to ~12k, and its `cached/turn`
from ~56k to ~46k or lower (the base cut on every turn, plus slower growth). At ~1,100 worker turns a week, that is
~11M fewer cached tokens for the worker alone. If `turns` per worker run rises instead, a missing tool is costing
turns: see which in the run streams.

## Usage limits

The driver reads Claude's usage from the session streams. It pauses the run at `cap` percent of the 5-hour window
(default 75) until the window resets, and stops at `week_cap` percent of the week (default 95), leaving room for
your own work. Every merge line in the log (`windvane status` shows its tail) and the phone's `usage` command show
the current levels.
