"""windvane: run Claude Code on a project's tickets unattended, with a critic, a reviewer, phone questions and a token
ledger. `windvane <command> --help` for each command."""
import argparse
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

from . import actions
from . import critic as critic_mod
from . import (doctor, driver, heavy, install, jobs, net, phone, planner, preflight, privacy, publiccheck, questions,
               requests, schedule, scratch, watch)
from . import held as held_mod
from . import periodic as periodic_mod
from .config import DIR, HOME, Project, TokenError, exe, find, slots, toplevel
from .config import periodic as periodic_entries
from .queue import Queue
from .runner import Usage, inherited_stop, plain_error, scope_github
from .stats import cmd_stats, shared_line  # cmd_stats: main runs `stats` by its name
from .util import git, gout, hhmm, last_log, load_json, now, read_jsonl


# commands the phone and the desk use to look and stop: a token file that can't be read mustn't block them
LOCAL = {"stop", "status", "overview", "stats", "watch", "deadline", "quiet", "mode", "await"}
STOP_WAIT = 90  # seconds `stop --now` waits for the run to end: a 5 s poll, then up to 60 s for SIGTERM to work


def parse_deadline(until, for_, usage=None, base=None):
    """--for from now, or --until: an HH:MM is its first time after `base` (default now)."""
    if for_:
        return now() + (schedule.span(for_) or sys.exit(usage or "--for takes e.g. 90m or 6h"))
    for fmt in ("%Y-%m-%d %H:%M",):
        try:
            return datetime.strptime(until, fmt)
        except ValueError:
            pass
    try:
        t = datetime.strptime(until, "%H:%M")
    except ValueError:
        sys.exit(usage or "--until takes HH:MM or 'YYYY-MM-DD HH:MM'")
    base = base or now()
    d = base.replace(hour=t.hour, minute=t.minute, second=0, microsecond=0)
    return d if d > base else d + timedelta(days=1)


def in_tmux(p):
    return bool(shutil.which("tmux")) and subprocess.run(
        ["tmux", "has-session", "-t", f"={p.tmux}"], capture_output=True).returncode == 0


def running(p):
    """A run started without --detach holds the lock but has no tmux session; tmux covers the moment before a
    detached run takes the lock, and a schedule's lock the waits between its windows."""
    return driver.locked(p) or in_tmux(p) or bool(schedule.state(p))


# ---------- commands

def cmd_init(a):
    root = Path(a.dir or os.getcwd()).resolve()
    if not (root / ".git").exists():
        sys.exit(f"{root} isn't a git repository's top level")
    d = root / DIR
    (d / "tickets").mkdir(parents=True, exist_ok=True)
    cfg = d / "config.toml"
    if not cfg.exists():
        text = (HOME / "templates/config.toml").read_text()
        text = text.replace('name = ""', f'name = "{a.name or root.name}"')
        if a.check:
            text = text.replace('check = ""', f"check = {json.dumps(a.check)}")
        forge = a.forge or guess_forge(root)
        text = text.replace('forge = "github"', f'forge = "{forge}"')
        cfg.write_text(text)
        print(f"forge: {forge}" + ("" if a.forge else " (from the remote; --forge github|git to choose)"))
    for name in ("QUEUE.md", "QUESTIONS.md", "CRITIC.md"):
        if not (d / name).exists():
            (d / name).write_text((HOME / "templates" / name).read_text())
    ignored = gitignore(root)
    if ignored:
        print(f".gitignore: added {' '.join(ignored)}")
    p = Project(root)
    hooks = install.phone_hooks(p)
    print(f"windvane set up in {d}\n  project name: {p.name}; state on this machine: {p.state}\n"
          f"  edit {DIR}/config.toml (the check command!) and {DIR}/CRITIC.md (what a 10 means here)\n"
          f"  then commit {DIR}/ and push it to {p.upstream}: the autopilot reads tickets from there")
    if hooks:
        print(f"  phone: status/stop/overview hooks in {', '.join(hooks)}")
    if p.cfg["forge"] == "github":
        for _, what, fix in doctor.public_warnings(p):
            print(f"  WARNING: {what}: {fix}")


def gitignore(root):
    """Add the patterns of files named like secrets (privacy.GITIGNORE) that the project's .gitignore lacks, at its
    end, never rewriting what's there; make the file when there's none. Returns the patterns added."""
    f = root / ".gitignore"
    text = f.read_text() if f.exists() else ""
    have = {line.strip() for line in text.splitlines()}
    missing = [g for g in privacy.GITIGNORE if g not in have]
    if missing:
        sep = "\n" if text and not text.endswith("\n") else ""
        f.write_text(text + sep + ("\n" if text else "") + "# files named like secrets\n" + "\n".join(missing) + "\n")
    return missing


def guess_forge(root):
    """github only when origin is on github.com and `gh` can open the PRs; plain git for anything else."""
    url = gout(root, "remote", "get-url", "origin") or ""
    return "github" if "github.com" in url and shutil.which("gh") else "git"


def need(p):
    if not p.exists:
        sys.exit(f"windvane: no {DIR}/config.toml here; run `windvane init` first")
    return p


def too_short(left, min_slot, ticket_hours):
    """Refuse a run that would start no ticket: one inside `min_slot`, or too short for any worker's wrap-up, or
    with a time box (`ticket_hours`) too short for it. `left`: None for a scheduled window."""
    worker = driver.WRAP_UP + driver.WRAP_LEAD
    if ticket_hours * 60 < worker:
        sys.exit(f"ticket_hours is {ticket_hours * 60:g} minutes, but a worker needs at least {worker}: it wraps up "
                 f"{driver.WRAP_UP} minutes before it's stopped, and starts only with that {driver.WRAP_LEAD} or more "
                 "minutes ahead. Raise it (e.g. --ticket-hours 1)")
    if left is None:
        return
    if left <= timedelta(minutes=min_slot):
        sys.exit(f"no ticket starts in a run's last {min_slot:g} minutes (min_slot), so this run would do nothing: "
                 "give it longer (e.g. --for 2h)" + (" or --min-slot 0" if left > timedelta(minutes=worker) else ""))
    if left <= timedelta(minutes=worker):
        sys.exit(f"a worker starts only with its wrap-up ({driver.WRAP_UP} minutes before the stop) at least "
                 f"{driver.WRAP_LEAD} minutes ahead, so a run of {worker} minutes or less would do nothing: "
                 "give it longer (e.g. --for 1h)")


def cmd_run(a, p):
    need(p)
    if not shutil.which("claude") and not os.environ.get(driver.SUPERVISED):  # (a restart's crash gets pushed)
        sys.exit("windvane run needs Claude Code (`claude`) on PATH: `windvane doctor` says how to install it")
    try:
        window = schedule.first(a.from_, a.until, a.for_, a.daily)
    except ValueError as e:
        sys.exit(str(e))
    if not (window or a.until or a.for_):
        sys.exit("say when to stop: --until HH:MM or --for 6h")
    if window and not (a.detach or a.supervise):
        sys.exit("--from and --daily wait in a tmux session, as a terminal won't stay open; run: windvane "
                 + " ".join(shlex.quote(x) for x in sys.argv[1:]) + " --detach")
    deadline = window[1] if window else parse_deadline(a.until, a.for_)
    if not os.environ.get(driver.SUPERVISED):  # (a restarted run still winds down)
        too_short(None if window else deadline - now(), a.min_slot, a.ticket_hours)
    if not p.cfg["isolate"] and gout(p.main_root(), "status", "--porcelain"):
        sys.exit("isolate = false runs in the checkout itself: commit or stash your changes first")
    p.state.mkdir(parents=True, exist_ok=True)
    if a.detach and in_tmux(p):  # (the detached run itself starts inside that session)
        sys.exit(f"already running in tmux session {p.tmux}")
    st = not os.environ.get(driver.SUPERVISED) and schedule.state(p)  # (not the window's own)
    if st and st["waiting"]:
        sys.exit(f"a schedule is {schedule.word(st)}; cancel it with windvane stop, then run again")
    if driver.locked(p) or st:
        sys.exit("already running: stop it with windvane stop")
    if not a.no_preflight and sys.stdin.isatty():  # never blocks a run started from a script
        ask_first(p, (deadline - (window[0] if window else now())).total_seconds() / 3600, a.parallel)
    if a.detach:
        args, skip = [], False
        for x in sys.argv[1:]:
            if skip or x == "--detach":
                skip = False
                continue
            if x == "-C":
                skip = True
                continue
            args.append(x)
        args += [] if "--no-preflight" in args else ["--no-preflight"]  # asked here already, if at all
        cmd = " ".join(json.dumps(x) for x in [*exe(), "-C", str(p.main_root()), *args, "--supervise"])
        subprocess.run(["tmux", "new-session", "-d", "-s", p.tmux, "-c", str(p.main_root()), cmd], check=True)
        waits = window and window[0] > now()
        print(f"autopilot {'waiting' if waits else 'running'} in tmux session {p.tmux}"
              + (f", starts {hhmm(window[0])}" if waits else "") + f", until {hhmm(deadline)}"
              + (f"; then daily {a.daily} until windvane stop" if a.daily else "") + "\n"
              f"  watch:  windvane watch      (or tmux attach -t {p.tmux}; leave with Ctrl-b d)\n"
              f"  status: windvane status     stop: windvane stop")
        return
    if a.supervise:  # the detached run: the driver in a process of its own, restarted after a crash
        argv = [x for x in sys.argv[1:] if x != "--supervise"]
        sys.exit(schedule.run(p, argv, window, a.daily) if window else driver.supervise(p, argv, deadline))
    pilot = driver.Pilot(p, a)
    ready = pilot.run(deadline)
    if pilot.signalled:  # its end is done: now die of the signal, as `supervise` and the shell expect
        signal.signal(pilot.signalled, signal.SIG_DFL)
        os.kill(os.getpid(), pilot.signalled)
    if not ready:
        sys.exit(1)  # it couldn't start (a failed setup): scripts see it


def ask_first(p, hours, parallel=None):
    print("preflight: reading the next tickets for questions you can answer now...", flush=True)
    qs = preflight.find(p, hours, Usage(p), parallel)
    if not qs:
        print("preflight: no questions; starting")
        return
    print(f"preflight: {len(qs)} question{'s' * (len(qs) > 1)}. Enter skips one: a run that needs it asks your phone.")
    preflight.ask(p, qs)


def cmd_preflight(a, p):
    need(p)
    hours = (parse_deadline(None, a.for_) - now()).total_seconds() / 3600
    if not a.json:
        return ask_first(p, hours) if sys.stdin.isatty() else sys.exit("preflight: no terminal; use --json")
    qs = preflight.find(p, hours, Usage(p))
    preflight.offered(p, qs)
    for x in qs:
        x["answer_with"] = f"windvane answer {x['id']} <letter, or your own words in quotes>"
    print(json.dumps(qs, indent=1))


def cmd_stop(a, p):
    killed = jobs.end_kept(p, why="windvane stop")  # a run's own end would too, but one past its stop has no run
    if killed:
        print(f"{p.display}: killed kept jobs: {', '.join(killed)}")
    if a.if_running and not running(p):
        print(f"{p.display}: autopilot isn't running")
        return
    p.state.mkdir(parents=True, exist_ok=True)
    stop_now = getattr(a, "now", False)  # callers that predate --now pass a Namespace without it
    if stop_now and not running(p):
        return print(f"{p.display}: autopilot isn't running")
    with schedule.turn(p):  # a window starting meanwhile: nothing starts, or it's running and its driver keeps STOP
        st = schedule.state(p)
        if st:
            schedule.stop(p)
    if st and st["waiting"]:
        return print(f"{p.display}: schedule ({schedule.what(st['daily'], st['end'])}) called off: nothing starts "
                     f"at {hhmm(st['start'])}")
    ends = f", and the schedule (daily {st['daily']}) ends: no later window starts" if st and st["daily"] else ""
    if not stop_now:
        (p.state / "STOP").touch()
        return print(f"{p.display}: autopilot stops after the current ticket{ends}")
    (p.state / "STOP").write_text("now")
    if ends:
        print(f"{p.display}: the schedule (daily {st['daily']}) ends: no later window starts")
    print(f"{p.display}: stopping now: killing the running sessions...", flush=True)
    for _ in range(STOP_WAIT):  # each session's next poll kills it (SIGTERM, then SIGKILL a minute later)
        if not driver.locked(p):
            return print(f"{p.display}: stopped; the worktrees keep their work for the next run")
        time.sleep(1)
    print(f"{p.display}: still stopping; `windvane status` shows when it's done")


def cmd_deadline(a, p):
    when = " ".join(a.when)
    usage = "windvane deadline takes HH:MM, 'YYYY-MM-DD HH:MM' or +90m / +2h"
    st = schedule.state(p)
    waits = st and st["waiting"]  # an HH:MM is then the first after the start
    t = (parse_deadline(None, when[1:], usage) if when.startswith("+")
         else parse_deadline(when, None, usage, st["start"] if waits else None))
    if not running(p):
        sys.exit(f"{p.display}: autopilot isn't running; start one with windvane run --until {t:%H:%M}")
    if waits and t <= st["start"]:
        sys.exit(f"{p.display}: the run starts {hhmm(st['start'])}; give an end after that")
    (p.state / "DEADLINE").write_text(t.isoformat(timespec="seconds"))
    later = f" (this window only: later ones keep daily {st['daily']})" if st and st["daily"] else ""
    if waits:
        return print(f"{p.display}: the run starting {hhmm(st['start'])} now ends at {t:%Y-%m-%d %H:%M}{later}")
    print(f"{p.display}: the run now ends at {t:%Y-%m-%d %H:%M}{later}. New tickets keep to it at once; running "
          "sessions are stopped by then")


def parallel_arg(v):
    if v.lower() == "auto":
        return "auto"
    if v.isdigit() and int(v) > 0:
        return int(v)
    raise argparse.ArgumentTypeError('a number of tickets, or "auto"')


def cmd_status(a, p):
    fetch_for_held(p)
    on, st = running(p), schedule.state(p)
    if a.line:
        print(status_line(p, on, st))
        return
    tmux = f"in tmux {p.tmux}" if in_tmux(p) else ""
    print("autopilot: " + (f"{schedule.word(st)}{', ' * bool(tmux)}{tmux}" if st
                           else f"running {tmux}".strip() if on else "not running"))
    print(f"questions now: {questions.mode(p)}")
    if (spent := actions.line(p)):
        print(f"{spent} this month (cap {actions.cap_minutes(p):,})")
    st = load_json(p.state / "state.json", {})
    for tid, slot in sorted(st.get("running", {}).items(), key=lambda kv: kv[1]) if on else ():
        print(f"slot {slot}: {tid}")
    for key, q in questions.waiting_on(st.get("questions", {})).items():
        print(f"waiting on you: {key}: {q['q']}  ({', '.join(f'{k}: {v}' for k, v in sorted(q['opts'].items()))})")
    for q in questions.with_ids(gout(p.main_root(), "show", f"{p.upstream}:{p.questions_rel}"), p):
        print(f"provisional: {q['ticket']}: {q['question']} Built {q['built'] or '?'}"
              + (f": {q['option']}" if q["option"] else "") + f". To change: windvane answer {q['id']} <letter>")
    for line in requests.unseen(p):
        print(f"windvane request {line}")
    print("\n".join(held_text(p, subject=None) + last_log(p, 12)))


def status_line(p, on, st):
    """The phone's one line on a project: on or off, held-back changes, Actions minutes, the log's last line."""
    held, last = held_count(p), (last_log(p) or [""])[0][11:]
    last = "" if held or HELD_LOG.search(last) else last  # the held count says it; or it's no longer held
    last = "" if st and st["waiting"] and "schedule: waiting" in last else last  # the line says it already
    extra = "".join(f". {x}" for x in (held, actions.line(p), f"Last: {last}" if last else "") if x)
    return f"{p.display}: autopilot {schedule.word(st) if st else 'running' if on else 'off'}{extra}"


def status_running():
    """The phone's `status`: a line for each known project whose autopilot is on (running, or a schedule waiting for
    its window), so the one that matters isn't lost among idle ones; `overview` still covers every project. A project
    whose repo or config is gone is skipped. One line when none runs: an empty answer would read as "no answer"."""
    lines = []
    for root in phone.projects().values():
        try:
            p = Project(Path(root))
            if not p.exists or not running(p):
                continue
            fetch_for_held(p)
            lines.append(status_line(p, True, schedule.state(p)))
        except (SystemExit, Exception):  # one project's broken config or state mustn't cost the others their line
            continue
    print("\n".join(lines) or "windvane: no autopilot running (overview lists every project)")


FETCH_S = 10  # seconds `status` gives its fetch: a slow remote costs a moment, never a hang


def fetch_for_held(p):
    """One quick, silent fetch when a held change was pushed by hand, so one merged on the remote meanwhile (squashed
    too: `held_here`) drops off `status` without waiting for a run; no network otherwise, and a failed fetch leaves
    the local view as it was. `--prune`: a head branch deleted once merged mustn't stay as where the change is."""
    if any(on for *_, on in held_mod.held_now(p)):
        net.call(["git", "fetch", "-q", "--prune", p.remote], p.main_root(), FETCH_S,
                 {**os.environ, "GIT_TERMINAL_PROMPT": "0"})


# A log line about a held branch: `status --line` leaves it out, as the held count (or none, once it's gone) says more.
HELD_LOG = re.compile(r"kept only locally on |the local branch |: still not pushed")


def held_count(p):
    """The phone's held-back count (pushed by hand but not merged counts too: it waits on the user) and how long the
    oldest has waited; "" when none waits."""
    held = held_mod.held_now(p)
    if not held:
        return ""
    n, pushed = len(held), sum(bool(on) for *_, on in held)
    waited = held_mod.age(time.time() - min(at for _, _, at, _ in held))
    state = ("not pushed" if not pushed else "pushed, not merged" if pushed == n
             else f"({n - pushed} not pushed, {pushed} pushed, not merged)")
    return f"{n} held-back change{'s' * (n > 1)} {state}, " + (f"oldest {waited}" if n > 1 else f"for {waited}")


def held_text(p, subject=60):
    """The held branches' lines (commit subjects cut at `subject`, or whole) and, after them, what happens next: a run
    retries only those not pushed, the user merges those pushed by hand; nothing when none waits."""
    now = held_mod.held_now(p)
    if not now:
        return []
    pushed = sum(bool(on) for *_, on in now)
    after = ("each run's start tries these again" if not pushed else "listed until main has them" if pushed == len(now)
             else "each run's start tries those not pushed again; the others are listed until main has them")
    return held_mod.held_lines(p, now, subject) + [f"  ({after})"]


def cmd_request(a):
    """`windvane request`: works from any directory, a windvane project or not; the inbox is the machine's."""
    if a.status:
        print("\n".join(requests.status_lines()))
        return
    if a.answer:
        code, line = requests.answer(a.answer, a.title or "")
        print(line)
        if code:
            sys.exit(code)
        return
    if not a.title:
        sys.exit('windvane request: give a title: windvane request "<what windvane should do>" [--body FILE|-]')
    try:
        body = sys.stdin.read() if a.body == "-" else Path(a.body).read_text(errors="replace") if a.body else ""
    except OSError as err:
        sys.exit(f"windvane request: can't read the body: {err.strerror}")
    code, line = requests.file(a.title, body)
    print(line)
    if code:
        sys.exit(code)


def cmd_overview(a, p):
    git(p.main_root(), "fetch", "-q", p.remote)
    q = Queue(p)
    st = [q.status(t) for t in q.order]
    done = st.count("done")
    lines = [f"{p.display}: {done} of {len(st)} tickets done; {st.count('wip')} in progress, {st.count('todo')} to do."]
    lines.append(f"Autopilot {'on' if running(p) else 'off'}.")
    work = next((m.group(1) for l in reversed(last_log(p, 200)) if (m := re.search(r" (\S+): worker run", l))), None)
    if running(p) and work:
        lines[-1] = f"Autopilot on, working on {work}: {q.titles.get(work, '')}."
    recs = read_jsonl(p.state / "critic.jsonl")
    c = critic_mod.calibration(recs, p.critic["pass"])
    if c:
        n = len(c["tickets"])
        line = (f"Critic, last {n} tickets: first scores {critic_mod.spread(c['first'])}; {c['final_on_bar']} of {n} "
                f"ended exactly on the pass mark ({c['bar']:g}).")
        if c["accepted"]:
            line += f" {c['accepted']} passed only by running out of revisions."
        lines.append(line)
    lines.append(check_line(p, recs))
    lines.append((spent := actions.line(p)) and f"{spent} this month.")
    lines += [l[:1].upper() + l[1:] for l in held_text(p)]  # overview's lines are sentences
    print("\n".join(filter(None, lines + overview_extra(p))))


def check_line(p, recs):
    """overview's word on the critic's scale: the checks that stopped telling tickets apart or that it keeps leaving
    unanswered, or, for an old
    numeric scale, how to convert it (the driver never edits a project's scale itself)."""
    if not p.critic["enabled"] or not (p.root / p.rubric_rel).is_file():
        return ""
    lad = critic_mod.rubric(p, p.root)
    if not lad:
        return (f"Critic scale is the old numeric kind; to convert it, rewrite {p.rubric_rel} as ladders of yes/no "
                f"checks, like {HOME / 'templates/CRITIC.md'}.")
    rates = critic_mod.rates(recs)
    said = [f"{d}.{cid} {a}" for d, cs in lad.items() for r, cid, _ in cs
            if cid in rates and (a := critic_mod.advice(*rates[cid], top=r > p.critic["pass"] or r == cs[-1][0]))]
    gaps = [f"Critic left {cid} unanswered in {k} of the last {n}: check its wording in {p.rubric_rel} or "
            f"windvane's {HOME / 'prompts/CRITIC.md'}." for cid, k, n in critic_mod.unanswered(recs)]
    return "\n".join(filter(None, [shared_line(p, lad), critic_mod.ceiling_line(p, lad),
                                    f"Critic checks, last 10 tickets: {'; '.join(said)}." if said else "", *gaps]))


def overview_extra(p, limit=30, most=15):
    """The project's own overview lines (`overview_extra`). Bounded, so the phone's `overview` (90 s for every
    project) still answers; a failure is one line, never the command's whole error output."""
    cmd = p.cfg["overview_extra"]
    if not cmd:
        return []
    r = subprocess.Popen(cmd, shell=True, cwd=p.root, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                         start_new_session=True)  # its own group: a timeout kills the children holding the pipes too
    try:
        out, err = r.communicate(timeout=limit)
    except subprocess.TimeoutExpired:
        os.killpg(r.pid, signal.SIGKILL)
        r.communicate()
        return [f"overview_extra: no answer in {limit:g} s"]
    if r.returncode:
        why = (err.strip().splitlines() or [""])[-1][:160]
        return [f"overview_extra failed (exit {r.returncode}){f': {why}' if why else ''}"]
    return out.rstrip().splitlines()[:most]


def cmd_ci(a, p):
    sys.exit(actions.ci(p, a.workflow, a.ref, a.dry_run))


def cmd_queue(a, p):
    git(p.main_root(), "fetch", "-q", p.remote)
    st = load_json(p.state / "state.json", {})
    q = Queue(p)
    for w in q.warnings():
        print(f"warning: {w}")
    aside = st.get("aside", {}) if running(p) else {}  # a finished run's set-asides start afresh in the next one
    print(q.listing(questions.tickets(st.get("questions", {})), hints=True, aside=aside)
          or f"no tickets on {p.upstream} yet")
    local = sorted(t for t in (p.norm(f.stem) for f in (p.root / p.tickets_rel).glob("*.md")) if t and t not in q.files)
    if local:
        print(f"not on {p.upstream} yet (the autopilot can't see them): {' '.join(local)}; commit and push")
    for e in p.periodic.values():
        if e["after_group"]:
            print(periodic_mod.group_line(e, q))
        if e["requests"]:
            n = len(requests.pending())
            print(f"{e['name']} runs while windvane's inbox has requests: {n} untriaged (windvane request --status)")


def waits_for(p, given):
    """The ids of the waiting questions `given` names: its own id (`T-003.q2`), or every one of its ticket's."""
    waits = questions.waiting_on(load_json(p.state / "state.json", {}).get("questions", {}))
    exact = [k for k in waits if k.lower() == given.strip().lower()]
    return exact or [k for k, q in waits.items() if questions.ticket_of(k, q) == p.norm(given)]


def cmd_answer(a, p):
    letter = a.answer.upper() if re.fullmatch(r"[A-Ea-e]", a.answer) else None
    why = "" if letter else privacy.refusal(p, a.answer)
    if why:  # before anything is written: the state's answers become a commit too
        sys.exit(f"{a.ticket}: not recorded: {why}. Answer again without it.")
    keys = waits_for(p, a.ticket)
    if len(keys) > 1:  # which one the letter is for can't be told
        sys.exit(f"{p.norm(a.ticket)} has {len(keys)} open questions: answer each by its id ({', '.join(keys)}), "
                 f"e.g. windvane answer {keys[0]} <letter>")
    x = preflight.lookup(p, a.ticket) if not keys else None
    if x:  # a preflight question: recorded on main for the ticket's worker
        print(f"{x['ticket']}: {preflight.record(p, [(x, letter, None if letter else a.answer)])}")
        return
    if not keys:
        p.norm(a.ticket.split(".")[0]) or sys.exit("give a ticket id, or a question's id from preflight (T-003.1)")
        if a.ticket.upper() in {k.upper() for k in questions.shown(p)["built"]}:
            sys.exit(f"{a.ticket}: its recommendation is being built and isn't on {p.main} yet; once its ticket "
                     "merges, answer again (`windvane status` then lists it)")
        sys.exit(f"{p.norm(a.ticket) or a.ticket}: no question of it waits for an answer (open, provisional on "
                 f"{p.main}, or from preflight)")
    questions.give_answer(p, keys[0], letter, None if letter else a.answer)
    preflight.forget(p, {keys[0]})  # preflight lists an open phone question under the same id
    print(f"{keys[0]}: answer recorded; the autopilot builds it on the ticket's next run")


def cmd_check(a, p):
    mem = getattr(a, "mem", None)
    if mem is not None and not p.cfg["heavy_check"]:
        sys.exit("check --mem: only a heavy check waits for free memory; this project has heavy_check off "
                 f"({DIR}/config.toml)")
    in_job = bool(os.environ.get(jobs.IN_JOB))
    if p.cfg["heavy_check"] and os.environ.get(heavy.STOP_AT) and not in_job:
        sys.exit(jobs.session_check(p, os.getcwd(), mem, a.lines))
    ok, summary, log = jobs.check(p, os.getcwd(), mem=mem, announce=in_job)
    if ok is False and log:
        print(jobs.tail(log, a.lines))
    print(summary)
    sys.exit(0 if ok else 1 if ok is False else heavy.TIMED_OUT)  # not run: not a failure


CRITIC_WAIT = 8  # minutes of retry pauses `windvane critic` may spend: the worker's Bash call stops at 10


def critic_retry(p, sleep=time.sleep):
    """`critic.judge`'s retry when a worker runs `windvane critic`: the run's backoff (`retries`, `retry_minutes`),
    as many pauses as fit in CRITIC_WAIT. When they're spent the worker is told the critic is unavailable and goes
    on; the driver asks the critic again, with its full backoff, after the worker finishes."""
    waits = []

    def retry(error):
        wait = p.ap["retry_minutes"] * 2 ** len(waits)
        if len(waits) >= p.ap["retries"] or sum(waits) + wait > CRITIC_WAIT:
            return False
        waits.append(wait)
        print(f"The critic session died ({phone.cut(plain_error(error))}); retry {len(waits)} in {wait:g} min.", flush=True)
        sleep(wait * 60)
        return True
    return retry


def cmd_critic(a, p):
    if getattr(a, "project", False):  # a worker's `windvane critic <ID>` has no --project
        return critic_project(a, p)
    tid = p.norm(a.ticket or "") or sys.exit("give a ticket id, or --project")
    if not p.critic["enabled"]:
        print("CRITIC STEP: skip. The critic is off for this project.")
        return
    stood = critic_mod.stands(p, tid, os.getcwd())
    if stood:
        print(f"CRITIC STEP: pass. The verdict on {stood[:12]} stands: since then you only merged {p.upstream} and "
              "resolved it. Go on to the check.")
        return
    usage = Usage(p)
    entry, text = critic_mod.judge(p, tid, os.getcwd(), usage=usage, stop_if=inherited_stop(p, usage),
                                   retry=critic_retry(p))
    print(text)
    if entry is None:  # the text ends with its CRITIC STEP line
        sys.exit(1)
    pr = driver.pr_for(p, gout(os.getcwd(), "branch", "--show-current"))
    if not critic_mod.comment(p, entry, pr):
        print(f"\n(couldn't post the critique on PR #{pr})")
    print("\n" + critic_mod.next_step(p, tid, entry))


def critic_project(a, p):
    """`windvane critic --project`: the critic on the whole project as it stands on main, run as the `project-critic`
    [[periodic]] preset would be (its covered points land as notes on their tickets), then its report."""
    need(p)
    if a.last:
        print(critic_mod.project_scores(p))
        return
    net.git(p.main_root(), "fetch", "-q", p.remote)
    lad = critic_mod.ladder(gout(p.main_root(), "show", f"{p.upstream}:{p.rubric_rel}"))
    dims = [d.strip().lower() for d in (a.dims or "").split(",") if d.strip()]
    if not lad:
        sys.exit(f"no ladder scale in {p.rubric_rel} on {p.upstream}: the project critic answers a ladder's checks")
    if set(dims) - set(lad):
        sys.exit(f"no dimension {', '.join(d for d in dims if d not in lad)} in {p.rubric_rel} (it has: {', '.join(lad)})")
    e = periodic_entries([{"name": "project-critic", "builtin": "project-critic", "dims": dims,
                           "parallel": a.parallel or slots(p), "minutes": a.minutes}])["project-critic"]
    before = len(read_jsonl(p.state / "critic.jsonl"))
    desk_periodic(p, e)
    got = [r for r in read_jsonl(p.state / "critic.jsonl")[before:] if r.get("ticket") == critic_mod.PROJECT]
    if not got or not got[-1].get("dims"):
        sys.exit("the project critic gave no verdict (see `windvane log`)")
    print(got[-1]["critique"])


def cmd_plan(a, p):
    need(p)
    goal = " ".join(a.goal) or p.cfg["goal"]
    held_mod.push_held(p)
    try:
        n = planner.plan(p, goal, not a.no_followups, Usage(p), None, merge=a.merge, max_new=a.max, busy_raises=True)
    except planner.PlanBusy as e:
        raise SystemExit(f"windvane: {e}; try again when it's done")
    print(f"{held_mod.n_tickets(n)} planned" + ("" if n else "; see the log: windvane status"))


def cmd_new(a, p):
    q = Queue(p, "HEAD")
    tid = q.next_id()
    f = p.root / p.tickets_rel / f"{tid}.md"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text((HOME / "templates/ticket.md").read_text().replace("<ID>", tid).replace("<title>", " ".join(a.title)))
    print(f)


def cmd_mode(a, p):
    print(questions.mode(p))


def cmd_quiet(a, p):
    """Both quiet-times files, yours for every project first, then this project's: a run keeps both."""
    print("Quiet times: `daily 23:00-08:00` or `2026-10-02 13:00-15:00`, one per line, in either file.")
    for f, whose in zip(questions.quiet_files(p), ("yours, every project", "this project only")):
        print(f"{f} ({whose}):")
        try:
            print(f.read_text().rstrip() or "(empty)" if f.exists() else "(none)")
        except OSError as e:
            print(f"(can't read it: {e.strerror or e})")
    print("now:", questions.mode(p))


def cmd_periodic(a, p):
    """A [[periodic]] entry and its `then` chain now, from the desk, as a run would between tickets. It takes the
    run's lock, so it never runs beside one (nor one beside it)."""
    need(p)
    e = p.periodic.get(a.name)
    if not e:
        sys.exit(f"no [[periodic]] entry {a.name!r} in {DIR}/config.toml"
                 + (f" (there is: {', '.join(p.periodic)})" if p.periodic else ""))
    desk_periodic(p, e)


def desk_periodic(p, e):
    """Run [[periodic]] entry `e` (and its chain) from the desk, under the run's lock."""
    if running(p):
        sys.exit("a run is busy: it runs periodic sessions itself between tickets; try again once it stops")
    if not p.cfg["isolate"] and gout(p.main_root(), "status", "--porcelain"):
        sys.exit("isolate = false runs in the checkout itself: commit or stash your changes first")
    keys = ("cap", "week_cap", "ticket_hours", "min_slot", "answer_wait")
    pilot = driver.Pilot(p, argparse.Namespace(**{k: p.ap[k] for k in keys}, replan=0, goal="", parallel=1))
    pilot.take_lock()
    try:
        for f in ("STOP", "DEADLINE"):  # an earlier run's
            (p.state / f).unlink(missing_ok=True)
        pilot.deadline = now() + timedelta(days=1)  # each session keeps to its own `minutes`
        if not (pilot.rules_ok() and pilot.prepare(0)):
            sys.exit(f"windvane: {pilot.halt}")
        pilot.periodic(e)
        if pilot.held:
            print(f"Held for you (parked in QUEUE.md): {', '.join(pilot.held)}")
    finally:
        pilot.lockfile.close()


class Version(argparse.Action):
    """Like argparse's own, but the line is worked out only when asked for: a clone's needs two git calls."""
    def __init__(self, option_strings, dest, **kw):
        super().__init__(option_strings, dest, nargs=0, default=argparse.SUPPRESS, help=kw.get("help"))

    def __call__(self, parser, *_):
        print(doctor.own_version())
        parser.exit()


def gb(text):
    """A `--mem` value: GB, 0 or more, as `heavy_mem` takes them."""
    try:
        v = float(text)
    except ValueError:
        v = -1
    if not 0 <= v < float("inf"):
        raise argparse.ArgumentTypeError(f"{text!r}: use a number of GB (0 or more)")
    return v


def expected(text):
    """An `--expect` value: minutes, more than 0."""
    try:
        v = float(text)
    except ValueError:
        v = 0
    if not 0 < v < float("inf"):
        raise argparse.ArgumentTypeError(f"{text!r}: use a number of minutes (more than 0)")
    return v


def public_check(root, github):
    """`public-check` (and `doctor --leaks`): any git repository, windvane's or not. Exit 2 outside one, as for a
    scan that failed."""
    if not root:
        print("windvane public-check: run it in a git repository", file=sys.stderr)
        return 2
    return publiccheck.main(root, github)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="windvane", description=__doc__)
    ap.add_argument("-C", dest="cwd", help="run as if started in this directory")
    ap.add_argument("--version", action=Version, help="which windvane this is: a release, or a clone's commit")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("init", help="set windvane up in a repository")
    s.add_argument("dir", nargs="?")
    s.add_argument("--name")
    s.add_argument("--check", help="the project's CI command")
    s.add_argument("--forge", choices=("github", "git"))
    s = sub.add_parser("install", help="add the windvane skill and the mention hook to Claude Code, asking first")
    s.add_argument("--yes", action="store_true", help="make every change without asking (for scripts)")
    s = sub.add_parser("uninstall", help="remove exactly what install added")
    s.add_argument("--yes", action="store_true", help="without asking")
    s = sub.add_parser("doctor", help="what this machine lacks for windvane, and how to fix it")
    s.add_argument("--clean", action="store_true",
                   help="first remove windvane's leftover temp dirs, and what every project's state dir no longer "
                   "needs (idle merged worktrees, old transcripts, merged tickets' critic packs)")
    s.add_argument("--dry-run", action="store_true", help="with --clean: list what would go, with sizes")
    s.add_argument("--leaks", action="store_true", help="instead: `windvane public-check` without --github")
    s = sub.add_parser("public-check", help="what this repo would expose if it went public: its whole history, and "
                       "with --github its pull requests and settings (reads and changes nothing)")
    s.add_argument("--github", action="store_true", help="also scan the pull requests and check the repo's settings, "
                   "through gh")
    s = sub.add_parser("phone", help="pushes and questions on your phone (ntfy); off until `phone setup`")
    s.add_argument("action", choices=("setup", "off", "rotate", "listen"),
                   help="setup: turn it on and subscribe; off; rotate: new topics; listen: act on the phone's replies")
    s.add_argument("--server", help=f"an ntfy server of your own (default {phone.SERVER})")
    s.add_argument("--no-service", action="store_true", help="setup: don't install the background listener service")
    s = sub.add_parser("run", help="work the tickets until a deadline")
    g = s.add_mutually_exclusive_group()
    g.add_argument("--until", help="HH:MM or 'YYYY-MM-DD HH:MM'")
    g.add_argument("--for", dest="for_", help="e.g. 90m or 6h")
    s.add_argument("--from", dest="from_", metavar="WHEN", help="with --detach: start at HH:MM or 'YYYY-MM-DD HH:MM' (now if past)")
    s.add_argument("--daily", metavar="HH:MM-HH:MM",
                   help="with --detach: run in this window every day (may cross midnight), until windvane stop")
    s.add_argument("--detach", action="store_true", help="run in a tmux session, restarted after a crash")
    s.add_argument("--supervise", action="store_true", help=argparse.SUPPRESS)  # what --detach runs in tmux
    s.add_argument("--no-preflight", action="store_true", help="start without asking the next tickets' questions")
    s.add_argument("--parallel", type=parallel_arg,
                   help='most tickets at a time, or "auto" (default from config: auto, up to max_parallel 4)')
    s.add_argument("--cap", type=float, help="max %% of the 5-hour window (default from config, 75)")
    s.add_argument("--week-cap", type=float, help="stop at this %% of the weekly limit (default 95)")
    s.add_argument("--ticket-hours", type=float, help="max hours for one worker run (default 3)")
    s.add_argument("--min-slot", type=float, help="don't start a ticket with fewer minutes left (default 30)")
    s.add_argument("--answer-wait", type=float, help="minutes a phone question waits before the recommendation")
    s.add_argument("--replan", type=int, default=0, metavar="N",
                   help="when the queue runs dry, plan new tickets (goal + critic notes), up to N times")
    s.add_argument("--goal", help="the goal for --replan (default: config's goal)")
    s = sub.add_parser("preflight", help="the questions the next tickets will need, to answer now")
    s.add_argument("--for", dest="for_", default="4h", help="how long the run will be (default 4h)")
    s.add_argument("--json", action="store_true", help="print them for a Claude session to ask")
    s = sub.add_parser("stop", help="stop after the current ticket")
    s.add_argument("--if-running", action="store_true")
    s.add_argument("--now", action="store_true", help="kill the running sessions now; their worktrees keep the work")
    s = sub.add_parser("deadline", help="move a running autopilot's deadline")
    s.add_argument("when", nargs="+", help="HH:MM, 'YYYY-MM-DD HH:MM', or from now: +90m, +2h")
    s = sub.add_parser("status", help="running or not, open questions, the log's tail")
    s.add_argument("--line", action="store_true", help="one line (for the phone)")
    s.add_argument("--running", action="store_true",
                   help="one line for every project whose autopilot is on, from anywhere (the phone's status; implies --line)")
    s = sub.add_parser("request", help="file a feature request or bug report to windvane itself, from any project")
    s.add_argument("title", nargs="?", help="one line: what windvane should do or stop doing")
    s.add_argument("--body", metavar="FILE", help="the details from a file, or - for stdin: what happened, evidence "
                   "(log lines, file:line in windvane, a repro), what you wanted, the workaround you used")
    s.add_argument("--status", action="store_true", help="every request and its verdict")
    s.add_argument("--answer", metavar="R-NNN", help="answer the triage's question on a request (the title argument "
                   "is the answer): it goes back to triage")
    sub.add_parser("overview", help="where the project stands (for the phone)")
    sub.add_parser("queue", help="the ticket queue")
    s = sub.add_parser("answer", help="answer a ticket's question at the desk")
    s.add_argument("ticket")
    s.add_argument("answer", help="a letter, or free text in quotes")
    s = sub.add_parser("check", help="the project's check, reusing a pass for the same tree")
    s.add_argument("--lines", type=int, default=60)
    s.add_argument("--mem", type=gb, help="with heavy_check: GB of free memory this check needs "
                                          "(default: the project's heavy_mem, else machine.toml's min_free_gb)")
    s = sub.add_parser("ci", help="start a GitHub Actions workflow, only within the month's minutes cap")
    s.add_argument("workflow", nargs="?", default="", help="its file or name (default: the repo's only one)")
    s.add_argument("--ref", default="", help="the branch to run it on (default: the current one)")
    s.add_argument("--dry-run", action="store_true", help="count and say, but start nothing")
    s = sub.add_parser("critic", help="have the critic judge a ticket's evidence pack, or the whole project")
    s.add_argument("ticket", nargs="?")
    s.add_argument("--project", action="store_true", help="judge the project on main against its CRITIC.md")
    s.add_argument("--dims", help="with --project: only these dimensions, comma-separated (default: all)")
    s.add_argument("--parallel", type=int, help="with --project: at most N dimension sessions at once")
    s.add_argument("--minutes", type=int, default=60, help="with --project: each session's time limit (default 60)")
    s.add_argument("--last", action="store_true", help="with --project: the latest verdicts' scores per dimension")
    s = sub.add_parser("bg", help="start a long command in the background")
    s.add_argument("--heavy", action="store_true", help="run it under the machine-wide heavy lock")
    s.add_argument("--mem", type=gb, help="with --heavy: GB of free memory it needs (default: as `heavy`)")
    s.add_argument("--keep", action="store_true", help="with --heavy: it outlives the session (until the run's "
                                                       "stop, plus heavy_keep_hours); the ticket resumes when it ends")
    s.add_argument("--expect", type=expected, metavar="MIN", help="with --heavy: minutes it likely runs (as `heavy --expect`)")
    s.add_argument("--cancel", action="store_true", help="end your own job of that name instead, waiting or running")
    s.add_argument("name")
    s.add_argument("command", nargs=argparse.REMAINDER)
    s = sub.add_parser("heavy", help="run a machine-heavy command (engine, render) one at a time on this machine")
    s.add_argument("--wait", type=float, help="minutes to wait for the machine before giving up "
                                              "(default: the session's stop, else machine.toml's heavy_wait, 60)")
    s.add_argument("--mem", type=gb, help="GB of free memory it needs before starting "
                                             "(default: the project's heavy_mem, else machine.toml's min_free_gb)")
    s.add_argument("--expect", type=expected, metavar="MIN", help="minutes it likely runs, for the estimate of the jobs waiting "
                                                   "behind it until it has run times of its own")
    s.add_argument("--status", action="store_true", help="who holds the lock and who waits, without waiting")
    s.add_argument("command", nargs=argparse.REMAINDER)
    s = sub.add_parser("await", help="wait (up to ~9.5 min) for a bg command (this ticket's own)")
    s.add_argument("name")
    s.add_argument("lines", nargs="?", type=int, default=40)
    s = sub.add_parser("plan", help="a planner run writes tickets for a goal and the critic's follow-ups")
    s.add_argument("goal", nargs="*")
    s.add_argument("--merge", action="store_true", help="merge the tickets at once (default: a PR to look at)")
    s.add_argument("--no-followups", action="store_true", help="leave out the critic's notes")
    s.add_argument("--max", type=int, default=6, help="at most this many new tickets (default 6)")
    s = sub.add_parser("periodic", help="run a [[periodic]] session (and its `then` chain) now")
    s.add_argument("name")
    s = sub.add_parser("new", help="write a new ticket file with the next id")
    s.add_argument("title", nargs="+")
    s = sub.add_parser("stats", help="tokens per role or ticket, from the ledger")
    s.add_argument("--days", type=float, default=7)
    s.add_argument("--by", choices=("role", "ticket"), default="role")
    s.add_argument("--wide", action="store_true", help="add the context columns, base and cached/turn: wider than 100 columns")
    s.add_argument("--critic", action="store_true", help="the spread of the critic's scores instead")
    s.add_argument("--periodic", action="store_true", help="the scores [[periodic]] sessions gave, oldest first")
    s.add_argument("--last", type=int, default=10, help="with --critic: over the last N tickets (default 10)")
    s.add_argument("--since", help="with --critic: only verdicts at or after a date (YYYY-MM-DD) or a commit")
    sub.add_parser("mode", help="how a design question is handled right now")
    sub.add_parser("quiet", help="show the quiet times (yours for every project, and this project's)")
    s = sub.add_parser("watch", help="follow the running sessions live (costs no tokens)")
    s.add_argument("-n", type=int, default=30, help="start with this many past events")
    s.add_argument("-o", type=int, default=6, help="lines of each command's output (0 hides output)")
    a = ap.parse_args(argv)
    if a.cwd:
        if not os.path.isdir(a.cwd):  # a phone hook whose repo was deleted: one line, not a traceback
            sys.exit(f"windvane: {a.cwd} doesn't exist (any more)")
        os.chdir(a.cwd)
    if a.cmd == "status" and a.running:
        return status_running()
    if a.cmd == "init":
        return cmd_init(a)
    if a.cmd in ("install", "uninstall"):
        return getattr(install, a.cmd)(a.yes)
    if a.cmd == "doctor":
        root = toplevel() if shutil.which("git") else None  # no git is one of the gaps doctor is there to name
        if a.leaks:
            return public_check(root, False)
        return doctor.main(Project(root) if root else None, a.clean, a.dry_run)
    if a.cmd == "public-check":
        return public_check(toplevel() if shutil.which("git") else None, a.github)
    if a.cmd == "phone":
        root = toplevel()
        return phone.main(a, Project(root) if root and (root / DIR / "config.toml").is_file() else None)
    if a.cmd == "request":
        return cmd_request(a)
    if a.cmd == "heavy":
        if a.status:
            return print("\n".join(heavy.status(heavy.reuse_of(Project(root)) if (root := toplevel()) else None)))
        cmd = a.command[1:] if a.command[:1] == ["--"] else a.command
        if not cmd:
            sys.exit("heavy: give a command")
        os.environ.update(scratch.job_env(cmd, "heavy"))
        reuse = None
        if root := toplevel():
            p = Project(root)
            a.mem = p.cfg["heavy_mem"] if a.mem is None else a.mem
            reuse = heavy.reuse_of(p)
        sys.exit(heavy.run(cmd, None if a.wait is None else a.wait * 60, mem=a.mem, reuse=reuse, expect=a.expect))
    p = find()
    phone.use(p)
    try:
        scope_github(p)
    except TokenError:
        if a.cmd not in LOCAL:
            raise
    if a.cmd == "run":
        for k in ("cap", "week_cap", "ticket_hours", "min_slot", "answer_wait"):
            if getattr(a, k) is None:
                setattr(a, k, p.ap[k])
    if a.cmd == "bg":
        cmd = a.command[1:] if a.command[:1] == ["--"] else a.command
        for flag, given in (("--mem", a.mem is not None), ("--keep", a.keep), ("--expect", a.expect is not None)):
            if given and not a.heavy:
                sys.exit(f"bg: {flag} goes with --heavy")
        if a.cancel:
            return sys.exit("bg --cancel: give only the job's name") if cmd or a.heavy else jobs.cancel(p, a.name)
        return jobs.bg(p, a.name, cmd, os.getcwd(), a.heavy, a.mem, a.keep, a.expect) if cmd else sys.exit("bg: give a command")
    if a.cmd == "await":
        jobs.await_(p, a.name, a.lines)  # exits 0 whatever the job's code: it prints it
        return
    if a.cmd == "watch":
        return watch.main(p, a.n, a.o)
    return globals()[f"cmd_{a.cmd}"](a, p)
