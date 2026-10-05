"""windvane: run Claude Code on a project's tickets unattended, with a critic, a reviewer, phone questions and a token
ledger. `windvane <command> --help` for each command."""
import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import textwrap
import time
from datetime import datetime, timedelta
from pathlib import Path

from . import actions
from . import critic as critic_mod
from . import doctor, driver, install, jobs, phone, preflight, questions, watch
from . import periodic as periodic_mod
from .config import DIR, HOME, Project, exe, find, toplevel
from .queue import Queue
from .runner import Usage, inherited_stop
from .util import git, gout, hhmm, load_json, now, read_jsonl


STOP_WAIT = 90  # seconds `stop --now` waits for the run to end: a 5 s poll, then up to 60 s for SIGTERM to work


def parse_deadline(until, for_, usage=None):
    if for_:
        m = re.fullmatch(r"(\d+(?:\.\d+)?)([hm])", for_)
        if not m:
            sys.exit(usage or "--for takes e.g. 90m or 6h")
        return now() + timedelta(**{"hours" if m.group(2) == "h" else "minutes": float(m.group(1))})
    for fmt in ("%Y-%m-%d %H:%M",):
        try:
            return datetime.strptime(until, fmt)
        except ValueError:
            pass
    try:
        t = datetime.strptime(until, "%H:%M")
    except ValueError:
        sys.exit(usage or "--until takes HH:MM or 'YYYY-MM-DD HH:MM'")
    d = now().replace(hour=t.hour, minute=t.minute, second=0, microsecond=0)
    return d if d > now() else d + timedelta(days=1)


def in_tmux(p):
    return bool(shutil.which("tmux")) and subprocess.run(
        ["tmux", "has-session", "-t", f"={p.tmux}"], capture_output=True).returncode == 0


def running(p):
    """A run started without --detach holds the lock but has no tmux session; tmux covers the moment before a
    detached run takes the lock."""
    return driver.locked(p) or in_tmux(p)


def last_log(p, n=1):
    """The log's last `n` lines (None: all of them)."""
    f = p.state / "autopilot.log"
    return f.read_text().splitlines()[-n if n else 0:] if f.exists() else []


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
    p = Project(root)
    hooks = install.phone_hooks(p)
    print(f"windvane set up in {d}\n  project name: {p.name}; state on this machine: {p.state}\n"
          f"  edit {DIR}/config.toml (the check command!) and {DIR}/CRITIC.md (what a 10 means here)\n"
          f"  then commit {DIR}/ and push it to {p.upstream}: the autopilot reads tickets from there")
    if hooks:
        print(f"  phone: status/stop/overview hooks in {', '.join(hooks)}")


def guess_forge(root):
    """github only when origin is on github.com and `gh` can open the PRs; plain git for anything else."""
    url = gout(root, "remote", "get-url", "origin") or ""
    return "github" if "github.com" in url and shutil.which("gh") else "git"


def need(p):
    if not p.exists:
        sys.exit(f"windvane: no {DIR}/config.toml here; run `windvane init` first")
    return p


def cmd_run(a, p):
    need(p)
    if not (a.until or a.for_):
        sys.exit("say when to stop: --until HH:MM or --for 6h")
    deadline = parse_deadline(a.until, a.for_)
    if not p.cfg["isolate"] and gout(p.main_root(), "status", "--porcelain"):
        sys.exit("isolate = false runs in the checkout itself: commit or stash your changes first")
    p.state.mkdir(parents=True, exist_ok=True)
    if a.detach and in_tmux(p):  # (the detached run itself starts inside that session)
        sys.exit(f"already running in tmux session {p.tmux}")
    if driver.locked(p):
        sys.exit("already running: stop it with windvane stop")
    if not a.no_preflight and sys.stdin.isatty():  # never blocks a run started from a script
        ask_first(p, (deadline - now()).total_seconds() / 3600, a.parallel)
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
        print(f"autopilot running in tmux session {p.tmux} until {hhmm(deadline)}\n"
              f"  watch:  windvane watch      (or tmux attach -t {p.tmux}; leave with Ctrl-b d)\n"
              f"  status: windvane status     stop: windvane stop")
        return
    if a.supervise:  # the detached run: the driver in a process of its own, restarted after a crash
        sys.exit(driver.supervise(p, [x for x in sys.argv[1:] if x != "--supervise"], deadline))
    if not driver.Pilot(p, a).run(deadline):
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
    for x in qs:
        x["answer_with"] = f"windvane answer {x['id']} <letter, or your own words in quotes>"
    print(json.dumps(qs, indent=1))


def cmd_stop(a, p):
    if a.if_running and not running(p):
        print(f"{p.display}: autopilot isn't running")
        return
    p.state.mkdir(parents=True, exist_ok=True)
    stop_now = getattr(a, "now", False)  # callers that predate --now pass a Namespace without it
    if stop_now and not running(p):
        return print(f"{p.display}: autopilot isn't running")
    if not stop_now:
        (p.state / "STOP").touch()
        return print(f"{p.display}: autopilot stops after the current ticket")
    (p.state / "STOP").write_text("now")
    print(f"{p.display}: stopping now: killing the running sessions...", flush=True)
    for _ in range(STOP_WAIT):  # each session's next poll kills it (SIGTERM, then SIGKILL a minute later)
        if not driver.locked(p):
            return print(f"{p.display}: stopped; the worktrees keep their work for the next run")
        time.sleep(1)
    print(f"{p.display}: still stopping; `windvane status` shows when it's done")


def cmd_deadline(a, p):
    when = " ".join(a.when)
    usage = "windvane deadline takes HH:MM, 'YYYY-MM-DD HH:MM' or +90m / +2h"
    t = parse_deadline(None, when[1:], usage) if when.startswith("+") else parse_deadline(when, None, usage)
    if not running(p):
        sys.exit(f"{p.display}: autopilot isn't running; start one with windvane run --until {t:%H:%M}")
    (p.state / "DEADLINE").write_text(t.isoformat(timespec="seconds"))
    print(f"{p.display}: the run now ends at {t:%Y-%m-%d %H:%M}. New tickets keep to it at once; running sessions are "
          "stopped by then")


def parallel_arg(v):
    if v.lower() == "auto":
        return "auto"
    if v.isdigit() and int(v) > 0:
        return int(v)
    raise argparse.ArgumentTypeError('a number of tickets, or "auto"')


def cmd_status(a, p):
    on = running(p)
    if a.line:
        last = (last_log(p) or [""])[0][11:]
        held = len(driver.held_lines(p))
        extra = "".join(f". {x}" for x in (held and f"{held} held-back change{'s' * (held > 1)} not pushed",
                                           actions.line(p), f"Last: {last}" if last else "") if x)
        print(f"{p.display}: autopilot {'running' if on else 'off'}{extra}")
        return
    print(f"autopilot: {('running in tmux ' + p.tmux if in_tmux(p) else 'running') if on else 'not running'}")
    print(f"questions now: {questions.mode(p)}")
    if (spent := actions.line(p)):
        print(f"{spent} this month (cap {actions.cap_minutes(p):,})")
    st = load_json(p.state / "state.json", {})
    for tid, slot in sorted(st.get("running", {}).items(), key=lambda kv: kv[1]) if on else ():
        print(f"slot {slot}: {tid}")
    for tid, q in st.get("questions", {}).items():
        print(f"waiting on you: {tid}: {q['q']}  ({', '.join(f'{k}: {v}' for k, v in sorted(q['opts'].items()))})")
    print("\n".join(held_text(p) + last_log(p, 12)))


def held_text(p):
    """The held branches' lines and, after them, what happens next; nothing when none waits."""
    lines = driver.held_lines(p)
    return lines + ["  (each run's start tries these again)"] * bool(lines)


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
            if cid in rates and (a := critic_mod.advice(*rates[cid], top=r == cs[-1][0]))]
    gaps = [f"Critic left {cid} unanswered in {k} of the last {n}: check its wording in {p.rubric_rel} or "
            f"prompts/CRITIC.md." for cid, k, n in critic_mod.unanswered(recs)]
    return "\n".join(filter(None, [shared_line(p, lad),
                                    f"Critic checks, last 10 tickets: {'; '.join(said)}." if said else "", *gaps]))


def shared_line(p, lad):
    """A check id two dimensions share: the critic answers by id, so one answer would score both."""
    dup = [f"{cid} ({', '.join(ds)})" for cid, ds in critic_mod.shared(lad).items()]
    return (f"Critic scale reuses check ids, so one answer scores both: {'; '.join(dup)}; rename one in "
            f"{p.rubric_rel}." if dup else "")


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
    print(q.listing(set(st.get("questions", {}))) or f"no tickets on {p.upstream} yet")
    local = sorted(t for t in (p.norm(f.stem) for f in (p.root / p.tickets_rel).glob("*.md")) if t and t not in q.files)
    if local:
        print(f"not on {p.upstream} yet (the autopilot can't see them): {' '.join(local)}; commit and push")
    for e in p.periodic.values():
        if e["after_group"]:
            print(periodic_mod.group_line(e, q))


def cmd_answer(a, p):
    letter = a.answer.upper() if re.fullmatch(r"[A-Ea-e]", a.answer) else None
    waits = load_json(p.state / "state.json", {}).get("questions", {})
    x = preflight.lookup(p, a.ticket) if p.norm(a.ticket) not in waits else None
    if x:  # a preflight question: recorded on main for the ticket's worker
        print(f"{x['ticket']}: {preflight.record(p, [(x, letter, None if letter else a.answer)])}")
        return
    tid = p.norm(a.ticket) or sys.exit("give a ticket id, or a question's id from preflight (T-003.1)")
    if tid not in waits:
        sys.exit(f"{tid}: no question of it waits for an answer (open, provisional on {p.main}, or from preflight)")
    questions.give_answer(p, tid, letter, None if letter else a.answer)
    preflight.forget(p, {tid})  # preflight lists an open phone question under its ticket id
    print(f"{tid}: answer recorded; the autopilot builds it on the ticket's next run")


def cmd_check(a, p):
    ok, summary, log = jobs.check(p, os.getcwd())
    if not ok and log:
        print(jobs.tail(log, a.lines))
    print(summary)
    sys.exit(0 if ok else 1)


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
        print(f"The critic session died ({error}); retry {len(waits)} in {wait:g} min.", flush=True)
        sleep(wait * 60)
        return True
    return retry


def cmd_critic(a, p):
    tid = p.norm(a.ticket) or sys.exit("give a ticket id")
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
    if entry is None:
        print("\nCRITIC STEP: none. Fix the pack and run it again; if the critic itself keeps failing, go on.")
        sys.exit(1)
    pr = driver.pr_for(p, gout(os.getcwd(), "branch", "--show-current"))
    if not critic_mod.comment(p, entry, pr):
        print(f"\n(couldn't post the critique on PR #{pr})")
    print("\n" + critic_mod.next_step(p, tid, entry))


def cmd_plan(a, p):
    need(p)
    goal = " ".join(a.goal) or p.cfg["goal"]
    driver.push_held(p)
    try:
        n = driver.plan(p, goal, not a.no_followups, Usage(p), None, merge=a.merge, max_new=a.max, busy_raises=True)
    except driver.PlanBusy as e:
        raise SystemExit(f"windvane: {e}; try again when it's done")
    print(f"{driver.n_tickets(n)} planned" + ("" if n else "; see the log: windvane status"))


def cmd_new(a, p):
    q = Queue(p, "HEAD")
    tid = q.next_id()
    f = p.root / p.tickets_rel / f"{tid}.md"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text((HOME / "templates/ticket.md").read_text().replace("<ID>", tid).replace("<title>", " ".join(a.title)))
    print(f)


# Relative cost of a token by kind, the API's price ratios (cache write 1.25x input, cache read 0.1x, output 5x):
# fixed weights rather than a price table, so the figure doesn't go stale when prices or models change.
WEIGHTS = {"input": 1, "cache_write": 1.25, "cache_read": 0.1, "output": 5}


def weighted(r):
    """A ledger row's tokens in fresh-input equivalents: what it costs, cache reads (most of the volume) included,
    whatever the model."""
    return sum(w * (r.get(kind) or 0) for kind, w in WEIGHTS.items())


# The runner's `killed` reasons that lose the run's spend: its time box or the run's deadline (where a hung session
# ends too), and the ticket runs again. "stop now", the caps and its own budget ("max turns") are deliberate stops.
LOST_KILLS = ("time",)


def plural(n, word):
    return f"{n} {word}{'s' * (n != 1)}"


def note(text):
    """A footnote, wrapped to the table's 100 columns."""
    print(textwrap.fill(text, 100, subsequent_indent=" "))


def cmd_stats(a, p):
    if a.critic:
        return critic_stats(a, p)
    rows = read_jsonl(p.state / "ledger.jsonl")
    since = f"{now() - timedelta(days=a.days):%Y-%m-%d %H:%M}"
    rows = [r for r in rows if r.get("at", "") >= since]
    if getattr(a, "periodic", False):
        return periodic_stats(rows, a.days)
    swept = sum(r.get("groups") or 0 for r in rows if r.get("event") == "swept")
    checks = [r for r in rows if r.get("event") in ("check", "check-start")]
    rows = [r for r in rows if not r.get("event")]  # a periodic score, an outcome, a sweep, a check: not a session
    if not rows:
        print("no runs yet")
        check_stats(checks, a.by)
        orphans(swept)
    else:
        session_stats(a, p, rows, swept, checks)


def check_runs(checks):
    """The checks that ran (a reused pass didn't): the end rows, plus a start row no end row matches (same ticket
    and start: the check was killed, so it has no minutes), counted as cut off like an end row with `finished:
    false`."""
    ends = [r for r in checks if r.get("event") == "check"]
    ended = {(r["ticket"], r.get("start")) for r in ends}
    killed = [{**r, "minutes": 0, "finished": False} for r in checks
              if r.get("event") == "check-start" and (r["ticket"], r.get("start")) not in ended]
    return [r for r in ends if not r.get("reused")] + killed, len(ends) - sum(not r.get("reused") for r in ends)


def check_stats(checks, by="role"):
    """What the check costs: its runs and minutes, per ticket that ran one, and the failed, cut off and reused ones
    (only when there are some); by ticket, the ticket whose checks took longest. Not in the table: a check spends
    time, not tokens."""
    if not checks:
        return
    ran, reused = check_runs(checks)
    mins = sum(r.get("minutes") or 0 for r in ran)
    m = lambda x: f"{x:.0f}" if x >= 10 else f"{x:.1f}"
    cut = sum(r.get("finished") is False for r in ran)
    failed = sum(not r.get("passed") for r in ran) - cut
    per = {}
    for r in ran:
        t = per.setdefault(r["ticket"], [0, 0.0])
        t[0] += 1
        t[1] += r.get("minutes") or 0
    print(f"checks: {plural(len(ran), 'run')}, {m(mins)} min" + (f" ({mins / len(per):.1f} min a ticket checked)"
          if per else "") + (f", {failed} failed" if failed else "") + (f", {cut} cut off" if cut else "")
          + (f", {reused} reused a pass" if reused else ""))
    if by == "ticket" and per:
        top = max(per, key=lambda t: (per[t][1], per[t][0]))
        print(f"most check time: {top}, {plural(per[top][0], 'run')}, {m(per[top][1])} min")


def orphans(swept):
    if swept:  # a dead driver's sessions, killed by the next run: its log says when
        print(f"orphaned process groups killed: {swept}")


def session_stats(a, p, rows, swept=0, checks=()):
    agg = {}
    key = (lambda r: r.get("ticket") or r["role"]) if a.by == "ticket" else (lambda r: r["role"])
    for r in rows:
        g = agg.setdefault(key(r), {"runs": 0, "died": 0, "killed": 0, "turns": 0, "in": 0, "out": 0, "cache": 0,
                                    "w": 0.0, "lost_w": 0.0, "usd": 0.0, "no_usd": 0, "min": 0.0, "base": 0,
                                    "based": 0, "ticket": a.by == "ticket" and bool(r.get("ticket"))})
        g["runs"] += 1
        g["turns"] += r.get("turns") or 0
        g["in"] += (r.get("input") or 0) + (r.get("cache_write") or 0)
        g["cache"] += r.get("cache_read") or 0
        g["out"] += r.get("output") or 0
        g["w"] += weighted(r)
        # died on the API or crashed, or killed for time: the ticket runs again, so it's pure loss
        lost = "died" if r.get("error") else "killed" if r.get("killed") in LOST_KILLS else None
        if lost:
            g[lost] += 1
            g["lost_w"] += weighted(r)
        g["usd"] += r.get("usd") or 0
        g["no_usd"] += r.get("usd") is None
        g["min"] += r.get("minutes") or 0
        g["base"] += r.get("base") or 0
        g["based"] += bool(r.get("base"))  # runs from before the ledger kept it don't count
    outcome = ticket_outcomes(p, agg) if a.by == "ticket" else {}
    wide = getattr(a, "wide", False)
    stats_table(a.by, agg, outcome, wide)
    if outcome:
        stats_per_merged(agg, outcome)
    check_stats(checks, a.by)
    died, killed = (sum(g[f] for g in agg.values()) for f in ("died", "killed"))
    if died or killed:
        lost, total = sum(g["lost_w"] for g in agg.values()), sum(g["w"] for g in agg.values())
        print(f"died or killed: {plural(died + killed, 'run')} ({lost_parts(died, killed)}), "
              f"{k(lost)} weighted tokens, {100 * lost / max(1, total):.0f}% of all")
    orphans(swept)
    est = sum(g["no_usd"] for g in agg.values())
    if est:
        note(f"(≥: {plural(est, 'run')} ended before {'its' if est == 1 else 'their'} result, so the API $ is "
             "missing: a floor; ?: none of the row's runs has one.)")
    note("(weighted: tokens in fresh-input equivalents, cache write 1.25, cached in 0.1, out 5: the same for every "
         "model. API $ prices them by model: a relative meter on a subscription.)")
    if wide:
        note("(base: a run's first-turn context, \"-\" for runs logged before windvane kept it; cached/turn: what "
             "each turn re-reads, base plus what the run has added.)")
    else:
        note(f"(--wide, {len(stats_header(a.by, outcome, True))} columns, adds base and cached/turn: what a run "
             "starts with and each turn re-reads.)")


def lost_parts(died, killed):
    """The lost runs' kinds, naming only those that happened: "died" alone, or "2 died, 1 killed for time" when both
    did (a "0 killed" on the usual ledger is noise)."""
    if not (died and killed):
        return "died" if died else "killed for time"
    return f"{died} died, {killed} killed for time"


def k(n):
    return f"{n / 1000:,.0f}k"


def usd(g):
    """A row's API $: a floor (≥) when some runs ended before their result, "?" when none of them has one."""
    if g["no_usd"] == g["runs"]:
        return "?"
    return ("≥" if g["no_usd"] else "") + f"{g['usd']:.2f}"


def stats_table(by, agg, outcome, wide=False):
    """One line per role or ticket, the costliest (weighted) first. 100 columns at most by default; `wide` adds the
    context columns, for tuning prompts."""
    k1 = lambda n, d: f"{n / d / 1000:,.1f}k" if d else "-"
    print(stats_header(by, outcome, wide))
    for name, g in sorted(agg.items(), key=lambda kv: -kv[1]["w"]):
        o = f" {outcome.get(name, '-'):7}" if outcome else ""
        more = f" {k1(g['base'], g['based']):>6} {k1(g['cache'], g['turns']):>11}" if wide else ""
        print(f"{name[:12]:12}{o} {g['runs']:4} {g['died'] + g['killed'] or '·':>11} {g['turns']:5} {k(g['in']):>9} "
              f"{k(g['cache']):>10} {k(g['out']):>7} {k(g['w']):>9} {usd(g):>7} {g['min']:5.0f}{more}")


def stats_header(by, outcome, wide):
    """The table's header, whose length is its width: the --wide note says it."""
    col = f" {'outcome':7}" if outcome else ""
    more = f" {'base':>6} {'cached/turn':>11}" if wide else ""
    return (f"{by:12}{col} {'runs':>4} {'died/killed':>11} {'turns':>5} {'fresh in':>9} {'cached in':>10} {'out':>7} "
            f"{'weighted':>9} {'API $':>7} {'min':>5}{more}")


def stats_per_merged(agg, outcome):
    """What a merged ticket costs on its own and with the rest spread over them: the runs with no ticket (planner,
    preflight, periodic), named, and tickets no longer in the queue (renumbered, folded, deleted), counted apart;
    and the share spent on tickets that didn't merge."""
    merged = [g for n, g in agg.items() if outcome.get(n) == "merged"]
    w = lambda gs: sum(g["w"] for g in gs)
    other = w(g for n, g in agg.items() if outcome.get(n) in ("open", "parked", "failed"))
    loose = sorted((n for n in agg if n not in outcome), key=lambda n: -agg[n]["w"])  # grouped by role: no ticket
    gone = sorted((n for n in agg if outcome.get(n) == "gone"), key=lambda n: -agg[n]["w"])
    if merged:
        m = len(merged)
        t = {f: sum(g[f] for g in merged) for f in ("runs", "usd", "no_usd")}
        print(f"per merged ticket ({m}): {t['runs'] / m:.1f} runs, {k(w(merged) / m)} weighted tokens, "
              f"{usd({**t, 'usd': t['usd'] / m})} API $")
        names = lambda ns: ", ".join(ns[:3]) + (f" and {len(ns) - 3} more" if len(ns) > 3 else "")
        parts = [f"{names(loose)}: {k(w(agg[n] for n in loose))}"] if loose else []
        parts += [f"{plural(len(gone), 'ticket')} no longer in the queue ({names(gone)}): "
                  f"{k(w(agg[n] for n in gone))}"] if gone else []
        over = w(agg[n] for n in loose + gone)
        print(f"with overhead: {k((w(merged) + over) / m)} weighted tokens per merged ticket")
        if parts:
            note(f"  overhead: {'; '.join(parts)}")
    print(f"spent on tickets not merged (open, parked, failed): {k(other)} weighted tokens, "
          f"{100 * other / max(1, other + w(merged)):.0f}% of ticket tokens")


def periodic_stats(rows, days):
    """The trend of each [[periodic]] entry's `score=<n>`: the last dozen, oldest first, and its last summary."""
    by = {}
    for r in rows:
        if r.get("event") == "score":
            by.setdefault(r["role"].split(":", 1)[-1], []).append(r)
    if not by:
        print(f"no periodic scores in {days:g} days (a session's PERIODIC line may carry score=<n>)")
        return
    for name, rs in by.items():
        s = [r["score"] for r in rs]
        trend = f"{s[-1] - s[0]:+g} since {rs[0]['at']}" if len(s) > 1 else f"first, {rs[0]['at']}"
        print(f"{name}: {' → '.join(f'{x:g}' for x in s[-12:])}  ({len(s)} run{'s' * (len(s) != 1)}, {trend})")
        print(f"  last: {rs[-1].get('summary', '')[:110]}")


def critic_stats(a, p):
    """Whether the critic's calibration spreads its scores out, over the last `--last` tickets it scored."""
    if a.last < 1:
        sys.exit("--last needs at least 1 ticket")
    recs = read_jsonl(p.state / "critic.jsonl")
    since = critic_since(p, a.since) if getattr(a, "since", None) else ""
    recs = [r for r in recs if r.get("at", "") >= since]
    c = critic_mod.calibration(recs, p.critic["pass"], a.last)
    if not c:
        print(f"no critic verdicts since {since}" if since else "no critic verdicts yet")
        return
    n, g = len(c["tickets"]), lambda x: f"{x:g}"
    print(f"critic: last {plural(n, 'ticket')}, {plural(c['verdicts'], 'verdict')}"
          + (f" since {since}" if since else "") + f", pass mark {g(c['bar'])}/10")
    print(f"first scores: {critic_mod.spread(c['first'])}")
    print(f"final scores: {critic_mod.spread(c['final'])}")
    print(f"needed revisions: {c['revised']} of {n}")
    print(f"exactly on the pass mark: {c['final_on_bar']} of {n} final scores, {c['on_bar']} of {c['verdicts']} verdicts")
    print(f"passed only by running out of revisions (accept): {c['accepted']} of {n}")
    lad = critic_mod.rubric(p, p.root)
    if not lad:
        print(f"cap checks answered: {c['checked']} of {c['verdicts']} verdicts; a hit cap lowered the score in "
              f"{c['capped']}")
    for t, rs in c["tickets"]:
        print(f"  {t:8} {' → '.join(g(r['score']) for r in rs):14} {rs[-1].get('step', '')}")
    if line := shared_line(p, lad):
        print(line)
    rates = critic_mod.rates(recs, a.last)
    if lad and rates:
        print("checks, yes on the first head / on any head, of the tickets that touched them:")
        for d, cs in lad.items():
            for r, cid, _ in cs:
                if cid in rates:
                    first, ever, k = rates[cid]
                    print(f"  {d + ' ' + g(r):9} {cid:10} {first:>2}/{k:<2} {ever:>2}/{k:<2} "
                          f"{critic_mod.advice(first, ever, k, top=r == cs[-1][0])}".rstrip())


def critic_since(p, when):
    """`--since` as the log's "YYYY-MM-DD HH:MM": a date as is, a commit (say the one that changed CALIBRATION.md)
    as its author time in local time, like the verdicts' own `at`: a rebase moves the commit time later, past
    verdicts given after the change was written."""
    if re.fullmatch(r"\d{4}-\d\d-\d\d", when):
        return when
    at = gout(p.root, "log", "-1", "--format=%ad", "--date=format-local:%Y-%m-%d %H:%M", f"{when}^{{commit}}", "--")
    return at or sys.exit(f"--since: {when!r} is neither a date (YYYY-MM-DD) nor a commit")


def ticket_outcomes(p, agg):
    """merged / parked / failed / open for each ticket in `agg`, from the queue's git-derived status and the ledger,
    "gone" for a ticket no longer in the queue (runs that belong to no ticket, like the planner's, get none)."""
    try:
        q = Queue(p)
    except Exception:  # no remote yet: no outcomes rather than no stats
        return {}
    names, failed = {"done": "merged", "parked": "parked"}, failed_tickets(p)
    return {t: names.get(q.status(t), "failed" if t in failed else "open") if t in q.files else "gone"
            for t, g in agg.items() if g["ticket"]}


def failed_tickets(p):
    """Tickets whose last attempt failed: the ledger's `outcome: failed` row (`Pilot.fail` writes it) unless a later
    worker run retried the ticket. Tickets with no such row, failed before the ledger kept it, by the log's
    `<ID>: FAILED: ...` line, the same way."""
    last, recorded = {}, set()
    for r in read_jsonl(p.state / "ledger.jsonl"):  # in the order things happened, whatever --days says
        t = r.get("ticket")
        if r.get("event") == "outcome" and t:
            last[t] = r.get("outcome")
            recorded.add(t)
        elif t and r.get("role") == "worker" and not r.get("event"):
            last[t] = "run"
    failed = {t for t in recorded if last[t] == "failed"}
    old = {}
    for line in last_log(p, None):
        if (m := re.match(r"\S+ \S+ (\S+): (FAILED:|worker run)", line)) and m[1] not in recorded:
            old[m[1]] = m[2]
    return failed | {t for t, what in old.items() if what == "FAILED:"}


def cmd_mode(a, p):
    print(questions.mode(p))


def cmd_quiet(a, p):
    f = p.state / "quiet.txt"
    print(f"{f} (`daily 23:00-08:00` or `2026-10-02 13:00-15:00`, one per line)")
    print(f.read_text() if f.exists() else "(none)")
    print("now:", questions.mode(p))


def cmd_periodic(a, p):
    """A [[periodic]] entry and its `then` chain now, from the desk, as a run would between tickets. It takes the
    run's lock, so it never runs beside one (nor one beside it)."""
    need(p)
    e = p.periodic.get(a.name)
    if not e:
        sys.exit(f"no [[periodic]] entry {a.name!r} in {DIR}/config.toml"
                 + (f" (there is: {', '.join(p.periodic)})" if p.periodic else ""))
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


def main(argv=None):
    ap = argparse.ArgumentParser(prog="windvane", description=__doc__)
    ap.add_argument("-C", dest="cwd", help="run as if started in this directory")
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
    sub.add_parser("doctor", help="what this machine lacks for windvane, and how to fix it")
    s = sub.add_parser("phone", help="pushes and questions on your phone (ntfy); off until `phone setup`")
    s.add_argument("action", choices=("setup", "off", "rotate", "listen"),
                   help="setup: turn it on and subscribe; off; rotate: new topics; listen: act on the phone's replies")
    s.add_argument("--server", help=f"an ntfy server of your own (default {phone.SERVER})")
    s.add_argument("--no-service", action="store_true", help="setup: don't install the background listener service")
    s = sub.add_parser("run", help="work the tickets until a deadline")
    g = s.add_mutually_exclusive_group()
    g.add_argument("--until", help="HH:MM or 'YYYY-MM-DD HH:MM'")
    g.add_argument("--for", dest="for_", help="e.g. 90m or 6h")
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
    sub.add_parser("overview", help="where the project stands (for the phone)")
    sub.add_parser("queue", help="the ticket queue")
    s = sub.add_parser("answer", help="answer a ticket's question at the desk")
    s.add_argument("ticket")
    s.add_argument("answer", help="a letter, or free text in quotes")
    s = sub.add_parser("check", help="the project's check, reusing a pass for the same tree")
    s.add_argument("--lines", type=int, default=60)
    s = sub.add_parser("ci", help="start a GitHub Actions workflow, only within the month's minutes cap")
    s.add_argument("workflow", nargs="?", default="", help="its file or name (default: the repo's only one)")
    s.add_argument("--ref", default="", help="the branch to run it on (default: the current one)")
    s.add_argument("--dry-run", action="store_true", help="count and say, but start nothing")
    s = sub.add_parser("critic", help="have the critic judge a ticket's evidence pack")
    s.add_argument("ticket")
    s = sub.add_parser("bg", help="start a long command in the background")
    s.add_argument("--heavy", action="store_true", help="run it under the machine-wide heavy lock")
    s.add_argument("name")
    s.add_argument("command", nargs=argparse.REMAINDER)
    s = sub.add_parser("heavy", help="run a machine-heavy command (engine, render) one at a time on this machine")
    s.add_argument("--wait", type=float, default=60, help="minutes to wait for the machine before giving up")
    s.add_argument("command", nargs=argparse.REMAINDER)
    s = sub.add_parser("await", help="wait (up to ~9.5 min) for a bg command")
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
    sub.add_parser("quiet", help="show the quiet times")
    s = sub.add_parser("watch", help="follow the running sessions live (costs no tokens)")
    s.add_argument("-n", type=int, default=30, help="start with this many past events")
    s.add_argument("-o", type=int, default=6, help="lines of each command's output (0 hides output)")
    a = ap.parse_args(argv)
    if a.cwd:
        os.chdir(a.cwd)
    if a.cmd == "init":
        return cmd_init(a)
    if a.cmd in ("install", "uninstall"):
        return getattr(install, a.cmd)(a.yes)
    if a.cmd == "doctor":
        root = toplevel()
        return doctor.main(Project(root) if root else None)
    if a.cmd == "phone":
        root = toplevel()
        return phone.main(a, Project(root) if root and (root / DIR / "config.toml").is_file() else None)
    if a.cmd == "heavy":
        from .heavy import run as heavy_run
        cmd = a.command[1:] if a.command[:1] == ["--"] else a.command
        sys.exit(heavy_run(cmd, a.wait * 60) if cmd else "heavy: give a command")
    p = find()
    phone.use(p)
    if a.cmd == "run":
        for k in ("cap", "week_cap", "ticket_hours", "min_slot", "answer_wait"):
            if getattr(a, k) is None:
                setattr(a, k, p.ap[k])
    if a.cmd == "bg":
        cmd = a.command[1:] if a.command[:1] == ["--"] else a.command
        return jobs.bg(p, a.name, cmd, os.getcwd(), a.heavy) if cmd else sys.exit("bg: give a command")
    if a.cmd == "await":
        return jobs.await_(p, a.name, a.lines)
    if a.cmd == "watch":
        return watch.main(p, a.n, a.o)
    return globals()[f"cmd_{a.cmd}"](a, p)
