"""The experiment mix: what kind of idea each experiment ticket is, and how many of each a planning round writes.

A loop that keeps only what wins and plans from what hurts most drifts toward small, safe tweaks of the same few
things, and plateaus early. So every experiment ticket names its kind in its header (`Idea: <kind>`): `fix` (a cause
the digest or a bench failure shows), `bold` (a structural change: a session added, merged or dropped, the steps
reordered, what a session is shown), `ablation` (something taken out), `revisit` (a rejected idea whose context
changed) or `scout` (an idea from outside, on top of the mix). Each round's experiments follow `[experiments] mix`,
moved toward the kinds the bench keeps (each kind's kept rate over its last RECENT) but never under `floor`, so no
kind dies out. A plateau first gets one exploration round (EXPLORE kinds only) before planning stops
(`experiment.Experiments.plateau_watch`).

Novelty: the planner sees EXPERIMENTS.md as one line per record, grouped by the mechanism each changed
(`Mechanism:` in the ticket's header), and a new experiment changing the same mechanism and files as one rejected in
the last NOVELTY records needs a `Different because:` line, or `vet` drops it from the plan.
"""
import re
from pathlib import Path

from .config import DIR
from .queue import header_line, touches
from .util import load_json, read_jsonl

KINDS = ("fix", "bold", "ablation", "revisit")  # the kinds `[experiments] mix` shares out
SCOUT = "scout"  # ideas from outside: on top of the mix, never counted against it
IDEAS = KINDS + (SCOUT,)
EXPLORE = ("bold", "ablation", SCOUT)  # the only kinds an exploration round plans
RECENT = 10  # a kind's kept rate is over its last this many decided experiments
NOVELTY = 20  # a repeat is checked against this many newest rejected records
EXPERIMENTS = f"{DIR}/EXPERIMENTS.md"
SHOWN = 4000  # characters of the grouped records a planner prompt gets: the newest lines
UNNAMED = "(mechanism not recorded)"
INCIDENTAL = ("tests/", "docs/", "CHANGELOG.md", "README.md")  # files most changes touch: no sign of the same idea


def idea(head):
    """A ticket header's `Idea:` kind, "" when it names none of IDEAS."""
    word = (header_line(head, "Idea").strip(" .`*").lower().split() or [""])[0]
    return word if word in IDEAS else ""


def mechanism(head):
    """A ticket header's `Mechanism:`, lowercased and spaced evenly ("" without one): what an idea changes, so a
    repeat shows however its title is worded."""
    return norm(header_line(head, "Mechanism"))


def norm(text):
    return " ".join(text.strip(" .`*").lower().split())


def is_experiment(text):
    """Whether a ticket's header says `Kind: experiment`."""
    return norm(header_line("\n".join(text.splitlines()[:8]), "Kind")) == "experiment"


def different(text):
    """The ticket's `Different because:` line, "" without one."""
    m = re.search(r"^\s*(?:\*\*)?Different because:(?:\*\*)?\s*(\S.*)$", text, re.M | re.I)
    return m.group(1).strip() if m else ""


def kind_of(row, ideas):
    """A ledger row's idea kind: its own `idea`, else its ticket's header; an experiment from before kinds were
    named counts as `fix`, which most of that planning was."""
    return row.get("idea") or ideas.get(row.get("ticket"), "") or "fix"


def decided(p, ideas):
    """The bench's verdicts, oldest first, as (ticket, kind, "kept" or "rejected"); a bold one's first rejection,
    sent back for its revision, isn't one."""
    out = []
    for r in read_jsonl(p.state / "ledger.jsonl"):
        if r.get("event") == "experiment" and r.get("verdict") == "kept":
            out.append((r.get("ticket"), kind_of(r, ideas), "kept"))
        elif r.get("event") == "outcome" and r.get("outcome") == "rejected":
            out.append((r.get("ticket"), kind_of(r, ideas), "rejected"))
    return out


def rates(rows):
    """{kind: (kept, decided)} over each kind's last RECENT verdicts."""
    out = {}
    for k in IDEAS:
        last = [v for _, kind, v in rows if kind == k][-RECENT:]
        out[k] = (last.count("kept"), len(last))
    return out


def shares(mix, floor, rate, kinds=KINDS):
    """{kind: share} of `kinds`, summing to 1: each kind's `mix` weight times its smoothed kept rate (kept + 1 over
    decided + 2, so a kind not yet tried keeps its weight), then lifted to `floor` with the rest scaled down."""
    w = {k: max(0.0, float(mix.get(k, 0))) * (rate.get(k, (0, 0))[0] + 1) / (rate.get(k, (0, 0))[1] + 2)
         for k in kinds}
    if not kinds:
        return {}
    if floor * len(kinds) >= 1 or not sum(w.values()):
        return {k: 1 / len(kinds) for k in kinds}
    low = set()
    while True:
        rest = sum(v for k, v in w.items() if k not in low)
        free = 1 - floor * len(low)
        out = {k: floor if k in low else w[k] / rest * free for k in kinds}
        more = {k for k in kinds if k not in low and out[k] < floor}
        if not more:
            return out
        low |= more


def counts(share, n):
    """`n` experiment slots apportioned by `share`: every kind at least one when there are slots enough, the rest
    by the largest remainder."""
    if not share or n <= 0:
        return {k: 0 for k in share}
    out = {k: 1 if n >= len(share) else 0 for k in share}
    left = n - sum(out.values())
    want = {k: max(0.0, s * n - out[k]) for k, s in share.items()}
    for k in share:
        add = min(left, int(want[k]))
        out[k] += add
        left -= add
    for k in sorted(share, key=lambda k: -(want[k] - int(want[k]))):
        if left <= 0:
            break
        out[k] += 1
        left -= 1
    return out


def exploring(p):
    """The exploration round a plateau started ({"at": verdict count, "ids": its experiments or None before its
    plan}), or None."""
    return load_json(p.state / "plateau.json", {}).get("explore")


def round_kinds(p, cd):
    """The kinds this round shares out: the mix's, `revisit` only with a rejected record to revisit; an exploration
    round's EXPLORE ones."""
    kinds = [k for k in KINDS if k != "revisit" or records(cd)]
    return [k for k in kinds if k in EXPLORE] if exploring(p) else kinds


def plan_counts(p, cd, n, ideas):
    """{kind: experiments this round may write} for `n` slots."""
    cfg = p.cfg["experiments"]
    kinds = round_kinds(p, cd)
    return counts(shares(cfg["mix"], float(cfg["floor"]), rates(decided(p, ideas)), kinds), n)


def mix_part(p, cd, n, ideas):
    """The planner prompt's section on this round's mix, for a project with `bench/`."""
    if not (Path(p.root) / "bench").is_dir():
        return ""
    got = plan_counts(p, cd, n, ideas)
    lead = ("## This round explores (a plateau: the last experiments were all rejected)\nOnly `bold`, `ablation` "
            "and `scout` experiments this round" if exploring(p) else "## This round's experiment mix\nOf the "
            "experiments you write this round")
    return (f"{lead}, at most: " + ", ".join(f"{k} {c}" for k, c in got.items()) + "; `scout` ideas from outside "
            "come on top. An experiment past its kind's count is dropped from the plan.")


def records(cd):
    """EXPERIMENTS.md's records, oldest first: {"id", "title", "date", "kind", "mechanism", "files", "numbers",
    "why"}; a record from before kinds and mechanisms were written has "" for them."""
    f = Path(cd) / EXPERIMENTS
    text = f.read_text() if f.is_file() else ""
    out = []
    for block in text.split("\n### ")[1:]:
        lines = block.strip().splitlines()
        m = re.match(r"(\S+?): (.*) \((\d{4}-\d\d-\d\d)\)$", lines[0]) or re.match(r"(\S+?): (.*)()$", lines[0])
        if not m:
            continue
        field = {}
        for line in lines[1:]:
            k, _, v = line.removeprefix("- ").partition(": ")
            if k in ("Kind", "Mechanism", "Files", "Verdict"):
                field[k] = v.strip()
            elif "→" in line and "numbers" not in field:
                field["numbers"] = v.strip() if v else line.removeprefix("- ")
        files = re.sub(r" and \d+ more$", "", field.get("Files", "")).split(", ")
        out.append({"id": m.group(1), "title": m.group(2), "date": m.group(3), "kind": field.get("Kind", ""),
                    "mechanism": norm(field.get("Mechanism", "")), "files": [x for x in files if x and x != "(none)"],
                    "numbers": field.get("numbers", ""), "why": field.get("Verdict", "").split(";")[0]})
    return out


def grouped_part(cd, most=SHOWN):
    """The planner prompt's section on the rejected experiments: one line each, grouped by the mechanism it changed,
    the group with the newest record first; the oldest lines left out to fit `most` characters."""
    recs = records(cd)
    if not recs:
        return ""
    keep, size = [], 0
    for r in reversed(recs):
        line = (f"- {r['id']} ({r['kind'] or 'kind not recorded'}{', ' + r['date'] if r['date'] else ''}): "
                f"{r['title']} · {', '.join(r['files'][:4]) or 'no files'} · {r['why'] or r['numbers']}")
        size += len(line) + 1
        if size > most and keep:
            break
        keep.append((r["mechanism"] or UNNAMED, line))
    groups = {}
    for mech, line in keep:
        groups.setdefault(mech, []).append(line)
    left = len(recs) - len(keep)
    return (f"## Experiments the bench rejected ({EXPERIMENTS}), by the mechanism each changed, newest first\n"
            + (f"({left} older left out)\n" if left else "")
            + "\n".join(f"{mech}:\n" + "\n".join(lines) for mech, lines in groups.items()))


def core(paths):
    return {x.rstrip("/") for x in paths if x and not x.startswith(INCIDENTAL)}


def overlaps(a, b):
    """Whether two sets of paths share a file, a directory counting for every file under it."""
    return any(x == y or x.startswith(y + "/") or y.startswith(x + "/") for x in core(a) for y in core(b))


def repeats(head, text, recs):
    """The newest of the last NOVELTY rejected records an experiment repeats (the same mechanism, or either one not
    named, and a shared file), None when it doesn't or says what's `different`."""
    if different(text):
        return None
    mech, files = mechanism(head), touches(head)
    for r in reversed(recs[-NOVELTY:]):
        if (not mech or not r["mechanism"] or mech == r["mechanism"]) and overlaps(files, r["files"]):
            return r
    return None


def vet(p, cd, new, order, n, ideas, log):
    """Drop from a plan the experiments it may not keep: a repeat (`repeats`), then each kind's past its round's
    count, keeping the first in queue order (the most valuable). `new`: {id: ticket text}. Returns the ids
    dropped, each with a log line; their files are removed and their queue bullets go with them."""
    room, recs, drop = plan_counts(p, cd, n, ideas), records(cd), []
    for tid in sorted(new, key=lambda t: order.index(t) if t in order else len(order)):
        text = new[tid]
        head = "\n".join(text.splitlines()[:8])
        if not is_experiment(text):
            continue
        kind = idea(head)
        if not kind:
            kind = "fix"
            log(f"plan: {tid} names no `Idea:` kind; counted as fix")
        r = repeats(head, text, recs)
        if r:
            log(f"plan: dropped {tid}: it changes the same mechanism ({r['mechanism'] or UNNAMED}) and files as "
                f"{r['id']}, which the bench rejected, with no `Different because:` line")
        elif kind != SCOUT and room.get(kind, 0) <= 0:
            log(f"plan: dropped {tid} ({kind}): this round's mix has no more room for {kind} experiments "
                f"({', '.join(f'{k} {c}' for k, c in room.items())})")
        else:
            room[kind] = room.get(kind, 0) - (kind != SCOUT)
            continue
        drop.append(tid)
    return drop


def report(p, ideas, titles):
    """`windvane experiments`' lines on the mix: each kind's share this round, its kept rate over its last RECENT
    and its last kept experiment."""
    cfg, rows = p.cfg["experiments"], decided(p, ideas)
    rate = rates(rows)
    share = shares(cfg["mix"], float(cfg["floor"]), rate, round_kinds(p, p.root))
    out = ["experiment mix" + (" (an exploration round after a plateau)" if exploring(p) else "")
           + f": share this round, kept over each kind's last {RECENT}, last kept"]
    for k in IDEAS:
        kept = [t for t, kind, v in rows if kind == k and v == "kept"]
        last = f"{kept[-1]} {titles.get(kept[-1], '')}".strip() if kept else "-"
        cut = "on top" if k == SCOUT else f"{share[k]:.0%}" if k in share else "0%"
        n = rate[k]
        out.append(f"  {k:<9} {cut:>6}  kept {f'{n[0]}/{n[1]}' if n[1] else '-':<5}  {last}")
    return out
