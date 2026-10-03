# Changelog

All notable changes to windvane are listed here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/).

## Unreleased

### Added
- `[[periodic]]` `after_group`: run the session when a group of tickets (a milestone) is finished, its prompt
  naming the group and its tickets; `windvane queue` shows the groups closest to finishing.
- `[[append_only]]`: a conflict only in entries appended to a numbered log (decisions, ADRs, migrations) is resolved
  by the driver, renumbering the branch's colliding entries and its references to them, without another worker run.

### Fixed
- `windvane init` picks `forge = "git"` for a remote that isn't on GitHub (or without `gh`), and says which it chose.
- `windvane queue` names tickets that aren't pushed yet, instead of "no tickets yet".
- `windvane install` without a terminal names `--yes`; getting started explains letting Claude set windvane up.

## 0.1.0 - 2026-10-02

The first release.

### The pipeline
- Works a git project's tickets unattended until a deadline: a fresh headless Claude Code worker per ticket, a
  critic that scores the outcome on the project's own scale, the project's check on the exact pushed head, a fresh
  read-only reviewer, and a merge done by the driver itself.
- Forge `github` (pull requests through `gh`) or plain `git` (the driver merges and pushes).
- Sessions work in the autopilot's own git worktrees, so the user's checkout is never touched.
- Parallel tickets: another ticket starts when its `Touches:` line doesn't overlap the running ones, the usage window
  has room, and a second slot would finish the queue sooner. Merges stay one at a time, each checked on the tree
  main will have.
- Crashed sessions (API errors, network drops) are retried after a growing pause instead of failing their ticket.
- A merge-conflict bounce re-checks only the resolution.
- `windvane plan` turns a goal and the critic's follow-up notes into tickets, safely at any time; `run --replan`
  plans more when the queue runs dry.
- `windvane heavy`: machine-heavy commands (engines, renders, big builds) run one at a time across every project.

### The critic
- Scores 0 to 10 per dimension on `.windvane/CRITIC.md`, blind to the pass mark; the ticket's score is its lowest.
- Splits its points into "Fix in this ticket" (up to 3 revisions) and "Beyond this ticket" (they feed `plan`).
- `windvane stats --critic` shows whether the scale spreads its scores; a collapsed scale is detected and reported.
- `windvane init` and the setup skill help write a good scale for a new project.

### Questions and the phone
- Product questions go into `.windvane/QUESTIONS.md` with lettered options and a recommendation, answered with
  `windvane answer` or from the phone.
- Preflight asks the questions the next tickets will predictably need when the run starts.
- Quiet times: no questions, the recommendation is built and marked provisional.
- A built-in ntfy phone (`windvane phone setup`): pushes for merges, failures, pauses and questions, censored first;
  answer buttons; `status`, `overview`, `usage` and `stop` from the phone.

### Watching and measuring
- `windvane watch`: a live view of every session (what it says, the commands it runs and their output), costing no
  tokens.
- A token ledger per run, role and ticket, shown by `windvane stats`, including killed runs.
- `windvane status`, `overview`, `queue`, and `deadline` / `stop --now` to steer a running autopilot.

### Install
- `pipx install` or `uv tool install` straight from git, or `sh install.sh` without either; standard library only,
  including the build backend.
- `windvane doctor` lists what the machine lacks and the command that fixes each.
- `windvane install` adds the windvane skill and a mention hook to Claude Code, asking before each change;
  `windvane uninstall` removes exactly that.
- A windvane checkout keeps itself current between tickets when it works on itself.
