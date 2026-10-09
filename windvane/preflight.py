"""Preflight: the product calls the next tickets will need, asked while the user is still at the desk.

One cheap read-only session (`prompts/PREFLIGHT.md`, role `preflight` in the ledger) reads the tickets likely to run
before the deadline and lists the questions their text makes predictable. Questions still waiting for the user join
them: a ticket's open phone question, and provisional answers built without them. Answers land under `## Answered` in
`.windvane/QUESTIONS.md` on main, where the ticket's worker finds them in its prompt and builds them without asking.
"""
import fcntl
import math
import re
import sys
from datetime import datetime, timedelta

from .config import slots
from .driver import REVIEW_DENY
from .held import land
from .planner import pending_ids
from .questions import BLOCK_RE, OPT_RE, blocks, give_answer, note, part, qkey, shown, ticket_of, waiting_on
from .queue import Queue
from .runner import rules, run_claude
from .util import git, gout, load_json, log, now, read_jsonl, save_json
from .worktree import clean_detach, ensure_worktree

PREFLIGHT_RE = re.compile(r"^PREFLIGHT: (\d+|NONE)", re.M)
# Read, Grep and Glob only: it runs in the user's checkout, and its prompt holds every text it needs.
DENY = [*REVIEW_DENY, "Bash", "WebFetch", "WebSearch"]


def minutes_per_ticket(p, default=60.0):
    """Session minutes per ticket over the last two weeks of the ledger (worker, critic, reviewer runs together)."""
    since = f"{now() - timedelta(days=14):%Y-%m-%d %H:%M}"
    per = {}
    for r in read_jsonl(p.state / "ledger.jsonl"):
        if r.get("ticket") and not r.get("event") and r.get("at", "") >= since:  # a session, not an outcome
            per[r["ticket"]] = per.get(r["ticket"], 0) + (r.get("minutes") or 0)
    return max(10.0, sum(per.values()) / len(per)) if per else default


def likely(p, q, hours, parallel=None):
    """The tickets the run will likely reach in `hours`, in queue order (at most eight: the prompt stays small)."""
    n = max(1, min(8, math.ceil(hours * 60 * slots(p, parallel) / minutes_per_ticket(p))))
    return [t for t in q.order if q.status(t) not in ("done", "parked")][:n]


def question(tid, q, body, kind, n):
    opts = dict(OPT_RE.findall(body))
    rec = re.search(r"^Recommendation:\s*\**([A-E])\b\W*(?:because\b\s*)?(.*)$", body, re.M)
    built = re.search(r"^Built:\s*([A-E])\W*(?:to change it:\s*)?(.*)$", body, re.M)
    context = "\n".join(l for l in body.splitlines()
                        if l.strip() and not re.match(r"- \*\*[A-E]\*\*|Recommendation:|Built:|Waits for", l))
    return {"id": f"{tid}.{n}", "ticket": tid, "kind": kind, "question": q, "context": context.strip(),
            "options": opts, "recommendation": rec.group(1) if rec else "", "why": rec.group(2).strip() if rec else "",
            "built": built.group(1) if built else "", "change_at": built.group(2).strip() if built else ""}


def provisionals(qtext, heading="Provisional", kind="provisional"):
    """Provisional answers built without the user, as questions to ask again (ids `T-003.p1`); or, for ## Open,
    questions left there that no run waits on (kind "stuck", ids `T-003.o1`): they block their ticket for good."""
    out, seen = [], {}
    for tid, q, body in blocks(part(qtext, heading)):
        seen[tid] = seen.get(tid, 0) + 1
        out.append(question(tid, q, body, kind, f"{kind[0]}{seen[tid]}"))
    return out


def waiting(p, qtext):
    """Questions already asked that still wait for the user: open phone questions, and provisional answers not yet
    offered at the desk (one skipped stays in `windvane status` and the run's last push, and `windvane answer` still
    takes it)."""
    out = []
    for key, q in waiting_on(load_json(p.state / "state.json", {}).get("questions", {})).items():
        out.append({"id": key, "ticket": ticket_of(key, q), "kind": "open", "question": q["q"], "context": "",
                    "options": q["opts"], "recommendation": q["rec"], "why": "", "built": "", "change_at": ""})
    prov = provisionals(qtext)
    keys = {qkey(x) for x in prov}
    if qtext:  # the record keeps only those still provisional
        note(p, lambda s: s.update(preflight=[k for k in s["preflight"] if k in keys]))
    offered = set(shown(p)["preflight"])
    return out + [x for x in prov if qkey(x) not in offered]


def offered(p, qs):
    """Provisional answers shown at the desk: preflight doesn't offer them again."""
    keys = [qkey(x) for x in qs if x["kind"] == "provisional"]
    if keys:
        note(p, lambda s: s["preflight"].extend(k for k in keys if k not in s["preflight"]))


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
        log(p, f"preflight run ({len(tids)} tickets: {', '.join(tids)})", echo=False)
        text, killed, error = run_claude(p, "preflight", "preflight", prompt, rules(p, "PREFLIGHT.md"), p.main_root(),
                                         deny=DENY, usage=usage)
        if killed or error or not PREFLIGHT_RE.search(text):
            why = f"preflight: no list ({killed or error or 'no PREFLIGHT line'})"
            log(p, why, echo=False)
            print(why, file=sys.stderr)  # stdout may be --json
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
    """The question `key` names (`T-003.1`, or `T-003` when the ticket has just one): saved by preflight, a
    provisional answer on main (so `windvane answer` changes one after its ticket merged), or a question under ## Open
    on main. None if not exactly one."""
    def pick(qs):
        hit = [x for x in qs if x["id"].lower() == key.lower()]
        if not hit and "." not in key:  # a mistyped T-001.2 must not answer T-001.1
            hit = [x for x in qs if x["ticket"] == p.norm(key)]
        return hit[0] if len(hit) == 1 else None
    x = pick(pending(p))
    if x:
        return x
    git(p.main_root(), "fetch", "-q", p.remote)
    qtext = gout(p.main_root(), "show", f"{p.upstream}:{p.questions_rel}")
    return pick(provisionals(qtext)) or pick(provisionals(qtext, "Open", "stuck")) or built_answer(p, key, qtext)


def built_answer(p, key, qtext):
    """The provisional answer on main of `T-003.q2`, one of a ticket's several questions its timeout built (its No
    answer push names that id), found by its question; None before it merges, or if the worker reworded it."""
    m = re.fullmatch(r"(?i)(.+)\.q(\d+)", key.strip())
    q = shown(p)["built"].get(f"{p.norm(m.group(1))}.q{m.group(2)}") if m and p.norm(m.group(1)) else None
    hit = [x for x in provisionals(qtext) if q and x["ticket"] == p.norm(m.group(1)) and x["question"] == q]
    return hit[0] if len(hit) == 1 else None


# ---------- recording answers

def block(x, letter, text, followup=""):
    """A question with its answer, as it goes under ## Answered."""
    if x["kind"] == "provisional" and letter and letter == x["built"]:
        ans = f"{letter} (confirmed the provisional answer)"
    else:
        ans = (f"{letter} ({x['options'].get(letter, '')})" if letter else "") + (" " if letter and text else "") \
              + (text or "")
        if x["kind"] == "provisional":
            ans += f". Changes the built {x['built']}" + (f": {followup} makes the change" if followup else "")
    lines = [f"### {x['ticket']}: {x['question']}", x["context"],
             *[f"- **{k}**: {v}" for k, v in sorted(x["options"].items())],
             f"Answer: {ans} (at the desk, {datetime.now():%Y-%m-%d})"]
    return "\n".join(l for l in lines if l)


def answered_block(prov, x):
    """The ## Provisional block the answer `x` is for: the one with its exact question, else the one its id names
    (`T-003.p2`: the ticket's second) with the same options, else the ticket's only one. A worker may reword the
    question on its branch, and a block left behind would be asked again next run. The options guard the id: blocks
    added or filed since preflight saved it shift the numbers, and a wrong block removed is never asked at all."""
    mine = [m for m in BLOCK_RE.finditer(prov) if m.group(1) == x["ticket"]]
    exact = [m for m in mine if m.group(2).strip() == x["question"]]
    n = re.search(r"\.p(\d+)$", x["id"])
    if exact:
        return exact[0]
    if n and 0 < int(n.group(1)) <= len(mine) and dict(OPT_RE.findall(mine[int(n.group(1)) - 1].group(3))) \
            == x["options"]:
        return mine[int(n.group(1)) - 1]
    return mine[0] if len(mine) == 1 else None


def file_answers(qtext, answers):
    """QUESTIONS.md with each (question, letter, text) moved or added under ## Answered."""
    text = qtext or "# Questions for the user\n\n## Open\n\n## Provisional\n\n## Answered\n"
    for heading, kind in (("Provisional", "provisional"), ("Open", "stuck")):
        sec = part(text, heading)
        hits = {m.span(): m for x, *_ in answers if x["kind"] == kind and (m := answered_block(sec, x))}
        keep = sec
        for (a, b) in sorted(hits, reverse=True):
            keep = keep[:a] + keep[b:]
        if hits:
            text = text.replace(sec, keep, 1)
    if "\n## Answered" not in "\n" + text:
        text = text.rstrip("\n") + "\n\n## Answered\n"
    head, _, rest = text.partition("## Answered")
    body, sep, tail = rest.partition("\n## ")
    new = "\n\n".join(block(*a) for a in answers)
    return f"{head}## Answered{body.rstrip()}\n\n{new}\n" + (f"\n## {tail}" if sep else "")


def changed(x, letter, text):
    return x["kind"] == "provisional" and not (letter and letter == x["built"])


def followup(p, cd, tid, x, letter, text):
    """A ticket that changes a merged ticket's provisional answer to the user's: the answer alone builds nothing."""
    want = (f"option {letter} ({x['options'].get(letter, '')})" if letter else "") + (": " if letter and text else "") \
        + (text or "")
    (cd / p.tickets_rel / f"{tid}.md").write_text(
        f"# {tid}: Change {x['ticket']}'s provisional answer: {x['question']}\nDepends on: nothing.\n\n"
        f"## Goal\n{x['ticket']} built option {x['built']} ({x['options'].get(x['built'], '')}) without the user. "
        f"At the desk they chose {want}.\n\n"
        f"## Scope\nChange what {x['ticket']} built to the user's answer"
        + (f" (where it lives: {x['change_at']})" if x["change_at"] else "") + ". Nothing else.\n\n"
        f"## Done when\n- The project does {want}, with a test that fails on the old behaviour.\n")
    q = cd / p.queue_rel
    q.write_text((q.read_text() if q.exists() else "# Queue\n").rstrip("\n") + f"\n- {tid} change {x['ticket']}'s "
                 "answer\n")


def forget(p, ids):
    """Drop answered questions from the saved preflight list, so they aren't offered again."""
    save_json(p.state / "preflight.json", {**load_json(p.state / "preflight.json", {}),
                                           "questions": [x for x in pending(p) if x["id"] not in ids]})


def record(p, answers):
    """Commit the answers to main (or hand open phone questions to the driver, which already waits on them).
    Returns a line for the user."""
    phone = [a for a in answers if a[0]["kind"] == "open"]
    for x, letter, text in phone:
        give_answer(p, x["id"], letter, text)
    forget(p, {x["id"] for x, _, _ in answers})
    filed = [a for a in answers if a[0]["kind"] != "open"]
    if not filed:
        return f"{len(phone)} answer{'s' * (len(phone) > 1)} passed to the waiting run" if phone else "no answers"
    cd = p.plan_dir
    with open(p.state / "plan.lock", "w") as lock:  # the planner's tree: nothing else commits there meanwhile
        fcntl.flock(lock, fcntl.LOCK_EX)
        git(p.main_root(), "fetch", "-q", p.remote)
        ensure_worktree(p, cd, setup=False)
        if not clean_detach(p, cd, p.upstream):
            return "couldn't check out main: answers not recorded"
        q, taken = Queue(p), set(pending_ids(p))
        for i, a in enumerate(filed):
            if changed(*a):
                tid = q.next_id(taken)
                taken.add(tid)
                followup(p, cd, tid, *a)
                filed[i] = (*a, tid)
        f = cd / p.questions_rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(file_answers(f.read_text() if f.exists() else "", filed))
        git(cd, "add", "-A", ".windvane")
        ids = ", ".join(sorted({a[0]["ticket"] for a in filed}))
        ok, where = land(p, cd, f"answers-{now():%Y%m%d-%H%M%S}", f"Answers at the desk: {ids}",
                         "\n\n".join(block(*a) for a in filed))
        clean_detach(p, cd, p.upstream)
    if not ok:
        return f"answers for {ids} not merged; they wait on {where}"
    log(p, f"preflight: answers for {ids} on {p.main}", echo=False)
    made = [a[3] for a in filed if len(a) > 3]
    return (f"{len(answers)} answer{'s' * (len(answers) > 1)} recorded on {p.main}"
            + (f"; {', '.join(made)} will change what was built" if made else ""))


# ---------- asking at the terminal

def show(x, i, n):
    lines = [f"\n[{i}/{n}] {x['ticket']}: {x['question']}"]
    if x["kind"] == "provisional":
        lines.append(f"  (built provisionally: {x['built']}; same letter keeps it)")
    elif x["kind"] == "open":
        lines.append("  (a run asked this on the phone; it waits for you)")
    lines += [f"  {l}" for l in x["context"].splitlines()]
    for k, v in sorted(x["options"].items()):
        lines.append(f"  {k}: {v}" + ("   <- recommended" + (f", because {x['why']}" if x["why"] else "")
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
    on the phone); `q` skips the rest; Ctrl-C keeps the answers so far and stops."""
    answers, stop = [], False
    for i, x in enumerate(qs, 1):
        say(show(x, i, len(qs)))
        offered(p, [x])
        try:
            reply = inp(f"  answer ({'/'.join(sorted(x['options']))}, your own words, Enter skips, q skips the rest): ")
        except EOFError:
            break
        except KeyboardInterrupt:  # keep what was typed, and don't start the run
            stop = True
            break
        if reply.strip().lower() == "q":
            break
        got = parse(reply, x)
        if got:
            answers.append((x, *got))
    if answers:
        say(record(p, answers))
    if stop:
        raise SystemExit("\npreflight: stopped" + ("; the answers above are kept" if answers else ""))
    return answers
