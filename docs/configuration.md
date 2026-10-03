# Configuration reference

windvane reads three files. Every key in every file is optional; the defaults below apply when a key is missing.

| File | Where | What |
|---|---|---|
| `.windvane/config.toml` | in the project, committed | how this project is built, checked, reviewed and merged |
| `machine.toml` | `~/.config/windvane/` | settings for this machine: where new projects go, `windvane heavy` |
| `phone.toml` | `~/.config/windvane/` | the ntfy phone, written by `windvane phone setup` (see [Phone setup](phone.md)) |

`XDG_CONFIG_HOME` moves `~/.config`, and `XDG_STATE_HOME` moves the state directory (`~/.local/state/windvane/`).

`windvane init` writes a commented `.windvane/config.toml` with every key at its default. Command-line options of
`windvane run` (`--parallel`, `--cap`, `--week-cap`, `--ticket-hours`, `--min-slot`, `--answer-wait`) override the
matching keys for one run.

## Top level

| Key | Default | Meaning |
|---|---|---|
| `name` | the repository's directory name | The project's short name: its state directory (`~/.local/state/windvane/<name>/`), the tmux session (`windvane-<name>`), and the prefix of phone question ids. Lower case letters, digits and dashes. |
| `prefix` | `"T"` | Ticket id prefix: `T-001`, `T-002`, and `T-002a` for a ticket split off another. Ticket branches start with the id in lower case (`t-001-...`). |
| `remote` | `"origin"` | The git remote sessions push to and the driver merges into. |
| `main` | `"main"` | The branch work is merged into. Sessions may never push to it or check it out. |
| `forge` | `"github"` | `github`: workers open pull requests with `gh` and the driver merges them. `git`: no pull requests; the driver merges the ticket branch and pushes `main` itself. |
| `check` | `""` | The project's test command, run by `windvane check` in the worker's worktree and by the driver on every pushed head and merged tree, e.g. `"pytest -q"` or `"npm test"`. Empty means no check. |
| `setup` | `""` | A command run once in each fresh autopilot worktree before it is used, e.g. `"npm ci"`. If it fails, the run stops with its output. |
| `isolate` | `true` | Work in the autopilot's own worktrees. `false` runs sessions in your checkout instead, for projects whose builds can't be duplicated; it also limits the run to one ticket at a time. |
| `goal` | `""` | What `windvane plan` and `windvane run --replan` aim at when no goal is given. |
| `author` | git's identity | `"Name <email>"` for the driver's own commits (merges, planned tickets). |
| `overview_extra` | `""` | A shell command, run in the checkout, whose output `windvane overview` (and so the phone's `overview`) adds below its own lines: the project's own milestones. First 15 lines, 30 s at most; a failure shows as one line. |
| `heavy_check` | `false` | The check itself is machine-heavy (a game engine, a render): run it under `windvane heavy`, one at a time on this machine. |

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
| `ticket_hours` | `3` | The longest one worker run may take. A run that hits it is stopped and the ticket is set aside. |
| `min_slot` | `30` | Minutes. No new ticket (or retry) starts with less than this left before the deadline. |
| `answer_wait` | `10` | Minutes a question sent to the phone waits for an answer. After that the run builds the recommended option and records it as provisional, so you can change it later. |
| `review_rounds` | `3` | Reviews in a row asking for changes before the ticket is set aside for you. A head sent back for a missing PR spends these too. |
| `check_retries` | `2` | Red checks (on the head, or on it merged with main) a ticket may go back to its worker for. One more sets it aside. A slow or flaky check spends only this, never the reviewer's rounds. |
| `conflict_retries` | `2` | Merge conflicts with main a ticket may go back to its worker for. One more sets it aside. Conflicts windvane resolves itself (`[[append_only]]` entries) don't count. |
| `partials` | `3` | Worker runs in a row that run out of time before the ticket is set aside (it is probably too big: split it). |
| `subagents` | `false` | Let worker sessions spawn subagents. Off by default: one fresh session per ticket is the design, and subagents multiply token use. |
| `inline_diff_lines` | `1200` | Diffs up to this many lines are put straight into the reviewer's prompt, saving it the tool calls to fetch them; larger ones it reads itself. |
| `retries` | `3` | How many times a session that dies outside its ticket's control (API error, network drop, crash) is rerun before the run stops with the error. |
| `retry_minutes` | `3` | The first pause before such a rerun; each further one doubles it (3, 6, 12 minutes). |
| `stale_days` | `3` | Days. An unfinished ticket branch with no commit for this long is no longer resumed before new work: it waits for its turn in the queue order. |
| `desk_label` | `"windvane merge"` | Forge `github`: your own open PRs with this label are checked on their exact head and merged between tickets, so you needn't merge by hand while a run is on. A red, conflicting or refused one gets a fix session. `""` turns it off. |
| `desk_fixes` | `2` | Fix sessions per desk PR before it is left for you (pushed once). A new head you push makes it a fresh candidate. |

## [[periodic]]

Sessions of the project's own (a playtest, then a triage into tickets) run between tickets, when no ticket is
running, in the `ci` worktree on current `main` (the checkout itself with `isolate = false`). None by default; see
the [README](../README.md#periodic-sessions) for an example.

| Key | Default | Meaning |
|---|---|---|
| `name` | `periodic-<n>` | Its name in the log, the phone push and the ledger (`periodic:<name>`). |
| `every` | unset | Run after this many merged tickets (counted over the run, restarts included). With neither `every` nor `after_group` set, it runs only as another entry's `then`. |
| `after_group` | unset | Run when a group of tickets is finished: a regex searched in each ticket's header (its first 8 lines), whose group 1 names the ticket's group, e.g. `'Milestone:\s*(M\d+)'`. A group is finished when none of its tickets is open (merged or `done:`; `parked:` ones don't hold it up). Groups finished when the run starts, or when the entry last ran, don't count, even if they gain a ticket and finish again (a playtest's fix tickets filed under its milestone don't start another); several finishing before it runs give one session, whose prompt names them and their tickets (`## Finished since the last run: M7 (12 tickets: T-031, ...)`). A round skipped before its session (the checkout busy, a failed `setup`, no time before the deadline) leaves them owed until the next merge. With `every` too, either makes it due and a run resets both. `windvane queue` shows the closest groups, and their parked tickets. |
| `minutes` | `60` | The session's time limit. It is skipped when the deadline is nearer. |
| `setup` | `""` | A shell command run first on current `main` (a build), with no input. A failure skips this round and is logged; so does a setup still running at `setup_minutes`, when the session would no longer fit before the deadline, or at any `windvane stop`. |
| `setup_minutes` | `60` | The longest `setup` may run. |
| `prompt` | required | The project's rules file for the session, a path in the repo. |
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

## [[append_only]]

An append-only numbered file (a decision log, ADRs, migrations). Two branches that each append an entry conflict, and
both took the next number. When a merge (a ticket's, or a desk PR's) conflicts only in such files, and both sides of
every conflict hunk only add whole entries, the driver resolves it without a session: main's entries first, then the
branch's, renumbered past main's where they collide, with the branch's references to them renumbered only on lines the
branch added (in any file). The merge commit, the log and the PR comment name the mapping (`D-5 → D-6`); the merged
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
| `min_free_gb` | `6` | `windvane heavy` waits until at least this much memory is available. Linux only; elsewhere only `busy` is waited on. |

## Quiet times

Not a TOML file: `~/.local/state/windvane/<name>/quiet.txt` lists times when nobody should be asked anything, one
per line:
```
daily 23:00-08:00
2026-10-02 13:00-15:00
```
In a quiet time workers build their recommended option for any product question and mark it provisional instead of
asking. `windvane quiet` shows the file and what it means right now; `windvane mode` prints only the latter.
