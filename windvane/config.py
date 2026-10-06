"""A project's windvane settings (`.windvane/config.toml`) and where its files live.

In git (shared by every checkout): `.windvane/config.toml`, `tickets/`, `QUEUE.md`, `QUESTIONS.md`, `CRITIC.md`,
`CALIBRATION.md`. On this machine only (`~/.local/state/windvane/<name>/`): the log, each run's stream, the token
ledger, critic verdicts, check passes, background jobs, quiet times, and the autopilot's two worktrees.
"""
import copy
import os
import re
import subprocess
import sys
import tomllib
from pathlib import Path

from .renumber import rules

PKG = Path(__file__).resolve().parent
# Where prompts/, templates/, skills/ and hooks/ are: inside the package when installed (pipx, uv, pip), at the top of
# a git checkout run through bin/windvane or an editable install.
HOME = PKG if (PKG / "prompts").is_dir() else PKG.parent
CHECKOUT = HOME == PKG.parent
DIR = ".windvane"

DEFAULTS = {
    "name": "",  # state dir, phone ids, tmux session; default: the repo's directory name
    "display_name": "",  # how pushes and the phone's replies name the project; default: `name`, first letter upper
    "prefix": "T",  # ticket ids: T-001, T-002a
    "remote": "origin",
    "main": "main",
    "forge": "github",  # github: PRs through gh; git: the driver merges and pushes to main itself
    # GitHub Actions minutes a month (0: not counted) and the share of them windvane never passes (`windvane ci`)
    "actions_minutes": 0, "actions_cap": 0.9,
    "check": "",  # the project's CI command, run by `windvane check` (empty: no check)
    "setup": "",  # run once in a fresh worktree (e.g. "npm ci"); failing stops the run
    "setup_minutes": 20,  # a setup still running then is killed and fails (a hung `npm ci` never wedges a run)
    "isolate": True,  # work in the autopilot's own worktrees; false: in the checkout itself (huge builds)
    "goal": "",  # what `windvane plan` aims at when given no goal
    "author": "",  # "Name <email>" for the driver's own commits; default: git's
    "heavy_check": False,  # the check is machine-heavy (an engine, a render): run it under `windvane heavy`
    "overview_extra": "",  # a command whose output `windvane overview` adds (the project's own milestones)
    "models": {"worker": "opus", "worker_effort": "high", "reviewer": "opus", "reviewer_effort": "high",
               "critic": "opus", "critic_effort": "high", "planner": "opus", "planner_effort": "high",
               "preflight": "sonnet", "preflight_effort": "medium",  # preflight: a light read of the next tickets
               "fix": "opus", "fix_effort": "high"},  # fix: makes a red or conflicting desk PR merge
    "critic": {"enabled": True, "pass": 8, "floor": 4, "revisions": 3},
    # parallel: "auto" starts another ticket whenever one fits beside the running ones (Touches) and in the usage
    # window, up to max_parallel; a number caps the slots at that
    "autopilot": {"parallel": "auto", "max_parallel": 4, "cap": 75, "week_cap": 95, "ticket_hours": 3,
                  "min_slot": 30, "answer_wait": 10, "review_rounds": 3,
                  "check_retries": 2, "conflict_retries": 2,  # red checks and merge conflicts a ticket may go back for
                  "partials": 3, "subagents": False, "inline_diff_lines": 1200,
                  "retries": 3, "retry_minutes": 3,  # a session that dies (API, network) reruns after 3, 6, 12 min
                  "stale_days": 3,  # a ticket branch untouched this long resumes in its queue turn, not first
                  # forge github: the owner's open PRs with this label are checked and merged between tickets ("":
                  # off); a red or conflicting one gets up to desk_fixes fix sessions
                  "desk_label": "windvane merge", "desk_fixes": 2},
    "permissions": {},  # <role>_allow / <role>_deny: rules added to that role's sessions (none by default)
}


def exe():
    """The command that starts this same windvane from anywhere (tmux, the phone's hooks, background jobs), where
    PATH may differ: the checkout's bin/windvane, the installed console script, or the interpreter as a last resort."""
    for f in (HOME / "bin/windvane", Path(sys.executable).parent / "windvane"):
        if f.is_file() and os.access(f, os.X_OK):
            return [str(f)]
    return [sys.executable, "-m", "windvane"]


def user_dir(var, default):
    """An XDG base directory's windvane folder: config (~/.config/windvane) or state (~/.local/state/windvane)."""
    return Path(os.environ.get(var) or Path.home() / default) / "windvane"


def slots(p, parallel=None):
    """The most tickets a run may work at once: `parallel` (a number or "auto") from the command line or config."""
    n = parallel or p.ap["parallel"]
    try:
        n = int(p.ap["max_parallel"] if str(n).lower() == "auto" else n)
    except ValueError:
        raise SystemExit(f'windvane: parallel = {n!r} in config.toml: use a number of tickets or "auto"')
    return max(1, n) if p.cfg["isolate"] else 1


def _merge(base, over):
    out = copy.deepcopy(base)  # a project's settings must never alias (and so change) the shared DEFAULTS
    for k, v in over.items():
        out[k] = _merge(base[k], v) if isinstance(v, dict) and isinstance(base.get(k), dict) else v
    return out


# The roles a project may grant or deny tools in `[permissions]`, by the key's name: the session's role in runner.py.
PERM_ROLES = {"worker": "worker", "critic": "critic", "review": "reviewer", "plan": "planner", "fix": "fix",
              "periodic": "periodic"}
# A permission rule as Claude Code reads it: a tool (capitalised, or an MCP server or tool), optionally with a
# non-empty specifier in parentheses. The CLI drops a rule it can't parse without a word, so windvane checks first.
RULE_RE = re.compile(r"(?:[A-Z]\w*(?:\((?!\s*\))[^()]*(?:\([^()]*\)[^()]*)*\))?|mcp__[\w-]+(?:__(?:[\w-]+|\*))?)")


def rule_lists(where, table, keys):
    """{key: [rules]} for the lists in `table` named by `keys`, plus what is wrong with them: not a list of strings, or
    a rule the CLI couldn't parse. Reported at a run's start (the log and the phone) and by `windvane doctor`."""
    lists, bad = {}, []
    for k in keys:
        v = table.get(k, [])
        if not (isinstance(v, list) and all(isinstance(r, str) for r in v)):
            bad.append(f"{where} {k} = {v!r}: use a list of rules, e.g. [\"Bash(tools/*)\"]")
            continue
        lists[k] = v
        bad += [f"{where} {k}: {r!r} isn't a permission rule (Tool or Tool(specifier))"
                for r in v if not RULE_RE.fullmatch(r)]
    return lists, bad


def permissions(table, entries):
    """`[permissions]` and the `[[periodic]]` entries' own `allow` / `deny`, as {role: {"allow", "deny"}} for
    runner.py, and the problems with them. An unknown key is one: a typo would otherwise grant nothing, silently."""
    keys = [f"{r}_{k}" for r in PERM_ROLES for k in ("allow", "deny")]
    table = table if isinstance(table, dict) else {}
    bad = [f"[permissions] {k}: no such key; use <role>_allow or <role>_deny for {', '.join(PERM_ROLES)}"
           for k in table if k not in keys]
    lists, wrong = rule_lists("[permissions]", table, keys)
    out = {PERM_ROLES[k.rsplit("_", 1)[0]]: {"allow": [], "deny": []} for k in keys}
    for k, v in lists.items():
        role, kind = k.rsplit("_", 1)
        out[PERM_ROLES[role]][kind] = v
    for e in entries.values():
        own, more = rule_lists(f"[[periodic]] {e['name']}", e, ("allow", "deny"))
        wrong += more
        out[f"periodic:{e['name']}"] = {k: out["periodic"][k] + own.get(k, []) for k in ("allow", "deny")}
    return out, bad + wrong


LANDS = ("near", "next", "end", "hold", "ask")  # where a [[periodic]] chain's new tickets go in the queue
BUILTINS = ("project-critic",)  # [[periodic]] sessions windvane brings, so a project needs no prompt of its own


def periodic(entries):
    """`[[periodic]]` sessions by name: a project's own session (a playtest, an audit) run every `every` merges, when a
    group of tickets (`after_group`: a regex on their headers, group 1 the group's name) is finished, or only as
    another entry's `then` when neither is set. `builtin` names one of windvane's own sessions instead of a `prompt`
    (`project-critic`: the critic on the whole project, its `dims` and `parallel` as `windvane critic --project`'s).
    Mistakes stop the run at once rather than at the Nth merge."""
    out = {}
    for i, e in enumerate(entries if isinstance(entries, list) else [entries]):
        name = str(e.get("name") or f"periodic-{i + 1}")
        if not re.fullmatch(r"[A-Za-z0-9_-]+", name) or name in out:
            raise SystemExit(f"windvane: [[periodic]] name = {name!r}: use a unique word of letters, digits, - or _")
        builtin = str(e.get("builtin", ""))
        if builtin and builtin not in BUILTINS:
            raise SystemExit(f"windvane: [[periodic]] {name}: builtin = {builtin!r}: use one of {', '.join(BUILTINS)}")
        if not e.get("prompt") and not builtin:
            raise SystemExit(f"windvane: [[periodic]] {name} needs prompt = \"<the project's rules file>\"")
        try:
            every, minutes, par = (int(e.get(k, d)) for k, d in (("every", 0), ("minutes", 60), ("parallel", 4)))
        except (TypeError, ValueError):
            raise SystemExit(f"windvane: [[periodic]] {name}: every, minutes and parallel are whole numbers")
        dims = e.get("dims", [])
        dims = [str(d).strip().lower() for d in (dims.split(",") if isinstance(dims, str) else dims) if str(d).strip()]
        setup_m = setup_minutes(e.get("setup_minutes", 60), f"[[periodic]] {name}: ")
        land = str(e.get("land", ""))
        if land and land not in LANDS:
            raise SystemExit(f"windvane: [[periodic]] {name}: land = {land!r}: use one of {', '.join(LANDS)}")
        group = str(e.get("after_group", ""))
        try:
            if group and re.compile(group).groups < 1:
                raise re.error("no (group) to name the ticket's group")
        except re.error as err:
            raise SystemExit(f"windvane: [[periodic]] {name}: after_group = {group!r}: {err}")
        out[name] = {"name": name, "land": land, "every": max(0, every), "after_group": group, "minutes": max(1, minutes),
                     "setup": e.get("setup", ""), "setup_minutes": setup_m,
                     "prompt": e.get("prompt", ""), "then": e.get("then", ""), "builtin": builtin,
                     "dims": dims, "parallel": max(1, par),
                     "allow": e.get("allow", []), "deny": e.get("deny", [])}
    for e in out.values():
        if e["then"] and e["then"] not in out:
            raise SystemExit(f"windvane: [[periodic]] {e['name']}: then = {e['then']!r} names no [[periodic]] entry")
    return out


def setup_minutes(v, where=""):
    """A `setup_minutes` value, top-level or a [[periodic]]'s (`where`): any number of minutes above 0."""
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v <= 0:
        raise SystemExit(f"windvane: {where}setup_minutes = {v!r} in config.toml: use a number of minutes above 0")
    return v


def actions_budget(c):
    """`actions_minutes` and `actions_cap`, checked at once: a typo here would let `windvane ci` spend unchecked."""
    m, cap = c["actions_minutes"], c["actions_cap"]
    if isinstance(m, bool) or not isinstance(m, (int, float)) or m < 0:
        raise SystemExit(f"windvane: actions_minutes = {m!r} in config.toml: use your plan's monthly minutes (0: off)")
    if isinstance(cap, bool) or not isinstance(cap, (int, float)) or not 0 < cap <= 1:
        raise SystemExit(f"windvane: actions_cap = {cap!r} in config.toml: use a share above 0, at most 1 (0.9: 90%)")


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
        self.display = c["display_name"] or self.name[0].upper() + self.name[1:]
        self.prefix = c["prefix"].upper()
        self.id_re = rf"{re.escape(self.prefix)}-\d+[a-z]?"
        self.remote, self.main = c["remote"], c["main"]
        self.upstream = f"{self.remote}/{self.main}"
        self.state = user_dir("XDG_STATE_HOME", ".local/state") / self.name
        self.tmux = f"windvane-{self.name}"
        self.models = c["models"]
        self.critic = c["critic"]
        self.ap = c["autopilot"]
        setup_minutes(c["setup_minutes"])
        actions_budget(c)
        self.periodic = periodic(c.get("periodic", []))
        self.perms, self.perm_problems = permissions(c["permissions"], self.periodic)
        self.append_only = rules(c.get("append_only", []))

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

    @property
    def plan_dir(self):
        """Where planner runs write tickets: always its own worktree, so a desk `plan` can't touch the checkout or
        the tree the driver is checking in."""
        return self.state / "plan"

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
