# Changelog

All notable changes to windvane are listed here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/). A version is its release date (`2026.10.9`); a second
release on the same day adds a number (`2026.10.9.1`).

## Unreleased

### Added
- Windows, through WSL2: `windvane doctor` says when it runs in WSL and warns, each with its fix, about a project,
  state dir or `projects_dir` on a Windows drive (under `/mnt/`), a Windows `claude`, `git`, `python3`, `node`, `gh` or
  `tmux` on `PATH`, and systemd being off (the `wsl.conf` setting that turns it on). `windvane init` warns about a
  repository on a Windows drive. [Getting started](docs/getting-started.md#on-windows) says how to install there.
- `windvane phone setup --token TOKEN` for an ntfy server of your own that lets nobody in without logging in
  (`auth-default-access: deny-all`). Pushes, the phone's replies and the buttons on each push all carry the token;
  it is kept private like the topics and never shown. A refused token is logged once and named by `setup`. The
  [phone docs](docs/phone.md#your-own-server) now explain how to run your own server, including the setting an
  iPhone needs.

### Changed
- The phone goes easier on the ntfy server. A dropped stream is opened again after 10 s, then 20, 40 and so on up
  to 5 minutes, with one log line per outage instead of one every 10 s, and a `429` waits out its `Retry-After`.
  A push the server refuses with a `429` is logged once with the limit's name, and the next push waits out its
  `Retry-After`; while a longer one lasts, pushes are skipped and questions are asked again after it.
- A run on a machine without the `windvane phone listen` service (`setup --no-service`, or macOS) holds one stream
  for the phone's replies instead of polling every 15 s, so several projects running at once no longer run into
  ntfy.sh's limit and miss answers.
- The `windvane phone listen` service waits, saying for whom, while a run reads the phone's replies on its own stream
  (or another listener does) and takes over within a second of its end, instead of exiting and being started again
  every 30 s.
- Behind a proxy or VPN that holds the stream back, the listener still polls every 15 s, but now tries a stream
  again every 30 minutes and goes back to it once one gets through. Its log line says what to check.

### Fixed
- `windvane heavy --status` gives a paused job's way out as `kill -CONT -<pgid>`, which every shell takes; the
  `kill -CONT -- -<pgid>` it gave before was refused by dash and busybox `sh` (`Illegal number: -`).
- On macOS the driver no longer runs `lsof` and `ps` over every process up to three times after each ticket to tidy
  the temp and state dirs: it asks only when a dir is old enough to go, and once.
- On macOS a helper the run started less than a second after it began (as a schedule's window does) is named at the
  run's end; `ps` gives whole seconds, and the rule asked for a second more than they need.
- A heavy job's waiting line is no longer said twice when one holder leaves and the next hasn't yet taken the lock.
- A run whose last ticket merged while it was looking for the next one takes its new windvane code up before it
  ends, instead of ending on the old code.

## 2026.10.21 - 2026-10-21

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
- Kept heavy jobs: `windvane bg --heavy --keep` starts a job that outlives its session (a long training run, a
  render). The driver holds the ticket and its worktree until the job ends, then resumes the ticket with the job's
  exit code and log. A project's `heavy_keep_hours` lets such a job go on that many hours past the run's stop; a
  deadline moved earlier brings its stop earlier too, and `windvane stop` kills it at once.
- A project's `heavy_reuses`: helpers its heavy jobs reuse between runs (an emulator, a Gradle daemon). The memory
  they hold counts as free for that project's heavy jobs, which no longer wait on memory they'd never need. A
  daemon that detached into its own dir (Gradle's, Kotlin's) counts by the checkout its build was started in. A
  heavy job waiting on memory such a helper holds, with no pattern naming it, says the word to add (for a JVM its
  main class, `GradleDaemon`, not `java`).

### Changed
- `windvane critic <ID>` refuses an evidence pack whose `notes.md` is over 4000 characters, the most the critic
  reads, saying the file's size and the cap, before any critic session starts and without counting a revision. It
  used to cut the notes there silently, and the critic failed notes that stopped mid-sentence. The worker rules give
  the limit in characters as well as lines.
- The critic refusal says the cap counts characters, not bytes (`wc -m`, not `wc -c`). The driver's own critic run
  on a worker's leftover pack cuts long notes at a line end and ends them with a line telling the critic so, from
  what size, and not to judge the notes unclear for it.
- A critic answer written like `- beyond: yes` that ends the Checks block before its last checks (in a scale with
  no `beyond` check) is logged once, naming the line, since every answer after it counts as no.
- A merge GitHub holds only for required checks still running is tried again every 2 minutes for up to an hour, with
  no new worker or review, instead of failing the ticket; if they're still running then, or the run ends first, it
  fails saying so, its approval kept. A failed check names `gh pr checks <n>`, a required review says `windvane retry
  <ID>` merges the kept approval once it's given, and a `gh pr ready` refused with HTTP 403 says the gh login lacks
  write access to pull requests and points to `gh auth status`. A draft hold you let go with `gh pr ready <n>` merges
  at the next run without a new worker.
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
- Two ticket pickers no longer hand out the same id. A plan or periodic session now counts the tickets `windvane new`
  left uncommitted in your checkout, and every picker picks and reserves its ids in one step under a lock in the
  state dir, so a `new` while a plan is picking takes the id after the plan's.
- `windvane request --body`, `bg` and `heavy` no longer quietly read another file than the one a session's sandbox
  wrote under that name. In the sandbox `$TMPDIR` is a folder below the one a `windvane` command sees, so a path the
  shell expanded can name a file an earlier command left there. When the two differ, or only the sandbox's exists,
  the command stops before doing anything, names both paths and says how to pass either.
- `windvane stats`' idle-launch hint prints the log's grep only when the log still has the ticket's partial lines.
  After a rotation, or with `--days` reaching further back, it says so and gives a command that finds the reason where
  it still is: the RESULT line in the ticket's latest quick run's transcript, else the commits its branch adds.
- A merge blocked by a required check that never started on the pull request (a `paths` filter, a workflow not on
  the default branch, Actions off) now fails naming it, "required check `ci` never started on this PR", read from the
  base branch's rulesets and protection, instead of "a required check or review blocks it". While any other check
  still runs (one it may wait on with `needs:`), the merge waits for it instead. The log line
  for a gh too old to tell required checks apart names the version that can, gh 2.50.0, and `gh --version`.
- `windvane new` no longer takes an id a running plan or periodic session was handed: the session claims its ids
  in the state dir (`id_claims/`) until its branch is pushed, and a claim whose process is gone is dropped. `new`
  also hands out again the ids of a plan whose pull request `closed_plans.json` already lists as closed, without
  reading that branch.
- `windvane await` reports a job whose start was killed before its command ran as `NEVER STARTED <name>` with the
  `windvane bg` command that starts it again (exit 3), not as `DONE <name> exit 1` with an empty log, which read as the
  command failing. `bg` removes its `<name>.lock` as each start finishes, and `windvane doctor` names
  `windvane doctor --clean` whenever some of the state dir can go.
- `windvane new` no longer overwrites a ticket. It takes the id after every ticket in the checkout it writes to (its
  own HEAD, uncommitted ticket files, and the upstream), not the main checkout's HEAD, which a worktree's branch can
  be ahead of; and it refuses, naming the file, if the file already exists.
- `windvane check` in a session runs as the session's bg job `check` for every check, not only a heavy one,
  so a check longer than a tool call's 10 minutes is no longer killed with the call and its pass lost:
  past ~9.5 min it exits 75 and `windvane await check` gives its result.
- `windvane check` called from a subdirectory runs at the worktree's top level, like the driver's gate, and inside
  Claude Code's Bash sandbox still records its pass instead of counting the sandbox's placeholders as uncommitted
  changes, so the driver no longer runs the same check again.
- `windvane check` run while one of the session's sandboxed commands is still alive records its pass too: the empty
  dotfiles the sandbox leaves as mount points while it runs aren't counted as uncommitted changes.
- An approved ticket whose PR was opened as a draft is marked ready and merged instead of failing after three
  refused tries; one a person turned into a draft is set aside with a push saying `gh pr ready <n>` lets it go, and
  a refused merge now says why (a draft, a required check or review), failing at once when no retry can clear it.
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
