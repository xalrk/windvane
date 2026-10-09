"""Design questions for the user, and quiet times.

A worker that needs a call it may not make writes it into `.windvane/QUESTIONS.md` on its ticket branch:

    ## Open
    ### T-003: <the question, one line>
    <context, a few lines>
    - **A**: <option, complete on its own>
    - **B**: <option>
    Recommendation: A, because ...
    Waits for the user: <why>        (only for money, licences, undoing merged work)

The driver sends it to the phone (phone.py), if there is one, and skips the ticket until it's answered (phone, or
`windvane answer` at the desk). Several blocks for one ticket are several questions (ids `T-003.q1`, `T-003.q2`): the
ticket waits until every one is answered or timed out. No answer in `answer_wait` minutes: the next run builds the recommendation and
records it under `## Provisional`. In a quiet time (`quiet.txt`) nothing is asked: runs build the recommendation.
"""
import re
from datetime import datetime, timedelta

from . import phone
from .util import load_json, save_json

OPT_RE = re.compile(r"^- \*\*([A-E])\*\*:?\s*(.+)$", re.M)
BLOCK_RE = re.compile(r"^### (\S+?):?[ \t]+(.*?)$(.*?)(?=^### |^PREFLIGHT:|^## |\Z)", re.M | re.S)


def part(text, heading):
    text = "\n" + (text or "")
    return text.split(f"\n## {heading}", 1)[1].split("\n## ", 1)[0] if f"\n## {heading}" in text else ""


def sections(text, tid, heading="Open"):
    """Every `### <tid>` block under `heading`, in order: a worker may ask several questions at once."""
    return [m.group(0).strip()
            for m in re.finditer(rf"^### {re.escape(tid)}\b.*?(?=^### |\Z)", part(text, heading), re.M | re.S)]


def section(text, tid, heading="Open"):
    return next(iter(sections(text, tid, heading)), "")


def keys(tid, n):
    """The ids of a ticket's `n` open questions: the ticket's own for one (the common case reads as before), else
    `<tid>.q1`, `<tid>.q2`... (like preflight's `.p1`): each is asked, answered and timed out on its own."""
    return [tid] if n == 1 else [f"{tid}.q{i}" for i in range(1, n + 1)]


def ticket_of(key, q):
    """The ticket a stored question is for (state.json from before several questions a ticket keys it by ticket)."""
    return q.get("ticket") or key


def waiting_on(qs):
    """{key: question} of state.json's questions still waiting for the user (one settled waits for its siblings)."""
    return {k: q for k, q in qs.items() if "result" not in q}


def tickets(qs):
    """The tickets state.json's questions hold back."""
    return {ticket_of(k, q) for k, q in qs.items()}


def parts(sec):
    """({letter: option}, recommended letter or "", must wait for the user, the one-line question)."""
    rec = re.search(r"^Recommendation:\s*\**([A-E])\b", sec, re.M)
    q = re.match(r"### \S+:?\s*(.*)", sec)
    return (dict(OPT_RE.findall(sec)), rec.group(1) if rec else "",
            bool(re.search(r"^Waits for the user:", sec, re.M)), q.group(1).strip() if q else "")


def provisional(text, ids=False):
    """[{ticket, question, built, option}] under ## Provisional; with `ids`, each also has the id `windvane answer`
    takes: its ticket's, or preflight's `T-003.p2` when the ticket has several."""
    out = []
    for m in re.finditer(r"^### (\S+?):?\s+(.*?)$(.*?)(?=^### |\Z)", part(text, "Provisional"), re.M | re.S):
        opts = dict(OPT_RE.findall(m.group(3)))
        built = re.search(r"^Built:\s*([A-E])", m.group(3), re.M)
        letter = built.group(1) if built else ""
        out.append({"ticket": m.group(1), "question": m.group(2).strip(), "built": letter,
                    "option": opts.get(letter, "")})
    if ids:
        for q in out:
            mine = [x for x in out if x["ticket"] == q["ticket"]]
            q["id"] = q["ticket"] if len(mine) == 1 else f"{q['ticket']}.p{mine.index(q) + 1}"
    return out


def blocks(text):
    """[(ticket, question, body)] for every `### <ID>: <question>` block in `text`."""
    return [(m.group(1), m.group(2).strip(), m.group(3).strip()) for m in BLOCK_RE.finditer(text or "")]


def answered(text, tid):
    """The `## Answered` blocks for one ticket, as written in QUESTIONS.md."""
    return [f"### {t}: {q}\n{body}" for t, q, body in blocks(part(text, "Answered")) if t == tid]


# ---------- quiet times

def windows(lines, at):
    """[(start, end)] of the windows `lines` set around `at` (a daily one on the day before, of and after it). Lines:
    `daily HH:MM-HH:MM` or `YYYY-MM-DD HH:MM-HH:MM` (a window may cross midnight; # starts a comment)."""
    out = []
    for raw in lines:
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        m = re.fullmatch(r"(daily|\d{4}-\d{2}-\d{2})\s+(\d{1,2}:\d{2})\s*-\s*(\d{1,2}:\d{2})", line)
        if not m:
            raise ValueError(f"quiet time not understood: {raw.strip()!r}")
        days = ([at.date() + timedelta(days=d) for d in (-1, 0, 1)] if m.group(1) == "daily"
                else [datetime.strptime(m.group(1), "%Y-%m-%d").date()])
        for day in days:
            s = datetime.combine(day, datetime.strptime(m.group(2), "%H:%M").time())
            e = datetime.combine(day, datetime.strptime(m.group(3), "%H:%M").time())
            out.append((s, e if e > s else e + timedelta(days=1)))
    return out


def quiet_until(lines, at):
    """End of the quiet window `at` falls in, or None (`windows` reads the lines). Touching windows chain."""
    ws = windows(lines, at)
    end = None
    while True:
        t = end or at
        ext = [e for s, e in ws if s <= t < e and (end is None or e > end)]
        if not ext:
            return end
        end = max(ext)


def quiet_now(p, at=None):
    """(end of the current quiet time or None, error text or None)."""
    f = p.state / "quiet.txt"
    try:
        return quiet_until(f.read_text().splitlines() if f.exists() else [], at or datetime.now()), None
    except ValueError as e:
        return None, str(e)
    except OSError as e:
        return None, f"can't read {f}: {e.strerror or e}"


def mode(p):
    end, err = quiet_now(p)
    if end:
        return f"provisional (the user is in a quiet time until {end:%a %H:%M}): build your recommendation"
    return "ask" + (f" (quiet times unreadable: {err})" if err else "")


# ---------- answers (from the phone through notify-ask, or `windvane answer` at the desk)

def desk_answers(p):
    return load_json(p.state / "answers.json", {})


def give_answer(p, tid, option=None, text=None):
    a = desk_answers(p)
    a[tid] = {"option": option, "text": text or "", "at": f"{datetime.now():%Y-%m-%d %H:%M}"}
    save_json(p.state / "answers.json", a)


def phone_answer(p, tid):
    """"ANSWER <letter>\t<text>", "LATER" or "" from the phone."""
    return phone.answer(p.qid(tid))


# ---------- what the user was shown (`shown.json` in the state dir): each question reaches them once

def qkey(q):
    return f"{q['ticket']}: {q['question']}"


def shown(p):
    """{"asked": {ticket: [questions sent to the phone]}, "pushed": [keys of provisional answers pushed],
    "preflight": [keys of provisional answers offered at the desk]}."""
    return {"asked": {}, "pushed": [], "preflight": [], **load_json(p.state / "shown.json", {})}


def note(p, change):
    """Load the record, let `change` edit it, save it."""
    s = shown(p)
    change(s)
    save_json(p.state / "shown.json", s)


def unseen(picks, asked):
    """The provisional answers in `picks` whose question the user never got. A worker may reword a question as it
    moves it to ## Provisional, so each sent question no pick matches word for word stands for one unmatched pick;
    with more unmatched picks than such questions, which is which can't be told, so all of them are spelled out."""
    left = [a for a in asked if a not in {q["question"] for q in picks}]
    out = [q for q in picks if q["question"] not in asked]
    return [] if len(out) <= len(left) else out
