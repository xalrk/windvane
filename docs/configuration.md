# Configuration reference

windvane reads three files. Every key in every file is optional; the defaults below apply when a key is missing.

| File | Where | What |
|---|---|---|
| `.windvane/config.toml` | in the project, committed | how this project is built, checked, reviewed and merged |
| `machine.toml` | `~/.config/windvane/` | settings for this machine: where new projects go, `windvane heavy`, `[state]` |
| `phone.toml` | `~/.config/windvane/` | the ntfy phone, written by `windvane phone setup` (see [Phone setup](phone.md)) |

`XDG_CONFIG_HOME` moves `~/.config`, and `XDG_STATE_HOME` moves the state directory (`~/.local/state/windvane/`).

`windvane init` writes a commented `.windvane/config.toml` with every key at its default. Command-line options of
`windvane run` (`--parallel`, `--cap`, `--week-cap`, `--ticket-hours`, `--min-slot`, `--answer-wait`) override the
matching keys for one run.

## Top level

| Key | Default | Meaning |
|---|---|---|
| `name` | the repository's directory name | The project's short name: its state directory (`~/.local/state/windvane/<name>/`), the tmux session (`windvane-<name>`), and the prefix of phone question ids. Lower case letters, digits and dashes. |
| `display_name` | `name`, first letter upper case (`acme` -> `Acme`) | How phone pushes, questions and the phone's `status` / `overview` replies name the project: `Acme: Done: T-007 ...`, `Acme windvane started`. |
| `prefix` | `"T"` | Ticket id prefix: `T-001`, `T-002`, and `T-002a` for a ticket split off another. Ticket branches start with the id in lower case (`t-001-...`). |
| `remote` | `"origin"` | The git remote sessions push to and the driver merges into. |
| `main` | `"main"` | The branch work is merged into. Sessions may never push to it or check it out. |
| `forge` | `"github"` | `github`: workers open pull requests with `gh` and the driver merges them. `git`: no pull requests; the driver merges the ticket branch and pushes `main` itself. |
| `actions_minutes` | `0` | Your GitHub Actions minutes a month (e.g. `2000`); `0` turns counting off. When set, windvane counts the month's billed minutes from your repos' run history, `windvane ci` keeps workflows under `actions_cap`, and `status` and `overview` show the count. See [A ticket's way through](concepts.md#a-tickets-way-through). |
| `actions_cap` | `0.9` | The share of `actions_minutes` windvane never passes. At or over it, the driver adds `[skip ci]` to its commits and pushes to your phone once that month. |
| `check` | `""` | The project's test command, run by `windvane check` in the worker's worktree and by the driver on every pushed head and merged tree, e.g. `"pytest -q"` or `"npm test"`. Empty means no check. |
| `setup` | `""` | A command run once in each fresh autopilot worktree before it is used, e.g. `"npm ci"`, with no input. If it fails, the run stops with its output. |
| `setup_minutes` | `20` | The longest `setup` may run, in minutes (any number above 0, e.g. `0.5`). One still running then (a hung `npm ci`) is killed, its child processes too, and fails as above: `setup ... timed out after 20 min in work: <its last line>; raise setup_minutes in config.toml or fix the setup`. A `windvane stop` or the run's deadline ends it sooner, and the run ends as stopped, not failed. |
| `teardown` | `""` | A command run once when a run ends, whatever ended it (the queue, the deadline, `windvane stop` or `stop --now`, a cap, a failed `setup`, a crash that isn't restarted, a SIGTERM or SIGHUP such as a closed terminal or tmux session, a detached driver the system kills), with no input, in the autopilot's `ci` worktree (the checkout with `isolate` off). It stops the helpers the project's tools start on purpose outside the sessions, which no session kill reaches, e.g. a shared Android emulator and its adb server: `teardown = "adb -s emulator-5554 emu kill; adb kill-server; ./gradlew --stop"`. It runs after the "stopped" push, so it can't hold that up; a failure or a timeout is logged with its last line and pushed with the command to run by hand, and the next run's "started" push names it once, as its helpers may still be up. A run killed outright (SIGKILL) is torn down at the next run's start, before its setup. A driver that crashes and is restarted keeps them; each `--daily` window tears down at its end. After it, what still runs in the worktrees (with `isolate` off: in the checkout, started during the run, on no terminal) is logged and named in one push, never killed; a killed driver's end isn't checked, as its sessions may still be at work. |
| `teardown_minutes` | `5` | The longest `teardown` may run, in minutes (any number above 0). One still running then is killed, its child processes too, and logged as timed out. |
| `isolate` | `true` | Work in the autopilot's own worktrees. `false` runs sessions in your checkout instead, for projects whose builds can't be duplicated; it also limits the run to one ticket at a time. |
| `goal` | `""` | What `windvane plan` and `windvane run --replan` aim at when no goal is given. |
| `author` | git's identity | `"Name <email>"` for every commit windvane makes: the driver's (merges, planned tickets) and its workers'. With `forge = "github"`, `windvane doctor` warns unless the email is a `users.noreply.github.com` one, since every pushed commit shows it. |
| `overview_extra` | `""` | A shell command, run in the checkout, whose output `windvane overview` (and so the phone's `overview`) adds below its own lines: the project's own milestones. First 15 lines, 30 s at most; a failure shows as one line. |
| `shared_notes` | `[]` | Notes files every ticket appends its findings to (e.g. `["NOTES.md"]`; paths in the repo, globs like `docs/*.md`, or a directory ending in `/`). They don't count when deciding which tickets may run side by side, whether on a Touches line or among the files a running branch changed. When a merge (a ticket's, or a desk PR's) conflicts only in these and `[[append_only]]` files, and in these files every conflict hunk only adds lines on both sides, the driver resolves it without a session: main's text, then the branch's added sections in order (a section starts at a Markdown heading, or is the added text itself). It's named in the merge commit, the log and the PR (`NOTES.md: kept main's, appended 2 sections`). A hunk that changes or deletes an existing line, on either side, or a conflict in any other file, goes back to the worker as before. Workers are told to add a new section and not rewrite existing text. |
| `scan_allow` | `[]` | Leak kinds that `windvane answer`, the sessions' pre-push hook and the checks before windvane pushes or merges let through: `"token"`, `"private-key"`, `"ntfy-url"`, `"ntfy-topic"`, `"private-term"`, `"email"`, `"secret-file"`. Only for a repository that stays private, e.g. `["private-term"]` when its tickets name your own projects ([what `.windvane/` holds](safety.md#what-windvane-holds)). |
| `heavy_check` | `false` | The check itself is machine-heavy (a game engine, a render): run it under `windvane heavy`, one at a time on this machine. A check that waited in vain for the machine is not run, not failed. |
| `heavy_mem` | unset | GB of free memory this project's heavy jobs (`windvane heavy`, `bg --heavy`) and its `heavy_check` wait for; unset: `machine.toml`'s `min_free_gb`. A job's own `--mem` (`check --mem` for the check) overrides it. A job that needs less can go by one at the head of the line that waits for more. |
| `heavy_keep_hours` | `0` | Hours a kept heavy job (`bg --heavy --keep`) may go on past the run's stop; `0`: the stop kills it, and the next run resumes its ticket. A job still running then is ended when that time comes, or by `windvane stop`; the next run keeps its ticket's worktree and resumes the ticket when it ends. |
| `heavy_reuses` | `[]` | Process-name patterns (as `busy`; for a JVM, a word of its command line such as `GradleDaemon`, or `KotlinCompileDaemon` for the Kotlin daemon it starts) of long-lived helpers this project's heavy jobs reuse, e.g. `["qemu-system", "GradleDaemon", "KotlinCompileDaemon"]`. Their resident memory counts as available to its heavy jobs and heavy check, when the process works for this project: the nearest process up its tree that works in a checkout (itself, or the shell that launched it) is in one of this project's checkouts or its state dir. A daemon that detached and works in no checkout (a Gradle daemon in `~/.gradle/daemon/<version>`) counts by the dir it was started from (`PWD` in its environment: where `./gradlew` ran). Linux only. A heavy job waiting on memory that such a helper of this project holds, with no pattern here naming it, says so once in its waiting line and suggests the word to add, its name or for a JVM its main class's last part such as `GradleDaemon` (the heavy check logs it to the project log too). |

## [models]

The Claude model and effort level for each kind of session. Models are anything `claude --model` accepts (`opus`,
`sonnet`, `haiku`, or a full model id); effort is anything `claude --effort` accepts, such as `medium` or `high`.

| Key | Default | Meaning |
|---|---|---|
| `worker` | `"opus"` | Builds each ticket. |
| `worker_effort` | `"high"` | |
| `reviewer` | `"opus"` | Reads the diff and approves or asks for changes. |
| `reviewer_effort` | `"high"` | |
| `critic` | `"opus"` | Scores each ticket's outcome. |
| `critic_effort` | `"high"` | |
| `planner` | `"opus"` | Writes tickets for `windvane plan`. |
| `planner_effort` | `"high"` | |
| `preflight` | `"sonnet"` | Reads the next tickets for predictable questions when a run starts: a light job. |
| `preflight_effort` | `"medium"` | |
| `fix` | `"opus"` | Makes a red or conflicting desk PR merge (`desk_label`). |
| `fix_effort` | `"high"` | |

`windvane stats` shows tokens and turns per role, which is what to look at before changing these. See
[Token efficiency](tokens.md).

## [critic]

| Key | Default | Meaning |
|---|---|---|
| `enabled` | `true` | Run the critic. `false` skips it: tickets go straight from the worker to the check and the review. |
| `pass` | `8` | The pass mark out of 10. A ticket whose lowest dimension scores this or more goes on. The critic never sees it: it scores blind, and the driver applies the mark. |
| `floor` | `4` | Under this on the first dimension (does it work at all), the worker is told to rethink the approach instead of patching it. |
| `revisions` | `3` | How many times the worker may revise for the critic. After the last one the ticket goes on with its score recorded (`accept`). The driver sets a ticket aside after one send-back more (a READY without the evidence pack counts). |

See [Concepts: the critic](concepts.md#the-critic-and-calibration).

## [autopilot]

| Key | Default | Meaning |
|---|---|---|
| `parallel` | `"auto"` | How many tickets run at once. `"auto"` opens another slot whenever a ready ticket doesn't overlap the running ones, the usage window has room, and either the window is under half of `cap` or one at a time wouldn't finish before the deadline, up to `max_parallel`. A number caps the slots at that; `1` works one ticket at a time. |
| `max_parallel` | `4` | The most slots `"auto"` opens. |
| `cap` | `75` | Percent of Claude's 5-hour usage window. At this level the run pauses until the window resets, then carries on. |
| `week_cap` | `95` | Percent of the weekly usage limit. At this level the run stops. |
| `ticket_hours` | `3` | The longest one worker run may take. A run that hits it is stopped and the ticket is set aside. At least 25 minutes (see `min_slot`): `windvane run` refuses less. |
| `min_slot` | `30` | Minutes. No new ticket (or retry) starts with less than this left before the deadline. Whatever it says, a worker also needs 25 minutes before its stop (the deadline or `ticket_hours`): its wrap-up is 20 minutes before it, and must be at least 5 ahead. |
| `answer_wait` | `10` | Minutes a question sent to the phone waits for an answer. After that the run builds the recommended option and records it as provisional, so you can change it later. |
| `review_rounds` | `3` | Reviews in a row asking for changes before the ticket is set aside for you. A head sent back for a missing PR spends these too. |
| `check_retries` | `2` | Red checks (on the head, or on it merged with main) a ticket may go back to its worker for. One more sets it aside. A slow or flaky check spends only this, never the reviewer's rounds. |
| `conflict_retries` | `2` | Merge conflicts with main a ticket may go back to its worker for. One more sets it aside. Conflicts windvane resolves itself (`[[append_only]]` entries) don't count. |
| `partials` | `3` | Worker runs in a row that run out of time before the ticket is set aside (it is probably too big: split it). |
| `subagents` | `false` | Let worker sessions spawn subagents. Off by default: one fresh session per ticket is the design, and subagents multiply token use. |
| `inline_diff_lines` | `1200` | Diffs up to this many lines are put straight into the reviewer's prompt, saving it the tool calls to fetch them; larger ones it reads itself. |
| `retries` | `3` | How many times a session that dies outside its ticket's control (API error, network drop, crash) is rerun before its ticket is set aside (other sessions work) or the run stops with the error (they don't). |
| `retry_minutes` | `3` | The first pause before such a rerun; each further one doubles it (3, 6, 12 minutes). |
| `stale_days` | `3` | Days. An unfinished ticket branch with no commit for this long is no longer resumed before new work: it waits for its turn in the queue order. |
| `desk_label` | `"windvane merge"` | Forge `github`: your own open PRs with this label are checked merged with `main` and merged between tickets, so you needn't merge by hand while a run is on. A red, conflicting or refused one gets a fix session. `""` turns it off. |
| `desk_fixes` | `2` | Fix sessions per desk PR before it is left for you (pushed once). A new head you push makes it a fresh candidate. |

## [[periodic]]

Sessions of the project's own (a playtest, then a triage into tickets) run between tickets, when no ticket is
running, in the `ci` worktree on current `main` (the checkout itself with `isolate = false`). None by default; see
the [README](../README.md#periodic-sessions) for an example.

| Key | Default | Meaning |
|---|---|---|
| `name` | `periodic-<n>` | Its name in the log, the phone push and the ledger (`periodic:<name>`). |
| `every` | unset | Run after this many merged tickets (counted over the run, restarts included). With none of `every`, `after_group` and `requests` set, it runs only as another entry's `then`. |
| `after_group` | unset | Run when a group of tickets is finished: a regex searched in each ticket's header (its first 8 lines), whose group 1 names the ticket's group, e.g. `'Milestone:\s*(M\d+)'`. A group is finished when none of its tickets is open (merged or `done:`; `parked:` ones don't hold it up). Groups finished when the run starts, or when the entry last ran, don't count, even if they gain a ticket and finish again (a playtest's fix tickets filed under its milestone don't start another); several finishing before it runs give one session, whose prompt names them and their tickets (`## Finished since the last run: M7 (12 tickets: T-031, ...)`). A round skipped before its session (the checkout busy, a failed `setup`, no time before the deadline) leaves them owed until the next merge. With `every` too, either makes it due and a run resets both. `windvane queue` shows the closest groups, and their parked tickets. |
| `requests` | `false` | Run while windvane's request inbox (`windvane request`, shared by every project on the machine) holds requests no round this run was given yet: the session gets up to 10 of them, fenced as untrusted data, and ends with a `REQUEST: R-NNN accept <ticket>`, `reject <why>` or `question <the question>` line each. The driver writes each verdict into its request and moves it to `done/` (an accept only for a ticket that landed or exists); a question stays in the inbox, pushed to the phone with how to answer it, until `windvane request --answer`; one with no line waits for the next run. `prompt` defaults to windvane's own `TRIAGE-REQUESTS.md`. windvane's own config has one, with `land = "end"`. |
| `minutes` | `60` | The session's time limit. It is skipped when the deadline is nearer. |
| `setup` | `""` | A shell command run first on current `main` (a build), with no input. A failure skips this round and is logged; so does a setup still running at `setup_minutes`, when the session would no longer fit before the deadline, or at any `windvane stop`. |
| `setup_minutes` | `60` | The longest `setup` may run, in minutes (any number above 0, e.g. `0.5`). |
| `prompt` | required | The project's rules file for the session, a path in the repo. Not with `builtin`; optional with `requests`. |
| `builtin` | `""` | One of windvane's own sessions instead of a `prompt`: `"project-critic"` runs `windvane critic --project` (its points an open ticket covers become that ticket's notes; a `then` triage gets the rest). |
| `dims`, `parallel` | all, `4` | With `builtin = "project-critic"`: the dimensions to judge (`["works", "clear"]`) and how many sessions at once. |
| `then` | `""` | Another `[[periodic]]` entry run right after, given this one's final text and the files it names. |
| `allow`, `deny` | `[]` | This session's own permission rules, after `[permissions]`' `periodic_allow` / `periodic_deny`; see [`[permissions]`](#permissions). |

A session ends with one line `PERIODIC: <summary>`. Only new files under `.windvane/tickets/`, lines added at the end
of a ticket that isn't merged or `done:` (a finding it already covers: any other change to a ticket is dropped and
logged) and lines added to `QUEUE.md` are kept; they land on `main` as a plan does, with the last session's final reply
(cut to 80 lines) in the commit and the PR, so what it found covered or chose not to ticket stays on record. A session cut off (deadline, stop, usage cap) or without the
line leaves nothing.

## [permissions]

Tools a project grants or denies per session role, on top of windvane's own rules (none by default). Sessions run in
auto mode, whose check may refuse a project's own scripts mid-ticket; an allow rule lets them through. A deny rule
narrows a role (a critic that must not edit, a triage that only writes docs). Rules use Claude Code's permission rule
syntax: `Tool` or `Tool(specifier)`, e.g. `Bash(tools/*)`, `Edit(docs/**)`, `WebFetch(domain:example.com)`.

```toml
[permissions]
worker_allow = ["Bash(tools/*)"]
critic_deny = ["Edit", "Write"]
```

| Key | Default | Meaning |
|---|---|---|
| `<role>_allow` | `[]` | Rules added to that role's allow list. Roles: `worker`, `critic`, `review`, `plan`, `fix`, and `periodic` (every `[[periodic]]` session; an entry adds its own `allow` / `deny`). |
| `<role>_deny` | `[]` | Rules added to that role's deny list. |

Deny wins over allow in Claude Code, so a project's allow can't lift windvane's own denies (pushing to `main`, say).
Claude Code silently drops a rule it can't parse. windvane checks every rule and key itself, so a bad one
(`bash(x)`, `Bash()`, a misspelt key) stops `windvane run` at its start, with a line in the log and the phone push.
`windvane periodic` stops before its setup, and a desk `plan` or `critic` before its session. `windvane doctor`
lists them all.

## [env]

Sessions read text anyone can write on a public repo (issues, PR comments), so they get your environment without the
variables whose names look like credentials: any name holding `TOKEN`, `SECRET`, `PASSWORD`, `PASSWD` or `API_KEY`,
ending in `_KEY`, or starting with `AWS_`. What Claude Code logs in with stays (`ANTHROPIC_*`, `CLAUDE_*`, and
`AWS_*` when `CLAUDE_CODE_USE_BEDROCK` is set), and so do gh's tokens (`GH_TOKEN`, `GITHUB_TOKEN` and their
enterprise forms), though the commands a session runs in its Bash sandbox don't get Claude Code's
([safety](safety.md#whats-trusted-and-what-a-session-can-reach)). `CLAUDE_CODE_SUBPROCESS_ENV_SCRUB` is dropped too:
Claude Code turns auto mode off when it is set, and a headless session can then write nothing.

| Key | Default | Meaning |
|---|---|---|
| `keep` | `[]` | Names of variables sessions still get, e.g. `["NPM_TOKEN"]` for a check that installs private packages. |

## [github]

By default sessions use your own `gh` login, which reaches every repository you can. A token for this repository
alone limits what a session (or a prompt injected into one) can read or push to.

| Key | Default | Meaning |
|---|---|---|
| `token_file` | `""` | A file holding a GitHub token. When set, sessions and the driver use it as `GH_TOKEN`, and git reaches github.com over HTTPS through `gh auth git-credential`, so pushes use it too (an SSH remote is rewritten to HTTPS for them). A set file that can't be read stops the command rather than fall back to your login. |

To make one, open GitHub's Settings, Developer settings, Fine-grained tokens, Generate new token. Under Repository
access choose Only select repositories and pick this one. Under Permissions give it Contents (read and write), Pull
requests (read and write) and Metadata (read-only); add Issues (read and write) only if your workers file issues.
Save it with no other text in the file and make it private to you:

```sh
umask 077; printf '%s\n' 'github_pat_...' > ~/.config/windvane/myproject.token
```

```toml
[github]
token_file = "~/.config/windvane/myproject.token"
```

`windvane doctor` checks that the token can read this repository and can't list any of your other private ones,
without printing it. Recommended for every public repository; `windvane ci` needs Actions (read and write) on it too.

## [[append_only]]

An append-only numbered file (a decision log, ADRs, migrations). Two branches that each append an entry conflict, and
both took the next number. When a merge (a ticket's, or a desk PR's) conflicts only in such files, and both sides of
every conflict hunk only add whole entries, the driver resolves it without a session: main's entries first, then the
branch's, renumbered past main's where they collide, with the branch's references to them renumbered only on lines the
branch added (in any file). The merge commit, the log, the PR comment and the merged push name the mapping (`D-5 → D-6`); the merged
tree is checked as usual. Anything else conflicting goes back to the worker (or a fix session) as before. None by
default.

| Key | Default | Meaning |
|---|---|---|
| `file` | required | The file, a path in the repo. |
| `entry` | required | A regex for the line that starts an entry; group 1 is its number. An entry runs to the next such line. |
| `ref` | required | How other lines refer to entry n, with `{n}` for the number (e.g. `D-{n}`). Zero padding is kept. |

## machine.toml

Settings for this machine, shared by every project on it.

| Key | Default | Meaning |
|---|---|---|
| `projects_dir` | none | Where the windvane skill creates new projects when you start one from a Claude Code session. |
| `busy` | `["UnrealEditor", "ShaderCompile", "blender"]` | Process names (regular expressions, as `pgrep` matches them) of heavy work started outside windvane. `windvane heavy` waits while any of them runs. |
| `min_free_gb` | `6` | `windvane heavy` waits until at least this much memory is available, for a job that doesn't say its own need (`--mem`, a project's `heavy_mem`). Linux only; elsewhere only `busy` is waited on. |
| `heavy_wait` | `60` | Minutes a heavy job run by hand (`windvane heavy`, `windvane check` with `heavy_check`) waits for the machine before giving up without running (exit 75); `windvane heavy --wait` overrides it. Sessions' heavy jobs and the driver's heavy check wait until their stop instead (the session's stop, or the run's end), so they keep their place in line. A check that gave up says `CHECK NOT RUN` and doesn't count as failed. |

## [state]

What of a project's state directory (`~/.local/state/windvane/<name>/`) may go. Set it in `machine.toml` for every
project; a project's own `[state]` table in config.toml overrides it key by key. The driver cleans its own project at a
run's start and end, and the transcripts and critic packs after each ticket; `windvane doctor --clean` cleans every
project whose autopilot isn't running (`--dry-run` lists what would go, with sizes).

Always removed, whatever these say: a parallel slot's worktree (`work-N`, `ci-N`) that is idle and holds nothing
unmerged (no uncommitted change, and no commit `main` lacks). Never removed: a tree a running session uses, a process
works in, or anything touched in the last hour. Slot 0's `work` and `ci` and the planner's `plan` stay: the next run
reuses them.

| Key | Default | Meaning |
|---|---|---|
| `keep_days` | `14` | Session transcripts (`runs/*.jsonl`) and merged tickets' critic packs (`critic/<ID>/`) and `bg` job logs (a merged ticket's `bg/<ID>/`, the desk's ended jobs in `bg/`; never a job still running) older than this go. `windvane stats` reads the token ledger, not the transcripts, so its numbers don't change. |
| `keep_runs` | `3` | An unmerged ticket's newest transcripts that stay however old, for a look at what its last runs did. |
| `trim_build` | `false` | Also remove the ignored files (build output, `node_modules`) of slot 0's and the planner's worktrees while they're idle. Off by default: the next run rebuilds them, which can take long (an engine, a Rust project). |

## Quiet times

Not TOML files: times when nobody should be asked anything, one per line, in `~/.config/windvane/quiet.txt` (yours,
for every project: when you sleep) and `~/.local/state/windvane/<name>/quiet.txt` (one project's own). A run keeps
both:
```
daily 23:00-08:00
2026-10-02 13:00-15:00
```
In a quiet time workers build their recommended option for any product question and mark it provisional instead of
asking, and a question already waiting for you gets its recommendation built at once; either way your phone is told
what was chosen and how to change it. Only questions that must wait for you (money, licences, undoing merged work)
wait until the quiet time ends. A worker's system prompt says when the quiet time ends, or when one starts before
its session can end. `windvane quiet` shows both files and what they mean right now; `windvane mode` prints only the
latter.
