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
ticket waits until every one is answered or timed out. No answer in `answer_wait` minutes: the next run builds the
recommendation and records it under `## Provisional`. In a quiet time nothing is asked: runs build the recommendation,
and a question already waiting gets its recommendation built at once. Quiet times are the user's own, for every
project (`~/.config/windvane/quiet.txt`), plus the project's (`quiet.txt` in its state dir).
"""
import re
from datetime import datetime, timedelta

from . import phone
from .config import user_dir
from .util import edit_json, load_json

OPT_RE = re.compile(r"^- \*\*([A-E])\*\*:?\s*(.+)$", re.M)
# An answered provisional block's id, at the end of its Answer line (preflight.block writes it)
FILED_RE = re.compile(r"^Answer:.*\(at the desk, [\d-]+, as \S+?\.p(\d+)\)$", re.M)
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


def provisional(text):
    """[{ticket, question, built, option}] under ## Provisional."""
    out = []
    for tid, q, body in blocks(part(text, "Provisional")):
        opts = dict(OPT_RE.findall(body))
        built = re.search(r"^Built:\s*([A-E])", body, re.M)
        letter = built.group(1) if built else ""
        out.append({"ticket": tid, "question": q, "built": letter, "option": opts.get(letter, "")})
    return out


def prov_numbers(text, known=None):
    """For each block under ## Provisional, in order, its number among its ticket's provisional questions (the 2 of
    `T-003.p2`), so that an id the user was shown names the same question until it's answered: a block keeps the
    number `known` ({qkey: number}, those shown) gives it wherever it now sits, and a new one takes the smallest number
    neither shown nor filed (an answered block's Answer line keeps its id). Answering one, in any order, never
    renumbers the rest, and a block merged later, even above the others, never takes an id shown for another."""
    known = known or {}
    taken = {}
    for tid, _, body in blocks(part(text, "Answered")):
        taken.setdefault(tid, set()).update(int(n) for n in FILED_RE.findall(body))
    for key, n in known.items():
        taken.setdefault(key.split(": ", 1)[0], set()).add(n)
    out = []
    for tid, q, _ in blocks(part(text, "Provisional")):
        n = known.get(f"{tid}: {q}")
        if n is None:
            n = min(set(range(1, len(taken.get(tid, ())) + 2)) - taken.get(tid, set()))
        taken.setdefault(tid, set()).add(n)
        out.append(n)
    return out


def remembered(p, text):
    """`prov_numbers` for `text` from the numbers shown before (shown.json's "ids"), which become these."""
    nums = prov_numbers(text, shown(p)["ids"])
    if text:
        keys = [f"{tid}: {q}" for tid, q, _ in blocks(part(text, "Provisional"))]
        note(p, lambda s: s.update(ids=dict(zip(keys, nums))))
    return nums


def with_ids(text, p=None):
    """`provisional`'s answers, each with the id `windvane answer` and preflight take (`T-003.p2`); given the project,
    the ids it showed before stay theirs."""
    return [{**q, "id": f"{q['ticket']}.p{n}"}
            for q, n in zip(provisional(text), remembered(p, text) if p else prov_numbers(text))]


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


QUIET_LEAD = timedelta(hours=2)  # a quiet time starting this soon is named before it starts


def quiet_next(lines, at):
    """(start, end) of the next quiet time after `at` (a daily one within a day), or None."""
    starts = [s for s, _ in windows(lines, at) if s > at]
    return (min(starts), quiet_until(lines, min(starts))) if starts else None


def quiet_files(p):
    """The user's quiet times for every project, then this project's own: a run keeps both."""
    return [user_dir("XDG_CONFIG_HOME", ".config") / "quiet.txt", p.state / "quiet.txt"]


def quiet_lines(p):
    """The lines of every quiet-times file that reads, and the first error (naming its file) or None: a typo in one
    file mustn't drop the other's quiet times."""
    lines, err = [], None
    for f in quiet_files(p):
        try:
            got = f.read_text().splitlines() if f.exists() else []
            windows(got, datetime.now())
        except OSError as e:
            err = err or f"can't read {f}: {e.strerror or e}"
        except ValueError as e:
            err = err or f"{e} in {f}"
        else:
            lines += got
    return lines, err


def quiet_now(p, at=None):
    """(end of the current quiet time or None, error text or None)."""
    lines, err = quiet_lines(p)
    return quiet_until(lines, at or datetime.now()), err


def mode(p, at=None):
    """What `windvane mode` tells a worker about to ask: right now, so a session that runs into a quiet time learns
    it at the question."""
    at = at or datetime.now()
    lines, err = quiet_lines(p)
    end, soon = quiet_until(lines, at), quiet_next(lines, at)
    if end:
        return f"provisional (the user is in a quiet time until {end:%a %H:%M}): build your recommendation"
    return ("ask" + (f" (a quiet time starts {soon[0]:%a %H:%M}: a question still unanswered then gets its "
                     "recommendation built)" if soon and soon[0] - at <= QUIET_LEAD else "")
            + (f" (a quiet-times file is ignored: {err})" if err else ""))


def quiet_note(p, until=None, at=None):
    """The worker's system prompt's section on quiet times, or "": in one, when it ends; one starting before `until`
    (the session's end, else QUIET_LEAD from now), when it starts. Times are absolute, so the note stays true all
    session; `windvane mode` says what holds at the moment of a question."""
    at = at or datetime.now()
    lines, _ = quiet_lines(p)
    end, soon = quiet_until(lines, at), quiet_next(lines, at)
    if end:
        return (f"\n\n## Quiet hours\nThe user is in quiet hours until {end:%a %H:%M}: asleep or away, they can't "
                "answer anything. Make every product call the provisional way (Decisions above): build your "
                "recommendation, and the phone tells them what you chose so they can change it later. Ask only what "
                f"must wait for the user; it stays unasked until {end:%H:%M}.")
    if soon and soon[0] <= (until or at + QUIET_LEAD):
        return (f"\n\n## Quiet hours\nThe user's quiet hours start at {soon[0]:%a %H:%M} and last until "
                f"{soon[1]:%a %H:%M}: then they can't answer anything. Until {soon[0]:%H:%M}, ask as usual; from then "
                "on `windvane mode` says provisional, and a question still unanswered at the start gets its "
                "recommendation built at once, so give each question a recommendation you'd stand by.")
    return ""


# ---------- answers (from the phone through notify-ask, or `windvane answer` at the desk)

def desk_answers(p):
    return load_json(p.state / "answers.json", {})


def give_answer(p, tid, option=None, text=None):
    answer = {"option": option, "text": text or "", "at": f"{datetime.now():%Y-%m-%d %H:%M}"}
    edit_json(p.state / "answers.json", lambda a: a.update({tid: answer}), {})


def drop_answers(p, settled):
    """Forget the answers to the questions in `settled`, keeping any a `windvane answer` gives meanwhile."""
    def drop(a):
        for k in settled:
            a.pop(k, None)
    edit_json(p.state / "answers.json", drop, {})


def phone_answer(p, tid):
    """"ANSWER <letter>\t<text>", "LATER" or "" from the phone."""
    return phone.answer(p.qid(tid), in_pass=True)


# ---------- what the user was shown (`shown.json` in the state dir): each question reaches them once

def qkey(q):
    return f"{q['ticket']}: {q['question']}"


def shown(p):
    """{"asked": {ticket: [questions sent to the phone]}, "pushed": [keys of provisional answers pushed],
    "preflight": [keys of provisional answers offered at the desk], "built": {`T-003.q2`: its question, built on a
    timeout}, "ids": {key of a provisional answer: the number of the `T-003.p2` id it was shown with}}."""
    return {**blank_shown(), **load_json(p.state / "shown.json", {})}


def blank_shown():
    return {"asked": {}, "pushed": [], "preflight": [], "built": {}, "ids": {}}


def note(p, change):
    """Load the record, let `change` edit it, save it, locked against another writer (the driver, `windvane status`)."""
    def edit(s):
        for k, v in blank_shown().items():
            s.setdefault(k, v)
        change(s)
    edit_json(p.state / "shown.json", edit, {})


def unseen(picks, asked):
    """The provisional answers in `picks` whose question the user never got. A worker may reword a question as it
    moves it to ## Provisional, so each sent question no pick matches word for word stands for one unmatched pick;
    with more unmatched picks than such questions, which is which can't be told, so all of them are spelled out."""
    left = [a for a in asked if a not in {q["question"] for q in picks}]
    out = [q for q in picks if q["question"] not in asked]
    return [] if len(out) <= len(left) else out
