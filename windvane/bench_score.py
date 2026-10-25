"""How a bench run is scored: the hidden tests run on a seed's final main, the judge's answers read, the noise rule,
and the verdict of a compare. Kept apart from the runner (`bench.py`) so a change to how windvane is measured is a
change to this one file, which `protected` keeps every session's branch away from: a run that could edit its own
scoring could improve its numbers without improving anything.
"""
import ast
import re
import shlex
import shutil
import subprocess
from pathlib import Path

from .stats import k

TEST_TIMEOUT = 600  # seconds the hidden tests of one seed may take
# unittest -v: a test's line (`test_x (module.Class.test_x)`), and its outcome at the end of it or of the docstring's
START = re.compile(r"^(\w+) \(([\w.]+)\)")
# (or alone on a line, when a warning the test caused was printed between the two)
OUTCOME = re.compile(r"(?:^| \.\.\. )(ok|FAIL|ERROR|skipped.*|expected failure|unexpected success)$")
CHECK = re.compile(r"^- (J\d+): (.+)$", re.M)  # a check in a seed's hidden/JUDGE.md
ANSWER = re.compile(r"^\W*(J\d+)\W+(yes|no)\b[\s,.:;\u2014-]*(.*)$", re.I | re.M)  # its line in the judge's reply


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
    subprocess.run(["git", "clone", "-q", str(remote), str(scratch)], cwd=scratch.parent, check=True,
                   capture_output=True)
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


def judge_checks(seed, home):
    """[(id, check)] of the seed's hidden/JUDGE.md; a ref from before the judge has none, and borrows the measuring
    checkout's (`home`/bench), so both sides of a compare meet the same judge."""
    for d in (seed["dir"], Path(home) / "bench" / seed["name"]):
        f = d / "hidden/JUDGE.md"
        if f.is_file():
            return CHECK.findall(f.read_text())
    return []


def answers(text, checks):
    """[{id, answer, reason}] for every check, from the judge's reply; one it skipped is a no."""
    got = {}
    for m in ANSWER.finditer(text or ""):
        got.setdefault(m[1].upper(), (m[2].lower(), m[3].strip()))
    return [{"id": i, "answer": got.get(i, ("no",))[0], "reason": got.get(i, ("", "not answered"))[1]}
            for i, _ in checks]


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


# What `bench compare` sets side by side: (key, label). Each is a run's value over the seeds compared.
MEASURES = (("merged", "merged"), ("hidden", "hidden tests"), ("judge", "judge yes"),
            ("per_merged", "tokens/merged"), ("minutes", "wall min"))


NOISE = ("one run a side: a difference of one hidden test or one judge check can't be told from noise "
         "(--repeat N runs each side N times)")


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
