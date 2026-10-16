# Troubleshooting and FAQ

Start with `windvane doctor` (what this machine lacks) and `windvane status` (what the run is doing, with the log's
tail). The full log is `~/.local/state/windvane/<name>/autopilot.log`, and each session's complete stream is in
`runs/` next to it; `windvane watch` shows them readably.

## Troubleshooting

**`windvane run` says there's nothing to do.**
The autopilot reads tickets from the remote's `main`, not from your checkout: commit and push `.windvane/`. Then
check `windvane queue`: a ticket waits while anything in its `Depends on:` line is unmerged, and `parked:` tickets
never start.

**`windvane run` refuses to start.**
`no ticket starts in a run's last 30 minutes (min_slot), so this run would do nothing`: give the run longer than
`min_slot` (`--for 2h`), or lower it (`--min-slot 0`). Workers need more still: one starts only while its wrap-up,
20 minutes before it's stopped, is at least 5 minutes ahead, so `windvane run` refuses a run of 25 minutes or less
(`a worker starts only with its wrap-up ...`) or a `ticket_hours` under 25 minutes, and a run ends with `no time left
for a worker` once that is all that's left. `` windvane run needs Claude Code (`claude`) on PATH ``: install
and log in to Claude Code; `windvane doctor` gives the command. After installing windvane itself, `windvane: command
not found` means `~/.local/bin` isn't on your `PATH`: run `export PATH="$HOME/.local/bin:$PATH"` and add that line
to your shell's startup file.

**`new windvane code not taken up: ... is gone`, or `windvane couldn't start its driver`.**
windvane was reinstalled or upgraded under a running autopilot, or a Python upgrade removed the interpreter it was
installed with. Reinstall it: `pipx reinstall windvane`, or `sh install.sh` in the clone you installed from. A run that
logged the first line goes on with its old code and restarts on the new one at its next update check; after the
second, start the run again. `windvane doctor` warns while a running driver's interpreter or install is gone.

**A ticket never starts beside the others (`T-024 waits: no Touches line, so it runs alone`).**
A ticket without a `Touches:` line can't be proven independent of the running ones, so it waits for them. Add the
line. Other `waits:` lines name the overlapping ticket and file.

**The run stops at once with a setup error.**
The `setup` command failed in a fresh worktree; its output is in the log. Try it by hand in a new clone of the
project.

**The check fails in the autopilot but passes on my machine.**
The check runs in a fresh worktree, without untracked files, local `.env` files or build output from your checkout.
Make it work from a clean clone, or prepare what it needs in `setup`.

**A ticket was set aside.**
The push and the log say why: reviews kept asking for changes (`review_rounds`), runs kept running out of time
(`partials`), one run hit `ticket_hours`, its max turns (set by the Claude CLI or the project's `.claude/` settings, not windvane) or its dollar budget, or its sessions kept dying. The work so far is on the ticket's branch. Usually the ticket is too big or too vague:
split it or sharpen "Done when", then run again; the next worker resumes the branch.

**The run paused (`paused ... Resuming at 14:00`).**
Claude's 5-hour usage reached `cap`. It resumes by itself when the window resets. Raise `cap` (or `run --cap`) to
let it spend more of your window.

**A question is waiting and nothing happens.**
`windvane status` lists questions waiting for you. Answer with `windvane answer T-007 B`. Other tickets keep running
meanwhile; without an answer the recommendation is built after `answer_wait` minutes.

**Sessions keep dying with an API or network error.**
Each is rerun after a growing pause (`retries`, `retry_minutes`) without failing its ticket. If only one ticket's
sessions keep dying (say "prompt is too long"), that ticket is set aside and the run goes on; if they keep dying
across tickets, the run stops and the log shows the error. Check that `claude` works by hand (`claude -p "hi"`).

**The network went down.**
The run waits it out: when a session dies or a git or `gh` call fails, it checks that the API and the git remote
answer, and if not it logs "network down since HH:MM" and looks again after 1, 2, 4, then every 5 minutes. When it's
back the session is resumed and the call tried again; no retry is spent, no ticket is failed, and failures meanwhile
don't count toward "two failed tickets in a row". An outage over 10 minutes is pushed once it ends. The deadline and
`windvane stop` still end the wait. Behind an HTTPS proxy, the proxy is what it checks for the API.

**A run was killed (reboot, `stop --now`, closed terminal).**
Start it again. Unfinished tickets are resumed from their branches and worktrees; nothing pushed is lost.

**"plan not pushed" (or "not merged") with a local branch.**
A plan, periodic change or desk answer whose push was refused waits on a local branch (`plan-...`), and the push says
why and what to do:
- "refused: it holds a secret in `README.md:4` (GitHub push protection); remove it from the branch's commit
  (`git commit --amend` on `plan-...`), then the next run pushes it; or allow it on GitHub if it isn't a secret:
  https://github.com/.../unblock-secret/...": GitHub's push protection found a token or key in that file and line.
  Pushing it by hand under another name fails the same way. In your checkout, `git switch` to that branch, take the
  secret out, `git commit --amend -a`, and `git switch -` back; the next run pushes the cleaned commit. A false
  positive (a test fixture, an example key): open the URL, allow it, and the next run pushes the branch as it is.
- "login refused; run `gh auth login` and the next run pushes it" (off GitHub: "check your login to the remote"):
  once you're logged in, the next run or `windvane plan` pushes it.
- "refused by a hook or branch rule; change the rule, or push it by hand with
  `git push origin plan-20261005-040000:plan-20261005-040000-b` and open a PR from it" (off GitHub: "and merge it into
  main"): the command names your branch and remote; run it in your checkout. Pushed under another name counts as
  pushed, so windvane stops pushing it: landing it (the PR, or the merge) is then yours. A change meant to land on
  main (a periodic report, a plan run with `--merge`) stays listed as "pushed to origin/plan-...-b, not merged: ...:
  open a PR from it and merge it" (off GitHub: "merge it into main") until main has it (or, on GitHub, its PR is
  closed); a plain plan goes from the list at once. Any merge counts, a squash or rebase merge too: once main holds
  the change, it drops off by itself, and `windvane status` fetches first while such a change is listed, so it goes
  as soon as the merge is on the remote. Without either, pushing
  again fails the same way until the rule changes.
- "network down; the next run pushes it again": nothing to do.
- "push failed; see the windvane log": the log (`windvane status`) has git's own words.

Each run (and each `windvane plan`) tries such a branch once more at its start, and one push ("1 held-back change
pushed") says where those that went through are now; one that still needs you (not merged, no PR) comes first, under
"... pushed, not merged". While it still fails, `windvane status` and `overview` list it with the newest reason
and how long it has waited ("held back for 3 days, not pushed: ..."), and the phone's `status` counts them with the
oldest one's age ("2 held-back changes not pushed, oldest 3 days"; "pushed, not merged" for those you pushed by
hand), in place of the log's last line. The local branch goes once the remote has it.
Only branches windvane kept this way are pushed again: not your own `plan-...` copies, nor one you pushed by hand.

**A plan PR I closed holds ticket ids.**
It doesn't: a plan branch whose PRs were all closed frees its ids and is remembered in
`~/.local/state/windvane/<name>/closed_plans.json`, so it isn't looked up again. Its branch is kept. Reopen that PR
and the next plan notices (one list of open PRs) and keeps its ids.
A request accepted into a ticket held on that plan goes back to the request inbox, with a `## Reopened` line,
so the next triage looks at it again.

**`gh` errors on a GitHub project.**
Run `gh auth status`. Workers open pull requests and the driver merges them with `gh`; it needs push and merge rights
on the repository. Projects not on GitHub use `forge = "git"`.

**The log says "actions: recount failed", or the phone's `status` says "Actions ... (stale: counted Oct 3, workers
skip CI)".**
With `actions_minutes` set, windvane recounts the month's Actions minutes through `gh` at each run's start and each
session's end. When a `gh` call fails, the log says so, naming the call, gh's own first line and the fix:
"actions: recount failed: `gh api repos/me/proj/actions/runs`: API rate limit exceeded (windvane waits for the limit
to reset)". A network error adds "(check the connection)", an HTTP 5xx "(GitHub's own error: the next recount tries
again)", a login problem "(run `gh auth status`)"; when gh's own line already names a command ("please run: gh auth
login"), nothing is added. Each cause is logged once a run: again
only when the cause changes or comes back after a recount worked. The next recount that works fixes the count.
Once the last good count is over a day old, the phone's line marks it stale and workers' commits carry `[skip ci]`
until a recount succeeds; the log says so once a run, with the same fix as the recount's line. A recount that waited on another project's driver counting isn't a failure and isn't
logged.

**The log says "gh guard: gh's alias list couldn't be read".**
Workers' `gh` goes through a guard that refuses workflow starts, including through your gh aliases. When
`gh alias list` fails or hangs, the guard can't tell an alias from a typo, so it refuses any first word that isn't a
gh command or one of your installed extensions (`gh dash`); gh's own commands still work. Run `gh alias list` to see
why it fails (often a broken `~/.config/gh/config.yml`). The log says this once a run.

**Empty `.env`, `package.json` or lock files appear in a worktree after a session.**
Claude Code makes these zero-byte stubs (with empty `.claude/commands`, `.claude/agents` and `node_modules/.bin`)
when `CLAUDE_CODE_SUBPROCESS_ENV_SCRUB` is set, and in a git worktree it can't hide them from `git status`. windvane
never sets it, and drops it from the sessions' environment even when your shell has it. Stubs older sessions left
are removed from windvane's own worktrees when a slot starts a ticket and when it ends one: only untracked, empty
files on that list, at the worktree's root. Any in your own checkout are yours to delete.

**A push says "Temp dir nearly full".**
windvane has already removed its own leftovers (and the `tmpXXXXXXXX` dirs older tests leaked, once a day old); see
what's left with `du --inodes -s /tmp/* | sort -n | tail`, and remove what you don't need before it stops every command.

**The disk is full (or nearly).**
windvane's state directory (`~/.local/state/windvane/`) holds each project's worktrees with their build output, every
session's transcript and the critic's screenshots. `windvane doctor --clean --dry-run` lists what of it can go, with
sizes, for every project; `windvane doctor --clean` removes it (a project whose autopilot is running is skipped: stop
it first, or let its run clean at its end). Work is never removed: a worktree with uncommitted changes or commits
`main` lacks stays. To free more, lower `keep_days` or turn on `trim_build` in [`[state]`](configuration.md#state).

**No pushes arrive on the phone.**
`windvane doctor` checks the phone. Make sure the app is subscribed to the topic `windvane phone setup` showed (and
to the right server, if you use your own). `windvane phone setup` again sends a test push.

**The critic gives everything the same score.**
`windvane stats --critic` shows the spread and each check's yes rate. Sharpen a check every ticket passes, lower one
none ever does (or, on an old numeric scale, rewrite `.windvane/CRITIC.md` as ladders of yes/no checks, like
`templates/CRITIC.md`), and record your own verdicts in `CALIBRATION.md`. See
[calibration](concepts.md#the-critic-and-calibration).

## FAQ

**Can I keep working in my checkout during a run?**
Yes. With `isolate = true` (the default) the sessions work in the autopilot's own worktrees and never touch your
checkout. Push your own changes on a branch and merge it as usual; the autopilot merges `main` into each ticket before
it merges it.

**Does it push to `main` directly?**
Only the driver merges, and only after the critic, the check on the merged tree, and the review. Sessions are denied
every form of pushing to `main`. See the [safety model](safety.md).

**Does it work without GitHub?**
Yes: `forge = "git"` works with any git remote. There are no pull requests then; the driver merges ticket branches
and pushes `main` itself.

**Which languages and frameworks does it support?**
Any. windvane only needs a git repository and a `check` command that exits non-zero on failure.

**Does it need a Claude subscription, or an API key?**
It runs the `claude` command, so it uses whatever Claude Code is logged in with. On a subscription, `cap` and
`week_cap` keep it from using up your limits; `stats` shows an API-price equivalent as a relative meter.

**How much does a ticket cost?**
`windvane stats --by ticket` shows tokens per ticket and the average per merged ticket. Most of it is the worker; see
[Token efficiency](tokens.md) for what drives it and how to tune it.

**I disagree with a critic score. What do I do?**
Add a line to `.windvane/CALIBRATION.md`: the date, the ticket, the critic's score, yours, and why. Every critic
reads it.

**How do I undo something it merged?**
Like any merge: `git revert` it on a branch. Tickets marked done in `QUEUE.md` or merged stay done; write a new
ticket for a different approach.

**How do I remove windvane from a project?**
Delete `.windvane/` from the repository and `~/.local/state/windvane/<name>/` from the machine (then `git worktree prune` in the checkout). `windvane uninstall`
removes the skill and hook from Claude Code.
