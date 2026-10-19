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

`windvane critic --project` (and the `project-critic` [[periodic]] preset) runs the same scale on the whole project
as it stands on main, one session per dimension (`project`), each in project mode (`prompts/CRITIC-project.md`). Its
record (ticket PROJECT) feeds the planner only its `new` follow-ups; those an open ticket covers become that ticket's
notes.
"""
import hashlib
import os
import re
import statistics
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import net, privacy
from .config import HOME
from .phone import cut
from .runner import plain_error, run_claude
from .util import append_jsonl, git, gout, load_json, read_jsonl

OLD_RE = re.compile(r"CRITIC: (\d+(?:\.\d+)?)/10 (\S+) fixes=(\d+)[ \t]*(.*)")
CRITIC_RE = re.compile(r"CRITIC: (\S+) ((?:[\w-]+=(?:\d+(?:\.\d+)?|-)[ \t]+)+?)fixes=(\d+)[ \t]*(.*)")
DIM_RE = re.compile(r"([\w-]+)=(\d+(?:\.\d+)?|-)")
# A dimension may be written with spaces (`out of the way<=2`): `checks()` maps the label to the scale's id
CAP_RE = re.compile(r"^[ \t]*(?:[-*][ \t]*)?[`*_]*(\w[\w \t./-]*?)[`*_ \t]*(?:<=|≤)[ \t]*(\d+(?:\.\d+)?)`?[^:\n]*:"
                    r"[ \t*_]*(yes|no)\b[*_]*(?:[ \t]*\(([ \t]*out of scope\b[^)]*)\))?", re.I | re.M)
NEW_RE = re.compile(r"CRITIC: (\S+) fixes=(\d+)[ \t]*(.*)")
HEAD_RE = re.compile(r"^#{2,}[ \t]+`?([\w-]+)")
RUNG_RE = re.compile(r"^[ \t]*[-*][ \t]*(\d+(?:\.\d+)?)[ \t]+`?([\w-]+)`?[ \t]*:[ \t]*(.+)$")
# An answer is a bulleted line (`-`, `*`, `1.`): a prose line in the Checks block ("overall it's safe: yes") answers
# nothing (T-116)
BULLET = r"^[ \t]*(?:[-*]|\d+\.)[ \t]+"
ANSWER_RE = re.compile(BULLET + r"`?(?:[\w-]+(?:\.|[ \t]+(?=\d)))?(?:\d+(?:\.\d+)?[ \t]+)?([\w-]+)`?[ \t]*:[ \t*_]*(yes|no|-)(?![\w-])[*_]*"
                       r"(?:[ \t]*\(([ \t]*out of scope\b[^)]*)\))?[ \t,;:.—-]*(.*)$", re.I)
# Any label before the answer (`on the course 3 reachable: yes`); `named()` keeps those the scale knows
LABEL = (r"([^:\n]+?)[ \t]*:[ \t*_]*(yes|no|-)(?![\w-])[*_]*"
         r"(?:[ \t]*\(([ \t]*out of scope\b[^)]*)\))?[ \t,;:.—-]*(.*)$")
LABEL_RE = re.compile(BULLET + LABEL, re.I)
SAID_RE = re.compile(r"^[ \t]*(?:(?:[-*]|\d+\.)[ \t]*)?" + LABEL, re.I)  # bulleted or not: what `labels()` shows
# A follow-up list's heading ends the Checks block; a bare `Beyond:` is one too, unless it's a bulleted answer to a
# check or dimension named `beyond` (`- beyond: no, ...`), which would cut off every answer after it (T-194)
LISTS_RE = re.compile(r"^(?![ \t]*(?:[-*]|\d+\.)[ \t]+[*_`]*beyond[*_`]*:[ \t*_]*(?:yes|no|-)(?![\w-]))"
                      r"[ \t#*_-]*(?:(?:fix in|beyond) this ticket\b|beyond[*_]*:)", re.I | re.M)
CHECKS_RE = re.compile(r"^[ \t#*_]*Checks[*_]*:[*_]*[ \t]*\n?", re.I | re.M)
ANCHOR_RE = re.compile(r"^[ \t]*Nearest anchor:[ \t]*(.+)$", re.I | re.M)
DONE = ("pass", "scope", "accept", "skip")
DENY = ["Bash", "Edit", "Write", "NotebookEdit", "Agent", "WebFetch", "WebSearch"]
# The project critic runs commands for its evidence, but never changes the tree or the repo (what it does change is
# put back after it, `restore`)
PROJECT_DENY = DENY[1:] + ["Bash(git commit *)", "Bash(git push *)", "Bash(git switch *)", "Bash(git checkout *)",
                           "Bash(git reset *)", "Bash(git stash *)", "Bash(gh *)"]
PROJECT = "PROJECT"  # critic.jsonl's ticket for a verdict on the whole project
COVERED_RE = re.compile(r"[(\[]?[ \t]*covered by[ \t]+([A-Za-z]+-\d+[a-z]?)[ \t]*[)\]]?", re.I)
NEW_TAG_RE = re.compile(r"[ \t]*[(\[][ \t]*new[ \t]*[)\]][ \t.]*$", re.I)

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
    """A ticket's verdicts, oldest first. A no-verdict line (`step: none`, `none()`) is a log for `stats`, not a
    verdict: the gate and the reviewer read the last real one."""
    return [e for e in read_jsonl(p.state / "critic.jsonl") if e.get("ticket") == tid and e.get("step") != "none"]


def first_score(p, tid):
    """The first score the critic gave a ticket, or None: shown beside the final one, which alone says little
    once workers revise until they pass."""
    return next((r["score"] for r in history(p, tid) if r.get("score") is not None), None)


def trail(first, final):
    """`2→8`, or `8` when the first score was the final one."""
    return f"{final:g}" if first is None or first == final else f"{first:g}→{final:g}"


def ceilings(lad):
    """{dimension: its top rung} for each ladder that stops under 10. The ticket scores its lowest dimension, so
    one of these caps every ticket; with the pass mark at that rung, every merged ticket reads the same number."""
    return {d: cs[-1][0] for d, cs in lad.items() if cs and cs[-1][0] < 10}


def ceiling_line(p, lad):
    """overview's and `stats --critic`'s word on a capped ladder, or ""."""
    low = ceilings(lad)
    if not low:
        return ""
    cap = min(low.values())
    return (f"Critic scale: {', '.join(f'{d} tops out at {r:g}' for d, r in low.items())}, so no ticket can score "
            f"above {cap:g}{' (the pass mark, so every merged ticket scores it)' if cap <= p.critic['pass'] else ''}: "
            f"give {'each' if len(low) > 1 else 'it'} a 10 rung in {p.rubric_rel}.")


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


class Scale(dict):
    """ladder()'s {dimension: [(rung, check id, question)]}, with `heads`, {a heading's full text: dimension}: the id
    is the heading's first word, and a critic copies `## on the course` whole as its label (R-003)."""

    def __init__(self, dims=(), heads=None):
        super().__init__(dims)
        self.heads = heads or {}


def words(label):
    """A label's words, lower case, without markup (`` ` ``, `*`, `_` around a word), dots or slashes:
    `**works.shown**` and `works/shown` read as ["works", "shown"]."""
    plain = re.sub(r"(?<!\w)_+|_+(?!\w)", "", re.sub(r"[`*]", "", label))
    return re.sub(r"[./]", " ", plain.lower()).split()


def ladder(text):
    """A scale written as ladders: {dimension: [(rung, check id, question)]}, in the scale's order, rungs low to
    high; {} for an old numeric scale. A dimension is a `## <name>` heading and its checks are `- <rung> <id>:
    <question>` lines under it."""
    out, heads, dim = {}, {}, None
    for line in (text or "").splitlines():
        if m := HEAD_RE.match(line):
            dim = m.group(1).lower()
            heads[" ".join(words(line.lstrip("#")))] = dim
        elif dim and (m := RUNG_RE.match(line)):
            out.setdefault(dim, []).append((float(m.group(1)), m.group(2).lower(), m.group(3).strip()))
    return Scale({d: sorted(cs, key=lambda c: c[0]) for d, cs in out.items()},
                 {h: d for h, d in heads.items() if d in out})


def named(lad, label):
    """The check id or dimension a critic's answer label names in the scale, or None. A label ending in a check id
    answers it whatever comes before (`works shown`, `works/shown`, `on the course 3 reachable`); a label that is a
    dimension's id or its full heading answers the dimension (`on the course: -`). Critics write all of these: a
    reply in `works shown: yes` form once matched nothing and cost a second critic run (R-001)."""
    w = words(label)
    if w and w[-1] in {cid for cs in lad.values() for _, cid, _ in cs}:
        return w[-1]
    whole = " ".join(w)
    return whole if whole in lad else getattr(lad, "heads", {}).get(whole)


def dimension(lad, label):
    """The dimension a check's label names before its id (`works safe`, `works.5.safe`, `on the course 3 reachable`),
    or None: how `answers()` tells two `safe` checks apart."""
    w = [x for x in words(label)[:-1] if not re.fullmatch(r"\d+(?:\.\d+)?", x)]
    whole = " ".join(w)
    return whole if whole in lad else getattr(lad, "heads", {}).get(whole)


def shared(lad):
    """{check id: [dimensions]} for each id a ladder uses more than once. A label that names the dimension
    (`works safe`) answers only that one; the id alone answers both (`answers()`). Reported, not fatal: the scale is
    the project's file, and a typo there mustn't stop a run."""
    dims = {}
    for d, cs in lad.items():
        for _, cid, _ in cs:
            dims.setdefault(cid, []).append(d)
    return {cid: ds for cid, ds in dims.items() if len(ds) > 1}


def reply_words():
    """{word: the reply part it starts} for each word the critic's reply format gives a meaning of its own: the
    first word of each heading in the reply shape `prompts/CRITIC.md` asks for (`Checks:`, `Fix in this ticket:`,
    `Beyond this ticket:`, `CRITIC:`, ...) and each key its CRITIC line writes (`fixes=`). Read from the prompt, so a
    heading added there is covered too. An id that is one of them can read as that part of the reply: a check named
    `beyond` cut off every answer after it for three review rounds (T-194)."""
    text = (HOME / "prompts/CRITIC.md").read_text()
    shape = re.search(r"^## Your reply\n.*?^```\n(.*?)^```", text, re.M | re.S)
    out = {}
    for line in shape.group(1).splitlines() if shape else []:
        if m := re.match(r"([A-Za-z][\w-]*)[A-Za-z \t]*:", line):
            out.setdefault(m.group(1).lower(), line.split(":", 1)[0] + ":")
            for key in re.findall(r"([A-Za-z][\w-]*)=", line):
                out.setdefault(key.lower(), f"{key}=")
    return out


def misread(text, rel):
    """[(what, fix)] for each id in a ladder scale that parses but will be misread: an id that is a word of the reply
    format (`reply_words`), two checks of one dimension with one id (the first answer scores both), and two headings
    with one id (their checks merge into one ladder). Warned at doctor and the run's start, never refused: an id that
    reads right today keeps working. `rel` is the scale's path, for the fix."""
    lad, reserved, out = ladder(text), reply_words(), []
    for d, cs in lad.items():
        ids = [cid for _, cid, _ in cs]
        for kind, x in [("dimension", d)] + [("check", cid) for cid in dict.fromkeys(ids)]:
            if x in reserved:
                where = "" if kind == "dimension" else f" (in {d})"
                out.append((f"critic scale: the {kind} id `{x}`{where} is a word of the critic's reply format "
                            f"(`{reserved[x]}`), so an answer to it can read as that part of the reply",
                            f"rename it in {rel}"))
        for cid in [c for c in dict.fromkeys(ids) if ids.count(c) > 1]:
            out.append((f"critic scale: dimension {d} has {ids.count(cid)} checks named `{cid}`, so one answer "
                        "scores them all", f"rename one in {rel}"))
    heads, n = {}, 0  # {heading's place: its id}, for the headings with rungs under them
    for line in (text or "").splitlines():
        if m := HEAD_RE.match(line):
            n, d = n + 1, m.group(1).lower()
        elif n and RUNG_RE.match(line):
            heads[n] = d
    heads = list(heads.values())
    for d in [d for d in dict.fromkeys(heads) if heads.count(d) > 1]:
        out.append((f"critic scale: {heads.count(d)} headings read as dimension `{d}` (its first word), so their "
                    "checks merge into one ladder", f"give each its own first word in {rel}"))
    return out


def scale_warnings(p, cwd):
    """`misread()` on the project's scale in `cwd`, or [] when it has none."""
    f = Path(cwd) / p.rubric_rel
    return misread(f.read_text(), p.rubric_rel) if f.is_file() else []


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


def block(text):
    """The lines that may answer checks: the Checks block, up to the "Fix in this ticket" / "Beyond this ticket"
    lists after it, so a follow-up never answers a check the block left out, and a reply that puts a list or its
    verdict first keeps its answers. With no `Checks:` line, the lines above the first list count."""
    body = (text or "").rsplit("CRITIC:", 1)[0]
    if m := CHECKS_RE.search(body):
        body = body[m.end():]
    return LISTS_RE.split(body, 1)[0].splitlines()


def answers(text, lad=None):
    """The critic's answers before its CRITIC line: {check id or dimension: (answer, evidence)}, the answer `yes`,
    `no`, `scope` (`no (out of scope: <why>)`, the why kept as evidence) or `-` (a dimension the ticket doesn't
    touch). With the scale `lad`, each label is read by what it names in it (`named`); without, a check may be
    named `safe`, `works.safe`, `5 safe` or `works 5 safe`. The first answer to an id wins: the Checks block comes
    first, and a later follow-up line that happens to read `safe: no ...` mustn't overturn it. Only `block()`'s
    bulleted lines count. An id two dimensions share is keyed (dimension, id): a label naming the dimension answers
    that one, the id alone every one not answered yet (T-116)."""
    out, both = {}, shared(lad) if lad else {}
    for line in block(text):
        m = LABEL_RE.match(line) if lad else ANSWER_RE.match(line)
        key = m and (named(lad, m.group(1)) if lad else m.group(1).lower())
        if key:
            a = "scope" if m.group(3) else m.group(2).lower()
            why = re.sub(r"^\s*out of scope\b[:,\s]*", "", m.group(3) or "", flags=re.I)
            got = (a, clip("; ".join(filter(None, [why.strip(), m.group(4).strip()]))))
            if key in both:
                d = dimension(lad, m.group(1))
                for k in [(d, key)] if d in both[key] else [(x, key) for x in both[key]]:
                    out.setdefault(k, got)
            else:
                out.setdefault(key, got)
    return out


def labels(text):
    """The labels of the answer-shaped lines in the Checks block, matched to the scale or not: what a no-verdict
    message shows, so the worker sees how the critic wrote them."""
    return [m.group(1).strip() for line in block(text) if (m := SAID_RE.match(line))]


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
            a, ev = ans.get((d, cid)) or ans.get(cid, ("none", ""))
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
    those failures in front of it before it picks a number. A reply without the checks gives ([], "").
    T-109: a cap answered `yes (out of scope: <why>)` has its why, not True, as hit: it still caps the dimension, but
    `judge` doesn't count it as a fix (a ticket whose cap was a choice the project made, such as merging as a step,
    went back three times with nothing to fix)."""
    body = (text or "").rsplit("CRITIC:", 1)[0]
    v = verdict(text or "")
    dims = list(v[1]) if v else []
    caps = []
    for d, n, a, scope in CAP_RE.findall(body):
        hit = a.lower() == "yes"
        if hit and scope:
            hit = re.sub(r"^\s*out of scope\b[:,\s]*", "", scope, flags=re.I).strip() or "no reason given"
        if d := dim_id(d, dims):
            caps.append((d, float(n), hit))
    m = ANCHOR_RE.search(body)
    return caps, (clip(m.group(1).strip()) if m else "")


def dim_id(label, dims):
    """The scale's id for a cap's dimension label: spaces and hyphens are the same (`out of the way` is
    `out-of-the-way`), and a heading's full text names its first word's id (`on the course` is `on`), as T-108 reads
    check labels. A one-word label the CRITIC line doesn't score stays as it is (a cap on an untouched dimension still
    counts); a longer one is None, so a sentence like `- caps at works<=5 (x): yes` is no cap."""
    w = words(label)
    whole, keys = "-".join(w), {"-".join(words(d.replace("-", " "))): d for d in dims}
    if whole in keys:
        return keys[whole]
    return keys.get(w[0]) or (whole if len(w) == 1 else None)


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


def unscored(critique, lad=None):
    """An earlier critique without its score lines, so a revision is judged afresh, not as "last time +1". With the
    scale, an answer goes in any form `answers()` reads."""
    def answer(l):
        return bool((m := LABEL_RE.match(l)) and named(lad, m.group(1))) if lad else bool(ANSWER_RE.match(l))
    return "\n".join(l for l in critique.splitlines()
                     if not re.match(r"\s*(Scores?:|CRITIC:|Nearest anchor:|Capped by the driver:|Unanswered)", l)
                     and not CAP_RE.match(l) and not answer(l)).strip()


def beyond(critique):
    m = re.search(r"^Beyond(?: this ticket)?:?\s*\n((?:[ \t]*[-*\d].*\n?)*)", critique or "", re.M)
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
    for r in latest.values():  # a project verdict's covered points are its tickets' notes already
        for b in r.get("new", []) if r.get("ticket") == PROJECT else beyond(r.get("critique")) + r.get("scoped", []):
            if b.lower() not in seen:
                seen.add(b.lower())
                out.append((r.get("ticket"), b))
    return out


def ticketed(r):
    """A record about one ticket: not a plan's marker nor a verdict on the whole project, which would skew the
    ticket critic's calibration."""
    return r.get("ticket") not in ("PLAN", PROJECT)


def calibration(recs, bar, last=10):
    """How the critic's scores spread over the last `last` tickets it scored, to show whether the calibration works:
    in the first self-run every final score sat exactly on the pass mark. Not-user-facing skips and plan markers carry
    no score and are left out. Returns None when nothing was scored."""
    by, latest = {}, {}
    for i, r in enumerate(recs):
        if r.get("score") is not None and ticketed(r):
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
        # T-109: a critic that waves caps through as out of scope shows here beside the ones it sent back
        "hits": sum(len(r.get("hits") or []) for _, rs in tickets for r in rs),
        "scoped_caps": sum(len(r.get("scoped") or []) for _, rs in tickets for r in rs if "hits" in r),
    }


def rates(recs, last=10):
    """Each check's answers over the last `last` tickets the critic answered checks for: {check id: (tickets whose
    first verdict said yes, tickets where any verdict did, tickets that touched it)}. The first verdict shows whether
    the check tells a weak first head from a good one; any verdict, whether a revision can reach it at all."""
    by, latest = {}, {}
    for i, r in enumerate(recs):
        if r.get("answers") and ticketed(r):
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
    rs = [r for r in recs if "unanswered" in r and ticketed(r)][-last:]
    n = {}
    for r in rs:
        for cid in dict.fromkeys(r["unanswered"]):
            n[cid] = n.get(cid, 0) + 1
    return [(cid, k, len(rs)) for cid, k in sorted(n.items(), key=lambda x: -x[1]) if k >= least]


def advice(first, ever, n, top=False, least=5):
    """What to do about a check, from its rates, once `least` tickets touched it: one that every first head passes
    can't tell good work from weak, and one no head ever passes only holds every score down. Except a `top` rung,
    one over the pass mark or its dimension's last: work beyond the bar is meant to be rare (workers stop revising at the bar), so "never
    yes" there is the scale working, and naming it in every `stats` and `overview` would bury the advice that
    matters. Left out, not reworded."""
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


def system_prompt(p, cwd, project=False):
    """The critic's rules, the scale and the calibration. An old numeric scale swaps in the numeric rules' sections
    (`prompts/CRITIC-numeric.md`), so a ladder project's critic never reads them; the project critic adds its few
    lines of project mode, so the ticket critic never reads those."""
    parts = [(HOME / "prompts/CRITIC.md").read_text()]
    if not rubric(p, cwd):
        split = lambda t: re.split(r"(?m)^(?=## )", t)
        new = {x.split("\n", 1)[0]: x.rstrip() + "\n\n" for x in split((HOME / "prompts/CRITIC-numeric.md").read_text())}
        parts = ["".join(new.get(x.split("\n", 1)[0], x) for x in split(parts[0])).rstrip() + "\n"]
    if project:
        parts.append((HOME / "prompts/CRITIC-project.md").read_text())
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
        return none(p, tid, "", f"no critic pack. Write {pack_dir(p, tid)}/notes.md, then run `windvane critic {tid}`.",
                    rerun=False)
    past = history(p, tid)
    n = revisions(past)
    if not facing:
        entry = {"ticket": tid, "sha": gout(cwd, "rev-parse", "--short", rev), "score": None, "fixes": 0,
                 "revisions": n, "step": "skip", "gist": cut(notes.strip().splitlines()[0]), "critique": ""}
        append_jsonl(p.state / "critic.jsonl", entry)
        return entry, "Not user-facing: no critique."
    ticket = gout(cwd, "show", f"{rev}:{p.tickets_rel}/{tid}.md")
    stat = "\n".join(gout(cwd, "diff", "--stat", f"{p.upstream}...{rev}").splitlines()[-40:])
    lad = rubric(p, cwd)
    earlier = [unscored(e["critique"], lad) for e in past if e.get("critique")]
    prompt = "\n\n".join(filter(None, [
        f"Critique {tid}. Follow the critic rules in your system prompt and end with the CRITIC line.",
        f"## The ticket\n{ticket[:6000]}",
        f"## The worker's pack notes\n{notes}",
        "## Pack images (Read them)\n" + "\n".join(map(str, images)) if images else "",
        f"## Files the change touches\n{stat}",
        "## Your earlier critiques of this ticket, oldest first\n" + "\n---\n".join(earlier) if earlier else "",
    ]))
    session = {}
    while True:
        text, _, error = run_claude(p, "critic", f"{tid}-critic", prompt, system_prompt(p, cwd), cwd, deny=DENY,
                                    usage=usage, stop_if=stop_if, ticket=tid, session=session)
        if not error or not retry or not retry(error):
            break
    if error:
        entry = {"ticket": tid, "sha": gout(cwd, "rev-parse", "--short", rev), "score": None, "fixes": 0,
                 "revisions": n, "step": "unavailable", "gist": cut(plain_error(error)), "critique": ""}
        append_jsonl(p.state / "critic.jsonl", entry)
        return entry, f"Critic unavailable ({cut(plain_error(error))})."  # the worker reads it
    why, asked = unread(p, tid, lad, text), {}
    if why and session.get("id"):  # one more turn in the same session reads its context cached, not fresh (T-116)
        again, killed, error = run_claude(p, "critic", f"{tid}-critic-reask", reask(tid, why, lad),
                                          system_prompt(p, cwd), cwd, deny=DENY, usage=usage, stop_if=stop_if,
                                          ticket=tid, resume=session["id"])
        if killed or error:  # no reply to it: the first reply stands, and the log says the re-ask failed
            asked = {"reask_failed": cut(killed or plain_error(error))}
        else:
            text, why, asked = again, unread(p, tid, lad, again), {"reasked": True}
    if why:
        return none(p, tid, text, why, **asked)
    _, dims, fixes, gist = verdict(text)
    saved = {"reasked": True} if asked else {}  # the re-ask saved this verdict: `stats --critic` counts it
    if lad:
        return climbed(p, tid, cwd, rev, lad, text, fixes, gist, n, saved)
    caps, anchor = checks(text)
    dims, was = capped(dims, caps)
    hits = [f"{d}<={c:g}" for d, c, hit in caps if hit is True]
    scoped = [f"{d}<={c:g} (out of scope: {hit})" for d, c, hit in caps if isinstance(hit, str)]
    critique = text[min((i for i in (text.find("Caps:"), text.find("Score")) if i >= 0), default=0):]
    if hits or scoped:
        low = ", ".join(f"{d} {was[d]:g} → {dims[d]:g}" for d in was)
        said = [f"{', '.join(hits)} answered yes"] if hits else []
        said += [f"{', '.join(s.split(' ', 1)[0] for s in scoped)} answered yes, out of scope"] if scoped else []
        why = "; ".join((["a hit cap counts as a fix"] if hits else [])
                        + (["out of scope caps the score but isn't a fix"] if scoped else []))
        critique += f"\nCapped by the driver: {'; '.join(said)}{f' ({low})' if low else ''}; {why}."
    entry = {"ticket": tid, "sha": gout(cwd, "rev-parse", "--short", rev), "score": min(dims.values()),
             "dims": dims, "fixes": fixes, "effective": max(fixes, len(hits)), "revisions": n, "gist": cut(gist),
             "step": step(p, dims, max(fixes, len(hits)), n),
             "critique": critique, "scale": scale(p, cwd), "checks": len(caps),
             "hits": hits, "scoped": scoped, "capped": was, "anchor": anchor, **saved}
    append_jsonl(p.state / "critic.jsonl", entry)
    return entry, critique


def unread(p, tid, lad, text):
    """Why the critic's reply is no verdict, or None when the driver can read one. A ladder reply that answers no
    check at all is none: counting every check as `no` would send good work back for the critic's own slip."""
    v = verdict(text)
    if not v:
        return "the critic's reply has no CRITIC line."
    if p.norm(v[0]) != tid:
        return f"the critic's CRITIC line is for {v[0]}, not {tid}."
    if not lad:
        return None if v[1] else "the critic's CRITIC line has no scores, and the scale has no checks to work them out."
    ans = answers(text, lad)
    if any(cid in ans or (d, cid) in ans for d, cs in lad.items() for _, cid, _ in cs):
        return None
    seen = labels(text)
    return ("no answer the driver could match to a check (labels it saw: "
            f"{', '.join(seen[:5]) + ('…' if len(seen) > 5 else '') if seen else 'none'}; the scale's ids: "
            f"{', '.join(cid for cs in lad.values() for _, cid, _ in cs)}).")


def reask(tid, why, lad):
    """The one follow-up turn a reply the driver can't read gets, in the critic's own session: a fresh critic run
    cost the worker a whole second review for a slip of format (T-116)."""
    shape = ", each check on its own line labelled by its id: `- <id>: yes|no, <evidence>`" if lad else ""
    return (f"The driver couldn't read that reply: {why} Write your whole reply again in the shape your rules "
            f"give{shape}, ending with its CRITIC line for {tid}.")


def climbed(p, tid, cwd, rev, lad, text, fixes, gist, n, saved=None):
    """judge's ending on a ladder: the driver scores the critic's check answers (`climb`), and the failed checks under
    the bar join its "Fix in this ticket" points."""
    ans = answers(text, lad)
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
             "failed": [cid for _, _, cid, _, _ in block], "scoped": scoped, "unanswered": missing,
             **(saved or {})}
    append_jsonl(p.state / "critic.jsonl", entry)
    return entry, critique


def none(p, tid, text, why, rerun=True, reasked=False, reask_failed=None):
    """judge's (None, reply) when there's no verdict, the reply ending in the step that says why. Only a missing pack
    is the worker's to fix: a reply the driver can't read is the critic's slip, and "fix the pack" sent a worker with
    a sound pack to pay for a second run (R-001). Logged as `step: none`, so `windvane stats` can count what no
    verdict costs: `reasked` only when the same-session re-ask replied, `reask_failed` why one didn't (an API error,
    the deadline). `history()` leaves these lines out."""
    append_jsonl(p.state / "critic.jsonl", {"ticket": tid, "step": "none", "why": why, "reasked": reasked,
                                            **({"reask_failed": reask_failed} if reask_failed else {})})
    todo = (f" Your pack isn't the problem: run `windvane critic {tid}` again; if the critic keeps failing, go on."
            if rerun else "")
    return None, f"{text}\n\nCRITIC STEP: none. No verdict: {why}{todo}".lstrip()


def comment(p, entry, pr):
    """A verdict that sends the work back, as a comment on the ticket's PR: someone reading GitHub sees why it
    waits (the PR body only gets the critique the worker copies there at the end). False when posting it failed."""
    if pr and entry and entry["step"] in ("revise", "rethink"):
        s = entry.get("score")
        dims = " ".join(f"{k}={v:g}" for k, v in entry.get("dims", {}).items() if k != "score")
        score = f"{'-' if s is None else f'{s:g}'}/10{f' ({dims})' if dims else ''}"
        r = net.call(["gh", "pr", "comment", pr, "--body", privacy.censor(
            f"**windvane critic: {score}, {entry['step']}** at {entry['sha']}\n\n"
            f"{entry.get('critique', '').strip()[-6000:]}")], p.main_root())
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


def project(p, cwd, run, dims=(), parallel=4):
    """The critic on the whole project at `cwd`'s HEAD: one session per dimension of the ladder (or of `dims`), up to
    `parallel` at once, each through `run(role, label, prompt, system, allow, deny)` -> (text, killed, error). Joined
    into one report, recorded in `critic.jsonl` (ticket PROJECT) with a `critic:project` score line in the ledger.
    Whatever a session changed in the tree is put back. Returns (record or None, the report or why there's none)."""
    from .queue import Queue
    lad = rubric(p, cwd)
    if not lad:
        return None, f"No ladder scale in {p.rubric_rel}: the project critic answers a ladder's checks."
    unknown = [d for d in dims if d not in lad]
    if unknown:
        return None, f"No dimension {', '.join(unknown)} in {p.rubric_rel} (it has: {', '.join(lad)})."
    pick = [d for d in lad if not dims or d in dims]
    q = Queue(p)
    open_ids = {t for t in q.files if not q.is_done(t)}
    listing = "\n".join(line for line in q.listing().splitlines() if line.split()[0] in open_ids)
    sha, system = gout(cwd, "rev-parse", "--short", "HEAD"), system_prompt(p, cwd, project=True)

    def one(d):
        prompt = "\n\n".join([
            f"Critique the project on {p.upstream} at {sha}: the {d} dimension only. Follow the critic rules in "
            "your system prompt, in project mode, and end with the CRITIC line.",
            f"## The {d} checks\n" + "\n".join(f"- {r:g} {cid}: {qn}" for r, cid, qn in lad[d]),
            f"## Open tickets (id, status, title)\n{listing or '(none)'}"])
        return run("critic:project", f"project-critic-{d}", prompt, system, ["Bash"], PROJECT_DENY)
    before = snapshot(cwd)
    try:
        with ThreadPoolExecutor(max_workers=max(1, min(parallel, len(pick)))) as ex:
            got = dict(zip(pick, ex.map(one, pick)))
    finally:
        restore(cwd, before)
    entry, report = joined(p, lad, pick, got, open_ids, sha)
    entry["scale"] = scale(p, cwd)
    append_jsonl(p.state / "critic.jsonl", entry)
    if entry["dims"]:
        append_jsonl(p.state / "ledger.jsonl", {"role": "critic:project", "event": "score", "score": entry["score"],
                                                "dims": entry["dims"], "summary": cut(report.splitlines()[0])})
    return entry, report


def joined(p, lad, pick, got, open_ids, sha):
    """The dimension sessions' replies as one record and report: each check's answer with its evidence, a joined
    Beyond list split into `new` points and those an open ticket covers ({ticket: [points]}), and the For-the-user
    lines. A dimension whose session failed or answered none of its checks is named, not scored."""
    dims, said, sections, users, failed, points = {}, {}, [], [], [], []
    for d in pick:
        text, killed, error = got[d]
        ans = answers(text, lad)
        if killed or error or not any(cid in ans for _, cid, _ in lad[d]):
            why = killed or (plain_error(error) if error else "it answered none of the checks")
            failed.append(d)
            sections.append(f"## {d}: no verdict ({cut(why)})")
            continue
        score, more, _, scoped, _ = climb({d: lad[d]}, ans, p.critic["pass"])
        dims[d] = score[d]
        said.update(more)
        lines = [f"- {cid} ({r:g}): {a}{f', {ev}' if ev else ''}" for r, cid, _ in lad[d]
                 for a, ev in [ans.get(cid, ("unanswered, counted as no", ""))]]
        sections.append(f"## {d}: {score[d]:g}\n" + "\n".join(lines))
        points += beyond(text) + scoped
        users += re.findall(r"^[ \t*_]*For the user:[*_]*[ \t]*(.+)$", text, re.M)
    new, covered, seen = [], {}, set()
    for b in points:
        m = COVERED_RE.search(b)
        point = NEW_TAG_RE.sub("", COVERED_RE.sub("", b)).strip(" ,;.") or b
        tid = p.norm(m.group(1)) if m else None
        if point.lower() in seen:
            continue
        seen.add(point.lower())
        if tid in open_ids:
            covered.setdefault(tid, []).append(point)
        else:
            new.append(point)
    scores = " ".join(f"{d}={dims[d]:g}" if d in dims else f"{d}=-" for d in pick)
    head = (f"Project critic on {p.upstream} at {sha}: {scores}; {len(new)} new follow-up{'s' * (len(new) != 1)}, "
            f"{sum(map(len, covered.values()))} covered by open tickets")
    report = "\n\n".join(filter(None, [
        head, *sections,
        new and "Beyond (new):\n" + "\n".join(f"- {b}" for b in new),
        covered and "Beyond (covered by open tickets):\n" + "\n".join(
            f"- {t}: {b}" for t, bs in covered.items() for b in bs),
        users and "For the user:\n" + "\n".join(f"- {u}" for u in dict.fromkeys(users))]))
    entry = {"ticket": PROJECT, "role": "project", "sha": sha, "score": min(dims.values()) if dims else None,
             "dims": dims, "step": "project" if dims else "unavailable", "answers": said, "new": new,
             "covered": covered, "for_user": list(dict.fromkeys(users)), "failed": failed, "critique": report}
    return entry, report


def snapshot(cwd):
    """`cwd` as it is: (its files as a tree object, staged in a copy of its index so the real one is never touched;
    its HEAD; its index's bytes)."""
    real = Path(cwd) / gout(cwd, "rev-parse", "--git-path", "index")
    index = real.read_bytes() if real.is_file() else None
    fd, idx = tempfile.mkstemp(prefix="windvane-index-")
    os.close(fd)
    if index is not None:
        Path(idx).write_bytes(index)
    try:
        index_git(cwd, idx, "add", "-A")
        return index_git(cwd, idx, "write-tree").stdout.strip(), gout(cwd, "rev-parse", "HEAD"), index
    finally:
        os.unlink(idx)


def restore(cwd, before):
    """Put `cwd` back as `snapshot` found it: files edited, deleted or added by a session that ran commands go back,
    a commit it made is undone and what it staged unstaged. Ignored files (caches, build output) stay."""
    tree, head, index = before
    if gout(cwd, "rev-parse", "HEAD") != head:
        git(cwd, "reset", "-q", "--soft", head)
    real = Path(cwd) / gout(cwd, "rev-parse", "--git-path", "index")
    if index is not None and (not real.is_file() or real.read_bytes() != index):
        real.write_bytes(index)
    if not tree or snapshot(cwd)[0] == tree:
        return
    fd, idx = tempfile.mkstemp(prefix="windvane-index-")
    os.close(fd)
    try:
        for args in (("read-tree", tree), ("checkout-index", "-a", "-f"), ("clean", "-qfd")):
            index_git(cwd, idx, *args)
    finally:
        os.unlink(idx)


def index_git(cwd, idx, *args):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                          env={**os.environ, "GIT_INDEX_FILE": idx})


def project_scores(p, last=10):
    """`windvane critic --project --last`: the latest project verdicts, oldest first, one line each with every
    dimension's score, so a trend shows."""
    recs = [r for r in read_jsonl(p.state / "critic.jsonl") if r.get("ticket") == PROJECT][-last:]
    if not recs:
        return "no project critic verdicts yet: run `windvane critic --project`"
    out = [f"project critic, last {len(recs)} verdict{'s' * (len(recs) != 1)} (pass mark {p.critic['pass']:g}):"]
    for r in recs:
        scores = " ".join(f"{d}={s:g}" for d, s in r.get("dims", {}).items()) or "no verdict"
        failed = f", no verdict on {', '.join(r['failed'])}" if r.get("failed") and r.get("dims") else ""
        out.append(f"  {r.get('at', '')}  {r.get('sha', ''):9} {scores}  ({len(r.get('new', []))} new, "
                   f"{sum(map(len, r.get('covered', {}).values()))} covered{failed})")
    return "\n".join(out)
