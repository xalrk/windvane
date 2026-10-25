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

`bench compare A B` sets two windvane refs (or earlier runs) side by side, running only what isn't recorded yet.
"""
import copy
import ast
import itertools
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
import tomllib
from datetime import datetime
from pathlib import Path

from .config import PERM_ROLES, Project, user_dir
from .heavy import machine
from .runner import rules as session_rules, run_claude
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
JUDGE_MINUTES = 30  # a judge still going after this is stopped: the checks it hasn't answered count as no
CHECK = re.compile(r"^- (J\d+): (.+)$", re.M)  # a check in a seed's hidden/JUDGE.md
ANSWER = re.compile(r"^\W*(J\d+)\W+(yes|no)\b[\s,.:;\u2014-]*(.*)$", re.I | re.M)  # its line in the judge's reply
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
        elif f.name not in ("bench.toml", "JUDGE.md"):
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


def goal(seed):
    """What the seed is for, as its CLAUDE.md opens (up to its first section), else its README."""
    for name in ("CLAUDE.md", "README.md", "README"):
        f = seed["dir"] / "seed" / name
        if f.is_file():
            return f.read_text().split("\n## ")[0].strip()
    return seed["name"]


def judge_checks(seed, home):
    """[(id, check)] of the seed's hidden/JUDGE.md; a ref from before the judge has none, and borrows the measuring
    checkout's (`home`/bench), so both sides of a compare meet the same judge."""
    for d in (seed["dir"], Path(home) / "bench" / seed["name"]):
        f = d / "hidden/JUDGE.md"
        if f.is_file():
            return CHECK.findall(f.read_text())
    return []


def judge_prompt(seed, tickets, checks):
    """The goal, the tickets this run worked, the checks: nothing else of the run."""
    parts = ["Judge the project in your working folder.", "## Its goal", goal(seed),
             "## The tickets it was built from"]
    parts += [(seed["dir"] / "seed/.windvane/tickets" / f"{t}.md").read_text().strip() for t in tickets]
    return "\n\n".join(parts + ["## The checks", "\n".join(f"- {i}: {c}" for i, c in checks)])


def answers(text, checks):
    """[{id, answer, reason}] for every check, from the judge's reply; one it skipped is a no."""
    got = {}
    for m in ANSWER.finditer(text or ""):
        got.setdefault(m[1].upper(), (m[2].lower(), m[3].strip()))
    return [{"id": i, "answer": got.get(i, ("no",))[0], "reason": got.get(i, ("", "not answered"))[1]}
            for i, _ in checks]


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


def judged(r):
    """The record's judge verdict when it is one: None with no checks, before the judge, or when the judge failed
    or was stopped (its unanswered checks would read as the project's noes)."""
    j = r.get("judge")
    return j if j and not j.get("error") and not j.get("stopped") else None


def judge_failed(r):
    """Why the record's judge gave no verdict, when it ran and gave none."""
    j = r.get("judge") or {}
    return j.get("error") or (j.get("stopped") and f"stopped ({j['stopped']})")


def yeses(r):
    """The judge's yes count of a seed's record, "-" when there is no verdict."""
    j = judged(r)
    return f"{j['yes']}/{j['total']}" if j else "-"


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


def main(a, p):
    if a.repeat != 1 and not a.what:
        raise SystemExit("bench: --repeat goes with `windvane bench compare <A> <B>`")
    if a.what:
        if a.what[0] != "compare" or len(a.what) != 3:
            raise SystemExit("bench: the only subcommand is `windvane bench compare <A> <B>`")
        return compare(a, p, *a.what[1:])
    if a.last:
        runs = runs_of(p)
        return print(table(list(runs.values())[-1]) if runs else "bench: no results yet (windvane bench runs one)")
    before = week_now()
    which, why = size(a.full, a.small, before, week_limit())
    print(f"bench: {which} ({why})", flush=True)
    return one(p, a, a.ref, which, before)[0]


def week_limit():
    return float(machine().get("bench_week_small", WEEK_SMALL))


def one(p, a, ref, which, before):
    """One bench run of windvane at `ref`: (exit code, its records)."""
    base = new_run(state_root() / "_bench")
    wt = base / "windvane"
    old = {s: signal.signal(s, stopped) for s in (signal.SIGTERM, signal.SIGHUP)}
    try:
        sha = ref_worktree(p, ref, wt)
        found = seeds(wt / "bench")
        if not found:
            raise SystemExit(f"bench: no seeds in bench/ at {ref}")
        chosen = pick(found, which)
        print("\n".join(plan_lines(chosen, a.hours, ref, sha)), flush=True)
        if a.dry_run:
            print(earlier(p, which))
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
                   "exit": proc.returncode}
            append_jsonl(results(p), rec)
            recs.append(rec)
    except (KeyboardInterrupt, Stopped) as e:  # Ctrl-C, or a closed terminal: no seed run goes on alone
        end_all(procs)
        print(f"bench: stopped ({str(e) or 'SIGINT'}): every seed run was ended; {len(recs)} of {len(procs)} seeds "
              f"scored{' (see bench --last)' if recs else ''}; their logs are in {base}", file=sys.stderr)
        return 130, recs
    print(table(recs))
    return 0, recs


# What `bench compare` sets side by side: (key, label). Each is a run's value over the seeds compared.
MEASURES = (("merged", "merged"), ("hidden", "hidden tests"), ("judge", "judge yes"),
            ("per_merged", "tokens/merged"), ("minutes", "wall min"))
NOISE = ("one run a side: a difference of one hidden test or one judge check can't be told from noise "
         "(--repeat N runs each side N times)")


def planned(p, sha, which):
    """The seeds a run of this size at `sha` works, by name, read from git: what makes a recorded run reusable."""
    names = (gout(p.main_root(), "ls-tree", "-d", "--name-only", f"{sha}:bench") or "").split()
    has = lambda path: subprocess.run(["git", "cat-file", "-e", f"{sha}:bench/{path}"], cwd=p.main_root(),
                                      capture_output=True).returncode == 0
    names = sorted(n for n in names if has(f"{n}/seed") and has(f"{n}/hidden/bench.toml"))
    return names if which == "full" else names[:SMALL_SEEDS]


def sides(p, refs, which, runs):
    """Each side of a compare: an earlier run's id (that run, fixed) or a ref, with its runs already recorded at the
    same sha and size over the same seeds, newest last."""
    out = []
    for name in refs:
        if name in runs:
            out.append({"name": name, "sha": runs[name][0]["sha"], "runs": [runs[name]], "fixed": True})
            continue
        sha = gout(p.main_root(), "rev-parse", "--verify", "--quiet", f"{name}^{{commit}}")
        if not sha:
            raise SystemExit(f"bench compare: {name!r} is neither a commit nor an earlier run's id")
        want = planned(p, sha, which)
        have = [rs for rs in runs.values() if rs[0].get("sha") == sha and rs[0].get("size") == which
                and sorted(r["seed"] for r in rs) == want]
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
    both = sides(p, refs, which, runs)
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


def runs_n(n):
    return f"{n} run" + ("" if n == 1 else "s")


def value(recs, key):
    """One measure of one run over these seed records; None where it has none (no judge, nothing merged)."""
    merged = sum(len(r["tickets"]["merged"]) for r in recs)
    if key == "merged":
        return merged
    if key == "hidden":
        return sum(r["passed"] for r in recs)
    if key == "judge":
        return sum(judged(r)["yes"] for r in recs) if all(map(judged, recs)) else None
    if key == "per_merged":
        return sum(r["weighted"] for r in recs) / merged if merged else None
    return max(r["minutes"] for r in recs)  # the seeds run side by side


def out_of(recs, key):
    """A measure's "/total" (hidden tests, judge checks), else nothing."""
    if key == "hidden":
        return f"/{sum(r['total'] for r in recs)}"
    if key == "judge" and all(map(judged, recs)):
        return f"/{sum(judged(r)['total'] for r in recs)}"
    return ""


def num(v, key, sign=False):
    if v is None:
        return "-"
    if key == "per_merged":
        return ("-" if v < 0 else "+" if v > 0 and sign else "") + k(abs(v))
    return f"{v:+.1f}".rstrip("0").rstrip(".") if sign and v else f"{v:.1f}".rstrip("0").rstrip(".")


def compare_table(both, which):
    """Per seed and in all: each measure's mean on A and B and B minus A. With repeats, a difference past the spread
    of either side's own runs is marked; without them, a line says one run can't tell a small one from noise."""
    per, common = counted(both)
    seen = sorted({r["seed"] for s in both for rs in s["runs"] for r in rs})
    A, B = both
    if not common:
        return f"bench compare: no seed was counted in every run of both sides ({', '.join(seen) or 'none ran'})"
    out = [f"bench compare ({which}): A = {A['name']} ({A['sha'][:7]}), {runs_n(len(A['runs']))} · "
           f"B = {B['name']} ({B['sha'][:7]}), {runs_n(len(B['runs']))}",
           f"{'seed':<12} {'measure':<14} {'A':>9} {'B':>9} {'B-A':>7}"]
    repeats, marked = max(len(A["runs"]), len(B["runs"])) > 1, 0
    for seed in common + ["all"]:
        names = common if seed == "all" else [seed]
        for i, (key, label) in enumerate(MEASURES):
            means, spread, delta = measure(per, names, key)
            past = repeats and delta is not None and abs(delta) > spread
            marked += past
            tot = [out_of([c[n] for n in names], key) for c in (per[0][0], per[1][0])]
            cells = [num(m, key) + (t if m is not None else "") for m, t in zip(means, tot)]
            out.append(f"{seed if i == 0 else '':<12} {label:<14} {cells[0]:>9} {cells[1]:>9} "
                       f"{num(delta, key, sign=True):>7}" + ("  past the noise" if past else ""))
    left = [s for s in seen if s not in common]
    if left:
        out.append(f"not compared: {', '.join(left)} (missing or contaminated in a run)")
    if not repeats:
        out.append(NOISE)
    elif not marked:
        out.append("no difference past the spread of either side's own runs")
    return "\n".join(out)


def counted(both):
    """Each side's runs as {seed: record} without the contaminated ones, and the seeds counted in every run of both."""
    per = [[{r["seed"]: r for r in rs if not r["contaminated"]} for rs in s["runs"]] for s in both]
    common = sorted(set.intersection(*(set(c) for side in per for c in side))) if all(per) else []
    return per, common


def measure(per, names, key):
    """One measure over the seeds `names`: (each side's mean over its runs, the wider spread of either side's own
    runs, B minus A), None where a run has no value (no judge, nothing merged)."""
    vals = [[value([c[n] for n in names], key) for c in side] for side in per]
    means = [None if not v or None in v else sum(v) / len(v) for v in vals]
    spread = max((max(v) - min(v) for v in vals if v and None not in v), default=0)
    return means, spread, None if None in means else means[1] - means[0]


COST_UP = 25  # a [[periodic]] bench's `bench_cost_up`: % more weighted tokens per merged ticket that is a regression
QUALITY = (("hidden", "hidden tests"), ("judge", "judge yes"))


def past_noise(delta, spread, repeats):
    """T-277's noise rule: with repeats, a difference wider than either side's own spread; with one run a side, more
    than one hidden test or judge check."""
    return delta is not None and abs(delta) > (spread if repeats else 1)


def regression(both, cost_up=COST_UP):
    """Whether B (the newer side) did worse than A past the noise: hidden tests or judge yeses down, or weighted
    tokens per merged ticket up more than `cost_up` % with neither quality number up. (the reasons, [] when it
    didn't, or None when no seed was counted on both sides; a line per seed that moved)."""
    per, common = counted(both)
    if not common:
        return None, []
    repeats = max(len(s["runs"]) for s in both) > 1
    why, up = [], False
    for key, label in QUALITY:
        _, spread, d = measure(per, common, key)
        if past_noise(d, spread, repeats):
            up |= d > 0
            why += [f"{label} down {num(-d, key)}"] if d < 0 else []
    (a, _), spread, d = measure(per, common, "per_merged")
    if not up and d is not None and a and d > a * cost_up / 100 and (not repeats or d > spread):
        why.append(f"weighted tokens per merged ticket up {d / a:.0%} (more than {cost_up:g}%)")
    return why, [line for seed in common if (line := moved(per, seed, cost_up))]


def moved(per, seed, cost_up):
    """`seed: hidden tests 5/8 → 3/8, ...` for the measures that changed on this seed, or ""."""
    parts = []
    for key, label in (("merged", "merged"), *QUALITY, ("per_merged", "tokens/merged")):
        (a, b), _, d = measure(per, [seed], key)
        if not d or (key == "per_merged" and not (a and abs(d) > a * cost_up / 100)):
            continue
        tot = [out_of([side[0][seed]], key) for side in per]
        parts.append(f"{label} {num(a, key)}{tot[0]} → {num(b, key)}{tot[1]}")
    return f"{seed}: {', '.join(parts)}" if parts else ""


def headline(both):
    """`hidden tests 12/14 → 10/14, judge yes 5/6 → 5/6, tokens/merged 410k → 520k` over the seeds both counted."""
    per, common = counted(both)
    if not common:
        return "no seed counted on both sides"
    parts = []
    for key, label in (*QUALITY, ("per_merged", "tokens/merged")):
        (a, b), _, _ = measure(per, common, key)
        tot = [out_of([side[0][n] for n in common], key) for side in per]
        parts.append(f"{label} {num(a, key)}{tot[0] * (a is not None)} → {num(b, key)}{tot[1] * (b is not None)}")
    return ", ".join(parts)


def summary(recs):
    """`hidden tests 12/14, judge yes 5/6, 410k tokens/merged` of one run, its counted seeds only."""
    good = [r for r in recs if not r["contaminated"]]
    if not good:
        return "no seed counted"
    parts = [f"{label} {num(value(good, key), key)}{out_of(good, key)}" for key, label in QUALITY]
    return ", ".join(parts + [f"{num(value(good, 'per_merged'), 'per_merged')} tokens/merged"])
