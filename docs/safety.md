# Safety model

windvane runs Claude Code sessions unattended on a real repository. This page says what they can and cannot do, and
what only the driver (plain Python, no model) does.

## Your checkout is never touched

With `isolate = true` (the default), every session works in git worktrees the autopilot owns, under
`~/.local/state/windvane/<name>/`:

| Worktree | Used by | For |
|---|---|---|
| `work/` (`work-1/`, ... per extra slot) | workers, and the critics they start | building tickets on their branches |
| `ci/` (`ci-1/`, ...) | the driver and the reviewer | checks, reviews and merges, always on a detached head |
| `plan/` | the planner | writing tickets, even with `isolate = false` |

Nothing the driver does changes your checkout's branch, index or files, so you can keep working at the desk during a
run. Destructive git commands (`reset --hard`, `clean`) only ever run in the driver's own `ci` worktrees.

`isolate = false` runs workers in your checkout instead, for projects whose builds can't be duplicated (a large game
engine project, for example). It also limits the run to one ticket at a time. Don't edit the checkout while such a
run is going.

One exception: when windvane works on its own repository, the driver fast-forwards the windvane checkout it runs
from between tickets, so what it merges takes effect from the next ticket. It only does so for a clean checkout on
`main`; any other is left alone with one log line. An installed windvane (pipx, uv, `install.sh`) is never changed.

## What sessions may and may not do

Sessions run headless with `claude -p` in Claude Code's `auto` permission mode, plus explicit allow and deny rules
per role:

| Role | May | May not |
|---|---|---|
| **Worker** | edit files in its worktree, run commands, commit, push its ticket branch, open and edit its pull request, run `windvane` helper commands | push to `main` (in any form), force-push, `reset --hard`, check out or switch to `main`, manage worktrees, merge pull requests, spawn subagents (unless `subagents = true`) |
| **Critic** | read files and the evidence pack | run any command, edit or write files, fetch from the web, spawn agents |
| **Reviewer** | read files, run commands such as `git diff`, `git log`, `git show` | edit or write files, commit, push, switch branches, reset, merge, comment on or close pull requests |
| **Planner** | write ticket files in its own worktree | commit, push, switch branches, spawn agents: the driver commits its tickets and opens the pull request (or merges, with `--merge`) |
| **Preflight** | read the next tickets (in your checkout, read-only) | run any command, edit or write files, fetch from the web |

Every session gets its rules (`prompts/*.md`) and the ticket in its prompt, and ends with a machine-readable result
line the driver acts on. Product calls are never a session's to make: they become questions for you (see
[Concepts](concepts.md#questions-and-quiet-times)).

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
reviewers, the critics and background jobs they started) and leaves the worktrees, so the next run resumes the work.

## The heavy lock

Some checks and commands need the whole machine: a game engine, a render, a large build. Two of them at once, or one
beside the user's own engine session, can exhaust memory. `windvane heavy -- <command>` runs such a command only:
- when no other `windvane heavy` job is running on this machine, across every project and slot (one machine-wide
  lock);
- while no process from `machine.toml`'s `busy` list is running (by default Unreal Editor, shader compilers and
  Blender);
- once at least `min_free_gb` of memory is available (Linux).

It waits up to `--wait` minutes (default 60) for the machine. It never takes another tool's lock, so a heavy command
that is itself a locking tool can't deadlock waiting on itself. Set `heavy_check = true` to run the project's check
this way; sessions use `windvane bg --heavy` for long heavy jobs.

## Secrets and personal data

windvane never writes ntfy topics, emails or tokens into code, prompts, logs, pushes, commits or pull requests.
Sessions inherit your Claude Code login and your `gh` login; they don't get any other credentials from windvane.
Treat an autopilot like a colleague with push access to the repository: give it the access the work needs and no
more.
