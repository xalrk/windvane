"""The critic: a fresh, read-only session that judges what a ticket's change gives the people who use the project,
0 to 10 on the project's own scale (`.windvane/CRITIC.md`), harshly. The code reviewer comes after it; the critic
judges the outcome.

The worker leaves an evidence pack in `<state>/critic/<ID>/`: `notes.md` (line 1 `User-facing: yes` or
`User-facing: no (<why>)`, then what a user now gets and measured numbers) and up to a few images (screenshots,
contact sheets). It runs `windvane critic <ID>` and follows the last line, `CRITIC STEP`: pass (lowest dimension >=
pass mark), scope (under it, but nothing this ticket can fix), revise, rethink (the first dimension under the floor:
the approach is wrong), accept (out of revisions). The critic scores blind; only the driver knows the bar. Every verdict goes to `critic.jsonl`; its "Beyond this ticket" points feed `windvane plan`.
"""
import hashlib
import re
import statistics
from pathlib import Path

from . import net
from .config import HOME
from .runner import run_claude
from .util import append_jsonl, git, gout, load_json, read_jsonl

OLD_RE = re.compile(r"CRITIC: (\d+(?:\.\d+)?)/10 (\S+) fixes=(\d+)[ \t]*(.*)")
CRITIC_RE = re.compile(r"CRITIC: (\S+) ((?:[\w-]+=(?:\d+(?:\.\d+)?|-)[ \t]+)+?)fixes=(\d+)[ \t]*(.*)")
DIM_RE = re.compile(r"([\w-]+)=(\d+(?:\.\d+)?|-)")
DONE = ("pass", "scope", "accept", "skip")
DENY = ["Bash", "Edit", "Write", "NotebookEdit", "Agent", "WebFetch", "WebSearch"]

NEXT = {
    "pass": "Meets the bar. Go on to the check; put the critique in the PR body under \"Critic\".",
    "scope": "Under the bar, but nothing this ticket can fix. Go on; put the critique in the PR body under \"Critic\".",
    "revise": ("Fix every \"Fix in this ticket\" point, refresh the pack, and run `windvane critic {tid}` again "
               "(revision {n} of {max}). Don't run the check until the critic lets it go."),
    "rethink": ("Under {floor}: the approach itself is wrong. Rethink it as the critique says, don't patch it; refresh "
                "the pack and run `windvane critic {tid}` again (revision {n} of {max})."),
    "accept": ("Out of revisions and still under the bar. Go on; put the critique in the PR body under "
               "\"Critic (under the bar)\"."),
    "skip": "Not user-facing: go on to the check (the reviewer checks that claim).",
}


def pack_dir(p, tid):
    return p.state / "critic" / tid


def pack(p, tid):
    """(notes or None, [image paths], user-facing?)."""
    d = pack_dir(p, tid)
    notes = (d / "notes.md").read_text()[:4000] if (d / "notes.md").is_file() else None
    images = sorted(x for x in d.glob("*") if x.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp"))[:4]
    facing = not re.match(r"\s*(user|player)-facing:\s*no", notes or "", re.I)
    return notes, images, facing


def history(p, tid):
    return [e for e in read_jsonl(p.state / "critic.jsonl") if e.get("ticket") == tid]


def revisions(records):
    return sum(r.get("step") in ("revise", "rethink") for r in records)


def verdict(text):
    """The last CRITIC line as (ticket, {dimension: score}, fixes, gist), or None. The ticket's score is its lowest
    dimension; `-` marks one the ticket doesn't touch and is left out. An old `CRITIC: 7/10 <ID>` line is one
    dimension, "score"."""
    lines = [l for l in (text or "").splitlines() if "CRITIC:" in l]
    for line in reversed(lines):
        if m := CRITIC_RE.search(line):
            dims = {k: float(v) for k, v in DIM_RE.findall(m.group(2)) if v != "-"}
            if dims:
                return m.group(1), dims, int(m.group(3)), m.group(4)
        if m := OLD_RE.search(line):
            return m.group(2), {"score": float(m.group(1))}, int(m.group(3)), m.group(4)
    return None


def step(p, dims, fixes, revs):
    """What a verdict leads to. The critic never sees the bar (T-022: shown it, it decided pass or fail first and
    wrote the matching number); the driver compares here. Open "Fix in this ticket" points block a pass whatever the
    score (a ticket once passed at 8 while its critique still named a crash race in its own
    code). Only the first dimension, whether the ticket works at all, can call for a rethink: a wording problem
    means revise."""
    c = p.critic
    if fixes == 0:
        return "pass" if min(dims.values()) >= c["pass"] else "scope"
    if revs >= c["revisions"]:
        return "accept"
    return "revise" if next(iter(dims.values())) >= c["floor"] else "rethink"


def unscored(critique):
    """An earlier critique without its score lines, so a revision is judged afresh, not as "last time +1"."""
    return "\n".join(l for l in critique.splitlines() if not re.match(r"\s*(Scores?:|CRITIC:)", l)).strip()


def beyond(critique):
    m = re.search(r"^Beyond this ticket:?\s*\n((?:[ \t]*[-*\d].*\n?)*)", critique or "", re.M)
    return [re.sub(r"^[ \t]*(?:[-*]|\d+\.)\s*", "", l).strip() for l in (m.group(1) if m else "").splitlines()
            if l.strip()]


def followups(p):
    """Every critique's "Beyond this ticket" points since the last plan run that used them: [(ticket, point)]."""
    recs = read_jsonl(p.state / "critic.jsonl")
    last = max((i for i, r in enumerate(recs) if r.get("ticket") == "PLAN"), default=-1)
    seen, out = set(), []
    for r in recs[last + 1:]:
        for b in beyond(r.get("critique")):
            if b.lower() not in seen:
                seen.add(b.lower())
                out.append((r.get("ticket"), b))
    return out


def calibration(recs, bar, last=10):
    """How the critic's scores spread over the last `last` tickets it scored, to show whether the calibration works:
    in the first self-run every final score sat exactly on the pass mark. Not-user-facing skips and plan markers carry
    no score and are left out. Returns None when nothing was scored."""
    by, latest = {}, {}
    for i, r in enumerate(recs):
        if r.get("score") is not None and r.get("ticket") != "PLAN":
            by.setdefault(r["ticket"], []).append(r)
            latest[r["ticket"]] = i
    order = sorted(by, key=latest.get)[-last:]
    if not order:
        return None
    tickets = [(t, by[t]) for t in order]
    scores = [r["score"] for _, rs in tickets for r in rs]
    return {
        "bar": bar, "tickets": tickets, "verdicts": len(scores),
        "first": [rs[0]["score"] for _, rs in tickets], "final": [rs[-1]["score"] for _, rs in tickets],
        "revised": sum(any(r.get("step") in ("revise", "rethink") for r in rs) for _, rs in tickets),
        "final_on_bar": sum(rs[-1]["score"] == bar for _, rs in tickets),
        "on_bar": sum(x == bar for x in scores),
        "accepted": sum(rs[-1].get("step") == "accept" for _, rs in tickets),
    }


def collapsed(recs, last=10, least=5):
    """One line when the scale stopped measuring anything, else None. Over the last `last` tickets scored (at least
    `least` of them): half or more of their first scores on one value, or every verdict's score but one in ten set by
    its fix count alone. The first self-run did both: 8 of its last 10 first scores were 8, and 0 fixes always meant
    8, 1 fix 7, so the score only repeated the fix list. Only verdicts on the newest scale count: a recalibration
    starts afresh."""
    now = next((r.get("scale") for r in reversed(recs) if r.get("score") is not None), None)
    c = calibration([r for r in recs if r.get("scale") == now], None, last)
    if not c or len(c["tickets"]) < least:
        return None
    n = lambda x: f"{x:g}"
    first = c["first"]
    top = statistics.mode(first)
    if 2 * first.count(top) >= len(first):
        return (f"Critic scale collapsed: {first.count(top)} of {len(first)} first scores are {n(top)}; "
                "recalibrate at the desk")
    by = {}
    for _, rs in c["tickets"]:
        for r in rs:
            by.setdefault(r.get("fixes", 0), []).append(r["score"])
    guess = {f: statistics.mode(xs) for f, xs in by.items()}
    hits = sum(xs.count(guess[f]) for f, xs in by.items())
    if len(set(guess.values())) > 1 and c["verdicts"] - hits <= c["verdicts"] // 10:
        rule = ", ".join(f"{f} fix{'es' * (f != 1)}: {n(guess[f])}" for f in sorted(guess))
        return (f"Critic scale collapsed: the fix count sets the score ({rule}) in {hits} of {c['verdicts']} "
                "verdicts; recalibrate at the desk")
    return None


def spread(xs):
    """"4-8 (median 6)": the range and middle of some scores."""
    n = lambda x: f"{x:g}"
    lo, hi = min(xs), max(xs)
    return f"{n(lo)}-{n(hi)} (median {n(statistics.median(xs))})" if lo != hi else f"all {n(lo)}"


def scale(p, cwd):
    """A short hash of the project's scale files: verdicts on an older scale don't count toward `collapsed`."""
    text = "".join(f.read_text() for rel in (p.rubric_rel, p.calibration_rel) if (f := Path(cwd) / rel).is_file())
    return hashlib.sha256(text.encode()).hexdigest()[:12]


def system_prompt(p, cwd):
    parts = [(HOME / "prompts/CRITIC.md").read_text()]
    for rel, head in ((p.rubric_rel, "# This project's scale"), (p.calibration_rel, "# The user's calibration")):
        f = Path(cwd) / rel
        if f.is_file():
            parts.append(f"{head}\n\n{f.read_text()}")
    return "\n\n".join(parts)


def judge(p, tid, cwd, rev="HEAD", usage=None, stop_if=None):
    """Critique a ticket's pack at `rev` and record the verdict. Returns (record or None, reply text)."""
    notes, images, facing = pack(p, tid)
    if notes is None:
        return None, f"No critic pack: write {pack_dir(p, tid)}/notes.md first."
    past = history(p, tid)
    n = revisions(past)
    if not facing:
        entry = {"ticket": tid, "sha": gout(cwd, "rev-parse", "--short", rev), "score": None, "fixes": 0,
                 "revisions": n, "step": "skip", "gist": notes.strip().splitlines()[0][:200], "critique": ""}
        append_jsonl(p.state / "critic.jsonl", entry)
        return entry, "Not user-facing: no critique."
    ticket = gout(cwd, "show", f"{rev}:{p.tickets_rel}/{tid}.md")
    stat = "\n".join(gout(cwd, "diff", "--stat", f"{p.upstream}...{rev}").splitlines()[-40:])
    earlier = [unscored(e["critique"]) for e in past if e.get("critique")]
    prompt = "\n\n".join(filter(None, [
        f"Critique {tid}. Follow the critic rules in your system prompt and end with the CRITIC line.",
        f"## The ticket\n{ticket[:6000]}",
        f"## The worker's pack notes\n{notes}",
        "## Pack images (Read them)\n" + "\n".join(map(str, images)) if images else "",
        f"## Files the change touches\n{stat}",
        "## Your earlier critiques of this ticket, oldest first\n" + "\n---\n".join(earlier) if earlier else "",
    ]))
    text, _, _ = run_claude(p, "critic", f"{tid}-critic", prompt, system_prompt(p, cwd), cwd, deny=DENY,
                            usage=usage, stop_if=stop_if, ticket=tid)
    v = verdict(text)
    if not v or p.norm(v[0]) != tid:
        return None, text or "The critic ended without a verdict."
    _, dims, fixes, gist = v
    critique = text[text.find("Score"):] if "Score" in text else text
    entry = {"ticket": tid, "sha": gout(cwd, "rev-parse", "--short", rev), "score": min(dims.values()),
             "dims": dims, "fixes": fixes, "revisions": n, "step": step(p, dims, fixes, n), "gist": gist[:200],
             "critique": critique, "scale": scale(p, cwd)}
    append_jsonl(p.state / "critic.jsonl", entry)
    return entry, critique


def comment(p, entry, pr):
    """A verdict that sends the work back, as a comment on the ticket's PR: someone reading GitHub sees why it
    waits (the PR body only gets the critique the worker copies there at the end). False when posting it failed."""
    if pr and entry and entry["step"] in ("revise", "rethink"):
        s = entry.get("score")
        dims = " ".join(f"{k}={v:g}" for k, v in entry.get("dims", {}).items() if k != "score")
        score = f"{'-' if s is None else f'{s:g}'}/10{f' ({dims})' if dims else ''}"
        r = net.call(["gh", "pr", "comment", pr, "--body", f"**windvane critic: {score}, {entry['step']}** "
                      f"at {entry['sha']}\n\n{entry.get('critique', '').strip()[-6000:]}"], p.main_root())
        return r.returncode == 0
    return True


def resolution(cwd, approved, head, upstream, why="conflict"):
    """What `head` holds beyond an approved head and the main it merged in, when that's all it is: a merge-conflict
    bounce (why "conflict") or a red merged tree (why "red") the worker fixed by merging main. Returns (diff, paths,
    tree) or None when anything else changed, which needs the full critic and review again.

    The diff runs from git's own merge of the approved head and that main (conflict markers and all) to `head`, so
    main's changes drop out and only the resolution is left. Every new commit off main must be a merge of main or a
    fix, and the fixes may touch only the conflicted files (for a red tree: files either side changed)."""
    ok = lambda *a: git(cwd, *a).returncode == 0
    if not approved or approved == head or not ok("merge-base", "--is-ancestor", approved, head):
        return None
    merges = 0
    for line in gout(cwd, "rev-list", "--parents", head, f"^{approved}", f"^{upstream}").splitlines():
        others = line.split()[2:]  # a merge's parents after the first: each must be on main
        if not all(ok("merge-base", "--is-ancestor", x, upstream) for x in others):
            return None
        merges += bool(others)
    base = gout(cwd, "merge-base", head, upstream)
    r = git(cwd, "merge-tree", "--write-tree", "--name-only", "--no-messages", approved, base)
    if not merges or not base or r.returncode not in (0, 1):
        return None
    tree, *conflicted = r.stdout.splitlines()
    allowed = set(filter(None, conflicted))
    if why == "red":
        fork = gout(cwd, "merge-base", approved, base)
        for side in (approved, base):
            allowed |= set(gout(cwd, "diff", "--name-only", fork, side).splitlines())
    paths = sorted(filter(None, gout(cwd, "diff", "--name-only", tree, head).splitlines()))
    if not set(paths) <= allowed:
        return None
    return gout(cwd, "diff", tree, head), paths, tree


def stands(p, tid, cwd, rev="HEAD"):
    """The approved head whose verdict still holds for `rev` (it only resolves a bounce off main), or ""."""
    ap = load_json(p.state / "state.json", {}).get("approved", {}).get(tid)
    head = gout(cwd, "rev-parse", rev)
    return ap["sha"] if ap and resolution(cwd, ap["sha"], head, p.upstream, ap.get("why")) else ""


def next_step(p, tid, entry):
    n = revisions(history(p, tid))
    return f"CRITIC STEP: {entry['step']}. " + NEXT[entry["step"]].format(
        tid=tid, n=n, max=p.critic["revisions"], floor=p.critic["floor"])
