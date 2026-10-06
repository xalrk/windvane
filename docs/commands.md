# Command reference

Every command takes `-C <dir>` before it to run as if started in that directory, and `--help` after it for its
options. Commands that read state (`status`, `queue`, `watch`, `stats` and so on) cost no tokens.

## Setting up

| Command | What it does |
|---|---|
| `windvane init [dir] [--name N] [--check CMD] [--forge github\|git]` | Set windvane up in a repository: writes `.windvane/` (config, queue, questions, critic scale). |
| `windvane doctor [--clean]` | What this machine lacks for windvane (Claude Code logged in and new enough for the session flags, gh, tmux, the phone), with the fix for each; it warns when the temp dir is over 90% full (inodes or space) and names windvane's leftovers there, which `--clean` removes. |
| `windvane install [--yes]` | Add the windvane skill and the mention hook to Claude Code, asking before each change. |
| `windvane uninstall [--yes]` | Remove exactly what `install` added. |
| `windvane phone setup [--server URL] [--no-service]` | Turn on pushes and questions on your phone through ntfy. See [Phone setup](phone.md). |
| `windvane phone off` / `rotate` / `listen` | Turn the phone off; make new topics; act on the phone's replies (what the background service runs). |

## Tickets

| Command | What it does |
|---|---|
| `windvane new <title>` | Write a new ticket file with the next free id. |
| `windvane plan [goal] [--merge] [--no-followups] [--max N]` | A planner session writes up to N tickets (default 6) for the goal (default: config's `goal`) and the critic's follow-up notes, and opens a pull request with them. `--merge` merges them at once; `--no-followups` leaves out the critic's notes. Its branch is `plan-<date>-<time>`; a push the remote refuses twice leaves the plan on a local branch of that name, for you to push. While another plan runs it says so and exits 1. |
| `windvane critic --project [--dims a,b] [--parallel N] [--minutes 60]` | The critic judges the whole project on `main` against `.windvane/CRITIC.md` (a ladder scale): one read-only session per dimension (`--dims` picks some), up to N at once (default: the run's `parallel`), every check answered with evidence for each `no`, joined into one printed report. Its follow-ups an open ticket covers are added to that ticket's notes on `main`; the `new` ones go to the next `windvane plan` or triage. Costs show in `stats` as `critic:project`; refused while a run is on. |
| `windvane critic --project --last` | The latest project verdicts, one line each: every dimension's score, new and covered follow-ups. |
| `windvane periodic <name>` | Run a `[[periodic]]` entry and its `then` chain now, as a run would between tickets; its tickets land where its `land` says. Refused while a run is on. |
| `windvane queue` | The queue: done, in progress, open, parked. |
| `windvane answer <ticket> <answer>` | Answer a ticket's question at the desk: a letter (`windvane answer T-007 B`) or free text in quotes. |
| `windvane preflight [--for 4h] [--json]` | The product questions the next tickets will need, asked now. `--json` prints them for a Claude Code session to ask. |

## Running

| Command | What it does |
|---|---|
| `windvane run --until HH:MM \| --for 6h [options]` | Work the tickets until the deadline (`--until` also takes `"YYYY-MM-DD HH:MM"`). Options below. |
| `windvane run --from <when> --until HH:MM \| --for 6h --detach` | Wait in tmux until `<when>` (HH:MM or `"YYYY-MM-DD HH:MM"`), then run. A start already past starts now; an `--until` before the start is the next day's (`--from 22:00 --until 06:00` runs overnight). |
| `windvane run --daily HH:MM-HH:MM --detach` | Run in this window every day (it may cross midnight), each window a run of its own with its own pushes; typed inside a window, the first runs from now. Between windows it waits without starting sessions. `stop` ends the schedule, `deadline` moves the end of the current or next window, `status` shows `waiting: starts 12:00 (daily 12:00-20:00)`. A reboot ends it. |
| `windvane stop [--now] [--if-running]` | Stop after the current ticket. `--now` kills every session of the run; their worktrees keep the work for the next run. `--if-running` (for scripts and phone hooks) does nothing when no run is going. |
| `windvane deadline <when>` | Move a running autopilot's deadline: `14:00`, `"2026-10-02 09:30"`, or from now, `+90m`, `+2h`. |

Options of `windvane run`:

| Option | Meaning |
|---|---|
| `--detach` | Run in a tmux session (`windvane-<name>`). A driver that crashes there is restarted with the same deadline and what it had done, pushing the error each time; a fourth crash within an hour ends the run. |
| `--parallel N\|auto` | The most tickets at a time (default from config). |
| `--cap`, `--week-cap`, `--ticket-hours`, `--min-slot`, `--answer-wait` | Override the matching [config keys](configuration.md#autopilot) for this run. |
| `--replan N [--goal G]` | When the queue runs dry, plan new tickets, up to N times. |
| `--no-preflight` | Start without asking the next tickets' questions. |

## Watching

| Command | What it does |
|---|---|
| `windvane status [--line [--running]]` | Running or not, the slots, questions waiting for you, a `held back for <age>, not pushed:` line per change a refused push kept on a local branch (with why and what to do, and its commit subject whole), the log's tail; while one you pushed by hand is listed, it fetches first, so one merged meanwhile (by a squash or rebase merge too) drops off by itself. `--line`: one line, with the held-back count and the oldest one's age; `--running`: such a line for every project whose autopilot is on, from anywhere (the phone's `status`). |
| `windvane overview` | Where the project stands: tickets merged, underway, not started; critic scale health; held-back changes, as in `status`; then `overview_extra`'s output. |
| `windvane watch [-n 30] [-o 6]` | Follow the running sessions live: what each says, the commands it runs, their output (`-o` lines each, 0 hides output). |
| `windvane stats [--days 7] [--by role\|ticket] [--wide]` | Tokens, turns and minutes per role or ticket, from the ledger, costliest first by weighted tokens (cache reads and output priced in), in 100 columns (`--wide` adds each run's starting context and what each turn re-reads, wider than 100 columns); runs that died or were killed for time, orphaned process groups killed, and by ticket the outcome and the cost per merged ticket with and without overhead. See [Token efficiency](tokens.md). |
| `windvane stats --critic [--last 10] [--since <YYYY-MM-DD\|commit>]` | How the critic's scores spread over the last N tickets, and each check's yes rate (first head, any head), naming one to sharpen (always yes) or lower (never yes, except a dimension's top rung, rare by design) and any check id two dimensions share; on an old numeric scale, how often its cap checks were answered and lowered a score; `--since` keeps only verdicts at or after a date or a commit's author time (say, the one that changed `CALIBRATION.md`). |
| `windvane stats --periodic [--days 7]` | Each [[periodic]] entry's `score=<n>` over time, oldest first, with its last summary. |
| `windvane mode` | How a design question is handled right now: asked, or built provisionally (quiet time). |
| `windvane quiet` | Show the quiet times file and what it means now. See [Quiet times](configuration.md#quiet-times). |

## Machine-heavy work

| Command | What it does |
|---|---|
| `windvane heavy [--wait 60] -- <command>` | Run a machine-heavy command (engine, render, big build) one at a time on this machine, waiting up to `--wait` minutes. See [the heavy lock](safety.md#the-heavy-lock). |

## Used by the sessions

These are what the autopilot's sessions call; you rarely need them yourself.

| Command | What it does |
|---|---|
| `windvane ci [workflow] [--ref <branch>] [--dry-run]` | Start a GitHub Actions workflow (default: the repo's only one) on the current branch, only if this month's minutes plus its last run's stay within `actions_cap`; prints what's used and what's left. `--dry-run` counts and says, but starts nothing. |
| `windvane check [--lines 60]` | Run the project's check, reusing a pass recorded for the same git tree. |
| `windvane critic <ticket>` | Have the critic judge a ticket's evidence pack, and print the worker's next step. |
| `windvane bg [--heavy] <name> <command...>` | Start a long command in the background (under the heavy lock with `--heavy`). |
| `windvane await <name> [lines]` | Wait up to about 9.5 minutes for a `bg` command; prints `RUNNING` while it still runs. |
