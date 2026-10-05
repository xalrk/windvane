"""[[periodic]] sessions: a project's own session (a playtest, an audit) between tickets every `every` merges, when a
group of tickets is finished (`after_group`), or by hand with `windvane periodic <name>`, on a detached copy of current
main.

An entry's `then` runs another right after, given the first one's output; an entry some other names in `then` (a
triage) also gets the critic's "Beyond this ticket" notes no plan or triage has used yet, and the queue, the way
`windvane plan` does. New ticket files under the ids the chain was given, lines added at the end of an open ticket (a
finding it already covers), and the lines it added to QUEUE.md land on main, with the last session's final reply in
the commit; every other change is dropped with the tree. Where the new tickets go in the queue is the entry's `land`
(inherited along the chain): `near` right after the ticket in progress, `next` at the top, `end` after everything,
`hold` parked for the desk, `ask` one phone question (no answer in `answer_wait` minutes: hold).

A `score=<n>` on the PERIODIC line goes to the ledger (`event: score`), and `windvane stats --periodic` shows it.
"""
import difflib
import os
import re
from datetime import timedelta
from pathlib import Path

from . import critic, phone
from .questions import quiet_now
from .queue import Queue, title as title_of
from .runner import plain_error
from .util import append_jsonl, git, gout, hhmm, last_match, now, push, run_limited

PERIODIC_RE = re.compile(r"PERIODIC:[ \t]*(.*)")
SCORE_RE = re.compile(r"\bscore=(-?\d+(?:\.\d+)?)\b")
ASK_LAND = (("near", "queue them right after the ticket in progress"), ("end", "queue them after everything else"),
            ("hold", "hold them, parked in QUEUE.md, for you at the desk"))


class Periodic:
    """The Pilot's [[periodic]] part. `periodic_at` and `groups_seen` (both carried across restarts) are each entry's
    merge count and its finished groups when it last ran; `held` the tickets parked for the desk this run, for the
    final summary. `groups_wait` (a skipped round's merge count) isn't carried: after a restart the owed groups are
    due at once, one more try rather than a lost playtest."""

    def periodic_due(self):
        """The first [[periodic]] entry whose `every` merges have passed since it last ran this run (restarts carry
        the count), or with a group finished since then, or None."""
        for e in self.p.periodic.values():
            if e["every"] and len(self.merged) - self.periodic_at.get(e["name"], 0) >= e["every"]:
                return e
            if e["after_group"] and len(self.merged) > self.groups_wait.get(e["name"], -1) and self.groups_finished(e):
                return e
        return None

    def group_queue(self):
        """The queue on main as last fetched, read again only when main moved: `periodic_due` looks between tickets,
        often, and a big project's queue costs a git call per ticket."""
        head = gout(self.p.main_root(), "rev-parse", self.p.upstream)
        if self.groups_read[0] != head:
            self.groups_read = (head, Queue(self.p))
        return self.groups_read[1]

    def groups_finished(self, e):
        """`e`'s groups finished since the run started or `e` last ran, as {group: its tickets}. The first look (a
        fresh run, a run by hand) only notes the groups finished already, on main as it is now: a group finished
        between runs (a PR merged on the forge) is no news. A seen group stays seen: one that opens again (a fix
        ticket filed under it) doesn't start the entry again when it finishes, or a playtest would chain to the next."""
        if e["name"] not in self.groups_seen:
            self.fetch()
        q = self.group_queue()
        found = groups(q, e["after_group"])
        done = {g for g, (_, left) in found.items() if not left}
        seen = self.groups_seen.setdefault(e["name"], sorted(done, key=natural))
        return {g: [t + " (parked)" * (t in q.parked) for t in found[g][0]] for g in sorted(done - set(seen), key=natural)}

    def periodic_ran(self, e, skipped=False):
        """`e` ran: its count starts again, and the groups finished now are seen. A round skipped before its session
        keeps them owed, due again after the next merge rather than at once (which would only skip it again)."""
        self.periodic_at[e["name"]] = len(self.merged)
        if skipped:
            self.groups_wait[e["name"]] = len(self.merged)
        elif e["after_group"]:
            self.groups_wait.pop(e["name"], None)
            done = {g for g, (_, left) in groups(self.group_queue(), e["after_group"]).items() if not left}
            self.groups_seen[e["name"]] = sorted(done | set(self.groups_seen.get(e["name"], [])), key=natural)

    def periodic(self, e):
        """Run `e` and the entries its `then` chains to, one after another in the ci tree on current main, then
        land the tickets they wrote. Whatever else they changed is discarded with the tree."""
        from .worktree import clean_detach
        p, cd, done, prev = self.p, self.p.ci_dir, set(), None
        if cd == p.work_dir and any(self.owner.values()):
            self.periodic_ran(e, skipped=True)
            self.log(f"{e['name']}: skipped: the checkout holds a ticket's unfinished work")
            return
        if not self.keep_changes(f"the {e['name']} round"):
            self.periodic_ran(e, skipped=True)
            return
        self.fetch()
        clean_detach(p, cd, p.upstream)
        kept, reply = None, ""  # what the last session that finished leaves; a cut-off one's changes are dropped
        land, last = "", e["name"]  # `land` is inherited along the chain: an entry's own setting wins
        self.handed = set()  # the ticket ids the chain's sessions were given: only those land
        while e and e["name"] not in done and not (p.state / "STOP").exists():
            done.add(e["name"])
            finished = self.groups_finished(e) if e["after_group"] else {}
            seen = list(self.groups_seen.get(e["name"], []))
            self.periodic_ran(e)
            land = e["land"] or land
            got = self.periodic_session(e, cd, prev, finished)
            if got is False:  # skipped before it started (a failed setup): its groups stay owed
                self.groups_seen[e["name"]] = seen
                self.periodic_ran(e, skipped=True)
            if not got:
                break
            reply, prev = got
            kept, last = self.periodic_keep(cd, e["name"]), e["name"]
            e = p.periodic.get(e["then"])
        self.save()
        try:
            if kept and (kept[0] or kept[3] or kept[2][0] != kept[2][1]):
                self.land_periodic(cd, *kept, land or "near", last, reply)
        finally:
            clean_detach(p, cd, p.upstream)

    def periodic_fits(self, e):
        left = self.end() - now()
        if left < timedelta(minutes=e["minutes"]):
            self.log(f"{e['name']}: skipped: {e['minutes']} min don't fit before the deadline")
            return False
        return True

    def periodic_setup(self, e, cd):
        """Run `e`'s setup with no stdin, within `setup_minutes` and so the session still fits before the deadline;
        any stop ends it (nothing is paid for yet). Why it failed, or ""."""
        until = min(now() + timedelta(minutes=e["setup_minutes"]), self.end() - timedelta(minutes=e["minutes"]))
        stopped = lambda: "stopped" if (self.p.state / "STOP").exists() else ""  # noqa: E731
        code, why, lines = run_limited(e["setup"], cd, until, stopped)
        last = lines[0].strip()[:200] if lines else "no output"
        return why or (f"exit {code}: {last}" if code else "")

    def periodic_session(self, e, cd, prev, finished=None):
        """One periodic session: (its final reply, that plus the files it names), False when it was skipped before it
        started (no time, a failed setup, no prompt), or None when it didn't finish. `finished`: the groups that made
        it due, {group: its tickets}, named in its prompt."""
        from .planner import pending_ids
        from .runner import rules
        name, p = e["name"], self.p
        if not self.periodic_fits(e):
            return False
        if e["setup"]:
            self.log(f"{name}: setup `{e['setup']}`")
            failed = self.periodic_setup(e, cd)
            if failed:
                self.log(f"{name}: skipped this round: setup failed ({failed})")
                if failed != "stopped":
                    self.periodic_failed(name, f"its setup `{e['setup']}` failed ({failed}).")
                return False
            if not self.periodic_fits(e):
                return False
        self.fetch()  # its rules as they are on main now: never a copy an earlier session in the chain edited
        own_rules = gout(cd, "show", f"{p.upstream}:{e['prompt']}")
        if not own_rules:
            self.log(f"{name}: skipped: its prompt {e['prompt']} isn't on {p.upstream}")
            self.periodic_failed(name, f"its prompt {e['prompt']} isn't on {p.upstream}.")
            return False
        first = int(re.search(r"\d+", Queue(p).next_id(taken_ids(p, cd) | pending_ids(p))).group())
        ids = [f"{p.prefix}-{n:03d}" for n in range(first, first + 6)]
        self.handed |= set(ids)
        triage = any(x["then"] == name for x in p.periodic.values())
        notes = critic.followups(p) if triage else []
        limit = now() + timedelta(minutes=e["minutes"])
        prompt = "\n\n".join(filter(None, [
            f"Periodic session {name} on {p.upstream} at {gout(cd, 'rev-parse', '--short', 'HEAD')}, after "
            f"{len(self.merged)} merged tickets this run. Today is {now():%Y-%m-%d}. End with the PERIODIC line.",
            finished and "## Finished since the last run: " + "; ".join(
                f"{g} ({len(ts)} ticket{'s' * (len(ts) != 1)}: {', '.join(ts)})" for g, ts in finished.items()),
            prev and f"## The previous session's output\n{prev}",
            notes and "## The critic's follow-up notes since the last plan (ticket, point)\n"
            + "\n".join(f"- ({t}) {b}" for t, b in notes[:60]),
            triage and f"## Existing tickets (id, status, title)\n{Queue(p).listing() or '(none yet)'}",
            f"## Ids for new tickets, in order (at most 6)\n{', '.join(ids)}",
            f"Time limit: {hhmm(limit)}; check with `date`.",
        ]))
        said = f", {len(notes)} critic note{'s' * (len(notes) != 1)}" if triage else ""
        said += f", {', '.join(finished)} finished" if finished else ""
        self.log(f"{name}: periodic session ({e['minutes']} min{said})")
        deny = ["Bash(git commit *)", "Bash(git push *)", "Bash(git switch *)", "Bash(git checkout *)",
                "Bash(git reset *)", "Bash(gh *)"] + ([] if p.ap["subagents"] else ["Agent"])
        system = rules(p, "PERIODIC.md") + "\n" + own_rules
        text, killed, error = self.claude(f"periodic:{name}", f"periodic-{name}", prompt, system, cd, limit,
                                          ["Bash(windvane *)"], deny)
        m = last_match(PERIODIC_RE, text)
        if killed or error or not m:
            why = killed or error or "no PERIODIC line"
            self.log(f"{name}: no result ({why}); its changes are dropped")
            if killed != "stop now":  # the user stopped it: nothing to tell
                what = (f"it was cut off ({killed})" if killed else f"the session failed ({plain_error(error)[:150]})"
                        if error else "it ended without its PERIODIC line")
                self.periodic_failed(name, f"{what}; its changes were dropped.")
            return None
        self.log(f"{name}: PERIODIC: {m.group(1)}")
        push(name, m.group(1)[:300] or "done", 3, "mag")
        if notes:  # used: the next plan (or triage) starts after them
            append_jsonl(p.state / "critic.jsonl", {"ticket": "PLAN", "step": f"periodic:{name}", "notes": len(notes)})
        score = SCORE_RE.search(m.group(1))
        if score:
            append_jsonl(p.state / "ledger.jsonl", {"role": f"periodic:{name}", "event": "score",
                                                    "score": float(score.group(1)), "summary": m.group(1)[:200]})
        return text.strip(), output(text, cd)

    def periodic_failed(self, name, why):
        """One push for a round that didn't finish: overnight, a playtest that stopped working was only in the log."""
        push(f"{name} round failed", f"The {name} round didn't finish: {why}", 3, "warning")

    def periodic_keep(self, cd, name):
        """What may land from the tree as it is: new ticket files under the ids the sessions were given (any other id
        could clash with a plan's), the lines added at the end of existing tickets, and the lines added to QUEUE.md.
        Returns ({path: text}, the new ticket ids, (QUEUE.md as the chain found it, with only the lines added to it),
        {path: the lines added at its end}): the land replays the queue lines and the notes on main."""
        from .planner import changed_paths
        p, notes, logged = self.p, {}, 0
        keep = {f: (cd / f).read_text(errors="replace") for f in changed_paths(cd)
                if f.startswith(p.tickets_rel + "/") and f.endswith(".md") and (cd / f).is_file()
                and p.norm(Path(f).stem) in self.handed and git(cd, "cat-file", "-e", f"HEAD:{f}").returncode}
        for f in changed_paths(cd):
            tid = p.norm(Path(f).stem)
            if f in keep or not (f.startswith(p.tickets_rel + "/") and f.endswith(".md") and tid):
                continue
            if git(cd, "cat-file", "-e", f"HEAD:{f}").returncode:
                continue  # a new file under an id it wasn't given: one of the other paths
            old = gout(cd, "show", f"HEAD:{f}").splitlines()
            new = (cd / f).read_text(errors="replace").splitlines() if (cd / f).is_file() else None
            if new is not None and new[:len(old)] == old and new[len(old):]:
                notes[f] = new[len(old):]
            else:
                logged += 1
                why = "deleted" if new is None else "changed, not only lines added at its end"
                self.log(f"{name}: {tid} {why}: that change is dropped")
        tickets = sorted(p.norm(Path(f).stem) for f in keep)
        old_q = gout(cd, "show", f"HEAD:{p.queue_rel}")
        queue = added_lines(old_q, (cd / p.queue_rel).read_text() if (cd / p.queue_rel).is_file() else old_q)
        other = len(changed_paths(cd)) - len(keep) - len(notes) - logged - (queue != old_q)
        if other:
            self.log(f"{name}: {other} other changed path{'s' * (other != 1)} will be dropped")
        return keep, tickets, (old_q, queue), notes

    def land_periodic(self, cd, keep, tickets, queues, notes, land, name, reply=""):
        """Land the kept tickets on main like a plan, placed in QUEUE.md where `land` says, so the queue picks them up
        (or, held, doesn't). Main may have moved since the chain began (a long session, the phone question): the
        queue lines it added are replayed onto main's QUEUE.md, so nothing pushed meanwhile is undone. If a plan took
        one of the ids meanwhile, the change is made on the commit the chain began on, so the land clashes and keeps
        it on a branch rather than overwrite that ticket. Notes go at the end of each ticket as main has it, unless it
        is done there (merged, or `done:`) or gone. The commit carries `reply` (the last session's, cut to 80 lines),
        so what it found already covered or chose not to ticket stays on record."""
        from .driver import DIR
        from .held import land as land_on_main
        from .worktree import clean_detach
        p = self.p
        base = gout(cd, "rev-parse", "HEAD")
        titles = {t: title_of(keep[f], t) for f in keep for t in [p.norm(Path(f).stem)] if t in tickets}
        if land == "ask" and tickets:
            land = self.ask_land(name, tickets, titles)
        self.fetch()
        clean_detach(p, cd, p.upstream)
        taken = sorted(set(tickets) & taken_ids(p, cd))
        if taken:
            self.log(f"{name}: {', '.join(taken)} taken on {p.upstream} meanwhile; landing from where the chain began")
            clean_detach(p, cd, base)
        q, anchor, main_q = Queue(p), None, gout(cd, "show", f"HEAD:{p.queue_rel}")
        queue = added_lines(*queues, onto=main_q)
        if tickets:
            anchor = q.pick(self.skip)[0] if land == "near" else None
            if anchor:
                titles[anchor] = q.titles.get(anchor, "")
            queue = place(queue, tickets, land, anchor, titles, name, p).rstrip("\n")
        if queue != main_q:
            keep[p.queue_rel] = queue + "\n"
        noted = []
        for f, lines in notes.items():
            tid, now_text = p.norm(Path(f).stem), (cd / f).read_text() if (cd / f).is_file() else None
            if now_text is None or tid in q.done:
                self.log(f"{name}: {tid} is {'gone' if now_text is None else 'done'} on {p.upstream}: "
                         f"its notes are dropped")
                continue
            keep[f] = now_text.rstrip("\n") + "\n" + "\n".join(lines) + "\n"
            noted.append(tid)
        if not (tickets or noted or queue != main_q):
            self.log(f"{name}: nothing left to land")
            return
        for f, text in keep.items():
            (cd / f).parent.mkdir(parents=True, exist_ok=True)
            (cd / f).write_text(text)
        git(cd, "add", "-A", DIR)
        n = len(tickets)
        title = ", ".join(filter(None, [n and f"{n} new ticket{'s' * (n != 1)}",
                                        noted and f"notes on {', '.join(noted)}"])) or "queue lines"
        title = f"Periodic: {title}"
        body = f"Written by the [[periodic]] session {name}; land = {land}."
        if reply:
            body += f"\n\n## Its final reply\n{cut_lines(reply, 80)}"
        ok, where = land_on_main(p, cd, f"periodic-{now():%Y%m%d-%H%M}", title, body)
        how = {"near": f"right after {anchor}" if anchor else "at the top", "next": "at the top",
               "end": "at the end", "hold": "held for the desk"}.get(land, "")
        what = f"{', '.join(tickets)}, {how}" if tickets else "no new tickets"
        how = ", not merged" + ("" if where.startswith("the local branch ") else "; merge it by hand")
        self.log(f"{title} ({what}) on {where}" + ("" if ok else how))
        if land == "hold":
            self.held += tickets
        if not ok:
            push("periodic change not merged", f"{title}: waits on {where}{how.removeprefix(', not merged')}.", 4,
                 "warning")

    def ask_land(self, name, tickets, titles):
        """`land = "ask"`: one phone question, near / end / hold; waits up to `answer_wait` minutes (and never past
        the deadline or a stop). No answer, "later", no phone or a quiet time: hold."""
        p = self.p
        qid = p.qid(re.sub(r"[^a-z0-9-]", "-", name.lower())[:20] + "-land")
        n = len(tickets)
        if quiet_now(p)[0]:
            self.log(f"{name}: quiet time, no question; the tickets are held")
            return "hold"
        which = ticket_list(tickets, titles)
        sent = phone.ask(qid, f"{name} wrote {n} ticket{'s' * (n != 1)}: {which}. Queue them? "
                         f"No answer in {self.a.answer_wait:g} min: hold.", [o for _, o in ASK_LAND])
        if not sent:
            self.log(f"{name}: {'no phone' if sent is None else 'the question did not send'}; the tickets are held")
            return "hold"
        until = min(now() + timedelta(minutes=self.a.answer_wait), self.end())
        while now() < until and not (p.state / "STOP").exists():
            got = phone.answer(qid)
            if got.startswith("ANSWER"):
                letter = (got.split()[1:] or [""])[0][:1].upper()
                land = dict(zip("ABC", (k for k, _ in ASK_LAND))).get(letter, "hold")
                self.log(f"{name}: answered {letter}: {land}")
                return land
            if got == "LATER":
                break
            self.sleep(min(5, max(0.0, (until - now()).total_seconds())))
        self.log(f"{name}: no answer; the tickets are held")
        return "hold"


def natural(text):
    """A sort key that puts M9 before M10."""
    return [int(x) if x.isdigit() else x for x in re.split(r"(\d+)", text)]


def groups(q, pattern):
    """{group: (its tickets by id, how many are still open)} for `after_group` = `pattern`, searched in each ticket's
    header (its first 8 lines); group 1 of the match names the ticket's group. Open means neither merged nor `done:`
    nor `parked:`: a parked ticket waits for the desk and shouldn't hold a finished group back."""
    out = {}
    for t in sorted(q.files, key=natural):
        m = re.search(pattern, "\n".join(q.text[t].splitlines()[:8]), re.M)
        g = m and (m.group(1) or "").strip()
        if g:
            ts, left = out.get(g, ([], 0))
            out[g] = (ts + [t], left + (not q.is_done(t) and t not in q.parked))
    return out


def group_line(e, q, most=5):
    """`windvane queue`'s line for an `after_group` entry: its open groups, the closest to finishing first. Parked
    tickets are named apart: they don't hold the group open, but its session will see them unfinished."""
    found = groups(q, e["after_group"])
    left = sorted(((n, g) for g, (_, n) in found.items() if n), key=lambda x: (x[0], natural(x[1])))
    parked = {g: sum(t in q.parked and not q.is_done(t) for t in found[g][0]) for _, g in left[:most]}
    shown = ", ".join(f"{g} ({n} left" + (f", {parked[g]} parked" if parked[g] else "") + ")"
                      for n, g in left[:most]) or "no group open"
    more = f", and {len(left) - most} more" if len(left) > most else ""
    return f"{e['name']} runs when a group finishes: {shown}{more}"


def place(queue, tickets, land, anchor, titles, name, p):
    """QUEUE.md text with a bullet for each new ticket where `land` puts it, in the order the session listed them
    (then by id). The session's own bullets for them are moved; every other line stays. `near`: after the bullet of
    `anchor` (the ticket in progress), adding one for it at the top when it has none; `next`: before the first bullet;
    `end`: no bullet, so they run after every queued ticket; `hold`: a `parked:` line each."""
    lines, mine = queue.rstrip("\n").split("\n"), {}
    rest = []

    def id_of(line):
        m = re.match(rf"\s*[-*]\s+(?:\*\*)?({p.id_re})\b", line, re.I)
        return m and p.norm(m.group(1))
    for line in lines:
        t = id_of(line)
        if t in tickets:
            mine.setdefault(t, line)
        else:
            rest.append(line)
    order = list(mine) + [t for t in tickets if t not in mine]
    new = [mine.get(t) or f"- {t} {titles.get(t, '')}".rstrip() for t in order]
    if land == "hold":
        rest += [f"- parked: {t} (held by periodic {name}: delete this line to queue it)" for t in order]
    elif land in ("near", "next"):
        top = next((i for i, line in enumerate(rest) if id_of(line)), len(rest))
        at = next((i + 1 for i, line in enumerate(rest) if anchor and id_of(line) == anchor), None)
        if at is None:
            at, new = top, ([f"- {anchor} {titles.get(anchor, '')}".rstrip()] if anchor else []) + new
        rest[at:at] = new
    return "\n".join(rest) + "\n"


def added_lines(old, new, onto=None):
    """`onto` (default `old`) plus the lines `new` added to `old`, each run of them after the last line of `old` before
    it that `onto` still has: a session may add queue lines, never drop or reorder existing ones, nor undo what main
    gained since it began."""
    a, b = old.splitlines(), new.splitlines()
    out = a if onto is None else onto.splitlines()
    kept = {}  # `old`'s line index -> its index in `out`
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, out, autojunk=False).get_opcodes():
        if op == "equal":
            kept.update(zip(range(i1, i2), range(j1, j2)))
    adds = []
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if op in ("insert", "replace"):
            adds.append((next((kept[i] + 1 for i in range(i2 - 1, -1, -1) if i in kept), 0), b[j1:j2]))
    out = list(out)
    for at, lines in reversed(adds):  # from the bottom, so the places above stay right
        out[at:at] = [line for line in lines if not line.strip() or line not in out]
    return "\n".join(out)


def ticket_list(tickets, titles):
    """`T-004 Title; T-005 Title` with whole titles (a cut one reads as a typo) while they fit a push, the rest
    counted: `; and 3 more`."""
    which = []
    for t in tickets:
        if len("; ".join(which)) > phone.MOST // 2:
            which.append(f"and {len(tickets) - len(which)} more")
            break
        which.append(f"{t} {titles.get(t, '')}".strip())
    return "; ".join(which)


def cut_lines(text, n):
    """`text` in at most `n` lines: the head, and the last line (a session's summary), with a marker for the cut."""
    lines = text.splitlines()
    if len(lines) <= n:
        return text
    return "\n".join(lines[:n - 2] + [f"… ({len(lines) - n + 1} lines cut)", lines[-1]])


def taken_ids(p, cd):
    """The ticket ids with a file in `cd`'s tickets folder."""
    here = cd / p.tickets_rel
    return {p.norm(Path(f).stem) for f in os.listdir(here)} - {None} if here.is_dir() else set()


def output(text, cd):
    """A session's final text plus the files it names that exist in `cd` (paths relative to it, or absolute), so a
    `then` session gets a report without being told where to look."""
    out, seen = [text.strip()[-6000:]], set()
    for path in re.findall(r"(?<![\w])((?:/|\.{0,2}/?)[\w.-]+(?:/[\w.-]+)*\.\w+)", text):
        f = (cd / path).resolve() if not path.startswith("/") else Path(path)
        if f in seen or not f.is_file() or len(seen) >= 5:
            continue
        seen.add(f)
        try:
            body = f.read_text()[:12000]
        except (UnicodeDecodeError, OSError):
            body = "(not text: open it yourself)"
        out.append(f"### {path} ({f})\n{body}")
    return "\n\n".join(out)
