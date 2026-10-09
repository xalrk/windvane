<p align="center">
  <img src="docs/assets/banner.png" alt="windvane: tickets in, merges out. An autopilot for Claude Code." width="100%">
</p>

<p align="center">
  <a href="LICENSE"><img alt="license: MIT" src="https://img.shields.io/badge/license-MIT-f5b942"></a>
  <img alt="python: 3.11+" src="https://img.shields.io/badge/python-3.11%2B-3776ab">
  <img alt="dependencies: none" src="https://img.shields.io/badge/dependencies-none-2ea44f">
  <img alt="runs on: Linux, macOS" src="https://img.shields.io/badge/runs%20on-Linux%20%7C%20macOS-5b6b9a">
</p>

# windvane

**An autopilot for [Claude Code](https://docs.claude.com/en/docs/claude-code).** You write tickets for a git project
(or have windvane plan them from a goal); it works through them unattended until a deadline. Each ticket gets a fresh
Claude Code session that builds it with tests, a critic that scores the result, your project's check, a read-only
code review, and a merge that only happens when all of them pass. Product questions reach your phone; a token ledger
shows what everything cost.

It is for developers who already use Claude Code and want it to keep shipping reviewed, tested work overnight or
while they are busy elsewhere, without touching their checkout and without merging anything unchecked.

- **Never touches your checkout.** Sessions work in the autopilot's own git worktrees; keep working at the desk.
- **Nothing merges unchecked.** The driver, not a model, merges, after the critic, the check on the merged tree and
  the review.
- **Asks, doesn't guess.** Product calls become lettered questions for you, answered with a tap on your phone.
- **Measured.** Tokens, turns and minutes per role and per ticket, and a check on whether the critic's scale works.
- **Small.** Python 3.11+, standard library only, MIT licensed.

## What a run looks like

`windvane watch` follows every session live (it costs no tokens). An excerpt from a real run, lines shortened:
```
══ 20261002-004600-T-015.jsonl
› Pushed the fix; refreshing the critic's evidence pack and asking the critic.
  Bash: git status --short | head -3; windvane check 2>&1 | tail -4
    CHECK PASSED in 2.7 min
› I fixed the bug the reviewer found: tapping Usage or Status on the phone during a usage pause no longer ends…
■ done: 26 turns, 9 min
[driver] 2026-10-02 00:55 T-015: critic 7/10 (works=7 clear=7 lean=8) (scope): built-in ntfy phone is solid…
[driver] 2026-10-02 00:55 T-015: check on ade6446: CHECK PASSED (reused the pass from 00:54 for this tree)
[driver] 2026-10-02 00:55 T-015: review run
[driver] 2026-10-02 00:57 T-015: review approve: meets every Done-when point with tests against a fake server
[driver] 2026-10-02 00:57 T-015: merged ade6446 · 5h 5% · week 9%
```

Your phone gets a push for each merge, failure and question:
```
Done: T-015 Phone notifications and answers that work for anyone
Built, checked, reviewed and merged. Critic 7/10. 5h 5% · week 9%
```
(`Critic 7/10` is the critic's score for the ticket, its lowest dimension; `5h 5% · week 9%` is how much of Claude's
5-hour and weekly usage limits is used so far.) Send it `status` for the autopilots that are on,
`overview` for every project.

`windvane stats` shows where the tokens went:
```
role         runs died/killed turns  fresh in  cached in     out  weighted   API $   min
worker         65           1  2204    3,741k   116,066k  1,595k   24,258k   85.03   911
reviewer       63           0   342    1,895k     9,728k    349k    5,087k   24.09    78
critic         79           0   366    1,125k     5,394k    197k    2,929k   14.01    43
planner         3           0    55      193k     1,903k     49k      674k    2.90     9
preflight       2           0     2       26k        18k      1k       38k    0.11     0
```

## Install

Requires Linux (macOS mostly works), Python 3.11+, git, and Claude Code logged in. Optional: `gh` for GitHub
projects, tmux for detached runs, the ntfy app for your phone.

```sh
pipx install git+https://github.com/xalrk/windvane            # or: uv tool install git+https://...
windvane doctor                                               # what this machine lacks, and how to fix each
```
Without pipx or uv, clone the repository and run `sh install.sh`. Details in [Getting started](docs/getting-started.md#install).

## Quick start

In a project with a remote:
```sh
cd ~/code/myproject
windvane init --check "pytest -q"                 # writes .windvane/ (config, queue, questions, critic scale)
$EDITOR .windvane/CRITIC.md                        # what a 10/10 means for this project
windvane new "Retry failed uploads with backoff"   # writes .windvane/tickets/T-001.md; fill it in
git add .windvane && git commit -m "windvane" && git push

windvane run --for 2h --detach                     # work the tickets for two hours, in tmux
windvane run --from 12:00 --until 20:00 --detach   # wait in tmux until noon, then work until 20:00
windvane run --daily 12:00-20:00 --detach          # every day 12:00-20:00 until `windvane stop` (a reboot ends it)
windvane watch                                     # follow it live
```
Or ask Claude Code: after `windvane install` (`--yes` when Claude runs it), a session that hears "let's build this with windvane" sets the project
up, writes the first tickets and starts the run for you.

The [getting started guide](docs/getting-started.md) walks through each step.

## How it works

```
             ┌──────────────── feedback ─────────────────┐
             ▼                                           │
 ticket ─▶ worker ─▶ critic ─▶ check ─▶ reviewer ─▶ merge ─▶ main
          builds,    scores,   the       reads the    by the
          tests,     sends     project's diff,        driver, on
          pushes     fixes     tests     approves     the merged
                     back                or not       tree
```

1. **Worker**: a fresh headless Claude Code session with the ticket and any earlier feedback in its prompt. It
   branches, builds with tests, pushes often, and opens a pull request.
2. **Critic**: a fresh read-only session scores what users get from the change on your project's own scale, and
   sends fixes back (up to three revisions). Its ideas beyond the ticket feed the next plan.
3. **Check**: the driver runs your project's test command on the exact pushed head.
4. **Reviewer**: a fresh read-only session reads the diff against the ticket and approves or asks for changes.
5. **Merge**: the driver merges `main` into the ticket, re-checks the merged tree if `main` moved, and merges.

Anything sent back reaches the next worker run as written feedback. Independent tickets (their `Touches:` lines
don't overlap) run side by side when the usage window has room and it would finish sooner. The run pauses near
your usage limit and stops at its deadline.

Read more in [Concepts](docs/concepts.md).

### Periodic sessions

Some work isn't a ticket: every few merges, play the game, load-test the service or audit the UI on current `main`,
and turn what it finds into tickets. Your project keeps the rules (and its build) in its own repo; windvane runs
them between tickets, when no ticket is running:

```toml
[[periodic]]
name = "playtest"
every = 6                                 # merged tickets between runs
minutes = 90                              # skipped when the deadline is nearer
setup = "tools/build.sh"                  # failing skips this round
prompt = "tools/autopilot/PLAYTEST.md"    # plays a build, writes a report, names its path
then = "triage"

[[periodic]]
name = "triage"                           # no `every`: runs only after the playtest
minutes = 30
prompt = "tools/autopilot/TRIAGE.md"      # reads the report, writes tickets
```

Each session ends with `PERIODIC: <summary>`, which is logged and pushed to your phone (a round that doesn't finish,
a failed setup, a session cut off or without that line, gets one push saying why); a `then` session gets the
previous one's final text and the files it names, plus, like `windvane plan`, the queue and the critic's "Beyond
this ticket" notes no plan or triage has used yet. Each session reads its prompt file from current `main`. New
tickets under `.windvane/tickets/`, lines added at the end of an open ticket, and lines added to `QUEUE.md` land on
`main` with the last session's final reply in the commit; any other change is discarded.
`land` says where the new tickets go in the queue: `near` (default: right after the ticket in progress), `next` (the
top), `end`, `hold` (parked in `QUEUE.md`, listed in the run's summary for you) or `ask` (one phone question: near,
end or hold; no answer in `answer_wait` minutes: hold). A `then` entry without its own `land` takes the one before.

An audit needs no prompt of your own: `builtin = "project-critic"` (with `every = 10` and `then = "triage"`, say)
runs the critic on the whole project against your `CRITIC.md`, the same as `windvane critic --project` at the desk.

`windvane periodic playtest` runs an entry (and its `then` chain) now, from the desk; it refuses while a run is on.
A `score=<n>` at the end of the `PERIODIC:` line goes to the ledger: `windvane stats --periodic` shows each entry's
scores over time. Sessions show in `windvane stats` as `periodic:<name>`.

### Your own PRs from the desk

While a run is on, merging your own PRs by hand races its tickets. Label one `windvane merge` (forge `github`, your
PRs only) and the autopilot merges it between tickets: it tests the merge with `main`, runs your check on that merge
(on the PR's head itself when it already holds `main`), comments the result and merges that head only. A red, conflicting or refused PR gets a short fix session
(`prompts/FIX.md`: the smallest change, never a weaker test) pushed to its branch, up to `desk_fixes` times; then it
is left for you with one push. Each head is checked once; push a new one to have it tried again. Fix sessions show
in `windvane stats` as `fix`. Set `desk_label = ""` under `[autopilot]` to turn it off.

### Append-only numbered files

A decision log, ADRs or migrations: two tickets that each append an entry take the same next number and conflict.
Name the file and the driver resolves such a conflict itself, without another worker run: main's entries stay, the
branch's follow, renumbered where they collide, and the branch's own references to them follow (`D-5 → D-6`, named in
the merge commit, the log, the PR and the merged push). Any other conflict goes back to the worker as before.

```toml
[[append_only]]
file = "docs/DECISIONS.md"
entry = '^\| D-(\d+) \|'   # a line that starts an entry; group 1 is its number
ref = 'D-{n}'              # how other lines refer to entry n
```

## Documentation

- [Getting started](docs/getting-started.md): install, set up a project, the first run
- [Concepts](docs/concepts.md): tickets, the pipeline, parallel slots, the critic and calibration, questions and
  quiet times
- [Command reference](docs/commands.md)
- [Configuration reference](docs/configuration.md): every key in `.windvane/config.toml` and `machine.toml`
- [Phone setup](docs/phone.md): pushes and answers through ntfy
- [Safety model](docs/safety.md): worktrees, what sessions may and may not do, what only the driver does, the heavy
  lock
- [Token efficiency](docs/tokens.md): where tokens go, and `windvane stats`
- [Troubleshooting and FAQ](docs/troubleshooting.md)

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md): the test suite runs the whole driver against a fake `claude` in a sandbox,
with nothing but the standard library. Changes are listed in [CHANGELOG.md](CHANGELOG.md).

## License

[MIT](LICENSE)
