# windvane

An autopilot for Claude Code on any git project. You write tickets (or have it plan them from a goal); it works
them one at a time while you're away. Each ticket gets a fresh headless session that builds it, then a harsh critic,
the project's check, a fresh read-only reviewer, and a merge. Questions reach your phone. A token ledger shows what
everything cost.

## Quick start
The easy way: tell any Claude Code session "here's my idea, let's build it with windvane". After `install`, a
mention of windvane or autopilot brings in the playbook (`skills/windvane/SKILL.md`): the session makes the private
repo, sets windvane up, writes the first tickets and starts the run, asking you only real product questions.

Install (Linux; Python 3.11+, git and Claude Code; `gh` for GitHub projects, tmux for `run --detach`):
```sh
pipx install git+https://github.com/xalrk/windvane   # or: uv tool install git+https://...
windvane doctor                                     # what this machine lacks, and the command that fixes each
windvane install                                    # the skill and the mention hook into Claude Code, asking first
```
Without pipx or uv: clone the repo and run `sh install.sh` (a venv in `~/.local/share/windvane`, the command in
`~/.local/bin`). For working on windvane itself, run `bin/windvane` from a clone; `bin/windvane install` also links it
onto PATH. `windvane uninstall` takes back exactly what `install` added (it keeps a list in
`~/.local/state/windvane/install.json`), then `pipx uninstall windvane` (install.sh: delete
`~/.local/share/windvane` and `~/.local/bin/windvane`). Other platforms: macOS mostly works, but `windvane heavy`
can't see free memory there (no `/proc/meminfo`) and waits only on busy processes; Windows is untested.

Then, in a project:
```sh
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
| `preflight [--for 4h] [--json]` | the product questions the next tickets will need, asked now (`run` in a terminal does it first; `--no-preflight` skips) |
| `status`, `queue`, `overview`, `watch` | what it's doing, the queue, where the project stands, a live view (no tokens) |
| `stop [--now]` | stop after the current ticket; `--now` kills every session of the run (workers, reviewers, the critics and `bg` jobs they started) and leaves the worktrees for the next run to resume |
| `deadline 14:00 \| "2026-10-02 09:30" \| +90m` | move a running autopilot's deadline: new tickets keep to it at once, running sessions are stopped by it |
| `plan [goal] [--merge]` | a planner session turns the goal and the critic's follow-up notes into tickets |
| `new <title>` | a ticket file with the next id |
| `answer T-007 B` / `answer T-007.1 "free text"` | answer a ticket's (or a preflight) question at the desk |
| `stats [--by ticket]` | tokens, turns and minutes per role or ticket |
| `stats --critic [--last N]` | whether the critic's calibration spreads its scores: first and final scores, revisions, scores on the pass mark, a collapsed scale |
| `quiet`, `mode` | quiet times (`~/.local/state/windvane/<name>/quiet.txt`) and what they mean right now |
| `heavy -- <cmd>` | run a machine-heavy command (engine, render, big build) one at a time on this machine |
| `phone setup [--server URL] [--no-service]`, `phone off`, `phone rotate`, `phone listen` | pushes and questions on your phone through ntfy (see below) |
| `doctor` | what this machine lacks for windvane (claude logged in, gh, tmux, the phone), with the fix for each |
| `install [--yes]`, `uninstall [--yes]` | the skill and the mention hook into Claude Code, asking before each change; and out again |
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
Config in `~/.config/windvane/` (`machine.toml`: `projects_dir` for new projects, `heavy`'s settings), state in
`~/.local/state/windvane/` (the XDG variables move both). Per project, `~/.local/state/windvane/<name>/`: `autopilot.log`, each session's stream (`runs/`), `ledger.jsonl`,
`critic.jsonl`, check passes, background jobs, quiet times, and the worktrees (`work/` for the sessions, `ci/`
for the driver's checks and merges, `plan/` for the planner, which gets its own even with `isolate = false`).

## The phone (ntfy)
Off by default: without it everything works, and questions wait for `windvane answer`. `windvane phone setup`
turns it on: two fully random topics on ntfy.sh (`--server` for your own), one for pushes and one for the phone's
replies, kept in `~/.config/windvane/phone.toml` (mode 600). It sends a test push, shows the topic to subscribe to in
the ntfy app (a QR code when `qrencode` is installed), and installs `windvane phone listen` as a systemd user
service (`--no-service` skips it; a run reads the replies itself anyway). `windvane phone off` turns it off;
`windvane phone rotate` makes new topics.

Pushes go out for each merge, failure, pause, question and the final summary, censored first (emails, tokens, keys,
passwords, your user and git names, the topics, the `redact` words in `phone.toml`; if the censor fails nothing is
sent). A question shows buttons A and B and "Answer on computer"; C to E are typed as a letter. The phone can also
send `status`, `overview`, `usage`, `stop` (after the current ticket) and `help`. Nothing else it sends does
anything: it is never instructions. With no answer in `answer_wait` minutes the run builds its recommendation and
marks it provisional; in quiet times nothing is asked. Questions marked `Waits for the user` (money, licences,
undoing merged work) always wait.

Your own `notify`, `notify-ask` and `ntfy-listen` scripts, when on PATH, are used instead; `windvane init` then
adds `status`, `stop` and `overview` hooks to `~/.config/ntfy/*.d/`.

## Design
What a ticket goes through:
1. **Worker** (fresh session, the ticket and any feedback already in its prompt): branch, build with tests, push
   often, evidence pack, `windvane critic`, `windvane check`, PR. Ends with a `RESULT:` line.
2. **Critic** (fresh, read-only, run by the worker): scores the outcome on the project's scale and splits its points
   into "Fix in this ticket" (up to 3 revisions) and "Beyond this ticket" (they feed `plan`).
   Once 5 tickets are scored on the current scale, the driver watches it: when half the recent first scores share one
   value, or the fix count alone predicts the score, the scale has collapsed. It logs and pushes that once a run, and
   `overview` and `stats --critic` show it until the scale is recalibrated.
3. **Check**: the driver runs the project's check on the exact pushed head. A clean pass is recorded per git tree, so
   the worker's own green check is reused and nothing runs twice.
4. **Reviewer** (fresh, read-only, the diff inlined in its prompt): approves or lists changes.
5. **Merge**: the driver merges main into the head in its own worktree, checks the merged tree when main moved, then
   merges the PR (or pushes, for forge `git`). A conflict or a red merged tree goes back to the worker.

Anything sent back reaches the ticket's next worker run as written feedback; tickets that keep bouncing are set aside.

**Parallel tickets** (`parallel = "auto"`, the default, up to `max_parallel = 4`; a number such as `parallel = 2`
or `--parallel 2` caps it): another ticket starts whenever one is ready whose `Touches:` line doesn't overlap the
running ones' and the usage window has room for one more (the 5-hour usage now plus the recent cost per ticket times
the tickets running, under `cap`). Each slot gets its own pair of worktrees when first used. A ticket without the
line runs alone. Related work waits too, since a worker beside it would read code about to change and bounce on a
conflict: a ticket naming a running ticket's id or a file in its `Touches:`, or tied to it by a planner's `Related:`
line. A held ticket keeps its place; the next one down that passes starts instead. A slot opens only when it pays:
when the tickets left, at recent tickets' minutes each, wouldn't finish one at a time before the deadline. Every 2
merge conflicts between side-by-side tickets drop a slot for the rest of the run. The log says why a slot sits idle
(`T-010 waits: overlaps T-008 on windvane/driver.py`, `T-013 waits: names windvane/critic.py, which T-010 changes`,
`no 2nd slot: 3 tickets x 9 min fit before 18:45 one at a time`), the run's summary counts conflict bounces, and
`windvane status` lists the running tickets and their slots. Merges stay one at a time, each checked on the tree
main will really have.

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
- **windvane keeps itself current.** Between tickets the driver fast-forwards the windvane checkout it runs from, but
  only a clean one on `main` (any other is left alone, with one log line), and restarts itself with the same deadline
  when its code or prompts changed: what it merges into windvane is used from the next ticket on. This is the one
  exception to the rule above, when windvane works on itself. A pipx/uv install is never changed; the log and the final
  summary say when a newer release is out.
- **The driver merges, not a Claude session.** The reviewer only reads, has no merge or push rights, and doesn't sit
  through CI. The driver checks the *merged* tree, which a rebase-before-review flow can't promise.
- **Fewer tool calls per run.** The ticket, feedback, PR body, critic verdict and diff go into the prompts, so
  sessions don't spend turns (each of which re-reads the whole context) fetching them.
- **A token ledger** per run, role and ticket, to see where the tokens go and tune models and effort per role.
- **Planning** from a goal plus the critic's follow-ups, optionally whenever the queue runs dry.
- **Generic phone questions** through ntfy, with no question PRs or branches to clean up.
- **Preflight**: a light session reads the tickets likely to run before the deadline and lists the product calls they
  will need, asked at the terminal (or by a desk Claude session) before the user leaves. Answers go under
  `## Answered` on main and into that ticket's worker prompt; the phone is left for what nobody could predict.

## Development
`python3 -m unittest discover -s tests` runs everything, including the whole driver against a fake `claude`, in a few
seconds. Python 3.11+, standard library only, including the build: `tools/build_backend.py` is the PEP 517 backend
`pyproject.toml` names, so installing needs nothing downloaded.
