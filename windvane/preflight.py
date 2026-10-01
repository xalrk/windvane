"""Preflight: the product calls the next tickets will need, asked while the user is still at the desk.

One cheap read-only session (`prompts/PREFLIGHT.md`, role `preflight` in the ledger) reads the tickets likely to run
before the deadline and lists the questions their text makes predictable. Questions still waiting for the user join
them: a ticket's open phone question, and provisional answers built without them. Answers land under `## Answered` in
`.windvane/QUESTIONS.md` on main, where the ticket's worker finds them in its prompt and builds them without asking.
"""
import fcntl
import math
import re
from datetime import datetime, timedelta

from .driver import REVIEW_DENY, clean_detach, ensure_worktree, land, rules
from .questions import OPT_RE, blocks, give_answer, part
from .queue import Queue
from .runner import run_claude
from .util import git, gout, load_json, log, now, read_jsonl, save_json

PREFLIGHT_RE = re.compile(r"^PREFLIGHT: (\d+|NONE)", re.M)



def minutes_per_ticket(p, default=60.0):
    """Session minutes per ticket over the last two weeks of the ledger (worker, critic, reviewer runs together)."""
    since = f"{now() - timedelta(days=14):%Y-%m-%d %H:%M}"
    per = {}
    for r in read_jsonl(p.state / "ledger.jsonl"):
        if r.get("ticket") and r.get("at", "") >= since:
            per[r["ticket"]] = per.get(r["ticket"], 0) + (r.get("minutes") or 0)
    return max(10.0, sum(per.values()) / len(per)) if per else default


def likely(p, q, hours, parallel=None):
    """The tickets the run will likely reach in `hours`, in queue order (at most eight: the prompt stays small)."""
    slots = max(1, parallel or p.ap["parallel"]) if p.cfg["isolate"] else 1
    n = max(1, min(8, math.ceil(hours * 60 * slots / minutes_per_ticket(p))))
    return [t for t in q.order if q.status(t) not in ("done", "parked")][:n]


def question(tid, q, body, kind, n):
    opts = dict(OPT_RE.findall(body))
    rec = re.search(r"^Recommendation:\s*\**([A-E])\b[^\w]*(.*)$", body, re.M)
    built = re.search(r"^Built:\s*([A-E])", body, re.M)
    context = "\n".join(l for l in body.splitlines()
                        if l.strip() and not re.match(r"- \*\*[A-E]\*\*|Recommendation:|Built:|Waits for", l))
    return {"id": f"{tid}.{n}", "ticket": tid, "kind": kind, "question": q, "context": context.strip(),
            "options": opts, "recommendation": rec.group(1) if rec else "", "why": rec.group(2).strip() if rec else "",
            "built": built.group(1) if built else ""}


def waiting(p, qtext):
    """Questions already asked that still wait for the user: open phone questions, provisional answers."""
    out = []
    for tid, q in load_json(p.state / "state.json", {}).get("questions", {}).items():
        out.append({"id": tid, "ticket": tid, "kind": "open", "question": q["q"], "context": "",
                    "options": q["opts"], "recommendation": q["rec"], "why": "", "built": ""})
    seen = {}
    for tid, q, body in blocks(part(qtext, "Provisional")):
        seen[tid] = seen.get(tid, 0) + 1
        out.append(question(tid, q, body, "provisional", f"p{seen[tid]}"))
    return out


def find(p, hours, usage=None, parallel=None):
    """Run the preflight session; returns the questions to ask (new ones, then those still waiting) and saves them
    for `windvane answer`."""
    git(p.main_root(), "fetch", "-q", p.remote)
    q = Queue(p)
    qtext = gout(p.main_root(), "show", f"{p.upstream}:{p.questions_rel}")
    out = waiting(p, qtext)
    tids = [t for t in likely(p, q, hours, parallel) if t not in {x["ticket"] for x in out if x["kind"] == "open"}]
    if tids:
        claude_md = gout(p.main_root(), "show", f"{p.upstream}:CLAUDE.md")
        prompt = "\n\n".join(filter(None, [
            "Preflight. Follow the preflight rules in your system prompt and end with the PREFLIGHT line.",
            f"## CLAUDE.md\n{claude_md[:6000]}" if claude_md else "",
            f"## .windvane/QUESTIONS.md\n{qtext[:8000]}" if qtext else "",
            "## The tickets, in the order they'll run\n"
            + "\n\n".join(f"<ticket id={t}>\n{q.text[t][:5000]}\n</ticket>" for t in tids),
        ]))
        log(p, f"preflight run ({len(tids)} tickets: {', '.join(tids)})")
        text, killed, error = run_claude(p, "preflight", "preflight", prompt, rules(p, "PREFLIGHT.md"), p.main_root(),
                                         deny=REVIEW_DENY, usage=usage)
        if killed or error or not PREFLIGHT_RE.search(text):
            log(p, f"preflight: no list ({killed or error or 'no PREFLIGHT line'})")
        n = {}
        for tid, qq, body in blocks(text.split("\nPREFLIGHT:", 1)[0]):
            tid = p.norm(tid)
            if tid in tids and len(OPT_RE.findall(body)) >= 2:
                n[tid] = n.get(tid, 0) + 1
                out.insert(sum(x["kind"] == "new" for x in out), question(tid, qq, body, "new", n[tid]))
    save_json(p.state / "preflight.json", {"at": f"{datetime.now():%Y-%m-%d %H:%M}", "questions": out})
    return out


def pending(p):
    return load_json(p.state / "preflight.json", {}).get("questions", [])


def lookup(p, key):
    """The saved preflight question `key` names (`T-003.1`, or `T-003` when the ticket has just one), or None."""
    qs = pending(p)
    hit = [x for x in qs if x["id"].lower() == key.lower()]
    if not hit:
        tid = p.norm(key)
        hit = [x for x in qs if x["ticket"] == tid]
    return hit[0] if len(hit) == 1 else None


# ---------- recording answers

def block(x, letter, text):
    """A question with its answer, as it goes under ## Answered."""
    if x["kind"] == "provisional" and letter and letter == x["built"]:
        ans = f"{letter} (confirmed the provisional answer)"
    else:
        ans = (f"{letter} ({x['options'].get(letter, '')})" if letter else "") + (" " if letter and text else "") \
              + (text or "")
        if x["kind"] == "provisional":
            ans += f". Changes the built {x['built']}: the change still has to be made."
    lines = [f"### {x['ticket']}: {x['question']}", x["context"],
             *[f"- **{k}**: {v}" for k, v in sorted(x["options"].items())],
             f"Answer: {ans} (at the desk, {datetime.now():%Y-%m-%d})"]
    return "\n".join(l for l in lines if l)


def file_answers(qtext, answers):
    """QUESTIONS.md with each (question, letter, text) moved or added under ## Answered."""
    text = qtext or "# Questions for the user\n\n## Open\n\n## Provisional\n\n## Answered\n"
    for x, _, _ in answers:
        if x["kind"] == "provisional":
            prov = part(text, "Provisional")
            m = re.search(rf"^### {re.escape(x['ticket'])}:?[ \t]+{re.escape(x['question'])}[ \t]*$.*?(?=^### |\Z)",
                          prov, re.M | re.S)
            if m:
                text = text.replace(prov, prov.replace(m.group(0), "", 1), 1)
    if "\n## Answered" not in "\n" + text:
        text = text.rstrip("\n") + "\n\n## Answered\n"
    head, _, rest = text.partition("## Answered")
    body, sep, tail = rest.partition("\n## ")
    new = "\n\n".join(block(*a) for a in answers)
    return f"{head}## Answered{body.rstrip()}\n\n{new}\n" + (f"\n## {tail}" if sep else "")


def record(p, answers):
    """Commit the answers to main (or hand open phone questions to the driver, which already waits on them).
    Returns a line for the user."""
    phone = [a for a in answers if a[0]["kind"] == "open"]
    for x, letter, text in phone:
        give_answer(p, x["ticket"], letter, text)
    filed = [a for a in answers if a[0]["kind"] != "open"]
    if not filed:
        return f"{len(phone)} answers passed to the waiting tickets" if phone else "no answers"
    cd = p.plan_dir
    with open(p.state / "plan.lock", "w") as lock:  # the planner's tree: nothing else commits there meanwhile
        fcntl.flock(lock, fcntl.LOCK_EX)
        git(p.main_root(), "fetch", "-q", p.remote)
        ensure_worktree(p, cd, setup=False)
        if not clean_detach(p, cd, p.upstream):
            return "couldn't check out main: answers not recorded"
        f = cd / p.questions_rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(file_answers(f.read_text() if f.exists() else "", filed))
        git(cd, "add", p.questions_rel)
        ids = ", ".join(sorted({x["ticket"] for x, _, _ in filed}))
        ok, where = land(p, cd, f"answers-{now():%Y%m%d-%H%M%S}", f"Answers at the desk: {ids}",
                         "\n\n".join(block(*a) for a in filed))
        clean_detach(p, cd, p.upstream)
    done = {x["id"] for x, _, _ in answers}
    save_json(p.state / "preflight.json", {**load_json(p.state / "preflight.json", {}),
                                           "questions": [x for x in pending(p) if x["id"] not in done]})
    if not ok:
        return f"answers for {ids} not merged; they wait on {where}"
    log(p, f"preflight: answers for {ids} on {p.main}")
    return f"{len(answers)} answers recorded on {p.main}"


# ---------- asking at the terminal

def show(x, i, n):
    lines = [f"\n[{i}/{n}] {x['ticket']}: {x['question']}"]
    if x["kind"] == "provisional":
        lines.append(f"  (built provisionally: {x['built']}; same letter keeps it)")
    elif x["kind"] == "open":
        lines.append("  (a run asked this on the phone; it waits for you)")
    lines += [f"  {l}" for l in x["context"].splitlines()]
    for k, v in sorted(x["options"].items()):
        lines.append(f"  {k}: {v}" + ("   <- recommended" + (f": {x['why']}" if x["why"] else "")
                                       if k == x["recommendation"] else ""))
    return "\n".join(lines)


def parse(reply, x):
    """(letter, text) from a terminal reply, or None to skip it."""
    reply = reply.strip()
    if not reply:
        return None
    if re.fullmatch(r"[A-Ea-e]", reply) and reply.upper() in x["options"]:
        return reply.upper(), ""
    return None, reply


def ask(p, qs, inp=input, say=print):
    """Ask each question at the terminal and record the answers. Enter skips one (a worker that still needs it asks
    on the phone); `q` skips the rest."""
    answers = []
    for i, x in enumerate(qs, 1):
        say(show(x, i, len(qs)))
        try:
            reply = inp(f"  answer ({'/'.join(sorted(x['options']))}, your own words, Enter skips, q skips the rest): ")
        except EOFError:
            break
        if reply.strip().lower() == "q":
            break
        got = parse(reply, x)
        if got:
            answers.append((x, *got))
    if answers:
        say(record(p, answers))
    return answers
