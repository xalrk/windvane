"""Merge conflicts in append-only numbered files (a decision log, ADRs, migrations), resolved without a worker run.

Two branches that each append an entry to such a file conflict, and both took the next number. `[[append_only]]` in
config.toml names the file, the line that starts an entry (group 1 its number) and how other lines refer to entry n.
When a merge conflicts only in those files, and on both sides every conflict hunk only adds whole entries, main's
entries stay, the branch's follow them, and the branch's colliding numbers move past main's. References are renumbered
only on lines the branch itself added (in any file): main's lines mean main's entries. Anything else: None, and the
caller bounces the ticket as before.
"""
import re
from pathlib import Path

from .util import git, gout

MARKS = ("<<<<<<< ", "||||||| ", "=======", ">>>>>>> ")


def rules(entries):
    """`[[append_only]]` entries, checked at load: a bad pattern stops the run at once, not at the first conflict."""
    out = []
    for e in entries if isinstance(entries, list) else [entries]:
        f, entry, ref = e.get("file"), e.get("entry"), e.get("ref")
        if not (f and entry and ref):
            raise SystemExit("windvane: [[append_only]] needs file, entry and ref (e.g. ref = 'D-{n}')")
        try:
            entry_re = re.compile(entry)
        except re.error as err:
            raise SystemExit(f"windvane: [[append_only]] {f}: entry = {entry!r}: {err}")
        if entry_re.groups < 1 or ref.count("{n}") != 1:
            raise SystemExit(f"windvane: [[append_only]] {f}: entry needs a (number) group and ref one {{n}}")
        before, after = ref.split("{n}")
        out.append({"file": f, "entry": entry_re, "ref": ref,
                    "ref_re": re.compile(("(?<!\\w)" if re.match(r"\w", before) else "") + re.escape(before)
                                         + r"(\d+)(?!\d)" + re.escape(after))})
    return out


def numbers(rule, text):
    out = []
    for line in text.splitlines():
        m = rule["entry"].search(line)
        if m and m.group(1) and m.group(1).isdigit():
            out.append(int(m.group(1)))
    return out


def hunks(text):
    """The file with diff3 conflict markers, as a list of plain lines and (ours, base, theirs) hunks; None when the
    markers don't parse (nested, or text that looks like one)."""
    out, cur, part = [], None, 0
    for line in text.splitlines(keepends=True):
        bare = line.rstrip("\n")
        mark = next((i for i, m in enumerate(MARKS) if bare == m.strip() or m.endswith(" ") and line.startswith(m)),
                    None)
        if cur is None:
            if mark == 0:
                cur, part = ([], [], []), 0
            else:
                out.append(line)
        elif mark is None:
            cur[part].append(line)
        elif mark == part + 1 and mark < 3:
            part = mark
        elif mark == 3 and part == 2:
            out.append(cur)
            cur = None
        else:
            return None
    return None if cur is not None else out


def whole_entries(rule, lines):
    """A side adds whole entries: something, and its first non-blank line starts an entry."""
    body = [l for l in lines if l.strip()]
    return bool(body) and bool(rule["entry"].search(body[0]))


def renumber_line(line, mapping, rule, entry=False):
    """`line` with its references to the entries in `mapping` renumbered, in one pass (D-5 → D-6 and D-6 → D-7 don't
    chain), keeping zero padding; on an entry line, also the entry's own number."""
    spans = []
    m = rule["entry"].search(line) if entry else None
    if m and m.group(1):
        spans.append(m.span(1))
    for r in rule["ref_re"].finditer(line):
        if not any(a < r.end(1) and r.start(1) < b for a, b in spans):
            spans.append(r.span(1))
    for a, b in sorted(spans, reverse=True):
        old = line[a:b]
        if old.isdigit() and int(old) in mapping:
            line = line[:a] + str(mapping[int(old)]).zfill(len(old)) + line[b:]
    return line


def added_lines(cd):
    """Per file, the lines the branch (MERGE_HEAD) added since it left main."""
    base = gout(cd, "merge-base", "HEAD", "MERGE_HEAD")
    out, f = {}, None
    for line in gout(cd, "diff", "-U0", "--no-color", "--no-renames", base, "MERGE_HEAD").splitlines():
        if line.startswith("+++ "):
            f = line[6:] if line.startswith("+++ b/") else None
        elif f and line.startswith("+"):
            out.setdefault(f, set()).add(line[1:])
    return out


def resolve(cd, rule_list):
    """Resolve the merge in progress in `cd` (HEAD main, MERGE_HEAD the branch) and stage it. Returns the renumbering
    as lines like "D-5 → D-6 (docs/DECISIONS.md)" ([] when no number collided), or None when it isn't only appended
    entries: then nothing is staged and the caller aborts the merge."""
    by_file = {r["file"]: r for r in rule_list}
    conflicted = gout(cd, "diff", "--name-only", "--diff-filter=U").splitlines()
    if not conflicted or any(f not in by_file for f in conflicted):
        return None
    staged, maps = {}, []  # maps: (rule, {old: new}, file)
    for f in conflicted:
        rule = by_file[f]
        stages = {int(s.split()[2]) for s in gout(cd, "ls-files", "-u", "--", f).splitlines()}
        if not {2, 3} <= stages or git(cd, "checkout", "--conflict=diff3", "--", f).returncode:
            return None
        try:
            parts = hunks((Path(cd) / f).read_text())
        except (OSError, UnicodeDecodeError):
            return None
        if parts is None:
            return None
        ours = gout(cd, "show", f":2:{f}")
        theirs = gout(cd, "show", f":3:{f}")
        base = gout(cd, "show", f":1:{f}") if 1 in stages else ""
        new = [n for h in parts if isinstance(h, tuple) for n in numbers(rule, "".join(h[2]))]
        if any(isinstance(h, tuple) and (h[1] or not whole_entries(rule, h[0]) or not whole_entries(rule, h[2]))
               for h in parts) or set(new) & set(numbers(rule, base)):
            return None
        taken = set(numbers(rule, ours)) | (set(numbers(rule, theirs)) - set(new))
        nxt, mapping = max(numbers(rule, ours), default=0) + 1, {}
        for n in new:
            if n < nxt or n in taken:
                while nxt in taken:
                    nxt += 1
                mapping[n] = nxt
                taken.add(nxt)
            nxt = max(nxt, mapping.get(n, n)) + 1
        staged[f] = parts
        maps.append((rule, mapping, f))
    added = added_lines(cd)
    main_lines = {}

    def rewrite(f, line, entry=False, theirs=False):
        """A merged line, renumbered when the branch added it (and main didn't have the same line)."""
        if not theirs:
            if line.rstrip("\n") not in added.get(f, ()):
                return line
            if f not in main_lines:
                main_lines[f] = set(gout(cd, "show", f"HEAD:{f}").splitlines())
            if line.rstrip("\n") in main_lines[f]:
                return line
        for rule, mapping, _ in maps:
            if mapping:
                line = renumber_line(line, mapping, rule, entry and rule is by_file.get(f))
        return line

    for f, parts in staged.items():
        out = []
        for h in parts:
            if not isinstance(h, tuple):
                out.append(rewrite(f, h))
                continue
            ours = list(h[0])
            if not ours[-1].endswith("\n"):
                ours[-1] += "\n"
            out += ours + [rewrite(f, l, entry=True, theirs=True) for l in h[2]]
        (Path(cd) / f).write_text("".join(out))
    if any(mapping for _, mapping, _ in maps):
        for f in added:
            path = Path(cd) / f
            if f in staged or not path.is_file():
                continue
            try:
                text = path.read_text()
            except (OSError, UnicodeDecodeError):
                continue
            new_text = "".join(rewrite(f, l) for l in text.splitlines(keepends=True))
            if new_text != text:
                path.write_text(new_text)
                git(cd, "add", "--", f)
    git(cd, "add", "--", *staged)
    if gout(cd, "diff", "--name-only", "--diff-filter=U"):
        return None
    return [f"{rule['ref'].replace('{n}', str(a))} → {rule['ref'].replace('{n}', str(b))} ({f})"
            for rule, mapping, f in maps for a, b in mapping.items()]
