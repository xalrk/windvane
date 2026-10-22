"""The phone: pushes, questions and the phone's commands through ntfy (ntfy.sh, or a server of your own).

Off by default: without it, questions wait for `windvane answer` at the desk. `windvane phone setup` turns it on with
two fully random topics (pushes go to one; the phone's buttons and typed commands come back on the other), kept in
~/.config/windvane/phone.toml (mode 600, outside every repo). On ntfy.sh a topic name is the only thing keeping
strangers out, so topics are never written to logs, pushes, PRs or commits, and every push is censored first. A
server of your own can also demand a token (`setup --token`): every request and every button then carries it, and it
is kept and censored as the topics are.

Whoever learns the reply topic can send to it, so nothing from the phone is ever instructions: only one of a few
fixed commands, or a letter choosing among the options windvane itself wrote for a question.

The user's own `notify` / `notify-ask` scripts, when on PATH, are used instead (detection first, built-in second).
"""
import fcntl
import functools
import getpass
import json
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import time
import threading
import tomllib
import urllib.error
import urllib.request
from email.utils import parsedate_to_datetime
from pathlib import Path

from . import util
from .config import exe, user_dir
from .util import log

SERVER = "https://ntfy.sh"
PUMP_EVERY = 15  # seconds between a run's polls of the reply topic when it has no stream (ntfy refills one per 5 s)
RETRY = 10  # seconds before the listener reconnects after a dropped stream, doubling while it keeps dropping
RETRY_CAP = 300  # the longest wait between reconnects: a server that is down hears from each install every 5 min
STEADY = 60  # seconds a stream must stay up (or deliver a message) for the next drop to wait RETRY again
STREAM_WAIT = 90  # seconds a stream may go silent (ntfy sends a keepalive every 45 s) before it counts as dropped
RESTREAM = 1800  # seconds a listener that fell back to polling waits before it tries a stream again
HOLD_CAP = 3600  # the longest Retry-After taken as given: a bad header must not silence the phone for days
QID_RE = r"[a-z][a-z0-9-]{1,39}"
HELP = ("status: the autopilots that are on\noverview: where each project stands\nusage: Claude usage\n"
        "stop: stop the runs after their current ticket\nanswer <id> B: pick an option of a question\n"
        "B: answer the newest open question with B\nlater <id>: answer at the computer instead")
SERVICE = "windvane-phone.service"


NTFY = 4096  # bytes ntfy takes for a whole message; past it the push arrives as an attachment
MOST = 1500  # bytes of free text one push carries (an error, a reason, an option): two fit in a push with room left
REASON = 300  # bytes of a reason or a log or session line: a set-aside or stop reason, a critic's gist, git's refusal
REST = " (the rest is in the log)"
ASKED = 200  # bytes a question push adds around its question and options: the letters, the line on typed answers
SEND_WAIT = 30  # seconds the user's `notify` / `notify-ask` get: a stalled network must not hold up the run's loop
LATE_FOR = 86400  # seconds a timed-out question's late phone answer still gets told what is being built
LATE_KEEP = 7 * 86400  # seconds a late `windvane answer` whose push keeps failing is kept: older, it's news to no one


def cut(text, most=REASON, *, log=None):
    """`text` in at most `most` UTF-8 bytes, ended on a character, never inside one. A cut falls at the last sentence
    or line end, a word end without one, or mid-word in one long token, and says so: with a `log`, the log gets the
    whole text and the cut ends "(the rest is in the log)"; without one, a plain "…". One rule for every bounded
    text, so a phone push or a session reads the same cut wherever it came from."""
    text = str(text).strip()
    if len(text.encode()) <= most:
        return text
    mark = REST if log else "…"
    keep = text.encode()[:most - len(mark.encode())].decode(errors="ignore")
    end, where = max(keep.rfind(". ") + 1, keep.rfind("\n")), "at a sentence end"
    if end <= len(keep) // 2:  # no sentence end late enough: a word end, or a bare cut in one long token
        end, where = (keep.rfind(" "), "at a word end") if " " in keep[1:] else (len(keep), "mid-word")
    if log:
        log(f"cut {where}; the whole text: {text}")
    return keep[:end].rstrip(" ,;:.") + mark


def ago(seconds):
    """How long ago something came, as a push says it: "" under an hour (it reads as new), else "5 hours ago" or
    "3 days ago"."""
    hours = int(seconds // 3600)
    if hours < 1:
        return ""
    n, unit = (hours, "hour") if hours < 24 else (hours // 24, "day")
    return f"{n} {unit}{'s' if n > 1 else ''} ago"


def whole(head, free, tail="", *, log, most=MOST):
    """`head`, the free text `free` (an error, a reason, an option) whole, then `tail`, the next step: one sentence in
    reading order, with one full stop. ntfy wraps a long line, so nothing is cut to fit a lock screen: a cut word
    reads as a typo and a "…" hides the point. Only free text past `most` bytes (a 5,000-character error) is cut, by
    `cut`'s rule, and `log`, the project's log, gets it whole."""
    free = " ".join(cut(free, most, log=log).split())
    free = free.rstrip(".") if tail.startswith((".", ";", ":", ",", ")")) else free
    return head + free + tail


def conf_path():
    return user_dir("XDG_CONFIG_HOME", ".config") / "phone.toml"


def state_dir():
    return user_dir("XDG_STATE_HOME", ".local/state") / "phone"


def conf():
    try:
        return tomllib.loads(conf_path().read_text())
    except (OSError, ValueError):
        return {}


def on(c):
    return bool(c.get("enabled") and c.get("topic") and c.get("reply_topic"))


def save_conf(c):
    """Written mode 600 from the start: the topics are the only lock on them."""
    f = conf_path()
    f.parent.mkdir(parents=True, exist_ok=True)
    lines = ["# windvane phone (ntfy). Keep private: anyone with a topic can read your pushes or send commands.",
             f'enabled = {"true" if c.get("enabled") else "false"}', f'server = {json.dumps(c["server"])}',
             f'topic = {json.dumps(c["topic"])}', f'reply_topic = {json.dumps(c["reply_topic"])}',
             *([f'token = {json.dumps(c["token"])}'] if c.get("token") else []),
             f'redact = {json.dumps(c.get("redact", []))}  # extra words to censor: names, hosts, projects']
    tmp = f.with_suffix(".tmp")
    tmp.unlink(missing_ok=True)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as out:
        out.write("\n".join(lines) + "\n")
    tmp.replace(f)


def auth(c):
    """The headers that open a server set to `deny-all`: none without a token (ntfy.sh, an open server)."""
    return {"Authorization": f"Bearer {c['token']}"} if c.get("token") else {}


def where(c):
    """Which server the phone uses, as doctor says it: never the address or the token, only that there is one."""
    own = c.get("server", SERVER).rstrip("/") != SERVER
    return ("own server" if own else "ntfy.sh") + (", token set" if c.get("token") else "")


def new_topic():
    """Random only: no prefix, name or word that would make it guessable."""
    return secrets.token_urlsafe(24)


# ---------- the censor (the same rules as the user's ntfy-redact)

def mask(s):
    return s[:2] + "***"


RULES = [
    (r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(-----END [A-Z ]*PRIVATE KEY-----|$)", lambda m: "[private key]"),
    (r"(?<=://)[^/\s:@]+:[^/\s@]+@", lambda m: "***@"),
    (r"\b(gh[pousr]_|github_pat_|sk-|xox[abprs]-|AKIA|AIza|glpat-|hf_)[A-Za-z0-9_\-]{10,}", lambda m: mask(m.group(0))),
    (r"\beyJ[\w-]{8,}\.[\w-]{8,}\.[\w-]{8,}", lambda m: mask(m.group(0))),
    (r"(?i)\b((?:sudo[ _-]?)?pass(?:word|wd|phrase)?|pwd|secret|token|api[_-]?key|auth|credentials?|pin)"
     r"(\s*(?:is|=|:)\s*)(\S+)", lambda m: m.group(1) + m.group(2) + "***"),
    (r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+", lambda m: mask(m.group(0)) + "@***"),
    (r"\b(?=[A-Za-z0-9+/_-]*\d)(?=[A-Za-z0-9+/_-]*[A-Za-z])[A-Za-z0-9+/_-]{32,}={0,2}", lambda m: mask(m.group(0))),
    (r"\b(\d{1,3})(?:\.\d{1,3}){3}\b", lambda m: m.group(1) + ".***"),
    (r"\b\d(?:[ -]?\d){12,18}\b", lambda m: mask(m.group(0))),
]


@functools.cache
def git_names():
    """Git's user name and the local part of its email: read once per process, not twice on every push."""
    out = []
    for key in ("user.name", "user.email"):
        r = subprocess.run(["git", "config", "--global", key], capture_output=True, text=True)
        out.append(r.stdout.strip().split("@")[0])
    return tuple(out)


def words(c):
    """Names that identify the user or open the phone: $USER, git's name and email, the topics, the token, `redact`."""
    found = {os.environ.get("USER", ""), c.get("topic", ""), c.get("reply_topic", ""), c.get("token", ""),
             *c.get("redact", []), *git_names()}
    return sorted((w for w in found if len(w) >= 3), key=len, reverse=True)


def censor(text, c):
    for pattern, repl in RULES:
        text = re.sub(pattern, repl, text, flags=re.S)
    for w in words(c):
        text = re.sub(re.escape(w), lambda m: mask(m.group(0)), text, flags=re.I)
    return text


# ---------- sending

_hold_until = 0.0  # no push before this: the server answered 429 (see send)
_held = False  # that 429 is in the log already: one line per spell of them, not one per push
_refused = 0  # the 401 or 403 the last push got (told once until a push goes through again), else 0


def rest(seconds, until=lambda: False):
    """Sleep `seconds`, a second at a time so `until()` cuts it short (a run's stream ends with its run)."""
    end = time.time() + seconds
    while not until() and time.time() < end:
        time.sleep(min(1.0, end - time.time()))


def retry_after(e, default):
    """Seconds an HTTP error's Retry-After asks for (seconds or a date), else `default`; at most HOLD_CAP."""
    v = (e.headers.get("Retry-After") or "").strip() if getattr(e, "headers", None) else ""
    try:
        s = float(v) if v.replace(".", "", 1).isdigit() else parsedate_to_datetime(v).timestamp() - time.time()
    except (TypeError, ValueError, OverflowError):
        return default
    return min(max(s, 0), HOLD_CAP)


def span(seconds):
    """A wait as a log line says it: "10 s", "2 min", "1 h"."""
    s = round(seconds)
    return f"{s} s" if s < 90 else f"{round(s / 60)} min" if s < 5400 else f"{round(s / 3600)} h"


def held_for(until):
    """What a 429's hold does to the pushes after it: a short one delays the next, a long one skips them."""
    left = until - time.time()
    if left <= SEND_WAIT:
        return f"the next push waits {span(left)}"
    return f"pushes until {time.strftime('%H:%M', time.localtime(until))} are skipped, and questions asked again after"


def limit_name(e):
    """What ntfy says a 429 is about ("limit reached: daily message quota reached"), from its JSON body."""
    try:
        said = json.loads(e.read() or b"{}").get("error", "")
    except (OSError, ValueError, AttributeError):
        said = ""
    return cut(str(said)) or "too many requests"


BAD_TOKEN = "the token in phone.toml isn't plain text: windvane phone setup --token -"


def refused(code, c):
    """What a 401 or 403 means for the user: a token to set or change, or, with a token ntfy knows (it answers 401
    to one it doesn't), topics its user hasn't been granted. Never the token itself."""
    if not c.get("token"):
        return f"the server asks for a token ({code}): windvane phone setup --token -"
    if code == 403:
        return "the server hasn't let the token's user use windvane's topics (403): run the two `ntfy access` lines " \
               "`windvane phone setup` prints"
    return f"the server refused the token ({code}): windvane phone setup --token -"


def tell(line):
    """A line about the phone's connection: the project's log in a run, stderr (the service's journal) outside."""
    if _p:
        log(_p, line, echo=False)
    else:
        print(line, file=sys.stderr, flush=True)


def send(c, title, msg, prio=3, tags="", buttons=(), wait=10):
    """One push to the push topic; each button ("Label=command") posts its command to the reply topic. Censored
    first: if the censor fails, nothing is sent. Never raises: a push is never worth stopping a run for. After a
    429 the pushes wait out its Retry-After, within `wait` (past it, this one fails as a stalled network does), so
    an install over the server's budget stops adding to it instead of being refused once a push."""
    global _hold_until, _held, _refused
    hold = _hold_until - time.time()
    if hold > wait:
        return False
    if hold > 0:
        rest(hold)
    try:
        msg, title = censor(msg, c), censor(title, c)
    except Exception:
        return False
    server = c.get("server", SERVER).rstrip("/")
    body = {"topic": c["topic"], "message": msg, "priority": int(prio)}
    if title:
        body["title"] = title
    if tags:
        body["tags"] = tags.split(",")
    acts = [{"action": "http", "label": b.split("=", 1)[0], "url": f"{server}/{c['reply_topic']}", "method": "POST",
             "body": b.split("=", 1)[1], "clear": False, **({"headers": auth(c)} if auth(c) else {})}
            for b in buttons[:3] if "=" in b]  # the phone posts a tap itself: a deny-all server wants the token there too
    if acts:
        body["actions"] = acts
    req = urllib.request.Request(server, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", **auth(c)})
    try:
        with urllib.request.urlopen(req, timeout=max(min(wait, 10), 0.01)):
            _held, _refused = False, 0
            return True
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            if not _refused:
                tell(f"windvane phone: {refused(e.code, c)}")
            _refused = e.code
        elif e.code == 429:
            _hold_until = time.time() + retry_after(e, RETRY)
            if not _held:
                tell(f"windvane phone: the ntfy server refused a push (429, {limit_name(e)}); "
                     f"{held_for(_hold_until)}. A server of your own lifts its limits: "
                     "windvane phone setup --server URL")
            _held = True
        e.close()
        return False
    except UnicodeError:  # a token edited into phone.toml by hand that can't go in a header
        if not _refused:
            tell(f"windvane phone: {BAD_TOKEN}")
        _refused = -1
        return False
    except (OSError, ValueError):
        return False


_project = ""  # the display name of the project this process works for (see use)
_p = None  # that project, whose log gets a push too long for ntfy


def use(p):
    """Name project `p` in every push and question this process sends: with two projects running, the phone must
    tell them apart at a glance. Set once where a process takes up its project (the CLI, a run)."""
    global _project, _p
    _project, _p = (p.display if p else ""), p


def titled(text):
    """The project in front, once: `Acme: Done: T-007 ...`; a run's own `windvane started` reads `Acme windvane
    started` (just `windvane started` for windvane itself)."""
    if not _project:
        return text
    if text.startswith("windvane "):
        return text if _project == "windvane" else f"{_project} {text}"
    return f"{_project}: {text}"


def push(title, msg, prio=3, tags="", buttons=(), wait=SEND_WAIT):
    """A push through the user's `notify` when it's on PATH, else the built-in one when the phone is on. True only
    when it went out (`notify` exited 0, or ntfy took it) within `wait` seconds: an answer counts as shown on the
    phone only then."""
    title, msg = titled(title), fit(msg)
    if shutil.which("notify"):
        args = ["notify", "-t", title, "-p", str(prio), "-g", tags]
        for b in buttons[:3]:
            args += ["-a", b]
        return (tool(args + [msg], wait) or (1,))[0] == 0
    c = conf()
    return on(c) and send(c, title, msg, prio, tags, buttons, wait)


def whole_to_log(line):
    """A cut push's whole text, for the project's log (stderr outside a project)."""
    if _p:
        log(_p, line, echo=False)
    else:
        print(line, file=sys.stderr, flush=True)


def fit(msg, most=NTFY):
    """The last guard on ntfy's 4,096 bytes, past which a push turns into an attachment: a message over `most` (most
    text is already bounded by `cut`) keeps its lines that fit, or a line's start when even the first doesn't, and
    the log gets it whole, once."""
    if len(msg.encode()) <= most:
        return msg
    keep = msg.encode()[:most - len(REST.encode())].decode(errors="ignore")
    keep = keep[:keep.rfind("\n")] if "\n" in keep else keep
    where = "at a line end" if msg[len(keep):].startswith("\n") else "mid-line"  # decided on what the cut left
    keep = keep.rstrip()
    whole_to_log(f"push cut {where} to fit ntfy; the whole message: {msg}")
    return keep + (REST if keep else REST.lstrip())


def tool(args, wait):
    """(exit code, stdout) of the user's `notify` / `notify-ask`, or None when it failed to start or ran past `wait`
    seconds. It runs in a session of its own, so a timeout kills its whole group: a `curl` it started must not post a
    question late, after the next pass has sent it again."""
    try:
        proc = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                text=True, start_new_session=True)
    except OSError:
        return None
    try:
        out, _ = proc.communicate(timeout=max(wait, 0.01))
        return proc.returncode, out
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:  # the group is gone already, or out of reach: at least the script itself
            proc.kill()
        proc.stdout.close()  # a child that left the group may hold it open: wait for the script alone
        proc.wait()
        return None


# ---------- questions

def ask(qid, question, options):
    """Send a question with buttons for A and B and "Answer on computer" (C to E are typed). True when sent, False
    when the push failed (worth another try), None when there's no phone: it then waits for `windvane answer`."""
    options = [cut(o, min(MOST, NTFY // 2 // max(len(options), 1)), log=whole_to_log) for o in options]
    room = NTFY - ASKED - sum(len(f"\nX: {o}".encode()) for o in options)  # the options keep at least half the push
    question = fit(titled(question), room)
    if shutil.which("notify-ask"):  # a timeout is a failed send too: the next pass sends it again
        return (tool(["notify-ask", qid, question, *options], SEND_WAIT) or (1,))[0] == 0
    c = conf()
    if not on(c):
        return None
    opts = dict(zip("ABCDE", options))
    d = state_dir()
    for sub in ("questions", "answers"):
        (d / sub).mkdir(parents=True, exist_ok=True)
    (d / "answers" / f"{qid}.json").unlink(missing_ok=True)
    (d / "questions" / f"{qid}.json").write_text(json.dumps({"options": opts, "asked": time.time()}))
    lines = "\n".join(f"{k}: {v}" for k, v in opts.items())
    more = [k for k in opts if k > "B"]
    typed = f"\nFor {'/'.join(more)}, send the letter (or: answer {qid} {more[0]})" if more else ""
    buttons = [f"{k}=answer {qid} {k}" for k in list(opts)[:2]] + [f"Answer on computer=later {qid}"]
    return send(c, f"Question ({qid})", fit(f"{question}\n{lines}{typed}"), 4, "question", buttons)


_pass_end = 0.0  # when the current pass's looks for answers must be done (see pump)


def pass_left():
    """Seconds the current pass's budget has left (0 or less: spent)."""
    return _pass_end - time.time()


def answer(qid, in_pass=False):
    """"ANSWER <letter>\t<text>", "LATER" or "" (not answered yet). `in_pass`: one of a pass's looks, which share
    the pass's SEND_WAIT, so a stalled network holds a pass over five questions for 30 s, not 2.5 min."""
    if shutil.which("notify-ask"):
        wait = pass_left() if in_pass else SEND_WAIT
        got = tool(["notify-ask", "--get", qid], wait) if wait > 0 else None
        out = got[1].strip() if got else ""  # none: not answered yet, as far as this pass can tell
        return out if out.startswith(("ANSWER", "LATER")) else ""
    try:
        a = json.loads((state_dir() / "answers" / f"{qid}.json").read_text())
    except (OSError, ValueError):
        return ""
    return "LATER" if a.get("later") else f"ANSWER {a['option']}\t{a['text']}"


def building(qid, key, rec, text=""):
    """The reply to an answer that came after its question timed out: the option being built by its letter and its
    text, so the user needn't scroll back to the question to see what A was."""
    what = whole(f"{rec} (", text, ")", log=whole_to_log) if text else rec
    return (f"{qid}: no answer came in time, so it's already building {what}; change it with "
            f"`windvane answer {key} <letter>`.")


def timed_out(qid, key, rec, text=""):
    """Question `qid` (`windvane answer`'s `key`) waited out its time and `rec` is being built: a phone answer that
    comes after it gets told so, not "recorded" and then never read. The built-in phone marks its question file;
    with the user's own `notify-ask`, `late.json` lists it for pump to look at. `text`: option `rec`'s text, which
    that reply names."""
    if shutil.which("notify-ask"):
        try:
            with late_lock(block=True) as late:
                late[qid] = {"key": key, "rec": rec, "text": text, "when": time.time()}
        except (OSError, TypeError):
            pass
        return
    q = state_dir() / "questions" / f"{qid}.json"
    try:
        q.write_text(json.dumps({**json.loads(q.read_text()), "built": rec, "key": key}))
    except (OSError, ValueError):
        pass


class late_lock:
    """`late.json` ({qid: {key, rec, text, when timed out, `told`: the reply it owes}}) under a lock, written back
    on leaving. Without `block`, None when another process holds it: one run on this machine looks at the late
    answers, so none is told twice."""
    def __init__(self, block=False):
        self.f, self.block, self.late = state_dir() / "late.json", block, None

    def __enter__(self):
        self.f.parent.mkdir(parents=True, exist_ok=True)
        self.lock = open(self.f.with_suffix(".lock"), "w")
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | (0 if self.block else fcntl.LOCK_NB))
        except OSError:
            self.lock.close()
            return None
        try:
            self.late = json.loads(self.f.read_text())
        except (OSError, ValueError):
            self.late = {}
        return self.late

    def __exit__(self, *exc):
        if self.late is not None:
            try:
                self.f.write_text(json.dumps(self.late))
            finally:
                self.lock.close()


def late_answers(desk=()):
    """Each timed-out question's phone answer, looked at once a pass for a day on what the pass's budget has left
    after the open questions: one that isn't what is being built gets told so, sent on that budget too, or on a later
    pass once it's spent (the answer is kept, not looked at again). After it, or the day, the question is dropped.
    `desk`: the pass's `windvane answer`s that came too late, as (line, push alone): all of them go in one push.
    True when that push went out."""
    told = {}
    with late_lock() as late:
        for qid, e in list((late or {}).items()):
            if isinstance(e, list):  # written by an older version: [key, rec, when]
                e = late[qid] = dict(zip(("key", "rec", "when"), e))
            if time.time() - e["when"] >= LATE_FOR:
                late.pop(qid)
                continue
            if pass_left() <= 0:
                break
            if not e.get("told"):
                got = answer(qid, in_pass=True)
                if not got or got.split()[1:2] == [e["rec"]]:
                    if got:
                        late.pop(qid)
                    continue
                e["told"] = building(qid, e["key"], e["rec"], e.get("text", ""))
            told[qid] = (late_line(qid, e), e["told"])
        if told and pass_left() <= 0:  # left for a later pass's budget
            told = {}
        sent = not_taken([*desk, *told.values()], wait=pass_left() if told else SEND_WAIT)
        if sent:
            for qid in told:
                late.pop(qid)
    return sent


def late_line(qid, e):
    """A timed-out question's line in a push of several late answers: its `windvane answer` id when it's this
    project's, and what is being built."""
    what = whole(f"{e['rec']} (", e.get("text", ""), ")", log=whole_to_log) if e.get("text") else e["rec"]
    return f"{e['key'] if _p and _p.qid(e['key']) == qid else qid}: building {what}"


def not_taken(answers, wait=SEND_WAIT):
    """One "Answer not taken" push for a pass's late answers, each (its line, its push alone): one keeps its own
    words, several share a line on how to change one, so answering three settled questions after a night is one push,
    not three saying the same thing. True when it went out."""
    if not answers:
        return False
    if len(answers) == 1:
        return push("Answer not taken", answers[0][1], wait=wait)
    head = (f"{len(answers)} answers came after their questions were settled, so they weren't taken; once a ticket "
            "merges, change one with `windvane answer <id> <letter>`:")
    return push("Answers not taken", "\n".join([head, *(line for line, _ in answers)]), wait=wait)


# ---------- projects the phone's commands reach

def register(p):
    """Remember a project's checkout, so the phone's status/overview/stop reach it."""
    f = state_dir() / "projects.json"
    try:
        known = json.loads(f.read_text())
    except (OSError, ValueError):
        known = {}
    if known.get(p.name) != str(p.main_root()):
        known[p.name] = str(p.main_root())
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps(known, indent=1))


def projects():
    try:
        known = json.loads((state_dir() / "projects.json").read_text())
    except (OSError, ValueError):
        return {}
    return {n: r for n, r in sorted(known.items()) if Path(r).is_dir()}


def display(name, root):
    """A known project's display name, for a reply that names it; its state name when its config won't load."""
    from .config import Project
    try:
        return Project(root).display
    except (SystemExit, OSError, ValueError):
        return name


def each(*args, timeout=90):
    """One line (or a few) per project from `windvane -C <root> <args>`."""
    out = []
    for name, root in projects().items():
        try:
            r = subprocess.run([*exe(), "-C", root, *args], capture_output=True, text=True, timeout=timeout)
            out.append(r.stdout.strip() or f"{display(name, root)}: no answer")
        except (OSError, subprocess.TimeoutExpired):
            out.append(f"{display(name, root)}: no answer")
    return out or ["No windvane project has run here yet."]


def running_lines():
    """`status`'s answer: only the projects whose autopilot is on (`overview` covers them all), the same lines as the
    `status.d` hook gives ntfy-listen."""
    try:
        r = subprocess.run([*exe(), "status", "--line", "--running"], capture_output=True, text=True, timeout=90)
        return r.stdout.strip() or "windvane: no answer"
    except (OSError, subprocess.TimeoutExpired):
        return "windvane: no answer"


def usage_text():
    from .config import Project
    from .runner import Usage
    roots = list(projects().values())
    if not roots:
        return "No windvane project has run here yet."
    p = Project(roots[0])
    u = Usage(p)
    u.probe(p)
    return u.text()


# ---------- commands from the phone

def reply(c, title, msg, *buttons):
    send(c, title, msg, 3, "speech_balloon", buttons)


def answer_question(c, qid, letter, note=""):
    d = state_dir()
    q = d / "questions" / f"{qid}.json"
    try:
        asked = json.loads(q.read_text())
        opts = asked["options"]
    except (OSError, ValueError, KeyError):
        return reply(c, "Answer not taken", f"No open question {qid}.")
    if asked.get("built"):
        return reply(c, "Answer not taken", building(qid, asked["key"], asked["built"], opts.get(asked["built"], "")))
    if letter not in opts:
        return reply(c, "Answer not taken", f"{qid} has options {', '.join(sorted(opts))}.")
    (d / "answers").mkdir(parents=True, exist_ok=True)
    (d / "answers" / f"{qid}.json").write_text(json.dumps({"option": letter, "text": opts[letter]}))
    q.unlink()
    reply(c, "Answer recorded", f"{qid}: {letter}, {cut(opts[letter])}{note}")


def later(c, qid):
    d = state_dir()
    if not (d / "questions" / f"{qid}.json").exists():
        return reply(c, "Nothing to postpone", f"No open question {qid}.")
    (d / "answers").mkdir(parents=True, exist_ok=True)
    (d / "answers" / f"{qid}.json").write_text(json.dumps({"later": True}))
    reply(c, f"{qid} waits for you", "At the computer: windvane answer <ticket> <letter>.")


def built(f):
    """Whether question file `f` timed out to its recommendation: a bare letter answers only a question still open."""
    try:
        return bool(json.loads(f.read_text()).get("built"))
    except (OSError, ValueError):
        return False


def answer_newest(c, letter):
    d = state_dir() / "questions"
    every = sorted(d.glob("*.json"), key=lambda f: f.stat().st_mtime) if d.exists() else []
    qs = [f for f in every if not built(f)]
    recent = [f for f in every if time.time() - f.stat().st_mtime < LATE_FOR]
    if not qs:  # only timed-out ones: the newest of the last day says what it's building instead
        return answer_question(c, recent[-1].stem, letter) if recent else reply(
            c, "Answer not taken", f"No open question to answer with {letter}.")
    older = f"\n({len(qs) - 1} older open: answer <id> {letter} for those.)" if len(qs) > 1 else ""
    answer_question(c, qs[-1].stem, letter, older)


def is_command(text):
    """Exactly one command and nothing more (for the push topic, which also carries windvane's own pushes)."""
    return bool(re.fullmatch(rf"(?i)\s*([a-e]\.?|status|overview|usage|stop|help|answer {QID_RE} [a-e]|later {QID_RE})"
                             r"\s*", text))


def handle(c, text, stale=False):
    """Act on one message from the phone. Unknown text only gets the list of commands back. A `stale` message (sent
    before this run or listener started) may still answer a question, but commands from then are dropped: a Stop
    tapped after the last run ended must not end the next one."""
    w = text.strip().lower().split()
    if stale and not (w[:1] in (["answer"], ["later"]) or len(w) == 1 and re.fullmatch(r"[a-e]\.?", w[0])):
        return
    if len(w) == 1 and re.fullmatch(r"[a-e]\.?", w[0]):
        return answer_newest(c, w[0][0].upper())
    if w == ["status"]:
        reply(c, "Status", running_lines(), "Overview=overview", "Usage=usage")
    elif w == ["overview"]:
        reply(c, "Overview", "\n\n".join(each("overview")), "Status=status")
    elif w == ["usage"]:
        reply(c, "Claude usage", usage_text(), "Status=status")
    elif w == ["stop"]:
        reply(c, "Stop", "\n".join(each("stop", "--if-running")), "Status=status")
    elif len(w) == 3 and w[0] == "answer" and re.fullmatch(QID_RE, w[1]) and re.fullmatch("[a-e]", w[2]):
        answer_question(c, w[1], w[2].upper())
    elif len(w) == 2 and w[0] == "later" and re.fullmatch(QID_RE, w[1]):
        later(c, w[1])
    elif w:
        reply(c, "Commands", HELP)


# ---------- the listener

def listen(once=False, until=lambda: False, since=None, heard=None):
    """Read the reply topic (and exact commands typed on the push topic) and act on each message. `once`: what's
    there now, then return; otherwise hold a stream open until `until()`. `heard`: a run's own stream (see
    keep_stream), called for each message acted on; it gives up quietly when another reader or the phone being off
    stops it. Commands sent before `since` (default: now) are stale. One reader at a time on this machine, so no
    command is done twice: the service's listener waits for the lock while a run's stream holds it, and takes over
    within a second of its release, instead of exiting for systemd to start it again every 30 s.

    Every install leans on one ntfy server's per-visitor budget, so a dropped stream is opened again after RETRY
    seconds, doubling to RETRY_CAP while it keeps dropping (a 429 waits its Retry-After), with one line per spell
    of drops; a stream that delivers or stays up STEADY seconds starts over at RETRY. Where a stream opens but
    nothing ever comes through (a proxy holding it back), it looks every PUMP_EVERY seconds instead, trying a
    stream again every RESTREAM seconds and going back to it once one brings anything."""
    since = time.time() if since is None else since
    d = state_dir()
    d.mkdir(parents=True, exist_ok=True)
    lock = open(d / "listen.lock", "a+")  # not "w": that would wipe the holder's name a waiting listener reads
    if not take(lock, "run" if heard else "once" if once else "listen", until):
        lock.close()
        return False
    recent, handled = [], 0
    wait, spell, polling, streamed, restream_at = RETRY, False, False, False, 0.0
    try:
        while not until():
            c = conf()
            if not on(c):
                if once or heard:
                    return False
                raise SystemExit("windvane phone: the phone is off (windvane phone setup)")
            last = d / "last-id"
            after = last.read_text().strip() if last.exists() else mark_now()
            server = c.get("server", SERVER).rstrip("/")
            retrying = polling and time.time() >= restream_at
            poll = once or (polling and not retrying)
            url = f"{server}/{c['reply_topic']},{c['topic']}/json?since={after}" + ("&poll=1" if poll else "")
            began, lines, delivered, opened, err = time.time(), 0, False, False, None
            try:
                req = urllib.request.Request(url, headers=auth(c))
                with urllib.request.urlopen(req, timeout=15 if poll else STREAM_WAIT) as stream:
                    opened = True
                    for raw in stream:
                        lines += 1
                        if polling and not poll:
                            polling = False
                            tell("windvane phone: the server's stream gets through again; polling stopped")
                        ev = json.loads(raw)
                        if ev.get("event") != "message":
                            continue
                        delivered = True
                        last.write_text(ev["id"])
                        text = ev.get("message", "")
                        if ev.get("topic") == c["topic"] and (ev.get("title") or not is_command(text)):
                            continue  # windvane's own pushes, or chat typed on the push topic
                        if until():
                            break
                        now = time.time()
                        recent = [t for t in recent if now - t < 60] + [now]
                        if len(recent) <= 10:  # a flood isn't the user
                            handle(c, text[:200], ev.get("time", 0) < int(since))
                            handled += 1
                            if heard:
                                heard()
            except (OSError, ValueError) as e:  # a dropped stream, a keepalive timeout, a server restart, a 429
                if once:
                    if isinstance(e, urllib.error.HTTPError):
                        e.close()
                    return False
                err = e
            if once:
                return handled
            streamed = streamed or (lines > 0 and not poll)
            if retrying and polling:
                restream_at = time.time() + RESTREAM
                if opened and isinstance(err, TimeoutError):
                    continue  # still held back: back to polling, quietly
            good = delivered or time.time() - began >= STEADY
            if good:
                wait, spell = RETRY, False
            if until() or (err is None and good and not polling):
                continue  # a stream that served its time is simply opened again
            if err is None and polling:
                rest(PUMP_EVERY, until)
                continue
            if opened and not lines and not streamed and isinstance(err, TimeoutError):
                polling, restream_at = True, time.time() + RESTREAM
                tell(f"windvane phone: the server's stream opened but brought nothing in {span(STREAM_WAIT)}: check "
                     f"for a proxy or VPN that buffers long requests (`env | grep -i proxy`, a company gateway). "
                     f"Asking every {span(PUMP_EVERY)} instead, and trying a stream again every {span(RESTREAM)}")
                continue
            is_429 = isinstance(err, urllib.error.HTTPError) and err.code == 429
            pause = max(retry_after(err, wait) if is_429 else wait, PUMP_EVERY if polling else 0)
            if not spell:
                tell(f"windvane phone: {dropped(err, c)}; reconnecting, next try in {span(pause)}")
                spell = True
            if isinstance(err, urllib.error.HTTPError):
                err.close()
            rest(pause, until)
            wait = min(wait * 2, RETRY_CAP)
    finally:
        lock.close()


def take(lock, kind, until):
    """Lock the one reader's place and write who holds it ("<pid> <kind>"). The `listen` kind (the service, or one
    run by hand) waits until the holder lets go or `until()`, a second at a time so a stop ends it at once, with one
    line naming the holder; a run's stream and `once` give up at once."""
    told = False
    while True:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            lock.seek(0)
            lock.truncate()
            lock.write(f"{os.getpid()} {kind}\n")
            lock.flush()
            if told:
                tell("windvane phone: the reply topic is free; listening")
            return True
        except OSError:
            if kind != "listen" or until():
                return False
        if not told:
            tell(f"windvane phone: {holder(lock)}")
            told = True
        time.sleep(1)


def holder(lock):
    """Who holds the reply topic, and what ends the wait, for a waiting listener's one line."""
    lock.seek(0)
    pid, _, kind = lock.read().strip().partition(" ")
    if kind == "run":
        return "a run reads the phone's replies on its own stream; listening once that run ends"
    if kind == "listen":
        leave = " (Ctrl-C leaves)" if sys.stderr.isatty() else ""
        return f"another `windvane phone listen` (pid {pid}) already reads the phone's replies; waiting until it " \
            f"stops{leave}"
    return "another reader holds the phone's replies; listening once it lets go"


def dropped(err, c):
    """Why a stream ended, for its spell's one log line."""
    if err is None:
        return "the stream ended at once"
    if isinstance(err, urllib.error.HTTPError) and err.code in (401, 403):
        return refused(err.code, c)
    if isinstance(err, UnicodeError):
        return BAD_TOKEN
    if isinstance(err, urllib.error.HTTPError):
        return f"the server refused the stream (429, {limit_name(err)})" if err.code == 429 else \
            f"the server answered {err.code}"
    return f"the stream dropped ({type(getattr(err, 'reason', err)).__name__})"


_last_pump = 0.0
_late_due = False  # a pass pump started whose late answers haven't been looked at yet
_run_start = None
_run_over = None  # a run's: set when it ends, which ends its stream (see keep_stream)
_stream = None  # that stream's thread
_stream_broke = False  # that stream raised, and its line is in the log
_heard, _heard_lock = 0, threading.Lock()  # messages the stream acted on that pump hasn't reported yet


def start_run(p):
    """A run starts: its project becomes reachable from the phone, and only commands from now on reach it."""
    global _run_start, _run_over
    register(p)
    _run_start = time.time()
    _run_over = threading.Event()
    keep_stream()


def end_run():
    """The run ends, and its stream with it (at its next message or keepalive at the latest), freeing the reply
    topic for another run's."""
    global _run_over
    over, _run_over = _run_over, None
    if over:
        over.set()
    if _stream:
        _stream.join(2)


def keep_stream():
    """A run with no listener (no service, or none on macOS) reads the reply topic itself, on one stream in a thread
    of its own: one open request instead of a poll every PUMP_EVERY seconds. pump starts it again when it ended:
    another run's stream or a listener held the lock, or the phone was off."""
    global _stream
    if _run_over is None or (_stream and _stream.is_alive()) or shutil.which("notify-ask") or not on(conf()):
        return
    _stream = threading.Thread(target=read_stream, args=(_run_over, _run_start), name="phone-stream", daemon=True)
    _stream.start()


def read_stream(over, since):
    """The run's stream thread. A stream that raises is started again by the next pump, told once until one ends
    without raising: a fault that keeps coming back must not fill the log a line every PUMP_EVERY."""
    global _stream_broke
    try:
        listen(until=over.is_set, since=since, heard=hear)
        _stream_broke = False
    except Exception as e:  # the phone must never stop a run: the next pump starts it again
        if not _stream_broke:
            tell(f"windvane phone: the run's stream stopped ({type(e).__name__}); it starts again within "
                 f"{span(PUMP_EVERY)}")
        _stream_broke = True


def hear():
    global _heard
    with _heard_lock:
        _heard += 1


def heard_since():
    """How many messages the run's stream acted on since the last look."""
    global _heard
    with _heard_lock:
        n, _heard = _heard, 0
    return n


def pump(late=True):
    """A run's own look at the reply topic: what its stream (see keep_stream) acted on since the last look, or,
    outside a run, a poll at most every PUMP_EVERY seconds. Nothing when the listener (or the user's ntfy-listen)
    already reads it. How many messages it acted on, so a waiting run wakes up for an answer. It starts a pass:
    the pass's looks for answers and its replies to late ones share one SEND_WAIT. Open questions come first, so a
    stalled network spends it on them: the driver, which looks at those, passes `late=False` and calls late_pass
    after its looks."""
    global _last_pump, _pass_end, _late_due
    _pass_end = time.time() + SEND_WAIT
    heard = heard_since()
    if time.time() - _last_pump < PUMP_EVERY:
        return heard
    _last_pump = time.time()
    if shutil.which("notify-ask"):
        _late_due = True
        if late:
            late_pass()
        return heard
    if not on(conf()):
        return heard
    if _run_over is not None:
        keep_stream()
        return heard
    try:
        return listen(once=True, since=_run_start) or 0
    except Exception:  # the phone must never stop a run
        return 0


def late_pass(desk=()):
    """The late answers of the pass pump started (once a pass), on what its budget has left, with `desk`, the pass's
    own late `windvane answer`s (see late_answers), which go out even when no pass is due. True when `desk` is done
    with: told, or there's no phone to tell; False keeps them for the next pass, as an unsent question is kept."""
    global _late_due
    due, _late_due = _late_due, False
    try:
        sent = late_answers(desk) if due else not_taken(desk)
        return sent or not desk or not (shutil.which("notify") or on(conf()))
    except Exception:  # the phone must never stop a run
        return not desk


# ---------- `windvane phone ...` at the desk

def access_help(c):
    """The grants a deny-all server needs for windvane's two topics, and no others: the token rides in every push's
    buttons, so whoever sees one can do what its user may. Printed at the terminal only, never pushed or logged."""
    if not c.get("token"):
        return
    print("On the server, let the token's user (<user> below) read and post on windvane's two topics, and only them:")
    for topic in (c["topic"], c["reply_topic"]):
        print(f"  ntfy access <user> {topic} rw")


def subscribe_help(c):
    url = f"{c['server'].rstrip('/')}/{c['topic']}"
    access_help(c)
    if c.get("token"):
        print("Your server lets in only its users: in the ntfy app's settings, add yours first (its name and password).")
        print("On an iPhone, pushes come only if the server's server.yml sets upstream-base-url: \"https://ntfy.sh\".")
    print("On the phone: install the ntfy app, tap +, and subscribe to this topic"
          + ("" if c["server"].rstrip("/") == SERVER else f" on server {c['server']} (\"Use another server\")") + ":")
    print(f"  {c['topic']}")
    if shutil.which("qrencode"):
        print("or scan this (it opens the topic):")
        subprocess.run(["qrencode", "-t", "ANSIUTF8", url], check=False)
    else:
        print(f"or open {url} on the phone.")
    print("Keep the topic private: anyone who has it can read your pushes. It's in", conf_path(), "(mode 600).")


def unit_path():
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "systemd/user" / SERVICE


def systemctl(*args):
    return subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True)


# Where systemd says it is the running init (sd_booted): WSL ships `systemctl` but boots without systemd by default.
SYSTEMD = Path("/run/systemd/system")


def has_systemd():
    return bool(shutil.which("systemctl")) and SYSTEMD.is_dir()


def no_systemd_fix():
    """How to get the phone's commands between runs without systemd; WSL has it one setting away."""
    if util.wsl():
        return ("put `[boot]` and `systemd=true` in /etc/wsl.conf, run `wsl --shutdown` in Windows, reopen WSL and "
                "run `windvane phone setup` again; until then the phone's commands work only during runs")
    return "keep `windvane phone listen` running (e.g. in tmux) for the phone's commands between runs"


def install_service():
    """A systemd user service running `windvane phone listen`, so the phone's commands work with no run on."""
    if not has_systemd():
        print(f"No systemd here: {no_systemd_fix()}. During a run, answers and stop reach it anyway.")
        return False
    f = unit_path()
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("[Unit]\nDescription=windvane phone: answers and commands from ntfy\nAfter=network-online.target\n\n"
                 f"[Service]\nExecStart={' '.join(exe())} phone listen\nRestart=always\nRestartSec=30\n\n"
                 "[Install]\nWantedBy=default.target\n")
    systemctl("daemon-reload")
    ok = systemctl("enable", "--now", SERVICE).returncode == 0
    print(f"listener: {'running as' if ok else 'could not start'} the user service {SERVICE}")
    return ok


def remove_service():
    if unit_path().exists():
        systemctl("disable", "--now", SERVICE)
        unit_path().unlink()
        systemctl("daemon-reload")
        print(f"listener: removed the user service {SERVICE}")


def restart_service():
    if unit_path().exists():
        systemctl("restart", SERVICE)


def mark_now():
    """Only messages from now on count: an old `stop` must not stop a new run."""
    d = state_dir()
    d.mkdir(parents=True, exist_ok=True)
    since = str(int(time.time()))
    (d / "last-id").write_text(since)
    return since


def read_token(token):
    """`--token`'s value: "-" reads it from stdin, unseen at a terminal (out of the shell's history), "" removes it."""
    if token == "-":
        token = getpass.getpass("token: ") if sys.stdin.isatty() else sys.stdin.readline()
    token = token.strip()
    if not token.isascii() or any(ch.isspace() for ch in token):  # it can't go in a header: every push would fail
        raise SystemExit("windvane phone: a token is one word of plain letters, digits and signs; paste it again")
    return token


def setup(server=None, service=True, project=None, token=None):
    """Turn the phone on, keeping the topics (and the token) it had. `token`: set ("" removes) the one a server of
    your own demands; a setup with only it changes nothing else."""
    c = conf()
    c["server"] = (server or c.get("server") or SERVER).rstrip("/")
    if token is not None:
        c["token"] = read_token(token)
    c.setdefault("topic", new_topic())
    c.setdefault("reply_topic", new_topic())
    c["enabled"] = True
    save_conf(c)
    mark_now()
    if token is not None:
        restart_service()  # a listener's open stream still sends the old token
    if project:
        register(project)
    if shutil.which("notify"):
        print("Your own `notify` / `notify-ask` are on PATH: windvane keeps using them; this setup is the fallback.")
    subscribe_help(c)  # first: a deny-all server takes the test push only once the topics are granted
    ok = send(c, "windvane: phone works", "Pushes and questions from windvane come here. Tap Status to try a "
              "command.", 3, "white_check_mark", ("Status=status",))
    print(f"test push: {'sent' if ok else f'FAILED ({why_failed(c)})'}")
    grant_then_retry(c, ok)
    if service:
        install_service()
    return 0 if ok else 1


def why_failed(c):
    """Why setup's test push failed, as far as windvane can tell."""
    return BAD_TOKEN if _refused == -1 else refused(_refused, c) if _refused else \
        "check the server address and the network"


def grant_then_retry(c, ok):
    """After a test push the server refused for want of the grants access_help printed: the next step."""
    if not ok and c.get("token") and _refused == 403:
        print("Run the two `ntfy access` lines above on the server, then `windvane phone setup` again for the test "
              "push.")


def off():
    c = conf()
    if not c:
        return print("the phone is off (never set up)")
    c["enabled"] = False
    save_conf(c)
    remove_service()
    print("the phone is off: questions wait for `windvane answer`. `windvane phone setup` turns it on again "
          "(same topics).")


def rotate():
    c = conf()
    if not c.get("topic"):
        raise SystemExit("windvane phone: not set up yet (windvane phone setup)")
    c["topic"], c["reply_topic"] = new_topic(), new_topic()
    save_conf(c)
    mark_now()
    restart_service()
    print("New topics made: the old ones get nothing more. In the ntfy app, unsubscribe the old topic, then:")
    subscribe_help(c)
    if on(c):
        ok = send(c, "windvane: new topic works", "Pushes come here from now on.", 3, "white_check_mark")
        print(f"test push: {'sent' if ok else f'FAILED ({why_failed(c)})'}")
        grant_then_retry(c, ok)


def main(a, project=None):
    if a.action == "setup":
        return setup(a.server, not a.no_service, project, a.token)
    if a.action == "off":
        return off()
    if a.action == "rotate":
        return rotate()
    if a.action == "listen":
        return listen()
