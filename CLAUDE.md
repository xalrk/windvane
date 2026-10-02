# windvane: rules for Claude sessions

windvane is an autopilot for Claude Code: a Python driver (`windvane/`) that runs fresh headless sessions per ticket
(worker, critic, reviewer, planner) with the rules in `prompts/`.

## Read first
`README.md` (How it works), `docs/concepts.md` and `docs/safety.md`, then only the modules your ticket touches.
`windvane/driver.py` is the pipeline; `prompts/*.md` are the sessions' rules.

## Hard rules
- **Python 3.11+, standard library only.** It must run on a fresh machine with nothing but git, python3 and claude
  (gh, tmux and ntfy's `notify`/`notify-ask` are optional extras, detected at run time).
- **Tests never reach real tools.** Anything that runs `claude`, `notify` or `gh` is tested through `tests/helpers.py`'s
  Sandbox (a fake `claude` on a PATH without the real ones, a bare remote, an isolated state dir). Every change comes
  with tests; `python3 -m unittest discover -s tests` must pass.
- **The user's checkout is sacred.** Nothing the driver does may change the main checkout's branch, index or files
  when `isolate` is on. Destructive git (`reset --hard`, `clean`) only ever runs in the driver's own `ci` worktree.
- **windvane is for every project, not one.** A feature one project needs (a playtest, its build, its decision log)
  comes in as a generic, optional extension point set in `.windvane/config.toml`, off by default; that project's
  prompts, scripts and names stay in its own repo. Never name a user's project in code, prompts, tests or docs.
- **Prompts are the product.** Every line of `prompts/*.md` is re-read on every turn of every session: keep them
  short, concrete and free of repetition. A rule worth adding should replace one, not just add up.
- **Token efficiency is measured, not guessed.** Changes meant to save tokens say how they'd show in
  `windvane stats` (turns, fresh input, output per role).
- Never put secrets, emails or the ntfy topic names in code, prompts, tests or pushes.
- **License: MIT.**

## Style
Match the surrounding code: small functions, docstrings that say *why*, plain names. User-facing text (pushes, CLI
output) is short and concrete.
