"""The ticket queue, derived from git so nobody maintains a status list. Read from <remote>/<main> and the remote
branches (fetch first).

  tickets  `.windvane/tickets/<ID>.md`; the header (first 8 lines) may hold `Depends on: T-001, T-002.`
  order    bullets in `.windvane/QUEUE.md` (`- T-003 ...` or `- **T-003** ...`) first, in order, then every other
           ticket by number; a bullet naming a split ticket's parent (T-004) stands for its parts (T-004a, T-004b)
  done     a merge on main from its branch (`t-003-...`, the whole branch name: `q-t-003-x` is another branch), or
           `- done: T-003 (why)` in QUEUE.md
  last     `- last: T-003 (why)` in QUEUE.md: starts only once no ticket not marked last can (wip or todo, even if
           only waiting for a running ticket's files); among themselves in the order of these lines
  parked   `- parked: T-003 (why)` in QUEUE.md: never started
  wip      an unmerged remote branch `t-003-...`: resumed before new work, unless no commit touched it for
           `stale_days`: then it waits for its turn in the order like new work
  waiting  a dependency isn't done; a `Depends on:` item that isn't a ticket id (`the user`, `M9`) waits until
           `- done: M9 (why)` in QUEUE.md names it. A wip ticket's own branch may add ticket ids to main's line
  asked    a question for it under `## Open` in QUESTIONS.md, whoever wrote it: blocked until it's answered
  touches  `Touches: windvane/driver.py, prompts/` in the header: tickets whose lists don't overlap may run at once
  related  `Related: T-004.` in the header: the same feature through other files, so never run beside T-004
"""
import re
import time
from pathlib import Path

from .questions import blocks, part, ticket_of, waiting_on
from .util import gout

ANSWERED = "it's answered"  # (a quote can't go in an f-string's braces before Python 3.12)
PEOPLE = ("the user", "user", "you")  # outside items that stand for someone's answer, not a milestone


def bullets(text, files, p):
    out = []
    for raw in re.findall(rf"^\s*[-*]\s+(?:\*\*)?({p.id_re})\b", text, re.M | re.I):
        tid = p.norm(raw)
        kids = sorted(t for t in files if re.fullmatch(rf"{re.escape(tid)}[a-z]", t)) if tid not in files else []
        for t in [tid] if tid in files else kids:
            if t not in out:
                out.append(t)
    return out


def header_line(head, key):
    """The text of a header's `<key>:` line, up to its full stop, a blank line, the next `Key:` line or a ` · `
    (headers that put several keys on one line: `Milestone: M9 · Depends on: nothing · Can start now`). The key counts
    only where one starts, at a line's start or after a ` · ` outside the `#` title and outside backticks, so a title
    or prose quoting `Depends on:` isn't read as one."""
    start = r"(?:^|^(?!#)(?:[^\n`]|`[^\n`]*`)*?[ \t]·[ \t])(?:\*\*)?"
    m = re.search(rf"{start}{key}:(.*?)(?:\.\s|\n\s*\n|\n(?=[\w ]+:)|\s·\s|\Z)", head, re.S | re.I | re.M)
    return m.group(1) if m else ""


def depends_on(head, p, key="Depends on"):
    return [t for t in (p.norm(x) for x in re.findall(p.id_re, header_line(head, key), re.I)) if t]


def outside(head, p):
    """The `Depends on:` items that aren't ticket ids (`the user`, `M9`, free text), without notes in
    brackets. An item naming a ticket (`T-004 (the parser)`) is a ticket dependency, and `nothing` (`nothing open`)
    is none."""
    items = re.split(r",|;|\band\b", re.sub(r"\([^)]*\)", "", header_line(head, "Depends on")))
    items = (" ".join(x.split()).strip(" .`*") for x in items)
    return [x for x in items if x and x.lower().split()[0] not in ("nothing", "none") and not re.search(p.id_re, x, re.I)]


def branch_depends(cwd, ref, path, p):
    """The ticket ids in the `Depends on:` line of a ticket's header as `ref` (its pushed branch) has it: a worker that
    finds its ticket can't go on until another merges says so there. Other words (`the user`) are left out: a
    question to the user is a BLOCKED run, not a dependency."""
    return depends_on("\n".join(gout(cwd, "show", f"{ref}:{path}").splitlines()[:8]), p)


def touches(head):
    """The paths in a ticket header's `Touches: a.py, docs/` line (files or directories), or an empty set: then the
    ticket may touch anything, and the autopilot runs nothing beside it."""
    m = re.search(r"^Touches:(.*)$", head, re.M | re.I)
    return {x.strip().strip("`").rstrip(".") for x in m.group(1).split(",") if x.strip().strip("`.")} if m else set()


def merged_branches(subjects, bp):
    """The ticket ids whose branches merge subjects name, by where a branch name starts in the subjects git and
    GitHub write: `Merge T-012: ... (branch t-012-x)` (the driver's), `Merge pull request #5 from owner/t-012-x`,
    `Merge branch 't-012-x'`, `Merge remote-tracking branch 'origin/t-012-x'`. An id inside another branch's name
    (`q-t-012-scope`, a question branch) isn't that ticket's merge."""
    start = r"(?:\(branch |\bfrom [\w.-]+/|\bbranch '(?:[\w.-]+/)?)"
    return re.findall(rf"{start}({bp}-\d+[a-z]?)-[\w.-]", subjects, re.I)


def title(text, tid):
    first = text.split("\n", 1)[0]
    return re.sub(rf"^#\s*{re.escape(tid)}:?\s*", "", first).strip() or tid


class Queue:
    def __init__(self, p, ref=None):
        self.p = p
        cwd = p.main_root()
        ref = ref or p.upstream
        show = lambda path: gout(cwd, "show", f"{ref}:{path}")
        names = gout(cwd, "ls-tree", "--name-only", ref, p.tickets_rel + "/").split("\n")
        self.files = {}
        for n in filter(None, names):
            tid = p.norm(Path(n).stem)
            if tid and n.endswith(".md"):
                self.files[tid] = n
        self.text = {t: show(f) for t, f in self.files.items()}
        qmd = show(p.queue_rel)
        listed = [t for t in bullets(re.sub(r"^\s*-\s*(done|parked|last):.*$", "", qmd, flags=re.M | re.I),
                                         self.files, p) if t in self.files]
        last = [t for t in bullets("".join(f"- {x}\n" for x in re.findall(r"^\s*-\s*last:(.*)$", qmd, re.M | re.I)),
                                   self.files, p) if t in self.files]
        num = lambda t: (int(re.search(r"\d+", t).group()), t)
        first = [t for t in listed if t not in last]
        self.order = first + sorted((t for t in self.files if t not in first and t not in last), key=num) + last
        self.last = set(last)
        heads = {t: "\n".join(self.text[t].splitlines()[:8]) for t in self.files}
        self.deps = {t: depends_on(heads[t], p) for t in self.files}
        self.outside = {t: outside(heads[t], p) for t in self.files}
        self.related = {t: depends_on(heads[t], p, "Related") for t in self.files}
        self.titles = {t: title(self.text[t], t) for t in self.files}
        self.touches = {t: touches(heads[t]) for t in self.files}

        bp = re.escape(p.prefix.lower())
        merges = gout(cwd, "log", ref, "--merges", "--format=%s")
        self.done = {p.norm(m) for m in merged_branches(merges, bp)}
        self.done |= {p.norm(m) for m in re.findall(rf"^\s*-\s*done:\s*({p.id_re})", qmd, re.M | re.I)}
        self.parked_lines = {p.norm(m): line.strip() for line, m in
                             re.findall(rf"^(\s*-\s*parked:\s*({p.id_re}).*)$", qmd, re.M | re.I)}
        self.parked = set(self.parked_lines)
        self.done_words = {" ".join(m.split()).strip(" .`*").lower()
                           for m in re.findall(r"^\s*-\s*done:\s*([^(\n]+)", qmd, re.M | re.I)}
        self.asked, self.asked_ids = {}, {}
        for t, question, _ in blocks(part(show(p.questions_rel), "Open")):
            if p.norm(t) in self.files:
                self.asked.setdefault(p.norm(t), question)
                self.asked_ids.setdefault(p.norm(t), []).append(t)
        self.done.discard(None)
        self.wip, fresh = {}, set()
        old = time.time() - float(p.ap["stale_days"]) * 86400
        for line in gout(cwd, "branch", "-r", "--no-merged", ref,
                         "--format=%(refname:short) %(committerdate:unix)").splitlines():
            b, _, when = line.partition(" ")
            m = re.fullmatch(rf"{re.escape(p.remote)}/({bp}-\d+[a-z]?)-.+", b, re.I)
            if m and p.norm(m.group(1)) not in self.done:
                tid = p.norm(m.group(1))
                self.wip.setdefault(tid, []).append(b.split("/", 1)[1])
                if int(when or 0) > old:
                    fresh.add(tid)
        self.stale = set(self.wip) - fresh  # every branch untouched for stale_days: the order decides, not "wip"
        self.branch_deps = {}  # ids a wip ticket's branch adds to main's `Depends on:` (it can't drop one)
        for tid, branches in self.wip.items():
            if tid in self.files:
                for b in branches:
                    for d in branch_depends(cwd, f"{p.remote}/{b}", self.files[tid], p):  # a typo'd id: no wait
                        if self.known(d) and d != tid and d not in self.deps[tid] + self.branch_deps.get(tid, []):
                            self.branch_deps.setdefault(tid, []).append(d)

    def known(self, tid):
        """Whether `tid` is a ticket on the ref, or a split ticket's parent."""
        return tid in self.files or any(re.fullmatch(rf"{re.escape(tid)}[a-z]", t) for t in self.files)

    def leads_to(self, start, target):
        """Whether `start` waits, through the tickets it waits on, for `target`: then neither can merge."""
        seen, todo = set(), [start]
        while todo:
            t = todo.pop()
            if t == target:
                return True
            if t not in seen:
                seen.add(t)
                todo += [d for d in self.deps.get(t, []) + self.branch_deps.get(t, []) if not self.is_done(d)]
        return False

    def is_done(self, tid):
        if tid in self.done:
            return True
        kids = [t for t in self.files if re.fullmatch(rf"{re.escape(tid)}[a-z]", t)]
        return bool(kids) and all(k in self.done for k in kids)

    def status(self, tid, blocked=()):
        if tid in self.done:
            return "done"
        if tid in self.parked:
            return "parked"
        if tid in blocked or tid in self.asked:
            return "blocked"
        if self.waits_on(tid):
            return "waiting"
        return "wip" if tid in self.wip else "todo"

    def waits_on(self, tid):
        """What `tid` still waits on: its undone dependencies, ticket ids and outside ones alike, main's and its
        branch's."""
        return ([d for d in self.deps.get(tid, []) + self.branch_deps.get(tid, []) if not self.is_done(d)]
                + [x for x in self.outside.get(tid, []) if x.lower() not in self.done_words])

    def to_run(self, skip=(), blocked=()):
        """The tickets not marked last that could still start (wip or todo, not in `skip`): while one is left, every
        last ticket waits, even when that one only waits for a running ticket's files."""
        return [t for t in self.order
                if t not in self.last and t not in skip and self.status(t, blocked) in ("wip", "todo")]

    def pick(self, skip=(), blocked=()):
        """(ticket, "wip"|"todo", its branches) for the next run, or (None, why)."""
        held = self.last if self.to_run(skip, blocked) else ()  # a wip last ticket isn't resumed ahead of them either
        ready = [(t, self.status(t, blocked)) for t in self.order if t not in skip and t not in held]
        for wants in (("wip",), ("wip", "todo")):
            for t, s in ready:
                if s in wants and (len(wants) == 2 or t not in self.stale):
                    return t, s, self.wip.get(t, [])
        left = sum(s not in ("done", "parked") for _, s in ready)
        return None, (f"{left} tickets left, none ready (questions, dependencies or skipped)" if left
                      else "queue empty")

    def waiting_for(self, tid):
        """The open tickets that wait on `tid` (or on its split parent), on main's `Depends on:` line or their
        branch's: what a set-aside or parked `tid` holds up."""
        on = lambda d: d == tid or re.fullmatch(rf"{re.escape(d)}[a-z]", tid)  # noqa: E731
        return [t for t in self.order if t != tid and t not in self.done and t not in self.parked
                and any(on(d) for d in self.deps.get(t, []) + self.branch_deps.get(t, []))]

    def hints(self, blocked=(), run=None):
        """How to release each wait that won't end by itself, one line per cause naming every ticket waiting on it:
        an outside item no `- done:` line names yet (someone's answer or a milestone), a parked dependency, or one
        the running driver set aside, failed or holds for a question. `run` is the driver's state.json (its
        `aside`, `fixes` and `failed` only while a run is on). {the first ticket waiting on it: [lines]}"""
        run = run or {}
        causes = {}
        for t in self.order:
            if self.status(t, blocked) == "waiting":
                for d in self.waits_on(t):
                    causes.setdefault(d, []).append(t)
        asks = self.asks(run)
        out = {}
        for d, waiters in causes.items():
            line = self.hint(d, waiters, blocked, asks, run)
            if line:
                out.setdefault(waiters[0], []).append(line)
        return out

    def asks(self, run):
        """{ticket: the ids of its questions still waiting for the user}, from the driver's state.json."""
        asks = {}
        for k, q in waiting_on(run.get("questions", {})).items():
            asks.setdefault(self.p.norm(ticket_of(k, q)), []).append(k)
        return asks

    def release(self, d, blocked, run):
        """What lets `d` go, in the words `hints` gives the tickets waiting on it, or "" when nothing holds it."""
        return self.hint(d, [], blocked, self.asks(run), run)

    def hint(self, d, waiters, blocked, asks, run):
        """The line for one cause `d`, or "" when it ends by itself (a ticket that's just not merged yet). With no
        `waiters`, only what holds `d` and what lets it go."""
        rel, aside, fixes = self.p.queue_rel, run.get("aside", {}), run.get("fixes", {})
        who = f"{', '.join(waiters)} wait{'s' * (len(waiters) == 1)}"
        until = lambda end: f": {who} until {end}" if waiters else ""  # noqa: E731
        if d in self.parked:
            return (f"{d} is parked{': ' + who if waiters else ''}; to unpark it, take `{self.parked_lines[d]}` out "
                    f"of {rel}")
        if d in aside:
            fix = fixes.get(d, "").rstrip(".")
            then = (f"so it isn't set aside again, {fix[:1].lower()}{fix[1:]}" if fix
                    else "the next run takes it up again")
            return f"{d} was set aside this run ({aside[d]}){until('it merges')}; {then}"
        if d in run.get("failed", []):
            return (f"{d} failed this run (its `FAILED` line in {self.p.state / 'autopilot.log'} says why)"
                    f"{until('it merges')}; `windvane retry {d}` puts it back in this run, or the next `windvane run` "
                    "takes it up")
        if d in self.files and (d in blocked or d in self.asked):
            ids = asks.get(d) or self.asked_ids.get(d)
            if not ids:  # answered: it waits only for its ticket's next run
                return ""
            named = f" ({', '.join(ids)})" if ids != [d] else ""
            return f"{d} has an open question{named}{until(ANSWERED)}: `windvane answer {ids[0]} <letter>`"
        if self.known(d):
            return ""
        done = f"add `- done: {d} (why)` to {rel}"
        if d.lower() in PEOPLE:
            return f"`{d}`: {who}; once you've answered, {done}"
        where = self.files[waiters[0]] if len(waiters) == 1 else "each of their tickets"
        return f"`{d}` isn't a ticket: {who}; once it's done, {done}, or take it out of `Depends on:` in {where}"

    def warnings(self):
        """Header lines the queue reads differently from how they look: a key indented off its line's start isn't a
        key, and an unclosed backtick hides every key after it on its line."""
        out, keys = [], r"(?:\*\*)?(?:Depends on|Related|Touches):"
        for t in (t for t in self.order if not self.is_done(t)):  # a merged ticket's header no longer matters
            for n, line in enumerate(self.text[t].splitlines()[:8], 1):
                if re.match(rf"\s+{keys}", line, re.I):
                    out.append(f"{self.files[t]} line {n}: `{line.strip()[:60]}` is indented: a key must start its "
                               "line, so it isn't read")
                if (not line.startswith("#") and line.count("`") % 2
                        and re.search(rf"{keys}|[ \t]·[ \t]", line, re.I)):
                    out.append(f"{self.files[t]} line {n}: an unclosed backtick: keys after it on this line aren't "
                               "read")
        return out

    def listing(self, blocked=(), hints=False, run=None):
        """One row per ticket; with `hints`, a line for each wait that needs someone to act, under the first ticket
        waiting on it (`hints`, `run`)."""
        rows, said = [], self.hints(blocked, run) if hints else {}
        for t in self.order:
            s = self.status(t, blocked)
            on = (f"{x} (from its branch)" if x in self.branch_deps.get(t, []) else x for x in self.waits_on(t))
            why = {"waiting": f"waiting on: {', '.join(on)}",
                   "blocked": f"question: {self.asked[t][:60]}" if t in self.asked else ""}.get(s, "")
            rows.append(f"{t:8} {s:8} {'(last) ' * (t in self.last)}{self.titles[t][:70]}"
                        + (f"  [{' '.join(self.wip[t])}{', stale' if t in self.stale else ''}]"
                           if t in self.wip else "") + (f"  ({why})" if why else ""))
            rows += [f"{'':8} hint: {h}" for h in said.get(t, [])]
        return "\n".join(rows)

    def next_id(self, taken=()):
        """The id after every ticket on the ref and every id in `taken` (claimed elsewhere, e.g. an open plan)."""
        nums = [int(re.search(r"\d+", t).group()) for t in [*self.files, *taken]]
        return f"{self.p.prefix}-{max(nums, default=0) + 1:03d}"
