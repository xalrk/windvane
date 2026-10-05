# Troubleshooting and FAQ

Start with `windvane doctor` (what this machine lacks) and `windvane status` (what the run is doing, with the log's
tail). The full log is `~/.local/state/windvane/<name>/autopilot.log`, and each session's complete stream is in
`runs/` next to it; `windvane watch` shows them readably.

## Troubleshooting

**`windvane run` says there's nothing to do.**
The autopilot reads tickets from the remote's `main`, not from your checkout: commit and push `.windvane/`. Then
check `windvane queue`: a ticket waits while anything in its `Depends on:` line is unmerged, and `parked:` tickets
never start.

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
(`partials`), one run hit `ticket_hours` or its max turns, or its sessions kept dying. The work so far is on the ticket's branch. Usually the ticket is too big or too vague:
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
- "login refused; run `gh auth login`, then push": log in again; the next run or `windvane plan` pushes it.
- "refused by a hook or branch rule; fix the rule or push to another branch": pushing again fails the same way until
  the rule changes; `git push origin plan-...:some-other-name` by hand counts as pushed.
- "network down; the next run pushes it again": nothing to do.
- "push failed; see the windvane log": the log (`windvane status`) has git's own words.

Each run (and each `windvane plan`) tries such a branch once more at its start and pushes "held-back change pushed"
when it goes through; while it still fails, only the log says so. The local branch goes once the remote has it.

**A plan PR I closed holds ticket ids.**
It doesn't: a plan branch whose PRs were all closed frees its ids and is remembered in
`~/.local/state/windvane/<name>/closed_plans.json`, so it isn't looked up again. Its branch is kept. If you reopen
that PR, delete its line there, or a newer plan may reuse its ids.

**`gh` errors on a GitHub project.**
Run `gh auth status`. Workers open pull requests and the driver merges them with `gh`; it needs push and merge rights
on the repository. Projects not on GitHub use `forge = "git"`.

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
