"""Design questions for the user, and quiet times.

A worker that needs a call it may not make writes it into `.windvane/QUESTIONS.md` on its ticket branch:

    ## Open
    ### T-003: <the question, one line>
    <context, a few lines>
    - **A**: <option, complete on its own>
    - **B**: <option>
    Recommendation: A, because ...
    Waits for the user: <why>        (only for money, licences, undoing merged work)

The driver sends it to the phone with `notify-ask` and skips the ticket until it's answered (phone, or
`windvane answer` at the desk). No answer in `answer_wait` minutes: the next run builds the recommendation and
records it under `## Provisional`. In a quiet time (`quiet.txt`) nothing is asked: runs build the recommendation.
"""
import re
import shutil
from datetime import datetime, timedelta

from .util import load_json, save_json, sh

OPT_RE = re.compile(r"^- \*\*([A-E])\*\*:?\s*(.+)$", re.M)


def part(text, heading):
    text = "\n" + (text or "")
    return text.split(f"\n## {heading}", 1)[1].split("\n## ", 1)[0] if f"\n## {heading}" in text else ""


def section(text, tid, heading="Open"):
    m = re.search(rf"^### {re.escape(tid)}\b.*?(?=^### |\Z)", part(text, heading), re.M | re.S)
    return m.group(0).strip() if m else ""


def parts(sec):
    """({letter: option}, recommended letter or "", must wait for the user, the one-line question)."""
    rec = re.search(r"^Recommendation:\s*\**([A-E])\b", sec, re.M)
    q = re.match(r"### \S+:?\s*(.*)", sec)
    return (dict(OPT_RE.findall(sec)), rec.group(1) if rec else "",
            bool(re.search(r"^Waits for the user:", sec, re.M)), q.group(1).strip() if q else "")


def provisional(text):
    """[{ticket, question, built, option}] under ## Provisional."""
    out = []
    for m in re.finditer(r"^### (\S+?):?\s+(.*?)$(.*?)(?=^### |\Z)", part(text, "Provisional"), re.M | re.S):
        opts = dict(OPT_RE.findall(m.group(3)))
        built = re.search(r"^Built:\s*([A-E])", m.group(3), re.M)
        letter = built.group(1) if built else ""
        out.append({"ticket": m.group(1), "question": m.group(2).strip(), "built": letter,
                    "option": opts.get(letter, "")})
    return out


# ---------- quiet times

def quiet_until(lines, at):
    """End of the quiet window `at` falls in, or None. Lines: `daily HH:MM-HH:MM` or `YYYY-MM-DD HH:MM-HH:MM`
    (a window may cross midnight; # starts a comment). Touching windows chain."""
    windows = []
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
            windows.append((s, e if e > s else e + timedelta(days=1)))
    end = None
    while True:
        t = end or at
        ext = [e for s, e in windows if s <= t < e and (end is None or e > end)]
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
    """"ANSWER <letter>\t<text>", "LATER" or "" from notify-ask."""
    if not shutil.which("notify-ask"):
        return ""
    out = sh(p.main_root(), "notify-ask", "--get", p.qid(tid))
    return out if out.startswith(("ANSWER", "LATER")) else ""
