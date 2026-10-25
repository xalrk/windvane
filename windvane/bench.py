"""`windvane bench`: build small seed projects with the real pipeline and score them on acceptance tests no session
sees. windvane's own critic judges windvane's own tickets, where revisions stop at the pass mark; this measures what
it exists for, the projects it builds, and what each merged ticket cost.

Each seed in `bench/<name>/` is `seed/` (the starting repo, tickets included) and `hidden/` (acceptance tests and
`bench.toml`: the command that runs them, and which test modules check which ticket). Per seed, a run makes a fresh
repo with a local bare remote and a state dir of its own under `<state root>/_bench/<run id>/<seed>/`, runs
`windvane run` there (the windvane of `--ref`, from a detached worktree of this checkout), then copies `hidden/` into a
scratch clone of the final `main` and runs it. The sessions are denied `bench/` and the reference solutions, have no
phone, gh or GitHub token, and every run stream is searched afterwards for the hidden files: a hit marks the seed's
result contaminated, reported but never counted.
"""
import ast
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
import tomllib
from datetime import datetime
from pathlib import Path

from .config import PERM_ROLES, Project, user_dir
from .heavy import machine
from .stats import k, ticket_outcomes, weighted
from .util import append_jsonl, gout, read_jsonl

NO_PHONE = "WINDVANE_NO_PHONE"  # "1": phone.py has no ntfy settings, so a seed run's pushes and questions go nowhere
BLOCKED = ("notify", "notify-ask", "gh")  # kept off a seed run's PATH: no phone, no GitHub
DROP_ENV = ("GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN")
WEEK_SMALL = 75  # machine.toml's `bench_week_small` default: over this weekly usage, a run is small
SMALL_SEEDS, SMALL_TICKETS = 2, 3
TEST_TIMEOUT = 600  # seconds the hidden tests of one seed may take
# Where a session could find what it must not see, relative to the checkout: the seeds' hidden tests and the
# reference solutions the suite checks them with.
DENIED = ("bench", "tests/fixtures/bench")
# unittest -v: a test's line (`test_x (module.Class.test_x)`), and its outcome at the end of it or of the docstring's
START = re.compile(r"^(\w+) \(([\w.]+)\)")
# (or alone on a line, when a warning the test caused was printed between the two)
OUTCOME = re.compile(r"(?:^| \.\.\. )(ok|FAIL|ERROR|skipped.*|expected failure|unexpected success)$")


def state_root():
    return user_dir("XDG_STATE_HOME", ".local/state")


def results(p):
    return p.state / "bench.jsonl"


def week_reading(files):
    """The newest weekly usage % among these usage.json files (every project's reads the same account), or None."""
    best = None
    for f in files:
        try:
            u = json.loads(Path(f).read_text())
        except (OSError, ValueError):
            continue
        if isinstance(u, dict) and u.get("week") is not None and (best is None or u.get("at", 0) > best[0]):
            best = (u.get("at", 0), u["week"])
    return best and best[1]


def week_now(extra=()):
    return week_reading([*state_root().glob("*/usage.json"), *extra])


def size(full, small, week, limit):
    """("full" or "small", why), from the flags or the latest weekly usage reading."""
    if full or small:
        return ("full", "--full") if full else ("small", "--small")
    if week is None:
        return "full", "no weekly usage reading yet"
    return ("small", f"weekly usage {week:g}% > {limit:g}%") if week > limit else (
        "full", f"weekly usage {week:g}% ≤ {limit:g}%")


def seeds(src):
    """{name: seed} for every `bench/<name>/` with a `seed/` and a `hidden/bench.toml`, by name."""
    out = {}
    for d in sorted(Path(src).iterdir()) if Path(src).is_dir() else []:
        if not ((d / "seed").is_dir() and (d / "hidden/bench.toml").is_file()):
            continue
        cfg = tomllib.loads((d / "seed/.windvane/config.toml").read_text())
        if cfg.get("forge") != "git" or "permissions" in cfg:
            raise SystemExit(f"bench: {d.name}'s seed config must say forge = \"git\" and set no [permissions] "
                             "(the bench sets them)")
        tickets = sorted(f.stem for f in (d / "seed/.windvane/tickets").glob("*.md"))
        out[d.name] = {"name": d.name, "dir": d, "tickets": tickets,
                       "bench": tomllib.loads((d / "hidden/bench.toml").read_text())}
    return out


def pick(found, which):
    """The seeds and tickets a run of this size works: full is all; small the first two seeds' first three tickets."""
    if which == "full":
        return [(s, s["tickets"]) for s in found.values()]
    return [(s, s["tickets"][:SMALL_TICKETS]) for s in list(found.values())[:SMALL_SEEDS]]


def deny_rules(bases):
    """Every role's deny rules for the checkout's (and the ref worktree's) hidden tests and reference solutions."""
    rules = []
    for base in bases:
        for rel in DENIED:
            d = Path(base) / rel
            rules += [f"Read(/{d}/**)", f"Edit(/{d}/**)", f"Bash(*{d}*)"]
    return rules


def permissions_toml(rules):
    lines = ["", "[permissions]  # windvane bench: the hidden tests stay hidden"]
    return "\n".join(lines + [f"{key}_deny = {json.dumps(rules)}" for key in PERM_ROLES]) + "\n"


def git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def make_repo(seed, tickets, home, rules):
    """`home`/repo from the seed (a ticket past `tickets` parked), pushed to `home`/remote.git; its Project, with the
    state dir the run's environment gives it."""
    repo, remote = home / "repo", home / "remote.git"
    shutil.copytree(seed["dir"] / "seed", repo)
    with open(repo / ".windvane/config.toml", "a") as f:
        f.write(permissions_toml(rules))
    parked = [t for t in seed["tickets"] if t not in tickets]
    if parked:
        with open(repo / ".windvane/QUEUE.md", "a") as f:
            f.writelines(f"- parked: {t} (left out of a small bench run)\n" for t in parked)
    ident = ["-c", "user.name=windvane bench", "-c", "user.email=bench@localhost"]
    git(home, "init", "-q", "--bare", "-b", "main", str(remote))
    git(repo, "init", "-q", "-b", "main")
    git(repo, "add", "-A")
    git(repo, *ident, "commit", "-qm", f"Seed: {seed['name']}")
    git(repo, "remote", "add", "origin", str(remote))
    git(repo, "push", "-q", "-u", "origin", "main")
    sp = Project(repo)
    sp.state = home / "state" / "windvane" / sp.name
    sp.state.mkdir(parents=True, exist_ok=True)
    # nobody to ask: every product question gets its recommendation built, as in a quiet time that never ends
    (sp.state / "quiet.txt").write_text("daily 00:00-00:00  # windvane bench: no one to answer\n")
    return sp


def clean_path(path, into):
    """PATH with every dir holding a BLOCKED tool replaced by a copy of it (symlinks) without that tool."""
    out = []
    for i, d in enumerate(filter(None, path.split(os.pathsep))):
        p = Path(d)
        if not any((p / b).exists() for b in BLOCKED):
            out.append(d)
            continue
        shim = into / str(i)
        shim.mkdir(parents=True, exist_ok=True)
        for f in p.iterdir():
            if f.name not in BLOCKED and not (shim / f.name).exists():
                (shim / f.name).symlink_to(f)
        out.append(str(shim))
    return os.pathsep.join(out)


def child_env(home, path):
    env = {k: v for k, v in os.environ.items() if k not in DROP_ENV and not k.startswith("WINDVANE_")}
    return {**env, "PATH": path, "XDG_STATE_HOME": str(home / "state"), NO_PHONE: "1", "WINDVANE_SELF_UPDATE": "0"}


def test_counts(files):
    """{module: number of tests} of the hidden test files, read from their source: a module that fails to import
    still counts all its tests as failed."""
    out = {}
    for f in files:
        tree = ast.parse(Path(f).read_text())
        out[Path(f).stem] = sum(isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name.startswith("test")
                                for c in tree.body if isinstance(c, ast.ClassDef) for n in c.body)
    return out


def outcomes(text):
    """{test id: outcome} from `unittest -v` output (Python 3.11+: the id is `module.Class.test`)."""
    out, cur = {}, None
    for line in text.splitlines():
        m = START.match(line)
        if m:
            cur = m.group(2) if m.group(2).endswith("." + m.group(1)) else f"{m.group(2)}.{m.group(1)}"
        o = OUTCOME.search(line)
        if o and cur:
            out[cur], cur = o.group(1), None
    return out


def score(seed, tickets, remote, scratch, env):
    """{ticket: {"passed", "total"}} of the hidden tests on the final main, run in a scratch clone of it."""
    git(scratch.parent, "clone", "-q", str(remote), str(scratch))
    return tally(seed, tickets, scratch, env)


def tally(seed, tickets, scratch, env=None):
    """Copy `hidden/` into `scratch` (never the repo the sessions used) and run it there: passed and total per
    ticket."""
    hidden = seed["dir"] / "hidden"
    for f in hidden.iterdir():
        if f.is_dir():
            shutil.copytree(f, scratch / f.name, dirs_exist_ok=True)
        elif f.name != "bench.toml":
            shutil.copy2(f, scratch / f.name)
    cmd = seed["bench"]["command"]
    try:
        r = subprocess.run(shlex.split(cmd) if isinstance(cmd, str) else cmd, cwd=scratch, capture_output=True,
                           text=True, env=env, timeout=seed["bench"].get("timeout", TEST_TIMEOUT))
        text = r.stdout + r.stderr
    except subprocess.TimeoutExpired as e:
        text = (e.stdout or b"").decode(errors="replace") + (e.stderr or b"").decode(errors="replace")
    (scratch.parent / "hidden.log").write_text(text)
    got, counts = outcomes(text), test_counts(hidden.glob("*.py"))
    out = {}
    for t in tickets:
        mods = seed["bench"]["tickets"].get(t, [])
        out[t] = {"passed": sum(v == "ok" for i, v in got.items() if any(i.startswith(m + ".") for m in mods)),
                  "total": sum(counts.get(m, 0) for m in mods)}
    return out


def needles(seed, bases):
    """What a run stream that saw the hidden tests would hold: their paths and file names, the references' path."""
    out = {f"bench/{seed['name']}/hidden", "fixtures/bench/reference"}
    out |= {str(Path(b) / "bench" / seed["name"] / "hidden") for b in bases}
    return sorted(out | {f.name for f in (seed["dir"] / "hidden").glob("*.py")})


def contamination(state, words):
    """[(file, word)]: each run stream of the seed's state dir naming a hidden path or file."""
    hits = []
    for f in sorted((state / "runs").glob("*")) if (state / "runs").is_dir() else []:
        try:
            text = f.read_text(errors="replace")
        except OSError:
            continue
        hits += [(f.name, w) for w in words if w in text]
    return hits


def costs(sp, tickets):
    """Weighted tokens and API $ of the seed run's sessions, in all and per ticket."""
    rows = [r for r in read_jsonl(sp.state / "ledger.jsonl") if not r.get("event")]
    per = {t: {"weighted": round(sum(weighted(r) for r in rows if r.get("ticket") == t)),
               "usd": round(sum(r.get("usd") or 0 for r in rows if r.get("ticket") == t), 4)} for t in tickets}
    return round(sum(map(weighted, rows))), round(sum(r.get("usd") or 0 for r in rows), 4), per


def fates(sp, tickets):
    """{"merged", "failed", "parked", "open"}: lists of the seed's tickets, from its queue and ledger."""
    subprocess.run(["git", "fetch", "-q", "origin"], cwd=sp.root, capture_output=True)
    got = ticket_outcomes(sp, {t: {"ticket": True} for t in tickets})
    return {f: [t for t in tickets if got.get(t) == f] for f in ("merged", "failed", "parked", "open")}


def table(recs):
    """The results of one run, a row per seed and a line of hidden tests per ticket."""
    r0 = recs[0]
    out = [f"bench {r0['run']}: {r0['ref']} ({r0['sha'][:7]}), {r0['size']}, weekly usage "
           f"{fmt(r0['week_before'])} → {fmt(r0['week_after'])}",
           f"{'seed':<12} {'merged':>6} {'failed':>6} {'parked':>6} {'hidden':>7} {'weighted':>9} {'API $':>7} "
           f"{'min':>5}"]
    for r in recs:
        t = r["tickets"]
        hidden = f"{r['passed']}/{r['total']}"
        out.append(f"{r['seed']:<12} {len(t['merged']):>6} {len(t['failed']):>6} {len(t['parked']):>6} "
                   f"{hidden:>7} {k(r['weighted']):>9} "
                   f"{r['usd']:>7.2f} {r['minutes']:>5.0f}" + ("  contaminated: not counted" if r["contaminated"]
                                                               else ""))
        out.append("  " + " · ".join(f"{tid} {h['passed']}/{h['total']}" for tid, h in r["hidden"].items()))
    good = [r for r in recs if not r["contaminated"]]
    if len(recs) > 1 and good:
        merged = sum(len(r["tickets"]["merged"]) for r in good)
        out.append(f"counted: {sum(r['passed'] for r in good)}/{sum(r['total'] for r in good)} hidden tests, "
                   f"{merged} merged, {k(sum(r['weighted'] for r in good))} weighted tokens"
                   + (f" ({k(sum(r['weighted'] for r in good) / merged)} per merged ticket)" if merged else ""))
    return "\n".join(out)


def fmt(v):
    return "?" if v is None else f"{v:g}%"


def runs_of(p):
    """{run id: [records]}, oldest first."""
    out = {}
    for r in read_jsonl(results(p)):
        out.setdefault(r.get("run"), []).append(r)
    return out


def earlier(p, which):
    """What an earlier run of this size cost, as a line."""
    runs = [rs for rs in runs_of(p).values() if rs[0].get("size") == which]
    if not runs:
        return f"earlier {which} runs: none yet, so no cost to go by"
    w = sum(r["weighted"] for rs in runs for r in rs) / len(runs)
    usd = sum(r["usd"] for rs in runs for r in rs) / len(runs)
    mins = sum(max(r["minutes"] for r in rs) for rs in runs) / len(runs)
    return (f"earlier {which} runs: {len(runs)}, each about {k(w)} weighted tokens, {usd:.2f} API $, "
            f"{mins:.0f} min")


def ref_worktree(p, ref, path):
    sha = gout(p.main_root(), "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
    if not sha:
        raise SystemExit(f"bench: no commit {ref!r} in this checkout")
    r = subprocess.run(["git", "worktree", "add", "-q", "--detach", str(path), sha], cwd=p.main_root(),
                       capture_output=True, text=True)
    if r.returncode:
        raise SystemExit(f"bench: can't make a worktree of {ref} at {path}: {r.stderr.strip()}")
    return sha


def drop_worktree(p, path):
    subprocess.run(["git", "worktree", "remove", "--force", str(path)], cwd=p.main_root(), capture_output=True)
    shutil.rmtree(path, ignore_errors=True)
    subprocess.run(["git", "worktree", "prune"], cwd=p.main_root(), capture_output=True)


def plan_lines(chosen, hours, ref, sha):
    seeds_ = f"{len(chosen)} seeds side by side" if len(chosen) > 1 else "1 seed"
    out = [f"plan: {seeds_}, up to {hours:g}h, windvane at {ref} ({sha[:7]})"]
    for s, ts in chosen:
        left = [t for t in s["tickets"] if t not in ts]
        out.append(f"  {s['name']}: {' '.join(ts)}" + (f" ({' '.join(left)} parked)" if left else ""))
    return out


def main(a, p):
    if a.last:
        runs = runs_of(p)
        return print(table(list(runs.values())[-1]) if runs else "bench: no results yet (windvane bench runs one)")
    limit = float(machine().get("bench_week_small", WEEK_SMALL))
    before = week_now()
    which, why = size(a.full, a.small, before, limit)
    print(f"bench: {which} ({why})", flush=True)
    run_id = f"{datetime.now():%Y%m%d-%H%M%S}"
    base = state_root() / "_bench" / run_id
    wt = base / "windvane"
    base.mkdir(parents=True)
    try:
        sha = ref_worktree(p, a.ref, wt)
        found = seeds(wt / "bench")
        if not found:
            raise SystemExit(f"bench: no seeds in bench/ at {a.ref}")
        chosen = pick(found, which)
        print("\n".join(plan_lines(chosen, a.hours, a.ref, sha)), flush=True)
        if a.dry_run:
            return print(earlier(p, which))
        return run(p, a, chosen, base, wt, sha, which, before)
    finally:
        drop_worktree(p, wt)
        if a.dry_run or not any(base.iterdir()):  # nothing ran: nothing to keep
            shutil.rmtree(base, ignore_errors=True)


def run(p, a, chosen, base, wt, sha, which, before):
    bases = [p.main_root(), wt]
    exe = [str(wt / "bin/windvane")]
    first = base / "bin/ref"  # the sessions' own `windvane check`, `critic` and `bg` are the ref's too
    first.mkdir(parents=True)
    (first / "windvane").symlink_to(exe[0])
    rules = deny_rules(bases)
    path = os.pathsep.join([str(first), clean_path(os.environ.get("PATH", ""), base / "bin")])
    caps = [x for k_, v in (("--cap", a.cap), ("--week-cap", a.week_cap)) if v is not None for x in (k_, f"{v:g}")]
    procs = []
    for s, ts in chosen:
        home = base / s["name"]
        home.mkdir()
        sp = make_repo(s, ts, home, rules)
        env = child_env(home, path)
        log = open(home / "run.log", "w")
        cmd = [*exe, "-C", str(sp.root), "run", "--for", f"{round(a.hours * 60)}m", "--no-preflight", *caps]
        procs.append((s, ts, sp, home, env, time.time(),
                      subprocess.Popen(cmd, cwd=sp.root, env=env, stdin=subprocess.DEVNULL, stdout=log,
                                       stderr=subprocess.STDOUT)))
        print(f"{s['name']}: running, log in {home / 'run.log'}", flush=True)
    ended = {}
    try:
        for s, *_, began, proc in procs:
            proc.wait()
            ended[s["name"]] = time.time()
    finally:  # a Ctrl-C ends every seed's run the way `windvane stop --now` would, before the worktree goes
        for s, *_, proc in procs:
            if proc.poll() is None:
                proc.terminate()
                proc.wait()
    after = week_now([sp.state / "usage.json" for _, _, sp, *_ in procs])
    recs = []
    for s, ts, sp, home, env, began, proc in procs:
        weighted_, usd, per = costs(sp, ts)
        hits = contamination(sp.state, needles(s, [p.main_root(), wt]))
        hidden = score(s, ts, home / "remote.git", home / "score", env)
        tickets = fates(sp, ts)
        rec = {"run": base.name, "ref": a.ref, "sha": sha, "size": which, "seed": s["name"], "tickets": tickets,
               "hidden": hidden, "passed": sum(h["passed"] for h in hidden.values()),
               "total": sum(h["total"] for h in hidden.values()), "weighted": weighted_, "usd": usd,
               "per_merged": {t: per[t] for t in tickets["merged"]},
               "minutes": round((ended.get(s["name"], time.time()) - began) / 60, 1), "week_before": before,
               "week_after": after, "contaminated": bool(hits), "hits": [f"{f}: {w}" for f, w in hits[:5]],
               "exit": proc.returncode}
        append_jsonl(results(p), rec)
        recs.append(rec)
    print(table(recs))
    return 0
