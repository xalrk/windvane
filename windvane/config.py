"""A project's windvane settings (`.windvane/config.toml`) and where its files live.

In git (shared by every checkout): `.windvane/config.toml`, `tickets/`, `QUEUE.md`, `QUESTIONS.md`, `CRITIC.md`,
`CALIBRATION.md`. On this machine only (`~/.local/state/windvane/<name>/`): the log, each run's stream, the token
ledger, critic verdicts, check passes, background jobs, quiet times, and the autopilot's two worktrees.
"""
import os
import re
import subprocess
import tomllib
from pathlib import Path

PKG = Path(__file__).resolve().parent
HOME = PKG.parent  # the windvane checkout: prompts/ and templates/
DIR = ".windvane"

DEFAULTS = {
    "name": "",  # state dir, phone ids, tmux session; default: the repo's directory name
    "prefix": "T",  # ticket ids: T-001, T-002a
    "remote": "origin",
    "main": "main",
    "forge": "github",  # github: PRs through gh; git: the driver merges and pushes to main itself
    "check": "",  # the project's CI command, run by `windvane check` (empty: no check)
    "setup": "",  # run once in a fresh worktree (e.g. "npm ci")
    "isolate": True,  # work in the autopilot's own worktrees; false: in the checkout itself (huge builds)
    "goal": "",  # what `windvane plan` aims at when given no goal
    "author": "",  # "Name <email>" for the driver's own commits; default: git's
    "heavy_check": False,  # the check is machine-heavy (an engine, a render): run it under `windvane heavy`
    "models": {"worker": "opus", "worker_effort": "high", "reviewer": "opus", "reviewer_effort": "high",
               "critic": "opus", "critic_effort": "high", "planner": "opus", "planner_effort": "high"},
    "critic": {"enabled": True, "pass": 8, "floor": 4, "revisions": 3},
    "autopilot": {"parallel": 2, "cap": 75, "week_cap": 95, "ticket_hours": 3, "min_slot": 30, "answer_wait": 10,
                  "review_rounds": 3, "partials": 3, "subagents": False, "inline_diff_lines": 1200,
                  "retries": 3, "retry_minutes": 3},  # a session that dies (API, network) reruns after 3, 6, 12 min
}


def _merge(base, over):
    out = dict(base)
    for k, v in over.items():
        out[k] = _merge(base[k], v) if isinstance(v, dict) and isinstance(base.get(k), dict) else v
    return out


def toplevel(start=None):
    r = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=start or os.getcwd(), capture_output=True,
                       text=True)
    return Path(r.stdout.strip()) if r.returncode == 0 else None


class Project:
    def __init__(self, root):
        self.root = Path(root).resolve()
        f = self.root / DIR / "config.toml"
        self.exists = f.is_file()
        self.cfg = _merge(DEFAULTS, tomllib.loads(f.read_text()) if self.exists else {})
        c = self.cfg
        self.name = re.sub(r"[^a-z0-9-]+", "-", (c["name"] or self.main_root().name).lower()).strip("-") or "project"
        self.prefix = c["prefix"].upper()
        self.id_re = rf"{re.escape(self.prefix)}-\d+[a-z]?"
        self.remote, self.main = c["remote"], c["main"]
        self.upstream = f"{self.remote}/{self.main}"
        self.state = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state") / "windvane" / self.name
        self.tmux = f"windvane-{self.name}"
        self.models = c["models"]
        self.critic = c["critic"]
        self.ap = c["autopilot"]

    def main_root(self):
        """The main checkout, also when `root` is one of its worktrees."""
        r = subprocess.run(["git", "rev-parse", "--path-format=absolute", "--git-common-dir"], cwd=self.root,
                           capture_output=True, text=True)
        return Path(r.stdout.strip()).parent if r.returncode == 0 else self.root

    # Paths in git, relative to a checkout.
    tickets_rel = f"{DIR}/tickets"
    queue_rel = f"{DIR}/QUEUE.md"
    questions_rel = f"{DIR}/QUESTIONS.md"
    rubric_rel = f"{DIR}/CRITIC.md"
    calibration_rel = f"{DIR}/CALIBRATION.md"

    @property
    def work_dir(self):
        """Where worker runs edit: the autopilot's own worktree, or the checkout when isolate is off."""
        return self.state / "work" if self.cfg["isolate"] else self.main_root()

    @property
    def ci_dir(self):
        """Where the driver checks, reviews and merges, always on a detached head."""
        return self.state / "ci" if self.cfg["isolate"] else self.main_root()

    def branch_prefix(self, tid):
        return tid.lower()

    def norm(self, tid):
        m = re.fullmatch(rf"(?i){re.escape(self.prefix)}-(\d+)([a-z]?)", tid.strip())
        return f"{self.prefix}-{m.group(1)}{m.group(2).lower()}" if m else None

    def qid(self, tid):
        """A notify-ask id for a ticket's question: 2-40 chars, a-z0-9-, starting with a letter."""
        name = self.name if self.name[0].isalpha() else "p" + self.name
        return f"{name[:39 - len(tid)].rstrip('-')}-{tid.lower()}"


def find(start=None):
    root = toplevel(start)
    if not root:
        raise SystemExit("windvane: not in a git repository")
    return Project(root)
