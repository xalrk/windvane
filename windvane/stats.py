"""`windvane stats`: what the runs cost, from the ledger (tokens per role or ticket, checks, the gh guard's refusals,
periodic scores), and how the critic's scores spread (`--critic`)."""
import re
import sys
import textwrap
from collections import Counter
from datetime import timedelta

from . import critic as critic_mod
from . import jobs
from .queue import Queue
from .util import gout, last_log, now, read_jsonl


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


def check_runs(p, checks):
    """(the checks that ran, reused passes, checks still running, heavy checks that never ran: their wait for the
    machine timed out, which is no failure). Ran: the end rows that didn't reuse a pass or wait in vain, plus
    a start row no end row matches (same ticket and start) whose check isn't running any more: it was killed, and
    counts as cut off like an end row with `finished: false`, timed from its start to its log's last write (the
    check's heartbeat keeps that within a minute of the kill; 0 with no log)."""
    ends = [r for r in checks if r.get("event") == "check"]
    ended = {(r["ticket"], r.get("start")) for r in ends}
    open_ = [r for r in checks if r.get("event") == "check-start" and (r["ticket"], r.get("start")) not in ended]
    live = [r for r in open_ if jobs.running(p, r)]
    killed = [{**r, "minutes": killed_minutes(p, r), "finished": False} for r in open_ if r not in live]
    ran = [r for r in ends if not r.get("reused") and not r.get("not_run")]
    not_run = [r for r in ends if r.get("not_run")]
    return ran + killed, sum(bool(r.get("reused")) for r in ends), len(live), not_run


def killed_minutes(p, start_row):
    """A killed check's minutes: the check wrote its log up to the kill, so the log's last write is about when."""
    try:
        return jobs.log_minutes(p.state / "checks" / start_row["log"], start_row["start"])
    except (KeyError, OSError):  # no log: a row from before logs were named, or the log was cleared by hand
        return 0


def check_stats(checks, by, p):
    """What the check costs: its runs and minutes, per ticket that ran one, and the failed, cut off, reused and still
    running ones (only when there are some); the longest check that bought nothing (failed or cut off), and by ticket,
    the ticket whose checks took longest. Not in the table: a check spends time, not tokens."""
    if not checks:
        return
    ran, reused, live, not_run = check_runs(p, checks)
    mins = sum(r.get("minutes") or 0 for r in ran)
    cut = sum(r.get("finished") is False for r in ran)
    failed = sum(not r.get("passed") for r in ran) - cut
    per = {}
    for r in ran:
        t = per.setdefault(r["ticket"], [0, 0.0])
        t[0] += 1
        t[1] += r.get("minutes") or 0
    print(f"checks: {plural(len(ran), 'run')}, {m(mins)} min" + (f" ({mins / len(per):.1f} min a ticket checked)"
          if per else "") + (f", {failed} failed" if failed else "") + (f", {cut} cut off" if cut else "")
          + (f", {reused} reused a pass" if reused else "") + (f", {live} still running" if live else ""))
    if not_run:  # the gate (or the session) ran each again rather than bounce the ticket: its wait is under "waits"
        print(f"not-run checks: {len(not_run)} (no bounce)")
    if by == "ticket" and per:
        top = max(per, key=lambda t: (per[t][1], per[t][0]))
        print(f"most check time: {top}, {plural(per[top][0], 'run')}, {m(per[top][1])} min")
    wasted = [r for r in ran if not r.get("passed") and (r.get("minutes") or 0) > 0]
    if wasted:
        w = max(wasted, key=lambda r: r["minutes"])
        print(f"costliest wasted check: {w['ticket']}, {m(w['minutes'])} min, "
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
            s = r.get("waited_s") or (60 * (r.get("minutes") or 0) if r.get("not_run") else 0)
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
    for line in (shared_line(p, lad), critic_mod.ceiling_line(p, lad)):
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


def shared_line(p, lad):
    """A check id two dimensions share: the critic answers by id, so one answer would score both."""
    dup = [f"{cid} ({', '.join(ds)})" for cid, ds in critic_mod.shared(lad).items()]
    return (f"Critic scale reuses check ids, so one answer scores both: {'; '.join(dup)}; rename one in "
            f"{p.rubric_rel}." if dup else "")
