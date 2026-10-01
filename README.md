# windvane

An autopilot for Claude Code on any git project. You write tickets (or have it plan them from a goal); it works
them one at a time while you're away. Each ticket gets a fresh headless session that builds it, then a harsh critic,
the project's check, a fresh read-only reviewer, and a merge. Questions reach your phone. A token ledger shows what
everything cost.

## Quick start
The easy way: tell any Claude Code session "here's my idea, let's build it with windvane". After `install`, a
mention of windvane or autopilot brings in the playbook (`skills/windvane/SKILL.md`): the session makes the private
repo, sets windvane up, writes the first tickets and starts the run, asking you only real product questions.

By hand:
```sh
git clone https://github.com/xalrk/windvane.git    # anywhere you like
windvane/bin/windvane install              # windvane on PATH, the skill, and the mention hook

cd ~/code/myproject
windvane init --check "pytest -q"                  # writes .windvane/ (config, queue, questions, critic scale)
$EDITOR .windvane/CRITIC.md                         # what a 10/10 means for this project
windvane new "Retry failed uploads with backoff"    # or: windvane plan "make uploads robust"
git add .windvane && git commit -m "windvane" && git push

windvane run --for 6h --detach                      # in tmux; `windvane watch` to follow it live
```

## Commands
| | |
|---|---|
| `run --until 18:00 \| --for 6h [--detach] [--replan N]` | work the queue until then; `--replan` plans more tickets when it runs dry |
| `status`, `queue`, `overview`, `watch` | what it's doing, the queue, where the project stands, a live view (no tokens) |
| `stop` | stop after the current ticket |
| `plan [goal] [--merge]` | a planner session turns the goal and the critic's follow-up notes into tickets |
| `new <title>` | a ticket file with the next id |
| `answer T-007 B` / `answer T-007 "free text"` | answer a ticket's question at the desk |
| `stats [--by ticket]` | tokens, turns and minutes per role or ticket |
| `quiet`, `mode` | quiet times (`~/.local/state/windvane/<name>/quiet.txt`) and what they mean right now |
| `heavy -- <cmd>` | run a machine-heavy command (engine, render, big build) one at a time on this machine |
| `check`, `critic T-007`, `bg [--heavy] <name> <cmd>`, `await <name>` | used by the sessions themselves |

## In the project (in git)
- `.windvane/config.toml`: the check command, forge (`github` PRs or plain `git`), models and effort per role, the
  critic's bar, usage caps. Every key is optional; see `templates/config.toml`.
- `.windvane/tickets/T-001.md`: `# T-001: title`, `Depends on: T-000.`, then Goal / Scope / Done when.
- `.windvane/QUEUE.md`: the order (`- T-004`), plus `- done: T-002 (why)` and `- parked: T-003 (why)`.
- `.windvane/QUESTIONS.md`: Open / Provisional / Answered design questions.
- `.windvane/CRITIC.md` (the scale) and `CALIBRATION.md` (your verdicts where you disagreed with the critic).

Status is derived from git and never maintained by hand: a ticket is done when its branch (`t-001-...`) is merged
into main, and in progress while such a branch exists unmerged.

## On this machine
`~/.local/state/windvane/<name>/`: `autopilot.log`, each session's stream (`runs/`), `ledger.jsonl`,
`critic.jsonl`, check passes, background jobs, quiet times, and the worktrees (`work/` for the sessions, `ci/`
for the driver's checks and merges, `plan/` for the planner, which gets its own even with `isolate = false`).

## The phone (ntfy)
With the user's `notify`, `notify-ask` and `ntfy-listen` set up, pushes go out for each merge, failure, pause,
question and the final summary. `windvane init` adds `status`, `stop` and `overview` hooks to
`~/.config/ntfy/*.d/`. A question shows buttons A and B and "Answer on computer". With no answer in `answer_wait`
minutes the run builds its recommendation and marks it provisional; in quiet times nothing is asked. Questions
marked `Waits for the user` (money, licences, undoing merged work) always wait. Without ntfy it all still works:
questions wait for `windvane answer`.

## Design
What a ticket goes through:
1. **Worker** (fresh session, the ticket and any feedback already in its prompt): branch, build with tests, push
   often, evidence pack, `windvane critic`, `windvane check`, PR. Ends with a `RESULT:` line.
2. **Critic** (fresh, read-only, run by the worker): scores the outcome on the project's scale and splits its points
   into "Fix in this ticket" (up to 3 revisions) and "Beyond this ticket" (they feed `plan`).
3. **Check**: the driver runs the project's check on the exact pushed head. A clean pass is recorded per git tree, so
   the worker's own green check is reused and nothing runs twice.
4. **Reviewer** (fresh, read-only, the diff inlined in its prompt): approves or lists changes.
5. **Merge**: the driver merges main into the head in its own worktree, checks the merged tree when main moved, then
   merges the PR (or pushes, for forge `git`). A conflict or a red merged tree goes back to the worker.

Anything sent back reaches the ticket's next worker run as written feedback; tickets that keep bouncing are set aside.

**Parallel tickets** (`parallel = 2`, `--parallel N`): tickets run side by side, each in its own pair of
worktrees, when their `Touches:` lines don't overlap. A ticket without the line runs alone. Every ticket still gets
exactly one fresh session, so this cuts wall-clock time without costing tokens. Merges stay one at a time, each
checked on the tree main will really have.

**Heavy work** (`windvane heavy -- <cmd>`, and `heavy_check = true` for an engine or render check): one machine-heavy
job at a time across every slot and project. It also waits while anything named in `~/.config/windvane/machine.toml`'s
`busy` list (Unreal, Blender by default) is running or free RAM is under `min_free_gb`. It never takes other tools'
locks, so a heavy command that is itself a locking tool can't deadlock.
A session that dies outside its ticket (API error, network drop, crash) is rerun after a growing pause
(`retries`, `retry_minutes`), without failing the ticket; sessions that keep dying stop the run with the error.
A worker that ends without its `RESULT:` line gets one more run to finish.

Design choices, and why:
- **Your checkout is never touched.** Sessions work in the autopilot's own worktree, so you can keep working at the
  desk while it runs. (`isolate = false` brings back in-checkout work for projects whose builds can't be duplicated.)
- **The driver merges, not a Claude session.** The reviewer only reads, has no merge or push rights, and doesn't sit
  through CI. The driver checks the *merged* tree, which a rebase-before-review flow can't promise.
- **Fewer tool calls per run.** The ticket, feedback, PR body, critic verdict and diff go into the prompts, so
  sessions don't spend turns (each of which re-reads the whole context) fetching them.
- **A token ledger** per run, role and ticket, to see where the tokens go and tune models and effort per role.
- **Planning** from a goal plus the critic's follow-ups, optionally whenever the queue runs dry.
- **Generic phone questions** through `notify-ask`, with no question PRs or branches to clean up.

## Development
`python3 -m unittest discover -s tests` runs everything, including the whole driver against a fake `claude`, in a few
seconds. Python 3.11+, standard library only.
