"""The experiment budget: how much of the account's week the bench may spend. Every experiment costs bench runs, so a
loop with no end could eat the whole week; benches (experiments' and the periodic bench's) start only while the
weekly usage is under `week_below`, and while what they spent this week plus the next run's estimate stays within
`week_share` points of the weekly limit, plus whatever the user allowed for this week (`windvane experiments allow`,
or the one phone question a week at the cap).

The account is the machine's, not a project's, so all of it is machine-wide, under `<state root>/_budget/`:
`spend.jsonl` gets a row at the end of every bench run (what it spent), `budget.json` this week's allowance and the
caps already asked about. A bench's spend is the weekly reading's rise across its span times its share of the
weighted tokens every windvane ledger on the machine logged in that span: work by hand at the same time isn't in any
ledger, so it counts as the bench's, which errs on the safe side. The week is the usage window's own: it ends at the
seven-day reset the usage reply gives (`week_reset` in a project's usage.json).
"""
import json
import time
from datetime import datetime
from pathlib import Path

from .config import user_dir
from .stats import weighted
from .util import append_jsonl, load_json, read_jsonl, save_json

WEEK = 7 * 86400
ESTIMATE_RUNS = 3  # a run's estimate: the mean spend of the last this many runs of its size
STEPS = (10, 20)  # what the phone question offers to allow on top of the share (A, B); C: no
MARK = "the experiment budget"  # how a set-aside reason starts when the budget holds an experiment


def root():
    return user_dir("XDG_STATE_HOME", ".local/state")


def spend_file():
    return root() / "_budget" / "spend.jsonl"


def state_file():
    return root() / "_budget" / "budget.json"


def reading(extra=()):
    """The newest usage reading among every project's usage.json (one account) and `extra`: {"week", "reset", "at"},
    `reset` the seven-day window's reset (epoch seconds, None when no reply gave it yet)."""
    best = {}
    for f in [*root().glob("*/usage.json"), *extra]:
        try:
            u = json.loads(Path(f).read_text())
        except (OSError, ValueError):
            continue
        if isinstance(u, dict) and u.get("week") is not None and u.get("at", 0) >= best.get("at", -1):
            best = {"week": u["week"], "reset": u.get("week_reset"), "at": u.get("at", 0)}
    return best


def current_reset(reset, at):
    """The reset ending the week `at` is in: a reading taken before an earlier reset rolls on by whole weeks."""
    if not reset:
        return None
    while reset <= at:
        reset += WEEK
    return reset


def rise(before, after):
    """How far the weekly reading rose from `before` to `after` (readings): from 0 when a reset came between them."""
    if after.get("week") is None:
        return 0.0
    if before.get("week") is None or (after.get("reset") and before.get("reset")
                                      and after["reset"] - before["reset"] > WEEK / 2):
        return float(after["week"])
    return max(0.0, after["week"] - before["week"])


def stamp(r):
    """A ledger row's time (`at`, minutes), as epoch seconds, or None."""
    try:
        return datetime.strptime(r.get("at") or "", "%Y-%m-%d %H:%M").timestamp()
    except (TypeError, ValueError):
        return None


def tokens(files, start, end):
    """Weighted tokens of the session rows these ledgers logged between `start` and `end` (epoch seconds; rows carry
    minutes, so the span starts at its minute)."""
    start -= start % 60
    total = 0.0
    for f in files:
        for r in read_jsonl(f):
            t = stamp(r)
            if not r.get("event") and t is not None and start <= t <= end:
                total += weighted(r)
    return total


def attribute(up, mine, every):
    """The bench's points of the weekly limit: the reading's rise times its share of every ledger's tokens."""
    return round(up * mine / every, 2) if every > 0 else 0.0


def runs():
    """Every bench run's spend row, the last one of each run: a run's opening row until its end replaces it."""
    return list({x.get("run"): x for x in read_jsonl(spend_file())}.values())


def estimate(which):
    """One run's spend at this size: the mean of the last ESTIMATE_RUNS that ended (any week); 0 with none."""
    same = [x.get("spent") or 0 for x in runs() if x.get("size") == which and not x.get("open")][-ESTIMATE_RUNS:]
    return round(sum(same) / len(same), 2) if same else 0.0


def begin(base, which, before):
    """The bench run in `base` starts: until `record` ends it, it counts as its size's estimate, so a run killed
    before its end (SIGKILL, a reboot) still counts, and so does one still going when another asks for room."""
    append_jsonl(spend_file(), {"run": base.name, "size": which, "started": round(time.time()), "open": True,
                                "week_before": before.get("week"), "spent": estimate(which)})


def record(base, started, before, which):
    """A spend row for the bench run in `base` (its seeds' and judges' ledgers and usage files are under it), from
    `started` until now; `before` is the usage reading at its start."""
    ended = time.time()
    after = reading(base.rglob("usage.json"))
    mine = tokens(base.rglob("ledger.jsonl"), started, ended)
    every = tokens(root().rglob("ledger.jsonl"), started, ended)
    up = rise(before, after)
    row = {"run": base.name, "size": which, "started": round(started), "ended": round(ended),
           "week_before": before.get("week"), "week_after": after.get("week"),
           "reset": current_reset(after.get("reset") or before.get("reset"), ended), "rise": up,
           "bench": round(mine), "all": round(every), "spent": attribute(up, mine, every)}
    append_jsonl(spend_file(), row)
    return row


class Budget:
    """This week's budget as it stands: the reading, what the bench spent, what's allowed, the cap."""

    def __init__(self, cfg, at=None):
        self.below, self.share = float(cfg["week_below"]), float(cfg["week_share"])
        self.at = at or time.time()
        r = reading()
        self.reset = current_reset(r.get("reset"), self.at)
        self.start = self.reset - WEEK if self.reset else self.at - WEEK
        # a reading from before this week began is last week's
        self.week = r.get("week") if r.get("at", 0) >= self.start else None
        self.rows = [x for x in runs() if x.get("ended", x.get("started", 0)) >= self.start]
        self.spent = round(sum(x.get("spent") or 0 for x in self.rows), 2)
        st = load_json(state_file(), {})
        mine = st.get("until") and (self.reset and abs(st["until"] - self.reset) < 3600
                                    or not self.reset and st["until"] > self.at)
        self.allowed = float(st.get("allowed", 0)) if mine else 0.0
        # the caps asked about on the phone this week: {"cap", "qid", "taken"} (its answer applied)
        self.asked = [a for a in st.get("asked", []) if isinstance(a, dict)] if mine else []
        self.cap = self.share + self.allowed

    def question_at(self, cap):
        """This week's phone question at `cap`, or None."""
        return next((a for a in self.asked if a.get("cap") == cap), None)

    def blocks(self, which, runs=1):
        """Why the next `runs` bench runs of this size may not start: ("week", why), ("share", why), or None."""
        if self.week is not None and self.week >= self.below:
            return "week", f"weekly usage at {self.week:g}% (benches start below {self.below:g}%)"
        need = estimate(which) * runs
        if self.spent + need > self.cap:
            return "share", (f"benches used {self.spent:g}% of this week, and the next run takes about {need:g}% "
                             f"(cap {self.cap:g}%)")
        return None

    def ends(self):
        """When this week resets, for people: "Thu 18:00", or "within 7 days" when no usage reply gave it yet."""
        return f"{datetime.fromtimestamp(self.reset):%a %H:%M}" if self.reset else "within 7 days"

    def save(self):
        """This week's allowance and asked caps, kept until the week resets."""
        save_json(state_file(), {"until": self.reset or self.at + WEEK, "allowed": self.allowed, "asked": self.asked})

    def allow(self, more, cap=None):
        """`more` points on top of the share, for this week only; with `cap`, the answer to the question asked at that
        cap, applied once however many times it is read."""
        if cap is not None:
            q = self.question_at(cap)
            if not q or q.get("taken"):
                return
            q["taken"] = True
        self.allowed = round(self.allowed + more, 2)
        self.cap = self.share + self.allowed
        self.save()

    def asking(self, cap, qid):
        """A question at `cap` went out: none is sent at that cap again this week."""
        self.asked.append({"cap": cap, "qid": qid, "taken": False})
        self.save()

    def waiting(self, tids):
        """`windvane status`'s line while experiments wait on the budget."""
        return (f"experiments wait on the budget: {', '.join(tids)} ({self.spent:g}% of this week spent on benches, "
                f"cap {self.cap:g}%, weekly usage {'?' if self.week is None else f'{self.week:g}%'}; the week resets "
                f"{self.ends()}); `windvane experiments` shows it")

    def lines(self):
        """`windvane experiments`: this week's spend, the cap, what's allowed and when the week resets."""
        extra = f" (share {self.share:g}% + {self.allowed:g}% you allowed this week)" if self.allowed else ""
        week = "?" if self.week is None else f"{self.week:g}%"
        out = [f"experiments: {self.spent:g}% of this week spent on benches ({len(self.rows)} "
               f"run{'s' * (len(self.rows) != 1)}), cap {self.cap:g}%{extra}",
               f"weekly usage {week}; benches start below {self.below:g}%; the week resets {self.ends()}"]
        sizes = {x.get("size") for x in runs() if not x.get("open")}
        est = [f"{w} {estimate(w):g}%" if w in sizes else f"{w} ? (none recorded)" for w in ("small", "full")]
        out.append(f"a run takes about: {', '.join(est)}")
        why = self.blocks("full") and self.blocks("small")
        out.append(f"benches wait: {why[1]}" if why else "benches may start")
        return out


def question(b, which, runs=1):
    """The phone question at the cap: its text and options (A, B: more this week; C: wait)."""
    need = estimate(which) * runs
    return (f"windvane experiments used {b.spent:g}% of this week (cap {b.cap:g}%), and the next bench run takes "
            f"about {need:g}%. Allow more this week?",
            [*(f"+{s}% this week" for s in STEPS), f"No, wait for the reset ({b.ends()})"])
