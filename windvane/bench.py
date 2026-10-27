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

After the hidden tests, a judge (one fresh session per seed, `prompts/JUDGE.md`) tries a copy of the final tree as
its user would, on the seed's `hidden/JUDGE.md` checks. It gets the seed's goal and tickets and nothing of how the
project was built (no critic scale or verdicts, pass mark, run log, ledger or windvane ref), so its yes count is a
score no session could aim at. Its tokens are the bench's own, kept out of the seed's cost per merged ticket.

Two sets of seeds: `bench/<name>/` is the frozen holdout, run by hand and in the weekly trend, never what an
automatic decision rests on; `bench/pool/<name>/` the training pool (`--pool`), which a project with a `seed` round
([[periodic]] `builtin = "seed"`, pool.py) benches its experiments and regressions on. `overfit` reads the two
against each other: the pool rising while the holdout falls is the loop learning its seeds, not how to build projects.

`bench compare A B` sets two windvane refs (or earlier runs) side by side, running only what isn't recorded yet.
`experiment_verdict` reads one such pair as an experiment ticket's keep or reject (experiment.py).
How a seed is scored and how two sides are compared live in `bench_score.py`, a protected path.

One bench runs at a time on a machine, whichever project or caller started it (`take_lock`): two side by side would
need twice the memory and slow each other, so neither's minutes would mean anything.
"""
import copy
import fcntl
import itertools
import json
import os
import shutil
import signal
import subprocess
import sys
import time
import tomllib
from datetime import datetime, timedelta
from pathlib import Path

# the scoring, and what callers (`periodic`, the tests) read through this module
from . import budget
from .bench_score import (NOISE, QUALITY, answers, compare_table, counted, headline, judge_checks,  # noqa: F401
                          judge_failed, judged, measure, num, outcomes, past_noise, regression, runs_n, score,
                          summary, tally, yeses)
from .config import PERM_ROLES, POOL, Project, user_dir
from .heavy import JOB_ENV, machine
from .runner import rules as session_rules, run_claude
from .stats import k, ticket_outcomes, weighted
from .util import append_jsonl, gout, hhmm, read_jsonl

NO_PHONE = "WINDVANE_NO_PHONE"  # "1": phone.py has no ntfy settings, so a seed run's pushes and questions go nowhere
BLOCKED = ("notify", "notify-ask", "gh")  # kept off a seed run's PATH: no phone, no GitHub
DROP_ENV = ("GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN")
WEEK_SMALL = 75  # machine.toml's `bench_week_small` default: over this weekly usage, a run is small
SMALL_SEEDS, SMALL_TICKETS = 2, 3
# Where a session could find what it must not see, relative to the checkout: the seeds' hidden tests and the
# reference solutions the suite checks them with.
DENIED = ("bench", "tests/fixtures/bench")
JUDGE_MINUTES = 30  # a judge still going after this is stopped: the checks it hasn't answered count as no
LOCK = "bench.lock"  # in the state root, so one for every project on the machine
WAIT_HINT = "windvane bench --status to check; windvane stop or Ctrl-C ends it"  # a background bench can't be Ctrl-C'd


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


def where(pool):
    """The folder of a set of seeds, relative to the checkout: the training pool's, or the holdout's."""
    return POOL if pool else "bench"


def set_of(r):
    """A record's set of seeds: "pool" or "holdout" (every run from before the pool)."""
    return "pool" if r.get("pool") else "holdout"


def seed_entry(p):
    """This project's `seed` round ([[periodic]] `builtin = "seed"`), or None."""
    return next((e for e in p.periodic.values() if e["builtin"] == "seed"), None)


def training(p):
    """Whether this project's automatic benches (an experiment's gate, the bench round) run on the training pool:
    once it has a `seed` round to fill it. Without one, they run on `bench/`'s seeds, as before there was a pool."""
    return seed_entry(p) is not None


def pool_short(p):
    """Why the training pool can't decide an experiment yet ("" when it can, or there's no pool): fewer seeds on main
    than the seed round's `pool_size`. Never the holdout instead: that would train on it."""
    e = seed_entry(p)
    have = len(planned(p, p.upstream, "full", True)) if e else 0
    return f"the training pool has {have} of its {e['pool_size']} seeds" if e and have < e["pool_size"] else ""


def seeds(src):
    """{name: seed} for every `<src>/<name>/` with a `seed/` and a `hidden/bench.toml`, by name."""
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
    """Every role's deny rules for the checkout's (and the ref worktree's) hidden tests and reference solutions: their
    absolute paths for every tool, and any shell command naming a seed's hidden folder or the references, however
    it gets there. A way round these (a glob, a `cd` first) is what the run streams are searched for afterwards."""
    rules = ["Bash(*bench/*/hidden*)", "Bash(*fixtures/bench*)"]
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










def needles(seed, bases):
    """What a run stream that saw the hidden tests would hold: their paths and file names, the references' path."""
    rel = f"{seed['dir'].parent.name}/{seed['name']}" if seed["dir"].parent.name == "pool" else seed["name"]
    out = {f"bench/{rel}/hidden", "fixtures/bench/reference"}
    out |= {str(Path(b) / "bench" / rel / "hidden") for b in bases}
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


def goal(seed):
    """What the seed is for, as its CLAUDE.md opens (up to its first section), else its README."""
    for name in ("CLAUDE.md", "README.md", "README"):
        f = seed["dir"] / "seed" / name
        if f.is_file():
            return f.read_text().split("\n## ")[0].strip()
    return seed["name"]




def judge_prompt(seed, tickets, checks):
    """The goal, the tickets this run worked, the checks: nothing else of the run."""
    parts = ["Judge the project in your working folder.", "## Its goal", goal(seed),
             "## The tickets it was built from"]
    parts += [(seed["dir"] / "seed/.windvane/tickets" / f"{t}.md").read_text().strip() for t in tickets]
    return "\n\n".join(parts + ["## The checks", "\n".join(f"- {i}: {c}" for i, c in checks)])




def judge_tree(remote, dest):
    """The final main as its user gets it: the files, without git history or `.windvane/` (tickets, critic scale,
    questions and their answers)."""
    git(dest.parent, "clone", "-q", str(remote), str(dest))
    for d in (".git", ".windvane"):
        shutil.rmtree(dest / d, ignore_errors=True)


def judge(sp, seed, tickets, checks, home, deny, env):
    """The judge's verdict on one seed's final main, or None when the seed has no checks. It runs in a folder of
    its own beside the seeds' (`<run>/judge/<seed>/`), denied every seed's home (where the critic's verdicts and
    the run's streams are) and the ref's worktree, with a state dir of its own: its tokens never reach the seed's
    ledger."""
    if not checks:
        return None
    own = home.parent / "judge" / seed["name"]
    own.mkdir(parents=True)
    tree = own / "tree"
    jp = copy.copy(sp)
    jp.state = own / "state"
    jp.state.mkdir()
    for _ in range(2):  # a session that failed (an API error, a crash) is the judge's, never the project's: once more
        shutil.rmtree(tree, ignore_errors=True)  # in a fresh copy: never what an attempt's trial left behind
        judge_tree(home / "remote.git", tree)
        until = time.time() + JUDGE_MINUTES * 60
        text, stopped_, error = run_claude(jp, "judge", f"judge-{seed['name']}",
                                           judge_prompt(seed, tickets, checks), session_rules(jp, "JUDGE.md"), tree,
                                           deny=deny, stop_if=lambda: "time" if time.time() > until else None,
                                           extra_env=env)
        if not error:
            break
    said = answers(text, checks)
    rows = [r for r in read_jsonl(jp.state / "ledger.jsonl") if not r.get("event")]
    return {"yes": sum(x["answer"] == "yes" for x in said), "total": len(checks), "answers": said,
            "weighted": round(sum(map(weighted, rows))), "usd": round(sum(r.get("usd") or 0 for r in rows), 4),
            "stopped": stopped_, "error": error}


def judge_deny(rules, base, chosen):
    """The seed sessions' deny rules, and every seed's home and the ref's worktree in this run's dir."""
    out = list(rules)
    for d in [base / s["name"] for s, _ in chosen] + [base / "windvane"]:
        out += [f"Read(/{d}/**)", f"Edit(/{d}/**)", f"Bash(*{d}/*)"]
    return out


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
           f"{'seed':<12} {'merged':>6} {'failed':>6} {'parked':>6} {'hidden':>7} {'judge':>6} {'weighted':>9} "
           f"{'API $':>7} {'min':>5}"]
    for r in recs:
        t = r["tickets"]
        hidden = f"{r['passed']}/{r['total']}"
        out.append(f"{r['seed']:<12} {len(t['merged']):>6} {len(t['failed']):>6} {len(t['parked']):>6} "
                   f"{hidden:>7} {yeses(r):>6} {k(r['weighted']):>9} "
                   f"{r['usd']:>7.2f} {r['minutes']:>5.0f}" + ("  contaminated: not counted" if r["contaminated"]
                                                               else ""))
        out.append("  " + " · ".join(f"{tid} {h['passed']}/{h['total']}" for tid, h in r["hidden"].items()))
        out += [f"  judge no: {x['id']} {x['reason']}" for x in (judged(r) or {}).get("answers", [])
                if x["answer"] != "yes"]
        if judge_failed(r):
            out.append(f"  judge failed: {judge_failed(r)} (no verdict: a compare won't reuse this run)")
    good = [r for r in recs if not r["contaminated"]]
    if len(recs) > 1 and good:
        merged = sum(len(r["tickets"]["merged"]) for r in good)
        out.append(f"counted: {sum(r['passed'] for r in good)}/{sum(r['total'] for r in good)} hidden tests, "
                   f"{merged} merged, {k(sum(r['weighted'] for r in good))} weighted tokens"
                   + (f" ({k(sum(r['weighted'] for r in good) / merged)} per merged ticket)" if merged else ""))
    ran = [r["judge"] for r in recs if r.get("judge")]
    if ran:
        out.append(f"the judge's own cost, not in the above: {k(sum(j['weighted'] for j in ran))} weighted tokens, "
                   f"{sum(j['usd'] for j in ran):.2f} API $")
    return "\n".join(out)








def experiment_verdict(both, saving):
    """An experiment's head (B) against main (A), past the noise rule: (kept, `hidden 40→39, judge 21→21,
    tokens/ticket +4%`, why), kept None when no seed was counted on both sides. Kept when neither hidden tests nor
    judge yeses drop and one rises, or both hold and weighted tokens per merged ticket fall by `saving` % (past the
    spread of either side's runs, with repeats). A side with no judge verdict holds its judge number."""
    per, common = counted(both)
    if not common:
        return None, "no seed counted on both sides", "nothing to compare"
    repeats = max(len(s["runs"]) for s in both) > 1
    parts, down, up = [], [], []
    for key, label in QUALITY:
        (a, b), spread, d = measure(per, common, key)
        parts.append(f"{key} {num(a, key)}→{num(b, key)}")
        if past_noise(d, spread, repeats):
            (up if d > 0 else down).append(label)
    (a, _), spread, d = measure(per, common, "per_merged")
    parts.append(f"tokens/ticket {d / a:+.0%}" if a and d is not None else "tokens/ticket -")
    cheaper = bool(a) and d is not None and -d >= a * saving / 100 and (not repeats or -d > spread)
    numbers = ", ".join(parts)
    if down:
        return False, numbers, f"{' and '.join(down)} down"
    if up:
        return True, numbers, f"{' and '.join(up)} up"
    if cheaper:
        return True, numbers, f"quality held, weighted tokens per merged ticket down {-d / a:.0%}"
    return False, numbers, (f"quality held, tokens per merged ticket not down {saving:g}%" if a and d is not None
                            else "quality held, and no cost per merged ticket on both sides")


def fmt(v):
    return "?" if v is None else f"{v:g}%"


def runs_of(p):
    """{run id: [records]}, oldest first."""
    out = {}
    for r in read_jsonl(results(p)):
        out.setdefault(r.get("run"), []).append(r)
    return out


def earlier(p, which, pool=False):
    """What an earlier run of this size and set cost, as a line."""
    want = "pool" if pool else "holdout"
    runs = [rs for rs in runs_of(p).values() if rs[0].get("size") == which and set_of(rs[0]) == want]
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


# What a bench run's numbers rest on in the measured worktree: the seeds and their hidden tests, and the scoring.
YARDSTICK = ("bench", "windvane/bench_score.py")


def yardstick_changed(wt, sha):
    """The files of YARDSTICK in the worktree `wt` that differ from `sha`'s committed tree (edited, added, deleted or
    committed on top), sorted: hidden tests or scoring changed after the worktree was made would score a run on
    something other than the ref's own yardstick."""
    diff = subprocess.run(["git", "diff", "--name-only", "--no-renames", sha, "--", *YARDSTICK], cwd=wt,
                          capture_output=True, text=True)
    new = subprocess.run(["git", "ls-files", "--others", "--exclude-standard", "--", *YARDSTICK], cwd=wt,
                         capture_output=True, text=True)
    if diff.returncode or new.returncode:
        return [f"(git can't tell: {(diff.stderr or new.stderr).strip()[:200]})"]
    return sorted(set((diff.stdout + new.stdout).split()))


def refuse_changed(wt, sha):
    """Stop the bench, saying why, rather than score with a yardstick that isn't `sha`'s committed one."""
    changed = yardstick_changed(wt, sha)
    if changed:
        more = f" and {len(changed) - 5} more" if len(changed) > 5 else ""
        raise SystemExit(f"bench: refusing to score: {', '.join(changed[:5])}{more} in the measured worktree {wt} "
                         f"differ from {sha[:7]}'s committed tree; hidden tests and scoring must be the ref's own")


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


class Stopped(Exception):
    """A SIGTERM or SIGHUP (a closed terminal, a dropped ssh): ended like a Ctrl-C."""


def stopped(sig, _frame):
    raise Stopped(signal.Signals(sig).name)


def new_run(root):
    """A fresh run dir: its id is the time, with -2, -3 ... for a second bench started in the same second."""
    stamp = f"{datetime.now():%Y%m%d-%H%M%S}"
    for n in itertools.count(1):
        base = root / (stamp if n == 1 else f"{stamp}-{n}")
        try:
            base.mkdir(parents=True)
            return base
        except FileExistsError:
            continue


def end_all(procs, wait=300):
    """End every seed run still going the way a signal ends `windvane run` (its sessions with it), killing one that
    hasn't ended after `wait` seconds; a second signal meanwhile doesn't cut this short."""
    old = {s: signal.signal(s, signal.SIG_IGN) for s in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)}
    try:
        live = [proc for *_, proc in procs if proc.poll() is None]
        for proc in live:
            proc.terminate()
        for proc in live:
            try:
                proc.wait(wait)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
    finally:
        for s, h in old.items():
            signal.signal(s, h)


def lock_file():
    return state_root() / LOCK


def holder():
    """The bench holding the machine's bench lock ({"project", "what", "pid", "since"}; {} in the instant before it
    writes them), or None. The lock is a flock, so it is free the moment its process is gone, whatever the file says."""
    f = lock_file()
    try:
        fd = os.open(f, os.O_RDONLY)
    except OSError:
        return None
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
            return None
        except OSError:
            pass
        try:
            h = json.loads(f.read_text() or "{}")
        except (OSError, ValueError):
            return {}
        return h if isinstance(h, dict) else {}
    finally:
        os.close(fd)


def started_by():
    """What this bench is for, from the background job running it: an experiment's ticket, a periodic round, or
    "" (by hand)."""
    job = Path(os.environ.get(JOB_ENV, "")).name
    if job.startswith("experiment-"):
        return job.removeprefix("experiment-")
    return f"{job.removeprefix('periodic-')} round" if job.startswith("periodic-") else ""


def describe(h):
    """A lock holder for people: "windvane T-321's bench, running since 01:40", "windvane weekly round's bench"."""
    what = " ".join(x for x in (h.get("project") or "another project", h.get("what")) if x)
    since = h.get("since")
    return f"{what}'s bench" + (f", running since {hhmm(datetime.fromtimestamp(since))}" if since else "")


def status_line():
    h = holder()
    return f"bench: {describe(h)} (pid {h.get('pid', '?')})" if h is not None else "bench: none running on this machine"


def take_lock(p):
    """Wait for the machine's bench lock and take it: the open file, held until it is closed or this process ends.
    A wait says so once; a signal during it (Stopped, KeyboardInterrupt) leaves the holder's file as it was."""
    f = lock_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(f, os.O_CREAT | os.O_RDWR, 0o600)  # not truncated on open: the holder's line is in it
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            if (h := holder()) is not None:
                print(f"bench: waiting for {describe(h)} ({WAIT_HINT})", flush=True)
            fcntl.flock(fd, fcntl.LOCK_EX)
    except BaseException:
        os.close(fd)
        raise
    os.ftruncate(fd, 0)
    os.pwrite(fd, json.dumps({"project": p.name, "what": started_by(), "pid": os.getpid(),
                              "since": time.time()}).encode(), 0)
    return fd


def drop_lock(fd):
    os.ftruncate(fd, 0)
    os.close(fd)


def locked(p, a, go):
    """`go()` holding the machine's bench lock (a dry run runs no seed, so it needs none). A stop while waiting ends
    the wait as a stop before any seed ran: nothing ran, so nothing failed."""
    if a.dry_run:
        return go()
    old = {s: signal.signal(s, stopped) for s in (signal.SIGTERM, signal.SIGHUP)}
    try:
        fd = take_lock(p)
    except (KeyboardInterrupt, Stopped) as e:
        print(f"bench: stopped ({str(e) or 'SIGINT'}) while waiting for the machine's bench: nothing ran",
              file=sys.stderr)
        return 130
    finally:
        for s, h in old.items():
            signal.signal(s, h)
    try:
        return go()
    finally:
        drop_lock(fd)


def main(a, p):
    if getattr(a, "status", False):
        return print(status_line())
    if a.repeat != 1 and not a.what:
        raise SystemExit("bench: --repeat goes with `windvane bench compare <A> <B>`")
    if a.what:
        if a.what[0] != "compare" or len(a.what) != 3:
            raise SystemExit("bench: the only subcommand is `windvane bench compare <A> <B>`")
        return locked(p, a, lambda: compare(a, p, *a.what[1:]))
    if a.last:
        runs = runs_of(p)
        return print(table(list(runs.values())[-1]) if runs else "bench: no results yet (windvane bench runs one)")

    def go():  # once the lock is held: a bench that waited reads the week as it is after the other one's spend
        before = week_now()
        which, why = size(a.full, a.small, before, week_limit())
        print(f"bench: {which} ({why}), {'the training pool' if a.pool else 'the holdout'}", flush=True)
        return one(p, a, a.ref, which, before)[0]
    return locked(p, a, go)


def week_limit():
    return float(machine().get("bench_week_small", WEEK_SMALL))


def one(p, a, ref, which, before):
    """One bench run of windvane at `ref`: (exit code, its records)."""
    base = new_run(state_root() / "_bench")
    wt = base / "windvane"
    old = {s: signal.signal(s, stopped) for s in (signal.SIGTERM, signal.SIGHUP)}
    try:
        sha = ref_worktree(p, ref, wt)
        refuse_changed(wt, sha)
        found = seeds(wt / where(a.pool))
        if not found:
            raise SystemExit(f"bench: no seeds in {where(a.pool)}/ at {ref}")
        chosen = pick(found, which)
        print("\n".join(plan_lines(chosen, a.hours, ref, sha)), flush=True)
        if a.dry_run:
            print(earlier(p, which, a.pool))
            return 0, []
        return run(p, a, chosen, base, wt, ref, sha, which, before)
    except (KeyboardInterrupt, Stopped):  # before any seed ran
        print("bench: stopped before any seed ran", file=sys.stderr)
        return 130, []
    finally:
        drop_worktree(p, wt)
        if a.dry_run or not any(base.iterdir()):  # nothing ran: nothing to keep
            shutil.rmtree(base, ignore_errors=True)
        for s, h in old.items():
            signal.signal(s, h)


def run(p, a, chosen, base, wt, ref, sha, which, before):
    bases = [p.main_root(), wt]
    exe = [str(wt / "bin/windvane")]
    first = base / "bin/ref"  # the sessions' own `windvane check`, `critic` and `bg` are the ref's too
    first.mkdir(parents=True)
    (first / "windvane").symlink_to(exe[0])
    rules = deny_rules(bases)
    clean = clean_path(os.environ.get("PATH", ""), base / "bin")
    path = os.pathsep.join([str(first), clean])
    caps = [x for k_, v in (("--cap", a.cap), ("--week-cap", a.week_cap)) if v is not None for x in (k_, f"{v:g}")]
    procs = []
    recs = []
    started, start = time.time(), budget.reading()
    budget.begin(base, which, start)
    try:
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
        for s, *_, began, proc in procs:
            proc.wait()
            ended[s["name"]] = time.time()
        after = week_now([sp.state / "usage.json" for _, _, sp, *_ in procs])
        refuse_changed(wt, sha)  # the seed runs are over: nothing is scored on a yardstick they could have edited
        for s, ts, sp, home, env, began, proc in procs:
            weighted_, usd, per = costs(sp, ts)
            hits = contamination(sp.state, needles(s, [p.main_root(), wt]))
            hidden = score(s, ts, home / "remote.git", home / "score", env)
            tickets = fates(sp, ts)
            # the judge has no windvane of the ref on its PATH, and a state dir of its own
            verdict = judge(sp, s, ts, judge_checks(s, p.main_root()), home, judge_deny(rules, base, chosen),
                            {**{t: "" for t in DROP_ENV}, "PATH": clean, NO_PHONE: "1",
                             "XDG_STATE_HOME": str(base / "judge" / s["name"] / "xdg")})
            rec = {"run": base.name, "ref": ref, "sha": sha, "size": which, "seed": s["name"], "tickets": tickets,
                   "hidden": hidden, "passed": sum(h["passed"] for h in hidden.values()),
                   "total": sum(h["total"] for h in hidden.values()), "judge": verdict, "weighted": weighted_,
                   "usd": usd, "per_merged": {t: per[t] for t in tickets["merged"]},
                   "minutes": round((ended.get(s["name"], time.time()) - began) / 60, 1), "week_before": before,
                   "week_after": after, "contaminated": bool(hits), "hits": [f"{f}: {w}" for f, w in hits[:5]],
                   "exit": proc.returncode, **({"pool": True} if a.pool else {})}
            append_jsonl(results(p), rec)
            recs.append(rec)
    except (KeyboardInterrupt, Stopped) as e:  # Ctrl-C, or a closed terminal: no seed run goes on alone
        end_all(procs)
        print(f"bench: stopped ({str(e) or 'SIGINT'}): every seed run was ended; {len(recs)} of {len(procs)} seeds "
              f"scored{' (see bench --last)' if recs else ''}; their logs are in {base}", file=sys.stderr)
        return 130, recs
    finally:  # what it spent counts against the experiment budget, however it ended
        budget.record(base, started, start, which)
    print(table(recs))
    return 0, recs




def planned(p, sha, which, pool=False):
    """The seeds of a set a run of this size at `sha` works, by name, read from git: what makes a recorded run
    reusable."""
    names = (gout(p.main_root(), "ls-tree", "-d", "--name-only", f"{sha}:{where(pool)}") or "").split()
    has = lambda path: subprocess.run(["git", "cat-file", "-e", f"{sha}:{where(pool)}/{path}"], cwd=p.main_root(),
                                      capture_output=True).returncode == 0
    names = sorted(n for n in names if has(f"{n}/seed") and has(f"{n}/hidden/bench.toml"))
    return names if which == "full" else names[:SMALL_SEEDS]


def sides(p, refs, which, runs, pool=False):
    """Each side of a compare: an earlier run's id (that run, fixed) or a ref, with its runs already recorded at the
    same sha, size and set over the same seeds, newest last."""
    out = []
    for name in refs:
        if name in runs:
            out.append({"name": name, "sha": runs[name][0]["sha"], "runs": [runs[name]], "fixed": True})
            continue
        sha = gout(p.main_root(), "rev-parse", "--verify", "--quiet", f"{name}^{{commit}}")
        if not sha:
            raise SystemExit(f"bench compare: {name!r} is neither a commit nor an earlier run's id")
        want = planned(p, sha, which, pool)
        have = [rs for rs in runs.values() if rs[0].get("sha") == sha and rs[0].get("size") == which
                and set_of(rs[0]) == ("pool" if pool else "holdout") and sorted(r["seed"] for r in rs) == want]
        out.append({"name": name, "sha": sha, "runs": [rs for rs in have if not any(map(judge_failed, rs))],
                    "failed": sum(any(map(judge_failed, rs)) for rs in have), "fixed": False})
    return out


def compare(a, p, *refs):
    """Run what's missing of N runs a side (one size for both), then print them side by side."""
    if a.repeat < 1:
        raise SystemExit("bench compare: --repeat is a number of runs, 1 or more")
    runs = runs_of(p)
    fixed = {runs[r][0]["size"] for r in refs if r in runs}
    if len(fixed) > 1:
        raise SystemExit("bench compare: those runs are of different sizes (full, small): compare runs of one size")
    if fixed:
        which, why = fixed.pop(), "the size of the earlier run compared"
    else:
        which, why = size(a.full, a.small, week_now(), week_limit())
    print(f"bench compare: {which} ({why})", flush=True)
    both = sides(p, refs, which, runs, a.pool)
    taken = set()
    for s in both:  # a ref compared with itself gets runs of its own on each side
        if not s["fixed"]:
            s["runs"] = [rs for rs in s["runs"] if rs[0]["run"] not in taken][-a.repeat:]
            taken |= {rs[0]["run"] for rs in s["runs"]}
    todo = [s for i in range(a.repeat) for s in both if not s["fixed"] and len(s["runs"]) <= i]  # A, B, A, B ...
    for s in both:
        print(f"{s['name']} ({s['sha'][:7]}): {runs_n(len(s['runs']))} recorded, "
              f"{sum(t is s for t in todo)} to run" + (f" ({runs_n(s['failed'])} not reused: a judge failed)"
                                                       if s.get("failed") else ""), flush=True)
    if a.dry_run:
        return 0
    for i, s in enumerate(todo, 1):
        print(f"bench compare: run {i} of {len(todo)}, {s['name']}", flush=True)
        code, recs = one(p, a, s["name"], which, week_now())
        if code:
            return code
        s["runs"].append(recs)
    print(compare_table(both, which))
    return 0


WEEK_RUNS = 7  # days of runs the overfit alarm reads: the weekly trend's


def drift(first, last):
    """Where one set's quality went from its `first` run to its `last` (each a run's records), over the seeds both
    counted, by the noise rule: ("up", "down" or "flat", or None when no seed was in both; each quality measure's
    first → last)."""
    both = [{"name": "first", "runs": [first]}, {"name": "last", "runs": [last]}]
    per, common = counted(both)
    if not common:
        return None, "no seed counted in both runs"
    moves, said = set(), []
    for key, label in QUALITY:
        (a, b), spread, d = measure(per, common, key)
        said.append(f"{label} {num(a, key)}→{num(b, key)}")
        if past_noise(d, spread, False):
            moves.add("up" if d > 0 else "down")
    return ("down" if "down" in moves else "up" if moves else "flat"), ", ".join(said)


def week_drift(runs, which_set, since):
    """One set's `drift` over its runs from run id `since` on, the newest size's first against its last;
    (None, why) with fewer than two."""
    rs = [x for x in runs if set_of(x[0]) == which_set and str(x[0].get("run")) >= since
          and not any(map(judge_failed, x))]
    rs = [x for x in rs if x[0].get("size") == rs[-1][0].get("size")] if rs else []
    if len(rs) < 2:
        return None, f"{len(rs)} {which_set} run{'s' * (len(rs) != 1)} this week"
    return drift(rs[0], rs[-1])


def overfit(p, at=None):
    """The overfit alarm over the week before `at`: (alarmed, the pool's line, the holdout's line). Alarmed when the
    training pool's quality rose past the noise while the frozen holdout's fell past it."""
    since = f"{(at or datetime.now()) - timedelta(days=WEEK_RUNS):%Y%m%d-%H%M%S}"
    runs = list(runs_of(p).values())
    (pool, pool_said), (hold, hold_said) = (week_drift(runs, s, since) for s in ("pool", "holdout"))
    return pool == "up" and hold == "down", f"pool {pool or '?'}: {pool_said}", f"holdout {hold or '?'}: {hold_said}"
