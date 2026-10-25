"""The training pool, `bench/pool/`: seeds the `seed` round writes (one [[periodic]] session, `prompts/SEED.md`), so the
experiments are kept or dropped on projects that change, while `bench/`'s own seeds stay a frozen holdout.

A session writes one seed to `bench/pool/new-<kind>/` in a scratch tree: `seed/` and `hidden/` in the bench's format,
and `reference/`, a solution it built. It joins the pool only when the bench's own scorer (`tally`) finds every
ticket's hidden tests failing on the bare seed and passing on the reference; the reference is never committed. Pool
seeds are `<NNN>-<kind>`, numbered as they join, and the pool keeps the newest `pool_size`. The ledger's `seed`
events (`joined` or `refused`) and `experiment` events (`kept`) say when the next round is due.
"""
import re
import shutil
import tempfile
from pathlib import Path

from .bench import seeds
from .bench_score import CHECK, tally
from .config import POOL
from .util import read_jsonl

KIND = re.compile(r"new-([a-z][a-z0-9-]{1,30})")


def names(root):
    """The pool's seeds in the tree `root`, oldest first."""
    d = Path(root) / POOL
    return sorted(x.name for x in d.iterdir() if re.fullmatch(r"\d{3}-.+", x.name)) if d.is_dir() else []


def kind(name):
    return name.split("-", 1)[1] if re.fullmatch(r"\d{3}-.+", name) else name


def taken(root):
    """The kinds a new seed may not be: the pool's, and the holdout's names (a pool seed like one would train on it)."""
    holdout = [x.name for x in (Path(root) / "bench").iterdir() if x.name != "pool"] if (Path(root) / "bench").is_dir() \
        else []
    return sorted({kind(n) for n in names(root)} | set(holdout))


def staged(root):
    """The one `new-<kind>` folder a session wrote under the pool, or (None, why)."""
    found = sorted((Path(root) / POOL).glob("new-*"))
    if len(found) != 1:
        return None, f"{len(found)} new-<kind> folders under {POOL}/, not one"
    m = KIND.fullmatch(found[0].name)
    if not m:
        return None, f"{found[0].name}: name it new-<kind>, the kind in lower case letters, digits and -"
    if m.group(1) in taken(root):
        return None, f"{m.group(1)} is in the pool or the holdout already"
    return found[0], ""


def no_reference(new):
    """A copytree `ignore` leaving out the seed folder `new`'s own `reference/`, and nothing deeper of that name."""
    return lambda d, found: ["reference"] if Path(d) == Path(new) and "reference" in found else []


def check(new):
    """Why the seed in the folder `new` can't join the pool, or "": it must load as a bench seed, map every ticket to
    hidden tests and have judge checks, and its hidden tests, run by the bench's scorer in scratch copies, must fail
    for every ticket on the bare seed and pass for every ticket on `reference/`."""
    with tempfile.TemporaryDirectory(prefix="windvane-seed-") as tmp:
        tmp = Path(tmp)
        shutil.copytree(new, tmp / "set" / new.name, ignore=no_reference(new))
        try:
            seed = seeds(tmp / "set").get(new.name)
        except (SystemExit, OSError, ValueError) as e:  # a bad config.toml or bench.toml
            return str(e)
        if not seed:
            return "no seed/ with a hidden/bench.toml"
        if not seed["tickets"]:
            return "no tickets in seed/.windvane/tickets/"
        mapped = seed["bench"].get("tickets", {})
        lack = [t for t in seed["tickets"] if not mapped.get(t)]
        missing = [m for t in seed["tickets"] for m in mapped.get(t, []) if not (new / "hidden" / f"{m}.py").is_file()]
        if lack or missing or not seed["bench"].get("command"):
            return (f"hidden/bench.toml maps no tests to {', '.join(lack)}" if lack else
                    f"hidden/{missing[0]}.py is missing" if missing else "hidden/bench.toml has no command")
        jf = new / "hidden/JUDGE.md"
        if not (jf.is_file() and CHECK.findall(jf.read_text())):
            return "hidden/JUDGE.md has no `- J1: ...` checks"
        if not (new / "reference").is_dir() or not any((new / "reference").iterdir()):
            return "no reference/ solution to check the hidden tests on"
        for side, overlay in (("bare", None), ("ref", new / "reference")):
            scratch = tmp / side / "tree"
            shutil.copytree(new / "seed", scratch)
            if overlay:
                shutil.copytree(overlay, scratch, dirs_exist_ok=True)
            got = tally(seed, seed["tickets"], scratch)
            if side == "bare":
                ok = [t for t, n in got.items() if n["passed"] >= n["total"]]
                if ok:
                    return f"the hidden tests of {', '.join(ok)} already pass on the bare seed: they measure nothing"
            else:
                bad = [f"{t} ({n['passed']}/{n['total']})" for t, n in got.items()
                       if not n["total"] or n["passed"] < n["total"]]
                if bad:
                    return f"the hidden tests fail on the reference solution: {', '.join(bad)}"
    return ""


def admit(root, new, size):
    """Copy the checked seed `new` into the pool in the tree `root` as `<NNN>-<kind>` (seed/ and hidden/ only), and
    drop the oldest past `size`. Returns (its name, [the names dropped])."""
    have = names(root)
    n = max([int(x[:3]) for x in have], default=0) + 1
    name = f"{n:03d}-{KIND.fullmatch(new.name).group(1)}"
    for part in ("seed", "hidden"):
        shutil.copytree(new / part, Path(root) / POOL / name / part)
    dropped = (have + [name])[:max(0, len(have) + 1 - size)]
    for old in dropped:
        shutil.rmtree(Path(root) / POOL / old)
    return name, dropped


def kept_since_seed(ledger):
    """The experiments the bench kept since the last seed joined the pool."""
    n = 0
    for r in read_jsonl(ledger):
        if r.get("event") == "seed" and r.get("verdict") == "joined":
            n = 0
        elif r.get("event") == "experiment" and r.get("verdict") == "kept":
            n += 1
    return n
