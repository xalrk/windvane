"""The critic: a fresh, read-only session that judges what a ticket's change gives the people who use the project,
on the project's own scale (`.windvane/CRITIC.md`), harshly. The code reviewer comes after it; the critic judges the
outcome.

The worker leaves an evidence pack in `<state>/critic/<ID>/`: `notes.md` (line 1 `User-facing: yes` or
`User-facing: no (<why>)`, then what a user now gets and measured numbers) and up to a few images (screenshots,
contact sheets). It runs `windvane critic <ID>` and follows the last line, `CRITIC STEP`: pass (lowest dimension >=
pass mark), scope (under it, but nothing this ticket can fix), revise, rethink (the first dimension under the floor:
the approach is wrong), accept (out of revisions). A scale written as ladders of yes/no checks (T-054) has the critic
answer the checks and the driver work out the score; an old numeric scale has the critic write the numbers. Either
way the critic never sees the bar. Every verdict goes to `critic.jsonl`; its "Beyond this ticket" points feed
`windvane plan`.
"""
import hashlib
import re
import statistics
from pathlib import Path

from . import net
from .config import HOME
from .phone import cut
from .runner import plain_error, run_claude
from .util import append_jsonl, git, gout, load_json, read_jsonl

OLD_RE = re.compile(r"CRITIC: (\d+(?:\.\d+)?)/10 (\S+) fixes=(\d+)[ \t]*(.*)")
CRITIC_RE = re.compile(r"CRITIC: (\S+) ((?:[\w-]+=(?:\d+(?:\.\d+)?|-)[ \t]+)+?)fixes=(\d+)[ \t]*(.*)")
DIM_RE = re.compile(r"([\w-]+)=(\d+(?:\.\d+)?|-)")
CAP_RE = re.compile(r"^[ \t]*(?:[-*][ \t]*)?`?([\w-]+)[ \t]*(?:<=|≤)[ \t]*(\d+(?:\.\d+)?)`?[^:\n]*:[ \t*_]*(yes|no)\b",
                    re.I | re.M)
NEW_RE = re.compile(r"CRITIC: (\S+) fixes=(\d+)[ \t]*(.*)")
HEAD_RE = re.compile(r"^#{2,}[ \t]+`?([\w-]+)")
RUNG_RE = re.compile(r"^[ \t]*[-*][ \t]*(\d+(?:\.\d+)?)[ \t]+`?([\w-]+)`?[ \t]*:[ \t]*(.+)$")
ANSWER_RE = re.compile(r"^[ \t]*(?:[-*][ \t]*)?`?(?:[\w-]+(?:\.|[ \t]+(?=\d)))?(?:\d+(?:\.\d+)?[ \t]+)?([\w-]+)`?[ \t]*:[ \t*_]*(yes|no|-)(?![\w-])[*_]*"
                       r"(?:[ \t]*\(([ \t]*out of scope\b[^)]*)\))?[ \t,;:.—-]*(.*)$", re.I)
LISTS_RE = re.compile(r"^[ \t#*_-]*(?:fix in|beyond) this ticket\b", re.I | re.M)
CHECKS_RE = re.compile(r"^[ \t#*_]*Checks[*_]*:[*_]*[ \t]*\n?", re.I | re.M)
ANCHOR_RE = re.compile(r"^[ \t]*Nearest anchor:[ \t]*(.+)$", re.I | re.M)
DONE = ("pass", "scope", "accept", "skip")
DENY = ["Bash", "Edit", "Write", "NotebookEdit", "Agent", "WebFetch", "WebSearch"]

NEXT = {
    "pass": "Meets the bar. Go on to the check; put the critique in the PR body under \"Critic\".",
    "scope": "Under the bar, but nothing this ticket can fix. Go on; put the critique in the PR body under \"Critic\".",
    "revise": ("Fix every failed check and \"Fix in this ticket\" point, refresh the pack, and run `windvane critic {tid}` again "
               "(revision {n} of {max}). Don't run the check until the critic lets it go."),
    "rethink": ("Under {floor}: the approach itself is wrong. Rethink it as the critique says, don't patch it; refresh "
                "the pack and run `windvane critic {tid}` again (revision {n} of {max})."),
    "accept": ("Out of revisions and still under the bar. Go on; put the critique in the PR body under "
               "\"Critic (under the bar)\"."),
    "skip": "Not user-facing: go on to the check (the reviewer checks that claim).",
    "unavailable": ("No critique this time: go on to the check, and say in the PR body that the critic was "
                    "unavailable. The driver asks it again after you finish."),
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
        if m := NEW_RE.search(line):  # a ladder's line: the driver scores the checks
            return m.group(1), {}, int(m.group(2)), m.group(3)
    return None


def ladder(text):
    """A scale written as ladders: {dimension: [(rung, check id, question)]}, in the scale's order, rungs low to
    high; {} for an old numeric scale. A dimension is a `## <name>` heading and its checks are `- <rung> <id>:
    <question>` lines under it."""
    out, dim = {}, None
    for line in (text or "").splitlines():
        if m := HEAD_RE.match(line):
            dim = m.group(1).lower()
        elif dim and (m := RUNG_RE.match(line)):
            out.setdefault(dim, []).append((float(m.group(1)), m.group(2).lower(), m.group(3).strip()))
    return {d: sorted(cs, key=lambda c: c[0]) for d, cs in out.items()}


def shared(lad):
    """{check id: [dimensions]} for each id a ladder uses more than once. The critic answers by id alone, so
    `answers()` can't tell two `safe` checks apart: both get the first answer. Reported, not fatal: the scale is the
    project's file, and a typo there mustn't stop a run."""
    dims = {}
    for d, cs in lad.items():
        for _, cid, _ in cs:
            dims.setdefault(cid, []).append(d)
    return {cid: ds for cid, ds in dims.items() if len(ds) > 1}


def clip(text, n=200):
    """`text` cut to at most `n` characters at a word boundary, with "…" when cut: a cut mid-word read as a typo in
    the log ("Nothing shows the \"a few )")."""
    if len(text) <= n:
        return text
    cut = text[:n - 1]
    return (cut.rsplit(None, 1)[0] if " " in cut.strip() else cut).rstrip(" ,;:") + "…"


def rubric(p, cwd):
    f = Path(cwd) / p.rubric_rel
    return ladder(f.read_text()) if f.is_file() else {}


def answers(text):
    """The critic's answers before its CRITIC line: {check id or dimension: (answer, evidence)}, the answer `yes`,
    `no`, `scope` (`no (out of scope: <why>)`, the why kept as evidence) or `-` (a dimension the ticket doesn't
    touch). A check may be named `safe`, `works.safe`, `5 safe` or `works 5 safe`: the critic reads all of those in
    the scale, the calibration and `overview`. The first answer to an id wins: the Checks block comes first, and a
    later follow-up line that happens to read `safe: no ...` mustn't overturn it. Only the Checks block counts, up
    to the "Fix in this ticket" / "Beyond this ticket" lists after it, so a follow-up never answers a check the block
    left out, and a reply that puts a list or its verdict first keeps its answers. With no `Checks:` line, the
    lines above the first list count."""
    out = {}
    body = (text or "").rsplit("CRITIC:", 1)[0]
    if m := CHECKS_RE.search(body):
        body = body[m.end():]
    body = LISTS_RE.split(body, 1)[0]
    for line in body.splitlines():
        if m := ANSWER_RE.match(line):
            a = "scope" if m.group(3) else m.group(2).lower()
            why = re.sub(r"^\s*out of scope\b[:,\s]*", "", m.group(3) or "", flags=re.I)
            out.setdefault(m.group(1).lower(), (a, clip("; ".join(filter(None, [why.strip(), m.group(4).strip()])))))
    return out


def climb(lad, ans, bar):
    """Score a ladder from the critic's answers. A dimension's score is its highest rung whose checks and every lower
    rung's are all `yes` (0 when its first rung fails); the ticket's is the lowest dimension. A check left unanswered
    counts as `no`. A plain `no` on a rung up to the first one at or over the bar blocks: it holds the score under the
    bar, so it goes back as a fix. `no (out of scope)` never blocks; it becomes a follow-up. A dimension answered `-`
    is left out, except the first: every ticket promises something.
    Returns ({dimension: score}, {check id: answer}, [blocking (dim, rung, id, question, evidence)],
    [follow-ups], [unanswered ids])."""
    dims, said, block, scoped, missing = {}, {}, [], [], []
    for i, (d, cs) in enumerate(lad.items()):
        if i and ans.get(d, ("",))[0] == "-":
            continue
        top = next((r for r, _, _ in cs if r >= bar), cs[-1][0])
        score, held = 0.0, set()
        for r, cid, q in cs:
            a, ev = ans.get(cid, ("none", ""))
            said[cid] = a
            missing += [cid] * (a == "none")
            if a != "yes":
                held.add(r)
                if a == "scope":
                    scoped.append(f"{q} (out of scope: {ev or 'no reason given'})")
                elif r <= top:
                    block.append((d, r, cid, q, ev or "unanswered"))
        for r in sorted({r for r, _, _ in cs}):
            if r in held:
                break
            score = r
        dims[d] = score
    return dims, said, block, scoped, missing



def checks(text):
    """The critic's cap checks, answered before its scores: ([(dimension, cap, hit?)], nearest anchor or "").
    T-046: 15 of 20 tickets ended on 7 with nothing to fix, among them T-040, whose critique named a lost milestone
    (a "lose work" cap at 5) three times. Making the critic answer each cap from the calibration, line by line, puts
    those failures in front of it before it picks a number. A reply without the checks gives ([], "")."""
    body = (text or "").rsplit("CRITIC:", 1)[0]
    caps = [(d, float(n), a.lower() == "yes") for d, n, a in CAP_RE.findall(body)]
    m = ANCHOR_RE.search(body)
    return caps, (clip(m.group(1).strip()) if m else "")


def capped(dims, caps):
    """`dims` with every hit cap applied, and {dimension: the critic's own number} for those it lowered. A hit cap
    wins over the number: a critic that says "yes, it can lose work" and then writes works=7 meant works=5 (the
    calibration says so). A cap on a dimension the critic marked `-` still counts: the failure it names is real.
    `judge` also counts each hit as a fix: T-040 went on as "scope" three times with a lost-work hole named and
    nothing listed to fix, and the calibration says a hit cap belongs under "Fix in this ticket"."""
    out, was = dict(dims), {}
    for d, n, hit in caps:
        if hit and out.get(d, n + 1) > n:
            if d in out:
                was.setdefault(d, out[d])
            out[d] = n
    return out, was


def step(p, dims, fixes, revs, floor=None):
    """What a verdict leads to. The critic never sees the bar (T-022: shown it, it decided pass or fail first and
    wrote the matching number); the driver compares here. Open "Fix in this ticket" points block a pass whatever the
    score (a ticket once passed at 8 while its critique still named a crash race in its own
    code). Only the first dimension, whether the ticket works at all, can call for a rethink: a wording problem
    means revise. A ladder passes its own `floor`, its first rung: a failed safety check on works 5 holds the score
    at 2, which is a fix, not a wrong approach."""
    c = p.critic
    if fixes == 0:
        return "pass" if min(dims.values()) >= c["pass"] else "scope"
    if revs >= c["revisions"]:
        return "accept"
    return "revise" if next(iter(dims.values())) >= (c["floor"] if floor is None else floor) else "rethink"


def unscored(critique):
    """An earlier critique without its score lines, so a revision is judged afresh, not as "last time +1"."""
    return "\n".join(l for l in critique.splitlines()
                     if not re.match(r"\s*(Scores?:|CRITIC:|Nearest anchor:|Capped by the driver:|Unanswered)", l)
                     and not CAP_RE.match(l) and not ANSWER_RE.match(l)).strip()


def beyond(critique):
    m = re.search(r"^Beyond this ticket:?\s*\n((?:[ \t]*[-*\d].*\n?)*)", critique or "", re.M)
    return [re.sub(r"^[ \t]*(?:[-*]|\d+\.)\s*", "", l).strip() for l in (m.group(1) if m else "").splitlines()
            if l.strip()]


def followups(p):
    """Each ticket's "Beyond this ticket" points from its latest critique since the last plan run that used them:
    [(ticket, point)]. Only the latest, as a re-critique restates the earlier points that still hold in new words, so
    taking every critique fed the planner the same notes two to four times."""
    recs = read_jsonl(p.state / "critic.jsonl")
    last = max((i for i, r in enumerate(recs) if r.get("ticket") == "PLAN"), default=-1)
    latest = {r.get("ticket"): r for r in recs[last + 1:] if r.get("critique")}  # dicts keep first-seen order
    seen, out = set(), []
    for r in latest.values():
        for b in beyond(r.get("critique")) + r.get("scoped", []):
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
        "checked": sum(bool(r.get("checks")) for _, rs in tickets for r in rs),
        "capped": sum(bool(r.get("capped")) for _, rs in tickets for r in rs),
    }


def rates(recs, last=10):
    """Each check's answers over the last `last` tickets the critic answered checks for: {check id: (tickets whose
    first verdict said yes, tickets where any verdict did, tickets that touched it)}. The first verdict shows whether
    the check tells a weak first head from a good one; any verdict, whether a revision can reach it at all."""
    by, latest = {}, {}
    for i, r in enumerate(recs):
        if r.get("answers") and r.get("ticket") != "PLAN":
            by.setdefault(r["ticket"], []).append(r)
            latest[r["ticket"]] = i
    out = {}
    for t in sorted(by, key=latest.get)[-last:]:
        rs = by[t]
        for cid in rs[0]["answers"]:
            first, ever, n = out.get(cid, (0, 0, 0))
            out[cid] = (first + (rs[0]["answers"][cid] == "yes"),
                        ever + any(r["answers"].get(cid) == "yes" for r in rs), n + 1)
    return out


def unanswered(recs, last=10, least=3):
    """Check ids the critic left unanswered in `least` or more of its last `last` verdicts: [(id, times, verdicts)].
    Each counts as `no` and can send work back, so the same id again and again points at the check's wording or the
    critic's prompt, not at the tickets."""
    rs = [r for r in recs if "unanswered" in r and r.get("ticket") != "PLAN"][-last:]
    n = {}
    for r in rs:
        for cid in dict.fromkeys(r["unanswered"]):
            n[cid] = n.get(cid, 0) + 1
    return [(cid, k, len(rs)) for cid, k in sorted(n.items(), key=lambda x: -x[1]) if k >= least]


def advice(first, ever, n, top=False, least=5):
    """What to do about a check, from its rates, once `least` tickets touched it: one that every first head passes
    can't tell good work from weak, and one no head ever passes only holds every score down. Except the `top` rung
    of its dimension: a 10 is meant to be rare (few tickets aim for one), so "never yes" there is the scale working,
    and naming it in every `stats` and `overview` would bury the advice that matters. Left out, not reworded."""
    if n < least:
        return ""
    if first == n:
        return "yes on every ticket: sharpen it"
    return "never yes: lower it" if ever == 0 and not top else ""


def spread(xs):
    """"4-8 (median 6)": the range and middle of some scores."""
    n = lambda x: f"{x:g}"
    lo, hi = min(xs), max(xs)
    return f"{n(lo)}-{n(hi)} (median {n(statistics.median(xs))})" if lo != hi else f"all {n(lo)}"


def scale(p, cwd):
    """A short hash of the project's scale files, so verdicts can be told apart by the scale they were given on."""
    text = "".join(f.read_text() for rel in (p.rubric_rel, p.calibration_rel) if (f := Path(cwd) / rel).is_file())
    return hashlib.sha256(text.encode()).hexdigest()[:12]


def system_prompt(p, cwd):
    """The critic's rules, the scale and the calibration. An old numeric scale swaps in the numeric rules' sections
    (`prompts/CRITIC-numeric.md`), so a ladder project's critic never reads them."""
    parts = [(HOME / "prompts/CRITIC.md").read_text()]
    if not rubric(p, cwd):
        split = lambda t: re.split(r"(?m)^(?=## )", t)
        new = {x.split("\n", 1)[0]: x.rstrip() + "\n\n" for x in split((HOME / "prompts/CRITIC-numeric.md").read_text())}
        parts = ["".join(new.get(x.split("\n", 1)[0], x) for x in split(parts[0])).rstrip() + "\n"]
    for rel, head in ((p.rubric_rel, "# This project's scale"), (p.calibration_rel, "# The user's calibration")):
        f = Path(cwd) / rel
        if f.is_file():
            parts.append(f"{head}\n\n{f.read_text()}")
    return "\n\n".join(parts)


def judge(p, tid, cwd, rev="HEAD", usage=None, stop_if=None, retry=None):
    """Critique a ticket's pack at `rev` and record the verdict. Returns (record or None, reply text).
    A critic session that dies (API error, crash) is no verdict on the ticket: `retry(error)` waits and says whether
    to run it again; when it says no, the record's step is "unavailable", with no score, so it never counts as one."""
    notes, images, facing = pack(p, tid)
    if notes is None:
        return None, f"No critic pack: write {pack_dir(p, tid)}/notes.md first."
    past = history(p, tid)
    n = revisions(past)
    if not facing:
        entry = {"ticket": tid, "sha": gout(cwd, "rev-parse", "--short", rev), "score": None, "fixes": 0,
                 "revisions": n, "step": "skip", "gist": cut(notes.strip().splitlines()[0]), "critique": ""}
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
    while True:
        text, _, error = run_claude(p, "critic", f"{tid}-critic", prompt, system_prompt(p, cwd), cwd, deny=DENY,
                                    usage=usage, stop_if=stop_if, ticket=tid)
        if not error or not retry or not retry(error):
            break
    if error:
        entry = {"ticket": tid, "sha": gout(cwd, "rev-parse", "--short", rev), "score": None, "fixes": 0,
                 "revisions": n, "step": "unavailable", "gist": cut(plain_error(error)), "critique": ""}
        append_jsonl(p.state / "critic.jsonl", entry)
        return entry, f"Critic unavailable ({cut(plain_error(error))})."  # the worker reads it
    v = verdict(text)
    if not v or p.norm(v[0]) != tid:
        return None, text or "The critic ended without a verdict."
    _, dims, fixes, gist = v
    lad = rubric(p, cwd)
    if lad:
        return climbed(p, tid, cwd, rev, lad, text, fixes, gist, n)
    if not dims:
        return None, text
    caps, anchor = checks(text)
    dims, was = capped(dims, caps)
    hits = [f"{d}<={c:g}" for d, c, hit in caps if hit]
    critique = text[min((i for i in (text.find("Caps:"), text.find("Score")) if i >= 0), default=0):]
    if hits:
        low = ", ".join(f"{d} {was[d]:g} → {dims[d]:g}" for d in was)
        critique += (f"\nCapped by the driver: {', '.join(hits)} answered yes{f' ({low})' if low else ''}; "
                     "a hit cap counts as a fix.")
    entry = {"ticket": tid, "sha": gout(cwd, "rev-parse", "--short", rev), "score": min(dims.values()),
             "dims": dims, "fixes": fixes, "effective": max(fixes, len(hits)), "revisions": n, "gist": cut(gist),
             "step": step(p, dims, max(fixes, len(hits)), n),
             "critique": critique, "scale": scale(p, cwd), "checks": len(caps),
             "hits": hits, "capped": was, "anchor": anchor}
    append_jsonl(p.state / "critic.jsonl", entry)
    return entry, critique


def climbed(p, tid, cwd, rev, lad, text, fixes, gist, n):
    """judge's ending on a ladder: the driver scores the critic's check answers (`climb`), and the failed checks under
    the bar join its "Fix in this ticket" points. A reply that answers no check at all is no verdict: counting every
    check as `no` would send good work back for the critic's own slip."""
    ans = answers(text)
    if not any(cid in ans for cs in lad.values() for _, cid, _ in cs):
        return None, text or "The critic answered none of the scale's checks."
    dims, said, block, scoped, missing = climb(lad, ans, p.critic["pass"])
    eff = fixes + len(block)
    scores = " ".join(f"{d}={dims[d]:g}" if d in dims else f"{d}=-" for d in lad)
    head, _, _ = text.rpartition("CRITIC:")
    starts = [m.start() for m in (CHECKS_RE.search(head), LISTS_RE.search(head)) if m]
    critique = head[min(starts, default=0):].rstrip()  # from the Checks block, or a list the critic put before it
    if block:
        critique += "\nFailed checks under the bar (fix these too):\n" + "\n".join(
            f"- {d}.{cid} (rung {r:g}) failed: {q} ({ev})" for d, r, cid, q, ev in block)
    if missing:
        critique += f"\nUnanswered, counted as no: {', '.join(missing)}"
    critique += f"\nCRITIC: {tid} {scores} fixes={eff} {gist}"
    entry = {"ticket": tid, "sha": gout(cwd, "rev-parse", "--short", rev), "score": min(dims.values()),
             "dims": dims, "fixes": fixes, "effective": eff, "revisions": n, "gist": cut(gist),
             "step": step(p, dims, eff, n, floor=next(iter(lad.values()))[0][0]),
             "critique": critique, "scale": scale(p, cwd), "answers": said,
             "failed": [cid for _, _, cid, _, _ in block], "scoped": scoped, "unanswered": missing}
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
