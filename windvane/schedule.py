"""Run times: a detached run that waits for its start (`run --from`), or runs in the same window every day
(`run --daily 12:00-20:00`).

The waiting happens here, in the tmux session `run --detach` starts; each window is one `driver.supervise` call with
that window's deadline, so it is a run of its own (its own start and summary pushes). `windvane stop` marks the
schedule stopped: a waiting one ends at once, a running window after its current tickets, and no later window starts.
`windvane deadline` moves the end of the running window, or of the next one while waiting. Nothing outlives the tmux
session: a reboot ends the schedule.
"""
import contextlib
import fcntl
import re
import time
from datetime import datetime, timedelta

from . import driver, questions, selfupdate
from .util import hhmm, load_json, log, now, save_json

STATE, LOCK, STOPPED, TURN = "schedule.json", "schedule.lock", "schedule-stop", "schedule.turn"  # in the state dir
POLL = 10  # seconds between looks at the clock and for a stop while waiting


# ---------- window maths

def span(for_):
    """`--for`'s 90m or 6h as a timedelta, or None when it isn't one."""
    m = re.fullmatch(r"(\d+(?:\.\d+)?)([hm])", for_ or "")
    return m and timedelta(**{"hours" if m.group(2) == "h" else "minutes": float(m.group(1))})


def clock_at(text, day):
    """('HH:MM' on `day`, or 'YYYY-MM-DD HH:MM'; whether it named its day). ValueError for anything else."""
    try:
        return datetime.strptime(text, "%Y-%m-%d %H:%M"), True
    except ValueError:
        return datetime.combine(day, datetime.strptime(text, "%H:%M").time()), False


def end_after(start, until, for_):
    """The end `--until`/`--for` give a window that starts at `start`: an HH:MM before it is the next day's."""
    if for_:
        return start + (span(for_) or _fail("--for takes e.g. 90m or 6h"))
    try:
        end, dated = clock_at(until, start.date())
    except ValueError:
        _fail("--until takes HH:MM or 'YYYY-MM-DD HH:MM'")
    if end <= start and dated:
        _fail(f"--until {until} is before --from {start:%Y-%m-%d %H:%M}")
    return end if end > start else end + timedelta(days=1)


def single(from_, until, for_, at):
    """(start, end) of `--from` with `--until`/`--for`, typed at `at`. An HH:MM start is today's, or tomorrow's when
    today's window is already over; a window under way starts at `at`, as a `--daily` one does."""
    if not (until or for_):
        _fail("say when to stop: --until HH:MM or --for 6h")
    try:
        start, dated = clock_at(from_, at.date())
    except ValueError:
        _fail("--from takes HH:MM or 'YYYY-MM-DD HH:MM'")
    end = end_after(start, until, for_)
    if end <= at and not dated:
        start += timedelta(days=1)
        end = end_after(start, until, for_)
    if end <= at:
        _fail(f"that window ended at {end:%Y-%m-%d %H:%M}")
    return max(start, at), end


def next_window(daily, at, after=None):
    """(start, end) of the first `daily` window (HH:MM-HH:MM, may cross midnight) still open at `at` and begun after
    `after`; one under way starts at `at`. The windows are `questions.windows`', as quiet times read them."""
    try:
        ws = questions.windows([f"daily {daily}"], at)
    except ValueError:
        _fail("--daily takes HH:MM-HH:MM, e.g. 12:00-20:00")
    start, end = min((s, e) for s, e in ws if e > at and (after is None or s > after))
    return max(start, at), end


def first(from_, until, for_, daily, at=None):
    """The first window of a scheduled run, or None for a plain one. ValueError says what's wrong."""
    at = at or now()
    if daily:
        if from_ or until or for_:
            _fail("--daily sets both ends of each window: leave out --from, --until and --for")
        return next_window(daily, at)
    return single(from_, until, for_, at) if from_ else None


def _fail(why):
    raise ValueError(why)


# ---------- the schedule in its tmux session

def run(p, argv, window, daily=None, start=None, sleep=time.sleep):
    """`run --from/--daily --detach`'s loop: wait for the window's start, have the supervisor run it to its end and,
    for `daily`, go on with the next window, until `windvane stop`. `argv` is `windvane run`'s own; each window's
    driver gets it without --from/--daily and with that window's --until. Returns the last window's exit code."""
    lock = open(p.state / LOCK, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        lock.close()
        return "windvane: a schedule already runs for this project"
    for f in (STOPPED, "DEADLINE"):  # an earlier schedule's or run's
        (p.state / f).unlink(missing_ok=True)
    rc, (s, e) = 0, window
    try:
        while True:
            save_json(p.state / STATE, {"start": iso(s), "end": iso(e), "daily": daily, "waiting": True})
            if s > now():
                log(p, f"schedule: waiting; starts {hhmm(s)} ({what(daily, e)})")
            if not wait(p, s, sleep):
                log(p, f"schedule ended by windvane stop, before {hhmm(s)}")
                break
            e = moved(p, s) or e
            nxt = daily and next_window(daily, e, s)  # for the window's stopped push
            with turn(p):  # a stop lands before this (nothing starts) or sees the window running (it stops)
                if stopped(p):
                    log(p, f"schedule ended by windvane stop, as the {hhmm(s)} window was starting")
                    break
                save_json(p.state / STATE, {"start": iso(s), "end": iso(e), "daily": daily, "waiting": False,
                                            "next": nxt and [iso(nxt[0]), iso(nxt[1])]})
            rc = driver.supervise(p, window_args(argv, e), e, start=start, sleep=sleep)
            (p.state / "DEADLINE").unlink(missing_ok=True)  # moved this window's end only
            if stopped(p):
                log(p, "schedule ended by windvane stop")
                break
            if not daily:
                break
            s, e = next_window(daily, now(), s)
    finally:
        for f in (STATE, STOPPED):
            (p.state / f).unlink(missing_ok=True)
        lock.close()
    return rc


@contextlib.contextmanager
def turn(p):
    """Held while the schedule starts a window and while `stop` reads and marks it, so `stop` says what happens."""
    with open(p.state / TURN, "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        yield


def wait(p, start, sleep):
    """Sleep until `start`; False when the schedule was stopped first."""
    while now() < start and not stopped(p):
        sleep(min(POLL, max((start - now()).total_seconds(), 0.01)))
    return not stopped(p)


def window_args(argv, end):
    out, skip = [], False
    for x in argv:
        if skip:
            skip = False
        elif x in ("--from", "--daily"):
            skip = True
        elif not x.startswith(("--from=", "--daily=")):
            out.append(x)
    return selfupdate.restart_args(out, end)


def moved(p, start):
    """The end `windvane deadline` gave the window starting at `start` while it waited, or None."""
    try:
        t = datetime.fromisoformat((p.state / "DEADLINE").read_text().strip())
    except (OSError, ValueError):
        return None
    return t if t > start else None


def iso(t):
    return t.isoformat(timespec="seconds")


# ---------- what status, stop and deadline say

def state(p):
    """The schedule kept for this project ({start, end, daily, waiting}, the end as `deadline` moved it), or None:
    none, or it ended (its lock goes with its process; a reboot ends it too)."""
    f = p.state / LOCK
    if not f.exists():
        return None
    with open(f) as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_SH | fcntl.LOCK_NB)
            return None
        except OSError:
            pass
    st = load_json(p.state / STATE, None)
    if not st:
        return None
    s, e = datetime.fromisoformat(st["start"]), datetime.fromisoformat(st["end"])
    return {"start": s, "end": (moved(p, s) or e) if st["waiting"] else e, "daily": st["daily"],
            "waiting": st["waiting"]}


def stop(p):
    (p.state / STOPPED).touch()


def stopped(p):
    return (p.state / STOPPED).exists()


def called_off(p):
    """A running schedule was stopped: its window's driver keeps the STOP that `stop` left, even one it starts
    with (it clears an earlier run's otherwise)."""
    return stopped(p) and state(p) is not None


def next_line(p):
    """The window driver's stopped push's last line, "Next window: tomorrow 12:00-20:00", or "" when no window
    follows (not daily, or stopped)."""
    nxt = (load_json(p.state / STATE, None) or {}).get("next")
    if not nxt or stopped(p) or state(p) is None:
        return ""
    s, e = map(datetime.fromisoformat, nxt)
    days = (s.date() - now().date()).days
    day = "today" if days == 0 else "tomorrow" if days == 1 else f"{s:%a %d %b}"
    return f"\nNext window: {day} {s:%H:%M}-{e:%H:%M}."


def what(daily, end):
    """"daily 12:00-20:00" (", this one until 18:00" when `deadline` moved it), or "until 20:00"."""
    if not daily:
        return f"until {hhmm(end)}"
    usual = datetime.strptime(daily.split("-")[1].strip(), "%H:%M").time()
    return f"daily {daily}" + ("" if end.time() == usual else f", this one until {hhmm(end)}")


def word(st):
    """status's word for a scheduled run: `waiting: starts 12:00 (daily 12:00-20:00)`, or running and its window."""
    if st["waiting"]:
        return f"waiting: starts {hhmm(st['start'])} ({what(st['daily'], st['end'])})"
    return f"running ({what(st['daily'], st['end'])})"
