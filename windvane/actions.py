"""The month's GitHub Actions minutes, counted from the run history, and the cap windvane keeps them under.

windvane is the only thing that spends the user's Actions minutes, so it counts them itself instead of asking the
billing API (which needs gh's `user` scope): every job that started this calendar month (UTC) in the login's repos,
whichever month its run was created in (a re-run is billed when it runs), each rounded up to the minute, times 10
on macOS runners and 2 on Windows ones; self-hosted runners are free.
A desk count done this way matched GitHub's own figure. A running job counts up to now, a cancelled one up to its end.

Off unless `.windvane/config.toml` sets `actions_minutes` (the plan's monthly minutes); `actions_cap` (0.9) is the
share of them windvane never goes past: `windvane ci` refuses a workflow whose estimate would, and at or over it the
driver adds `[skip ci]` to the commits it pushes, and so do the workers' own (`worker_git`, decided at
each commit from the cached count).
"""
import fcntl
import json
import math
import os
import shlex
import shutil
import sys
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import net
from .config import user_dir
from .util import load_json, log, push, run, save_json

MULT = (("self-hosted", 0), ("macos", 10), ("windows", 2))  # the first label that matches sets the rate
FRESH_S = 600  # a total this young is reused: status and every merge would otherwise list every repo's runs again
WAIT_S = 120  # how long a driver waits on another's count before giving up on it
RERUN_DAYS = 31  # GitHub re-runs a run up to 30 days after it was created; one more for clock slack
SKIP = "[skip ci]"
# Denied to worker sessions: workflows start only through `windvane ci`, which runs gh itself, past these rules. They
# cover gh's own commands (with flags before the verb too) and the REST endpoints that start, re-run or turn one on;
# a method flag on an /actions/ path is denied whatever it does, as a worker has no call to write there.
DENY = [*(f"Bash(gh {verb}*)" for verb in ("workflow run", "workflow enable", "run rerun")),
        "Bash(gh workflow * run *)", "Bash(gh workflow * enable *)", "Bash(gh run * rerun*)",
        "Bash(gh api *dispatches*)", "Bash(gh api *rerun*)", "Bash(gh api */enable*)",
        *(f"Bash(gh api *{a}*{b}*)" for flag in ("-X", "--method")
          for a, b in ((flag, "/actions/"), ("/actions/", flag)))]
GUARD = "windvane gh guard"  # in the wrapper's first lines: how a guard is told from the real gh


def cache_file():
    """One per user, not per project: the minutes are the login's, whichever project counts them."""
    return user_dir("XDG_STATE_HOME", ".local/state") / "actions.json"


def budget(p):
    """(the month's minutes, the cap as a share of them), or None when the budget is off (the default)."""
    minutes = p.cfg["actions_minutes"]  # checked by config.actions_budget
    return (minutes, p.cfg["actions_cap"]) if minutes else None


def month(now=None):
    return (now or datetime.now(timezone.utc)).strftime("%Y-%m")


def stamp(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")) if s else None


def rate(labels):
    low = [str(x).lower() for x in labels or ()]
    return next((m for word, m in MULT if any(word in x for x in low)), 1)


def job_minutes(job, now):
    """A job's billed minutes: its time rounded up to the minute, times its runner's rate. One that never started
    (skipped, queued) costs nothing; one still running counts up to now."""
    start = stamp(job.get("started_at"))
    if not start:
        return 0
    end = stamp(job.get("completed_at")) or now
    secs = max(0, (end - start).total_seconds())
    return math.ceil(secs / 60) * rate(job.get("labels"))


def api(cd, path):
    """A GET through `gh api`, parsed; None when it fails (no gh, not logged in, no network)."""
    r = net.call(["gh", "api", path], cd)
    try:
        return json.loads(r.stdout) if r.returncode == 0 else None
    except ValueError:
        return None


def pages(cd, path, key, stop=lambda item: False):
    """Every item of a paged list (`key` in each page), 100 a page, up to the first for which `stop` is true; None
    when a page fails."""
    out, n = [], 1
    while True:
        page = api(cd, f"{path}{'&' if '?' in path else '?'}per_page=100&page={n}")
        if page is None:
            return None
        items = page.get(key) or []
        for i, item in enumerate(items):
            if stop(item):
                return out + items[:i]
        out += items
        if len(items) < 100:
            return out
        n += 1


def run_minutes(cd, repo, run, now, whole=False):
    """A run's minutes billed this month (or in all, `whole`): every attempt's jobs (a re-run is billed again) that
    started this month, so a run created last month and re-run now counts only its new jobs."""
    jobs = pages(cd, f"repos/{repo}/actions/runs/{run['id']}/jobs?filter=all", "jobs")
    return None if jobs is None else sum(job_minutes(j, now) for j in jobs
                                         if whole or month(stamp(j.get("started_at")) or now) == month(now))


def repos(cd):
    """The login's repos, owner/name: the only place its minutes can go."""
    r = net.call(["gh", "repo", "list", "--limit", "1000", "--json", "nameWithOwner", "-q", ".[].nameWithOwner"], cd)
    return r.stdout.split() if r.returncode == 0 else None


def billed(cd, cache, repo, run, now):
    """{repo, id, workflow, minutes} for a run: from the cache once it finished, else fetched (and cached if done).
    Keyed by attempt too: a re-run keeps its run's id, and its new jobs must be fetched."""
    key = f"{repo}#{run['id']}#{run.get('run_attempt') or 1}"
    if key in cache["runs"]:
        return cache["runs"][key]
    mins = run_minutes(cd, repo, run, now)
    if mins is None:
        return None
    done = {"repo": repo, "id": run["id"], "workflow": run.get("path", ""), "minutes": mins}
    if run.get("status") == "completed":
        cache["runs"][key] = done
    return done


def load(now):
    """This month's cache: finished runs' minutes, the last total, the month a push said "at the cap"."""
    cache = load_json(cache_file(), {})
    if cache.get("month") != month(now):
        cache = {"month": month(now), "runs": {}, "warned": cache.get("warned", "")}
    return cache


@contextmanager
def locked(wait=None):
    """The count's lock, next to the cache: two projects' drivers starting together would otherwise make the same gh
    calls twice and the last one's cache would win. Yields whether it was got within `wait` seconds."""
    path = cache_file().with_suffix(".lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    give_up = time.time() + (WAIT_S if wait is None else wait)
    with path.open("a") as fh:
        while True:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.time() >= give_up:
                    yield False
                    return
                time.sleep(0.2)
        try:
            yield True
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def this_month(run, m, now):
    """Whether a run may have jobs billed in month `m`: created in it, or re-run in it (its latest attempt's start)."""
    return any(run.get(k) and month(stamp(run[k])) == m for k in ("created_at", "run_started_at", "updated_at"))


def count(cd, now=None, max_age=FRESH_S):
    """{"used": minutes billed this month, "at": when} or None when the count failed. One count at a time across
    drivers: one that finds a count in flight waits for it and reads its total. A finished run is fetched once and
    kept in the cache; running ones are fetched each time."""
    now = now or datetime.now(timezone.utc)
    asked = time.time()
    if (fresh := cached_total(now, max_age)) is not None:
        return fresh
    with locked() as got:
        cache = load(now)
        total = cache.get("total")
        if total and (total["at"] >= asked or time.time() - total["at"] < max_age):
            return total  # counted by another driver while this one waited
        if not got:
            return None
        return count_now(cd, cache, now)


def cached_total(now, max_age):
    total = load(now).get("total")
    return total if total and time.time() - total["at"] < max_age else None


def count_now(cd, cache, now):
    names = repos(cd)
    if names is None:
        return None
    m, used = month(now), 0
    first = datetime.strptime(m, "%Y-%m").replace(tzinfo=timezone.utc)
    oldest = (first - timedelta(days=RERUN_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")  # GitHub's format: compares as text
    for repo in names:
        # Newest created first, back to the oldest run that can still be re-run this month. The runs list filters by
        # `created` only (and stops at 1,000 runs then), not by when a run last ran, so last month's runs are read
        # here and only those whose latest attempt started this month are fetched.
        runs = pages(cd, f"repos/{repo}/actions/runs", "workflow_runs",
                     lambda run: (run.get("created_at") or oldest) < oldest)
        for run in runs if runs is not None else [None]:
            if run and not this_month(run, m, now):
                continue
            done = run and billed(cd, cache, repo, run, now)
            if not done:
                return None
            used += done["minutes"]
    cache["total"] = {"used": used, "at": time.time()}
    save_json(cache_file(), cache)
    return cache["total"]


def cached():
    """The last count, if it is this month's: status and overview never wait on the network for it."""
    cache = load_json(cache_file(), {})
    return cache.get("total") if cache.get("month") == month() else None


def workflow(cd, repo, name):
    """The repo's workflow (`name`, `path`, `state`) for a name, file name or path; with no name, its only one (or
    only active one). Raises ValueError with what to pass instead."""
    flows = (api(cd, f"repos/{repo}/actions/workflows?per_page=100") or {}).get("workflows") or []
    pick = [w for w in flows if not name or name in (w.get("name"), w.get("path"), base(w))]
    if len(pick) > 1:
        pick = [w for w in pick if w.get("state") == "active"] or pick
    if len(pick) != 1:
        raise ValueError(f"{'no' if not pick else 'more than one'} workflow{f' named {name!r}' if name else ''} in "
                         f"{repo}; name one of: {', '.join(map(base, flows)) or 'none'}")
    return pick[0]


def base(w):
    return w.get("path", "").rsplit("/", 1)[-1]


def estimate(cd, repo, file, now=None):
    """The workflow's last finished run's billed minutes: what the next one will likely cost. None with no such run,
    or when it can't be fetched."""
    now = now or datetime.now(timezone.utc)
    page = api(cd, f"repos/{repo}/actions/workflows/{file}/runs?status=completed&per_page=1")
    runs = (page or {}).get("workflow_runs") or []
    if not runs:
        return None
    return run_minutes(cd, repo, runs[0], now, whole=True)


def cap_minutes(p):
    minutes, cap = budget(p)
    return math.floor(minutes * cap)


def fmt(n):
    return f"{math.ceil(n):,}"


def line(p, total=None):
    """`Actions 1,983/2,000 min` for status and overview (with the count's time once it's over an hour old), or ""
    with the budget off or nothing counted yet."""
    b = budget(p)
    total = total or (cached() if b else None)
    if not (b and total):
        return ""
    when = time.strftime("%H:%M", time.localtime(total["at"]))
    return f"Actions {fmt(total['used'])}/{fmt(b[0])} min" + f" (counted {when})" * (time.time() - total["at"] > 3600)


def refresh(p):
    """Recount in the background (the driver at its start, every session at its end): status, the phone and worker
    starts only read the cache, never wait. One background count per process at a time."""
    if budget(p) and counting.acquire(blocking=False):
        threading.Thread(target=refresh_now, args=(p.root,), daemon=True).start()


counting = threading.Lock()


def refresh_now(cd):
    try:
        count(cd)
    finally:
        counting.release()


def cached_over_cap(p):
    """Whether the cache says the cap is reached, with no count: a missing total or last month's counts as reached,
    since a `[skip ci]` too many costs nothing. The worker's commit hook makes the same call in its own words."""
    total = cached()
    return not total or total["used"] >= cap_minutes(p)


def over_cap(p, cd=None):
    """Whether the month's minutes are at or over the cap. A count that fails falls back on the last one, and with
    none at all says no: workflows run only by hand (`windvane ci`), so `[skip ci]` is a backstop, not the guard."""
    if not budget(p):
        return False
    total = count(cd or p.root) or cached()
    return bool(total) and total["used"] >= cap_minutes(p)


logged = set()  # the projects whose log has said "at the cap" in this process: once a run, not once a commit
asked = set()  # the projects whose log has said their workers are asked for [skip ci], their hooks being their own


def tag(p, msg, cd=None):
    """`msg` with `[skip ci]` added when the month's minutes are at or over the cap, in case a workflow still runs on
    push. Logged once a run; the first time in a month, one phone push."""
    if SKIP in msg or not over_cap(p, cd):
        return msg
    if p.name not in logged:
        logged.add(p.name)
        log(p, f"GitHub Actions at the cap ({line(p)}, cap {cap_minutes(p):,}): the driver's commits carry {SKIP}")
    with locked() as got:  # under the count's lock, so this write can't drop a total another driver just saved
        cache = load(None)
        warn = got and cache.get("warned") != cache["month"]
        if warn:
            cache["warned"] = cache["month"]
            save_json(cache_file(), cache)
    if warn:
        push("Actions minutes at the cap", f"{line(p)} used this month (cap {cap_minutes(p):,}). windvane adds "
             f"{SKIP} to its commits and `windvane ci` refuses workflows until the month ends.", 4, "warning")
    return f"{msg} {SKIP}"


def worker_git(p, wd):
    """(environment, prompt line) for a worker session. With a budget set its own commits get a prepare-commit-msg
    hook, given in git's environment config, never the repo's config (the user's checkout shares it), that adds
    `[skip ci]` when the cached total is at the cap at the time of each commit: a session runs for hours and the
    month's count can cross the cap meanwhile. A project with its own core.hooksPath keeps it, and the prompt asks
    for the tag instead when the cache says the cap is reached at the session's start. Never counts: starts don't
    wait on gh."""
    if not budget(p):
        return {}, ""
    if run(["git", "config", "core.hooksPath"], wd).stdout.strip():
        if not cached_over_cap(p):
            return {}, ""
        if p.name not in asked:
            asked.add(p.name)
            log(p, f"GitHub Actions at the cap and core.hooksPath is the project's: workers are asked for {SKIP}")
        return {}, f"GitHub Actions minutes are at the month's cap: end each commit message's first line with {SKIP}."
    n = int(os.environ.get("GIT_CONFIG_COUNT") or 0)
    return {"GIT_CONFIG_COUNT": str(n + 1), f"GIT_CONFIG_KEY_{n}": "core.hooksPath",
            f"GIT_CONFIG_VALUE_{n}": str(skip_hooks(p, wd))}, ""


# The hook's test of the cache: exit 0 (no tag) only when this month's cached total is under the cap.
UNDER = ("import json, sys, time\n"
         "try:\n    c = json.load(open(sys.argv[1]))\nexcept (OSError, ValueError):\n    c = {}\n"
         "t = c.get('total') or {}\n"
         "ok = c.get('month') == time.strftime('%Y-%m', time.gmtime()) and t.get('used', 1e9) < int(sys.argv[2])\n"
         "sys.exit(0 if ok else 1)\n")


def skip_hooks(p, wd):
    """A hooks folder in the state dir: the repo's own hooks linked in, and a prepare-commit-msg that runs the repo's
    own (if any) and then, unless the cached total is under the cap (written into it), adds `[skip ci]` to the first
    line. It reads only the cache: no network in a git hook."""
    own = Path(wd, run(["git", "rev-parse", "--git-common-dir"], wd).stdout.strip(), "hooks").resolve()
    folder = p.state / "skip-ci-hooks" / Path(wd).resolve().name  # one per worktree: parallel slots rebuild their own
    shutil.rmtree(folder, ignore_errors=True)
    folder.mkdir(parents=True)
    for hook in own.iterdir() if own.is_dir() else ():
        if hook.suffix != ".sample" and hook.name != "prepare-commit-msg" and os.access(hook, os.X_OK):
            (folder / hook.name).symlink_to(hook)
    first = own / "prepare-commit-msg"
    script = folder / "prepare-commit-msg"
    script.write_text("#!/bin/sh\n# windvane: [skip ci] while the month's GitHub Actions minutes are at the cap\n"
                      + (f'{shlex.quote(str(first))} "$@" || exit $?\n' if os.access(first, os.X_OK) else "")
                      + f"python3 -c {shlex.quote(UNDER)} {shlex.quote(str(cache_file()))} {cap_minutes(p)} "
                      "2>/dev/null && exit 0\n"
                      f"grep -qF '{SKIP}' \"$1\" && exit 0\n"
                      f"{{ printf '%s {SKIP}\\n' \"$(sed -n 1p \"$1\")\"; sed 1d \"$1\"; }} > \"$1.windvane\" "
                      '&& mv "$1.windvane" "$1"\n')
    script.chmod(0o755)
    return folder


# The worker's `gh`: the DENY rules see only the command as typed, so `env gh …`, a script or a subprocess get past
# them; this sees every call. Standard library only, as it runs on whatever python ran the driver.
GUARD_SCRIPT = r"""
import os, sys
SHORT = set("RXHfFqtp")  # gh's one-letter flags that take a value
LONG = {"--repo", "--method", "--header", "--raw-field", "--field", "--input", "--jq", "--template", "--preview",
        "--hostname", "--cache"}
FIELDS = {"-f", "-F", "--field", "--raw-field", "--input"}  # what turns gh api into a POST


def parse(a):
    # (positional words, [(flag, value)]) the way gh's flag parser reads them: a short group like -iXPOST is
    # -i -X POST (a letter that takes a value takes the rest of the group, or the next argument), so no flag's
    # value is taken for the command or the API path.
    pos, flags, i = [], [], 0
    while i < len(a):
        x, i = a[i], i + 1
        if x == "--":
            return pos + a[i:], flags
        if x.startswith("--"):
            name, eq, v = x.partition("=")
            if not eq and name in LONG and i < len(a):
                v, i = a[i], i + 1
            flags.append((name, v))
        elif x.startswith("-") and len(x) > 1:
            for j, c in enumerate(x[1:], 2):
                if c not in SHORT:
                    flags.append(("-" + c, ""))
                    continue
                v = x[j:]
                v = v[1:] if v[:1] == "=" and len(v) > 1 else v
                if not v and i < len(a):
                    v, i = a[i], i + 1
                flags.append(("-" + c, v))
                break
        else:
            pos.append(x)
    return pos, flags


def method(flags):
    # gh api's method: -X/--method if given, else POST when it sends fields (-f, -F, --input), else GET.
    m = [v for f, v in flags if f in ("-X", "--method")]
    return (m[-1] if m else "POST" if any(f in FIELDS for f, _ in flags) else "GET").upper()


def refused(a):
    w, flags = parse(a)
    if w[:1] == ["workflow"] and w[1:2] in (["run"], ["enable"]) or w[:2] == ["run", "rerun"]:
        return True
    if w[:1] == ["alias"] and w[1:2] in (["set"], ["import"]):  # an alias could hide a refused command
        return True
    paths = w[1:] if w[:1] == ["api"] else []  # gh api takes one; every word is checked, should a parse go wrong
    return any(any(s in p for s in ("dispatches", "rerun", "/enable")) or "actions/" in p and method(flags) != "GET"
               for p in paths)


a = sys.argv[1:]
if refused(a):
    sys.exit("windvane: workers don't start, re-run or enable GitHub workflows (or add gh aliases); "
             "`windvane ci [workflow]` starts one within the month's Actions minutes.")
if not REAL:
    print("gh: not found (windvane's guard found no gh on PATH when the session started)", file=sys.stderr)
    sys.exit(127)
os.execv(REAL, [REAL, *a])
"""


def is_guard(f):
    try:
        with open(f, "rb") as fh:
            return GUARD.encode() in fh.read(1 << 13)  # it follows the python's path, up to 4096 bytes
    except OSError:
        return False


def real_gh():
    """The first gh on PATH that isn't a guard, or None. `windvane ci` runs as a worker's command, under the guard's
    PATH: it starts its workflow with this, which is the guard dir taken off PATH (any project's, by what the file
    says), not a path the guard recorded, so it needs nothing from the session's state and works the same at the
    desk. Only that one call: the rest of windvane's gh calls, and the commands it runs for the session (`check`,
    `bg`, `heavy`), stay behind the guard."""
    for d in os.environ.get("PATH", "").split(os.pathsep):
        f = Path(d, "gh")
        if d and f.is_file() and os.access(f, os.X_OK) and not is_guard(f):
            return str(f)
    return None


def gh_guard(p, wd):
    """Environment for a worker session: on forge github, PATH led by a `gh` that refuses to start, re-run or enable
    a workflow and passes everything else to the real gh, unchanged. On with or without a budget: workflows run only
    by hand (`windvane ci`) is a rule of windvane, not of the budget. The Bash tool runs the user's shell setup, which
    may put another dir first; Claude Code sources CLAUDE_ENV_FILE after it, before each command, so the guard goes
    first there too (after the user's own env file, if they set one)."""
    if p.cfg["forge"] != "github":
        return {}
    folder = p.state / "gh-guard"
    folder.mkdir(parents=True, exist_ok=True)
    tmp = folder / f".gh.{os.getpid()}.{threading.get_ident()}"  # parallel slots write it at once: whole or not at all
    # sh starts it with this python: a shebang can't hold a path with spaces, nor one over 255 bytes (a deep venv)
    tmp.write_text(f"#!/bin/sh\n''''exec {shlex.quote(sys.executable)} \"$0\" \"$@\" # '''\n"
                   f"# {GUARD}: workers never start, re-run or enable a GitHub workflow.\n"
                   f"REAL = {real_gh()!r}\n{GUARD_SCRIPT}")
    tmp.chmod(0o755)
    os.replace(tmp, folder / "gh")
    own = os.environ.get("CLAUDE_ENV_FILE")
    env_file = folder / f"env-{Path(wd).resolve().name}"  # one per worktree: a session-start hook may append to it
    if own and Path(own).resolve() == env_file.resolve():  # started from a worker of this worktree: it'd source itself
        own = None
    env_file.write_text((f". {shlex.quote(own)}\n" if own else "")
                        + f'export PATH={shlex.quote(str(folder))}"{os.pathsep}$PATH"\n')
    return {"PATH": f"{folder}{os.pathsep}{os.environ.get('PATH', '')}", "CLAUDE_ENV_FILE": str(env_file)}


def ci(p, name="", ref="", dry=False):
    """`windvane ci [workflow]`: start a workflow with `gh workflow run`, but only when this month's minutes plus its
    estimate (its last finished run's, rounded up) stay within the cap. Prints what was used and what's left; returns
    the exit code."""
    cd = p.root
    repo = net.sh(cd, "gh", "repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner")
    if not repo:
        print("windvane ci: no GitHub repo here (or gh is missing, logged out or offline)")
        return 1
    b = budget(p)
    total = b and count(cd, max_age=60)
    if b and not total:
        print("windvane ci: couldn't count this month's Actions minutes (gh missing, logged out or offline), so no "
              "workflow starts")
        return 1
    if b:
        cap, used = cap_minutes(p), math.ceil(total["used"])
        print(f"Actions this month: {used:,} of {fmt(b[0])} min used; the cap is {cap:,} ({b[1]:.0%}), "
              f"{max(0, cap - used):,} left.")
    else:
        print("No Actions budget set (actions_minutes in .windvane/config.toml): the minutes aren't counted.")
    try:
        flow = workflow(cd, repo, name)
    except ValueError as e:
        print(f"windvane ci: {e}")
        return 1
    file = base(flow)
    if b:
        est = estimate(cd, repo, file)
        print(f"{file}: its last finished run billed {est:,} min." if est is not None else
              f"{file}: no finished run to estimate from; counted as 0 min.")
        est = est or 0
        if used + est > cap:
            print(f"Refused: {used:,} + {est:,} = {used + est:,} min would pass the cap of {cap:,}. Test it on this "
                  "machine, or leave it to the user.")
            return 1
        print(f"After it, about {cap - used - est:,} min left under the cap.")
    if flow.get("state") != "active":
        print(f"{file} is {flow.get('state', 'not active').replace('_', ' ')} on GitHub, so it can't start; "
              "turning it back on is the user's call.")
        return 1
    ref = ref or run(["git", "symbolic-ref", "-q", "--short", "HEAD"], cd).stdout.strip()
    cmd = ["gh", "workflow", "run", file, *(["--ref", ref] if ref else [])]
    if dry:
        print(f"Dry run: would run {' '.join(cmd)}")
        return 0
    r = net.call([real_gh() or "gh", *cmd[1:]], cd)
    if r.returncode:
        print(f"windvane ci: gh workflow run failed: {' '.join((r.stderr or r.stdout).split())[:300]}")
        return 1
    print(f"Started {file} on {ref or 'the default branch'}.")
    return 0
