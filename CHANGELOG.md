# Changelog

All notable changes to windvane are listed here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/). A version is its release date (`2026.10.9`); a second
release on the same day adds a number (`2026.10.9.1`).

## Unreleased

The first public release of windvane, an autopilot for Claude Code. It works through a git project's tickets
unattended until a deadline: each change is built by a fresh Claude Code session, then scored by a critic, tested
with the project's own check and read by a code reviewer before the driver merges it. Your checkout is never touched.

Requires Linux, Python 3.11 or newer, git and Claude Code. macOS mostly works. Optional: `gh` for GitHub projects,
tmux for detached runs and the ntfy app for your phone. windvane uses the Python standard library only. See the
[README](https://github.com/xalrk/windvane#install) to install it and
[Getting started](https://github.com/xalrk/windvane/blob/main/docs/getting-started.md) for a first run.

### The pipeline
- A fresh headless Claude Code worker per ticket, working in the autopilot's own git worktrees.
- A critic scores what users get from the change on the project's own scale and sends fixes back; the project's
  check runs on the exact pushed head; a read-only reviewer approves the diff; the driver itself merges, after
  running the check on the merged tree.
- `forge = "github"` merges through pull requests with `gh`; `forge = "git"` merges and pushes directly.
- Independent tickets, whose `Touches:` lines don't overlap, run side by side while the usage window has room.
- `windvane plan` turns a goal, and the critic's notes beyond each ticket, into new tickets; `run --replan` plans
  more when the queue runs dry.
- Merge conflicts only in entries appended to numbered logs (decisions, ADRs, migrations; `[[append_only]]`) are
  resolved by the driver without another worker run.

### Running unattended
- Deadlines (`--until`, `--for`), a later start (`--from`) and daily windows (`--daily 12:00-20:00`); `windvane
  deadline` moves a running deadline and `windvane stop` ends a run, at once with `--now`.
- Pauses near the usage limits and stops at the deadline.
- Crashed sessions are retried after a growing pause, a ticket whose sessions keep dying is set aside, network
  outages are waited out, and a detached driver that crashes is restarted.
- Periodic sessions (`[[periodic]]`), such as a playtest or an audit, run every few tickets or when a group of
  tickets is finished, and their findings become tickets.
- Pull requests of your own labelled `windvane merge` are tested and merged between tickets, so they don't race
  the autopilot's.

### The critic
- Each project describes what its best looks like in `.windvane/CRITIC.md`: a few dimensions, each a ladder of
  yes/no checks. The critic answers the checks and the driver works out the score; the critic never sees the pass
  mark.
- Its points split into fixes for this ticket and follow-ups for the planner.
- `windvane stats --critic` and `windvane overview` show whether the scale spreads its scores; pushes show a
  ticket's first and final score (`2→8`).

### Questions and your phone
- Product calls become lettered questions with a recommendation in `.windvane/QUESTIONS.md`, answered with
  `windvane answer` or from your phone.
- Preflight asks the questions the next tickets will predictably need when a run starts.
- In quiet times, per project or your own, the recommended option is built and marked provisional, to change later.
- Phone notifications through ntfy (`windvane phone setup`): merges, failures, pauses and questions, with answer
  buttons, and `status`, `overview`, `usage` and `stop` from the phone.

### Watching and measuring
- `windvane watch`: a live view of every session, at no token cost.
- A token ledger per run, role and ticket, shown by `windvane stats`; `windvane status`, `overview` and `queue` show
  where a run and the project stand.

### Install
- `pipx install` or `uv tool install` from GitHub, or `sh install.sh` from a clone.
- `windvane doctor` lists what the machine lacks and how to fix each; `windvane install` adds the windvane skill and
  a mention hook to Claude Code, asking first, and `windvane uninstall` removes them.
