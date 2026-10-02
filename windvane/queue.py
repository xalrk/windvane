"""The ticket queue, derived from git so nobody maintains a status list. Read from <remote>/<main> and the remote
branches (fetch first).

  tickets  `.windvane/tickets/<ID>.md`; the header (first 8 lines) may hold `Depends on: T-001, T-002.`
  order    bullets in `.windvane/QUEUE.md` (`- T-003 ...` or `- **T-003** ...`) first, in order, then every other
           ticket by number; a bullet naming a split ticket's parent (T-004) stands for its parts (T-004a, T-004b)
  done     a merge on main from its branch (`t-003-...`, the whole branch name: `q-t-003-x` is another branch), or
           `- done: T-003 (why)` in QUEUE.md
  parked   `- parked: T-003 (why)` in QUEUE.md: never started
  wip      an unmerged remote branch `t-003-...`: resumed before new work, unless no commit touched it for
           `stale_days`: then it waits for its turn in the order like new work
  waiting  a dependency isn't done
  touches  `Touches: windvane/driver.py, prompts/` in the header: tickets whose lists don't overlap may run at once
  related  `Related: T-004.` in the header: the same feature through other files, so never run beside T-004
"""
import re
import time
from pathlib import Path

from .util import gout


def bullets(text, files, p):
    out = []
    for raw in re.findall(rf"^\s*[-*]\s+(?:\*\*)?({p.id_re})\b", text, re.M | re.I):
        tid = p.norm(raw)
        kids = sorted(t for t in files if re.fullmatch(rf"{re.escape(tid)}[a-z]", t)) if tid not in files else []
        for t in [tid] if tid in files else kids:
            if t not in out:
                out.append(t)
    return out


def depends_on(head, p, key="Depends on"):
    m = re.search(rf"{key}:(.*?)(?:\.\s|\n\s*\n|\Z)", head, re.S | re.I)
    return [t for t in (p.norm(x) for x in re.findall(p.id_re, m.group(1) if m else "", re.I)) if t]


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
        listed = [t for t in bullets(re.sub(r"^\s*-\s*(done|parked):.*$", "", qmd, flags=re.M | re.I),
                                         self.files, p) if t in self.files]
        num = lambda t: (int(re.search(r"\d+", t).group()), t)
        self.order = listed + sorted((t for t in self.files if t not in listed), key=num)
        heads = {t: "\n".join(self.text[t].splitlines()[:8]) for t in self.files}
        self.deps = {t: depends_on(heads[t], p) for t in self.files}
        self.related = {t: depends_on(heads[t], p, "Related") for t in self.files}
        self.titles = {t: title(self.text[t], t) for t in self.files}
        self.touches = {t: touches(heads[t]) for t in self.files}

        bp = re.escape(p.prefix.lower())
        merges = gout(cwd, "log", ref, "--merges", "--format=%s")
        self.done = {p.norm(m) for m in merged_branches(merges, bp)}
        self.done |= {p.norm(m) for m in re.findall(rf"^\s*-\s*done:\s*({p.id_re})", qmd, re.M | re.I)}
        self.parked = {p.norm(m) for m in re.findall(rf"^\s*-\s*parked:\s*({p.id_re})", qmd, re.M | re.I)}
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
        if tid in blocked:
            return "blocked"
        if not all(self.is_done(d) for d in self.deps.get(tid, [])):
            return "waiting"
        return "wip" if tid in self.wip else "todo"

    def pick(self, skip=(), blocked=()):
        """(ticket, "wip"|"todo", its branches) for the next run, or (None, why)."""
        ready = [(t, self.status(t, blocked)) for t in self.order if t not in skip]
        for wants in (("wip",), ("wip", "todo")):
            for t, s in ready:
                if s in wants and (len(wants) == 2 or t not in self.stale):
                    return t, s, self.wip.get(t, [])
        left = sum(s not in ("done", "parked") for _, s in ready)
        return None, (f"{left} tickets left, none ready (questions, dependencies or skipped)" if left
                      else "queue empty")

    def listing(self, blocked=()):
        rows = []
        for t in self.order:
            rows.append(f"{t:8} {self.status(t, blocked):8} {self.titles[t][:70]}"
                        + (f"  [{' '.join(self.wip[t])}{', stale' if t in self.stale else ''}]"
                           if t in self.wip else ""))
        return "\n".join(rows)

    def next_id(self, taken=()):
        """The id after every ticket on the ref and every id in `taken` (claimed elsewhere, e.g. an open plan)."""
        nums = [int(re.search(r"\d+", t).group()) for t in [*self.files, *taken]]
        return f"{self.p.prefix}-{max(nums, default=0) + 1:03d}"
