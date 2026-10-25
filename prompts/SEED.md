# windvane seed run

You write one new seed project for the bench's training pool: a small project windvane will build from its tickets,
scored on hidden tests its sessions never see. The user is away; nobody answers questions.

- **Kind**: one from this list not under "Kinds taken" in your prompt: `todo` (a task list CLI), `wordfreq` (a word
  counter over files), `csvtool` (filter and sum CSV columns), `timer` (a pomodoro log), `inventory` (stock in and
  out), `quiz` (a flash-card drill), `units` (a unit converter), `budget` (monthly envelopes), `habits` (a streak
  tracker), `bookmarks` (tagged links), `recipes` (scale a recipe), `gradebook` (weighted grades).
- **Format**: as the seeds in `bench/` (read one first). `seed/` is the starting repo: `CLAUDE.md` (what it is, hard
  rules, run and test), a stub program that starts and does little, `tests/` with one passing smoke test, and
  `.windvane/` with `config.toml` (`forge = "git"`, a `check`, a `goal`, `[critic]`; no `[permissions]`), `QUEUE.md`
  and 3-4 tickets `T-001`..., each `# <ID>: <title>`, `Depends on:`, `Touches:`, Goal, Scope and concrete Done when.
  `hidden/` is `bench.toml` (`command` running `accept_*.py` with `unittest -v`, `[tickets]` naming each ticket's test
  modules), the `accept_*.py` tests (behaviour a user sees, from the tickets' words only, never the reference's
  internals) and `JUDGE.md` (`- J1: ...` checks a user can try by hand). Python standard library only.
- **Reference**: `reference/` holds the files that, laid over `seed/`, finish every ticket. Check it yourself in a
  scratch copy under `$TMPDIR` (seed, then reference, then hidden on top): every ticket's tests must pass there and
  fail on the bare seed. The driver checks the same and refuses the seed otherwise; `reference/` is never committed.
- Write only under the folder your prompt names. Don't commit, push or switch branches.
- Your last line is exactly `SEED: <kind>: <one line on the project and its tickets>`.
