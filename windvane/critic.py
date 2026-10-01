"""The critic: a fresh, read-only session that judges what a ticket's change gives the people who use the project,
0 to 10 on the project's own scale (`.windvane/CRITIC.md`), harshly. The code reviewer comes after it; the critic
judges the outcome.

The worker leaves an evidence pack in `<state>/critic/<ID>/`: `notes.md` (line 1 `User-facing: yes` or
`User-facing: no (<why>)`, then what a user now gets and measured numbers) and up to a few images (screenshots,
contact sheets). It runs `windvane critic <ID>` and follows the last line, `CRITIC STEP`: pass (>= pass mark), scope
(under it, but nothing this ticket can fix), revise, rethink (under the floor: the approach is wrong), accept (out of
revisions). Every verdict goes to `critic.jsonl`; its "Beyond this ticket" points feed `windvane plan`.
"""
import re
from pathlib import Path

from .config import HOME
from .runner import run_claude
from .util import append_jsonl, gout, last_match, read_jsonl

CRITIC_RE = re.compile(r"CRITIC: (\d+(?:\.\d+)?)/10 (\S+) fixes=(\d+)[ \t]*(.*)")
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


def step(p, score, fixes, revs):
    c = p.critic
    if score >= c["pass"]:
        return "pass"
    if fixes == 0:
        return "scope"
    if revs >= c["revisions"]:
        return "accept"
    return "revise" if score >= c["floor"] else "rethink"


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


def system_prompt(p, cwd):
    parts = [(HOME / "prompts/CRITIC.md").read_text()]
    for rel, head in ((p.rubric_rel, "# This project's scale"), (p.calibration_rel, "# The user's calibration")):
        f = Path(cwd) / rel
        if f.is_file():
            parts.append(f"{head}\n\n{f.read_text()}")
    return "\n\n".join(parts).replace("{pass}", str(p.critic["pass"])).replace("{floor}", str(p.critic["floor"]))


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
    earlier = [e["critique"] for e in past if e.get("critique")]
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
    m = last_match(CRITIC_RE, text)
    if not m or p.norm(m.group(2)) != tid:
        return None, text or "The critic ended without a verdict."
    score, fixes = float(m.group(1)), int(m.group(3))
    critique = text[text.find("Score:"):] if "Score:" in text else text
    entry = {"ticket": tid, "sha": gout(cwd, "rev-parse", "--short", rev), "score": score, "fixes": fixes,
             "revisions": n, "step": step(p, score, fixes, n), "gist": m.group(4)[:200], "critique": critique}
    append_jsonl(p.state / "critic.jsonl", entry)
    return entry, critique


def next_step(p, tid, entry):
    n = revisions(history(p, tid))
    return f"CRITIC STEP: {entry['step']}. " + NEXT[entry["step"]].format(
        tid=tid, n=n, max=p.critic["revisions"], floor=p.critic["floor"])
