"""[[periodic]] sessions: a project's own session (a playtest, an audit) between tickets every `every` merges, or by
hand with `windvane periodic <name>`, on a detached copy of current main.

An entry's `then` runs another right after, given the first one's output; an entry some other names in `then` (a
triage) also gets the critic's "Beyond this ticket" notes no plan or triage has used yet, and the queue, the way
`windvane plan` does. New ticket files under the ids the chain was given, and the lines it added to QUEUE.md, land on
main; every other change is dropped with the tree. Where the new tickets go in the queue is the entry's `land`
(inherited along the chain): `near` right after the ticket in progress, `next` at the top, `end` after everything,
`hold` parked for the desk, `ask` one phone question (no answer in `answer_wait` minutes: hold).

A `score=<n>` on the PERIODIC line goes to the ledger (`event: score`), and `windvane stats --periodic` shows it.
"""
import difflib
import os
import re
import signal
import subprocess
import threading
import time
from collections import deque
from datetime import timedelta
from pathlib import Path

from . import critic, phone
from .questions import quiet_now
from .queue import Queue, title as title_of
from .util import append_jsonl, git, gout, hhmm, last_match, now, push

PERIODIC_RE = re.compile(r"PERIODIC:[ \t]*(.*)")
SCORE_RE = re.compile(r"\bscore=(-?\d+(?:\.\d+)?)\b")
ASK_LAND = (("near", "queue them right after the ticket in progress"), ("end", "queue them after everything else"),
            ("hold", "hold them, parked in QUEUE.md, for you at the desk"))


class Periodic:
    """The Pilot's [[periodic]] part. `periodic_at` (carried across restarts) is each entry's merge count when it last
    ran; `held` the tickets parked for the desk this run, for the final summary."""

    def periodic_due(self):
        """The first [[periodic]] entry whose `every` merges have passed since it last ran this run (restarts carry
        the count), or None."""
        for e in self.p.periodic.values():
            if e["every"] and len(self.merged) - self.periodic_at.get(e["name"], 0) >= e["every"]:
                return e
        return None

    def periodic(self, e):
        """Run `e` and the entries its `then` chains to, one after another in the ci tree on current main, then
        land the tickets they wrote. Whatever else they changed is discarded with the tree."""
        from .driver import clean_detach
        p, cd, done, prev = self.p, self.p.ci_dir, set(), None
        if cd == p.work_dir and any(self.owner.values()):
            self.periodic_at[e["name"]] = len(self.merged)
            self.log(f"{e['name']}: skipped: the checkout holds a ticket's unfinished work")
            return
        self.fetch()
        clean_detach(p, cd, p.upstream)
        kept = None  # what the last session that finished leaves to land; a cut-off one's changes are dropped
        land, last = "", e["name"]  # `land` is inherited along the chain: an entry's own setting wins
        self.handed = set()  # the ticket ids the chain's sessions were given: only those land
        while e and e["name"] not in done and not (p.state / "STOP").exists():
            done.add(e["name"])
            self.periodic_at[e["name"]] = len(self.merged)
            land = e["land"] or land
            prev = self.periodic_session(e, cd, prev)
            if prev is None:
                break
            kept, last = self.periodic_keep(cd, e["name"]), e["name"]
            e = p.periodic.get(e["then"])
        self.save()
        try:
            if kept and kept[0]:
                self.land_periodic(cd, *kept, land or "near", last)
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
        with subprocess.Popen(e["setup"], shell=True, cwd=cd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, start_new_session=True) as proc:
            lines = deque(maxlen=1)
            reader = threading.Thread(target=lambda: lines.extend(proc.stdout), daemon=True)
            reader.start()
            why = ""
            while proc.poll() is None and not why:
                why = ("stopped" if (self.p.state / "STOP").exists() else "out of time" if now() >= until else "")
                if not why:
                    time.sleep(0.2)
            if why:
                try:  # the whole group: a build's own children too
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:  # it ended (and was reaped) as the stop came
                    pass
                proc.wait()
            reader.join(5)
        last = lines[0].decode(errors="replace").strip()[:200] if lines else "no output"
        return why or (f"exit {proc.returncode}: {last}" if proc.returncode else "")

    def periodic_session(self, e, cd, prev):
        """One periodic session; its final text, or None when it was skipped or didn't finish."""
        from .driver import pending_ids, rules
        name, p = e["name"], self.p
        if not self.periodic_fits(e):
            return None
        if e["setup"]:
            self.log(f"{name}: setup `{e['setup']}`")
            failed = self.periodic_setup(e, cd)
            if failed:
                self.log(f"{name}: skipped this round: setup failed ({failed})")
                return None
            if not self.periodic_fits(e):
                return None
        self.fetch()  # its rules as they are on main now: never a copy an earlier session in the chain edited
        own_rules = gout(cd, "show", f"{p.upstream}:{e['prompt']}")
        if not own_rules:
            self.log(f"{name}: skipped: its prompt {e['prompt']} isn't on {p.upstream}")
            return None
        here = cd / p.tickets_rel
        taken = {p.norm(Path(f).stem) for f in os.listdir(here)} - {None} if here.is_dir() else set()
        first = int(re.search(r"\d+", Queue(p).next_id(taken | pending_ids(p))).group())
        ids = [f"{p.prefix}-{n:03d}" for n in range(first, first + 6)]
        self.handed |= set(ids)
        triage = any(x["then"] == name for x in p.periodic.values())
        notes = critic.followups(p) if triage else []
        limit = now() + timedelta(minutes=e["minutes"])
        prompt = "\n\n".join(filter(None, [
            f"Periodic session {name} on {p.upstream} at {gout(cd, 'rev-parse', '--short', 'HEAD')}, after "
            f"{len(self.merged)} merged tickets this run. Today is {now():%Y-%m-%d}. End with the PERIODIC line.",
            prev and f"## The previous session's output\n{prev}",
            notes and "## The critic's follow-up notes since the last plan (ticket, point)\n"
            + "\n".join(f"- ({t}) {b}" for t, b in notes[:60]),
            triage and f"## Existing tickets (id, status, title)\n{Queue(p).listing() or '(none yet)'}",
            f"## Ids for new tickets, in order (at most 6)\n{', '.join(ids)}",
            f"Time limit: {hhmm(limit)}; check with `date`.",
        ]))
        said = f", {len(notes)} critic note{'s' * (len(notes) != 1)}" if triage else ""
        self.log(f"{name}: periodic session ({e['minutes']} min{said})")
        deny = ["Bash(git commit *)", "Bash(git push *)", "Bash(git switch *)", "Bash(git checkout *)",
                "Bash(git reset *)", "Bash(gh *)"] + ([] if p.ap["subagents"] else ["Agent"])
        system = rules(p, "PERIODIC.md") + "\n" + own_rules
        text, killed, error = self.claude(f"periodic:{name}", f"periodic-{name}", prompt, system, cd, limit,
                                          ["Bash(windvane *)"], deny)
        m = last_match(PERIODIC_RE, text)
        if killed or error or not m:
            self.log(f"{name}: no result ({killed or error or 'no PERIODIC line'}); its changes are dropped")
            return None
        self.log(f"{name}: PERIODIC: {m.group(1)}")
        push(f"{p.name}: {name}", m.group(1)[:300] or "done", 3, "mag")
        if notes:  # used: the next plan (or triage) starts after them
            append_jsonl(p.state / "critic.jsonl", {"ticket": "PLAN", "step": f"periodic:{name}", "notes": len(notes)})
        score = SCORE_RE.search(m.group(1))
        if score:
            append_jsonl(p.state / "ledger.jsonl", {"role": f"periodic:{name}", "event": "score",
                                                    "score": float(score.group(1)), "summary": m.group(1)[:200]})
        return output(text, cd)

    def periodic_keep(self, cd, name):
        """What may land from the tree as it is: new ticket files under the ids the sessions were given (any other id
        could clash with a plan's), and QUEUE.md with only the lines added to it. Returns ({path: text}, the new
        ticket ids)."""
        from .driver import changed_paths
        p = self.p
        keep = {f: (cd / f).read_text(errors="replace") for f in changed_paths(cd)
                if f.startswith(p.tickets_rel + "/") and f.endswith(".md") and (cd / f).is_file()
                and p.norm(Path(f).stem) in self.handed and git(cd, "cat-file", "-e", f"HEAD:{f}").returncode}
        tickets = sorted(p.norm(Path(f).stem) for f in keep)
        old_q = gout(cd, "show", f"HEAD:{p.queue_rel}")
        queue = added_lines(old_q, (cd / p.queue_rel).read_text() if (cd / p.queue_rel).is_file() else old_q)
        if queue != old_q:
            keep[p.queue_rel] = queue + "\n"
        other = len(changed_paths(cd)) - len(keep)
        if other:
            self.log(f"{name}: {other} other changed path{'s' * (other != 1)} will be dropped")
        return keep, tickets

    def land_periodic(self, cd, keep, tickets, land, name):
        """Land the kept tickets on main like a plan, placed in QUEUE.md where `land` says, so the queue picks them up
        (or, held, doesn't)."""
        from .driver import DIR, clean_detach, land as land_on_main
        p = self.p
        titles = {t: title_of(keep[f], t) for f in keep for t in [p.norm(Path(f).stem)] if t in tickets}
        if land == "ask" and tickets:
            land = self.ask_land(name, tickets, titles)
        self.fetch()
        clean_detach(p, cd, p.upstream)
        q, anchor = Queue(p), None
        if tickets:
            anchor = q.pick(self.skip)[0] if land == "near" else None
            titles[anchor] = q.titles.get(anchor, "")
            queue = keep.get(p.queue_rel) or gout(cd, "show", f"HEAD:{p.queue_rel}") + "\n"
            keep[p.queue_rel] = place(queue, tickets, land, anchor, titles, name, p)
        for f, text in keep.items():
            (cd / f).parent.mkdir(parents=True, exist_ok=True)
            (cd / f).write_text(text)
        git(cd, "add", "-A", DIR)
        n = len(tickets)
        title = f"Periodic: {n} new ticket{'s' * (n != 1)}"
        ok, where = land_on_main(p, cd, f"periodic-{now():%Y%m%d-%H%M}", title,
                                 f"Written by the [[periodic]] session {name}; land = {land}.")
        how = {"near": f"right after {anchor}" if anchor else "at the top", "next": "at the top",
               "end": "at the end", "hold": "held for the desk"}.get(land, "")
        what = f"{', '.join(tickets)}, {how}" if tickets else "queue lines only"
        self.log(f"{title} ({what}) on {where}" + ("" if ok else ", not merged; merge it by hand"))
        if land == "hold":
            self.held += tickets
        if not ok:
            push(f"{p.name}: tickets not merged", f"{title} wait on {where}: merge it by hand.", 4, "warning")

    def ask_land(self, name, tickets, titles):
        """`land = "ask"`: one phone question, near / end / hold; waits up to `answer_wait` minutes (and never past
        the deadline or a stop). No answer, "later", no phone or a quiet time: hold."""
        p = self.p
        qid = p.qid(re.sub(r"[^a-z0-9-]", "-", name.lower())[:20] + "-land")
        n = len(tickets)
        if quiet_now(p)[0]:
            self.log(f"{name}: quiet time, no question; the tickets are held")
            return "hold"
        which = "; ".join(f"{t} {titles.get(t, '')[:60]}".strip() for t in tickets)
        which = which if len(which) <= 400 else which[:399] + "…"
        sent = phone.ask(qid, f"{p.name}: {name} wrote {n} ticket{'s' * (n != 1)}: {which}. Queue them? "
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


def added_lines(old, new):
    """`new` with every line `old` had kept: a session may add queue lines, never drop or reorder existing ones."""
    a, b, out = old.splitlines(), new.splitlines(), []
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        out += a[i1:i2] if op in ("equal", "delete") else b[j1:j2] if op == "insert" else a[i1:i2] + b[j1:j2]
    return "\n".join(out)


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
