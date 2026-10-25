# Concepts

## Tickets

A ticket is a Markdown file in `.windvane/tickets/`, named after its id:

```markdown
# T-007: Retry failed uploads with backoff
Depends on: T-004.
Touches: src/upload.py, tests/test_upload.py.

## Goal
Uploads on a flaky connection fail outright; users lose their work. (Evidence: issue #31.)

## Scope
Retry with exponential backoff, at most 5 tries. Don't change the upload API.

## Done when
- A test with a server failing twice shows the upload succeeding on the third try.
- After 5 failures the error names the file and the last HTTP status.
```

- **Depends on:** tickets that must be merged first. A ticket waits until all of them are. Anything else named here
  (`the user`, `M9`, free text) makes it wait too, until a `- done: M9 (why)` line in `QUEUE.md` names it.
  `windvane queue` shows what each waiting ticket waits on, and a hint for each wait that only you can end: the
  `- done:` line to add for an item that isn't a ticket, the `- parked:` line to remove for a parked dependency, or
  that the dependency was set aside this run. To release one ticket waiting on `the user`, edit its `Depends on:`
  line; `- done: the user` releases all of them. A worker that finds its ticket can't go on until
  another ticket merges adds that ticket's id here on its own branch: the queue reads the line there too (it can add
  a ticket, never drop one main has), holds the ticket until that one merges, then resumes it first. To release it
  sooner, edit the line on that branch; a push says so when the ticket it waits on can't merge first.
- **Touches:** the files and directories the ticket changes. It decides what may run in parallel (below). A ticket
  without this line runs alone.
- **Related:** (optional, written by the planner) open tickets that change the same feature through other files.
  Related tickets never run side by side.
- **Kind:** (optional) `experiment` for a change meant to make the built projects better: it merges only when the
  bench shows it better than `main` ([experiments](#the-critic-and-calibration)).
- **Goal / Scope / Done when:** what the worker builds and checks itself against. "Done when" points should be
  checkable, ideally by a test. The reviewer judges the change against them.

The header keys are read from the first 8 lines, and a key must start its line (`  Depends on:`, indented, isn't
one) or follow a ` · ` on a line that puts several keys together (`Milestone: M9 · Depends on: T-004`). A key inside
backticks is quoted, not read, so an unclosed backtick hides every key after it on that line. `windvane queue` warns
of both above its list.

`windvane new "<title>"` writes a ticket from the template with the next free id: after every ticket on this
checkout's branch, in its tickets folder and the main checkout's, on the upstream, on an unmerged plan or periodic
branch whose pull request isn't known to be closed, among the ids a running plan or periodic session is writing
tickets with, and in a dropped session's patch (below). Every picker (`new`, a plan, a periodic session, the
follow-ups of answers given at the desk) picks and reserves its ids under one lock in the state dir (`ids.lock`), so
two started at the same moment never share an id. It never overwrites a ticket file: one that appeared by other means
is skipped for the next id, and should that be taken too it refuses: run it again. `windvane plan "<goal>"` has a planner session write several, from a goal and the
critic's follow-up notes; it opens a pull request with them for you to look at (`--merge` merges them at once).
A plan, like a `[[periodic]]` session, ends with a line the driver reads (`PLAN: ...`, `PERIODIC: ...`). A session
that ends without it is asked for it once, in the same session: one short turn whose input comes from the cache, the
tree left as the first turn wrote it. Should it still give none, its tickets don't land, but what it changed under
`.windvane/` (since the last session of its chain that finished) is kept as a patch in the state dir's `dropped/`, and
a push gives the command that gets them back on `main` (`git apply --unidiff-zero <patch>`: the patch has no context
lines, so it still applies after the queue has moved).

### The queue and ticket status

`.windvane/QUEUE.md` gives the order: tickets listed as `- T-004` are worked top to bottom, then any other ticket by
number. More kinds of line:
```
- done: T-002 (folded into T-005)     counts as merged without a merge of its own
- done: M9 (beta shipped)             releases tickets with `Depends on: M9`
- parked: T-003 (needs a design call)  never started
- last: T-009 (polish)                runs only once no ticket not marked last can start, in the order of these lines
- rejected: T-011 (hidden 40→39, …)   an experiment the bench didn't keep: done, never merged, its branch kept
```
A ticket with a question under `## Open` in `QUESTIONS.md` on `main` waits until it's answered (moved under
`## Answered`), whoever wrote it: a question written at the desk stops its ticket like a worker's does.
Status is never kept by hand: it is derived from git. A ticket is **done** when its branch (`t-007-...`, by the
name's start: `q-t-007-...` is another branch) is merged into `main`, **in progress** while such a branch exists
unmerged (a new run resumes it first, unless nobody committed to it for `stale_days`: then it waits for its turn in
the order), and **open** otherwise. `windvane queue` shows all three.

## A ticket's way through

```
             ┌──────────────── feedback ─────────────────┐
             ▼                                           │
 ticket ─▶ worker ─▶ critic ─▶ check ─▶ reviewer ─▶ merge ─▶ main
          builds,    scores,   the       reads the    by the
          tests,     sends     project's diff,        driver, on
          pushes     fixes     tests     approves     the merged
                     back                or not       tree
```

1. **Worker.** A fresh headless Claude Code session in the autopilot's worktree. Its prompt already holds the
   ticket and any feedback from earlier rounds. It branches, builds with tests, pushes after each step, asks the
   critic, runs the check, and opens a pull request. It ends with a `RESULT:` line: `READY`, `PARTIAL` (out of
   time; the next run resumes the branch), `BLOCKED` (a question for you) or `FAILED`.
2. **Critic.** Started by the worker; scores the outcome (below).
3. **Check.** The driver runs the project's `check` on the exact pushed head. A passing result is recorded per git
   tree, so the worker's own passing check is reused rather than run twice.
   The placeholders Claude Code's Bash sandbox binds over paths it guards (character devices git can't track) don't
   count as changes, so a pass recorded inside the sandbox is reused outside it. Neither do the mount points made
   for them, which a check outside the sandbox sees as empty dotfiles while a sandboxed command is alive.
   `windvane check` runs at the
   worktree's top level, as the driver's gate does, from whatever directory it's called in.
   The check is the gate, on your machine, so hosted CI isn't needed: a workflow on `push` or `pull_request` runs
   twice per ticket (its PR, its merge) and can use up a month's Actions minutes in days. Test other versions in the
   check (windvane's own, `tools/check_pythons.sh`, runs the suite on the oldest Python installed and imports every
   module on the newest; `--all` runs the suite on all of 3.11 to 3.14), keep workflows
   `workflow_dispatch` only, and `windvane doctor` warns about any that run on their own.
   With `actions_minutes` set (your plan's monthly minutes), windvane counts the month's billed minutes itself from
   the run history of your private and internal repos (every job that started this month, a re-run of an older run
   too, each rounded up to the minute, macOS x10, Windows x2; finished attempts cached in the state dir, one count at
   a time across projects) and keeps them under `actions_cap` (90%): a session starts a workflow only through
   `windvane ci`, which refuses one whose last run's minutes would pass the cap. Public repos run free on GitHub's
   standard runners, so they aren't counted and `windvane ci` starts theirs without a cap (a larger runner, billed
   even there, isn't counted either). Workers can't start, re-run or enable one themselves (a `gh` guard first on
   their PATH refuses it, see [Safety](safety.md#workflows-run-only-by-hand)). At or over the cap the driver's
   merges and commits and the workers' own commits carry `[skip ci]` (through a git hook
   given only to the worker session, never your repo's config, that checks the last count at each commit: counts
   run in the background, so no session waits on one), and your phone hears of it once that month. Workers' commits
   carry it too while there is no count yet, only last month's, or one over a day old (its recounts failing: the log
   says so once a run). `windvane ci` says how much is used and left, and `doctor` names the count next to a
   workflow that runs on every push; `status` and `overview` leave it out. The count reads GitHub's run
   history, not its bill, so it can run a little under GitHub's figure: keep the cap below 100%.
4. **Reviewer.** A fresh read-only session with the diff, the ticket and the critic's verdict in its prompt. It
   approves or lists changes.
5. **Merge.** The driver merges `main` into the ticket's head in its own worktree, runs the check again if `main`
   moved meanwhile, then merges the pull request (forge `github`) or pushes `main` (forge `git`). A PR whose required
   checks are still running is tried again every two minutes, without a session, and so is one whose required check
   hasn't shown up yet, for one more pass after the last check ends; one that never does fails the ticket with the
   `gh workflow run` command that starts it.

Anything sent back (critic points, review changes, a red check, a merge conflict) reaches the ticket's next worker
run as written feedback. Each kind of send-back spends its own budget, so a flaky check can't use up the reviewer's
rounds. A ticket that keeps bouncing is **set aside** for the rest of the run: after `review_rounds` reviews asking
for changes, more than `check_retries` red checks, `conflict_retries` merge conflicts or `[critic] revisions` critic
send-backs, `partials` runs in a row out of time, or a run longer than `ticket_hours` (or out of its own turns).
The run's summary and the phone say which budget ran out and why.

A session that dies outside its ticket's control (API error, crash) is rerun after a growing pause (`retries`,
`retry_minutes`). When one ticket's sessions have died more than `retries` times while other sessions worked, that
ticket is set aside for the rest of the run, with one push, and its branch is kept. With no other session in between
(one ticket at a time), it is set aside on trial while another ticket runs: if that session dies too, the run stops,
as it does whenever sessions keep dying across tickets; a restart (self-update, `supervise`) keeps the trial. A dead critic is retried the same way; when it keeps dying the
worker is told it's unavailable and goes on, and no score is recorded.

An expired Claude login is no one's fault, so it spends no retry and sets no ticket aside: the run pushes once (once
per machine, however many runs share the login) asking you to run `claude` and /login, starts no session meanwhile,
and goes on by itself, with a push saying so, once `claude auth status` or the login file shows you're back. A
deadline reached while it waits ends the run with "the Claude login expired at HH:MM".

## Touches and parallel slots

With `parallel = "auto"` (the default), windvane starts another ticket beside the running ones when all of these hold:
- its `Touches:` line shares no file with a running ticket's (a directory covers every file under it);
  a running ticket's files also include those its branch has already changed, committed or not, since a Touches
  line written before the work can miss one (each merge logs the files its ticket changed outside its line);
- it doesn't name a running ticket's id or one of the files that ticket touches, and isn't `Related:` to it;
- `shared_notes` files (see [Configuration](configuration.md#top-level)) never count for these: every ticket appends
  to them, and a merge resolves conflicts that only add sections;
- the 5-hour usage window has room for one more: current usage plus the recent cost per ticket times the tickets
  running stays under `cap`;
- another slot pays: the 5-hour window is under half of `cap`, or the tickets left, at recent tickets' minutes each,
  wouldn't finish one at a time before the deadline.

A held ticket keeps its place in the queue; the next one that passes starts instead. Each slot gets its own pair of
worktrees. Every two merge conflicts between side-by-side tickets drop one slot for the rest of the run. Merges stay
one at a time, each checked on the tree `main` will have.

The log says why a slot is idle:
```
T-010 waits: overlaps T-008 on src/api.py
T-013 waits: names src/auth.py, which T-010 changes
no 2nd slot: 3 tickets x 9 min fit before 18:45 one at a time
```

## The critic and calibration

The critic is a fresh session that judges what a ticket gives the people who use the project, not the code (that is
the reviewer's job). It tries the change as a user would: in a throwaway copy of the ticket's committed head, it runs
the command that shows the ticket's promise and the tests, then one or two ways it could fail (a second pass, a
restart midway), for at most `[critic] trial_minutes` (15 by default; when they run out it answers on what it saw).
A case it sees fail sends the ticket back, named. The worker leaves an **evidence pack** for it outside git:
`notes.md` (the command that shows the promise, and what the critic can't run itself: measurements over time, before
and after numbers, a long run's output) and up to four images. A project whose output is visual or slow to make can set `[critic] show`, a command
windvane runs once per tree in the critic's copy, so the critic also sees what the project itself renders. The critic also reads
the diff of the text users read, so the pack quotes none of it: every Markdown or reStructuredText file outside
`.windvane/` (README, docs, CHANGELOG) and the files `[critic] text` names (message files, UI strings), up to 12,000
characters, the biggest hunks cut first and named. A change no user will ever see, even through a later ticket (a
refactor, test tooling), is declared `User-facing: no` and skips the critique; the reviewer checks that claim. Logic
a planned ticket will show is critiqued now.

**The scale** is `.windvane/CRITIC.md`, written per project: who uses it, what best in class looks like, and two to
four dimensions, each a ladder of rungs (say 2, 5, 8, 10) with a few yes/no checks on each (`- 5 safe: no known way
it crashes or loses work`). The first dimension is always whether the ticket works at all. Checks are answered by
trying the change, so each asks what a user meets when it's run, broken or read, never what the pack or a test
shows (`doctor` warns of one worded that way). Nor does a check ask for the project's check or full suite
passing: the driver runs the check on the final head after the critic, so the critic never sees it pass and
doesn't answer `no` for it (`doctor` and each critique's start warn of a check naming the check's command, or
"check passed", "the test suite passes" and the like). The critic answers every
check of each dimension the ticket touches, `yes`, `no` or `no (out of scope: <why>)`, with a clause of evidence,
and never writes a number: a dimension's score is its highest rung whose checks and every lower rung's are all
`yes`, and the ticket's is the lowest dimension. So give every ladder a 10 rung, reachable by a good ticket: one
that stops at 8 holds every ticket at 8 or under (`overview` and `stats --critic` say so). Pushes and the run's
summary show a ticket's first score beside its final one (`2→8`): with the pass mark at 8, the final one alone says
little. A check left unanswered counts as `no` and the log names it; an id left unanswered in 3 of the last 10
verdicts shows in `overview` and `stats --critic`. An answer counts only when its label names an id in the scale
(the log names any other), so no word is reserved for an id: a bulleted `- beyond: yes` answers a check named
`beyond`, and is the "Beyond" list's heading otherwise. Don't give two checks of one dimension, or two dimensions,
the same id: `doctor` and the run's start warn of each, and the first run on such a scale pushes the first warning
(`… (1 more: windvane doctor lists them)` when there are more). (A scale without rungs still works the old way, the
critic writing the numbers; `overview` says how to convert it.)

**The verdict** splits its points into "Fix in this ticket" and "Beyond this ticket". The critic never sees the pass
mark; the driver turns the score into the worker's next step. A plain `no` on a rung up to the first one at or over
the mark becomes a fix; `no (out of scope)` becomes a follow-up instead:

| Step | When | The worker |
|---|---|---|
| `pass` | lowest dimension ≥ `pass` | goes on to the check |
| `scope` | under the mark, but nothing this ticket can fix | goes on; the points go in the PR |
| `revise` | failed checks or fixes listed | fixes them and asks again, up to `revisions` times |
| `rethink` | the first dimension's first rung fails (old scales: under `floor`) | rethinks the approach |
| `accept` | out of revisions | goes on, score recorded |

"Beyond this ticket" points go in the pull request and feed the next `windvane plan`.

**A re-critique** judges the revision, not the whole change again. Its prompt holds the diff from the head the critic
last judged (the ticket's own files only, so merging `main` in adds nothing), that verdict's failed checks and "Fix in
this ticket" points, which it answers afresh, and its `yes` answers, which it answers again only where the diff reaches
what they judged. Those it leaves out carry over (`carried` in `critic.jsonl`) and count in the score; a first critique
carries nothing, so a check it leaves out is a `no`. An answer the driver can't count (a label naming no check, a line
off its bullet) is asked again in the same session, and carries nothing if it stays. When only the pack changed, the
prompt says so. A changed scale, a judged head that is gone, or a numeric scale is judged afresh. Its depth is the
revision's own when only the critic sent it back (see [`depth`](configuration.md#critic)).

**Who judges what.** The critic owns behaviour as a user meets it: each of its points names what a user meets (a
command's output, a push, a lost file, a wait) and what fixed looks like, never a missing test or something the pack
should show. The reviewer owns the code: tests, races, the project's rules and the failure paths the critic's trial
didn't reach, without redoing a trial a passing verdict reports; the planner drops a note that only asks for a test.

**Calibration.** A check every ticket passes, or none ever does, measures nothing. `.windvane/CALIBRATION.md` holds
your own verdicts where you disagreed with the critic (date, ticket, its answer, yours, why) and anchors as check
answers ("T-004: `safe: no`"); every critic reads it. `windvane stats --critic` shows how scores spread over recent
tickets (first and final scores, revisions, how many sit exactly on the pass mark) and each check's yes rate over the
last 10 tickets, on first heads and on any head; it and `overview` name a check `yes` on every ticket ("sharpen
it") and one never `yes` ("lower it"). Each verdict's answers are kept in `critic.jsonl`.

**The benchmark.** The critic's scale says whether one ticket is good enough to merge, and revisions stop once it
says yes, so its scores can't tell a better windvane from a worse one. `windvane bench` measures what windvane is
for, the projects it builds: small seed projects (a command-line tool, a web app, a text game) in `bench/`, each
built by the real pipeline from its first tickets, then scored on acceptance tests that check only what the
tickets promise, with what each merged ticket cost. The tests are held out: every role's sessions are denied them by
path (reading anything under `bench/` or the reference solutions, and any shell command that names a seed's hidden
folder), they are copied only into a scratch clone of the final `main`, and since a deny rule matches paths, not
intent, every session's stream is searched afterwards: a seed whose streams name a hidden test file or folder is
reported as contaminated and not counted. A score the builders could see would
be trained to; one they never see shows whether a prompt or pipeline change made the built project better.
Hidden tests miss what a user also meets (an unclear error, a command hard to find, a wrong README), so a judge
session then tries each finished seed as its user would, on the seed's own yes/no checks. It is told the goal and
the tickets, never the critic's scale, its verdicts, the pass mark or which windvane built it, so no session
can aim at its score either. `windvane bench compare A B` sets two windvane versions side by side, and with
`--repeat` tells a real difference from the runs' own noise. A benchmark nobody runs measures nothing, and one per
ticket would cost more than the tickets, so a `[[periodic]]` entry with `builtin = "bench"` runs it after changes to
the prompts or the pipeline have landed, in the background while the run goes on, and writes a ticket when the
projects got worse or dearer than with the windvane last benchmarked.

**Experiments.** That round only flags a regression after it landed. A change meant to make projects better is a
ticket with `Kind: experiment` in its header (the planner writes one for changes to what sessions are told or the
steps they take, in a project with `bench/`), and it must earn its merge: after its critic, check and review pass,
its head is benchmarked against current `main` (`experiment_repeat` runs of the head; `main`'s recorded runs at its
sha are reused, one is made when there are none) as a background job, `windvane await experiment-<id>` following it.
It is kept when, past the noise rule, neither hidden tests nor judge yeses drop and at least one rises, or both hold
and weighted tokens per merged ticket fall by `experiment_saving` %; it then merges as usual, its numbers in the log
and its PR body. Otherwise it is rejected: not merged, its branch kept, `- rejected: <id> (numbers)` in `QUEUE.md`,
and a record in `.windvane/EXPERIMENTS.md` (the idea, its files, both sides' numbers) that the planner and the
periodic sessions get, so an idea the bench turned down isn't planned again unless the ticket says what's different.
If `main` moves on during the bench, the head is compared with the new `main`, benching only its side again when its
results are missing. The approved head is saved until its bench ends: a bench run that doesn't fit before the
deadline (checked before the critic, too) sets the ticket aside, and the next run, or this one after a restart, goes
straight to the bench with no worker, critic or review paid for again, reusing every run recorded meanwhile.

## Questions and quiet times

Workers make engineering calls themselves and say why in the pull request. **Product calls** (what users see or
do, naming, scope, look and feel, money, licences) are yours. A worker that needs one writes it into
`.windvane/QUESTIONS.md`:

```markdown
## Open
### T-007: How many upload retries before giving up?
Each retry waits twice as long; 5 tries take about a minute on a dead connection.
- **A**: 5 tries, about a minute
- **B**: 3 tries, about 15 seconds
Recommendation: A, because large uploads on mobile often need the longer window.
```

What happens next depends on the time:
- **Normally**, the worker stops `BLOCKED`, the question goes to your phone (if set up), and the ticket waits while
  others run. Answer with a tap, or at the desk with `windvane answer T-007 B` (or free text in quotes). With no
  answer in `answer_wait` minutes, the next run builds the recommendation and marks it provisional. Each question
  reaches the phone once: when the ticket merges, its Done push names the answer built in one line. A ticket with
  several questions at once asks each on its own as `T-007.q1`, `T-007.q2` (answer them by those ids) and waits
  until every one is answered or timed out. An answer that comes after its question was settled isn't taken. The
  phone says so in one push per pass however many came: "Answer not taken" for one, or "Answers not taken" listing
  each id and the option being built. Once the ticket merges, change it with `windvane answer T-007 B`.
- **In a quiet time** (see [configuration](configuration.md#quiet-times); yours in `~/.config/windvane/quiet.txt`
  covers every project) nobody is asked. The worker builds its recommendation at once, records it under
  `## Provisional` with what to change to pick another option, and lists it in the pull request; its Done push spells
  the question out, since you never saw it. A question already waiting when the quiet time starts gets its
  recommendation built at once, with a push saying which. When the quiet time ends, the phone gets a list of any
  provisional answers no push has shown yet (none: no list).
- Questions marked `Waits for the user:` (spending money, assets of unclear licence, undoing merged work) are never
  answered by default: they are asked even in a quiet time, and wait for you.

**Preflight.** When `windvane run` starts in a terminal, a light session reads the tickets likely to run before the
deadline and lists the product calls they will predictably need, so you can answer them before you leave.
`windvane preflight` runs it on its own; `run --no-preflight` skips it. Answers go under `## Answered` on `main`
and into that ticket's worker prompt. Provisional answers are offered here once; one you skip stays in
`windvane status` and the run's last push, and `windvane answer` still changes it. `windvane answer` also takes a
question left under `## Open` on `main` that no run waits on (it would block its ticket for good).

## Where things live

In the project (committed): `.windvane/config.toml`, `tickets/`, `QUEUE.md`, `QUESTIONS.md`, `CRITIC.md`,
`CALIBRATION.md`.

On this machine only, under `~/.local/state/windvane/<name>/`:

| Path | What |
|---|---|
| `autopilot.log` | the driver's log, one line per event |
| `runs/` | each session's full stream, which `windvane watch` reads |
| `ledger.jsonl` | tokens, turns and minutes per session, which `windvane stats` reads |
| `bg/experiment-<id>.log` | an experiment ticket's bench, as `windvane await experiment-<id>` follows it |
| `bench.jsonl` | `windvane bench`'s results, one record per seed per run (the seed runs themselves live under `~/.local/state/windvane/_bench/`) |
| `critic.jsonl`, `critic/<ID>/` | the critic's verdicts, and each ticket's evidence pack |
| `quiet.txt` | quiet times |
| `held.json`, `held_why.json`, `closed_plans.json` | changes kept on a local branch to land when pushed again, and why their push was refused; plan branches whose PRs were closed |
| `tmp/<ID>/` | the `TMPDIR` of a ticket's sessions and checks, removed when it merges or fails; the driver also removes `windvane-*` dirs over an hour old that nothing uses from the machine's temp dir, at the start and between tickets |
| `dropped/` | the changes of a plan or periodic session dropped for want of its end line (or cut off), one patch each, whose new tickets' ids stay taken until it's removed after `keep_days`, like old transcripts |
| `id_claims/`, `ids.lock` | the ticket ids a running plan or periodic session was handed, so `windvane new` skips them until its branch is pushed; a claim whose process is gone is removed. The lock is held while an id is picked and reserved |
| `work/`, `ci/`, `plan/` | worktrees: the sessions', the driver's checks and merges, the planner's (`work-1/`, `ci-1/` and so on for extra slots) |
