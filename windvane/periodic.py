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

`windvane periodic <name>` while a run is on asks that run for the round: the ask waits in the state dir (`asked.json`)
until the run's next gap between tickets, which takes it before any other due entry, from the config on main as it is
then, and is dropped when the run ends.

An entry with `builtin = "project-critic"` needs no prompt: it runs the critic on the whole project (`critic.project`),
whose points an open ticket covers land as that ticket's notes, and whose new ones a `then` triage gets.

An entry with `builtin = "bench"` measures windvane itself after changes to what its sessions are told or do: once
tickets touching its `after_touch` paths have merged since the last recorded bench run, it starts `windvane bench
compare <that run's commit> <main>` as a background job (the first time, a baseline: `bench --ref <main>`), and the
run goes on with tickets meanwhile. When the job ends, a regression past the noise rule (`bench.regression`) is
written up as one ticket and one push; anything else is one log line. It never reverts anything.
"""
import difflib
import os
import re
import tomllib
from datetime import datetime, timedelta
from pathlib import Path

from . import bench, budget, critic, jobs, phone, requests
from .config import DIR, exe, periodic as periodic_entries, permissions
from .questions import quiet_now
from .queue import Queue, mend_queue, title as title_of
from .runner import NO_REQUEST, plain_error
from .util import (append_jsonl, edit_json, git, gout, hhmm, last_match, load_json, now, push, read_jsonl, run_limited,
                   save_json)

PERIODIC_RE = re.compile(r"PERIODIC:[ \t]*(.*)")
SCORE_RE = re.compile(r"\bscore=(-?\d+(?:\.\d+)?)\b")
PERIODIC_LINE = "`PERIODIC: <one-line summary of what you found and the tickets you wrote>`"
REASK_TIME = timedelta(minutes=3)  # a re-ask for the end line, past a session that ended at its limit
ASKED = "asked.json"  # the state dir's `windvane periodic <name>` asks a run holds for its next gap: {name: when}
ASK_LAND = (("near", "queue them right after the ticket in progress"), ("end", "queue them after everything else"),
            ("hold", "hold them, parked in QUEUE.md, for you at the desk"))


class Periodic:
    """The Pilot's [[periodic]] part. `periodic_at` and `groups_seen` (both carried across restarts) are each entry's
    merge count and its finished groups when it last ran; `held` the tickets parked for the desk this run, for the
    final summary. `groups_wait` (a skipped round's merge count) isn't carried: after a restart the owed groups are
    due at once, one more try rather than a lost playtest."""

    def periodic_due(self):
        """An entry asked for with `windvane periodic <name>` (`asked_due`), else the first [[periodic]] entry whose
        `every` merges have passed since it last ran this run (restarts carry the count), or with a group finished
        since then, or with requests in the inbox no round this run was given (one that failed waits for the next
        run, never loops), or None."""
        e = self.asked_due()
        if e:
            if e["builtin"] == "bench":  # a regression's ticket first: a new round would overwrite its verdict
                return dict(e, bench="land" if self.bench_due(e) == "land" else "start")
            return e
        for e in self.p.periodic.values():
            if e["builtin"] == "bench":
                due = self.bench_due(e)
                if due:
                    return dict(e, bench=due)
                continue
            if e["every"] and len(self.merged) - self.periodic_at.get(e["name"], 0) >= e["every"]:
                return e
            if e["after_group"] and len(self.merged) > self.groups_wait.get(e["name"], -1) and self.groups_finished(e):
                return e
            if e["requests"] and requests.pending(self.requests_tried):
                return e
        return None

    def asked_due(self):
        """The oldest ask's entry, as main's config has it now (an entry added after the run started works), marked
        `asked`; its chain looks up `then` there too. One skipped as the checkout was busy waits for the next merge,
        as a skipped group does. An ask for a name main's config and the run's both lack is dropped with a push."""
        p = self.p
        for name in asked(p):
            if len(self.merged) <= self.asks_wait.get(name, -1):
                continue
            entries = {**p.periodic, **main_entries(p, log=self.log)}
            if name not in entries:
                unask(p, name)
                self.log(f"{name}: asked for, but no [[periodic]] entry has that name on {p.upstream}: dropped")
                push(f"{name} round not run", f"You asked for the {name} round, but {DIR}/config.toml on "
                     f"{p.upstream} has no [[periodic]] entry {name}: the ask is dropped.", 3, "warning")
                continue
            for role, perms in permissions(p.cfg["permissions"], entries)[0].items():
                p.perms.setdefault(role, perms)  # an entry new on main: its own allow / deny
            self.asked_chain = entries
            return dict(entries[name], asked=True)
        return None

    def asked_taken(self, e, why=""):
        """The asked round `e` ran, or was skipped for a reason the user hears (`why`, pushed once): the ask is gone,
        never tried again."""
        unask(self.p, e["name"])
        self.asks_wait.pop(e["name"], None)
        if why:
            push(f"{e['name']} round not run", f"The {e['name']} round you asked for didn't run: {why}", 3, "warning")

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

    def periodic_busy(self, e):
        """`e` skipped as the checkout is busy: due again after the next merge, an asked one still owed."""
        self.periodic_ran(e, skipped=True)
        if e.get("asked"):
            self.asks_wait[e["name"]] = len(self.merged)

    def periodic(self, e):
        """Run `e` and the entries its `then` chains to, one after another in the ci tree on current main, then
        land the tickets they wrote. Whatever else they changed is discarded with the tree."""
        from .planner import release_ids, snapshot
        from .worktree import clean_detach
        if e["builtin"] == "bench":
            return self.bench_land(e) if e.get("bench") == "land" else self.bench_start(e)
        p, cd, done, prev = self.p, self.p.ci_dir, set(), None
        entries = self.asked_chain if e.get("asked") else p.periodic
        if cd == p.work_dir and any(self.owner.values()):
            self.log(f"{e['name']}: skipped: the checkout holds a ticket's unfinished work")
            return self.periodic_busy(e)
        if not self.keep_changes(f"the {e['name']} round"):
            return self.periodic_busy(e)
        self.fetch()
        clean_detach(p, cd, p.upstream)
        kept, reply = None, ""  # what the last session that finished leaves; a cut-off one's changes are dropped
        land, last = "", e["name"]  # `land` is inherited along the chain: an entry's own setting wins
        self.handed = set()  # the ticket ids the chain's sessions were given: only those land
        self.chain_base = "HEAD"  # the tree the last session that finished left: a dropped one's patch starts there
        self.triaged = []  # (the requests a session was given, its final reply, its name): verdicts after the land
        try:  # the ids its sessions were handed stay claimed until they've landed or been dropped
            while e and e["name"] not in done and not (p.state / "STOP").exists():
                done.add(e["name"])
                finished = self.groups_finished(e) if e["after_group"] else {}
                seen = list(self.groups_seen.get(e["name"], []))
                self.periodic_ran(e)
                land = e["land"] or land
                got = self.periodic_session(e, cd, prev, finished, entries)
                if got is False:  # skipped before it started (a failed setup): its groups stay owed
                    self.groups_seen[e["name"]] = seen
                    self.periodic_ran(e, skipped=True)
                if not got:
                    break
                reply, prev = got
                kept, last = self.periodic_keep(cd, e["name"]), e["name"]
                self.chain_base = snapshot(cd)
                e = entries.get(e["then"])
            self.save()
            try:
                landed, held = [], {}
                if kept and (kept[0] or kept[3] or kept[2][0] != kept[2][1]):
                    landed, held = self.land_periodic(cd, *kept, land or "near", last, reply)
                for reqs, text, name in self.triaged:
                    self.verdicts(reqs, text, landed, held, name)
            finally:
                clean_detach(p, cd, p.upstream)
        finally:
            release_ids(p, "periodic")

    def periodic_fits(self, e):
        left = self.end() - now()
        if left < timedelta(minutes=e["minutes"]):
            self.log(f"{e['name']}: skipped: {e['minutes']} min don't fit before the deadline")
            if e.get("asked"):
                self.asked_taken(e, f"its {e['minutes']} min don't fit before the run's deadline "
                                    f"({hhmm(self.end())}).")
            return False
        return True

    def periodic_setup(self, e, cd):
        """Run `e`'s setup with no stdin, within `setup_minutes` and so the session still fits before the deadline;
        any stop ends it (nothing is paid for yet). Why it failed, or ""."""
        until = min(now() + timedelta(minutes=e["setup_minutes"]), self.end() - timedelta(minutes=e["minutes"]))
        stopped = lambda: "stopped" if (self.p.state / "STOP").exists() else ""  # noqa: E731
        code, why, lines = run_limited(e["setup"], cd, until, stopped)
        last = phone.cut(lines[0], log=self.log) if lines else "no output"
        return why or (f"exit {code}: {last}" if code else "")

    def periodic_session(self, e, cd, prev, finished=None, entries=None):
        """One periodic session: (its final reply, that plus the files it names), False when it was skipped before it
        started (no time, a failed setup, no prompt), or None when it didn't finish. `finished`: the groups that made
        it due, {group: its tickets}, named in its prompt; `entries`: the chain's (main's for an asked round). An
        asked round's ask is taken once its session starts or is skipped for good (`asked_taken`)."""
        from .planner import end_reask, files, keep_dropped, pick_ids, reapply, rejected_part
        from .runner import rules
        name, p = e["name"], self.p
        entries = entries or p.periodic
        if not self.periodic_fits(e):
            return False
        if e.get("asked"):  # whatever comes next, it was this ask's round: a failed setup is pushed once below
            self.asked_taken(e)
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
        if e["builtin"]:
            return self.project_critic(e, cd)
        reqs = requests.pending(self.requests_tried)[:requests.PER_ROUND] if e["requests"] else []
        if e["requests"] and not reqs:
            self.log(f"{name}: skipped: no untriaged requests")
            return False
        self.requests_tried |= {f.stem for f in reqs}
        self.fetch()  # its rules as they are on main now: never a copy an earlier session in the chain edited
        own_rules = (gout(cd, "show", f"{p.upstream}:{e['prompt']}") if e["prompt"]
                     else rules(p, "TRIAGE-REQUESTS.md"))
        if not own_rules:
            self.log(f"{name}: skipped: its prompt {e['prompt']} isn't on {p.upstream}")
            self.periodic_failed(name, f"its prompt {e['prompt']} isn't on {p.upstream}.")
            return False
        # the chain's earlier ones: in cd, and still claimed with the new ones
        ids = pick_ids(p, "periodic", 6, cd, own="periodic", keep=self.handed)
        self.handed |= set(ids)
        triage = any(x["then"] == name for x in entries.values())
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
            triage and "## Existing tickets (id, status, title)\n"
            + (Queue(p).listing(compact=True, keep=self.merged) or "(none yet)"),
            rejected_part(cd),  # so an idea the bench turned down isn't ticketed again unchanged
            f"## Ids for new tickets, in order (at most 6)\n{', '.join(ids)}",
            f"Time limit: {hhmm(limit)}; check with `date`.",
            reqs and requests.prompt_part(reqs),
        ]))
        said = f", {len(notes)} critic note{'s' * (len(notes) != 1)}" if triage else ""
        said += f", {', '.join(finished)} finished" if finished else ""
        said += f", requests {', '.join(f.stem for f in reqs)}" if reqs else ""
        self.log(f"{name}: periodic session ({e['minutes']} min{said})")
        deny = ["Bash(git commit *)", "Bash(git push *)", "Bash(git switch *)", "Bash(git checkout *)",
                "Bash(git reset *)", "Bash(gh *)", NO_REQUEST] + ([] if p.ap["subagents"] else ["Agent"])
        system = rules(p, "PERIODIC.md") + "\n" + own_rules
        session = {}
        text, killed, error = self.claude(f"periodic:{name}", f"periodic-{name}", prompt, system, cd, limit,
                                          ["Bash(windvane *)"], deny, session=session)
        m, said = last_match(PERIODIC_RE, text), text
        if not (killed or error or m) and session.get("id"):  # ended well but without its line: asked once, tree as is
            more = " (your `REQUEST:` lines first)" if reqs else ""
            asked = end_reask(PERIODIC_LINE, more)  # cut off or failed, it's said as such below
            again, killed, error = self.claude(f"periodic:{name}", f"periodic-{name}-reask", asked, system, cd,
                                               max(limit, now() + REASK_TIME), deny=critic.DENY, resume=session["id"])
            m = last_match(PERIODIC_RE, again)
            if m:  # the reply stays the first one's; a verdict the re-ask repeats wins over its first one
                said = f"{again}\n{text}"
                text = f"{text.rstrip()}\n{m.group(0)}"
        if killed or error or not m:
            why = killed or error or "no PERIODIC line"
            n, kept = keep_dropped(p, cd, name, self.chain_base)
            self.log(f"{name}: {why}; dropped {files(n)} (kept in {kept})" if kept
                     else f"{name}: no result ({why}); its changes are dropped")
            if killed != "stop now":  # the user stopped it: nothing to tell
                what = (f"it was cut off ({killed})" if killed else f"the session failed ({phone.cut(plain_error(error), log=self.log)})"
                        if error else "it ended without its PERIODIC line")
                self.periodic_failed(name, f"{what}; its changes were dropped" + (
                    f" ({files(n)}): {reapply(kept)}" if kept else "."))
            return None
        self.log(f"{name}: PERIODIC: {m.group(1)}")
        push(name, phone.cut(m.group(1), log=self.log) or "done", 3, "mag")
        if notes:  # used: the next plan (or triage) starts after them
            append_jsonl(p.state / "critic.jsonl", {"ticket": "PLAN", "step": f"periodic:{name}", "notes": len(notes)})
        if reqs:
            self.triaged.append((reqs, said, name))
        score = SCORE_RE.search(m.group(1))
        if score:
            append_jsonl(p.state / "ledger.jsonl", {"role": f"periodic:{name}", "event": "score",
                                                    "score": float(score.group(1)), "summary": phone.cut(m.group(1))})
        return text.strip(), output(text, cd)

    def project_critic(self, e, cd):
        """The `project-critic` preset: the critic on the project in `cd`, one session per dimension. Its report
        is the session's output for a `then`; each point an open ticket covers is added at the end of that ticket,
        where `periodic_keep` finds it as a note."""
        p, name = self.p, e["name"]
        limit = now() + timedelta(minutes=e["minutes"])
        self.log(f"{name}: the project critic ({', '.join(e['dims']) or 'every dimension'}, "
                 f"up to {e['parallel']} at once, {e['minutes']} min)")
        entry, report = critic.project(p, cd, lambda role, label, prompt, system, allow, deny: self.claude(
            role, label, prompt, system, cd, limit, allow, deny), e["dims"], e["parallel"])
        if not entry or not entry["dims"]:
            self.log(f"{name}: no result ({phone.cut(report.splitlines()[-1] if report else 'no report')})")
            if not (p.state / "STOP").exists():
                self.periodic_failed(name, "the critic gave no verdict on any dimension.")
            return None
        head = report.splitlines()[0]
        self.log(f"{name}: {head}")
        push(name, phone.cut(head, log=self.log), 3, "mag")
        for tid, points in entry["covered"].items():
            f = cd / p.tickets_rel / f"{tid}.md"
            if f.is_file():
                text = f.read_text()
                f.write_text(text + "\n" * (not text.endswith("\n")) + f"\n## From the project critic ({now():%Y-%m-%d})\n"
                             + "".join(f"- {b}\n" for b in points))
        return report, report

    def verdicts(self, reqs, text, landed, held, name):
        """Write a triage session's `REQUEST:` verdicts into its requests (an accept only for a ticket that landed or
        exists on main, or waits on a branch: this land's `held` ones, or a plan's), log each, and push each question,
        under its request's title: the requests' answers come from the user."""
        from .planner import pending_branches
        self.fetch()
        q, titles = Queue(self.p), {f.stem: requests.fields(requests.read(f))[0] for f in reqs}
        done = requests.apply(reqs, text, set(landed), lambda t: t in q.files, self.log,
                              {**pending_branches(self.p), **held})
        for rid, (kind, word) in done.items():
            self.log(f"{name}: {rid} {word}")
            if kind == "question":  # not a phone question: a bare letter would answer the newest notify-ask instead
                push(f"windvane request {rid} asks", f"Request: {phone.cut(titles[rid], log=self.log)}\n"
                     f"Question: {phone.cut(word.removeprefix('question: '), log=self.log)}\n"
                     f"Not answerable from the phone: {requests.how_to_answer(rid)}", 3, "question")
        left = len(reqs) - len(done)
        if left:
            self.log(f"{name}: {left} request{'s' * (left != 1)} got no verdict: "
                     f"{'they wait' if left != 1 else 'it waits'} for the next run")

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
        mend_queue(p, cd)
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
        so what it found already covered or chose not to ticket stays on record. Returns (the tickets that reached
        main, {ticket: the branch it waits on} when the land stayed on one): only those may answer a request."""
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
            return [], {}
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
        from .planner import branch_name
        branch = branch_name(p, cd, "periodic")
        with self.critical():  # a signal waits until the land is pushed
            ok, where = landed = land_on_main(p, cd, branch, title, body, fresh=True)
        branch = landed.branch
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
        return (tickets, {}) if ok else ([], dict.fromkeys(tickets, branch))

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
        while now() < until and not (p.state / "STOP").exists() and not (self.signalled or self.kill_all):
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

    def bench_due(self, e):
        """A bench entry's turn: "land" once its finished job found a regression (a ticket to write, in a quiet gap),
        "start" when tickets touching its `after_touch` paths merged since the windvane last benchmarked (or since a
        round that ran to its end and left nothing to compare: it waits for a new change; one cut off by a run's end
        stays due), or nothing is recorded yet (a baseline); else None. A finished job's verdict is read here, once.
        Main's head is looked at only when it moved."""
        p, name = self.p, e["name"]
        st = load_json(bench_file(p, name), {})
        if st.get("job") and jobs.alive(Path(st["job"])):
            return None
        if st.get("job") and not st.get("verdict"):
            st = self.bench_verdict(e, st)
        if st.get("verdict") == "regression":
            return "land" if len(self.merged) > self.bench_wait.get(name, -1) else None
        head = gout(p.main_root(), "rev-parse", p.upstream)
        if not (e["after_touch"] and head):
            return None
        if self.bench_seen.get(name, ("",))[0] != head:
            base = last_benched(p, head)
            since = newer(p, base, st.get("head") if st.get("verdict") == "failed" else None, head)
            due = (since is None or since != head and bool(touching(p, since, head, e["after_touch"]))) and "start"
            self.bench_seen[name] = (head, due or None)
        return self.bench_seen[name][1]

    def bench_start(self, e):
        """Start the bench on main as it is now, in the background: a compare with the windvane last benchmarked,
        sized by the weekly usage (`bench.size`), with the run's caps, its `minutes` shared out among the runs to do.
        Not over the weekly cap, after a stop, or when its `minutes` don't fit before the deadline."""
        p, name = self.p, e["name"]
        self.fetch()
        head = gout(p.main_root(), "rev-parse", p.upstream)
        self.bench_seen[name] = (head, None)  # tried at this head, started or not: due again after a new change
        base, week = last_benched(p, head), self.usage.week
        st = load_json(bench_file(p, name), {})
        why = ("stopped" if (p.state / "STOP").exists() else
               f"weekly usage at {week:g}% (cap {self.a.week_cap:g}%)" if week is not None and week >= self.a.week_cap
               else "a bench round is running already" if st.get("job") and jobs.alive(Path(st["job"]))
               else "the last round's regression is still to be ticketed" if st.get("verdict") == "regression"
               else f"{head[:7]} is the windvane last benchmarked: nothing to compare" if base == head else "")
        if why or not self.periodic_fits(e):
            if why:
                self.log(f"{name}: bench not started: {why}")
                if e.get("asked"):
                    self.asked_taken(e, f"{why}.")
            return
        which, said = bench.size(False, False, bench.week_now() if week is None else week, bench.week_limit())
        todo = 1 if base is None else 1 + (not bench_runs(p, base, which))
        held, _ = self.budget_holds(name, which, todo)
        if held:
            self.log(f"{name}: bench not started: {budget.MARK}: {held}")
            if e.get("asked"):
                self.asked_taken(e, f"{budget.MARK}: {held}.")
            return
        hours = (e["minutes"] - todo * bench.JUDGE_MINUTES) / 60 / todo
        if hours < 0.25:
            why = f"{e['minutes']} min leave under 15 min for each of its {todo} runs"
            self.log(f"{name}: bench not started: {why}")
            if e.get("asked"):
                self.asked_taken(e, f"{why}.")
            return
        args = (["--ref", head] if base is None else ["compare", base, head]) + [
            f"--{which}", "--hours", f"{hours:.3g}", "--cap", f"{self.a.cap:g}", "--week-cap", f"{self.a.week_cap:g}"]
        touched = touching(p, base, head, e["after_touch"]) if base else []
        job, after = bench_job(p, name), f"{datetime.now():%Y%m%d-%H%M%S}"  # its runs' ids are their start times
        try:
            jobs.bg(p, job.name, bench_command(p, args), p.main_root(), quiet=True)
        except SystemExit as err:  # `bg` refuses with an exit; the driver goes on
            self.log(f"{name}: bench not started: {err}")
            if e.get("asked"):
                self.asked_taken(e)  # its failure is pushed just below
            return self.periodic_failed(name, f"the bench didn't start: {err}")
        if e.get("asked"):
            self.asked_taken(e)
        save_json(bench_file(p, name), {"job": str(job), "base": base, "head": head, "size": which, "after": after,
                                        "touched": touched})
        what = (f"a baseline at {head[:7]}" if base is None else f"{base[:7]} against {head[:7]}, after "
                + ", ".join(t for t, *_ in touched) if touched else f"{base[:7]} against {head[:7]}")
        self.log(f"{name}: bench started in the background ({what}; {which}: {said}; up to {e['minutes']} min; "
                 f"the run waits for it before it ends); log {job}.log")

    def bench_verdict(self, e, st):
        """The ended job's verdict, saved with the round: a baseline or no regression is one log line, a regression
        waits for a quiet gap to be ticketed (`bench_land`), a job that left nothing to compare is one push."""
        p, name = self.p, e["name"]
        head = bench_runs(p, st["head"], st["size"], st["after"])
        base = st["base"] and bench_runs(p, st["base"], st["size"])
        both = [{"name": side, "sha": st[k], "runs": rs[-1:]}
                for k, side, rs in (("base", "last benchmarked", base), ("head", "main", head)) if rs]
        why, moved = bench.regression(both, e["bench_cost_up"]) if len(both) == 2 else (None, [])
        if not head or st["base"] and why is None:
            exit_f = Path(f"{st['job']}.exit")  # none when the job died with its wrapper (a reboot, the OOM killer)
            code = jobs.exit_code(exit_f, 0) if exit_f.exists() else "?"
            lack = (f"no run of {st['head'][:7]}" if not head else f"no run of {st['base'][:7]}" if not base
                    else "no seed counted on both sides")
            self.log(f"{name}: bench ended (exit {code}) with {lack} to compare; log {st['job']}.log")
            self.periodic_failed(name, f"the bench ended (exit {code}) with {lack} to compare; log {st['job']}.log. "
                                 f"It runs again after the next change to its paths, or now with `windvane periodic "
                                 f"{name}`.")
            st["verdict"] = "failed"
        elif not st["base"]:
            self.log(f"{name}: bench baseline recorded at {st['head'][:7]}: {bench.summary(head[-1])}")
            st["verdict"] = "baseline"
        elif why:
            st.update(verdict="regression", why=why, moved=moved, numbers=bench.headline(both),
                      table=bench.compare_table(both, st["size"]))
            self.log(f"{name}: bench regression, {st['base'][:7]} → {st['head'][:7]}: {'; '.join(why)} "
                     f"({st['numbers']}); a ticket follows at the next quiet gap")
        else:
            self.log(f"{name}: bench {st['base'][:7]} → {st['head'][:7]}: no regression ({bench.headline(both)})")
            st["verdict"] = "flat"
        save_json(bench_file(p, name), st)
        return st

    def bench_land(self, e):
        """Write the regression up as one ticket, landed where the entry's `land` says (near by default), and one
        push. Nothing is reverted: the ticket names what merged in between, both sides' numbers and the seeds that
        moved, for a worker to find the cause."""
        from .planner import pick_ids, release_ids
        from .worktree import clean_detach
        p, cd, name = self.p, self.p.ci_dir, e["name"]
        if cd == p.work_dir and any(self.owner.values()) or not self.keep_changes(f"the {name} round"):
            self.log(f"{name}: the bench regression's ticket waits for the next gap: the checkout is busy")
            self.bench_wait[name] = len(self.merged)
            return
        st = load_json(bench_file(p, name), {})
        self.fetch()
        clean_detach(p, cd, p.upstream)
        try:
            tid = pick_ids(p, "periodic", 1, cd, own="periodic")[0]
            self.handed = {tid}
            old_q = gout(cd, "show", f"HEAD:{p.queue_rel}")
            try:
                landed, held = self.land_periodic(cd, {f"{p.tickets_rel}/{tid}.md": regression_ticket(tid, st)}, [tid],
                                                  (old_q, old_q), {}, e["land"] or "near", name, st["numbers"])
            finally:
                clean_detach(p, cd, p.upstream)
        finally:
            release_ids(p, "periodic")
        save_json(bench_file(p, name), dict(st, verdict="ticketed", ticket=tid))
        where = "queued" if landed else f"on {held[tid]}, not merged" if tid in held else "not landed"
        push(f"{name}: regression", phone.cut(f"{'; '.join(st['why'])} after {after_list(st)}. {tid} written "
                                                    f"({where}); nothing was reverted.", log=self.log), 4, "warning")

    def bench_waits(self):
        """The idle lines of a run whose queue is done while a bench round runs: it waits for the verdict (and a
        regression's ticket) rather than end and throw the round away; the deadline still ends it."""
        return [f"waiting for the {name} bench round to end (started {datetime.fromtimestamp(started):%H:%M}), "
                f"at the latest until the deadline ({hhmm(self.end())})" for name, _, started in live_benches(self.p)]

    def bench_unread(self):
        """Whether a bench entry's round has ended with its verdict not yet read, or a regression's ticket not yet
        written (and not put off by a busy checkout): a run about to end passes once more, so a bench that ended
        during its last pass isn't left for the next run. Reading always gives a verdict, so this can't loop."""
        for name, e in self.p.periodic.items():
            st = load_json(bench_file(self.p, name), {}) if e["builtin"] == "bench" else {}
            if st.get("job") and not jobs.alive(Path(st["job"])) and (
                    not st.get("verdict") or st["verdict"] == "regression"
                    and len(self.merged) > self.bench_wait.get(name, -1)):
                return True
        return False

    def bench_at_end(self):
        """The run's end (a stop, the deadline) ends every bench round still running, its own or one a desk's
        `windvane periodic` or a run before a restart started: no run would be left to read its verdict. One push
        each; the round stays due, so the next run starts it again."""
        for name, st, _ in live_benches(self.p):
            if not jobs.end_job(Path(st["job"]), f"windvane: bench ended with the run ({self.stop_reason})"):
                continue
            save_json(bench_file(self.p, name), dict(st, verdict="ended"))
            what = f"{st['base'][:7]} against {st['head'][:7]}" if st["base"] else f"a baseline at {st['head'][:7]}"
            self.log(f"{name}: bench ended with the run ({self.stop_reason}) before it finished; nothing compared")
            push(f"{name} round cut off", f"The bench round ({what}) was ended with the run ({self.stop_reason}) "
                 "before it finished: nothing was compared. The next run starts it again.", 3, "warning")


def asked(p):
    """The names asked for with `windvane periodic <name>` while a run was on, oldest first."""
    got = load_json(p.state / ASKED, {})
    return sorted(got, key=lambda n: str(got[n]))


def ask(p, name):
    """Hold `name` for the running autopilot's next gap. False when it was asked for already (it stays asked once)."""
    return edit_json(p.state / ASKED, lambda d: name not in d and not d.update({name: now().isoformat()}), {})


def unask(p, name):
    """Take back the ask for `name`; False when there was none."""
    return edit_json(p.state / ASKED, lambda d: d.pop(name, None) is not None, {})


def clear_asked(p):
    """Drop every ask (a run ended, or a fresh one starts): the names dropped."""
    def drop(d):
        names = list(d)
        d.clear()
        return names
    return edit_json(p.state / ASKED, drop, {})


def main_entries(p, log=None):
    """The [[periodic]] entries in the config on main as last fetched (a push from the checkout updates it), or {}
    when it has none or they don't parse (`log` says why)."""
    text = gout(p.main_root(), "show", f"{p.upstream}:{DIR}/config.toml")
    try:
        return periodic_entries(tomllib.loads(text).get("periodic", [])) if text else {}
    except (tomllib.TOMLDecodeError, SystemExit) as e:
        if log:
            log(f"the [[periodic]] entries on {p.upstream} don't read: {e}")
        return {}


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
        if len("; ".join(which).encode()) > phone.MOST // 2:
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


def bench_file(p, name):
    """A bench entry's round, carried across restarts: its job, the commits it compares, then its verdict."""
    return p.state / f"bench-{name}.json"


def bench_job(p, name):
    """The background job a bench entry starts: `windvane await periodic-<name>` follows it from the desk."""
    return p.state / "bg" / f"periodic-{name}"


def bench_command(p, args):
    """The command a bench round runs: this windvane's `bench`, on the project's own checkout."""
    return [*exe(), "-C", str(p.main_root()), "bench", *args]


def bench_runs(p, sha, size, after=""):
    """The recorded bench runs of `sha` at `size` a compare would reuse (its seeds, no judge failed), from run id
    `after` on."""
    want = bench.planned(p, sha, size)
    return [rs for rs in bench.runs_of(p).values() if rs[0].get("sha") == sha and rs[0].get("size") == size
            and str(rs[0].get("run")) >= after and sorted(r["seed"] for r in rs) == want
            and not any(map(bench.judge_failed, rs))]


def last_benched(p, head):
    """The newest commit in `head`'s history with a recorded bench run (the windvane last benchmarked), or None."""
    shas = {r.get("sha") for r in read_jsonl(bench.results(p))} - {None}
    for sha in gout(p.main_root(), "rev-list", head).split() if shas else ():
        if sha in shas:
            return sha
    return None


def newer(p, a, b, head):
    """The later of commits `a` and `b` (either may be None) in `head`'s history; one not in it doesn't count."""
    on = [c for c in (a, b) if c and git(p.main_root(), "merge-base", "--is-ancestor", c, head).returncode == 0]
    if len(on) == 2 and git(p.main_root(), "merge-base", "--is-ancestor", on[0], on[1]).returncode == 0:
        return on[1]
    return on[0] if on else None


def touching(p, since, head, prefixes):
    """[[ticket (or short sha), subject, [paths]]] for each change on main's first-parent line after `since` (a
    ticket's merge, or a commit pushed to main) that touched a path under one of `prefixes`, oldest first."""
    text = gout(p.main_root(), "log", "--first-parent", "-m", "--name-only", "--reverse", "--format=%x01%H%x00%s",
                f"{since}..{head}")
    out = []
    for chunk in text.split("\x01")[1:]:
        top, *paths = chunk.strip("\n").split("\n")
        sha, subject = top.split("\x00", 1)
        hit = [f for f in paths if f and any(f.startswith(x) for x in prefixes)]
        if hit:
            m = re.search(rf"\b{p.id_re}\b", subject, re.I)
            if m:  # the driver's merge subject, `Merge T-007: <title> (branch ...) [skip ci]`, as its title
                subject = re.sub(r"\s*\(branch [^)]*\)|\s*\[skip ci\]", "",
                                 re.sub(rf"(?i)^Merge {re.escape(m.group(0))}:\s*", "", subject))
            out.append([p.norm(m.group(0)) if m else sha[:7], subject, hit])
    return out


def after_list(st, most=4):
    """`T-277, T-278 and 3 more`: the changes a bench round came after, or its commit."""
    names = [t for t, *_ in st.get("touched", [])]
    return (", ".join(names[:most]) + (f" and {len(names) - most} more" if len(names) > most else "")
            or st["head"][:7])


def regression_ticket(tid, st):
    """The ticket a bench regression writes: what got worse, what merged in between, both sides' numbers."""
    base, head = st["base"][:7], st["head"][:7]
    merged = [f"- {t}: {subject} ({', '.join(paths[:5])}{', …' * (len(paths) > 5)})"
              for t, subject, paths in st.get("touched", [])]
    return "\n".join([
        f"# {tid}: Bench regression after {after_list(st)}", "Depends on: nothing.", "",
        "## Goal",
        f"The bench of main at {head} did worse than at {base}, the windvane last benchmarked: {'; '.join(st['why'])}."
        " Find which change in between caused it and fix it, or show why it is worth its cost. Nothing was reverted.",
        "", "## Merged in between", *(merged or ["- (none recorded)"]), "",
        f"## The numbers: `windvane bench compare {base} {head}` ({st['size']})", "```", st["table"], "```", "",
        "## Seeds that moved", *([f"- {m}" for m in st["moved"]] or ["- none alone: the difference is in the total"]),
        "", "## Done when",
        f"- `windvane bench compare {base} HEAD` shows no regression past the noise, or the PR shows why the new cost "
        "is worth it.", ""])


def live_benches(p):
    """[(entry name, its round, when its job started)] for each bench round whose job still runs."""
    out = []
    for f in sorted(p.state.glob("bench-*.json")):
        st = load_json(f, {})
        if st.get("job") and jobs.alive(Path(st["job"])):
            try:
                started = float(Path(st["job"] + ".start").read_text())
            except (OSError, ValueError):
                started = f.stat().st_mtime
            out.append((f.stem.removeprefix("bench-"), st, started))
    return out


def bench_status(p):
    """`windvane status`'s lines on the bench rounds running in the background."""
    out = []
    for name, st, started in live_benches(p):
        what = f"{st['base'][:7]} against {st['head'][:7]}" if st["base"] else f"a baseline at {st['head'][:7]}"
        out.append(f"bench: periodic {name} running since {datetime.fromtimestamp(started):%H:%M} ({what}); "
                   f"`windvane await periodic-{name}` follows it")
    return out
