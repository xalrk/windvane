# Changelog

All notable changes to windvane are listed here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/). A version is its release date (`2026.10.9`); a second
release on the same day adds a number (`2026.10.9.1`).

## Unreleased

### Added
- `windvane retry <ID>` puts a failed ticket back in a running autopilot's queue once you've fixed the cause. One
  that failed at the merge after its review approved is merged without a worker or review while its branch still has
  that head. `windvane status` lists each failed ticket with the command.
- `windvane bg --cancel <name>`: end your own background job, waiting for the machine or running. Its whole
  process group goes, its place in the heavy queue frees for the jobs behind it, and its log ends with
  `windvane: cancelled`. `windvane await` now says `WAITING` for a `--heavy` job still in the queue.
- `windvane heavy --expect <min>` and `bg --heavy --expect <min>`: a heavy job says how long it likely runs, so the
  jobs waiting behind a command never timed before still get an estimate (recorded run times win over it).
  `heavy --status` shows how long the holder has run and usually runs, `running 72 min, usually 40`.
- `windvane public-check [--github]`: what a repository would expose if it went public, before it does. It reads
  every commit on every branch and tag in one pass (added lines, files named like secrets, commit messages, personal
  author emails), workflows that give a fork's pull request secrets, and with `--github` the pull requests and
  comments, secret scanning, push protection and existing Actions runs. It prints kinds and places, never the text,
  then the next steps: rotate first, then `git filter-repo`. `windvane doctor --leaks` now runs the same check.
- Claude Code's login file is out of a session's reach, in auto mode: deny rules keep the file tools off it, and
  sessions run Bash in Claude Code's sandbox, whose commands can't read it or see the login variables. Writes and the
  network stay open in it; `windvane` and `git` commands run outside it.
- Secrets kept out of `.windvane/`, which is pushed with your code: `windvane answer` refuses an answer holding a
  token, an ntfy URL or topic, a personal email or a word from `~/.config/windvane/private-terms.txt`; a change
  windvane would push to main, or a ticket it would merge, whose new `.windvane/` lines hold one stays local, and the
  phone hears the kind and place, never the text. `windvane doctor --leaks` checks a repository's whole history
  before it goes public, and `init` and `doctor` warn when a GitHub repository already is.
- Every push a session makes is scanned by a `pre-push` hook (your own still runs after it), and every branch again
  before it merges: a token, a private key, an ntfy topic, a personal email, a private word or a file named like a
  secret (`.env`, `*.pem`, `id_rsa`) is refused by kind and place. A ticket's branch that holds one goes back to its
  worker and merges as one commit once it's taken out. Reviews, critiques, check logs, session replies and workers'
  PR text are censored before they reach GitHub (home path, username and hostname too), and `windvane init` adds the
  secret-file patterns to `.gitignore`.
- `windvane --version`: the release, or for a clone its commit (and whether it has changes). `windvane doctor`
  prints it as its first line.
- `windvane heavy --mem <GB>` (and `bg --heavy --mem`), and a project's `heavy_mem`: the free memory a heavy job
  waits for, instead of `machine.toml`'s one `min_free_gb` for every job.
- A project's `heavy_reuses`: helpers its heavy jobs reuse between runs (an emulator, a Gradle daemon). The memory
  they hold counts as free for that project's heavy jobs, which no longer wait on memory they'd never need. A
  daemon that detached into its own dir (Gradle's, Kotlin's) counts by the checkout its build was started in. A
  heavy job waiting on memory such a helper holds, with no pattern naming it, says the word to add (for a JVM its
  main class, `GradleDaemon`, not `java`).

### Changed
- The head of the heavy line takes the lock only once its memory is free, and while it waits for memory a job behind
  it that fits runs first, instead of every job waiting behind it.
- A driver run from a windvane checkout now takes up new code that another project's driver (or a `git pull`)
  already put in the shared checkout, and looks for it after every merge, tickets running or not. It restarts when no
  ticket runs; a driver whose slots stay busy stops starting tickets once it has run old code for 3 hours, and
  restarts when they end. The restart's log line says how much slot time that drain left idle.
- `windvane heavy --status` says a paused holder or waiter in one pair of parentheses with how to resume it
  (`kill -CONT -- -<pgid>`, which wakes the stopped command along with `windvane heavy`), and a job whose lock
  holder has gone says `the machine lock is free` instead of waiting for it. Sessions are told to pass `--mem` to
  heavy jobs that need more or less memory than a big build.

### Fixed
- `windvane check` called from a subdirectory runs at the worktree's top level, like the driver's gate, and inside
  Claude Code's Bash sandbox still records its pass instead of counting the sandbox's placeholders as uncommitted
  changes, so the driver no longer runs the same check again.
- `windvane check` run while one of the session's sandboxed commands is still alive records its pass too: the empty
  dotfiles the sandbox leaves as mount points while it runs aren't counted as uncommitted changes.
- An approved ticket whose PR was opened as a draft is marked ready and merged instead of failing after three
  refused tries; one a person turned into a draft is set aside with a push saying `gh pr ready <n>` lets it go, and
  a refused merge now says why (a draft, a required check or review), failing at once when no retry can clear it.
- `heavy_keep_hours` keeps a kept job (`bg --heavy --keep`) running past the run's stop. The job was told only
  the stop, and its own watch killed it while the run wound down, before the hours were added. It is now told the
  stop plus the hours from the start; `windvane stop` still kills it at once, and a deadline moved earlier brings its
  stop earlier too.
- A ticket whose branch a killed run left checked out in another slot's tree lands again. Its next worker couldn't
  switch to the branch, and every READY bounced with "No open PR" while the PR was open, until `review_rounds` failed
  it. The driver now detaches such a tree before the worker starts (one with uncommitted changes keeps the ticket, so
  its worker finds them). When your own checkout has the branch, it is left alone and the worker is told to push to
  the branch from a detached head. A detached READY lands as the branch it stands on, a READY on no branch is told to
  switch to its branch, and a failing `gh` at landing is waited out instead of taken for a missing PR.
- A change the driver lands through a PR (desk answers, a `--merge` plan, a held change) no longer gives up when
  its first `gh pr merge` fails. While GitHub is still working out whether the new PR can merge it waits and tries
  again (half a minute at most), and when main moved under it the change is replayed onto main, pushed again and
  merged. Desk answers that still can't merge close their PR instead of leaving it to conflict with every later
  change, and say how to answer again; a plan or held change keeps its PR open and `windvane status` lists it.
- No worker starts once its wrap-up time has passed, whatever `min_slot` says. With `--min-slot 0` (or a short
  `ticket_hours`) a worker could start with its wrap-up behind it, end PARTIAL at once and be launched again until
  its ticket was set aside. A worker now starts only while its wrap-up is at least 5 minutes ahead; otherwise the run
  ends with `no time left for a worker`, and `windvane run` refuses a run of 25 minutes or less, or a
  `ticket_hours` under 25 minutes.
- `windvane await` no longer reports a running job as `DONE ... exit 1`. Inside Claude Code's Bash sandbox it can't
  see the job's process, so it now refuses there as `bg` does, instead of writing a false exit that let a second copy
  start under the same name; outside it, a pid owned by another user (EPERM) counts as alive, not as a death.
- A run no longer ends when its install moves from under it, as a reinstall or a Python upgrade can do. If the
  restart command is gone, the self-update keeps the run on its old code. It says once how to reinstall, and the
  next pull after that takes the new code up. A `run --detach` that can't start its driver tears down and pushes
  the reinstall step; it doesn't crash. `windvane doctor` names a running driver's interpreter or install that has
  gone.
- New windvane code that the run can't take up now sends one push a run, naming the file and the reinstall
  command. A restart whose exec fails is no longer a crash. That can happen on a noexec mount, with a bad `#!` line,
  or when the install is removed at that moment. That push says to check for the first two as well. The run goes
  on on its old code. It tries again after a reinstall or at windvane's next update.
- A Ctrl-C while a run winds down no longer skips its save, teardown and unlock: it kills the sessions still running
  and the wind-down finishes (a third Ctrl-C quits at once). A failed teardown stays in the next "started" push until
  that push really goes out. Git steps the driver guards from Ctrl-C fail at once on a password or passphrase prompt
  instead of hanging until the network timeout, and the refusal says it's a login problem; an untrusted ssh host
  key says to accept it by hand rather than that the network is down.

## 2026.10.13 - 2026-10-13

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
