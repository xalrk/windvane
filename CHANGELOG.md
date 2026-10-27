# Changelog

All notable changes to windvane are listed here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/). A version is its release date (`2026.10.9`); a second
release on the same day adds a number (`2026.10.9.1`).

## Unreleased

### Added
- `check_measure = true` (with `heavy_check`) runs a check that holds timing tests as a `--measure` heavy job, so it
  waits for a quiet CPU. Every check's log now says the load at its start, end and peak, and a failed check whose
  load peaked over `measure_load` says so in its summary: `CHECK FAILED (exit 1) in 12.0 min, load peaked 34.12
  (quiet is ≤ 4): timing failures may be load; …`. The worker prompt names `--measure` for timing runs.
- A run on windvane itself undoes a merged change that breaks its own driver. After a self-update the new code is on
  trial until a ticket merges on it, or for an hour; two crashes, or every ticket failing at the same step with the
  same error (a driver error, or a check's same error lines), revert the merges that changed the driver's code on
  `main` (a revert commit through the usual PR or push, never a force push), write a ticket with the error, put the
  reverted tickets back in the queue after it, and send one push. The run goes on on the last good code and redoes
  the reverted tickets once the fix merges, even ones it merged itself. Reverting the revert, by hand or with
  GitHub's Revert button, merges them again. A second bad update in a row stops the run instead.
- `windvane run --replan forever` (or `replan = "forever"` in the config): an autopilot that keeps improving the
  project with no input. It plans whenever the queue runs dry, with no count to reach; with nothing merged since
  the last plan, the next waits an hour. In a project with `bench/`, the planner also gets where the projects on
  the machine hurt (`stats --projects --json`, numbers only, each project by letter) and the newest bench runs, and
  ranks its ideas by what costs projects most. Once a week, at the usage window's reset, one push gives the week's
  trend.
- A scout for ideas from outside (`builtin = "scout"` in a [[periodic]] entry): every `scout_every` (15) tickets
  and in each plateau's exploration round, it reads what changed in Claude Code, new models and write-ups on other
  agent loops, and writes at most 3 `Idea: scout` experiments. It alone reads the web, fenced as docs/safety.md says,
  and runs on its own (no `then`). A model newer than its family's newest in the ledger (with `bench/`) also makes
  the next plan's experiments one `ablation` round. `windvane stats --periodic` shows each entry's sessions and cost.
- A plateau stop for experiments, in every run: when the last `plateau` (8) experiments were all rejected, the next
  plan that writes experiments is one exploration round of only `bold`, `ablation` and `scout` ideas; if that round is all rejected too,
  experiment planning stops with one push and the run goes on with other tickets, until an experiment is kept, the
  goal or a seed changes, or `windvane experiments resume`.
- A mix of ideas for experiments, so the loop doesn't drift into small tweaks of the same few things: each
  experiment ticket names its kind (`Idea: fix`, `bold`, `ablation`, `revisit` or `scout`), and each plan's
  experiments follow `[experiments] mix` (fix 40%, bold 25%, ablation 20%, revisit 15%, scout on top), moved toward
  the kinds the bench keeps but never under `floor` (10%). The planner sees the rejected experiments one line each,
  grouped by the mechanism they changed, and a repeat of one of the last 20 needs a `Different because:` line or is
  dropped from the plan. A rejected bold experiment gets one revision, its worker shown which hidden tests and judge
  checks moved on each seed. `windvane experiments` shows each kind's share, kept rate and last kept experiment.
- `windvane stats --bench --trend`: this week against the last in four lines, each a before → after: the frozen
  seeds' hidden tests, judge and tokens per merged ticket, this project's weighted tokens per merged ticket, the
  experiments run, kept and rejected, and the bench budget used.
- A training pool for the benchmark, so experiments aren't kept for helping three projects and nothing else. A
  `[[periodic]]` entry with `builtin = "seed"` writes a new seed project into `bench/pool/` while the pool is short of
  `pool_size` (4) and after every `seed_every` (5) kept experiments; it joins only when its hidden tests fail on the
  bare seed and pass on the session's reference solution, which is never committed, and the oldest leaves past
  `pool_size`. Only that round's own branch may write `bench/pool/`. With it, experiments and the bench round run on
  the pool (`windvane bench --pool`), an experiment waits for a full pool rather than merging unmeasured, and
  `bench/`'s seeds are a frozen holdout run by hand. A refused seed is tried again the next run, at most three times per commit of `main`. When a holdout run shows
  the pool's numbers up and the holdout's down past the noise in the same week, one push says so and experiment
  planning stops until `windvane experiments resume`.
- `check_quick` in the config: a first pass `windvane check` runs before `check`, such as the tests the change
  touches. A failure there ends the check in a minute, `CHECK FAILED (quick pass, exit N)`, with no
  pass recorded, instead of a full check's length; its pass goes on to `check`. The driver's gate runs `check`
  alone, so the quick pass never decides a merge. Both commands get `WINDVANE_CHANGED` (a file listing the
  branch's changed paths) and `WINDVANE_CHECK_BY` (`worker` or `driver`), to pick or order tests by the change.
  `windvane doctor` and `stats --projects` suggest it for a project whose checks average over 10 minutes a ticket.
- An experiment budget (`[experiments]` in the config): a bench, an experiment's or the periodic one, starts only
  while the weekly usage is below `week_below` (75%) and this week's bench spend plus the run's estimate stays
  within `week_share` (30 points of the weekly limit), plus what you allowed this week. A bench that doesn't fit
  isn't started: its experiment waits, set aside but not failed, and the run goes on. At the share, one phone
  question a week per cap offers +10% or +20% for this week (no answer: wait). `windvane experiments` shows this
  week's spend, the cap and when the week resets; `windvane experiments allow +10` raises it for this week. Each
  bench run's spend is the weekly reading's rise times its share of every ledger's tokens in its span.
- Experiment tickets: `Kind: experiment` in a ticket's header (the planner and periodic sessions write it for changes
  to what sessions are told or do, in a project with `bench/`) adds a bench before the merge. After its critic, check
  and review pass, its head is benchmarked against current `main` (`experiment_repeat`, 2 runs of the head by
  default; `main`'s recorded runs reused), and it merges only when hidden tests or judge yeses rise past the noise
  with neither dropping, or both hold and tokens per merged ticket fall by `experiment_saving` (10%). Its numbers go
  in its PR body and the log. Otherwise it isn't merged: its branch is kept, `QUEUE.md` gets `- rejected: <id>
  (numbers)`, `.windvane/EXPERIMENTS.md` a record of the idea, its files and both sides' numbers, and one push says
  so. The planner and periodic sessions get that file, so a rejected idea isn't planned again unchanged.
- `protected` in `.windvane/config.toml`: paths no session's change may touch. By default, in a project with
  `bench/`, the benchmark's seeds and hidden tests, the judge's rules, the scoring and the config keys that set
  protection and the experiments' budget. A ticket, plan or periodic branch that changes one isn't merged: its branch
  is kept and held for you, one push names the paths and how to land it by hand, and `windvane status` lists it. The
  bench refuses to score when its measured worktree's `bench/` or scoring differs from the ref's committed tree.
- `windvane bench` has a blind judge: after the hidden tests, one session per seed tries a copy of the finished
  project as its user would, on about eight yes/no checks in the seed's `hidden/JUDGE.md`, knowing its goal and
  tickets but not the critic's scale or verdicts, the pass mark or the windvane ref. Its yes count, answers and
  reasons go into `bench.jsonl` and the run's table; its tokens are the bench's own, never in a seed's cost per
  merged ticket. A judge that fails twice or runs out of time gives no verdict (`-`, and `judge failed: <why>`),
  never a row of noes.
- `windvane bench compare <A> <B>`: two windvane refs, or earlier runs, side by side per seed and in all (tickets
  merged, hidden tests, judge yeses, tokens per merged ticket, wall minutes, B minus A), running only what isn't
  recorded yet. `--repeat N` runs each side N times and marks only a difference past the spread of each side's own
  runs; without it, a line says one run can't tell a small difference from noise.
- `windvane stats --bench`: every recorded bench run, a line each.
- A `[[periodic]]` entry with `builtin = "bench"` benchmarks windvane after changes land: once tickets touching its
  `after_touch` paths (`["prompts/", ...]`) have merged since the last recorded bench run, the run starts `windvane
  bench compare` between that commit and `main` as a background job and goes on with tickets (`windvane status`
  shows it). A regression past the noise (hidden tests or judge yeses down, or tokens per merged ticket up more than
  `bench_cost_up`, 25% by default, with neither up) writes one ticket with the changes merged in between, both
  sides' numbers and the seeds that moved, and sends one push; it never reverts anything. The first run records a
  baseline. It keeps to the run's caps and deadline; a run whose queue is done waits for it, and a stop or the
  deadline ends it with one push, leaving the round due for the next run.
- `windvane stats --projects [--since DATE] [--json]`: every project on the machine in one digest, read from each
  project's state dir and repo: tickets merged, failed and parked with their reasons, each role's share of
  weighted tokens and its cost per merged ticket, the critic's first-pass rate and median first and final
  scores, reviews that bounced a critic pass, check and `await` minutes, re-fixes and `CALIBRATION.md` lines
  added; then each measure's spread across projects and the projects at either end. It only reads.
- `windvane bench`: builds three small seed projects (a command-line ledger, a notes web app, a text maze game) with
  the real pipeline, side by side, each in a fresh repo with a local remote and a state dir of its own, then scores
  them on acceptance tests no session sees, run in a scratch clone of each final `main`. It records tickets merged,
  failed and parked, hidden tests passed per ticket, weighted tokens and API $ (per merged ticket too), minutes and
  weekly usage before and after in `bench.jsonl`, and prints a table (`--last` prints it again). `--ref` picks the
  windvane commit measured; the run is small (two seeds, three tickets) when weekly usage is over `machine.toml`'s
  `bench_week_small`; `--dry-run` shows the plan and what earlier runs that size cost. Sessions are denied the
  hidden tests by path, and a seed whose run streams name one anyway is reported as contaminated and not counted.
  A Ctrl-C, SIGTERM or SIGHUP ends every seed run with it and removes the worktree.
- `[critic] show`: a project whose output is visual or slow to make gives the critic a command that shows it (a
  screenshot script, a headless render). windvane runs it once per tree in the critic's copy of the head, and the
  critic gets the images it leaves in `$WINDVANE_SHOW_DIR` and its last lines of output. `show_heavy` runs it under
  `windvane heavy`, `show_minutes` limits it; a show that fails is no verdict on the ticket.
- `windvane periodic <name>` while a run is on asks that run for the round instead of refusing: it runs at the run's
  next gap between tickets, from the config on `main` as it is then, without stopping the run. `--cancel` takes the
  ask back, and `windvane status` shows it.
- The critic scales to the ticket: a small change to no risky file, never sent back, gets a light read of its diff
  and pack in a few turns instead of the full trial, and a light read that suspects a hidden fault asks for the trial
  and gets it. `[critic] depth` pins either one, and `light_lines` and `risky` set what `auto` counts as small and
  risky. `windvane stats --critic` shows the critic's tokens per ticket and its misses at each depth.
- The critic tries the change before it judges it. Each critique runs in a throwaway copy of the ticket's committed
  head, where the critic runs the command that shows the ticket's promise, the tests and small probes, then one or two
  ways it could fail (a second pass, a restart midway). A case it sees fail sends the ticket back, named. Commits,
  pushes, branch switches, `gh` and the `windvane` commands that drive the autopilot are refused there, and the copy
  is removed afterwards however the critique ends. `[critic] trial_minutes` (default 15) bounds it: then the critic
  answers on what it saw; `0` keeps it read-only. The evidence pack shrinks to the command that shows the promise and
  what the critic can't run itself. A worker's `windvane critic` runs as its session's background job `critic`, so a
  critique longer than one tool call keeps going: the worker asks again and gets that critique's verdict.
- The critic reads what a ticket changed in the text users read: its prompt holds the diff of the Markdown and
  reStructuredText files outside `.windvane/` (README, docs, CHANGELOG) and of the files `[critic] text` names
  (message files, UI strings), up to 12,000 characters, the biggest hunks cut first and named. The evidence pack no
  longer quotes that text, and a scale's check about text judges the text itself, not whether the pack quoted it.
- `windvane stats --critic` says what the critic bought: which checks sent work back, alone or with others, and
  whether the next head changed the ticket's tree or only the evidence pack; what those revision rounds cost the
  worker in turns and weighted tokens; the reviews that still asked for changes after a critic pass; and the checks
  that never decided anything.
- `windvane heavy --measure` (and `bg --heavy --measure`) for a command that measures timing: it also waits until
  the 1-minute load average is at or under `machine.toml`'s new `measure_load` (default a quarter of the CPU count),
  naming the processes using the most CPU, and lets the jobs behind it by meanwhile, for `machine.toml`'s
  `measure_hold` minutes (default 20): then it holds the line until the load is down (or has stayed up that long
  again with no heavy job running). Every heavy job's log now says the load at its start, end and peak, and
  `heavy --status` marks a measurement.
- Windows, through WSL2: `windvane doctor` says when it runs in WSL and warns, each with its fix, about a project,
  state dir or `projects_dir` on a Windows drive (under `/mnt/`), a Windows `claude`, `git`, `python3`, `node`, `gh` or
  `tmux` on `PATH`, and systemd being off (the `wsl.conf` setting that turns it on). `windvane init` warns about a
  repository on a Windows drive. [Getting started](docs/getting-started.md#on-windows) says how to install there.
- `windvane phone setup --token TOKEN` for an ntfy server of your own that lets nobody in without logging in
  (`auth-default-access: deny-all`). Pushes, the phone's replies and the buttons on each push all carry the token;
  it is kept private like the topics and never shown. A refused token is logged once and named by `setup`. The
  [phone docs](docs/phone.md#your-own-server) now explain how to run your own server, including the setting an
  iPhone needs.
  Setup prints the `ntfy access` lines that grant the token's user windvane's two topics with that user's name
  filled in, and `windvane phone rotate` adds the lines that take the old topics back. Those old topics stay in
  `phone.toml`, and every setup prints their lines again until `windvane phone setup --revoked`. Setup also checks
  that the token's user may read the reply topic, and a refused test names the one `ntfy access` line still to run.
- A ticket can wait for another ticket's open question: `Depends on: T-004's answer` (or `the answer to T-004`),
  on main or the ticket's branch, holds it while a question keyed to T-004 is under `## Open`, even after T-004
  merged. `windvane queue` shows `waiting on: T-004's open question` with the `windvane answer` that releases it. A
  worker that ends BLOCKED on a question already open under another ticket now waits for that answer instead of
  failing, and the run takes it up again once the answer comes.

### Changed
- `windvane await` on a heavy job that waits for memory or a quiet CPU names who holds it now, as
  `heavy --status` does, each process with how long it has run (`running 3 h 10 min`) and this project's own helpers
  marked, instead of only how much is free; the log's list from when the wait began is left out. A waiting job's
  advice to add a helper to `heavy_reuses` names an interpreter by its script or `-m` module (`serve.py`), not
  `python3`, past the common options that take a value (`-W ignore`, `-r ./preload.js`), and gives none for inline
  code (`-c`, `-e`) or a name that isn't a safe pattern.
- The heavy queue lets a short job go before a long one: a job that usually runs 5 minutes or less goes ahead of a
  waiting one at least three times as long, so a 1-minute check no longer waits behind a 25-minute render. The long
  job is passed over 15 minutes in all at most (or its own run time, if less); jobs with no recorded time or `--expect` keep arrival order,
  and a measurement is never passed. Both jobs' logs and `heavy --status` say who was let by.
- The critic no longer fails a check for want of the project's check or full suite passing: its rules say the
  driver runs the check on the final head after it, so it judges the tests it ran on the change. A scale check
  that asks for one (naming the check's command, or "check passed", "the test suite passes" and the like) gets a
  warning from `windvane doctor` and at each critique's start, telling you to drop it from its rung.
- `windvane status`, the phone's `status` and `overview` no longer show the month's Actions minutes: the cap acts on
  its own, and `windvane ci` and `doctor` still report the count. `status` says what happens to questions in words
  (`questions: pushed to your phone as they come`, or `none pushed until 08:00 (quiet time)`) instead of the
  worker's `questions now: ask`.
- Plan and periodic triage sessions get only the open tickets in their listing, with the done ones as a count
  (one an open ticket depends on, or merged this run, stays): thousands of tokens less on every turn on a repository
  with a few hundred done tickets, and it no longer grows with every merge. `windvane queue` shows the same, open tickets
  first; `windvane queue --all` lists every ticket. Titles in both are cut at a word. Sessions append their queue
  lines without reading QUEUE.md; one that didn't end in a line break no longer gets a ticket joined onto its
  last line.
- The run's last push puts what needs you first (questions, provisional answers, failures, tickets set aside, the
  heavy-job settings to change), then what merged as a count and ids, then the critic's scores; no title shows twice.
  One past ntfy's 4,096 bytes loses the critic's scores, then merge ids, never a line you have to act on.
- A **Failed** push for a failure in the work itself ends with the run log's path and `windvane retry <ID>` for once
  the cause is fixed, and a ticket set aside after too many runs ran out of time says how to split it (`windvane new`,
  then `done:` in QUEUE.md).

- The critic scale new projects start from (`templates/CRITIC.md`) asks what a user meets when the change is
  tried, not what the pack or a test shows, and has a starting ladder for an app with a UI, a game or visual
  output, and a library beside the command-line one. `windvane doctor` and a run's start warn of a check worded
  to grade the pack or the tests ("the pack quotes", "a test covers"), naming it.
- `windvane await` on a job still in the heavy queue says where it stands now, read as `heavy --status` reads it:
  who holds the lock and since when, the job's place in line, what it waits on and the work ahead. The log lines it
  still shows say how long ago they were written, and an estimate that ran out reads `overdue since 03:57`, not a
  time an hour ago; a waiting job's log says its line again once its estimate runs out.
- `windvane heavy --status` judges every waiter as its own wait does, not only the head: a job let by a head short
  of memory is next in line, its work ahead counts only the jobs it waits behind, and this project's own
  `heavy_reuses` helpers count as free memory for its jobs.
- A dropped late answer's line in `windvane status` says whether its ticket has merged, so you know whether
  `windvane answer` changes it now or only after the merge. At most 20 unshown ones are kept for it; the log names
  any past that.
- The phone goes easier on the ntfy server. A dropped stream is opened again after 10 s, then 20, 40 and so on up
  to 5 minutes, with one log line per outage instead of one every 10 s, and a `429` waits out its `Retry-After`.
  A push the server refuses with a `429` is logged once with the limit's name, and the next push waits out its
  `Retry-After`; while a longer one lasts, pushes are skipped and questions are asked again after it.
- A run on a machine without the `windvane phone listen` service (`setup --no-service`, or macOS) holds one stream
  for the phone's replies instead of polling every 15 s, so several projects running at once no longer run into
  ntfy.sh's limit and miss answers.
- The `windvane phone listen` service waits, saying for whom (again when that changes), while a run reads the
  phone's replies on its own stream (or another listener does) and takes over within a second of its end, instead
  of exiting and being started again every 30 s.
- Behind a proxy or VPN that holds the stream back, the listener still polls every 15 s, but now tries a stream
  again every 30 minutes and goes back to it once one gets through. Its log line says what to check. A proxy that
  turns up after the stream worked (a VPN joined later) is caught too, and an answer sent while it tries a stream
  waits at most 65 s (the 50 s try and the next poll), not 105 s.

### Fixed
- The empty read-only files a killed sandboxed command leaves no longer break git silently. A stale
  `.git/config.lock` made every `git push -u` and tracking branch fail with `could not lock config file`; the driver
  now removes these in the repo's git dir, its worktrees and the state dir at a run's start and between tickets (never
  one a running sandbox holds), and `windvane doctor` names them per place, the checkout's too, for `--clean` to
  remove. The push hint names `git push <remote> <branch>` for when a live sandbox holds the lock.
- `windvane stats` and `stats --projects` tell a heavy check's wait for the machine from its run: the `checks:` line
  gives run minutes and minutes waiting for the machine apart (`minutes` and `waited` in `--json`), and the minutes per
  checked ticket are run minutes. A check killed while it still waited in the queue counts as cancelled in the queue,
  not cut off, so the cut-off count is only checks that really ran.
- A provisional or open question's option wrapped onto an indented second line is read whole, in `status`, the
  summary and the question push, not cut at its first line's end.
- `windvane stop` with no autopilot running says there's nothing to stop and leaves no stop request behind (a
  waiting schedule is still called off). `windvane retry` with none running says what the next run will do with that
  ticket, as main has it after a fetch: that it isn't a ticket there, is already merged, is parked, waits on another
  ticket or has an open question, instead of promising to take any id up. Every command line argument now has a line in `--help`.
- A worker whose push the pre-push scan refused is told why. A READY whose branch isn't on the remote goes back at
  once with the refusal's reason (or the `git push -u` to run, when its branch was never pushed) instead of after a
  full check and a "No open PR" bounce; the next worker resumes the local branch instead of being told to start the
  ticket afresh; and the same refusal twice in a row sets the ticket aside with the fix, so the run stops spending a
  worker run and a check on it each round. `windvane retry` now takes a set-aside ticket back too.
- A SIGTERM, SIGHUP or `stop --now` during a retry's backoff ends the run within seconds, not when the backoff runs
  out (up to minutes later, after a service manager's stop timeout), so the save, the teardown and the unlock still
  happen.
- A plan or periodic session that ends without its end line (a misspelt `PLAN:`) is asked for it once in the same
  session, instead of its tickets being thrown away. If it still gives none, what it wrote is kept as a patch in the
  state dir's `dropped/` and a push says where; the log no longer says "nothing written" for a plan that wrote files.
- A project's own name in `private-terms.txt` no longer blocks work in that project's own repository: the sessions'
  pre-push hook, the scans before windvane pushes or merges, `windvane answer` and the masking of what windvane posts
  there let it through, while every other private word is still refused, and the name still is in every other
  repository (a submodule goes by its own remote's name). `windvane public-check` doesn't count it either, and says
  so.
- Nothing a run starts reaches your tmux server any more. Sessions, the project check and `setup`/`teardown` got the
  driver's `TMUX` and `TMUX_PANE` under `run --detach`, and tmux goes by `$TMUX` before `TMUX_TMPDIR`: a session that
  made a scratch tmux server and ended it with `tmux kill-server` ended yours, and every detached run on it.
- A plan, periodic or desk-answers branch that another machine pushed under the same name between windvane naming
  it and pushing it goes to the next free suffix (`-2`) instead of being held back with "see the windvane log". A
  push refused because its branch name was taken (a second clash, or a branch held before) says so, with the hand
  push and the commands that land it.
- A pushed driver branch with no PR (the desk answers' line, and `status` for one pushed by hand) names the commands
  that land it: `gh pr create --head <branch> --base main --fill`, then `gh pr merge`; off GitHub,
  `git merge <remote>/<branch>` on main.
- A repo made private between runs gets `[skip ci]` on its workers' commits at the Actions cap from the run's first
  worker: each run reads the repo's visibility afresh as it starts, instead of trusting the last run's.
- A 403 from a token without Actions access names the missing permission (Actions: read to count, read and write
  to start a workflow) in `windvane ci` and the recount's log line, not `gh auth status`. `windvane ci` says why its
  count failed too.
- A session's hooks (the push scan, and `[skip ci]` at the Actions cap) apply to its own worktree only. They used to
  become the hooks of every repository a check clones or inits, so `git lfs install` in a test's clone failed under
  `windvane check` and a test's own hooks never ran.
- A session's push from a git submodule in its worktree is scanned against what that submodule's own remote already
  has, not the project's main branch, and the submodule's own hooks run there instead of the project's. That
  includes a submodule cloned in place with its own `.git` folder, as `.gitmodules` names it when the session starts.
- A driver stopped while it rebuilt a session's hooks folder no longer leaves it empty or half-written, which let
  a session already running in that worktree push without the scan: each folder is built beside it and swapped in.
- A kept job (`bg --heavy --keep`) that its own watch ended at the run's stop, before the run had wound down to it, is
  now listed under "Kept jobs killed at the stop" in the run's summary, and its log says the run's stop killed it. On
  a slow machine the run used to miss it.
- Two `windvane answer` runs that landed in the same second no longer pick one branch name, which left the second's
  branch only on this machine (its push was refused); a periodic session's branch, named to the minute, could
  collide the same way. Every branch the driver lands its own tickets on now gets a suffix when its name is taken.
- `windvane heavy --status` gives a paused job's way out as `kill -CONT -<pgid>`, which every shell takes; the
  `kill -CONT -- -<pgid>` it gave before was refused by dash and busybox `sh` (`Illegal number: -`).
- On macOS the driver no longer runs `lsof` and `ps` over every process up to three times after each ticket to tidy
  the temp and state dirs: it asks only when a dir is old enough to go, and once.
- On macOS a helper the run started less than a second after it began (as a schedule's window does) is named at the
  run's end; `ps` gives whole seconds, and the rule asked for a second more than they need.
- A heavy job's waiting line is no longer said twice when one holder leaves and the next hasn't yet taken the lock.
- A run whose last ticket merged while it was looking for the next one takes its new windvane code up before it
  ends, instead of ending on the old code.
- The phone listener's line about a proxy holding its stream back says, when it runs as the service, what to add
  and where: `Environment=NO_PROXY=<host>` under `[Service]` with `systemctl --user edit windvane-phone`. An
  `export` in your shell doesn't reach the service.
- A stream the listener opened again after a try got through, and that brings nothing again, falls back to polling
  at once and says why, instead of reconnecting to one more silent stream first.

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
