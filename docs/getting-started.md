# Getting started

This guide takes you from nothing to a first unattended run on one of your projects.

## Requirements

- **Linux.** macOS mostly works, but `windvane heavy` can't see free memory there and waits only on busy processes.
  Windows is untested.
- **Python 3.11 or newer** and **git**.
- **[Claude Code](https://docs.claude.com/en/docs/claude-code)**, installed and logged in (`claude` on your `PATH`).
- **Optional:** the [GitHub CLI](https://cli.github.com) `gh`, logged in, for projects on GitHub (pull requests);
  **tmux** for `windvane run --detach`; the [ntfy](https://ntfy.sh) app on your phone for pushes and answers.

windvane itself needs nothing beyond the Python standard library.

## Install

With [pipx](https://pipx.pypa.io) or [uv](https://docs.astral.sh/uv/):
```sh
pipx install git+https://github.com/xalrk/windvane
# or
uv tool install git+https://github.com/xalrk/windvane
```
Without either, from a clone:
```sh
git clone https://github.com/xalrk/windvane
sh windvane/install.sh     # a venv in ~/.local/share/windvane, the command in ~/.local/bin
```
Then:
```sh
windvane doctor     # what this machine lacks, with the command that fixes each
windvane install    # optional: the windvane skill and a mention hook for Claude Code, asking before each change
```
`windvane install` lets any Claude Code session pick up windvane when you mention it ("let's build this with
windvane"): the session can then create the repository, set windvane up, write the first tickets and start the run
for you. Everything below can also be done by hand.

### Let your Claude set it up

Point a Claude Code session at this repository and say "install windvane and set it up". It has no terminal to
confirm in, so it should run `windvane install --yes`, then `windvane doctor`. While the repository is private, the
install needs access to it first: `gh auth login` (and collaborator access), then `gh auth setup-git`.

## Set up a project

In the project's checkout (it needs a remote to push to; GitHub, or any git server with forge `git`):
```sh
cd ~/code/myproject
windvane init --check "pytest -q"
```
`init` writes `.windvane/`:

| File | What to do with it |
|---|---|
| `config.toml` | Check the `check` command: it must run the project's tests and exit non-zero on failure. `init` sets `forge = "github"` only for a GitHub remote with `gh` installed, and `git` otherwise (`--forge` to choose). See the [configuration reference](configuration.md). |
| `CRITIC.md` | Describe what a 10/10 means for this project: who uses it, what to beat, and 2-4 dimensions with what a 2, a 5 and an 8 look like. See [the critic](concepts.md#the-critic-and-calibration). |
| `QUEUE.md` | The order to work tickets in. Empty is fine: tickets are worked by number. |
| `QUESTIONS.md` | Where design questions for you collect. Leave it as it is. |

## Write a first ticket

```sh
windvane new "Retry failed uploads with backoff"
$EDITOR .windvane/tickets/T-001.md
```
Fill in the goal, the scope, a `Touches:` line with the files it will change, and "Done when" points that a test can
prove. Small, concrete tickets work best: a few hours of work for a person at most. See [Tickets](concepts.md#tickets).

Or let a planner session write several from a goal:
```sh
windvane plan "make uploads robust on flaky connections"
```
It opens a pull request with the new tickets; merge it once you're happy with them.

The autopilot reads tickets from the remote, so commit and push:
```sh
git add .windvane && git commit -m "windvane setup and first tickets" && git push
```

## Run it

```sh
windvane run --for 2h --detach     # or: --until 18:00
```
Started in a terminal, the run first asks any product questions the next tickets will predictably need
(preflight), so you can answer them before you leave. `--detach` then moves it into a tmux session.

While it runs:
```sh
windvane watch      # follow every session live: what it says, the commands it runs and their output
windvane status     # running or not, the slots, questions waiting for you, the log's tail
windvane queue      # done, in progress, open
```
None of these cost tokens. Your checkout stays untouched: the sessions work in the autopilot's own worktrees, so you
can keep working at the desk.

Each ticket that makes it through the critic, the check and the review is merged into `main` by the driver; one that
can't be finished is left on its branch with the reason in the log. No new ticket starts in the last 30 minutes
(`min_slot`); at the deadline any session still running is stopped (its branch keeps the work for the next run), and
a summary is printed and pushed.

```sh
windvane stop             # stop after the current ticket
windvane deadline +1h     # or move the deadline
windvane stats            # what it cost, per role
```

## Next steps

- [Phone setup](phone.md): pushes for merges and questions, answered with a tap.
- [Quiet times](configuration.md#quiet-times): hours when nobody is asked anything.
- [Parallel tickets](concepts.md#touches-and-parallel-slots): several tickets at once.
- [Troubleshooting](troubleshooting.md) if something doesn't work.
