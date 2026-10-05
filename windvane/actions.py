"""The month's GitHub Actions minutes, counted from the run history, and the cap windvane keeps them under.

windvane is the only thing that spends the user's Actions minutes, so it counts them itself instead of asking the
billing API (which needs gh's `user` scope): every job of every run created this calendar month (UTC) in the login's
repos, each rounded up to the minute, times 10 on macOS runners and 2 on Windows ones; self-hosted runners are free.
A desk count done this way matched GitHub's own figure. A running job counts up to now, a cancelled one up to its end.

Off unless `.windvane/config.toml` sets `actions_minutes` (the plan's monthly minutes); `actions_cap` (0.9) is the
share of them windvane never goes past: `windvane ci` refuses a workflow whose estimate would, and at or over it the
driver adds `[skip ci]` to the commits it pushes.
"""
import json
import math
import time
from datetime import datetime, timezone

from . import net
from .config import user_dir
from .util import load_json, log, push, run, save_json

MULT = (("self-hosted", 0), ("macos", 10), ("windows", 2))  # the first label that matches sets the rate
FRESH_S = 600  # a total this young is reused: status and every merge would otherwise list every repo's runs again
SKIP = "[skip ci]"


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


def run_minutes(cd, repo, run, now):
    """A run's billed minutes, every attempt's jobs (a re-run is billed again)."""
    jobs = pages(cd, f"repos/{repo}/actions/runs/{run['id']}/jobs?filter=all", "jobs")
    return None if jobs is None else sum(job_minutes(j, now) for j in jobs)


def repos(cd):
    """The login's repos, owner/name: the only place its minutes can go."""
    r = net.call(["gh", "repo", "list", "--limit", "1000", "--json", "nameWithOwner", "-q", ".[].nameWithOwner"], cd)
    return r.stdout.split() if r.returncode == 0 else None


def billed(cd, cache, repo, run, now):
    """{repo, id, workflow, minutes} for a run: from the cache once it finished, else fetched (and cached if done)."""
    key = f"{repo}#{run['id']}"
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


def count(cd, now=None, max_age=FRESH_S):
    """{"used": minutes billed this month, "at": when} or None when the count failed. A finished run is fetched once
    and kept in the cache; running ones are fetched each time."""
    now = now or datetime.now(timezone.utc)
    m, cache = month(now), load(now)
    if cache.get("total") and time.time() - cache["total"]["at"] < max_age:
        return cache["total"]
    names = repos(cd)
    if names is None:
        return None
    used = 0
    for repo in names:
        # Newest first, until last month's: the API's `created` filter stops at 1,000 runs, this doesn't.
        runs = pages(cd, f"repos/{repo}/actions/runs", "workflow_runs",
                     lambda run: month(stamp(run.get("created_at")) or now) < m)
        for run in runs if runs is not None else [None]:
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
    cache = load(now)
    done = billed(cd, cache, repo, runs[0], now)
    save_json(cache_file(), cache)
    return done and done["minutes"]


def cap_minutes(p):
    minutes, cap = budget(p)
    return math.floor(minutes * cap)


def fmt(n):
    return f"{math.ceil(n):,}"


def line(p, total=None):
    """`Actions 1,983/2,000 min` for status and overview, or "" with the budget off or nothing counted yet."""
    b = budget(p)
    total = total or (cached() if b else None)
    return f"Actions {fmt(total['used'])}/{fmt(b[0])} min" if b and total else ""


def over_cap(p, cd=None):
    """Whether the month's minutes are at or over the cap. A count that fails falls back on the last one, and with
    none at all says no: workflows run only by hand (`windvane ci`), so `[skip ci]` is a backstop, not the guard."""
    if not budget(p):
        return False
    total = count(cd or p.root) or cached()
    return bool(total) and total["used"] >= cap_minutes(p)


logged = set()  # the projects whose log has said "at the cap" in this process: once a run, not once a commit


def tag(p, msg, cd=None):
    """`msg` with `[skip ci]` added when the month's minutes are at or over the cap, in case a workflow still runs on
    push. Logged once a run; the first time in a month, one phone push."""
    if SKIP in msg or not over_cap(p, cd):
        return msg
    if p.name not in logged:
        logged.add(p.name)
        log(p, f"GitHub Actions at the cap ({line(p)}, cap {cap_minutes(p):,}): the driver's commits carry {SKIP}")
    cache = load(None)
    if cache.get("warned") != cache["month"]:
        cache["warned"] = cache["month"]
        save_json(cache_file(), cache)
        push("Actions minutes at the cap", f"{line(p)} used this month (cap {cap_minutes(p):,}). windvane adds "
             f"{SKIP} to its commits and `windvane ci` refuses workflows until the month ends.", 4, "warning")
    return f"{msg} {SKIP}"


def now_line(p):
    """status and overview's `Actions ...` line: a count at most FRESH_S old, or the last one when it fails."""
    return line(p, count(p.root) or cached()) if budget(p) else ""


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
    r = net.call(cmd, cd)
    if r.returncode:
        print(f"windvane ci: gh workflow run failed: {' '.join((r.stderr or r.stdout).split())[:300]}")
        return 1
    print(f"Started {file} on {ref or 'the default branch'}.")
    return 0
