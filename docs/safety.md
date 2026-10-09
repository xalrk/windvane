# Safety model

windvane runs Claude Code sessions unattended on a real repository. This page says what they can and cannot do, and
what only the driver (plain Python, no model) does.

## Your checkout is never touched

With `isolate = true` (the default), every session works in git worktrees the autopilot owns, under
`~/.local/state/windvane/<name>/`:

| Worktree | Used by | For |
|---|---|---|
| `work/` (`work-1/`, ... per extra slot) | workers, and the critics they start | building tickets on their branches |
| `ci/` (`ci-1/`, ...) | the driver, the reviewer and `[[periodic]]` sessions | checks, reviews and merges, always on a detached head |
| `plan/` | the planner | writing tickets, even with `isolate = false` |

Nothing the driver does changes your checkout's branch, index or files, so you can keep working at the desk during a
run. Destructive git commands (`reset --hard`, `clean`) only ever run in the driver's own `ci` worktrees.

`isolate = false` runs workers in your checkout instead, for projects whose builds can't be duplicated (a large game
engine project, for example). It also limits the run to one ticket at a time. Don't edit the checkout while such a
run is going. Changes you leave uncommitted there are never deleted: before a ticket's check or merge, a periodic
round or a desk PR cleans the checkout, they're stashed (`git stash list`: `windvane kept: <time> (<why>)`, with a
push); if the stash fails, nothing is cleaned: the round or desk PR is skipped, and an approved ticket waits,
its approval kept, trying again every 30 seconds.

One exception: when windvane works on its own repository, the driver fast-forwards the windvane checkout it runs
from between tickets, so what it merges takes effect from the next ticket. It only does so for a clean checkout on
`main`; any other is left alone with one log line. An installed windvane (pipx, uv, `install.sh`) is never changed.

## What sessions may and may not do

Sessions run headless with `claude -p` in Claude Code's `auto` permission mode, plus explicit allow and deny rules
per role:

| Role | May | May not |
|---|---|---|
| **Worker** | edit files in its worktree, run commands, commit, push its ticket branch, open and edit its pull request, run `windvane` helper commands, file up to 2 `windvane request`s | push to `main` (in any form), force-push, `reset --hard`, check out or switch to `main`, manage worktrees, merge pull requests, spawn subagents (unless `subagents = true`), edit windvane's own files or its request inbox |
| **Critic** | read files and the evidence pack | run any command, edit or write files, fetch from the web, spawn agents |
| **Reviewer** | read files, run commands such as `git diff`, `git log`, `git show` | edit or write files, commit, push, switch branches, reset, merge, comment on or close pull requests |
| **Planner** | write ticket files in its own worktree | commit, push, switch branches, spawn agents: the driver commits its tickets and opens the pull request (or merges, with `--merge`) |
| **Periodic** | run commands and edit files on a detached `ci/` tree between tickets | commit, push, switch branches, reset, use `gh`, spawn agents: the driver keeps only new tickets, lines added at the end of open tickets and added `QUEUE.md` lines, lands them, and discards the rest |
| **Preflight** | read the next tickets (in your checkout, read-only) | run any command, edit or write files, fetch from the web |

Every session gets its rules (`prompts/*.md`) and the ticket in its prompt, and ends with a machine-readable result
line the driver acts on. Product calls are never a session's to make: they become questions for you (see
[Concepts](concepts.md#questions-and-quiet-times)).

### Workflows run only by hand

On forge `github`, every worker session's PATH starts with a `gh` wrapper from the state dir. It refuses `gh workflow
run` and `enable`, `gh run rerun`, `gh alias set` (an alias could hide one) and `gh api` calls that start, re-run or
enable a workflow or write under `/actions/`, however they're made (`env gh …`, a script, a subprocess), with one line
pointing to `windvane ci`; everything else goes to the real `gh` unchanged. Aliases already in your gh config are
checked too: a call starting with one is expanded as gh would and refused the same way, and a shell alias (`!…`) is
refused when its text names one of those commands. The list is read afresh on each such call, so an alias added mid-session counts at once, and the guard fails closed: when `gh alias list` fails or takes over 5 s, a call starting with a word gh doesn't know is refused (gh's own commands still pass). Each refusal is a `gh-refused` row in the ledger, and `windvane stats` counts workflow starts, alias changes and unread lists apart. The session's `CLAUDE_ENV_FILE` puts
it first again after your shell setup (and sources yours, if you set one). It's on with or without an Actions budget:
workflows start only through `windvane ci`. The worker's deny rules catch the plain forms first. A session could still
call the real `gh` by its full path; for a hard stop, log `gh` in with a token that has no Actions write permission.

## What only the driver does

- **Merging.** The reviewer has no merge or push rights. The driver merges `main` into the ticket's head in its own
  worktree, runs the check on that merged tree when `main` moved, and only then merges the pull request or pushes
  `main`. It verifies the merge landed before it deletes the ticket's branches.
- **Running the check** on the exact pushed head, and recording passes per git tree.
- **Deciding what runs**: the queue order, dependencies, parallel slots, retries, setting tickets aside.
- **Spending limits**: pausing at `cap` percent of the 5-hour usage window, stopping at `week_cap` percent of the
  week, stopping new work `min_slot` minutes before the deadline, and stopping any session at the deadline.
- **Talking to your phone**: sessions never see the topics; every push is censored first.

`windvane stop` ends a run after its current ticket; `windvane stop --now` kills every session of the run (workers,
reviewers, the critics and background jobs they started) and leaves the worktrees, so the next run resumes the work. A
nested session that ignores the stop is killed outright a few seconds later, and if the driver itself dies, the next
run's start kills what its sessions left running, a worker that outlived it included (each session records the
process that owns it, so a desk `windvane plan` still running beside the run keeps its own). It only kills a process it
can tell is the same one, by its start time (`/proc`, or `ps` on macOS), as a reboot may have handed the numbers to
other programs. What it killed is counted in the ledger (a `swept` row).

## The heavy lock

Some checks and commands need the whole machine: a game engine, a render, a large build. Two of them at once, or one
beside the user's own engine session, can exhaust memory. `windvane heavy -- <command>` runs such a command only:
- when no other `windvane heavy` job is running on this machine, across every project and slot (one machine-wide
  lock);
- while no process from `machine.toml`'s `busy` list is running (by default Unreal Editor, shader compilers and
  Blender);
- once at least `min_free_gb` of memory is available (Linux).

Jobs waiting for the lock run in arrival order: each joins a queue beside the lock when it asks, so a job that
arrives just as the lock frees can't overtake one that has waited, and a waiter that dies drops out of the line. While
it waits, its log says who holds the lock (pid, command, since when) and how many jobs are ahead of it.
It waits up to `machine.toml`'s `heavy_wait` minutes (default 60; `--wait` overrides it) for the machine, then gives
up without running the command: it says what it waited for (the holder, or what keeps the machine busy) and exits 75.
It never takes another tool's lock, so a heavy command that is itself a locking tool can't deadlock waiting on itself.
Set `heavy_check = true` to run the project's check this way; sessions use `windvane bg --heavy` for long heavy jobs.
A check that gave up says `CHECK NOT RUN: waited N min for the machine lock (held by ...); run it again`: a busy
machine says nothing about the change, so it isn't a failure (its ledger row says not run, `stats` lists it apart),
and the driver's merge gate runs it again, logging each wait, until the run's stop or deadline, without a worker run
or a bounce.

## Requests to windvane

`windvane request` lets a desk session or a worker in any project file a request into windvane's local inbox
(`~/.local/state/windvane/_requests/`); critic, reviewer, planner and periodic sessions can't. A body is untrusted data
from a session that may have read anything: it is censored and its outside paths are cut as it is filed, and the
triage session reads it fenced as data, never as instructions. A request can't change config, prompts or permissions:
the triage writes tickets only, which become code through the normal worker, critic and reviewer, and they land at the
queue's end. Only the desk answers a triage question (`request --answer` refuses a session), since the question is a
call for the user. The role checks and a worker's cap of 2 are kept by the command from where it runs and the
environment the runner sets (a session that claims to be the desk is still the session its `WINDVANE_SESSION`
names, from any directory), so they stop mistakes, not a shell set on getting round them; a worker's edit tools are denied the inbox and
windvane's files, but its shell could still write there: that is why every R-file is read as untrusted.

## Secrets and personal data

windvane never writes ntfy topics, emails or tokens into code, prompts, logs, pushes, commits or pull requests.
Sessions inherit your Claude Code login and your `gh` login; they don't get any other credentials from windvane.
Treat an autopilot like a colleague with push access to the repository: give it the access the work needs and no
more.
