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
5-hour and weekly usage limits is used so far.)

`windvane stats` shows where the tokens went:
```
role         runs turns  fresh in  cached in     out   API $   min
worker         24   791    1,349k    41,878k    597k   31.10   215
reviewer       24   119      711k     3,594k    126k    8.93    24
critic         29   144      443k     2,433k     77k    5.57    16
planner         1    11       74k       376k     16k    0.99     3
preflight       1     1       12k         9k      0k    0.05     0
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
