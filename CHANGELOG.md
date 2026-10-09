# Changelog

All notable changes to windvane are listed here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/). A version is its release date (`2026.10.9`); a second
release on the same day adds a number (`2026.10.9.1`).

## Unreleased

### Added
- `windvane run --from 12:00 --until 20:00 --detach` waits in tmux for its start; `run --daily 12:00-20:00 --detach`
  runs in that window every day until `windvane stop`, and `status` says when it next starts.
- `[[periodic]]` `after_group`: run the session when a group of tickets (a milestone) is finished, its prompt
  naming the group and its tickets; `windvane queue` shows the groups closest to finishing.
- `[[append_only]]`: a conflict only in entries appended to a numbered log (decisions, ADRs, migrations) is resolved
  by the driver, renumbering the branch's colliding entries and its references to them, without another worker run.

- Quiet times of your own for every project: `~/.config/windvane/quiet.txt`, beside each project's. In a quiet time a
  question already waiting for you gets its recommendation built at once (a push says which), not held all night,
  and a worker's system prompt says when the quiet time ends, or when one starts before its session can end.
- Pushes and the run's summary show a ticket's first critic score beside its final one (`2→8`); `overview` and
  `stats --critic` warn about a scale whose ladder stops under 10, since that caps every ticket's score.

### Fixed
- A heavy check that waited its whole `heavy_wait` (new in `machine.toml`, default 60 minutes) for the machine lock
  says `CHECK NOT RUN` with who held it, exits 75, and isn't counted as failed; the driver runs it again instead of
  sending the ticket back to its worker.
- A `windvane bg` job a session started before windvane updated itself is still found by `await` (and not started
  twice) when it runs in the session's worktree; one that can't be tied to it is named in one line.
- A ticket that asks several questions at once gets each to you as `T-007.q1`, `T-007.q2`: pushed, listed in
  `status` and preflight, answered (`windvane answer T-007.q2 B`) and timed out one by one; before, only the first
  reached you. `status` names a ticket's several provisional answers as `T-007.p1`, `T-007.p2`.
- `windvane init` picks `forge = "git"` for a remote that isn't on GitHub (or without `gh`), and says which it chose.
- `windvane queue` names tickets that aren't pushed yet, instead of "no tickets yet".
- `windvane install` without a terminal names `--yes`; getting started explains letting Claude set windvane up.
- The phone's `stop` and `overview` hooks aren't lost: `install`, `init` and a run's start change install.json one
  at a time, never save over one they can't read (they say so instead), and a run puts back its project's own hooks
  when they went while its repo was away.

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
