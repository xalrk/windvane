"""`windvane stats`: what the runs cost, from the ledger (tokens per role or ticket, checks, the gh guard's refusals,
periodic scores), how the critic's scores spread (`--critic`), and every project on the machine side by side
(`--projects`)."""
import json
import re
import statistics
import sys
import textwrap
import time
from collections import Counter
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

from . import critic as critic_mod
from . import jobs, phone
from .config import DEFAULTS, Project, user_dir
from .queue import Queue
from .util import PARTIAL_LOG, gout, last_log, log_has, log_paths, now, read_jsonl, shell_path


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


# A worker launch that ended PARTIAL or BLOCKED within this many turns did nothing: launches that found themselves
# past their wrap-up and stopped took 3 to 8 turns, while PARTIAL runs that built something took 10 or more.
IDLE_TURNS = 8


def plural(n, word):
    return f"{n} {word}{'s' * (n != 1)}"


def note(text, indent=" "):
    """A footnote, wrapped to the table's 100 columns."""
    print(textwrap.fill(text, 100, subsequent_indent=indent))


def cmd_stats(a, p):
    if a.critic:
        return critic_stats(a, p)
    if getattr(a, "bench", False):
        return bench_stats(p)
    rows = read_jsonl(p.state / "ledger.jsonl")
    since = f"{now() - timedelta(days=a.days):%Y-%m-%d %H:%M}"
    rows = [r for r in rows if r.get("at", "") >= since]
    if getattr(a, "periodic", False):
        return periodic_stats(rows, a.days)
    swept = sum(r.get("groups") or 0 for r in rows if r.get("event") == "swept")
    checks = [r for r in rows if r.get("event") in ("check", "check-start")]
    refusals = [r for r in rows if r.get("event") == "gh-refused"]
    awaits = [r for r in rows if r.get("event") == "await"]
    rows = [r for r in rows if not r.get("event")]  # a periodic score, an outcome, a sweep, a check: not a session
    if not rows:
        print("no runs yet")
        check_stats(checks, a.by, p)
        wait_stats(checks, awaits)
        orphans(swept)
        guard_stats(refusals, a.by)
    else:
        session_stats(a, p, rows, swept, checks, refusals, awaits)


def bench_stats(p):
    """`windvane bench`'s recorded runs, a line each, oldest first: a drift in what the projects built get or cost
    shows without a compare. A contaminated seed's result isn't counted, as in the run's own table."""
    runs = {}
    for r in read_jsonl(p.state / "bench.jsonl"):
        runs.setdefault(r.get("run"), []).append(r)
    if not runs:
        return print("no bench runs yet (windvane bench runs one)")
    print(f"{'run':<17} {'ref':<22} {'size':<5} {'seeds':>5} {'hidden':>7} {'judge':>6} {'tokens/merged':>13}")
    for run, recs in runs.items():
        good = [r for r in recs if not r.get("contaminated")]
        merged = sum(len(r["tickets"]["merged"]) for r in good)
        judged = [r["judge"] for r in good if r.get("judge")]
        failed = [r["seed"] for r in good if r.get("judge") and (r["judge"].get("error") or r["judge"].get("stopped"))]
        # a failed judge's unanswered checks aren't the project's noes, and a sum without it reads as the run's
        judge = f"{sum(j['yes'] for j in judged)}/{sum(j['total'] for j in judged)}" if judged and not failed else "-"
        ref = f"{recs[0]['ref']} ({recs[0]['sha'][:7]})"
        hidden = f"{sum(r['passed'] for r in good)}/{sum(r['total'] for r in good)}"
        print(f"{run:<17} {ref[:22]:<22} {recs[0]['size']:<5} {len(good):>5} {hidden:>7} {judge:>6} "
              f"{k(sum(r['weighted'] for r in good) / merged) if merged else '-':>13}"
              + (f"  ({len(recs) - len(good)} contaminated)" if len(good) < len(recs) else "")
              + (f"  (judge failed: {', '.join(failed)})" if failed else ""))


def check_runs(p, checks, running=jobs.running):
    """(the checks that ran, reused passes, checks still running, heavy checks that never ran: their wait for the
    machine timed out, which is no failure, and those cancelled while they waited in the heavy queue). Ran: the end
    rows that didn't reuse a pass or wait in vain, plus a start row no end row matches (same ticket and start) whose
    check isn't running any more: it was killed, and counts as cut off like an end row with `finished: false`, timed
    from its start to its log's last write (the check's heartbeat keeps that within a minute of the kill; 0 with no
    log). A killed check whose log holds only the queue's lines never ran: cancelled, not cut off."""
    ends = [r for r in checks if r.get("event") == "check"]
    ended = {(r["ticket"], r.get("start")) for r in ends}
    open_ = [r for r in checks if r.get("event") == "check-start" and (r["ticket"], r.get("start")) not in ended]
    live = [r for r in open_ if running(p, r)]
    killed = [{**r, **killed_times(p, r), "finished": False} for r in open_ if r not in live]
    done = [r for r in ends + killed if not r.get("reused") and not r.get("not_run")]
    ran = [r for r in done if not r.get("cancelled_queued")]
    not_run = [r for r in ends if r.get("not_run")]
    return ran, sum(bool(r.get("reused")) for r in ends), len(live), not_run, [r for r in done if r not in ran]


def killed_times(p, start_row):
    """A killed check's minutes and wait (jobs.killed_times): the check wrote its log up to the kill."""
    try:
        return jobs.killed_times(p.state / "checks" / start_row["log"], start_row["start"])
    except (KeyError, OSError):  # no log: a row from before logs were named, or the log was cleared by hand
        return {"minutes": 0}


def waited_min(r):
    """A check row's minutes in the heavy queue: its `waited_s`, or all its minutes for a not-run row from before
    rows kept it."""
    return (r.get("waited_s") or (60 * (r.get("minutes") or 0) if r.get("not_run") else 0)) / 60


def run_min(r):
    """A check row's minutes at work: its minutes less its wait for the machine."""
    return max(0.0, (r.get("minutes") or 0) - waited_min(r))


def queue_min(ran, not_run, cancelled):
    """All the checks' minutes in the heavy queue: the ran ones' waits, and all of the never-run ones'."""
    return sum(waited_min(r) for r in ran + not_run + cancelled)


def check_stats(checks, by, p):
    """What the check costs: its runs and minutes, per ticket that ran one, and the failed, cut off, reused and still
    running ones (only when there are some); the longest check that bought nothing (failed or cut off), and by ticket,
    the ticket whose checks took longest. Not in the table: a check spends time, not tokens."""
    if not checks:
        return
    ran, reused, live, not_run, cancelled = check_runs(p, checks)
    mins, waited = sum(run_min(r) for r in ran), queue_min(ran, not_run, cancelled)
    cut = sum(r.get("finished") is False for r in ran)
    failed = sum(not r.get("passed") for r in ran) - cut
    per = {}
    for r in ran:
        t = per.setdefault(r["ticket"], [0, 0.0])
        t[0] += 1
        t[1] += run_min(r)
    print(f"checks: {plural(len(ran), 'run')}, {m(mins)} min" + (" running" if waited else "")
          + (f" ({mins / len(per):.1f} min a ticket checked)" if per else "")
          + (f", {m(waited)} min waiting for the machine" if waited else "")
          + (f", {failed} failed" if failed else "") + (f", {cut} cut off" if cut else "")
          + (f", {len(cancelled)} cancelled in the queue" if cancelled else "")
          + (f", {reused} reused a pass" if reused else "") + (f", {live} still running" if live else ""))
    if not_run:  # the gate (or the session) ran each again rather than bounce the ticket: its wait is under "waits"
        print(f"not-run checks: {len(not_run)} (no bounce)")
    if by == "ticket" and per:
        top = max(per, key=lambda t: (per[t][1], per[t][0]))
        print(f"most check time: {top}, {plural(per[top][0], 'run')}, {m(per[top][1])} min")
    wasted = [r for r in ran if not r.get("passed") and run_min(r) > 0]
    if wasted:
        w = max(wasted, key=run_min)
        print(f"costliest wasted check: {w['ticket']}, {m(run_min(w))} min, "
              + ("cut off" if w.get("finished") is False else "failed"))


def m(x):
    """Minutes, to a tenth below 10."""
    return f"{x:.0f}" if x >= 10 else f"{x:.1f}"


def wait_stats(checks, awaits):
    """The time each role spent blocked rather than working: in `windvane await` (the ledger's `await` rows) and
    waiting for the heavy lock (a heavy check's `waited_s`; a not-run check from before rows kept it waited all its
    minutes). A check row with no role is the gate's. No line for a role that never waited."""
    per = {}
    for r in awaits:
        per.setdefault(r.get("role") or "?", [0, 0])[0] += r.get("seconds") or 0
    for r in checks:
        if r.get("event") == "check":
            s = 60 * waited_min(r)
            per.setdefault(r.get("role") or "driver", [0, 0])[1] += s
    for role, (aw, hv) in sorted(per.items(), key=lambda kv: -sum(kv[1])):
        parts = [f"{m(aw / 60)} min in await"] * bool(aw) + [f"{m(hv / 60)} min for the heavy lock"] * bool(hv)
        if parts:
            print(f"waits, {role}: {', '.join(parts)}")


def orphans(swept):
    if swept:  # a dead driver's sessions, killed by the next run: its log says when
        print(f"orphaned process groups killed: {swept}")


# What the gh guard refused, by its row's kind; a row from before kinds were logged was a workflow start.
GUARD_KINDS = {"workflow": ("workflow start", ""), "alias": ("alias change", ""),
               "unread": ("unknown command", "alias list unread")}


def guard_stats(refusals, by):
    """How often workers tried to start a workflow past `windvane ci` (the gh guard's `gh-refused` rows), each kind
    of refusal by its own name, and by ticket the one that tried most: a ticket that keeps trying wants a workflow
    run its prompt doesn't give it."""
    if not refusals:
        return
    top = ""
    if by == "ticket":
        name, n = Counter(r.get("ticket") or "no ticket" for r in refusals).most_common(1)[0]
        top = f"most: {name}, {n}"
    kinds = Counter(r.get("kind") if r.get("kind") in GUARD_KINDS else "workflow" for r in refusals)
    shown = [(plural(kinds[k], word), why) for k, (word, why) in GUARD_KINDS.items() if kinds[k]]
    parts = [s + (f" ({why})" if why else "") for s, why in shown[:-1]] + [shown[-1][0]]
    tail = [x for x in (shown[-1][1], top) if x]  # the last kind's note and the top ticket share one bracket
    print(f"gh guard refused: {', '.join(parts)}" + (f" ({'; '.join(tail)})" if tail else ""))


def session_stats(a, p, rows, swept=0, checks=(), refusals=(), awaits=()):
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
    check_stats(checks, a.by, p)
    wait_stats(checks, awaits)
    died, killed = (sum(g[f] for g in agg.values()) for f in ("died", "killed"))
    if died or killed:
        lost, total = sum(g["lost_w"] for g in agg.values()), sum(g["w"] for g in agg.values())
        print(f"died or killed: {plural(died + killed, 'run')} ({lost_parts(died, killed)}), "
              f"{k(lost)} weighted tokens, {100 * lost / max(1, total):.0f}% of all")
    idle_stats(rows, p)
    orphans(swept)
    guard_stats(refusals, a.by)
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
        note(f"(--wide, {len(stats_header(a.by, outcome, True, name_width(a.by, agg, outcome)))} columns, adds base and cached/turn: what a run "
             "starts with and each turn re-reads.)")


def idle_stats(rows, p):
    """Worker launches that ended PARTIAL within IDLE_TURNS: each paid a worker's start-up tokens for no work. A
    quick BLOCKED is a worker asking a product question, its job, so it's only counted apart. The ticket with the
    most gets a command that says why they stopped (why_partial), on its own line so it never wraps. Rows from
    before the ledger kept `result` don't count. No line when there were none."""
    workers = [r for r in rows if r.get("role") == "worker"]
    quick = [r for r in workers if (r.get("turns") or 0) <= IDLE_TURNS]
    idle = [r for r in quick if r.get("result") == "PARTIAL"]
    if not idle:
        return
    asked = sum(r.get("result") == "BLOCKED" for r in quick)
    fresh = sum((r.get("input") or 0) + (r.get("cache_write") or 0) for r in idle)
    share = 100 * sum(map(weighted, idle)) / max(1, sum(map(weighted, workers)))
    top = Counter(r.get("ticket") or "no ticket" for r in idle).most_common()
    names = ", ".join(f"{t} ×{n}" if n > 1 else t for t, n in top[:3])
    names += f" and {len(top) - 3} more" if len(top) > 3 else ""
    names += f". Not counted: {plural(asked, 'quick question')} (BLOCKED)" if asked else ""
    note(f"idle worker launches: {len(idle)} (PARTIAL within {IDLE_TURNS} turns), {k(fresh)} fresh in, "
         f"{k(sum(r.get('output') or 0 for r in idle))} out, {share:.0f}% of worker weighted tokens; most: {names}")
    if top[0][0] != "no ticket":
        print(f" why: {why_partial(p, top[0][0], idle)}")


def why_partial(p, tid, idle):
    """Why `tid`'s quick partials stopped: the reason its newest idle row kept from its RESULT line. Rows from before
    the ledger kept it get where the reason still is, as a command: the logs' grep only when a log holds such a line
    (with `--days` reaching rows older than the logs, it would print nothing), else the RESULT line of its latest
    idle run's transcript (`runs/`, pruned after some days), else the commits its branch adds. With none of them left,
    says so rather than naming a command that finds nothing."""
    if (said := next((r["reason"] for r in reversed(idle) if r.get("ticket") == tid and r.get("reason")), None)):
        return f"{tid}'s last idle run said: {said}"
    (oldf, logf), needle = log_paths(p), PARTIAL_LOG.format(tid=tid)
    if (hits := [shell_path(f) for f in (oldf, logf) if log_has(f, needle)]):
        return f'grep {"-h " if len(hits) > 1 else ""}"{needle}" {" ".join(hits)}'
    gone = f"the log no longer has {tid}'s partials" if logf.exists() else f"no log at {shell_path(logf)}"
    runs = [p.state / "runs" / r["stream"] for r in idle if r.get("ticket") == tid and r.get("stream")]
    if (f := next((f for f in reversed(runs) if f.is_file()), None)):
        # [^"\]: the line ends where its JSON string does, or at its escaped newline; fish reads the quotes alike
        return f"""{gone}; its last idle run's: grep -o 'RESULT: PARTIAL[^"\\]*' {shell_path(f)} | tail -1"""
    try:
        branches = Queue(p).wip.get(tid)
    except Exception:  # no remote yet
        branches = None
    if branches:
        return f"{gone}, nor its transcripts; its branch's commits: git log {p.upstream}..{p.remote}/{branches[0]}"
    return f"{gone}, nor its transcripts, and {tid} has no branch left to say why"


def lost_parts(died, killed):
    """The lost runs' kinds, naming only those that happened: "died" alone, or "2 died, 1 killed for time" when both
    did (a "0 killed" on the usual ledger is noise)."""
    if not (died and killed):
        return "died" if died else "killed for time"
    return f"{died} died, {killed} killed for time"


def k(n):
    """`n` tokens in thousands. Under 1,000, "<1k": a rounded "0k" reads as none spent."""
    return f"{n / 1000:,.0f}k" if n >= 1000 else "<1k" if n > 0 else "0"


def usd(g):
    """A row's API $: a floor (≥) when some runs ended before their result, "?" when none of them has one."""
    if g["no_usd"] == g["runs"]:
        return "?"
    return ("≥" if g["no_usd"] else "") + f"{g['usd']:.2f}"


def stats_table(by, agg, outcome, wide=False):
    """One line per role or ticket, the costliest (weighted) first. 100 columns at most by default; `wide` adds the
    context columns, for tuning prompts."""
    k1 = lambda n, d: f"{n / d / 1000:,.1f}k" if d else "-"
    w = name_width(by, agg, outcome)
    print(stats_header(by, outcome, wide, w))
    for name, g in sorted(agg.items(), key=lambda kv: -kv[1]["w"]):
        o = f" {outcome.get(name, '-'):7}" if outcome else ""
        more = f" {k1(g['base'], g['based']):>6} {k1(g['cache'], g['turns']):>11}" if wide else ""
        print(f"{name[:w]:{w}}{o} {g['runs']:4} {g['died'] + g['killed'] or '·':>11} {g['turns']:5} {k(g['in']):>9} "
              f"{k(g['cache']):>10} {k(g['out']):>7} {k(g['w']):>9} {usd(g):>7} {g['min']:5.0f}{more}")


def name_width(by, agg, outcome):
    """The first column's width: 12, or the longest name (`critic:project`, `periodic:playtest`) while the table
    still fits 100 columns."""
    room = 12 + 100 - len(stats_header(by, outcome, False))
    return max(12, min(max(map(len, agg), default=12), room))


def stats_header(by, outcome, wide, w=12):
    """The table's header, whose length is its width: the --wide note says it."""
    col = f" {'outcome':7}" if outcome else ""
    more = f" {'base':>6} {'cached/turn':>11}" if wide else ""
    return (f"{by:{w}}{col} {'runs':>4} {'died/killed':>11} {'turns':>5} {'fresh in':>9} {'cached in':>10} {'out':>7} "
            f"{'weighted':>9} {'API $':>7} {'min':>5}{more}")


def stats_per_merged(agg, outcome):
    """What a merged ticket costs on its own and with the rest spread over them: the runs with no ticket (planner,
    preflight, periodic), named, and tickets no longer in the queue (renumbered, folded, deleted), counted apart;
    and the share spent on tickets that didn't merge."""
    merged = [g for n, g in agg.items() if outcome.get(n) == "merged"]
    w = lambda gs: sum(g["w"] for g in gs)
    other = w(g for n, g in agg.items() if outcome.get(n) in ("open", "parked", "failed", "rejected"))
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
        return no_verdicts(p, recs, since)
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
              f"{c['capped']}; caps answered yes: {c['hits']} sent back, {c['scoped_caps']} out of scope")
    for t, rs in c["tickets"]:
        print(f"  {t:8} {' → '.join(g(r['score']) for r in rs):14} {rs[-1].get('step', '')}")
    no_verdicts(p, recs, since)
    for line in (shared_line(p, lad), unanswered_line(p, recs), critic_mod.ceiling_line(p, lad)):
        if line:
            print(line)
    rates = critic_mod.rates(recs, a.last)
    if lad and rates:
        print("checks, yes on the first head / on any head, of the tickets that touched them:")
        for d, cs in lad.items():
            for r, cid, _ in cs:
                if cid in rates:
                    first, ever, k = rates[cid]
                    print(f"  {d + ' ' + g(r):9} {cid:10} {first:>2}/{k:<2} {ever:>2}/{k:<2} "
                          f"{critic_mod.advice(first, ever, k, top=r > c['bar'] or r == cs[-1][0])}".rstrip())
    bought(p, [r for r in recs if r.get("ticket") in dict(c["tickets"])], lad, c["bar"], since)


SENT_BACK = ("revise", "rethink")  # the critic's steps that send the work back to the worker
PASSED = ("pass", "scope", "accept")  # its steps that let the work go on to the review, a verdict given
STEP_RE = re.compile(r"(?m)^CRITIC STEP: (\w+)")  # at a line start, as the critic prints it: not grepped code or docs
CALL_RE = re.compile(r"(?:^|&&|;|\|\||\n)[ \t]*(?:timeout\s+\S+\s+)?(?:\S*/)?windvane\s+"
                     r"(?:critic\b|bg\s+(?:--\S+\s+)*\S+\s+(?:\S*/)?windvane\s+critic\b|await\s+critic\b)")
REVIEW_LOG_RE = re.compile(r"(\S+ \S+) (\S+): review (approve|changes): (.*)")


def bought(p, recs, lad, bar, since):
    """What the critic's verdicts bought in the window: which checks sent work back and whether the next head
    changed the ticket's tree or only the pack, what those revision rounds cost the worker, the passes the reviewer
    still bounced, and the checks that never decided anything. Read from critic.jsonl, the ledger, the log and the
    worker streams, so a change to the critic can show what it saved."""
    by = {}
    for r in recs:
        if critic_mod.ticketed(r):
            by.setdefault(r["ticket"], []).append(r)
    backs = [(t, r, rs[i + 1] if i + 1 < len(rs) else None)
             for t, rs in by.items() for i, r in enumerate(rs) if r.get("step") in SENT_BACK]
    if not by:
        return
    times = Counter(Counter(t for t, _, _ in backs).values())
    went = [f"{times[n]} went back {n_times(n)}" for n in sorted(times)]
    note(f"what it bought: {len(by) - sum(times.values())} of {plural(len(by), 'ticket')} passed first time"
         + "".join(f", {w}" for w in went) + (f"; {plural(len(backs), 'revision')}: {shape(backs)}" if backs else ""))
    sent_back_table(recs, backs, lad)
    rounds_line(p, by, backs)
    misses(p, by, since)
    depths(p, by, since)
    later_runs(p, by, since)
    undecided(recs, lad, bar)


def n_times(n):
    return {1: "once", 2: "twice"}.get(n, f"{n} times")


def changed(r, nxt):
    """What a sent-back verdict's next call judged: "tree" when the ticket's head moved (`sha` differs), "pack" when
    only the evidence pack did, "none" when the critic wasn't asked again."""
    return "none" if nxt is None else "tree" if nxt.get("sha") != r.get("sha") else "pack"


def shape(backs):
    n = Counter(changed(r, nxt) for _, r, nxt in backs)
    return ", ".join(f"{n[k]} {what}" for k, what in (("tree", "changed the tree"), ("pack", "only the pack"),
                                                      ("none", "not asked again")) if n[k])


def sent_back_table(recs, backs, lad):
    """Per check: its yes answers of all its answers, and the revisions it sent back alone or with other checks, by
    what the next head changed. Checks in the scale's order, then any an older scale had."""
    yes, asked = answered(recs)
    rows = {}
    for _, r, nxt in backs:
        failed = r.get("failed") or []
        for cid in dict.fromkeys(failed):
            row = rows.setdefault(cid, Counter())
            row["alone" if len(set(failed)) == 1 else "others"] += 1
            row[changed(r, nxt)] += 1
    order = [cid for cs in (lad or {}).values() for _, cid, _ in cs] + list(asked) + list(rows)
    order = [cid for cid in dict.fromkeys(order) if cid in asked or cid in rows]
    if not order:
        return
    print("  check        yes      sent back: alone  with others  tree  pack only")
    for cid in order:
        row = rows.get(cid, Counter())
        print(f"  {cid:12} {f'{yes[cid]}/{asked[cid]}':8} {row['alone']:>16}  {row['others']:>11}  {row['tree']:>4}"
              f"  {row['pack']:>9}")


def answered(recs):
    """Each check's yes answers and its answers of any kind, over the ticket verdicts in `recs`."""
    yes, asked = Counter(), Counter()
    for r in recs:
        for cid, a in (r.get("answers") or {}).items() if critic_mod.ticketed(r) else ():
            asked[cid] += 1
            yes[cid] += a == "yes"
    return yes, asked


def stream_rounds(p, runs):
    """The worker's revision rounds on a ticket, in order, as [(turns, weighted tokens)], from its worker runs'
    ledger rows `runs`: each from a `CRITIC STEP: revise` (or rethink) the critic call printed to the next critic
    call, across the ticket's worker runs (a run cut off mid-round carries on in the next). A turn's input and cache tokens are its own, from the stream; its
    output isn't in the stream (each turn's usage there is a partial count), so a round gets the run's output in
    proportion to its turns."""
    rounds, cur = [], None
    for row in runs:
        seen = {}  # every turn of the run, by message id
        for ev in turns_and_steps(p.state / "runs" / row["stream"]):
            msg = ev.get("message") or {}
            if ev.get("type") == "assistant":
                mid = msg.get("id") or len(seen)
                seen[mid] = msg.get("usage") or {}
                if cur is not None:
                    cur.setdefault(row["stream"], {})[mid] = seen[mid]
                    if any(CALL_RE.search(str((c.get("input") or {}).get("command", ""))) for c in tool_uses(msg)):
                        cur = None
            elif ev.get("type") == "user" and isinstance(msg.get("content"), list):
                for c in msg["content"]:
                    if c.get("type") == "tool_result" and (step := critic_step(c.get("content"))):
                        cur = {} if step in SENT_BACK else None
                        rounds += [cur] * (cur is not None)
        per_turn = (row.get("output") or 0) / max(len(seen), 1)
        for r in rounds:
            for mid, u in r.get(row["stream"], {}).items():
                u["_out"] = per_turn
    return [(sum(len(v) for v in r.values()), sum(turn_weight(u) for v in r.values() for u in v.values()))
            for r in rounds]


def critic_step(content):
    """The critic step a tool result ends on: the last line-start `CRITIC STEP:` in its text, the line the worker
    follows. A result's content is its text or a list of parts."""
    if isinstance(content, list):
        content = "\n".join(str(c.get("text", "")) for c in content if isinstance(c, dict))
    steps = STEP_RE.findall(content) if isinstance(content, str) else []
    return steps[-1] if steps else None


def turns_and_steps(path):
    """A stream's assistant turns and the tool results that hold a critic step, parsed: only those lines are, since
    the rest (tool output, mostly) is most of a stream's size, and `stats` reads every worker stream in the window."""
    try:
        lines = Path(path).read_text(errors="replace").splitlines()
    except OSError:
        return []
    out = []
    for line in lines:
        if '"assistant"' in line[:40] or "CRITIC STEP" in line:
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            out += [ev] * isinstance(ev, dict)
    return out


def tool_uses(msg):
    content = msg.get("content")
    return [c for c in content if isinstance(c, dict) and c.get("type") == "tool_use"] if isinstance(content, list) else []


def turn_weight(u):
    return weighted({"input": u.get("input_tokens"), "cache_write": u.get("cache_creation_input_tokens"),
                     "cache_read": u.get("cache_read_input_tokens"), "output": u.get("_out")})


def rounds_line(p, by, backs):
    """What the revision rounds cost the worker, split by what they changed when a ticket's rounds in the streams
    line up one to one with its sent-back verdicts."""
    if not backs:
        return
    total, split, unmatched = [0, 0, 0], {}, 0
    runs = {}
    for row in read_jsonl(p.state / "ledger.jsonl"):
        if row.get("role") == "worker" and not row.get("event") and row.get("ticket") in by and row.get("stream"):
            runs.setdefault(row["ticket"], []).append(row)
    for t in by:
        verdicts = [(r, nxt) for u, r, nxt in backs if u == t]
        rounds = stream_rounds(p, runs.get(t, []))
        for turns, w in rounds:
            total[0] += 1
            total[1] += turns
            total[2] += w
        if len(rounds) != len(verdicts):
            unmatched += len(rounds)
            continue
        for (r, nxt), (turns, w) in zip(verdicts, rounds):
            s = split.setdefault(changed(r, nxt), [0, 0, 0])
            s[0], s[1], s[2] = s[0] + 1, s[1] + turns, s[2] + w
    if not total[0]:
        print("revision rounds: none found in the worker streams")
        return
    parts = [f"{s[0]} {what}: {plural(s[1], 'turn')}, {k(s[2])}" for key, what in
             (("tree", "changed the tree"), ("pack", "only the pack"), ("none", "not asked again"))
             if (s := split.get(key))]
    parts += [f"{unmatched} not matched to a verdict"] * bool(unmatched)
    note(f"revision rounds in the worker streams: {plural(total[0], 'round')}, {plural(total[1], 'turn')}, "
         f"{k(total[2])} weighted tokens" + (f" ({'; '.join(parts)})" if parts else "")
         + "; a round's output tokens are its run's, shared out by turns")


def reviewed(p, by, since):
    """The reviews after a critic pass, as [(the review's log match, the verdict that passed it)]."""
    out = []
    for line in last_log(p, None):
        m = REVIEW_LOG_RE.match(line)
        if not m or m[2] not in by or m[1] < since:
            continue
        before = [r for r in by[m[2]] if r.get("at", "") <= m[1]]
        if before and before[-1].get("step") in PASSED:
            out.append((m, before[-1]))
    return out


def misses(p, by, since):
    """The critic's misses: reviews that asked for changes on a ticket whose latest verdict before the review let the
    work go on, with the review's line from the log."""
    seen = reviewed(p, by, since)
    reviewed_n, missed = len(seen), [m for m, _ in seen if m[3] == "changes"]
    if not reviewed_n:
        return
    print(f"critic misses: {missed and len(missed) or 'none'} of {plural(reviewed_n, 'review')} after a critic pass "
          f"asked for changes" + ":" * bool(missed))
    for m in missed:
        print(f"  {m[2]:8} {m[1]}  {critic_mod.clip(m[4], 70)}")


def depths(p, by, since):
    """The critic's cost and misses by depth (`critic.depth`): a ticket counts at the depth `auto` started it at (a
    light read that asked for the trial counts as light), with its critic sessions' weighted tokens from the ledger,
    then the reviews after a pass that still asked for changes, by the depth of the verdict that passed it. Nothing
    until a verdict records its depth."""
    began = {t: "light" if rs[0].get("asked_trial") else rs[0]["depth"] for t, rs in by.items() if rs[0].get("depth")}
    if not began:
        return
    cost = Counter()
    for row in read_jsonl(p.state / "ledger.jsonl"):
        if row.get("role") == "critic" and row.get("ticket") in began and row.get("at", "") >= since:
            cost[row["ticket"]] += weighted(row)
    parts = []
    for d in ("light", "full"):
        if ts := [t for t, v in began.items() if v == d]:
            asked = sum(any(r.get("asked_trial") for r in by[t]) for t in ts)
            parts.append(f"{d} {plural(len(ts), 'ticket')}, {sum(cost[t] for t in ts) / len(ts) / 1000:.0f}k"
                         + " weighted critic tokens" * (not parts) + " each"
                         + f", {asked} asked for the trial" * (d == "light"))
    seen = [(v["depth"], m[3] == "changes") for m, v in reviewed(p, by, since) if v.get("depth")]
    missed = [f"{d} {sum(x for e, x in seen if e == d)} of {sum(e == d for e, _ in seen)}" for d in ("light", "full")
              if any(e == d for e, _ in seen)]
    note("by depth (a ticket by the depth it started at): " + "; ".join(parts) + "."
         + (f" Reviews after a pass that asked for changes: {', '.join(missed)}" if missed else ""))


def later_runs(p, by, since):
    """The critic's first critique of a ticket against the ones after it, in turns and weighted tokens from the
    ledger, and the checks re-critiques carried over (`critic.carry`): what judging the revision instead of the whole
    change again saves shows in the later ones. A critique starts at a `-critic` or `-critic-light` session; a light
    read's `-critic-trial`, the time-up turn and the re-ask are the same critique."""
    runs = {}
    for row in read_jsonl(p.state / "ledger.jsonl"):
        t, label = row.get("ticket"), row.get("label", "")
        if row.get("role") != "critic" or t not in by or row.get("at", "") < since:
            continue
        mine = runs.setdefault(t, [])
        if not mine or label in (f"{t}-critic", f"{t}-critic-light"):
            mine.append([0, 0.0])
        mine[-1][0] += row.get("turns") or 0
        mine[-1][1] += weighted(row)
    first = [rs[0] for rs in runs.values()]
    later = [r for rs in runs.values() for r in rs[1:]]
    if not first:
        return
    each = lambda xs: f"{sum(x[0] for x in xs) / len(xs):.1f} turns and {sum(x[1] for x in xs) / len(xs) / 1000:.0f}k"
    carried = [r for rs in by.values() for r in rs if "carried" in r]
    note(f"critic runs: first critiques {len(first)}, {each(first)} weighted tokens each"
         + (f"; later ones {len(later)}, {each(later)} each, {sum(x[1] for x in later) / len(first) / 1000:.0f}k "
            f"per ticket" if later else "; no later ones")
         + (f"; {sum(len(r['carried']) for r in carried)} checks carried over in {plural(len(carried), 're-critique')}"
            if carried else ""))


def undecided(recs, lad, bar, least=5):
    """Checks that decided nothing in the window, once `least` verdicts answered them: always yes, so never sent
    work back, or (over the pass mark) never yes, so never raised a score. Their answers cost the critic's output
    tokens either way."""
    above = {cid for cs in (lad or {}).values() for r, cid, _ in cs if r > bar}
    yes, asked = answered(recs)
    always = [f"{cid} {asked[cid]}/{asked[cid]}" for cid in asked if asked[cid] >= least and yes[cid] == asked[cid]]
    never = [f"{cid} 0/{asked[cid]}" for cid in asked if asked[cid] >= least and cid in above and not yes[cid]]
    if always or never:
        note("decided nothing: " + "; ".join([f"always yes: {', '.join(always)}"] * bool(always)
                                            + [f"never yes over the pass mark: {', '.join(never)}"] * bool(never)))


def no_verdicts(p, recs, since):
    """What replies with no verdict cost (T-116): the `step: none` runs, and the same-session re-asks that try to
    save one, from critic.jsonl (a verdict they saved says `reasked`, as does a none they didn't, or `reask_failed`),
    with the re-ask sessions' tokens from the ledger by their `-critic-reask` label. Nothing when there were none."""
    nones = [r for r in recs if r.get("step") == "none"]
    saved = sum(r.get("reasked") is True and r.get("step") != "none" for r in recs)
    replied, failed = sum(r.get("reasked") is True for r in nones), sum(bool(r.get("reask_failed")) for r in nones)
    runs = [r for r in read_jsonl(p.state / "ledger.jsonl") if not r.get("event") and r.get("at", "") >= since
            and str(r.get("label", "")).endswith("-critic-reask")]
    if nones:
        print(f"no verdict: {plural(len(nones), 'critic run')} (step: none)")
    parts = ([f"{saved} saved a verdict"] * bool(saved) + [f"{replied} still no verdict"] * bool(replied)
             + [f"{failed} failed"] * bool(failed))
    cost = f"{plural(len(runs), 'session')}, {k(sum(map(weighted, runs)))} weighted tokens" if runs else ""
    if parts or cost:
        print(f"re-asks: {', '.join(parts)}" + (f" ({cost})" if parts and cost else cost))


def critic_since(p, when):
    """`--since` as the log's "YYYY-MM-DD HH:MM": a date as is, a commit (say the one that changed CALIBRATION.md)
    as its author time in local time, like the verdicts' own `at`: a rebase moves the commit time later, past
    verdicts given after the change was written."""
    if re.fullmatch(r"\d{4}-\d\d-\d\d", when):
        return when
    at = gout(p.root, "log", "-1", "--format=%ad", "--date=format-local:%Y-%m-%d %H:%M", f"{when}^{{commit}}", "--")
    return at or sys.exit(f"--since: {when!r} is neither a date (YYYY-MM-DD) nor a commit")


def ticket_outcomes(p, agg):
    """merged / parked / failed / open / rejected (an experiment the bench didn't keep) for each ticket in `agg`, from
    the queue's git-derived status and the ledger, "gone" for a ticket no longer in the queue (runs that belong to no
    ticket, like the planner's, get none)."""
    try:
        q = Queue(p)
    except Exception:  # no remote yet: no outcomes rather than no stats
        return {}
    names, failed = {"done": "merged", "parked": "parked"}, failed_tickets(p)
    return {t: "rejected" if t in q.rejected else names.get(q.status(t), "failed" if t in failed else "open")
            if t in q.files else "gone"
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


def unanswered_line(p, recs):
    """The check ids the critic's replies left unanswered most (counted as no), top three, by the rule `overview`
    and `doctor` use (`critic.unanswered`), so all three name the same ids: an id the driver misreads (a check named
    `beyond` once read as the "Beyond this ticket" list) shows as one id piling up here."""
    n = critic_mod.unanswered(recs)
    if not n:
        return ""
    more = f", {len(n) - 3} more" if len(n) > 3 else ""
    return (f"unanswered checks, counted as no (times of the last {n[0][2]} verdicts): "
            + ", ".join(f"{cid} {k}" for cid, k, _ in n[:3]) + f"{more}; reword or rename "
            f"{'it' if len(n) == 1 else 'them'} in {p.rubric_rel}.")


def shared_line(p, lad):
    """A check id two dimensions share: the critic answers by id, so one answer would score both."""
    dup = [f"{cid} ({', '.join(ds)})" for cid, ds in critic_mod.shared(lad).items()]
    return (f"Critic scale reuses check ids, so one answer scores both: {'; '.join(dup)}; rename one in "
            f"{p.rubric_rel}." if dup else "")


# ---------- `stats --projects`: every project on this machine, side by side

SHARED_DIRS = ("phone",)  # state-root folders every project shares; `_`-prefixed ones are windvane's own as well
ID = r"[A-Z][A-Z0-9]*-\d+[a-z]?"
MERGED_RE = re.compile(rf"(\S+ \S+) ({ID}): merged [0-9a-f]{{7}}\b")
FAILED_RE = re.compile(rf"\S+ \S+ ({ID}): FAILED: (.*)")
# A later ticket's text naming a merged one as what it fixes ("fixes T-12", "a bug in T-12's check") or as broken
# ("T-12 broke", "T-12's cap is wrong"), the words next to the id: a "fix" further off is mostly about something else.
FIXES = r"(?:fix|fixes|fixing|re-?fix\w*|revert|reverts|reverting|undo|undoes)(?:\s+(?:for|what|in|the|to))?"
BROKEN_BY = r"(?:broken|bugs?|regression|regressed)\s+(?:\w+\s+)?(?:by|in|from|of)"
BROKEN = r"(?:broke|broken|bugs?|buggy|regress\w*|wrong|reverted)"
CHECK_ALIVE_S = 120  # a running check's heartbeat writes its log at least once a minute
QUICK_HINT_MIN = 10  # checks averaging more a checked ticket, with no `check_quick`, get its hint


def projects_stats(a):
    """`stats --projects`: each project's state dir under the state root (one with a ledger) read side by side over
    `--since` (default 30 days), then each measure's spread across them. Reads only: no lock is taken, so a run in
    another project never waits on it, and no file is written."""
    since = projects_since(a.since)
    repos = phone.projects()  # every run records its checkout there as it starts
    rows = [project_digest(d, repos.get(d.name), since) for d in project_dirs(user_dir("XDG_STATE_HOME", ".local/state"))]
    digest = {"since": since, "projects": rows, "across": across(rows)}
    if a.json:
        return print(json.dumps(digest, indent=1))
    print_digest(digest)


def projects_since(when):
    if when is None:
        return f"{now() - timedelta(days=30):%Y-%m-%d %H:%M}"
    try:
        ok = re.fullmatch(r"\d{4}-\d\d-\d\d", when) and date.fromisoformat(when)
    except ValueError:  # 2026-13-45: an empty digest would read as a quiet month
        ok = False
    return when if ok else sys.exit(f"--since: {when!r} isn't a date (YYYY-MM-DD); with --projects a commit means "
                                    "nothing")


def project_dirs(root):
    try:
        dirs = sorted(d for d in root.iterdir() if d.is_dir())
    except OSError:
        return []
    return [d for d in dirs if not d.name.startswith("_") and d.name not in SHARED_DIRS
            and (d / "ledger.jsonl").is_file()]


def load_project(repo):
    """The project at `repo`, for its config and queue; None when its config no longer loads."""
    try:
        return Project(repo)
    except (SystemExit, Exception):
        return None


def project_digest(d, repo, since):
    """One project's measures over the window, as plain data: the table prints it, `--json` dumps it."""
    sp = SimpleNamespace(state=d)  # all the log and ledger readers need
    ledger = read_jsonl(d / "ledger.jsonl")
    rows = [r for r in ledger if r.get("at", "") >= since]
    log = last_log(sp, None)
    merged = list(dict.fromkeys(m[2] for line in log if (m := MERGED_RE.match(line)) and m[1] >= since))
    p = load_project(repo) if repo else None
    q = queue_of(p)
    failed = failed_in(ledger, log, since)
    parked = {t: parked_why(line, t) for t, line in q.parked_lines.items()} if q else None
    return {
        "name": d.name, "repo": str(repo) if repo else None,
        "tickets": {"merged": len(merged), "failed": len(failed), "parked": None if parked is None else len(parked)},
        "tokens": token_shares(rows, len(merged)),
        "critic": critic_digest(sp, d, since, p.critic["pass"] if p else DEFAULTS["critic"]["pass"]),
        "checks": checks_digest(sp, rows),
        "check_quick": bool(p.cfg["check_quick"]) if p else None,
        "refixes": refixes(q, merged) if q else None,
        "calibration_lines": calibration_lines(p, since) if p else None,
        "failed": failed, "parked": parked,
    }


def parked_why(line, tid):
    """A `- parked: <ID> (why)` line's why, without the one pair of brackets around it: a bracket inside stays."""
    why = re.sub(rf"^\s*-\s*parked:\s*{re.escape(tid)}\s*", "", line, flags=re.I).strip()
    if why.startswith("(") and why.endswith(")"):
        why = why[1:-1].strip()
    return why or "no reason given"


def queue_of(p):
    try:
        return Queue(p) if p else None
    except Exception:  # no remote branch to read the queue from
        return None


def token_shares(rows, merged):
    """Weighted tokens in all and per merged ticket, and each role's share of them (a `periodic:<name>` or
    `critic:project` session counts under its role)."""
    by = Counter()
    for r in rows:
        if not r.get("event") and r.get("role"):
            by[r["role"].split(":")[0]] += weighted(r)
    total = sum(by.values())
    per = lambda w: round(w / merged) if merged else None
    return {"weighted": round(total), "per_merged": per(total),
            "roles": {role: {"share": round(100 * w / total, 1), "per_merged": per(w)} for role, w in by.most_common()}}


def critic_digest(sp, d, since, bar):
    """The critic on tickets it first judged in the window: how many passed first time, the median first and final
    score, the final scores exactly on the pass mark; and the reviews after a critic pass that still sent work back."""
    by = {}
    for r in read_jsonl(d / "critic.jsonl"):
        if critic_mod.ticketed(r) and r.get("ticket"):
            by.setdefault(r["ticket"], []).append(r)
    by = {t: rs for t, rs in by.items() if rs[0].get("at", "") >= since}
    scored = {t: [r for r in rs if r.get("score") is not None] for t, rs in by.items()}
    scored = {t: rs for t, rs in scored.items() if rs}
    med = lambda xs: statistics.median(xs) if xs else None
    seen = reviewed(sp, by, since)
    return {"tickets": len(scored), "bar": bar,
            "first_pass": sum(rs[0].get("step") in PASSED for rs in scored.values()),
            "median_first": med([rs[0]["score"] for rs in scored.values()]),
            "median_final": med([rs[-1]["score"] for rs in scored.values()]),
            "final_on_bar": sum(rs[-1]["score"] == bar for rs in scored.values()),
            "reviews": len(seen), "sent_back": sum(m[3] == "changes" for m, _ in seen)}


def written_lately(sp, row):
    """A check still running, told without its lock (taking one, even to test it, is the running project's): its
    heartbeat wrote its log within CHECK_ALIVE_S."""
    try:
        return time.time() - (sp.state / "checks" / row["log"]).stat().st_mtime < CHECK_ALIVE_S
    except (KeyError, OSError):
        return False


def checks_digest(sp, rows):
    ran, _, _, not_run, cancelled = check_runs(sp, [r for r in rows if r.get("event") in ("check", "check-start")],
                                               written_lately)
    mins, tickets = sum(run_min(r) for r in ran), {r["ticket"] for r in ran}
    cut = sum(r.get("finished") is False for r in ran)
    awaited = sum(r.get("seconds") or 0 for r in rows if r.get("event") == "await" and r.get("role") == "worker")
    return {"runs": len(ran), "tickets": len(tickets), "per_ticket": round(mins / len(tickets), 1) if tickets else None,
            "minutes": round(mins, 1), "waited": round(queue_min(ran, not_run, cancelled), 1),
            "failed": sum(not r.get("passed") for r in ran) - cut, "cut_off": cut,
            "cancelled_queued": len(cancelled), "worker_await": round(awaited / 60, 1)}


def quick_hint(per_ticket, has_quick):
    """The one line that points a project with slow checks at `check_quick`: a failing check then ends at its quick
    pass, in a minute, instead of the full length. None when the checks are short, it's set, or the config is
    unknown (it may be set)."""
    if per_ticket is None or per_ticket <= QUICK_HINT_MIN or has_quick is not False:
        return None
    return (f"checks averaged {m(per_ticket)} min per checked ticket: set `check_quick` for a first pass that shows "
            "a failure in a minute (docs/configuration.md#the-checks-environment)")


def failed_in(ledger, log, since):
    """{ticket: its one-line reason} for the tickets that failed in the window and weren't run again since: the
    ledger's `outcome: failed` row, unless a later worker run retried it, with the log's `<ID>: FAILED:` reason."""
    last = {}
    for r in ledger:
        t = r.get("ticket")
        if t and r.get("event") == "outcome":
            last[t] = r if r.get("outcome") == "failed" else None
        elif t and r.get("role") == "worker" and not r.get("event"):
            last[t] = None
    why = {m[1]: m[2] for line in log if (m := FAILED_RE.match(line))}
    return {t: critic_mod.clip(why.get(t, "no reason in the log"), 100)
            for t, r in sorted(last.items()) if r and r.get("at", "") >= since}


def refixes(q, merged):
    """[[merged ticket, the later ticket that fixes it]]: a ticket numbered after one merged in the window whose text
    names it in a sentence about a fix, a bug or something broken."""
    num = lambda t: int(re.search(r"\d+", t).group())
    out = []
    for m in merged:
        pat = refix_re(m)
        by = next((t for t in sorted(q.text, key=num) if num(t) > num(m) and pat.search(q.text[t])), None)
        if by:
            out.append([m, by])
    return out


def refix_re(tid):
    """What a later ticket says when it re-fixes `tid`: a fix word just before its id, or a broken word within three
    words after it, in the same sentence."""
    t = rf"(?<![\w-]){re.escape(tid)}(?![\w-])"
    return re.compile(rf"\b(?:{FIXES}|{BROKEN_BY})\s+{t}|{t}(?:'s)?[^\w.!?;\n]{{0,3}}(?:\w+[^\S\n]+){{0,3}}?{BROKEN}\b",
                      re.I)


def calibration_lines(p, since):
    """Lines added to the project's CALIBRATION.md on its main branch in the window: the user tuning or overruling
    the critic."""
    out = gout(p.main_root(), "log", p.upstream, f"--since={since}", "--numstat", "--format=", "--",
               p.calibration_rel)
    return sum(int(n) for n, *_ in (line.split("\t") for line in out.splitlines() if line.strip()) if n.isdigit())


# Each measure `across` compares: (label, how to read it from a project's digest, how to print a value).
pct = lambda a, b: round(100 * a / b) if b else None
MEASURES = [
    ("merged tickets", lambda d: d["tickets"]["merged"], "{:g}"),
    ("worker share of tokens", lambda d: d["tokens"]["roles"].get("worker", {}).get("share"), "{:g}%"),
    ("critic share of tokens", lambda d: d["tokens"]["roles"].get("critic", {}).get("share"), "{:g}%"),
    ("weighted per merged ticket", lambda d: d["tokens"]["per_merged"], "k"),
    ("critic first-pass rate", lambda d: pct(d["critic"]["first_pass"], d["critic"]["tickets"]), "{:g}%"),
    ("median first critic score", lambda d: d["critic"]["median_first"], "{:g}"),
    ("median final critic score", lambda d: d["critic"]["median_final"], "{:g}"),
    ("final scores on the pass mark", lambda d: pct(d["critic"]["final_on_bar"], d["critic"]["tickets"]), "{:g}%"),
    ("reviews sent back after a pass", lambda d: pct(d["critic"]["sent_back"], d["critic"]["reviews"]), "{:g}%"),
    ("check min per checked ticket", lambda d: d["checks"]["per_ticket"], "{:g}"),
    ("worker min in await", lambda d: d["checks"]["worker_await"], "{:,.0f}"),
    ("re-fixes", lambda d: None if d["refixes"] is None else len(d["refixes"]), "{:g}"),
    ("CALIBRATION.md lines added", lambda d: d["calibration_lines"], "{:g}"),
    ("failed tickets", lambda d: d["tickets"]["failed"], "{:g}"),
]


def show(value, fmt):
    return k(value) if fmt == "k" else fmt.format(value)


def across(rows):
    """Each measure's lowest and highest value with the projects that have it, over the projects that have the
    measure at all; none for a measure fewer than two projects have."""
    out = []
    for label, get, fmt in MEASURES:
        have = [(get(d), d["name"]) for d in rows if get(d) is not None]
        if len(have) < 2:
            continue
        lo, hi = min(v for v, _ in have), max(v for v, _ in have)
        out.append({"measure": label, "low": lo, "low_projects": [n for v, n in have if v == lo],
                    "high": hi, "high_projects": [n for v, n in have if v == hi]})
    return out


def print_digest(digest):
    rows = digest["projects"]
    print(f"{plural(len(rows), 'project')} since {digest['since']}")
    for d in rows:
        print()
        print_project(d)
    if not digest["across"]:
        return
    fmts = {label: fmt for label, _, fmt in MEASURES}
    end = lambda x, e: f"{show(x[e], fmts[x['measure']])} {', '.join(x[e + '_projects'])}"
    spread = [x for x in digest["across"] if x["low"] != x["high"]]
    w = max((len(end(x, "low")) for x in spread), default=0) + 2
    print()
    print(f"{'across projects':32} {'lowest':{w}} highest")
    for x in digest["across"]:  # one where every project has the same value says so once, not as both ends
        print(f"  {x['measure']:30} " + (f"{end(x, 'low'):{w}} {end(x, 'high')}" if x in spread else
                                          f"{show(x['low'], fmts[x['measure']])} in all {len(x['low_projects'])}"))


def print_project(d):
    t, w, c, ch = d["tickets"], d["tokens"], d["critic"], d["checks"]
    print(f"{d['name']}  {d['repo'] or 'repo unknown'}")
    print(f"  tickets: {t['merged']} merged, {t['failed']} failed"
          + (f", {t['parked']} parked now" if t["parked"] is not None else ""))
    each = lambda x: f", {k(x)} a merged ticket" if x is not None else ""
    roles = ", ".join(f"{r} {v['share']:g}%" + (f" ({k(v['per_merged'])})" if v["per_merged"] is not None else "")
                      for r, v in w["roles"].items())
    note(f"  weighted tokens: {k(w['weighted'])}{each(w['per_merged'])}"
         + (f"; by role, share (per merged ticket): {roles}" if roles else ""), "    ")
    if c["tickets"]:
        print(f"  critic: {plural(c['tickets'], 'ticket')}, {c['first_pass']} passed first time, median score "
              f"{c['median_first']:g} first, {c['median_final']:g} final, {c['final_on_bar']} final on the pass "
              f"mark ({c['bar']:g})")
    if c["reviews"]:
        print(f"  reviews after a critic pass: {c['reviews']}, {c['sent_back']} sent back")
    per = f" ({m(ch['per_ticket'])} min per checked ticket)" if ch["per_ticket"] is not None else ""
    queued = f", {m(ch['waited'])} min waiting for the machine" if ch["waited"] else ""
    cancelled = f", {ch['cancelled_queued']} cancelled in the queue" if ch["cancelled_queued"] else ""
    print(f"  checks: {plural(ch['runs'], 'run')}{per}{queued}, {ch['failed']} failed, {ch['cut_off']} cut off"
          f"{cancelled}; workers {m(ch['worker_await'])} min in await")
    if hint := quick_hint(ch["per_ticket"], d.get("check_quick")):
        note(f"  {hint}", "    ")
    if d["repo"] is None:
        print("  repo unknown: no re-fixes, CALIBRATION.md lines or parked tickets (it hasn't run since runs "
              "recorded their checkout)")
    elif d["refixes"] is None:
        print("  repo's config or queue doesn't load: no re-fixes, CALIBRATION.md lines or parked tickets")
    else:
        print(f"  re-fixes: {len(d['refixes'])}" + (f" ({', '.join(f'{a} by {b}' for a, b in d['refixes'])})"
                                                     if d["refixes"] else ""))
        print(f"  CALIBRATION.md: {plural(d['calibration_lines'], 'line')} added")
    for what, items in (("failed", d["failed"]), ("parked", d["parked"] or {})):
        for tid, why in items.items():
            print(f"  {what} {tid}: {critic_mod.clip(why, 80)}")
