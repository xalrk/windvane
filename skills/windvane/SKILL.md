---
name: windvane
description: windvane (also "autopilot"), the user's autopilot that builds a project ticket by ticket in fresh headless Claude sessions while they're away. Use whenever the user mentions windvane or autopilot in any wording, e.g. "here's my idea, let's build it with windvane", "yo claude, put windvane on my idea", "put autopilot on this repo", "run autopilot overnight", "how's autopilot doing", "stop autopilot".
---

# windvane

The user's autopilot (command `windvane`). It works a repo's tickets
(`.windvane/tickets/`) one fresh session each: worker, then critic, check, reviewer, and a merge done by its driver.
It runs independent tickets side by side, asks the user design questions on their phone (ntfy), and keeps a token
ledger. `windvane <command> --help` explains each command and `windvane doctor` what this machine lacks; this is the
playbook.

## A new project from an idea
Whenever the user gives an idea (any wording, a line or a page) and wants windvane or autopilot to build it.
Do everything yourself. The user should only have to answer real product questions.
1. **Pin the idea down.** Restate it in a few lines. Ask only the questions that change what gets built (who it's
   for, the platform or stack when it matters, the one thing it must do well), in one AskUserQuestion with
   recommendations. Decide engineering questions yourself.
2. **Make the repo.** Put it in the user's projects folder: `projects_dir` in `~/.config/windvane/machine.toml`
   (if it isn't set, ask once and save their answer there), as `<projects_dir>/<short-name>`. Then `git init -b main`, and a minimal skeleton in the stack you chose
   (project files, one smoke test, a test command that passes). Add a `CLAUDE.md` with what the project is, its hard
   rules, how to run and test it, and that tests come with every change. Then
   `gh repo create "$(gh api user -q .login)/<short-name>" --private --source . --push`. Repos are private unless the
   user says otherwise.
3. **Set up windvane.** Run `windvane init --check "<test command>"`. Put the idea in `goal` in
   `.windvane/config.toml`. Set `heavy_check = true` if the check runs an engine, a render or a big build.
4. **Write the first tickets yourself.** You already hold the context, so this is cheaper than a planner run. Write
   4 to 8 tickets in `.windvane/tickets/T-001.md` and onward, each sized for one session of at most a few hours:
   ```
   # T-001: <imperative title>
   Depends on: nothing.
   Touches: <files/dirs it will change>.

   ## Goal
   ## Scope
   ## Done when
   - <checkable outcome, with the tests that prove it>
   ```
   Write `Touches:` carefully: tickets whose lists don't overlap run in parallel, and a ticket without the line runs
   alone. Name test files (`tests/test_<topic>.py`), not `tests/`, which overlaps every ticket. Put the order in
   `.windvane/QUEUE.md` (`- T-001 <title>`). A product call nobody has made yet becomes a ticket whose first step
   asks it (the worker rules cover how).
5. **Calibrate the critic, with the user.** It scores every ticket and sends work back below the bar, so its scale
   decides what "done" means. Uncalibrated, it drifts toward scoring right at the pass mark.
   - `.windvane/CRITIC.md`: who uses this, what 10/10 is (name the products to beat), and what 2, 5 and 8 look like
     here.
   - `.windvane/CALIBRATION.md`: 3 or 4 anchors on your own first tickets, e.g. "T-002's settings screen as a bare
     form with no defaults: 3/10; remembered values, inline validation, works by keyboard: 8/10". Add: most first
     versions land 4-6, 8+ is earned, and a score sitting on the pass mark needs a reason.
   - The bar: `[critic] pass` (default 8) and `revisions` (default 3) in the config. A higher bar means more
     revision rounds, which is better work but more tokens.
   Then **explain it to the user** in a few plain lines: the scale, two anchors, the pass mark and what it costs. Ask
   with AskUserQuestion: looks right / score harsher / score gentler / other changes (they can type them). If they're
   away, ask with `notify-ask` instead. Adjust and repeat until they're happy. Don't start the run before they answer;
   after 10 minutes with no answer, go with it and say so in step 8.
6. **Commit and push** (`git add -A && git commit && git push`). The autopilot reads tickets from `origin/main`.
7. **Start it.** Check `windvane quiet`, and ask the user about a deadline if they gave none and one matters (a
   shutdown, a meeting). Then `windvane preflight --for <hours> --json`: the product questions the next tickets will
   need. Ask them with AskUserQuestion (each question's options, recommended one first; up to four at a time), and
   record each answer with the command in its `answer_with` (a letter, or their own words in quotes). A question they
   skip goes to their phone later, if a run still needs it. Then `windvane run --for <hours> --detach --no-preflight
   --replan 2`. `--replan` plans more tickets from the goal and the critic's notes when the queue runs dry.
8. **Tell the user** in a few lines what you set up, the first tickets, and how to follow it: `windvane watch` (live,
   costs nothing), `windvane status`, their phone's `status`/`overview`/`stop`, and `windvane stop`.

## On an existing repo (the user wants windvane on the current project)
Run `windvane init --check "<the repo's test command>"`, then either write the tickets for what the user wants or run
`windvane plan --merge "<goal>"`. Calibrate the critic with the user as in step 5, then commit, push, and start it.

## Running, watching, steering
| | |
|---|---|
| `windvane status` / `queue` / `overview` | state, open questions, the queue, where the project stands |
| `windvane watch` | live view of the sessions (local, no tokens) |
| `windvane stop` | stop after the current tickets |
| `windvane answer T-007 B` (or `"free text"`) | answer a ticket's design question at the desk |
| `windvane stats [--by ticket]` | tokens, turns and minutes per role or ticket |
| `windvane new "<title>"` / `windvane plan "<goal>"` | add tickets by hand / by a planner run |
| `windvane run --until 13:50 --parallel 3` | a deadline, and at most 3 tickets at a time (default "auto": up to 4, as Touches and usage allow) |
| `windvane heavy -- <cmd>` | run an engine/render/big build one at a time on this machine |

The user overruling a critic score: add a line to `.windvane/CALIBRATION.md` (date, ticket, critic's score, theirs,
why). The critics read it.

## Don't
- Edit files in the autopilot's worktrees (`~/.local/state/windvane/<name>/work*`, `ci*`). They're its own.
- Push to `main` of a repo while its autopilot runs without telling the user. A desk change goes on a branch and a PR
  the user merges, or wait for the run to stop.
- Start a second `windvane run` on a project that's already running (`windvane status` says).
