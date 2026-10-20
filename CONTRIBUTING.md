# Contributing to windvane

Thanks for helping! Bug reports, ideas, fixes to the docs and code are all welcome. windvane is small on purpose: a
Python driver (`windvane/`) and the rules its Claude sessions follow (`prompts/`). This page covers reporting a
problem, running windvane from a clone, how the tests work, and what we look for in a change.

## Reporting bugs and suggesting features
Open an issue on GitHub. For a bug, it helps to include:
- what you ran, what you expected and what happened instead;
- the output of `windvane doctor` (its first line is what `windvane --version` prints) and your OS;
- the lines of `windvane status` or the run's log around the problem.

Logs can hold your paths, branch names and ticket titles, so look them over before you paste them, and leave out any
token, email address or ntfy topic.

If you'd like to make a larger change (a new command, a config key, a change to how sessions work), please open an
issue first so we can talk it through before you spend time on it. Small fixes can go straight to a pull request.

**Security problems:** please don't open a public issue. Report them privately as [SECURITY.md](SECURITY.md)
describes.

## Setup
```sh
git clone https://github.com/xalrk/windvane
cd windvane
bin/windvane doctor          # what this machine lacks
bin/windvane --help
```
`bin/windvane` runs the checkout directly; no install, no virtual environment, nothing to download.

## Tests
```sh
python3 -m unittest discover -s tests -q
```
It runs the whole driver end to end, so it takes a while. `tools/check_pythons.sh` runs it on every Python from
3.11 up that you have installed, the test modules side by side (slowest first, once it has timed them), reruns a
failed module once on its own and names it (a test that times its steps can miss on a busy machine), and prints the
five slowest modules; give it unittest arguments and it runs just those, in one process per Python. The CI workflow
(`.github/workflows/test.yml`) runs it on Linux and macOS; it is started by hand, so a maintainer runs it on your pull
request before merging.

The tests never run the real tools. Anything that would call `claude`, `gh` or `notify`, or reach your phone, goes
through `tests/helpers.py`'s `Sandbox`:
- a throwaway project repository with a bare `origin` remote, set up for forge `git`;
- a fake `claude` on a `PATH` that holds only it and the system's bin directories, so the real `claude`, `gh` and
  ntfy scripts can't be found. It reads the system prompt it is given, plays that role (worker, critic, reviewer,
  planner, preflight) and logs each call to `calls.jsonl` for assertions;
- its own `HOME`, `XDG_STATE_HOME` and `XDG_CONFIG_HOME`, so nothing reads or writes your real state or config.

```python
from helpers import Sandbox

class MyTest(unittest.TestCase):
    def setUp(self):
        self.s = Sandbox()
        self.addCleanup(self.s.close)

    def test_something(self):
        self.s.ticket("T-001", touches="app.py")
        self.s.publish()
        ...
```
The phone tests use a local fake ntfy server in the same spirit, and `tests/test_docs.py` keeps these docs in step
with the code: every config key documented, every command mentioned, every relative link resolving.

`tests/test_clean_machine.py` walks a new user's first hour (install.sh into an empty home, `doctor`, `install`,
`init`, `new`, and a run that merges the ticket) with nothing but the system's tools and the fake `claude`. To run
the same on a machine that has never seen windvane, `tools/clean_machine_check.sh` runs it in a throwaway podman or
docker container from a pinned Debian image; it downloads, so it's by hand, never part of the check. Its `--shell`
leaves you in such a container with windvane installed, to try the first run with the real `claude`.

## What we look for
These are what make windvane safe to leave running overnight, so every change keeps them:
- **Nothing to install.** windvane needs only git, Python 3.11+ and `claude`, and uses the standard library alone.
  Please don't add dependencies; optional tools (`gh`, tmux, `qrencode`, ntfy) are detected at run time.
- **Tests come with the change**, and the whole suite passes.
- **Your checkout stays yours.** With `isolate` on, nothing the driver does changes the main checkout's branch, index
  or files; destructive git (`reset --hard`, `clean`) only runs in the driver's own `ci` worktree.
- **Prompts stay short.** Every line of `prompts/*.md` is re-read on every turn of every session, so it costs tokens
  all day. Keep them concrete and free of repetition; a new rule should replace an old one where it can.
- **Token savings are measured.** A change meant to save tokens shows how in `windvane stats` (turns, fresh input,
  output per role).
- **No secrets** in code, prompts, tests or commits: no emails, tokens or ntfy topics.

## Style
Match the surrounding code: small functions, plain names, docstrings that say *why*. Text users read (CLI output,
pushes) is short and concrete.

## Changes
Open a pull request against `main` with what changed and why, and add a line to `CHANGELOG.md` under
"Unreleased". Small, focused pull requests are easiest to review.

## License
windvane is released under the [MIT License](LICENSE). By contributing, you agree that your contributions are
licensed under the same terms.
