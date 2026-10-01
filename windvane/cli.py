"""windvane: run Claude Code on a project's tickets unattended, with a critic, a reviewer, phone questions and a token
ledger. `windvane <command> --help` for each command."""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

from . import critic as critic_mod
from . import driver, jobs, preflight, questions, watch
from .config import DIR, HOME, Project, find
from .queue import Queue
from .runner import Usage, inherited_stop
from .util import git, gout, hhmm, load_json, now, read_jsonl


def parse_deadline(until, for_):
    if for_:
        m = re.fullmatch(r"(\d+(?:\.\d+)?)([hm])", for_)
        if not m:
            sys.exit("--for takes e.g. 90m or 6h")
        return now() + timedelta(**{"hours" if m.group(2) == "h" else "minutes": float(m.group(1))})
    for fmt in ("%Y-%m-%d %H:%M",):
        try:
            return datetime.strptime(until, fmt)
        except ValueError:
            pass
    try:
        t = datetime.strptime(until, "%H:%M")
    except ValueError:
        sys.exit("--until takes HH:MM or 'YYYY-MM-DD HH:MM'")
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
    f = p.state / "autopilot.log"
    return f.read_text().splitlines()[-n:] if f.exists() else []


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
        if a.forge:
            text = text.replace('forge = "github"', f'forge = "{a.forge}"')
        cfg.write_text(text)
    for name in ("QUEUE.md", "QUESTIONS.md", "CRITIC.md"):
        if not (d / name).exists():
            (d / name).write_text((HOME / "templates" / name).read_text())
    p = Project(root)
    hooks = install_hooks(p)
    print(f"windvane set up in {d}\n  project name: {p.name}; state on this machine: {p.state}\n"
          f"  edit {DIR}/config.toml (the check command!) and {DIR}/CRITIC.md (what a 10 means here)\n"
          f"  then commit {DIR}/ and push it to {p.upstream}: the autopilot reads tickets from there")
    if hooks:
        print(f"  phone: status/stop/overview hooks in {', '.join(hooks)}")


def install_hooks(p):
    base = Path.home() / ".config/ntfy"
    if not base.exists():
        return []
    made = []
    exe = HOME / "bin/windvane"
    for kind, args in (("status.d", "status --line"), ("stop.d", "stop --if-running"), ("overview.d", "overview")):
        d = base / kind
        d.mkdir(exist_ok=True)
        f = d / f"windvane-{p.name}"
        f.write_text(f"#!/usr/bin/env bash\n# windvane {p.name}: `{args.split()[0]}` from the phone (ntfy-listen)\n"
                     f"exec {exe} -C {p.main_root()} {args}\n")
        f.chmod(0o755)
        made.append(str(d))
    return made


def cmd_install(a):
    target = Path.home() / ".local/bin/windvane"
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_symlink() or target.exists():
        target.unlink()
    target.symlink_to(HOME / "bin/windvane")
    print(f"{target} -> {HOME / 'bin/windvane'}")
    claude_dir = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
    skill = claude_dir / "skills/windvane"
    skill.parent.mkdir(parents=True, exist_ok=True)
    if skill.is_symlink() or not skill.exists():
        skill.unlink(missing_ok=True)
        skill.symlink_to(HOME / "skills/windvane")
        print(f"{skill} -> {HOME / 'skills/windvane'} (the playbook new sessions load)")
    if add_keyword_hook(claude_dir / "settings.json"):
        print("~/.claude/settings.json: a hook that brings windvane in whenever a prompt mentions it or autopilot")


def add_keyword_hook(settings):
    """Add the UserPromptSubmit keyword hook to Claude Code's user settings, once, keeping everything else."""
    cmd = str(HOME / "hooks/mention_hook.py")
    try:
        data = json.loads(settings.read_text()) if settings.exists() else {}
    except ValueError:
        print(f"{settings} isn't valid JSON: add the hook yourself (UserPromptSubmit -> {cmd})")
        return False
    groups = data.setdefault("hooks", {}).setdefault("UserPromptSubmit", [])
    if any(h.get("command") == cmd for g in groups for h in g.get("hooks", [])):
        return False
    groups.append({"hooks": [{"type": "command", "command": cmd, "timeout": 5}]})
    tmp = settings.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    tmp.replace(settings)
    return True


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
        cmd = " ".join(json.dumps(x) for x in [str(HOME / "bin/windvane"), "-C", str(p.main_root()), *args])
        subprocess.run(["tmux", "new-session", "-d", "-s", p.tmux, "-c", str(p.main_root()), cmd], check=True)
        print(f"autopilot running in tmux session {p.tmux} until {hhmm(deadline)}\n"
              f"  watch:  windvane watch      (or tmux attach -t {p.tmux}; leave with Ctrl-b d)\n"
              f"  status: windvane status     stop: windvane stop")
        return
    driver.Pilot(p, a).run(deadline)


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
        print(f"{p.name}: autopilot isn't running")
        return
    p.state.mkdir(parents=True, exist_ok=True)
    (p.state / "STOP").touch()
    print(f"{p.name}: autopilot stops after the current ticket")


def cmd_status(a, p):
    on = running(p)
    if a.line:
        last = (last_log(p) or [""])[0][11:]
        print(f"{p.name}: autopilot {'running' if on else 'off'}" + (f". Last: {last}" if last else ""))
        return
    print(f"autopilot: {('running in tmux ' + p.tmux if in_tmux(p) else 'running') if on else 'not running'}")
    print(f"questions now: {questions.mode(p)}")
    st = load_json(p.state / "state.json", {})
    for tid, q in st.get("questions", {}).items():
        print(f"waiting on you: {tid}: {q['q']}  ({', '.join(f'{k}: {v}' for k, v in sorted(q['opts'].items()))})")
    print("\n".join(last_log(p, 12)))


def cmd_overview(a, p):
    git(p.main_root(), "fetch", "-q", p.remote)
    q = Queue(p)
    st = [q.status(t) for t in q.order]
    done = st.count("done")
    lines = [f"{p.name}: {done} of {len(st)} tickets done; {st.count('wip')} in progress, {st.count('todo')} to do."]
    lines.append(f"Autopilot {'on' if running(p) else 'off'}.")
    work = next((m.group(1) for l in reversed(last_log(p, 200)) if (m := re.search(r" (\S+): worker run", l))), None)
    if running(p) and work:
        lines[-1] = f"Autopilot on, working on {work}: {q.titles.get(work, '')}."
    scores = [e["score"] for e in read_jsonl(p.state / "critic.jsonl") if e.get("score") is not None][-10:]
    if scores:
        lines.append(f"Critic: {sum(scores) / len(scores):.1f}/10 over the last {len(scores)} verdicts.")
    print("\n".join(lines))


def cmd_queue(a, p):
    git(p.main_root(), "fetch", "-q", p.remote)
    st = load_json(p.state / "state.json", {})
    print(Queue(p).listing(set(st.get("questions", {}))) or "no tickets yet")


def cmd_answer(a, p):
    letter = a.answer.upper() if re.fullmatch(r"[A-Ea-e]", a.answer) else None
    waits = load_json(p.state / "state.json", {}).get("questions", {})
    x = preflight.lookup(p, a.ticket) if p.norm(a.ticket) not in waits else None
    if x:  # a preflight question: recorded on main for the ticket's worker
        print(f"{x['ticket']}: {preflight.record(p, [(x, letter, None if letter else a.answer)])}")
        return
    tid = p.norm(a.ticket) or sys.exit("give a ticket id, or a preflight question's id (T-003.1)")
    questions.give_answer(p, tid, letter, None if letter else a.answer)
    print(f"{tid}: answer recorded; the autopilot builds it on the ticket's next run")


def cmd_check(a, p):
    ok, summary, log = jobs.check(p, os.getcwd())
    if not ok and log:
        print(jobs.tail(log, a.lines))
    print(summary)
    sys.exit(0 if ok else 1)


def cmd_critic(a, p):
    tid = p.norm(a.ticket) or sys.exit("give a ticket id")
    if not p.critic["enabled"]:
        print("CRITIC STEP: skip. The critic is off for this project.")
        return
    usage = Usage(p)
    entry, text = critic_mod.judge(p, tid, os.getcwd(), usage=usage, stop_if=inherited_stop(p, usage))
    print(text)
    if entry is None:
        print("\nCRITIC STEP: none. Fix the pack and run it again; if the critic itself keeps failing, go on.")
        sys.exit(1)
    print("\n" + critic_mod.next_step(p, tid, entry))


def cmd_plan(a, p):
    need(p)
    goal = " ".join(a.goal) or p.cfg["goal"]
    n = driver.plan(p, goal, not a.no_followups, Usage(p), None, merge=a.merge, max_new=a.max)
    print(f"{n} tickets planned" + ("" if n else "; see the log: windvane status"))


def cmd_new(a, p):
    q = Queue(p, "HEAD")
    tid = q.next_id()
    f = p.root / p.tickets_rel / f"{tid}.md"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text((HOME / "templates/ticket.md").read_text().replace("<ID>", tid).replace("<title>", " ".join(a.title)))
    print(f)


def cmd_stats(a, p):
    rows = read_jsonl(p.state / "ledger.jsonl")
    since = f"{now() - timedelta(days=a.days):%Y-%m-%d %H:%M}"
    rows = [r for r in rows if r.get("at", "") >= since]
    if not rows:
        print("no runs yet")
        return
    agg = {}
    key = (lambda r: r.get("ticket") or r["role"]) if a.by == "ticket" else (lambda r: r["role"])
    for r in rows:
        g = agg.setdefault(key(r), {"runs": 0, "turns": 0, "in": 0, "out": 0, "cache": 0, "usd": 0.0, "min": 0.0, "est": 0})
        g["runs"] += 1
        g["turns"] += r.get("turns") or 0
        g["in"] += (r.get("input") or 0) + (r.get("cache_write") or 0)
        g["cache"] += r.get("cache_read") or 0
        g["out"] += r.get("output") or 0
        g["usd"] += r.get("usd") or 0
        g["min"] += r.get("minutes") or 0
        g["est"] += bool(r.get("estimated"))
    k = lambda n: f"{n / 1000:,.0f}k"
    usd = lambda v, est: ("≥" if est else "") + f"{v:.2f}"  # a run killed before its result has no $: a floor
    outcome = ticket_outcomes(p, agg) if a.by == "ticket" else {}
    col = f" {'outcome':7}" if outcome else ""
    print(f"{a.by:12}{col} {'runs':>4} {'turns':>5} {'fresh in':>9} {'cached in':>10} {'out':>7} {'API $':>7} {'min':>5}")
    for name, g in sorted(agg.items(), key=lambda kv: -kv[1]["usd"]):
        o = f" {outcome.get(name, '-'):7}" if outcome else ""
        print(f"{name[:12]:12}{o} {g['runs']:4} {g['turns']:5} {k(g['in']):>9} {k(g['cache']):>10} {k(g['out']):>7} "
              f"{usd(g['usd'], g['est']):>7} {g['min']:5.0f}")
    if outcome:
        merged = [g for n, g in agg.items() if outcome.get(n) == "merged"]
        tok = lambda gs: sum(g["in"] + g["out"] for g in gs)
        other = tok(g for n, g in agg.items() if outcome.get(n) in ("open", "parked"))
        if merged:
            m = len(merged)
            print(f"per merged ticket ({m}): {sum(g['runs'] for g in merged) / m:.1f} runs, {k(tok(merged) / m)} tokens, "
                  f"{usd(sum(g['usd'] for g in merged) / m, sum(g['est'] for g in merged))} API $")
        print(f"spent on tickets not merged: {k(other)} tokens, {100 * other / max(1, other + tok(merged)):.0f}% "
              f"of ticket tokens (fresh in + out)")
    est = sum(g["est"] for g in agg.values())
    if est:
        print(f"(≥: {est} runs ended before their result; their tokens are counted from the stream, their API $ is not.)")
    print("(API $ is what the tokens would cost on the API: a relative meter on a subscription.)")


def ticket_outcomes(p, agg):
    """merged / parked / open for each ticket in `agg`, from the queue's git-derived status ("-" for runs that
    belong to no ticket, like the planner's)."""
    try:
        q = Queue(p)
    except Exception:  # no remote yet: no outcomes rather than no stats
        return {}
    names = {"done": "merged", "parked": "parked"}
    return {t: names.get(q.status(t), "open") for t in agg if t in q.files}


def cmd_mode(a, p):
    print(questions.mode(p))


def cmd_quiet(a, p):
    f = p.state / "quiet.txt"
    print(f"{f} (`daily 23:00-08:00` or `2026-10-02 13:00-15:00`, one per line)")
    print(f.read_text() if f.exists() else "(none)")
    print("now:", questions.mode(p))


def main(argv=None):
    ap = argparse.ArgumentParser(prog="windvane", description=__doc__)
    ap.add_argument("-C", dest="cwd", help="run as if started in this directory")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("init", help="set windvane up in a repository")
    s.add_argument("dir", nargs="?")
    s.add_argument("--name")
    s.add_argument("--check", help="the project's CI command")
    s.add_argument("--forge", choices=("github", "git"))
    sub.add_parser("install", help="put windvane on PATH (~/.local/bin)")
    s = sub.add_parser("run", help="work the tickets until a deadline")
    g = s.add_mutually_exclusive_group()
    g.add_argument("--until", help="HH:MM or 'YYYY-MM-DD HH:MM'")
    g.add_argument("--for", dest="for_", help="e.g. 90m or 6h")
    s.add_argument("--detach", action="store_true", help="run in a tmux session")
    s.add_argument("--no-preflight", action="store_true", help="start without asking the next tickets' questions")
    s.add_argument("--parallel", type=int, help="tickets at a time (default from config, 2)")
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
    s = sub.add_parser("status", help="running or not, open questions, the log's tail")
    s.add_argument("--line", action="store_true", help="one line (for the phone)")
    sub.add_parser("overview", help="where the project stands (for the phone)")
    sub.add_parser("queue", help="the ticket queue")
    s = sub.add_parser("answer", help="answer a ticket's question at the desk")
    s.add_argument("ticket")
    s.add_argument("answer", help="a letter, or free text in quotes")
    s = sub.add_parser("check", help="the project's check, reusing a pass for the same tree")
    s.add_argument("--lines", type=int, default=60)
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
    s = sub.add_parser("new", help="write a new ticket file with the next id")
    s.add_argument("title", nargs="+")
    s = sub.add_parser("stats", help="tokens per role or ticket, from the ledger")
    s.add_argument("--days", type=float, default=7)
    s.add_argument("--by", choices=("role", "ticket"), default="role")
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
    if a.cmd == "install":
        return cmd_install(a)
    if a.cmd == "heavy":
        from .heavy import run as heavy_run
        cmd = a.command[1:] if a.command[:1] == ["--"] else a.command
        sys.exit(heavy_run(cmd, a.wait * 60) if cmd else "heavy: give a command")
    p = find()
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
