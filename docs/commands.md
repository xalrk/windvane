# Command reference

Every command takes `-C <dir>` before it to run as if started in that directory, and `--help` after it for its
options. Commands that read state (`status`, `queue`, `watch`, `stats` and so on) cost no tokens.

## Setting up

| Command | What it does |
|---|---|
| `windvane init [dir] [--name N] [--check CMD] [--forge github\|git]` | Set windvane up in a repository: writes `.windvane/` (config, queue, questions, critic scale). |
| `windvane doctor` | What this machine lacks for windvane (Claude Code logged in, gh, tmux, the phone), with the fix for each. |
| `windvane install [--yes]` | Add the windvane skill and the mention hook to Claude Code, asking before each change. |
| `windvane uninstall [--yes]` | Remove exactly what `install` added. |
| `windvane phone setup [--server URL] [--no-service]` | Turn on pushes and questions on your phone through ntfy. See [Phone setup](phone.md). |
| `windvane phone off` / `rotate` / `listen` | Turn the phone off; make new topics; act on the phone's replies (what the background service runs). |

## Tickets

| Command | What it does |
|---|---|
| `windvane new <title>` | Write a new ticket file with the next free id. |
| `windvane plan [goal] [--merge] [--no-followups] [--max N]` | A planner session writes up to N tickets (default 6) for the goal (default: config's `goal`) and the critic's follow-up notes, and opens a pull request with them. `--merge` merges them at once; `--no-followups` leaves out the critic's notes. |
| `windvane periodic <name>` | Run a `[[periodic]]` entry and its `then` chain now, as a run would between tickets; its tickets land where its `land` says. Refused while a run is on. |
| `windvane queue` | The queue: done, in progress, open, parked. |
| `windvane answer <ticket> <answer>` | Answer a ticket's question at the desk: a letter (`windvane answer T-007 B`) or free text in quotes. |
| `windvane preflight [--for 4h] [--json]` | The product questions the next tickets will need, asked now. `--json` prints them for a Claude Code session to ask. |

## Running

| Command | What it does |
|---|---|
| `windvane run --until HH:MM \| --for 6h [options]` | Work the tickets until the deadline (`--until` also takes `"YYYY-MM-DD HH:MM"`). Options below. |
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
| `windvane status [--line]` | Running or not, the slots, questions waiting for you, the log's tail. `--line`: one line. |
| `windvane overview` | Where the project stands: tickets merged, underway, not started; critic scale health. |
| `windvane watch [-n 30] [-o 6]` | Follow the running sessions live: what each says, the commands it runs, their output (`-o` lines each, 0 hides output). |
| `windvane stats [--days 7] [--by role\|ticket]` | Tokens, turns and minutes per role or ticket, from the ledger. See [Token efficiency](tokens.md). |
| `windvane stats --critic [--last 10]` | How the critic's scores spread over the last N tickets. |
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
| `windvane check [--lines 60]` | Run the project's check, reusing a pass recorded for the same git tree. |
| `windvane critic <ticket>` | Have the critic judge a ticket's evidence pack, and print the worker's next step. |
| `windvane bg [--heavy] <name> <command...>` | Start a long command in the background (under the heavy lock with `--heavy`). |
| `windvane await <name> [lines]` | Wait up to about 9.5 minutes for a `bg` command; prints `RUNNING` while it still runs. |
