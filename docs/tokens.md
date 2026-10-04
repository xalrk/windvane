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
no tokens itself. By role, over the last 7 days (`--days N` to change), costliest first:

```
role         runs died turns  fresh in  cached in     out  weighted   API $   min   base cached/turn
worker         51    1  1779    3,062k    98,766k  1,299k   20,198k   70.22   663  12.6k       55.5k
reviewer       50    0   277    1,558k     8,292k    281k    4,182k   19.74    55  25.8k       29.9k
critic         64    0   300      918k     4,558k    159k    2,397k   11.43    34  13.0k       15.2k
planner         2    0    38      138k     1,435k     34k      489k    2.08     7  16.0k       37.8k
preflight       2    0     2       26k        18k      1k       38k    0.11     0      -        9.1k
died or retried: 1 runs, 948k weighted tokens, 3% of all
```

| Column | Meaning |
|---|---|
| `runs` | sessions started |
| `died` | sessions that died on an API error or crashed (the ledger's `error` is set): the driver runs them again, so their tokens buy nothing |
| `turns` | model turns: each one re-reads the context |
| `fresh in` | input tokens not served from the prompt cache (cache writes included): the expensive part of input |
| `cached in` | input tokens read from the cache: cheap per token, but most of the volume |
| `out` | output tokens |
| `weighted` | what the tokens cost, in fresh-input equivalents (below); rows sort by it |
| `API $` | what these tokens would cost at API prices: a relative meter, not a bill, on a subscription. `≥` when some runs ended before their result (no $ for them), `?` when none has one |
| `min` | wall-clock minutes |
| `base` | average first-turn context per run: system prompt, tools, `CLAUDE.md` files and the prompt (`-` for runs logged before the ledger kept it) |
| `cached/turn` | `cached in` over `turns`: what each turn re-reads; the gap to `base` is what the run's own tool output added |

**Weighted tokens** count each kind of token by what it costs relative to fresh input, from the API's price ratios:
fresh input 1, cache write 1.25, cached input 0.1, output 5. They are fixed weights, not a price table, so the figure
doesn't go stale when prices change, and it is the same for every model (API $ is the one that prices by model).
Cached input is ~94% of all tokens, so at 0.1 it still costs more than fresh input: a figure that leaves it out
flatters a run that re-reads a big context.

The `died or retried` line is what outages and crashes cost: their runs' weighted tokens, and their share of all.

By ticket (`--by ticket`), with each ticket's outcome and what a merged ticket costs:

```
ticket       outcome runs died turns  fresh in  cached in     out  weighted   API $   min   base cached/turn
T-017        merged     6    0   106      286k     7,412k     86k    1,527k    5.48    20      -       69.9k
T-016        merged     3    0   118      174k     9,505k     66k    1,499k    4.62    30      -       80.6k
T-032        merged     8    1   115      302k     6,706k     89k    1,492k    5.53    76  22.5k       58.3k
...
planner      -          2    0    38      138k     1,435k     34k      489k    2.08     7  16.0k       37.8k
preflight    -          2    0     2       26k        18k      1k       38k    0.11     0      -        9.1k
per merged ticket (38): 4.3 runs, 704k weighted tokens, 2.66 API $
with overhead (planner, preflight, periodic): 718k per merged ticket
spent on tickets not merged (open, parked, failed): 0k weighted tokens, 0% of ticket tokens
died or retried: 1 runs, 948k weighted tokens, 3% of all
```

- `outcome`: `merged` and `parked` come from git; `failed` is a ticket whose last attempt in the log ended in
  `FAILED` (a later worker run makes it `open` again); `open` is everything else; `-` is a run with no ticket.
- `per merged ticket`: the merged tickets' own runs, averaged.
- `with overhead`: the same, plus every run with no ticket (planner, preflight, periodic sessions, the plan's
  critic) spread over the merged tickets: the figure to compare with a person's work.
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

Compare on the next run's `windvane stats`: the worker's `base` should drop from ~21k to ~12k, and its `cached/turn`
from ~56k to ~46k or lower (the base cut on every turn, plus slower growth). At ~1,100 worker turns a week, that is
~11M fewer cached tokens for the worker alone. If `turns` per worker run rises instead, a missing tool is costing
turns: see which in the run streams.

## Usage limits

The driver reads Claude's usage from the session streams. It pauses the run at `cap` percent of the 5-hour window
(default 75) until the window resets, and stops at `week_cap` percent of the week (default 95), leaving room for
your own work. Every merge line in the log (`windvane status` shows its tail) and the phone's `usage` command show
the current levels.
